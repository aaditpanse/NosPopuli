"""
Independent second-source assertion extractor for `princewilliam-bos`.

Second source: Granicus MinutesViewer HTML ("Briefs") action minutes,
  https://pwcgov.granicus.com/MinutesViewer.php?view_id=23&clip_id=<N>

These "Brief" documents are rendered independently from the VoteLog JSON API.
Vote outcomes are read ONLY from those MinutesViewer documents. The primary
data endpoints (ViewPublisher / ViewPublisherRSS / votelog.ashx) are never
touched. Meetings are located by probing clip_id integers and reading the
meeting date printed inside each Brief document.

Parsing is deliberately tolerant of layout: it does not assume newlines vs
spaces, and it treats &nbsp; and HTML tags as delimiters, so it works whether
fetch_text returns collapsed text, newline text, or lightly-tagged HTML.
Evidence quotes are always sliced verbatim out of the exact fetched string.
"""

import re

EXTRACTOR_VERSION = "1"

_BASE = "https://pwcgov.granicus.com/MinutesViewer.php"
_VIEW_ID = "23"
# Known-recent anchor clip_id (2026-07-07 per the source profile). Used only as
# a starting point for clip_id probing -- not a fetch of the primary source.
_ANCHOR_CLIP = 3859

_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12,
}

# Agenda label token: "3.A." / "5." / "12.B)" delimited by whitespace / entity
# boundary / tag boundary (tolerant of collapsed text and light HTML).
_LABEL_RE = re.compile(
    r'(?:^|(?<=[\s;>]))(\d{1,2}(?:\.[A-Za-z])?)(?:\.|\))(?=[\s&<]|$)'
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
_TAG_RE = re.compile(r'<[^>]+>')

_PASS_KW = ("APPROVED", "ADOPTED", "PASSED", "CARRIED", "DEFERRED")
_FAIL_KW = ("DENIED", "FAILED", "DEFEATED")

_STOP_WORDS = {"chair", "vice", "meeting", "member", "supervisor", "hon"}


def _doc_url(clip):
    return "%s?view_id=%s&clip_id=%d" % (_BASE, _VIEW_ID, clip)


def _clean(s):
    if not s:
        return ""
    s = _TAG_RE.sub(' ', s)
    s = s.replace('&nbsp;', ' ')
    return s


def _names(seg):
    """Extract last-name tokens from a captured name segment."""
    seg = _clean(seg)
    if not seg:
        return []
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
        if _NAME_OK.match(name) and name.lower() not in _STOP_WORDS:
            out.append(name)
    return out


def _roster(header):
    """Extract board-member last names from the header roster."""
    header = _clean(header)
    segs = header.split('Hon.')
    out = []
    for s in segs[1:]:
        s = s.split('\n')[0]
        s = s.split(',')[0]
        toks = [t for t in re.split(r'\s+', s.strip()) if t]
        if not toks:
            continue
        name = toks[-1].strip('.').strip()
        if _NAME_OK.match(name) and name.lower() not in _STOP_WORDS:
            out.append(name)
    seen = set()
    res = []
    for n in out:
        if n not in seen:
            seen.add(n)
            res.append(n)
    return res


def _parse_item(content, cstart, url, raw):
    cu = content.upper()

    result = None
    kw = None
    for k in _FAIL_KW:
        if k in cu:
            result, kw = 'fail', k
            break
    if result is None:
        for k in _PASS_KW:
            if k in cu:
                result, kw = 'pass', k
                break
    if result is None:
        if 'UNANIMOUS' in cu:
            result, kw = 'pass', 'UNANIMOUS'
        else:
            return None

    absent_names, no_names, abstain_names, recuse_names = [], [], [], []
    for m in _ABSENT_RE.finditer(content):
        absent_names += _names(m.group(1))
    for m in _NO_RE.finditer(content):
        no_names += _names(m.group(1))
    for m in _ABSTAIN_RE.finditer(content):
        abstain_names += _names(m.group(1))
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

    kwpos = cu.find(kw)
    abs_pos = cstart + (kwpos if kwpos >= 0 else 0)
    quote = raw[abs_pos:abs_pos + 300].strip()

    return {
        "result": result,
        "counts": None,
        "positions": positions,
        "unanimous": unanimous,
        "evidence": {"quote": quote, "doc_url": url},
    }


def _parse_meeting(raw, url):
    if not raw:
        return None

    ntext = _clean(raw)
    dm = _DATE_RE.search(ntext)
    if not dm:
        return None
    mon = _MONTHS.get(dm.group(1).lower())
    if not mon:
        return None
    date = "%04d-%02d-%02d" % (int(dm.group(3)), mon, int(dm.group(2)))

    labels = list(_LABEL_RE.finditer(raw))

    header = raw[:labels[0].start()] if labels else raw[:4000]
    roster = _roster(header)

    items = {}
    for i, m in enumerate(labels):
        key = m.group(1).lower().rstrip(').')
        cstart = m.end()
        cend = labels[i + 1].start() if i + 1 < len(labels) else len(raw)
        content = raw[cstart:cend]
        entry = _parse_item(content, cstart, url, raw)
        if entry:
            items.setdefault(key, []).append(entry)

    absent_all = set()
    for m in _ABSENT_RE.finditer(ntext):
        for n in _names(m.group(1)):
            absent_all.add(n)

    present = [r for r in roster if r not in absent_all]
    absent = [r for r in roster if r in absent_all]
    absent += [n for n in sorted(absent_all) if n not in roster]

    return {
        "date": date,
        "completed": bool(items),
        "items": items,
        "attendance": {"present": present, "absent": absent},
    }


def extract(rt, args):
    max_meetings = int(args[0]) if args else 0

    assertions = {}
    entry_count = 0
    run_meta = {
        "source_id": "princewilliam-bos-oracle",
        "extractor_version": EXTRACTOR_VERSION,
        "row_counts": {"meetings": 0, "entries": 0},
    }
    if max_meetings <= 0:
        return assertions, run_meta

    cache = {}
    budget = [140]

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
        parsed = None
        if txt:
            try:
                parsed = _parse_meeting(txt, url)
            except Exception:
                parsed = None
        cache[clip] = parsed
        return parsed

    # The store's newest completed meeting corresponds to the anchor clip.
    # Collect the most recent completed meetings scanning downward from it.
    top = _ANCHOR_CLIP
    collected = {}
    misses = 0
    cid = top
    while len(collected) < max_meetings and cid > top - 120 and misses < 20:
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

    run_meta["row_counts"] = {"meetings": len(assertions), "entries": entry_count}
    return assertions, run_meta
