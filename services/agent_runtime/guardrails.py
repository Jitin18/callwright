"""
Guardrails.

INPUT (every customer utterance, before the model may reply when GUARD_BEFORE_RESPONSE=true)
  1. rules          – deterministic injection / exfiltration patterns (<1 ms), always on
  2. topic          – off-topic questions (news, politics, public figures, weather, general knowledge…):
                      a local embedding classifier (topic.py), always on once loaded
  3. prompt_shield  – Azure AI Content Safety Prompt Shields (jailbreak / injection)   [if configured]
  4. content        – Azure AI Content Safety text analysis (hate, sexual, violence,
                      self-harm)                                                      [if configured]

OUTPUT (the model's transcript, checked sentence by sentence while it streams)
  1. secret_leak    – a sentence containing the caller's security answer / memorable word
  2. pii_echo       – the caller's date of birth, postcode, full phone number or address read back
  3. card_digits    – any 11–19 digit run (only last 4 may ever be spoken)
  4. grounding      – every number must appear in a tool result or in the customer's words
  5. commanding     – telling the customer how to answer ("say yes or no", "digits only", a date format…)
  6. goodbye_after_transfer – once the call is being transferred, no goodbye
  A violation cancels the response, clears playback, and asks the model to rephrase.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from typing import Optional

import httpx

from .azure_auth import AzureAuth
from .config import settings
from .pii import contains_any
from .session import Session
from .topic import topic_guard

_INJECTION = [
    r"ignore (all |any |the |your |my )?(previous |prior |above |earlier |system )?(instructions|rules|prompts?|guidelines)",
    r"(someone|somebody|anyone) else'?s? (account|balance|details|card|points|order)",
    r"another (customer|person|member)'?s? (account|balance|details|card|points|order)",
    r"(reveal|show|print|repeat) (me )?(your|the) (system )?(prompt|instructions|rules)",
    r"\byou are now\b", r"\bdeveloper mode\b", r"\bjailbreak\b", r"pretend (to be|you are)",
    r"\bact as (an?|the) (?!customer)", r"disregard (your|the) (rules|instructions|guidelines)",
    r"skip (the )?(security|verification|id ?& ?v)", r"without (the )?(security|verification) (check|questions)",
]
_INJECTION_RE = re.compile("|".join(_INJECTION), re.I)
_COMMANDING = re.compile(
    r"\b(say|answer|respond with|reply with|just say)\s+(?:either\s+)?[\"']?yes[\"']?\s+or\s+[\"']?no\b|"
    r"\byes or no\b|\bdigits only\b|\bjust the (?:\d+ )?(?:digits|numbers)\b|\bonly the (?:digits|numbers)\b|"
    r"\bin the format\b|\bdd/mm|\bday,? month,? (?:and )?year\b|\bas it appears\b|\bexactly as (?:it|shown)\b|"
    r"\b19[- ]digit\b|\bstarting with 6001|\bletter by letter\b|\bone digit at a time\b|\bthe last 11 digits\b|"
    r"\bfull (?:card )?number or (?:just )?(?:the )?last\b|\bor just the last (?:part|few|digits|11)\b",
    re.I)
_GOODBYE = re.compile(r"\b(good ?bye|bye[- ]?bye|bye for now|have a (?:lovely|great|good|nice) (?:day|evening|afternoon))\b", re.I)
_MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
           "november", "december"]
_NUM = re.compile(r"\d[\d,]*(?:\.\d+)?")
_LONG_DIGITS = re.compile(r"\b\d(?:[\s-]?\d){10,18}\b")


@dataclass
class Verdict:
    stage: str
    name: str
    status: str            # pass | flag | block | timeout | error | skipped
    ms: int
    detail: Optional[str] = None


@dataclass
class InputResult:
    verdicts: list[Verdict] = field(default_factory=list)

    @property
    def blocked(self) -> Optional[Verdict]:
        return next((v for v in self.verdicts if v.status == "block"), None)


class Guardrails:
    def __init__(self) -> None:
        cs = settings.content_safety
        self._cs = cs
        self._auth = AzureAuth(cs.auth_scope, cs.api_key, key_header="Ocp-Apim-Subscription-Key") \
            if cs.configured else None
        self._http = httpx.AsyncClient(timeout=settings.guardrail_timeout_s)

    async def close(self) -> None:
        await self._http.aclose()
        if self._auth:
            await self._auth.close()

    # ---------------------------------------------------------------- input
    async def check_input(self, s: Session, text: str) -> InputResult:
        res = InputResult()
        t0 = time.perf_counter()
        m = _INJECTION_RE.search(text)
        res.verdicts.append(Verdict("input", "rules", "block" if m else "pass", _ms(t0),
                                    "instruction-override pattern" if m else None))
        self._emit(s, res.verdicts[-1])
        if not m:
            t1 = time.perf_counter()
            tv = await asyncio.to_thread(topic_guard.check, text, s.pending_run() is not None)
            res.verdicts.append(Verdict("input", "topic", "block" if tv.off_topic else ("skipped" if tv.skipped else "pass"),
                                        _ms(t1), f"off topic (like: {tv.nearest!r}, {tv.score_out})" if tv.off_topic
                                        else tv.skipped or None))
            self._emit(s, res.verdicts[-1])
            if tv.off_topic:
                return res
        if not self._auth:
            v = Verdict("input", "content_safety", "skipped", 0, "Azure AI Content Safety not configured")
            res.verdicts.append(v)
            self._emit(s, v)
            return res
        for coro in asyncio.as_completed([self._timed("prompt_shield", self._prompt_shield(text)),
                                          self._timed("content", self._content(text))]):
            v = await coro
            res.verdicts.append(v)
            self._emit(s, v)
        return res

    async def _post(self, path: str, body: dict) -> dict:
        url = f"{self._cs.endpoint.rstrip('/')}/contentsafety/{path}?api-version={self._cs.api_version}"
        r = await self._http.post(url, json=body, headers=await self._auth.headers())
        r.raise_for_status()
        return r.json()

    async def _prompt_shield(self, text: str) -> tuple[str, Optional[str]]:
        data = await self._post("text:shieldPrompt", {"userPrompt": text, "documents": []})
        attack = data.get("userPromptAnalysis", {}).get("attackDetected")
        return ("block", "prompt attack detected") if attack else ("pass", None)

    async def _content(self, text: str) -> tuple[str, Optional[str]]:
        data = await self._post("text:analyze", {"text": text, "categories": ["Hate", "Sexual", "Violence", "SelfHarm"],
                                                 "outputType": "FourSeverityLevels"})
        worst = [c for c in data.get("categoriesAnalysis", []) if c.get("severity", 0) >= self._cs.block_severity]
        if not worst:
            return "pass", None
        cats = ", ".join(c["category"] for c in worst)
        # Self-harm is flagged for a human, not silently refused.
        return ("flag" if all(c["category"] == "SelfHarm" for c in worst) else "block"), cats

    async def _timed(self, name: str, coro) -> Verdict:
        t0 = time.perf_counter()
        try:
            status, detail = await asyncio.wait_for(coro, timeout=settings.guardrail_timeout_s)
            return Verdict("input", name, status, _ms(t0), detail)
        except asyncio.TimeoutError:
            return Verdict("input", name, "block" if settings.guardrail_fail_mode == "closed" else "timeout",
                           _ms(t0), "check timed out")
        except Exception as e:  # noqa: BLE001 — a safety-service outage must never crash the call
            return Verdict("input", name, "block" if settings.guardrail_fail_mode == "closed" else "error",
                           _ms(t0), f"{type(e).__name__}: {str(e)[:120]}")

    # ---------------------------------------------------------------- output
    def check_sentence(self, s: Session, sentence: str) -> list[str]:
        """Returns violations for one sentence of the model's speech (empty list = fine)."""
        t0 = time.perf_counter()
        violations: list[str] = []
        if contains_any(sentence, s.vault.secrets()):
            violations.append("never repeat the caller's security answer or memorable word")
            self._emit(s, Verdict("output", "secret_leak", "block", _ms(t0)))
        echoed = _pii_echo(s, sentence)
        if echoed:
            violations.append(f"never read back the caller's {echoed}")
            self._emit(s, Verdict("output", "pii_echo", "block", _ms(t0), echoed))
        if _COMMANDING.search(sentence):
            violations.append("never tell the customer how to answer or what format to use; ask naturally")
            self._emit(s, Verdict("output", "commanding", "block", _ms(t0), _COMMANDING.search(sentence).group(0)))
        if s.transferred and _GOODBYE.search(sentence):
            violations.append("the call is being transferred to a colleague: don't say goodbye, just ask them to "
                              "stay on the line")
            self._emit(s, Verdict("output", "goodbye_after_transfer", "block", _ms(t0)))
        if _LONG_DIGITS.search(sentence):
            violations.append("refer to card numbers only by their last 4 digits")
            self._emit(s, Verdict("output", "card_digits", "block", _ms(t0)))
        stray = [n for n in numbers(sentence) if n not in s.allowed_numbers and not _benign(n)]
        if stray:
            violations.append("only use numbers that appear in tool results: remove "
                              + ", ".join(_fmt(n) for n in stray))
            self._emit(s, Verdict("output", "grounding", "block", _ms(t0), f"ungrounded {stray}"))
        if not violations:
            self._emit(s, Verdict("output", "sentence", "pass", _ms(t0)))
        return violations

    @staticmethod
    def _emit(s: Session, v: Verdict) -> None:
        s.bus.emit("guardrail", stage=v.stage, name=v.name, status=v.status, ms=v.ms, detail=v.detail)


def _pii_echo(s: Session, sentence: str) -> str | None:
    """The caller's own date of birth, postcode, phone number or address must never be read back to them."""
    low = sentence.lower()
    flat = re.sub(r"[^a-z0-9]", "", low)
    dob = s.vault.get("dob")
    if dob:
        y, m, d = dob.split("-")
        if y in low and _MONTHS[int(m) - 1] in low:
            return "date of birth"
        if re.search(rf"\b0?{int(d)}[/.-]0?{int(m)}[/.-](?:{y}|{y[2:]})\b", low):
            return "date of birth"
    pc = s.vault.get("postcode") or ""
    addr = s.vault.get("address") or ""
    for code in filter(None, [pc] + re.findall(r"[A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2}", addr.upper())):
        if re.sub(r"[^a-z0-9]", "", code.lower()) in flat:
            return "postcode"
    for key in ("phone", "mobile"):
        digits = re.sub(r"\D", "", str(s.vault.get(key) or ""))
        if len(digits) >= 10 and digits in re.sub(r"\D", "", sentence):
            return "phone number"
    street = re.match(r"\s*(\d+\s+[A-Za-z]+(?:\s+[A-Za-z]+)?)", addr)
    if street and street.group(1).lower() in low:
        return "address"
    return None


def numbers(text: str) -> list[float]:
    out = []
    for m in _NUM.findall(text):
        try:
            out.append(float(m.replace(",", "")))
        except ValueError:
            pass
    return out


def allowed_numbers(*sources) -> set[float]:
    """Every number (and every digit group, so ISO dates count) in the given data."""
    out: set[float] = set()
    for src in sources:
        text = src if isinstance(src, str) else json.dumps(src, default=str)
        out.update(numbers(text))
        out.update(float(int(p)) for p in re.findall(r"\d+", text))
    return out


def _benign(n: float) -> bool:
    return n <= 12 or (1990 <= n <= 2100 and n == int(n))


def _fmt(n: float) -> str:
    return str(int(n)) if n == int(n) else str(n)


def _ms(t0: float) -> int:
    return round((time.perf_counter() - t0) * 1000)
