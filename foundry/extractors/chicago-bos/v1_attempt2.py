"""Deterministic extractor for source `chicago-bos`.

Chicago City Council via the City Clerk eLMS public REST API (Dynamics 365
backend, Swagger 2.0, no auth). Meetings, agenda matters and per-member
roll-call votes are structured JSON, so vote_events are derived from
structured data and omit the `evidence` block (allowed for structured
sources by the artifact contract).

Meeting records are large and deeply nested with unknown key names, so
matters and their vote arrays are located by a case-insensitive recursive
walk rather than by fixed key paths.

Re-point at another eLMS tenant by changing BASE / PORTAL / BODY.
"""

import datetime
import re

EXTRACTOR_VERSION = "1"

BASE = "https://api.chicityclerkelms.chicago.gov"
PORTAL = "https://chicityclerkelms.chicago.gov"
BODY = "City Council"
SOURCE_ID = "chicago-bos"
SCHEMA_VERSION = "1.5"

VOTE_MAP = {
    "yea": "aye", "aye": "aye", "yes": "aye", "y": "aye",
    "nay": "no", "no": "no", "n": "no",
    "abstain": "abstain", "abstained": "abstain", "abstention": "abstain",
    "present": "present",
    "absent": "absent", "not voting": "absent", "excused": "absent",
    "recused": "recused", "recuse": "recused", "recusal": "recused",
}

_VOTER_KEYS = ("votername", "personname", "voter", "membername")
_VOTE_KEYS = ("vote", "votevalue", "votetext", "value", "voteresult")
_SKIP_STATUS_RE = re.compile(r"cancel|rescind|repeal|no quorum|adjourn",
                             re.I)


# ---------- generic helpers ----------------------------------------------
def _ci_get(d, *names):
    if not isinstance(d, dict):
        return None
    low = {k.lower(): k for k in d.keys()}
    for n in names:
        k = low.get(n)
        if k is not None and d[k] not in (None, "", [], {}):
            return d[k]
    return None


def _rows(data):
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for k in ("data", "value", "items", "results", "records",
                  "meetings"):
            v = data.get(k)
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


# ---------- vote / matter detection --------------------------------------
def _vote_from_element(el):
    if not isinstance(el, dict):
        return None
    low = {k.lower(): k for k in el.keys()}
    name = None
    for vk in _VOTER_KEYS:
        if vk in low and el[low[vk]]:
            name = el[low[vk]]
            break
    vote = None
    for vk in _VOTE_KEYS:
        if vk in low and el[low[vk]]:
            vote = el[low[vk]]
            break
    if name is None or vote is None:
        return None
    pos = VOTE_MAP.get(str(vote).strip().lower())
    if pos is None:
        return None
    nm = _clean_name(name)
    if not nm:
        return None
    return nm, pos


def _positions_from_list(v):
    positions = []
    for el in v:
        r = _vote_from_element(el)
        if r:
            positions.append({"member": r[0], "position": r[1]})
    return positions


def _is_vote_list(v):
    if not isinstance(v, list) or len(v) < 2:
        return False
    good = 0
    for el in v:
        if _vote_from_element(el):
            good += 1
    return good >= max(2, len(v) // 2)


def _is_matter(node):
    return isinstance(node, dict) and _ci_get(node, "matterid") is not None


def _walk(rec):
    """Return (matters_map, all_position_lists).

    matters_map: matter-key -> (matter_dict, best_position_list)
    all_position_lists: every position list found (for attendance).
    """
    matters = {}
    all_lists = []

    def visit(node, matter):
        if isinstance(node, dict):
            if _is_matter(node):
                matter = node
            for k, v in node.items():
                if isinstance(v, list) and _is_vote_list(v):
                    positions = _positions_from_list(v)
                    if positions:
                        all_lists.append(positions)
                        if matter is not None:
                            mid = _ci_get(matter, "matterid") or id(matter)
                            key = str(mid)
                            cur = matters.get(key)
                            if cur is None or len(positions) > len(cur[1]):
                                matters[key] = (matter, positions)
                else:
                    visit(v, matter)
        elif isinstance(node, list):
            for x in node:
                visit(x, matter)

    visit(rec, None)
    return matters, all_lists


def _counts(positions):
    c = {}
    for p in positions:
        c[p["position"]] = c.get(p["position"], 0) + 1
    return c


def _explicit_result(matter):
    parts = []
    for k in ("result", "outcome", "disposition", "passed", "actionname",
              "action", "actiontext", "status"):
        v = _ci_get(matter, k)
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


def _matter_title(matter):
    t = _ci_get(matter, "title", "subject", "mattertitle", "mattername",
                "recordname", "name", "description", "caption")
    if t:
        return re.sub(r"\s+", " ", str(t)).strip()
    return None


def _matter_action(matter):
    a = _ci_get(matter, "actionname", "action", "actiontext", "disposition",
                "result", "status")
    if a:
        return re.sub(r"\s+", " ", str(a)).strip()
    return None


# ---------- enumeration ---------------------------------------------------
def _param_variants(skip):
    return [
        {"filter": "body eq '%s'" % BODY, "sort": "date desc",
         "top": 100, "skip": skip},
        {"$filter": "body eq '%s'" % BODY, "$orderby": "date desc",
         "$top": 100, "$skip": skip},
        {"top": 100, "skip": skip},
        None,
    ]


def _absorb(rows, seen, candidates, today):
    added = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        mid = _ci_get(row, "meetingid", "id", "meetingagendaid",
                      "meetingguid")
        if not mid or mid in seen:
            continue
        seen.add(mid)
        added += 1
        body = _ci_get(row, "body", "bodyname", "committee")
        if body and "city council" not in str(body).lower():
            continue
        status = _ci_get(row, "status")
        if status and _SKIP_STATUS_RE.search(str(status)):
            continue
        d = _parse_date(_ci_get(row, "date", "meetingdate", "startdatetime",
                                "meetingdatetime"))
        if d is None or d >= today:
            continue
        candidates.append((d, mid))
    return added


def _enumerate(rt, max_meetings):
    today = datetime.date.today()
    candidates = []
    seen = set()
    variant_index = None
    first_data = None

    for vi in range(len(_param_variants(0))):
        try:
            data = rt.fetch_json(BASE + "/meeting-agenda",
                                 params=_param_variants(0)[vi])
        except Exception:
            continue
        if _rows(data):
            variant_index = vi
            first_data = data
            _absorb(_rows(data), seen, candidates, today)
            break
    if variant_index is None:
        return []

    skip = len(_rows(first_data))
    target = max(max_meetings * 6, 18)
    guard = 0
    while len(candidates) < target and guard < 12:
        guard += 1
        try:
            data = rt.fetch_json(
                BASE + "/meeting-agenda",
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

    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates


# ---------- per-meeting extraction ---------------------------------------
def _process_meeting(rt, d, mid, members):
    data_url = "%s/meeting-agenda/%s" % (BASE, mid)
    try:
        rec = rt.fetch_json(data_url)
    except Exception:
        return None
    if not isinstance(rec, dict):
        return None

    rd = _parse_date(_ci_get(rec, "date", "meetingdate", "startdatetime",
                             "meetingdatetime"))
    if rd is not None:
        d = rd
    if d is None or d >= datetime.date.today():
        return None

    matters, all_lists = _walk(rec)
    if not matters:
        return None

    iso = d.isoformat()
    meeting_id = "%s-%s" % (SOURCE_ID, iso)
    human_url = "%s/meeting/%s" % (PORTAL, mid)

    item_recs = []
    vote_recs = []
    used_slugs = {}

    # attendance across every position list (incl. attendance-only rolls)
    attendance = {}
    for lst in all_lists:
        for p in lst:
            attendance.setdefault(p["member"], set()).add(p["position"])

    for key, (matter, positions) in matters.items():
        counts = _counts(positions)
        aye = counts.get("aye", 0)
        no = counts.get("no", 0)
        genuine = (aye + no) >= 1  # exclude pure present/absent roll calls
        if not genuine:
            continue

        src_fn = _ci_get(matter, "recordnumber", "filenumber",
                         "matterrecordnumber", "matterfilenumber", "number")
        matter_id = _ci_get(matter, "matterid")
        base = _slug(src_fn) or _slug(matter_id) or ("item%d" %
                                                     (len(used_slugs) + 1))
        n = used_slugs.get(base, 0)
        used_slugs[base] = n + 1
        slug = base if n == 0 else "%s-%d" % (base, n + 1)
        item_id = "%s-%s" % (meeting_id, slug)

        res = _explicit_result(matter)
        if res is None:
            res = "pass" if aye > no else "fail"

        for p in positions:
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

        title = _matter_title(matter)
        if title:
            item_recs.append({
                "item_id": item_id,
                "meeting_id": meeting_id,
                "title": title[:300],
                "action": _matter_action(matter) or "Considered",
                "file_number": None,  # Chicago letter-prefixed ids (O2026-..)
                "result": res,        # don't fit the schema NNNN-NNNN regex
                "source_file_number": src_fn,
                "source_url": human_url,
                "data_source_url": data_url,
            })

    if not vote_recs:
        return None

    attendance_out = {}
    for name, poss in attendance.items():
        attendance_out[name] = ("present"
                                if any(p != "absent" for p in poss)
                                else "absent")
    # ensure every voter is represented in attendance
    for vr in vote_recs:
        for p in vr["positions"]:
            attendance_out.setdefault(
                p["member"],
                "absent" if p["position"] == "absent" else "present")
            members[p["member"]] = True
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

    fetch_cap = max_meetings * 8 + 12
    tried = 0
    for d, mid in candidates:
        if len(meetings_out) >= max_meetings:
            break
        if tried >= fetch_cap:
            break
        tried += 1
        try:
            result = _process_meeting(rt, d, mid, members)
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
