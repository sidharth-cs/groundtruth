"""Domain model for a vendor due-diligence pile.

The shapes here encode two non-negotiables from the brief:

1. Nothing is asserted without provenance. A `Claim` cannot exist without a
   `Citation`, and a `Citation` cannot exist without exact character offsets
   into a specific document. If the sources do not support a statement, the
   system emits `SupportLevel.UNSUPPORTED` rather than inventing one.

2. Disagreement is data, not an error. Two sources that contradict each other
   produce a `Conflict` that a human resolves; the system never silently picks
   a winner.
"""

from __future__ import annotations

import enum
import hashlib
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, Field, model_validator


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------
# Documents
# --------------------------------------------------------------------------


class DocumentKind(str, enum.Enum):
    """What a document is. Drives which rules apply to it.

    UNKNOWN is a legitimate terminal answer — a misfiled document that the
    system cannot confidently classify is reported, not guessed at.
    """

    INCORPORATION_CERTIFICATE = "incorporation_certificate"
    OWNERSHIP_DISCLOSURE = "ownership_disclosure"
    # A passport or national ID supplied for a beneficial owner. What makes it
    # useful in screening is the fields printed on it — date of birth,
    # nationality, document number — not the photograph.
    IDENTITY_DOCUMENT = "identity_document"
    SANCTIONS_SCREENING_RESULT = "sanctions_screening_result"
    INVOICE = "invoice"
    CONTRACT = "contract"
    CONTRACT_AMENDMENT = "contract_amendment"
    BANK_DETAILS = "bank_details"
    UNKNOWN = "unknown"


class DocumentStatus(str, enum.Enum):
    PENDING = "pending"
    PARSED = "parsed"
    QUARANTINED = "quarantined"  # contained instructions aimed at the system
    FAILED = "failed"


class Chunk(BaseModel):
    """An addressable span of a document.

    Offsets are into the document's normalised text and are what make a
    citation checkable: a reviewer can slice the source and see the quote.
    """

    id: UUID = Field(default_factory=uuid4)
    document_id: UUID
    ordinal: int
    text: str
    char_start: int
    char_end: int

    @model_validator(mode="after")
    def _offsets_are_sane(self) -> Chunk:
        if self.char_end <= self.char_start:
            raise ValueError("chunk char_end must be greater than char_start")
        if len(self.text) != self.char_end - self.char_start:
            raise ValueError(
                "chunk text length must equal char_end - char_start; "
                "citations are unverifiable otherwise"
            )
        return self


class Document(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    pile_id: str
    filename: str
    media_type: str
    text: str = ""
    kind: DocumentKind = DocumentKind.UNKNOWN
    status: DocumentStatus = DocumentStatus.PENDING
    # Set when status is QUARANTINED or FAILED — the reason is reported to
    # the human. None for a genuinely empty (not merely unparseable) file.
    quarantine_reason: str | None = None
    content_hash: str = ""
    ingested_at: datetime = Field(default_factory=_now)

    @model_validator(mode="after")
    def _hash_content(self) -> Document:
        if not self.content_hash and self.text:
            self.content_hash = hashlib.sha256(self.text.encode()).hexdigest()
        return self


# --------------------------------------------------------------------------
# Claims and provenance
# --------------------------------------------------------------------------


class Citation(BaseModel):
    """A pointer to the exact place a claim came from.

    `quote` is stored verbatim so the register is readable on its own, but the
    offsets are authoritative: `verify()` re-slices the document and confirms
    the quote still matches. A citation that cannot be verified is a bug we
    want to fail loudly, not a footnote nobody checks.
    """

    document_id: UUID
    chunk_id: UUID
    char_start: int
    char_end: int
    quote: str

    def verify(self, document_text: str) -> bool:
        return document_text[self.char_start : self.char_end] == self.quote


class SupportLevel(str, enum.Enum):
    """How well the sources back a claim.

    UNSUPPORTED exists so the system has something honest to say when it has
    nothing. Behaviour five of the brief: it never bluffs.
    """

    SUPPORTED = "supported"
    PARTIAL = "partial"
    UNSUPPORTED = "unsupported"


class Claim(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    pile_id: str
    attribute: str  # e.g. "registered_name", "ubo.full_name", "invoice.total"
    value: str | None
    support: SupportLevel
    citations: list[Citation] = Field(default_factory=list)
    extracted_at: datetime = Field(default_factory=_now)

    @model_validator(mode="after")
    def _supported_claims_must_cite(self) -> Claim:
        if self.support is not SupportLevel.UNSUPPORTED and not self.citations:
            raise ValueError(
                f"claim {self.attribute!r} is marked {self.support.value} but "
                "carries no citation; every supported claim must be traceable"
            )
        if self.support is SupportLevel.UNSUPPORTED and self.value is not None:
            raise ValueError(
                f"claim {self.attribute!r} is unsupported but carries a value; "
                "the system must not invent values it cannot source"
            )
        return self


class Conflict(BaseModel):
    """Two or more sources disagreeing about the same attribute.

    Surfaced for a human, never auto-resolved. `resolved_value` is only ever
    set by an approved human decision.
    """

    id: UUID = Field(default_factory=uuid4)
    pile_id: str
    attribute: str
    claims: list[Claim]
    detected_at: datetime = Field(default_factory=_now)
    resolved_value: str | None = None
    resolved_by_decision_id: UUID | None = None

    @property
    def is_resolved(self) -> bool:
        return self.resolved_by_decision_id is not None


# --------------------------------------------------------------------------
# Rules and findings
# --------------------------------------------------------------------------


class Severity(str, enum.Enum):
    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class Finding(BaseModel):
    """A rule that did not pass, pointing at the evidence.

    `rule_id` refers to a rule defined in a checklist file, not in code — a new
    rule is a data change.
    """

    id: UUID = Field(default_factory=uuid4)
    pile_id: str
    rule_id: str
    title: str
    detail: str
    severity: Severity
    citations: list[Citation] = Field(default_factory=list)
    # Set only by a rule that genuinely compares two values (today:
    # numeric_not_greater_than). None for every other rule type — a
    # required_claim finding has no "compared values" to show, and inventing
    # an empty one would be its own small bluff.
    compared: dict | None = None
    found_at: datetime = Field(default_factory=_now)


# --------------------------------------------------------------------------
# Human decisions
# --------------------------------------------------------------------------


class DecisionKind(str, enum.Enum):
    CONFLICT = "conflict"
    FINDING = "finding"
    UPDATE = "update"
    # A watchlist alert awaiting adjudication.
    ALERT = "alert"


class DecisionState(str, enum.Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    # Deliberately escalated rather than settled: the reviewer looked at it and
    # could not clear it on the evidence available. Only alerts use this.
    #
    # It is not a synonym for PENDING. Pending means nobody has looked yet;
    # escalated means somebody looked and refused to call it either way, which
    # is a real answer an analyst gives and the correct one when discounting
    # evidence is absent. It blocks onboarding, so an unresolved sanctions
    # alert can never sit inside a register that otherwise reads clean.
    ESCALATED = "escalated"


class Decision(BaseModel):
    """One item awaiting a human yes or no.

    Decisions are individual on purpose: rejecting one finding must leave every
    other pending item untouched.
    """

    id: UUID = Field(default_factory=uuid4)
    pile_id: str
    run_id: UUID
    kind: DecisionKind
    subject_id: UUID  # the Conflict / Finding / Update this gates
    summary: str
    state: DecisionState = DecisionState.PENDING
    # For conflicts: which value the human chose.
    chosen_value: str | None = None
    note: str | None = None
    decided_at: datetime | None = None
    # Who settled it.
    #
    # Self-asserted: there is no authentication in this build, so this records
    # the name the reviewer gave, not an identity anyone verified. It is still
    # worth keeping — "rejected by Sid at 14:02 because the amendment
    # supersedes it" is the record an examiner asks for, and a decision log with
    # no actor at all is not one. The README says plainly that it is unverified.
    decided_by: str | None = None

    def settle(
        self,
        state: DecisionState,
        chosen_value: str | None = None,
        note: str | None = None,
        by: str | None = None,
    ) -> Decision:
        if state is DecisionState.PENDING:
            raise ValueError(
                "settling a decision requires approved, rejected or escalated"
            )
        if state is DecisionState.ESCALATED and self.kind is not DecisionKind.ALERT:
            raise ValueError(
                "only a watchlist alert can be escalated; conflicts, findings "
                "and updates are approved or rejected"
            )
        return self.model_copy(
            update={
                "state": state,
                "chosen_value": chosen_value,
                "note": note,
                "decided_at": _now(),
                "decided_by": (by or "").strip() or "unnamed reviewer",
            }
        )

    @property
    def blocks_onboarding(self) -> bool:
        """Only one disposition clears a watchlist alert: rejecting it.

        Approved means confirmed true match. Escalated means a reviewer looked
        and could not clear it. Pending means nobody has looked at all — which
        is not a clean result, and must never report as one.
        """
        if self.kind is not DecisionKind.ALERT:
            return False
        return self.state is not DecisionState.REJECTED


# --------------------------------------------------------------------------
# Register (the deliverable)
# --------------------------------------------------------------------------


class RegisterSection(BaseModel):
    """One section of the deliverable.

    `content_hash` is what makes "nothing else changed" provable: an update
    that should not have touched a section must leave its hash identical.
    """

    id: str  # stable slug, e.g. "identity", "ownership", "sanctions"
    heading: str
    body: str
    claim_ids: list[UUID] = Field(default_factory=list)

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.body.encode()).hexdigest()


class Register(BaseModel):
    pile_id: str
    sections: list[RegisterSection] = Field(default_factory=list)
    generated_at: datetime = Field(default_factory=_now)
    revision: int = 1

    def section(self, section_id: str) -> RegisterSection | None:
        return next((s for s in self.sections if s.id == section_id), None)

    def hashes(self) -> dict[str, str]:
        """Section id -> content hash. Diff two of these to prove exactly which
        sections an update touched."""
        return {s.id: s.content_hash for s in self.sections}


# --------------------------------------------------------------------------
# Run accounting
# --------------------------------------------------------------------------


class StageCost(BaseModel):
    stage: str
    model_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    wall_ms: int = 0
    usd: float = 0.0


class RunState(str, enum.Enum):
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"


class Run(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    pile_id: str
    state: RunState = RunState.RUNNING
    started_at: datetime = Field(default_factory=_now)
    finished_at: datetime | None = None
    stages: list[StageCost] = Field(default_factory=list)
    # Free-form provenance: what changed, when, because of which source.
    events: list[dict[str, Any]] = Field(default_factory=list)

    def total(self) -> StageCost:
        agg = StageCost(stage="total")
        for s in self.stages:
            agg.model_calls += s.model_calls
            agg.input_tokens += s.input_tokens
            agg.output_tokens += s.output_tokens
            agg.wall_ms += s.wall_ms
            agg.usd += s.usd
        return agg
