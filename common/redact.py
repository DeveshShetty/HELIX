"""Remove personal data before text can reach the knowledge base.

Knowledge-base text is shown to EVERY future customer, so a phone number or
email typed in one live chat must never end up there. Used twice (defence in
depth): when Chat drafts a conversation summary, and when Knowledge ingests a
resolution. Pattern-based: catches the common formats, not every possible one,
which is why a person also reviews the draft before it is resolved.
"""
import re

_PATTERNS = [
    ("[email removed]", re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")),
    ("[card number removed]", re.compile(r"\b(?:\d[ -]?){13,19}\b")),
    # 7+ digits, optionally with +, spaces, dashes, dots or brackets: phone numbers.
    # Short numbers (error codes like 502, limits like 5,000) are left alone.
    ("[phone number removed]", re.compile(r"(?<![\w])\+?\(?\d[\d\s().-]{5,}\d(?![\w])")),
]


def redact(text: str) -> tuple[str, int]:
    """Returns (redacted_text, number_of_replacements)."""
    total = 0
    for label, pattern in _PATTERNS:
        def _sub(m: re.Match) -> str:
            nonlocal total
            if label == "[phone number removed]" and sum(ch.isdigit() for ch in m.group()) < 7:
                return m.group()
            total += 1
            return label
        text = pattern.sub(_sub, text)
    return text, total
