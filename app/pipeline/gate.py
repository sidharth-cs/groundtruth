"""The rule that makes review a barrier rather than a formality.

One definition, used by every surface. A guard that lives in the CLI alone is
theatre: the browser and any agent driving the MCP server would walk straight
past it, and the brief requires that a machine and a human drive the same flow
through the same gate.

The invariant, stated plainly so the test can quote it:

    No run may commit while any item raised for review is still pending.

Rejecting an item is a decision. Escalating one is a decision. Leaving it alone
is not — it means nobody has looked yet, and a register produced without anyone
looking cannot honestly be called reviewed.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from app.core.llm import ModelAdapter
from app.domain.models import (
    Chunk,
    Claim,
    Conflict,
    Decision,
    DecisionKind,
    DecisionState,
    Document,
    Register,
    StageCost,
)
from app.pipeline.update import apply_arrival


class GateError(Exception):
    """A refusal a caller should report, not a crash. Every surface turns this
    into its own idiom: an HTTP status, an exit code, an error dict."""


def unsettled(values: dict) -> list[tuple[int, Decision]]:
    """Every item still awaiting a human, with its review index.

    The index is what a reviewer acts on, so it is carried alongside the
    decision — an error that says "3 items pending" is a complaint, while one
    that says "items 2, 4 and 5 are pending" is instructions.
    """
    return [
        (index, decision)
        for index, decision in enumerate(
            Decision.model_validate(d) for d in values.get("decisions", [])
        )
        if decision.state is DecisionState.PENDING
    ]


def refusal(items: list[tuple[int, Decision]]) -> str:
    """The message every surface gives when it refuses to commit."""
    listed = "; ".join(f"[{i}] {d.kind.value}: {d.summary}" for i, d in items)
    return (
        f"{len(items)} item(s) still pending review, so this run cannot commit. "
        f"Approve, reject or escalate each one first: {listed}"
    )


# --------------------------------------------------------------------------
# arrivals — proposed, never applied
# --------------------------------------------------------------------------


def propose_arrival(
    values: dict, path: Path, adapter: ModelAdapter
) -> tuple[dict, dict | None]:
    """Work out what an arriving document *would* change, and raise it for
    review — or queue it if a proposal is already outstanding.

    A second arrival cannot be computed while one is outstanding: it would be
    reasoned about against a register the first proposal may still change
    underneath it. That guarantee does not move. What used to happen instead
    was a hard refusal — honest, but a dead end that had to be retried by
    hand. Queueing keeps the exact same guarantee (nothing outstanding is
    ever computed early — see the early return below, which computes nothing
    and raises no decision) while giving the second submission somewhere to
    go: calling this again with the same path, after the outstanding one is
    settled, computes it normally.

    Deliberately not auto-advanced from inside `settle()`. `settle()` is the
    one function every decision, on every surface, goes through — teaching it
    about arrivals specifically would put arrival-only reasoning in the most
    shared function in the module for a queue with at most a few filenames in
    it. A reviewer re-submitting the same path once is a smaller cost than
    that.

    Returns `(state_patch, proposal)`. `proposal` is `None` when the file
    only queued: nothing was computed and no decision was raised. The patch
    deliberately contains no `register`, `claims` or `conflicts` key when a
    proposal *is* computed: an arrival is the one operation that rewrites a
    register a reviewer has already signed off, so it is the last one that
    should be allowed to do it unattended.
    """
    filename = path.name
    # Drop any stale entry for this exact file first — covers both the retry
    # that is about to compute it for real, and a resubmission of a file
    # that's already queued (moves it to the back rather than duplicating it).
    pending = [f for f in values.get("pending_arrivals", []) if f != filename]

    if values.get("proposed_update"):
        pending.append(filename)
        return {
            "pending_arrivals": pending,
            "events": [{
                "stage": "arrival", "decision": "queued",
                "because_of": filename, "queue_position": len(pending),
                "awaiting": "the outstanding update to be settled first",
            }],
        }, None

    before = Register.model_validate(values["register"])
    result = apply_arrival(
        path,
        before,
        [Claim.model_validate(c) for c in values["claims"]],
        [Conflict.model_validate(c) for c in values["conflicts"]],
        adapter,
        values["pile_id"],
        alerts=values.get("alerts", []),
    )
    proposal = result.to_proposal(before.hashes())

    changed = ", ".join(result.sections_rebuilt) or "nothing"
    decisions = list(values.get("decisions", []))
    decisions.append(
        Decision(
            pile_id=values["pile_id"],
            run_id=values["run_id"],
            kind=DecisionKind.UPDATE,
            # The proposal is the subject. It has no id of its own, so the
            # decision carries one and the proposal records it back.
            subject_id=uuid4(),
            summary=(
                f"{result.arrival} ({result.document.kind.value}) would rebuild "
                f"{changed}; {len(result.new_claims)} claim(s), "
                f"{len(result.new_conflicts)} conflict(s)"
            ),
        ).model_dump(mode="json")
    )
    proposal["decision_id"] = decisions[-1]["id"]

    return {
        "proposed_update": proposal,
        "decisions": decisions,
        # Clears this filename out of the queue if it was sitting there from
        # an earlier queued attempt — see the dedup at the top of this
        # function. Unchanged (still []) on a first-time, never-queued call.
        "pending_arrivals": pending,
        "events": [{
            "stage": "arrival", "decision": "proposed",
            "because_of": result.arrival,
            "would_rebuild": result.sections_rebuilt,
            "awaiting": "human approval",
        }],
    }, proposal


def _apply_proposal(values: dict, proposal: dict) -> dict:
    """Fold an approved update into the record.

    The arriving document and its chunks land here and nowhere else. Persisting
    them is what lets a citation from an arrival be re-sliced from its source
    later; without it the register can assert where a claim came from but not
    show it, which is the same as not having provenance at all.
    """
    document = Document.model_validate(proposal["document"])
    chunks = dict(values.get("chunks") or {})
    chunks[str(document.id)] = proposal["chunks"]

    patch = {
        "register": proposal["register"],
        "claims": list(values.get("claims", [])) + proposal["new_claims"],
        "conflicts": list(values.get("conflicts", [])) + proposal["new_conflicts"],
        "documents": list(values.get("documents", [])) + [proposal["document"]],
        "chunks": chunks,
        "proposed_update": None,
        "events": [{
            "stage": "arrival", "decision": "applied",
            "because_of": proposal["arrival"],
            "sections_rebuilt": proposal["sections_rebuilt"],
            "sections_untouched": proposal["sections_untouched"],
            "claims_added": proposal["claims_added"],
            "conflicts_raised": proposal["conflicts_raised"],
            "value_changes": proposal.get("value_changes", []),
        }],
    }
    # The ledger has to include work that actually happened. An arrival's model
    # calls were computed and then dropped, so every applied update made the
    # run's totals quietly wrong.
    if proposal.get("cost"):
        patch["stage_costs"] = [proposal["cost"]]
    return patch


def _discard_proposal(proposal: dict) -> dict:
    """A rejected update leaves the register byte-identical. The rejection
    itself stays on the record — the decision keeps its state and note."""
    return {
        "proposed_update": None,
        "events": [{
            "stage": "arrival", "decision": "rejected",
            "because_of": proposal["arrival"],
            "register_unchanged": True,
        }],
    }


# --------------------------------------------------------------------------
# settling
# --------------------------------------------------------------------------


def settle(
    values: dict,
    index: int,
    state: DecisionState,
    chosen_value: str | None = None,
    note: str | None = None,
    by: str | None = None,
) -> dict:
    """Settle exactly one item and return the state patch.

    One definition for the CLI, the HTTP API and the MCP server. Three copies
    of this drifted apart once already; a gate that means something different
    depending on which door you came through is not a gate.

    Settling an UPDATE is what applies or discards its proposal — the decision
    and the mutation it authorises cannot be separated, or the authorisation
    becomes advisory again.
    """
    decisions = [Decision.model_validate(d) for d in values.get("decisions", [])]
    if not 0 <= index < len(decisions):
        raise GateError(f"index {index} out of range (0..{len(decisions) - 1})")
    if decisions[index].state is not DecisionState.PENDING:
        raise GateError(
            f"decision {index} is already {decisions[index].state.value}; "
            "decisions are settled once"
        )

    decisions[index] = decisions[index].settle(
        state, chosen_value=chosen_value, note=note, by=by
    )
    patch: dict = {"decisions": [d.model_dump(mode="json") for d in decisions]}

    if decisions[index].kind is DecisionKind.UPDATE:
        proposal = values.get("proposed_update")
        if not proposal:
            raise GateError(
                f"decision {index} gates an update, but no proposal is outstanding"
            )
        resolved = (
            _apply_proposal(values, proposal) if state is DecisionState.APPROVED
            else _discard_proposal(proposal)
        )
        # decisions must win: the patch above already carries the settled list.
        patch = {**resolved, **patch}

    return patch
