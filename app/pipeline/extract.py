"""Turning parsed documents into cited claims, and spotting disagreement.

Every claim produced here carries a citation with exact offsets, and every
citation is verified against the source before the claim is allowed to exist.
If the model (or the offline adapter) reports a value that cannot be located in
the document, the claim is dropped rather than emitted uncitable — a
hallucinated value dies at this boundary rather than reaching the register.

Conflict detection is deliberately dumb: same attribute, different values,
different documents. It does not try to decide which source wins. Choosing a
winner is a human decision, and pretending otherwise is how a register ends up
confidently wrong.
"""

from __future__ import annotations

from app.core.injection import screen
from app.core.llm import Meter
from app.domain.models import (
    Chunk,
    Citation,
    Claim,
    Conflict,
    Document,
    DocumentKind,
    DocumentStatus,
    SupportLevel,
)
from app.pipeline.ingest import find_chunk

# Which attributes are worth looking for in which kind of document. Keeping
# this as a mapping rather than "extract everything from everything" cuts model
# calls and stops an invoice being mined for ownership data it cannot have.
ATTRIBUTES_BY_KIND: dict[DocumentKind, list[str]] = {
    DocumentKind.INCORPORATION_CERTIFICATE: [
        "registered_name", "company_number", "incorporation_date", "jurisdiction",
    ],
    DocumentKind.OWNERSHIP_DISCLOSURE: [
        "registered_name", "company_number", "ubo_name", "ubo_percentage",
    ],
    DocumentKind.IDENTITY_DOCUMENT: [
        "ubo_name", "ubo_date_of_birth", "ubo_nationality", "ubo_document_number",
    ],
    DocumentKind.SANCTIONS_SCREENING_RESULT: [
        "screening_result", "screening_date",
    ],
    DocumentKind.INVOICE: [
        "invoice_number", "invoice_total",
    ],
    DocumentKind.CONTRACT: [
        "contract_value", "effective_date",
    ],
    DocumentKind.CONTRACT_AMENDMENT: [
        "contract_value", "effective_date",
    ],
    DocumentKind.BANK_DETAILS: [
        "bank_iban",
    ],
    DocumentKind.UNKNOWN: [],
}

# Attributes where two different values across sources is a genuine
# contradiction rather than two legitimately different facts. An invoice number
# differing between two invoices is normal; a company number differing between
# two documents about the same entity is not.
CONFLICTABLE = {
    "registered_name",
    "company_number",
    "incorporation_date",
    "jurisdiction",
    "ubo_percentage",
    "contract_value",
    "screening_result",
}


def classify(
    document: Document, meter: Meter, widened: bool = False
) -> DocumentKind:
    """Label a document by kind.

    A quarantined document is never classified. Its text is hostile input, and
    letting it choose its own label is a smaller version of the same mistake as
    letting it choose its own outcome — the poisoned cover note in the test
    corpus talks about "sanctions screening" purely to blend in, and would
    otherwise be filed as a screening result. Untrusted content gets no say in
    how it is filed, and costs no model call either.

    `widened` selects the second-pass strategy, used by the graph only after a
    first pass has already failed across the pile.
    """
    if document.status is DocumentStatus.QUARANTINED:
        return DocumentKind.UNKNOWN

    label = meter.classify(document.text, document.filename, widened=widened)
    try:
        return DocumentKind(label)
    except ValueError:
        return DocumentKind.UNKNOWN


def screen_document(document: Document) -> Document:
    """Quarantine a document that tries to give the system orders.

    The document is kept and reported. It is simply never treated as
    instruction and never mined for facts.
    """
    verdict = screen(document.text)
    if not verdict.is_injection:
        return document
    return document.model_copy(
        update={
            "status": DocumentStatus.QUARANTINED,
            "quarantine_reason": verdict.reason,
        }
    )


def extract_claims(
    document: Document,
    chunks: list[Chunk],
    meter: Meter,
    pile_id: str,
) -> list[Claim]:
    """Extract cited claims from one document.

    A quarantined document yields nothing: its content is evidence of an
    attempted injection, not a source of facts.
    """
    if document.status is DocumentStatus.QUARANTINED:
        return []

    attributes = ATTRIBUTES_BY_KIND.get(document.kind, [])
    if not attributes:
        return []

    claims: list[Claim] = []
    for extraction in meter.extract(document.text, attributes):
        # Verify before trusting. The adapter reports offsets; we re-slice the
        # source and confirm they hold. Anything that fails here would have
        # produced an unverifiable citation.
        quoted = document.text[extraction.char_start : extraction.char_end]
        if quoted != extraction.value:
            continue

        chunk_id = find_chunk(chunks, extraction.char_start)
        if chunk_id is None:
            continue

        citation = Citation(
            document_id=document.id,
            chunk_id=chunk_id,
            char_start=extraction.char_start,
            char_end=extraction.char_end,
            quote=quoted,
        )
        claims.append(
            Claim(
                pile_id=pile_id,
                attribute=extraction.attribute,
                value=extraction.value,
                support=SupportLevel.SUPPORTED,
                citations=[citation],
            )
        )
    return claims


def unsupported(pile_id: str, attribute: str) -> Claim:
    """The honest answer when the sources do not carry an attribute."""
    return Claim(
        pile_id=pile_id,
        attribute=attribute,
        value=None,
        support=SupportLevel.UNSUPPORTED,
        citations=[],
    )


def _normalise(value: str) -> str:
    return " ".join(value.strip().lower().replace(",", "").split())


def detect_conflicts(claims: list[Claim], pile_id: str) -> list[Conflict]:
    """Same attribute, materially different values, different documents.

    No winner is chosen. The conflict is data for a human to resolve.
    """
    by_attribute: dict[str, list[Claim]] = {}
    for claim in claims:
        if claim.attribute in CONFLICTABLE and claim.value:
            by_attribute.setdefault(claim.attribute, []).append(claim)

    conflicts: list[Conflict] = []
    for attribute, group in by_attribute.items():
        distinct: dict[str, list[Claim]] = {}
        for claim in group:
            distinct.setdefault(_normalise(claim.value or ""), []).append(claim)
        if len(distinct) < 2:
            continue
        # One representative claim per distinct value keeps the conflict
        # readable when several documents repeat the same figure.
        representatives = [group_[0] for group_ in distinct.values()]
        if len({c.citations[0].document_id for c in representatives}) < 2:
            continue  # same document contradicting itself is a parse issue, not a source conflict
        conflicts.append(
            Conflict(pile_id=pile_id, attribute=attribute, claims=representatives)
        )
    return conflicts
