"""The gate is a barrier, not a suggestion.

The brief is unambiguous: *"Before the final output is committed, a person
reviews what the system intends to do, approves what is right and rejects what
is wrong in the same review, item by item, and the system respects every
decision."* And: *"A success message only ever means the output is genuinely in
the state it claims."*

A system that lets a run commit while items sit unreviewed satisfies neither.
It is not enough that pending items go unapplied — the run must not finish, and
nothing may report success, until a human has actually looked at every one.

These tests were written before the fix and observed to fail. If any of them
ever passes vacuously, the invariant has quietly died.
"""

from __future__ import annotations

import os
import uuid

import pytest
from langgraph.checkpoint.postgres import PostgresSaver

from app.core.llm import OfflineAdapter
from app.domain.models import Decision, DecisionKind, DecisionState
from app.pipeline.graph import Pipeline
from tests.conftest import needs_postgres
from tests.test_resilience import CONN, run_cli, state_of


def start(repo, pile="meridian") -> str:
    thread = f"gate-{uuid.uuid4().hex[:8]}"
    result = run_cli(["run", pile, "--thread", thread], repo)
    assert result.returncode == 0, result.stdout + result.stderr
    return thread


def decisions_of(thread: str) -> list[Decision]:
    return [Decision.model_validate(d) for d in state_of(thread).get("decisions", [])]


# --------------------------------------------------------------------------
# the barrier
# --------------------------------------------------------------------------


@needs_postgres
def test_resume_refuses_while_any_item_is_pending(repo):
    """The whole gate, in one assertion.

    Before the fix `resume` called `invoke(None, cfg)` after checking only that
    the thread existed, so a reviewer who settled nothing at all could still
    commit the register.
    """
    thread = start(repo)
    assert any(d.state is DecisionState.PENDING for d in decisions_of(thread)), (
        "precondition: this pile must raise something to review"
    )

    resumed = run_cli(["resume", thread], repo)
    assert resumed.returncode != 0, (
        "resume succeeded with items still pending; the gate is advisory"
    )
    combined = resumed.stdout + resumed.stderr
    assert "pending" in combined.lower()

    # And it must actually not have committed.
    values = state_of(thread)
    stages = [e["stage"] for e in values.get("events", [])]
    assert "commit" not in stages, "commit ran despite the refusal"


@needs_postgres
def test_resume_proceeds_once_every_item_is_settled(repo):
    """The barrier must be a barrier, not a wall. Settle everything and the run
    completes — otherwise the fix has simply broken the pipeline."""
    thread = start(repo)
    for index, decision in enumerate(decisions_of(thread)):
        if decision.kind is DecisionKind.CONFLICT:
            run_cli(["approve", thread, str(index), "--value", "41%"], repo)
        elif decision.kind is DecisionKind.ALERT:
            run_cli(["escalate", thread, str(index), "--note", "cannot clear"], repo)
        else:
            run_cli(["reject", thread, str(index), "--note", "reviewed"], repo)

    assert not [d for d in decisions_of(thread) if d.state is DecisionState.PENDING]

    resumed = run_cli(["resume", thread], repo)
    assert resumed.returncode == 0, resumed.stdout + resumed.stderr
    assert "commit" in [e["stage"] for e in state_of(thread).get("events", [])]


# --------------------------------------------------------------------------
# silence is not compliance
# --------------------------------------------------------------------------


@needs_postgres
def test_an_unadjudicated_alert_blocks_onboarding(repo):
    """The sharpest failure in the system, and the least obvious.

    `commit` built its blocker list from ESCALATED decisions only. A watchlist
    alert nobody had looked at therefore produced `onboarding_blocked: False` —
    the identical result to a reviewer clearing it as a false positive. An
    un-adjudicated sanctions hit and a discounted one cannot report the same
    thing.

    This is the same principle the checklist engine already applies with
    NOT_EVALUATED, enforced where getting it wrong is a regulatory breach
    rather than a bug report.
    """
    thread = start(repo)
    alerts = [d for d in decisions_of(thread) if d.kind is DecisionKind.ALERT]
    assert alerts, "precondition: meridian must raise a watchlist alert"

    # Settle everything EXCEPT the alert, then force the commit stage directly,
    # bypassing the resume guard — this asserts commit is defensive in its own
    # right and not merely protected by a check at the call site.
    for index, decision in enumerate(decisions_of(thread)):
        if decision.kind is DecisionKind.ALERT:
            continue
        if decision.kind is DecisionKind.CONFLICT:
            run_cli(["approve", thread, str(index), "--value", "41%"], repo)
        else:
            run_cli(["reject", thread, str(index), "--note", "reviewed"], repo)

    values = state_of(thread)
    result = Pipeline(OfflineAdapter()).commit(values)

    assert result["onboarding_blocked"] is True, (
        "an alert nobody adjudicated reported the same onboarding state as a "
        "cleared one"
    )
    # The reason must say why it is held, not merely that it is. A reviewer
    # reading the register has to be able to tell "nobody looked" apart from
    # "somebody looked and could not clear it".
    assert any("not yet adjudicated" in reason for reason in result["blocking_reasons"]), (
        result["blocking_reasons"]
    )


@needs_postgres
def test_a_discounted_alert_does_not_block(repo):
    """The other side of the same coin: rejecting an alert as a false positive
    is a real disposition and must let the vendor through. Without this, the
    test above could be satisfied by blocking unconditionally."""
    thread = start(repo)
    for index, decision in enumerate(decisions_of(thread)):
        if decision.kind is DecisionKind.CONFLICT:
            run_cli(["approve", thread, str(index), "--value", "41%"], repo)
        else:
            run_cli(["reject", thread, str(index), "--note", "discounted"], repo)

    result = Pipeline(OfflineAdapter()).commit(state_of(thread))
    assert result["onboarding_blocked"] is False, result["blocking_reasons"]


# --------------------------------------------------------------------------
# every surface, not just the one that was tested
# --------------------------------------------------------------------------


@needs_postgres
def test_the_api_refuses_to_resume_with_pending_items(repo):
    """A guard on the CLI alone would be theatre: the browser and any agent
    would still walk straight past it."""
    from fastapi.testclient import TestClient

    from app.api.main import app

    client = TestClient(app)
    started = client.post("/api/runs", json={"pile": "meridian"}).json()
    thread = started["thread"]
    assert [d for d in started["decisions"] if d["state"] == "pending"]

    response = client.post(f"/api/runs/{thread}/resume")
    assert response.status_code == 409, (
        f"expected 409, got {response.status_code}: {response.text}"
    )
    assert "pending" in response.text.lower()


@needs_postgres
def test_mcp_refuses_to_resume_with_pending_items(repo):
    """The machine interface drives the same gate as the human one."""
    from app.mcp import server

    thread = start(repo)
    result = server.resume_run.fn(thread) if hasattr(server.resume_run, "fn") \
        else server.resume_run(thread)
    assert "error" in result, result
    assert "pending" in result["error"].lower()
