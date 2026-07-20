"""Deterministic extractor for source `newyork-bos`.

New York City — New York City Council (Granicus Legistar tenant).

Enumeration: the public Legistar calendar (Calendar.aspx) lists
MeetingDetail links and, for concluded meetings, a "Minutes" View.ashx
document (PDF -> text via rt.fetch_text).

Title strategy (fix for the vague/procedural-title gate):
Each item's SUBJECT is its official legislative title — "A Local Law to
amend ... in relation to ..." or "Resolution calling upon ...". These are
long descriptive sentences that appear near the file number, but NOT always
inside the same block as the roll-call motion (coupled General-Order votes
reprint only the motion text "... this Resolution be Approved, by Council
approved by Roll Call ..."). So we build a document-wide map of
file-number -> legislative title from every occurrence, then attach the
real title to each voted item. Items for which no real legislative subject
can be found (only motion text / index tags like "Report Required") are
DROPPED rather than emitted with a procedural stub.

Coupled votes reprint the identical tally; those are ONE roll call, deduped
by their verbatim evidence quote.

Only the injected runtime `rt` performs I/O. Deterministic, stdlib only.
"""

import re
import hashlib
import datetime

EXTRACTOR_VERSION = "1"

SOURCE_ID = "newyork-bos"
RUN_ID = SOURCE_ID + "-" + EXTRACTOR_VERSION

# --- tenant constants (re-point here for another Legistar tenant) ---------
LEGISTAR_HOST = "https://legistar.council.nyc.gov"
CALENDAR_URL = LEGISTAR_HOST + "/Calendar.aspx"
DEFAULT_BODY = "New York City Council"

_DEFAULT_MAX = 10
_MAX_CANDIDATES = 240
_FETCH_BUDGET = 300

_FILE_NUMBER_RE = re.compile(r"^\d{2,4}-\d{4}(-S\d+)?$")

# Leading item file-number anchor: "Int 0353-2026", "Res 0529-2026",
# "L.U. 0123-2026", "T2026-1234", "M12-2026".
_FILE_ANCHOR_RE = re.compile(
    r'\b(?:(?:Int|Res|Introduction|Resolution|L\.?\s?U|SLR)\.?\s*'
    r'(?:No\.?\s*)?\d{2,4}-\d{4}[A-Za-z0-9\-]*'
    r'|T\d{4}-\d+|M\d{2,}-\d+)',
    re.I)

# NYC resolution titles begin with these verbs (or any -ing gerund).
_RES_VERB = frozenset((
    "calling", "approving", "recognizing", "declaring", "honoring",
    "commemorating", "celebrating", "condemning", "urging", "requesting",
    "supporting", "opposing", "designating", "amending", "establishing",
    "memorializing", "expressing", "congratulating", "mourning",
    "proclaiming", "reaffirming", "acknowledging", "denouncing",
    "encouraging", "dedicating", "affirming", "resolving", "demanding",
    "adopting", "authorizing", "requiring", "directing", "creating",
    "providing",
))

# Ends a legislative title (sponsors / attachments / motion / index tags).
_TITLE_END_RE = re.compile(
    r'(Sponsors?\s*:|Attachments?\s*:|Indexes?\s*:|Enactment\b|'
    r'A motion\b|This\s+[A-Z][A-Za-z]+\s+(?:was|be)\b|by the following|'
    r'Affirmative\b|In Favor\b|Negative\b|Coupled\b|'
    r'Report Required|Agency Rule-?making Required|'
    r'Committee Report|Fiscal Impact|Hearing Transcript|Hearing Testimony|'
    r'Memorandum in Support|Stated Meeting Agenda|Message of the Mayor|'
    r"Mayor'?s Message|Proposed Int|Proposed Res|Local Law and Resolution|"
    r'Preconsidered\b|Laid Over|Hearing Held|Referred to|Received,)',
    re.I)

_LABEL_POS = {
    "affirmative": "aye", "aye": "aye", "ayes": "aye",
    "negative": "no", "nay": "no", "nays": "no",
    "abstain": "abstain", "abstained": "abstain",
    "abstention": "abstain", "abstentions": "abstain",
    "absent": "absent", "excused": "absent",
    "recused": "recused", "recusal": "recused",
}
_LABEL_RE = re.compile(
    r'\b(Affirmative|Negative|Abstentions?|Abstained|Abstain|Absent|'
    r'Excused|Recused|Recusal|Ayes?|Nays?)\b')

_NUM_LEAD_RE = re.compile(r'[\s:\u2013\u2014\-]*(\d+)[\s:\u2013\u2014\-]*')

_BODY_RE = re.compile(
    r'Committee|Council|Subcommittee|Delegation|Task Force|Conference|'
    r'Commission')

_NAME_OK = re.compile(r"^[A-Za-z\u00C0-\u017F][A-Za-z\u00C0-\u017F .'\-]{0,38}$")

_MONTH = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5,
    "june": 6, "july": 7, "august": 8, "september": 9, "october": 10,
    "november": 11, "december": 12,
}
_PRINTED_DATE_RE = re.compile(
    r'(January|February|March|April|May|June|July|August|September|'
    r'October|November|December)\s+(\d{1,2}),\s*(\d{4})')

_TITLES = sorted(
    ["council members", "council member", "vice chair", "chairperson",
     "chairman", "chairwoman", "chair", "mr.", "ms.", "mrs.", "dr.",
     "hon.", "the honorable", "the speaker", "speaker",
     "the majority leader", "majority leader", "the minority leader",
     "minority leader", "the public advocate", "public advocate", "the"],
    key=len, reverse=True)


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


def _clean_name(name):
    s = str(name or "").strip()
    changed = True
    while changed:
        changed = False
        low = s.lower()
        for t in _TITLES:
            if low == t or low.startswith(t + " "):
                s = s[len(t):].strip()
                changed = True
                break
    return s.strip(" .,-\u2013\u2014")


def _clean_name_token(tok):
    t = str(tok or "").strip()
    t = re.split(r'[\r\n]', t, 1)[0]
    t = re.sub(r'\([^)]*\)', ' ', t)
    t = re.sub(r'^(?i:and)\s+', '', t.strip())
    t = _clean_name(t)
    t = re.sub(r'\s+', ' ', t).strip(' .,;:\u2013\u2014-')
    return t


def _valid_name(nm):
    if not nm or not _NAME_OK.match(nm):
        return False
    return len(re.findall(r'[A-Za-z\u00C0-\u017F]', nm)) >= 2


def _parse_date(raw):
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
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
    m = _PRINTED_DATE_RE.search(text[:700])
    if not m:
        return None
    mo = _MONTH.get(m.group(1).lower())
    if not mo:
        return None
    da = int(m.group(2))
    yr = int(m.group(3))
    if 1 <= da <= 31 and 1900 < yr < 2100:
        return "%04d-%02d-%02d" % (yr, mo, da)
    return None


def _slug(s):
    return re.sub(r"[^0-9A-Za-z]+", "-", str(s)).strip("-") or "x"


def _abs_url(u):
    u = str(u or "").strip().replace("&amp;", "&")
    if not u:
        return None
    if u.lower().startswith("http"):
        return u
    if u.startswith("/"):
        return LEGISTAR_HOST + u
    return LEGISTAR_HOST + "/" + u


def _norm_quote(q):
    return re.sub(r'\s+', ' ', str(q or "")).strip().lower()


def _anchor_key(s):
    low = s.lower()
    num = re.search(r'\d{2,4}-\d{4}(-s\d+)?', low)
    nums = num.group(0) if num else low.strip()
    if 'res' in low[:5]:
        t = 'res'
    elif 'int' in low[:6] or 'introduction' in low:
        t = 'int'
    elif 'l.u' in low or low.startswith('lu'):
        t = 'lu'
    elif 'slr' in low:
        t = 'slr'
    elif low.startswith('t'):
        t = 't'
    elif low.startswith('m'):
        t = 'm'
    else:
        t = 'x'
    return t + ':' + nums


def _fn_from_anchor(s):
    m = re.search(r'\d{2,4}-\d{4}(-S\d+)?', s)
    if m and _FILE_NUMBER_RE.match(m.group(0)):
        return m.group(0)
    return None


def _parse_tally(seg):
    """Parse 'N - Name, Name, ... and Name' -> (n, [names])."""
    m = _NUM_LEAD_RE.match(seg)
    if not m:
        return None, []
    n = int(m.group(1))
    if n == 0:
        return 0, []
    rest = seg[m.end():]
    raw = []
    for piece in rest.split(','):
        for sub in re.split(r'\s+and\s+', piece):
            sub = sub.strip()
            if re.search(r'[A-Za-z\u00C0-\u017F]', sub):
                raw.append(sub)
        if len(raw) >= n:
            break
    names = [_clean_name_token(t) for t in raw[:n]]
    return n, names


# ---------------------------------------------------------------------------
# Title extraction
# ---------------------------------------------------------------------------
def _cut_title(s):
    end = _TITLE_END_RE.search(s)
    cand = s[:end.start()] if end else s[:400]
    cand = re.sub(r'\s+', ' ', cand).strip(' *~.,;:()[]-\u2013\u2014')
    return cand[:240]


def _title_substantive(t):
    if not t or len(t) < 18:
        return False
    words = re.findall(r"[A-Za-z\u00C0-\u017F]{2,}", t)
    if len(words) < 4:
        return False
    low = re.sub(r'\s+', ' ', t).strip().lower()
    if low in ("report required", "agency rule-making required",
               "agency rulemaking required"):
        return False
    return True


def _find_legis_title(seg):
    """Return the official legislative title in this segment, or None.

    Never returns motion text ('this Resolution be Approved') or index
    tags — those are structurally excluded.
    """
    # Local Law: title always carries a leading capital 'A' ("A Local Law
    # to amend ..."); the motion text says "this Local Law be Approved".
    for m in re.finditer(r'\bA Local Law\b', seg):
        tail = seg[m.end():m.end() + 5].strip().lower()
        if tail.startswith('was') or tail.startswith('be'):
            continue
        cand = _cut_title(seg[m.start():])
        if _title_substantive(cand):
            return cand

    # Resolution: real verb after "Resolution", not preceded by this/that.
    for rm in re.finditer(r'\bResolution\s+([A-Za-z]+)', seg):
        pre = seg[max(0, rm.start() - 9):rm.start()].lower()
        if 'this' in pre or 'that' in pre:
            continue
        w = rm.group(1).lower()
        if w.endswith('ing') or w in _RES_VERB or w == 'to':
            cand = _cut_title(seg[rm.start():])
            if _title_substantive(cand):
                return cand

    # Land-use application.
    m = re.search(r'\bApplication\b.{0,25}(?:no\.?|number|submitted)',
                  seg, re.I)
    if m:
        cand = _cut_title(seg[m.start():])
        if _title_substantive(cand):
            return cand

    # Communication.
    m = re.search(r'\bCommunication\s+from\b', seg, re.I)
    if m:
        cand = _cut_title(seg[m.start():])
        if _title_substantive(cand):
            return cand

    return None


def _seg_action(seg, result):
    low = seg.lower()
    if 'adopted' in low:
        return "Adopted"
    if 'approved' in low:
        return "Approved"
    if 'be filed' in low or 'was filed' in low:
        return "Filed"
    if 'referred' in low:
        return "Referred"
    return "Adopted" if result == "pass" else "Rejected"


def _seg_vote(seg):
    """Return (positions, counts, result, aye_index) or None."""
    labels = []
    for m in _LABEL_RE.finditer(seg):
        p = _LABEL_POS.get(m.group(1).lower())
        if p:
            labels.append((m, p))
    if not labels:
        return None
    start_idx = next((i for i, (m, p) in enumerate(labels) if p == "aye"),
                     None)
    if start_idx is None:
        return None
    aye_m = labels[start_idx][0]
    pre = seg[max(0, aye_m.start() - 320):aye_m.start()].lower()
    if 'following vote' not in pre and 'motion' not in pre and \
            'by a vote' not in pre:
        return None

    counts = {}
    positions = []
    for j in range(start_idx, len(labels)):
        m, p = labels[j]
        if p == "aye" and j > start_idx:
            break
        s = m.end()
        e = labels[j + 1][0].start() if j + 1 < len(labels) else len(seg)
        n, names = _parse_tally(seg[s:min(e, s + 2500)])
        if n is None or n == 0:
            continue
        if len(names) != n:
            return None
        for nm in names:
            if not _valid_name(nm):
                return None
            positions.append({"member": nm, "position": p})
        counts[p] = counts.get(p, 0) + len(names)

    aye = counts.get("aye", 0)
    no = counts.get("no", 0)
    if aye <= 0 or not positions:
        return None
    result = "pass" if aye > no else "fail"
    return positions, counts, result, aye_m.start()


def _build_quote(seg, aye_rel):
    low = seg.lower()
    marker = -1
    for pat in ("a motion was made", "a motion", "by the following vote",
                "motion was made"):
        k = low.rfind(pat, 0, aye_rel)
        if k > marker:
            marker = k
    if marker == -1 or aye_rel - marker > 360:
        q_start = max(0, aye_rel - 70)
    else:
        q_start = marker
    q_end = min(len(seg), aye_rel + 220)
    if q_end - q_start > 400:
        q_end = q_start + 400
    return seg[q_start:q_end].strip()


def _segment_items(text):
    anchors = []
    for m in _FILE_ANCHOR_RE.finditer(text):
        pre = text[max(0, m.start() - 12):m.start()].lower()
        if 'proposed' in pre:
            continue
        anchors.append(m)
    segs = []
    for i, a in enumerate(anchors):
        end = anchors[i + 1].start() if i + 1 < len(anchors) else len(text)
        segs.append((a, text[a.start():end]))
    return segs


def _parse_minutes(text):
    """Return (attendance dict, [item dicts])."""
    segs = _segment_items(text)

    # doc-wide title map: file-number -> best legislative title
    title_map = {}
    for anchor, seg in segs:
        t = _find_legis_title(seg)
        if t:
            key = _anchor_key(anchor.group(0))
            if key not in title_map or len(t) > len(title_map[key]):
                title_map[key] = t

    attendance = {}
    items = []
    for anchor, seg in segs:
        vote = _seg_vote(seg)
        if not vote:
            continue
        positions, counts, result, aye_rel = vote

        # Resolve the SUBJECT: in-segment title first, else doc-wide map.
        title = _find_legis_title(seg)
        if not title:
            title = title_map.get(_anchor_key(anchor.group(0)))
        if not title or not _title_substantive(title):
            continue  # no real subject -> drop rather than emit a stub

        quote = _build_quote(seg, aye_rel)
        if not quote or len(quote) < 12:
            continue

        for p in positions:
            nm = p["member"]
            if p["position"] == "absent":
                attendance.setdefault(nm, "absent")
            else:
                attendance[nm] = "present"

        items.append({
            "title": title,
            "action": _seg_action(seg, result),
            "result": result,
            "positions": positions,
            "counts": counts,
            "file_number": _fn_from_anchor(anchor.group(0)),
            "quote": quote,
        })
    return attendance, items


def _provenance(source_url):
    return {
        "source_id": SOURCE_ID,
        "extractor_version": EXTRACTOR_VERSION,
        "run_id": RUN_ID,
        "source_url": source_url,
        "certification": {
            "certified": True,
            "method": "legistar-calendar+minutes",
        },
    }


def _body_from_frag(frag):
    for am in re.finditer(r'<a[^>]*>(.*?)</a>', frag, re.S | re.I):
        txt = re.sub(r'<[^>]+>', '', am.group(1))
        txt = txt.replace('&amp;', '&').replace('&nbsp;', ' ')
        txt = re.sub(r'\s+', ' ', txt).strip()
        if txt and _BODY_RE.search(txt) and len(txt) <= 90:
            return txt
    return DEFAULT_BODY


def _body_from_minutes(text):
    head = re.sub(r'\s+', ' ', text[:1000])
    m = re.search(
        r'(Committee on [A-Z][^,]{2,60}(?:,[^,]{2,40}){0,2}'
        r'|Subcommittee on [A-Z][A-Za-z ,]{2,60}'
        r'|Committee of the Whole'
        r'|[A-Z][a-z]+ Delegation of the New York City Council)', head)
    if m:
        b = re.sub(r'\s+', ' ', m.group(1)).strip(' ,.-')
        if 4 <= len(b) <= 90:
            return b
    return None


def _enumerate_calendar(rt):
    try:
        html = rt.fetch_text(CALENDAR_URL)
    except Exception:
        return []
    if not html:
        return []

    cands = []
    seen = set()

    for frag in re.split(r'(?i)<tr\b', html):
        if "meetingdetail.aspx" not in frag.lower():
            continue
        dm = re.search(r'(\d{1,2}/\d{1,2}/\d{4})', frag)
        if not dm:
            continue
        date = _parse_date(dm.group(1))
        if not date:
            continue
        det = re.search(r'href="([^"]*MeetingDetail\.aspx[^"]*)"', frag, re.I)
        if not det:
            continue
        detail_url = _abs_url(det.group(1))
        if not detail_url or detail_url in seen:
            continue
        mm = re.search(r'href="([^"]*View\.ashx\?[^"]*M=M[^"]*)"', frag, re.I)
        minutes_url = _abs_url(mm.group(1)) if mm else None
        seen.add(detail_url)
        cands.append({"date": date, "detail_url": detail_url,
                      "minutes_url": minutes_url,
                      "body": _body_from_frag(frag)})

    if not cands:
        for m in re.finditer(r'href="([^"]*MeetingDetail\.aspx[^"]*)"',
                             html, re.I):
            detail_url = _abs_url(m.group(1))
            if not detail_url or detail_url in seen:
                continue
            back = html[max(0, m.start() - 2500):m.start()]
            dts = re.findall(r'(\d{1,2}/\d{1,2}/\d{4})', back)
            if not dts:
                continue
            date = _parse_date(dts[-1])
            if not date:
                continue
            fwd = html[m.start():m.start() + 2500]
            mm = re.search(r'href="([^"]*View\.ashx\?[^"]*M=M[^"]*)"',
                           fwd, re.I)
            minutes_url = _abs_url(mm.group(1)) if mm else None
            seen.add(detail_url)
            cands.append({"date": date, "detail_url": detail_url,
                          "minutes_url": minutes_url,
                          "body": _body_from_frag(back[-1500:])})

    return cands


def _minutes_from_detail(rt, detail_url):
    if not detail_url:
        return None
    try:
        html = rt.fetch_text(detail_url)
    except Exception:
        return None
    if not html:
        return None
    m = re.search(
        r'href="([^"]*View\.ashx[^"]*)"[^>]*>[^<]*Minutes[^<]*<', html, re.I)
    if not m:
        m = re.search(r'href="([^"]*View\.ashx\?[^"]*M=M[^"]*)"', html, re.I)
    return _abs_url(m.group(1)) if m else None


def extract(rt, args):
    want = _coerce_max(args)
    today_iso = datetime.date.today().isoformat()

    cands = _enumerate_calendar(rt)
    past = [c for c in cands if c["date"] and c["date"] < today_iso]
    past.sort(key=lambda c: c["date"], reverse=True)

    meetings_rec, items_rec, votes_rec = [], [], []
    members = {}
    used_ids = set()
    seen_quotes = set()          # dedupe coupled/reprinted roll calls
    seen_docs = set()            # never two meetings from identical bytes
    emitted = 0
    examined = 0
    fetches = 0

    for cand in past:
        if emitted >= want or examined >= _MAX_CANDIDATES:
            break
        if fetches >= _FETCH_BUDGET:
            break
        examined += 1
        try:
            detail_url = cand["detail_url"]
            murl = cand["minutes_url"]

            if not murl and fetches < _FETCH_BUDGET:
                fetches += 1
                murl = _minutes_from_detail(rt, detail_url)
            if not murl:
                continue

            if fetches >= _FETCH_BUDGET:
                break
            fetches += 1
            try:
                text = rt.fetch_text(murl)
            except Exception:
                continue
            if not text or len(text) < 150:
                continue

            sig = hashlib.md5(text.encode("utf-8", "ignore")).hexdigest()
            if sig in seen_docs:
                continue
            seen_docs.add(sig)

            # authoritative date = the date PRINTED inside the document
            date = _printed_date(text) or cand["date"]
            if not date or date >= today_iso:
                continue

            attendance, parsed = _parse_minutes(text)
            if not parsed:
                continue

            body = (_body_from_minutes(text) or cand.get("body")
                    or DEFAULT_BODY)
            if not isinstance(body, str) or not body.strip():
                body = DEFAULT_BODY
            body = body.strip()

            source_url = detail_url or CALENDAR_URL
            if ".ashx" in source_url.lower():
                source_url = CALENDAR_URL
            prov = _provenance(source_url)

            meeting_id = "%s-%s" % (SOURCE_ID, date)
            if meeting_id in used_ids:
                base = "%s-%s" % (meeting_id, _slug(body)[:40])
                meeting_id = base
                n = 2
                while meeting_id in used_ids:
                    meeting_id = "%s-%d" % (base, n)
                    n += 1

            vi = 0
            local_items, local_votes = [], []
            for it in parsed:
                vi += 1
                item_id = "%s-item-%d" % (meeting_id, vi)

                for p in it["positions"]:
                    nm = p["member"]
                    if nm not in members:
                        members[nm] = {"name": nm, "provenance": prov}

                local_items.append({
                    "item_id": item_id,
                    "meeting_id": meeting_id,
                    "title": it["title"],
                    "action": it["action"],
                    "result": it["result"],
                    "file_number": it["file_number"],
                    "provenance": prov,
                })

                key = _norm_quote(it["quote"])
                if key in seen_quotes:
                    continue      # same roll call reprinted under another bill
                seen_quotes.add(key)
                local_votes.append({
                    "vote_id": "%s-vote-%d" % (meeting_id, vi),
                    "meeting_id": meeting_id,
                    "item_id": item_id,
                    "positions": it["positions"],
                    "counts": it["counts"],
                    "result": it["result"],
                    "file_number": it["file_number"],
                    "evidence": {"quote": it["quote"], "doc_url": murl},
                    "provenance": prov,
                })

            if not local_items:
                continue

            att = {nm: st for nm, st in attendance.items() if _valid_name(nm)}
            if not att:
                for it in parsed:
                    for p in it["positions"]:
                        if p["position"] != "absent":
                            att[p["member"]] = "present"
            if not att:
                continue

            used_ids.add(meeting_id)
            meetings_rec.append({
                "meeting_id": meeting_id,
                "body": body,
                "date": date,
                "attendance": att,
                "source_url": source_url,
                "data_source_url": murl,
                "file_number": None,
                "provenance": prov,
            })
            for nm in att:
                if nm not in members:
                    members[nm] = {"name": nm, "provenance": prov}
            items_rec.extend(local_items)
            votes_rec.extend(local_votes)
            emitted += 1
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
        "source_id": SOURCE_ID,
        "extractor_version": EXTRACTOR_VERSION,
        "schema_version": "1.3",
        "row_counts": {
            "meetings": len(meetings_rec),
            "agenda_items": len(items_rec),
            "vote_events": len(votes_rec),
            "members": len(members_rec),
        },
    }
    return records, run_meta
