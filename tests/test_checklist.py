"""The checker must be capable of finding nothing, and must never pass a rule
it could not run.

A checker that always reports something is not a checker, and a rule that
quietly passes when its evidence is missing is worse than no rule at all.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.llm import Meter, OfflineAdapter
from app.domain.models import Claim, SupportLevel
from app.domain.rules import Checklist, ChecklistEngine, Outcome, parse_money
from app.pipeline.extract import classify, detect_conflicts, extract_claims, screen_document
from app.pipeline.ingest import load_pile
from tests.conftest import screen_claims


def run_pile(pile_dir: Path, pile_id: str, checklist_path: Path,
             watchlist_path: Path | None = None):
    meter = Meter(OfflineAdapter(), max_calls=200)
    documents, claims = [], []
    for document, chunks in load_pile(pile_dir, pile_id):
        document = screen_document(document)
        document = document.model_copy(update={"kind": classify(document, meter)})
        documents.append(document)
        claims.extend(extract_claims(document, chunks, meter, pile_id))
    conflicts = detect_conflicts(claims, pile_id)
    alerts = screen_claims(claims, watchlist_path) if watchlist_path else None
    result = ChecklistEngine(
        claims, conflicts, documents, pile_id, alerts=alerts
    ).evaluate(Checklist.load(checklist_path))
    return result, claims, conflicts, documents, meter


def test_clean_pile_reports_nothing(northwind, checklist_path, watchlist_path):
    """The rarest output in this industry, and the one worth testing hardest.

    Screened against the real OFAC extract, not an assumed-empty alert list —
    otherwise this asserts a clean result rather than demonstrating one.
    """
    result, *_ = run_pile(northwind, "northwind", checklist_path, watchlist_path)
    assert result.findings == []
    assert result.not_evaluated == [], (
        "a clean result must mean every rule actually ran, not that some were "
        f"skipped: {[r.rule_id for r in result.not_evaluated]}"
    )
    assert "no findings" in result.summary()
    assert "every rule ran" in result.summary().lower()


def test_defective_pile_reports_exactly_the_planted_defects(meridian, checklist_path):
    result, *_ = run_pile(meridian, "meridian", checklist_path)
    assert {f.rule_id for f in result.findings} == {
        "OWN-002",  # ownership contradicted across sources
        "SCR-002",  # screening alert left open
        "COM-001",  # invoice exceeds the contract it bills against
        "INT-001",  # a document tried to give instructions
    }


def test_every_finding_can_be_traced(meridian, checklist_path):
    """A finding a reviewer cannot check is an assertion, not a finding."""
    # One run only. Documents get fresh ids per run, so citations from one run
    # can never be checked against documents from another.
    result, _, _, docs, _ = run_pile(meridian, "meridian", checklist_path)
    documents = {d.id: d for d in docs}
    for finding in result.findings:
        # INT-001 is about a document as a whole, so it cites no span.
        if finding.rule_id == "INT-001":
            continue
        assert finding.citations, f"{finding.rule_id} carries no citation"
        for citation in finding.citations:
            document = documents.get(citation.document_id)
            assert document is not None
            assert citation.verify(document.text), f"{finding.rule_id} cites a bad span"


def test_conflict_is_surfaced_not_resolved(meridian, checklist_path):
    _, _, conflicts, _, _ = run_pile(meridian, "meridian", checklist_path)
    assert len(conflicts) == 1
    conflict = conflicts[0]
    assert conflict.attribute == "ubo_percentage"
    assert not conflict.is_resolved, "the system must not pick a winner by itself"
    assert {c.value for c in conflict.claims} == {"62%", "41%"}


def test_a_rule_without_evidence_is_not_evaluated_rather_than_passed():
    """Silence is not compliance."""
    checklist = Checklist.load(Path("checklists/vendor_onboarding_v1.yaml"))
    # No claims at all: every value_in / comparison rule lacks its evidence.
    result = ChecklistEngine([], [], [], "empty").evaluate(checklist)
    outcomes = {r.rule_id: r.outcome for stage in result.stages for r in stage.results}

    assert outcomes["SCR-002"] is Outcome.NOT_EVALUATED
    assert outcomes["COM-001"] is Outcome.NOT_EVALUATED
    assert Outcome.PASSED not in {
        outcomes[r] for r in ("SCR-002", "SCR-003", "COM-001")
    }


def test_zero_findings_with_an_unevaluated_rule_is_not_called_clean():
    """The dangerous case: nothing failed, but something never ran.

    Reported separately from a genuine pass, because a reviewer skimming for
    "no findings" would otherwise read the two identically.
    """
    from app.domain.rules import ChecklistResult, RuleResult, StageResult

    result = ChecklistResult(
        checklist_id="x", name="Checklist",
        stages=[StageResult(stage_id="s", name="Stage", results=[
            RuleResult("R-001", "passed rule", Outcome.PASSED),
            RuleResult("R-002", "could not run", Outcome.NOT_EVALUATED,
                       note="no supported claim"),
        ])],
    )
    summary = result.summary()
    assert result.findings == []
    assert "not a clean result" in summary
    assert "could not be evaluated" in summary

    genuinely_clean = ChecklistResult(
        checklist_id="x", name="Checklist",
        stages=[StageResult(stage_id="s", name="Stage", results=[
            RuleResult("R-001", "passed rule", Outcome.PASSED),
        ])],
    )
    assert "every rule ran and every rule passed" in genuinely_clean.summary().lower()


def test_unsupported_claims_do_not_satisfy_a_required_rule():
    """An UNSUPPORTED claim is the system saying it could not find something."""
    checklist = Checklist.load(Path("checklists/vendor_onboarding_v1.yaml"))
    claims = [
        Claim(pile_id="p", attribute="registered_name", value=None,
              support=SupportLevel.UNSUPPORTED)
    ]
    result = ChecklistEngine(claims, [], [], "p").evaluate(checklist)
    failed = {f.rule_id for f in result.findings}
    assert "IDN-001" in failed


def test_cross_currency_comparison_refuses_to_guess():
    """Comparing USD against EUR without a rate would be inventing a fact."""
    checklist = Checklist.load(Path("checklists/vendor_onboarding_v1.yaml"))
    cite = {"support": SupportLevel.SUPPORTED}
    from app.domain.models import Citation
    from uuid import uuid4

    def claim(attribute, value):
        doc, chunk = uuid4(), uuid4()
        return Claim(
            pile_id="p", attribute=attribute, value=value,
            citations=[Citation(document_id=doc, chunk_id=chunk,
                                char_start=0, char_end=len(value), quote=value)],
            **cite,
        )

    result = ChecklistEngine(
        [claim("invoice_total", "USD 90,000.00"), claim("contract_value", "EUR 10,000.00")],
        [], [], "p",
    ).evaluate(checklist)
    com001 = next(
        r for stage in result.stages for r in stage.results if r.rule_id == "COM-001"
    )
    assert com001.outcome is Outcome.NOT_EVALUATED
    assert "currencies differ" in com001.note


def test_com001_finding_cites_both_the_invoice_and_the_contract():
    """The one rule that compares two documents must cite both of them.

    _finding() only ever attached the left (invoice) claim's citations, so the
    contract's own citation for contract_value never reached the Finding — a
    reviewer could open the invoice this finding is about but not the contract.
    """
    checklist = Checklist.load(Path("checklists/vendor_onboarding_v1.yaml"))
    from app.domain.models import Citation
    from uuid import uuid4

    def claim(attribute, value):
        doc, chunk = uuid4(), uuid4()
        return Claim(
            pile_id="p", attribute=attribute, value=value, support=SupportLevel.SUPPORTED,
            citations=[Citation(document_id=doc, chunk_id=chunk,
                                char_start=0, char_end=len(value), quote=value)],
        )

    invoice = claim("invoice_total", "USD 142,000.00")
    contract = claim("contract_value", "USD 96,000.00")

    result = ChecklistEngine([invoice, contract], [], [], "p").evaluate(checklist)
    com001 = next(f for f in result.findings if f.rule_id == "COM-001")

    cited_documents = {c.document_id for c in com001.citations}
    assert invoice.citations[0].document_id in cited_documents
    assert contract.citations[0].document_id in cited_documents, (
        "the contract's own citation must reach the finding, not just the invoice's"
    )


def test_com001_finding_carries_the_computed_difference():
    """A reviewer should not have to subtract two numbers out of a sentence.
    Same fixture values as the brief's own worked example (142,000 vs 96,000 ->
    a 46,000 difference), just in USD rather than EUR — the arithmetic doesn't
    care which currency, and this repo's fixtures are already USD-denominated.
    """
    checklist = Checklist.load(Path("checklists/vendor_onboarding_v1.yaml"))
    from app.domain.models import Citation
    from uuid import uuid4

    def claim(attribute, value):
        doc, chunk = uuid4(), uuid4()
        return Claim(
            pile_id="p", attribute=attribute, value=value, support=SupportLevel.SUPPORTED,
            citations=[Citation(document_id=doc, chunk_id=chunk,
                                char_start=0, char_end=len(value), quote=value)],
        )

    invoice = claim("invoice_total", "USD 142,000.00")
    contract = claim("contract_value", "USD 96,000.00")
    result = ChecklistEngine([invoice, contract], [], [], "p").evaluate(checklist)
    com001 = next(f for f in result.findings if f.rule_id == "COM-001")

    assert com001.compared == {
        "left_attribute": "invoice_total", "left_value": "USD 142,000.00",
        "right_attribute": "contract_value", "right_value": "USD 96,000.00",
        "difference": "USD 46,000.00",
    }


def test_a_finding_without_two_compared_values_has_none_not_a_fake_one():
    """required_claim and value_in findings have nothing to compare -- inventing
    an empty {} here would be exactly the kind of bluff this field exists to
    avoid on the one rule that genuinely has two values."""
    checklist = Checklist.load(Path("checklists/vendor_onboarding_v1.yaml"))
    result = ChecklistEngine([], [], [], "empty").evaluate(checklist)
    idn001 = next(f for f in result.findings if f.rule_id == "IDN-001")
    assert idn001.compared is None


@pytest.mark.parametrize("raw,expected", [
    ("USD 48,000.00", ("USD", 48000.0)),
    ("$1,250", (None, 1250.0)),
    ("EUR 10,000.00", ("EUR", 10000.0)),
    ("not an amount", None),
])
def test_money_parsing(raw, expected):
    assert parse_money(raw) == expected
