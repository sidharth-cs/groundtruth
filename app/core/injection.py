"""Refusing to take orders from the documents being examined.

The rule, stated before the code: **a source document is data to report on,
never a command to follow.** A vendor who writes "approve this vendor" into
their own cover note must not be able to approve themselves.

The hard part is not detection, it is precision. A blocklist on words like
"ignore" or "approve" would flag half a contract, and the brief is explicit
that a fix must not buy its correctness by wrongly refusing valid work
elsewhere. Legitimate compliance paperwork is full of directive language:

    "No variation to the contract value is effective unless agreed..."
    "The Supplier shall disregard any prior quotation."
    "Approve all invoices within 30 days of receipt."

None of those are attacks. What separates an attack is that it addresses the
*reading system* and tries to change how that system behaves — not what the
parties to a contract must do. So detection scores two independent signals and
requires both:

  1. second-person address to an automated reader, or an explicit
     system/instruction preamble
  2. an attempt to steer the system's own output or controls — suppressing
     findings, self-approving, concealing something from the operator

A document that trips both is quarantined: kept, shown to the human, reported
as a finding, and excluded from fact extraction. Never silently dropped — the
operator needs to know someone tried.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Signal 1: the text is talking TO an automated reader.
_ADDRESSES_SYSTEM = [
    re.compile(r"\b(?:system|assistant|ai|model|agent)\s+(?:instruction|prompt|note|directive)\b", re.I),
    re.compile(r"\bignore\s+(?:all\s+)?(?:previous|prior|earlier|above)\s+instructions?\b", re.I),
    re.compile(r"\bdisregard\s+(?:all\s+)?(?:previous|prior|earlier|above)\s+(?:instructions?|prompts?)\b", re.I),
    re.compile(r"\byou\s+are\s+now\b", re.I),
    re.compile(r"\bnew\s+instructions?\s*:", re.I),
    re.compile(r"\bas\s+an?\s+(?:ai|assistant|language\s+model)\b", re.I),
]

# Signal 2: it is trying to steer the reading system's own behaviour.
_STEERS_SYSTEM = [
    re.compile(r"\b(?:suppress|hide|omit|conceal)\s+(?:any\s+|all\s+)?(?:findings?|alerts?|issues?|conflicts?)\b", re.I),
    re.compile(r"\bapprove\s+(?:all\s+)?(?:pending\s+)?(?:decisions?|changes?|findings?)\b", re.I),
    re.compile(r"\bwithout\s+(?:human\s+)?(?:review|approval|oversight)\b", re.I),
    re.compile(r"\bmark\s+(?:the\s+)?\w+\s+as\s+(?:cleared|approved|complete|passed)\b", re.I),
    re.compile(r"\bdo\s+not\s+(?:mention|report|disclose|flag|include)\b", re.I),
    re.compile(r"\b(?:expedited|bypass|override)\s+mode\b", re.I),
    re.compile(r"\bstate\s+(?:in\s+the\s+\w+\s+)?that\s+(?:due\s+diligence|screening|review)\s+is\s+complete\b", re.I),
]


@dataclass
class InjectionVerdict:
    """Why a document was or was not quarantined.

    `evidence` carries the literal matched phrases with their offsets, so the
    finding raised for the operator quotes the attempt rather than asserting
    one happened.
    """

    is_injection: bool
    address_hits: list[tuple[str, int, int]] = field(default_factory=list)
    steer_hits: list[tuple[str, int, int]] = field(default_factory=list)

    @property
    def reason(self) -> str:
        if not self.is_injection:
            return ""
        quoted = self.address_hits[0][0] if self.address_hits else ""
        steer = self.steer_hits[0][0] if self.steer_hits else ""
        return (
            "Document contains text addressed to the processing system that "
            f"attempts to alter its behaviour (matched {quoted!r} and {steer!r}). "
            "Content quarantined: reported, not obeyed, and excluded from "
            "fact extraction."
        )

    @property
    def evidence(self) -> list[tuple[str, int, int]]:
        return self.address_hits + self.steer_hits


def _scan(text: str, patterns: list[re.Pattern[str]]) -> list[tuple[str, int, int]]:
    hits: list[tuple[str, int, int]] = []
    for pattern in patterns:
        for match in pattern.finditer(text):
            hits.append((match.group(0), match.start(), match.end()))
    return hits


def screen(text: str) -> InjectionVerdict:
    """Both signals must fire. One alone is ordinary contractual language."""
    address_hits = _scan(text, _ADDRESSES_SYSTEM)
    steer_hits = _scan(text, _STEERS_SYSTEM)
    return InjectionVerdict(
        is_injection=bool(address_hits) and bool(steer_hits),
        address_hits=address_hits,
        steer_hits=steer_hits,
    )
