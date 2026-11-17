"""Prompt-injection defence.

Two places untrusted text enters the prompt:
  1. the user's message           -> if it looks like an injection, refuse.
  2. retrieved KB documents       -> strip lines that look like instructions
     (indirect injection: a poisoned runbook must not steer the model).
And one place untrusted text leaves:
  3. the model's answer           -> if it echoes an injection pattern, block it.

Pattern matching is a cheap first layer, not a complete defence. The other
layers are structural: retrieved text is wrapped in delimiters and labelled as
data in the prompt (see llm.py), and the chat has no tools it could be
tricked into calling.
"""
import re

INJECTION_PATTERNS = [
    r"ignore\s+(all\s+)?(the\s+)?(previous|prior|above|earlier|your)\s+(instructions|rules|prompts?)",
    r"disregard\s+(all\s+)?(the\s+)?(previous|prior|above|your)\s+\w+",
    r"forget\s+(all\s+)?(your|previous)\s+(instructions|rules)",
    r"(reveal|print|show|repeat)\s+(me\s+)?(your|the)\s+(system\s+)?(prompt|instructions)",
    r"\bsystem\s+prompt\b",
    r"you\s+are\s+now\s+(in\s+)?\w+",
    r"\b(developer|maintenance|jailbreak|dan)\s+mode\b",
    r"\bact\s+as\s+(an?\s+)?(admin|root|system)\b",
    r"(send|email|give|share)\s+(us\s+|me\s+)?((your|their|the|a)\s+)?password",
    r"\btell\s+the\s+user\s+(that|to)\b",
]
_COMPILED = [re.compile(p, re.IGNORECASE) for p in INJECTION_PATTERNS]


def looks_like_injection(text: str) -> bool:
    return any(p.search(text) for p in _COMPILED)


def sanitize_document(text: str) -> tuple[str, int]:
    """Drop sentences/lines that look like instructions. Returns (clean_text, removed_count)."""
    kept, removed = [], 0
    for para in re.split(r"\n\s*\n", text):
        # Check whole sentences (they may wrap across lines), not individual lines.
        sentences = re.split(r"(?<=[.!?])\s+", " ".join(para.split()))
        good = [s for s in sentences if not looks_like_injection(s)]
        if len(good) == len(sentences):
            kept.append(para)                # untouched: keep original formatting (numbered steps)
        else:
            removed += len(sentences) - len(good)
            if good:
                kept.append(" ".join(good))
    return "\n\n".join(kept).strip(), removed
