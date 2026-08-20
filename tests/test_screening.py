"""Watchlist screening, and the three answers an analyst can give.

Sanctions screening is the one place in this system where a false negative is
not a bug report, it is a regulatory breach. So these tests are written against
the two failure modes that actually cause breaches in practice:

  * an alert that never fires, because the name matcher was too strict;
  * an alert that fires, gets discounted on nothing, and reads as cleared.

They run against the watchlist this repository ships, which is fabricated —
every party, every passport number, every date of birth. The schema is modelled
on how real consolidated lists publish, so the matcher is exercised against the
shape real data arrives in rather than a convenient one.
"""

from __future__ import annotations

import pytest

from app.core.llm import Meter, OfflineAdapter
from app.domain.models import (
    Decision,
    DecisionKind,
    DecisionState,
    Register,
)
from app.domain.rules import Checklist, ChecklistEngine, Outcome
from app.domain.screening import (
    Adjudication,
    Comparison,
    DiscountStrength,
    PartyType,
    is_weak_alias,
    load_watchlist,
    name_score,
    screen,
    shares_distinctive_token,
)
from app.pipeline.extract import classify, detect_conflicts, extract_claims, screen_document
from app.pipeline.graph import _alert_to_dict, build_register
from app.pipeline.ingest import load_pile
from app.pipeline.update import apply_arrival
from uuid import uuid4


@pytest.fixture(scope="module")
def watchlist(request):
    path = request.config.rootpath / "watchlists" / "synthetic_consolidated.json"
    entries, provenance = load_watchlist(path)
    return entries, provenance


def listing(watchlist, name: str):
    entries, _ = watchlist
    return next(e for e in entries if e.name == name)


# --------------------------------------------------------------------------
# the list itself
# --------------------------------------------------------------------------


def test_the_bundled_watchlist_is_synthetic_and_says_so(watchlist):
    """A screening system whose list has no provenance is unauditable — and one
    that ships fabricated data has to say that plainly rather than let a
    reviewer assume it is real.

    The brief asks for invented data. `scripts/curate_watchlist.py` builds the
    same schema from the genuine OFAC SDN file for anyone who wants it; that
    path is opt-in and nothing in the default configuration touches it.
    """
    entries, provenance = watchlist
    assert "make_watchlist.py" in provenance["source"]
    assert "fictional" in provenance["note"].lower()
    assert "resemblance to a real listed party is accidental" in provenance["note"]
    assert 20 <= len(entries) <= 30
    assert any(e.party_type is PartyType.INDIVIDUAL for e in entries), (
        "UBO screening needs listed natural persons, not only entities"
    )
    assert any(e.aliases for e in entries), "alias handling needs listings with aliases"


# --------------------------------------------------------------------------
# name matching
# --------------------------------------------------------------------------


def test_score_denominator_is_the_longer_name():
    """A one-token listing must not score 100% against anything containing it.

    With `min()` as the denominator, 'PIONEER LOGISTICS' scored a perfect match
    against every company with those two words anywhere in a longer name, which
    is how a screening engine ends up escalating its entire vendor book.
    """
    assert name_score("Pioneer Logistics", "Pioneer Logistics") == 1.0
    partial = name_score("Meridian Componentes Holdings Ltd",
                         "Meridian Research and Production Kombinat OJSC")
    assert 0.0 < partial < 1.0
    assert partial == pytest.approx(0.5, abs=0.2)


def test_legal_form_is_stripped_before_measuring_alias_strength():
    """'PML CO LTD' is a two-character name wearing a suit.

    Measuring length before stripping the legal form made it look distinctive,
    and a weak alias treated as strong is a false positive nobody discounts.
    """
    assert is_weak_alias("PML CO LTD")
    assert is_weak_alias("PML")
    assert not is_weak_alias("Nordstrand Maritime and Trading Company")


def test_a_shared_distinctive_token_still_raises():
    """Recall rule: below-threshold scores must not silently drop a real hit."""
    assert shares_distinctive_token(
        "Meridian Componentes Holdings Ltd", "MERIDIAN RESEARCH AND PRODUCTION KOMBINAT OJSC"
    )
    # 'holdings', 'ltd' and 'company' are generic and must not carry a match on
    # their own, or every company in the corpus alerts against every listing.
    assert not shares_distinctive_token("Acme Holdings Ltd", "Zenith Holdings Ltd")


# --------------------------------------------------------------------------
# the three outcomes
# --------------------------------------------------------------------------


def test_true_match_requires_a_corroborating_identifier(watchlist):
    """Name alone must never reach TRUE_MATCH.

    An earlier version required name plus *any* matching identifier, and party
    type matched almost always, so TRUE_MATCH was reachable on a name and the
    fact that both sides were companies. That is not corroboration.
    """
    entries, _ = watchlist
    alerts = screen("Pioneer Logistics", PartyType.ENTITY, entries,
                    subject_country="Iran")
    hit = next(a for a in alerts if a.listed.name == "PIONEER LOGISTICS")
    verdict, reason = hit.recommendation()
    assert verdict is Adjudication.TRUE_MATCH, reason

    # Same name, no corroborating country: cannot be called a true match.
    weaker = screen("Pioneer Logistics", PartyType.ENTITY, entries,
                    subject_country="United Kingdom")
    hit2 = next(a for a in weaker if a.listed.name == "PIONEER LOGISTICS")
    assert hit2.recommendation()[0] is not Adjudication.TRUE_MATCH


def test_party_type_mismatch_discounts_to_false_positive(watchlist):
    """An individual cannot be the company on the list."""
    entries, _ = watchlist
    alerts = screen("Pioneer Logistics", PartyType.INDIVIDUAL, entries,
                    subject_role="ultimate beneficial owner")
    hit = next(a for a in alerts if a.listed.name == "PIONEER LOGISTICS")
    verdict, reason = hit.recommendation()
    assert verdict is Adjudication.FALSE_POSITIVE
    assert "type" in reason.lower()


def test_unavailable_evidence_escalates_rather_than_clears(watchlist):
    """The whole point. Absent evidence is not evidence of difference.

    A partial name match with nothing available to discount it on must reach a
    human, not be quietly filed as a false positive because the discounting
    fields happened to be empty.
    """
    entries, _ = watchlist
    alerts = screen("Meridian Componentes Holdings Ltd", PartyType.ENTITY, entries,
                    subject_country="Malta", subject_incorporated="14 March 2019")
    hit = next(a for a in alerts
               if a.listed.name == "MERIDIAN RESEARCH AND PRODUCTION KOMBINAT OJSC")
    verdict, reason = hit.recommendation()
    assert verdict is Adjudication.ESCALATE_AOC, reason

    incorporation = next(i for i in hit.identifiers
                         if i.name == "date of incorporation")
    assert incorporation.comparison is Comparison.UNAVAILABLE
    assert incorporation.strength is DiscountStrength.NONE, (
        "an absent field must carry no discounting weight"
    )


def test_the_engine_never_clears_an_alert_itself(watchlist):
    """`recommendation` is advisory. Nothing in screening settles anything."""
    entries, _ = watchlist
    alerts = screen("Pioneer Logistics", PartyType.ENTITY, entries,
                    subject_country="Iran")
    assert alerts, "expected at least one alert to reason about"
    for alert in alerts:
        assert not hasattr(alert, "adjudication"), (
            "an alert must not carry its own verdict; the Decision holds it"
        )
        verdict, reason = alert.recommendation()
        assert reason, "a recommendation without a reason is not reviewable"


def test_an_identity_document_is_what_turns_an_escalation_into_a_discount(watchlist):
    """The counterfactual the whole screening design turns on.

    Same subject, same listing, same 100% name match. Without an identity
    document there is nothing to discount it on, so it escalates and holds the
    vendor. With one, a date of birth and a document number that both differ
    are strong discounting evidence under the Wolfsberg criteria, and it
    becomes a defensible false positive.

    This is also the argument against face matching: what changed the outcome
    was the fields printed on the document, not the photograph.
    """
    entries, _ = watchlist
    subject = "Anselm Rhodes Vanterpool"   # a real listed name; this person is fictional

    def verdict(**identity):
        alerts = screen(subject, PartyType.INDIVIDUAL, entries,
                        subject_role="ultimate beneficial owner", **identity)
        hit = next(a for a in alerts if a.listed.name == "VANTERPOOL, Anselm Rhodes")
        assert hit.score == 1.0, "precondition: the names must match exactly"
        return hit.recommendation()

    without, reason = verdict()
    assert without is Adjudication.ESCALATE_AOC, reason
    assert "not available" in reason

    with_id, reason = verdict(subject_dob="14 August 1986",
                              subject_nationality="United Kingdom",
                              subject_document_number="548812093")
    assert with_id is Adjudication.FALSE_POSITIVE, reason
    assert "date of birth" in reason


def test_identifiers_are_chosen_by_party_type(watchlist):
    """Comparing a company's incorporation date against a person's date of
    birth is nonsense, and padding an alert with fields that can never apply
    makes it look better evidenced than it is."""
    entries, _ = watchlist
    person = screen("Anselm Rhodes Vanterpool", PartyType.INDIVIDUAL, entries)[0]
    company = screen("Pioneer Logistics", PartyType.ENTITY, entries)[0]

    assert {i.name for i in person.identifiers} == {
        "party type", "date of birth", "nationality", "identity document number"
    }
    assert {i.name for i in company.identifiers} == {
        "party type", "jurisdiction", "date of incorporation"
    }


def test_secondary_identifiers_were_parsed_out_of_the_remarks(watchlist):
    """Consolidated lists publish date of birth and passport numbers only as
    free text inside one remarks field. Left unparsed, every listed individual
    would screen on name alone."""
    listed = listing(watchlist, "VANTERPOOL, Anselm Rhodes")
    assert listed.date_of_birth == "19 Jun 1951"
    assert listed.place_of_birth == "Road Town"
    assert listed.nationality == "Antigua"
    # Both passports, including the "alt. Passport" continuation.
    assert listed.document_numbers == ["1084010", "19820215"]

    # Absent fields stay empty rather than being guessed. An invented
    # nationality or document number would discount an alert on evidence that
    # does not exist, which is the precise failure this system exists to
    # prevent — so the parser has to leave a gap where the list leaves one.
    sparse = listing(watchlist, "OKONKWO, Emeka Chidubem")
    assert sparse.document_numbers == [], "no passport is published for this entry"
    partial = listing(watchlist, "HALVORSEN, Bjorn Aksel")
    assert partial.nationality == "", "no nationality is published for this entry"
    assert partial.document_numbers == ["N4471209"]


def test_a_clean_vendor_raises_nothing(watchlist, northwind):
    """The control. Screened against the real list, hits nothing."""
    entries, _ = watchlist
    assert screen("Northwind Fulfilment Services Ltd", PartyType.ENTITY, entries) == []
    assert screen("Priya Raghunathan", PartyType.INDIVIDUAL, entries) == []


# --------------------------------------------------------------------------
# the gate
# --------------------------------------------------------------------------


def alert_decision(**kwargs) -> Decision:
    return Decision(
        pile_id="meridian", run_id=uuid4(), kind=DecisionKind.ALERT,
        subject_id=uuid4(), summary="alert", **kwargs,
    )


def test_only_an_alert_can_be_escalated():
    """Escalation is a sanctions concept. A missing invoice is not escalated,
    it is approved or rejected, and allowing a third state everywhere would
    make 'escalated' mean nothing in particular."""
    alert = alert_decision()
    assert alert.settle(DecisionState.ESCALATED).state is DecisionState.ESCALATED

    finding = Decision(
        pile_id="meridian", run_id=uuid4(), kind=DecisionKind.FINDING,
        subject_id=uuid4(), summary="finding",
    )
    with pytest.raises(ValueError, match="only a watchlist alert"):
        finding.settle(DecisionState.ESCALATED)


def test_only_rejecting_an_alert_clears_the_vendor():
    """Exactly one disposition clears a watchlist alert.

    An earlier version of this test asserted that a PENDING alert does *not*
    block — it encoded the bug rather than catching it. Pending means nobody
    has looked yet, and a vendor nobody has screened-and-cleared cannot report
    the same onboarding state as one a reviewer discounted on evidence.
    """
    assert alert_decision().blocks_onboarding, "unreviewed is not cleared"
    assert alert_decision().settle(DecisionState.APPROVED).blocks_onboarding, (
        "an approved alert is a confirmed true match"
    )
    assert alert_decision().settle(DecisionState.ESCALATED).blocks_onboarding
    assert not alert_decision().settle(DecisionState.REJECTED).blocks_onboarding

    # And the property is about alerts. A pending finding is not a sanctions
    # hold; it is gated by the commit barrier instead.
    finding = Decision(
        pile_id="meridian", run_id=uuid4(), kind=DecisionKind.FINDING,
        subject_id=uuid4(), summary="finding",
    )
    assert not finding.blocks_onboarding


# --------------------------------------------------------------------------
# the checklist rule
# --------------------------------------------------------------------------


def evaluate(alerts, checklist_path):
    return ChecklistEngine([], [], [], "x", alerts=alerts).evaluate(
        Checklist.load(checklist_path)
    )


def scr004(result):
    return next(r for stage in result.stages for r in stage.results
                if r.rule_id == "SCR-004")


def test_scr004_distinguishes_unscreened_from_clean(checklist_path):
    """`None` and `[]` mean opposite things and must not collapse.

    None: screening never ran, so nothing is known.
    []:   screening ran and hit nothing, which is a genuine pass.
    """
    assert scr004(evaluate(None, checklist_path)).outcome is Outcome.NOT_EVALUATED
    assert scr004(evaluate([], checklist_path)).outcome is Outcome.PASSED


@pytest.mark.parametrize("state,expected", [
    ("pending", Outcome.NOT_EVALUATED),   # nobody has looked yet
    ("rejected", Outcome.PASSED),         # discounted as a false positive
    ("approved", Outcome.FAILED),         # confirmed true match
    ("escalated", Outcome.FAILED),        # could not be cleared
])
def test_scr004_maps_each_adjudication(state, expected, checklist_path):
    alert = {
        "subject_name": "Meridian Componentes Holdings Ltd",
        "listed_uid": "48555",
        "listed_name": "MERIDIAN RESEARCH AND PRODUCTION KOMBINAT OJSC",
        "listed_programme": "RUSSIA-EO14024",
        "decision_state": state,
    }
    assert scr004(evaluate([alert], checklist_path)).outcome is expected


# --------------------------------------------------------------------------
# the register
# --------------------------------------------------------------------------


@pytest.fixture
def meridian_pile(meridian):
    meter = Meter(OfflineAdapter(), max_calls=200)
    documents, claims = [], []
    for document, chunks in load_pile(meridian, "meridian"):
        document = screen_document(document)
        document = document.model_copy(update={"kind": classify(document, meter)})
        documents.append(document)
        claims.extend(extract_claims(document, chunks, meter, "meridian"))
    return claims, detect_conflicts(claims, "meridian")


def meridian_alerts(watchlist):
    entries, _ = watchlist
    return [_alert_to_dict(a) for a in screen(
        "Meridian Componentes Holdings Ltd", PartyType.ENTITY, entries,
        subject_country="Malta", subject_incorporated="14 March 2019",
    )]


def test_an_unscreened_register_does_not_claim_a_clean_screening(meridian_pile):
    """Passing no alerts must not print 'none raised'."""
    claims, conflicts = meridian_pile
    register = build_register("meridian", claims, conflicts, alerts=None)
    assert "watchlist" not in register.section("screening").body


def test_the_register_states_the_alert_and_the_block(meridian_pile, watchlist):
    claims, conflicts = meridian_pile
    alerts = meridian_alerts(watchlist)
    assert alerts

    pending = build_register("meridian", claims, conflicts, alerts)
    assert "UNRESOLVED" in pending.section("screening").body

    settled = [{**a, "decision_state": "escalated",
                "decision_note": "no DOI on the listing"} for a in alerts]
    blocked = build_register("meridian", claims, conflicts, settled)
    body = blocked.section("screening").body
    assert "ONBOARDING BLOCKED" in body
    assert "no DOI on the listing" in body, "the reviewer's reason belongs in the record"

    # Discounted alerts stay in the register. Deleting a cleared alert would
    # hide that the vendor was ever hit, which is exactly the history an
    # examiner asks for.
    cleared = [{**a, "decision_state": "rejected"} for a in alerts]
    assert "FALSE POSITIVE" in build_register(
        "meridian", claims, conflicts, cleared
    ).section("screening").body


def test_alerts_live_only_in_the_screening_section(meridian_pile, watchlist):
    """Otherwise a focused update to any other section would have to know
    about screening, and the byte-identical proof would stop meaning anything."""
    claims, conflicts = meridian_pile
    alerts = meridian_alerts(watchlist)
    without = build_register("meridian", claims, conflicts, alerts=[])
    with_alerts = build_register("meridian", claims, conflicts, alerts)

    moved = [sid for sid, h in without.hashes().items()
             if with_alerts.hashes()[sid] != h]
    assert moved == ["screening"]


def test_an_arrival_does_not_erase_a_live_alert(meridian_pile, watchlist, arrivals):
    """Regression. `apply_arrival` rebuilds affected sections, and the
    re-screening arrival affects `screening`. Rebuilding it without carrying the
    alerts through deleted a live sanctions alert from the register — silently,
    and through the back door of an unrelated document landing in a folder.
    """
    claims, conflicts = meridian_pile
    alerts = [{**a, "decision_state": "escalated"} for a in meridian_alerts(watchlist)]
    register = build_register("meridian", claims, conflicts, alerts)

    result = apply_arrival(
        arrivals / "meridian_09_rescreen.txt", register, claims, conflicts,
        OfflineAdapter(), "meridian", alerts=alerts,
    )
    assert result.sections_rebuilt == ["screening"], "precondition for this test"

    body = result.register.section("screening").body
    assert "ONBOARDING BLOCKED" in body, "the arrival erased a live watchlist alert"
    assert "DISPUTED" in body, "and it should still surface the new contradiction"
