#!/usr/bin/env python3
"""Curate a small, real watchlist extract from the OFAC SDN list.

The full SDN list is 19,190 entries and about 5.6 MB. Committing all of it
would add noise and weight for no gain: the point of this system is the
adjudication workflow, not list size. So this pulls a deliberately small
extract that still produces every case an analyst actually meets.

Every entry below is **real** and unmodified, published by the US Treasury at
https://www.treasury.gov/ofac/downloads/sdn.csv — a public government list
that exists to be screened against. Nothing here is proprietary: commercial
screening products aggregate and enrich lists like this one, and no such
product's data appears anywhere in this repository.

The vendors screened against it remain entirely synthetic.

    python scripts/curate_watchlist.py            # uses a cached sdn.csv
    python scripts/curate_watchlist.py --fetch    # re-downloads it first
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import urllib.request
from pathlib import Path

SDN_URL = "https://www.treasury.gov/ofac/downloads/sdn.csv"
ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "watchlists" / "ofac_sdn_extract.json"
CACHE = ROOT / ".cache" / "sdn.csv"

# Chosen for what each one teaches the adjudicator, not at random.
#
#   collision  — shares a distinctive token with a synthetic vendor, so it
#                raises a real alert that a human has to reason about
#   individual — UBO screening needs listed natural persons
#   weak-alias — short or generic aliases, the documented cause of most false
#                positives (Wolfsberg names these explicitly)
#   volume     — ordinary entries so the list is not obviously curated
WANTED = [
    # --- collisions with the synthetic vendors -----------------------------
    "MERIDIAN RESEARCH AND PRODUCTION FIRM JSC",
    "AIRCRAFT COMPONENTS LOGISTICS LTD",
    "LLC IQ COMPONENTS",
    "PIONEER LOGISTICS",
    "PRIMORYE MARITIME LOGISTICS CO LTD",
    "CARGO FREIGHT INTERNATIONAL",
    "OPTIMA FREIGHT OY",
    "GALAX TRADING CO., LTD.",
    "NORDSTRAND MARITIME AND TRADING COMPANY",
    "FARTRADE HOLDINGS S.A.",
    "BEIT EL-MAL HOLDINGS",
    # --- entities, ordinary ------------------------------------------------
    "BANCO NACIONAL DE CUBA",
    "AEROCARIBBEAN AIRLINES",
    "ANGLO-CARIBBEAN CO., LTD.",
    "ELECTRONIC COMPONENTS INDUSTRIES CO",
    "DALIAN HAIBO INTERNATIONAL FREIGHT CO. LTD.",
    "WEIHAI WORLD-SHIPPING FREIGHT",
    "INFORMATION SYSTEMS IRAN",
    "PISHRO SYSTEMS RESEARCH COMPANY",
    # --- individuals, for UBO screening ------------------------------------
    "ABBAS, Abu",
    "AL RAHMAN, Shaykh Umar Abd",
    "AL ZAWAHIRI, Dr. Ayman",
    "AL-ZOMOR, Abboud Abdul Latif Hassan",
]


def fetch() -> None:
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    print(f"downloading {SDN_URL}")
    urllib.request.urlretrieve(SDN_URL, CACHE)
    print(f"saved {CACHE} ({CACHE.stat().st_size:,} bytes)")


def clean(value: str | None) -> str:
    value = (value or "").strip().strip('"')
    # OFAC writes "-0-" where a field is not applicable.
    return "" if value == "-0-" else value


def parse_aliases(remarks: str) -> list[str]:
    """Pull a.k.a. names out of the free-text remarks field."""
    aliases: list[str] = []
    for part in remarks.split(";"):
        part = part.strip()
        for marker in ("a.k.a. ", "f.k.a. ", "n.k.a. "):
            if part.startswith(marker):
                alias = part[len(marker):].strip().strip("'\".")
                if alias:
                    aliases.append(alias)
    return aliases


def parse_identifiers(remarks: str) -> dict:
    """Pull the secondary identifiers out of the remarks field.

    OFAC has no columns for these — date of birth, place of birth, nationality
    and passport numbers all live inside one free-text field, semicolon
    separated. They are also the fields the Wolfsberg guidance names as the
    means of telling a true match from a false one, so leaving them buried in
    prose would mean screening on names alone.

    A subject who cannot be discounted on any of these is exactly the subject
    an analyst escalates, so parsing them badly is worse than not parsing them:
    a missed nationality reads as "unavailable" and pushes a clearable alert
    into escalation. Hence the deliberate strictness below — a field is either
    recognised confidently or left absent.
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
        elif lowered.startswith(("passport ", "alt. passport ",
                                 "national id no. ", "identification number ")):
            # "Passport 1084010 (Egypt)" -> "1084010"
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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fetch", action="store_true", help="re-download the SDN list")
    args = parser.parse_args()

    if args.fetch or not CACHE.exists():
        fetch()

    rows = list(csv.reader(CACHE.open(encoding="utf-8", errors="replace")))
    by_name = {clean(r[1]).upper(): r for r in rows if len(r) > 3}

    extract, missing = [], []
    for wanted in WANTED:
        row = by_name.get(wanted.upper())
        if row is None:
            missing.append(wanted)
            continue
        sdn_type = clean(row[2])
        remarks = clean(row[11]) if len(row) > 11 else ""
        extract.append({
            "uid": clean(row[0]),
            "name": clean(row[1]),
            # OFAC leaves the type blank for entities; make it explicit, because
            # individual-versus-entity is a first-class discounting factor.
            "party_type": "individual" if sdn_type == "individual" else "entity",
            "programme": clean(row[3]),
            "title": clean(row[4]),
            "aliases": parse_aliases(remarks),
            **parse_identifiers(remarks),
            "remarks": remarks,
        })

    if missing:
        print(f"warning: {len(missing)} wanted entries not found in this "
              f"publication: {missing}", file=sys.stderr)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({
        "source": SDN_URL,
        "source_name": "US Treasury OFAC Specially Designated Nationals list",
        "licence": "public domain — US Government work",
        "note": (
            "A curated extract, not the full list. Entries are unmodified. "
            "Vendors screened against it are synthetic."
        ),
        "entries": extract,
    }, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    individuals = sum(1 for e in extract if e["party_type"] == "individual")
    with_alias = sum(1 for e in extract if e["aliases"])
    with_dob = sum(1 for e in extract if e["date_of_birth"])
    with_doc = sum(1 for e in extract if e["document_numbers"])
    print(f"wrote {OUT.relative_to(ROOT)}")
    print(f"  {len(extract)} entries — {individuals} individual, "
          f"{len(extract) - individuals} entity")
    print(f"  {with_alias} carry aliases")
    print(f"  {with_dob} carry a date of birth, {with_doc} a document number")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
