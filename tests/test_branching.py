"""The decision that changes the path.

Behaviour one asks for stages whose decisions can alter the route — a retry, a
skip, an escalation. Two things have to be true for that to be real rather than
decorative:

  * the branch must actually be reachable on some input, and
  * the retry must do something different from the attempt that just failed.

A retry that repeats an identical deterministic call gets an identical answer.
That is a loop with a label on it. So the second attempt uses a different
strategy, and these tests cover both the strategy and the route.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from app.core.llm import Meter, OfflineAdapter
from app.domain.models import DocumentKind
from app.pipeline.extract import classify, screen_document
from app.pipeline.graph import Pipeline
from app.pipeline.ingest import load_pile
from tests.conftest import needs_postgres


def unknown_ratio(pile_dir, pile_id, widened: bool) -> float:
    meter = Meter(OfflineAdapter(), max_calls=200)
    considered = unknown = 0
    for document, _ in load_pile(pile_dir, pile_id):
        document = screen_document(document)
        if document.status.value == "quarantined":
            continue
        considered += 1
        if classify(document, meter, widened=widened) is DocumentKind.UNKNOWN:
            unknown += 1
    return unknown / considered if considered else 0.0


def test_widened_pass_is_a_different_strategy_not_a_repeat(repo):
    """If both passes agreed on everything, retrying would be pointless."""
    primary = unknown_ratio(repo / "corpora" / "ambiguous", "ambiguous", widened=False)
    widened = unknown_ratio(repo / "corpora" / "ambiguous", "ambiguous", widened=True)
    assert primary > 0.34, "fixture no longer trips the tolerance; the branch is unreachable"
    assert widened < primary, "the widened pass returned the same answers — that is not a retry"
    assert widened == 0.0


def test_widened_pass_does_not_change_the_normal_piles(repo):
    """The looser strategy must not leak into piles that classify cleanly.

    A second pass that improves the hard case by mislabelling the easy one has
    bought its correctness somewhere else.
    """
    for pile in ("meridian", "northwind"):
        assert unknown_ratio(repo / "corpora" / pile, pile, widened=False) == 0.0


def test_unidentifiable_pile_defeats_both_passes(repo):
    """Escalation needs an input that genuinely cannot be labelled."""
    for widened in (False, True):
        assert unknown_ratio(
            repo / "corpora" / "unidentifiable", "unidentifiable", widened=widened
        ) == 1.0


def _run(pile: str, thread: str, repo) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "app.cli", "run", pile, "--thread", thread],
        cwd=repo, env={**os.environ, "LLM_PROVIDER": "offline"},
        capture_output=True, text=True, timeout=180,
    )


@needs_postgres
def test_route_retry_then_continue(repo):
    import uuid

    thread = f"branch-retry-{uuid.uuid4().hex[:6]}"
    result = _run("ambiguous", thread, repo)
    assert result.returncode == 0, result.stdout + result.stderr

    out = result.stdout
    assert "strategy=primary" in out
    assert "widened_retry" in out, "the retry branch was not taken"
    assert "attempt=2" in out
    assert "escalate" not in out, "it escalated when the retry should have rescued it"
    # having recovered, it must go on to do the actual work
    assert "extract" in out and "gate" in out


@needs_postgres
def test_route_retry_then_escalate(repo):
    import uuid

    thread = f"branch-esc-{uuid.uuid4().hex[:6]}"
    result = _run("unidentifiable", thread, repo)
    assert result.returncode == 0, result.stdout + result.stderr

    out = result.stdout
    assert "widened_retry" in out, "it escalated without retrying first"
    assert "ESCALATED" in out
    assert "Stopping rather than extracting facts" in out

    # The point of escalating is that the work does not happen. Read the stage
    # list itself rather than the whole output: the reason text mentions
    # extraction, and the cost table is also indented and keyed by stage name.
    stage_block = out.split("STAGES", 1)[1].split("COST AND TIME", 1)[0]
    stages = [
        line.split()[0]
        for line in stage_block.splitlines()
        if line.startswith("  ") and line.strip()
    ]
    assert stages == ["intake", "screen", "classify", "classify", "escalate"], stages
    assert "extract" not in stages, "it extracted facts against labels it did not trust"
    assert "gate" not in stages, "it reached the gate despite escalating"


@needs_postgres
def test_escalation_still_reports_what_it_spent(repo):
    """A halted run must still account for the work it did before stopping."""
    import uuid

    thread = f"branch-cost-{uuid.uuid4().hex[:6]}"
    out = _run("unidentifiable", thread, repo).stdout
    assert "COST AND TIME" in out
    # two classification attempts over three documents
    assert "total        6 calls" in out.replace("  ", "  ")


def test_retry_is_bounded(repo):
    """Bounded retry is resilience. Unbounded retry is a spend leak."""
    pipeline = Pipeline(OfflineAdapter(), max_stage_retries=2)
    state = {
        "documents": [
            {"status": "parsed", "kind": "unknown", "filename": f"d{i}.txt"}
            for i in range(3)
        ],
        "classify_attempts": 2,
    }
    assert pipeline.after_classify(state) == "escalate"

    state["classify_attempts"] = 1
    assert pipeline.after_classify(state) == "retry"


def test_quarantined_documents_do_not_trigger_escalation():
    """They are unlabelled on purpose, so they must not count as failures."""
    pipeline = Pipeline(OfflineAdapter(), max_stage_retries=2)
    state = {
        "documents": [
            {"status": "quarantined", "kind": "unknown", "filename": "attack.txt"},
            {"status": "parsed", "kind": "invoice", "filename": "inv.txt"},
        ],
        "classify_attempts": 1,
    }
    assert pipeline.after_classify(state) == "continue"
