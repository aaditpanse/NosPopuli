"""Deterministic extractor for source `stafford-bos`
(Stafford County, Virginia — Board of Supervisors).

Platform: CivicClerk (CivicPlus Meetings Select). The public site
(staffordcova.portal.civicclerk.com) is a JavaScript SPA, but the SPA is
driven by a CivicClerk OData backend on the sibling `*.api.civicclerk.com`
host, which serves machine-readable Event records (including published-file
listings) and file streams. This module talks only to that backend through
the injected runtime; a human-viewable overview page is used for source_url.

Votes for Stafford BOS live inside narrative minutes PDFs, so vote_events are
derived from prose (roster minus named dissent/abstention/absence) and every
one of them carries a verbatim `evidence` quote copied from the fetched text.
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
    "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19,
    "twenty": 20,
}

_TITLE_TOKENS = {
    "chair", "chairman", "chairwoman", "chairperson", "vice", "vicechair",
    "supervisor", "supervisors", "member", "members", "mr", "mrs", "ms",
    "dr", "hon", "honorable", "the", "and", "county", "administrator",
    "clerk", "attorney", "deputy", "present", "absent", "staff", "also",
    "none", "board", "of",
}

MOTION_RE = re.compile(
    r"(?:motion|resolution|ordinance)[^.]{0,220}?"
    r"\b(carried|passed|failed|defeated|adopted|approved)\b",
    re.I | re.S,
)


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


def _parse_names(block):
    """Parse a present/absent roster block into a list of person names."""
    if not block:
        return []
    if re.match(r"^\s*none\b", block, re.I):
        return []
    names = []
    for frag in re.split(r"[;,\n]", block):
        toks = re.findall(r"[A-Za-z][A-Za-z.'\-]+", frag)
        kept = [t for t in toks
                if t.lower().strip(".") not in _TITLE_TOKENS and not t.isupper()
                or (len(t) > 1 and t[0].isupper() and t.lower().strip(".") not in _TITLE_TOKENS)]
        # keep only plausible name tokens (Capitalized, not a title word)
        kept = [t for t in toks
                if t[:1].isupper() and t.lower().strip(".") not in _TITLE_TOKENS]
        if len(kept) >= 2:
            nm = _clean_ws(" ".join(kept[:3]))
            if nm and nm not in names:
                names.append(nm)
    return names


def _parse_attendance(text):
    """Return {name: 'present'|'absent'} derived from a roll-call block."""
    present, absent = [], []

    mp = re.search(
        r"(?:Members?\s+)?Present\s*:?\s*(.{0,320}?)"
        r"(?:Members?\s+Absent|Absent\s*:|\n\s*\n|Also\s+present|Staff|$)",
        text, re.I | re.S)
    if mp:
        present = _parse_names(mp.group(1))

    ma = re.search(
        r"(?:Members?\s+)?Absent\s*:?\s*(.{0,220}?)"
        r"(?:\n\s*\n|Also\s+present|Staff|Present\s*:|$)",
        text, re.I | re.S)
    if ma:
        absent = _parse_names(ma.group(1))

    attendance = {}
    for n in present:
        attendance[n] = "present"
    for n in absent:
        if n not in attendance:  # a present listing wins if conflicting
            attendance[n] = "absent"
    return attendance


def _parse_tally(w):
    m = re.search(r"vote of\s+([0-9]+|[A-Za-z]+)\s*(?:to|[-\u2013\u2014])\s*([0-9]+|[A-Za-z]+)",
                  w, re.I)
    if not m:
        m = re.search(r"\b(\d{1,2})\s*[-\u2013\u2014]\s*(\d{1,2})\b", w)
        if not m:
            return None
    a, b = _tonum(m.group(1)), _tonum(m.group(2))
    if a is None or b is None:
        return None
    return (a, b)


def _exceptions(window, present_names):
    """Find named dissenters/abstainers/recusals within a motion window."""
    nays, abst, rec = set(), set(), set()

    def _scan(pattern, bucket):
        for mm in re.finditer(pattern, window, re.I):
            seg = window[max(0, mm.start() - 95):mm.start()]
            low = seg.lower()
            for nm in present_names:
                if _surname(nm).lower() in low:
                    bucket.add(nm)

    _scan(r"voting\s+(?:nay|no|against)\b", nays)
    _scan(r"abstain", abst)
    _scan(r"recus", rec)
    return nays, abst, rec


def _build_evidence_window(text, m):
    """Return (window, quote) around a motion-outcome match."""
    ls = max(0, m.start() - 120)
    seg = text[ls:m.start()]
    cut = max(seg.rfind("."), seg.rfind("\n"))
    if cut != -1:
        ls = ls + cut + 1
    qe = m.end()
    rseg = text[qe:qe + 170]
    dot = rseg.find(".")
    qe = qe + (dot + 1 if dot != -1 else len(rseg))
    window = text[ls:qe]
    quote = window.strip()
    if len(quote) > 400:
        quote = quote[-400:].strip()
    return window, quote


def _extract_votes(text, attendance, doc_url, max_votes=60):
    present = [n for n, s in attendance.items() if s == "present"]
    absent = [n for n, s in attendance.items() if s == "absent"]
    out = []
    if not present:
        return out
    last_qe = -1
    for m in MOTION_RE.finditer(text):
        if len(out) >= max_votes:
            break
        if m.start() < last_qe:
            continue
        verb = m.group(1).lower()
        window, quote = _build_evidence_window(text, m)
        last_qe = m.start() + len(window)
        if not quote:
            continue

        result = "fail" if verb in ("failed", "defeated") else "pass"
        tally = _parse_tally(window)

        # Do not fabricate a defeat with no numeric evidence.
        if result == "fail" and tally is None:
            continue

        nays, abst, rec = _exceptions(window, present)

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

        # If the document states an explicit tally, our reconstructed roll
        # must match it exactly; otherwise we cannot attribute the split
        # honestly and skip rather than invent a distribution.
        if tally is not None:
            a, b = tally
            if counts.get("aye", 0) != a or counts.get("no", 0) != b:
                continue

        action = {
            "failed": "failed", "defeated": "failed",
            "adopted": "adopted", "approved": "approved",
            "carried": "approved", "passed": "approved",
        }.get(verb, "approved")

        title = _clean_ws(window)[:180] or "Board action"

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
        {"$filter": filt, "$orderby": "startDateTime desc",
         "$top": str(top), "$expand": "publishedFiles"},
        {"$filter": filt, "$orderby": "startDateTime desc", "$top": str(top)},
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
            return _files_of(data)
    except Exception:
        pass
    return []


def _pick_minutes(files):
    """Prefer approved minutes / action summaries over full agenda packets."""
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

    # Filter to Board of Supervisors where the tenant hosts several bodies.
    bos = [e for e in events if "board of supervisors" in _event_body(e).lower()]
    if bos:
        events = bos

    today = date.today()
    seen_members = []
    seen_member_set = set()

    def _remember(name):
        if name and name not in seen_member_set:
            seen_member_set.add(name)
            seen_members.append(name)

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
                    continue  # skip future / today (not strictly past)
            except Exception:
                continue

            files = _get_files(rt, ev)
            fid = _pick_minutes(files)
            if fid is None:
                continue  # no actions/minutes document -> skip

            text, doc_url = _fetch_file_text(rt, fid)
            if not text:
                continue

            attendance = _parse_attendance(text)
            if not attendance:
                continue  # cannot derive roster honestly -> skip

            mid = f"{SOURCE_ID}-{d}"
            source_url = f"{PORTAL_BASE}/event/{eid}/overview"

            meeting = {
                "meeting_id": mid,
                "body": _event_body(ev),
                "date": d,
                "attendance": attendance,
                "source_url": source_url,
                "data_source_url": doc_url,
                "file_number": None,
                "source_id": SOURCE_ID,
                "extractor_version": EXTRACTOR_VERSION,
            }

            votes = _extract_votes(text, attendance, doc_url)

            # Register the meeting even if it produced no attributable votes;
            # attendance is itself a usable record.
            records["meetings"].append(meeting)
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
            # Robustness: skip any meeting whose documents are missing or
            # unparseable rather than crashing the run.
            continue

    for nm in seen_members:
        records["members"].append({
            "name": nm,
            "source_id": SOURCE_ID,
            "extractor_version": EXTRACTOR_VERSION,
        })

    run_meta["row_counts"] = {k: len(v) for k, v in records.items()}
    return records, run_meta
