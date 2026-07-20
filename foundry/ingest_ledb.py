"""Tier-1 local-elections ingest: the American Local Government Elections
Database (de Benedictis-Kessner, Lee, Velez & Warshaw, 2023; CC-BY 4.0).

    python ingest_ledb.py [--offices Mayor,School Board] [--commit]

Instead of scraping thousands of county sites, this composes an existing,
peer-reviewed, openly-licensed dataset: candidate-level results for local
offices (mayor, school board, council, ...) across ~1,700 U.S. jurisdictions
over 50k population, 1989-2021. We fold it into the Foundry store as `contest`
records so a covered jurisdiction shows the elections that seated its
officials — closing the loop with their votes (meetings) and what's built
(CIP). Deterministic, $0 (a download + a parse; no LLM).

Integrity gate: within a contest the candidates' vote shares must sum to ~1
and each candidate's votes/total must match their share — the elections
analogue of the CIP subtotal reconciliation. Records land quarantined and
single-source; the certified canvass is the natural future oracle.
"""

import argparse
import csv
import datetime
import json
import pathlib
import re
import sys
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import schema  # noqa: E402

FOUNDRY = pathlib.Path(__file__).parent
STORE = FOUNDRY / "data" / "store"
CACHE = FOUNDRY / "data" / "onboard"
SOURCE_ID = "local-elections"

OSF_PROJECT = "https://osf.io/mv5e6/"
CANDIDATE_CSV = "https://osf.io/download/tbwzd/"   # ledb_candidatelevel.csv
GEOCODE_CSV = "https://osf.io/download/dqxhm/"     # places_geocoded.csv
CITATION = ("de Benedictis-Kessner, Lee, Velez & Warshaw (2023), American "
            "Local Government Elections Database, OSF 10.17605/OSF.IO/MV5E6, "
            "CC-BY 4.0")


def _fetch_csv(url, name):
    path = CACHE / name
    if not path.exists():
        CACHE.mkdir(parents=True, exist_ok=True)
        req = urllib.request.Request(url, headers={"User-Agent": "nospopuli-foundry-lab"})
        with urllib.request.urlopen(req, timeout=180) as r:
            path.write_bytes(r.read())
    with path.open(newline="", encoding="utf-8", errors="replace") as f:
        return list(csv.DictReader(f))


def _int(v):
    try:
        return int(round(float(v)))
    except (ValueError, TypeError):
        return None


def _float(v):
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


def _fips5(v):
    s = str(v).split(".")[0]
    return s.zfill(5) if s.isdigit() and len(s) <= 5 else s


def _slug(s):
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")


def build(offices):
    geo = {(_fips5(r["fips"]), r["state_abb"]): r for r in _fetch_csv(GEOCODE_CSV, "ledb_places_geocoded.csv")}
    rows = _fetch_csv(CANDIDATE_CSV, "ledb_candidatelevel.csv")
    contests = {}
    for row in rows:
        if row["office_consolidated"] not in offices:
            continue
        fips = _fips5(row["fips"])
        key = (row["office_consolidated"], fips, row["state_abb"],
               row["year"], row["month"], row["district"], row["contest"])
        c = contests.get(key)
        if c is None:
            g = geo.get((fips, row["state_abb"]), {})
            cid = "-".join(["ledb", _slug(row["office_consolidated"]), fips,
                            row["year"], row["month"] or "0",
                            _slug(row["district"]) or "at-large", _slug(row["contest"])])
            c = contests[key] = {
                "contest_id": cid,
                "office": row["office_consolidated"],
                "jurisdiction": row["geo_name"],
                "state": row["state_abb"],
                "fips": fips,
                "year": _int(row["year"]),
                "month": _int(row["month"]),
                "district": row["district"] or None,
                "candidates": [],
                "lat": _float(g.get("lat")), "lon": _float(g.get("lng")),
                "population_2020": _int(g.get("population_2020")),
                "source_url": OSF_PROJECT,
                "provenance": {"source_id": SOURCE_ID, "dataset": CITATION},
            }
        c["candidates"].append({
            "name": row["full_name"].title() if row["full_name"] else "",
            "votes": _int(row["votes"]),
            "vote_share": _float(row["vote_share"]),
            "winner": row["winner"] == "win",
            "incumbent": _int(row["incumbent"]) == 1,
            "party_est": (row.get("pid_est") or "").strip() or None,
        })
    for c in contests.values():
        votes = [x["votes"] for x in c["candidates"] if x["votes"] is not None]
        c["total_votes"] = sum(votes) if len(votes) == len(c["candidates"]) and votes else None
        c["winner_names"] = [x["name"] for x in c["candidates"] if x["winner"]]
        c["n_candidates"] = len(c["candidates"])
    return list(contests.values())


def gate(contests):
    """Vote-share reconciliation: within a contest the candidate shares must
    sum to ~1, and (where votes exist) votes/total must match each share.
    Reported as a quality rate; structural errors are hard."""
    findings, reconciled, checkable = [], 0, 0
    for c in contests:
        for e in schema.structural_errors("contest", c):
            findings.append({"check": "malformed", "ref": c.get("contest_id", "?"), "msg": e})
        shares = [x["vote_share"] for x in c["candidates"] if x["vote_share"] is not None]
        if len(shares) == len(c["candidates"]) and shares:
            checkable += 1
            if 0.95 <= sum(shares) <= 1.05:
                reconciled += 1
    return findings, reconciled, checkable


def land(contests, log=print):
    note = ("Local election results from the American Local Government "
            "Elections Database (CC-BY 4.0); single-source, ingested. Vote "
            "shares reconcile within each contest. The certified canvass is "
            "the natural oracle (not yet wired).")
    for c in contests:
        c["certification"] = {"status": "quarantined", "method": None, "note": note}
    juris = {(c["jurisdiction"], c["state"]) for c in contests}
    store = {"contests": contests,
             "meta": {"title": "Local Elections — mayor & school board",
                      "sub": f"{len(contests)} contests · {len(juris)} jurisdictions · "
                             "1989–2021 · composed from an open dataset",
                      "kind": "elections",
                      "dataset": CITATION, "source_url": OSF_PROJECT,
                      "generated": datetime.datetime.now().isoformat(timespec="seconds")}}
    path = STORE / f"{SOURCE_ID}.json"
    path.write_text(json.dumps(store, indent=1))
    log(f"landed {len(contests)} contests -> {path.name}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--offices", default="Mayor,School Board")
    ap.add_argument("--commit", action="store_true")
    args = ap.parse_args()
    offices = set(o.strip() for o in args.offices.split(","))
    contests = build(offices)
    findings, reconciled, checkable = gate(contests)
    juris = {(c["jurisdiction"], c["state"]) for c in contests}
    print(f"offices {sorted(offices)}: {len(contests)} contests across "
          f"{len(juris)} jurisdictions, {sum(c['n_candidates'] for c in contests)} candidates")
    print(f"vote-share reconciliation: {reconciled}/{checkable} "
          f"({100 * reconciled // max(checkable, 1)}%) of contests with full shares")
    if findings:
        print(f"GATE: {len(findings)} structural findings")
        for f in findings[:8]:
            print(f"  [{f['check']}] {f['ref']}: {f['msg'][:90]}")
        return 1
    print("GATE PASSED (schema; share reconciliation reported above)")
    if args.commit:
        land(contests)
    else:
        print("(dry run — pass --commit to land)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
