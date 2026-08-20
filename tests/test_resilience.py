"""The two claims that cannot be proven by reading the code.

A run killed mid-flight and resumed, and two runs at once. Both need a real
database and a real second process, so both are integration tests against
Postgres rather than unit tests against a stand-in.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
import uuid
from collections import Counter
from pathlib import Path

import pytest
from langgraph.checkpoint.postgres import PostgresSaver

from app.core.llm import OfflineAdapter
from app.core.locks import PileBusy, pile_lock
from app.pipeline.graph import Pipeline
from tests.conftest import needs_postgres

CONN = os.environ["DATABASE_URL"] + "?sslmode=disable"


def state_of(thread: str) -> dict:
    with PostgresSaver.from_conn_string(CONN) as cp:
        app = Pipeline(OfflineAdapter()).build(checkpointer=cp)
        return app.get_state({"configurable": {"thread_id": thread}}).values


def run_cli(args: list[str], repo: Path, **kwargs) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["LLM_PROVIDER"] = "offline"
    return subprocess.run(
        [sys.executable, "-m", "app.cli", *args],
        cwd=repo, env=env, capture_output=True, text=True, **kwargs
    )


@needs_postgres
def test_killed_run_resumes_without_repeating_work(repo):
    """SIGKILL mid-stage, then resume in a fresh process.

    The assertion is not merely that it finishes — a restart from scratch would
    also finish. It is that no stage runs twice and no model call is paid for
    twice.
    """
    thread = f"killtest-{uuid.uuid4().hex[:8]}"
    env = dict(os.environ)
    env.update({
        "LLM_PROVIDER": "offline",
        "DOCTASK_STALL_STAGE": "extract",   # hold one stage open so the kill lands inside it
        "DOCTASK_STALL_SECONDS": "30",
    })

    process = subprocess.Popen(
        [sys.executable, "-m", "app.cli", "run", "meridian", "--thread", thread],
        cwd=repo, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    try:
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            if state_of(thread):        # a checkpoint exists, so stages have completed
                break
            time.sleep(0.4)
        else:
            pytest.fail("run never checkpointed before the timeout")
        time.sleep(1.0)                 # let it get inside the stalled stage
        assert process.poll() is None, "process exited before it could be killed"
        os.kill(process.pid, signal.SIGKILL)
    finally:
        process.wait(timeout=15)

    assert process.returncode not in (0, None), "process should have died, not completed"

    resumed = run_cli(["resume", thread], repo)
    assert resumed.returncode == 0, resumed.stdout + resumed.stderr

    values = state_of(thread)
    executions = Counter(event["stage"] for event in values["events"])
    repeated = {stage: n for stage, n in executions.items() if n > 1}
    assert not repeated, f"work was repeated after resume: {repeated}"

    calls = sum(c["model_calls"] for c in values["stage_costs"])
    assert calls == 14, f"expected 14 model calls as in an uninterrupted run, got {calls}"
    assert len(values["claims"]) == 15
    # 1 conflict + 4 findings + 1 watchlist alert.
    assert len(values["decisions"]) == 6


@needs_postgres
def test_two_runs_on_different_piles_do_not_contend(repo):
    """Independent piles must not serialise against each other."""
    started = time.monotonic()
    processes = [
        subprocess.Popen(
            [sys.executable, "-m", "app.cli", "run", pile,
             "--thread", f"par-{pile}-{uuid.uuid4().hex[:6]}", "--lock-wait", "5"],
            cwd=repo, env={**os.environ, "LLM_PROVIDER": "offline"},
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        for pile in ("meridian", "northwind")
    ]
    outputs = [(p.wait(timeout=120), p.stdout.read()) for p in processes]
    elapsed = time.monotonic() - started

    for code, output in outputs:
        assert code == 0, output
        assert "pile busy" not in output
    assert elapsed < 120


@needs_postgres
def test_same_pile_twice_is_serialised_not_corrupted(repo):
    """Two runs against one pile must not interleave.

    The second either waits its turn or is told the pile is busy. What must not
    happen is both proceeding and one overwriting the other's state.
    """
    thread_a = f"lock-a-{uuid.uuid4().hex[:6]}"
    thread_b = f"lock-b-{uuid.uuid4().hex[:6]}"

    with pile_lock("meridian", wait_seconds=5):
        # The lock is held here, so a run must not be able to start.
        blocked = run_cli(
            ["run", "meridian", "--thread", thread_a, "--lock-wait", "1"], repo
        )
        assert blocked.returncode == 3, blocked.stdout + blocked.stderr
        assert "pile busy" in blocked.stderr
        assert not state_of(thread_a), "a blocked run must not leave partial state"

    # Lock released: the same command now succeeds.
    allowed = run_cli(
        ["run", "meridian", "--thread", thread_b, "--lock-wait", "10"], repo
    )
    assert allowed.returncode == 0, allowed.stdout + allowed.stderr
    values = state_of(thread_b)
    assert len(values["decisions"]) == 6


@needs_postgres
def test_lock_is_released_when_the_holder_dies():
    """A SIGKILL mid-run must not wedge a pile forever.

    Advisory locks are session-scoped, so the database releases them when the
    connection drops. Worth asserting rather than assuming: the alternative is
    a pile nobody can process again until someone restarts Postgres.
    """
    pile = f"ephemeral-{uuid.uuid4().hex[:8]}"
    code = (
        "import time, os, sys;"
        "sys.path.insert(0, os.getcwd());"
        "from app.core.locks import pile_lock;"
        f"ctx = pile_lock({pile!r}, wait_seconds=5);"
        "ctx.__enter__();"
        "print('locked', flush=True);"
        "time.sleep(60)"
    )
    holder = subprocess.Popen(
        [sys.executable, "-c", code],
        cwd=Path(__file__).resolve().parent.parent,
        env={**os.environ, "LLM_PROVIDER": "offline"},
        stdout=subprocess.PIPE, text=True,
    )
    try:
        assert holder.stdout.readline().strip() == "locked"
        with pytest.raises(PileBusy):
            with pile_lock(pile, wait_seconds=1):
                pass
        os.kill(holder.pid, signal.SIGKILL)
        holder.wait(timeout=10)
        time.sleep(0.5)
        # Must now be acquirable by anyone.
        with pile_lock(pile, wait_seconds=5):
            pass
    finally:
        if holder.poll() is None:
            holder.kill()
