"""OpenFEC (api.open.fec.gov) client — federal campaign finance.

Powers two surfaces:
- Election detail "Money & momentum": per-candidate receipts, disbursements,
  cash-on-hand, and the PAC-vs-individual split for FEDERAL races (House,
  Senate, President). FEC has no state or local data, so non-federal contests
  return None and the section stays hidden.
- Bill "Money behind the sponsors": the FEC campaign totals for a bill's
  sponsor(s), shown beside "Who's pushing this." We deliberately do NOT claim
  a direct entity->sponsor contribution link — resolving a lobbying entity to
  its PAC(s) is the OpenSecrets normalization layer (planned). This is the
  honest, side-by-side version: what was lobbied next to what the sponsor raised.

Key: FEC_API_KEY in .env (free at api.data.gov). Falls back to DEMO_KEY
(rate-limited ~30/hr, 50/day) so dev works without one. Results are disk-cached
via correspondence.db, guarded so a missing DB degrades to uncached rather than
broken — finance totals move at most daily.
"""

import os
import re
import sys
import json
import pathlib
import datetime
import zipfile
import requests

FEC_BASE = "https://api.open.fec.gov/v1"
FEC_API_KEY = (os.getenv("FEC_API_KEY") or "DEMO_KEY").strip()

_session = requests.Session()
_session.headers.update({
    "Accept": "application/json",
    "User-Agent": "NosPopuli/1.0 (civic transparency; nospopuli.org)",
})

_TTL = 24 * 3600  # finance totals update at most daily

# Bill type -> the chamber its sponsor sits in (sponsor of an H.R. is a
# Representative; sponsor of an S. is a Senator). Used to scope the FEC search.
_BILL_TYPE_OFFICE = {
    "hr": "H", "hres": "H", "hjres": "H", "hconres": "H",
    "s": "S", "sres": "S", "sjres": "S", "sconres": "S",
}

# Full state name -> USPS code. FEC's candidate search only accepts the 2-letter
# code, but callers hand us either form (a member's state is the full name).
_STATE_TO_USPS = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
    "district of columbia": "DC", "florida": "FL", "georgia": "GA", "hawaii": "HI",
    "idaho": "ID", "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN", "mississippi": "MS",
    "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY",
    "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK",
    "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
    "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT",
    "vermont": "VT", "virginia": "VA", "washington": "WA", "west virginia": "WV",
    "wisconsin": "WI", "wyoming": "WY", "puerto rico": "PR",
}


def _usps(state):
    """A 2-letter USPS code from either a code ('VA') or a full name ('Virginia')."""
    s = (state or "").strip()
    if not s:
        return None
    if len(s) == 2:
        return s.upper()
    return _STATE_TO_USPS.get(s.lower(), s.upper())


def _cache_get(key):
    try:
        from correspondence.db import get_disk_cache
        return get_disk_cache(key, _TTL)
    except Exception:
        return None


def _cache_set(key, value):
    try:
        from correspondence.db import set_disk_cache
        set_disk_cache(key, value)
    except Exception:
        pass


def _get(path, params):
    p = dict(params)
    p["api_key"] = FEC_API_KEY
    resp = _session.get(f"{FEC_BASE}/{path}", params=p, timeout=20)
    resp.raise_for_status()
    return resp.json()


def office_for_bill_type(bill_type):
    return _BILL_TYPE_OFFICE.get((bill_type or "").lower())


def office_from_contest(office_str, level):
    """Map a Google Civic contest to an FEC office code, or None if the race is
    not federal (FEC only covers House/Senate/President)."""
    s = (office_str or "").lower()
    lv = " ".join(level or []).lower()
    if "president" in s:
        return "P"
    # Federal legislative offices name the chamber explicitly; a bare "senate"
    # or "house" without "state"/level=country would be a state legislature, so
    # require a federal signal.
    federal = ("u.s." in s or "united states" in s or "federal" in lv or "country" in lv)
    if not federal:
        return None
    if "senate" in s or "senator" in s:
        return "S"
    if "house" in s or "representative" in s or "congress" in s:
        return "H"
    return None


def clean_name(raw):
    """Normalize a name into a plain 'First Last' the FEC full-text search can
    match. Congress.gov hands us sponsor names like 'Rep. Arrington, Jodey C.
    [R-TX-19]' — the honorific, the [party-state-district] tag, and the middle
    initial all defeat FEC search, and the 'LAST, First' order needs flipping."""
    n = (raw or "").strip()
    n = re.sub(r"\[[^\]]*\]", "", n)                       # drop [R-TX-19]
    n = re.sub(r"^(rep|sen|del|res|hon|senator|representative|commissioner|dr|mr|mrs|ms)\.?\s+",
               "", n, flags=re.I)                          # drop honorific
    n = n.strip().strip(",").strip()
    if "," in n:                                           # 'LAST, First M.' -> 'First M. LAST'
        last, rest = n.split(",", 1)
        n = f"{rest.strip()} {last.strip()}"
    n = re.sub(r"\b[A-Za-z]\.", " ", n)                    # drop middle initials 'C.'
    n = re.sub(r"\b(jr|sr|ii|iii|iv)\b\.?", "", n, flags=re.I)
    return re.sub(r"\s+", " ", n).strip()


def _last_name(name):
    """Best-effort surname from either 'First Last' or 'LAST, First' forms."""
    n = (name or "").strip()
    if "," in n:
        return n.split(",")[0].strip().lower()
    parts = re.sub(r"\b(jr|sr|ii|iii|iv)\b\.?", "", n, flags=re.I).split()
    return parts[-1].lower() if parts else ""


def _pick_candidate(results, name, office):
    """Choose the best candidate match: surname must agree; prefer the office we
    expected and the most recent election year."""
    surname = _last_name(name)
    scored = []
    for r in results:
        if surname and surname not in (r.get("name", "").lower()):
            continue
        years = r.get("election_years") or [0]
        scored.append((
            r.get("office") == office,        # office match first
            max(years),                       # then most recent
            r,
        ))
    if not scored:
        return None
    scored.sort(key=lambda t: (t[0], t[1]), reverse=True)
    return scored[0][2]


# -------------------------------------------------------- local reads
#
# Since Phase 6 (2026-09-26) the finance routes read the bulk snapshot, not
# the API: derived/fec/candidates-<cycle>.json (every candidate's committee
# totals and sources; each member's PACs) beside fec-<cycle>.json. Donor
# employers (member_industries) stay live: they need the multi-GB indiv file.

_CAND_FILES = {}


def _candidates(cycle):
    """One cycle's candidates file, reread when it changes; None if absent."""
    import graph
    path = graph.data_path("fec_candidates", cycle=cycle)
    try:
        mtime = path.stat().st_mtime_ns
    except FileNotFoundError:
        return None
    hit = _CAND_FILES.get(int(cycle))
    if not hit or hit[0] != mtime:
        hit = _CAND_FILES[int(cycle)] = (mtime, json.loads(path.read_text()))
    return hit[1]


def _cycles():
    """The cycles on disk, newest first."""
    import graph
    return sorted((int(p.stem.split("-")[-1]) for p in graph.data_glob("fec_candidates")), reverse=True)


def finance_row(cid, rec, cycle):
    """A candidates-file record in the shape the API path returned. Pure."""
    return {
        "candidate_id": cid, "name": rec["name"],
        "party": _PARTY_FULL.get(rec["party"], rec["party"]),
        "office": _OFFICE_FULL.get(rec["office"], rec["office"]),
        "incumbent": _ICI_FULL.get(rec["ici"], ""), "cycle": cycle,
        **{k: rec.get(k) for k in ("receipts", "disbursements", "cash_on_hand", "from_individuals",
                                   "from_pacs", "indiv_itemized", "indiv_unitemized", "from_party",
                                   "self_funding", "coverage_end_date")},
        "fec_url": f"https://www.fec.gov/data/candidate/{cid}/",
        "source": "FEC bulk: committee_summary for the principal campaign committee",
    }


def candidate_finance(name, state=None, office=None, cycle=None):
    """A candidate's latest totals by name, from the candidates files: the
    surname must agree, then the expected office, then the latest election
    year (as the API search was used). None when nothing agrees."""
    query = clean_name(name) or (name or "").strip()
    if not query:
        return None
    usps = _usps(state)
    for cy in ([int(cycle)] if cycle else _cycles()):
        data = _candidates(cy)
        if not data:
            continue
        results = [{"candidate_id": cid, "name": r["name"], "office": r["office"],
                    "election_years": [r["election_year"]]}
                   for cid, r in data["candidates"].items()
                   if (not office or r["office"] == office) and (not usps or office == "P" or r["state"] == usps)]
        cand = pick_local_candidate(results, query, office)
        if cand:
            return finance_row(cand["candidate_id"], data["candidates"][cand["candidate_id"]], cy)
    return None


def pick_local_candidate(results, name, office):
    """_pick_candidate over a whole cycle's file, which the API's full-name
    search used to narrow first: a typed first name must agree when any
    same-surname candidate's does (Bob → Robert), and two candidates still
    equal on office and year are no answer, never a guess. Pure."""
    from agents.member_search_agent import NICKNAMES
    surname = _last_name(name)
    given = [w for w in re.findall(r"[a-z]+", (name or "").lower()) if w != surname]
    rows = [r for r in results if surname and surname in (r.get("name") or "").lower()]
    if given:
        def first_names(r):
            return set(re.findall(r"[a-z]+", (r.get("name") or "").lower().split(",", 1)[-1]))
        agree = [r for r in rows if any(w in first_names(r) or NICKNAMES.get(w) in first_names(r) for w in given)]
        rows = agree or rows
    if not rows:
        return None
    key = lambda r: (r.get("office") == office, max(r.get("election_years") or [0]))
    top = max(key(r) for r in rows)
    best = [r for r in rows if key(r) == top]
    return best[0] if len({r["candidate_id"] for r in best}) == 1 else None


def _member(name, state, office):
    """The legislator a name means, among those who served in the state and
    chamber given; None unless exactly one fits."""
    import graph
    from agents.member_search_agent import match_members
    usps = _usps(state)
    term = {"S": "sen", "H": "rep"}.get(office)
    pool = [leg for leg in graph.legislators()
            if (not usps or any(t.get("state") == usps for t in leg.get("terms", [])))
            and (not term or any(t.get("type") == term for t in leg.get("terms", [])))]
    member, _ = match_members(clean_name(name) or name or "", pool)
    return member


def member_finance(name, state, chamber, cycle=None):
    """FEC campaign finance for a federal member, scoped by chamber: the
    member's own FEC ids from the legislators file, never a name search of
    the FEC's candidates (two Rick Scotts). Latest cycle with money."""
    c = (chamber or "").lower()
    office = "S" if "senate" in c or "senator" in c else "H" if "house" in c or "rep" in c else None
    leg = _member(name, state, office)
    if not leg:
        return None
    ids = [i for i in leg["id"].get("fec", []) if not office or i.startswith(office)]
    for cy in ([int(cycle)] if cycle else _cycles()):
        data = _candidates(cy) or {"candidates": {}}
        for cid in ids:
            if cid in data["candidates"]:
                return finance_row(cid, data["candidates"][cid], cy)
    return None


def sponsor_finance(sponsor_name, state, bill_type, cycle=None):
    """FEC campaign totals for a bill sponsor, scoped by the bill's chamber."""
    office = office_for_bill_type(bill_type)
    return member_finance(sponsor_name, state, {"S": "senate", "H": "house"}.get(office), cycle)


def _member_pacs(candidate_id, cycle):
    data = _candidates(cycle) or {}
    return {n: a for n, a in (data.get("pacs") or {}).get(candidate_id, [])}




def _principal_committee(cid):
    """Live, for member_industries only."""
    try:
        r = _get(f"candidate/{cid}/committees/", {"designation": "P", "per_page": 5})
        cms = r.get("results", [])
        return cms[0]["committee_id"] if cms else None
    except Exception as e:
        print(f"[FEC] committee lookup {cid}: {e}")
        return None


def _pac_title(name):
    """Title-case an ALL-CAPS FEC committee name, keeping PAC/JFC uppercase."""
    t = " ".join(w.capitalize() for w in (name or "").split())
    for a in ("Pac", "Jfc", "Llc", "Inc", "Pc"):
        t = re.sub(rf"\b{a}\b", a.upper(), t)
    return t


def top_pac_contributors(candidate_id, cycle, candidate_name=None, limit=8):
    """Named PACs that gave to a member over the cycle and the one before,
    ranked. [{name, amount}]. The PACs' own filings (pas2, 24K): what each
    PAC reported giving, which does not always equal what the campaign
    reported receiving."""
    if not candidate_id or not cycle:
        return []
    pacs = {}
    for cy in (int(cycle), int(cycle) - 2):
        for n, a in _member_pacs(candidate_id, cy).items():
            pacs[n] = pacs.get(n, 0.0) + a
    return [{"name": _pac_title(n), "amount": round(a, 2)}
            for n, a in sorted(pacs.items(), key=lambda kv: kv[1], reverse=True)][:limit]


def member_pac_interests(candidate_id, cycle, candidate_name=None, limit=12):
    """A member's PAC money grouped by the interest each PAC represents — the
    generalized "who funds this candidate," from factual PAC identity (industry,
    cause, or political vehicle), never a guess about individual donors. Spans
    the cycle and the one before: a senator's PAC money lands in their
    election cycle. Returns {"cycle", "total", "interests": [{interest,
    total, share, top: [names]}]}."""
    if not candidate_id or not cycle:
        return {"cycle": cycle, "interests": []}
    agg = {}
    for cy in (int(cycle), int(cycle) - 2):
        for n, a in _member_pacs(candidate_id, cy).items():
            agg[n] = agg.get(n, 0.0) + a
    if not agg:
        return {"cycle": cycle, "total": 0, "interests": []}

    from money import industry_classifier
    labels = industry_classifier.classify_pacs(list(agg))
    buckets = {}
    for name, amt in agg.items():
        interest = labels.get(name.upper()) or "Other"
        b = buckets.setdefault(interest, {"total": 0.0, "pacs": []})
        b["total"] += amt
        b["pacs"].append((name, amt))

    total = sum(v["total"] for v in buckets.values()) or 1
    rows = []
    for interest, b in sorted(buckets.items(), key=lambda kv: kv[1]["total"], reverse=True):
        top = [_pac_title(n) for n, _ in sorted(b["pacs"], key=lambda x: x[1], reverse=True)[:4]]
        rows.append({"interest": interest, "total": round(b["total"], 2),
                     "share": round(b["total"] / total, 3), "top": top})
    return {"cycle": cycle, "total": round(total, 2), "interests": rows[:limit],
            "source": "FEC bulk pas2, 24K contributions filed by the PAC"}


# Employer strings that carry no industry — donors' non-jobs. FEC is full of
# them (they're the biggest "employers" by dollars); they'd swamp any industry
# ranking, so they're dropped before classification.
_NON_INDUSTRY = {
    "NOT EMPLOYED", "RETIRED", "SELF", "SELF EMPLOYED", "SELF-EMPLOYED",
    "NONE", "N/A", "NA", "NULL", "HOMEMAKER", "UNEMPLOYED", "NOT APPLICABLE",
    "INFORMATION REQUESTED", "REQUESTED", "NOT PROVIDED", "", "NONE LISTED",
}


def top_employers(candidate_id, cycle, limit=40):
    """Top donor employers for a candidate (FEC by_employer aggregate, scoped to
    the member's committee), minus the non-employer buckets. [{employer, total}]."""
    if not candidate_id or not cycle:
        return []
    cm = _principal_committee(candidate_id)
    if not cm:
        return []
    try:
        data = _get("schedules/schedule_a/by_employer/", {
            "committee_id": cm, "cycle": int(cycle), "sort": "-total", "per_page": 100})
    except Exception as e:
        print(f"[FEC] by_employer {candidate_id}: {e}")
        return []
    out = []
    for r in data.get("results", []):
        emp = (r.get("employer") or "").strip()
        if emp.upper() in _NON_INDUSTRY:
            continue
        tot = r.get("total") or 0
        if tot > 0:
            out.append({"employer": emp, "total": round(tot, 2)})
        if len(out) >= limit:
            break
    return out


def _industries_for_cycle(candidate_id, cycle):
    """Aggregate a cycle's donor employers into {industry: dollars}, plus the
    total dollars that landed in a *recognized* industry (the signal strength)."""
    emps = top_employers(candidate_id, cycle, 40)
    if not emps:
        return {}, 0.0
    from money import industry_classifier
    labels = industry_classifier.classify([e["employer"] for e in emps])
    agg = {}
    for e in emps:
        ind = labels.get(e["employer"].upper()) or "Other"
        agg[ind] = agg.get(ind, 0.0) + e["total"]
    classified = sum(v for k, v in agg.items() if k != "Other")
    return agg, classified


def member_industries(candidate_id, cycle, limit=10):
    """Estimated industry breakdown of a member's individual donors — the
    OpenSecrets-style rollup, reconstructed from raw FEC by classifying each
    donor employer (industry_classifier) and summing dollars by industry.

    A current cycle is often too early to be meaningful, so we also look at the
    prior cycle and show whichever has more classified money. Returns
    {"cycle": <used>, "industries": [{industry, total, share}]}."""
    if not candidate_id or not cycle:
        return {"cycle": cycle, "industries": []}
    ck = f"fec:ind:v3:{candidate_id}:{cycle}"
    cached = _cache_get(ck)
    if cached is not None:
        return cached

    agg_c, cls_c = _industries_for_cycle(candidate_id, int(cycle))
    agg_p, cls_p = _industries_for_cycle(candidate_id, int(cycle) - 2)
    agg, used = (agg_p, int(cycle) - 2) if cls_p > cls_c else (agg_c, int(cycle))

    if not agg:
        out = {"cycle": used, "industries": []}
        _cache_set(ck, out)
        return out

    total = sum(agg.values()) or 1
    # Recognized industries lead; the unclassified remainder sits at the bottom
    # so it doesn't crowd out the real signal.
    other = agg.pop("Other", 0.0)
    rows = [{"industry": k, "total": round(v, 2), "share": round(v / total, 3)}
            for k, v in sorted(agg.items(), key=lambda kv: kv[1], reverse=True)][:limit]
    if other > 0:
        rows.append({"industry": "Unclassified employers",
                     "total": round(other, 2), "share": round(other / total, 3)})
    out = {"cycle": used, "industries": rows}
    _cache_set(ck, out)
    return out


def race_candidates(office, state, cycle, limit=10):
    """Every FEC-registered candidate for a federal race who reported money,
    from the cycle's candidates file, largest receipts first — used when we
    have no candidate roster from elsewhere, so a "U.S. Senate (VA)" page can
    populate straight from the FEC. `office` is 'S'/'H'/'P'; `state` is
    required for House/Senate, ignored for President. [] when none."""
    if office not in ("S", "H", "P") or not cycle:
        return []
    data = _candidates(cycle)
    if not data:
        return []
    usps = (state or "").upper()
    out = [finance_row(cid, r, int(cycle)) for cid, r in data["candidates"].items()
           if r["office"] == office and r["status"] == "C" and r["election_year"] == int(cycle)
           and (office == "P" or r["state"] == usps) and (r["receipts"] or r["disbursements"])]
    out.sort(key=lambda f: f.get("receipts") or 0, reverse=True)
    return out[:limit]


# ------------------------------------------------------------- bulk files
#
# The offline path: FEC's own quarterly/cycle bulk files instead of the
# rate-limited API, for a full snapshot_fec-shaped run without a key. Reads
# five `|`-delimited files for one cycle — weball (candidate totals), cn
# (candidates, for CAND_PCC), ccl (candidate<->committee linkage), cm
# (committees), pas2 (itemized committee-to-candidate contributions, the
# giving side of Schedule A line 11C) — and writes the exact
# fec-<cycle>.json shape snapshot_fec does, so graph.py's readers
# (build_money, fec_detail, the /ledger "funds" answer) don't know which
# path built the file.

FEC_BULK_BASE = "https://www.fec.gov/files/bulk-downloads"

# cn/ccl/cm each publish a header file at .../data_dictionaries/<name>_header_file.csv
# (fetched below); weball's is NOT there (checked 2026-09-26) — its 30 columns are
# pinned here from fec.gov/campaign-finance-data/all-candidates-file-description/,
# and verified against a real download (30 pipe-fields, CVG_END_DT 07/15/2026 for
# S6VA00093 matching data/fec-2026.json's coverage_end_date for the same member).
_WEBALL_COLS = (
    "CAND_ID", "CAND_NAME", "CAND_ICI", "PTY_CD", "CAND_PTY_AFFILIATION",
    "TTL_RECEIPTS", "TRANS_FROM_AUTH", "TTL_DISB", "TRANS_TO_AUTH",
    "COH_BOP", "COH_COP", "CAND_CONTRIB", "CAND_LOANS", "OTHER_LOANS",
    "CAND_LOAN_REPAY", "OTHER_LOAN_REPAY", "DEBTS_OWED_BY", "TTL_INDIV_CONTRIB",
    "CAND_OFFICE_ST", "CAND_OFFICE_DISTRICT", "SPEC_ELECTION", "PRIM_ELECTION",
    "RUN_ELECTION", "GEN_ELECTION", "GEN_ELECTION_PRECENT",
    "OTHER_POL_CMTE_CONTRIB", "POL_PTY_CONTRIB", "CVG_END_DT",
    "INDIV_REFUNDS", "CMTE_REFUNDS",
)
_CN_COLS = ("CAND_ID", "CAND_NAME", "CAND_PTY_AFFILIATION", "CAND_ELECTION_YR",
            "CAND_OFFICE_ST", "CAND_OFFICE", "CAND_OFFICE_DISTRICT", "CAND_ICI",
            "CAND_STATUS", "CAND_PCC", "CAND_ST1", "CAND_ST2", "CAND_CITY",
            "CAND_ST", "CAND_ZIP")
_CCL_COLS = ("CAND_ID", "CAND_ELECTION_YR", "FEC_ELECTION_YR", "CMTE_ID",
             "CMTE_TP", "CMTE_DSGN", "LINKAGE_ID")
_CM_COLS = ("CMTE_ID", "CMTE_NM", "TRES_NM", "CMTE_ST1", "CMTE_ST2", "CMTE_CITY",
            "CMTE_ST", "CMTE_ZIP", "CMTE_DSGN", "CMTE_TP", "CMTE_PTY_AFFILIATION",
            "CMTE_FILING_FREQ", "ORG_TP", "CONNECTED_ORG_NM", "CAND_ID")
_PAS2_COLS = ("CMTE_ID", "AMNDT_IND", "RPT_TP", "TRANSACTION_PGI", "IMAGE_NUM",
              "TRANSACTION_TP", "ENTITY_TP", "NAME", "CITY", "STATE", "ZIP_CODE",
              "EMPLOYER", "OCCUPATION", "TRANSACTION_DT", "TRANSACTION_AMT",
              "OTHER_ID", "CAND_ID", "TRAN_ID", "FILE_NUM", "MEMO_CD",
              "MEMO_TEXT", "SUB_ID")

# Every zip holds exactly one `|`-delimited member; its internal filename
# doesn't follow the stem (pas226.zip's member is itpas2.txt, not
# pas226.txt), so _bulk_rows takes whatever single file is inside rather
# than assuming a name.


def _bulk_dir(cycle):
    import graph  # lazy: graph.py's module-level sys.path/harness import is
                   # unwanted weight for callers of the rest of this file
    d = graph.DATA_DIR / "raw" / "fec" / str(cycle)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _bulk_manifest(cycle):
    p = _bulk_dir(cycle) / "manifest.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return {}  # a corrupt manifest just re-downloads everything, not fatal


def _download_bulk(name, cycle, manifest, errors, remote=None):
    """One bulk zip, conditional on the Last-Modified we recorded last time.
    Writes to a `.part` file and only `os.replace`s it in on a full 200 —
    a network error partway through must never clobber a good file with a
    partial one. A 304 or a failed refresh silently keeps what's on disk
    (the data is still correct, just possibly stale); a failed *first*
    download with nothing on disk is fatal for the whole run, because a
    snapshot missing a file would silently under-report every member."""
    yy = str(cycle)[-2:]
    remote = remote or f"{name}{yy}.zip"
    path = f"/files/bulk-downloads/{cycle}/{remote}"
    url = FEC_BULK_BASE + f"/{cycle}/{remote}"
    dest = _bulk_dir(cycle) / (f"{name}.zip" if remote.endswith(".zip") else f"{name}.csv")
    headers = {}
    prior = manifest.get(name, {})
    if prior.get("last_modified") and dest.exists():
        headers["If-Modified-Since"] = prior["last_modified"]
    try:
        resp = _session.get(url, headers=headers, timeout=120)
    except Exception as e:
        errors.append(f"{type(e).__name__}: {path}")
        if dest.exists():
            return dest
        raise RuntimeError(f"bulk download failed with no cached copy: {path}") from e
    if resp.status_code == 304:
        return dest
    if resp.status_code != 200:
        errors.append(f"HTTP {resp.status_code}: {path}")
        if dest.exists():
            return dest
        raise RuntimeError(f"bulk download failed with no cached copy: {path}")
    part = dest.with_name(dest.name + ".part")
    part.write_bytes(resp.content)
    os.replace(part, dest)  # atomic: a reader never sees a half-written zip
    lm = resp.headers.get("Last-Modified")
    if lm:
        manifest[name] = {"last_modified": lm}
    return dest


def _bulk_rows(zip_path, cols):
    """Every row of the one member in a bulk zip, as a dict keyed by the
    pinned column order. Bulk files are latin-1, not utf-8 — a name like
    "O'BRIEN" round-trips, but a French or Spanish donor name would mojibake
    under utf-8's strict decoder and raise instead of just reading oddly."""
    with zipfile.ZipFile(zip_path) as z:
        text = z.read(z.namelist()[0]).decode("latin-1")
    return [dict(zip(cols, line.split("|"))) for line in text.splitlines() if line]


def _f(s):
    """A bulk money field to float; blank fields are common (no loans, no
    self-funding) and mean zero, not missing."""
    try:
        return float(s) if s not in (None, "") else 0.0
    except ValueError:
        return 0.0


def _cvg_end_iso(mmddyyyy):
    """CVG_END_DT is 'MM/DD/YYYY'; snapshot_fec's coverage_end_date is the
    API's isoformat() of a midnight datetime — matched here so a reader
    can't tell which path produced the field."""
    if not mmddyyyy:
        return None
    try:
        m, d, y = mmddyyyy.split("/")
        return datetime.datetime(int(y), int(m), int(d)).isoformat()
    except ValueError:
        return None


def bulk_principal_committees(ccl_rows, cn_rows, cycle):
    """cand_id -> principal committee id, from the ccl row with
    CMTE_DSGN=='P' whose FEC_ELECTION_YR matches this cycle (a senator not
    up until 2028 still carries a 2026-cycle linkage row; CAND_ELECTION_YR
    is the candidate's own next election, not this file's cycle, so
    filtering on it would drop off-cycle senators entirely).

    Not observed in the 2026 file, but if more than one 'P' row survives for
    a candidate, the higher LINKAGE_ID (FEC's own filing order) wins — a
    stated tie-break beats a silent one.

    Falls back to cn's own CAND_PCC for any candidate ccl has no row for at
    all: in the real 2026 files, 8 sitting members (e.g. Andy Barr,
    H0KY06104) have zero ccl rows of any cycle, yet cn.txt's CAND_PCC
    matches the API-built fec-2026.json's committee_id for every one of
    them — ccl's linkage table is the more precise source when it has a
    row, but it isn't always current."""
    by_cand = {}
    for r in ccl_rows:
        if r["CMTE_DSGN"] != "P" or r["FEC_ELECTION_YR"] != str(cycle):
            continue
        cur = by_cand.get(r["CAND_ID"])
        if cur is None or r["LINKAGE_ID"] > cur["LINKAGE_ID"]:
            by_cand[r["CAND_ID"]] = r
    out = {cid: r["CMTE_ID"] for cid, r in by_cand.items()}
    for r in cn_rows:
        if r["CAND_ID"] not in out and r["CAND_PCC"]:
            out[r["CAND_ID"]] = r["CAND_PCC"]
    return out


def bulk_committee_names(cm_rows):
    return {r["CMTE_ID"]: r["CMTE_NM"] for r in cm_rows}


def bulk_totals(weball_rows):
    """cand_id -> the same `totals` dict snapshot_fec builds from
    committee/<id>/totals/, mapped from weball's columns. The one real
    difference from the API path: weball is scoped to the CANDIDATE (every
    authorized committee summed together), while committee/<id>/totals/ is
    scoped to the single principal COMMITTEE. For a candidate with only one
    authorized committee (true of most sitting members) the two coincide;
    for one who also runs a joint fundraising or leadership PAC as a
    separate authorized committee, weball's totals run higher."""
    out = {}
    for r in weball_rows:
        out[r["CAND_ID"]] = {
            "receipts": _f(r["TTL_RECEIPTS"]),
            "disbursements": _f(r["TTL_DISB"]),
            "last_cash_on_hand_end_period": _f(r["COH_COP"]),
            "individual_contributions": _f(r["TTL_INDIV_CONTRIB"]),
            "other_political_committee_contributions": _f(r["OTHER_POL_CMTE_CONTRIB"]),
            "coverage_end_date": _cvg_end_iso(r["CVG_END_DT"]),
        }
    return out


def bulk_top_pacs(pas2_rows, committee_id, candidate_name, cm_names, limit=None):
    """graph.top_pacs's ranking, fed from the giving side instead of the
    receiving side: pas2 rows are each PAC's own itemization of a 24K
    contribution (line 11C, per the pas2 header file) it made, so a row
    with OTHER_ID == `committee_id` is one contribution *to* that
    committee, and CMTE_ID is the giver. Reuses graph.top_pacs rather than
    reimplementing its conduit/self-name exclusions.

    Two decisions, made against the real 2026 pas2 file (961 24K rows to
    Warner's committee, 95 of them AMNDT_IND=='A'):
    - Amendments are kept as filed. No (CMTE_ID, TRAN_ID) pair in that file
      paired an 'N' with an 'A' — the file's few same-key duplicates were
      both 'N' (a contribution and its later reversal, e.g. +2500/-2500),
      and those correctly net to zero when summed, so there is nothing to
      collapse.
    - Rows with MEMO_CD == 'X' are dropped (6 rows / $16,000 in that
      committee): FEC's memo convention means the dollars are already
      itemized elsewhere (e.g. a joint fundraising committee's transfer,
      restated at the receiving end), and counting a memo row would
      double-count them."""
    import graph
    receipts = [{"contributor_name": cm_names.get(r["CMTE_ID"], r["CMTE_ID"]),
                 "contributor_committee_id": r["CMTE_ID"],
                 "contribution_receipt_amount": _f(r["TRANSACTION_AMT"])}
                for r in pas2_rows
                if r["OTHER_ID"] == committee_id and r["TRANSACTION_TP"] == "24K"
                and r["MEMO_CD"] != "X"]
    return graph.top_pacs(receipts, candidate_name, **({"limit": limit} if limit else {}))


# FEC party codes as the API spelled them. A primary roster filters on the
# word "democratic" or "republican", so Minnesota's DFL must carry it.
_PARTY_FULL = {"DEM": "DEMOCRATIC PARTY", "REP": "REPUBLICAN PARTY", "DFL": "DEMOCRATIC-FARMER-LABOR",
               "IND": "INDEPENDENT", "LIB": "LIBERTARIAN PARTY", "GRE": "GREEN PARTY",
               "CON": "CONSTITUTION PARTY", "REF": "REFORM PARTY", "NPA": "NO PARTY AFFILIATION",
               "UN": "UNAFFILIATED", "OTH": "OTHER", "W": "WRITE-IN", "NNE": "NONE", "UNK": "UNKNOWN"}
_OFFICE_FULL = {"H": "House", "S": "Senate", "P": "President"}
_ICI_FULL = {"I": "Incumbent", "C": "Challenger", "O": "Open seat"}


def _ymd_iso(yyyymmdd):
    """committee_summary's dates are YYYYMMDD; the API gave isoformat()."""
    try:
        return datetime.datetime.strptime(yyyymmdd, "%Y%m%d").isoformat()
    except (TypeError, ValueError):
        return None


def bulk_composition(summary_rows):
    """committee id -> where its money came from, from FEC's committee
    summary file: the same split the API's committee totals gave (small
    unitemized gifts, itemized individuals, PACs, party, the candidate's
    own money), which weball does not carry. Pure."""
    out = {}
    for r in summary_rows:
        out[r["CMTE_ID"]] = {
            "receipts": _f(r["TTL_RECEIPTS"]), "disbursements": _f(r["TTL_DISB"]),
            "cash_on_hand": _f(r["COH_COP"]), "from_individuals": _f(r["INDV_CONTB"]),
            "indiv_itemized": _f(r["INDV_ITEM_CONTB"]), "indiv_unitemized": _f(r["INDV_UNITEM_CONTB"]),
            "from_pacs": _f(r["OTH_CMTE_CONTB"]), "from_party": _f(r["PTY_CMTE_CONTB"]),
            "self_funding": _f(r["CAND_CNTB"]), "coverage_end_date": _ymd_iso(r.get("CVG_END_DT")),
        }
    return out


def bulk_candidates(cn_rows, principal, composition):
    """Every candidate of the cycle whose principal committee filed a
    summary: who they are (cn) and their committee's money (composition).
    A candidate with no filed summary has nothing to show and is left out.
    Pure."""
    out = {}
    for r in cn_rows:
        cm = principal.get(r["CAND_ID"])
        money = composition.get(cm) if cm else None
        if not money:
            continue
        out[r["CAND_ID"]] = {
            "name": r["CAND_NAME"], "party": r["CAND_PTY_AFFILIATION"], "office": r["CAND_OFFICE"],
            "state": r["CAND_OFFICE_ST"], "district": r["CAND_OFFICE_DISTRICT"], "ici": r["CAND_ICI"],
            "status": r["CAND_STATUS"], "election_year": int(r["CAND_ELECTION_YR"] or 0),
            "committee_id": cm, **money}
    return out


def snapshot_fec_bulk(cycle, out_path=None):
    """snapshot_fec's output, built from FEC bulk downloads instead of the
    API: no key, no 60/minute limit, one pass instead of resumed calls.
    Not resumable and doesn't need to be — a cycle's bulk files are already
    a complete snapshot, not a page at a time. Idempotent: re-running with
    refreshed files just overwrites."""
    import graph
    errors = []
    manifest = _bulk_manifest(cycle)
    paths = {}
    try:
        for name in ("weball", "cn", "ccl", "cm", "pas2"):
            paths[name] = _download_bulk(name, cycle, manifest, errors)
        paths["committee_summary"] = _download_bulk("committee_summary", cycle, manifest, errors,
                                                    remote=f"committee_summary_{cycle}.csv")
    finally:
        (_bulk_dir(cycle) / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True))

    weball = _bulk_rows(paths["weball"], _WEBALL_COLS)
    cn = _bulk_rows(paths["cn"], _CN_COLS)
    ccl = _bulk_rows(paths["ccl"], _CCL_COLS)
    cm = _bulk_rows(paths["cm"], _CM_COLS)
    pas2 = _bulk_rows(paths["pas2"], _PAS2_COLS)

    import csv
    import io
    summary = list(csv.DictReader(io.StringIO(paths["committee_summary"].read_bytes().decode("latin-1"))))

    principal = bulk_principal_committees(ccl, cn, cycle)
    cm_names = bulk_committee_names(cm)
    totals_by_cand = bulk_totals(weball)
    candidates = bulk_candidates(cn, principal, bulk_composition(summary))
    member_pacs = {}

    legs = json.loads(graph.data_path("public", name=graph.LEGISLATORS_SOURCE).read_text())
    today = datetime.date.today().isoformat()
    source = f"FEC bulk downloads, cycle {cycle}: " + ", ".join(
        f"{n} ({manifest.get(n, {}).get('last_modified', 'unknown date')})"
        for n in ("weball", "cn", "ccl", "cm", "pas2"))
    snap = {"meta": {"cycle": cycle, "started": today, "source": source, "errors": errors},
            "members": {}}

    for leg in legs:
        bio = leg["id"]["bioguide"]
        name = leg["name"].get("official_full") or leg["name"].get("last")
        cids = graph.fec_candidate_ids(leg)
        rec = {"name": name, "candidate_ids": cids, "complete": False, "fetched": today}
        placed = False
        for cid in cids:
            cm_id = principal.get(cid)
            if not cm_id:
                continue
            rec.update(candidate_id=cid, committee_id=cm_id, committee_name=cm_names.get(cm_id, cm_id))
            t = totals_by_cand.get(cid)
            if t:
                rec["totals"] = t
            else:
                rec["gap"] = f"no weball totals row for {cid} in {cycle}"
            rows, total, n = bulk_top_pacs(pas2, cm_id, name, cm_names, limit=10 ** 6)
            rec["top_pacs"], rec["pac_total"], rec["pac_receipts"] = rows[:graph.FEC_TOP_PACS], total, n
            member_pacs[cid] = [[r["name"], r["amount"]] for r in rows]
            placed = True
            break
        if not placed:
            rec["gap"] = ("no FEC candidate id for this chamber in the legislators file"
                          if not cids else f"no principal campaign committee for {cycle}")
        rec["complete"] = True
        snap["members"][bio] = rec

    snap["meta"]["updated"] = datetime.datetime.now().isoformat(timespec="seconds")
    snap["meta"]["counts"] = {
        "members": len(snap["members"]),
        "complete": sum(1 for m in snap["members"].values() if m["complete"]),
        "with_committee": sum(1 for m in snap["members"].values() if m.get("committee_id")),
    }
    path = pathlib.Path(out_path) if out_path else graph.data_path("fec", cycle=cycle)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.part")
    tmp.write_text(json.dumps(snap, separators=(",", ":")))
    os.replace(tmp, path)  # same atomic-write guarantee as the bulk zips
    # Every candidate, and each member's whole PAC list: what the finance
    # routes read. Its own file, untracked, so the tracked fec file stays
    # the size the graph needs.
    if not out_path:
        cpath = graph.data_path("fec_candidates", cycle=cycle)
        ctmp = cpath.with_name(cpath.name + ".part")
        ctmp.write_text(json.dumps({"meta": {"cycle": cycle, "source": source, "updated": snap["meta"]["updated"],
                                             "counts": {"candidates": len(candidates), "members_with_pacs": len(member_pacs)}},
                                    "candidates": candidates, "pacs": member_pacs}, separators=(",", ":")))
        os.replace(ctmp, cpath)
    return snap["meta"]


if __name__ == "__main__" and len(sys.argv) > 1 and sys.argv[1] == "bulk":
    # python -m sources.fec_client bulk <cycle> — writes derived/fec/fec-<cycle>.json
    print(json.dumps(snapshot_fec_bulk(int(sys.argv[2])), indent=1))

elif __name__ == "__main__":
    # Smoke test — hits the live API with whatever key is in the environment.
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))
    FEC_API_KEY = (os.getenv("FEC_API_KEY") or "DEMO_KEY").strip()
    for nm, st, off in [("Mark Warner", "VA", "S"),
                        ("Suhas Subramanyam", "VA", "H"),
                        ("Nancy Pelosi", "CA", "H")]:
        r = candidate_finance(nm, st, off)
        if r:
            print(f"{r['name']:28} {r['office']:22} cycle {r['cycle']} "
                  f"receipts ${(r['receipts'] or 0):,.0f}  PAC ${(r['from_pacs'] or 0):,.0f}")
        else:
            print(f"{nm}: no match")
