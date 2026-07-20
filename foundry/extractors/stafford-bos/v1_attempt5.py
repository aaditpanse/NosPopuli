"""Deterministic extractor for source `stafford-bos`
(Stafford County, Virginia — Board of Supervisors).

Platform: CivicClerk (CivicPlus Meetings Select). The public portal is a JS
SPA backed by an OData API at `staffordcova.api.civicclerk.com`. `/v1/Events`
lists meetings (with publishedFiles); minutes PDFs are streamed as text via
`/v1/Meetings/GetMeetingFileStream(...)`. Votes are recorded in the minutes as
explicit tally blocks, e.g.:

    "The Voting Board tally was: Yea: (7) Diggs, Guy, Evans, Yeung, Allen,
     English, Vanuch No: (0)"

so per-member positions are parsed directly from each motion's tally (Yea /
No / Abstain / Absent / Recused) — never defaulted across the roster. Every
vote carries a verbatim `evidence` quote sliced from the fetched document.
"""

import re
from datetime import date

# ---------------------------------------------------------------------------
# Tenant / platform constants — re-point by editing these.
# ---------------------------------------------------------------------------
SOURCE_ID = "stafford-bos"
EXTRACTOR_VERSION = "1"
SCHEMA_VERSION = "1.3"

TENANT = "staffordcova"
PORTAL_BASE = f"https://{TENANT}.portal.civicclerk.com"
API_BASE = f"https://{TENANT}.api.civicclerk.com"
BODY_MATCH = "board of supervisors"
BODY_NAME = "Board of Supervisors"

EVENTS_URL = f"{API_BASE}/v1/Events"
FILE_STREAM_TMPL = (
    API_BASE + "/v1/Meetings/GetMeetingFileStream(fileId={fid},plainText={pt})"
)

# ---------------------------------------------------------------------------
# Vote-tally parsing
# ---------------------------------------------------------------------------
CAT_RE = re.compile(
    r"\b(Yeas?|Ayes?|Nays?|No|Abstain\w*|Absten\w*|Absent|Recus\w*)\s*:?\s*"
    r"\(\s*(\d+)\s*\)",
    re.I,
)

_STOP = {
    "the", "and", "no", "nay", "yea", "aye", "item", "work", "session",
    "board", "county", "chairman", "chairwoman", "chairperson", "chair",
    "vice", "supervisor", "supervisors", "mr", "mrs", "ms", "dr", "hon",
    "absent", "abstain", "abstained", "abstention", "abstentions", "present",
    "voting", "tally", "was", "were", "by", "of", "to", "seconded", "motion",
    "motioned", "moved", "none", "staff", "also", "member", "members",
    "recused", "recuse", "discussion", "consideration", "presentation",
    "presentations", "report", "reports", "district", "regular", "meeting",
    "call", "order", "roll", "adopted", "approve", "approved", "carried",
}


def _clean_ws(s):
    return re.sub(r"\s+", " ", s or "").strip()


def _surname(name):
    toks = [t for t in re.split(r"\s+", name.strip()) if t]
    return toks[-1] if toks else name


def _catpos(word):
    w = word.lower()
    if w.startswith(("yea", "aye")):
        return "aye"
    if w.startswith(("no", "nay")):
        return "no"
    if w.startswith("absten") or w.startswith("abstain"):
        return "abstain"
    if w.startswith("absent"):
        return "absent"
    if w.startswith("recus"):
        return "recused"
    return None


def _tally_names(seg, n):
    """Extract up to `n` Title-case surnames from a tally category segment.

    Requires a lowercase 2nd char so ALL-CAPS section headings (e.g. "WORK
    SESSION ITEMS") that follow a zero/short category are never treated as
    member names.
    """
    out = []
    if n <= 0:
        return out
    for tok in re.findall(r"[A-Z][a-z][A-Za-z.'\-]*", seg):
        if tok.lower().strip(".") in _STOP:
            continue
        out.append(tok)
        if len(out) >= n:
            break
    return out


def _counts(positions):
    c = {}
    for pr in positions:
        c[pr["position"]] = c.get(pr["position"], 0) + 1
    return c


def _canon_map(surnames, text):
    """Best-effort surname -> 'First Surname' canonicalization."""
    m = {}
    for sur in surnames:
        rx = re.compile(r"\b([A-Z][a-z]+)\s+" + re.escape(sur) + r"\b")
        full = None
        for mm in rx.finditer(text):
            fn = mm.group(1)
            if fn.lower() in _STOP or fn.isupper():
                continue
            full = fn + " " + sur
            break
        m[sur.lower()] = full or sur
    return m


def _group_blocks(markers):
    """Group flat CAT markers into per-motion tally blocks (Yea..last)."""
    blocks = []
    i = 0
    while i < len(markers):
        if _catpos(markers[i].group(1)) != "aye":
            i += 1
            continue
        grp = [markers[i]]
        j = i + 1
        while j < len(markers):
            gap = markers[j].start() - grp[-1].end()
            p = _catpos(markers[j].group(1))
            if p == "aye" or gap > 200:
                break
            grp.append(markers[j])
            j += 1
        blocks.append(grp)
        i = j if j > i else i + 1
    return blocks


def _extract_votes(text, doc_url):
    """Return (votes, attendance) parsed from explicit tally blocks."""
    markers = list(CAT_RE.finditer(text))
    blocks = _group_blocks(markers)

    raw = []
    for grp in blocks:
        cats = {"aye": [], "no": [], "abstain": [], "absent": [], "recused": []}
        for k, mk in enumerate(grp):
            pos = _catpos(mk.group(1))
            cnt = int(mk.group(2))
            seg_end = (grp[k + 1].start() if k + 1 < len(grp)
                       else min(len(text), mk.end() + 120))
            seg = text[mk.end():seg_end]
            cats[pos].extend(_tally_names(seg, cnt))
        if not cats["aye"] and not cats["no"]:
            continue  # no actual voting content

        first, last = grp[0], grp[-1]
        qend = last.end()
        base = max(0, first.start() - 340)
        back = text[base:first.start()]
        li = max(back.lower().rfind("motion"), back.lower().rfind("moved"))
        qs = base + li if li != -1 else max(0, first.start() - 40)
        if qend - qs > 400:
            qs = qend - 400
        quote = text[qs:qend].strip()
        title = _clean_ws(text[qs:first.start()])[:180] or "Board action"
        if not quote:
            continue
        raw.append({"cats": cats, "quote": quote, "title": title})

    surnames = set()
    for r in raw:
        for v in r["cats"].values():
            surnames.update(v)
    canon = _canon_map(surnames, text)

    present, absent_only = set(), set()
    votes = []
    for r in raw:
        positions, seen = [], set()
        for pk in ("aye", "no", "abstain", "absent", "recused"):
            for s in r["cats"][pk]:
                name = canon.get(s.lower(), s)
                if name in seen:
                    continue
                seen.add(name)
                positions.append({"member": name, "position": pk})
                if pk == "absent":
                    absent_only.add(name)
                else:
                    present.add(name)
        if not positions:
            continue
        counts = _counts(positions)
        naye, nno = counts.get("aye", 0), counts.get("no", 0)
        result = "pass" if naye > nno else "fail"
        votes.append({
            "title": r["title"],
            "action": "approved" if result == "pass" else "failed",
            "result": result,
            "positions": positions,
            "counts": counts,
            "evidence": {"quote": r["quote"], "doc_url": doc_url},
        })

    attendance = {}
    for n in present:
        attendance[n] = "present"
    for n in absent_only:
        attendance.setdefault(n, "absent")
    return votes, attendance


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
    for k in ("eventName", "categoryName", "category", "bodyName", "name"):
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
    """Prefer approved Minutes; fall back to an action/summary doc.

    Never returns a full agenda packet (avoids hundreds of pages).
    """
    fallback = None
    for f in files:
        fid, label = _file_meta(f)
        if fid is None:
            continue
        if "minute" in label:
            return fid
        if ("action" in label or "summary" in label) and fallback is None:
            fallback = fid
    return fallback


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

    bos = [e for e in events if BODY_MATCH in _event_body(e).lower()]
    if bos:
        events = bos

    today = date.today()
    member_order, member_set = [], set()

    def _remember(name):
        if name and name not in member_set:
            member_set.add(name)
            member_order.append(name)

    seen_dates = set()
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
                    continue  # skip future / today's un-minuted events
            except Exception:
                continue

            mid = f"{SOURCE_ID}-{d}"
            if mid in seen_dates:
                continue

            files = _get_files(rt, ev)
            fid = _pick_minutes(files)
            if fid is None:
                continue

            text, doc_url = _fetch_file_text(rt, fid)
            if not text:
                continue

            votes, attendance = _extract_votes(text, doc_url)
            if not attendance or "present" not in attendance.values():
                continue

            seen_dates.add(mid)
            records["meetings"].append({
                "meeting_id": mid,
                "body": _event_body(ev),
                "date": d,
                "attendance": attendance,
                "source_url": f"{PORTAL_BASE}/event/{eid}/overview",
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
                for pr in v["positions"]:
                    _remember(pr["member"])
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

    for nm in member_order:
        records["members"].append({
            "name": nm,
            "source_id": SOURCE_ID,
            "extractor_version": EXTRACTOR_VERSION,
        })

    run_meta["row_counts"] = {k: len(v) for k, v in records.items()}
    return records, run_meta
