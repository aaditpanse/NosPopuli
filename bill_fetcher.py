import re
import requests
import os
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from cachetools import TTLCache
from threading import RLock

_bill_cache_lock       = RLock()
_related_cache_lock    = RLock()
_amendments_cache_lock = RLock()
_text_cache_lock       = RLock()
_cosponsors_cache_lock = RLock()
from documentor_agent import log_action
from congress_breaker import congress_get, CongressOutageError

load_dotenv()

CONGRESS_API_KEY = os.getenv("CONGRESS_API_KEY")
GOVINFO_API_KEY  = os.getenv("GovInfo_API_KEY")

_session = requests.Session()

_bill_cache         = TTLCache(maxsize=256, ttl=3600)
_actions_cache      = TTLCache(maxsize=256, ttl=1800)
_text_cache         = TTLCache(maxsize=128, ttl=7200)
_related_cache      = TTLCache(maxsize=256, ttl=3600)
_amendments_cache   = TTLCache(maxsize=256, ttl=3600)
_cosponsors_cache   = TTLCache(maxsize=256, ttl=3600)

def fetch_bill(congress_number, bill_type, bill_number):
    # Manual cache so transient None returns (network blip, 5xx, rate limit)
    # don't poison the TTL window. Only successful responses are stored.
    key = (congress_number, bill_type, bill_number)
    with _bill_cache_lock:
        hit = _bill_cache.get(key)
    if hit is not None:
        return hit

    url = f"https://api.congress.gov/v3/bill/{congress_number}/{bill_type}/{bill_number}"
    
    params = {
        "api_key": CONGRESS_API_KEY,
        "format": "json"
    }
    
    try:
        response = congress_get(url, params=params, timeout=10)
    except CongressOutageError as e:
        print(f"[BILL FETCHER] Congress.gov unavailable for {bill_type}{bill_number}: {e}")
        return None
    except Exception as e:
        print(f"[BILL FETCHER] Unexpected error: {type(e).__name__}")
        return None
    
    if response.status_code == 429:
        print(f"[BILL FETCHER] Rate limited by Congress.gov")
        return None
    
    if response.status_code == 404:
        print(f"[BILL FETCHER] Bill not found: {bill_type}{bill_number}")
        return None
    
    if response.status_code != 200:
        print(f"[BILL FETCHER] Error {response.status_code} for {bill_type}{bill_number}")
        return None
    
    try:
        data = response.json()
    except Exception as e:
        print(f"[BILL FETCHER] Failed to parse JSON: {e}")
        return None
    
    if "bill" not in data:
        print(f"[BILL FETCHER] Unexpected response structure for {bill_type}{bill_number}")
        return None
    
    bill = data["bill"]
    
    # Safe extraction with fallbacks
    title = bill.get("title", "Unknown title")
    latest_action = (bill.get("latestAction") or {}).get("text", "No action recorded")
    
    log_action(
        agent_name="bill_fetcher",
        action="fetch_bill",
        input_data={
            "congress": congress_number,
            "type": bill_type,
            "number": bill_number
        },
        output_data={
            "title": title,
            "status": latest_action
        }
    )

    with _bill_cache_lock:
        _bill_cache[key] = data
    return data


def fetch_law(congress, law_number):
    """
    Fetches bill data by public law number.
    """
    url = f"https://api.congress.gov/v3/law/{congress}/pub/{law_number}"
    
    params = {
        "api_key": CONGRESS_API_KEY,
        "format": "json"
    }
    
    try:
        response = _session.get(url, params=params, timeout=10)
    except requests.exceptions.Timeout:
        print(f"[BILL FETCHER] Timeout fetching law {congress} pub {law_number}")
        return None
    except Exception as e:
        print(f"[BILL FETCHER] Error fetching law: {e}")
        return None

    if response.status_code == 429:
        print(f"[BILL FETCHER] Rate limited fetching law")
        return None
    if response.status_code == 404:
        print(f"[BILL FETCHER] Law {congress} pub {law_number} not in item endpoint — trying list fallback")
        fallback_url = f"https://api.congress.gov/v3/law/{congress}/pub"
        try:
            r2 = _session.get(fallback_url, params={**params, "limit": 250}, timeout=10)
            if r2.status_code == 200:
                for bill in r2.json().get("bills", []):
                    for law in (bill.get("laws") or []):
                        raw = str(law.get("number", ""))
                        # Normalise "119-98" → "98" before comparing
                        seq = raw.split("-")[-1] if "-" in raw else raw
                        if seq == str(law_number):
                            bill_type = (bill.get("type") or "").lower()
                            bill_number_val = bill.get("number")
                            if bill_type and bill_number_val:
                                return fetch_bill(congress, bill_type, int(bill_number_val))
        except Exception as e:
            print(f"[BILL FETCHER] Fallback error: {e}")
        return None
    if response.status_code != 200:
        print(f"[BILL FETCHER] Law not found: {congress} pub {law_number}")
        return None

    try:
        data = response.json()
    except Exception:
        return None

    bill = data.get("bill")
    
    if not bill:
        return None
    
    # Extract bill identifiers directly from the bill object
    bill_congress = bill.get("congress", congress)
    bill_type = bill.get("type", "").lower()
    bill_number = bill.get("number")
    
    if not bill_type or not bill_number:
        return None
    
    log_action(
        agent_name="bill_fetcher",
        action="fetch_law",
        input_data={"congress": congress, "law_number": law_number},
        output_data={"bill_type": bill_type, "bill_number": bill_number}
    )
    
    # Fetch full bill data using existing fetch_bill
    return fetch_bill(bill_congress, bill_type, int(bill_number))
def fetch_related_bills(congress, bill_type, bill_number, max_results=5):
    """
    Fetch related bills from Congress.gov, grouped by relationship type.
    Returns dict: {identical, related, superseded} — drops 'Procedurally related'.
    """
    # Manual cache so failure paths (network blip, non-200, bad JSON) don't
    # poison the TTL window with an empty result.
    key = (congress, bill_type, bill_number, max_results)
    with _related_cache_lock:
        hit = _related_cache.get(key)
    if hit is not None:
        return hit

    url = f"https://api.congress.gov/v3/bill/{congress}/{bill_type}/{bill_number}/relatedbills"
    params = {"api_key": CONGRESS_API_KEY, "format": "json", "limit": 50}

    try:
        r = _session.get(url, params=params, timeout=10)
    except Exception as e:
        print(f"[BILL FETCHER] Related bills error: {e}")
        return {"identical": [], "related": [], "superseded": []}

    if r.status_code != 200:
        print(f"[BILL FETCHER] Related bills: HTTP {r.status_code}")
        return {"identical": [], "related": [], "superseded": []}

    try:
        raw = r.json().get("relatedBills", [])
    except Exception:
        return {"identical": [], "related": [], "superseded": []}

    identical = []
    related = []
    superseded = []
    seen = set()

    for b in raw:
        details = b.get("relationshipDetails", [])
        rel_type = details[0].get("type", "") if details else ""
        if rel_type == "Procedurally related":
            continue

        key = f"{b.get('congress')}{(b.get('type') or '').lower()}{b.get('number')}"
        if key in seen:
            continue
        seen.add(key)

        entry = {
            "congress": b.get("congress"),
            "type": (b.get("type") or "").lower(),
            "number": b.get("number"),
            "title": b.get("title", "").strip(),
            "latest_action": (b.get("latestAction") or {}).get("text", ""),
            "latest_action_date": (b.get("latestAction") or {}).get("actionDate", ""),
        }

        if rel_type == "Identical bill":
            identical.append(entry)
        elif rel_type == "Superseded by":
            superseded.append(entry)
        else:
            related.append(entry)

    # Identical: keep only most recent by latest_action_date
    if len(identical) > 1:
        identical.sort(key=lambda x: x.get("latest_action_date") or "", reverse=True)
        identical = identical[:1]

    result = {
        "identical": identical,
        "related": related[:max_results],
        "superseded": superseded[:1],
    }
    with _related_cache_lock:
        _related_cache[key] = result
    return result


def fetch_amendments(congress, bill_type, bill_number, max_results=50):
    """
    Fetch amendments formally filed against this bill from Congress.gov.
    Returns list of amendment entries, capped at max_results.
    """
    # Manual cache so failure paths don't poison the TTL window.
    key = (congress, bill_type, bill_number, max_results)
    with _amendments_cache_lock:
        hit = _amendments_cache.get(key)
    if hit is not None:
        return hit

    url = f"https://api.congress.gov/v3/bill/{congress}/{bill_type}/{bill_number}/amendments"
    params = {"api_key": CONGRESS_API_KEY, "format": "json", "limit": max_results}

    try:
        r = _session.get(url, params=params, timeout=10)
    except Exception as e:
        print(f"[BILL FETCHER] Amendments error: {e}")
        return []

    if r.status_code == 404:
        return []
    if r.status_code != 200:
        print(f"[BILL FETCHER] Amendments: HTTP {r.status_code}")
        return []

    try:
        raw = r.json().get("amendments", [])
    except Exception:
        return []

    results = []
    for a in raw:
        atype = (a.get("type") or "").upper()
        number = a.get("number")
        title = (a.get("description") or a.get("purpose") or "").strip()
        # Skip amendments with no real title — just the bare identifier repeated
        if not title or title.upper() == f"{atype} {number}":
            continue
        results.append({
            "congress": a.get("congress"),
            "type": atype.lower(),
            "number": number,
            "title": title,
            "latest_action": (a.get("latestAction") or {}).get("text", ""),
            "latest_action_date": (a.get("latestAction") or {}).get("actionDate", ""),
        })

    with _amendments_cache_lock:
        _amendments_cache[key] = results
    return results


_AMENDS_PATTERNS = [
    re.compile(r"[Tt]o\s+amend\s+(?:the\s+)?(.+?)\s*(?:of\s+\d{4})?(?:,|\.|\sto\b)", re.IGNORECASE),
    re.compile(r"[Tt]o\s+reauthorize\s+(?:the\s+)?(.+?)\s*(?:of\s+\d{4})?(?:,|\.|\sto\b)", re.IGNORECASE),
]
_REAUTH_PATTERN = re.compile(r"reauthorize", re.IGNORECASE)

def parse_amends_from_title(title: str, summary: str = "") -> dict | None:
    """
    Extract what law a bill amends or reauthorizes from its title, falling back
    to the first paragraph of the summary if the title yields nothing.
    Returns {"label": "Amends"|"Reauthorizes", "act_name": str} or None.
    """
    sources = [title or ""]
    if summary:
        first_para = (summary or "").split("\n")[0][:400]
        sources.append(first_para)

    for text in sources:
        for pattern in _AMENDS_PATTERNS:
            m = pattern.search(text)
            if m:
                act_name = m.group(1).strip().rstrip(",.")
                if len(act_name) < 5 or len(act_name) > 120:
                    continue
                label = "Reauthorizes" if _REAUTH_PATTERN.search(text[:m.start() + 15]) else "Amends"
                return {"label": label, "act_name": act_name}

    return None


def _strip_html_to_text(html_str: str, max_chars: int) -> str:
    """Extract readable bill text from a Congress.gov / GovInfo page.

    The bill is served as a single <pre> block whose whitespace *is* the
    structure — section headings, subsection indentation, alignment. Keep that
    verbatim (the frontend renders it monospace, pre-wrap); only fall back to a
    flat extraction when there's no <pre>. Previously all runs of spaces were
    collapsed, which flattened the bill into an unreadable wall."""
    soup = BeautifulSoup(html_str, "html.parser")
    pre = soup.find("pre")
    text = pre.get_text() if pre else soup.get_text(separator="\n")

    # Drop the GPO boilerplate header ("[Congressional Bills 118th Congress]",
    # "[From the U.S. Government Publishing Office]", "[H.R. 815 Enrolled ...]").
    lines = text.split("\n")
    while lines and (not lines[0].strip() or lines[0].lstrip().startswith("[")):
        lines.pop(0)
    text = "\n".join(lines)

    text = re.sub(r"[ \t]+\n", "\n", text)   # trailing whitespace per line
    text = re.sub(r"\n{3,}", "\n\n", text)   # collapse oversized gaps
    return text.strip()[:max_chars]


# Stage suffixes ranked most-authoritative-first. GovInfo packageId = BILLS-{congress}{type}{number}{stage}.
_GOVINFO_STAGE_PRIORITY = ["enr", "eas", "eah", "es", "eh", "rs", "rh", "pcs", "pch", "is", "ih"]


def _govinfo_text_fallback(congress, bill_type, bill_number, max_chars):
    """
    Pull the most recent published version from GovInfo when Congress.gov hasn't
    synced text yet. Probes each stage suffix in priority order (enr → es/eh →
    is/ih) directly against the deterministic packageId URL. First hit wins.
    """
    btype = bill_type.lower()
    base = f"BILLS-{congress}{btype}{bill_number}"
    for stage in _GOVINFO_STAGE_PRIORITY:
        pkg = base + stage
        url = f"https://www.govinfo.gov/content/pkg/{pkg}/html/{pkg}.htm"
        try:
            r = _session.get(url, timeout=8, allow_redirects=False)
        except Exception:
            continue
        if r.status_code != 200:
            continue
        text = _strip_html_to_text(r.text, max_chars)
        if len(text) < 200:
            continue
        print(f"[BILL FETCHER] GovInfo fallback: {len(text)} chars for {bill_type}{bill_number} ({pkg})")
        return text
    return None


def fetch_bill_text(congress, bill_type, bill_number, max_chars=8000):
    """
    Fetch and strip actual bill text. Tries Congress.gov's text-versions endpoint
    first; falls back to GovInfo's direct package URL when Congress.gov has no
    versions yet (common for bills < ~2 weeks old).
    Returns cleaned plain text capped at max_chars, or None if unavailable.
    """
    # Manual cache so transient None returns don't poison the TTL window.
    # "Genuinely no text yet" returns None too — we re-fetch those next call,
    # which is fine because new bills publish text within days.
    key = (congress, bill_type, bill_number, max_chars)
    with _text_cache_lock:
        hit = _text_cache.get(key)
    if hit is not None:
        return hit
    result = _fetch_bill_text_uncached(congress, bill_type, bill_number, max_chars)
    if result:
        with _text_cache_lock:
            _text_cache[key] = result
    return result


def _fetch_bill_text_uncached(congress, bill_type, bill_number, max_chars=8000):
    url = f"https://api.congress.gov/v3/bill/{congress}/{bill_type}/{bill_number}/text"
    params = {"api_key": CONGRESS_API_KEY, "format": "json"}

    try:
        r = _session.get(url, params=params, timeout=10)
    except Exception as e:
        print(f"[BILL FETCHER] Text versions error: {e}")
        return _govinfo_text_fallback(congress, bill_type, bill_number, max_chars)

    if r.status_code != 200:
        print(f"[BILL FETCHER] Text versions: HTTP {r.status_code}")
        return _govinfo_text_fallback(congress, bill_type, bill_number, max_chars)

    try:
        versions = r.json().get("textVersions", [])
    except Exception:
        return _govinfo_text_fallback(congress, bill_type, bill_number, max_chars)

    if not versions:
        return _govinfo_text_fallback(congress, bill_type, bill_number, max_chars)

    # Prefer the most authoritative/current version, most-final first. Matched as
    # a substring of the version type, so "Enrolled" catches "Enrolled Bill", etc.
    FORMAT_PRIORITY = [
        "Public Law", "Enrolled", "Engrossed Amendment", "Engrossed in Senate",
        "Engrossed in House", "Placed on Calendar", "Reported in Senate",
        "Reported in House", "Referred in", "Introduced in Senate", "Introduced in House",
    ]
    selected_url = None

    for priority in FORMAT_PRIORITY:
        for version in versions:
            if priority.lower() in (version.get("type") or "").lower():
                for fmt in version.get("formats", []):
                    if fmt.get("type") == "Formatted Text":
                        selected_url = fmt.get("url")
                        break
            if selected_url:
                break
        if selected_url:
            break

    # Fallback — first Formatted Text URL available
    if not selected_url:
        for version in versions:
            for fmt in version.get("formats", []):
                if fmt.get("type") == "Formatted Text":
                    selected_url = fmt.get("url")
                    break
            if selected_url:
                break

    if not selected_url:
        print(f"[BILL FETCHER] No formatted text URL found for {bill_type}{bill_number}")
        return _govinfo_text_fallback(congress, bill_type, bill_number, max_chars)

    try:
        r = _session.get(selected_url, timeout=15)
        if r.status_code != 200:
            return _govinfo_text_fallback(congress, bill_type, bill_number, max_chars)

        text = _strip_html_to_text(r.text, max_chars)
        print(f"[BILL FETCHER] Fetched bill text: {len(text)} chars for {bill_type}{bill_number}")
        return text

    except Exception as e:
        print(f"[BILL FETCHER] Text fetch error: {e}")
        return _govinfo_text_fallback(congress, bill_type, bill_number, max_chars)


def fetch_cosponsors(congress, bill_type, bill_number, limit=250):
    """
    Fetches cosponsors for a bill from Congress.gov.
    Returns list of cosponsor dicts with name, party, state, bioguide_id.
    """
    # Manual cache so failure paths don't poison the TTL window.
    key = (congress, bill_type, bill_number, limit)
    with _cosponsors_cache_lock:
        hit = _cosponsors_cache.get(key)
    if hit is not None:
        return hit

    url = f"https://api.congress.gov/v3/bill/{congress}/{bill_type}/{bill_number}/cosponsors"
    params = {"api_key": CONGRESS_API_KEY, "format": "json", "limit": limit}

    try:
        r = _session.get(url, params=params, timeout=10)
    except Exception as e:
        print(f"[BILL FETCHER] Cosponsors error: {e}")
        return []

    if r.status_code != 200:
        print(f"[BILL FETCHER] Cosponsors: HTTP {r.status_code}")
        return []

    try:
        raw = r.json().get("cosponsors", [])
    except Exception:
        return []

    result = []
    for c in raw:
        result.append({
            "name": c.get("fullName", ""),
            "first_name": c.get("firstName", ""),
            "last_name": c.get("lastName", ""),
            "party": c.get("party", ""),
            "state": c.get("state", ""),
            "bioguide_id": c.get("bioguideId", ""),
            "sponsorship_date": c.get("sponsorshipDate", ""),
            "is_original": c.get("isOriginalCosponsor", False),
        })
    with _cosponsors_cache_lock:
        _cosponsors_cache[key] = result
    return result


if __name__ == "__main__":
    import json
    
    url = f"https://api.congress.gov/v3/law/119/pub/87"
    params = {"api_key": CONGRESS_API_KEY, "format": "json"}
    response = requests.get(url, params=params, timeout=10)
    
    print(f"Status: {response.status_code}")
    print(json.dumps(response.json(), indent=2)[:2000])
    
    