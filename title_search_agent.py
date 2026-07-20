import re
import json
import os
import requests
from datetime import datetime, timedelta
from dotenv import load_dotenv
from documentor_agent import log_action
from search_agent import parse_package_id

load_dotenv()

CONGRESS_API_KEY = os.getenv("CONGRESS_API_KEY")
GOVINFO_API_KEY = os.getenv("GovInfo_API_KEY")

CACHE_PATH = os.path.join(os.path.dirname(__file__), "popular_names_cache.json")
CACHE_MAX_AGE_DAYS = 7
SCRAPE_COOLDOWN_HOURS = 6  # don't retry a failed scrape for this long
SCRAPE_FAILURE_SENTINEL = "__scrape_failed__"

# Small hardcoded table for acts where Congress.gov title search is unreliable
# (popular name differs significantly from official title, or the source bill is
# very old and poorly indexed)
POPULAR_NAMES = {
    # Education
    "title ix":                {"congress": 92,  "type": "s",  "number": 659,  "title": "Education Amendments of 1972"},
    "title 9":                 {"congress": 92,  "type": "s",  "number": 659,  "title": "Education Amendments of 1972"},
    # FERPA — the "Buckley Amendment," enacted as part of the Education
    # Amendments of 1974 (Pub. L. 93-380, H.R. 69, 93rd Congress).
    "ferpa":                   {"congress": 93,  "type": "hr", "number": 69,   "title": "Education Amendments of 1974 (FERPA)"},
    "ferpa of 1974":           {"congress": 93,  "type": "hr", "number": 69,   "title": "Education Amendments of 1974 (FERPA)"},
    "family educational rights and privacy act": {"congress": 93, "type": "hr", "number": 69, "title": "Education Amendments of 1974 (FERPA)"},
    "buckley amendment":       {"congress": 93,  "type": "hr", "number": 69,   "title": "Education Amendments of 1974 (FERPA)"},
    # Civil Rights Act of 1964 titles
    "title vi":                {"congress": 88,  "type": "hr", "number": 7152, "title": "Civil Rights Act of 1964"},
    "title vii":               {"congress": 88,  "type": "hr", "number": 7152, "title": "Civil Rights Act of 1964"},
    "title 6":                 {"congress": 88,  "type": "hr", "number": 7152, "title": "Civil Rights Act of 1964"},
    "title 7":                 {"congress": 88,  "type": "hr", "number": 7152, "title": "Civil Rights Act of 1964"},
    # Fair Housing Act (Title VIII of Civil Rights Act of 1968)
    "title viii":              {"congress": 90,  "type": "hr", "number": 2516, "title": "Civil Rights Act of 1968 (Fair Housing Act)"},
    "title 8":                 {"congress": 90,  "type": "hr", "number": 2516, "title": "Civil Rights Act of 1968 (Fair Housing Act)"},
    "fair housing act":        {"congress": 90,  "type": "hr", "number": 2516, "title": "Civil Rights Act of 1968 (Fair Housing Act)"},
    # Other landmarks
    "voting rights act":       {"congress": 89,  "type": "hr", "number": 6400, "title": "Voting Rights Act of 1965"},
    "civil rights act":        {"congress": 88,  "type": "hr", "number": 7152, "title": "Civil Rights Act of 1964"},
    "civil rights act of 1968":{"congress": 90,  "type": "hr", "number": 2516, "title": "Civil Rights Act of 1968"},
    "equal pay act":           {"congress": 88,  "type": "hr", "number": 6060, "title": "Equal Pay Act of 1963"},
    "ada":                     {"congress": 101, "type": "s",  "number": 933,  "title": "Americans with Disabilities Act of 1990"},
    "americans with disabilities act": {"congress": 101, "type": "s", "number": 933, "title": "Americans with Disabilities Act of 1990"},
    # Recent acts where short popular names are too generic for relevance search
    "chips act":               {"congress": 117, "type": "hr", "number": 4346, "title": "CHIPS and Science Act"},
    "chips and science act":   {"congress": 117, "type": "hr", "number": 4346, "title": "CHIPS and Science Act"},
    "pact act":                {"congress": 117, "type": "hr", "number": 3967, "title": "Honoring our PACT Act of 2022"},
    "honoring our pact act":   {"congress": 117, "type": "hr", "number": 3967, "title": "Honoring our PACT Act of 2022"},
    "farm bill":               {"congress": 115, "type": "hr", "number": 2,    "title": "Agriculture Improvement Act of 2018"},
    "agriculture improvement act": {"congress": 115, "type": "hr", "number": 2, "title": "Agriculture Improvement Act of 2018"},
    # Landmark recent statutes
    "affordable care act":     {"congress": 111, "type": "hr", "number": 3590, "title": "Patient Protection and Affordable Care Act"},
    "obamacare":               {"congress": 111, "type": "hr", "number": 3590, "title": "Patient Protection and Affordable Care Act"},
    "dodd-frank":              {"congress": 111, "type": "hr", "number": 4173, "title": "Dodd-Frank Wall Street Reform and Consumer Protection Act"},
    "dodd frank":              {"congress": 111, "type": "hr", "number": 4173, "title": "Dodd-Frank Wall Street Reform and Consumer Protection Act"},
    # National security / surveillance landmarks
    "patriot act":             {"congress": 107, "type": "hr", "number": 3162, "title": "USA PATRIOT Act"},
    "usa patriot act":         {"congress": 107, "type": "hr", "number": 3162, "title": "USA PATRIOT Act"},
    "freedom act":             {"congress": 114, "type": "hr", "number": 2048, "title": "USA FREEDOM Act of 2015"},
    "usa freedom act":         {"congress": 114, "type": "hr", "number": 2048, "title": "USA FREEDOM Act of 2015"},
    # SAVE Act — "Safeguard American Voter Eligibility Act". Multiple bills use
    # the SAVE acronym (SMART Save Act, Healthcare Employees Save Act, etc.), so
    # GovInfo phrase search returns those alongside the voting bill. Pin the
    # voter-eligibility version explicitly. 119 HR 7296 is the standalone House
    # bill in the current Congress (S 1383 was a vehicle bill swap, see commit
    # e8ebb60). Hardcoding the current-Congress version surfaces it for the
    # plain "SAVE Act" query — readers searching for prior-Congress versions can
    # use the year-stripped fallback ("Safeguard American Voter Eligibility Act").
    "save act":                {"congress": 119, "type": "hr", "number": 7296, "title": "SAVE America Act"},
    "save america act":        {"congress": 119, "type": "hr", "number": 7296, "title": "SAVE America Act"},
    "safeguard american voter eligibility act": {"congress": 119, "type": "hr", "number": 7296, "title": "SAVE America Act"},
}

# ---------------------------------------------------------------------------
# Popular names cache — scraped from congress.gov/popular-names weekly
# ---------------------------------------------------------------------------

def _cache_is_stale():
    if not os.path.exists(CACHE_PATH):
        return True
    age = datetime.now() - datetime.fromtimestamp(os.path.getmtime(CACHE_PATH))
    return age > timedelta(days=CACHE_MAX_AGE_DAYS)

def _scrape_popular_names():
    from bs4 import BeautifulSoup

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "en-US,en;q=0.9",
    }

    try:
        response = requests.get(
            "https://www.congress.gov/popular-names",
            headers=headers,
            timeout=20,
        )
        if response.status_code != 200:
            print(f"[POPULAR NAMES] Scrape failed: HTTP {response.status_code}")
            return {}
    except Exception as e:
        print(f"[POPULAR NAMES] Scrape error: {e}")
        return {}

    soup = BeautifulSoup(response.text, "html.parser")
    cache = {}

    # The page renders a table where each row has: popular name | citation(s)
    # Citations look like "118 H.R. 1234" or "89 S. 456"
    bill_pattern = re.compile(
        r"(\d{2,3})\s+(H\.R\.|S\.|H\.J\.Res\.|S\.J\.Res\.|H\.Con\.Res\.|S\.Con\.Res\.|H\.Res\.|S\.Res\.)\s*(\d+)",
        re.IGNORECASE,
    )
    type_map = {
        "h.r.": "hr", "s.": "s", "h.j.res.": "hjres", "s.j.res.": "sjres",
        "h.con.res.": "hconres", "s.con.res.": "sconres", "h.res.": "hres", "s.res.": "sres",
    }

    for row in soup.select("table tr"):
        cells = row.find_all("td")
        if len(cells) < 2:
            continue

        name = cells[0].get_text(separator=" ", strip=True).lower()
        citation_text = cells[1].get_text(separator=" ", strip=True)

        match = bill_pattern.search(citation_text)
        if not match or not name:
            continue

        congress = int(match.group(1))
        bill_type = type_map.get(match.group(2).lower().replace(" ", ""), match.group(2).lower())
        number = int(match.group(3))

        cache[name] = {"congress": congress, "type": bill_type, "number": number}

    print(f"[POPULAR NAMES] Scraped {len(cache)} entries")
    return cache

def _load_popular_names_cache():
    if _cache_is_stale():
        # Check if we recently failed — avoid hammering a 403 source
        if os.path.exists(CACHE_PATH):
            try:
                with open(CACHE_PATH) as f:
                    existing = json.load(f)
                if existing.get(SCRAPE_FAILURE_SENTINEL):
                    failed_at = existing[SCRAPE_FAILURE_SENTINEL]
                    age_hours = (datetime.now() - datetime.fromisoformat(failed_at)).total_seconds() / 3600
                    if age_hours < SCRAPE_COOLDOWN_HOURS:
                        print(f"[POPULAR NAMES] Scrape failed {age_hours:.1f}h ago — skipping retry")
                        return {}
            except Exception:
                pass

        print("[POPULAR NAMES] Cache stale or missing — refreshing")
        data = _scrape_popular_names()
        if data:
            try:
                with open(CACHE_PATH, "w") as f:
                    json.dump(data, f)
            except Exception as e:
                print(f"[POPULAR NAMES] Failed to write cache: {e}")
        else:
            # Write a failure sentinel so we don't retry for SCRAPE_COOLDOWN_HOURS
            try:
                with open(CACHE_PATH, "w") as f:
                    json.dump({SCRAPE_FAILURE_SENTINEL: datetime.now().isoformat()}, f)
            except Exception:
                pass
            return {}

    if not os.path.exists(CACHE_PATH):
        return {}

    try:
        with open(CACHE_PATH) as f:
            data = json.load(f)
        # Don't expose the sentinel key as real cache entries
        return {k: v for k, v in data.items() if k != SCRAPE_FAILURE_SENTINEL}
    except Exception:
        return {}

# ---------------------------------------------------------------------------
# GovInfo phrase search — matches "may be cited as" preamble text
# ---------------------------------------------------------------------------

def _govinfo_phrase_search(act_name, limit=5, congress=None):
    """Phrase search on the GovInfo BILLS collection. Returns a list of distinct
    bills (deduped by congress/type/number — GovInfo indexes every print version
    of a bill separately) ordered by GovInfo's relevance score.

    When `congress` is given, restrict to bills from that Congress. Useful when
    the caller has a year hint and the original act would otherwise be drowned
    out by later reauthorizations whose preamble repeats the original's title."""
    q = f'"{act_name}" collection:BILLS'
    if congress is not None:
        q += f" congress:{congress}"
    payload = {
        "query": q,
        "pageSize": 25,  # over-fetch so dedup leaves room for `limit` distinct bills
        "offsetMark": "*",
        "sorts": [{"field": "score", "sortOrder": "DESC"}],
    }
    try:
        response = requests.post(
            "https://api.govinfo.gov/search",
            json=payload,
            params={"api_key": GOVINFO_API_KEY},
            timeout=15,
        )
        if response.status_code != 200:
            print(f"[TITLE SEARCH] GovInfo phrase search: HTTP {response.status_code}")
            return []
        raw = response.json().get("results", [])
    except Exception as e:
        print(f"[TITLE SEARCH] GovInfo phrase search error: {e}")
        return []

    out = []
    seen = set()
    for item in raw:
        parsed = parse_package_id(item.get("packageId", ""))
        if not parsed:
            continue
        key = (parsed["congress"], parsed["type"], parsed["number"])
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "congress": parsed["congress"],
            "type": parsed["type"],
            "number": parsed["number"],
            "title": item.get("title", ""),
            "date_issued": item.get("dateIssued", ""),
            "latest_action": "",
            "source": "govinfo_phrase",
            "is_original": False,
            "is_law": False,
            "law_number": None,
        })
        if len(out) >= limit:
            break
    return out

# ---------------------------------------------------------------------------
# Congress.gov listing scan — last-ditch fallback for very recent bills
# GovInfo hasn't ingested yet. Bounded by congress × type × date window.
# ---------------------------------------------------------------------------

_SCAN_STOPWORDS = {
    "act", "bill", "legislation", "law", "vote",
    "the", "of", "and", "for", "to", "a", "an", "in", "on", "with",
}


def _congress_gov_title_scan(named_entity, congresses=(119, 118), types=("hr", "s"),
                              limit=4, days_back=90):
    """When GovInfo's BILLS collection lags Congress.gov on a freshly introduced
    bill, scan Congress.gov listings for titles containing every distinctive
    keyword from the act name. Restricted to a recent date window so a single
    page per (congress, type) covers it and we don't paginate forever."""
    keywords = {
        w for w in named_entity.lower().split()
        if w not in _SCAN_STOPWORDS and len(w) > 3
    }
    if len(keywords) < 2:
        return []  # too generic — would match noise

    from datetime import datetime, timedelta
    since = (datetime.utcnow() - timedelta(days=days_back)).strftime("%Y-%m-%dT00:00:00Z")

    out = []
    seen = set()
    for congress in congresses:
        for btype in types:
            try:
                r = requests.get(
                    f"https://api.congress.gov/v3/bill/{congress}/{btype}",
                    params={
                        "api_key": CONGRESS_API_KEY,
                        "format": "json",
                        "limit": 250,
                        "fromDateTime": since,
                    },
                    timeout=15,
                )
            except Exception as e:
                print(f"[TITLE SEARCH] Listing scan error ({congress} {btype}): {e}")
                continue
            if r.status_code != 200:
                continue
            try:
                bills = r.json().get("bills", [])
            except Exception:
                continue
            for b in bills:
                title_lower = (b.get("title") or "").lower()
                if not all(kw in title_lower for kw in keywords):
                    continue
                key = (congress, btype, b.get("number"))
                if key in seen or not b.get("number"):
                    continue
                seen.add(key)
                out.append({
                    "congress": b.get("congress"),
                    "type": btype,
                    "number": b.get("number"),
                    "title": b.get("title", ""),
                    "date_issued": (b.get("latestAction") or {}).get("actionDate", ""),
                    "latest_action": (b.get("latestAction") or {}).get("text", ""),
                    "source": "congress_listing",
                    "is_original": False,
                    "is_law": False,
                    "law_number": None,
                })
                if len(out) >= limit:
                    return out
    return out


def _congress_gov_law_scan(named_entity, congresses=(119, 118), limit=4):
    """Scan Congress.gov's enacted-law listing for a named act whose title
    contains every distinctive keyword.

    This is the authoritative source for a law's *enrolled* title. Two failure
    modes make the GovInfo phrase search (Phase 2) miss recently-enacted acts:
    GovInfo's BILLS index lags Congress.gov by days/weeks on fresh enactments,
    and it indexes each print version under the title that version carried — so
    a bill introduced as "Housing for the 21st Century Act" that is enacted as
    the "21st Century ROAD to Housing Act" is only findable under its *old*
    name. The /law endpoint always reflects the current public-law title, so it
    catches these where every other phase returns nothing.

    The listing is bounded (roughly 100–300 laws per Congress) and sorted
    newest-first, so unlike the general bill-listing scan there's no 250-row
    truncation problem for the recent enactments this targets."""
    keywords = {
        w for w in named_entity.lower().split()
        if w not in _SCAN_STOPWORDS and len(w) > 3
    }
    if len(keywords) < 2:
        return []  # too generic — would match noise

    out = []
    seen = set()
    for congress in congresses:
        try:
            r = requests.get(
                f"https://api.congress.gov/v3/law/{congress}/pub",
                params={
                    "api_key": CONGRESS_API_KEY,
                    "format": "json",
                    "limit": 250,
                    "sort": "updateDate+desc",
                },
                timeout=15,
            )
        except Exception as e:
            print(f"[TITLE SEARCH] Law scan error ({congress}): {e}")
            continue
        if r.status_code != 200:
            continue
        try:
            laws = r.json().get("bills", [])
        except Exception:
            continue
        for law in laws:
            title_lower = (law.get("title") or "").lower()
            if not all(kw in title_lower for kw in keywords):
                continue
            btype = (law.get("type") or "").lower()
            number = law.get("number")
            lcongress = law.get("congress", congress)
            key = (lcongress, btype, number)
            if key in seen or not number:
                continue
            seen.add(key)
            raw_law_num = str((law.get("laws") or [{}])[0].get("number", ""))
            law_number = raw_law_num.split("-")[-1] if "-" in raw_law_num else raw_law_num
            out.append({
                "congress": lcongress,
                "type": btype,
                "number": number,
                "title": law.get("title", ""),
                "date_issued": (law.get("latestAction") or {}).get("actionDate", ""),
                "latest_action": (law.get("latestAction") or {}).get("text", ""),
                "source": "congress_law_scan",
                "is_original": True,
                "is_law": True,
                "law_number": law_number,
            })
            if len(out) >= limit:
                return out
    return out


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def search_by_title(named_entity, max_recent=3):
    """
    Title search for named acts.

    Phase 0: hardcoded POPULAR_NAMES table (historical acts with divergent titles)
    Phase 1: scraped congress.gov popular names cache (auto-refreshed weekly)
    Phase 2: GovInfo BILLS phrase search (deduped, multi-result)
    Phase 3: Congress.gov enacted-law title scan (enrolled title, no GovInfo lag)
    Phase 4: Congress.gov bill listing scan (fresh, not-yet-enacted bills)

    Congress.gov's /v3/bill endpoint does NOT support keyword search — the `query`
    parameter is silently ignored and the response is a default-sorted list that
    has nothing to do with the request. That whole phase was injecting garbage
    into every search that wasn't already in the popular-names tables, so it's
    gone. GovInfo phrase search is the load-bearing path.
    """
    entity_lower = named_entity.lower().strip()

    # If the user pinned a specific year ("of 2004"), narrow Phase 2 to that
    # Congress. Without this, reauthorizations swamp the original because their
    # text repeats the original's title many times — higher phrase score wins.
    _year_match = re.search(r"\bof\s+(\d{4})\b", entity_lower)
    _year_congress = None
    if _year_match:
        _year = int(_year_match.group(1))
        if 1789 <= _year <= 2100:
            _year_congress = (_year - 1787) // 2  # Congress N covers years 2N+1787 and 2N+1788

    # Phase 0 — hardcoded table.
    # Try the literal key first, then a year-stripped variant ("act of 2022" → "act"),
    # since the router often appends an official year that the table omits.
    _lookup_keys = [entity_lower]
    _year_stripped = re.sub(r"\s+of\s+\d{4}\s*$", "", entity_lower).strip()
    if _year_stripped and _year_stripped != entity_lower:
        _lookup_keys.append(_year_stripped)

    _hit_key = next((k for k in _lookup_keys if k in POPULAR_NAMES), None)
    if _hit_key:
        entry = POPULAR_NAMES[_hit_key]
        result = {
            "congress": entry["congress"],
            "type": entry["type"].lower(),
            "number": entry["number"],
            "title": entry["title"],
            "date_issued": "",
            "latest_action": "",
            "source": "popular_names_hardcoded",
            "is_original": True,
            "is_law": False,
            "law_number": None,
        }
        log_action(
            agent_name="title_search", action="search_by_title",
            input_data={"named_entity": named_entity},
            output_data={"original_found": True, "original_source": result["source"], "recent_count": 0},
        )
        return [result]

    # Phase 1 — scraped popular names cache
    cache = _load_popular_names_cache()
    if entity_lower in cache:
        entry = cache[entity_lower]
        result = {
            "congress": entry["congress"],
            "type": entry["type"],
            "number": entry["number"],
            "title": named_entity,
            "date_issued": "",
            "latest_action": "",
            "source": "popular_names_cache",
            "is_original": True,
            "is_law": False,
            "law_number": None,
        }
        log_action(
            agent_name="title_search", action="search_by_title",
            input_data={"named_entity": named_entity},
            output_data={"original_found": True, "original_source": result["source"], "recent_count": 0},
        )
        return [result]

    # Phase 2 — GovInfo phrase search. Over-fetch so dedup leaves enough distinct
    # bills to give the caller something to work with even when an act has many
    # reauthorizations indexed.
    results = _govinfo_phrase_search(named_entity, limit=max_recent + 1, congress=_year_congress)
    # When the year filter returns nothing — e.g. the act's text doesn't repeat
    # its title literally in that Congress's session — retry without the filter
    # so the user still gets the closest matches instead of an empty page.
    if not results and _year_congress is not None:
        results = _govinfo_phrase_search(named_entity, limit=max_recent + 1)

    # Phase 3 — Congress.gov enacted-law title scan. Authoritative for the
    # enrolled title, so it catches recent enactments GovInfo hasn't ingested
    # and acts renamed between introduction and enactment (where Phase 2 is
    # searching the stale introduced title). Tried before the general listing
    # scan because the /law endpoint is bounded and reliably sorted.
    if not results:
        print(f"[TITLE SEARCH] GovInfo empty — scanning Congress.gov enacted laws for: {named_entity}")
        results = _congress_gov_law_scan(named_entity, limit=max_recent + 1)

    # Phase 4 — Congress.gov bill listing scan. Last-ditch for freshly
    # introduced (not-yet-enacted) bills that GovInfo's BILLS index lags on.
    if not results:
        print(f"[TITLE SEARCH] Law scan empty — scanning Congress.gov listings for: {named_entity}")
        results = _congress_gov_title_scan(named_entity, limit=max_recent + 1)

    log_action(
        agent_name="title_search", action="search_by_title",
        input_data={"named_entity": named_entity},
        output_data={
            "original_found": bool(results),
            "original_source": results[0]["source"] if results else None,
            "recent_count": max(0, len(results) - 1),
        },
    )
    return results
