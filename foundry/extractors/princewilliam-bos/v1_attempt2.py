"""Deterministic extractor for source `princewilliam-bos`.

Prince William County, Virginia — Board of County Supervisors.

Strategy (per verified source profile):
  Primary source is the Granicus VoteLog JSON API:
    GET https://pwcgov.granicus.com/core/Handlers/media/votelog.ashx
        ?action=search&viewId=23&meetingBodyId=1&pageindex=N
  which returns an array (VoteLogMeetings) of completed meetings, each with
  clip_id, date, AgendaLink, MinutesLink and an inline Votes array carrying
  full per-item, per-attendee vote detail.  That single endpoint gives us
  everything the schema needs (attendance, agenda items, per-member votes)
  without downloading full agenda packets.

The module is deterministic and uses only the injected runtime (rt) for I/O.
"""

import re
import datetime

EXTRACTOR_VERSION = "1"

VOTELOG_URL = "https://pwcgov.granicus.com/core/Handlers/media/votelog.ashx"
VIEW_ID = "23"
BODY_ID = "1"
DEFAULT_BODY = "Board of County Supervisors"

RUN_ID = "princewilliam-bos-" + EXTRACTOR_VERSION

# Granicus vote tokens -> schema position vocabulary.
VOTE_MAP = {
    "yes": "aye", "aye": "aye", "y": "aye",
    "no": "no", "nay": "no", "n": "no",
    "abstain": "abstain", "abstained": "abstain", "abstention": "abstain",
    "absent": "absent",
    "recuse": "recused", "recused": "recused", "recusal": "recused",
    "present": "present",
}

# Titles stripped from names so the same person collapses to one member.
_TITLES = sorted(
    ["vice chair", "vice-chair", "chair-at-large", "chair at-large",
     "at-large chair", "chairman", "chairwoman", "chairperson", "chair",
     "supervisor", "mr.", "ms.", "mrs.", "dr.", "mayor", "hon.",
     "the honorable"],
    key=len, reverse=True,
)

_MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10, "nov": 11, "november": 11, "dec": 12,
    "december": 12,
}

_DEFAULT_MAX = 10


def _coerce_max(max_meetings):
    """Be tolerant about what the harness passes in for max_meetings."""
    val = max_meetings
    # argparse.Namespace or similar object
    if hasattr(val, "max_meetings"):
        val = getattr(val, "max_meetings")
    # unwrap single-element containers
    if isinstance(val, (list, tuple)):
        val = val[0] if val else None
    if isinstance(val, bool):
        return _DEFAULT_MAX
    if isinstance(val, int):
        return val if val > 0 else _DEFAULT_MAX
    if isinstance(val, float):
        return int(val) if val > 0 else _DEFAULT_MAX
    if isinstance(val, str):
        m = re.search(r"\d+", val)
        if m:
            n = int(m.group(0))
            return n if n > 0 else _DEFAULT_MAX
    return _DEFAULT_MAX


def _cikey(d, *keys):
    """Case-insensitive dict lookup; returns first matching key's value."""
    if not isinstance(d, dict):
        return None
    lowered = {k.lower(): k for k in d.keys()}
    for key in keys:
        real = lowered.get(key.lower())
        if real is not None:
            return d[real]
    return None


def _clean_name(name):
    s = str(name or "").strip()
    changed = True
    while changed:
        changed = False
        low = s.lower()
        for t in _TITLES:
            if low.startswith(t + " "):
                s = s[len(t):].strip()
                changed = True
                break
    return s or str(name or "").strip()


def _parse_date(raw):
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    # Unix timestamp (seconds or ms).
    m = re.fullmatch(r"(\d{10,13})", s)
    if m:
        ts = int(m.group(1))
        if len(m.group(1)) == 13:
            ts //= 1000
        try:
            return datetime.datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d")
        except (OverflowError, OSError, ValueError):
            return None
    # ISO prefix.
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return "%s-%s-%s" % (m.group(1), m.group(2), m.group(3))
    # US numeric.
    m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", s)
    if m:
        mo, da, yr = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if 1 <= mo <= 12 and 1 <= da <= 31:
            return "%04d-%02d-%02d" % (yr, mo, da)
    # Month-name form (e.g. "Jul 7, 2026", "July 7 2026 2:00 PM").
    m = re.search(r"([A-Za-z]+)\.?\s+(\d{1,2}),?\s+(\d{4})", s)
    if m:
        mo = _MONTHS.get(m.group(1).lower())
        if mo:
            return "%04d-%02d-%02d" % (int(m.group(3)), mo, int(m.group(2)))
    return None


def _map_result(raw):
    if raw is None:
        return None
    s = str(raw).strip().lower()
    if s in ("pass", "passed", "carried", "carry", "adopted", "approved",
             "approve", "aye"):
        return "pass"
    if s in ("fail", "failed", "denied", "deny", "defeated", "lost",
             "no", "died"):
        return "fail"
    return None


def _item_number(text, idx):
    if text:
        m = re.match(r"\s*([0-9]+[A-Za-z]?(?:[.\-][0-9A-Za-z]+)*)\s*\)", text)
        if m:
            return m.group(1)
        m = re.match(r"\s*([0-9A-Za-z][0-9A-Za-z.\-]{0,12})\s*[)\.:]", text)
        if m:
            return m.group(1)
    return "i%d" % idx


def _slug(s):
    return re.sub(r"[^0-9A-Za-z]+", "-", str(s)).strip("-") or "x"


def _action_from_motion(motion, mapped_result):
    if motion:
        m = re.search(r"motion to ([A-Za-z][A-Za-z \-/]*)", motion, re.I)
        if m:
            verb = m.group(1).strip()
            verb = re.split(r"\bmoved\b", verb, flags=re.I)[0].strip()
            if verb:
                return verb[:120]
        return motion.strip()[:200]
    if mapped_result == "pass":
        return "Approved"
    if mapped_result == "fail":
        return "Failed"
    return "Action recorded"


def _meetings_from_response(data):
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        val = _cikey(data, "VoteLogMeetings", "Meetings", "meetings",
                     "results", "data")
        if isinstance(val, list):
            return val
        for v in data.values():
            if isinstance(v, list):
                return v
    return []


def _provenance(source_url):
    return {
        "source_id": "princewilliam-bos",
        "extractor_version": EXTRACTOR_VERSION,
        "run_id": RUN_ID,
        "source_url": source_url,
        "certification": {
            "certified": True,
            "method": "granicus-votelog-json",
        },
    }


def _enumerate_meetings(rt, want):
    """Page through the VoteLog search API, newest first."""
    collected = []
    seen = set()
    page = 1
    max_pages = 30
    while len(collected) < want and page <= max_pages:
        params = {
            "action": "search",
            "viewId": VIEW_ID,
            "meetingBodyId": BODY_ID,
            "pageindex": str(page),
        }
        try:
            data = rt.fetch_json(VOTELOG_URL, params=params)
        except Exception:
            break
        meetings = _meetings_from_response(data)
        if not meetings:
            break
        new = 0
        for m in meetings:
            if not isinstance(m, dict):
                continue
            clip = _cikey(m, "clip_id", "ClipId", "clipId", "ClipID")
            date_raw = _cikey(m, "date", "MeetingDate", "meeting_date", "Date")
            key = str(clip) if clip is not None else "d:" + str(date_raw)
            if key in seen:
                continue
            seen.add(key)
            new += 1
            votes = _cikey(m, "Votes", "votes")
            if isinstance(votes, list) and votes:
                collected.append(m)
        if new == 0:
            break
        page += 1
    return collected


def extract(rt, max_meetings):
    want = _coerce_max(max_meetings)

    meetings_rec = []
    items_rec = []
    votes_rec = []
    members = {}  # normalized name -> record

    raw_meetings = _enumerate_meetings(rt, want)

    # Parse dates; keep only meetings with a usable date; newest first.
    dated = []
    for m in raw_meetings:
        d = _parse_date(_cikey(m, "date", "MeetingDate", "meeting_date", "Date"))
        if not d:
            continue
        dated.append((d, m))
    dated.sort(key=lambda t: t[0], reverse=True)
    dated = dated[:want]

    used_meeting_ids = set()

    for date, m in dated:
        try:
            clip = _cikey(m, "clip_id", "ClipId", "clipId", "ClipID")
            votes = _cikey(m, "Votes", "votes")
            if not isinstance(votes, list) or not votes:
                continue

            meeting_id = "princewilliam-bos-%s" % date
            if meeting_id in used_meeting_ids:
                meeting_id = "%s-%s" % (meeting_id, _slug(clip))
            used_meeting_ids.add(meeting_id)

            body = _cikey(m, "Name", "BodyName", "body") or DEFAULT_BODY
            if not isinstance(body, str) or not body.strip():
                body = DEFAULT_BODY

            ml = _cikey(m, "MinutesLink", "minutesLink")
            if isinstance(ml, str) and ml.startswith("http"):
                source_url = ml
            elif clip is not None:
                source_url = ("https://pwcgov.granicus.com/MinutesViewer.php"
                              "?view_id=%s&clip_id=%s" % (VIEW_ID, clip))
            else:
                source_url = ("%s?action=search&viewId=%s&meetingBodyId=%s"
                              % (VOTELOG_URL, VIEW_ID, BODY_ID))

            prov = _provenance(source_url)

            # ---- First pass over votes: build attendance roster. ----
            attendance = {}  # normalized name -> present bool
            parsed_votes = []
            for idx, v in enumerate(votes, start=1):
                if not isinstance(v, dict):
                    continue
                agenda_text = _cikey(v, "AgendaItemText", "agenda_item_text",
                                     "Title") or ""
                motion = _cikey(v, "MotionText", "motion_text", "Motion") or ""
                result_raw = _cikey(v, "VoteResult", "vote_result", "Result")
                details = _cikey(v, "VoteDetails", "vote_details", "Details")
                positions = []
                counts = {}
                if isinstance(details, list):
                    for d in details:
                        if not isinstance(d, dict):
                            continue
                        att = _cikey(d, "Attendee", "attendee")
                        name = None
                        if isinstance(att, dict):
                            name = _cikey(att, "Name", "name")
                        if not name:
                            name = _cikey(d, "Name", "name", "Attendee")
                        vote_tok = _cikey(d, "Vote", "vote", "Value")
                        pos = VOTE_MAP.get(str(vote_tok or "").strip().lower())
                        if not name or not pos:
                            continue
                        nm = _clean_name(name)
                        if not nm:
                            continue
                        positions.append({"member": nm, "position": pos})
                        counts[pos] = counts.get(pos, 0) + 1
                        present = pos != "absent"
                        if nm not in attendance:
                            attendance[nm] = present
                        elif present:
                            attendance[nm] = True
                        if nm not in members:
                            members[nm] = {"name": nm, "provenance": prov}
                parsed_votes.append((idx, agenda_text, motion, result_raw,
                                     positions, counts))

            if not attendance:
                continue

            att_obj = {nm: ("present" if p else "absent")
                       for nm, p in attendance.items()}

            meetings_rec.append({
                "meeting_id": meeting_id,
                "body": body,
                "date": date,
                "attendance": att_obj,
                "source_url": source_url,
                "file_number": None,
                "provenance": prov,
            })

            # ---- Second pass: agenda items + vote events. ----
            used_item_suffix = {}
            for idx, agenda_text, motion, result_raw, positions, counts in parsed_votes:
                num = _item_number(agenda_text, idx)
                base = _slug(num)
                cnt = used_item_suffix.get(base, 0)
                used_item_suffix[base] = cnt + 1
                suffix = base if cnt == 0 else "%s-%d" % (base, cnt + 1)

                item_id = "%s-item-%s" % (meeting_id, suffix)
                title = agenda_text.strip() or (motion.strip()[:200]
                                                if motion else "Item %d" % idx)
                mapped = _map_result(result_raw)
                action = _action_from_motion(motion, mapped)

                items_rec.append({
                    "item_id": item_id,
                    "meeting_id": meeting_id,
                    "title": title,
                    "action": action,
                    "result": mapped,
                    "file_number": None,
                    "provenance": prov,
                })

                if mapped in ("pass", "fail") and positions and counts:
                    votes_rec.append({
                        "vote_id": "%s-vote-%s" % (meeting_id, suffix),
                        "meeting_id": meeting_id,
                        "item_id": item_id,
                        "positions": positions,
                        "counts": counts,
                        "result": mapped,
                        "file_number": None,
                        "provenance": prov,
                    })
        except Exception:
            continue

    members_rec = [members[k] for k in sorted(members.keys())]

    records = {
        "meetings": meetings_rec,
        "agenda_items": items_rec,
        "vote_events": votes_rec,
        "members": members_rec,
    }

    run_meta = {
        "source_id": "princewilliam-bos",
        "extractor_version": EXTRACTOR_VERSION,
        "schema_version": "1.2",
        "row_counts": {
            "meetings": len(meetings_rec),
            "agenda_items": len(items_rec),
            "vote_events": len(votes_rec),
            "members": len(members_rec),
        },
    }
    return records, run_meta
