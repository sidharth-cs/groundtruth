"""The agent graph.

Stages are named, their decisions are recorded, and some of those decisions
change the path: classification that comes back mostly unknown is retried with
a widened attribute set, and if it is still unusable after the retry budget the
run escalates to a human instead of pressing on with bad labels. That is the
difference the brief draws between an agentic system and a fixed script with
labels on it.

The graph is checkpointed after every stage. Killing the process mid-run and
starting it again resumes from the last completed stage — no stage re-runs, no
model call is paid for twice, and no finished work is lost.

Before the register is committed the graph interrupts and waits for a human.
The interrupt is not cosmetic: nothing downstream of `gate` executes until a
person has decided, item by item, and the decisions are read back out of the
store rather than assumed.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Annotated, Any, Literal, TypedDict
from uuid import UUID, uuid4

from langgraph.graph import END, StateGraph

from app.core.llm import Meter, ModelAdapter
from app.domain.models import (
    Chunk,
    Claim,
    Conflict,
    Decision,
    DecisionKind,
    DecisionState,
    Document,
    DocumentKind,
    DocumentStatus,
    Finding,
    Register,
    RegisterSection,
    StageCost,
)
from app.domain.rules import Checklist, ChecklistEngine
from app.domain.screening import (
    Adjudication,
    Alert,
    PartyType,
    load_watchlist,
)
from app.domain.screening import screen as screen_subject
from app.pipeline import extract as extract_mod
from app.pipeline.ingest import load_pile


def _alert_to_dict(alert: Alert) -> dict:
    """Alerts are dataclasses, not pydantic models, so they are flattened here
    for the checkpointer. Everything a reviewer needs to adjudicate must
    survive a process restart, including the evidence."""
    recommendation, reason = alert.recommendation()
    return {
        "id": str(alert.id),
        "subject_name": alert.subject_name,
        "subject_type": alert.subject_type.value,
        "subject_role": alert.subject_role,
        "listed_uid": alert.listed.uid if alert.listed else "",
        "listed_name": alert.listed.name if alert.listed else "",
        "listed_type": alert.listed.party_type.value if alert.listed else "",
        "listed_programme": alert.listed.programme if alert.listed else "",
        "listed_aliases": alert.listed.aliases if alert.listed else [],
        "score": round(alert.score, 4),
        "matched_on": alert.matched_on,
        "via_weak_alias": alert.via_weak_alias,
        "identifiers": [
            {
                "name": i.name,
                "subject_value": i.subject_value,
                "listed_value": i.listed_value,
                "comparison": i.comparison.value,
                "strength": i.strength.value,
                "note": i.note,
            }
            for i in alert.identifiers
        ],
        "recommendation": recommendation.value,
        "recommendation_reason": reason,
    }
    # Deliberately no "adjudication" field. The alert's outcome lives on its
    # Decision and nowhere else; a second copy here would be one restart away
    # from disagreeing with the first, and the disagreement would be silent.


def _keep_last(_: Any, new: Any) -> Any:
    return new


def _extend(existing: list[Any], new: list[Any]) -> list[Any]:
    return (existing or []) + (new or [])


class PipelineState(TypedDict, total=False):
    """Everything the graph carries between stages.

    Kept flat and serialisable so the checkpointer can round-trip it. Anything
    that cannot survive a process restart does not belong here.
    """

    run_id: str
    pile_id: str
    pile_path: str
    checklist_path: str

    documents: Annotated[list[dict], _keep_last]
    chunks: Annotated[dict, _keep_last]  # document_id -> [chunk dicts]
    claims: Annotated[list[dict], _keep_last]
    conflicts: Annotated[list[dict], _keep_last]
    findings: Annotated[list[dict], _keep_last]
    # The full checklist tally (passed/failed/not-evaluated), not just the
    # failures. `findings` above is only the FAILED subset; a reviewer cannot
    # tell "every rule ran and passed" from "some rules never ran" without
    # this, and the brief specifically grades that distinction.
    checklist_summary: Annotated[dict, _keep_last]
    alerts: Annotated[list[dict], _keep_last]
    watchlist_path: Annotated[str, _keep_last]
    watchlist_available: Annotated[bool, _keep_last]
    watchlist_source: Annotated[str, _keep_last]
    watchlist_name: Annotated[str, _keep_last]
    onboarding_blocked: Annotated[bool, _keep_last]
    blocking_reasons: Annotated[list[str], _keep_last]
    register: Annotated[dict, _keep_last]
    decisions: Annotated[list[dict], _keep_last]
    # An arriving document's proposed change, held here until a human settles
    # the UPDATE decision that gates it. None means nothing is outstanding.
    proposed_update: Annotated[dict | None, _keep_last]
    # Filenames submitted while proposed_update was already occupied. Whole-
    # list-rewrite, like decisions — a filename is added or removed, never
    # appended-and-forgotten, so this uses _keep_last rather than _extend.
    pending_arrivals: Annotated[list[str], _keep_last]

    # A soft hide from the main case list — set by a human, not the pipeline;
    # nothing in the graph reads it. `total=False` means every checkpoint
    # written before this field existed reads back as unset, and callers
    # treat that as False (see app/api/main.py's `.get("archived", False)`).
    archived: Annotated[bool, _keep_last]

    classify_attempts: Annotated[int, _keep_last]
    escalated: Annotated[bool, _keep_last]
    escalation_reason: Annotated[str, _keep_last]

    stage_costs: Annotated[list[dict], _extend]
    events: Annotated[list[dict], _extend]


# Fraction of documents that may come back UNKNOWN before classification is
# considered to have failed rather than merely found an odd file.
_UNKNOWN_TOLERANCE = 0.34


def _test_stall(stage: str) -> None:
    """Test-only instrumentation: hold a named stage open.

    Proving "kill it mid-run and it resumes" needs the kill to land *inside* a
    stage rather than in the microsecond gap between two, and the offline
    pipeline finishes in about thirty milliseconds. Setting
    DOCTASK_STALL_STAGE=extract holds that stage open so the SIGKILL is
    deterministic.

    This changes timing only. It is never set in normal operation, it cannot
    alter what any stage computes, and no production path reads it.
    """
    if os.environ.get("DOCTASK_STALL_STAGE") != stage:
        return
    seconds = float(os.environ.get("DOCTASK_STALL_SECONDS", "10"))
    print(f"[stall] holding stage {stage!r} open for {seconds}s", flush=True)
    time.sleep(seconds)


class Pipeline:
    def __init__(
        self,
        adapter: ModelAdapter,
        max_model_calls: int = 200,
        max_stage_retries: int = 2,
    ) -> None:
        self._adapter = adapter
        self._max_model_calls = max_model_calls
        self._max_retries = max_stage_retries

    # -- helpers ---------------------------------------------------------

    def _meter(self) -> Meter:
        return Meter(self._adapter, self._max_model_calls)

    @staticmethod
    def _cost(stage: str, meter: Meter, started: float) -> dict:
        return StageCost(
            stage=stage,
            model_calls=meter.usage.calls,
            input_tokens=meter.usage.input_tokens,
            output_tokens=meter.usage.output_tokens,
            wall_ms=int((time.monotonic() - started) * 1000),
            usd=meter.usage.usd,
        ).model_dump()

    @staticmethod
    def _event(stage: str, decision: str, **detail: Any) -> dict:
        return {"stage": stage, "decision": decision, "at": time.time(), **detail}

    # -- stages ----------------------------------------------------------

    def intake(self, state: PipelineState) -> dict:
        started = time.monotonic()
        loaded = load_pile(Path(state["pile_path"]), state["pile_id"])
        documents = [doc for doc, _ in loaded]
        chunks = {str(doc.id): [c.model_dump(mode="json") for c in ch] for doc, ch in loaded}
        return {
            "documents": [d.model_dump(mode="json") for d in documents],
            "chunks": chunks,
            "stage_costs": [self._cost("intake", self._meter(), started)],
            "events": [
                self._event("intake", "loaded", documents=len(documents),
                            chunks=sum(len(c) for c in chunks.values()))
            ],
        }

    def screen(self, state: PipelineState) -> dict:
        """Quarantine anything that tries to give the system orders."""
        started = time.monotonic()
        screened, quarantined = [], []
        for raw in state["documents"]:
            doc = extract_mod.screen_document(Document.model_validate(raw))
            screened.append(doc.model_dump(mode="json"))
            if doc.status is DocumentStatus.QUARANTINED:
                quarantined.append(doc.filename)
        return {
            "documents": screened,
            "stage_costs": [self._cost("screen", self._meter(), started)],
            "events": [
                self._event(
                    "screen",
                    "quarantined" if quarantined else "clean",
                    quarantined=quarantined,
                )
            ],
        }

    def classify(self, state: PipelineState) -> dict:
        started = time.monotonic()
        meter = self._meter()
        attempt = state.get("classify_attempts", 0) + 1

        # A retry that repeats the same call gets the same answer, which would
        # make the branch decoration rather than a decision. Attempts after the
        # first use the widened strategy.
        widened = attempt > 1

        classified = []
        for raw in state["documents"]:
            doc = Document.model_validate(raw)
            kind = extract_mod.classify(doc, meter, widened=widened)
            classified.append(doc.model_copy(update={"kind": kind}).model_dump(mode="json"))

        # Quarantined documents are legitimately UNKNOWN — they were never
        # classified on purpose — so they are excluded from the ratio that
        # decides whether classification failed.
        considered = [
            d for d in classified
            if d["status"] != DocumentStatus.QUARANTINED.value
        ]
        unknown = [d for d in considered if d["kind"] == DocumentKind.UNKNOWN.value]
        ratio = (len(unknown) / len(considered)) if considered else 0.0

        return {
            "documents": classified,
            "classify_attempts": attempt,
            "stage_costs": [self._cost("classify", meter, started)],
            "events": [
                self._event(
                    "classify",
                    "widened_retry" if widened else "attempted",
                    attempt=attempt, strategy="widened" if widened else "primary",
                    unknown=len(unknown), considered=len(considered),
                    unknown_ratio=round(ratio, 3),
                )
            ],
        }

    def after_classify(self, state: PipelineState) -> Literal["retry", "escalate", "continue"]:
        """A real decision that changes the path.

        Too many unknowns means the pile is not what the system thinks it is.
        Retry once, then stop and ask a human rather than extracting facts
        against labels nobody trusts.
        """
        docs = state["documents"]
        considered = [d for d in docs if d["status"] != DocumentStatus.QUARANTINED.value]
        if not considered:
            return "continue"
        unknown = sum(1 for d in considered if d["kind"] == DocumentKind.UNKNOWN.value)
        if unknown / len(considered) <= _UNKNOWN_TOLERANCE:
            return "continue"
        if state.get("classify_attempts", 0) < self._max_retries:
            return "retry"
        return "escalate"

    def escalate(self, state: PipelineState) -> dict:
        docs = state["documents"]
        considered = [d for d in docs if d["status"] != DocumentStatus.QUARANTINED.value]
        unknown = [d["filename"] for d in considered if d["kind"] == DocumentKind.UNKNOWN.value]
        reason = (
            f"Classification did not stabilise after {state.get('classify_attempts', 0)} "
            f"attempts: {len(unknown)} of {len(considered)} documents remain "
            f"unidentified ({', '.join(unknown[:5])}). Stopping rather than "
            "extracting facts against labels that are not trusted."
        )
        return {
            "escalated": True,
            "escalation_reason": reason,
            "events": [self._event("escalate", "halted", unknown=unknown)],
        }

    def extract(self, state: PipelineState) -> dict:
        started = time.monotonic()
        _test_stall("extract")
        meter = self._meter()
        pile_id = state["pile_id"]

        claims: list[Claim] = []
        for raw in state["documents"]:
            doc = Document.model_validate(raw)
            chunk_dicts = state["chunks"].get(str(doc.id), [])
            chunks = [Chunk.model_validate(c) for c in chunk_dicts]
            claims.extend(extract_mod.extract_claims(doc, chunks, meter, pile_id))

        return {
            "claims": [c.model_dump(mode="json") for c in claims],
            "stage_costs": [self._cost("extract", meter, started)],
            "events": [self._event(
                "extract", "extracted", claims=len(claims),
                # A document longer than the model's per-call limit is read in
                # part. Saying so matters more here than anywhere: the span
                # check that validates an extraction runs against the whole
                # text, so an attribute the model never saw and one that is
                # genuinely absent both produce no claim. Without this number a
                # register built from the first fifth of a contract would
                # report "not supported by the sources" and be believed.
                **({"chars_not_read": meter.usage.chars_unread}
                   if meter.usage.chars_unread else {}),
            )],
        }

    def screen_parties(self, state: PipelineState) -> dict:
        """Screen every party the pile names against the watchlist.

        Costs no model call: name matching is deterministic and belongs in
        code, not in a prompt. An LLM asked "is this the same company" would
        produce a confident answer with no auditable basis, and a sanctions
        decision has to be explainable to a regulator line by line.

        The engine raises and evidences alerts. It never clears one.
        """
        started = time.monotonic()
        claims = [Claim.model_validate(c) for c in state["claims"]]

        def value_of(attribute: str) -> str | None:
            for claim in claims:
                if claim.attribute == attribute and claim.value:
                    return claim.value
            return None

        watchlist_path = Path(
            state.get("watchlist_path") or "watchlists/synthetic_consolidated.json"
        )
        if not watchlist_path.is_file():
            # Screening is a capability, not a guarantee. If the list is
            # absent, say so loudly rather than reporting zero alerts, which
            # would read exactly like a clean result.
            return {
                "alerts": [],
                "watchlist_available": False,
                "stage_costs": [self._cost("screen_parties", self._meter(), started)],
                "events": [
                    self._event("screen_parties", "unavailable",
                                reason=f"no watchlist at {watchlist_path}")
                ],
            }

        watchlist, provenance = load_watchlist(watchlist_path)
        country = value_of("jurisdiction")
        incorporated = value_of("incorporation_date")

        subjects: list[tuple[str, PartyType, str]] = []
        if name := value_of("registered_name"):
            subjects.append((name, PartyType.ENTITY, "vendor"))
        if ubo := value_of("ubo_name"):
            subjects.append((ubo, PartyType.INDIVIDUAL, "ultimate beneficial owner"))

        alerts: list[Alert] = []
        for subject_name, party_type, role in subjects:
            alerts.extend(screen_subject(
                subject_name, party_type, watchlist,
                subject_role=role,
                # Jurisdiction and incorporation date belong to the vendor. A
                # UBO screened with the company's country would be discounted
                # on evidence that is not about them.
                subject_country=country if party_type is PartyType.ENTITY else None,
                subject_incorporated=incorporated if party_type is PartyType.ENTITY else None,
                # From the UBO's identity document, if the pile carries one.
                # Absent, these compare as UNAVAILABLE and push the alert
                # towards escalation, which is the correct outcome: an
                # unidentified beneficial owner matching a listed name is not
                # something to clear on a hunch.
                subject_dob=value_of("ubo_date_of_birth"),
                subject_nationality=value_of("ubo_nationality"),
                subject_document_number=value_of("ubo_document_number"),
            ))

        return {
            "alerts": [_alert_to_dict(a) for a in alerts],
            "watchlist_available": True,
            "watchlist_source": provenance.get("source", ""),
            "watchlist_name": provenance.get("source_name", ""),
            "stage_costs": [self._cost("screen_parties", self._meter(), started)],
            "events": [
                self._event(
                    "screen_parties", "screened",
                    subjects=[s[0] for s in subjects],
                    watchlist_entries=len(watchlist),
                    alerts=len(alerts),
                )
            ],
        }

    def reconcile(self, state: PipelineState) -> dict:
        started = time.monotonic()
        claims = [Claim.model_validate(c) for c in state["claims"]]
        conflicts = extract_mod.detect_conflicts(claims, state["pile_id"])
        return {
            "conflicts": [c.model_dump(mode="json") for c in conflicts],
            "stage_costs": [self._cost("reconcile", self._meter(), started)],
            "events": [
                self._event("reconcile", "compared",
                            conflicts=[c.attribute for c in conflicts])
            ],
        }

    def compose(self, state: PipelineState) -> dict:
        """Build the register. Sections are stable and independently hashed.

        Section ids never change between runs, which is what makes "this update
        touched only the screening section" a provable statement rather than an
        assurance.
        """
        started = time.monotonic()
        claims = [Claim.model_validate(c) for c in state["claims"]]
        conflicts = [Conflict.model_validate(c) for c in state["conflicts"]]
        register = build_register(
            state["pile_id"], claims, conflicts, state.get("alerts", [])
        )
        return {
            "register": register.model_dump(mode="json"),
            "stage_costs": [self._cost("compose", self._meter(), started)],
            "events": [
                self._event("compose", "built",
                            sections=[s.id for s in register.sections])
            ],
        }

    def check(self, state: PipelineState) -> dict:
        started = time.monotonic()
        claims = [Claim.model_validate(c) for c in state["claims"]]
        conflicts = [Conflict.model_validate(c) for c in state["conflicts"]]
        documents = [Document.model_validate(d) for d in state["documents"]]

        checklist = Checklist.load(Path(state["checklist_path"]))
        # `alerts` is None when screening never ran — the engine needs that
        # distinction to report NOT_EVALUATED rather than a pass.
        alerts = state.get("alerts") if state.get("watchlist_available") else None
        result = ChecklistEngine(
            claims, conflicts, documents, state["pile_id"], alerts=alerts
        ).evaluate(checklist)

        total_rules = sum(len(stage.results) for stage in result.stages)
        return {
            "findings": [f.model_dump(mode="json") for f in result.findings],
            "checklist_summary": {
                "checklist_name": result.name,
                "total_rules": total_rules,
                "passed": total_rules - len(result.findings) - len(result.not_evaluated),
                "failed": len(result.findings),
                "not_evaluated": [
                    {"rule_id": r.rule_id, "title": r.title, "note": r.note}
                    for r in result.not_evaluated
                ],
                "summary": result.summary(),
            },
            "stage_costs": [self._cost("check", self._meter(), started)],
            "events": [
                self._event(
                    "check", "evaluated",
                    findings=len(result.findings),
                    not_evaluated=[r.rule_id for r in result.not_evaluated],
                    summary=result.summary(),
                )
            ],
        }

    def gate(self, state: PipelineState) -> dict:
        """Everything consequential becomes an item awaiting a human yes or no.

        Decisions are individual. Rejecting one finding must leave every other
        pending item untouched, so they are created as separate rows rather
        than one approve-everything blob.
        """
        run_id = UUID(state["run_id"])
        pile_id = state["pile_id"]
        decisions: list[Decision] = []

        for raw in state["conflicts"]:
            conflict = Conflict.model_validate(raw)
            values = " vs ".join(repr(c.value) for c in conflict.claims)
            decisions.append(
                Decision(
                    pile_id=pile_id, run_id=run_id, kind=DecisionKind.CONFLICT,
                    subject_id=conflict.id,
                    summary=f"Sources disagree on {conflict.attribute}: {values}",
                )
            )

        for raw in state["findings"]:
            finding = Finding.model_validate(raw)
            decisions.append(
                Decision(
                    pile_id=pile_id, run_id=run_id, kind=DecisionKind.FINDING,
                    subject_id=finding.id,
                    summary=f"[{finding.severity.value}] {finding.rule_id}: {finding.title}",
                )
            )

        # Watchlist alerts. Each one is adjudicated individually and carries
        # three outcomes rather than two, because "I cannot clear this on the
        # evidence available" is a real answer and the honest one when
        # discounting evidence is absent.
        for raw in state.get("alerts", []):
            decisions.append(
                Decision(
                    pile_id=pile_id, run_id=run_id, kind=DecisionKind.ALERT,
                    subject_id=UUID(raw["id"]),
                    summary=(
                        f"{raw['subject_role']} {raw['subject_name']!r} "
                        f"~ {raw['listed_name']!r} "
                        f"({raw['listed_programme']}) at {raw['score']:.0%}"
                    ),
                )
            )

        return {
            "decisions": [d.model_dump(mode="json") for d in decisions],
            "events": [
                self._event("gate", "awaiting_human", pending=len(decisions))
            ],
        }

    def commit(self, state: PipelineState) -> dict:
        """Apply only what a human approved.

        Reads the decisions back rather than assuming them. A rejected conflict
        stays unresolved and is reported as such; it is not quietly dropped.
        """
        started = time.monotonic()
        decisions = [Decision.model_validate(d) for d in state.get("decisions", [])]
        approved = [d for d in decisions if d.state is DecisionState.APPROVED]
        rejected = [d for d in decisions if d.state is DecisionState.REJECTED]
        pending = [d for d in decisions if d.state is DecisionState.PENDING]

        escalated = [d for d in decisions if d.state is DecisionState.ESCALATED]

        register = Register.model_validate(state["register"])
        conflicts = [Conflict.model_validate(c) for c in state["conflicts"]]

        resolved = 0
        for decision in approved:
            if decision.kind is not DecisionKind.CONFLICT:
                continue
            for conflict in conflicts:
                if conflict.id == decision.subject_id and decision.chosen_value:
                    conflict.resolved_value = decision.chosen_value
                    conflict.resolved_by_decision_id = decision.id
                    resolved += 1

        # Stamp each alert with how its decision landed, so the register states
        # the outcome rather than leaving every alert reading "unresolved".
        alerts = [dict(a) for a in state.get("alerts", [])]
        by_subject = {str(d.subject_id): d for d in decisions
                      if d.kind is DecisionKind.ALERT}
        for alert in alerts:
            if decision := by_subject.get(alert["id"]):
                alert["decision_state"] = decision.state.value
                alert["decision_note"] = decision.note

        alerts_changed = any(a.get("decision_state") for a in alerts)
        if resolved or alerts_changed:
            register = build_register(
                state["pile_id"],
                [Claim.model_validate(c) for c in state["claims"]],
                conflicts,
                alerts,
            )
            register.revision = Register.model_validate(state["register"]).revision + 1

        # What holds the vendor.
        #
        # Only one disposition clears a watchlist alert: a reviewer rejecting
        # it as a false positive. Everything else holds.
        #
        #   rejected   -> discounted on evidence. Cleared.
        #   approved   -> confirmed true match. Obviously blocks.
        #   escalated  -> looked at, could not be cleared. Blocks.
        #   pending    -> nobody has looked. Blocks.
        #
        # That last line is the one that was missing, and it was the most
        # damaging omission in the system. `blockers` read only `escalated`, so
        # an alert nobody had adjudicated reported `onboarding_blocked: False`
        # — the identical result to one a reviewer had cleared. An unexamined
        # sanctions hit and a discounted one cannot say the same thing. It is
        # the same principle the checklist engine already applies with
        # NOT_EVALUATED, enforced where getting it wrong is a regulatory
        # breach rather than a bug report.
        def held(decision: Decision) -> str | None:
            if decision.kind is not DecisionKind.ALERT:
                return None
            if decision.state is DecisionState.REJECTED:
                return None
            reason = {
                DecisionState.PENDING: "not yet adjudicated by any reviewer",
                DecisionState.APPROVED: "confirmed as a true match",
                DecisionState.ESCALATED: "escalated; could not be cleared",
            }[decision.state]
            note = f" — {decision.note}" if decision.note else ""
            return f"{decision.summary} [{reason}]{note}"

        blockers = [b for b in (held(d) for d in decisions) if b]

        return {
            "register": register.model_dump(mode="json"),
            "conflicts": [c.model_dump(mode="json") for c in conflicts],
            "alerts": alerts,
            "onboarding_blocked": bool(blockers),
            "blocking_reasons": blockers,
            "stage_costs": [self._cost("commit", self._meter(), started)],
            "events": [
                self._event(
                    "commit", "applied",
                    approved=len(approved), rejected=len(rejected),
                    escalated=len(escalated), still_pending=len(pending),
                    conflicts_resolved=resolved,
                    onboarding_blocked=bool(blockers),
                )
            ],
        }

    # -- assembly --------------------------------------------------------

    def build(self, checkpointer: Any = None) -> Any:
        graph = StateGraph(PipelineState)

        graph.add_node("intake", self.intake)
        graph.add_node("screen", self.screen)
        graph.add_node("classify", self.classify)
        graph.add_node("escalate", self.escalate)
        graph.add_node("extract", self.extract)
        graph.add_node("screen_parties", self.screen_parties)
        graph.add_node("reconcile", self.reconcile)
        graph.add_node("compose", self.compose)
        graph.add_node("check", self.check)
        graph.add_node("gate", self.gate)
        graph.add_node("commit", self.commit)

        graph.set_entry_point("intake")
        graph.add_edge("intake", "screen")
        graph.add_edge("screen", "classify")

        # The decision that can change the path.
        graph.add_conditional_edges(
            "classify",
            self.after_classify,
            {"retry": "classify", "escalate": "escalate", "continue": "extract"},
        )
        graph.add_edge("escalate", END)

        graph.add_edge("extract", "screen_parties")
        graph.add_edge("screen_parties", "reconcile")
        graph.add_edge("reconcile", "compose")
        graph.add_edge("compose", "check")
        graph.add_edge("check", "gate")
        graph.add_edge("gate", "commit")
        graph.add_edge("commit", END)

        # Nothing past the gate runs until a human has decided.
        return graph.compile(checkpointer=checkpointer, interrupt_after=["gate"])


# --------------------------------------------------------------------------
# register construction
# --------------------------------------------------------------------------

# Section id -> (heading, attributes it reports on). Stable ids are what make
# "only this section changed" checkable.
_SECTIONS: list[tuple[str, str, list[str]]] = [
    ("identity", "Identity and registration",
     ["registered_name", "company_number", "incorporation_date", "jurisdiction"]),
    ("ownership", "Beneficial ownership",
     ["ubo_name", "ubo_percentage", "ubo_date_of_birth", "ubo_nationality",
      "ubo_document_number"]),
    ("screening", "Sanctions and adverse media screening",
     ["screening_result", "screening_date"]),
    ("commercial", "Commercial documents",
     ["contract_value", "effective_date", "invoice_number", "invoice_total"]),
    ("banking", "Remittance details", ["bank_iban"]),
]


# attribute -> the single section that reports it. Used by focused updates to
# work out which sections an arrival can possibly affect.
SECTION_FOR_ATTRIBUTE: dict[str, str] = {
    attribute: section_id
    for section_id, _, attributes in _SECTIONS
    for attribute in attributes
}


def build_section(
    section_id: str,
    claims: list[Claim],
    conflicts: list[Conflict],
    alerts: list[dict] | None = None,
) -> RegisterSection:
    """Build exactly one section.

    Kept separate from `build_register` on purpose: a focused update must be
    able to rebuild one section without touching, or even recomputing, any
    other. Rebuilding everything and putting the unchanged parts back would
    reproduce the same bytes while doing all the work — which is the thing the
    brief specifically calls out.
    """
    heading, attributes = next(
        (h, a) for sid, h, a in _SECTIONS if sid == section_id
    )

    by_attribute: dict[str, list[Claim]] = {}
    for claim in claims:
        if claim.attribute in attributes:
            by_attribute.setdefault(claim.attribute, []).append(claim)

    unresolved = {c.attribute for c in conflicts if not c.is_resolved}
    resolved = {c.attribute: c.resolved_value for c in conflicts if c.is_resolved}

    lines: list[str] = []
    claim_ids: list[UUID] = []
    for attribute in attributes:
        found = by_attribute.get(attribute, [])
        if attribute in unresolved:
            values = " | ".join(sorted({c.value or "" for c in found}))
            lines.append(
                f"{attribute}: DISPUTED — sources disagree ({values}). "
                "Awaiting human resolution."
            )
        elif attribute in resolved:
            lines.append(f"{attribute}: {resolved[attribute]} (resolved by reviewer)")
        elif found:
            lines.append(f"{attribute}: {found[0].value}")
        else:
            # The honest sentence, not a blank that reads as fine.
            lines.append(f"{attribute}: not supported by the sources")
        claim_ids.extend(c.id for c in found)

    # Watchlist alerts belong in the screening section and nowhere else, so an
    # arrival that only affects, say, banking still leaves this text byte-stable.
    #
    # An alert is written into the register whatever its outcome. A true match
    # and a discounted false positive are both facts about this vendor that the
    # next reviewer needs to see; deleting a cleared alert would hide the fact
    # that the vendor was ever hit, which is the record an examiner asks for.
    if section_id == "screening" and alerts is not None:
        if not alerts:
            lines.append("watchlist alerts: none raised")
        for alert in alerts:
            state = alert.get("decision_state", "pending")
            verdict = {
                "pending": "UNRESOLVED — awaiting adjudication",
                "approved": "TRUE MATCH — confirmed by reviewer",
                "rejected": "FALSE POSITIVE — discounted by reviewer",
                "escalated": "ESCALATED (abundance of caution) — ONBOARDING BLOCKED",
            }.get(state, state)
            lines.append(
                f"watchlist alert: {alert['subject_role']} "
                f"{alert['subject_name']!r} vs watchlist entry {alert['listed_uid']} "
                f"{alert['listed_name']!r} ({alert['listed_programme']}), "
                f"name score {alert['score']:.0%} — {verdict}"
            )
            if note := alert.get("decision_note"):
                lines.append(f"  reviewer note: {note}")

    return RegisterSection(
        id=section_id, heading=heading, body="\n".join(lines), claim_ids=claim_ids
    )


def build_register(
    pile_id: str,
    claims: list[Claim],
    conflicts: list[Conflict],
    alerts: list[dict] | None = None,
) -> Register:
    return Register(
        pile_id=pile_id,
        sections=[
            build_section(section_id, claims, conflicts, alerts)
            for section_id, _, _ in _SECTIONS
        ],
    )


def new_run_id() -> str:
    return str(uuid4())
