"""Deterministic extractor for source `stafford-bos`
(Stafford County, Virginia — Board of Supervisors).

Platform: CivicClerk (CivicPlus Meetings Select). The public portal is a JS
SPA driven by an OData backend on `staffordcova.api.civicclerk.com`, which
serves Event records (with published-file listings) and file streams (PDFs
delivered as text). All I/O goes through the injected runtime.

Votes live inside narrative minutes PDFs, so vote_events are derived from
prose (roster minus named dissent/abstention/recusal/absence) and each carries
a verbatim `evidence` quote copied from the fetched document text.
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
# Vocabulary / regexes
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
    "none", "board", "of", "were", "was", "district", "stafford", "virginia",
    "regular", "meeting", "supervisor's", "supervisors'",
}

# Stafford magisterial districts — excluded so district names aren't parsed
# as member names.
_DISTRICTS = {
    "aquia", "falmouth", "garrisonville", "george", "washington", "griffis",
    "widewater", "hartwood", "rock", "hill",
}

_TALLY_RES = [
    re.compile(r"vote of\s+([0-9]{1,2})\s*(?:to|[-\u2013\u2014])\s*([0-9]{1,2})", re.I),
    re.compile(r"vote of\s+([a-z]+)\s+to\s+([a-z]+)", re.I),
    re.compile(r"\bayes?\b\D{0,6}(\d{1,2})\D{0,40}?\bnays?\b\D{0,6}(\d{1,2})", re.I),
]

OPENER_RE = re.compile(
    r"\bmotion\b|\bmoved\b|"
    r"the\s+board\s+(?:voted|adopted|approved|authorized|awarded|appointed|"
    r"reappointed|denied)",
    re.I)

NAY_TRIG = (r"voting\s+(?:nay|no|against|in\s+opposition)|opposed\b|"
            r"the\s+(?:nay|no)\s+vote")
_FAILWORDS_RE = re.compile(
    r"motion\s+(?:failed|was\s+defeated)|failed\s+by|defeated|"
    r"did\s+not\s+(?:carry|pass)", re.I)


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


def _names(block):
    """Full-name list from a roll-call fragment (backup path only)."""
    if not block or re.match(r"^\s*none\b", block, re.I):
        return []
    out = []
    for frag in re.split(r"[;,\n]|\band\b", block):
        toks = [t for t in re.findall(r"[A-Za-z][A-Za-z.'\-]+", frag)
                if t[:1].isupper()
                and t.lower().strip(".") not in _TITLE_TOKENS
                and t.lower().strip(".") not in _DISTRICTS]
        if len(toks) >= 2:
            nm = _clean_ws(" ".join(toks[:2]))
            if nm and nm not in out:
                out.append(nm)
    return out


def _roster_surnames(text):
    """Board roster as surnames, from repeated role-tagged mentions."""
    c = {}
    for mm in re.finditer(
            r"(?:Supervisors?|Vice\s+Chair(?:man|woman)?|"
            r"Chair(?:man|woman|person)?)\s+([A-Z][a-zA-Z'\-]{2,})", text):
        s = mm.group(1)
        sl = s.lower().strip(".")
        if sl in _TITLE_TOKENS or sl in _DISTRICTS:
            continue
        c[s] = c.get(s, 0) + 1
    roster = [s for s, n in sorted(c.items(), key=lambda kv: (-kv[1], kv[0]))
              if n >= 3][:11]
    if len(roster) >= 3:
        return roster
    # backup: roll-call present block -> surnames
    m = re.search(r"\bpresent\b[^A-Za-z]{0,6}(.{0,460}?)(?:\babsent\b|\.\s|\.$)",
                  text[:9000], re.I | re.S)
    if m:
        for nm in _names(m.group(1)):
            s = _surname(nm)
            if s not in roster:
                roster.append(s)
    return roster[:11]


def _parse_attendance(text):
    roster = _roster_surnames(text)
    absent = set()
    m2 = re.search(
        r"\babsent\b[^A-Za-z0-9]{0,6}(?:members?|were|was)?[^A-Za-z0-9]{0,6}"
        r"(.{0,220}?)(?:\.\s|\.$|\n\s*\n)", text[:9000], re.I | re.S)
    if m2 and not re.match(r"\s*none", m2.group(1), re.I):
        ab = m2.group(1)
        for s in roster:
            if re.search(r"\b" + re.escape(s) + r"\b", ab):
                absent.add(s)
    att = {}
    for s in roster:
        att[s] = "absent" if s in absent else "present"
    return att


def _parse_tally(seg):
    for rx in _TALLY_RES:
        m = rx.search(seg)
        if m:
            a, b = _tonum(m.group(1)), _tonum(m.group(2))
            if a is not None and b is not None and a + b <= 15:
                return (a, b, m.start(), m.end())
    return None


def _match(sur, roster):
    for n in roster:
        if n.lower() == sur.lower() or _surname(n).lower() == sur.lower():
            return n
    return None


def _collect(seg, trig, roster):
    """Members named in a dissent/abstain/recuse clause within seg."""
    found = []
    for mm in re.finditer(trig, seg, re.I):
        pre = seg[max(0, mm.start() - 120):mm.start()]
        idx = max(pre.lower().rfind("supervisor"), pre.lower().rfind("chair"))
        frag = pre[idx:] if idx != -1 else pre
        for tok in re.findall(r"[A-Z][a-zA-Z'\-]{2,}", frag):
            tl = tok.lower().strip(".")
            if tl in _TITLE_TOKENS or tl in _DISTRICTS:
                continue
            m = _match(tok, roster)
            name = m if m else tok
            if name not in found:
                found.append(name)
    return found


def _build_positions(present, absent, result, nays, abst, rec):
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


def _extract_votes(text, attendance, doc_url, max_votes=150):
    present = [n for n, s in attendance.items() if s == "present"]
    absent = [n for n, s in attendance.items() if s == "absent"]
    out = []
    if not present:
        return out

    consumed = -1
    for om in OPENER_RE.finditer(text):
        if len(out) >= max_votes:
            break
        p = om.start()
        if p < consumed:
            continue
        seg = text[p:p + 520]

        tally = _parse_tally(seg)
        vmatch = re.search(r"\b(carried|adopted|approved|passed|failed|defeated)\b",
                           seg, re.I)
        umatch = re.search(r"\bunanimous(?:ly)?\b", seg, re.I)
        if not (tally or vmatch or umatch):
            continue  # opener with no recorded outcome -> not a vote

        oe = 0
        anchor_start = 0
        if tally:
            oe = max(oe, tally[3]); anchor_start = tally[2]
        if vmatch:
            oe = max(oe, vmatch.end()); anchor_start = anchor_start or vmatch.start()
        if umatch:
            oe = max(oe, umatch.end()); anchor_start = anchor_start or umatch.start()

        nays = _collect(seg, NAY_TRIG, present)
        abst = _collect(seg, r"abstain", present)
        rec = _collect(seg, r"recus", present)

        if tally:
            a, b, _, _ = tally
            result = "pass" if a >= b else "fail"
            # We can honestly reconstruct only unanimous outcomes or splits
            # whose dissenters are explicitly named.
            if len(nays) != b and not (b == 0 and not nays):
                consumed = p + oe
                continue
        else:
            if _FAILWORDS_RE.search(seg):
                consumed = p + oe
                continue  # bare failure with no attributable roll
            result = "pass"

        # Named members not already on the roster are added as present voters.
        pres = list(present)
        for nm in list(nays) + list(abst) + list(rec):
            if nm not in pres and nm not in absent:
                pres.append(nm)

        positions, counts = _build_positions(pres, absent, result, nays, abst, rec)

        # Verbatim evidence window containing the motion and its outcome.
        qstart = p
        if oe > 380:
            qstart = max(p, p + anchor_start - 160)
        qend = min(len(text), p + oe)
        tail = text[qend:qend + 70]
        dot = tail.find(".")
        if dot != -1:
            qend = qend + dot + 1
        quote = text[qstart:qend].strip()
        if len(quote) > 400:
            quote = quote[-400:].strip()
        consumed = p + oe
        if not quote:
            continue

        action = "failed" if result == "fail" else "approved"
        out.append({
            "title": _clean_ws(text[p:p + 120]) or "Board action",
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
            return _files_of(data)
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
                continue

            text, doc_url = _fetch_file_text(rt, fid)
            if not text:
                continue

            attendance = _parse_attendance(text)
            if not attendance or "present" not in attendance.values():
                continue

            votes = _extract_votes(text, attendance, doc_url)

            seen_meeting_ids.add(mid)
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

    for nm in seen_members:
        records["members"].append({
            "name": nm,
            "source_id": SOURCE_ID,
            "extractor_version": EXTRACTOR_VERSION,
        })

    run_meta["row_counts"] = {k: len(v) for k, v in records.items()}
    return records, run_meta
