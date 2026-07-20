"""LegiScan Pull API client — the state-legislation data source.

Replaces the OpenStates v3 integration. LegiScan's free "Public" key allows
30,000 queries/month (~1,000/day) with uniform coverage across all 50 states +
Congress, a real full-text search engine, and a per-bill `change_hash` for cheap
change detection. See the v1.91 user manual for the full operation set.

Call shape (Pull API):
    https://api.legiscan.com/?key=APIKEY&op=OPERATION&PARAM=VALUE
Every response carries a top-level "status" of "OK" or "ERROR" (with an
"alert.message"). Keys, operations and state abbreviations are case-insensitive.

Query discipline (mirrors the 30k/mo budget):
- getSessionList / getSessionPeople are cached long — sessions and their member
  rosters barely move within a session.
- getRollCall / getPerson / getBillText payloads are effectively static once
  issued; cached aggressively.
- getBill is the workhorse (one query per bill) and getSearch is one query per
  page; both cached on the order of the manual's refresh guidance.

Failure is non-fatal: network / ERROR responses log and return None (or an empty
collection), so callers degrade the same way the OpenStates path did.
"""

import os
import base64
import requests
from threading import RLock
from dotenv import load_dotenv
from cachetools import TTLCache
from documentor_agent import log_action

load_dotenv()

LEGISCAN_API_KEY = os.getenv("LEGISCAN_API_KEY", "")
LEGISCAN_BASE = "https://api.legiscan.com/"

_session = requests.Session()
_session.headers.update({"User-Agent": "NosPopuli/1.0 (civic transparency; nospopuli.org)"})

# Cache tiers. TTLs reflect how often the underlying data actually changes vs.
# the manual's refresh guidance, tuned toward staying well under the query quota.
_session_cache  = TTLCache(maxsize=128,  ttl=86400)   # getSessionList — 24h
_people_cache   = TTLCache(maxsize=128,  ttl=86400)   # getSessionPeople — 24h
_bill_cache     = TTLCache(maxsize=512,  ttl=1800)    # getBill — 30 min
_text_cache     = TTLCache(maxsize=256,  ttl=7200)    # getBillText — 2h (static doc)
_rollcall_cache = TTLCache(maxsize=512,  ttl=86400)   # getRollCall — static once issued
_person_cache   = TTLCache(maxsize=512,  ttl=86400)   # getPerson — daily
_search_cache   = TTLCache(maxsize=256,  ttl=3600)    # getSearch — 1h

_lock = RLock()


def has_key() -> bool:
    return bool(LEGISCAN_API_KEY)


def _call(op: str, **params) -> dict | None:
    """Issue one Pull API request. Returns the parsed payload dict on status=OK,
    else None (network error, HTTP error, or status=ERROR)."""
    if not LEGISCAN_API_KEY:
        print("[LEGISCAN] LEGISCAN_API_KEY not set — state data unavailable")
        return None

    query = {"key": LEGISCAN_API_KEY, "op": op, **params}
    try:
        r = _session.get(LEGISCAN_BASE, params=query, timeout=30)
    except Exception as e:
        print(f"[LEGISCAN] {op} request error: {e}")
        return None

    if r.status_code != 200:
        print(f"[LEGISCAN] {op} HTTP {r.status_code}")
        return None

    try:
        data = r.json()
    except Exception:
        print(f"[LEGISCAN] {op} non-JSON response")
        return None

    if data.get("status") != "OK":
        alert = (data.get("alert") or {}).get("message", "")
        print(f"[LEGISCAN] {op} ERROR: {alert}")
        return None

    return data


def _pick(data: dict, *keys):
    """Return the first present key from a payload (tolerates casing variants
    like sessionpeople / sessionPeople across LegiScan operations)."""
    for k in keys:
        if k in data:
            return data[k]
    return None


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------

def get_session_id(state: str) -> int | None:
    """Current (most recent, non-prior) regular session_id for a state, cached.
    Prefers an in-progress regular session; falls back to the newest session."""
    state = (state or "").upper()
    with _lock:
        if state in _session_cache:
            return _session_cache[state]

    data = _call("getSessionList", state=state)
    sessions = (data or {}).get("sessions") or []
    session_id = None
    if sessions:
        # Newest by year_end, preferring regular (special == 0) sessions.
        regular = [s for s in sessions if not s.get("special")]
        pool = regular or sessions
        latest = max(pool, key=lambda s: (s.get("year_end", 0), s.get("session_id", 0)))
        session_id = latest.get("session_id")

    with _lock:
        _session_cache[state] = session_id
    return session_id


def get_session_people(session_id: int) -> dict:
    """Map {people_id: {name, party, district, role}} for every legislator active
    in a session. One query, cached long — this is what turns getRollCall
    people_ids into named seats without a getPerson call per legislator."""
    if not session_id:
        return {}
    with _lock:
        if session_id in _people_cache:
            return _people_cache[session_id]

    data = _call("getSessionPeople", id=session_id)
    block = _pick(data or {}, "sessionpeople", "sessionPeople") or {}
    people = block.get("people") or []
    out = {}
    for p in people:
        pid = p.get("people_id")
        if pid is None:
            continue
        out[pid] = {
            "name": p.get("name", ""),
            "party": p.get("party", ""),
            "district": p.get("district", ""),
            "role": p.get("role", ""),
        }

    with _lock:
        _people_cache[session_id] = out
    return out


# ---------------------------------------------------------------------------
# Bills
# ---------------------------------------------------------------------------

def get_master_list(state: str) -> list:
    """Summary list of every bill in a state's current session (one query).
    Returns a list of bill summary dicts {bill_id, number, title, description,
    status_date, last_action, last_action_date, change_hash, url}. Used for the
    'recent activity' feed — there is no dedicated recent-bills endpoint."""
    data = _call("getMasterList", state=(state or "").upper())
    masterlist = (data or {}).get("masterlist") or {}
    out = []
    for k, v in masterlist.items():
        # "session" is a metadata key mixed in among the numeric bill keys.
        if k == "session" or not isinstance(v, dict) or "bill_id" not in v:
            continue
        out.append(v)
    return out


def get_bill(bill_id) -> dict | None:
    """Full bill payload (history, sponsors, votes, texts, subjects, sasts…)."""
    try:
        bill_id = int(bill_id)
    except (TypeError, ValueError):
        return None
    with _lock:
        if bill_id in _bill_cache:
            return _bill_cache[bill_id]

    data = _call("getBill", id=bill_id)
    bill = (data or {}).get("bill")
    if bill is not None:
        with _lock:
            _bill_cache[bill_id] = bill
    return bill


def get_bill_text(doc_id) -> dict | None:
    """Bill text record: {doc_id, mime, mime_id, text_size, text_hash, bytes}.
    Decodes LegiScan's base64 `doc` into raw `bytes` for the caller to extract
    (PDF for most states, HTML for some)."""
    try:
        doc_id = int(doc_id)
    except (TypeError, ValueError):
        return None
    with _lock:
        if doc_id in _text_cache:
            return _text_cache[doc_id]

    data = _call("getBillText", id=doc_id)
    text = (data or {}).get("text")
    if not text:
        return None
    try:
        text = dict(text)
        text["bytes"] = base64.b64decode(text.get("doc", ""))
    except Exception as e:
        print(f"[LEGISCAN] getBillText {doc_id} decode error: {e}")
        return None
    text.pop("doc", None)  # drop the (large) base64 blob once decoded

    with _lock:
        _text_cache[doc_id] = text
    return text


def get_roll_call(roll_call_id) -> dict | None:
    """Vote detail: summary counts + individual votes [{people_id, vote_text}]."""
    try:
        roll_call_id = int(roll_call_id)
    except (TypeError, ValueError):
        return None
    with _lock:
        if roll_call_id in _rollcall_cache:
            return _rollcall_cache[roll_call_id]

    data = _call("getRollCall", id=roll_call_id)
    rc = (data or {}).get("roll_call")
    if rc is not None:
        with _lock:
            _rollcall_cache[roll_call_id] = rc
    return rc


# ---------------------------------------------------------------------------
# People
# ---------------------------------------------------------------------------

def get_person(people_id) -> dict | None:
    """Single legislator record with third-party IDs (opensecrets, ftm, etc.)."""
    try:
        people_id = int(people_id)
    except (TypeError, ValueError):
        return None
    with _lock:
        if people_id in _person_cache:
            return _person_cache[people_id]

    data = _call("getPerson", id=people_id)
    person = (data or {}).get("person")
    if person is not None:
        with _lock:
            _person_cache[people_id] = person
    return person


def get_sponsored_list(people_id) -> list:
    """Bills sponsored by a legislator. Returns the raw bills list."""
    data = _call("getSponsoredList", id=people_id)
    block = _pick(data or {}, "sponsoredbills", "sponsoredBills") or {}
    return block.get("bills") or []


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

def search(state: str, query: str, page: int = 1, year: int = 2) -> list:
    """Full-text search via LegiScan's engine. `year`: 1=all, 2=current,
    3=recent, 4=prior, >1900=exact. Returns a list of result dicts (the numeric
    keys of `searchresult`, sorted by relevance), each with bill_id / bill_number
    / relevance / title / last_action / change_hash / url."""
    state = (state or "").upper()
    cache_key = (state, query.lower().strip(), page, year)
    with _lock:
        if cache_key in _search_cache:
            return _search_cache[cache_key]

    data = _call("getSearch", state=state, query=query, page=page, year=year)
    searchresult = (data or {}).get("searchresult") or {}

    results = []
    for k, v in searchresult.items():
        if k == "summary" or not isinstance(v, dict):
            continue
        results.append(v)
    results.sort(key=lambda r: r.get("relevance", 0), reverse=True)

    log_action(
        agent_name="legiscan",
        action="search",
        input_data={"state": state, "query": query, "page": page},
        output_data={"count": len(results)},
    )

    with _lock:
        _search_cache[cache_key] = results
    return results
