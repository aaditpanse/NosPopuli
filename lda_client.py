"""Senate LDA (Lobbying Disclosure Act) API client.

Wraps lda.senate.gov/api/v1 — public, no key required (rate-limited to ~15
requests/min anonymously; set LDA_API_KEY in .env to raise it). Powers the
Lobbying tab: entity search + per-entity spend / issue / lobbyist profiles.

Everything is disk_cached because filings only change quarterly, which also
keeps us comfortably under the anonymous rate limit — a cold profile is the
only thing that pays the multi-request aggregation cost.

Modeling notes:
- A lobbying FIRM (registrant) files on behalf of a CLIENT and reports
  `income` (what the client paid it). An in-house org files for itself and
  reports `expenses`. So a filing's spend is income OR expenses, never both.
- Client records are duplicated across registrations — the same company owns
  many client IDs — so profiles aggregate by name (fuzzy), not by a single id.
  True entity resolution is the later OpenSecrets normalization layer.
- Bills are named in the free-text activity `description`, not a structured
  field, so we regex them out for a "bills lobbied" list.
"""

import os
import re
import datetime
import requests
from dotenv import load_dotenv

# Load .env BEFORE importing correspondence.db — it reads SUPABASE_DB_URL at
# import time, so the env must be populated first (matters when this module is
# the entrypoint, e.g. `python lda_client.py seed`; in-app, api.py loads first).
load_dotenv()

from documentor_agent import log_action
from correspondence.db import get_disk_cache, set_disk_cache, record_bill_mentions

LDA_BASE = "https://lda.senate.gov/api/v1"
LDA_API_KEY = os.getenv("LDA_API_KEY", "")

_session = requests.Session()
_session.headers.update({
    "Accept": "application/json",
    "User-Agent": "NosPopuli/1.0 (civic transparency; nospopuli.org)",
})
if LDA_API_KEY:
    _session.headers.update({"Authorization": f"Token {LDA_API_KEY}"})

_SEARCH_TTL = 24 * 3600     # entity search — names are stable
_PROFILE_TTL = 12 * 3600    # entity profile — filings change quarterly

# LDA search matches the registered name as a substring, so an acronym only
# hits if the org registered under it (PhRMA, AARP do; AIPAC, the NRA don't).
# Expand well-known advocacy/industry acronyms to the name they actually file
# under, searched alongside the raw query. Ambiguous acronyms are omitted.
LOBBY_ALIASES = {
    "aipac": "American Israel Public Affairs Committee",
    "nra": "National Rifle Association",
    "aclu": "American Civil Liberties Union",
    "naacp": "National Association for the Advancement of Colored People",
    "api": "American Petroleum Institute",
    "nam": "National Association of Manufacturers",
    "bio": "Biotechnology Innovation Organization",
    "phrma": "Pharmaceutical Research and Manufacturers of America",
    "mpa": "Motion Picture Association",
    "mpaa": "Motion Picture Association",
    "riaa": "Recording Industry Association of America",
    "seiu": "Service Employees International Union",
    "uaw": "United Automobile Workers",
    "aft": "American Federation of Teachers",
    "nea": "National Education Association",
    "ama": "American Medical Association",
    "aha": "American Hospital Association",
    "nfib": "National Federation of Independent Business",
    "afscme": "American Federation of State, County and Municipal Employees",
    "uschamber": "Chamber of Commerce of the United States",
    "chamber of commerce": "Chamber of Commerce of the United States",
    "nab": "National Association of Broadcasters",
    "ncta": "NCTA - The Internet & Television Association",
    "eff": "Electronic Frontier Foundation",
    "hrc": "Human Rights Campaign",
    "nra of america": "National Rifle Association",
    "pcma": "Pharmaceutical Care Management Association",
    "ahip": "America's Health Insurance Plans",
    "sifma": "Securities Industry and Financial Markets Association",
    "aopa": "Aircraft Owners and Pilots Association",
    "nahb": "National Association of Home Builders",
    "nar": "National Association of Realtors",
    "cta": "Consumer Technology Association",
}
_MAX_PAGES_PER_YEAR = 4     # cap aggregation requests to stay under rate limit
_PAGE_SIZE = 25

_QUARTER = {
    "first_quarter": "Q1", "second_quarter": "Q2",
    "third_quarter": "Q3", "fourth_quarter": "Q4",
    "mid_year": "H1", "year_end": "H2",
}

# Match "H.R. 3684", "S 1582", "H.J.Res. 7", etc. in free text.
_BILL_RE = re.compile(
    r"\b(H\.?\s?R\.?|S\.?|H\.?\s?J\.?\s?Res\.?|S\.?\s?J\.?\s?Res\.?|"
    r"H\.?\s?Con\.?\s?Res\.?|S\.?\s?Con\.?\s?Res\.?|H\.?\s?Res\.?|S\.?\s?Res\.?)"
    r"\s*(\d{1,5})\b",
    re.IGNORECASE,
)

# Stripped-uppercase prefix → the app's canonical bill_type + display label.
_BILL_TYPES = {
    "HR": ("hr", "H.R."), "S": ("s", "S."),
    "HJRES": ("hjres", "H.J.Res."), "SJRES": ("sjres", "S.J.Res."),
    "HCONRES": ("hconres", "H.Con.Res."), "SCONRES": ("sconres", "S.Con.Res."),
    "HRES": ("hres", "H.Res."), "SRES": ("sres", "S.Res."),
}


def _congress_for_year(year):
    """Map a filing year to its Congress number (each Congress spans two years,
    starting in odd years: 2025-26 = 119th)."""
    try:
        y = int(year)
    except (TypeError, ValueError):
        return None
    return (y - 1789) // 2 + 1


def _spend(filing):
    for key in ("income", "expenses"):
        val = filing.get(key)
        if val:
            try:
                return float(val)
            except (TypeError, ValueError):
                pass
    return 0.0


def extract_bill_refs(text):
    """Pull bill mentions out of a free-text activity description, returned as
    (bill_type, number, display) tuples — e.g. ('hr', 3684, 'H.R.')."""
    out = set()
    for m in _BILL_RE.finditer(text or ""):
        prefix = re.sub(r"[^A-Za-z]", "", m.group(1)).upper()
        entry = _BILL_TYPES.get(prefix)
        if not entry:
            continue
        btype, label = entry
        out.add((btype, int(m.group(2)), label))
    return out


def _get(path, params):
    resp = _session.get(f"{LDA_BASE}/{path}", params=params, timeout=20)
    resp.raise_for_status()
    return resp.json()


def _search_one(term, results, seen):
    """Search registrants + clients for one term, appending de-duplicated hits
    (across kind + name) into `results`. Collapses the many duplicate client
    registrations that share a name — they resolve to the same name-keyed
    profile, so showing each is noise."""
    try:
        regs = _get("registrants/", {"registrant_name": term, "page_size": 10})
        for r in regs.get("results", []):
            key = ("registrant", r["name"].upper().strip())
            if key in seen:
                continue
            seen.add(key)
            results.append({
                "id": r["id"], "name": r["name"], "kind": "registrant",
                "subtitle": r.get("description") or "Lobbying firm",
                "state": r.get("state_display"),
            })
    except Exception as e:
        print(f"[LDA] registrant search error ({term!r}): {e}")

    try:
        cls = _get("clients/", {"client_name": term, "page_size": 25})
        for c in cls.get("results", []):
            key = ("client", c["name"].upper().strip())
            if key in seen:
                continue
            seen.add(key)
            results.append({
                "id": c["id"], "name": c["name"], "kind": "client",
                "subtitle": c.get("general_description") or "Lobbying client",
                "state": c.get("state_display"),
            })
    except Exception as e:
        print(f"[LDA] client search error ({term!r}): {e}")


def search_entities(query, limit=20):
    """Search registrants (lobbying firms) and clients by name. Known acronyms
    (AIPAC, NRA, …) are expanded to the full name they file under and searched
    alongside the raw query. Returns a combined, de-duplicated, ranked list of
    {id, name, kind, subtitle, state}."""
    q = (query or "").strip()
    if len(q) < 2:
        return []

    ck = f"lda:search:v2:{q.lower()}"
    cached = get_disk_cache(ck, _SEARCH_TTL)
    if cached is not None:
        return cached

    ql = q.lower()
    results, seen = [], set()
    _search_one(q, results, seen)

    alias = LOBBY_ALIASES.get(ql)
    if alias and alias.lower() != ql:
        _search_one(alias, results, seen)

    # Rank: names that begin with the raw query or its alias expansion first,
    # then shortest name (the canonical entity over long subsidiary variants).
    prefixes = tuple(p for p in (ql, (alias or "").lower()) if p)
    results.sort(key=lambda x: (not x["name"].lower().startswith(prefixes), len(x["name"])))
    results = results[:limit]
    set_disk_cache(ck, results)
    return results


def _fetch_entity_filings(kind, name, years):
    name_param = "client_name" if kind == "client" else "registrant_name"
    filings = []
    for yr in years:
        page = 1
        while page <= _MAX_PAGES_PER_YEAR:
            try:
                data = _get("filings/", {
                    name_param: name, "filing_year": yr,
                    "page": page, "page_size": _PAGE_SIZE, "ordering": "-dt_posted",
                })
            except Exception as e:
                print(f"[LDA] filings error {name} {yr} p{page}: {e}")
                break
            filings.extend(data.get("results", []))
            if not data.get("next"):
                break
            page += 1
    return filings


def _aggregate_profile(kind, name, filings, years):
    total = 0.0
    by_quarter, counterparties, issues, lobbyists, bill_refs = {}, {}, {}, {}, {}
    activities = []

    for f in filings:
        spend = _spend(f)
        total += spend

        congress = _congress_for_year(f.get("filing_year"))
        qlabel = f"{f.get('filing_year')} {_QUARTER.get(f.get('filing_period'), f.get('filing_period', ''))}".strip()
        by_quarter[qlabel] = by_quarter.get(qlabel, 0.0) + spend

        # Counterparty: for a client, the firms it hired; for a firm, its clients.
        cp_src = (f.get("registrant") if kind == "client" else f.get("client")) or {}
        cp = cp_src.get("name")
        if cp:
            counterparties[cp] = counterparties.get(cp, 0.0) + spend

        acts = f.get("lobbying_activities") or []
        for a in acts:
            code = a.get("general_issue_code")
            if code:
                slot = issues.setdefault(code, {"display": a.get("general_issue_code_display") or code, "count": 0})
                slot["count"] += 1
            for lb in (a.get("lobbyists") or []):
                p = lb.get("lobbyist") or {}
                nm = " ".join(filter(None, [p.get("first_name"), p.get("last_name")])).strip()
                if nm:
                    lobbyists[nm] = lobbyists.get(nm, 0) + 1
            for (btype, bnum, label) in extract_bill_refs(a.get("description")):
                if not congress:
                    continue
                key = (congress, btype, bnum, label)
                bill_refs[key] = bill_refs.get(key, 0) + 1

        if len(activities) < 12:
            desc = "; ".join(a.get("description", "") for a in acts if a.get("description"))[:300]
            activities.append({
                "period": qlabel,
                "counterparty": cp,
                "spend": spend,
                "issues": sorted({a.get("general_issue_code_display") for a in acts if a.get("general_issue_code_display")}),
                "description": desc,
            })

    def topn(d, n):
        return [{"name": k, "value": round(v, 2)} for k, v in
                sorted(d.items(), key=lambda kv: kv[1], reverse=True)[:n]]

    return {
        "kind": kind,
        "name": name,
        "years": years,
        "total_spend": round(total, 2),
        "filing_count": len(filings),
        "by_quarter": [{"quarter": k, "spend": round(v, 2)} for k, v in sorted(by_quarter.items())],
        "counterparties": topn(counterparties, 12),
        "issues": [{"code": k, "display": v["display"], "count": v["count"]}
                   for k, v in sorted(issues.items(), key=lambda kv: kv[1]["count"], reverse=True)][:12],
        "lobbyists": [k for k, _ in sorted(lobbyists.items(), key=lambda kv: kv[1], reverse=True)][:20],
        "bills_lobbied": [
            {"congress": c, "type": t, "number": n,
             "display": f"{label} {n}", "count": v}
            for (c, t, n, label), v in
            sorted(bill_refs.items(), key=lambda kv: kv[1], reverse=True)[:24]
        ],
        "activities": activities,
    }


def _bill_mention_rows(profile):
    """Flatten a profile's bills_lobbied into rows for the bill → entity index."""
    if not profile:
        return []
    return [
        {"congress": b["congress"], "bill_type": b["type"], "bill_number": b["number"],
         "entity_name": profile["name"], "entity_kind": profile["kind"],
         "mentions": b["count"], "entity_spend": profile["total_spend"]}
        for b in profile.get("bills_lobbied", [])
    ]


# Big cross-sector spenders used to seed the bill index so common bills have a
# "Who's pushing this" panel immediately, before organic entity views fill it in.
SEED_ENTITIES = [
    "Chamber of Commerce of the United States",
    "Pharmaceutical Research and Manufacturers of America",
    "American Medical Association",
    "American Hospital Association",
    "National Association of Realtors",
    "Blue Cross Blue Shield Association",
    "Lockheed Martin Corporation",
    "Boeing Company",
    "Meta Platforms",
    "Amazon.com Services",
    "Google",
    "Microsoft Corporation",
    "American Petroleum Institute",
    "Exxon Mobil Corporation",
    "Comcast Corporation",
    "Pfizer Inc.",
    "AARP",
    "American Bankers Association",
    "Business Roundtable",
    "National Association of Broadcasters",
]


def seed_lobbying_index(names=None, verbose=True):
    """Warm the bill → entity index by profiling major spenders (both kinds) and
    recording their bill mentions explicitly — works even for cached profiles."""
    names = names or SEED_ENTITIES
    total = 0
    for nm in names:
        for kind in ("client", "registrant"):
            try:
                p = get_entity_profile(kind, nm)
                rows = _bill_mention_rows(p)
                if rows:
                    record_bill_mentions(rows)
                    total += len(rows)
                    if verbose:
                        print(f"[SEED] {kind:10} {nm}: {len(rows)} bills, ${p['total_spend']:,.0f}")
            except Exception as e:
                print(f"[SEED] error {kind} {nm!r}: {e}")
    if verbose:
        print(f"[SEED] done — {total} (bill,entity) mentions recorded")
    return total


def get_entity_profile(kind, name, years=None):
    """Aggregate an entity's recent filings into a profile: total spend, spend
    per quarter, top counterparties, issue-area breakdown, lobbyists, bills
    lobbied, and recent activity. `kind` is 'client' or 'registrant'."""
    name = (name or "").strip()
    if not name or kind not in ("client", "registrant"):
        return None

    if years is None:
        y = datetime.date.today().year
        years = [y, y - 1]

    ck = f"lda:profile:v2:{kind}:{name.lower()}:{'-'.join(map(str, years))}"
    cached = get_disk_cache(ck, _PROFILE_TTL)
    if cached is not None:
        return cached

    filings = _fetch_entity_filings(kind, name, years)
    profile = _aggregate_profile(kind, name, filings, years)
    set_disk_cache(ck, profile)

    # Populate the reverse bill → entity index (powers the per-bill panel). This
    # is the lazy half: every profile viewed contributes its bill mentions.
    try:
        record_bill_mentions(_bill_mention_rows(profile))
    except Exception as e:
        print(f"[LDA] bill-mention index error: {e}")

    log_action(
        agent_name="lda",
        action="get_entity_profile",
        input_data={"kind": kind, "name": name, "years": years},
        output_data={"filings": len(filings), "spend": profile["total_spend"]},
    )
    return profile


if __name__ == "__main__":
    import sys
    # Load .env from this file's directory so the seed works regardless of cwd.
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
    from correspondence.db import init_db
    init_db()
    if len(sys.argv) > 1 and sys.argv[1] == "seed":
        seed_lobbying_index()
    else:
        print("usage: python lda_client.py seed")
