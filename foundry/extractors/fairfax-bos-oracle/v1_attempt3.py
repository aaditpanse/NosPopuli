"""Independent second-source assertion extractor for fairfax-bos.

Second source: the county-hosted Granicus video platform
(video.fairfaxcounty.gov). We enumerate archived Board of Supervisors
meetings from ViewPublisher.php (each archived meeting row links to a
MediaPlayer.php?clip_id=NNNN video/agenda), then read the meeting's
structured HTML minutes (MinutesViewer.php). Those Granicus minutes list
numbered agenda items each with an action/disposition status ('Done',
'Adopted', 'Approved', ...) produced independently of the Clerk's
vote-narrative PDF on the primary CMS.

We NEVER touch the primary CMS data pages
(www.fairfaxcounty.gov/boardofsupervisors/...). Item assertions are emitted
only where the fetched Granicus document explicitly records a disposition.
"""

import html
import re

EXTRACTOR_VERSION = "1"

_HOST = "https://video.fairfaxcounty.gov"
_VP_URL = _HOST + "/ViewPublisher.php"
_MP_URL = _HOST + "/MediaPlayer.php"
_MV_URL = _HOST + "/MinutesViewer.php"
_VIEW_ID = "7"

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

_TITLE_RE = re.compile(
    r"((?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|"
    r"Dec(?:ember)?)\.?)[\s\xa0]+(\d{1,2}),?[\s\xa0]+(\d{4})",
    re.IGNORECASE,
)
_CLIP_RE = re.compile(r"clip_id=(\d+)")
_DOCID_RE = re.compile(r"doc_id=([0-9a-fA-F\-]{8,})")

_ITEM_RE = re.compile(r"^\s*(\d+(?:\.[A-Za-z0-9]+)*)[.)]\s*(.*)$")

_PASS_TOKENS = ["Done", "Adopted", "Approved", "Passed", "Carried",
                "Concurred", "Granted", "Authorized"]
_FAIL_TOKENS = ["Failed", "Denied", "Rejected", "Defeated"]
_PASS_RE = re.compile(r"\b(" + "|".join(_PASS_TOKENS) + r")\b")
_FAIL_RE = re.compile(r"\b(" + "|".join(_FAIL_TOKENS) + r")\b")
_UNAN_RE = re.compile(r"\bunanimous(?:ly)?\b", re.IGNORECASE)

_AYE_RE = re.compile(r"\bayes?\b[:\s]*?(\d+)", re.IGNORECASE)
_NAY_RE = re.compile(r"\b(?:nays?|noes?)\b[:\s]*?(\d+)", re.IGNORECASE)
_ABS_RE = re.compile(r"\babstain(?:ed|ing|s)?\b[:\s]*?(\d+)", re.IGNORECASE)
_ABSENT_RE = re.compile(r"\babsent\b[:\s]*?(\d+)", re.IGNORECASE)
_RECUSED_RE = re.compile(r"\brecused\b[:\s]*?(\d+)", re.IGNORECASE)

_DISSENT_RE = re.compile(
    r"Supervisor\s+([A-Z][A-Za-z'\-]+)\s+(?:voting|voted)\s+"
    r'"?(no|nay|aye|yes)"?', re.IGNORECASE)
_OPPOSED_RE = re.compile(
    r"Supervisor\s+([A-Z][A-Za-z'\-]+)\s+(?:opposed|dissenting|dissented)",
    re.IGNORECASE)
_ABSTAIN_NAME_RE = re.compile(
    r"Supervisor\s+([A-Z][A-Za-z'\-]+)\s+abstain(?:ed|ing)?", re.IGNORECASE)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _to_text(s):
    """Strip HTML tags to readable text (pass-through for plain text)."""
    if s is None:
        return ""
    s = re.sub(r"(?is)<script.*?</script>", " ", s)
    s = re.sub(r"(?is)<style.*?</style>", " ", s)
    s = re.sub(r"(?s)<[^>]+>", "\n", s)
    s = html.unescape(s)
    s = s.replace("\xa0", " ")
    return s


def _parse_date(mon, day, year):
    key = mon.strip().rstrip(".").lower()[:3]
    m = _MONTHS.get(key)
    if not m:
        return None
    try:
        return "%04d-%02d-%02d" % (int(year), m, int(day))
    except ValueError:
        return None


def _enumerate_meetings(rt):
    """Return list of {date, clip_id} newest-first from ViewPublisher.

    NOTE: clip ids live in href attributes, so we scan the RAW markup
    (fetch_text returns the page source) and must NOT strip tags first.
    """
    try:
        raw = rt.fetch_text(_VP_URL, params={"view_id": _VIEW_ID})
    except Exception:
        raw = None
    if not raw:
        return []

    idx = raw.lower().find("archived video")
    region = raw[idx:] if idx != -1 else raw

    titles = [(m.start(), m.group(1), m.group(2), m.group(3))
              for m in _TITLE_RE.finditer(region)]
    clips = [(m.start(), m.group(1)) for m in _CLIP_RE.finditer(region)]

    meetings = []
    seen = set()
    for i, (pos, mon, day, year) in enumerate(titles):
        date = _parse_date(mon, day, year)
        if not date:
            continue
        nxt = titles[i + 1][0] if i + 1 < len(titles) else len(region)
        clip_id = None
        for cpos, cid in clips:
            if pos <= cpos < nxt:
                clip_id = cid
                break
        if clip_id is None:
            continue  # no video/document -> not a completed archived meeting
        if date in seen:
            continue
        seen.add(date)
        meetings.append({"date": date, "clip_id": clip_id})

    meetings.sort(key=lambda m: m["date"], reverse=True)
    return meetings


def _score(text):
    return len(_PASS_RE.findall(text)) + len(_FAIL_RE.findall(text))


def _fetch_best_document(rt, clip_id):
    """Fetch the Granicus document that records the most dispositions.
    Returns (raw_text, stripped_text, url) or (None, None, None)."""
    candidates = []  # (raw, url)

    mp_url = "%s?view_id=%s&clip_id=%s" % (_MP_URL, _VIEW_ID, clip_id)
    try:
        mp_raw = rt.fetch_text(
            _MP_URL, params={"view_id": _VIEW_ID, "clip_id": clip_id})
    except Exception:
        mp_raw = None
    if mp_raw:
        candidates.append((mp_raw, mp_url))

    doc_ids = []
    if mp_raw:
        for m in _DOCID_RE.finditer(mp_raw):
            d = m.group(1)
            if d not in doc_ids:
                doc_ids.append(d)
    for doc_id in doc_ids[:4]:
        mv_url = ("%s?view_id=%s&clip_id=%s&doc_id=%s"
                  % (_MV_URL, _VIEW_ID, clip_id, doc_id))
        try:
            mv_raw = rt.fetch_text(
                _MV_URL, params={"view_id": _VIEW_ID, "clip_id": clip_id,
                                 "doc_id": doc_id})
        except Exception:
            mv_raw = None
        if mv_raw:
            candidates.append((mv_raw, mv_url))

    if not doc_ids:
        mv_url = "%s?view_id=%s&clip_id=%s" % (_MV_URL, _VIEW_ID, clip_id)
        try:
            mv_raw = rt.fetch_text(
                _MV_URL, params={"view_id": _VIEW_ID, "clip_id": clip_id})
        except Exception:
            mv_raw = None
        if mv_raw:
            candidates.append((mv_raw, mv_url))

    best = None
    best_score = -1
    for raw, url in candidates:
        stripped = _to_text(raw)
        sc = _score(stripped)
        if sc > best_score:
            best_score = sc
            best = (raw, stripped, url)
    return best if best else (None, None, None)


def _iter_items(text):
    """Yield (item_key, title_text, block_text) from stripped text."""
    lines = text.splitlines()
    starts = []
    for i, ln in enumerate(lines):
        m = _ITEM_RE.match(ln)
        if m:
            starts.append((i, m.group(1).rstrip(")").lower(),
                           m.group(2).strip()))
    for j, (li, key, rest) in enumerate(starts):
        end_li = starts[j + 1][0] if j + 1 < len(starts) else len(lines)
        block_lines = lines[li:end_li]
        block = " ".join(x.strip() for x in block_lines if x.strip())
        title = rest
        if not title:
            for x in block_lines[1:]:
                if x.strip():
                    title = x.strip()
                    break
        yield key, title, block


def _anchor_start(raw, title):
    toks = [t for t in re.split(r"[^A-Za-z0-9]+", title) if t]
    toks = [t for t in toks if re.match(r"^[A-Za-z0-9]+$", t)][:6]
    if len(toks) < 2:
        return None
    sep = r"(?:\s|&nbsp;|&#160;|<[^>]*>)+"
    pat = sep.join(re.escape(t) for t in toks)
    m = re.search(pat, raw, re.IGNORECASE | re.DOTALL)
    return m.start() if m else None


def _make_quote(raw, title, keyword):
    """Return a verbatim substring of `raw` (<=400 chars) containing the
    outcome keyword, anchored near the item title where possible."""
    kw_re = re.compile(r"\b" + re.escape(keyword) + r"\b")
    start = _anchor_start(raw, title)
    if start is not None:
        window = raw[start:start + 2500]
        km = kw_re.search(window)
        if km:
            end = start + km.end()
            s = start
            if end - s > 400:
                s = end - 400
            q = raw[s:end]
            if keyword.lower() in q.lower():
                return q[:400]
    km = kw_re.search(raw)
    if km:
        s = max(0, km.end() - 380)
        e = min(len(raw), km.end() + 20)
        q = raw[s:e]
        if keyword.lower() in q.lower():
            return q[:400]
    return None


def _parse_counts(block):
    counts = {}
    for key, rx in (("aye", _AYE_RE), ("no", _NAY_RE), ("abstain", _ABS_RE),
                    ("absent", _ABSENT_RE), ("recused", _RECUSED_RE)):
        m = rx.search(block)
        if m:
            try:
                counts[key] = int(m.group(1))
            except ValueError:
                pass
    return counts or None


def _parse_positions(block):
    positions = {}
    for m in _DISSENT_RE.finditer(block):
        stance = m.group(2).lower()
        positions[m.group(1)] = "aye" if stance in ("aye", "yes") else "no"
    for m in _OPPOSED_RE.finditer(block):
        positions.setdefault(m.group(1), "no")
    for m in _ABSTAIN_NAME_RE.finditer(block):
        positions[m.group(1)] = "abstain"
    return positions


def _analyze(block, title, raw, doc_url):
    fail_m = _FAIL_RE.search(block)
    pass_m = _PASS_RE.search(block)
    if fail_m:
        result, keyword = "fail", fail_m.group(1)
    elif pass_m:
        result, keyword = "pass", pass_m.group(1)
    else:
        return None

    quote = _make_quote(raw, title, keyword)
    if not quote:
        return None

    counts = _parse_counts(block)
    positions = _parse_positions(block)
    if _UNAN_RE.search(block):
        unanimous = True
    elif counts and counts.get("no", 0) > 0:
        unanimous = False
    else:
        unanimous = None

    return {
        "result": result,
        "counts": counts,
        "positions": positions,
        "unanimous": unanimous,
        "evidence": {"quote": quote, "doc_url": doc_url},
    }


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------

def extract(rt, args):
    max_meetings = int(args[0])

    assertions = {}
    entry_count = 0

    meetings = _enumerate_meetings(rt)

    covered = 0
    for meeting in meetings:
        if covered >= max_meetings:
            break
        date = meeting["date"]
        clip_id = meeting["clip_id"]

        raw, stripped, doc_url = _fetch_best_document(rt, clip_id)
        if not raw:
            continue

        items = {}
        for key, title, block in _iter_items(stripped):
            entry = _analyze(block, title, raw, doc_url)
            if entry is None:
                continue
            items.setdefault(key, []).append(entry)
            entry_count += 1

        assertions[date] = {
            "attendance": {"present": [], "absent": []},
            "items": items,
        }
        covered += 1

    run_meta = {
        "source_id": "fairfax-bos-oracle",
        "extractor_version": EXTRACTOR_VERSION,
        "row_counts": {"meetings": len(assertions), "entries": entry_count},
    }
    return assertions, run_meta
