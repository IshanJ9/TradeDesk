"""Detects text that tries to give the model instructions.

This is a flag, not the defence. The defence is structural: the LLM has no tool that can send
an order, so even a fooled model can only draft a card that still needs the trader's click.
Flagged text is withheld from the model entirely and shown to the trader, quoted, as a notice.
"""

import re
from dataclasses import dataclass

_PATTERNS: dict[str, re.Pattern] = {
    name: re.compile(rx, re.IGNORECASE)
    for name, rx in {
        "ignore-instructions": r"\b(ignore|disregard|forget|override)\b.{0,30}\b(previous|prior|above|earlier|all|any|your)\b.{0,30}\b(instructions?|rules?|prompts?|guidelines?)\b",
        "system-prompt": r"\b(system|developer)\s+(prompt|message|mode|instructions?)\b",
        "role-change": r"\byou\s+are\s+now\b|\bact\s+as\s+(an?\s+)?(admin|system|developer|root|unrestricted)\b",
        "new-instructions": r"\bnew\s+instructions?\s*:",
        # a trading verb directly followed by "all/everything" (allowing "my", "the"...), not merely near it
        "bulk-trade": r"\b(sell|buy|liquidate|dump|transfer|withdraw)\b(?:\s+(?:my|your|the|of|out|off))*\s+(all|everything|entire|every)\b",
        "skip-approval": r"\bwithout\s+(asking|confirmation|confirming|approval|the\s+trader)\b|\b(do\s*n[o']t|never)\s+(ask|confirm|tell|show|warn)\b",
        "fake-tags": r"<\s*/?\s*(system|assistant|tool|instructions?)\s*>",
        "jailbreak": r"\bjailbreak\b|\bdeveloper\s+mode\b|\bDAN\b",
    }.items()
}


@dataclass(frozen=True)
class Finding:
    source: str  # where the text came from, e.g. "instrument EVILCORP"
    text: str
    matched: tuple[str, ...]


def scan(text: str) -> tuple[str, ...]:
    """Names of the patterns that matched. Empty means nothing suspicious was found."""
    return tuple(name for name, rx in _PATTERNS.items() if rx.search(text))
