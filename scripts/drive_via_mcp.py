#!/usr/bin/env python3
"""Drive the entire system over MCP, with no human and no interface.

This is behaviour four's proof. It launches the MCP server as a subprocess,
connects over stdio, and runs the whole flow — including the approval gate —
by calling tools. Nothing here touches the CLI, the graph, or the database
directly; every step goes through the machine interface.

    python scripts/drive_via_mcp.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

REPO = Path(__file__).resolve().parent.parent


def payload(result) -> dict:
    """Tool results arrive as content blocks; pull the JSON back out."""
    if getattr(result, "structuredContent", None):
        data = result.structuredContent
        return data.get("result", data) if isinstance(data, dict) else data
    for block in result.content:
        text = getattr(block, "text", None)
        if text:
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return {"text": text}
    return {}


def show(title: str, body: str = "") -> None:
    print(f"\n{'=' * 70}\n{title}\n{'=' * 70}")
    if body:
        print(body)


async def main() -> int:
    env = dict(os.environ)
    env.setdefault(
        "DATABASE_URL", "postgresql://doctask:doctask@localhost:5432/doctask"
    )

    params = StdioServerParameters(
        # The interpreter running this script, not a hardcoded venv path.
        # Inside the container there is no ./.venv — dependencies are installed
        # system-wide — and hardcoding it made behaviour 4 fail there while
        # passing on the host, which is the worst way for a check to be wrong.
        command=sys.executable,
        args=["-m", "app.mcp.server"],
        cwd=str(REPO),
        env=env,
    )

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            tools = await session.list_tools()
            show("TOOLS EXPOSED", "\n".join(
                f"  {t.name}" for t in tools.tools
            ))
            assert {"approve_decision", "reject_decision"} <= {t.name for t in tools.tools}, \
                "the gate must be an operation on the machine interface"

            # 1. run the pile ------------------------------------------------
            run = payload(await session.call_tool("run_pile", {"pile": "meridian"}))
            thread = run["thread"]
            show("1. run_pile", (
                f"  thread          : {thread}\n"
                f"  outcome         : {run['outcome']}\n"
                f"  paused before   : {run['paused_before']}\n"
                f"  model calls     : {run['cost']['total_model_calls']}\n"
                f"  decisions raised: {len(run['decisions'])}"
            ))
            for d in run["decisions"]:
                print(f"    [{d['state']}] {d['index']}. {d['summary'][:74]}")
            assert run["paused_before"] == ["commit"], "must stop before committing"

            # 2. settle items individually -----------------------------------
            approved = payload(await session.call_tool("approve_decision", {
                "thread": thread, "index": 0, "chosen_value": "41%",
                "note": "amended disclosure supersedes the 2024 filing",
            }))
            rejected = payload(await session.call_tool("reject_decision", {
                "thread": thread, "index": 3,
                "note": "amendment no.1 raises the contract value; invoice is correct",
            }))
            show("2. approve one, reject one — every other item must be untouched", (
                f"  approved index 0 -> {approved['settled']['state']}\n"
                f"  rejected index 3 -> {rejected['settled']['state']}\n"
                f"  still pending    : {rejected['still_pending']}"
            ))
            for d in rejected["decisions"]:
                print(f"    [{d['state']:8}] {d['index']}. {d['summary'][:66]}")
            assert rejected["still_pending"] == 4, "rejecting one must not disturb the rest"

            # 2b. the third outcome ------------------------------------------
            # Sanctions adjudication has three answers, not two, and a machine
            # driving this system must be able to reach all of them. Escalation
            # is refused on anything that is not a watchlist alert.
            alerts = payload(await session.call_tool("get_alerts", {"thread": thread}))
            alert = alerts["alerts"][0]
            refused = payload(await session.call_tool("escalate_decision", {
                "thread": thread, "index": 1, "note": "not an alert",
            }))
            escalated = payload(await session.call_tool("escalate_decision", {
                "thread": thread, "index": 5,
                "note": "listing carries no date of incorporation; "
                        "cannot discount on jurisdiction alone",
            }))
            show("2b. escalate_decision — the answer that is neither yes nor no", (
                f"  watchlist       : {alerts['watchlist_source']}\n"
                f"  alert           : {alert['subject_name']!r} vs OFAC SDN "
                f"{alert['listed_uid']} {alert['listed_name']!r}\n"
                f"  name score      : {alert['score']:.0%} "
                f"(matched on {alert['matched_on']!r})\n"
                f"  engine suggests : {alert['recommendation']} — "
                f"{alert['recommendation_reason']}"
            ))
            for i in alert["identifiers"]:
                print(f"    {i['name']:22} {i['comparison']:12} "
                      f"{i['subject_value'] or '(absent)'} / "
                      f"{i['listed_value'] or '(absent)'}  [{i['strength']}]")
            print(f"\n    escalating a finding -> refused: {refused['error']}")
            print(f"    escalating the alert -> {escalated['settled']['state']}")
            assert "error" in refused, "a finding must not be escalable"
            assert escalated["settled"]["state"] == "escalated"

            # 2c. the gate is a barrier ---------------------------------------
            # Three items are still unreviewed at this point. Committing now
            # would produce a register nobody had finished checking, and a
            # "committed" message that meant nothing.
            premature = payload(await session.call_tool("resume_run", {"thread": thread}))
            show("2c. resume_run refuses while anything is still pending", (
                f"  {premature['error'][:150]}…"
            ))
            for item in premature["pending"]:
                print(f"    [{item['index']}] {item['kind']:8} {item['summary'][:62]}")
            assert "error" in premature, "the gate let a run commit unreviewed"

            for index in (item["index"] for item in premature["pending"]):
                payload(await session.call_tool("reject_decision", {
                    "thread": thread, "index": index,
                    "note": "reviewed and not accepted",
                }))
            print("\n    every item now settled")

            # 3. resume ------------------------------------------------------
            resumed = payload(await session.call_tool("resume_run", {"thread": thread}))
            commit = resumed["events"][-1]
            show("3. resume_run — only approved work is applied", (
                f"  approved={commit['approved']}  rejected={commit['rejected']}  "
                f"still_pending={commit['still_pending']}  "
                f"conflicts_resolved={commit['conflicts_resolved']}\n"
                f"  register revision: {resumed['register']['revision']}"
            ))
            ownership = next(s for s in resumed["register"]["sections"] if s["id"] == "ownership")
            print(f"    ownership -> {ownership['body'].splitlines()[-1]}")
            assert "resolved by reviewer" in ownership["body"]

            screening = next(s for s in resumed["register"]["sections"]
                             if s["id"] == "screening")
            print(f"    screening -> {screening['body'].splitlines()[-2]}")
            assert "ONBOARDING BLOCKED" in screening["body"], (
                "an escalated sanctions alert must hold the vendor in the register"
            )

            # 4. a new document arrives -------------------------------------
            before = payload(await session.call_tool("get_register", {"thread": thread}))
            arrival = payload(await session.call_tool("apply_new_document", {
                "thread": thread,
                "path": "corpora/_arrivals/meridian_09_rescreen.txt",
            }))
            show("4. apply_new_document — PROPOSES a focused update, applies nothing", (
                f"  arrival         : {arrival['arrival']} ({arrival['document_kind']})\n"
                f"  cost            : {arrival['cost']['model_calls']} model calls "
                f"(the full run was {run['cost']['total_model_calls']})\n"
                f"  claims added    : {arrival['claims_added']}\n"
                f"  conflicts raised: {arrival['conflicts_raised']}\n"
                f"  would rebuild   : {arrival['sections_would_rebuild']}\n"
                f"  would not touch : {arrival['sections_would_not_touch']}\n"
                f"  applied         : {arrival['applied']}  "
                f"(awaiting approval at index {arrival['awaiting_approval_at_index']})"
            ))
            assert arrival["sections_would_rebuild"] == ["screening"]
            assert arrival["cost"]["model_calls"] < run["cost"]["total_model_calls"]
            assert arrival["applied"] is False

            # The register must not have moved yet. An update is the one
            # operation that rewrites a register a reviewer already signed off,
            # so it is the last one that should happen unattended.
            mid = payload(await session.call_tool("get_register", {"thread": thread}))
            assert mid["hashes"] == before["hashes"], (
                "the register changed before anyone approved the update"
            )
            print("\n    register unchanged while the update awaits review ✓")

            approved_update = payload(await session.call_tool("approve_decision", {
                "thread": thread,
                "index": arrival["awaiting_approval_at_index"],
                "note": "re-screen corroborated by the provider reference",
            }))
            print(f"    approved -> {approved_update['settled']['update']}")

            after = payload(await session.call_tool("get_register", {"thread": thread}))
            unchanged = [
                s for s in before["hashes"]
                if before["hashes"][s] == after["hashes"].get(s)
            ]
            print(f"\n    hash comparison across the arrival:")
            for section, digest in before["hashes"].items():
                same = digest == after["hashes"].get(section)
                print(f"      {section:12} {'identical' if same else 'CHANGED  '} {digest[:12]}")
            assert sorted(unchanged) == ["banking", "commercial", "identity", "ownership"]

            # The arrival rebuilds the screening section. The escalated alert
            # must survive that rebuild — otherwise an unrelated document
            # landing in a watched folder silently clears a sanctions hold.
            rebuilt = next(s for s in after["sections"] if s["id"] == "screening")
            assert "ONBOARDING BLOCKED" in rebuilt["body"], (
                "the arrival erased a live watchlist alert from the register"
            )
            print("      screening rebuilt, and the sanctions hold survived it")

            # 5. the report --------------------------------------------------
            report = payload(await session.call_tool("get_run_report", {"thread": thread}))
            show("5. get_run_report — what it cost, stage by stage")
            for c in report["cost"]["by_stage"]:
                print(f"    {c['stage']:22} {c['model_calls']:3} calls  {c['wall_ms']:6} ms  ${c['usd']:.4f}")
            print(f"    {'TOTAL':22} {report['cost']['total_model_calls']:3} calls  "
                  f"{report['cost']['total_wall_ms']:6} ms  ${report['cost']['total_usd']:.4f}")

    show("RESULT", (
        "  Whole flow driven by a program over MCP: ran the pile, read the gate,\n"
        "  approved one item, rejected another, escalated a sanctions alert it\n"
        "  could not clear, was REFUSED a commit while items were still\n"
        "  unreviewed, settled them, resumed, proposed a focused update from a\n"
        "  new document, confirmed the register had not moved, approved it, and\n"
        "  proved which sections changed.\n\n"
        "  Every gate a human meets, a machine meets identically. No interface\n"
        "  was involved at any point."
    ))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
