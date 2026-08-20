"""An update must cost like an update, and must prove what it left alone.

The proof is about the work performed, not just the output. Rebuilding every
section and putting the unchanged ones back would produce identical bytes while
doing all the work — the brief calls that out by name — so these tests assert
on which sections were rebuilt as well as on which hashes moved.
"""

from __future__ import annotations

import pytest

from app.core.llm import Meter, OfflineAdapter
from app.domain.rules import Checklist, ChecklistEngine
from app.pipeline.extract import classify, detect_conflicts, extract_claims, screen_document
from app.pipeline.graph import build_register
from app.pipeline.ingest import load_pile
from app.pipeline.update import apply_arrival, prove_untouched
from tests.conftest import screen_claims


@pytest.fixture
def baseline(meridian, checklist_path):
    meter = Meter(OfflineAdapter(), max_calls=200)
    documents, claims = [], []
    for document, chunks in load_pile(meridian, "meridian"):
        document = screen_document(document)
        document = document.model_copy(update={"kind": classify(document, meter)})
        documents.append(document)
        claims.extend(extract_claims(document, chunks, meter, "meridian"))
    conflicts = detect_conflicts(claims, "meridian")
    register = build_register("meridian", claims, conflicts)
    return register, claims, conflicts, meter.usage.calls


@pytest.mark.parametrize("arrival_name,expected_section,expected_attribute", [
    ("meridian_09_rescreen.txt", "screening", "screening_result"),
    ("meridian_10_amendment_one.txt", "commercial", "contract_value"),
])
def test_arrival_rebuilds_only_the_section_it_affects(
    baseline, arrivals, arrival_name, expected_section, expected_attribute
):
    register, claims, conflicts, _ = baseline
    result = apply_arrival(
        arrivals / arrival_name, register, claims, conflicts,
        OfflineAdapter(), "meridian",
    )
    proof = prove_untouched(register, result.register, [expected_section])

    # what was rebuilt, not merely what changed
    assert result.sections_rebuilt == [expected_section]
    assert expected_section not in result.sections_untouched

    # and the hashes agree
    assert proof["changed"] == [expected_section]
    assert len(proof["unchanged"]) == 4
    assert proof["matches_expectation"]

    assert expected_attribute in {c.attribute for c in result.new_conflicts}


def test_an_update_costs_far_less_than_a_full_run(baseline, arrivals):
    register, claims, conflicts, full_run_calls = baseline
    result = apply_arrival(
        arrivals / "meridian_09_rescreen.txt", register, claims, conflicts,
        OfflineAdapter(), "meridian",
    )
    assert result.cost.model_calls == 2, "one classify and one extract, for one document"
    assert result.cost.model_calls < full_run_calls / 3


def test_contradiction_is_surfaced_never_applied(baseline, arrivals):
    """A re-screen clearing a flagged match is almost certainly a supersession.
    Almost certainly is not a standard a compliance register should apply."""
    register, claims, conflicts, _ = baseline
    result = apply_arrival(
        arrivals / "meridian_09_rescreen.txt", register, claims, conflicts,
        OfflineAdapter(), "meridian",
    )
    screening = next(s for s in result.register.sections if s.id == "screening")
    assert "DISPUTED" in screening.body
    assert "no match" in screening.body and "potential match" in screening.body
    assert all(not c.is_resolved for c in result.new_conflicts)


def test_provenance_answers_what_changed_and_why(baseline, arrivals):
    register, claims, conflicts, _ = baseline
    result = apply_arrival(
        arrivals / "meridian_10_amendment_one.txt", register, claims, conflicts,
        OfflineAdapter(), "meridian",
    )
    provenance = result.provenance()
    assert provenance["because_of"] == "meridian_10_amendment_one.txt"
    assert provenance["sections_rebuilt"] == ["commercial"]
    assert set(provenance["sections_untouched"]) == {
        "identity", "ownership", "screening", "banking"
    }
    assert provenance["conflicts_raised"] == ["contract_value"]
    assert provenance["model_calls"] == 2
    assert provenance["at"] > 0


def test_value_changes_names_what_a_section_hash_only_proves_moved(baseline, arrivals):
    """sections_rebuilt proves *which* section changed; it says nothing about
    *what* changed within it. A reviewer approving blind still had to open the
    register and diff it by eye — this is that diff, computed once, alongside
    the proposal.

    screening_date is not in CONFLICTABLE, so a new re-screen date is a clean
    supersession and belongs here. screening_result *is* conflictable and this
    exact arrival disputes it (test_contradiction_is_surfaced_never_applied) —
    it must NOT appear here, because "old -> new" would misstate a dispute as
    a settled fact.
    """
    register, claims, conflicts, _ = baseline
    result = apply_arrival(
        arrivals / "meridian_09_rescreen.txt", register, claims, conflicts,
        OfflineAdapter(), "meridian",
    )
    by_attribute = {c["attribute"]: c for c in result.value_changes}

    assert "screening_date" in by_attribute, (
        f"expected a clean value change for screening_date, got {sorted(by_attribute)}"
    )
    change = by_attribute["screening_date"]
    assert change["new_value"] != change["old_value"]
    assert change["old_value"] is not None, "meridian's baseline already has a screening date"

    assert "screening_result" not in by_attribute, (
        "screening_result is disputed by this arrival, not cleanly superseded "
        "-- it belongs in conflicts_raised, not value_changes"
    )


def test_amendment_captures_the_new_value_not_the_superseded_one(arrivals):
    """"amended from USD 48,000 to USD 96,000" — recording 48,000 would store
    the exact figure the document exists to supersede."""
    text = (arrivals / "meridian_10_amendment_one.txt").read_text()
    found = OfflineAdapter().extract(text, ["contract_value"]).extractions
    assert [e.value for e in found] == ["USD 96,000.00"]
    assert text[found[0].char_start:found[0].char_end] == found[0].value


def test_a_plain_contract_still_extracts_its_own_value():
    """Regression guard on the amendment pattern."""
    found = OfflineAdapter().extract(
        "Contract value: USD 48,000.00", ["contract_value"]
    ).extractions
    assert [e.value for e in found] == ["USD 48,000.00"]


def test_second_run_on_a_different_document_set(northwind, checklist_path, watchlist_path):
    """The brief grades whether it works a second time on different documents
    inside the declared set, not just on the demo pile."""
    meter = Meter(OfflineAdapter(), max_calls=200)
    documents, claims = [], []
    for document, chunks in load_pile(northwind, "northwind"):
        document = screen_document(document)
        document = document.model_copy(update={"kind": classify(document, meter)})
        documents.append(document)
        claims.extend(extract_claims(document, chunks, meter, "northwind"))

    assert len(documents) == 6
    assert claims, "a different document set produced nothing"

    result = ChecklistEngine(
        claims, detect_conflicts(claims, "northwind"), documents, "northwind",
        alerts=screen_claims(claims, watchlist_path),
    ).evaluate(Checklist.load(checklist_path))
    assert result.findings == []
    assert result.not_evaluated == []
