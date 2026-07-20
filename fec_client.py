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
