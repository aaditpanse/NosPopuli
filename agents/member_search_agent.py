import requests
import os
from dotenv import load_dotenv
from agents.documentor_agent import log_action
from sources.congress_breaker import congress_get, CongressOutageError

load_dotenv()

CONGRESS_API_KEY = os.getenv("CONGRESS_API_KEY")
_session = requests.Session()
GOVINFO_API_KEY = os.getenv("GovInfo_API_KEY")

NICKNAMES = {
    "ted": "edward",
    "bill": "william",
    "bob": "robert",
    "joe": "joseph",
    "jim": "james",
    "mike": "michael",
    "dick": "richard",
    "chuck": "charles",
    "bernie": "bernard",
    "liz": "elizabeth",
    "betty": "elizabeth",
    "jack": "john",
    "mitt": "willard",
    "al": "albert",
    "tom": "thomas",
    "dan": "daniel",
    "fred": "frederick",
    "ed": "edward",
    "pat": "patricia",
}

def search_member(name):
    parts = [p.lower() for p in name.strip().split()]
    
    # Expand nicknames
    expanded_parts = []
    for p in parts:
        if p in NICKNAMES:
            expanded_parts.append(NICKNAMES[p])
    
    all_parts = parts + expanded_parts
    
    url = "https://api.congress.gov/v3/member"
    best_match = None
    best_score = 0
    next_url = None
    pages_checked = 0
    max_pages = 10
    
    params = {
        "api_key": CONGRESS_API_KEY,
        "format": "json",
        "limit": 250,
    }
    
    while pages_checked < max_pages:
        try:
            if next_url and pages_checked > 0:
                response = congress_get(next_url, timeout=10)
            else:
                response = congress_get(url, params=params, timeout=10)
        except CongressOutageError as e:
            print(f"[MEMBER_SEARCH] Congress.gov unavailable: {e}")
            return best_match  # may be None — caller handles "not found"

        if response.status_code != 200:
            break
        
        data = response.json()
        members = data.get("members", [])
        
        for m in members:
            member_name = (m.get("name") or "").lower()
            name_tokens = member_name.replace(",", "").split()
            
            score = 0
            for part in all_parts:
                if part in member_name:
                    # Last name (first token before comma) worth 3x
                    if name_tokens and part == name_tokens[0]:
                        score += 3
                    else:
                        score += 1
            
            if score > best_score:
                best_score = score
                best_match = m
        
        # Perfect match — last name + first name both found
        if best_score >= 4:
            break
        
        pagination = data.get("pagination", {})
        next_url = pagination.get("next")
        if not next_url:
            break
        
        if "api_key" not in next_url:
            next_url += f"&api_key={CONGRESS_API_KEY}"
        
        pages_checked += 1
    
    if not best_match or best_score == 0:
        print(f"[MEMBER SEARCH] No match found for: {name}")
        return None
    
    m = best_match
    raw_terms = m.get("terms", {})
    if isinstance(raw_terms, dict):
        terms = raw_terms.get("item", [])
    elif isinstance(raw_terms, list):
        terms = raw_terms
    else:
        terms = []
    
    result = {
        "bioguide_id": m.get("bioguideId"),
        "name": m.get("name"),
        "party": m.get("partyName"),
        "state": m.get("state"),
        "chamber": terms[-1].get("chamber", "") if terms else "",
        "start_year": terms[0].get("startYear") if terms else None,
        "end_year": terms[-1].get("endYear") if terms else None,
        "current": m.get("currentMember", False),
        "url": m.get("url"),
    }
    
    log_action(
        agent_name="member_search",
        action="search_member",
        input_data={"name": name},
        output_data={"found": result["name"], "bioguide_id": result["bioguide_id"], "score": best_score}
    )
    
    return result

def fetch_member_profile(bioguide_id):
    """
    Fetches full member profile including bio, terms, and stats.
    """
    url = f"https://api.congress.gov/v3/member/{bioguide_id}"
    params = {"api_key": CONGRESS_API_KEY, "format": "json"}

    try:
        response = congress_get(url, params=params, timeout=10)
    except CongressOutageError as e:
        print(f"[MEMBER_PROFILE] Congress.gov unavailable: {e}")
        return None
    if response.status_code != 200:
        return None
    
    data = response.json()
    member = data.get("member", {})
    
    photo_url = member.get("depiction", {}).get("imageUrl", "")
    
    terms = member.get("terms", [])
    if isinstance(terms, dict):
        terms = terms.get("item", [])
    
    years_served = 0
    chambers = set()
    for term in terms:
        start = term.get("startYear") or 0
        end = term.get("endYear") or 2026
        if start:
            years_served += (end - start)
        chamber = term.get("chamber", "")
        if chamber:
            chambers.add(chamber)
    
    party_history = member.get("partyHistory", [])
    current_party = party_history[-1].get("partyName", "") if party_history else ""
    
    profile = {
        "bioguide_id": bioguide_id,
        "name": member.get("directOrderName", ""),
        "party": current_party,
        "state": member.get("state", ""),
        "birth_year": member.get("birthYear", ""),
        "photo_url": photo_url,
        "current": member.get("currentMember", False),
        "chambers": list(chambers),
        "terms": terms,
        "years_served": years_served,
        "official_url": member.get("officialWebsiteUrl", ""),
        "congress_url": member.get("url", ""),
    }
    
    log_action(
        agent_name="member_search",
        action="fetch_member_profile",
        input_data={"bioguide_id": bioguide_id},
        output_data={"name": profile["name"], "years_served": years_served}
    )
    
    return profile

def fetch_member_legislation(bioguide_id, limit=20):
    sponsored_url = f"https://api.congress.gov/v3/member/{bioguide_id}/sponsored-legislation"
    cosponsored_url = f"https://api.congress.gov/v3/member/{bioguide_id}/cosponsored-legislation"

    sponsored = []
    policy_areas = {}

    # Fetch 250 bills for accurate policy area distribution
    try:
        r = congress_get(sponsored_url, params={
            "api_key": CONGRESS_API_KEY, "format": "json", "limit": 250
        }, timeout=30)
        bills_raw = r.json().get("sponsoredLegislation", []) if r.status_code == 200 else []
    except (CongressOutageError, Exception):
        bills_raw = []

    if bills_raw:
        bills = bills_raw
        for bill in bills:
            policy = (bill.get("policyArea") or {}).get("name", "Other")
            if policy and policy != "None":
                policy_areas[policy] = policy_areas.get(policy, 0) + 1

            # Only keep the most recent `limit` bills for display
            if len(sponsored) < limit:
                sponsored.append({
                    "congress": bill.get("congress"),
                    "type": (bill.get("type") or "").lower(),
                    "number": bill.get("number"),
                    "title": bill.get("title", ""),
                    "latest_action": (bill.get("latestAction") or {}).get("text", ""),
                    "date": (bill.get("latestAction") or {}).get("actionDate", ""),
                    "policy_area": policy,
                })

    # Total counts
    try:
        r2 = congress_get(sponsored_url, params={
            "api_key": CONGRESS_API_KEY, "format": "json", "limit": 1
        }, timeout=10)
        sponsored_count = r2.json().get("pagination", {}).get("count", 0) if r2.status_code == 200 else 0
    except (CongressOutageError, Exception):
        sponsored_count = 0

    try:
        r3 = congress_get(cosponsored_url, params={
            "api_key": CONGRESS_API_KEY, "format": "json", "limit": 1
        }, timeout=10)
        cosponsored_count = r3.json().get("pagination", {}).get("count", 0) if r3.status_code == 200 else 0
    except (CongressOutageError, Exception):
        cosponsored_count = 0

    log_action(
        agent_name="member_search",
        action="fetch_member_legislation",
        input_data={"bioguide_id": bioguide_id},
        output_data={"sponsored_count": sponsored_count, "cosponsored_count": cosponsored_count}
    )

    return {
        "sponsored": sponsored,
        "sponsored_count": sponsored_count,
        "cosponsored_count": cosponsored_count,
        "policy_areas": policy_areas,
    }
if __name__ == "__main__":
    print("MEMBER SEARCH TEST")
    print("-" * 40)
    
    member = search_member("Ted Kennedy")
    if member:
        print(f"Found: {member['name']}")
        print(f"Bioguide: {member['bioguide_id']}")
        print(f"Party: {member['party']} · State: {member['state']}")
        print()
        
        profile = fetch_member_profile(member['bioguide_id'])
        if profile:
            print(f"Years served: {profile['years_served']}")
            print(f"Chambers: {profile['chambers']}")
            print(f"Photo URL: {profile['photo_url'][:70]}")
            print()
        
        legislation = fetch_member_legislation(member['bioguide_id'])
        print(f"Sponsored: {legislation['sponsored_count']}")
        print(f"Cosponsored: {legislation['cosponsored_count']}")
        print(f"Policy areas: {legislation['policy_areas']}")
        print()
        print("Recent bills:")
        for bill in legislation['sponsored'][:3]:
            print(f"  {bill['type'].upper()}{bill['number']} — {bill['title'][:60]}")
    else:
        print("Member not found")