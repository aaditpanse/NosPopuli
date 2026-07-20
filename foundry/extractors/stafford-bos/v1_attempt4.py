"""Deterministic extractor for source `stafford-bos`
(Stafford County, Virginia — Board of Supervisors).

CivicClerk (CivicPlus Meetings Select). The public portal is a JS SPA driven
by an OData backend on `staffordcova.api.civicclerk.com` that serves Event
records (with published-file listings) and file streams (PDFs as text). All
I/O goes through the injected runtime. Votes are parsed from narrative minutes
PDFs; every derived vote carries a verbatim `evidence` quote.
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
    "regular", "meeting", "call", "order", "roll",
}

_DISTRICTS = {
    "aquia", "falmouth", "garrisonville", "george", "washington", "griffis",
    "widewater", "hartwood", "rock", "hill",
}

_DIST_RX = (r"(?:Aquia|Falmouth|Garrisonville|George\s+Washington|"
            r"Griffis[-\s]?Widewater|Hartwood|Rock\s+Hill)")

_TALLY_RES = [
    re.compile(r"vote of\s+([0-9]{1,2})\s*(?:to|[-\u2013\u2014])\s*([0-9]{1,2})", re.I),
    re.compile(r"vote of\s+([a-z]+)\s+to\s+([a-z]+)", re.I),
    re.compile(r"\bayes?\b\D{0,6}(\d{1,2})\D{0,40}?\bnays?\b\D{0,6}(\d{1,2})", re.I),
]

OUT_RE = re.compile(r"\b(carried|adopted|approved|passed|failed|defeated)\b", re.I)

NAY_TRIG = (r"voting\s+(?:nay|no|against|in\s+opposition|in\s+the\s+negative)"
            r"|opposed\b|the\s+(?:nay|no|dissenting)\s+vote|in\s+the\s+negative")
_FAILWORDS_RE = re.compile(
    r"motion\s+(?:failed|was\s+defeated)|failed\s+by|defeated|"
    r"did\s+not\s+(?:carry|pass)", re.I)
_NOVOTE_RE = re.compile(
    r"withdraw|tabl|defer|continued to|no action|first reading|"
    r"public hearing (?:was )?(?:opened|held|closed)", re.I)


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


def _roster(text):
    """Board roster via several strategies; returns list of member names."""
    cand = []

    def add(nm):
        toks = [t for t in _clean_ws(nm).split()
                if t.lower().strip(".") not in _TITLE_TOKENS
                and t.lower().strip(".") not in _DISTRICTS]
        nm2 = " ".join(toks)
        if len(nm2.split()) >= 2 and nm2 not in cand:
            cand.append(nm2)

    # A) full name paired with a magisterial district
    for mm in re.finditer(
            r"([A-Z][a-z]+(?:\s+[A-Z][a-z'\-]+){1,2})"
            r"(?:\s*,?\s*(?:Chair(?:man|woman)?|Vice\s+Chair(?:man|woman)?))?"
            r"\s*,?\s*" + _DIST_RX + r"\s+District", text):
        add(mm.group(1))
    for mm in re.finditer(
            _DIST_RX + r"\s+District\s*[-\u2013:,]?\s*"
            r"([A-Z][a-z]+\s+[A-Z][a-z'\-]+)", text):
        add(mm.group(1))
    # B) "Full Name, Chairman/Vice Chairman"
    for mm in re.finditer(
            r"([A-Z][a-z]+\s+[A-Z][a-z'\-]+)\s*,\s*"
            r"(?:Chair(?:man|woman)?|Vice\s+Chair(?:man|woman)?)\b", text):
        add(mm.group(1))
    if len(cand) >= 4:
        return cand[:11]

    # C) role/honorific + surname frequency
    c = {}
    for mm in re.finditer(
            r"(?:Supervisors?|Vice\s+Chair(?:man|woman)?|"
            r"Chair(?:man|woman|person)?|Mr|Mrs|Ms|Dr)\.?\s+"
            r"([A-Z][a-zA-Z'\-]{2,})", text):
        s = mm.group(1)
        sl = s.lower().strip(".")
        if sl in _TITLE_TOKENS or sl in _DISTRICTS:
            continue
        c[s] = c.get(s, 0) + 1
    for s, n in sorted(c.items(), key=lambda kv: (-kv[1], kv[0])):
        if n >= 2 and not any(_surname(x).lower() == s.lower() for x in cand):
            cand.append(s)
    if len(cand) >= 3:
        return cand[:12]

    # D) roll-call present block
    m = re.search(r"\bpresent\b[^A-Za-z]{0,10}(.{0,520}?)(?:\babsent\b|\.\s)",
                  text[:12000], re.I | re.S)
    if m:
        for nm in _names(m.group(1)):
            if nm not in cand:
                cand.append(nm)
    return cand[:12]


def _parse_attendance(text):
    roster = _roster(text)
    if not roster:
        return {}
    absent = set()
    m2 = re.search(
        r"\babsent\b[^A-Za-z0-9]{0,10}(?:members?|were|was)?[^A-Za-z0-9]{0,10}"
        r"(.{0,240}?)(?:\.\s|\.$|\n\s*\n|also\b)", text[:12000], re.I | re.S)
    if m2 and not re.match(r"\s*none", m2.group(1), re.I):
        ab = m2.group(1)
        for nm in roster:
            if re.search(r"\b" + re.escape(_surname(nm)) + r"\b", ab):
                absent.add(nm)
    att = {}
    for nm in roster:
        att[nm] = "absent" if nm in absent else "present"
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


def _collect(win, trig, roster):
    found = []
    for mm in re.finditer(trig, win, re.I):
        pre = win[max(0, mm.start() - 120):mm.start()]
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


def _explicit_roll(win, roster):
    pairs = []
    for mm in re.finditer(
            r"([A-Z][a-zA-Z'\-]{2,})\s*[-\u2013:]\s*"
            r"(Aye|Yea|Yes|Nay|No|Abstain|Absent|Recus\w*)", win, re.I):
        sur = mm.group(1)
        if sur.lower().strip(".") in _TITLE_TOKENS or sur.lower() in _DISTRICTS:
            continue
        p = mm.group(2).lower()
        posn = {"aye": "aye", "yea": "aye", "yes": "aye", "nay": "no",
                "no": "no", "abstain": "abstain", "absent": "absent"}.get(p)
        if posn is None and p.startswith("recus"):
            posn = "recused"
        if posn is None:
            continue
        m = _match(sur, roster)
        name = m if m else sur
        pairs.append((name, posn))
    seen, res = set(), []
    for m, p in pairs:
        if m in seen:
            continue
        seen.add(m)
        res.append((m, p))
    return res


def _counts(positions):
    c = {}
    for pr in positions:
        c[pr["position"]] = c.get(pr["position"], 0) + 1
    return c


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
    return positions, _counts(positions)


def _extract_votes(text, attendance, doc_url, max_votes=200):
    present = [n for n, s in attendance.items() if s == "present"]
    absent = [n for n, s in attendance.items() if s == "absent"]
    out = []
    if not present:
        return out

    consumed = -1
    for om in OUT_RE.finditer(text):
        if len(out) >= max_votes:
            break
        s = om.start()
        if s < consumed:
            continue
        win = text[max(0, s - 300):s + 280]
        if not re.search(r"motion|moved|board|resolution|ordinance|\bvote\b",
                         win, re.I):
            continue  # an outcome word not attached to an action -> skip

        verb = om.group(1).lower()
        tally = _parse_tally(win)
        rolls = _explicit_roll(win, roster=present)
        nays = _collect(win, NAY_TRIG, present)
        abst = _collect(win, r"abstain", present)
        rec = _collect(win, r"recus", present)
        fail = (verb in ("failed", "defeated")
                or bool(_FAILWORDS_RE.search(win))
                or (tally and tally[0] < tally[1]))
        result = "fail" if fail else "pass"

        if rolls and len(rolls) >= 3:
            positions = [{"member": m, "position": p} for m, p in rolls]
            counts = _counts(positions)
            result = "pass" if counts.get("aye", 0) >= counts.get("no", 0) else "fail"
        else:
            if _NOVOTE_RE.search(win) and not tally:
                consumed = s + 80
                continue
            if tally:
                a, b, _, _ = tally
                # Only unanimous, or splits whose dissenters are named,
                # can be reconstructed honestly.
                if b > 0 and len(nays) != b:
                    consumed = s + 80
                    continue
            elif fail:
                consumed = s + 80
                continue
            pres = list(present)
            for nm in nays + abst + rec:
                if nm not in pres and nm not in absent:
                    pres.append(nm)
            positions, counts = _build_positions(
                pres, absent, result, set(nays), set(abst), set(rec))

        if not positions:
            consumed = s + 80
            continue

        # verbatim evidence window
        ls = max(0, s - 220)
        seg = text[ls:s]
        cut = max(seg.rfind("."), seg.rfind("\n"))
        if cut != -1:
            ls = ls + cut + 1
        ext = text[s:s + 220]
        dot = ext.find(".")
        qe = s + (dot + 1 if dot != -1 else 180)
        quote = text[ls:qe].strip()
        if len(quote) > 400:
            quote = quote[-400:].strip()
        consumed = qe
        if not quote:
            continue

        out.append({
            "title": _clean_ws(text[ls:s + 40])[:180] or "Board action",
            "action": "failed" if result == "fail" else "approved",
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
                    continue
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
