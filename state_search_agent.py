"""State bill search + lookup, backed by LegiScan.

Replaces the OpenStates search integration. LegiScan's full-text `getSearch`
engine is relevance-ranked and covers every state, so the hand-maintained
`STATE_SESSIONS` map and the throttled `/jurisdictions` session lookups are
gone — search defaults to the current session automatically (`year=2`).

Result dicts keep the shape the frontend already consumes; `ocd_id` now carries
LegiScan's integer `bill_id` (as a string) so identity flows unchanged through
the frontend, notifications, and the correspondence DB.
"""

import re
import legiscan_client as legiscan
from documentor_agent import log_action

STATE_JURISDICTIONS = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas",
    "CA": "California", "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware",
    "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho",
    "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
    "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland",
    "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi",
    "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
    "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
    "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma",
    "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina",
    "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah",
    "VT": "Vermont", "VA": "Virginia", "WA": "Washington", "WV": "West Virginia",
    "WI": "Wisconsin", "WY": "Wyoming"
}

SKIP_PATTERNS = [
    "celebrating the life", "commending", "recognizing the",
    "honoring the", "congratulating", "acknowledging",
    "commemorating", "proclaiming", "expressing support for the designation"
]

ENABLED_STATES = set(STATE_JURISDICTIONS.keys())

# Per-state validator floor. Default is 5 (matches federal); thin-metadata
# states drop to 4 so we don't return empty too aggressively. This is the
# obvious knob to tune as we get real usage signals — change values here, no
# code change needed elsewhere.
_DEFAULT_STATE_VALIDATOR_FLOOR = 5
STATE_VALIDATOR_FLOOR = {
    # Thin metadata / small legislatures
    "WY": 4, "SD": 4, "ND": 4, "VT": 4, "NH": 4, "AK": 4, "DE": 4,
    "MT": 4, "RI": 4, "ME": 4, "ID": 4, "NE": 4, "HI": 4,
    # Rich metadata — keep at 5 (CA, NY, TX, FL, IL, etc. use the default)
}


def get_state_validator_floor(state_code: str) -> int:
    return STATE_VALIDATOR_FLOOR.get((state_code or "").upper(), _DEFAULT_STATE_VALIDATOR_FLOOR)


ENACTED_ACTION_KEYWORDS = [
    "signed by governor", "enacted", "chaptered", "became law",
    "approved by governor", "signed into law",
]


def get_jurisdiction(state_code):
    return STATE_JURISDICTIONS.get(state_code.upper())


def filter_enacted(bills):
    """Keep only bills whose latest action indicates they were signed/enacted."""
    return [
        b for b in bills
        if any(kw in (b.get("latest_action") or "").lower() for kw in ENACTED_ACTION_KEYWORDS)
    ]


def _chamber_from_number(bill_number: str) -> str:
    """Infer originating chamber from a bill number prefix (H*/A* = lower,
    S* = upper). Heuristic — search results don't carry chamber directly."""
    n = (bill_number or "").strip().upper()
    if n[:1] in ("H", "A"):
        return "lower"
    if n[:1] == "S":
        return "upper"
    return ""


def _normalize_ident(s: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (s or "").upper())


def _normalize_search_result(item, state_code):
    """Map a LegiScan getSearch result to the frontend bill shape."""
    bill_number = item.get("bill_number", "")
    return {
        "ocd_id": str(item.get("bill_id", "")),
        "identifier": bill_number,
        "title": item.get("title", ""),
        "abstract": "",              # not in search payload; filled on detail fetch
        "subjects": [],
        "state": state_code,
        "jurisdiction": get_jurisdiction(state_code),
        "session": "",
        "chamber": _chamber_from_number(bill_number),
        "latest_action": item.get("last_action", ""),
        "latest_action_date": item.get("last_action_date", ""),
        "sponsor": None,
        "url": item.get("url", ""),
        "is_state_bill": True,
        "source": "legiscan",
        "relevance": item.get("relevance", 0),
    }


def fetch_state_bill_by_identifier(identifier, state_code, session=None):
    """
    Direct lookup of a state bill by its identifier (e.g. 'HB 1234').
    Returns a list (same shape as search_state_bills) with the exact-number
    matches, newest first. session="any" widens to all sessions.
    """
    state_code = state_code.upper()
    if state_code not in ENABLED_STATES:
        return []

    # year: 2=current session, 1=all sessions (for "any").
    year = 1 if session == "any" else 2
    results = legiscan.search(state_code, identifier, page=1, year=year)

    target = _normalize_ident(identifier)
    matches = []
    for item in results:
        if _normalize_ident(item.get("bill_number", "")) == target:
            matches.append(_normalize_search_result(item, state_code))

    matches.sort(key=lambda b: b.get("latest_action_date", ""), reverse=True)

    log_action(
        agent_name="state_search",
        action="fetch_state_bill_by_identifier",
        input_data={"identifier": identifier, "state": state_code},
        output_data={"found": len(matches) > 0},
    )
    return matches


def search_state_bills(query, state_code, session=None, limit=10):
    """
    Full-text search LegiScan for bills in a given state.
    Returns normalized bill objects compatible with the frontend.
    """
    state_code = state_code.upper()

    if state_code not in ENABLED_STATES:
        print(f"[STATE SEARCH] State {state_code} not yet enabled")
        return []

    raw = legiscan.search(state_code, query, page=1)

    results = []
    for item in raw:
        title = (item.get("title") or "").lower()
        if any(p in title for p in SKIP_PATTERNS):
            continue
        results.append(_normalize_search_result(item, state_code))
        if len(results) >= limit:
            break

    log_action(
        agent_name="state_search",
        action="search_state_bills",
        input_data={"query": query, "state": state_code},
        output_data={"results_count": len(results)},
    )
    return results


def get_recent_state_bills(state_code, limit=10, session=None):
    """
    Fetch recent substantive bills for feed generation via the session master
    list, sorted by most recent action. No query — just latest activity.
    """
    state_code = state_code.upper()
    if state_code not in ENABLED_STATES:
        return []

    master = legiscan.get_master_list(state_code)
    if not master:
        return []

    master.sort(key=lambda b: b.get("last_action_date") or "", reverse=True)

    results = []
    for b in master:
        title = (b.get("title") or "").lower()
        if any(p in title for p in SKIP_PATTERNS):
            continue
        bill_number = b.get("number", "")
        results.append({
            "ocd_id": str(b.get("bill_id", "")),
            "identifier": bill_number,
            "title": b.get("title", ""),
            "state": state_code,
            "session": "",
            "chamber": _chamber_from_number(bill_number),
            "latest_action": b.get("last_action", ""),
            "latest_action_date": b.get("last_action_date", ""),
            "url": b.get("url", ""),
            "is_state_bill": True,
            "source": "legiscan",
        })
        if len(results) >= limit:
            break

    return results
