"""Deterministic extractor for source `seattle-bos`.

Seattle City Council (Granicus Legistar tenant, "seattle").

Meetings and per-member votes come from the public Legistar REST API
(webapi.legistar.com/v1/seattle). eventitems/{id}/votes gives structured
per-member positions BUT lists only members who actually voted; absent
members are omitted. We therefore reconstruct each meeting's roster (union
of everyone who voted at the meeting, plus anyone the Minutes roll-call
lists as Absent/Excused) and mark non-voters on each motion as `absent`.
This captures real dissent/abstention (from the API) AND absences, so votes
are not uniformly full-roster ayes.

The official Minutes PDF is fetched once per meeting to verify the printed
date, guarantee byte-distinct documents, and read the roll call. Full agenda
packets are never downloaded. Only the injected runtime `rt` performs I/O.
"""

import re
import hashlib
import datetime
import urllib.parse

EXTRACTOR_VERSION = "1"

SOURCE_ID = "seattle-bos"
RUN_ID = SOURCE_ID + "-" + EXTRACTOR_VERSION

# --- tenant constants (re-point here for another Legistar tenant) ---------
BASE = "https://webapi.legistar.com/v1/seattle/"
PORTAL = "https://seattle.legistar.com/"
CALENDAR_URL = PORTAL + "Calendar.aspx"
BODY_ID = 138                       # 'City Council'
DEFAULT_BODY = "City Council"

_DEFAULT_MAX = 10
_PAGE = 100
_MAX_PAGES = 12
_MAX_EXAMINE = 60
_VOTES_PER_MEETING = 40
_FETCH_CAP = 600

_NAME_OK = re.compile(r"^[A-Za-z\u00C0-\u017F][A-Za-z\u00C0-\u017F .'\-]{0,38}$")

_MONTH = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5,
    "june": 6, "july": 7, "august": 8, "september": 9, "october": 10,
    "november": 11, "december": 12,
}
_PRINTED_DATE_RE = re.compile(
    r'(January|February|March|April|May|June|July|August|September|'
    r'October|November|December)\s+(\d{1,2}),\s*(\d{4})')

_ROLL_LABEL_RE = re.compile(
    r'(Present|Absent|Excused|Late Arrival)\s*:\s*(\d+)\s*-', re.I)
_NAME_TOKEN_RE = re.compile(r"[A-Z][A-Za-z\u00C0-\u017F'\u2019\-]+")

_STRIP_WORDS = frozenset((
    "a", "an", "the", "to", "of", "in", "on", "for", "and", "or", "by",
    "with", "at", "as", "be", "this", "that", "no", "not", "relating",
    "relation", "resolution", "ordinance", "introduction", "appointment",
    "reappointment", "member", "application", "amend", "amending",
    "amended", "approve", "approved", "adopt", "adopted", "pass", "passed",
    "fail", "failed", "confirm", "confirmed", "council", "committee",
    "roll", "call", "motion", "made", "filed", "referred", "type", "action",
    "result", "details", "version", "agenda", "certain", "prior", "acts",
    "term", "board",
))

# Legistar VoteValueName -> schema position vocabulary.
_VOTE_MAP = {
    "in favor": "aye", "yea": "aye", "aye": "aye", "ayes": "aye",
    "yes": "aye", "for": "aye", "approve": "aye",
    "opposed": "no", "nay": "no", "no": "no", "nays": "no",
    "against": "no", "reject": "no",
    "abstain": "abstain", "abstained": "abstain", "abstention": "abstain",
    "absent": "absent", "excused": "absent", "excused absence": "absent",
    "not present": "absent",
    "present": "present",
    "recused": "recused", "recusal": "recused", "conflict": "recused",
}


# --------------------------------------------------------------------------
class _Budget(object):
    def __init__(self, rt, cap):
        self.rt = rt
        self.cap = cap
        self.n = 0

    def json(self, url):
        if self.n >= self.cap:
            return None
        self.n += 1
        try:
            return self.rt.fetch_json(url)
        except Exception:
            return None

    def text(self, url):
        if self.n >= self.cap:
            return None
        self.n += 1
        try:
            return self.rt.fetch_text(url)
        except Exception:
            return None


def _coerce_max(args):
    val = args
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


def _g(d, key):
    if not isinstance(d, dict):
        return None
    v = d.get(key)
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _as_list(data):
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        v = data.get("value")
        if isinstance(v, list):
            return v
    return None


def _clean_name(tok):
    t = str(tok or "").strip()
    t = re.split(r'[\r\n]', t, 1)[0]
    t = re.sub(r'\([^)]*\)', ' ', t)
    t = re.sub(r'^(?i:councilmember|council member|council president|'
               r'president|the honorable|hon\.?|mr\.?|ms\.?|mrs\.?|dr\.?)\s+',
               '', t.strip())
    t = re.sub(r'\s+', ' ', t).strip(" .,;:\u2013\u2014-")
    return t


def _valid_name(nm):
    if not nm or not _NAME_OK.match(nm):
        return False
    return len(re.findall(r'[A-Za-z\u00C0-\u017F]', nm)) >= 2


def _has_subject(t):
    if not t:
        return False
    words = re.findall(r"[A-Za-z\u00C0-\u017F]{2,}", t.lower())
    remaining = [w for w in words if w not in _STRIP_WORDS]
    return len(remaining) >= 2


def _parse_date(raw):
    if raw is None:
        return None
    s = str(raw).strip()
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return "%s-%s-%s" % (m.group(1), m.group(2), m.group(3))
    m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", s)
    if m:
        mo, da, yr = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if 1 <= mo <= 12 and 1 <= da <= 31:
            return "%04d-%02d-%02d" % (yr, mo, da)
    return None


def _printed_date(text):
    m = _PRINTED_DATE_RE.search(text[:1400])
    if not m:
        return None
    mo = _MONTH.get(m.group(1).lower())
    if not mo:
        return None
    da, yr = int(m.group(2)), int(m.group(3))
    if 1 <= da <= 31 and 1900 < yr < 2100:
        return "%04d-%02d-%02d" % (yr, mo, da)
    return None


def _parse_rollcall(text):
    """Return (present_surnames set, absent_surnames set) from the roll call."""
    present, absent = set(), set()
    m = re.search(r'ROLL\s+CALL', text, re.I)
    if not m:
        return present, absent
    seg = text[m.end(): m.end() + 1200]
    labels = list(_ROLL_LABEL_RE.finditer(seg))
    for i, lm in enumerate(labels):
        n = int(lm.group(2))
        start = lm.end()
        end = labels[i + 1].start() if i + 1 < len(labels) else len(seg)
        chunk = seg[start:end]
        toks = [t for t in _NAME_TOKEN_RE.findall(chunk)
                if not t.isupper() and len(t) >= 2]
        names = toks[:n]
        label = lm.group(1).lower()
        target = absent if label in ("absent", "excused") else present
        for nm in names:
            target.add(nm)
    return present, absent


def _slug(s):
    return re.sub(r"[^0-9A-Za-z]+", "-", str(s)).strip("-") or "x"


def _provenance(source_url):
    return {
        "source_id": SOURCE_ID,
        "extractor_version": EXTRACTOR_VERSION,
        "run_id": RUN_ID,
        "source_url": source_url,
        "certification": {
            "certified": True,
            "method": "legistar-rest-api+minutes-rollcall",
        },
    }


def _events_url(filt, skip):
    return BASE + "events?" + urllib.parse.urlencode({
        "$filter": filt,
        "$orderby": "EventDate desc",
        "$top": str(_PAGE),
        "$skip": str(skip),
    }, quote_via=urllib.parse.quote)


def _fetch_events(b, today_iso):
    out, seen = [], set()
    filt = ("EventBodyId eq %d and EventDate lt datetime'%sT00:00:00'"
            % (BODY_ID, today_iso))
    ok = False
    for page in range(_MAX_PAGES):
        rows = _as_list(b.json(_events_url(filt, page * _PAGE)))
        if rows is None:
            break
        ok = True
        if not rows:
            break
        for e in rows:
            eid = e.get("EventId")
            if eid in seen:
                continue
            seen.add(eid)
            out.append(e)
        if len(rows) < _PAGE:
            break
    if not ok or not out:
        filt2 = "EventBodyId eq %d" % BODY_ID
        for page in range(_MAX_PAGES):
            rows = _as_list(b.json(_events_url(filt2, page * _PAGE)))
            if not rows:
                break
            for e in rows:
                eid = e.get("EventId")
                if eid in seen:
                    continue
                seen.add(eid)
                out.append(e)
            if len(rows) < _PAGE:
                break
    return out


def _raw_positions(b, item_id):
    """Structured per-member positions for members who actually voted."""
    rows = _as_list(b.json(BASE + "eventitems/%s/votes" % item_id))
    if not rows:
        return None
    positions = []
    for v in rows:
        nm = _clean_name(_g(v, "VotePersonName"))
        val = (_g(v, "VoteValueName") or "").strip().lower()
        pos = _VOTE_MAP.get(val)
        if pos is None or not _valid_name(nm):
            continue
        positions.append({"member": nm, "position": pos})
    return positions or None


def extract(rt, args):
    want = _coerce_max(args)
    b = _Budget(rt, _FETCH_CAP)
    today_iso = datetime.date.today().isoformat()

    events = _fetch_events(b, today_iso)

    cand = []
    for e in events:
        date = _parse_date(_g(e, "EventDate"))
        if not date or date >= today_iso:
            continue
        murl = _g(e, "EventMinutesFile")
        if not murl:
            continue
        cand.append((date, e.get("EventId") or 0, e, murl))
    cand.sort(key=lambda t: (t[0], t[1] if isinstance(t[1], int) else 0),
              reverse=True)

    # ---- Phase 1: gather meeting data & a global surname->fullname map ----
    gathered = []
    surname_map = {}
    seen_docs = set()
    examined = 0

    for date, eid, ev, murl in cand:
        if len(gathered) >= want or examined >= _MAX_EXAMINE:
            break
        if b.n >= _FETCH_CAP:
            break
        examined += 1
        try:
            mtext = b.text(murl)
            if not mtext or len(mtext) < 120:
                continue
            printed = _printed_date(mtext)
            if printed and printed != date:
                continue
            sig = hashlib.md5(mtext.encode("utf-8", "ignore")).hexdigest()
            if sig in seen_docs:
                continue

            items = _as_list(b.json(BASE + "events/%s/eventitems" % eid))
            if not items:
                continue

            parsed_items = []
            vote_fetches = 0
            for it in items:
                title = (_g(it, "EventItemTitle")
                         or _g(it, "EventItemMatterName"))
                matter_file = _g(it, "EventItemMatterFile")
                if not matter_file or not title or not _has_subject(title):
                    continue
                iid = it.get("EventItemId")
                if iid is None:
                    continue
                passed = it.get("EventItemPassedFlag")
                item_result = ("pass" if passed == 1
                               else ("fail" if passed == 0 else None))
                action = (_g(it, "EventItemActionName")
                          or ("Passed" if item_result == "pass" else None)
                          or "Considered")

                raw = None
                rollcall = it.get("EventItemRollCallFlag")
                if ((rollcall == 1 or passed is not None)
                        and vote_fetches < _VOTES_PER_MEETING
                        and b.n < _FETCH_CAP):
                    vote_fetches += 1
                    raw = _raw_positions(b, iid)

                parsed_items.append({
                    "iid": iid,
                    "title": title[:240],
                    "action": action,
                    "item_result": item_result,
                    "passed": passed,
                    "raw": raw,
                })
                for p in (raw or []):
                    fn = p["member"]
                    surname_map.setdefault(fn.split()[-1].lower(), fn)

            if not any(pi["raw"] for pi in parsed_items):
                continue

            source_url = _g(ev, "EventInSiteURL") or CALENDAR_URL
            low = source_url.lower()
            if ".ashx" in low or "webapi." in low or "/api/" in low:
                source_url = CALENDAR_URL

            _, absent_surnames = _parse_rollcall(mtext)

            seen_docs.add(sig)
            gathered.append({
                "date": date,
                "eid": eid,
                "body": _g(ev, "EventBodyName") or DEFAULT_BODY,
                "source_url": source_url,
                "murl": murl,
                "items": parsed_items,
                "absent_surnames": absent_surnames,
            })
        except Exception:
            continue

    # ---- Phase 2: build records (roster reconstruction + absences) --------
    meetings_rec, items_rec, votes_rec = [], [], []
    members = {}
    used_ids = set()

    def _full(surname):
        fn = surname_map.get(surname.lower())
        return fn if fn else surname

    for g in gathered:
        prov = _provenance(g["source_url"])
        date = g["date"]

        meeting_id = "%s-%s" % (SOURCE_ID, date)
        if meeting_id in used_ids:
            base_id = "%s-%s" % (meeting_id, _slug(g["body"])[:30])
            meeting_id = base_id
            k = 2
            while meeting_id in used_ids:
                meeting_id = "%s-%d" % (base_id, k)
                k += 1

        # meeting roster = everyone who voted + anyone marked absent
        voters = set()
        for pi in g["items"]:
            for p in (pi["raw"] or []):
                voters.add(p["member"])
        absent_full = {_full(sn) for sn in g["absent_surnames"]}
        roster = set(voters) | absent_full

        local_items, local_votes = [], []
        for pi in g["items"]:
            item_id = "%s-item-%s" % (meeting_id, pi["iid"])
            local_items.append({
                "item_id": item_id,
                "meeting_id": meeting_id,
                "title": pi["title"],
                "action": pi["action"],
                "result": pi["item_result"],
                "file_number": None,
                "provenance": prov,
            })
            raw = pi["raw"]
            if not raw:
                continue

            voted = {p["member"] for p in raw}
            positions = [dict(p) for p in raw]
            for nm in sorted(roster - voted):
                if _valid_name(nm):
                    positions.append({"member": nm, "position": "absent"})

            counts = {}
            for p in positions:
                counts[p["position"]] = counts.get(p["position"], 0) + 1
            aye = counts.get("aye", 0)
            no = counts.get("no", 0)
            if aye == 0 and no == 0:
                continue
            if pi["passed"] == 1:
                vresult = "pass"
            elif pi["passed"] == 0:
                vresult = "fail"
            else:
                vresult = "pass" if aye > no else "fail"

            local_votes.append({
                "vote_id": "%s-vote-%s" % (meeting_id, pi["iid"]),
                "meeting_id": meeting_id,
                "item_id": item_id,
                "positions": positions,
                "counts": counts,
                "result": vresult,
                "file_number": None,
                "provenance": prov,
            })

        if not local_votes:
            continue

        attendance = {}
        for nm in roster:
            if not _valid_name(nm):
                continue
            attendance[nm] = "absent" if (nm in absent_full and
                                          nm not in voters) else "present"
        if not attendance:
            continue

        used_ids.add(meeting_id)
        meetings_rec.append({
            "meeting_id": meeting_id,
            "body": g["body"],
            "date": date,
            "attendance": attendance,
            "source_url": g["source_url"],
            "data_source_url": g["murl"],
            "file_number": None,
            "provenance": prov,
        })
        for nm in attendance:
            members.setdefault(nm, {"name": nm, "provenance": prov})
        for vt in local_votes:
            for p in vt["positions"]:
                members.setdefault(p["member"],
                                   {"name": p["member"], "provenance": prov})
        items_rec.extend(local_items)
        votes_rec.extend(local_votes)

    members_rec = [members[k] for k in sorted(members.keys())]

    records = {
        "meetings": meetings_rec,
        "agenda_items": items_rec,
        "vote_events": votes_rec,
        "members": members_rec,
    }
    run_meta = {
        "source_id": SOURCE_ID,
        "extractor_version": EXTRACTOR_VERSION,
        "schema_version": "1.5",
        "row_counts": {
            "meetings": len(meetings_rec),
            "agenda_items": len(items_rec),
            "vote_events": len(votes_rec),
            "members": len(members_rec),
        },
    }
    return records, run_meta
