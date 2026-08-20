"""Focused updates when a new document arrives.

An arrival must cost like an arrival. This reads and classifies exactly one
document, extracts from exactly that document, and rebuilds only the sections
whose attributes it could possibly have touched. Sections outside that set are
carried across as the identical objects — not recomputed and compared, not
rebuilt and found to match, but never rebuilt at all. That is what makes
"nothing else changed" a fact about the work done rather than a claim about
the output.

When the arrival disagrees with what the register already says, a conflict is
raised for a human. A re-screen that clears a previously flagged match is
exactly this case: it is almost certainly a supersession, but "almost
certainly" is not a standard a compliance register should silently apply.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

from app.core.llm import Meter, ModelAdapter
from app.domain.models import (
    Chunk,
    Claim,
    Conflict,
    Document,
    DocumentStatus,
    Register,
    StageCost,
)
from app.pipeline import extract as extract_mod
from app.pipeline.graph import SECTION_FOR_ATTRIBUTE, build_section
from app.pipeline.ingest import chunk_document, read_document


@dataclass
class UpdateResult:
    """What an arrival did, in terms a reviewer can check."""

    arrival: str
    document: Document
    # Carried so the document can be persisted alongside its claims when the
    # update is approved. Without them a citation from an arrival has nothing
    # to re-slice against and the register cannot prove where it came from.
    chunks: list[Chunk] = field(default_factory=list)
    new_claims: list[Claim] = field(default_factory=list)
    new_conflicts: list[Conflict] = field(default_factory=list)
    sections_rebuilt: list[str] = field(default_factory=list)
    sections_untouched: list[str] = field(default_factory=list)
    # What a section-level "rebuilt" proves and what it doesn't: it proves
    # *only* those sections changed, not what changed within them. A reviewer
    # approving blind still has to open the register and diff it by eye.
    # One entry per attribute this arrival actually changed the value of —
    # not every claim it carries, only the ones that disagree with what the
    # register already said (or that are new: old_value is None).
    value_changes: list[dict] = field(default_factory=list)
    register: Register | None = None
    cost: StageCost | None = None

    def to_proposal(self, hashes_before: dict[str, str]) -> dict:
        """Everything a reviewer needs to judge this update, and everything the
        system needs to apply it later, in one checkpointable dict.

        A proposal has to survive a restart intact: a reviewer who comes back
        tomorrow must see the same proposed change, not a recomputed one that
        may differ because the sources moved underneath it.
        """
        return {
            "arrival": self.arrival,
            "document": self.document.model_dump(mode="json"),
            "chunks": [c.model_dump(mode="json") for c in self.chunks],
            "register": self.register.model_dump(mode="json") if self.register else None,
            "new_claims": [c.model_dump(mode="json") for c in self.new_claims],
            "new_conflicts": [c.model_dump(mode="json") for c in self.new_conflicts],
            "sections_rebuilt": self.sections_rebuilt,
            "sections_untouched": self.sections_untouched,
            "claims_added": [
                {"attribute": c.attribute, "value": c.value} for c in self.new_claims
            ],
            "conflicts_raised": [c.attribute for c in self.new_conflicts],
            "value_changes": self.value_changes,
            "cost": self.cost.model_dump(mode="json") if self.cost else None,
            "hashes_before": hashes_before,
            "hashes_after": self.register.hashes() if self.register else {},
            "proposed_at": time.time(),
        }

    def provenance(self) -> dict:
        """What changed, when, and because of which source."""
        return {
            "at": time.time(),
            "because_of": self.arrival,
            "document_id": str(self.document.id),
            "document_kind": self.document.kind.value,
            "claims_added": [
                {"attribute": c.attribute, "value": c.value} for c in self.new_claims
            ],
            "conflicts_raised": [c.attribute for c in self.new_conflicts],
            "sections_rebuilt": self.sections_rebuilt,
            "sections_untouched": self.sections_untouched,
            "model_calls": self.cost.model_calls if self.cost else 0,
        }


def apply_arrival(
    arrival_path: Path,
    register: Register,
    existing_claims: list[Claim],
    existing_conflicts: list[Conflict],
    adapter: ModelAdapter,
    pile_id: str,
    max_model_calls: int = 20,
    alerts: list[dict] | None = None,
) -> UpdateResult:
    started = time.monotonic()
    meter = Meter(adapter, max_model_calls)

    document = read_document(arrival_path, pile_id)
    chunks = chunk_document(document)
    document = extract_mod.screen_document(document)
    document = document.model_copy(
        update={"kind": extract_mod.classify(document, meter)}
    )

    new_claims = extract_mod.extract_claims(document, chunks, meter, pile_id)

    # Only sections whose attributes this arrival actually carries can change.
    # A quarantined arrival carries nothing, so nothing changes — the document
    # is still recorded and reported.
    affected = {
        SECTION_FOR_ATTRIBUTE[c.attribute]
        for c in new_claims
        if c.attribute in SECTION_FOR_ATTRIBUTE
    }

    all_claims = existing_claims + new_claims
    # Re-run conflict detection over the merged set, then keep only the
    # conflicts this arrival is responsible for.
    merged_conflicts = extract_mod.detect_conflicts(all_claims, pile_id)
    known = {(c.attribute, tuple(sorted(str(x.id) for x in c.claims)))
             for c in existing_conflicts}
    new_conflicts = [
        c for c in merged_conflicts
        if (c.attribute, tuple(sorted(str(x.id) for x in c.claims))) not in known
    ]

    # Value-level diff for the sections that got rebuilt, so approving one
    # doesn't require a reviewer to diff the register by eye afterward.
    # Deliberately excludes anything in new_conflicts: a disputed attribute
    # isn't cleanly "old value -> new value", it's two values now coexisting
    # for a human to settle — that story belongs to conflicts_raised, not
    # here, and telling both would say the same fact two different ways.
    disputed = {c.attribute for c in new_conflicts}
    prior_by_attribute = {c.attribute: c.value for c in existing_claims}
    value_changes = [
        {
            "attribute": claim.attribute,
            "old_value": prior_by_attribute.get(claim.attribute),
            "new_value": claim.value,
        }
        for claim in new_claims
        if claim.attribute not in disputed
        and prior_by_attribute.get(claim.attribute) != claim.value
    ]

    # A newly disputed attribute changes how its section reads, even if the
    # arrival's own claim is not the one displayed.
    for conflict in new_conflicts:
        if conflict.attribute in SECTION_FOR_ATTRIBUTE:
            affected.add(SECTION_FOR_ATTRIBUTE[conflict.attribute])

    # Carry unaffected sections across untouched. Same objects, not rebuilt.
    conflicts_for_render = existing_conflicts + new_conflicts
    sections = []
    rebuilt, untouched = [], []
    for section in register.sections:
        if section.id in affected:
            # Alerts are carried in so a rebuild of the screening section keeps
            # them. Rebuilding it without them would silently delete a live
            # watchlist alert from the register — the exact failure this system
            # exists to prevent, arriving through the back door of an unrelated
            # document landing in the watched folder.
            sections.append(
                build_section(section.id, all_claims, conflicts_for_render, alerts)
            )
            rebuilt.append(section.id)
        else:
            sections.append(section)
            untouched.append(section.id)

    updated = Register(
        pile_id=pile_id, sections=sections, revision=register.revision + 1
    )

    return UpdateResult(
        arrival=arrival_path.name,
        document=document,
        chunks=chunks,
        new_claims=new_claims,
        new_conflicts=new_conflicts,
        sections_rebuilt=rebuilt,
        sections_untouched=untouched,
        value_changes=value_changes,
        register=updated,
        cost=StageCost(
            stage=f"arrival:{arrival_path.name}",
            model_calls=meter.usage.calls,
            input_tokens=meter.usage.input_tokens,
            output_tokens=meter.usage.output_tokens,
            wall_ms=int((time.monotonic() - started) * 1000),
            usd=meter.usage.usd,
        ),
    )


def prove_untouched(before: Register, after: Register, expected_changed: list[str]) -> dict:
    """Compare section hashes and report the truth, whatever it is.

    Returns the actual changed and unchanged sets alongside what was expected,
    so a caller can assert on it rather than trust a boolean.
    """
    before_hashes = before.hashes()
    after_hashes = after.hashes()
    shared = set(before_hashes) & set(after_hashes)

    changed = sorted(s for s in shared if before_hashes[s] != after_hashes[s])
    unchanged = sorted(s for s in shared if before_hashes[s] == after_hashes[s])

    return {
        "changed": changed,
        "unchanged": unchanged,
        "expected_changed": sorted(expected_changed),
        "matches_expectation": changed == sorted(expected_changed),
        "before": before_hashes,
        "after": after_hashes,
    }
