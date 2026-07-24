"""Deterministic extractor for source `chicago-bos`.

Chicago City Council via the City Clerk eLMS public REST API (Dynamics 365
backend, Swagger 2.0, no auth).

Structure learned from the live fetch trace:
  * /meeting-agenda?filter=body eq 'City Council'&sort=date desc&top=100
    returns {facets, data[...], meta}; rows under `data`, future events
    first (calendar is published ~1yr ahead).
  * /meeting-agenda/{id} returns the full meeting record; agenda LINES are
    nested, each line carries a `title` (the legislative subject) and a
    nested matter object holding matterId / recordNumber / hasVotes.
  * roll-call votes are NOT embedded; they are fetched per matter at
    /meeting-agenda/{id}/matter/{matterId}/votes -> [{voterName, vote,
    personId}, ...].

Because the title lives on the agenda LINE while matterId/recordNumber live
on the nested matter object, title resolution walks the ancestor chain of
each matter node (nearest ancestor first, so we get the line's subject and
never a far-up section heading). Every vote_event is emitted together with
its agenda_item sharing the same id stem, so there are no orphan votes.

Votes come from structured JSON, so `evidence` is omitted (allowed for
structured sources).

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
_TITLE_KEYS = ("title", "subject", "mattertitle", "recordtitle",
               "legistartitle", "recordname", "caption", "description",
               "name")
_FN_KEYS = ("recordnumber", "filenumber", "matterrecordnumber",
            "matterfilenumber", "mattercode", "number")
_SKIP_STATUS_RE = re.compile(r"cancel|rescind|repeal|no quorum", re.I)

_VOTES_PER_MEETING = 60
_TOTAL_VOTE_FETCHES = 180


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
                  "meetings", "votes"):
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


def _valid_title(v, record_number):
    if not isinstance(v, str):
        return None
    t = re.sub(r"\s+", " ", v).strip()
    if len(t) < 3:
        return None
    if sum(1 for c in t if c.isalpha()) < 3:
        return None
    if record_number and t.strip().lower() == str(record_number).strip().lower():
        return None
    return t[:300]


# ---------- vote parsing --------------------------------------------------
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
    out = []
    for el in v:
        r = _vote_from_element(el)
        if r:
            out.append({"member": r[0], "position": r[1]})
    return out


def _counts(positions):
    c = {}
    for p in positions:
        c[p["position"]] = c.get(p["position"], 0) + 1
    return c


# ---------- matter collection (with ancestor chain) ----------------------
def _collect_matters(rec):
    found = {}
    order = []

    def visit(node, ancestors):
        if isinstance(node, dict):
            mid = _ci_get(node, "matterid")
            if mid is not None:
                key = str(mid)
                if key not in found:
                    found[key] = (node, list(ancestors))
                    order.append(key)
            ancestors.append(node)
            for v in node.values():
                visit(v, ancestors)
            ancestors.pop()
        elif isinstance(node, list):
            for x in node:
                visit(x, ancestors)

    visit(rec, [])
    return [(k, found[k][0], found[k][1]) for k in order]


def _matter_has_votes(node, ancestors):
    for scope in [node] + list(reversed(ancestors)):
        hv = _ci_get(scope, "hasvotes")
        if hv in (True, "true", "True", 1):
            return True
        if _ci_get(scope, "votetype"):
            return True
        break  # only the matter node's own flags
    return False


def _resolve_filenumber(node, ancestors):
    fn = _ci_get(node, *_FN_KEYS)
    if fn:
        return fn
    for anc in reversed(ancestors):
        fn = _ci_get(anc, *_FN_KEYS)
        if fn:
            return fn
    return None


def _resolve_title(node, ancestors, record_number):
    t = _valid_title(_ci_get(node, *_TITLE_KEYS), record_number)
    if t:
        return t
    for anc in reversed(ancestors):
        t = _valid_title(_ci_get(anc, *_TITLE_KEYS), record_number)
        if t:
            return t
    return None


def _resolve_action(node, ancestors):
    for scope in [node] + list(reversed(ancestors)):
        a = _ci_get(scope, "actionname", "action", "actiontext",
                    "disposition")
        if a:
            return re.sub(r"\s+", " ", str(a)).strip()[:120]
    return None


# ---------- enumeration ---------------------------------------------------
def _list_params(skip):
    return {"filter": "body eq '%s'" % BODY, "sort": "date desc",
            "top": 100, "skip": skip}


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

    try:
        data = rt.fetch_json(BASE + "/meeting-agenda",
                             params=_list_params(0))
    except Exception:
        return []
    rows = _rows(data)
    if not rows:
        return []
    _absorb(rows, seen, candidates, today)

    skip = len(rows)
    target = max(max_meetings * 6, 18)
    guard = 0
    while len(candidates) < target and guard < 12:
        guard += 1
        try:
            data = rt.fetch_json(BASE + "/meeting-agenda",
                                 params=_list_params(skip))
        except Exception:
            break
        rows = _rows(data)
        if not rows:
            break
        if _absorb(rows, seen, candidates, today) == 0:
            break
        skip += len(rows)

    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates


# ---------- per-meeting extraction ---------------------------------------
def _process_meeting(rt, d, mid, members, budget):
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

    matters = _collect_matters(rec)
    if not matters:
        return None

    iso = d.isoformat()
    meeting_id = "%s-%s" % (SOURCE_ID, iso)
    human_url = "%s/meeting/%s" % (PORTAL, mid)

    item_recs = []
    vote_recs = []
    used_slugs = {}
    attendance = {}
    fetched = 0

    for key, node, ancestors in matters:
        if fetched >= _VOTES_PER_MEETING or budget[0] <= 0:
            break
        if not _matter_has_votes(node, ancestors):
            continue
        matter_id = _ci_get(node, "matterid")
        if not matter_id:
            continue

        src_fn = _resolve_filenumber(node, ancestors)
        title = _resolve_title(node, ancestors, src_fn)
        if not title:
            continue  # no usable subject -> skip (avoids title/orphan gate)

        votes_url = "%s/meeting-agenda/%s/matter/%s/votes" % (
            BASE, mid, matter_id)
        budget[0] -= 1
        fetched += 1
        try:
            vdata = rt.fetch_json(votes_url)
        except Exception:
            continue
        positions = _positions_from_list(_rows(vdata))
        counts = _counts(positions)
        aye = counts.get("aye", 0)
        no = counts.get("no", 0)
        if (aye + no) < 1:
            continue  # attendance-only / empty roll call

        base = _slug(src_fn) or _slug(matter_id) or ("item%d" %
                                                     (len(used_slugs) + 1))
        n = used_slugs.get(base, 0)
        used_slugs[base] = n + 1
        slug = base if n == 0 else "%s-%d" % (base, n + 1)
        item_id = "%s-%s" % (meeting_id, slug)

        res = "pass" if aye > no else "fail"

        for p in positions:
            attendance.setdefault(p["member"], set()).add(p["position"])
            members[p["member"]] = True

        item_recs.append({
            "item_id": item_id,
            "meeting_id": meeting_id,
            "title": title,
            "action": _resolve_action(node, ancestors) or "Adopted",
            "file_number": None,  # Chicago letter-prefixed ids (O2026-...)
            "result": res,        # don't match schema NNNN-NNNN regex
            "source_file_number": src_fn,
            "source_url": human_url,
            "data_source_url": data_url,
        })
        vote_recs.append({
            "vote_id": "%s-v" % item_id,
            "item_id": item_id,
            "meeting_id": meeting_id,
            "positions": positions,
            "counts": counts,
            "result": res,
            "source_url": human_url,
            "data_source_url": votes_url,
        })

    if not vote_recs:
        return None

    attendance_out = {}
    for name, poss in attendance.items():
        attendance_out[name] = ("present"
                                if any(p != "absent" for p in poss)
                                else "absent")
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
    budget = [_TOTAL_VOTE_FETCHES]

    candidates = _enumerate(rt, max_meetings)

    record_cap = max_meetings * 10 + 15
    tried = 0
    for d, mid in candidates:
        if len(meetings_out) >= max_meetings:
            break
        if tried >= record_cap or budget[0] <= 0:
            break
        tried += 1
        try:
            result = _process_meeting(rt, d, mid, members, budget)
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
