import json
import threading
from datetime import datetime

from correspondence.db import _cursor

_lock = threading.Lock()

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
        with _cursor() as cur:
            cur.execute("SELECT *, bill AS bill_id FROM flags ORDER BY timestamp DESC")  # monitor.js reads bill_id
            return cur.fetchall()
    except Exception as e:
        print(f"[FLAG] Error fetching flags: {e}")
        return []

def _append(entry):
    # The live table names the bill column `bill`, not `bill_id`, and makes
    # `query` NOT NULL. Until 2026-09-26 every bill flag failed on both, and
    # the error was only printed.
    row = (
        entry["timestamp"],
        entry["event"],
        entry.get("query") or "",
        json.dumps(entry["results_shown"]) if "results_shown" in entry else None,
        entry.get("reason"),
        entry.get("notes"),
        entry.get("bill_id"),
        entry.get("flagged_section"),
        entry.get("congress"),
    )
    with _lock:
        try:
            with _cursor() as cur:
                cur.execute("""
                    INSERT INTO flags (timestamp, event, query, results_shown, reason,
                                       notes, bill, flagged_section, congress)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """, row)
            print(f"[FLAG] Logged: {entry['event']} — {entry.get('query') or entry.get('bill_id')}")
        except Exception as e:
            print(f"[FLAG] Error logging flag: {e}")
