"""State legislator search + profile, backed by LegiScan.

LegiScan has no name-search endpoint, so we resolve a legislator against the
state's current-session roster (`getSessionPeople`, one cached query) and
fuzzy-match by name. Identity flows as `ocd_person_id` = str(people_id) so the
frontend member page keeps working. Sponsored bills come from `getSponsoredList`.

Note: LegiScan person records carry no photo/email, so those fields degrade to
empty (they were populated from OpenStates before).
"""

import legiscan_client as legiscan
from documentor_agent import log_action
from state_search_agent import STATE_JURISDICTIONS, ENABLED_STATES

_PARTY = {"D": "Democratic", "R": "Republican", "I": "Independent",
          "L": "Libertarian", "G": "Green"}


def _name_score(query: str, name: str) -> int:
    """Crude match score: exact > all-tokens-present > last-token-present > 0."""
    q = (query or "").strip().lower()
    n = (name or "").strip().lower()
    if not q or not n:
        return 0
    if q == n:
        return 3
    q_tokens = q.split()
    if all(t in n for t in q_tokens):
        return 2
    if q_tokens and q_tokens[-1] in n:
        return 1
    return 0


def search_state_member(name, state_code):
    """Find a state legislator by name via the current-session roster."""
    state_code = state_code.upper()
    if state_code not in ENABLED_STATES:
        return None

    session_id = legiscan.get_session_id(state_code)
    if not session_id:
        return None

    roster = legiscan.get_session_people(session_id)  # {people_id: {name, party, district, role}}
    best_pid, best = None, 0
    for pid, person in roster.items():
        score = _name_score(name, person.get("name", ""))
        if score > best:
            best, best_pid = score, pid

    if not best_pid:
        return None

    member = normalize_state_member({"people_id": best_pid, **roster[best_pid]}, state_code)
    log_action(
        agent_name="state_member",
        action="search_state_member",
        input_data={"name": name, "state": state_code},
        output_data={"found": True, "people_id": best_pid},
    )
    return member


def fetch_state_member_profile(ocd_person_id):
    """Fetch a fuller profile for a legislator by people_id (getPerson)."""
    person = legiscan.get_person(ocd_person_id)
    if not person:
        return None
    return normalize_state_member(person, None)


def fetch_state_member_bills(ocd_person_id, state_code, limit=10):
    """Bills sponsored by a legislator (getSponsoredList)."""
    bills_raw = legiscan.get_sponsored_list(ocd_person_id)
    bills = []
    for b in bills_raw[:limit]:
        number = b.get("number", "")
        bills.append({
            "ocd_id": str(b.get("bill_id", "")),
            "identifier": number,
            "title": b.get("title") or number,
            "session": "",
            "latest_action": b.get("last_action", ""),
            "date": b.get("last_action_date", ""),
            "is_state_bill": True,
        })
    return bills


def normalize_state_member(p, state_code):
    """Normalize a LegiScan person record to the NosPopuli member shape."""
    role = p.get("role", "")
    chamber_label = "House" if role == "Rep" else "Senate" if role == "Sen" else (role or "")
    party = _PARTY.get((p.get("party") or "").upper(), p.get("party", ""))
    resolved_state = state_code
    if not resolved_state:
        sid = p.get("state_id")
        # getPerson carries state_id; leave state blank if we can't resolve it.
        resolved_state = "" if sid is None else ""

    return {
        "ocd_person_id": str(p.get("people_id", "")),
        "name": p.get("name", ""),
        "party": party,
        "state": resolved_state or "",
        "chamber": chamber_label,
        "chambers": [chamber_label] if chamber_label else [],
        "district": p.get("district", ""),
        "title": role,
        "email": "",
        "photo_url": "",
        "links": [],
        "current": True,
        "is_state_legislator": True,
        "source": "legiscan",
        # Compatibility fields for renderMemberPage
        "bioguide_id": None,
        "start_year": None,
        "end_year": None,
        "birth_year": None,
    }
