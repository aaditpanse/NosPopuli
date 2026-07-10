"""Extractor artifact: Pittsburgh City Council via the Legistar Web API.

This is extractor v1 for source `pittsburgh-legistar`, hand-written for M0.
At M1 this file is what the synthesis loop generates; at M2 it is what the
repair loop rewrites. It is deliberately boring: deterministic, no LLM,
maps API responses into the domain schema and nothing else.

Discovery notes (the "source profile", spec module 1):
  - Cheapest rung: a real API (webapi.legistar.com/v1/pittsburgh), no key.
  - Second independent-ish source: the clerk's published minutes PDF,
    linked from each event (EventMinutesFile). Same vendor hosts both, so
    this is a weaker oracle than a truly independent publisher — but the
    minutes are a separately-authored clerk document, which is what makes
    reconciliation meaningful. Recorded honestly here per the spec.
"""

import time

import requests

import schema

SOURCE_ID = "pittsburgh-legistar"
EXTRACTOR_VERSION = "1"
API = "https://webapi.legistar.com/v1/pittsburgh"

# Legistar vote/rollcall value names → schema position vocabulary.
POSITION_MAP = {
    "Aye": "aye", "Yea": "aye",
    "No": "no", "Nay": "no",
    "Abstain": "abstain",
    "Absent": "absent",
    "Present": "present",
    "Recused": "recused", "Recuse": "recused",
}


def _get(path, params=None, retries=3):
    for attempt in range(retries):
        try:
            r = requests.get(API + path, params=params, timeout=30)
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, ValueError):
            if attempt == retries - 1:
                raise
            time.sleep(2 * (attempt + 1))


def recent_final_meetings(top=3):
    """Event stubs for the most recent City Council meetings with final minutes."""
    return _get("/events", params={
        "$top": top,
        "$orderby": "EventDate desc",
        "$filter": "EventBodyId eq 1 and EventMinutesStatusName eq 'Final'",
    })


def extract(event_ids):
    """Run the extractor over the given Legistar event ids.

    Returns (records, run_meta). records is
    {"meetings": [...], "agenda_items": [...], "vote_events": [...],
     "members": [...]} in schema v1 shapes. run_meta carries the row counts
    and timings the validation harness's delta layer consumes (spec module 4).
    """
    t0 = time.time()
    meetings, agenda_items, vote_events = [], [], []
    members = {}  # name -> member record, deduped across meetings

    for event_id in event_ids:
        event = _get(f"/events/{event_id}")
        items = _get(f"/events/{event_id}/eventitems")
        date = event["EventDate"][:10]

        attendance = {}
        for item in items:
            if item.get("EventItemRollCallFlag"):
                for rc in _get(f"/eventitems/{item['EventItemId']}/rollcalls"):
                    status = "present" if rc["RollCallValueName"] == "Present" else "absent"
                    attendance[rc["RollCallPersonName"]] = status
                    members.setdefault(rc["RollCallPersonName"],
                                       {"name": rc["RollCallPersonName"],
                                        "person_id": rc["RollCallPersonId"]})
                time.sleep(0.2)

        meetings.append({
            "meeting_id": f"{SOURCE_ID}-{event_id}",
            "body": event["EventBodyName"].strip(),
            "date": date,
            "time": event.get("EventTime"),
            "location": event.get("EventLocation"),
            "attendance": attendance,
            "source_url": event.get("EventInSiteURL"),
            "minutes_url": event.get("EventMinutesFile"),
        })

        for item in items:
            file_number = item.get("EventItemMatterFile")
            if not file_number:
                continue  # procedural rows (ROLL CALL, section headers) are not records
            flag = item.get("EventItemPassedFlagName")
            item_id = f"{SOURCE_ID}-item-{item['EventItemId']}"
            agenda_items.append({
                "item_id": item_id,
                "meeting_id": f"{SOURCE_ID}-{event_id}",
                "file_number": file_number,
                "title": (item.get("EventItemTitle") or "").strip(),
                "action": item.get("EventItemActionName"),
                "result": {"Pass": "pass", "Fail": "fail"}.get(flag),
                "enactment_number": None,  # API doesn't carry it; minutes do
            })

            if flag is None:
                continue  # no final action -> no recorded vote to fetch
            votes = _get(f"/eventitems/{item['EventItemId']}/votes")
            time.sleep(0.2)
            if not votes:
                continue  # voice vote / motion; minutes show no Aye block either
            positions, counts = [], {}
            for v in votes:
                pos = POSITION_MAP.get(v["VoteValueName"], v["VoteValueName"].lower())
                positions.append({"member": v["VotePersonName"], "position": pos})
                counts[pos] = counts.get(pos, 0) + 1
                members.setdefault(v["VotePersonName"],
                                   {"name": v["VotePersonName"],
                                    "person_id": v["VotePersonId"]})
            vote_events.append({
                "vote_id": f"{SOURCE_ID}-vote-{item['EventItemId']}",
                "meeting_id": f"{SOURCE_ID}-{event_id}",
                "item_id": item_id,
                "file_number": file_number,
                "positions": positions,
                "counts": counts,
                "result": {"Pass": "pass", "Fail": "fail"}[flag],
            })

    records = {
        "meetings": meetings,
        "agenda_items": agenda_items,
        "vote_events": vote_events,
        "members": sorted(members.values(), key=lambda m: m["name"]),
    }
    run_meta = {
        "source_id": SOURCE_ID,
        "extractor_version": EXTRACTOR_VERSION,
        "schema_version": schema.SCHEMA_VERSION,
        "event_ids": list(event_ids),
        "row_counts": {k: len(v) for k, v in records.items()},
        "elapsed_seconds": round(time.time() - t0, 1),
    }
    return records, run_meta
