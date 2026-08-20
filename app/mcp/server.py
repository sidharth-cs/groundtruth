"""MCP server — the machine interface.

Everything a person can do through the CLI, another program can do here, and
that deliberately includes the approval gate. `approve_decision` and
`reject_decision` are tools like any other: whoever drives this system, human
or agent, makes that call explicitly. There is no path that commits a register
without someone — or something — having said yes to each item.

That is the whole point of exposing approval rather than hiding it. An
interface that runs the pipeline but keeps the gate behind a web page has not
made the system machine-drivable; it has made the machine wait for a human to
click. Here the gate is an operation, and refusing to approve is a first-class
outcome rather than a timeout.

    python -m app.mcp.server          # stdio, for an agent to connect to
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from langgraph.checkpoint.postgres import PostgresSaver
from mcp.server import MCPServer

from app.core.config import get_settings
from app.core.llm import build_adapter
from app.core.locks import PileBusy, case_lock, pile_lock
from app.domain.models import Claim, Conflict, Decision, DecisionKind, DecisionState, Register
from app.pipeline.gate import GateError, propose_arrival, refusal, unsettled
from app.pipeline.gate import settle as gate_settle
from app.pipeline.graph import Pipeline, new_run_id

DEFAULT_CHECKLIST = "checklists/vendor_onboarding_v1.yaml"

server = MCPServer(
    name="doctask",
    version="0.1.0",
    instructions=(
        "Owns a pile of vendor due-diligence documents: builds a cited register, "
        "checks it against a rule file, and keeps it current as new documents "
        "arrive. Consequential decisions are gated — call list_decisions, then "
        "approve_decision or reject_decision per item, then resume_run. Nothing "
        "is committed until each item has been decided explicitly."
    ),
)


def _conn() -> str:
    url = get_settings().database_url
    return url if "sslmode=" in url else f"{url}?sslmode=disable"


def _pipeline() -> Pipeline:
    settings = get_settings()
    return Pipeline(
        build_adapter(settings),
        max_model_calls=settings.max_model_calls_per_run,
        max_stage_retries=settings.max_stage_retries,
    )


def _cfg(thread: str) -> dict[str, Any]:
    return {"configurable": {"thread_id": thread}}


def _costs(state: dict) -> dict:
    costs = state.get("stage_costs", [])
    return {
        "by_stage": [
            {"stage": c["stage"], "model_calls": c["model_calls"],
             "wall_ms": c["wall_ms"], "usd": round(c["usd"], 6)}
            for c in costs
        ],
        "total_model_calls": sum(c["model_calls"] for c in costs),
        "total_wall_ms": sum(c["wall_ms"] for c in costs),
        "total_usd": round(sum(c["usd"] for c in costs), 6),
    }


def _decisions(state: dict) -> list[dict]:
    out = []
    for i, raw in enumerate(state.get("decisions", [])):
        d = Decision.model_validate(raw)
        out.append({
            "index": i,
            "kind": d.kind.value,
            "state": d.state.value,
            "summary": d.summary,
            "chosen_value": d.chosen_value,
            "note": d.note,
        })
    return out


@server.tool(
    description=(
        "Run a document pile through the pipeline, stopping at the approval "
        "gate. Returns the thread id needed for every later call, the stages "
        "that ran, what each cost, and the decisions now awaiting a yes or no. "
        "Nothing is committed by this call."
    )
)
def run_pile(pile: str, checklist: str = DEFAULT_CHECKLIST, thread: str | None = None) -> dict:
    pile_path = Path(pile) if Path(pile).exists() else Path("corpora") / pile
    if not pile_path.is_dir():
        return {"error": f"no such pile: {pile_path}"}

    run_id = new_run_id()
    thread = thread or f"{pile_path.name}-{run_id[:8]}"

    # run_pile held no lock and could not see the one the CLI and API take, so
    # a machine-driven run could start against a pile a human was already
    # running. Same lock, same key.
    with pile_lock(pile_path.name, wait_seconds=10), \
            PostgresSaver.from_conn_string(_conn()) as cp:
        cp.setup()
        app = _pipeline().build(checkpointer=cp)
        state = app.invoke(
            {"run_id": run_id, "pile_id": pile_path.name,
             "pile_path": str(pile_path), "checklist_path": checklist},
            _cfg(thread),
        )
        paused_before = list(app.get_state(_cfg(thread)).next)

    if state.get("escalated"):
        return {
            "thread": thread,
            "outcome": "escalated",
            "reason": state["escalation_reason"],
            "events": state.get("events", []),
            "cost": _costs(state),
        }

    return {
        "thread": thread,
        "outcome": "awaiting_approval",
        "paused_before": paused_before,
        "events": state.get("events", []),
        "cost": _costs(state),
        "decisions": _decisions(state),
        "next_step": (
            "Call approve_decision or reject_decision for each pending index, "
            "then resume_run. Rejecting one item leaves the others untouched."
        ),
    }


@server.tool(
    description="List every decision for a run and its current state."
)
def list_decisions(thread: str) -> dict:
    with PostgresSaver.from_conn_string(_conn()) as cp:
        app = _pipeline().build(checkpointer=cp)
        snapshot = app.get_state(_cfg(thread))
        if not snapshot.values:
            return {"error": f"no run found for thread {thread!r}"}
        return {
            "thread": thread,
            "paused_before": list(snapshot.next),
            "decisions": _decisions(snapshot.values),
        }


def _settle(thread: str, index: int, state: DecisionState,
            chosen_value: str | None, note: str | None,
            reviewer: str | None = None) -> dict:
    try:
        return _settle_locked(thread, index, state, chosen_value, note, reviewer)
    except PileBusy as busy:
        # Another reviewer holds this case. A machine caller gets the reason as
        # data and can retry; a traceback would just look like a broken tool.
        return {"error": str(busy), "retryable": True}


def _settle_locked(thread: str, index: int, state: DecisionState,
                   chosen_value: str | None, note: str | None,
                   reviewer: str | None = None) -> dict:
    with case_lock(thread), PostgresSaver.from_conn_string(_conn()) as cp:
        app = _pipeline().build(checkpointer=cp)
        cfg = _cfg(thread)
        snapshot = app.get_state(cfg)
        if not snapshot.values:
            return {"error": f"no run found for thread {thread!r}"}

        # Exactly one item changes. Every other decision keeps its own state.
        # Settling an update decision applies or discards its proposal.
        try:
            patch = gate_settle(snapshot.values, index, state, chosen_value, note,
                                by=reviewer)
        except (GateError, ValueError) as exc:
            # e.g. escalating a finding rather than an alert. The caller is a
            # machine, so it gets the reason as data, not a traceback.
            return {"error": str(exc), "index": index}
        app.update_state(cfg, patch)

        decisions = [Decision.model_validate(d) for d in patch["decisions"]]
        settled = {"index": index, "state": state.value,
                   "summary": decisions[index].summary}
        if decisions[index].kind is DecisionKind.UPDATE:
            settled["update"] = (
                "applied" if state is DecisionState.APPROVED
                else "discarded; the register is unchanged"
            )
        return {
            "thread": thread,
            "settled": settled,
            "still_pending": sum(1 for d in decisions if d.state is DecisionState.PENDING),
            "decisions": _decisions(patch),
        }


@server.tool(
    description=(
        "Approve one decision by index. For a conflict, pass chosen_value to "
        "record which value the reviewer accepted; without it the conflict "
        "stays unresolved. Affects only this item."
    )
)
def approve_decision(thread: str, index: int, chosen_value: str | None = None,
                     note: str | None = None, reviewer: str | None = None) -> dict:
    return _settle(thread, index, DecisionState.APPROVED, chosen_value, note, reviewer)


@server.tool(
    description=(
        "Reject one decision by index. The rest of the pending items are "
        "unaffected and remain pending."
    )
)
def reject_decision(thread: str, index: int, note: str | None = None,
                    reviewer: str | None = None) -> dict:
    return _settle(thread, index, DecisionState.REJECTED, None, note, reviewer)


@server.tool(
    description=(
        "Escalate one watchlist alert that could not be cleared on the evidence "
        "available. This is the third outcome, distinct from approve (confirmed "
        "true match) and reject (discounted false positive): the reviewer looked "
        "and refused to call it either way. It blocks onboarding for the pile. "
        "Only decisions of kind 'alert' can be escalated. Pass note to record "
        "which evidence was missing."
    )
)
def escalate_decision(thread: str, index: int, note: str | None = None,
                      reviewer: str | None = None) -> dict:
    return _settle(thread, index, DecisionState.ESCALATED, None, note, reviewer)


@server.tool(
    description=(
        "List the watchlist alerts raised for a run, with the full per-identifier "
        "comparison behind each one and the engine's non-binding recommendation. "
        "Call this before settling an alert: the score alone is not enough to "
        "adjudicate on, and the engine never clears an alert itself."
    )
)
def get_alerts(thread: str) -> dict:
    with PostgresSaver.from_conn_string(_conn()) as cp:
        app = _pipeline().build(checkpointer=cp)
        snapshot = app.get_state(_cfg(thread))
        if not snapshot.values:
            return {"error": f"no run found for thread {thread!r}"}

        values = snapshot.values
        if not values.get("watchlist_available", True):
            return {
                "thread": thread,
                "watchlist_available": False,
                "note": ("No watchlist was loaded, so nothing was screened. This "
                         "is not the same as a clean result."),
            }

        # Index by subject so each alert reports how its decision landed.
        state_by_subject = {
            str(d["subject_id"]): d for d in values.get("decisions", [])
            if d.get("kind") == "alert"
        }
        alerts = []
        for alert in values.get("alerts", []):
            decision = state_by_subject.get(alert["id"], {})
            alerts.append({**alert,
                           "decision_state": decision.get("state", "pending"),
                           "decision_note": decision.get("note")})

        return {
            "thread": thread,
            "watchlist_available": True,
            "watchlist_source": values.get("watchlist_source", ""),
            "alerts": alerts,
            "onboarding_blocked": values.get("onboarding_blocked", False),
            "blocking_reasons": values.get("blocking_reasons", []),
        }


@server.tool(
    description=(
        "Continue a run past the approval gate and commit. REFUSES while any "
        "item is still pending: every conflict, finding and alert must first be "
        "approved, rejected or escalated. Call list_decisions to see what is "
        "outstanding. Rejected items are discarded, and only approved work is "
        "applied."
    )
)
def resume_run(thread: str) -> dict:
    try:
        return _resume_locked(thread)
    except PileBusy as busy:
        return {"error": str(busy), "retryable": True}


def _resume_locked(thread: str) -> dict:
    with case_lock(thread), PostgresSaver.from_conn_string(_conn()) as cp:
        app = _pipeline().build(checkpointer=cp)
        cfg = _cfg(thread)
        values = app.get_state(cfg).values
        if not values:
            return {"error": f"no run found for thread {thread!r}"}
        # A machine driving this system meets the same gate a human does.
        if pending := unsettled(values):
            return {
                "error": refusal(pending),
                "pending": [
                    {"index": i, "kind": d.kind.value, "summary": d.summary}
                    for i, d in pending
                ],
            }
        state = app.invoke(None, cfg)  # None resumes rather than restarts
        paused_before = list(app.get_state(cfg).next)

    register = Register.model_validate(state["register"])
    return {
        "thread": thread,
        "paused_before": paused_before,
        "events": state.get("events", [])[-1:],
        "cost": _costs(state),
        "register": {
            "revision": register.revision,
            "sections": [
                {"id": s.id, "heading": s.heading, "body": s.body,
                 "content_hash": s.content_hash}
                for s in register.sections
            ],
        },
    }


@server.tool(
    description=(
        "Return the current register with a content hash per section. Diff two "
        "of these to prove exactly which sections an update touched."
    )
)
def get_register(thread: str) -> dict:
    with PostgresSaver.from_conn_string(_conn()) as cp:
        app = _pipeline().build(checkpointer=cp)
        snapshot = app.get_state(_cfg(thread))
        if not snapshot.values or not snapshot.values.get("register"):
            return {"error": f"no register for thread {thread!r}"}
        register = Register.model_validate(snapshot.values["register"])
        return {
            "thread": thread,
            "revision": register.revision,
            "hashes": register.hashes(),
            "sections": [
                {"id": s.id, "heading": s.heading, "body": s.body} for s in register.sections
            ],
        }


@server.tool(
    description=(
        "PROPOSE a focused update from one newly arrived document. This does "
        "NOT change the register: it works out which sections the arrival "
        "would rebuild and which would stay byte-identical, then raises an "
        "'update' decision for a human. Call approve_decision on that index to "
        "apply it, or reject_decision to discard it and leave the register "
        "exactly as it was. A contradiction raises a conflict rather than "
        "overwriting. Only one update is ever computed at a time — a second "
        "call while one is outstanding queues (response has queued: true) "
        "rather than computing early; call again with the same path once the "
        "outstanding one is settled."
    )
)
def apply_new_document(thread: str, path: str) -> dict:
    arrival = Path(path)
    if not arrival.is_file():
        return {"error": f"no such file: {arrival}"}

    with case_lock(thread), PostgresSaver.from_conn_string(_conn()) as cp:
        app = _pipeline().build(checkpointer=cp)
        cfg = _cfg(thread)
        snapshot = app.get_state(cfg)
        if not snapshot.values:
            return {"error": f"no run found for thread {thread!r}"}

        try:
            patch, proposal = propose_arrival(
                snapshot.values, arrival, build_adapter(get_settings())
            )
        except GateError as exc:
            return {"error": str(exc)}
        app.update_state(cfg, patch)

    if proposal is None:
        return {
            "thread": thread,
            "queued": True,
            "queue_position": len(patch["pending_arrivals"]),
            "note": "an update is already outstanding; call apply_new_document "
                    "again with this same path once it is settled",
        }

    return {
        "thread": thread,
        "arrival": proposal["arrival"],
        "document_kind": proposal["document"]["kind"],
        "quarantined": proposal["document"]["status"] == "quarantined",
        "applied": False,
        "awaiting_approval_at_index": len(patch["decisions"]) - 1,
        "claims_added": proposal["claims_added"],
        "conflicts_raised": proposal["conflicts_raised"],
        "sections_would_rebuild": proposal["sections_rebuilt"],
        "sections_would_not_touch": proposal["sections_untouched"],
        "cost": {
            "model_calls": (proposal["cost"] or {}).get("model_calls", 0),
            "wall_ms": (proposal["cost"] or {}).get("wall_ms", 0),
        },
        "hashes_before": proposal["hashes_before"],
        "hashes_if_approved": proposal["hashes_after"],
    }


@server.tool(
    description=(
        "Full report for a run: stages executed with the decision taken at "
        "each, per-stage cost and time, decision states, and the register."
    )
)
def get_run_report(thread: str) -> dict:
    with PostgresSaver.from_conn_string(_conn()) as cp:
        app = _pipeline().build(checkpointer=cp)
        snapshot = app.get_state(_cfg(thread))
        if not snapshot.values:
            return {"error": f"no run found for thread {thread!r}"}
        values = snapshot.values
        register = (
            Register.model_validate(values["register"]) if values.get("register") else None
        )
        return {
            "thread": thread,
            "pile_id": values.get("pile_id"),
            "paused_before": list(snapshot.next),
            "escalated": values.get("escalated", False),
            "escalation_reason": values.get("escalation_reason"),
            "events": values.get("events", []),
            "cost": _costs(values),
            "decisions": _decisions(values),
            "register_revision": register.revision if register else None,
            "register_hashes": register.hashes() if register else None,
        }


def main() -> None:
    server.run()


if __name__ == "__main__":
    main()
