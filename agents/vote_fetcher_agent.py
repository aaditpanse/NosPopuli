import requests
import xml.etree.ElementTree as ET
import os
from dotenv import load_dotenv
from agents.documentor_agent import log_action

load_dotenv()

CONGRESS_API_KEY = os.getenv("CONGRESS_API_KEY")
_session = requests.Session()

# ── House fetcher ──

def fetch_house_votes(vote_ref):
    """
    Fetches individual member votes for a House roll call.
    Tries Congress.gov API first (118th+), falls back to clerk.house.gov XML.
    """
    if not vote_ref:
        return None

    congress = vote_ref["congress"]
    session = vote_ref["session"]
    roll = vote_ref["roll"]
    year = vote_ref["year"]
    direct_url = vote_ref.get("url")

    members = None

    # Try Congress.gov API first (118th Congress onward)
    if congress >= 118:
        members = _fetch_house_congress_api(congress, session, roll)

    # Fall back to clerk.house.gov XML
    if not members:
        url = direct_url or f"https://clerk.house.gov/evs/{year}/roll{str(roll).zfill(3)}.xml"
        members = _fetch_house_clerk_xml(url)

    if members:
        log_action(
            agent_name="vote_fetcher",
            action="fetch_house_votes",
            input_data={"congress": congress, "roll": roll},
            output_data={"members_found": len(members)}
        )

    return members

def _fetch_house_congress_api(congress, session, roll):
    """Fetch from Congress.gov house-vote endpoint."""
    try:
        url = f"https://api.congress.gov/v3/house-vote/{congress}/{session}/{roll}/members"
        params = {"api_key": CONGRESS_API_KEY, "format": "json", "limit": 250}
        response = _session.get(url, params=params, timeout=10)

        if response.status_code != 200:
            return None

        data = response.json()
        raw = data.get("houseRollCallVoteMemberVotes", {}).get("memberVotes", [])

        members = []
        for m in raw:
            members.append({
                "name": f"{m.get('firstName', '')} {m.get('lastName', '')}".strip(),
                "party": m.get("party", ""),
                "state": m.get("state", ""),
                "vote": _normalize_vote(m.get("votePosition", ""))
            })
        return members if members else None

    except Exception as e:
        print(f"[VOTE FETCHER] Congress API error: {e}")
        return None

def _fetch_house_clerk_xml(url):
    """Fetch from clerk.house.gov XML feed."""
    try:
        response = _session.get(url, timeout=10)
        if response.status_code != 200:
            return None

        root = ET.fromstring(response.content)
        members = []

        for recorded_vote in root.findall(".//recorded-vote"):
            legislator = recorded_vote.find("legislator")
            vote_el = recorded_vote.find("vote")

            if legislator is None or vote_el is None:
                continue

            name = legislator.text.strip() if legislator.text else ""
            party = legislator.get("party", "")
            state = legislator.get("state", "")
            vote = _normalize_vote(vote_el.text)

            if name:
                members.append({
                    "name": name,
                    "party": party,
                    "state": state,
                    "vote": vote
                })

        return members if members else None

    except Exception as e:
        print(f"[VOTE FETCHER] Clerk XML error: {e}")
        return None

# ── Senate fetcher ──

def fetch_senate_votes(vote_ref):
    """
    Fetches individual member votes for a Senate roll call.
    Uses senate.gov XML directly.
    """
    if not vote_ref:
        return None

    congress = vote_ref["congress"]
    session = vote_ref["session"]
    roll = vote_ref["roll"]

    try:
        url = f"https://www.senate.gov/legislative/LIS/roll_call_votes/vote{congress}{session}/vote_{congress}_{session}_{str(roll).zfill(5)}.xml"
        response = _session.get(url, timeout=10)

        if response.status_code != 200:
            print(f"[VOTE FETCHER] Senate XML not found: {url}")
            return None

        root = ET.fromstring(response.content)
        members = []

        for member in root.findall(".//member"):
            first = member.findtext("first_name", "").strip()
            last = member.findtext("last_name", "").strip()
            party = member.findtext("party", "").strip()
            state = member.findtext("state", "").strip()
            vote = _normalize_vote(member.findtext("vote_cast", ""))

            members.append({
                "name": f"{first} {last}".strip(),
                "party": party,
                "state": state,
                "vote": vote
            })

        if members:
            log_action(
                agent_name="vote_fetcher",
                action="fetch_senate_votes",
                input_data={"congress": congress, "roll": roll},
                output_data={"members_found": len(members)}
            )

        return members if members else None

    except Exception as e:
        print(f"[VOTE FETCHER] Senate XML error: {e}")
        return None

# ── Normalize vote values ──

def _normalize_vote(raw):
    """Normalize various vote strings to Yea/Nay/Present/Not Voting."""
    if not raw:
        return "Not Voting"
    r = raw.strip().lower()
    if r in ("yea", "aye", "yes", "y"):
        return "Yea"
    if r in ("nay", "no", "n"):
        return "Nay"
    if r in ("present", "p"):
        return "Present"
    return "Not Voting"

if __name__ == "__main__":
    from agents.historian_agent import fetch_bill_actions
    from agents.vote_parser_agent import parse_vote_references

    print("VOTE FETCHER TEST")
    print("-" * 40)

    # ACA
    actions = fetch_bill_actions(111, "hr", 3590)
    refs = parse_vote_references(actions)

    print("ACA House votes:")
    house = fetch_house_votes(refs["house"])
    if house:
        yeas = sum(1 for m in house if m["vote"] == "Yea")
        nays = sum(1 for m in house if m["vote"] == "Nay")
        print(f"  {len(house)} members · Yea: {yeas} · Nay: {nays}")
        print(f"  Sample: {house[0]}")
        print(f"  Sample: {house[1]}")
    else:
        print("  No data found")

    print()
    print("ACA Senate votes:")
    senate = fetch_senate_votes(refs["senate"])
    if senate:
        yeas = sum(1 for m in senate if m["vote"] == "Yea")
        nays = sum(1 for m in senate if m["vote"] == "Nay")
        print(f"  {len(senate)} members · Yea: {yeas} · Nay: {nays}")
        print(f"  Sample: {senate[0]}")
        print(f"  Sample: {senate[1]}")
    else:
        print("  No data found")