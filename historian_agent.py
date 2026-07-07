import requests
import os
import json
import hashlib
from dotenv import load_dotenv
from cachetools import TTLCache, cached
from threading import RLock
from documentor_agent import log_action
from correspondence.db import get_disk_cache, set_disk_cache

load_dotenv()

CONGRESS_API_KEY = os.getenv("CONGRESS_API_KEY")

# The prose timeline is a Haiku call that's stable for a given set of actions.
# Key the cache on a hash of the actions themselves so it auto-invalidates the
# moment the bill takes a new action, with no TTL guesswork.
_HISTORY_CACHE_TTL_SECONDS = 30 * 24 * 3600  # 30 days


def _history_cache_key(actions):
    payload = json.dumps(
        [(a.get("actionDate"), a.get("text")) for a in actions],
        sort_keys=True,
    )
    return "hist:v1:" + hashlib.sha1(payload.encode()).hexdigest()

_session = requests.Session()
_actions_cache = TTLCache(maxsize=256, ttl=1800)

@cached(cache=_actions_cache, lock=RLock())
def fetch_bill_actions(congress_number, bill_type, bill_number):
    """Gets the full action history of a bill - every step it took through Congress"""

    url = f"https://api.congress.gov/v3/bill/{congress_number}/{bill_type}/{bill_number}/actions"
    params = {"api_key": CONGRESS_API_KEY, "format": "json", "limit": 20}

    try:
        response = _session.get(url, params=params, timeout=10)
    except requests.exceptions.Timeout:
        print(f"[HISTORIAN] Timeout fetching actions for {bill_type}{bill_number}")
        return None
    except Exception as e:
        print(f"[HISTORIAN] Error fetching actions: {e}")
        return None

    if response.status_code == 429:
        print(f"[HISTORIAN] Rate limited fetching actions")
        return None
    if response.status_code != 200:
        print(f"[HISTORIAN] Error {response.status_code} fetching actions")
        return None

    try:
        data = response.json()
    except Exception:
        return None

    actions = data.get("actions", [])

    log_action(
        agent_name="historian",
        action="fetch_bill_actions",
        input_data={"congress": congress_number, "type": bill_type, "number": bill_number},
        output_data={"action_count": len(actions)}
    )

    return actions

def fetch_related_bills(congress_number, bill_type, bill_number):
    """Finds bills related to this one"""

    url = f"https://api.congress.gov/v3/bill/{congress_number}/{bill_type}/{bill_number}/relatedbills"
    params = {"api_key": CONGRESS_API_KEY, "format": "json", "limit": 5}

    try:
        response = _session.get(url, params=params, timeout=10)
    except requests.exceptions.Timeout:
        print(f"[HISTORIAN] Timeout fetching related bills")
        return None
    except Exception as e:
        print(f"[HISTORIAN] Error fetching related bills: {e}")
        return None

    if response.status_code == 429:
        print(f"[HISTORIAN] Rate limited fetching related bills")
        return None
    if response.status_code != 200:
        return None

    try:
        data = response.json()
    except Exception:
        return None

    related = data.get("relatedBills", [])

    log_action(
        agent_name="historian",
        action="fetch_related_bills",
        input_data={"congress": congress_number, "type": bill_type, "number": bill_number},
        output_data={"related_count": len(related)}
    )

    return related

def summarize_history(actions, client):
    """Uses AI to turn the raw action list into a readable timeline"""
    
    if not actions:
        return "No action history available."

    cache_key = _history_cache_key(actions)
    try:
        cached_summary = get_disk_cache(cache_key, _HISTORY_CACHE_TTL_SECONDS)
        if cached_summary:
            return cached_summary
    except Exception as e:
        print(f"[HISTORIAN] Timeline cache read error: {e}")

    action_text = "\n".join([
        f"{a['actionDate']}: {a['text']}"
        for a in actions
    ])
    
    prompt = f"""
    You are a legislative historian.
    Your only job is to explain how a bill moved through Congress
    in plain chronological language an ordinary person can follow.
    Be concise. Use plain English. No jargon.
    
    Raw action history:
    {action_text}
    
    Write a short readable timeline of how this bill became law.
    """
    
    message = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        messages=[
            {"role": "user", "content": prompt}
        ]
    )
    
    summary = message.content[0].text

    try:
        set_disk_cache(cache_key, summary)
    except Exception as e:
        print(f"[HISTORIAN] Timeline cache write error: {e}")

    log_action(
        agent_name="historian",
        action="summarize_history",
        input_data={"action_count": len(actions)},
        output_data={"preview": summary[:100]}
    )

    return summary

def make_event_title(text):
    text = text.strip()

    for sep in [', and in addition', '; and in addition', ', for a period', '. For a period']:
        if sep in text:
            text = text[:text.index(sep)]

    text = text.rstrip('.,;')

    if len(text) > 120:
        text = text[:120].rsplit(' ', 1)[0] + '…'

    return text

def structure_history(actions):
    """Returns actions as structured list for frontend timeline rendering."""
    if not actions:
        return []

    import re
    seen = set()
    structured = []
    for a in actions:
        text = a.get("text", "")
        date = a.get("actionDate", "")
        key = f"{date}:{text[:80]}"
        if key in seen:
            continue
        seen.add(key)

        chamber = a.get("chamber", "")

        text_lower = text.lower()
        if any(w in text_lower for w in ["became public law", "signed by president", "presented to president"]):
            event_type = "signed"
        elif any(w in text_lower for w in ["passed house", "passed senate", "agreed to in"]):
            event_type = "passed"
        elif any(w in text_lower for w in ["committee", "reported by"]):
            event_type = "committee"
        elif "introduced" in text_lower:
            event_type = "introduced"
        elif "referred" in text_lower:
            event_type = "referred"
        elif "conference" in text_lower:
            event_type = "conference"
        elif "vetoed" in text_lower:
            event_type = "vetoed"
        else:
            event_type = "action"

        yea = nay = None
        vote_match = re.search(
            r'(?:yeas? and nays?|yea-nay vote|recorded vote)[^\d]*(\d+)\s*[-–]\s*(\d+)',
            text,
            re.IGNORECASE
        )
        if vote_match:
            yea = int(vote_match.group(1))
            nay = int(vote_match.group(2))
        else:
            vote_match2 = re.search(
                r':\s*(\d{1,3})\s*[-–]\s*(\d{1,3})(?:\s*\(Roll|\s*Record)',
                text,
                re.IGNORECASE
            )
            if vote_match2:
                yea = int(vote_match2.group(1))
                nay = int(vote_match2.group(2))

        raw = a.get("text", "")
        structured.append({
            "date": date,
            "text": make_event_title(raw),
            "detail": raw if len(raw) > 120 else None,
            "chamber": chamber,
            "event_type": event_type,
            "yea": yea,
            "nay": nay,
        })

    return structured

if __name__ == "__main__":
    import anthropic
    
    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    
    print("HISTORIAN AGENT")
    print("-" * 40)
    
    actions = fetch_bill_actions(111, "hr", 3590)
    related = fetch_related_bills(111, "hr", 3590)
    
    print(f"Found {len(actions)} actions in history")
    print(f"Found {len(related)} related bills")
    print()
    print("TIMELINE:")
    print("-" * 40)
    timeline = summarize_history(actions, client)
    print(timeline)
    
    if related:
        print()
        print("RELATED BILLS:")
        print("-" * 40)
        for bill in related[:3]:
            print(f"- {bill.get('title', 'No title')} ({bill.get('relationshipDetails', [{}])[0].get('type', 'Related')})")