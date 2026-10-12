"""
Deterministic normalisation and validation of customer answers.

The coordinator (Voice Live model) proposes a value; this module disposes. Nothing reaches a tool unless it
passes here. Customers answer naturally ("twelve oh four eighty eight", "S E one seven P B", "double seven",
"yeah go on then"), so the parsing lives in code and the assistant never has to tell anyone how to answer.

Finders read values straight from the transcript (what the customer actually said); `_validate` in the
Orchestrator prefers them over the model's typed value and falls back to the model when they find nothing.
Each finder returns a Found: a complete value, or `incomplete=True` with the partial text when the customer has
only said part of it (7 of 11 card digits, "SE1" without the rest). Incomplete is not wrong: nothing is
verified and no retry is used.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Any, Optional

from .config import settings

# ---------------------------------------------------------------------------- spoken numbers
_UNITS = {"zero": 0, "nought": 0, "oh": 0, "o": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
          "seven": 7, "eight": 8, "nine": 9}
_TEENS = {"ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
          "seventeen": 17, "eighteen": 18, "nineteen": 19}
_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fourty": 40, "fifty": 50, "sixty": 60, "seventy": 70,
         "eighty": 80, "ninety": 90}
_ORDINALS = {"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6, "seventh": 7, "eighth": 8,
             "ninth": 9, "tenth": 10, "eleventh": 11, "twelfth": 12, "thirteenth": 13, "fourteenth": 14,
             "fifteenth": 15, "sixteenth": 16, "seventeenth": 17, "eighteenth": 18, "nineteenth": 19,
             "twentieth": 20, "thirtieth": 30}
_MULT = {"double": 2, "triple": 3}
_DIGIT_WORDS = {k: str(v) for k, v in _UNITS.items()}


def _tokens(text: str) -> list[str]:
    return re.findall(r"[A-Za-z]+|\d+|[^\sA-Za-z\d]", (text or "").replace("-", " "))


def spoken_digits_to_text(text: str) -> str:
    """Digit-by-digit speech → digits: 'nine eight double two oh six' → '982206'. Other words are kept."""
    tokens = _tokens(text)
    out: list[str] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i].lower()
        if tok in _MULT and i + 1 < len(tokens):
            nxt = tokens[i + 1].lower()
            d = _DIGIT_WORDS.get(nxt) if nxt != "o" else "0"
            if d is None and nxt.isdigit() and len(nxt) == 1:
                d = nxt
            if d is not None:
                out.append(d * _MULT[tok])
                i += 2
                continue
        if tok in _DIGIT_WORDS and tok not in ("o", "oh"):
            out.append(_DIGIT_WORDS[tok])
        elif tok in ("o", "oh") and ((out and out[-1].isdigit()) or (i + 1 < len(tokens) and (
                tokens[i + 1].isdigit() or tokens[i + 1].lower() in _DIGIT_WORDS or tokens[i + 1].lower() in _MULT))):
            out.append("0")
        else:
            out.append(" " + tokens[i] + " ")
        i += 1
    return re.sub(r"\s+", " ", "".join(out)).strip()


def words_to_numbers(text: str) -> str:
    """Number words → numerals for dates: 'twelfth of April nineteen eighty eight' → '12 of April 19 88'.
    'oh four' → '04' (spoken day/month), compound tens ('twenty first' → 21)."""
    tokens = _tokens(text.lower())
    out: list[str] = []
    i = 0
    while i < len(tokens):
        t = tokens[i]
        nxt = tokens[i + 1] if i + 1 < len(tokens) else ""
        if t in ("oh", "o") and (nxt in _UNITS or nxt.isdigit()) and nxt not in ("oh", "o"):
            out.append("0" + str(_UNITS.get(nxt, nxt)))
            i += 2
            continue
        if t in _TENS:
            val = _TENS[t]
            if nxt in _UNITS and _UNITS[nxt] > 0:
                val += _UNITS[nxt]
                i += 1
            elif nxt in _ORDINALS and _ORDINALS[nxt] < 10:
                val += _ORDINALS[nxt]
                i += 1
            out.append(str(val))
        elif t in _TEENS:
            out.append(str(_TEENS[t]))
        elif t in _ORDINALS:
            out.append(str(_ORDINALS[t]))
        elif t in _UNITS and t not in ("o", "oh"):
            out.append(str(_UNITS[t]))
        elif t == "thousand" and out and out[-1].isdigit():
            out[-1] = str(int(out[-1]) * 1000)
            if nxt == "and":
                i += 1
            if i + 1 < len(tokens) and (tokens[i + 1] in _UNITS or tokens[i + 1] in _TEENS or tokens[i + 1] in _TENS):
                rest = words_to_numbers(" ".join(tokens[i + 1:i + 3])).split()
                if rest and rest[0].isdigit():
                    out[-1] = str(int(out[-1]) + int(rest[0]))
                    i += 1 + (1 if len(tokens) > i + 2 and tokens[i + 2] in _UNITS and tokens[i + 1] in _TENS else 0)
        elif t in ("st", "nd", "rd", "th") and out and out[-1].isdigit():
            pass                                   # "12th" was tokenised as "12" + "th"
        else:
            out.append(t)
        i += 1
    return " ".join(out)


# ---------------------------------------------------------------------------- results
@dataclass
class Found:
    value: Any = None
    incomplete: bool = False
    partial: str = ""          # what was heard so far (kept to join with the rest of the answer)
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.value is not None


NOTHING = Found()


# Hesitations while reading a number don't end it ("one two... sorry... three four"); "no" does (a correction).
_HESITATION = re.compile(r"\b(?:uh+|um+|erm*|hmm+|ah+|sorry|hang on|wait|let me see|like|it'?s|it is)\b|\.{2,}|…", re.I)


def _digit_runs(text: str) -> list[str]:
    """Runs of digits as spoken ('+44 7700 900-111' → ['447700900111'])."""
    flat = spoken_digits_to_text(_HESITATION.sub(" ", text))
    # groups read with pauses ("1234, 5678, 901") are one number
    return [re.sub(r"\D", "", m.group()) for m in re.finditer(r"\+?\d(?:[\d\s,-]*\d)?", flat)]


# ---------------------------------------------------------------------------- card / order numbers
def normalise_card(value: Any) -> Optional[str]:
    digits = re.sub(r"\D", "", spoken_digits_to_text(str(value or "")))
    suffix_len = settings.card_length - len(settings.card_prefix)
    if len(digits) == suffix_len:
        return settings.card_prefix + digits
    if len(digits) == settings.card_length and digits.startswith(settings.card_prefix):
        return digits
    return None


def find_card(text: str) -> Found:
    """Last complete card number in the utterance (so 'no sorry, it's …' corrections win)."""
    runs = _digit_runs(text)
    for digits in reversed(runs):
        card = normalise_card(digits)
        if card:
            return Found(card)
    longest = max(runs, key=len, default="")
    if 2 <= len(longest) < settings.card_length and normalise_card(longest) is None:
        return Found(incomplete=True, partial=longest, reason="only part of the card number was heard")
    return NOTHING


def find_card_number(text: str) -> Optional[str]:
    return find_card(text).value


def normalise_order_number(value: Any) -> Optional[str]:
    digits = re.sub(r"\D", "", spoken_digits_to_text(str(value or "")))
    return digits if len(digits) == settings.order_number_length else None


def find_order(text: str) -> Found:
    runs = _digit_runs(text)
    for digits in reversed(runs):
        if len(digits) == settings.order_number_length:
            return Found(digits)
    longest = max(runs, key=len, default="")
    if 2 <= len(longest) < settings.order_number_length:
        return Found(incomplete=True, partial=longest, reason="only part of the order number was heard")
    return NOTHING


def find_order_number(text: str) -> Optional[str]:
    return find_order(text).value


# ---------------------------------------------------------------------------- phone numbers
def normalise_uk_phone(value: Any) -> Optional[str]:
    digits = re.sub(r"\D", "", spoken_digits_to_text(str(value or "").replace("+", " ")))
    if digits.startswith("44"):
        digits = "0" + digits[2:]
    return digits if re.fullmatch(r"0\d{9,10}", digits) else None


def normalise_uk_mobile(value: Any) -> Optional[str]:
    v = normalise_uk_phone(value)
    return v if v and re.fullmatch(r"07\d{9}", v) else None


def find_phone(text: str) -> Found:
    flat = spoken_digits_to_text(text.replace("+", " plus "))
    runs = [re.sub(r"\D", "", m.group()) for m in re.finditer(r"\d(?:[\d\s,-]*\d)?", flat)]
    for digits in reversed(runs):
        v = normalise_uk_phone(digits)
        if v:
            return Found(v)
    longest = max(runs, key=len, default="")
    if 3 <= len(longest) < 11:
        return Found(incomplete=True, partial=longest, reason="only part of the phone number was heard")
    return NOTHING


def find_mobile(text: str) -> Found:
    f = find_phone(text)
    if f.ok and not f.value.startswith("07"):
        return Found(reason="not a UK mobile number")
    return f


# ---------------------------------------------------------------------------- postcodes
_POSTCODE = re.compile(r"[A-Z]{1,2}\d[A-Z\d]?\d[A-Z]{2}")
_OUTWARD = re.compile(r"[A-Z]{1,2}\d[A-Z\d]?")
_NATO = {"alpha": "A", "alfa": "A", "bravo": "B", "charlie": "C", "delta": "D", "echo": "E", "foxtrot": "F",
         "golf": "G", "hotel": "H", "india": "I", "juliet": "J", "juliett": "J", "kilo": "K", "lima": "L", "mike": "M",
         "november": "N", "oscar": "O", "papa": "P", "quebec": "Q", "romeo": "R", "sierra": "S", "tango": "T",
         "uniform": "U", "victor": "V", "whiskey": "W", "whisky": "W", "xray": "X", "yankee": "Y", "zulu": "Z"}
# Short words that are never postcode letters when said on their own ("it's SE1", "is", "my", "of").
_FILLER = {"is", "it", "it's", "its", "my", "of", "to", "in", "on", "at", "or", "an", "as", "we", "he", "do", "go",
           "by", "up", "if", "um", "uh", "er", "ok", "oh", "a", "i", "i'm", "be", "no"}
_PC_PART = re.compile(r"[A-Za-z]{1,2}\d[A-Za-z\d]?|\d[A-Za-z]{2}|[A-Za-z]{1,2}\d[A-Za-z\d]?\d[A-Za-z]{2}")


def _postcode_chars(text: str) -> list[str]:
    """Postcode-ish tokens: 'S as in Sierra E one seven P B' → ['S','E','1','7','P','B']. Real words break a run;
    commas and full stops (pauses) don't."""
    raw = re.findall(r"[A-Za-z']+|\d+|[,.;]", text.replace("-", " "))
    out: list[str] = []
    i = 0
    while i < len(raw):
        t, low = raw[i], raw[i].lower()
        if t in ",.;":
            i += 1
            continue
        if i + 3 < len(raw) and raw[i + 1].lower() == "as" and raw[i + 2].lower() == "in" and len(t) == 1:
            out.append(t.upper())          # "S as in Sierra"
            i += 4
            continue
        if low in _NATO:
            out.append(_NATO[low])
        elif low in _UNITS and low not in ("o", "oh"):
            out.append(str(_UNITS[low]))
        elif low in _MULT and i + 1 < len(raw):
            nxt = raw[i + 1]
            ch = str(_UNITS[nxt.lower()]) if nxt.lower() in _UNITS else nxt.upper()
            out.append(ch * _MULT[low])
            i += 2
            continue
        elif t.isdigit() and len(t) <= 2:
            out.append(t)
        elif t.isalpha() and len(t) <= 2 and low not in _FILLER:
            out.append(t.upper())          # spelled letters: "S", "E", "PB"
        elif _PC_PART.fullmatch(t):
            out.append(t.upper())          # "SE1", "7PB", "SE17PB"
        else:
            out.append("|")                # a real word: postcode runs don't cross it
        i += 1
    return out


def normalise_postcode(value: Any) -> Optional[str]:
    v = re.sub(r"[^A-Z0-9]", "", str(value or "").upper())
    if not _POSTCODE.fullmatch(v):
        return None
    return f"{v[:-3]} {v[-3:]}"


def find_postcode(text: str) -> Found:
    chars = _postcode_chars(text)
    best: Optional[str] = None
    best_outward: Optional[str] = None
    n = len(chars)
    for j in range(n):
        for i in range(j, -1, -1):
            if chars[i] == "|" or chars[j] == "|":
                break
            cand = "".join(chars[i:j + 1])
            if len(cand) > 8:
                break
            if _POSTCODE.fullmatch(cand):
                best = cand                        # keep the last (and, from that end, the longest) match
            elif _OUTWARD.fullmatch(cand) and j == n - 1 or (_OUTWARD.fullmatch(cand) and j + 1 < n and chars[j + 1] == "|"):
                best_outward = cand
    if best:
        return Found(f"{best[:-3]} {best[-3:]}")
    if best_outward:
        return Found(incomplete=True, partial=best_outward, reason="only the first part of the postcode was heard")
    return NOTHING


# ---------------------------------------------------------------------------- dates
_MONTHS = {m: i for i, m in enumerate(["january", "february", "march", "april", "may", "june", "july", "august",
                                        "september", "october", "november", "december"], 1)}
_MONTHS.update({m[:3]: i for m, i in list(_MONTHS.items())})
_MONTHS["sept"] = 9


def _year(y: str, *, dob: bool) -> Optional[int]:
    v = int(y)
    if len(y) == 4:
        return v
    if len(y) == 2:
        if not dob:
            return 2000 + v
        # Two-digit birth years: callers are adults, so '05' is 2005 only if that's at least 16 years ago.
        return 2000 + v if 2000 + v <= date.today().year - 16 else 1900 + v
    return None


def _mk(y: Optional[int], m: int, d: int) -> Optional[str]:
    try:
        return date(y, m, d).isoformat() if y else None
    except (ValueError, TypeError):
        return None


def find_date(text: str, *, dob: bool = False) -> Found:
    """Natural UK dates (day first) → ISO. For dates of birth, two-digit years pivot and only past dates count."""
    t = words_to_numbers(text)
    # join spoken years: '19 88' → '1988', '20 05' → '2005'
    t = re.sub(r"\b(19|20) (\d{2})\b", r"\1\2", t)
    found: list[str] = []
    partial = ""
    # 1) ISO yyyy-mm-dd
    for m in re.finditer(r"\b(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})\b", text):
        v = _mk(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        if v:
            found.append(v)
    # 2) day monthname [year] / monthname day [year]
    mon = r"(" + "|".join(sorted(_MONTHS, key=len, reverse=True)) + r")"
    for m in re.finditer(r"\b(\d{1,2})(?:\s+of)?\s+" + mon + r"\b(?:\s*,?\s*(\d{4}|\d{2}))?", t):
        y = _year(m.group(3), dob=dob) if m.group(3) else None
        v = _mk(y, _MONTHS[m.group(2)], int(m.group(1)))
        if v:
            found.append(v)
        elif not m.group(3):
            partial = m.group(0)
    for m in re.finditer(r"\b" + mon + r"\s+(?:the\s+)?(\d{1,2})\b(?:\s*,?\s*(\d{4}|\d{2}))?", t):
        y = _year(m.group(3), dob=dob) if m.group(3) else None
        v = _mk(y, _MONTHS[m.group(1)], int(m.group(2)))
        if v:
            found.append(v)
        elif not m.group(3):
            partial = partial or m.group(0)
    # 3) numeric day-first: 12/04/88, 12-4-1988, 12 04 88, 12.04.1988
    for m in re.finditer(r"\b(\d{1,2})\s*[/.\- ]\s*(\d{1,2})\s*[/.\- ]\s*(\d{4}|\d{2})\b", t):
        v = _mk(_year(m.group(3), dob=dob), int(m.group(2)), int(m.group(1)))
        if v:
            found.append(v)
    # 4) compact ddmmyyyy / ddmmyy
    for m in re.finditer(r"\b(\d{2})(\d{2})(\d{4}|\d{2})\b", t):
        v = _mk(_year(m.group(3), dob=dob), int(m.group(2)), int(m.group(1)))
        if v:
            found.append(v)
    if not found and not dob:
        # "in March" / "back in March 2025": the first of that month (the most recent one if no year was said)
        for m in re.finditer(r"\b" + mon + r"\b(?:\s+(\d{4}))?", t):
            month, year = _MONTHS[m.group(1)], int(m.group(2)) if m.group(2) else None
            if year is None:
                today = date.today()
                year = today.year if month <= today.month else today.year - 1
            v = _mk(year, month, 1)
            if v:
                found.append(v)
                partial = ""
    for v in reversed(found):
        if not dob or v < date.today().isoformat():
            return Found(v)
    if found:
        return Found(reason="that date of birth is in the future")
    if partial:
        return Found(incomplete=True, partial=partial, reason="the year wasn't heard")
    return NOTHING


def find_dob(text: str) -> Found:
    return find_date(text, dob=True)


def normalise_date(value: Any, *, past_only: bool = False) -> Optional[str]:
    s = str(value or "").strip()
    try:
        d = date.fromisoformat(s[:10])
    except (ValueError, TypeError):
        f = find_date(s, dob=past_only)
        if not f.ok:
            return None
        d = date.fromisoformat(f.value)
    if d.year < 1900:
        return None
    if past_only and d >= date.today():
        return None
    return d.isoformat()


# ---------------------------------------------------------------------------- yes / no
_YES = [r"\byes\b", r"\byeah\b", r"\byep\b", r"\byup\b", r"\baye\b", r"\bsure\b", r"\bcorrect\b", r"\bright\b",
        r"\bgo on\b", r"\bgo ahead\b", r"\bplease do\b", r"\bok(ay)?\b", r"\babsolutely\b", r"\bdefinitely\b",
        r"\bof course\b", r"\buh[- ]?huh\b", r"\bmm[- ]?hmm\b", r"\btrue\b", r"\bthat'?s it\b", r"\bplease\b",
        r"\byes please\b", r"\bi do\b", r"\bi am\b", r"\bi would\b", r"\bi have\b"]
_NO = [r"\bno\b", r"\bnope\b", r"\bnah\b", r"\bnot really\b", r"\bno thanks?\b", r"\bno thank you\b",
       r"\bdon'?t\b", r"\bdo not\b", r"\bnegative\b", r"\bfalse\b", r"\bincorrect\b", r"\bwrong\b",
       r"\bnot right\b", r"\bisn'?t right\b", r"\bnot correct\b", r"\bi'?m not\b", r"\bnot at all\b",
       r"\bi haven'?t\b", r"\bi wouldn'?t\b", r"\bthat'?s (all|everything)\b", r"\bnothing else\b"]
_UNCLEAR = {"unclear", "unsure", "not sure", "contradictory", "maybe", "dunno", "don't know", "i don't know"}


def normalise_bool(value: Any) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    v = re.sub(r"[^\w' -]", " ", str(value or "").strip().lower())
    v = re.sub(r"\s+", " ", v).strip()
    if not v or v in _UNCLEAR:
        return None
    neg = [p for p in _NO if re.search(p, v)]
    pos = [p for p in _YES if re.search(p, v)]
    # "not right" / "isn't correct" contain a yes-word: negations win over the word they negate
    if any(re.search(p, v) for p in (r"\bnot (right|correct)\b", r"\bisn'?t (right|correct)\b")):
        return False
    if neg and not pos:
        return False
    if pos and not neg:
        return True
    if pos and neg:   # "no, that's right" / "yes, no problem": the first word decides; otherwise unclear
        first = v.split()[0]
        if first in ("yes", "yeah", "yep", "yup", "correct", "right"):
            return True
        if first in ("no", "nope", "nah"):
            return False
    return None


def normalise_int(value: Any) -> Optional[int]:
    try:
        return int(str(value).replace(",", "").strip())
    except (ValueError, TypeError):
        nums = re.findall(r"\d+", words_to_numbers(str(value or "")).replace(",", ""))
        return int(nums[0]) if len(nums) == 1 else None


# ---------------------------------------------------------------------------- other kinds
def normalise_email(value: Any) -> Optional[str]:
    v = str(value or "").strip().lower().replace(" at ", "@").replace(" dot ", ".").replace(" ", "")
    return v if re.fullmatch(r"[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}", v) else None


def normalise_text(value: Any) -> Optional[str]:
    v = re.sub(r"\s+", " ", str(value or "")).strip()
    return v or None


def join_spelled(value: str) -> str:
    """'F-A-L-C-O-N' / 'f a l c o n' → 'falcon' (single letters only; real words untouched)."""
    parts = re.split(r"[\s.-]+", value.strip())
    if len(parts) >= 3 and all(len(p) == 1 and p.isalpha() for p in parts):
        return "".join(parts)
    return value


# Transcript finders by slot kind (used before the model's typed value).
FINDERS = {"card_number": find_card, "order_number": find_order, "dob": find_dob, "date": find_date, "postcode": find_postcode,
           "phone": find_phone, "mobile": find_mobile}


def normalise(kind: str, value: Any, choices: Optional[list[str]] = None) -> tuple[Any, Optional[str]]:
    """Returns (normalised_value, invalid_reason). invalid_reason is None when valid."""
    if value is None:
        return None, None
    if kind == "card_number":
        v = normalise_card(value)
        return (v, None) if v else (None, "that card number didn't look complete")
    if kind == "dob":
        v = normalise_date(value, past_only=True)
        return (v, None) if v else (None, "that date of birth wasn't recognised")
    if kind == "date":
        v = normalise_date(value)
        return (v, None) if v else (None, "that date wasn't recognised")
    if kind == "email":
        v = normalise_email(value)
        return (v, None) if v else (None, "that email address wasn't recognised")
    if kind == "mobile":
        v = normalise_uk_mobile(value)
        return (v, None) if v else (None, "that mobile number wasn't recognised")
    if kind == "order_number":
        v = normalise_order_number(value)
        return (v, None) if v else (None, "that order number didn't look complete")
    if kind == "phone":
        v = normalise_uk_phone(value)
        return (v, None) if v else (None, "that phone number wasn't recognised")
    if kind == "postcode":
        v = normalise_postcode(value) or find_postcode(str(value)).value
        return (v, None) if v else (None, "that postcode wasn't recognised")
    if kind == "yes_no":
        v = normalise_bool(value)
        return (v, None) if v is not None else (None, "the answer wasn't a clear yes or no")
    if kind == "integer":
        v = normalise_int(value)
        return (v, None) if v is not None else (None, "that number wasn't understood")
    if kind == "choice":
        v = str(value)
        return (v, None) if choices and v in choices else (None, "that didn't match one of the options")
    if kind == "secret":
        v = normalise_text(join_spelled(str(value)))
        return (v, None) if v else (None, "the answer was empty")
    # text, name, address
    v = normalise_text(value)
    return (v, None) if v else (None, "the answer was empty")
