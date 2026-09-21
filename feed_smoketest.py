"""
Opt-in live feed smoke. Not part of unittest discover.

Hits POST /feed for a handful of American places and checks soft
invariants (current congress, no blocked lede, one-per-member in the
headline). Live payloads move; REVIEW is not a CI failure.

Usage:
    python feed_smoketest.py
    python feed_smoketest.py --base http://localhost:8000 --out report.json
"""

import argparse
import json
import sys
import time

import requests

from feed_agent import _title_blocked, current_congress, title_matches_interest

PLACES = [
    {
        "name": "Fairfax VA-10",
        "interests": ["healthcare", "housing", "veterans"],
        "senator_bioguides": ["K000384", "W000805"],
        "rep_bioguide": "S001230",
        "state_code": "VA",
    },
    {
        "name": "Manhattan NY-12",
        "interests": ["housing", "economy"],
        "senator_bioguides": ["S000148", "G000555"],
        "rep_bioguide": "N000002",
        "state_code": "NY",
    },
    {
        "name": "Rural Kansas KS-1",
        "interests": ["agriculture", "economy"],
        "senator_bioguides": ["M000934", "M001198"],
        "rep_bioguide": "M001227",
        "state_code": "KS",
    },
    {
        "name": "Los Angeles CA-30",
        "interests": ["climate", "housing"],
        "senator_bioguides": ["P000145", "S001150"],
        "rep_bioguide": "F000468",
        "state_code": "CA",
    },
    {
        "name": "Anchorage AK-AL",
        "interests": ["climate", "veterans"],
        "senator_bioguides": ["M001153", "S001198"],
        "rep_bioguide": "B001327",
        "state_code": "AK",
    },
    {
        "name": "Honolulu HI-1",
        "interests": ["climate", "healthcare"],
        "senator_bioguides": ["S001194", "H001042"],
        "rep_bioguide": "C001055",
        "state_code": "HI",
    },
    {
        "name": "El Paso TX-16",
        "interests": ["immigration", "housing"],
        "senator_bioguides": ["C001056", "C000127"],
        "rep_bioguide": "G000587",
        "state_code": "TX",
    },
    {
        "name": "Miami FL-27",
        "interests": ["climate", "housing"],
        "senator_bioguides": ["R000595", "S001217"],
        "rep_bioguide": "S001200",
        "state_code": "FL",
    },
]


def _headline(items):
    eligible = [
        b for b in items
        if b.get("headline_eligible", not b.get("is_appropriations"))
    ]
    return eligible[:4]


def _check(place, data):
    notes = []
    items = data.get("items") or []
    if not items:
        return "REVIEW", "empty pool"
    congress = current_congress()
    front = _headline(items)
    if not front:
        return "FAIL", "no non-appropriations headline"
    lede = front[0]
    lede_congress = lede.get("congress")
    if lede_congress and int(lede_congress) != congress:
        notes.append(f"lede congress {lede_congress} != {congress}")
    if _title_blocked(lede.get("title")):
        notes.append(f"blocked lede: {lede.get('title')[:60]}")
    bios = [
        b.get("sponsor_bioguide")
        for b in front
        if b.get("feed_reason") == "your_rep" and b.get("sponsor_bioguide")
    ]
    if len(bios) != len(set(bios)):
        notes.append("duplicate sponsor in headline")
    for b in items:
        reason = b.get("feed_interest") or b.get("feed_reason")
        if reason in place["interests"] and not title_matches_interest(b.get("title"), reason):
            notes.append(f"stem-gate miss: {b.get('title')[:50]}")
            break
    if notes:
        return "FAIL", "; ".join(notes)
    return "PASS", f"{len(items)} items · lede {lede.get('type')}{lede.get('number')}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://localhost:8000")
    ap.add_argument("--timeout", type=int, default=60)
    ap.add_argument("--out", help="write JSON report")
    args = ap.parse_args()

    report = {"base": args.base, "places": []}
    totals = {"pass": 0, "fail": 0, "review": 0, "error": 0}

    for place in PLACES:
        started = time.perf_counter()
        try:
            r = requests.post(
                f"{args.base}/feed",
                json={
                    "interests": place["interests"],
                    "senator_bioguides": place["senator_bioguides"],
                    "rep_bioguide": place["rep_bioguide"],
                    "state_code": place["state_code"],
                },
                timeout=args.timeout,
            )
            elapsed = time.perf_counter() - started
            if r.status_code != 200:
                totals["error"] += 1
                row = {"name": place["name"], "ok": False, "elapsed": elapsed,
                       "error": f"HTTP {r.status_code}"}
                print(f"  ERR     {elapsed:5.2f}s  {place['name']}  → HTTP {r.status_code}")
            else:
                data = r.json()
                tag, detail = _check(place, data)
                key = tag.lower()
                totals[key] = totals.get(key, 0) + 1
                row = {
                    "name": place["name"],
                    "ok": True,
                    "elapsed": elapsed,
                    "verdict": tag,
                    "detail": detail,
                    "count": data.get("count"),
                    "state_status": data.get("state_status"),
                }
                print(f"  {tag:7s} {elapsed:5.2f}s  {place['name']}  {detail}"
                      f"  [{data.get('state_status')}]")
        except Exception as e:
            elapsed = time.perf_counter() - started
            totals["error"] += 1
            row = {"name": place["name"], "ok": False, "elapsed": elapsed,
                   "error": f"{type(e).__name__}: {e}"}
            print(f"  ERR     {elapsed:5.2f}s  {place['name']}  → {row['error']}")
        report["places"].append(row)

    print(f"\nsummary: {totals['pass']} pass · {totals['fail']} fail · "
          f"{totals['review']} review · {totals['error']} error")
    report["totals"] = totals
    if args.out:
        with open(args.out, "w") as f:
            json.dump(report, f, indent=2)
        print(f"wrote {args.out}")
    sys.exit(1 if totals["fail"] or totals["error"] else 0)


if __name__ == "__main__":
    main()
