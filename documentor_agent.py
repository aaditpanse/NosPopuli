import json
import os
from datetime import datetime
import threading

# Append-only JSON Lines. The old format was one JSON array rewritten in full
# (read, append, dump with indent=2) on every agent call — O(file) memory and
# time per call, behind a global lock.
LOG_FILE = "agent_log.jsonl"
_LEGACY_FILE = "agent_log.json"
_lock = threading.Lock()


def _migrate_legacy():
    """One-time: turn an existing agent_log.json array into JSONL."""
    if os.path.exists(LOG_FILE) or not os.path.exists(_LEGACY_FILE):
        return
    try:
        with open(_LEGACY_FILE, "r") as f:
            entries = json.load(f)
    except (OSError, ValueError):
        entries = []
    with open(LOG_FILE, "w") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")
    os.replace(_LEGACY_FILE, _LEGACY_FILE + ".migrated")


def log_action(agent_name, action, input_data, output_data):
    entry = {
        "timestamp": datetime.now().isoformat(),
        "agent": agent_name,
        "action": action,
        "input": input_data,
        "output": output_data
    }
    line = json.dumps(entry, default=str) + "\n"
    with _lock:
        _migrate_legacy()
        with open(LOG_FILE, "a") as f:
            f.write(line)
    print(f"[DOCUMENTOR] Logged: {agent_name} → {action}")


def read_log(after=0, limit=None):
    """Entries appended after byte offset `after`, and the new offset.
    Readers poll with the offset they were handed, so the whole file is never
    re-read or re-sent."""
    with _lock:
        _migrate_legacy()
    entries = []
    try:
        with open(LOG_FILE, "rb") as f:
            f.seek(max(0, after))
            for raw in f:
                if not raw.endswith(b"\n"):
                    break  # partial line from a concurrent writer; next poll
                try:
                    entries.append(json.loads(raw))
                except ValueError:
                    continue
                if limit and len(entries) >= limit:
                    break
            offset = f.tell()
    except FileNotFoundError:
        return [], 0
    return entries, offset


def clear_log():
    with _lock:
        open(LOG_FILE, "w").close()
