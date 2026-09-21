import os
import threading
from datetime import datetime

_lock = threading.Lock()

def _get_client():
    from supabase import create_client  # lazy: ~12 MB import
    url = os.environ["SUPABASE_URL"]
    key = os.environ["SUPABASE_API_KEY"]
    return create_client(url, key)

def log_search_flag(query, results_shown, reason, notes=""):
    """Log when a user flags search results as unhelpful."""
    entry = {
        "timestamp": datetime.now().isoformat(),
        "event": "search_flag",
        "query": query,
        "results_shown": results_shown,
        "reason": reason,
        "notes": notes,
    }
    _append(entry)

def log_bill_flag(bill_id, congress, bill_type, reason, notes="", flagged_section="translation"):
    """Log when a user flags a bill translation or timeline as wrong."""
    entry = {
        "timestamp": datetime.now().isoformat(),
        "event": "bill_flag",
        "bill_id": bill_id,
        "congress": str(congress),
        "flagged_section": flagged_section,
        "reason": reason,
        "notes": notes,
    }
    _append(entry)

def get_flags():
    """Return all flags."""
    try:
        client = _get_client()
        response = client.table("flags").select("*").order("timestamp", desc=True).execute()
        return response.data
    except Exception as e:
        print(f"[FLAG] Error fetching flags: {e}")
        return []

def _append(entry):
    with _lock:
        try:
            client = _get_client()
            client.table("flags").insert(entry).execute()
            print(f"[FLAG] Logged: {entry['event']} — {entry.get('query') or entry.get('bill_id')}")
        except Exception as e:
            print(f"[FLAG] Error logging flag: {e}")