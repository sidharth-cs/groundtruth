"""HTTP layer for the review interface.

A third surface over the same operations the CLI and MCP server already expose.
That is deliberate: approval is one behaviour with three front doors, not three
implementations that can drift apart. Every endpoint here delegates to the same
graph and the same decision objects, so a decision settled in the browser is
indistinguishable from one settled by an agent over MCP.

The React app is served from here too, so the whole thing is one origin and one
process — no CORS, no second server for a reviewer to start.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from langgraph.checkpoint.postgres import PostgresSaver
from pydantic import BaseModel

from app.core.config import get_settings
from app.core.llm import build_adapter
from app.core.locks import PileBusy, case_lock, pile_lock
from app.domain.models import Citation, Claim, Conflict, Decision, DecisionState, Finding, Register
from app.domain.screening import PartyType, load_watchlist, screen
from app.pipeline.gate import GateError, propose_arrival, refusal, unsettled
from app.pipeline.gate import settle as gate_settle
from app.pipeline.graph import Pipeline, _alert_to_dict, new_run_id
from app.pipeline.ingest import SUPPORTED_SUFFIXES

REPO = Path(__file__).resolve().parent.parent.parent
# In the container the built interface lives outside /app, because compose
# bind-mounts the repo over /app and would otherwise hide it.
UI_DIST = Path(os.environ.get("DOCTASK_UI_DIR") or REPO / "ui" / "dist")
DEFAULT_CHECKLIST = "checklists/vendor_onboarding_v1.yaml"

app = FastAPI(
    title="doctask",
    description="Review interface for a pile of documents that never quite agree.",
    version="0.1.0",
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


def _snapshot(thread: str):
    with PostgresSaver.from_conn_string(_conn()) as cp:
        graph = _pipeline().build(checkpointer=cp)
        snap = graph.get_state(_cfg(thread))
        if not snap.values:
            raise HTTPException(404, f"no run found for thread {thread!r}")
        return snap.values, list(snap.next)


def _view(values: dict, paused_before: list[str]) -> dict:
    """One shape for the whole UI, so the client never has to stitch calls."""
    register = (
        Register.model_validate(values["register"]) if values.get("register") else None
    )
    costs = values.get("stage_costs", [])
    decisions = [Decision.model_validate(d) for d in values.get("decisions", [])]
    alerts_by_id = {a["id"]: a for a in values.get("alerts", [])}
    findings_by_id = _findings_by_id(values)

    # Deliberately no "thread" key here. The thread id is the caller's handle,
    # not something derivable from state, and every endpoint already supplies
    # it. An earlier version returned run_id under this name and, because the
    # view is spread after it, silently overwrote the real thread — the client
    # then asked for a run that had never existed under that id.
    return {
        "pile_id": values.get("pile_id"),
        "paused_before": paused_before,
        "escalated": values.get("escalated", False),
        "escalation_reason": values.get("escalation_reason"),
        "archived": values.get("archived", False),
        "stages": [
            {
                "stage": e["stage"],
                "decision": e["decision"],
                "detail": {k: v for k, v in e.items()
                           if k not in ("stage", "decision", "at")},
            }
            for e in values.get("events", [])
        ],
        "cost": {
            "by_stage": [
                {"stage": c["stage"], "model_calls": c["model_calls"],
                 "wall_ms": c["wall_ms"], "usd": c["usd"]}
                for c in costs
            ],
            "total_model_calls": sum(c["model_calls"] for c in costs),
            "total_wall_ms": sum(c["wall_ms"] for c in costs),
            "total_usd": sum(c["usd"] for c in costs),
        },
        "decisions": [
            {
                "index": i,
                "kind": d.kind.value,
                "state": d.state.value,
                "summary": d.summary,
                "chosen_value": d.chosen_value,
                "note": d.note,
                "decided_by": d.decided_by,
                "decided_at": d.decided_at.isoformat() if d.decided_at else None,
                # For an alert, the full evidence. A reviewer cannot adjudicate
                # a sanctions hit from a score, and a UI that shows only the
                # score invites a rubber-stamp.
                "alert": alerts_by_id.get(str(d.subject_id)),
                # For a finding, the values the rule actually compared and a
                # citation for each — the same reasoning: a severity and a
                # rule id is not evidence a reviewer can check.
                "finding": findings_by_id.get(str(d.subject_id)),
                # For a conflict, each competing value with its own citation —
                # this is also what the UI builds its per-option accept
                # buttons from directly, so there is only one source of truth
                # for "what are the competing values here."
                "conflict": _conflict_evidence(values, d),
                # For an update, the same evidence a reviewer got the moment
                # they submitted the arrival — persisted here so it survives
                # a refresh instead of existing only in that one response.
                "update": _update_evidence(values, d),
            }
            for i, d in enumerate(decisions)
        ],
        "onboarding_blocked": values.get("onboarding_blocked", False),
        "blocking_reasons": values.get("blocking_reasons", []),
        # The full checklist tally, not just the failures below in `decisions`.
        # Without this a reviewer sees findings but has no way to tell "every
        # other rule passed" from "some rules never ran" — the two read
        # identically as "no finding for this rule" otherwise.
        #
        # `or None` matters: LangGraph defaults an untouched `Annotated[dict,
        # ...]` channel to `{}`, not to an absent key. When a run escalates
        # during classify, `check()` never executes and this would otherwise
        # ship `{}` — falsy in Python (so nothing here noticed), truthy in
        # JS (so the frontend renders a checklist card for a check that never
        # ran). `{}` only ever means "channel untouched"; a real result
        # always carries at least `checklist_name` and `total_rules`.
        "checklist_summary": values.get("checklist_summary") or None,
        # Distinguishes "screened, nothing hit" from "never screened". A UI that
        # shows an empty alert list for both would be reporting a clean result
        # it does not have.
        "watchlist_available": values.get("watchlist_available"),
        "watchlist_source": values.get("watchlist_source", ""),
        "watchlist_name": values.get("watchlist_name", ""),
        "register": {
            "revision": register.revision,
            "sections": [
                {"id": s.id, "heading": s.heading, "body": s.body,
                 "content_hash": s.content_hash}
                for s in register.sections
            ],
        } if register else None,
        # Listed without their text. A pile can carry 10MB of documents and
        # this shape is refetched after every decision; the text is served
        # separately, once, when a reviewer actually opens one.
        "documents": [
            {"id": d["id"], "filename": d["filename"], "kind": d["kind"],
             "status": d["status"],
             "quarantine_reason": d.get("quarantine_reason"),
             "chars": len(d.get("text", "")),
             "cites": _citation_count(values, d["id"])}
            for d in values.get("documents", [])
        ],
        # Provenance, addressed by attribute so the interface can put a source
        # behind every line of the register.
        #
        # Without this the register is a list of assertions and a reviewer has
        # to take them on trust — which is the exact thing this system claims
        # not to ask of anyone. The quote is re-sliced from the document text at
        # the stored offsets rather than being echoed back, so what the browser
        # shows is the source, not a copy that could have drifted from it.
        "provenance": _provenance(values),
    }


def _citation_count(values: dict, document_id: str) -> int:
    """How many claims this document is the source for."""
    return sum(
        1
        for raw in values.get("claims", [])
        for c in raw.get("citations", [])
        if str(c["document_id"]) == document_id
    )


def _spans_in(values: dict, document_id: str) -> list[dict]:
    """Every cited span in one document, in reading order.

    This is the whole point of the viewer: a reviewer opens the source and sees
    the exact stretches of text the register is standing on, in place, in
    context. `verified` is computed by re-slicing the document at the stored
    offsets — if a citation has drifted from its source the viewer says so
    rather than showing the stored copy and looking correct.
    """
    text = next(
        (d.get("text", "") for d in values.get("documents", [])
         if d["id"] == document_id),
        "",
    )
    spans = []
    for raw in values.get("claims", []):
        claim = Claim.model_validate(raw)
        for citation in claim.citations:
            if str(citation.document_id) != document_id:
                continue
            spans.append({
                "attribute": claim.attribute,
                "value": claim.value,
                "char_start": citation.char_start,
                "char_end": citation.char_end,
                "verified": text[citation.char_start:citation.char_end] == citation.quote,
            })
    return sorted(spans, key=lambda s: s["char_start"])


def _resolve_citation(values: dict, citation: Citation) -> dict:
    """Re-slice one citation live against its source.

    The single place this happens, so every citation in the interface — a
    register line, a finding, an alert identifier — is checked the same way
    rather than trusted because it came from a different call site.
    """
    documents = {d["id"]: d for d in values.get("documents", [])}
    doc = documents.get(str(citation.document_id), {})
    source = doc.get("text", "")
    sliced = source[citation.char_start:citation.char_end]
    return {
        "document_id": str(citation.document_id),
        "filename": doc.get("filename", "unknown"),
        "char_start": citation.char_start,
        "char_end": citation.char_end,
        # Re-sliced live. If this ever disagrees with the stored quote the
        # citation is broken, and the interface should show that rather than
        # hide it behind the stored copy.
        "quote": sliced or citation.quote,
        "verified": bool(source) and sliced == citation.quote,
        # Enough surrounding text to see the quote in its sentence.
        "context_before": source[max(0, citation.char_start - 90):citation.char_start],
        "context_after": source[citation.char_end:citation.char_end + 90],
    }


def _provenance(values: dict) -> dict[str, list[dict]]:
    by_attribute: dict[str, list[dict]] = {}
    for raw in values.get("claims", []):
        claim = Claim.model_validate(raw)
        entries = by_attribute.setdefault(claim.attribute, [])
        for citation in claim.citations:
            entries.append({
                "value": claim.value,
                "support": claim.support.value,
                **_resolve_citation(values, citation),
            })
        if not claim.citations:
            entries.append({
                "value": claim.value, "support": claim.support.value,
                "filename": None, "quote": None, "verified": False,
                "char_start": None, "char_end": None,
                "context_before": "", "context_after": "",
            })
    return by_attribute


def _findings_by_id(values: dict) -> dict[str, dict]:
    """A finding's own evidence, the way an alert already carries its own.

    `summary` on the Decision is only severity + rule id + title — the values
    the rule actually compared, and citations for every one of them, live
    here instead of forcing a reviewer to reconstruct them from the register.
    """
    out = {}
    for raw in values.get("findings", []):
        finding = Finding.model_validate(raw)
        out[str(finding.id)] = {
            "rule_id": finding.rule_id,
            "detail": finding.detail,
            "severity": finding.severity.value,
            "citations": [_resolve_citation(values, c) for c in finding.citations],
            # None for every rule type except numeric_not_greater_than — see
            # the Finding model's own comment on why this isn't backfilled.
            "compared": finding.compared,
        }
    return out


def _conflict_evidence(values: dict, decision: Decision) -> dict | None:
    """Each side of a conflict: its value and its own citation. The one place
    this is computed — the UI builds both its evidence display and its
    per-option accept buttons from this same list, so there's no second,
    separately-filtered list of values that could drift out of alignment
    with it (there used to be exactly that: `_options_for` filtered claims
    with `if c.value`, this function didn't, and the two lists were only
    positionally correlated in the frontend — a latent bug, never triggered
    because no conflict in this system has produced an empty-value claim yet,
    removed by removing the second list rather than patching the filter)."""
    if decision.kind.value != "conflict":
        return None
    for raw in values.get("conflicts", []):
        conflict = Conflict.model_validate(raw)
        if conflict.id != decision.subject_id:
            continue
        return {
            "attribute": conflict.attribute,
            "claims": [
                {
                    "value": c.value,
                    "citations": [_resolve_citation(values, cit) for cit in c.citations],
                }
                for c in conflict.claims
            ],
        }
    return None


def _update_evidence(values: dict, decision: Decision) -> dict | None:
    """The outstanding proposal's own evidence, keyed onto its Decision.

    Unlike a conflict/finding/alert, a proposal has no domain id of its own
    to match `subject_id` against — `propose_arrival` mints a fresh one for
    the Decision and records it back onto the proposal as `decision_id`. This
    was previously only ever returned once, in the immediate response to
    submitting the arrival — a refresh lost it, because `_view()` never read
    `proposed_update` at all. Section hashes alone prove *which* sections a
    reviewer is approving; `value_changes` says what actually changed inside
    them.
    """
    if decision.kind.value != "update":
        return None
    proposal = values.get("proposed_update")
    if not proposal or proposal.get("decision_id") != str(decision.id):
        return None
    return {
        "arrival": proposal["arrival"],
        "document_kind": proposal["document"]["kind"],
        "sections_rebuilt": proposal["sections_rebuilt"],
        "sections_untouched": proposal["sections_untouched"],
        "value_changes": proposal.get("value_changes", []),
        "claims_added": proposal["claims_added"],
        "conflicts_raised": proposal["conflicts_raised"],
    }


# --------------------------------------------------------------------------
# API
# --------------------------------------------------------------------------


class RunRequest(BaseModel):
    pile: str
    checklist: str = DEFAULT_CHECKLIST
    thread: str | None = None


class SettleRequest(BaseModel):
    chosen_value: str | None = None
    note: str | None = None
    # Self-asserted; there is no authentication in this build. Recorded anyway,
    # because a decision log with no actor is not a decision log.
    reviewer: str | None = None


class ArrivalRequest(BaseModel):
    path: str


class ArchiveRequest(BaseModel):
    archived: bool = True


class ScreenRequest(BaseModel):
    name: str
    party_type: str = "entity"
    country: str = ""
    incorporated: str = ""
    date_of_birth: str = ""
    nationality: str = ""
    document_number: str = ""


# What each seeded pile is for, in a reviewer's words rather than mine.
#
# These were fixture names — "unidentifiable", "ambiguous" — which tell you
# nothing unless you wrote them. A pile is a scenario, and the interface should
# say which scenario it is before you spend a run finding out.
#
# Insertion order is the order the reviewer sees, and it is chosen rather than
# alphabetical. Sorting by directory name led with "ambiguous", which is a
# retry-path fixture and the least interesting thing here — so the first
# scenario anyone met was the weakest one in the build. The vendor with real
# defects goes first, the clean control second so "no findings" lands straight
# after "four findings", and the edge cases follow.
PILE_DESCRIPTIONS: dict[str, dict[str, str]] = {
    "meridian": {
        "title": "Vendor with problems",
        "blurb": "Ownership disputed between two filings, an invoice larger than "
                 "its contract, a document trying to give the system orders, and "
                 "a name that collides with a sanctions listing.",
        "expect": "4 findings, 1 conflict, 1 watchlist alert",
    },
    "northwind": {
        "title": "Vendor with nothing wrong",
        "blurb": "The control. Every rule runs and every rule passes. A checker "
                 "that cannot report a clean pile is not a checker.",
        "expect": "no findings, honestly",
    },
    "identified": {
        "title": "Beneficial owner who shares a name with a sanctioned person",
        "blurb": "A 100% name match against the watchlist, plus the passport "
                 "that clears him. Remove the passport and the same alert has to "
                 "be escalated instead.",
        "expect": "1 alert, discountable on date of birth and passport number",
    },
    "ambiguous": {
        "title": "Documents that do not say what they are",
        "blurb": "The usual phrases are stripped out, so the first pass fails to "
                 "identify them. The retry uses a different strategy and recovers.",
        "expect": "classify retries, then continues",
    },
    "unidentifiable": {
        "title": "Documents nothing can identify",
        "blurb": "Both passes fail. The run stops and asks for a human rather "
                 "than extracting facts against labels it does not trust.",
        "expect": "escalates, and extracts nothing",
    },
}


@app.get("/api/piles")
def list_piles() -> dict:
    root = REPO / "corpora"
    found = {
        p.name: len([f for f in p.iterdir() if f.is_file()])
        for p in root.iterdir()
        if p.is_dir() and not p.name.startswith("_")
    }
    # Seeded piles in their curated order, then anything uploaded, newest last.
    names = [n for n in PILE_DESCRIPTIONS if n in found]
    names += sorted(n for n in found if n not in PILE_DESCRIPTIONS)
    piles = [
        {"name": n, "documents": found[n],
         # A real field, not a string a client would have to pattern-match
         # ("Uploaded by you." in the blurb) to tell demo templates apart
         # from a reviewer's own.
         "demo": n in PILE_DESCRIPTIONS,
         **PILE_DESCRIPTIONS.get(n, {
             "title": n,
             "blurb": "Uploaded by you.",
             "expect": "",
         })}
        for n in names
    ]
    return {"piles": piles}


@app.post("/api/piles")
async def create_pile(name: str = Form(...),
                      files: list[UploadFile] = File(...)) -> dict:
    """Make a pile out of documents the reviewer supplies.

    The brief grades whether this works a second time on different documents
    inside the declared format set. Five pre-baked piles cannot demonstrate
    that — the reviewer has to be able to bring their own.

    Rejected formats are reported by name rather than skipped quietly. A pile
    that silently drops the one file you cared about is worse than one that
    refuses it.
    """
    # Basenames only, and no dotfiles. An uploaded filename is attacker-
    # controlled input; "../../etc/passwd" must land as a rejected name, not as
    # a path. Nothing here should be able to write outside corpora/.
    slug = re.sub(r"[^a-z0-9-]+", "-", name.strip().lower()).strip("-")
    if not slug:
        raise HTTPException(400, "give the pile a name")
    if slug in PILE_DESCRIPTIONS:
        raise HTTPException(409, f"{slug!r} is a seeded pile; pick another name")

    target = (REPO / "corpora" / slug).resolve()
    if target.parent != (REPO / "corpora").resolve():
        raise HTTPException(400, "invalid pile name")

    # Bounded, because `await upload.read()` with no argument pulls the whole
    # body into memory and the batch was buffered entirely before anything was
    # written. One large file was an unauthenticated way to exhaust the server.
    settings = get_settings()
    if len(files) > settings.max_upload_files:
        raise HTTPException(
            413,
            f"{len(files)} files; this accepts at most {settings.max_upload_files} "
            "in one upload",
        )

    accepted, rejected = [], []
    staged: list[tuple[Path, bytes]] = []
    seen: set[str] = set()
    budget = settings.max_upload_bytes
    for upload in files:
        filename = Path(upload.filename or "").name
        if not filename or filename.startswith("."):
            rejected.append({"filename": upload.filename, "why": "unusable filename"})
            continue
        if Path(filename).suffix.lower() not in SUPPORTED_SUFFIXES:
            rejected.append({
                "filename": filename,
                "why": f"unsupported format; this system reads "
                       f"{', '.join(sorted(SUPPORTED_SUFFIXES))}",
            })
            continue
        if filename in seen:
            # Two files with the same basename would otherwise both "accept",
            # and the second would silently overwrite the first on disk —
            # the response would report documents that no longer exist.
            rejected.append({
                "filename": filename,
                "why": "duplicate filename in this upload; rename one and retry",
            })
            continue
        seen.add(filename)

        # Read in bounded chunks and stop the moment the budget is gone, rather
        # than reading it all and measuring afterwards.
        blob = bytearray()
        while chunk := await upload.read(64 * 1024):
            blob.extend(chunk)
            if len(blob) > budget:
                raise HTTPException(
                    413,
                    f"{filename} exceeds the remaining upload budget "
                    f"({settings.max_upload_bytes} bytes per upload)",
                )
        budget -= len(blob)
        staged.append((target / filename, bytes(blob)))
        accepted.append(filename)

    if not accepted:
        raise HTTPException(
            400,
            "no readable documents. Supported formats: "
            + ", ".join(sorted(SUPPORTED_SUFFIXES)),
        )

    target.mkdir(parents=True, exist_ok=True)
    for path, blob in staged:
        path.write_bytes(blob)

    return {"pile": slug, "accepted": accepted, "rejected": rejected,
            "documents": len(accepted)}


@app.post("/api/screen")
def screen_party(req: ScreenRequest) -> dict:
    """Screen one name against the watchlist, without a pile.

    This is a tool affordance, not part of the brief: the pipeline derives its
    parties from documents, which is the right behaviour for a document system.
    But an analyst checking one name is a real thing analysts do, and it makes
    the three adjudication outcomes inspectable without preparing a corpus for
    each one.

    It raises alerts and evidences them. Like everywhere else, it settles
    nothing — there is no decision to record because there is no pile to hold.
    """
    path = REPO / "watchlists" / "synthetic_consolidated.json"
    if not path.is_file():
        raise HTTPException(503, "no watchlist is loaded, so nothing can be screened")

    watchlist, provenance = load_watchlist(path)
    alerts = screen(
        req.name.strip(),
        PartyType(req.party_type),
        watchlist,
        subject_role=req.party_type,
        subject_country=req.country or None,
        subject_incorporated=req.incorporated or None,
        subject_dob=req.date_of_birth or None,
        subject_nationality=req.nationality or None,
        subject_document_number=req.document_number or None,
    )
    return {
        "name": req.name,
        "watchlist_source": provenance.get("source", ""),
        "watchlist_name": provenance.get("source_name", ""),
        "watchlist_entries": len(watchlist),
        "alerts": [_alert_to_dict(a) for a in alerts],
        # Said explicitly. An empty list here means screened-and-clear, and the
        # interface must never let that be confused with not-screened.
        "screened": True,
    }


@app.post("/api/runs")
def start_run(req: RunRequest) -> dict:
    pile_path = REPO / "corpora" / req.pile
    if not pile_path.is_dir():
        raise HTTPException(404, f"no such pile: {req.pile}")

    run_id = new_run_id()
    thread = req.thread or f"{req.pile}-{run_id[:8]}"

    try:
        with pile_lock(req.pile, wait_seconds=10):
            with PostgresSaver.from_conn_string(_conn()) as cp:
                cp.setup()
                graph = _pipeline().build(checkpointer=cp)
                graph.invoke(
                    {"run_id": run_id, "pile_id": req.pile,
                     "pile_path": str(pile_path), "checklist_path": req.checklist},
                    _cfg(thread),
                )
                snap = graph.get_state(_cfg(thread))
    except PileBusy as busy:
        raise HTTPException(409, str(busy)) from busy

    return {"thread": thread, **_view(snap.values, list(snap.next))}


@app.get("/api/runs")
def list_runs(limit: int = 12) -> dict:
    """Recent cases, most recently touched first.

    Exists because a review that cannot be returned to is not a review. The
    checkpointer has held every case all along — the interface simply had no way
    to ask for one, so a refresh stranded the reviewer in front of an empty
    page while the work sat safely in Postgres, unreachable.

    Ordered by the checkpoint timestamp, which is stored inline on the row. The
    channel values live in a separate blob table, so the state itself is loaded
    only for the handful of threads actually being listed.
    """
    import psycopg

    with psycopg.connect(_conn()) as db:
        rows = db.execute(
            """
            SELECT thread_id, MAX(checkpoint->>'ts') AS touched
            FROM checkpoints
            GROUP BY thread_id
            ORDER BY touched DESC
            LIMIT %s
            """,
            (max(1, min(limit, 50)),),
        ).fetchall()

    cases = []
    with PostgresSaver.from_conn_string(_conn()) as cp:
        graph = _pipeline().build(checkpointer=cp)
        for thread, touched in rows:
            snap = graph.get_state(_cfg(thread))
            values = snap.values
            if not values or not values.get("pile_id"):
                continue  # a thread that never got past its first write
            decisions = values.get("decisions", [])
            pending = sum(1 for d in decisions if d.get("state") == "pending")
            cases.append({
                "thread": thread,
                "pile_id": values["pile_id"],
                "touched": touched,
                "pending": pending,
                "decisions": len(decisions),
                "archived": values.get("archived", False),
                # What the reviewer needs to decide whether to open it.
                "status": (
                    "escalated" if values.get("escalated")
                    else "blocked" if values.get("onboarding_blocked")
                    else "awaiting review" if pending
                    else "committed" if "commit" in snap.next or not snap.next
                    else "in progress"
                ),
            })
    return {"cases": cases}


@app.get("/api/runs/{thread}")
def get_run(thread: str) -> dict:
    values, paused = _snapshot(thread)
    return {"thread": thread, **_view(values, paused)}


@app.get("/api/runs/{thread}/documents/{document_id}")
def get_document(thread: str, document_id: str) -> dict:
    """One source document, with every span the register cites from it.

    Served on demand rather than with the run, because this is the only part of
    the payload that scales with the size of the pile.

    A quarantined document is returned in full, deliberately. Its text is the
    evidence for the finding against it — a reviewer should be able to read the
    instruction the system refused to obey, and see for themselves that no
    claim was extracted from it.
    """
    values, _ = _snapshot(thread)
    document = next(
        (d for d in values.get("documents", []) if d["id"] == document_id), None
    )
    if document is None:
        raise HTTPException(404, f"no document {document_id!r} in this case")

    return {
        "id": document["id"],
        "filename": document["filename"],
        "kind": document["kind"],
        "status": document["status"],
        "quarantine_reason": document.get("quarantine_reason"),
        "text": document.get("text", ""),
        "spans": _spans_in(values, document_id),
    }


def _settle(thread: str, index: int, state: DecisionState,
            chosen_value: str | None, note: str | None,
            reviewer: str | None = None) -> dict:
    # Held across the read and the write. Without it two reviewers settling
    # different items both succeed and one decision is silently discarded —
    # reproduced on the first attempt, kept reproduced in
    # tests/test_concurrent_review.py.
    with case_lock(thread), PostgresSaver.from_conn_string(_conn()) as cp:
        graph = _pipeline().build(checkpointer=cp)
        cfg = _cfg(thread)
        snap = graph.get_state(cfg)
        if not snap.values:
            raise HTTPException(404, f"no run found for thread {thread!r}")

        # Exactly one item moves. Every other decision keeps its own state.
        # Settling an update decision is what applies or discards its proposal.
        try:
            patch = gate_settle(snap.values, index, state, chosen_value, note,
                                by=reviewer)
        except GateError as exc:
            raise HTTPException(409, str(exc)) from exc
        except ValueError as exc:
            # e.g. escalating a finding. 422: the request was well formed, the
            # transition is not one this decision kind allows.
            raise HTTPException(422, str(exc)) from exc
        graph.update_state(cfg, patch)
        after = graph.get_state(cfg)

    return {"thread": thread, **_view(after.values, list(after.next))}


@app.exception_handler(PileBusy)
def _busy(request, exc: PileBusy):
    from fastapi.responses import JSONResponse

    # Contention is a real answer, not a failure. Say who is busy and why.
    return JSONResponse({"detail": str(exc)}, status_code=409)


@app.post("/api/runs/{thread}/decisions/{index}/approve")
def approve(thread: str, index: int, req: SettleRequest) -> dict:
    return _settle(thread, index, DecisionState.APPROVED, req.chosen_value,
                   req.note, reviewer=req.reviewer)


@app.post("/api/runs/{thread}/decisions/{index}/reject")
def reject(thread: str, index: int, req: SettleRequest) -> dict:
    return _settle(thread, index, DecisionState.REJECTED, None, req.note,
                   reviewer=req.reviewer)


@app.post("/api/runs/{thread}/decisions/{index}/escalate")
def escalate(thread: str, index: int, req: SettleRequest) -> dict:
    """The third outcome, for watchlist alerts only: could not be cleared."""
    return _settle(thread, index, DecisionState.ESCALATED, None, req.note,
                   reviewer=req.reviewer)


@app.post("/api/runs/{thread}/resume")
def resume(thread: str) -> dict:
    with case_lock(thread), PostgresSaver.from_conn_string(_conn()) as cp:
        graph = _pipeline().build(checkpointer=cp)
        cfg = _cfg(thread)
        values = graph.get_state(cfg).values
        if not values:
            raise HTTPException(404, f"no run found for thread {thread!r}")
        # The gate is a barrier. Committing while items sit unreviewed would
        # make every "Committed" message a claim nobody had checked.
        if pending := unsettled(values):
            raise HTTPException(409, refusal(pending))
        graph.invoke(None, cfg)   # None resumes rather than restarting
        snap = graph.get_state(cfg)
    return {"thread": thread, **_view(snap.values, list(snap.next))}


@app.post("/api/runs/{thread}/archive")
def set_archived(thread: str, req: ArchiveRequest) -> dict:
    """A soft hide from the main case list, not a pipeline decision.

    Pure checkpoint mutation — the same `case_lock` + `graph.update_state`
    shape `_settle` and `resume` already use, but with no `gate_settle` call
    and no `graph.invoke`: nothing here is a decision, and nothing here runs
    graph logic. Archiving must never touch decisions, the register, or
    anything else already in state — this patch names exactly one key.
    """
    with case_lock(thread), PostgresSaver.from_conn_string(_conn()) as cp:
        graph = _pipeline().build(checkpointer=cp)
        cfg = _cfg(thread)
        if not graph.get_state(cfg).values:
            raise HTTPException(404, f"no run found for thread {thread!r}")
        graph.update_state(cfg, {"archived": req.archived})
        snap = graph.get_state(cfg)
    return {"thread": thread, **_view(snap.values, list(snap.next))}


@app.get("/api/runs/{thread}/arrivals")
def list_arrivals(thread: str) -> dict:
    """Only the files generated for this case's own pile.

    Ownership is the filename prefix — `<pile_id>_...` — which the upload
    endpoint below owns and never trusts from a client. A pile id is a slug
    (`[a-z0-9-]+`, never contains `_`), so splitting on the first underscore
    is unambiguous.
    """
    values, _ = _snapshot(thread)
    prefix = f"{values['pile_id']}_"
    root = REPO / "corpora" / "_arrivals"
    files = (
        sorted(f.name for f in root.iterdir() if f.is_file() and f.name.startswith(prefix))
        if root.is_dir() else []
    )
    return {"arrivals": files}


@app.post("/api/runs/{thread}/arrivals/upload")
async def upload_arrival(thread: str, file: UploadFile = File(...)) -> dict:
    """A reviewer's own case gets a real arrival, not just the bundled ones.

    Same validation as creating a pile: basename only, extension allowlist,
    bounded read. The stored name is prefixed with this case's own pile id —
    that prefix, not anything the client sends, is what `arrival()` below
    checks before applying one.
    """
    values, _ = _snapshot(thread)
    pile_id = values["pile_id"]

    filename = Path(file.filename or "").name
    if not filename or filename.startswith("."):
        raise HTTPException(400, "unusable filename")
    if Path(filename).suffix.lower() not in SUPPORTED_SUFFIXES:
        raise HTTPException(
            400,
            f"unsupported format; this system reads {', '.join(sorted(SUPPORTED_SUFFIXES))}",
        )

    settings = get_settings()
    blob = bytearray()
    while chunk := await file.read(64 * 1024):
        blob.extend(chunk)
        if len(blob) > settings.max_upload_bytes:
            raise HTTPException(
                413, f"{filename} exceeds the upload budget ({settings.max_upload_bytes} bytes)"
            )

    root = REPO / "corpora" / "_arrivals"
    root.mkdir(parents=True, exist_ok=True)
    stored_name = f"{pile_id}_{filename}"
    target = root / stored_name
    if target.exists():
        raise HTTPException(409, f"{filename!r} was already uploaded as an arrival for this case")
    target.write_bytes(bytes(blob))
    return {"arrival": stored_name}


@app.post("/api/runs/{thread}/arrivals")
def arrival(thread: str, req: ArrivalRequest) -> dict:
    filename = Path(req.path).name

    with case_lock(thread), PostgresSaver.from_conn_string(_conn()) as cp:
        graph = _pipeline().build(checkpointer=cp)
        cfg = _cfg(thread)
        snap = graph.get_state(cfg)
        if not snap.values:
            raise HTTPException(404, f"no run found for thread {thread!r}")
        if not filename.startswith(f"{snap.values['pile_id']}_"):
            raise HTTPException(
                403, f"{filename!r} was not generated for this case (pile {snap.values['pile_id']!r})"
            )
        path = REPO / "corpora" / "_arrivals" / filename
        if not path.is_file():
            raise HTTPException(404, f"no such arrival: {req.path}")
        values = snap.values

        # An arrival proposes. Nothing in the register moves until the update
        # decision it raises is approved.
        try:
            patch, proposal = propose_arrival(
                values, path, build_adapter(get_settings())
            )
        except GateError as exc:
            raise HTTPException(409, str(exc)) from exc
        graph.update_state(cfg, patch)
        after = graph.get_state(cfg)

    if proposal is None:
        return {
            "thread": thread,
            "queued": {
                "name": filename,
                "position": len(patch["pending_arrivals"]),
                "note": "an update is already outstanding for this case; "
                        "this one becomes available once that one is settled",
            },
            **_view(after.values, list(after.next)),
        }

    return {
        "thread": thread,
        "proposed": {
            "name": proposal["arrival"],
            "kind": proposal["document"]["kind"],
            "claims_added": proposal["claims_added"],
            "conflicts_raised": proposal["conflicts_raised"],
            "sections_would_rebuild": proposal["sections_rebuilt"],
            "sections_would_not_touch": proposal["sections_untouched"],
            "model_calls": (proposal["cost"] or {}).get("model_calls", 0),
            "hashes_before": proposal["hashes_before"],
            "hashes_after": proposal["hashes_after"],
            "decision_index": len(patch["decisions"]) - 1,
        },
        **_view(after.values, list(after.next)),
    }


@app.get("/api/health")
def health() -> dict:
    settings = get_settings()
    return {
        "ok": True,
        "llm_provider": settings.llm_provider,
        "uses_live_model": settings.uses_live_model,
    }


# --------------------------------------------------------------------------
# static UI
# --------------------------------------------------------------------------

if UI_DIST.is_dir():
    app.mount("/assets", StaticFiles(directory=UI_DIST / "assets"), name="assets")

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(UI_DIST / "index.html")
else:
    @app.get("/")
    def index_missing() -> dict:
        return {
            "error": "the UI has not been built",
            "fix": "run `npm --prefix ui install && npm --prefix ui run build`, "
                   "or use `docker compose up --build` which does it for you",
            "api_still_works": "/api/piles, /api/runs, /docs",
        }
