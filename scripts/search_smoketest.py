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

The Tier 2 quality eval lives here too (--label, --baseline, --score; see
the "Tier 2 eval" section): a labelled question set every change to search
is scored against.
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


# ------------------------------------------------------------ Tier 2 eval
#
# The graph-search plan's Phase 0 (~/.claude/plans, 2026-09-29): a labelled
# set every later phase is scored against. The search log held 15 questions
# when this was built (the server is new; the old logs did not move), so the
# set is EVAL_QUERIES, the log, the smoke cases, and WRITTEN_QUERIES: asks for
# every intent the graph search answers, written by hand. Each row says where
# it came from and who labelled it.
#
#   python -m scripts.search_smoketest --label scripts/search_eval.json   # server: DB + Haiku, once
#   python -m scripts.search_smoketest --baseline scripts/search_eval.json preds.json [--base URL]
#   python -m scripts.search_smoketest --judge scripts/search_eval.json preds.json    # rate what the run found
#   python -m scripts.search_smoketest --score scripts/search_eval.json preds.json

EVAL_PATH = "scripts/search_eval.json"
HAIKU = "claude-haiku-4-5-20251001"

# What a question asks for, whatever the words. The graph search picks one of
# these; today's routes are mapped onto them by intent_of.
INTENTS = ("find_bills", "person_record", "how_voted", "who_voted", "money", "compare_states",
           "copied_bills", "seat_holder", "committee", "organization", "explain_law", "local_place",
           "elections", "off_topic")

# The intents whose answer is a set of bills: only these are scored on
# recall and nDCG ("write me a poem" has no relevant bill to find).
BILL_INTENTS = ("find_bills", "explain_law", "compare_states", "copied_bills", "how_voted", "who_voted",
                "money", "organization", "committee")

# A reader for the "my reps" asks: Arlington, Virginia (VA-8).
_ARLINGTON = {"state": "VA", "cd": 8}

WRITTEN_QUERIES = (
    # how one person voted
    ("how did Tim Kaine vote on the NDAA", None), ("how did Mark Warner vote on the CHIPS Act", None),
    ("did Ted Cruz vote for the infrastructure bill", None), ("Elizabeth Warren votes on crypto", None),
    ("how did Susan Collins vote on the Respect for Marriage Act", None),
    ("how did Mitch McConnell vote on the Inflation Reduction Act", None),
    ("Bernie Sanders votes on minimum wage", None), ("how did Jim Jordan vote on Ukraine aid", None),
    ("how did my senators vote on the Laken Riley Act", _ARLINGTON),
    ("how did my representative vote on the debt ceiling", _ARLINGTON),
    ("my reps' votes on abortion", _ARLINGTON), ("what has my congressman sponsored", _ARLINGTON),
    ("how did my state senator vote on the budget", _ARLINGTON),
    ("how did Scott Surovell vote on marijuana", None),
    # who voted
    ("who voted against the CHIPS Act", None), ("which Republicans voted for the Respect for Marriage Act", None),
    ("who voted no on the Laken Riley Act", None), ("who voted for the Inflation Reduction Act", None),
    ("which senators voted against Ukraine aid", None), ("who voted against right to repair in Colorado", None),
    ("which Virginia delegates voted against the minimum wage increase", None),
    # a person's record
    ("Alexandria Ocasio-Cortez", None), ("what has Josh Hawley sponsored", None), ("Jeion Ward", None),
    ("Scott Wiener housing bills", None), ("Andrea Stewart-Cousins", None), ("Nancy Pelosi's record", None),
    ("what committees is Chuck Grassley on", None), ("bills sponsored by Glenn Youngkin", None),
    ("who is Abigail Spanberger", None), ("Greg Abbott vetoes", None), ("Gavin Newsom signed bills on AI", None),
    # money and influence
    ("who lobbied on the CHIPS Act", None), ("who lobbied on the Inflation Reduction Act", None),
    ("who funds Ted Cruz", None), ("how much did Tim Kaine raise in 2024", None),
    ("did pharmaceutical PACs give to senators who voted on drug pricing", None),
    ("which PACs gave to members who voted for the crypto bill", None),
    ("who lobbied on TikTok legislation", None), ("oil and gas lobbying on the Inflation Reduction Act", None),
    ("who paid for lobbying on the farm bill", None), ("top donors to Mark Warner", None),
    # an organization
    ("Pfizer", None), ("what did Exxon Mobil lobby on", None), ("National Rifle Association lobbying", None),
    ("AARP lobbying on social security", None), ("what bills did Google lobby on", None),
    ("Lockheed Martin", None), ("Chamber of Commerce lobbying 2023", None), ("Planned Parenthood", None),
    ("how much does Amazon spend on lobbying", None), ("Koch Industries", None),
    # comparing states
    ("which states passed right to repair", None), ("states that banned TikTok on government devices", None),
    ("which states legalized sports betting", None), ("states with paid family leave laws", None),
    ("which states passed data privacy laws", None), ("states banning ranked choice voting", None),
    ("which states raised the minimum wage in 2023", None), ("states with school voucher programs", None),
    ("which states passed laws on AI deepfakes in elections", None), ("states that capped insulin costs", None),
    # copied bills
    ("bills copied from model legislation", None), ("same bill in multiple states about critical race theory", None),
    ("which states introduced the same drag show bill", None), ("model bills about ESG investing", None),
    ("copycat bills on bathroom access", None), ("states with identical app store age verification bills", None),
    # who holds a seat
    ("who represents Virginia's 8th district", None), ("who was the governor of California in 2015", None),
    ("who is my congressman", _ARLINGTON), ("who are my state legislators", _ARLINGTON),
    ("who held Virginia senate district 35 in 2020", None), ("who is the speaker of the house", None),
    ("who was president in 2009", None), ("who represents Fairfax County in the House of Delegates", None),
    # committees
    ("House Armed Services Committee", None), ("who chairs the Senate Finance Committee", None),
    ("what did the Senate Judiciary Committee report", None), ("bills referred to the House Energy and Commerce Committee", None),
    ("who is on the Senate Intelligence Committee", None), ("Virginia House Appropriations Committee", None),
    ("ranking member of House Oversight", None), ("what bills did the Senate Banking Committee report", None),
    # a named or numbered law
    ("what does the Inflation Reduction Act do", None), ("explain the CHIPS Act", None),
    ("what is Title IX", None), ("HR 5376 117th congress", None), ("Virginia HB 1 2024", None),
    ("what did the Bipartisan Infrastructure Law fund", None),
    # local
    ("Fairfax County", None), ("Loudoun County board of supervisors", None), ("Arlington zoning", None),
    ("Los Angeles County budget", None), ("Pittsburgh city council", None), ("Allegheny County capital projects", None),
    ("what is my county doing about housing", _ARLINGTON), ("Harris County flood control", None),
    # elections
    ("when is the next election in Virginia", None), ("who is running for senate in Pennsylvania", None),
    ("Virginia primary 2025", None), ("am I registered to vote", None), ("polling for the Georgia senate race", None),
    # topics across the layers
    ("housing bills in Texas", None), ("Florida abortion laws", None), ("California AI safety bill", None),
    ("New York climate law", None), ("Ohio sports betting", None), ("Georgia election law", None),
    ("Pennsylvania school funding", None), ("Montana TikTok ban", None), ("Utah social media age limit", None),
    ("Colorado right to repair wheelchairs", None), ("Illinois assault weapons ban", None),
    ("Michigan right to work repeal", None),
    # off topic
    ("best pizza near me", None), ("weather tomorrow", None), ("who won the super bowl", None),
    ("stock price of Apple", None), ("write me a poem", None),
)


def eval_questions(log_path="search_log.jsonl"):
    """[{question, source, reader}]: every question once, first source wins.
    The search log is read if it is here (the server)."""
    import os
    rows, seen = [], set()

    def add(q, source, reader=None):
        k = " ".join((q or "").lower().split())
        if k and k not in seen:
            seen.add(k)
            rows.append({"question": q.strip(), "source": source, "reader": reader})
    for q in EVAL_QUERIES:
        add(q, "eval_queries")
    if os.path.exists(log_path):
        for line in open(log_path):
            try:
                add(json.loads(line).get("query") or "", "search_log")
            except ValueError:
                pass
    for cases in GROUPS.values():
        for c in cases:
            add(c.query, "smoke")
    for q, reader in WRITTEN_QUERIES:
        add(q, "written", reader)
    return rows


def held_out(question):
    """One question in five, by a hash of its words: scored, never tuned on. Pure."""
    import hashlib
    return int(hashlib.sha1(" ".join(question.lower().split()).encode()).hexdigest(), 16) % 5 == 0


def bill_key(r):
    """A search or story row's graph id (instrument/us/119/hr/1, instrument/va/2026/hb/1),
    or None when it names no bill. Pure."""
    if not r.get("type") or r.get("number") in (None, ""):
        return None
    t = str(r["type"]).lower()
    if r.get("state"):
        return f"instrument/{str(r['state']).lower()}/{r.get('session')}/{t}/{r['number']}"
    if r.get("congress"):
        return f"instrument/us/{r['congress']}/{t}/{r['number']}"
    return None


def intent_of(plate):
    """Today's /ledger answer, as one of INTENTS. Pure."""
    kind = plate.get("plate")
    if kind == "graph":
        return {"votes": "how_voted", "voters": "who_voted", "holder": "seat_holder", "committee": "committee",
                "reported": "committee", "referrals": "committee", "lobbied_on": "money", "funds": "money",
                "org_lobbied": "organization", "sponsored": "person_record", "nominated": "person_record",
                "signed_by": "person_record", "law": "explain_law"}.get(plate.get("ask"), "find_bills")
    if kind in ("bill", "state_bill"):
        return "explain_law"
    if kind in ("home", "off_topic"):
        return "off_topic"
    return {"elections": "elections", "uncharted": "local_place"}.get(kind) or \
        {"member": "person_record", "committee": "committee", "off_topic": "off_topic"}.get(
            plate.get("query_type"), "find_bills")


def recall_at(found, relevant, k=10):
    """Share of the relevant bills in the first k found; None with nothing relevant. Pure."""
    if not relevant:
        return None
    return len(set(found[:k]) & set(relevant)) / len(relevant)


def ndcg_at(found, grades, k=10):
    """nDCG@k with graded relevance ({id: 2 yes, 1 partly}); None with nothing relevant. Pure."""
    import math
    ideal = sorted(grades.values(), reverse=True)[:k]
    if not ideal:
        return None
    dcg = sum(grades.get(x, 0) / math.log2(i + 2) for i, x in enumerate(found[:k]))
    return dcg / sum(g / math.log2(i + 2) for i, g in enumerate(ideal))


def score_eval(labels, preds, only_held_out=True):
    """Mean intent accuracy, entity-link accuracy, recall@10 and nDCG@10 over
    the labelled rows (the held-out slice by default). Pure."""
    out = {"questions": 0, "intent": [], "links": [], "recall@10": [], "ndcg@10": [], "unjudged": []}
    for row in labels:
        if only_held_out and not row.get("held_out"):
            continue
        p = preds.get(row["question"])
        if p is None:
            continue
        out["questions"] += 1
        out["intent"].append(float(p.get("intent") == row["intent"]))
        want = {e["node_id"] for e in row.get("entities") or [] if e.get("node_id")}
        if want:
            out["links"].append(len(want & set(p.get("entities") or [])) / len(want))
        if row["intent"] not in BILL_INTENTS:
            continue
        grades = {b: 2 if m == "yes" else 1 for b, m in (row.get("bills") or {}).items() if m in ("yes", "partly")}
        found = p.get("bills") or []
        out["unjudged"].append(sum(1 for b in found[:10] if b not in (row.get("bills") or {})))
        r = recall_at(found, [b for b, g in grades.items() if g == 2])
        if r is not None:
            out["recall@10"].append(r)
        n = ndcg_at(found, grades)
        if n is not None:
            out["ndcg@10"].append(n)
    unjudged = out.pop("unjudged")
    return {**{k: (round(sum(v) / len(v), 3), len(v)) if isinstance(v, list) and v else v for k, v in out.items()},
            # Results no label rates count as irrelevant; run --judge first.
            "unjudged_in_top10": sum(unjudged)}


def _haiku_json(client, prompt, max_tokens=2000, tries=2):
    """Haiku's JSON answer. Asked again once when it is not JSON (4 of 218
    labels on the first run wrote a bare HB 1 inside an object)."""
    import re
    for attempt in range(tries):
        msg = client.messages.create(model=HAIKU, max_tokens=max_tokens, messages=[{"role": "user", "content": prompt}])
        text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
        m = re.search(r"\{.*\}", text, re.S)
        try:
            return json.loads(m.group(0)) if m else {}
        except ValueError:
            if attempt == tries - 1:
                raise


def _candidates(cur, kind, surface, bill=None, backend=None):
    """Graph nodes a named entity could be: [{id, name, detail}], at most 6.
    People, committees and lobbying organizations go through the graph's own
    lookups, which know aliases ("Bernie Sanders" is Bernard Sanders) and
    committee names ("Senate Judiciary" is the Committee on the Judiciary)."""
    import graph
    if kind == "bill" and bill:
        t, n = (bill.get("type") or "").lower().replace(".", "").replace(" ", ""), bill.get("number")
        if bill.get("state"):
            cur.execute("""SELECT instrument_id, title, session FROM bill_doc WHERE jurisdiction = %s
                           AND bill_type = %s AND number = %s ORDER BY session DESC LIMIT 6""",
                        (graph.state_div(bill["state"].lower()), t, str(n)))
        else:
            cur.execute("""SELECT instrument_id, title, congress FROM bill_doc WHERE jurisdiction = %s
                           AND bill_type = %s AND number = %s AND (%s::int IS NULL OR congress = %s::int)
                           ORDER BY congress DESC LIMIT 6""",
                        ("ocd-division/country:us", t, str(n), bill.get("congress"), bill.get("congress")))
        return [{"id": i, "name": title, "detail": str(when)} for i, title, when in cur.fetchall()]
    if not surface.strip():
        return []
    backend = backend or graph.pg_backend()
    if kind == "person":
        return [{"id": p["id"], "name": p["name"], "detail": f"{p.get('seat') or ''} {p.get('jurisdiction') or ''}".strip()}
                for p in backend["persons"](surface)[:6]]
    if kind == "committee":
        return [{"id": c["id"], "name": c["name"], "detail": (c.get("props") or {}).get("chamber") or ""}
                for c in backend["committees"](surface)[:6]]
    if kind == "organization":
        return [{"id": o["id"], "name": o["name"], "detail": "lobbying"} for o in backend["orgs"](surface)[:6]]
    if kind == "place":
        cur.execute("""SELECT id, name, coalesce(props->>'jurisdiction', '') FROM graph_node
                       WHERE kind = 'jurisdiction' AND name ILIKE %s ORDER BY length(name) LIMIT 6""", (f"%{surface}%",))
        return [{"id": i, "name": nm, "detail": d} for i, nm, d in cur.fetchall()]
    return []


_LABEL_PROMPT = """You label questions for a civic search engine over US federal and state legislation,
legislators, votes, lobbying and campaign money, and some county governments.

Question: {question!r}
{reader}
Return ONLY JSON:
{{"intent": one of {intents},
  "jurisdiction": "federal" | "state" | "local" | "all",
  "about_me": true if the question asks about the reader's own representatives or place,
  "entities": [{{"surface": words in the question, "kind": "person"|"bill"|"committee"|"organization"|"place",
                 "bill": for a bill only, {{"congress": number or null, "state": two letters or null, "type": "hr"|"s"|"hb"|..., "number": number}}
                 (for a named act, give its bill if you know it, e.g. the Inflation Reduction Act is congress 117 hr 5376)}}],
  "search_text": the topic words a bill search should use, or "",
  "known_bills": up to 5 bills you know answer the question, each {{"congress": number or null, "state": two letters or null, "type": ..., "number": ...}}}}
Intents: find_bills (bills on a topic), person_record (one person's record), how_voted (how a person voted),
who_voted (who voted which way on a bill), money (lobbying or campaign money around a bill or person),
compare_states (which states did something), copied_bills (the same bill text in several places),
seat_holder (who holds or held a seat), committee, organization (what an organization lobbied or funded),
explain_law (one named or numbered bill or law), local_place (a county or city government),
elections, off_topic."""

_JUDGE_PROMPT = """Question: {question!r}

1. For each entity, pick the graph node the question means, or null if none fits:
{entities}
2. Rate each bill for the question: "yes" (it is what the question asks about), "partly" (related,
not the answer), or "no". Judge by the title and summary only.
{bills}
Return ONLY JSON: {{"entities": {{"<entity index>": "<node id or null>"}}, "bills": {{"<bill id>": "yes"|"partly"|"no"}}}}"""


def label_question(client, cur, row, pool_size=20):
    """One labelled row. Two Haiku calls: what the question is and names; then
    which graph node each name is and which pooled bills answer it. The pool
    is today's index (federal, and the named state's) plus the bills the
    labeller names; a later engine that finds a relevant bill outside it is
    not credited, which undercounts it."""
    import graph
    from search import bill_index
    from agents.ledger_agent import extract_state
    q, reader = row["question"], row.get("reader")
    head = _haiku_json(client, _LABEL_PROMPT.format(
        question=q, intents=list(INTENTS),
        reader=f"The reader lives in {reader['state']}, congressional district {reader['cd']}.\n" if reader else ""))
    ents = []
    for e in (head.get("entities") or [])[:5]:
        ents.append({"surface": e.get("surface") or "", "kind": e.get("kind"),
                     "candidates": _candidates(cur, e.get("kind"), e.get("surface") or "", e.get("bill"))})
    text = head.get("search_text") or q
    pool = [bill_key(r) for r in bill_index.search(text, None, pool_size)]
    st = (extract_state(q) or (reader or {}).get("state") or "").lower()
    if st and st.upper() in graph.loaded_states():
        pool += [bill_key(r) for r in bill_index.search(text, None, pool_size, jurisdiction=graph.state_div(st))]
    elif head.get("jurisdiction") in ("state", "all") or head.get("intent") in ("compare_states", "copied_bills"):
        # "Which states passed right to repair" names no state: every state's bills.
        pool += [bill_key(r) for r in bill_index.search(text, None, pool_size, jurisdiction=None)]
    pool += [c["id"] for e in ents if e["kind"] == "bill" for c in e["candidates"]]
    # Bills the labeller knows, found in the index by number: without them
    # the pool is only what today's search finds, and a bill it misses (the
    # 2024 TikTok law, passed inside H.R. 815) could never count as relevant.
    for b in (head.get("known_bills") or [])[:5]:
        pool += [c["id"] for c in _candidates(cur, "bill", "", b)[:1]]
    pool = list(dict.fromkeys(b for b in pool if b))
    cards = {}
    if pool:
        cur.execute("""SELECT instrument_id, title, coalesce(policy_area, ''), is_law, left(coalesce(summary, ''), 280)
                       FROM bill_doc WHERE instrument_id = ANY(%s)""", (pool,))
        cards = {i: f"{t} [{p}{', law' if law else ''}] {s}" for i, t, p, law, s in cur.fetchall()}
    judged = {"entities": {}, "bills": {}}
    if cards or any(e["candidates"] for e in ents):
        judged = _haiku_json(client, _JUDGE_PROMPT.format(
            question=q,
            entities="\n".join(f"{i}: {e['surface']!r} ({e['kind']}) — " + "; ".join(
                f"{c['id']} = {c['name']} {c['detail']}" for c in e["candidates"]) for i, e in enumerate(ents)
                if e["candidates"]) or "(none)",
            bills="\n".join(f"{b}: {c}" for b, c in cards.items()) or "(none)"), max_tokens=4000)
    for i, e in enumerate(ents):
        pick = (judged.get("entities") or {}).get(str(i))
        e["node_id"] = pick if pick in {c["id"] for c in e["candidates"]} else None
        e.pop("candidates")
    return {**row, "held_out": held_out(q), "labelled_by": "haiku", "intent": head.get("intent"),
            "jurisdiction": head.get("jurisdiction"), "about_me": bool(head.get("about_me")),
            "entities": ents, "bills": {b: m for b, m in (judged.get("bills") or {}).items() if b in cards}}


def build_labels(path):
    """Label every eval question once; rows already in `path` are kept, so a
    stopped run resumes and owner corrections are never overwritten."""
    import os
    import anthropic
    from correspondence.db import _get_pool
    done = {r["question"]: r for r in json.load(open(path))} if os.path.exists(path) else {}
    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    questions = eval_questions()

    def save():
        # Every labelled row, new or kept: a resumed run once saved only the
        # rows before its last new one and dropped the 63 kept after it.
        have = {r["question"]: r for r in rows}
        with open(path, "w") as f:
            json.dump([have.get(q["question"]) or done[q["question"]] for q in questions
                       if q["question"] in have or q["question"] in done], f, indent=1)

    rows = []
    for row in questions:
        if row["question"] in done:
            rows.append(done[row["question"]])
            continue
        try:
            with _get_pool().connection() as conn, conn.cursor() as cur:
                rows.append(label_question(client, cur, row))
        except Exception as e:                                  # noqa: BLE001 - recorded, not hidden
            print(f"  label failed: {row['question']!r}: {type(e).__name__}: {e}", flush=True)
            continue
        print(f"{len(rows):3d} {rows[-1]['intent']:<14} {len(rows[-1]['bills']):2d} bills  {row['question']}", flush=True)
        save()
    save()
    return rows


def judge_missing(path, preds_path):
    """Judge every bill a run returned that the labels have not rated, and add
    it to the labels (labelled_by stays per row; each bill rating is Haiku's).
    Without this a system is scored only on the bills the pool happened to
    hold: today's search, held to the last two Congresses, returned recent
    student-loan bills nobody had judged and scored zero for it. Run before
    --score for every system scored."""
    import os
    import anthropic
    from correspondence.db import _get_pool
    labels, preds = json.load(open(path)), json.load(open(preds_path))
    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    added = 0
    for row in labels:
        missing = [b for b in (preds.get(row["question"]) or {}).get("bills") or [] if b not in row["bills"]]
        if not missing:
            continue
        with _get_pool().connection() as conn, conn.cursor() as cur:
            cur.execute("""SELECT instrument_id, title, coalesce(policy_area, ''), is_law, left(coalesce(summary, ''), 280)
                           FROM bill_doc WHERE instrument_id = ANY(%s)""", (missing,))
            cards = {i: f"{t} [{p}{', law' if law else ''}] {s}" for i, t, p, law, s in cur.fetchall()}
        if not cards:
            continue
        got = _haiku_json(client, _JUDGE_PROMPT.format(question=row["question"], entities="(none)",
                                                       bills="\n".join(f"{b}: {c}" for b, c in cards.items())))
        new = {b: m for b, m in (got.get("bills") or {}).items() if b in cards and m in ("yes", "partly", "no")}
        row["bills"].update(new)
        added += len(new)
        with open(path, "w") as f:
            json.dump(labels, f, indent=1)
    print(f"judged {added} bill(s) the labels had not rated")
    return added

def baseline(labels, base, timeout=120):
    """Today's /ledger for each labelled question: {question: {intent,
    entities, bills}}. The reader's state goes as the home state; today's
    routes take no district."""
    import graph
    preds = {}
    for row in labels:
        reader = row.get("reader") or {}
        try:
            # /ledger allows 20 a minute from one address; a fast system hit
            # it and its 429s once scored as empty answers (2026-09-29).
            for _ in range(6):
                r = requests.post(f"{base}/ledger", json={"question": row["question"], "state_code": reader.get("state")},
                                  timeout=timeout)
                if r.status_code != 429:
                    break
                time.sleep(20)
            r.raise_for_status()
            lines = [json.loads(ln) for ln in r.text.splitlines() if ln.strip()]
        except Exception as e:                                  # noqa: BLE001 - recorded as a miss
            preds[row["question"]] = {"intent": None, "entities": [], "bills": [], "error": str(e)[:200]}
            continue
        plate = next((ln for ln in lines if ln.get("section") == "plate"), {})
        member = next((ln for ln in lines if ln.get("section") == "member"), {})
        answer = next((ln for ln in lines if ln.get("section") == "answer"), {})
        ents, bills = [], [bill_key(s) for s in plate.get("stories") or []]
        if plate.get("plate") == "bill":
            bills = [bill_key({"congress": plate.get("congress"), "type": plate.get("bill_type"), "number": plate.get("number")})]
        elif plate.get("plate") == "state_bill":
            bills = [bill_key({"state": plate.get("state_code"), "session": plate.get("session"),
                               "type": plate.get("bill_type"), "number": plate.get("number")})]
        bills += [x.get("item_id") for x in plate.get("rows") or [] if str(x.get("item_id") or "").startswith("instrument/")]
        ents += [b for b in bills[:1] if plate.get("plate") in ("bill", "state_bill")]
        bio = ((member.get("member") or {}).get("bioguide_id"))
        if bio:
            ents.append(graph.node_id("person", f"bioguide/{bio}"))
        # The graph search says what it understood; before it, the plate's
        # kind was the only intent there was.
        if answer.get("intent"):
            ents += [e["node_id"] for e in answer.get("understood") or [] if e.get("node_id")]
        preds[row["question"]] = {"intent": answer.get("intent") or intent_of(plate), "entities": ents,
                                  "bills": list(dict.fromkeys(b for b in bills if b))[:10]}
        print(f"  {preds[row['question']]['intent']:<14} {len(preds[row['question']]['bills']):2d}  {row['question']}", flush=True)
    return preds


def main():
    if len(sys.argv) > 2 and sys.argv[1] == "--label":
        rows = build_labels(sys.argv[2])
        print(f"wrote {sys.argv[2]}: {len(rows)} labelled questions")
        return
    if len(sys.argv) > 3 and sys.argv[1] == "--baseline":
        base = sys.argv[sys.argv.index("--base") + 1] if "--base" in sys.argv else "http://127.0.0.1:8000"
        preds = baseline(json.load(open(sys.argv[2])), base)
        with open(sys.argv[3], "w") as f:
            json.dump(preds, f, indent=1)
        print(f"wrote {sys.argv[3]}: {len(preds)} answers")
        return
    if len(sys.argv) > 3 and sys.argv[1] == "--judge":
        judge_missing(sys.argv[2], sys.argv[3])
        return
    if len(sys.argv) > 3 and sys.argv[1] == "--score":
        labels, preds = json.load(open(sys.argv[2])), json.load(open(sys.argv[3]))
        print(json.dumps({"held_out": score_eval(labels, preds), "all": score_eval(labels, preds, False)}, indent=1))
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
