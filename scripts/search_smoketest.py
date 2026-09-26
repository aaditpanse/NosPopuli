"""
Federal search regression harness.

Hits POST /search with a curated set of queries grouped by what they probe,
captures top results + timing + fast-path flag, and prints a clean report.

Usage:
    python search_smoketest.py                    # run all groups
    python search_smoketest.py fastpath named     # run only listed groups
    python search_smoketest.py --fresh            # bypass server cache
    python search_smoketest.py --base http://localhost:8000
    python search_smoketest.py --out report.json

The evaluation gate for the local /search (plan step 40) is here too: pool
both systems' top 10 for EVAL_QUERIES, have the owner judge the pool blind,
then score recall@10. Pooling calls the router and expander (Haiku) and
GovInfo once a query; run it on the server, where the index is.

    python -m scripts.search_smoketest --pool pool.json
    python -m scripts.search_smoketest --score pool.json judgments.json
"""

import argparse
import json
import sys
import time
from dataclasses import dataclass
from typing import Optional

import requests


@dataclass
class Case:
    query: str
    expect_contains: Optional[str] = None  # substring match on top-3 (case-insensitive)
    expect_empty: bool = False             # expect no results / polite redirect
    notes: str = ""


GROUPS: dict[str, list[Case]] = {
    "fastpath": [
        Case("H.R. 4838",                  expect_contains="hr",    notes="regex shortcut"),
        Case("s 2",                        expect_contains="s",     notes="regex shortcut"),
        Case("HJRES 12",                   expect_contains="hjres", notes="regex shortcut"),
        Case("show me hr 1",               expect_contains="hr",    notes="intro prefix"),
        Case("tell me about S.J.Res. 5",   expect_contains="sjres", notes="punctuated form"),
    ],
    "named": [
        Case("Inflation Reduction Act",    expect_contains="inflation reduction"),
        Case("CHIPS Act",                  expect_contains="chips"),
        Case("Laken Riley Act",            expect_contains="laken riley"),
        Case("Respect for Marriage Act",   expect_contains="respect for marriage"),
        Case("PACT Act",                   expect_contains="pact",  notes="ambiguous"),
    ],
    "concept": [
        Case("bills about student loan forgiveness", expect_contains="loan"),
        Case("legislation on TikTok ban",            notes="should surface divestiture/foreign-app bills"),
        Case("bills regulating AI in healthcare",    notes="AI in titles uses many phrasings"),
        Case("crypto stablecoin regulation",         expect_contains="digital asset"),
        Case("right to repair",                      expect_contains="repair"),
    ],
    "edge": [
        Case("something about drones over cities",   notes="vague — should not hallucinate"),
        Case("that bill Pelosi was talking about last week", notes="no anchor, validator should floor"),
        Case("bill to ban TikTok from 2024",         notes="date constraint — likely degrades"),
        Case("marijuana",                            notes="single broad word — may be floored"),
        Case("farm bill",                            expect_contains="agriculture", notes="hardcoded → HR 2"),
    ],
    "negative": [
        Case("weather forecast for tomorrow",        expect_empty=True, notes="off-topic"),
        Case("HR 999999",                            expect_empty=True, notes="nonexistent bill"),
    ],
}


def run_case(base: str, case: Case, fresh: bool, timeout: int) -> dict:
    started = time.perf_counter()
    payload = {"question": case.query, "max_results": 5, "fresh": fresh}
    try:
        r = requests.post(f"{base}/search", json=payload, timeout=timeout)
        elapsed = time.perf_counter() - started
        if r.status_code != 200:
            return {"ok": False, "elapsed": elapsed, "error": f"HTTP {r.status_code}", "body": r.text[:300]}
        data = r.json()
    except Exception as e:
        return {"ok": False, "elapsed": time.perf_counter() - started, "error": f"{type(e).__name__}: {e}"}

    results = data.get("results") or data.get("bills") or []
    top = []
    for item in results[:3]:
        title = item.get("title") or item.get("name") or ""
        bill_id = " ".join(filter(None, [
            (item.get("type") or "").upper(),
            str(item.get("number") or "")
        ])).strip()
        top.append({"id": bill_id, "title": title[:120]})

    return {
        "ok": True,
        "elapsed": elapsed,
        "route": data.get("route") or data.get("query_type"),
        "fast_path": data.get("_fast_path") or (data.get("debug") or {}).get("_fast_path"),
        "n_results": len(results),
        "top": top,
        "verdict": _verdict(case, top),
    }


def _verdict(case: Case, top: list[dict]) -> str:
    if case.expect_empty:
        return "PASS (empty)" if len(top) == 0 else f"REVIEW (expected empty, got {len(top)})"
    if not case.expect_contains:
        return "PASS" if top else "FAIL (no results)"
    needle = case.expect_contains.lower()
    haystack = " ".join((t["id"] + " " + t["title"]) for t in top).lower()
    return "PASS" if needle in haystack else f"FAIL (no '{case.expect_contains}' in top 3)"


# ---------------------------------------------------------------- evaluation

# Drafted 2026-09-26 for the owner to edit: the 14 legislation asks in the
# search log, then topic asks, named acts, older and enacted asks, and the
# vague or misspelled way people actually type.
EVAL_QUERIES = (
    "healthcare bills in virginia", "ai regulation", "healthcare",
    "water rights in the colorado basin and tribal allocations", "ai regulation bills in virginia",
    "kennedy healthcare", "healthcare bills in texas", "bills about rural broadband in montana",
    "doctors and ai", "county grant programs", "healthcare bars", "how did nobody here vote on zoning",
    "healthcare bill on guns", "anti sexual assualt",
    "bills to lower insulin prices", "student loan forgiveness", "what is congress doing about fentanyl",
    "banning stock trading by members of congress", "tiktok ban", "right to repair",
    "bills about wildfire prevention", "minimum wage increase", "paid family leave", "police body cameras",
    "social security solvency", "child tax credit expansion", "border wall funding",
    "daylight saving time permanent", "protecting kids online social media", "crypto stablecoin regulation",
    "drug pricing medicare negotiation", "veterans burn pits", "election security and voting machines",
    "climate change carbon tax", "gun background checks", "abortion access", "marijuana legalization",
    "affordable housing for teachers", "electric vehicle tax credits", "PFAS forever chemicals in drinking water",
    "surprise medical bills", "data privacy law", "nuclear power plants licensing", "farm bill",
    "college athletes getting paid", "rail safety after east palestine", "microplastics", "ukraine aid",
    "semiconductor manufacturing", "opioid treatment access in rural areas",
    "Inflation Reduction Act", "CHIPS and Science Act", "Laken Riley Act", "Respect for Marriage Act",
    "PACT Act", "Affordable Care Act repeal", "USA PATRIOT Act reauthorization", "Dodd-Frank rollback",
    "Great American Outdoors Act", "First Step Act",
    "laws passed about the 2008 financial crisis", "hurricane katrina relief",
    "no child left behind reauthorization", "bills about the iraq war funding 2007",
    "laws signed about covid relief", "what laws protect whistleblowers", "9/11 first responders health",
    "something to help small farmers", "stuff about making college cheaper",
    "bills for ppl with disabilities working", "helthcare for vets", "is anyone trying to fix the post office",
)


def _bill_key(r):
    return f"{r.get('congress')}-{(r.get('type') or '').lower()}-{r.get('number')}"


def blind_order(query, keys):
    """The pool in an order that says nothing about which system found a
    bill, and is the same on every run. Pure."""
    import hashlib
    return sorted(set(keys), key=lambda k: hashlib.sha1(f"{query}|{k}".encode()).hexdigest())


def recall_at(found, relevant, k=10):
    """Share of the judged-relevant bills in the first k found; None when
    nothing was judged relevant, since then there is nothing to recall. Pure."""
    if not relevant:
        return None
    return len(set(found[:k]) & set(relevant)) / len(relevant)


def score(pool_rows, judgments, k=10):
    """Mean recall@k per system over the queries with a relevant bill.
    judgments: {query: {bill_key: "yes" | "partly" | "no"}}. "partly" counts
    as not relevant here and is reported apart. Pure."""
    out = {"queries": 0, "unjudged": 0, "old": 0.0, "new": 0.0, "partly": 0, "per_query": []}
    for row in pool_rows:
        marks = judgments.get(row["query"]) or {}
        if any(c not in marks for c in row["pool"]):
            out["unjudged"] += 1
            continue
        relevant = [c for c, m in marks.items() if m == "yes"]
        out["partly"] += sum(1 for m in marks.values() if m == "partly")
        old, new = recall_at(row["old"], relevant, k), recall_at(row["new"], relevant, k)
        out["per_query"].append({"query": row["query"], "relevant": len(relevant), "old": old, "new": new})
        if old is not None:
            out["queries"] += 1
            out["old"] += old
            out["new"] += new
    if out["queries"]:
        out["old"] /= out["queries"]
        out["new"] /= out["queries"]
    return out


def build_pool(queries, k=10):
    """Both systems' top k for each query, at the same point: search_bills'
    output. Today's side runs the router and expander as /search does; the
    local side gets the router's Congress filter and enacted flag, so the
    comparison is the retrieval alone."""
    import os
    import anthropic
    from agents.router_agent import structure_question
    from agents.query_expander_agent import expand_query
    from agents.search_agent import search_bills
    from search import bill_index
    from correspondence.db import _get_pool
    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    rows = []
    for q in queries:
        row = {"query": q, "old": [], "new": [], "error": None}
        try:
            s = structure_question(q, get_client=lambda: client)
            s.setdefault("congress_numbers", [119, 118])
            row.update(route=s.get("query_type"), congresses=s["congress_numbers"],
                       full_history=bool(s.get("full_history")), laws_only=s.get("status") == "enacted")
            s["expanded_terms"] = expand_query(s.get("keywords") or [], s.get("topic") or "", client) or []
            s["original_question"] = q
            # A law hit with no bill behind it has no key to judge.
            row["old"] = [_bill_key(r) for r in search_bills(s, k) if r.get("type") and r.get("number")][:k]
            row["new"] = [_bill_key(r) for r in bill_index.search(
                q, None if row["full_history"] else row["congresses"], k, row["laws_only"])]
        except Exception as e:                              # noqa: BLE001 - recorded, not hidden
            row["error"] = f"{type(e).__name__}: {e}"
        row["pool"] = blind_order(q, row["old"] + row["new"])
        rows.append(row)
        print(f"{len(row['old']):2d} old  {len(row['new']):2d} new  {len(row['pool']):2d} pool  {q}", flush=True)
    # What the judge reads: title, policy area and the opening of the CRS
    # summary, from the same bill_doc rows for both systems.
    keys = sorted({c for r in rows for c in r["pool"]})
    ids = ["instrument/us/" + k.replace("-", "/") for k in keys]
    with _get_pool().connection() as conn, conn.cursor() as cur:
        cur.execute("""SELECT instrument_id, title, policy_area, is_law, introduced, left(summary, 600)
                       FROM bill_doc WHERE instrument_id = ANY(%s)""", (ids,))
        bills = {i.removeprefix("instrument/us/").replace("/", "-"):
                 {"title": t, "policy_area": p, "is_law": bool(l), "introduced": str(d or ""), "summary": s}
                 for i, t, p, l, d, s in cur.fetchall()}
    return {"k": k, "queries": rows, "bills": bills}


def main():
    if len(sys.argv) > 2 and sys.argv[1] == "--pool":
        out = build_pool(EVAL_QUERIES)
        with open(sys.argv[2], "w") as f:
            json.dump(out, f, indent=1)
        print(f"wrote {sys.argv[2]}: {len(out['queries'])} queries, {len(out['bills'])} bills")
        return
    if len(sys.argv) > 3 and sys.argv[1] == "--score":
        pool, judgments = (json.load(open(p)) for p in sys.argv[2:4])
        print(json.dumps(score(pool["queries"], judgments, pool["k"]), indent=1))
        return
    ap = argparse.ArgumentParser()
    ap.add_argument("groups", nargs="*", default=list(GROUPS.keys()))
    ap.add_argument("--base", default="http://localhost:8000")
    ap.add_argument("--fresh", action="store_true", help="bypass server cache")
    ap.add_argument("--timeout", type=int, default=60)
    ap.add_argument("--out", help="write full JSON report to this path")
    args = ap.parse_args()

    unknown = [g for g in args.groups if g not in GROUPS]
    if unknown:
        sys.exit(f"unknown groups: {unknown}. available: {list(GROUPS)}")

    report = {"base": args.base, "fresh": args.fresh, "groups": {}}
    totals = {"pass": 0, "fail": 0, "review": 0, "error": 0}

    for group in args.groups:
        print(f"\n=== {group.upper()} ===")
        group_out = []
        for case in GROUPS[group]:
            res = run_case(args.base, case, args.fresh, args.timeout)
            group_out.append({"query": case.query, "notes": case.notes, **res})
            if not res["ok"]:
                totals["error"] += 1
                print(f"  ERR     {res['elapsed']:5.2f}s  {case.query!r}  → {res['error']}")
                continue
            v = res["verdict"]
            tag = "PASS" if v.startswith("PASS") else ("FAIL" if v.startswith("FAIL") else "REVIEW")
            totals["pass" if tag == "PASS" else "fail" if tag == "FAIL" else "review"] += 1
            fp = " [fast]" if res["fast_path"] else ""
            print(f"  {tag:7s} {res['elapsed']:5.2f}s  {case.query!r}{fp}")
            for t in res["top"]:
                print(f"           - {t['id'] or '—':10s} {t['title']}")
            if not v.startswith("PASS"):
                print(f"           verdict: {v}")
        report["groups"][group] = group_out

    print(f"\nsummary: {totals['pass']} pass · {totals['fail']} fail · "
          f"{totals['review']} review · {totals['error']} error")
    report["totals"] = totals

    if args.out:
        with open(args.out, "w") as f:
            json.dump(report, f, indent=2)
        print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
