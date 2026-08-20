"""The checklist engine.

The engine understands rule *types*. It knows nothing about vendor onboarding,
sanctions, or invoices — that knowledge lives entirely in the YAML checklist.
Adding a rule, a threshold, a jurisdiction or a client is a data change; only
adding a genuinely new *kind* of check touches this file. That boundary is the
point.

Two behaviours matter as much as the checks themselves:

* **An honest empty result.** A clean pile must produce zero findings, not a
  reassuring-sounding near-miss. A checker that always finds something is not
  a checker.
* **A rule that cannot be evaluated says so.** If the data needed for a check
  is absent, that is reported as `NOT_EVALUATED` rather than silently passing.
  A check that quietly passes when it could not run is worse than no check.
"""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from app.domain.models import Claim, Conflict, Document, DocumentStatus, Finding, Severity, SupportLevel


class Outcome(str, enum.Enum):
    PASSED = "passed"
    FAILED = "failed"
    # The check could not run because the data it needs is not there. Kept
    # distinct from PASSED on purpose: silence is not evidence of compliance.
    NOT_EVALUATED = "not_evaluated"


@dataclass
class RuleResult:
    rule_id: str
    title: str
    outcome: Outcome
    finding: Finding | None = None
    note: str = ""


@dataclass
class StageResult:
    stage_id: str
    name: str
    results: list[RuleResult] = field(default_factory=list)

    @property
    def findings(self) -> list[Finding]:
        return [r.finding for r in self.results if r.finding is not None]

    @property
    def passed(self) -> bool:
        return all(r.outcome is not Outcome.FAILED for r in self.results)


@dataclass
class ChecklistResult:
    checklist_id: str
    name: str
    stages: list[StageResult] = field(default_factory=list)

    @property
    def findings(self) -> list[Finding]:
        return [f for stage in self.stages for f in stage.findings]

    @property
    def not_evaluated(self) -> list[RuleResult]:
        return [
            r for stage in self.stages for r in stage.results
            if r.outcome is Outcome.NOT_EVALUATED
        ]

    def summary(self) -> str:
        """What a reviewer is told. Never overstates."""
        n = len(self.findings)
        blocked = len(self.not_evaluated)
        if n == 0 and blocked == 0:
            return (
                f"{self.name}: no findings. Every rule ran and every rule passed."
            )
        if n == 0:
            return (
                f"{self.name}: no findings, but {blocked} rule(s) could not be "
                "evaluated because the required evidence is absent. This is not "
                "a clean result."
            )
        return (
            f"{self.name}: {n} finding(s)"
            + (f", {blocked} rule(s) not evaluated" if blocked else "")
            + "."
        )


# --------------------------------------------------------------------------
# value parsing
# --------------------------------------------------------------------------

_MONEY = re.compile(r"(?:(USD|EUR|GBP|INR)|[$€£₹])?\s*([\d,]+(?:\.\d{1,2})?)", re.I)

_DATE_FORMATS = ["%Y-%m-%d", "%d %B %Y", "%d %b %Y", "%B %d, %Y"]


def parse_money(raw: str) -> tuple[str | None, float] | None:
    """Return (currency, amount). Currency may be None if not stated.

    Returns None when the string does not contain a parseable amount, so a
    comparison rule can report NOT_EVALUATED rather than comparing garbage.
    """
    match = _MONEY.search(raw or "")
    if not match:
        return None
    currency, amount = match.group(1), match.group(2)
    try:
        return (currency.upper() if currency else None, float(amount.replace(",", "")))
    except ValueError:
        return None


def parse_date(raw: str) -> date | None:
    raw = (raw or "").strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


# --------------------------------------------------------------------------
# engine
# --------------------------------------------------------------------------


@dataclass
class Checklist:
    id: str
    name: str
    description: str
    stages: list[dict[str, Any]]

    @classmethod
    def load(cls, path: Path) -> Checklist:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        missing = {"id", "name", "stages"} - set(raw)
        if missing:
            raise ValueError(f"{path.name}: checklist is missing {sorted(missing)}")
        return cls(
            id=raw["id"],
            name=raw["name"],
            description=raw.get("description", ""),
            stages=raw["stages"],
        )


class ChecklistEngine:
    """Evaluates a checklist against a pile's claims, conflicts and documents."""

    def __init__(
        self,
        claims: list[Claim],
        conflicts: list[Conflict],
        documents: list[Document],
        pile_id: str,
        today: date | None = None,
        alerts: list[dict] | None = None,
    ) -> None:
        self._pile_id = pile_id
        self._conflicts = conflicts
        self._documents = documents
        # None means screening did not run, which is not the same as running and
        # finding nothing. The distinction decides between NOT_EVALUATED and
        # PASSED, and collapsing it would let an unscreened pile read as clear.
        self._alerts = alerts
        self._today = today or datetime.now(timezone.utc).date()
        # Only supported claims count as evidence. An UNSUPPORTED claim is the
        # system saying "I could not find this", which must not satisfy a
        # required_claim rule.
        self._claims: dict[str, list[Claim]] = {}
        for claim in claims:
            if claim.support is not SupportLevel.UNSUPPORTED:
                self._claims.setdefault(claim.attribute, []).append(claim)

    # -- helpers ---------------------------------------------------------

    def _first(self, attribute: str) -> Claim | None:
        found = self._claims.get(attribute)
        return found[0] if found else None

    def _finding(
        self, rule: dict[str, Any], detail: str, claim: Claim | None,
        compared: dict | None = None,
    ) -> Finding:
        return Finding(
            pile_id=self._pile_id,
            rule_id=rule["id"],
            title=rule["title"],
            detail=detail,
            severity=Severity(rule.get("severity", "medium")),
            citations=list(claim.citations) if claim else [],
            compared=compared,
        )

    # -- rule types ------------------------------------------------------

    def _required_claim(self, rule: dict[str, Any]) -> RuleResult:
        attribute = rule["attribute"]
        claim = self._first(attribute)
        if claim and claim.value:
            return RuleResult(rule["id"], rule["title"], Outcome.PASSED)
        return RuleResult(
            rule["id"], rule["title"], Outcome.FAILED,
            self._finding(
                rule,
                f"No supported claim for {attribute!r} was found in the sources.",
                None,
            ),
        )

    def _value_in(self, rule: dict[str, Any]) -> RuleResult:
        attribute = rule["attribute"]
        allowed = {str(a).lower() for a in rule["allowed"]}
        claim = self._first(attribute)
        if claim is None or not claim.value:
            return RuleResult(
                rule["id"], rule["title"], Outcome.NOT_EVALUATED,
                note=f"no supported claim for {attribute!r}",
            )
        if claim.value.strip().lower() in allowed:
            return RuleResult(rule["id"], rule["title"], Outcome.PASSED)
        return RuleResult(
            rule["id"], rule["title"], Outcome.FAILED,
            self._finding(
                rule,
                f"{attribute} is {claim.value!r}; acceptable values are "
                f"{sorted(allowed)}.",
                claim,
            ),
        )

    def _date_within_days(self, rule: dict[str, Any]) -> RuleResult:
        attribute, limit = rule["attribute"], int(rule["days"])
        claim = self._first(attribute)
        if claim is None or not claim.value:
            return RuleResult(
                rule["id"], rule["title"], Outcome.NOT_EVALUATED,
                note=f"no supported claim for {attribute!r}",
            )
        parsed = parse_date(claim.value)
        if parsed is None:
            return RuleResult(
                rule["id"], rule["title"], Outcome.NOT_EVALUATED,
                note=f"could not parse {claim.value!r} as a date",
            )
        age = (self._today - parsed).days
        if age <= limit:
            return RuleResult(rule["id"], rule["title"], Outcome.PASSED)
        return RuleResult(
            rule["id"], rule["title"], Outcome.FAILED,
            self._finding(
                rule,
                f"{attribute} is {claim.value} — {age} days old, limit is {limit}.",
                claim,
            ),
        )

    def _numeric_not_greater_than(self, rule: dict[str, Any]) -> RuleResult:
        left_attr, right_attr = rule["attribute"], rule["compare_to"]
        left_claim, right_claim = self._first(left_attr), self._first(right_attr)
        if not left_claim or not right_claim:
            missing = left_attr if not left_claim else right_attr
            return RuleResult(
                rule["id"], rule["title"], Outcome.NOT_EVALUATED,
                note=f"no supported claim for {missing!r}",
            )
        left, right = parse_money(left_claim.value or ""), parse_money(right_claim.value or "")
        if left is None or right is None:
            return RuleResult(
                rule["id"], rule["title"], Outcome.NOT_EVALUATED,
                note="one side is not a parseable amount",
            )
        left_ccy, left_amt = left
        right_ccy, right_amt = right
        # Comparing across currencies without a rate would be inventing a fact.
        if left_ccy and right_ccy and left_ccy != right_ccy:
            return RuleResult(
                rule["id"], rule["title"], Outcome.NOT_EVALUATED,
                note=f"currencies differ ({left_ccy} vs {right_ccy}); no rate available",
            )
        if left_amt <= right_amt:
            return RuleResult(rule["id"], rule["title"], Outcome.PASSED)
        # The currency label for the difference: both sides already agree by
        # this point (the mismatch branch above returns first), so whichever
        # side actually stated one is correct to use for the delta too.
        ccy = left_ccy or right_ccy
        difference = f"{ccy + ' ' if ccy else ''}{left_amt - right_amt:,.2f}"
        finding = self._finding(
            rule,
            f"{left_attr} is {left_claim.value} which exceeds {right_attr} "
            f"of {right_claim.value}.",
            left_claim,
            compared={
                "left_attribute": left_attr, "left_value": left_claim.value,
                "right_attribute": right_attr, "right_value": right_claim.value,
                "difference": difference,
            },
        )
        # Cite both sides of the comparison, not just the left one — a
        # reviewer needs to open the contract as much as the invoice.
        finding.citations.extend(right_claim.citations)
        return RuleResult(rule["id"], rule["title"], Outcome.FAILED, finding)

    def _no_open_conflict(self, rule: dict[str, Any]) -> RuleResult:
        attribute = rule["attribute"]
        open_conflicts = [
            c for c in self._conflicts
            if c.attribute == attribute and not c.is_resolved
        ]
        if not open_conflicts:
            return RuleResult(rule["id"], rule["title"], Outcome.PASSED)
        conflict = open_conflicts[0]
        values = " vs ".join(repr(c.value) for c in conflict.claims)
        finding = self._finding(
            rule,
            f"Sources disagree on {attribute}: {values}. Awaiting a human "
            "decision; not resolved automatically.",
            None,
        )
        # Cite every side of the disagreement so the reviewer sees both.
        for claim in conflict.claims:
            finding.citations.extend(claim.citations)
        return RuleResult(rule["id"], rule["title"], Outcome.FAILED, finding)

    def _no_quarantined_documents(self, rule: dict[str, Any]) -> RuleResult:
        quarantined = [
            d for d in self._documents if d.status is DocumentStatus.QUARANTINED
        ]
        if not quarantined:
            return RuleResult(rule["id"], rule["title"], Outcome.PASSED)
        names = ", ".join(d.filename for d in quarantined)
        return RuleResult(
            rule["id"], rule["title"], Outcome.FAILED,
            self._finding(
                rule,
                f"{len(quarantined)} document(s) quarantined: {names}. "
                + (quarantined[0].quarantine_reason or ""),
                None,
            ),
        )

    def _no_unresolved_watchlist_alert(self, rule: dict[str, Any]) -> RuleResult:
        """Every raised alert must have been adjudicated, and none may be held.

        Three outcomes map onto three results:

          * no alert raised, or every alert discounted  -> PASSED
          * an alert confirmed or escalated             -> FAILED
          * an alert still pending                      -> NOT_EVALUATED

        Pending is deliberately not a failure. The reviewer has not looked yet,
        and reporting "screening failed" before anyone has looked would be as
        dishonest as reporting it passed. NOT_EVALUATED is the honest state, and
        it already prevents the checklist from claiming a clean bill of health.
        """
        if self._alerts is None:
            return RuleResult(
                rule["id"], rule["title"], Outcome.NOT_EVALUATED,
                note="no screening was performed, so no alert state exists",
            )

        pending = [a for a in self._alerts if a.get("decision_state", "pending")
                   == "pending"]
        held = [a for a in self._alerts
                if a.get("decision_state") in ("approved", "escalated")]

        if held:
            described = "; ".join(
                f"{a['subject_name']!r} vs watchlist entry {a['listed_uid']} "
                f"{a['listed_name']!r} ({a['listed_programme']}) — "
                f"{a['decision_state']}"
                for a in held
            )
            return RuleResult(
                rule["id"], rule["title"], Outcome.FAILED,
                self._finding(
                    rule,
                    f"{len(held)} watchlist alert(s) not cleared: {described}. "
                    "Onboarding is blocked.",
                    None,
                ),
            )

        if pending:
            return RuleResult(
                rule["id"], rule["title"], Outcome.NOT_EVALUATED,
                note=f"{len(pending)} watchlist alert(s) awaiting adjudication",
            )

        return RuleResult(rule["id"], rule["title"], Outcome.PASSED)

    # -- dispatch --------------------------------------------------------

    def evaluate(self, checklist: Checklist) -> ChecklistResult:
        handlers = {
            "required_claim": self._required_claim,
            "value_in": self._value_in,
            "date_within_days": self._date_within_days,
            "numeric_not_greater_than": self._numeric_not_greater_than,
            "no_open_conflict": self._no_open_conflict,
            "no_quarantined_documents": self._no_quarantined_documents,
            "no_unresolved_watchlist_alert": self._no_unresolved_watchlist_alert,
        }

        result = ChecklistResult(checklist_id=checklist.id, name=checklist.name)
        for stage in checklist.stages:
            stage_result = StageResult(stage_id=stage["id"], name=stage["name"])
            for rule in stage.get("rules", []):
                handler = handlers.get(rule["type"])
                if handler is None:
                    # An unknown rule type is a checklist authoring error. It
                    # must never be treated as a pass.
                    stage_result.results.append(
                        RuleResult(
                            rule["id"], rule["title"], Outcome.NOT_EVALUATED,
                            note=f"unknown rule type {rule['type']!r}",
                        )
                    )
                    continue
                stage_result.results.append(handler(rule))
            result.stages.append(stage_result)
        return result
