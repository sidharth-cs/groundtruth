#!/usr/bin/env python3
"""Generate the synthetic watchlist this system ships with.

The brief is explicit: *"Where your build needs a client, a company, or data,
invent them. Fictional clients and fabricated test data are expected; real
third-party data is not welcome."*

So the bundled list is invented. Every party below — every company, every
natural person, every passport number and date of birth — is fabricated. The
sanctioning body is fictional. Any resemblance to a real listed party is
accidental and unintended.

What is *not* invented is the shape. The schema, the free-text remarks
convention, the alias markers and the programme-code style are modelled on how
real consolidated sanctions lists are published, because the point of the
exercise is that the screening engine handles the format real lists come in.
`scripts/curate_watchlist.py` remains in the repository and builds the same
schema from the genuine OFAC SDN file for anyone who wants to point this at the
real thing; it is opt-in and nothing in the default path touches it.

The entries are chosen for what each one teaches the adjudicator:

  collision  — shares a distinctive token with a synthetic vendor, so it raises
               a real alert a human has to reason about
  individual — UBO screening needs listed natural persons, with the secondary
               identifiers (date of birth, nationality, document number) that
               distinguish a true match from a false one
  weak-alias — short or generic aliases, the documented cause of most false
               positives
  volume     — ordinary entries, so the list is not obviously three fixtures

    python scripts/make_watchlist.py
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "watchlists" / "synthetic_consolidated.json"

# Fabricated sanctioning authority and programme codes. The country suffix is
# real because jurisdiction matching has to mean something — a vendor
# incorporated in Malta being screened against a listing tied to a country is
# the comparison an analyst actually makes.
ENTRIES: list[dict] = [
    # --- collisions with the synthetic vendor corpora ----------------------
    {
        "uid": "SYN-10041",
        "name": "MERIDIAN RESEARCH AND PRODUCTION KOMBINAT OJSC",
        "party_type": "entity",
        "programme": "SYN-RUSSIA",
        "remarks": "Industrial research combine. Listed for procurement activity.",
    },
    {
        "uid": "SYN-10042",
        "name": "PIONEER LOGISTICS",
        "party_type": "entity",
        "programme": "SYN-IRAN",
        "remarks": "Freight forwarding. Short two-token name; expect collisions.",
    },
    {
        "uid": "SYN-10043",
        "name": "PENROSE MARITIME LOGISTICS CO LTD",
        "party_type": "entity",
        "programme": "SYN-DPRK",
        "aliases": ["PML CO LTD"],
        "remarks": "a.k.a. PML CO LTD; shipping agent.",
    },
    {
        "uid": "SYN-10044",
        "name": "AIRCRAFT COMPONENTS LOGISTICS LIMITED",
        "party_type": "entity",
        "programme": "SYN-RUSSIA",
        "aliases": ["ACL LOGISTICS", "AVIATION COMPONENTS LOGISTICS LLC"],
        "remarks": "a.k.a. ACL LOGISTICS; a.k.a. AVIATION COMPONENTS LOGISTICS LLC.",
    },
    {
        "uid": "SYN-10045",
        "name": "LLC IQ COMPONENTS",
        "party_type": "entity",
        "programme": "SYN-RUSSIA",
        "remarks": "Electronics distributor.",
    },
    # --- ordinary entities -------------------------------------------------
    {"uid": "SYN-10046", "name": "CARGO FREIGHT INTERNATIONAL", "party_type": "entity",
     "programme": "SYN-DRCONGO", "remarks": "Freight consolidator."},
    {"uid": "SYN-10047", "name": "OPTIMA FREIGHT OY", "party_type": "entity",
     "programme": "SYN-BELARUS", "remarks": "Baltic freight agent."},
    {"uid": "SYN-10048", "name": "GALAX TRADING CO., LTD.", "party_type": "entity",
     "programme": "SYN-CUBA", "remarks": "General trading."},
    {"uid": "SYN-10049", "name": "NORDSTRAND MARITIME AND TRADING COMPANY",
     "party_type": "entity", "programme": "SYN-CUBA", "remarks": "Ship management."},
    {"uid": "SYN-10050", "name": "FARTRADE HOLDINGS S.A.", "party_type": "entity",
     "programme": "SYN-IRAQ", "remarks": "Holding company."},
    {"uid": "SYN-10051", "name": "BANCO ORIENTAL DE INVERSIONES", "party_type": "entity",
     "programme": "SYN-CUBA", "aliases": ["BOI"], "remarks": "a.k.a. BOI; state bank."},
    {"uid": "SYN-10052", "name": "AEROLINEAS DEL CARIBE ORIENTAL", "party_type": "entity",
     "programme": "SYN-CUBA", "remarks": "Regional carrier."},
    {"uid": "SYN-10053", "name": "ELECTRONIC COMPONENTS INDUSTRIES CO",
     "party_type": "entity", "programme": "SYN-IRAN", "aliases": ["ECI"],
     "remarks": "a.k.a. ECI; component manufacturer."},
    {"uid": "SYN-10054", "name": "DALIAN HAIBRIDGE INTERNATIONAL FREIGHT CO. LTD.",
     "party_type": "entity", "programme": "SYN-DPRK", "remarks": "Freight forwarder."},
    {"uid": "SYN-10055", "name": "WEIHAI WORLDLINE SHIPPING FREIGHT",
     "party_type": "entity", "programme": "SYN-DPRK", "remarks": "Shipping agent."},
    {"uid": "SYN-10056", "name": "PISHRO SYSTEMS RESEARCH COMPANY", "party_type": "entity",
     "programme": "SYN-IRAN", "remarks": "Systems research."},
    {"uid": "SYN-10057", "name": "INFORMATION SYSTEMS TEHRAN", "party_type": "entity",
     "programme": "SYN-IRAN", "remarks": "Systems integrator."},
    {"uid": "SYN-10058", "name": "ANGLO-CARIBBEAN CO., LTD.", "party_type": "entity",
     "programme": "SYN-CUBA", "remarks": "Trading company."},
    {"uid": "SYN-10059", "name": "BEIT EL-MAL HOLDINGS", "party_type": "entity",
     "programme": "SYN-SYRIA", "remarks": "Investment holding."},
    # --- natural persons, for UBO screening --------------------------------
    #
    # Fully invented identities. The one at SYN-20003 is the counterfactual the
    # `identified` corpus is built around: a synthetic vendor's beneficial
    # owner shares this name exactly, and only the passport and date of birth
    # tell the two apart.
    {
        "uid": "SYN-20001", "name": "OKONKWO, Emeka Chidubem", "party_type": "individual",
        "remarks": "DOB 12 Feb 1961; POB Enugu; nationality Nigeria; Gender Male.",
        "programme": "SYN-SDGT",
    },
    {
        "uid": "SYN-20002", "name": "HALVORSEN, Bjorn Aksel", "party_type": "individual",
        "remarks": "DOB 04 Sep 1955; POB Bergen; Passport N4471209 (Norway).",
        "programme": "SYN-SDGT",
    },
    {
        "uid": "SYN-20003", "name": "VANTERPOOL, Anselm Rhodes", "party_type": "individual",
        "remarks": (
            "DOB 19 Jun 1951; POB Road Town; nationality Antigua; "
            "Passport 1084010 (Antigua); alt. Passport 19820215."
        ),
        "programme": "SYN-SDGT",
    },
    {
        "uid": "SYN-20004", "name": "ZULUETA MARCHENA, Idalberto", "party_type": "individual",
        "remarks": "DOB 27 Nov 1948; POB Matanzas; nationality Cuba.",
        "programme": "SYN-CUBA",
    },
]


def parse_identifiers(remarks: str) -> dict:
    """Same parser shape the real-list curator uses.

    The secondary identifiers live inside one free-text field because that is
    how consolidated lists publish them, and a screening engine that cannot
    read them screens on names alone.
    """
    found: dict = {"date_of_birth": "", "place_of_birth": "",
                   "nationality": "", "document_numbers": []}
    for part in remarks.split(";"):
        part = part.strip()
        lowered = part.lower()
        if lowered.startswith("dob ") and not found["date_of_birth"]:
            found["date_of_birth"] = part[4:].strip()
        elif lowered.startswith("pob ") and not found["place_of_birth"]:
            found["place_of_birth"] = part[4:].strip()
        elif lowered.startswith("nationality ") and not found["nationality"]:
            found["nationality"] = part[len("nationality "):].strip()
        elif lowered.startswith(("passport ", "alt. passport ")):
            value = part.split(None, 1)[1] if lowered.startswith("passport ") \
                else part.split(". ", 1)[-1].split(None, 1)[-1]
            # Trailing sentence punctuation is not part of a document
            # number. "Passport 19820215." parsed as "19820215." would
            # never match the real thing, and the mismatch would read as
            # discounting evidence rather than as a parser bug.
            number = value.split("(")[0].strip().rstrip(" ,.;")
            if number:
                found["document_numbers"].append(number)
    return found


def parse_aliases(remarks: str) -> list[str]:
    aliases: list[str] = []
    for part in remarks.split(";"):
        part = part.strip()
        for marker in ("a.k.a. ", "f.k.a. ", "n.k.a. "):
            if part.startswith(marker):
                alias = part[len(marker):].strip().strip("'\".")
                if alias:
                    aliases.append(alias)
    return aliases


def main() -> int:
    entries = []
    for raw in ENTRIES:
        remarks = raw.get("remarks", "")
        entries.append({
            "uid": raw["uid"],
            "name": raw["name"],
            "party_type": raw["party_type"],
            "programme": raw["programme"],
            "title": "",
            "aliases": raw.get("aliases") or parse_aliases(remarks),
            **parse_identifiers(remarks),
            "remarks": remarks,
        })

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({
        "source": "generated by scripts/make_watchlist.py",
        "source_name": (
            "Synthetic Consolidated Sanctions List — a fabricated dataset for "
            "this project"
        ),
        "licence": "invented for this repository; not derived from any real list",
        "note": (
            "EVERY party here is fictional: every company, every person, every "
            "passport number and date of birth. The sanctioning body does not "
            "exist. Any resemblance to a real listed party is accidental. The "
            "schema is modelled on how real consolidated lists publish, so the "
            "screening engine handles the shape real data arrives in. To screen "
            "against the genuine OFAC SDN list instead, run "
            "scripts/curate_watchlist.py — that path is opt-in and nothing in "
            "the default configuration uses it."
        ),
        "entries": entries,
    }, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    individuals = sum(1 for e in entries if e["party_type"] == "individual")
    print(f"wrote {OUT.relative_to(ROOT)}")
    print(f"  {len(entries)} entries — {individuals} individual, "
          f"{len(entries) - individuals} entity")
    print(f"  {sum(1 for e in entries if e['aliases'])} carry aliases")
    print(f"  {sum(1 for e in entries if e['date_of_birth'])} carry a date of birth")
    print(f"  {sum(1 for e in entries if e['document_numbers'])} carry a document number")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
