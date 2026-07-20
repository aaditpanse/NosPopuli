"""Independent second-source assertion extractor for fairfax-bos.

Second source: the county-hosted Granicus video platform
(video.fairfaxcounty.gov). We enumerate archived Board of Supervisors
meetings from ViewPublisher.php, then read each meeting's structured HTML
agenda/minutes document (MinutesViewer.php, falling back to MediaPlayer.php).
These Granicus minutes carry numbered agenda items together with an
item-level action status ('Done', 'Adopted', 'Approved', etc.) produced
independently of the Clerk's vote-narrative PDF on the primary CMS.

We NEVER touch the primary CMS data pages
(www.fairfaxcounty.gov/boardofsupervisors/...). Item assertions are emitted
only where the fetched Granicus document explicitly records a disposition
for that item.
"""

import html
import re

EXTRACTOR_VERSION = "1"

_HOST = "https://video.fairfaxcounty.gov"
_VP_URL = _HOST + "/ViewPublisher.php"
_MP_URL = _HOST + "/MediaPlayer.php"
_MV_URL = _HOST + "/MinutesViewer.php"
_VIEW_ID = "7"

_RSS_CANDIDATES = [
    (_HOST + "/RSSFeed.php", {"view_id": _VIEW_ID, "type": "video"}),
    (_HOST + "/RSSFeed.php", {"view_id": _VIEW_ID, "type": "minutes"}),
    (_HOST + "/RSS.php", {"view_id": _VIEW_ID}),
]

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

_TITLE_RE = re.compile(
    r"((?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|"
    r"Dec(?:ember)?)\.?)\s+(\d{1,2}),?\s+(\d{4})",
    re.IGNORECASE,
)
_CID_MARK_RE = re.compile(r"_CID_(\d+)_")
_CLIP_RE = re.compile(r"clip_id=(\d+)")
_DOCID_RE = re.compile(r"doc_id=([0-9a-fA-F\-]{8,})")

_ITEM_RE = re.compile(r"^\s*(\d+(?:\.[A-Za-z0-9]+)*)[.)]\s*(.*)$")

# Disposition / outcome tokens as they appear in the Granicus minutes.
_PASS_TOKENS = ["Done", "Adopted", "Approved", "Passed", "Carried",
                "Concurred", "Granted", "Authorized"]
_FAIL_TOKENS = ["Failed", "Denied", "Rejected", "Defeated"]
_PASS_RE = re.compile(r"\b(" + "|".join(_PASS_TOKENS) + r")\b", re.IGNORECASE)
_FAIL_RE = re.compile(r"\b(" + "|".join(_FAIL_TOKENS) + r")\b", re.IGNORECASE)
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
    """Convert HTML (or pass through plain text) to a readable text form."""
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


def _pair_titles_to_clips(text):
    """Given text that contains _CID_<n>_ markers, pair each meeting date to
    the clip id that follows it."""
    text = text.replace("\xa0", " ")
    titles = [(m.start(), m.group(1), m.group(2), m.group(3))
              for m in _TITLE_RE.finditer(text)]
    clips = [(m.start(), m.group(1)) for m in _CID_MARK_RE.finditer(text)]
    out = []
    seen = set()
    for i, (pos, mon, day, year) in enumerate(titles):
        date = _parse_date(mon, day, year)
        if not date:
            continue
        nxt = titles[i + 1][0] if i + 1 < len(titles) else len(text)
        clip_id = None
        for cpos, cid in clips:
            if pos <= cpos < nxt:
                clip_id = cid
                break
        if clip_id is None:
            continue
        if date in seen:
            continue
        seen.add(date)
        out.append({"date": date, "clip_id": clip_id})
    return out


def _enumerate_meetings(rt):
    """Return list of {date, clip_id} newest-first."""
    # Primary path: ViewPublisher.
    try:
        raw = rt.fetch_text(_VP_URL, params={"view_id": _VIEW_ID})
    except Exception:
        raw = None
    meetings = []
    if raw:
        marked = _CLIP_RE.sub(lambda m: " _CID_" + m.group(1) + "_ ", raw)
        idx = marked.lower().find("archived video")
        region = marked[idx:] if idx != -1 else marked
        meetings = _pair_titles_to_clips(_to_text(region))

    # Fallback: RSS feeds (URLs survive as element text even when tags are
    # stripped).
    if not meetings:
        for url, params in _RSS_CANDIDATES:
            try:
                rss = rt.fetch_text(url, params=params)
            except Exception:
                rss = None
            if not rss:
                continue
            marked = _CLIP_RE.sub(lambda m: " _CID_" + m.group(1) + "_ ", rss)
            meetings = _pair_titles_to_clips(_to_text(marked))
            if meetings:
                break

    meetings.sort(key=lambda m: m["date"], reverse=True)
    return meetings


def _fetch_minutes(rt, clip_id):
    """Fetch the richest available Granicus document for a clip.
    Returns (text, url)."""
    mp_url = "%s?view_id=%s&clip_id=%s" % (_MP_URL, _VIEW_ID, clip_id)
    try:
        mp_raw = rt.fetch_text(_MP_URL,
                               params={"view_id": _VIEW_ID, "clip_id": clip_id})
    except Exception:
        mp_raw = None

    candidates = []

    # Try MinutesViewer with any doc_id discovered on the MediaPlayer page.
    doc_id = None
    if mp_raw:
        m = _DOCID_RE.search(mp_raw)
        if m:
            doc_id = m.group(1)

    mv_params = {"view_id": _VIEW_ID, "clip_id": clip_id}
    mv_url = "%s?view_id=%s&clip_id=%s" % (_MV_URL, _VIEW_ID, clip_id)
    if doc_id:
        mv_params["doc_id"] = doc_id
        mv_url += "&doc_id=" + doc_id
    try:
        mv_raw = rt.fetch_text(_MV_URL, params=mv_params)
    except Exception:
        mv_raw = None
    if mv_raw:
        candidates.append((mv_raw, mv_url))
    if mp_raw:
        candidates.append((mp_raw, mp_url))

    # Pick the document that records the most dispositions.
    best = None
    best_hits = -1
    for raw, url in candidates:
        txt = _to_text(raw)
        hits = len(_PASS_RE.findall(txt)) + len(_FAIL_RE.findall(txt))
        if hits > best_hits:
            best_hits = hits
            best = (raw, url)
    return best if best else (None, None)


def _iter_items(text):
    """Yield (item_key, title_text, block_text)."""
    lines = [ln for ln in text.splitlines()]
    starts = []  # (line_index, key, inline_rest)
    for i, ln in enumerate(lines):
        m = _ITEM_RE.match(ln)
        if m:
            starts.append((i, m.group(1).rstrip(")").lower(), m.group(2).strip()))
    for j, (li, key, rest) in enumerate(starts):
        end_li = starts[j + 1][0] if j + 1 < len(starts) else len(lines)
        block_lines = lines[li:end_li]
        block = " ".join(x.strip() for x in block_lines if x.strip())
        if rest:
            title = rest
        else:
            title = ""
            for x in block_lines[1:]:
                if x.strip():
                    title = x.strip()
                    break
        yield key, title, block


def _make_quote(orig, title, keyword):
    """Return a verbatim substring of `orig` (<=400 chars) that includes the
    outcome keyword, anchored near the item title."""
    start = None
    if title:
        words = [w for w in re.split(r"\s+", title) if w][:6]
        if words:
            pat = r"\s+".join(re.escape(w) for w in words)
            m = re.search(pat, orig)
            if m:
                start = m.start()
    if start is not None:
        seg = orig[start:start + 900]
        km = re.search(r"(?i)\b" + re.escape(keyword) + r"\b", seg)
        if km:
            end = start + km.end()
        else:
            end = min(len(orig), start + 400)
        if end - start > 400:
            start = end - 400
        q = orig[start:end]
        if q:
            return q[:400]
    km = re.search(r"(?i)\b" + re.escape(keyword) + r"\b", orig)
    if km:
        s = max(0, km.start() - 220)
        e = min(len(orig), km.end() + 40)
        return orig[s:e][:400]
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


def _analyze(block, title, orig, doc_url):
    fail_m = _FAIL_RE.search(block)
    pass_m = _PASS_RE.search(block)
    if fail_m:
        result = "fail"
        keyword = fail_m.group(1)
    elif pass_m:
        result = "pass"
        keyword = pass_m.group(1)
    else:
        return None

    quote = _make_quote(orig, title, keyword)
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

        raw, doc_url = _fetch_minutes(rt, clip_id)
        if not raw:
            continue

        text = _to_text(raw)
        items = {}
        for key, title, block in _iter_items(text):
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
