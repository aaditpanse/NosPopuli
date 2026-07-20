"""Deterministic extractor for source `stafford-bos`
(Stafford County, Virginia — Board of Supervisors).

Platform: CivicClerk (CivicPlus Meetings Select). The public portal
(staffordcova.portal.civicclerk.com) is a JavaScript SPA, but it is driven by
a CivicClerk OData backend on the sibling host `staffordcova.api.civicclerk.com`
which serves machine-readable Event records (with published-file listings) and
file streams (PDFs delivered as text). All I/O goes through the injected rt.

Votes for Stafford BOS live inside narrative minutes PDFs, so vote_events are
derived from prose (roster minus named dissent/abstention/absence) and every
one carries a verbatim `evidence` quote copied from the fetched document text.
"""

import re
from datetime import date

# ---------------------------------------------------------------------------
# Tenant / platform constants — re-point the artifact by editing these only.
# ---------------------------------------------------------------------------
SOURCE_ID = "stafford-bos"
EXTRACTOR_VERSION = "1"
SCHEMA_VERSION = "1.3"

TENANT = "staffordcova"
PORTAL_BASE = f"https://{TENANT}.portal.civicclerk.com"
API_BASE = f"https://{TENANT}.api.civicclerk.com"

BODY_NAME = "Board of Supervisors"

EVENTS_URL = f"{API_BASE}/v1/Events"
FILE_STREAM_TMPL = (
    API_BASE + "/v1/Meetings/GetMeetingFileStream(fileId={fid},plainText={pt})"
)


# ---------------------------------------------------------------------------
# Small parsing helpers
# ---------------------------------------------------------------------------
_NUMWORDS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
    "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
}

_TITLE_TOKENS = {
    "chair", "chairman", "chairwoman", "chairperson", "vice", "vicechair",
    "supervisor", "supervisors", "member", "members", "mr", "mrs", "ms",
    "dr", "hon", "honorable", "the", "and", "county", "administrator",
    "clerk", "attorney", "deputy", "present", "absent", "staff", "also",
    "none", "board", "of", "were", "was", "district",
}

# Anchors that indicate a recorded numeric tally.
_TALLY_RES = [
    re.compile(r"vote of\s+([0-9]{1,2})\s*(?:to|[-\u2013\u2014])\s*([0-9]{1,2})", re.I),
    re.compile(r"vote of\s+([a-z]+)\s+to\s+([a-z]+)", re.I),
    re.compile(r"\bayes?\b\D{0,6}(\d{1,2})\D{0,40}?\bnays?\b\D{0,6}(\d{1,2})", re.I),
]

# Anchors for unanimous outcomes with no numeric tally.
_UNANIMOUS_RE = re.compile(
    r"(?:carried|adopted|approved|passed)\s+(?:by\s+)?(?:a\s+)?unanimous"
    r"|(?:carried|adopted|approved|passed)\s+unanimously"
    r"|unanimously\s+(?:carried|adopted|approved|passed|voted)",
    re.I)

# Fallback: an explicit motion that carried, without a printed tally.
_MOTION_RE = re.compile(
    r"(?:motion|moved|move)\b[^.]{0,200}?\b(carried|adopted|approved|passed)\b",
    re.I | re.S)

_FAIL_RE = re.compile(r"fail|defeat|denied|did not (?:carry|pass)", re.I)
_NOVOTE_RE = re.compile(r"withdraw|tabled|deferred|continued to|no action", re.I)


def _tonum(tok):
    tok = tok.strip().lower()
    if tok.isdigit():
        return int(tok)
    return _NUMWORDS.get(tok)


def _clean_ws(s):
    return re.sub(r"\s+", " ", s or "").strip()


def _surname(name):
    toks = [t for t in re.split(r"\s+", name.strip()) if t]
    return toks[-1] if toks else name


def _name_tokens(frag):
    toks = re.findall(r"[A-Za-z][A-Za-z.'\-]+", frag)
    return [t for t in toks
            if t[:1].isupper() and t.lower().strip(".") not in _TITLE_TOKENS]


def _parse_name_list(block):
    """Parse a roster block into a list of full person names."""
    if not block or re.match(r"^\s*none\b", block, re.I):
        return []
    names = []
    for frag in re.split(r"[;,\n]|\band\b", block):
        kept = _name_tokens(frag)
        if len(kept) >= 2:
            nm = _clean_ws(" ".join(kept[:3]))
            if nm and nm not in names:
                names.append(nm)
    return names


_STOPS = (r"(?:members?\s+)?absent|invocation|pledge|recogn|approv|consent|"
          r"adopt|minutes|call to order|quorum|\n\s*\n|proclamation")


def _parse_attendance(text):
    """Return {name: 'present'|'absent'} derived from the roll call."""
    present, absent = [], []

    mp = re.search(
        r"(?:members?\s+)?present\s*(?:members?|were|was|are|:|[-\u2013\u2014])?\s*"
        r"(.{0,380}?)(?:" + _STOPS + ")",
        text, re.I | re.S)
    if mp:
        present = _parse_name_list(mp.group(1))

    ma = re.search(
        r"(?:members?\s+)?absent\s*(?:members?|were|was|:|[-\u2013\u2014])?\s*"
        r"(.{0,240}?)(?:\n\s*\n|also\s+present|staff|invocation|pledge|"
        r"present|recogn)",
        text, re.I | re.S)
    if ma:
        absent = _parse_name_list(ma.group(1))

    # Fallback: derive roster from role-tagged full-name mentions.
    if not present:
        discovered = []
        for m in re.finditer(
                r"(?:Supervisors?|Chair(?:man|woman|person)?|"
                r"Vice\s+Chair(?:man|woman)?)\s+"
                r"([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,2})", text):
            nm = _clean_ws(m.group(1))
            if nm not in discovered:
                discovered.append(nm)
        for m in re.finditer(
                r"([A-Z][a-z]+\s+[A-Z][a-z]+)\s*,\s*"
                r"(?:Chair(?:man|woman)?|Vice\s+Chair(?:man|woman)?)", text):
            nm = _clean_ws(m.group(1))
            if nm not in discovered:
                discovered.append(nm)
        present = [n for n in discovered if n not in absent]

    attendance = {}
    for n in present:
        attendance[n] = "present"
    for n in absent:
        attendance.setdefault(n, "absent")
    return attendance


def _parse_tally(window):
    for rx in _TALLY_RES:
        m = rx.search(window)
        if m:
            a, b = _tonum(m.group(1)), _tonum(m.group(2))
            if a is not None and b is not None and a + b <= 15:
                return (a, b)
    return None


def _exceptions(window, present_names):
    """Named dissenters / abstainers / recusals inside a motion window."""
    nays, abst, rec = set(), set(), set()

    def _scan(pattern, bucket):
        for mm in re.finditer(pattern, window, re.I):
            seg = window[max(0, mm.start() - 100):mm.start()].lower()
            for nm in present_names:
                if _surname(nm).lower() in seg:
                    bucket.add(nm)

    _scan(r"voting\s+(?:nay|no|against|in\s+opposition)|opposed", nays)
    _scan(r"abstain", abst)
    _scan(r"recus", rec)
    return nays, abst, rec


def _window(text, s, e):
    """Coherent evidence window around a span, verbatim substring of text."""
    ls = max(0, s - 200)
    seg = text[ls:s]
    cut = max(seg.rfind("."), seg.rfind("\n"))
    if cut != -1:
        ls = ls + cut + 1
    rseg = text[e:e + 200]
    dot = rseg.find(".")
    qe = e + (dot + 1 if dot != -1 else len(rseg))
    win = text[ls:qe]
    quote = win.strip()
    if len(quote) > 400:
        quote = quote[-400:].strip()
    return win, quote


def _build_positions(result, present, absent, nays, abst, rec):
    positions = []
    for n in present:
        if n in nays:
            p = "no"
        elif n in abst:
            p = "abstain"
        elif n in rec:
            p = "recused"
        else:
            p = "aye" if result == "pass" else "no"
        positions.append({"member": n, "position": p})
    for n in absent:
        positions.append({"member": n, "position": "absent"})
    counts = {}
    for pr in positions:
        counts[pr["position"]] = counts.get(pr["position"], 0) + 1
    return positions, counts


def _extract_votes(text, attendance, doc_url, max_votes=80):
    present = [n for n, s in attendance.items() if s == "present"]
    absent = [n for n, s in attendance.items() if s == "absent"]
    out = []
    if not present:
        return out

    # Collect anchor spans: (start, end, kind). Tallies first (strongest).
    anchors = []
    for rx in _TALLY_RES:
        for m in rx.finditer(text):
            anchors.append((m.start(), m.end(), "tally"))
    for m in _UNANIMOUS_RE.finditer(text):
        anchors.append((m.start(), m.end(), "unanimous"))
    for m in _MOTION_RE.finditer(text):
        anchors.append((m.start(), m.end(), "motion"))
    anchors.sort(key=lambda a: (a[0], {"tally": 0, "unanimous": 1, "motion": 2}[a[2]]))

    used_end = -1
    for s, e, kind in anchors:
        if len(out) >= max_votes:
            break
        if s < used_end:
            continue

        win, quote = _window(text, s, e)
        if not quote:
            continue

        ctx = text[max(0, s - 90):e + 120]
        if _NOVOTE_RE.search(ctx):
            used_end = e
            continue

        result = "fail" if _FAIL_RE.search(ctx) else "pass"
        tally = _parse_tally(win)
        if tally is not None and tally[0] < tally[1]:
            result = "fail"

        if kind != "tally" and tally is None and result == "fail":
            used_end = e
            continue  # no numeric evidence for a defeat -> do not invent

        nays, abst, rec = _exceptions(win, present)
        positions, counts = _build_positions(result, present, absent,
                                             nays, abst, rec)

        # If the document prints a tally, our reconstruction must match it
        # exactly; otherwise attribution is a guess and we skip.
        if tally is not None:
            a, b = tally
            if counts.get("aye", 0) != a or counts.get("no", 0) != b:
                used_end = e
                continue

        used_end = e
        action = "failed" if result == "fail" else "approved"
        title = _clean_ws(win)[:180] or "Board action"
        out.append({
            "title": title,
            "action": action,
            "result": result,
            "positions": positions,
            "counts": counts,
            "evidence": {"quote": quote, "doc_url": doc_url},
        })
    return out


# ---------------------------------------------------------------------------
# CivicClerk backend access
# ---------------------------------------------------------------------------
def _events_from(data):
    if isinstance(data, dict):
        for k in ("value", "items", "Events", "data"):
            if isinstance(data.get(k), list):
                return data[k]
        return []
    if isinstance(data, list):
        return data
    return []


def _get_events(rt, top):
    today = date.today().isoformat()
    filt = f"startDateTime lt {today}T00:00:00Z"
    param_sets = [
        {"$filter": filt, "$orderby": "startDateTime desc", "$top": str(top)},
        {"$filter": filt, "$orderby": "startDateTime desc",
         "$top": str(top), "$expand": "publishedFiles"},
        {"$orderby": "startDateTime desc", "$top": str(top)},
    ]
    for p in param_sets:
        try:
            data = rt.fetch_json(EVENTS_URL, params=p)
        except Exception:
            continue
        evs = _events_from(data)
        if evs:
            return evs
    return []


def _event_id(ev):
    for k in ("id", "eventId", "Id", "EventId"):
        if ev.get(k) is not None:
            return ev[k]
    return None


def _event_date(ev):
    for k in ("startDateTime", "startDate", "date", "meetingDate", "eventDate"):
        v = ev.get(k)
        if isinstance(v, str) and re.match(r"^\d{4}-\d{2}-\d{2}", v):
            return v[:10]
    return None


def _event_body(ev):
    for k in ("categoryName", "category", "bodyName", "eventName", "name"):
        v = ev.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return BODY_NAME


def _files_of(ev):
    for k in ("publishedFiles", "files", "eventFiles", "PublishedFiles"):
        v = ev.get(k)
        if isinstance(v, list):
            return v
    return []


def _file_meta(f):
    fid = None
    for k in ("fileId", "id", "publishedFileId", "FileId", "Id"):
        if f.get(k) is not None:
            fid = f[k]
            break
    label = " ".join(str(f.get(k) or "") for k in
                     ("name", "fileName", "type", "fileType", "categoryName"))
    return fid, label.lower()


def _get_files(rt, ev):
    files = _files_of(ev)
    if files:
        return files
    eid = _event_id(ev)
    if eid is None:
        return []
    try:
        data = rt.fetch_json(f"{API_BASE}/v1/Events({eid})",
                             params={"$expand": "publishedFiles"})
        if isinstance(data, dict):
            fs = _files_of(data)
            if fs:
                return fs
    except Exception:
        pass
    return []


def _pick_minutes(files):
    action_fallback = None
    for f in files:
        fid, label = _file_meta(f)
        if fid is None:
            continue
        if "minute" in label:
            return fid
        if ("action" in label or "summary" in label) and action_fallback is None:
            action_fallback = fid
    return action_fallback


def _fetch_file_text(rt, fid):
    for pt in ("false", "true"):
        url = FILE_STREAM_TMPL.format(fid=fid, pt=pt)
        try:
            t = rt.fetch_text(url)
        except Exception:
            continue
        if t and len(t) > 200:
            return t, url
    return None, None


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------
def extract(rt, args):
    max_meetings = int(args[0]) if args else 1

    records = {"meetings": [], "agenda_items": [], "vote_events": [], "members": []}
    run_meta = {
        "source_id": SOURCE_ID,
        "extractor_version": EXTRACTOR_VERSION,
        "schema_version": SCHEMA_VERSION,
        "row_counts": {},
    }

    top = min(max(max_meetings * 6, 40), 250)
    events = _get_events(rt, top)
    if not events:
        run_meta["row_counts"] = {k: 0 for k in records}
        return records, run_meta

    bos = [e for e in events if "board of supervisors" in _event_body(e).lower()]
    if bos:
        events = bos

    today = date.today()
    seen_members, seen_member_set = [], set()

    def _remember(name):
        if name and name not in seen_member_set:
            seen_member_set.add(name)
            seen_members.append(name)

    seen_meeting_ids = set()
    kept = 0
    for ev in events:
        if kept >= max_meetings:
            break
        try:
            d = _event_date(ev)
            eid = _event_id(ev)
            if not d or eid is None:
                continue
            try:
                y, mo, dy = (int(x) for x in d.split("-"))
                if date(y, mo, dy) >= today:
                    continue  # strictly-past only
            except Exception:
                continue

            mid = f"{SOURCE_ID}-{d}"
            if mid in seen_meeting_ids:
                continue

            files = _get_files(rt, ev)
            fid = _pick_minutes(files)
            if fid is None:
                continue  # no actions/minutes document -> skip

            text, doc_url = _fetch_file_text(rt, fid)
            if not text:
                continue

            attendance = _parse_attendance(text)
            if not attendance or "present" not in attendance.values():
                continue  # cannot derive a roster honestly -> skip

            votes = _extract_votes(text, attendance, doc_url)

            seen_meeting_ids.add(mid)
            source_url = f"{PORTAL_BASE}/event/{eid}/overview"
            records["meetings"].append({
                "meeting_id": mid,
                "body": _event_body(ev),
                "date": d,
                "attendance": attendance,
                "source_url": source_url,
                "data_source_url": doc_url,
                "file_number": None,
                "source_id": SOURCE_ID,
                "extractor_version": EXTRACTOR_VERSION,
            })
            for nm in attendance:
                _remember(nm)

            for i, v in enumerate(votes, start=1):
                item_id = f"{mid}-item-{i}"
                vote_id = f"{mid}-vote-{i}"
                records["agenda_items"].append({
                    "item_id": item_id,
                    "meeting_id": mid,
                    "title": v["title"],
                    "action": v["action"],
                    "result": v["result"],
                    "file_number": None,
                    "source_id": SOURCE_ID,
                    "extractor_version": EXTRACTOR_VERSION,
                })
                records["vote_events"].append({
                    "vote_id": vote_id,
                    "meeting_id": mid,
                    "item_id": item_id,
                    "positions": v["positions"],
                    "counts": v["counts"],
                    "result": v["result"],
                    "file_number": None,
                    "evidence": v["evidence"],
                    "source_id": SOURCE_ID,
                    "extractor_version": EXTRACTOR_VERSION,
                })

            kept += 1
        except Exception:
            continue

    for nm in seen_members:
        records["members"].append({
            "name": nm,
            "source_id": SOURCE_ID,
            "extractor_version": EXTRACTOR_VERSION,
        })

    run_meta["row_counts"] = {k: len(v) for k, v in records.items()}
    return records, run_meta
