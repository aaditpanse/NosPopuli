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
import datetime
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


def candidate_finance(name, state=None, office=None, cycle=None):
    """Resolve a candidate to their latest FEC finance totals.

    Returns a dict (candidate_id, name, party, office, incumbent, cycle,
    receipts, disbursements, cash_on_hand, from_individuals, from_pacs, fec_url)
    or None when there's no confident federal match. `office` is 'H'/'S'/'P'.
    """
    name = (name or "").strip()
    if not name:
        return None
    query = clean_name(name) or name
    usps = _usps(state)

    ck = f"fec:cand:v2:{query.lower()}:{usps or ''}:{office or ''}:{cycle or 'latest'}"
    cached = _cache_get(ck)
    if cached is not None:
        return cached or None  # cached {} (a confirmed miss) -> None

    base = {"per_page": 8, "sort": "-election_years"}
    if office:
        base["office"] = office
    if usps and office != "P":
        base["state"] = usps

    def _search(q):
        try:
            return _get("candidates/search/", {**base, "q": q}).get("results", [])
        except Exception as e:
            print(f"[FEC] candidate search error {q!r}: {e}")
            return []

    # Full name first; then fall back to the surname alone, which catches
    # nicknames FEC files under (Thom vs Thomas, Bob vs Robert).
    cand = _pick_candidate(_search(query), query, office)
    if not cand:
        surname = _last_name(query)
        if surname and surname != query.lower():
            cand = _pick_candidate(_search(surname), query, office)
    if not cand:
        _cache_set(ck, {})  # remember the miss so we don't re-query all day
        return None

    out = _finance_dict(cand, _latest_totals(cand["candidate_id"], cycle))
    _cache_set(ck, out)
    return out


def _latest_totals(cid, cycle=None):
    """The candidate's most-recent real election-cycle totals row. FEC returns
    one row per cycle plus, for some candidates, a null-cycle career-aggregate
    row that sorts first — we want the latest cycle, not the lifetime total."""
    try:
        tp = {"per_page": 20, "sort": "-cycle"}
        if cycle:
            tp["cycle"] = cycle
        rows = _get(f"candidate/{cid}/totals/", tp).get("results") or []
    except Exception as e:
        print(f"[FEC] totals error {cid}: {e}")
        return {}
    dated = [r for r in rows if r.get("cycle")]
    return max(dated, key=lambda r: r["cycle"]) if dated else (rows[0] if rows else {})


def _finance_dict(cand, t):
    cid = cand["candidate_id"]
    return {
        "candidate_id": cid,
        "name": cand.get("name", ""),
        "party": cand.get("party_full") or cand.get("party") or "",
        "office": cand.get("office_full") or cand.get("office") or "",
        "incumbent": cand.get("incumbent_challenge_full") or "",
        "cycle": t.get("cycle"),
        "receipts": t.get("receipts"),
        "disbursements": t.get("disbursements"),
        "cash_on_hand": t.get("last_cash_on_hand_end_period"),
        "from_individuals": t.get("individual_itemized_contributions"),
        "from_pacs": t.get("other_political_committee_contributions"),
        # Composition — where the money comes from. FEC reports these cleanly;
        # named donors/industries do not (self-reported employer fields are
        # unusable), so that's the OpenSecrets layer, not this.
        "indiv_itemized": t.get("individual_itemized_contributions"),
        "indiv_unitemized": t.get("individual_unitemized_contributions"),
        "from_party": t.get("political_party_committee_contributions"),
        "self_funding": t.get("candidate_contribution"),
        "fec_url": f"https://www.fec.gov/data/candidate/{cid}/",
    }


def sponsor_finance(sponsor_name, state, bill_type, cycle=None):
    """FEC campaign totals for a bill sponsor, scoped by the bill's chamber."""
    return candidate_finance(
        sponsor_name, state=state, office=office_for_bill_type(bill_type), cycle=cycle
    )


def member_finance(name, state, chamber, cycle=None):
    """FEC campaign finance for a sitting federal member, scoped by chamber."""
    c = (chamber or "").lower()
    office = "S" if "senate" in c or "senator" in c else "H" if "house" in c or "rep" in c else None
    return candidate_finance(name, state=state, office=office, cycle=cycle)


_CONDUITS = ("ACTBLUE", "WINRED")  # pass-throughs for individual donors, not org donors


def _principal_committee(cid):
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


def _pac_totals(candidate_id, cycle, candidate_name=None):
    """Raw {pac_name: dollars} of committee (PAC) contributions to a candidate.
    FEC Schedule A line F3-11C = "contributions from other political committees"
    (actual PACs; the broad contributor_type=committee also returned entity_type
    =ORG bank/processor rows). Drops the candidate's own committees, conduits
    (ActBlue/WinRed), and joint-fundraising vehicles bearing the candidate name."""
    name_tokens = {t.upper() for t in clean_name(candidate_name).split() if len(t) > 2}
    cm = _principal_committee(candidate_id)
    if not cm:
        return {}
    try:
        data = _get("schedules/schedule_a/", {
            "committee_id": cm, "two_year_transaction_period": int(cycle),
            "line_number": "F3-11C", "sort": "-contribution_receipt_amount",
            "per_page": 100,
        })
    except Exception as e:
        print(f"[FEC] pac contributors {candidate_id}: {e}")
        return {}
    agg = {}
    for r in data.get("results", []):
        nm = (r.get("contributor_name") or "").strip()
        up = nm.upper()
        if not nm or r.get("entity_type") == "CAN":
            continue
        if any(c in up for c in _CONDUITS):
            continue
        if any(t in up for t in name_tokens):
            continue
        amt = r.get("contribution_receipt_amount") or 0
        if amt > 0:
            agg[nm] = agg.get(nm, 0.0) + amt
    return agg


def top_pac_contributors(candidate_id, cycle, candidate_name=None, limit=8):
    """Named PAC/committee contributors to a candidate, ranked. [{name, amount}]."""
    if not candidate_id or not cycle:
        return []
    ck = f"fec:pac:v3:{candidate_id}:{cycle}"
    cached = _cache_get(ck)
    if cached is not None:
        return cached
    agg = _pac_totals(candidate_id, cycle, candidate_name)
    rows = [{"name": _pac_title(k), "amount": round(v, 2)}
            for k, v in sorted(agg.items(), key=lambda kv: kv[1], reverse=True)][:limit]
    _cache_set(ck, rows)
    return rows


def member_pac_interests(candidate_id, cycle, candidate_name=None, limit=12):
    """A member's PAC money grouped by the interest each PAC represents — the
    generalized "who funds this candidate," from factual PAC identity (industry,
    cause, or political vehicle), never a guess about individual donors. Returns
    {"cycle", "total", "interests": [{interest, total, share, top: [names]}]}."""
    if not candidate_id or not cycle:
        return {"cycle": cycle, "interests": []}
    ck = f"fec:pacint:v2:{candidate_id}:{cycle}"
    cached = _cache_get(ck)
    if cached is not None:
        return cached

    agg = _pac_totals(candidate_id, cycle, candidate_name)
    if not agg:
        out = {"cycle": cycle, "total": 0, "interests": []}
        _cache_set(ck, out)
        return out

    import industry_classifier
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
    # Push "Other"/leadership-style buckets after the real interests? Keep them —
    # colleague money is real; but sort by dollars so the biggest lead.
    out = {"cycle": cycle, "total": round(total, 2), "interests": rows[:limit]}
    _cache_set(ck, out)
    return out


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
    import industry_classifier
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
    """Every FEC-registered candidate for a federal race, with finance — used
    when we have no candidate roster from elsewhere (Google Civic voterinfo is
    gone), so a "U.S. Senate (VA)" page can populate straight from the FEC.

    `office` is 'S'/'H'/'P'; `state` is required for House/Senate, ignored for
    President. Returns finance dicts sorted by receipts, only those with money
    reported (drops paper filers), or [] when the race has none.
    """
    if office not in ("S", "H", "P") or not cycle:
        return []
    ck = f"fec:race:v1:{office}:{(state or '').upper()}:{cycle}"
    cached = _cache_get(ck)
    if cached is not None:
        return cached

    params = {"office": office, "election_year": int(cycle),
              "candidate_status": "C", "per_page": 30, "sort": "-first_file_date"}
    if state and office != "P":
        params["state"] = state.upper()
    try:
        results = _get("candidates/", params).get("results", [])
    except Exception as e:
        print(f"[FEC] race lookup error {office}/{state}/{cycle}: {e}")
        return []

    out = []
    for cand in results:
        fin = _finance_dict(cand, _latest_totals(cand["candidate_id"], cycle))
        if fin.get("receipts") or fin.get("disbursements"):
            out.append(fin)
    out.sort(key=lambda f: f.get("receipts") or 0, reverse=True)
    out = out[:limit]
    _cache_set(ck, out)
    return out


if __name__ == "__main__":
    # Smoke test — hits the live API with whatever key is in the environment.
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
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
