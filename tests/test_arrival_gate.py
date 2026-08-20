"""An arriving document proposes; it does not decide.

The brief lists three things a human gates, and updates is one of them:
*"Conflicts, findings, **and updates** are approved or rejected by a person
before they commit."*

Before this change, an arrival wrote the register, the claims and the conflicts
straight through `update_state`. No decision was created. `DecisionKind.UPDATE`
was defined in the domain model and constructed nowhere — there was even an
error message describing a code path that did not exist. The focused update,
the one operation that changes a register a reviewer has already signed off,
was the only mutation in the system that nobody had to approve.

These tests were written before the fix and observed to fail.
"""

from __future__ import annotations

import uuid

import pytest

from app.domain.models import Decision, DecisionKind, DecisionState, Register
from tests.conftest import needs_postgres
from tests.test_gate import decisions_of, start
from tests.test_resilience import run_cli, state_of

ARRIVAL = "meridian_09_rescreen.txt"


def settle_everything(repo, thread) -> None:
    """Get the run past its initial gate so arrivals are the thing under test."""
    for index, decision in enumerate(decisions_of(thread)):
        if decision.state is not DecisionState.PENDING:
            continue
        if decision.kind is DecisionKind.CONFLICT:
            run_cli(["approve", thread, str(index), "--value", "41%"], repo)
        elif decision.kind is DecisionKind.ALERT:
            run_cli(["reject", thread, str(index), "--note", "discounted"], repo)
        else:
            run_cli(["reject", thread, str(index), "--note", "reviewed"], repo)
    resumed = run_cli(["resume", thread], repo)
    assert resumed.returncode == 0, resumed.stdout + resumed.stderr


@pytest.fixture
def committed(repo):
    thread = start(repo)
    settle_everything(repo, thread)
    return thread


def register_of(thread) -> Register:
    return Register.model_validate(state_of(thread)["register"])


def update_decisions(thread) -> list[tuple[int, Decision]]:
    return [(i, d) for i, d in enumerate(decisions_of(thread))
            if d.kind is DecisionKind.UPDATE]


# --------------------------------------------------------------------------
# proposing
# --------------------------------------------------------------------------


@needs_postgres
def test_an_arrival_proposes_and_changes_nothing(repo, committed):
    """The heart of it. An arrival must leave the register byte-identical
    until somebody approves the update."""
    before = register_of(committed)
    assert not update_decisions(committed), "precondition: no update pending yet"

    result = run_cli(["arrival", committed, ARRIVAL], repo)
    assert result.returncode == 0, result.stdout + result.stderr

    after = register_of(committed)
    assert after.hashes() == before.hashes(), (
        "an arrival changed the register before anyone approved it"
    )
    assert after.revision == before.revision

    proposed = update_decisions(committed)
    assert len(proposed) == 1, "an arrival must raise exactly one update decision"
    index, decision = proposed[0]
    assert decision.state is DecisionState.PENDING
    assert ARRIVAL in decision.summary


@needs_postgres
def test_the_proposal_says_which_sections_it_would_touch(repo, committed):
    """A reviewer approving an update blind is not reviewing anything. The
    proposal has to state what would change and what would not."""
    run_cli(["arrival", committed, ARRIVAL], repo)
    proposal = state_of(committed).get("proposed_update")

    assert proposal, "no proposal was recorded"
    assert proposal["sections_rebuilt"] == ["screening"]
    assert set(proposal["sections_untouched"]) == {
        "identity", "ownership", "commercial", "banking"
    }
    assert proposal["claims_added"], "a proposal with no claims is not an update"


# --------------------------------------------------------------------------
# rejecting
# --------------------------------------------------------------------------


@needs_postgres
def test_a_rejected_arrival_leaves_every_section_byte_identical(repo, committed):
    """Rejection has to be real. If the register moves anyway, the review was
    decoration."""
    before = register_of(committed)
    run_cli(["arrival", committed, ARRIVAL], repo)
    index, _ = update_decisions(committed)[0]

    rejected = run_cli(
        ["reject", committed, str(index), "--note", "re-screen not corroborated"], repo
    )
    assert rejected.returncode == 0, rejected.stdout + rejected.stderr

    after = register_of(committed)
    assert after.hashes() == before.hashes(), "a rejected update still changed the register"
    assert state_of(committed).get("proposed_update") in (None, {}), (
        "the discarded proposal is still sitting in state"
    )
    # The rejection itself stays on the record.
    assert decisions_of(committed)[index].state is DecisionState.REJECTED


# --------------------------------------------------------------------------
# approving
# --------------------------------------------------------------------------


@needs_postgres
def test_an_approved_arrival_rebuilds_only_its_section(repo, committed):
    before = register_of(committed)
    run_cli(["arrival", committed, ARRIVAL], repo)
    index, _ = update_decisions(committed)[0]

    approved = run_cli(["approve", committed, str(index)], repo)
    assert approved.returncode == 0, approved.stdout + approved.stderr

    after = register_of(committed)
    moved = [s for s, h in before.hashes().items() if after.hashes()[s] != h]
    assert moved == ["screening"]
    assert after.revision == before.revision + 1


@needs_postgres
def test_an_approved_arrivals_citations_reslice_and_verify(repo, committed):
    """The arrival document was never persisted, only its claims. So every
    claim an arrival contributed rendered as `filename: unknown, verified:
    False` — the provenance panel broke on exactly the operation it exists to
    demonstrate."""
    run_cli(["arrival", committed, ARRIVAL], repo)
    index, _ = update_decisions(committed)[0]
    run_cli(["approve", committed, str(index)], repo)

    values = state_of(committed)
    filenames = {d["filename"] for d in values["documents"]}
    assert ARRIVAL in filenames, "the arrival document was never persisted"

    from app.api.main import _provenance

    sources = _provenance(values)["screening_result"]
    from_arrival = [s for s in sources if s["filename"] == ARRIVAL]
    assert from_arrival, "no citation traced back to the arrival"
    for source in from_arrival:
        assert source["verified"] is True, (
            f"arrival citation does not re-slice: {source}"
        )
        assert source["quote"]


@needs_postgres
def test_arrival_cost_reaches_the_run_ledger(repo, committed):
    """`apply_arrival` builds a real StageCost and nothing persisted it, so the
    run's totals under-reported every applied update, permanently."""
    before = sum(c["model_calls"] for c in state_of(committed)["stage_costs"])
    run_cli(["arrival", committed, ARRIVAL], repo)
    index, _ = update_decisions(committed)[0]
    run_cli(["approve", committed, str(index)], repo)

    costs = state_of(committed)["stage_costs"]
    after = sum(c["model_calls"] for c in costs)
    assert after > before, "the arrival's model calls never reached the ledger"
    assert any(c["stage"].startswith("arrival") for c in costs), (
        [c["stage"] for c in costs]
    )


# --------------------------------------------------------------------------
# the limits, stated rather than discovered
# --------------------------------------------------------------------------


@needs_postgres
def test_a_second_arrival_queues_instead_of_computing_early(repo, committed):
    """Stacking proposals would need rebasing: the second cannot be computed
    against a register the first may still change. That guarantee is the
    part that must never regress. What changed is the response to it: a
    second submission used to be refused outright and had to be retried by
    hand later; it now queues, visibly, and nothing about it is computed
    (no claims extracted, no decision raised) until the first is settled."""
    run_cli(["arrival", committed, ARRIVAL], repo)
    assert len(update_decisions(committed)) == 1, "precondition: one proposal outstanding"

    second = run_cli(["arrival", committed, "meridian_10_amendment_one.txt"], repo)
    assert second.returncode == 0, second.stdout + second.stderr
    assert "queued" in second.stdout.lower()

    values = state_of(committed)
    assert values.get("pending_arrivals") == ["meridian_10_amendment_one.txt"]
    # Still exactly one UPDATE decision — the guarantee under test. Queueing
    # must not have computed the second one early.
    assert len(update_decisions(committed)) == 1


@needs_postgres
def test_a_queued_arrival_becomes_available_once_the_first_settles(repo, committed):
    """Queued, not automatic — the reviewer re-applies it themselves once
    they're done with the first, using the exact same command. Auto-advancing
    would mean either touching settle() (used by every decision kind, on
    every surface) or duplicating advance logic three times over; queueing
    without auto-advance gets the same practical outcome — no dead-end error
    — at much lower risk to the one function everything else depends on."""
    run_cli(["arrival", committed, ARRIVAL], repo)
    index, _ = update_decisions(committed)[0]
    run_cli(["arrival", committed, "meridian_10_amendment_one.txt"], repo)

    resolved = run_cli(["approve", committed, str(index)], repo)
    assert resolved.returncode == 0, resolved.stdout + resolved.stderr

    retried = run_cli(["arrival", committed, "meridian_10_amendment_one.txt"], repo)
    assert retried.returncode == 0, retried.stdout + retried.stderr
    assert "PROPOSED UPDATE" in retried.stdout

    assert len(update_decisions(committed)) == 2
    assert state_of(committed).get("pending_arrivals") == [], (
        "settling the retry must clear it from the queue, not leave a stale entry"
    )
