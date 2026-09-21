import re
from agents.documentor_agent import log_action

def parse_vote_references(actions):
    """
    Scans bill actions for roll call vote references.
    Returns house and senate vote references if found.
    Both can be present, one, or neither.
    """
    
    house_vote = None
    senate_vote = None
    
    for action in actions:
        text = action.get("text", "")
        date = action.get("actionDate", "")
        recorded_votes = action.get("recordedVotes", [])
        
        # ── House: look for Roll no. pattern ──
        if not house_vote:
            house_patterns = [
                r"[Rr]oll\s+[Nn]o\.?\s*:?\s*(\d+)",
                r"[Rr]oll\s+[Cc]all\s+[Nn]o\.?\s*:?\s*(\d+)",
                r"[Rr]ecord(?:ed)?\s+[Vv]ote\s+[Nn]o\.?\s*:?\s*(\d+)",
            ]
            for pattern in house_patterns:
                match = re.search(pattern, text)
                if match and date:
                    year = int(date[:4])
                    congress = action.get("congress") or _year_to_congress(year)
                    session = 1 if year % 2 != 0 else 2
                    roll = int(match.group(1))
                    
                    # Check if there's a direct URL in recordedVotes
                    url = None
                    for rv in (recorded_votes or []):
                        if isinstance(rv, dict) and "house" in rv.get("chamber", "").lower():
                            url = rv.get("url")
                            break
                    
                    house_vote = {
                        "roll": roll,
                        "session": session,
                        "year": year,
                        "congress": congress,
                        "url": url
                    }
                    break
        
        # ── Senate: look for Record Vote Number pattern ──
        if not senate_vote:
            senate_patterns = [
                r"[Rr]ecord\s+[Vv]ote\s+[Nn]umber\s*:?\s*(\d+)",
                r"[Rr]ecorded\s+[Vv]ote\s+[Nn]umber\s*:?\s*(\d+)",
                r"[Ss]enate\s+[Vv]ote\s+[Nn]umber\s*:?\s*(\d+)",
            ]
            for pattern in senate_patterns:
                match = re.search(pattern, text)
                if match and date:
                    year = int(date[:4])
                    congress = action.get("congress") or _year_to_congress(year)
                    session = 1 if year % 2 != 0 else 2
                    roll = int(match.group(1))
                    
                    senate_vote = {
                        "roll": roll,
                        "session": session,
                        "year": year,
                        "congress": congress,
                    }
                    break
        
        # Also check recordedVotes field directly for URLs
        for rv in (recorded_votes or []):
            if not isinstance(rv, dict):
                continue
            chamber = rv.get("chamber", "").lower()
            url = rv.get("url", "")
            roll = rv.get("rollNumber")
            date_str = rv.get("date", date)
            year = int(date_str[:4]) if date_str else None
            
            if roll and year:
                session = 1 if year % 2 != 0 else 2
                congress = _year_to_congress(year)
                
                if "house" in chamber and not house_vote:
                    house_vote = {
                        "roll": int(roll),
                        "session": session,
                        "year": year,
                        "congress": congress,
                        "url": url
                    }
                elif "senate" in chamber and not senate_vote:
                    senate_vote = {
                        "roll": int(roll),
                        "session": session,
                        "year": year,
                        "congress": congress,
                        "url": url
                    }
    
    result = {
        "house": house_vote,
        "senate": senate_vote
    }
    
    log_action(
        agent_name="vote_parser",
        action="parse_vote_references",
        input_data={"action_count": len(actions)},
        output_data={
            "house_found": house_vote is not None,
            "senate_found": senate_vote is not None,
            "house_roll": house_vote["roll"] if house_vote else None,
            "senate_roll": senate_vote["roll"] if senate_vote else None,
        }
    )
    
    return result

def _year_to_congress(year):
    """Convert a year to congress number."""
    # 117th Congress: 2021-2022, 118th: 2023-2024, etc.
    return ((year - 1789) // 2) + 1

if __name__ == "__main__":
    # Test with the ACA actions we already know work
    from sources.bill_fetcher import fetch_bill
    from agents.historian_agent import fetch_bill_actions
    
    print("VOTE PARSER TEST")
    print("-" * 40)
    
    # Test with ACA - known to have recorded votes
    actions = fetch_bill_actions(111, "hr", 3590)
    result = parse_vote_references(actions)
    
    print(f"ACA House vote: {result['house']}")
    print(f"ACA Senate vote: {result['senate']}")
    print()
    
    # Test with Postal Service Reform Act - we saw Senate vote 70 in the XML
    actions2 = fetch_bill_actions(117, "hr", 3076)
    result2 = parse_vote_references(actions2)
    
    print(f"Postal Service House vote: {result2['house']}")
    print(f"Postal Service Senate vote: {result2['senate']}")