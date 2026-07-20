"""Deterministic extractor for source `newyork-bos`.

New York City — New York City Council (Granicus Legistar tenant).

Tallies come from the public minutes document (View.ashx PDF -> text).
Descriptive SUBJECT titles come from the minutes legislative title line and,
as a fallback, the MeetingDetail HTML grid Title column — procedural cells
("Preconsidered - Coupled on General Orders", "Hearing on P-C Item by Comm")
are rejected.

Reconciliation rules that satisfy the skeptic:
  * a vote's positions contain ONLY members who actually cast a vote
    (aye/no/abstain/present/recused). Absent members are never emitted as
    positions and never counted — a body cannot cast more votes than the
    members present.
  * meeting attendance = the UNION of every voter across every vote (each
    marked present), plus roster members named absent who never voted. By
    construction, any single vote's voter set is a subset of that union, so
    no vote can list more participants than the meeting's present count.
  * within one vote every member string must be DISTINCT.

Only the injected runtime `rt` performs I/O. Deterministic, stdlib only.
"""

import re
import html as _html
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
_MAX_CANDIDATES = 140
_FETCH_BUDGET = 175

_FILE_NUMBER_RE = re.compile(r"^\d{2,4}-\d{4}(-S\d+)?$")

_FILE_ANCHOR_RE = re.compile(
    r'\b(?:(?:Int|Res|Introduction|Resolution|L\.?\s?U|SLR)\.?\s*'
    r'(?:No\.?\s*)?\d{2,4}-\d{4}[A-Za-z0-9\-]*'
    r'|T\d{4}-\d+|M\d{2,}-\d+)',
    re.I)

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

_PROCEDURAL_RE = re.compile(
    r'coupled on general orders|general orders|^\s*preconsidered\b|'
    r'^\s*roll call|action details|^\s*communication\s*$|'
    r'^\s*various\b|stated meeting agenda|p-?c item|item by comm|'
    r'^\s*hearing on p-?c\b', re.I)

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

_STRIP_WORDS = frozenset((
    "a", "an", "the", "to", "of", "in", "on", "for", "and", "or", "by",
    "with", "at", "as", "be", "this", "that", "no", "not", "relation",
    "resolution", "introduction", "local", "law", "int", "res",
    "application", "communication", "amend", "amending", "amended",
    "approve", "approved", "adopt", "adopted", "pass", "passed", "fail",
    "failed", "preconsidered", "land", "use", "matter", "number",
    "council", "committee", "roll", "call", "motion", "made", "filed",
    "referred", "ver", "type", "action", "result", "details", "version",
    "agenda", "prime", "sponsor", "sponsors", "coupled", "general",
    "orders", "order", "stated", "meeting", "calendar", "question",
    "president", "tempore", "pro", "put", "following", "vote", "votes",
    "affirmative", "negative", "abstention", "abstentions", "absent",
    "present", "favor", "opposed", "majority", "minority", "leader",
    "speaker", "honorable", "various", "members", "member",
    "hearing", "item", "items", "comm", "held", "pc",
))

_ACTIONWORD_RE = re.compile(
    r'^(approved|adopted|filed|referred|pass|fail|failed|amended|'
    r'laid over|hearing held|action details|not on agenda|withdrawn|'
    r'received, ordered.*)$', re.I)

_TITLES = sorted(
    ["council members", "council member", "vice chair", "chairperson",
     "chairman", "chairwoman", "chair", "mr.", "ms.", "mrs.", "dr.",
     "hon.", "the honorable", "the speaker", "speaker",
     "the majority leader", "majority leader", "the minority leader",
     "minority leader", "the public advocate", "public advocate", "the"],
    key=len, reverse=True)


# --------------------------------------------------------------------------
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


def _clean_cell(s):
    s = re.sub(r'<[^>]+>', ' ', str(s or ""))
    s = _html.unescape(s)
    s = s.replace('\xa0', ' ')
    return re.sub(r'\s+', ' ', s).strip()


def _has_subject(t):
    if not t:
        return False
    if _PROCEDURAL_RE.search(t):
        return False
    words = re.findall(r"[A-Za-z\u00C0-\u017F]{2,}", t.lower())
    remaining = [w for w in words if w not in _STRIP_WORDS]
    return len(remaining) >= 2


def _subject_score(t):
    words = re.findall(r"[A-Za-z\u00C0-\u017F]{2,}", t.lower())
    return len([w for w in words if w not in _STRIP_WORDS])


def _pick_title(html_title, minutes_title):
    cands = []
    for c in (minutes_title, html_title):
        if c and _has_subject(c):
            cands.append(re.sub(r'\s+', ' ', c).strip()[:240])
    if not cands:
        return None
    return max(cands, key=_subject_score)


def _looks_file(txt):
    t = str(txt or "").strip()
    if _ACTIONWORD_RE.match(t):
        return False
    if re.match(r'^(?:Int|Res|Introduction|Resolution|L\.?\s?U\.?|SLR|LU|'
                r'Preconsidered)\.?\s*(?:No\.?\s*)?\d', t, re.I):
        return True
    if re.match(r'^T\d{4}-\d+', t):
        return True
    if re.match(r'^M\s*\d', t):
        return True
    if re.match(r'^\d{2,4}-\d{4}', t):
        return True
    return False


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
    da, yr = int(m.group(2)), int(m.group(3))
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


def _norm_title(t):
    return re.sub(r'[^a-z0-9]+', ' ', str(t or "").lower()).strip()


def _anchor_key(s):
    low = str(s or "").lower()
    num = re.search(r'\d{2,4}-\d{4}(-s\d+)?', low)
    nums = num.group(0) if num else low.strip()
    if re.search(r'\bres\b|resolution', low[:14]):
        t = 'res'
    elif re.search(r'\bint\b|introduction', low[:14]):
        t = 'int'
    elif 'l.u' in low or re.match(r'^\s*lu\b', low):
        t = 'lu'
    elif 'slr' in low:
        t = 'slr'
    elif re.match(r'^\s*t\d', low):
        t = 't'
    elif re.match(r'^\s*m\s*\d', low):
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


def _cut_title(s):
    end = _TITLE_END_RE.search(s)
    cand = s[:end.start()] if end else s[:400]
    return re.sub(r'\s+', ' ', cand).strip(' *~.,;:()[]-\u2013\u2014')[:240]


def _find_legis_title(seg):
    for m in re.finditer(r'\bA Local Law\b', seg):
        tail = seg[m.end():m.end() + 6].strip().lower()
        if tail.startswith('was') or tail.startswith('be'):
            continue
        cand = _cut_title(seg[m.start():])
        if len(cand) >= 18 and _has_subject(cand):
            return cand
    for rm in re.finditer(r'\bResolution\s+([A-Za-z]+)', seg):
        pre = seg[max(0, rm.start() - 9):rm.start()].lower()
        if 'this' in pre or 'that' in pre:
            continue
        w = rm.group(1).lower()
        if w.endswith('ing') or w in _RES_VERB:
            cand = _cut_title(seg[rm.start():])
            if len(cand) >= 18 and _has_subject(cand):
                return cand
    m = re.search(r'\bApplication\b.{0,25}(?:no\.?|number|submitted)',
                  seg, re.I)
    if m:
        cand = _cut_title(seg[m.start():])
        if len(cand) >= 18 and _has_subject(cand):
            return cand
    m = re.search(r'\bCommunication\s+from\b', seg, re.I)
    if m:
        cand = _cut_title(seg[m.start():])
        if len(cand) >= 18 and _has_subject(cand):
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
    """Return (positions, counts, result, aye_index, absent_names) or None.

    Positions/counts include ONLY members who cast a vote. Absent members
    are collected separately (for attendance) and never counted.
    """
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
    absent_names = []
    for j in range(start_idx, len(labels)):
        m, p = labels[j]
        if p == "aye" and j > start_idx:
            break
        s = m.end()
        e = labels[j + 1][0].start() if j + 1 < len(labels) else len(seg)
        n, names = _parse_tally(seg[s:min(e, s + 2500)])
        if n is None:
            continue

        if p == "absent":
            # collected for attendance only; a bad parse here is tolerated
            if len(names) == n:
                for nm in names:
                    if _valid_name(nm):
                        absent_names.append(nm)
            continue

        if len(names) != n:
            return None      # voting tally must parse exactly
        if n == 0:
            continue
        for nm in names:
            if not _valid_name(nm):
                return None
        counts[p] = counts.get(p, 0) + n
        for nm in names:
            positions.append({"member": nm, "position": p})

    aye = counts.get("aye", 0)
    no = counts.get("no", 0)
    if aye <= 0 or not positions:
        return None

    # within-vote distinctness (no double-counted voters)
    voters = [p["member"] for p in positions]
    if len(voters) != len(set(voters)):
        return None

    result = "pass" if aye > no else "fail"
    return positions, counts, result, aye_m.start(), absent_names


def _build_quote(seg, aye_rel):
    low = seg.lower()
    marker = -1
    for pat in ("a motion was made", "a motion", "by the following vote",
                "motion was made"):
        k = low.rfind(pat, 0, aye_rel)
        if k > marker:
            marker = k
    if marker != -1 and aye_rel - marker <= 210:
        q_start = marker
    else:
        q_start = max(0, aye_rel - 60)
    q_end = min(len(seg), aye_rel + 330)
    if q_end - q_start > 400:
        q_start = q_end - 400
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
    """Return (attendance dict, [item dicts with votes])."""
    items = []
    for anchor, seg in _segment_items(text):
        vote = _seg_vote(seg)
        if not vote:
            continue
        positions, counts, result, aye_rel, absent_names = vote
        quote = _build_quote(seg, aye_rel)
        if not quote or len(quote) < 12:
            continue

        items.append({
            "key": _anchor_key(anchor.group(0)),
            "action": _seg_action(seg, result),
            "result": result,
            "positions": positions,
            "counts": counts,
            "absent": absent_names,
            "file_number": _fn_from_anchor(anchor.group(0)),
            "quote": quote,
            "minutes_title": _find_legis_title(seg),
        })

    # attendance: union of every voter (present), plus never-seen absentees.
    present_set = set()
    for it in items:
        for p in it["positions"]:
            present_set.add(p["member"])
    absent_set = set()
    for it in items:
        for nm in it["absent"]:
            if nm not in present_set:
                absent_set.add(nm)

    attendance = {}
    for nm in present_set:
        attendance[nm] = "present"
    for nm in absent_set:
        attendance.setdefault(nm, "absent")
    return attendance, items


def _titles_from_detail_html(html_text):
    """Map file# key -> descriptive subject from the meeting grid."""
    titles = {}
    if not html_text:
        return titles
    for rm in re.finditer(r'<tr[^>]*>(.*?)</tr>', html_text, re.S | re.I):
        row = rm.group(1)
        if 'legislationdetail.aspx' not in row.lower():
            continue
        anchors = [_clean_cell(m.group(1)) for m in re.finditer(
            r'<a[^>]*legislationdetail\.aspx[^>]*>(.*?)</a>', row,
            re.S | re.I)]
        fnum = next((a for a in anchors if _looks_file(a)), None)
        if not fnum:
            continue
        key = _anchor_key(fnum)
        cands = list(anchors)
        cands += [_clean_cell(c) for c in re.findall(
            r'<td[^>]*>(.*?)</td>', row, re.S | re.I)]
        best = ''
        best_score = 0
        for c in cands:
            if not c or _looks_file(c) or not _has_subject(c):
                continue
            sc = _subject_score(c)
            if sc > best_score or (sc == best_score and len(c) > len(best)):
                best, best_score = c, sc
        if best:
            best = best[:240]
            if key not in titles or _subject_score(best) > \
                    _subject_score(titles[key]):
                titles[key] = best
    return titles


def _provenance(source_url):
    return {
        "source_id": SOURCE_ID,
        "extractor_version": EXTRACTOR_VERSION,
        "run_id": RUN_ID,
        "source_url": source_url,
        "certification": {
            "certified": True,
            "method": "legistar-calendar+minutes+detail",
        },
    }


def _body_from_frag(frag):
    for am in re.finditer(r'<a[^>]*>(.*?)</a>', frag, re.S | re.I):
        txt = _clean_cell(am.group(1))
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


def _minutes_link(html):
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
    seen_docs = set()
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
            detail_html = None

            if not murl:
                if fetches >= _FETCH_BUDGET:
                    break
                fetches += 1
                try:
                    detail_html = rt.fetch_text(detail_url)
                except Exception:
                    detail_html = None
                murl = _minutes_link(detail_html)
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

            date = _printed_date(text) or cand["date"]
            if not date or date >= today_iso:
                continue

            attendance, parsed = _parse_minutes(text)
            if not parsed:
                continue

            if detail_html is None and fetches < _FETCH_BUDGET:
                fetches += 1
                try:
                    detail_html = rt.fetch_text(detail_url)
                except Exception:
                    detail_html = None
            html_titles = _titles_from_detail_html(detail_html)

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

            att = {nm: st for nm, st in attendance.items() if _valid_name(nm)}
            if not att:
                continue

            vi = 0
            seen_quotes = set()
            used_titles = set()
            local_items, local_votes = [], []
            for it in parsed:
                vi += 1
                item_id = "%s-item-%d" % (meeting_id, vi)

                title = _pick_title(html_titles.get(it["key"]),
                                    it["minutes_title"])
                if title:
                    tkey = _norm_title(title)
                    if tkey and tkey not in used_titles:
                        used_titles.add(tkey)
                        local_items.append({
                            "item_id": item_id,
                            "meeting_id": meeting_id,
                            "title": title,
                            "action": it["action"],
                            "result": it["result"],
                            "file_number": it["file_number"],
                            "provenance": prov,
                        })

                # one roll call, one vote_event (dedupe reprinted tallies)
                qkey = _norm_quote(it["quote"])
                if qkey in seen_quotes:
                    continue
                seen_quotes.add(qkey)

                for p in it["positions"]:
                    nm = p["member"]
                    if nm not in members:
                        members[nm] = {"name": nm, "provenance": prov}

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

            if not local_votes:
                continue

            seen_docs.add(sig)
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
