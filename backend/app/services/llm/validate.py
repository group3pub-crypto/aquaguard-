"""Deterministic validation of model output. Returns a violation code, or None when acceptable."""

from __future__ import annotations

import re
import unicodedata

from app.services.llm.facts import Facts

MAX_WORDS = 80  # prompt asks for <=60; a little slack before we reject
MAX_CHARS = 600
_NUM = re.compile(r"(?<![\w.])\d+(?:[.,]\d+)?")
_URL = re.compile(
    r"(https?:|www\.|ftp:|\b[\w-]+\.(com|in|org|net|io|co|app|ly|me|xyz)\b|@\w)", re.I
)
_MARKDOWN = re.compile(
    r"(\*|_{1,2}\w|`|^#|\n#|\[[^\]]*\]\(|^\s*[-+>]\s|\n\s*[-+>]\s|\||~~|<[^>]+>)"
)
_PHONE = re.compile(r"(\+\d[\d\s\-()]{6,}|\b\d{3,}[\s\-]\d{3,}[\s\-]?\d*\b|\(\d{2,}\)\s*\d{3,})")
_GUARANTEE = re.compile(
    r"\b(guarantee[sd]?|definitely|certainly|no (further )?(damage|risk|harm)|100\s*%|"
    r"will not (leak|flood))\b",
    re.I,
)
# Claims that the valve was acted on; only allowed when facts.valve_state_confirmed is true.
_VALVE_CLAIM = re.compile(
    r"(shut\s*(off|down)|shut-off|turned off|switched off|closed|cut off|stopped)\W+(\w+\W+){0,4}?"
    r"(automatically|already|successfully)|"
    r"(has|have|was|were|is|been)\s+(been\s+)?"
    r"(shut|closed|turned off|switched off|cut off|isolated)|"
    r"we\s+(have\s+)?(shut|closed|turned off|cut|isolated)|"
    r"(water|supply|valve)\b[^.]{0,30}\b(is|has been|was)\s+(now\s+)?(off|closed|shut|cut)|"
    r"(बंद कर दिया|बंद हो गया|बंद की गई|काट दिया)",
    re.I,
)


def _digits(text: str) -> str:
    out = []
    for ch in text:
        out.append(str(unicodedata.digit(ch)) if ch.isdigit() and not ch.isascii() else ch)
    return "".join(out)


def _decimals(tok: str) -> int:
    return len(tok.split(".")[1]) if "." in tok else 0


def _matches(tok: str, allowed: set[float]) -> bool:
    n = float(tok.replace(",", "."))
    tol = 0.5 * 10 ** -_decimals(tok)
    return any(abs(n - a) <= max(tol + 1e-9, 0.01 * abs(a)) for a in allowed)


def validate_text(text: str, facts: Facts) -> str | None:
    if not text or not text.strip():
        return "empty"
    if len(text) > MAX_CHARS or len(text.split()) > MAX_WORDS:
        return "too_long"
    if any(unicodedata.category(c)[0] == "C" and c not in "\n\t" for c in text):
        return "control_chars"
    text = _digits(text)
    if _URL.search(text):
        return "url"
    if _MARKDOWN.search(text):
        return "markdown"
    if _PHONE.search(text):
        return "phone_number"
    if _GUARANTEE.search(text):
        return "guarantee"
    if not facts.valve_state_confirmed and _VALVE_CLAIM.search(text):
        return "unconfirmed_action_claim"
    allowed = facts.allowed_numbers()
    for tok in _NUM.findall(text):
        if not _matches(tok, allowed):
            return "unsupported_number"
    return None
