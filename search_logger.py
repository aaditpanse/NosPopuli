import json
import os
import threading
from datetime import datetime

SEARCH_LOG_FILE = "search_log.jsonl"
_LEGACY_FILE = "search_log.json"
_lock = threading.Lock()

def log_search(query, query_type, expanded_terms, results_count, result_ids, confidence=1.0):
    entry = {
        "timestamp": datetime.now().isoformat(),
        "event": "search",
        "query": query,
        "query_type": query_type,
        "confidence": confidence,
        "expanded_terms": expanded_terms or [],
        "results_count": results_count,
        "result_ids": result_ids[:5] if result_ids else []
    }
    _append(entry)

def log_bill_opened(bill_id, title, from_query):
    """Log when a user opens a bill detail page."""
    entry = {
        "timestamp": datetime.now().isoformat(),
        "event": "bill_opened",
        "bill_id": bill_id,
        "title": title,
        "from_query": from_query
    }
    _append(entry)

def log_member_opened(bioguide_id, name, from_query):
    """Log when a user opens a member profile."""
    entry = {
        "timestamp": datetime.now().isoformat(),
        "event": "member_opened",
        "bioguide_id": bioguide_id,
        "name": name,
        "from_query": from_query
    }
    _append(entry)

def get_log():
    """Return full search log (analyst_agent reads it whole; nothing else)."""
    with _lock:
        _migrate_legacy()
    out = []
    try:
        with open(SEARCH_LOG_FILE, "r") as f:
            for line in f:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        pass
    return out


def clear_log():
    with _lock:
        open(SEARCH_LOG_FILE, "w").close()
        if os.path.exists(_LEGACY_FILE):
            os.replace(_LEGACY_FILE, _LEGACY_FILE + ".migrated")


def _migrate_legacy():
    if os.path.exists(SEARCH_LOG_FILE) or not os.path.exists(_LEGACY_FILE):
        return
    try:
        with open(_LEGACY_FILE, "r") as f:
            entries = json.load(f)
    except (OSError, ValueError):
        entries = []
    with open(SEARCH_LOG_FILE, "w") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")
    os.replace(_LEGACY_FILE, _LEGACY_FILE + ".migrated")

def _append(entry):
    line = json.dumps(entry, default=str) + "\n"
    with _lock:
        _migrate_legacy()
        with open(SEARCH_LOG_FILE, "a") as f:
            f.write(line)