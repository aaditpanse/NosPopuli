"""
Independent second-source assertion extractor for `princewilliam-bos`.

Second source: Granicus MinutesViewer HTML ("Briefs") action minutes,
  https://pwcgov.granicus.com/MinutesViewer.php?view_id=23&clip_id=<N>

These "Brief" documents are rendered independently from the VoteLog JSON API.
This extractor reads vote outcomes ONLY from those MinutesViewer documents.
It never touches the primary data endpoints (ViewPublisher / ViewPublisherRSS /
votelog.ashx). Meetings are located by probing clip_id integers and reading the
meeting date printed inside each Brief document.
"""

import re

EXTRACTOR_VERSION = "1"

_BASE = "https://pwcgov.granicus.com/MinutesViewer.php"
_VIEW_ID = "23"
# Known-recent anchor clip_id (2026-07-07 per source profile). Used only as a
# starting point for clip_id probing -- not a data fetch of the primary source.
_ANCHOR_CLIP = 3859

_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12,
}

# A line that is nothing but an agenda label, e.g. " 3.A. ", " 5. ", " 12.B ".
_MARKER_RE = re.compile(
    r'(?m)^[ \t\r]*(\d{1,2}(?:\.[A-Za-z])?)\.?[ \t\r]*$'
)
_DATE_RE = re.compile(
    r'\b(January|February|March|April|May|June|July|August|September|October|'
    r'November|December)\s+(\d{1,2}),\s+(\d{4})\b', re.I
)

_ABSENT_RE = re.compile(r'ABSENT(?:\s+FROM\s+MEETING)?\s*:\s*([^/\n]*)', re.I)
_NO_RE = re.compile(r'\b(?:NO|NAY|NAYS|NOES)\s*:\s*([^/\n]*)', re.I)
_ABSTAIN_RE = re.compile(r'\bABSTAIN(?:ED|ING|S)?\s*:\s*([^/\n]*)', re.I)
_RECUSE_RE = re.compile(r'\bRECUS(?:E|ED|AL|ES)?\s*:\s*([^/\n]*)', re.I)

_NAME_OK = re.compile(r"^[A-Za-z][A-Za-z.'\-]*$")

_PASS_KW = ("APPROVED", "ADOPTED", "PASSED", "CARRIED")
_FAIL_KW = ("DENIED", "FAILED", "DEFEATED")


def _doc_url(clip):
    return "%s?view_id=%s&clip_id=%d" % (_BASE, _VIEW_ID, clip)


def _names(seg):
    """Extract last-name tokens from a captured name segment."""
    if not seg:
        return []
    seg = seg.replace('&nbsp;', ' ')
    for stop in ('Res.', 'RES.', 'res.', 'No.'):
        idx = seg.find(stop)
        if idx != -1:
            seg = seg[:idx]
    seg = seg.split('/')[0]
    parts = re.split(r',|;| and | AND | And ', seg)
    out = []
    for p in parts:
        p = p.strip().strip('.').strip()
        if not p:
            continue
        toks = [t for t in re.split(r'\s+', p) if t]
        if not toks:
            continue
        name = toks[-1].strip('.').strip()
        if _NAME_OK.match(name) and name.lower() not in (
            'chair', 'vice', 'meeting', 'member', 'supervisor'
        ):
            out.append(name)
    return out


def _roster(header):
    """Extract board-member last names from the document header roster."""
    header = header.replace('&nbsp;', ' ')
    segs = header.split('Hon.')
    out = []
    for s in segs[1:]:
        s = s.split('\n')[0]
        s = s.split(',')[0]
        toks = [t for t in re.split(r'\s+', s.strip()) if t]
        if not toks:
            continue
        name = toks[-1].strip('.').strip()
        if _NAME_OK.match(name) and name.lower() not in ('chair', 'vice'):
            out.append(name)
    seen = set()
    res = []
    for n in out:
        if n not in seen:
            seen.add(n)
            res.append(n)
    return res


def _make_quote(fulltext, abs_pos):
    line_start = fulltext.rfind('\n', 0, abs_pos) + 1
    line_end = fulltext.find('\n', abs_pos)
    if line_end == -1:
        line_end = len(fulltext)
    quote = fulltext[line_start:line_end]
    if len(quote) > 400:
        quote = fulltext[line_start:line_start + 400]
    return quote.strip()


def _parse_item(content, fulltext, cstart, url):
    cu = content.upper()

    result = None
    kw = None
    if any(k in cu for k in _FAIL_KW):
        result = 'fail'
        for k in _FAIL_KW:
            if k in cu:
                kw = k
                break
    elif any(k in cu for k in _PASS_KW):
        result = 'pass'
        for k in _PASS_KW:
            if k in cu:
                kw = k
                break
    elif 'UNANIMOUS' in cu:
        result = 'pass'
        kw = 'UNANIMOUS'
    else:
        return None

    absent_names = []
    for m in _ABSENT_RE.finditer(content):
        absent_names += _names(m.group(1))
    no_names = []
    for m in _NO_RE.finditer(content):
        no_names += _names(m.group(1))
    abstain_names = []
    for m in _ABSTAIN_RE.finditer(content):
        abstain_names += _names(m.group(1))
    recuse_names = []
    for m in _RECUSE_RE.finditer(content):
        recuse_names += _names(m.group(1))

    positions = {}
    for n in no_names:
        positions[n] = 'no'
    for n in abstain_names:
        positions.setdefault(n, 'abstain')
    for n in recuse_names:
        positions.setdefault(n, 'recused')
    for n in absent_names:
        positions.setdefault(n, 'absent')

    if 'UNANIMOUS' in cu:
        unanimous = True
    elif no_names or abstain_names or recuse_names:
        unanimous = False
    else:
        unanimous = None

    idx = cu.find(kw)
    abs_pos = cstart + (idx if idx >= 0 else 0)
    quote = _make_quote(fulltext, abs_pos)

    return {
        "result": result,
        "counts": None,
        "positions": positions,
        "unanimous": unanimous,
        "evidence": {"quote": quote, "doc_url": url},
    }


def _parse_meeting(text, url):
    if not text:
        return None
    markers = list(_MARKER_RE.finditer(text))
    header = text[:markers[0].start()] if markers else text

    dm = _DATE_RE.search(header) or _DATE_RE.search(text)
    if not dm:
        return None
    mon = _MONTHS.get(dm.group(1).lower())
    if not mon:
        return None
    date = "%04d-%02d-%02d" % (int(dm.group(3)), mon, int(dm.group(2)))

    roster = _roster(header)

    items = {}
    for i, m in enumerate(markers):
        key = m.group(1).lower().rstrip(').')
        cstart = m.end()
        cend = markers[i + 1].start() if i + 1 < len(markers) else len(text)
        content = text[cstart:cend]
        entry = _parse_item(content, text, cstart, url)
        if entry:
            items.setdefault(key, []).append(entry)

    absent_all = set()
    for m in _ABSENT_RE.finditer(text):
        for n in _names(m.group(1)):
            absent_all.add(n)

    present = [r for r in roster if r not in absent_all]
    absent = [r for r in roster if r in absent_all]
    absent += [n for n in sorted(absent_all) if n not in roster]

    attendance = {"present": present, "absent": absent}

    return {
        "date": date,
        "completed": bool(items),
        "items": items,
        "attendance": attendance,
        "clip": None,
    }


def extract(rt, args):
    max_meetings = int(args[0]) if args else 0
    cache = {}
    budget = [70]

    def get(clip):
        if clip in cache:
            return cache[clip]
        if budget[0] <= 0:
            cache[clip] = None
            return None
        budget[0] -= 1
        url = _doc_url(clip)
        try:
            txt = rt.fetch_text(url)
        except Exception:
            txt = None
        parsed = _parse_meeting(txt, url) if txt else None
        cache[clip] = parsed
        return parsed

    assertions = {}
    entry_count = 0

    if max_meetings <= 0:
        run_meta = {
            "source_id": "princewilliam-bos-oracle",
            "extractor_version": EXTRACTOR_VERSION,
            "row_counts": {"meetings": 0, "entries": 0},
        }
        return assertions, run_meta

    # Locate the newest completed meeting by scanning upward from the anchor.
    top = _ANCHOR_CLIP
    misses = 0
    cid = _ANCHOR_CLIP
    while cid <= _ANCHOR_CLIP + 15 and misses < 8:
        info = get(cid)
        if info and info.get("completed"):
            top = cid
            misses = 0
        else:
            misses += 1
        cid += 1

    # Collect the most recent completed meetings scanning downward.
    collected = {}
    misses = 0
    cid = top
    while len(collected) < max_meetings and cid > top - 90 and misses < 16:
        info = get(cid)
        if info and info.get("completed"):
            d = info["date"]
            if d not in collected:
                collected[d] = info
                misses = 0
            else:
                misses += 1
        else:
            misses += 1
        cid -= 1

    ordered = sorted(collected.values(), key=lambda x: x["date"], reverse=True)
    ordered = ordered[:max_meetings]

    for info in ordered:
        assertions[info["date"]] = {
            "attendance": info["attendance"],
            "items": info["items"],
        }
        for _, entries in info["items"].items():
            entry_count += len(entries)

    run_meta = {
        "source_id": "princewilliam-bos-oracle",
        "extractor_version": EXTRACTOR_VERSION,
        "row_counts": {"meetings": len(assertions), "entries": entry_count},
    }
    return assertions, run_meta
