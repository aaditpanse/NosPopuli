import json
import os
from documentor_agent import log_action

_HERE = os.path.dirname(os.path.abspath(__file__))
_DATA_PATH = os.path.join(_HERE, "data", "legislators-current.json")
_ZIP3_PATH = os.path.join(_HERE, "data", "zip3_to_state.json")

_LEGISLATORS = None


def legislators():
    """The current-legislators roster (1.4 MB JSON, ~6.5 MB parsed), loaded
    on first use rather than at import so the API boots without it."""
    global _LEGISLATORS
    if _LEGISLATORS is None:
        try:
            with open(_DATA_PATH) as f:
                _LEGISLATORS = json.load(f)
        except FileNotFoundError:
            print(f"[CIVIC] WARNING: legislators file not found at {_DATA_PATH}")
            _LEGISLATORS = []
    return _LEGISLATORS

try:
    with open(_ZIP3_PATH) as f:
        ZIP3_TO_STATE = json.load(f)
except FileNotFoundError:
    print(f"[CIVIC] WARNING: zip3_to_state file not found at {_ZIP3_PATH}")
    ZIP3_TO_STATE = {}


def resolve_zip(zip_code):
    """
    Takes a zip code, returns state and current representatives.
    Uses a static 3-digit-prefix → state map shipped with the repo, so we
    have no runtime dependency on pgeocode (which downloads data on first use
    and breaks on ephemeral filesystems).
    """
    if not zip_code:
        print(f"[CIVIC] Empty zip code")
        return None
    zip_code = str(zip_code).strip()
    # Accept 5 or 9-digit ZIP+4 formats
    digits = "".join(c for c in zip_code if c.isdigit())[:5]
    if len(digits) != 5:
        print(f"[CIVIC] Invalid zip code format: {zip_code}")
        return None

    state = ZIP3_TO_STATE.get(digits[:3])
    if not state:
        print(f"[CIVIC] Could not resolve zip: {zip_code}")
        return None
    
    # Find senators (type=sen, state matches)
    senators = []
    representative = None
    
    for member in legislators():
        terms = member.get("terms", [])
        if not terms:
            continue
        
        last_term = terms[-1]
        
        # Must be current (end date 2025 or later or no end date)
        end = last_term.get("end", "")
        if end and end < "2025-01-01":
            continue
        
        member_state = last_term.get("state", "")
        member_type = last_term.get("type", "")
        
        if member_state != state:
            continue
        
        name = f"{member['name']['first']} {member['name']['last']}"
        bioguide = member['id'].get('bioguide', '')
        party = last_term.get("party", "")
        
        start_year = (last_term.get("start") or "")[:4]
        end_year = (last_term.get("end") or "")[:4]
        person = {
            "name": name,
            "bioguide_id": bioguide,
            "party": party,
            "state": state,
            "chamber": "Senate" if member_type == "sen" else "House",
            "contact_form": last_term.get("contact_form", ""),
            "url": last_term.get("url", ""),
            "term_start": start_year,
            "term_end": end_year,
        }
        
        if member_type == "sen":
            senators.append(person)
        elif member_type == "rep":
            # For now take the first rep found
            # House district resolution requires zip→district mapping
            if not representative:
                representative = person
    
    result = {
        "zip_code": zip_code,
        "state": state,
        "senators": senators[:2],
        "representative": representative,
    }
    
    log_action(
        agent_name="civic_resolver",
        action="resolve_zip",
        input_data={"zip_code": zip_code},
        output_data={
            "state": state,
            "senators": [s["name"] for s in senators[:2]],
            "representative": representative["name"] if representative else None
        }
    )
    
    return result

if __name__ == "__main__":
    from documentor_agent import log_action
    
    print("CIVIC RESOLVER TEST")
    print("-" * 40)
    
    for zip_code in ["05401", "10001", "90210"]:
        print(f"Zip: {zip_code}")
        result = resolve_zip(zip_code)
        if result:
            print(f"  State: {result['state']}")
            print(f"  Senators: {[s['name'] for s in result['senators']]}")
            print(f"  Rep: {result['representative']['name'] if result['representative'] else 'None'}")
        print()