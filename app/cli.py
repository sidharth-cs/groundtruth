"""Operator surface.

One command runs a pile to the approval gate; another lists what is waiting;
another settles items individually; another resumes. Every command takes a
thread id, and the thread id is the resume handle — kill the process at any
point and the next command picks up from the last completed stage.

    python -m app.cli run meridian
    python -m app.cli pending  <thread>
    python -m app.cli approve  <thread> <n> [--value "62%"]
    python -m app.cli reject   <thread> <n> [--note "..."]
    python -m app.cli resume   <thread>
    python -m app.cli report   <thread>
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from langgraph.checkpoint.postgres import PostgresSaver

from app.core.config import get_settings
from app.core.llm import build_adapter
from app.core.locks import PileBusy, case_lock, pile_lock
from app.domain.models import Decision, DecisionKind, DecisionState, Register
from app.pipeline.gate import GateError, propose_arrival, refusal, unsettled
from app.pipeline.gate import settle as gate_settle
from app.pipeline.graph import Pipeline, new_run_id

DEFAULT_CHECKLIST = "checklists/vendor_onboarding_v1.yaml"


def _conn_string() -> str:
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


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------


def _print_events(state: dict) -> None:
    print("\nSTAGES")
    for event in state.get("events", []):
        detail = {k: v for k, v in event.items() if k not in ("stage", "decision", "at")}
        rendered = ", ".join(f"{k}={v}" for k, v in detail.items())
        print(f"  {event['stage']:10} {event['decision']:16} {rendered}")


def _print_costs(state: dict) -> None:
    costs = state.get("stage_costs", [])
    if not costs:
        return
    print("\nCOST AND TIME")
    calls = ms = 0.0
    usd = 0.0
    for c in costs:
        calls += c["model_calls"]; ms += c["wall_ms"]; usd += c["usd"]
        print(f"  {c['stage']:10} {c['model_calls']:3} calls  {c['wall_ms']:6} ms  ${c['usd']:.4f}")
    print(f"  {'total':10} {int(calls):3} calls  {int(ms):6} ms  ${usd:.4f}")


def _print_pending(state: dict) -> None:
    decisions = [Decision.model_validate(d) for d in state.get("decisions", [])]
    if not decisions:
        print("\nNo decisions were raised.")
        return
    print(f"\nDECISIONS ({sum(1 for d in decisions if d.state is DecisionState.PENDING)} pending)")
    alerts = {a["id"]: a for a in state.get("alerts", [])}
    for i, d in enumerate(decisions):
        mark = {"pending": " ", "approved": "+", "rejected": "-", "escalated": "!"}[
            d.state.value
        ]
        print(f"  [{mark}] {i:>2}. {d.kind.value:8} {d.summary}")
        # An alert is unadjudicable without its evidence, so the reviewer gets
        # the full identifier comparison inline rather than a bare score.
        if alert := alerts.get(str(d.subject_id)):
            print(f"          engine suggests: {alert['recommendation']} "
                  f"— {alert['recommendation_reason']}")
            for ident in alert["identifiers"]:
                subject = ident["subject_value"] or "(absent)"
                listed = ident["listed_value"] or "(absent)"
                print(f"            {ident['name']:14} {ident['comparison']:12} "
                      f"{subject} / {listed}  [{ident['strength']}]")
        if d.note:
            print(f"          note: {d.note}")


def _print_register(state: dict) -> None:
    raw = state.get("register")
    if not raw:
        return
    register = Register.model_validate(raw)
    print(f"\nREGISTER (revision {register.revision})")
    for section in register.sections:
        print(f"\n  ## {section.heading}   [{section.content_hash[:12]}]")
        for line in section.body.splitlines():
            print(f"     {line}")

    if state.get("onboarding_blocked"):
        print("\n  ONBOARDING BLOCKED — unresolved watchlist alert")
        for reason in state.get("blocking_reasons", []):
            print(f"     {reason}")


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------


def cmd_run(args: argparse.Namespace) -> int:
    pile_path = Path(args.pile) if Path(args.pile).exists() else Path("corpora") / args.pile
    if not pile_path.is_dir():
        print(f"no such pile: {pile_path}", file=sys.stderr)
        return 2

    run_id = new_run_id()
    thread = args.thread or f"{pile_path.name}-{run_id[:8]}"

    try:
        # Runs against the same pile are serialised; different piles do not
        # contend and proceed in parallel.
        with pile_lock(pile_path.name, wait_seconds=args.lock_wait):
            with PostgresSaver.from_conn_string(_conn_string()) as cp:
                cp.setup()
                app = _pipeline().build(checkpointer=cp)
                state = app.invoke(
                    {
                        "run_id": run_id,
                        "pile_id": pile_path.name,
                        "pile_path": str(pile_path),
                        "checklist_path": args.checklist,
                    },
                    _cfg(thread),
                )
                snapshot = app.get_state(_cfg(thread))
    except PileBusy as busy:
        print(f"pile busy: {busy}", file=sys.stderr)
        return 3

    _print_events(state)
    _print_costs(state)

    if state.get("escalated"):
        print(f"\nESCALATED — {state['escalation_reason']}")
        print(f"\nthread: {thread}")
        return 0

    _print_pending(state)
    print(f"\nPaused before: {', '.join(snapshot.next) or '(complete)'}")
    print(f"thread: {thread}")
    print(f"\nnext:  python -m app.cli approve {thread} <n>   /   reject {thread} <n>")
    print(f"then:  python -m app.cli resume {thread}")
    return 0


def cmd_pending(args: argparse.Namespace) -> int:
    with PostgresSaver.from_conn_string(_conn_string()) as cp:
        app = _pipeline().build(checkpointer=cp)
        snapshot = app.get_state(_cfg(args.thread))
        if not snapshot.values:
            print(f"no run found for thread {args.thread!r}", file=sys.stderr)
            return 2
        _print_pending(snapshot.values)
        print(f"\nPaused before: {', '.join(snapshot.next) or '(complete)'}")
    return 0


def _settle(thread: str, index: int, state: DecisionState, value: str | None,
            note: str | None, by: str | None = None) -> int:
    # Same lock the browser and the MCP server take. A gate that behaves
    # differently depending on which door you came through is not a gate.
    with case_lock(thread), PostgresSaver.from_conn_string(_conn_string()) as cp:
        app = _pipeline().build(checkpointer=cp)
        cfg = _cfg(thread)
        snapshot = app.get_state(cfg)
        if not snapshot.values:
            print(f"no run found for thread {thread!r}", file=sys.stderr)
            return 2

        # Settle exactly one. Every other item keeps its own state — rejecting
        # one finding must not disturb the rest. Settling an update is also
        # what applies or discards the change it proposed.
        try:
            patch = gate_settle(snapshot.values, index, state, value, note, by=by)
        except (GateError, ValueError) as exc:
            # e.g. escalating a finding, or an index that does not exist.
            # Report the reason rather than letting a traceback stand in for it.
            print(str(exc), file=sys.stderr)
            return 2
        app.update_state(cfg, patch)

        decisions = [Decision.model_validate(d) for d in patch["decisions"]]
        who = decisions[index].decided_by
        print(f"{state.value} by {who}: {decisions[index].summary}")
        if decisions[index].kind is DecisionKind.UPDATE:
            print("register updated" if state is DecisionState.APPROVED
                  else "update discarded; the register is unchanged")
        remaining = sum(1 for d in decisions if d.state is DecisionState.PENDING)
        print(f"{remaining} still pending")
    return 0


def cmd_arrival(args: argparse.Namespace) -> int:
    """Propose a focused update from a newly arrived document.

    It proposes. Nothing in the register moves until the update is approved.
    """
    path = Path(args.path)
    if not path.is_file():
        path = Path("corpora/_arrivals") / Path(args.path).name
    if not path.is_file():
        print(f"no such arrival: {args.path}", file=sys.stderr)
        return 2

    with case_lock(args.thread), PostgresSaver.from_conn_string(_conn_string()) as cp:
        app = _pipeline().build(checkpointer=cp)
        cfg = _cfg(args.thread)
        snapshot = app.get_state(cfg)
        if not snapshot.values:
            print(f"no run found for thread {args.thread!r}", file=sys.stderr)
            return 2
        try:
            patch, proposal = propose_arrival(
                snapshot.values, path, build_adapter(get_settings())
            )
        except GateError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        app.update_state(cfg, patch)

    if proposal is None:
        position = len(patch["pending_arrivals"])
        print(f"\nqueued — an update is already outstanding for {args.thread}.")
        print(f"  position        : {position}")
        print(f"  becomes available once the outstanding update is settled; "
              f"re-run this same command then.")
        return 0

    print(f"\nPROPOSED UPDATE from {proposal['arrival']}")
    print(f"  would rebuild   : {', '.join(proposal['sections_rebuilt']) or 'nothing'}")
    print(f"  would not touch : {', '.join(proposal['sections_untouched'])}")
    for claim in proposal["claims_added"]:
        print(f"  claim           : {claim['attribute']} = {claim['value']}")
    for attribute in proposal["conflicts_raised"]:
        print(f"  contradicts     : {attribute}")
    print(f"  cost            : {proposal['cost']['model_calls']} model calls")
    index = len(patch["decisions"]) - 1
    print(f"\nNothing has changed yet. Approve or reject item {index}:")
    print(f"  python -m app.cli approve {args.thread} {index}")
    print(f"  python -m app.cli reject  {args.thread} {index} --note '...'")
    return 0


def cmd_approve(args: argparse.Namespace) -> int:
    return _settle(args.thread, args.index, DecisionState.APPROVED, args.value, args.note,
                   by=args.by)


def cmd_reject(args: argparse.Namespace) -> int:
    return _settle(args.thread, args.index, DecisionState.REJECTED, None, args.note,
                   by=args.by)


def cmd_escalate(args: argparse.Namespace) -> int:
    """The third answer: looked at it, could not clear it, holding the vendor."""
    return _settle(args.thread, args.index, DecisionState.ESCALATED, None, args.note,
                   by=args.by)


def cmd_resume(args: argparse.Namespace) -> int:
    with case_lock(args.thread), PostgresSaver.from_conn_string(_conn_string()) as cp:
        app = _pipeline().build(checkpointer=cp)
        cfg = _cfg(args.thread)
        snapshot = app.get_state(cfg)
        if not snapshot.values:
            print(f"no run found for thread {args.thread!r}", file=sys.stderr)
            return 2
        # The gate is a barrier, not a formality.
        if pending := unsettled(snapshot.values):
            print(refusal(pending), file=sys.stderr)
            return 2
        # Passing None resumes from the checkpoint rather than starting over.
        state = app.invoke(None, cfg)
        after = app.get_state(cfg)

    _print_events(state)
    _print_costs(state)
    _print_register(state)
    print(f"\nPaused before: {', '.join(after.next) or '(complete)'}")
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    with PostgresSaver.from_conn_string(_conn_string()) as cp:
        app = _pipeline().build(checkpointer=cp)
        snapshot = app.get_state(_cfg(args.thread))
        if not snapshot.values:
            print(f"no run found for thread {args.thread!r}", file=sys.stderr)
            return 2
        _print_events(snapshot.values)
        _print_costs(snapshot.values)
        _print_pending(snapshot.values)
        _print_register(snapshot.values)
        print(f"\nPaused before: {', '.join(snapshot.next) or '(complete)'}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="app.cli", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="run a pile to the approval gate")
    p_run.add_argument("pile", help="pile name under corpora/, or a path")
    p_run.add_argument("--checklist", default=DEFAULT_CHECKLIST)
    p_run.add_argument("--thread", default=None, help="reuse a thread id")
    p_run.add_argument(
        "--lock-wait", type=float, default=10.0,
        help="seconds to wait for a busy pile before giving up (default 10)",
    )
    p_run.set_defaults(func=cmd_run)

    p_pending = sub.add_parser("pending", help="list decisions awaiting a human")
    p_pending.add_argument("thread")
    p_pending.set_defaults(func=cmd_pending)

    p_approve = sub.add_parser("approve", help="approve one decision by index")
    p_approve.add_argument("thread")
    p_approve.add_argument("index", type=int)
    p_approve.add_argument("--value", default=None, help="chosen value, for conflicts")
    p_approve.add_argument("--by", default=None,
                          help="who is deciding (recorded on the decision)")
    p_approve.add_argument("--note", default=None)
    p_approve.set_defaults(func=cmd_approve)

    p_reject = sub.add_parser("reject", help="reject one decision by index")
    p_reject.add_argument("thread")
    p_reject.add_argument("index", type=int)
    p_reject.add_argument("--by", default=None,
                          help="who is deciding (recorded on the decision)")
    p_reject.add_argument("--note", default=None)
    p_reject.set_defaults(func=cmd_reject)

    p_escalate = sub.add_parser(
        "escalate",
        help="escalate a watchlist alert that cannot be cleared (blocks onboarding)",
    )
    p_escalate.add_argument("thread")
    p_escalate.add_argument("index", type=int)
    p_escalate.add_argument("--by", default=None,
                          help="who is deciding (recorded on the decision)")
    p_escalate.add_argument("--note", default=None, help="why it could not be cleared")
    p_escalate.set_defaults(func=cmd_escalate)

    p_arrival = sub.add_parser(
        "arrival", help="propose a focused update from a newly arrived document")
    p_arrival.add_argument("thread")
    p_arrival.add_argument("path", help="file path, or a name in corpora/_arrivals")
    p_arrival.set_defaults(func=cmd_arrival)

    p_resume = sub.add_parser("resume", help="continue after decisions are settled")
    p_resume.add_argument("thread")
    p_resume.set_defaults(func=cmd_resume)

    p_report = sub.add_parser("report", help="show everything known about a run")
    p_report.add_argument("thread")
    p_report.set_defaults(func=cmd_report)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
