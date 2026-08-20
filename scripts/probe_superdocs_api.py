#!/usr/bin/env python3
"""Probe the SuperDocs REST surface the Task 2 builds depend on.

Purpose: the web editor showing a bug does not tell us whether the API is
usable. The Zoom panel and Classroom add-on only ever touch four calls —
upload, chat, approve, export. This exercises exactly those, end to end, on a
small synthetic document, and reports which of them actually work.

The key is read from .env (SUPERDOCS_API_KEY) and never printed, logged, or
sent anywhere except api.superdocs.app.

Usage:
    echo 'SUPERDOCS_API_KEY=sk_your_key_here' >> .env
    ./.venv/bin/python scripts/probe_superdocs_api.py
"""

from __future__ import annotations

import base64
import json
import os
import sys
import time
from pathlib import Path

import httpx

BASE = "https://api.superdocs.app"
TIMEOUT = httpx.Timeout(180.0, connect=15.0)  # ops can take minutes; not a crash

# A deliberately structured document: several distinct sections, one link, and
# values we can check survived. Small enough to be cheap, structured enough
# that a reassembly bug shows up as a missing section.
SAMPLE_HTML = """<h1>Statement of Work</h1>
<h2>Parties</h2>
<p>Client: Northwind Logistics Ltd. Supplier: Meridian Componentes Holdings Ltd.</p>
<h2>Scope</h2>
<p>Supplier will deliver warehouse automation consulting.</p>
<h2>Commercials</h2>
<p>Fees: USD 48,000. Start date: 3 September 2026.</p>
<h2>Reference</h2>
<p>See <a href="https://example.com/terms">standard terms</a> for details.</p>
<h2>Signatures</h2>
<p>Signed for and on behalf of both parties.</p>
"""

EXPECTED_SECTIONS = ["Parties", "Scope", "Commercials", "Reference", "Signatures"]


def load_key() -> str:
    for line in (Path(".env").read_text().splitlines() if Path(".env").exists() else []):
        if line.strip().startswith("SUPERDOCS_API_KEY="):
            return line.split("=", 1)[1].strip()
    key = os.environ.get("SUPERDOCS_API_KEY", "")
    if not key:
        sys.exit(
            "SUPERDOCS_API_KEY not found.\n"
            "Add it to .env:  echo 'SUPERDOCS_API_KEY=sk_...' >> .env\n"
            "(.env is gitignored; the key is never printed by this script.)"
        )
    return key


def step(n: int, label: str) -> None:
    print(f"\n[{n}] {label}")
    print("-" * 60)


def main() -> int:
    key = load_key()
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    session_id = f"probe-{int(time.time())}"
    results: dict[str, str] = {}

    with httpx.Client(base_url=BASE, headers=headers, timeout=TIMEOUT) as client:
        # -- 1. upload -----------------------------------------------------
        step(1, "UPLOAD  POST /v1/documents/upload-base64")
        try:
            r = client.post(
                "/v1/documents/upload-base64",
                json={
                    "document_html": base64.b64encode(SAMPLE_HTML.encode()).decode(),
                    "session_id": session_id,
                    "return_html": True,
                },
            )
            print(f"    status {r.status_code}")
            if r.status_code >= 400:
                print(f"    body: {r.text[:400]}")
                results["upload"] = f"FAIL {r.status_code}"
                return report(results)
            body = r.json()
            html = body.get("document_html") or body.get("html") or ""
            present = [s for s in EXPECTED_SECTIONS if s in html]
            print(f"    sections returned: {len(present)}/{len(EXPECTED_SECTIONS)} {present}")
            results["upload"] = (
                "OK" if len(present) == len(EXPECTED_SECTIONS)
                else f"PARTIAL ({len(present)}/{len(EXPECTED_SECTIONS)} sections)"
            )
        except Exception as e:
            results["upload"] = f"ERROR {type(e).__name__}: {e}"
            return report(results)

        # -- 2. chat (the edit) -------------------------------------------
        step(2, "EDIT    POST /v1/chat/async  (approval_mode=ask_every_time)")
        job_id = None
        try:
            r = client.post(
                "/v1/chat/async",
                json={
                    "message": "Remove the hyperlink in the Reference section, "
                               "keeping the visible text. Change nothing else.",
                    "session_id": session_id,
                    "approval_mode": "ask_every_time",
                },
            )
            print(f"    status {r.status_code}")
            if r.status_code >= 400:
                print(f"    body: {r.text[:400]}")
                results["chat"] = f"FAIL {r.status_code}"
                return report(results)
            job_id = r.json().get("job_id")
            print(f"    job_id: {job_id}")
            results["chat"] = "OK" if job_id else "FAIL no job_id"
        except Exception as e:
            results["chat"] = f"ERROR {type(e).__name__}: {e}"
            return report(results)

        # -- 3. poll to awaiting_approval ----------------------------------
        step(3, "POLL    GET /v1/jobs/{job_id}  (slow is normal, not a crash)")
        pending, status = [], "unknown"
        deadline = time.time() + 300
        try:
            while time.time() < deadline:
                r = client.get(f"/v1/jobs/{job_id}")
                if r.status_code >= 400:
                    print(f"    status {r.status_code}: {r.text[:300]}")
                    break
                data = r.json()
                status = data.get("status", "unknown")
                print(f"    {int(time.time() % 1000):>4}s status={status}")
                if status in ("awaiting_approval", "completed", "failed", "error"):
                    meta = data.get("metadata") or {}
                    pending = meta.get("pending_changes") or []
                    # The documented gotcha: proposed-change content arrives as
                    # a JSON-encoded STRING and needs a second parse.
                    for i, ch in enumerate(pending):
                        for field in ("content", "proposed", "change"):
                            v = ch.get(field)
                            if isinstance(v, str):
                                try:
                                    ch[field] = json.loads(v)
                                    print(f"    change[{i}].{field}: needed second parse (documented)")
                                except json.JSONDecodeError:
                                    pass
                    break
                time.sleep(4)
            print(f"    final status: {status}, pending_changes: {len(pending)}")
            for ch in pending[:5]:
                print(f"      chunk_id={ch.get('chunk_id')} keys={sorted(ch.keys())}")
            results["poll"] = f"{status}, {len(pending)} pending"
        except Exception as e:
            results["poll"] = f"ERROR {type(e).__name__}: {e}"

        # -- 4. approve ----------------------------------------------------
        step(4, "APPROVE POST /v1/chat/{session_id}/approve")
        if pending:
            try:
                r = client.post(
                    f"/v1/chat/{session_id}/approve",
                    json={"changes": [{"chunk_id": c.get("chunk_id"), "approved": True}
                                      for c in pending if c.get("chunk_id")]},
                )
                print(f"    status {r.status_code} {r.text[:200]}")
                results["approve"] = "OK" if r.status_code < 400 else f"FAIL {r.status_code}"
            except Exception as e:
                results["approve"] = f"ERROR {type(e).__name__}: {e}"
        else:
            results["approve"] = "SKIPPED (nothing pending to approve)"
            print("    skipped — no pending changes returned")

        # -- 5. export -----------------------------------------------------
        step(5, "EXPORT  POST /v1/documents/export  (exports are free)")
        try:
            r = client.post(
                "/v1/documents/export",
                json={"session_id": session_id, "format": "html"},
            )
            print(f"    status {r.status_code}, {len(r.content)} bytes")
            if r.status_code < 400:
                out = r.text if r.headers.get("content-type","").startswith("text") else ""
                present = [s for s in EXPECTED_SECTIONS if s in out]
                kept = len(present)
                print(f"    sections in export: {kept}/{len(EXPECTED_SECTIONS)} {present}")
                missing = set(EXPECTED_SECTIONS) - set(present)
                if missing:
                    print(f"    *** MISSING AFTER EDIT: {sorted(missing)}")
                    print("    *** This reproduces the truncation/retrieval bug on the API path.")
                link_gone = 'href="https://example.com/terms"' not in out
                print(f"    hyperlink removed: {link_gone}")
                results["export"] = (
                    f"OK ({kept}/{len(EXPECTED_SECTIONS)} sections, link_removed={link_gone})"
                )
                Path("probe_export.html").write_text(out)
                print("    wrote probe_export.html for inspection")
            else:
                print(f"    body: {r.text[:300]}")
                results["export"] = f"FAIL {r.status_code}"
        except Exception as e:
            results["export"] = f"ERROR {type(e).__name__}: {e}"

    return report(results)


def report(results: dict[str, str]) -> int:
    print("\n" + "=" * 60)
    print("VERDICT — can the Task 2 builds stand on this API?")
    print("=" * 60)
    for k in ("upload", "chat", "poll", "approve", "export"):
        print(f"  {k:9} {results.get(k, 'not reached')}")
    broken = [k for k, v in results.items() if v.startswith(("FAIL", "ERROR"))]
    partial = [k for k, v in results.items() if v.startswith("PARTIAL") or "MISSING" in v]
    print()
    if broken:
        print(f"  API path is BLOCKED at: {', '.join(broken)}")
        print("  -> Send this output to hello@superdocs.app with 'urgent' in the subject.")
    elif partial:
        print(f"  API path DEGRADED at: {', '.join(partial)} — content loss reproduced.")
        print("  -> Send this output to hello@superdocs.app with 'urgent' in the subject.")
    else:
        print("  API path is HEALTHY. The editor bug does not block the builds.")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
