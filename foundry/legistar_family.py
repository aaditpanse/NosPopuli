"""Legistar family: probe + preview-extract any Legistar Web API tenant.

The Pittsburgh work (M0-M2) proved the family; this module generalizes the
read side so a fresh tenant can be previewed straight from a search — recent
meetings, items, per-member votes — with zero LLM calls. A preview is
INGEST-ONLY by construction: no golden set, no second-source oracle, no
certification. Promoting a previewed tenant to a real source still goes
through the M-gates.

Deliberately dependency-free (requests only) so api.py can import it.
"""

import datetime
import time

import requests

API = "https://webapi.legistar.com/v1/{slug}"
POSITION_MAP = {"Aye": "aye", "Yea": "aye", "No": "no", "Nay": "no",
                "Abstain": "abstain", "Absent": "absent",
                "Present": "present", "Recused": "recused", "Recuse": "recused"}
NOTE = "search preview — ingest-only, tenant not onboarded (no oracle, no golden set)"


def _get(slug, path, params=None):
    r = requests.get(API.format(slug=slug) + path, params=params, timeout=25,
                     headers={"User-Agent": "nospopuli-foundry-lab"})
    r.raise_for_status()
    time.sleep(0.15)
    return r.json()


def probe(slug):
    """True if this slug is a live Legistar Web API tenant. NB: anything short
    of a 200 list means nothing — the API 500s with the same
    'LegistarConnectionString' error for every unknown slug, and
    *.legistar.com is wildcard DNS, so absence and disabled are
    indistinguishable from outside."""
    try:
        r = requests.get(API.format(slug=slug) + "/bodies", params={"$top": 1},
                         timeout=20, headers={"User-Agent": "nospopuli-foundry-lab"})
        return r.status_code == 200 and isinstance(r.json(), list)
    except Exception:
        return False


def preview(slug, top=5, progress=None):
    """Recent past meetings with items and recorded votes, newest first."""
    today = datetime.date.today().isoformat()
    events = _get(slug, "/events", {
        "$top": top, "$orderby": "EventDate desc",
        "$filter": f"EventDate le datetime'{today}'"})
    source_id = f"{slug}-legistar"
    quarantined = {"status": "quarantined", "method": None, "note": NOTE}
    meetings, agenda_items, vote_events, members = [], [], [], {}

    for n, event in enumerate(events):
        if progress:
            progress(n / max(len(events), 1))
        meeting_id = f"{source_id}-{event['EventId']}"
        items = _get(slug, f"/events/{event['EventId']}/eventitems")
        attendance = {}
        for item in items:
            if item.get("EventItemRollCallFlag"):
                for rc in _get(slug, f"/eventitems/{item['EventItemId']}/rollcalls"):
                    attendance[rc["RollCallPersonName"]] = \
                        "present" if rc["RollCallValueName"] == "Present" else "absent"
        meetings.append({
            "meeting_id": meeting_id,
            "body": (event.get("EventBodyName") or "").strip(),
            "date": event["EventDate"][:10],
            "attendance": attendance,
            "source_url": event.get("EventInSiteURL"),
            "minutes_url": event.get("EventMinutesFile"),
            "certification": dict(quarantined)})

        for item in items:
            if not item.get("EventItemMatterFile"):
                continue
            item_id = f"{source_id}-item-{item['EventItemId']}"
            flag = item.get("EventItemPassedFlagName")
            agenda_items.append({
                "item_id": item_id, "meeting_id": meeting_id,
                "file_number": item["EventItemMatterFile"],
                "title": (item.get("EventItemTitle") or "").strip(),
                "action": item.get("EventItemActionName"),
                "result": {"Pass": "pass", "Fail": "fail"}.get(flag),
                "certification": dict(quarantined)})
            if flag is None:
                continue
            votes = _get(slug, f"/eventitems/{item['EventItemId']}/votes")
            if not votes:
                continue
            positions, counts = [], {}
            for v in votes:
                pos = POSITION_MAP.get(v["VoteValueName"], v["VoteValueName"].lower())
                positions.append({"member": v["VotePersonName"], "position": pos})
                counts[pos] = counts.get(pos, 0) + 1
                members.setdefault(v["VotePersonName"], {"name": v["VotePersonName"]})
            vote_events.append({
                "vote_id": f"{source_id}-vote-{item['EventItemId']}",
                "meeting_id": meeting_id, "item_id": item_id,
                "file_number": item["EventItemMatterFile"],
                "positions": positions, "counts": counts,
                "result": {"Pass": "pass", "Fail": "fail"}[flag],
                "certification": dict(quarantined)})

    return {"meetings": meetings, "agenda_items": agenda_items,
            "vote_events": vote_events, "members": sorted(members.values(),
                                                          key=lambda m: m["name"])}
