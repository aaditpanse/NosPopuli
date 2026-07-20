"""Tier-2 recent local elections: Virginia ENR (post-2021, non-hostile CDN).

    python ingest_va_enr.py [--commit]

The Tier-1 open dataset stops at 2021. Clarity (the national vendor family)
turned into a JS app with anti-bot 403s, so per this system's rule we pivot
rather than fight it. Virginia's Enhanced-Reporting system publishes recent
results as plain CDN CSVs — this pulls the 2023 November General local winners
(School Board and Board of Supervisors, by locality/district), which extends
every covered VA county past 2021 and fills the Stafford gap the academic
dataset left. Lands as `contest` records in a separate elections store so it
merges with the Tier-1 data in the UI without clobbering it.

Known automation gap: the per-election file GUIDs live behind the ENR app's
JS API (not yet mapped), so the 2023 file URL is pinned here. Discovering
GUIDs for arbitrary elections is the follow-up that makes this fully generic.
Winner-only for now (the ENR "Winners" file carries no vote counts; the full
results file — with counts for the reconciliation gate — is the next pull).
"""

import argparse
import csv
import datetime
import io
import json
import pathlib
import re
import sys
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import schema  # noqa: E402

FOUNDRY = pathlib.Path(__file__).parent
STORE = FOUNDRY / "data" / "store"
SOURCE_ID = "va-elections"

# 2023 November General — Election Winners file (VA ENR CDN). See module note
# on GUID discovery being the automation follow-up.
WINNERS_URL = ("https://enr.elections.virginia.gov/cdn/results/"
               "d2c804ee-4ec2-46bb-91d7-5b41526eab03/"
               "Election%20Winners_c87acf3b-7398-435a-b326-8658a34888df.csv")
ELECTION_YEAR = 2023
SOURCE_PAGE = "https://enr.elections.virginia.gov/results/public/Virginia"
DATASET = "Virginia Department of Elections, Enhanced Reporting (public CDN)"

# offices we surface (the local governing bodies + schools)
OFFICE_MAP = [
    (re.compile(r"School Board", re.I), "School Board"),
    (re.compile(r"Board of Supervisors", re.I), "Board of Supervisors"),
    (re.compile(r"\bMayor\b", re.I), "Mayor"),
    (re.compile(r"City Council|Town Council|Council Member|Member Council", re.I), "City Council"),
]


def _office(raw):
    for rx, label in OFFICE_MAP:
        if rx.search(raw):
            m = re.search(r"\(([^)]+)\)", raw)
            return label, (m.group(1).strip() if m else None)
    return None, None


def build():
    req = urllib.request.Request(WINNERS_URL, headers={"User-Agent": "nospopuli-foundry-lab"})
    with urllib.request.urlopen(req, timeout=60) as r:
        text = r.read().decode("utf-8", errors="replace")
    contests = {}
    for row in csv.DictReader(io.StringIO(text)):
        office, district = _office(row.get("Office", ""))
        locality = (row.get("Locality") or "").strip().title()
        if not office or not locality:
            continue
        district = district or (row.get("District") or "").strip() or None
        key = (locality, office, district)
        c = contests.get(key)
        if c is None:
            cid = "vaenr-{}-{}-{}-{}".format(
                ELECTION_YEAR, re.sub(r"[^a-z0-9]+", "-", locality.lower()).strip("-"),
                re.sub(r"[^a-z0-9]+", "-", office.lower()).strip("-"),
                re.sub(r"[^a-z0-9]+", "-", (district or "at-large").lower()).strip("-"))
            c = contests[key] = {
                "contest_id": cid, "office": office, "jurisdiction": locality,
                "state": "VA", "year": ELECTION_YEAR, "month": 11, "district": district,
                "candidates": [], "winner_names": [], "total_votes": None,
                "source_url": SOURCE_PAGE,
                "provenance": {"source_id": SOURCE_ID, "dataset": DATASET},
            }
        name = (row.get("BallotName") or "").strip()
        if name:
            c["candidates"].append({"name": name, "votes": None, "vote_share": None,
                                    "winner": True, "incumbent": None,
                                    "party_est": (row.get("PoliticalParty") or "").strip() or None})
            c["winner_names"].append(name)
    for c in contests.values():
        c["n_candidates"] = len(c["candidates"])
    return [c for c in contests.values() if c["candidates"]]


def land(contests, log=print):
    note = ("2023 local election winners from Virginia's Enhanced Reporting "
            "system (official). Winner-only (vote counts pending the full "
            "results file); single-source, quarantined.")
    for c in contests:
        c["certification"] = {"status": "quarantined", "method": None, "note": note}
    juris = {c["jurisdiction"] for c in contests}
    store = {"contests": contests,
             "meta": {"title": "Virginia Local Elections — 2023",
                      "sub": f"{len(contests)} contests · {len(juris)} localities · "
                             "board of supervisors & school board · official (VA ENR)",
                      "kind": "elections", "dataset": DATASET, "source_url": SOURCE_PAGE,
                      "generated": datetime.datetime.now().isoformat(timespec="seconds")}}
    (STORE / f"{SOURCE_ID}.json").write_text(json.dumps(store, indent=1))
    log(f"landed {len(contests)} contests -> {SOURCE_ID}.json")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    args = ap.parse_args()
    contests = build()
    findings = [e for c in contests for e in
                (f"{c['contest_id']}: {x}" for x in schema.structural_errors("contest", c))]
    juris = sorted({c["jurisdiction"] for c in contests})
    print(f"{len(contests)} contests across {len(juris)} VA localities "
          f"({sum(c['n_candidates'] for c in contests)} winners)")
    covered = [j for j in juris if j in ("Fairfax County", "Loudoun County",
                                         "Prince William County", "Stafford County")]
    print("covered counties present:", covered)
    if findings:
        print(f"GATE: {len(findings)} structural findings"); [print("  ", f) for f in findings[:6]]
        return 1
    print("GATE PASSED (schema)")
    if args.commit:
        land(contests)
    else:
        print("(dry run — pass --commit to land)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
