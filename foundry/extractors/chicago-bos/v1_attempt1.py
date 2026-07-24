"""Deterministic extractor for source `chicago-bos`.

Chicago City Council via the City Clerk Electronic Legislative Management
System (eLMS) public REST API (Microsoft Power Platform / Dynamics 365
backend, Swagger 2.0, no auth). All meeting metadata, agenda matters, and
per-member roll-call votes are served as structured JSON, so vote_events
are derived from structured records and omit the `evidence` block (allowed
by the artifact contract for already-structured sources).

Only stdlib is used; all I/O flows through the injected runtime `rt`.

Re-pointing at another eLMS tenant: change BASE / PORTAL / BODY below.
"""

import datetime
import re

EXTRACTOR_VERSION = "1"

# ---- tenant-specific constants (defined once) ---------------------------
BASE = "https://api.chicityclerkelms.chicago.gov"          # machine API root
PORTAL = "https://chicityclerkelms.chicago.gov"            # human web portal
BODY = "City Council"
SOURCE_ID = "chicago-bos"
SCHEMA_VERSION = "1.5"

# Normalize eLMS vote values into the schema position vocabulary.
VOTE_MAP = {
    "yea": "aye", "aye": "aye", "yes": "aye", "y": "aye",
    "nay": "no", "no": "no", "n": "no",
    "abstain": "abstain", "abstained": "abstain", "abstention": "abstain",
    "present": "present",
    "absent": "absent", "not voting": "absent", "excused": "absent",
    "recused": "recused", "recuse": "recused", "recusal": "recused",
}

_OUTCOME_RE = re.compile(r"pass|adopt|approv|concur|carr|agreed|fail|"
                         r"reject|defeat|lost", re.I)


# ---------- small robust helpers -----------------------------------------
def _get(d, *keys):
    if not isinstance(d, dict):
        return None
    for k in keys:
        if k in d and d[k] not in (None, "", [], {}):
            return d[k]
    return None


def _rows(data):
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for k in ("value", "items", "results", "data", "meetings",
                  "records", "votes"):
            v = data.get(k)
            if isinstance(v, list):
                return v
    return []


def _items(rec):
    for k in ("items", "agendaItems", "matters", "lineItems", "agenda",
              "agendaItem", "matterList"):
        v = rec.get(k) if isinstance(rec, dict) else None
        if isinstance(v, list):
            return v
    return []


def _actions(item):
    for k in ("actions", "matterActions", "actionList", "history"):
        v = item.get(k) if isinstance(item, dict) else None
        if isinstance(v, list):
            return v
    return []


def _parse_date(s):
    if not s:
        return None
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", str(s))
    if m:
        try:
            return datetime.date(int(m.group(1)), int(m.group(2)),
                                 int(m.group(3)))
        except ValueError:
            return None
    m = re.search(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b", str(s))
    if m:
        try:
            return datetime.date(int(m.group(3)), int(m.group(1)),
                                 int(m.group(2)))
        except ValueError:
            return None
    return None


def _slug(s):
    if not s:
        return None
    s = re.sub(r"[^A-Za-z0-9]+", "-", str(s)).strip("-").lower()
    return s or None


def _clean_name(name):
    name = re.sub(r"\s+", " ", str(name)).strip()
    if not name:
        return None
    if any(ch.isdigit() for ch in name):
        return None
    if not any(ch.isalpha() for ch in name):
        return None
    return name


# ---------- vote handling -------------------------------------------------
def _positions(votes_raw):
    positions = []
    for v in votes_raw:
        if not isinstance(v, dict):
            continue
        raw_name = _get(v, "voterName", "personName", "name", "voter",
                        "member", "memberName")
        raw_vote = _get(v, "vote", "voteValue", "value", "position",
                        "voteText")
        if not raw_name or not raw_vote:
            continue
        name = _clean_name(raw_name)
        if not name:
            continue
        pos = VOTE_MAP.get(str(raw_vote).strip().lower())
        if pos is None:
            continue
        positions.append({"member": name, "position": pos})
    return positions


def _counts(positions):
    c = {}
    for p in positions:
        c[p["position"]] = c.get(p["position"], 0) + 1
    return c


def _has_outcome(item):
    for act in _actions(item):
        a = _get(act, "actionName", "action", "actionText", "actionType")
        if a and _OUTCOME_RE.search(str(a)):
            return True
    a = _get(item, "action", "actionName", "status", "lastAction")
    if a and _OUTCOME_RE.search(str(a)):
        return True
    return False


def _collect_votes(rt, mid, item, budget):
    # 1) votes embedded in an action
    for act in _actions(item):
        vs = act.get("votes") or act.get("voteList") or act.get("rollCall")
        if isinstance(vs, list) and vs:
            return vs, act
    # 2) votes directly on the item
    vs = item.get("votes")
    if isinstance(vs, list) and vs:
        return vs, None
    # 3) fetch the dedicated votes endpoint (only when there's a signal)
    matter_id = _get(item, "matterId", "lineId", "id", "matterID",
                     "matterGuid")
    signal = item.get("hasVotes") or _get(item, "voteType") or \
        _has_outcome(item)
    if matter_id and signal and budget[0] > 0:
        budget[0] -= 1
        url = "%s/meeting-agenda/%s/matter/%s/votes" % (BASE, mid, matter_id)
        try:
            data = rt.fetch_json(url)
            vs = _rows(data)
            if vs:
                return vs, None
        except Exception:
            pass
    return None, None


def _action_name(item, action_obj):
    if isinstance(action_obj, dict):
        a = _get(action_obj, "actionName", "action", "actionText",
                 "actionType", "result")
        if a:
            return re.sub(r"\s+", " ", str(a)).strip()
    a = _get(item, "action", "actionName", "lastAction", "actionText",
             "status")
    if a:
        return re.sub(r"\s+", " ", str(a)).strip()
    for act in _actions(item):
        a = _get(act, "actionName", "action", "actionText", "actionType")
        if a:
            return re.sub(r"\s+", " ", str(a)).strip()
    return None


def _explicit_result(item, action_obj):
    parts = []
    if isinstance(action_obj, dict):
        for k in ("result", "outcome", "actionName", "action", "actionText"):
            v = action_obj.get(k)
            if v is not None:
                parts.append(str(v))
    for k in ("action", "actionName", "status", "result"):
        v = item.get(k) if isinstance(item, dict) else None
        if v is not None:
            parts.append(str(v))
    blob = " ".join(parts).lower()
    if not blob:
        return None
    if re.search(r"fail|reject|defeat|not pass|lost", blob):
        return "fail"
    if re.search(r"pass|adopt|approv|concur|carr|agreed", blob):
        return "pass"
    return None


# ---------- enumeration ---------------------------------------------------
def _param_variants(skip):
    return [
        {"filter": "body eq '%s'" % BODY, "sort": "date desc",
         "top": 100, "skip": skip},
        {"$filter": "body eq '%s'" % BODY, "$orderby": "date desc",
         "$top": 100, "$skip": skip},
        {"top": 200, "skip": skip},
        None,
    ]


def _absorb(rows, seen, candidates, today):
    added = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        mid = _get(row, "meetingId", "id", "meetingAgendaId",
                   "meetingAgendaID", "meetingGuid")
        if not mid or mid in seen:
            continue
        seen.add(mid)
        added += 1
        body = _get(row, "body", "bodyName", "committee", "bodyDescription")
        if body and "city council" not in str(body).lower():
            continue
        d = _parse_date(_get(row, "date", "meetingDate", "startDateTime",
                             "meetingDateTime"))
        if d is None or d >= today:
            continue
        candidates.append((d, mid, row))
    return added


def _enumerate(rt, max_meetings):
    today = datetime.date.today()
    candidates = []
    seen = set()
    variant_index = None
    first_data = None

    # discover a working parameter variant
    for vi in range(len(_param_variants(0))):
        try:
            data = rt.fetch_json(BASE + "/meeting-agenda",
                                 params=_param_variants(0)[vi])
        except Exception:
            continue
        rows = _rows(data)
        if rows:
            variant_index = vi
            first_data = data
            _absorb(rows, seen, candidates, today)
            break
    if variant_index is None:
        return []

    skip = len(_rows(first_data))
    next_url = _get(first_data, "@odata.nextLink", "nextLink")
    target = max(max_meetings * 5, 15)
    guard = 0
    while len(candidates) < target and guard < 120:
        guard += 1
        try:
            if next_url:
                data = rt.fetch_json(next_url)
            else:
                data = rt.fetch_json(BASE + "/meeting-agenda",
                                     params=_param_variants(skip)[variant_index])
        except Exception:
            break
        rows = _rows(data)
        if not rows:
            break
        added = _absorb(rows, seen, candidates, today)
        if added == 0:
            break
        skip += len(rows)
        next_url = _get(data, "@odata.nextLink", "nextLink")

    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates


# ---------- per-meeting extraction ---------------------------------------
def _process_meeting(rt, d, mid, listing_row, members):
    data_url = "%s/meeting-agenda/%s" % (BASE, mid)
    rec = None
    try:
        rec = rt.fetch_json(data_url)
    except Exception:
        rec = None
    if not isinstance(rec, dict):
        if isinstance(listing_row, dict) and _items(listing_row):
            rec = listing_row
        else:
            return None

    rd = _parse_date(_get(rec, "date", "meetingDate", "startDateTime",
                          "meetingDateTime"))
    if rd is not None:
        d = rd
    if d >= datetime.date.today():
        return None

    items = _items(rec)
    if not items:
        return None

    iso = d.isoformat()
    meeting_id = "%s-%s" % (SOURCE_ID, iso)
    human_url = "%s/meeting/%s" % (PORTAL, mid)

    item_recs = []
    vote_recs = []
    attendance = {}          # name -> set(positions)
    used_slugs = {}
    vote_budget = [60]

    for item in items:
        if not isinstance(item, dict):
            continue

        src_fn = _get(item, "fileNumber", "recordNumber", "matterFileNumber",
                      "number", "matterRecordNumber")
        title_src = _get(item, "title", "subject", "matterTitle",
                         "matterName", "name", "description")
        matter_id = _get(item, "matterId", "lineId", "id", "matterID",
                         "matterGuid")

        votes_raw, action_obj = _collect_votes(rt, mid, item, vote_budget)
        positions = _positions(votes_raw) if votes_raw else []

        # procedural rows with no file number and no recorded vote: skip
        if not src_fn and not positions:
            continue

        base_slug = _slug(src_fn) or _slug(matter_id) or \
            ("item%d" % (len(used_slugs) + 1))
        n = used_slugs.get(base_slug, 0)
        used_slugs[base_slug] = n + 1
        slug = base_slug if n == 0 else "%s-%d" % (base_slug, n + 1)
        item_id = "%s-%s" % (meeting_id, slug)

        action_name = _action_name(item, action_obj) or "Considered"

        vote_result = None
        if positions:
            counts = _counts(positions)
            aye = counts.get("aye", 0)
            no = counts.get("no", 0)
            res = _explicit_result(item, action_obj)
            if res is None:
                res = "pass" if aye > no else "fail"
            vote_result = res
            for p in positions:
                attendance.setdefault(p["member"], set()).add(p["position"])
                members[p["member"]] = True
            vote_recs.append({
                "vote_id": "%s-v" % item_id,
                "meeting_id": meeting_id,
                "positions": positions,
                "counts": counts,
                "result": res,
                "source_url": human_url,
                "data_source_url": data_url,
            })

        # only emit an agenda_item when the source carries a real subject
        if title_src:
            title = re.sub(r"\s+", " ", str(title_src)).strip()
            if title:
                item_recs.append({
                    "item_id": item_id,
                    "meeting_id": meeting_id,
                    "title": title[:300],
                    "action": action_name,
                    "file_number": None,     # Chicago letter-prefixed ids
                    "result": vote_result,   # (O2026-...) don't fit the regex
                    "source_file_number": src_fn,
                    "source_url": human_url,
                    "data_source_url": data_url,
                })

    if not vote_recs:
        return None

    attendance_out = {}
    for name, poss in attendance.items():
        present = any(p != "absent" for p in poss)
        attendance_out[name] = "present" if present else "absent"
    if not attendance_out:
        return None

    meeting_rec = {
        "meeting_id": meeting_id,
        "body": BODY,
        "date": iso,
        "attendance": attendance_out,
        "source_url": human_url,
        "data_source_url": data_url,
        "file_number": None,
        "source_id": SOURCE_ID,
        "extractor_version": EXTRACTOR_VERSION,
    }
    return meeting_rec, item_recs, vote_recs


# ---------- entry point ---------------------------------------------------
def extract(rt, args):
    try:
        max_meetings = int(args[0])
    except Exception:
        max_meetings = 3
    if max_meetings < 2:
        max_meetings = 2

    run_id = "%s-run-%d" % (SOURCE_ID, max_meetings)

    meetings_out = []
    items_out = []
    votes_out = []
    members = {}
    used_meeting_ids = set()

    candidates = _enumerate(rt, max_meetings)

    fetch_cap = max_meetings * 6 + 6
    tried = 0
    for d, mid, row in candidates:
        if len(meetings_out) >= max_meetings:
            break
        if tried >= fetch_cap:
            break
        tried += 1
        try:
            result = _process_meeting(rt, d, mid, row, members)
        except Exception:
            result = None
        if result is None:
            continue
        meeting_rec, item_recs, vote_recs = result
        if meeting_rec["meeting_id"] in used_meeting_ids:
            continue
        used_meeting_ids.add(meeting_rec["meeting_id"])
        meetings_out.append(meeting_rec)
        items_out.extend(item_recs)
        votes_out.extend(vote_recs)

    members_out = [{"name": n} for n in sorted(members)]

    records = {
        "meetings": meetings_out,
        "agenda_items": items_out,
        "vote_events": votes_out,
        "members": members_out,
    }
    run_meta = {
        "source_id": SOURCE_ID,
        "extractor_version": EXTRACTOR_VERSION,
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "row_counts": {
            "meetings": len(meetings_out),
            "agenda_items": len(items_out),
            "vote_events": len(votes_out),
            "members": len(members_out),
        },
    }
    return records, run_meta
