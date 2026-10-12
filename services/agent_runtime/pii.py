"""
PII handling. Three rules:

1. The VAULT holds raw values captured during the call (card number, name, DOB,
   address, contact details, security answers). Only the journey engine and MCP
   tool calls read the vault.
2. Anything that leaves the runtime — UI events, logs, conversation history given
   and the conversation history — goes through Redactor first.
3. Secrets (security answer, memorable word) are never echoed, never logged, and
   any assistant sentence containing one is blocked by the output rail.

The Voice Live model hears the caller directly (in your Azure tenant); redaction protects
everything we emit and store.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from .normalizers import spoken_digits_to_text

_CARD = re.compile(r"\b\d(?:[\s-]?\d){10,18}\b")
_EMAIL = re.compile(r"\b[\w.%+-]+@[\w.-]+\.[a-z]{2,}\b", re.I)
_PHONE = re.compile(r"(?:\+44\s?7\d{3}|\b07\d{3})\s?\d{3}\s?\d{3}\b")
_POSTCODE = re.compile(r"\b[A-Z]{1,2}\d[A-Z\d]?\s?\d[A-Z]{2}\b", re.I)
_DATE = re.compile(r"\b(?:\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}|\d{4}-\d{2}-\d{2})\b")
_MONTHS = r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
_ORD = r"(?:\d{1,2}(?:st|nd|rd|th)?|first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth|eleventh|twelfth|thirteenth|fourteenth|fifteenth|sixteenth|seventeenth|eighteenth|nineteenth|twentieth|twenty[- ]\w+|thirtieth|thirty[- ]first)"
# Spoken dates of birth: "twelfth of April 1988", "April 12th, 1988", "12 April eighty-eight"
_SPOKEN_DATE = re.compile(rf"\b(?:(?:the )?{_ORD}(?: of)? {_MONTHS}|{_MONTHS} (?:the )?{_ORD})(?:,? (?:\d{{2,4}}|nineteen[- ]\w+(?:[- ]\w+)?|\w+[- ]\w+))?\b", re.I)

# Categories of vault keys → display label.
SECRET_KEYS = {"security_answer", "memorable_word"}
LABELS = {"card_number": "card", "full_name": "name", "dob": "date of birth", "address": "address",
          "email": "email", "mobile": "phone", "phone": "phone", "postcode": "postcode",
          "security_answer": "secret", "memorable_word": "secret"}


@dataclass
class Vault:
    values: dict[str, Any] = field(default_factory=dict)

    def put(self, key: str, value: Any) -> None:
        self.values[key] = value

    def get(self, key: str) -> Any:
        return self.values.get(key)

    def drop(self, key: str) -> None:
        self.values.pop(key, None)

    def sensitive_strings(self) -> list[tuple[str, str]]:
        out = []
        for k, v in self.values.items():
            if k in LABELS and isinstance(v, str) and len(v) >= 3:
                out.append((k, v))
                if k == "full_name":
                    out.extend((k, part) for part in v.split() if len(part) >= 3)
        return sorted(out, key=lambda kv: -len(kv[1]))

    def secrets(self) -> list[str]:
        return [str(v) for k, v in self.values.items() if k in SECRET_KEYS and v]


def _last4(s: str) -> str:
    return re.sub(r"\D", "", s)[-4:]


class Redactor:
    def __init__(self, vault: Vault) -> None:
        self.vault = vault

    def text(self, text: str | None, extra: dict[str, Any] | None = None) -> str:
        """Redact known vault values, `extra` values (just extracted, not yet in the vault) and PII patterns."""
        if not text:
            return text or ""
        out = text
        known = self.vault.sensitive_strings()
        if extra:
            known += Vault({k: v for k, v in extra.items() if k in LABELS}).sensitive_strings()
        for key, val in sorted(known, key=lambda kv: -len(kv[1])):
            out = re.sub(re.escape(val), f"⟨{LABELS[key]}⟩", out, flags=re.I)
        flat = spoken_digits_to_text(out) if re.search(r"\b(one|two|three|four|five|six|seven|eight|nine|zero|oh|double)\b", out, re.I) else out
        if flat != out and _CARD.search(flat):
            out = flat
        out = _CARD.sub(lambda m: f"⟨card ••{_last4(m.group())}⟩", out)
        out = _EMAIL.sub("⟨email⟩", out)
        out = _PHONE.sub("⟨phone⟩", out)
        out = _POSTCODE.sub("⟨postcode⟩", out)
        out = _DATE.sub("⟨date⟩", out)
        out = _SPOKEN_DATE.sub("⟨date⟩", out)
        return out

    def assistant_text(self, text: str) -> str:
        """Assistant speech only states system facts: mask vault values and card numbers, keep ordinary dates."""
        out = text or ""
        for key, val in self.vault.sensitive_strings():
            if key != "full_name":  # the verified first name is intentionally spoken
                out = re.sub(re.escape(val), f"⟨{LABELS[key]}⟩", out, flags=re.I)
        return _CARD.sub(lambda m: f"⟨card ••{_last4(m.group())}⟩", out)

    def value(self, key: str, value: Any) -> Any:
        if value is None:
            return None
        if key in SECRET_KEYS:
            return "⟨secret⟩"
        if key == "card_number":
            return f"⟨card ••{str(value)[-4:]}⟩"
        if key in LABELS:
            return f"⟨{LABELS[key]}⟩"
        if isinstance(value, str):
            return self.text(value)
        return value

    def mapping(self, data: dict[str, Any] | None) -> dict[str, Any]:
        if not data:
            return {}
        return {k: self.value(k, v) if not isinstance(v, (dict, list)) else self.deep(v) for k, v in data.items()}

    def deep(self, obj: Any) -> Any:
        if isinstance(obj, dict):
            return {k: (self.value(k, v) if not isinstance(v, (dict, list)) else self.deep(v)) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self.deep(v) for v in obj]
        if isinstance(obj, str):
            return self.text(obj)
        return obj


def contains_any(text: str, needles: Iterable[str]) -> list[str]:
    low = re.sub(r"[^a-z0-9 ]", " ", text.lower())
    hits = []
    for n in needles:
        n2 = re.sub(r"[^a-z0-9 ]", " ", str(n).lower()).strip()
        if n2 and re.search(rf"\b{re.escape(n2)}\b", low):
            hits.append(n)
    return hits
