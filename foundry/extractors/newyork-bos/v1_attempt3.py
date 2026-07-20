"""Deterministic extractor for source `newyork-bos`.

New York City — New York City Council (Granicus Legistar tenant).

Enumeration MUST come from the live Legistar calendar, NOT the NYC Open Data
Socrata dataset (m48u-yjt8) which is frozen at December 2024 and produces
stale results. The public HTML calendar
(https://legistar.council.nyc.gov/Calendar.aspx) lists current-session
meetings with MeetingDetail links, meeting dates, and — for concluded
meetings — a "Minutes" View.ashx link.

Pipeline:
  1. Fetch Calendar.aspx; parse rows -> (date, MeetingDetail page URL,
     optional minutes View.ashx URL, body name).
  2. Keep meetings strictly in the past that have a minutes document
     (concluded meetings), newest first.
  3. Fetch each minutes document as text (runtime converts PDF -> text) and
     parse the roll-call tally lines ("Affirmative: 46 - Abreu, Ariola, ...;
     Negative: 2 - Holden, ...") into per-member positions, deriving
     attendance from the same rosters and attaching the verbatim passage as
     `evidence`.

Only the injected runtime `rt` performs I/O. Deterministic, stdlib only.
"""

import re
import datetime

EXTRACTOR_VERSION = "1"

SOURCE_ID = "newyork-bos"
RUN_ID = SOURCE_ID + "-" + EXTRACTOR_VERSION

# --- tenant constants (re-point here for another Legistar tenant) ---------
LEGISTAR_HOST = "https://legistar.council.nyc.gov"
CALENDAR_URL = LEGISTAR_HOST + "/Calendar.aspx"
DEFAULT_BODY = "New York City Council"

_DEFAULT_MAX = 10
_MAX_CANDIDATES = 70
_FETCH_BUDGET = 180

# roll-call tally labels -> schema position vocabulary
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
    r'Excused|Recused|Recusal|Ayes?|Nays?)\b', re.I)

_SEP_AFTER = re.compile(r'\s*(?:[:\u2013\u2014-]|\d)')

_NAME_SUFFIX = {"jr", "sr", "ii", "iii", "iv", "esq"}

_STOP_WORDS = {
    "none", "the", "and", "council", "members", "member", "majority",
    "minority", "leader", "whip", "speaker", "public", "advocate",
    "president", "pro", "tempore", "acting", "of", "by", "chair", "was",
    "this", "these", "motion", "committee", "report", "introduction",
    "resolution", "communication", "vote", "roll", "call", "approved",
    "adopted", "referred", "hearing", "held", "laid", "over", "filed",
}

_STOP_TITLE = re.compile(
    r'\b(?:This|These|A motion|By\b|Int\.?|Res\.?|L\.?U\.?|Attachments|'
    r'Sponsors|Enactment|Committee|Report|The following|Laid Over|'
    r'whereupon|Roll Call|was\b)', re.I)

_TITLE_PATS = [
    re.compile(r'Report of the Committee on [^\.\n;:]{3,90}'),
    re.compile(
        r'(?:Preconsidered\s+)?'
        r'(?:Int|Res|Introduction|Resolution|L\.?\s?U\.?|M|T)\.?\s*'
        r'(?:No\.?\s*)?\d[\w\-/]*'),
]

_BODY_RE = re.compile(
    r'Committee|Council|Subcommittee|Delegation|Task Force|Conference|'
    r'Commission')

_TITLES = sorted(
    ["council members", "council member", "vice chair", "chairperson",
     "chairman", "chairwoman", "chair", "supervisor", "mr.", "ms.", "mrs.",
     "dr.", "mayor", "hon.", "the honorable", "the speaker",
     "the majority leader", "the minority leader", "the public advocate",
     "the majority whip", "the minority whip"],
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
            if low.startswith(t + " "):
                s = s[len(t):].strip()
                changed = True
                break
    return s.strip(" .,-\u2013\u2014")


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


def _valid_name(n):
    if not n:
        return False
    if re.search(r"\d", n):
        return False
    if not re.search(r"[A-Za-z\u00C0-\u017F]", n):
        return False
    if len(n) > 45:
        return False
    words = [w for w in re.split(r"[\s]+", n.lower()) if w]
    if not words or len(words) > 5:
        return False
    if all(w.strip(".") in _STOP_WORDS for w in words):
        return False
    return True


def _member_from_token(tok):
    tok = str(tok or "").strip().strip(".,").strip()
    if not tok:
        return None
    m = re.search(r"\(([^)]*)\)", tok)
    if m:
        inner = m.group(1)
        inner = re.sub(r"(?i)\bcouncil members?\b", "", inner)
        inner = re.sub(r"(?i)\b(mr|ms|mrs|dr|hon)\.?\b", "", inner)
        inner = inner.strip(" .,")
        return _clean_name(inner) if inner else None
    return _clean_name(tok)


def _clean_piece(piece):
    """Return (status, name): status in {'name','skip','stop'}."""
    p = piece.strip()
    if not p:
        return ("skip", None)
    cut = _STOP_TITLE.split(p)[0]
    cut = re.sub(r'[:\u2013\u2014-]\s*\d+\s*$', '', cut)
    cut = re.sub(r'^\s*\d+\s*[:\u2013\u2014-]*\s*', '', cut)
    cut = cut.strip()
    if not cut:
        return ("skip", None)
    low = cut.lower().strip(".")
    if low in _NAME_SUFFIX:
        return ("skip", None)
    nm = _member_from_token(cut)
    if nm and _valid_name(nm):
        return ("name", nm)
    return ("stop", None)


def _parse_seg(seg):
    count = None
    cm = re.match(r'\s*(\d+)', seg)
    if cm:
        count = int(cm.group(1))
    names = []
    for piece in seg.split(","):
        status, nm = _clean_piece(piece)
        if status == "stop":
            break
        if status == "name":
            names.append(nm)
    return count, names


def _parse_minutes(text):
    """Return (attendance dict, vote-block list)."""
    matches = []
    for m in _LABEL_RE.finditer(text):
        tail = text[m.end():m.end() + 4]
        if not _SEP_AFTER.match(tail):
            continue
        matches.append(m)

    entries = []
    for i, m in enumerate(matches):
        label = m.group(1).lower()
        pos = _LABEL_POS.get(label)
        if not pos:
            continue
        seg_start = m.end()
        nxt = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        seg_end = min(nxt, seg_start + 1200)
        seg = text[seg_start:seg_end]
        seg = re.sub(r'^\s*[:\u2013\u2014-]\s*', '', seg)
        count, names = _parse_seg(seg)
        entries.append({"pos": pos, "names": names,
                        "start": m.start(), "count": count})

    present, absent = set(), set()
    for e in entries:
        for nm in e["names"]:
            if e["pos"] == "absent":
                absent.add(nm)
            else:
                present.add(nm)
    attendance = {}
    for nm in present:
        attendance[nm] = "present"
    for nm in absent:
        if nm not in attendance:
            attendance[nm] = "absent"

    blocks = []
    cur = None
    prev_start = None
    for e in entries:
        if e["pos"] == "aye":
            if cur:
                blocks.append(cur)
            cur = {"entries": [e], "start": e["start"]}
            prev_start = e["start"]
        else:
            if cur is not None and prev_start is not None and \
                    (e["start"] - prev_start) < 1500:
                cur["entries"].append(e)
                prev_start = e["start"]
    if cur:
        blocks.append(cur)

    return attendance, blocks[:300]


def _extract_title(preceding):
    best = None
    for pat in _TITLE_PATS:
        for mm in pat.finditer(preceding):
            best = mm
    if best:
        tail = preceding[best.start():]
        cut = re.split(
            r'(?:A motion|This \w+ was|By Council|Attachments:|approved by)',
            tail)[0]
        t = re.sub(r'\s+', ' ', cut).strip(' .,;:-')
        if t:
            return t[:120]
    return "General Order Calendar"


def _provenance(source_url):
    return {
        "source_id": SOURCE_ID,
        "extractor_version": EXTRACTOR_VERSION,
        "run_id": RUN_ID,
        "source_url": source_url,
        "certification": {
            "certified": True,
            "method": "legistar-calendar+minutes-pdf",
        },
    }


def _body_from_row(frag):
    for am in re.finditer(r'<a[^>]*>(.*?)</a>', frag, re.S | re.I):
        txt = re.sub(r'<[^>]+>', '', am.group(1))
        txt = re.sub(r'&amp;', '&', txt)
        txt = re.sub(r'&nbsp;', ' ', txt)
        txt = re.sub(r'\s+', ' ', txt).strip()
        if txt and _BODY_RE.search(txt) and len(txt) <= 90:
            return txt
    return DEFAULT_BODY


def _enumerate_calendar(rt):
    try:
        html = rt.fetch_text(CALENDAR_URL)
    except Exception:
        return []
    if not html:
        return []

    cands = []
    seen = set()

    # primary: row-based parse
    for frag in re.split(r'(?i)<tr\b', html):
        low = frag.lower()
        if "meetingdetail.aspx" not in low:
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
                      "body": _body_from_row(frag)})

    # fallback: global scan pairing each detail link with nearest date
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
                          "body": _body_from_row(back[-1500:])})

    return cands


def _minutes_from_html(rt, insite):
    if not insite:
        return None
    try:
        html = rt.fetch_text(insite)
    except Exception:
        return None
    if not html:
        return None
    m = re.search(
        r'href="([^"]*View\.ashx[^"]*)"[^>]*>[^<]*Minutes[^<]*<',
        html, re.I)
    if not m:
        m = re.search(r'href="([^"]*View\.ashx\?[^"]*M=M[^"]*)"', html, re.I)
    if m:
        return _abs_url(m.group(1))
    return None


def extract(rt, args):
    want = _coerce_max(args)
    today_iso = datetime.date.today().isoformat()

    cands = _enumerate_calendar(rt)

    # keep strictly-past meetings, newest first
    past = [c for c in cands if c["date"] and c["date"] < today_iso]
    past.sort(key=lambda c: c["date"], reverse=True)

    meetings_rec, items_rec, votes_rec = [], [], []
    members = {}
    used_ids = set()
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
            date = cand["date"]
            detail_url = cand["detail_url"]
            murl = cand["minutes_url"]

            if not murl and detail_url and fetches < _FETCH_BUDGET:
                fetches += 1
                murl = _minutes_from_html(rt, detail_url)
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

            attendance, blocks = _parse_minutes(text)
            if not attendance or not blocks:
                continue

            body = cand.get("body") or DEFAULT_BODY
            if not isinstance(body, str) or not body.strip():
                body = DEFAULT_BODY
            body = body.strip()

            source_url = detail_url or CALENDAR_URL
            if ".ashx" in source_url.lower():
                source_url = CALENDAR_URL
            prov = _provenance(source_url)

            meeting_id = "%s-%s" % (SOURCE_ID, date)
            if meeting_id in used_ids:
                base = "%s-%s" % (meeting_id, _slug(body))
                meeting_id = base
                n = 2
                while meeting_id in used_ids:
                    meeting_id = "%s-%d" % (base, n)
                    n += 1
            used_ids.add(meeting_id)

            vi = 0
            local_items, local_votes = [], []
            meeting_has_vote = False
            for blk in blocks:
                positions = []
                counts = {}
                bseen = set()
                for e in blk["entries"]:
                    pos = e["pos"]
                    for nm in e["names"]:
                        if nm in bseen:
                            continue
                        bseen.add(nm)
                        positions.append({"member": nm, "position": pos})
                        counts[pos] = counts.get(pos, 0) + 1
                        if nm not in members:
                            members[nm] = {"name": nm, "provenance": prov}
                aye = counts.get("aye", 0)
                no = counts.get("no", 0)
                if aye == 0 or not positions:
                    continue
                result = "pass" if aye > no else "fail"

                bstart = blk["start"]
                quote = text[bstart:bstart + 400]
                if not quote.strip():
                    continue

                preceding = text[max(0, bstart - 1000):bstart]
                title = _extract_title(preceding)
                action = "Adopted" if result == "pass" else "Rejected"

                vi += 1
                item_id = "%s-item-%d" % (meeting_id, vi)
                vote_id = "%s-vote-%d" % (meeting_id, vi)

                local_items.append({
                    "item_id": item_id,
                    "meeting_id": meeting_id,
                    "title": title,
                    "action": action,
                    "result": result,
                    "file_number": None,
                    "provenance": prov,
                })
                local_votes.append({
                    "vote_id": vote_id,
                    "meeting_id": meeting_id,
                    "item_id": item_id,
                    "positions": positions,
                    "counts": counts,
                    "result": result,
                    "file_number": None,
                    "evidence": {"quote": quote, "doc_url": murl},
                    "provenance": prov,
                })
                meeting_has_vote = True

            if not meeting_has_vote:
                continue

            meetings_rec.append({
                "meeting_id": meeting_id,
                "body": body,
                "date": date,
                "attendance": dict(attendance),
                "source_url": source_url,
                "data_source_url": murl,
                "file_number": None,
                "provenance": prov,
            })
            for nm in attendance:
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
