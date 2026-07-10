"""Extractor for source `pittsburgh-legistar` (Pittsburgh City Council,
Legistar Web API) targeting Foundry domain schema v1.

Deterministic, stdlib-only. All HTTP goes through the injected `fetch_json`
callable, which GETs
    https://webapi.legistar.com/v1/pittsburgh{path}
and returns parsed JSON.

Repair note (v3): the upstream source renamed the per-item file-number field.
Event items now carry the matter/file number in `EventItemFile`
(e.g. "2026-0672") rather than the previous `EventItemMatterFile`. Because
the old code keyed record emission on `EventItemMatterFile`, every item was
treated as procedural and skipped, producing zero agenda_items and zero
vote_events. Reading `EventItemFile` (with a fallback to the legacy key)
restores the records.
"""

import re
import json
import hashlib

EXTRACTOR_VERSION = "3"
SOURCE_ID = "pittsburgh-legistar"
SCHEMA_VERSION = "1"

_FILE_NUMBER_RE = re.compile(r"^\d{4}-\d{4}$")

# Legistar vote-value names -> schema POSITIONS vocabulary.
_POSITION_MAP = {
    "aye": "aye",
    "yea": "aye",
    "yes": "aye",
    "y": "aye",
    "no": "no",
    "nay": "no",
    "n": "no",
    "abstain": "abstain",
    "abstention": "abstain",
    "absent": "absent",
    "present": "present",
    "recused": "recused",
    "recuse": "recused",
    "recusal": "recused",
}


# ---------------------------------------------------------------------------
# small deterministic helpers
# ---------------------------------------------------------------------------

def _date(value):
    """'2026-06-30T00:00:00' -> '2026-06-30'; None-safe."""
    if not value:
        return None
    s = str(value)
    return s[:10] if len(s) >= 10 else s


def _file_number(it):
    """Read the item's matter/file number.

    The changed source exposes this as `EventItemFile`. Older responses used
    `EventItemMatterFile`; we fall back to it so historical fixtures still
    reconcile.
    """
    val = it.get("EventItemFile")
    if val is None:
        val = it.get("EventItemMatterFile")
    if val is None:
        return None
    s = str(val).strip()
    return s or None


def _norm_position(name):
    """Legistar vote value name -> POSITIONS token, or None if unknown."""
    if not name:
        return None
    return _POSITION_MAP.get(str(name).strip().lower())


def _norm_attendance(name):
    """Roll-call value name -> 'present' / 'absent' (schema attendance)."""
    if not name:
        return None
    key = str(name).strip().lower()
    if "present" in key:
        return "present"
    if "absent" in key:
        return "absent"
    # Anything else (e.g. excused/late) is treated as not-present.
    return "absent"


def _norm_result(passed_flag_name):
    """Legistar passed-flag name -> 'pass' / 'fail' / None."""
    if not passed_flag_name:
        return None
    key = str(passed_flag_name).strip().lower()
    if key in ("pass", "passed"):
        return "pass"
    if key in ("fail", "failed"):
        return "fail"
    return None


def _derive_result(counts):
    """Fallback result from tallies when no passed-flag is recorded."""
    return "pass" if counts.get("aye", 0) > counts.get("no", 0) else "fail"


def _make_run_id(event_ids):
    seed = "|".join(str(e) for e in event_ids) + "|" + EXTRACTOR_VERSION + "|" + SCHEMA_VERSION
    return "run-" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]


def _attach(record, run_id):
    """Add a provenance + certification block (spec: Feature module 8)."""
    digest = hashlib.sha256(
        json.dumps(record, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
    record["provenance"] = {
        "source_id": SOURCE_ID,
        "extractor_version": EXTRACTOR_VERSION,
        "run_id": run_id,
        "certification": {"algorithm": "sha256", "content_hash": digest},
    }
    return record


def _call(fetch_json, path):
    """fetch_json wrapper that always yields a list where a list is expected."""
    data = fetch_json(path)
    return data if data else []


# ---------------------------------------------------------------------------
# main entry point
# ---------------------------------------------------------------------------

def extract(fetch_json, event_ids):
    run_id = _make_run_id(event_ids)

    meetings = []
    agenda_items = []
    vote_events = []
    members = {}  # key -> {"name":..., "person_id":...}

    def record_person(pid, name):
        if pid is None and not name:
            return
        key = ("id", pid) if pid is not None else ("name", name)
        if key not in members:
            members[key] = {"name": name, "person_id": pid}

    for eid in event_ids:
        event = fetch_json(f"/events/{eid}")
        if not event:
            continue

        meeting_id = f"{SOURCE_ID}-{event['EventId']}"
        items = _call(fetch_json, f"/events/{eid}/eventitems")

        # --- attendance from roll-call item(s) -----------------------------
        attendance = {}
        for it in items:
            if it.get("EventItemRollCallFlag"):
                rolls = _call(fetch_json, f"/eventitems/{it['EventItemId']}/rollcalls")
                for r in rolls:
                    name = r.get("RollCallPersonName")
                    pid = r.get("RollCallPersonId")
                    record_person(pid, name)
                    status = _norm_attendance(r.get("RollCallValueName"))
                    if name and status:
                        attendance[name] = status

        meeting = {
            "meeting_id": meeting_id,
            "body": event.get("EventBodyName"),
            "date": _date(event.get("EventDate")),
            "attendance": attendance,
            "source_url": event.get("EventInSiteURL"),
            # helpful extras (not required by schema)
            "legistar_event_id": event.get("EventId"),
            "time": event.get("EventTime"),
            "location": event.get("EventLocation"),
            "agenda_url": event.get("EventAgendaFile"),
            "minutes_url": event.get("EventMinutesFile"),
        }
        meetings.append(_attach(meeting, run_id))

        # --- agenda items + vote events ------------------------------------
        for it in items:
            file_number = _file_number(it)
            if not file_number:
                continue  # procedural rows (roll call, headings) are not records

            item_id = f"{SOURCE_ID}-item-{it['EventItemId']}"

            agenda_item = {
                "item_id": item_id,
                "meeting_id": meeting_id,
                "file_number": file_number,
                "title": it.get("EventItemTitle"),
                "action": it.get("EventItemActionName"),
                "result": _norm_result(it.get("EventItemPassedFlagName")),
                # extras
                "matter_type": it.get("EventItemMatterType"),
                "matter_status": it.get("EventItemMatterStatus"),
                "action_text": it.get("EventItemActionText"),
                "agenda_sequence": it.get("EventItemAgendaSequence"),
                "legistar_matter_id": it.get("EventItemMatterId"),
            }
            agenda_items.append(_attach(agenda_item, run_id))

            # recorded per-member vote (empty list where none exists)
            votes = _call(fetch_json, f"/eventitems/{it['EventItemId']}/votes")
            if not votes:
                continue

            positions = []
            counts = {}
            for v in votes:
                name = v.get("VotePersonName")
                pid = v.get("VotePersonId")
                record_person(pid, name)
                pos = _norm_position(v.get("VoteValueName"))
                if pos and name:
                    positions.append({"member": name, "position": pos})
                    counts[pos] = counts.get(pos, 0) + 1

            result = _norm_result(it.get("EventItemPassedFlagName"))
            if result is None:
                result = _derive_result(counts)

            vote_event = {
                "vote_id": f"{SOURCE_ID}-vote-{it['EventItemId']}",
                "meeting_id": meeting_id,
                "item_id": item_id,
                "file_number": file_number,
                "positions": positions,
                "counts": counts,
                "result": result,
            }
            vote_events.append(_attach(vote_event, run_id))

    # --- members: one per distinct person, sorted by name ------------------
    member_records = [
        _attach(dict(m), run_id)
        for m in sorted(members.values(), key=lambda m: (m["name"] or ""))
    ]

    records = {
        "meetings": meetings,
        "agenda_items": agenda_items,
        "vote_events": vote_events,
        "members": member_records,
    }

    run_meta = {
        "source_id": SOURCE_ID,
        "extractor_version": EXTRACTOR_VERSION,
        "schema_version": SCHEMA_VERSION,
        "event_ids": list(event_ids),
        "run_id": run_id,
        "row_counts": {
            "meetings": len(meetings),
            "agenda_items": len(agenda_items),
            "vote_events": len(vote_events),
            "members": len(member_records),
        },
    }

    return records, run_meta
