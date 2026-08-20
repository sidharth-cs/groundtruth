"""Watchlist screening and alert adjudication.

This models the work an investigation specialist actually does: a screening
engine raises alerts, and a human decides each one. The engine's job is to
present evidence well, not to decide.

The design follows published industry guidance rather than any employer's
internal procedure:

  * Wolfsberg Group, *Sanctions Screening Guidance* — secondary identifiers
    (date of birth, place of birth, nationality) as the means of distinguishing
    a true match from a false one; address attributes as identifying
    information; and weak aliases (short strings, digits, common nicknames,
    geographic references) as a known source of false positives.
  * FFIEC BSA/AML Examination Manual, OFAC section — tiered review, with
    tier-one adjudication escalating to tier-two.
  * OFAC, *A Framework for Compliance Commitments* — a risk-based programme
    escalates and holds rather than clearing what it cannot resolve.

Three outcomes, not two. An analyst who cannot clear an alert must be able to
say so: `ESCALATE_AOC` records "I could not discount this on the evidence
available" and **blocks onboarding**, rather than letting an unresolved
sanctions alert sit inside a register that otherwise reads clean. That is the
same principle as `NOT_EVALUATED` in the checklist engine — silence is not
compliance.

The engine never auto-clears. Every alert reaches a human with a score, a
per-identifier comparison, and a recommendation it is free to ignore.
"""

from __future__ import annotations

import enum
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID, uuid4


class PartyType(str, enum.Enum):
    INDIVIDUAL = "individual"
    ENTITY = "entity"
    UNKNOWN = "unknown"


class Comparison(str, enum.Enum):
    """The result of comparing one identifier between the two parties."""

    MATCH = "match"
    MISMATCH = "mismatch"
    # The subject or the listing simply does not carry this attribute. Kept
    # distinct from MISMATCH: absent evidence is not evidence of difference,
    # and treating it as such is how real alerts get wrongly discounted.
    UNAVAILABLE = "unavailable"


class DiscountStrength(str, enum.Enum):
    """How much weight a mismatch carries when discounting an alert.

    Wolfsberg separates strong secondary identifiers (date of birth, date of
    incorporation) from weaker situational factors (profession, residence).
    """

    STRONG = "strong"
    WEAK = "weak"
    NONE = "none"


class Adjudication(str, enum.Enum):
    PENDING = "pending"
    TRUE_MATCH = "true_match"
    FALSE_POSITIVE = "false_positive"
    # Could not be discounted on available evidence. Blocks onboarding.
    ESCALATE_AOC = "escalate_aoc"


# Legal-form suffixes carry no identifying information and wreck naive
# similarity: "Meridian Ltd" and "Meridian LLC" are the same name for
# screening purposes.
_LEGAL_FORMS = {
    "ltd", "limited", "llc", "llp", "plc", "inc", "incorporated", "corp",
    "corporation", "co", "company", "gmbh", "sa", "sas", "srl", "spa", "bv",
    "nv", "ab", "as", "oy", "pte", "pty", "jsc", "ojsc", "cjsc", "zao", "ooo",
    "sarl", "kg", "ag", "de", "cv", "lda", "sdn", "bhd", "fze", "fzc", "dmcc",
}

# Words so common in company names that sharing one is not evidence of
# anything. Matching on these alone is the classic false-positive generator.
_GENERIC_TOKENS = {
    "international", "global", "group", "holdings", "holding", "trading",
    "trade", "services", "service", "solutions", "systems", "industries",
    "industrial", "general", "enterprises", "enterprise", "commercial",
    "national", "maritime", "shipping", "logistics", "freight", "cargo",
    "research", "production", "firm", "development", "technologies",
    "technology", "and", "of", "the", "for",
}


def normalise(name: str) -> str:
    text = (name or "").lower()
    # "SMITH, John" -> "john smith", the SDN convention for individuals
    if "," in text:
        head, _, tail = text.partition(",")
        text = f"{tail.strip()} {head.strip()}"
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    return " ".join(text.split())


def tokens(name: str) -> list[str]:
    return [t for t in normalise(name).split() if t not in _LEGAL_FORMS]


def distinctive_tokens(name: str) -> set[str]:
    """Tokens that actually identify. Everything else is noise."""
    return {t for t in tokens(name) if t not in _GENERIC_TOKENS and len(t) > 2}


def is_weak_alias(alias: str) -> bool:
    """Wolfsberg: short strings, digits, nicknames and geographic references
    generate false positives and are poor screening terms on their own.

    Length is measured *after* legal forms are stripped. "PML CO LTD" is ten
    characters but carries three of information, and treating it as a strong
    alias is how an abbreviation ends up matching half a vendor book.
    """
    if any(ch.isdigit() for ch in alias):
        return True
    meaningful = "".join(tokens(alias))
    if len(meaningful) <= 4:
        return True
    distinctive = distinctive_tokens(alias)
    if not distinctive:
        return True
    # Every identifying part is an initialism.
    if all(len(t) <= 3 for t in distinctive):
        return True
    return False


def shares_distinctive_token(subject: str, listed: str) -> bool:
    """Whether the two names share anything actually identifying.

    This is the recall rule: alert on any shared distinctive token. Missing an
    alert is a compliance failure, while raising one an analyst discounts in
    ten seconds is merely work.
    """
    return bool(distinctive_tokens(subject) & distinctive_tokens(listed))


def name_score(subject: str, listed: str) -> float:
    """Similarity over distinctive tokens, in 0..1.

    The denominator is the *larger* token set, which keeps the number honest.
    An earlier version divided by the smaller one, so a single-token listing
    like "MERIDIAN" scored 100% against "Meridian Componentes Holdings" —
    technically a full subset match, but presenting that to an analyst as a
    perfect hit overstates it badly. One shared token out of two is 50%, and
    that is what they should see.

    Deliberately not a character-level ratio: "Meridian Componentes Holdings"
    and "Meridian Research and Production Firm" look distant character-wise
    yet share the one token that matters.
    """
    a, b = distinctive_tokens(subject), distinctive_tokens(listed)
    if not a or not b:
        # Fall back to all tokens when a name is entirely generic, so a
        # generic-only collision still gets a score rather than vanishing.
        a, b = set(tokens(subject)), set(tokens(listed))
        if not a or not b:
            return 0.0
    return len(a & b) / max(len(a), len(b))


@dataclass
class Identifier:
    """One attribute compared between the subject and the listed party."""

    name: str
    subject_value: str | None
    listed_value: str | None
    comparison: Comparison
    strength: DiscountStrength
    note: str = ""


@dataclass
class ListedParty:
    uid: str
    name: str
    party_type: PartyType
    programme: str
    aliases: list[str] = field(default_factory=list)
    # Secondary identifiers, parsed out of OFAC's free-text remarks field by
    # scripts/curate_watchlist.py. Wolfsberg names exactly these as the means
    # of distinguishing a true match from a false one, and OFAC publishes them
    # only as prose, so they are structured at curation time rather than being
    # re-parsed on every comparison.
    date_of_birth: str = ""
    place_of_birth: str = ""
    nationality: str = ""
    document_numbers: list[str] = field(default_factory=list)
    remarks: str = ""
    title: str = ""


@dataclass
class Alert:
    """One subject matched against one listed party, awaiting adjudication."""

    id: UUID = field(default_factory=uuid4)
    subject_name: str = ""
    subject_type: PartyType = PartyType.UNKNOWN
    subject_role: str = ""          # "vendor", "ultimate beneficial owner", ...
    listed: ListedParty | None = None
    score: float = 0.0
    matched_on: str = ""            # primary name, or which alias fired
    via_weak_alias: bool = False
    identifiers: list[Identifier] = field(default_factory=list)

    # Deliberately no adjudication field. An alert's outcome lives on the
    # Decision that gates it and nowhere else. A copy here would be one restart
    # or one partial write away from disagreeing with the Decision, and a
    # sanctions record that disagrees with itself is worse than no record: both
    # halves look authoritative and neither is checkable.

    @property
    def strong_mismatches(self) -> list[Identifier]:
        return [
            i for i in self.identifiers
            if i.comparison is Comparison.MISMATCH
            and i.strength is DiscountStrength.STRONG
        ]

    @property
    def unavailable(self) -> list[Identifier]:
        return [i for i in self.identifiers if i.comparison is Comparison.UNAVAILABLE]

    def recommendation(self) -> tuple[Adjudication, str]:
        """What the engine would suggest — and why.

        Advisory only. Nothing in the pipeline acts on this; it exists so the
        human starts from evidence rather than a blank alert. The engine does
        not clear alerts, at any score.
        """
        if self.via_weak_alias and not self.strong_mismatches:
            return (
                Adjudication.FALSE_POSITIVE,
                f"Matched only via the weak alias {self.matched_on!r}. Weak "
                "aliases are a documented source of false positives and are "
                "poor evidence on their own.",
            )
        if self.strong_mismatches:
            which = ", ".join(i.name for i in self.strong_mismatches)
            return (
                Adjudication.FALSE_POSITIVE,
                f"Strong discounting factor(s) differ: {which}. "
                f"Name similarity {self.score:.0%} on distinctive tokens.",
            )
        # An exact name alone is not confirmation — two companies can share a
        # name — so a true match needs the name plus at least one corroborating
        # identifier. Party type is excluded from that count: nearly every
        # entry is an entity, so it agrees almost always and corroborates
        # nothing.
        corroborating = [
            i for i in self.identifiers
            if i.comparison is Comparison.MATCH and i.name != "party type"
        ]
        if self.score >= 0.99 and corroborating:
            which = ", ".join(i.name for i in corroborating)
            return (
                Adjudication.TRUE_MATCH,
                f"Names align on every distinctive token and {which} "
                f"corroborate{'s' if len(corroborating) == 1 else ''}. "
                "Nothing available discounts this.",
            )
        if self.unavailable:
            which = ", ".join(i.name for i in self.unavailable)
            return (
                Adjudication.ESCALATE_AOC,
                f"Cannot be discounted: {which} not available on one side, and "
                "no strong factor differs. Absent evidence is not evidence of "
                "difference. Escalate rather than clear.",
            )
        return (
            Adjudication.ESCALATE_AOC,
            f"Name similarity {self.score:.0%} with nothing to discount it on. "
            "Escalate.",
        )


def load_watchlist(path: Path) -> tuple[list[ListedParty], dict]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    parties = [
        ListedParty(
            uid=e["uid"],
            name=e["name"],
            party_type=PartyType(e.get("party_type", "entity")),
            programme=e.get("programme", ""),
            aliases=e.get("aliases", []),
            date_of_birth=e.get("date_of_birth", ""),
            place_of_birth=e.get("place_of_birth", ""),
            nationality=e.get("nationality", ""),
            document_numbers=e.get("document_numbers", []),
            remarks=e.get("remarks", ""),
            title=e.get("title", ""),
        )
        for e in raw["entries"]
    ]
    provenance = {k: v for k, v in raw.items() if k != "entries"}
    return parties, provenance


def _compare(name: str, subject: str | None, listed: str | None,
             strength: DiscountStrength, note: str = "") -> Identifier:
    if not subject or not listed:
        return Identifier(name, subject, listed, Comparison.UNAVAILABLE,
                          DiscountStrength.NONE,
                          note or "not stated on one side")
    same = normalise(subject) == normalise(listed)
    return Identifier(
        name, subject, listed,
        Comparison.MATCH if same else Comparison.MISMATCH,
        strength if not same else DiscountStrength.NONE,
        note,
    )


def screen(
    subject_name: str,
    subject_type: PartyType,
    watchlist: list[ListedParty],
    subject_role: str = "vendor",
    subject_country: str | None = None,
    subject_incorporated: str | None = None,
    subject_dob: str | None = None,
    subject_nationality: str | None = None,
    subject_document_number: str | None = None,
    threshold: float = 0.5,
) -> list[Alert]:
    """Raise an alert for every listed party the subject plausibly matches.

    Alerts are raised generously and discounted deliberately. Missing an alert
    is a compliance failure; raising one a human discounts in ten seconds is
    merely work.
    """
    alerts: list[Alert] = []

    for party in watchlist:
        best_score = name_score(subject_name, party.name)
        matched_on, weak = party.name, False
        shares = shares_distinctive_token(subject_name, party.name)

        for alias in party.aliases:
            alias_score = name_score(subject_name, alias)
            if alias_score > best_score:
                best_score, matched_on = alias_score, alias
                weak = is_weak_alias(alias)
                shares = shares_distinctive_token(subject_name, alias)

        # Recall first: a shared distinctive token is enough to alert even when
        # the score is low, because a subset match ("MERIDIAN" inside a longer
        # name) is exactly the case an analyst must see. The score then tells
        # them how much of the name actually corresponds.
        if not shares and best_score < threshold:
            continue

        identifiers = [
            # Individual-versus-entity is decisive: a company cannot be a
            # listed natural person.
            _compare(
                "party type",
                subject_type.value if subject_type is not PartyType.UNKNOWN else None,
                party.party_type.value,
                DiscountStrength.STRONG,
                "an entity cannot be a listed individual",
            ),
        ]

        # Which identifiers matter depends on what kind of party this is.
        # Comparing a company's incorporation date against a person's date of
        # birth would be nonsense, and padding the list with fields that can
        # never apply makes an alert look better evidenced than it is.
        if subject_type is PartyType.INDIVIDUAL:
            identifiers += [
                # Wolfsberg's primary secondary identifier. A confirmed
                # different date of birth is the cleanest discount there is.
                _compare("date of birth", subject_dob, party.date_of_birth,
                         DiscountStrength.STRONG),
                _compare("nationality", subject_nationality, party.nationality,
                         DiscountStrength.STRONG),
                # This is what an identity document actually contributes.
                # Matching a document number is near-conclusive; a different
                # one discounts strongly. The photograph contributes nothing a
                # human reviewer is qualified to adjudicate from a scan.
                _compare(
                    "identity document number",
                    subject_document_number,
                    party.document_numbers[0] if party.document_numbers else None,
                    DiscountStrength.STRONG,
                    "listing carries "
                    + (f"{len(party.document_numbers)} document number(s)"
                       if party.document_numbers else "no document number"),
                ),
            ]
        else:
            identifiers += [
                # OFAC does not publish a country field per row; the programme
                # is the nearest available signal and is weaker.
                _compare(
                    "jurisdiction",
                    subject_country,
                    _programme_country(party.programme),
                    DiscountStrength.WEAK,
                    "inferred from the sanctions programme, not a stated address",
                ),
                _compare(
                    "date of incorporation",
                    subject_incorporated,
                    None,  # not carried in the SDN CSV export
                    DiscountStrength.STRONG,
                    "not available in this list export",
                ),
            ]

        alerts.append(Alert(
            subject_name=subject_name,
            subject_type=subject_type,
            subject_role=subject_role,
            listed=party,
            score=best_score,
            matched_on=matched_on,
            via_weak_alias=weak,
            identifiers=identifiers,
        ))

    alerts.sort(key=lambda a: a.score, reverse=True)
    return alerts


# Programmes whose jurisdiction is unambiguous from the name. Anything absent
# resolves to None, which becomes UNAVAILABLE rather than a guessed mismatch.
_PROGRAMME_COUNTRY = {
    "CUBA": "Cuba",
    "IRAN": "Iran",
    # Iran Financial Sanctions Regulations — country-specific despite the name.
    "IFSR": "Iran",
    "DPRK": "North Korea",
    "RUSSIA": "Russia",
    "UKRAINE": "Ukraine",
    "DRCONGO": "Democratic Republic of the Congo",
    "SYRIA": "Syria",
    "VENEZUELA": "Venezuela",
    "BELARUS": "Belarus",
    "IRAQ": "Iraq",
}


def _programme_country(programme: str) -> str | None:
    upper = (programme or "").upper()
    for key, country in _PROGRAMME_COUNTRY.items():
        if key in upper:
            return country
    return None
