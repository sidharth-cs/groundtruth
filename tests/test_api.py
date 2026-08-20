"""The HTTP surface, and the parts of it that take input from outside.

Upload is the only endpoint that accepts attacker-controlled filenames, so it
gets the most attention here. Everything else in this system reads files the
operator put there deliberately; this one writes files a browser sent.
"""

from __future__ import annotations

import shutil

import pytest
from fastapi.testclient import TestClient

from app.api.main import REPO, app


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def cleanup():
    made: list[str] = []
    yield made
    for slug in made:
        shutil.rmtree(REPO / "corpora" / slug, ignore_errors=True)
        # Arrivals uploaded for this pile are always named "<slug>_...", by
        # construction (upload_arrival owns the prefix) — sweep those too, or
        # a re-run of a test that uploads a fixed filename collides with
        # itself via the 409 duplicate-name guard.
        for f in (REPO / "corpora" / "_arrivals").glob(f"{slug}_*"):
            f.unlink(missing_ok=True)


def upload(client, name, files):
    return client.post(
        "/api/piles",
        data={"name": name},
        files=[("files", (fn, body, "text/plain")) for fn, body in files],
    )


# --------------------------------------------------------------------------
# upload
# --------------------------------------------------------------------------


@pytest.mark.parametrize("hostile,lands_as", [
    # Every one of these carries a SUPPORTED extension on purpose. With a bare
    # "../../etc/passwd" the format check refuses it first and the traversal
    # guard is never reached — the test would pass with the basename stripping
    # deleted, which makes it worse than no test. These get past the format
    # check, so only `Path(name).name` stands between them and an arbitrary
    # write.
    ("../../../../tmp/pwned.txt", "pwned.txt"),
    ("../outside.txt", "outside.txt"),
    ("/tmp/absolute.txt", "absolute.txt"),
    ("....//....//escape.md", "escape.md"),
])
def test_a_hostile_filename_cannot_escape_the_corpora_directory(
    client, cleanup, hostile, lands_as
):
    """An uploaded filename is input, not a path.

    The failure guarded against here is not subtle: `(target / name).write_bytes`
    with a name of "../../x.txt" writes wherever the traversal points.
    """
    cleanup.append("traversal-probe")
    response = upload(client, "traversal probe", [(hostile, b"payload")])
    assert response.status_code == 200

    pile = (REPO / "corpora" / "traversal-probe").resolve()
    written = [p.resolve() for p in pile.rglob("*") if p.is_file()]

    # Flattened to a basename and kept inside the pile.
    assert [p.name for p in written] == [lands_as]
    for path in written:
        assert pile in path.parents, f"escaped to {path}"

    # And nothing appeared where the traversal was aiming.
    for stray in (REPO.parent / "outside.txt", REPO / "outside.txt",
                  REPO.parent / "escape.md"):
        assert not stray.exists(), f"{stray} was written"


def test_unsupported_formats_are_named_not_dropped(client, cleanup):
    """A pile that silently loses the one file you cared about is worse than
    one that refuses it out loud."""
    cleanup.append("mixed-formats")
    response = upload(client, "mixed formats", [
        ("good.txt", b"Registered name: Probe Ltd\n"),
        ("bad.xyz", b"nope"),
        ("also_bad.exe", b"nope"),
    ])
    body = response.json()
    assert response.status_code == 200
    assert body["accepted"] == ["good.txt"]
    assert {r["filename"] for r in body["rejected"]} == {"bad.xyz", "also_bad.exe"}
    assert "unsupported format" in body["rejected"][0]["why"]


def test_an_upload_with_nothing_readable_is_refused(client):
    response = upload(client, "all junk", [("a.xyz", b"x"), ("b.exe", b"y")])
    assert response.status_code == 400
    assert "no readable documents" in response.json()["detail"]


def test_a_seeded_pile_cannot_be_overwritten(client):
    """Uploading over `meridian` would quietly change what every other test and
    every demo is run against."""
    response = upload(client, "meridian", [("x.txt", b"content")])
    assert response.status_code == 409


def test_an_unnamed_pile_is_refused(client):
    assert upload(client, "   ", [("x.txt", b"content")]).status_code == 400
    # A name that slugs to nothing is the same case arriving differently.
    assert upload(client, "!!!", [("x.txt", b"content")]).status_code == 400


def test_a_duplicate_filename_in_one_upload_is_rejected_not_overwritten(client, cleanup):
    """Two files named the same in one batch used to both "accept", and the
    second silently overwrote the first on disk — the response would then
    claim two documents landed when only one actually existed afterward."""
    cleanup.append("dup-probe")
    response = upload(client, "dup probe", [
        ("contract.txt", b"first upload, must be the one that lands"),
        ("contract.txt", b"second upload, must be rejected"),
    ])
    body = response.json()
    assert response.status_code == 200
    assert body["accepted"] == ["contract.txt"]
    assert body["rejected"] == [
        {"filename": "contract.txt", "why": "duplicate filename in this upload; rename one and retry"}
    ]

    on_disk = REPO / "corpora" / "dup-probe" / "contract.txt"
    assert on_disk.read_bytes() == b"first upload, must be the one that lands"


# --------------------------------------------------------------------------
# arrivals
# --------------------------------------------------------------------------


def test_a_cases_arrivals_are_scoped_to_its_own_pile(client, a_case):
    """meridian's bundled arrival must not be offered to, or applicable
    against, a northwind case. Both used to read one flat, unscoped
    directory — any case could see and apply any pile's arrival."""
    meridian_thread = a_case["thread"]
    northwind_thread = client.post("/api/runs", json={"pile": "northwind"}).json()["thread"]

    meridian_arrivals = client.get(f"/api/runs/{meridian_thread}/arrivals").json()["arrivals"]
    assert "meridian_09_rescreen.txt" in meridian_arrivals

    northwind_arrivals = client.get(f"/api/runs/{northwind_thread}/arrivals").json()["arrivals"]
    assert "meridian_09_rescreen.txt" not in northwind_arrivals, (
        "a case must not be offered another pile's arrival"
    )

    refused = client.post(
        f"/api/runs/{northwind_thread}/arrivals",
        json={"path": "meridian_09_rescreen.txt"},
    )
    assert refused.status_code == 403, refused.text


def test_a_reviewer_can_upload_and_apply_their_own_arrival(client, cleanup):
    """Bring-your-own-case must complete all three movements. There used to
    be no way to supply an arrival for a case you uploaded yourself — only
    the bundled meridian_NN_*.txt files, referenced by path, could ever be
    applied."""
    cleanup.append("own-arrival-probe")
    upload(client, "own arrival probe", [
        ("incorporation.txt",
         b"Registered name: Probe Freight Ltd\nCompany number: PFL-001\n"),
    ])
    thread = client.post("/api/runs", json={"pile": "own-arrival-probe"}).json()["thread"]

    uploaded = client.post(
        f"/api/runs/{thread}/arrivals/upload",
        files={"file": ("update.txt", b"Company number: PFL-001-A\n", "text/plain")},
    )
    assert uploaded.status_code == 200, uploaded.text
    stored_name = uploaded.json()["arrival"]
    assert stored_name.startswith("own-arrival-probe_"), (
        "the server must own the prefix, not trust one from the client"
    )

    listed = client.get(f"/api/runs/{thread}/arrivals").json()["arrivals"]
    assert stored_name in listed

    applied = client.post(f"/api/runs/{thread}/arrivals", json={"path": stored_name})
    assert applied.status_code == 200, applied.text


def test_an_updates_evidence_survives_a_refresh_not_just_the_first_response(client, a_case):
    """proposed_update carried the rebuild proof and the value-level diff, but
    _view() never read it — a reviewer who submitted an arrival and then
    reloaded the page lost that evidence entirely; only the terse one-line
    summary survived. This proves a *second, independent* GET sees the same
    evidence the original POST did, not that the POST response alone has it.
    """
    thread = a_case["thread"]
    posted = client.post(
        f"/api/runs/{thread}/arrivals", json={"path": "meridian_09_rescreen.txt"}
    )
    assert posted.status_code == 200, posted.text

    refetched = client.get(f"/api/runs/{thread}").json()
    update_decision = next(d for d in refetched["decisions"] if d["kind"] == "update")
    evidence = update_decision["update"]
    assert evidence is not None
    assert evidence["arrival"] == "meridian_09_rescreen.txt"
    assert evidence["sections_rebuilt"] == ["screening"]

    changed = {c["attribute"] for c in evidence["value_changes"]}
    assert "screening_date" in changed
    assert "screening_result" not in changed, (
        "screening_result is disputed by this arrival, not superseded"
    )


def test_uploading_the_same_arrival_name_twice_is_rejected_not_overwritten(client, cleanup):
    """The exact bug fixed in create_pile (duplicate filenames silently
    overwriting each other) applies equally here if left unguarded."""
    cleanup.append("dup-arrival-probe")
    upload(client, "dup arrival probe", [("incorporation.txt", b"Registered name: X\n")])
    thread = client.post("/api/runs", json={"pile": "dup-arrival-probe"}).json()["thread"]

    first = client.post(
        f"/api/runs/{thread}/arrivals/upload",
        files={"file": ("update.txt", b"first", "text/plain")},
    )
    assert first.status_code == 200
    second = client.post(
        f"/api/runs/{thread}/arrivals/upload",
        files={"file": ("update.txt", b"second", "text/plain")},
    )
    assert second.status_code == 409, second.text


# --------------------------------------------------------------------------
# case lifecycle
# --------------------------------------------------------------------------


def test_a_thread_reads_unarchived_by_default(client, a_case):
    """`archived` did not exist as a field until this session -- every
    checkpoint written before it must still read False, not error or come
    back missing. `total=False` on PipelineState is what makes this true;
    this proves it rather than trusting the TypedDict declaration."""
    assert a_case["archived"] is False


def test_archiving_a_case_touches_nothing_else(client, a_case):
    """The one place this plan adds new persisted state, and reproducibility
    is explicitly the constraint on it: archiving must be a pure label, never
    a mutation of decisions, the register, or anything a reviewer already
    settled."""
    thread = a_case["thread"]
    before = client.get(f"/api/runs/{thread}").json()

    archived = client.post(f"/api/runs/{thread}/archive", json={"archived": True})
    assert archived.status_code == 200, archived.text
    assert archived.json()["archived"] is True

    after = client.get(f"/api/runs/{thread}").json()
    assert after["archived"] is True
    assert after["decisions"] == before["decisions"]
    assert after["register"] == before["register"]
    assert after["stages"] == before["stages"]

    # And it's reversible -- a soft hide, not a one-way door.
    unarchived = client.post(f"/api/runs/{thread}/archive", json={"archived": False})
    assert unarchived.json()["archived"] is False


def test_the_recent_cases_list_reports_archived_state(client, a_case):
    thread = a_case["thread"]
    client.post(f"/api/runs/{thread}/archive", json={"archived": True})
    cases = client.get("/api/runs?limit=50").json()["cases"]
    entry = next(c for c in cases if c["thread"] == thread)
    assert entry["archived"] is True
    client.post(f"/api/runs/{thread}/archive", json={"archived": False})  # tidy up


def test_archiving_a_nonexistent_thread_is_refused(client):
    response = client.post("/api/runs/does-not-exist/archive", json={"archived": True})
    assert response.status_code == 404


# --------------------------------------------------------------------------
# ad-hoc screening
# --------------------------------------------------------------------------


def test_ad_hoc_screening_reaches_all_three_outcomes(client):
    """The panel exists so the outcomes are inspectable without building a
    corpus for each one. If it can only ever produce one, it is decoration."""

    def verdict(**payload):
        body = client.post("/api/screen", json=payload).json()
        assert body["screened"] is True
        return [a["recommendation"] for a in body["alerts"]]

    assert "true_match" in verdict(
        name="Pioneer Logistics", party_type="entity", country="Iran")
    assert "escalate_aoc" in verdict(
        name="Pioneer Logistics", party_type="entity")
    assert "false_positive" in verdict(
        name="Anselm Rhodes Vanterpool", party_type="individual",
        date_of_birth="14 August 1986", document_number="548812093")


def test_screened_and_clear_is_distinguishable_from_never_screened(client):
    """An empty alert list must come with the fact that screening ran."""
    body = client.post("/api/screen", json={
        "name": "Northwind Fulfilment Services Ltd", "party_type": "entity",
    }).json()
    assert body["alerts"] == []
    assert body["screened"] is True
    assert body["watchlist_entries"] > 0
    assert "make_watchlist.py" in body["watchlist_source"]


# --------------------------------------------------------------------------
# the audit trail
# --------------------------------------------------------------------------


def test_a_settled_decision_records_who_decided_and_why(client):
    """Who, when, why. A decision log carrying only a state is not a decision
    log, and an examiner asks all three.

    The name is self-asserted — there is no authentication in this build — but
    unverified is not the same as absent, and the README says which it is.
    """
    thread = client.post("/api/runs", json={"pile": "meridian"}).json()["thread"]
    body = client.post(
        f"/api/runs/{thread}/decisions/1/reject",
        json={"note": "amendment supersedes it", "reviewer": "A. Reviewer"},
    ).json()

    settled = body["decisions"][1]
    assert settled["state"] == "rejected"
    assert settled["decided_by"] == "A. Reviewer", (
        "the reviewer's name was sent and not recorded — this regressed once "
        "already, silently, because 'unnamed reviewer' looks like a default "
        "rather than a dropped field"
    )
    assert settled["note"] == "amendment supersedes it"
    assert settled["decided_at"]


def test_an_unnamed_reviewer_is_recorded_as_unnamed_not_as_nobody(client):
    """Refusing to name yourself is allowed. Pretending the decision made
    itself is not."""
    thread = client.post("/api/runs", json={"pile": "meridian"}).json()["thread"]
    body = client.post(f"/api/runs/{thread}/decisions/1/reject", json={}).json()
    assert body["decisions"][1]["decided_by"] == "unnamed reviewer"


# --------------------------------------------------------------------------
# the document viewer
# --------------------------------------------------------------------------


@pytest.fixture
def a_case(client):
    return client.post("/api/runs", json={"pile": "meridian"}).json()


def test_a_cited_span_reslices_from_the_document_it_names(client, a_case):
    """The one claim this system rests on: *this* value came from *this* place
    in *this* file. It is only checkable if a reviewer can open the source and
    find the words at the offsets the register cites."""
    thread = a_case["thread"]
    source = a_case["provenance"]["ubo_percentage"][0]
    body = client.get(f"/api/runs/{thread}/documents/{source['document_id']}").json()

    assert body["filename"] == source["filename"]
    # Sliced from the text the endpoint returned, not from a stored copy.
    assert body["text"][source["char_start"]:source["char_end"]] == source["quote"]

    span = next(s for s in body["spans"] if s["char_start"] == source["char_start"])
    assert span["attribute"] == "ubo_percentage"
    assert span["verified"] is True


def test_the_quarantined_document_is_readable_and_cites_nothing(client, a_case):
    """Behaviour 8 is only convincing if a reviewer can read the instruction the
    system refused to obey and see for themselves that nothing was taken from
    it. Hiding the payload would make the defence unverifiable."""
    thread = a_case["thread"]
    listed = next(d for d in a_case["documents"] if d["status"] == "quarantined")
    assert listed["cites"] == 0, "a quarantined document must contribute no claims"

    body = client.get(f"/api/runs/{thread}/documents/{listed['id']}").json()
    assert "SYSTEM INSTRUCTION" in body["text"], "the payload must remain readable"
    assert body["spans"] == []
    assert body["quarantine_reason"]


def test_every_document_reports_how_many_claims_it_supports(client, a_case):
    """A source nobody cited is a fact about the pile, not a gap in the table."""
    counted = {d["filename"]: d["cites"] for d in a_case["documents"]}
    assert sum(counted.values()) > 0
    assert all(d["chars"] > 0 for d in a_case["documents"])


# --------------------------------------------------------------------------
# checklist summary
# --------------------------------------------------------------------------


def test_checklist_summary_surfaces_not_evaluated_as_a_first_class_field(client, a_case):
    """`not_evaluated` used to exist only inside a raw stages[].detail dump —
    computed correctly by the engine, then dropped before it became something
    a reviewer would actually see. It must be its own field, not archaeology.

    Meridian's watchlist alert has not been adjudicated yet at check time — the
    graph interrupts at the gate before any human has looked — so SCR-004
    (no_unresolved_watchlist_alert) is genuinely NOT_EVALUATED, not FAILED:
    "the reviewer has not looked yet" is exactly the honest state this field
    exists to carry. Real data from a real run, not a constructed fixture.
    """
    summary = a_case["checklist_summary"]
    assert summary is not None
    assert summary["total_rules"] == (
        summary["passed"] + summary["failed"] + len(summary["not_evaluated"])
    )
    assert "SCR-004" in {r["rule_id"] for r in summary["not_evaluated"]}
    for rule in summary["not_evaluated"]:
        assert rule["note"], f"{rule['rule_id']} has no reason a reviewer could act on"

    finding_decisions = [d for d in a_case["decisions"] if d["kind"] == "finding"]
    assert summary["failed"] == len(finding_decisions), (
        "the tally must agree with what the gate actually raised for review"
    )
    assert summary["summary"], "the human-readable sentence must be present too"


def test_a_run_that_escalates_before_check_reports_no_checklist_summary(client):
    """"unidentifiable" stops at classify — two passes, still unlabeled, the
    graph refuses to extract facts against labels it does not trust. `check()`
    never runs, so `checklist_summary` must never appear as a real-looking
    value either. LangGraph defaults an untouched `Annotated[dict, ...]`
    channel to `{}`, not to an absent key — `{}` is falsy in Python but
    truthy in JS, so the frontend's `run?.checklist_summary && <.../>` guard
    would render a checklist card with no `checklist_name`, no rule counts,
    nothing: a card that lies about a check that never happened. Must be
    `None`, which JSON-serializes to `null` and the same guard correctly
    treats as absent.
    """
    body = client.post("/api/runs", json={"pile": "unidentifiable"}).json()
    assert body["escalated"] is True
    assert body["checklist_summary"] is None


# --------------------------------------------------------------------------
# finding evidence
# --------------------------------------------------------------------------


def test_a_finding_carries_its_own_evidence_like_an_alert_does(client, a_case):
    """A FINDING decision used to ship only severity + rule id + title —
    strictly less evidence than an ALERT gets for the same adjudication task.
    COM-001 (invoice vs. contract) is the sharpest case: both compared values
    live in `detail`, and after the citation fix (tests/test_checklist.py::
    test_com001_finding_cites_both_the_invoice_and_the_contract) both
    documents must be openable from here, not just the invoice.
    """
    com001 = next(d for d in a_case["decisions"]
                   if d["kind"] == "finding" and "COM-001" in d["summary"])
    finding = com001["finding"]
    assert finding is not None
    assert finding["rule_id"] == "COM-001"
    assert "invoice_total" in finding["detail"] and "contract_value" in finding["detail"]

    cited_filenames = {c["filename"] for c in finding["citations"]}
    assert len(cited_filenames) >= 2, (
        f"expected both the invoice and the contract cited, got {cited_filenames}"
    )
    for citation in finding["citations"]:
        assert citation["verified"] is True, f"broken citation: {citation}"

    assert finding["compared"] == {
        "left_attribute": "invoice_total", "left_value": "USD 84,500.00",
        "right_attribute": "contract_value", "right_value": "USD 48,000.00",
        "difference": "USD 36,500.00",
    }


@pytest.mark.parametrize("document_id", [
    "not-a-real-id", "../../etc/passwd", "00000000-0000-0000-0000-000000000000",
])
def test_asking_for_a_document_that_is_not_in_the_case_is_refused(
    client, a_case, document_id
):
    response = client.get(f"/api/runs/{a_case['thread']}/documents/{document_id}")
    assert response.status_code == 404


# --------------------------------------------------------------------------
# piles
# --------------------------------------------------------------------------


def test_every_seeded_pile_says_what_it_is(client):
    """Fixture names told a reviewer nothing. "unidentifiable" is not a
    description of anything unless you wrote it."""
    piles = client.get("/api/piles").json()["piles"]
    assert len(piles) >= 5
    for pile in piles:
        assert pile["title"] and pile["title"] != pile["name"], pile["name"]
        assert pile["blurb"]
        assert pile["documents"] > 0


def test_demo_piles_are_flagged_by_a_real_field_not_by_matching_a_string(client, cleanup):
    """A client used to have to pattern-match "Uploaded by you." in the blurb
    to tell a demo template from a reviewer's own pile -- fragile, and wrong
    the moment the blurb's wording changed for any other reason."""
    cleanup.append("demo-flag-probe")
    upload(client, "demo flag probe", [("incorporation.txt", b"Registered name: X\n")])
    piles = {p["name"]: p for p in client.get("/api/piles").json()["piles"]}

    assert piles["meridian"]["demo"] is True
    assert piles["demo-flag-probe"]["demo"] is False
