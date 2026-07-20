"""Independent second-source assertion extractor for fairfax-bos.

Second source: county-hosted Granicus video platform
(video.fairfaxcounty.gov). We enumerate archived Board of Supervisors
meetings from ViewPublisher.php, then read each meeting's structured
HTML agenda/minutes (MediaPlayer.php / MinutesViewer.php). We NEVER touch
the primary CMS (www.fairfaxcounty.gov/boardofsupervisors ...) data pages.

The Granicus agenda/minutes documents are produced independently of the
Clerk's vote-narrative PDFs; assertions are only emitted where the fetched
document *explicitly* records a motion outcome. When the document is silent
(the common case for this source, which carries only action-status markers),
no item assertion is emitted — a missing assertion is correct.
"""

import re

EXTRACTOR_VERSION = "1"

_VP_URL = "https://video.fairfaxcounty.gov/ViewPublisher.php"
_MP_URL = "https://video.fairfaxcounty.gov/MediaPlayer.php"
_MV_URL = "https://video.fairfaxcounty.gov/MinutesViewer.php"
_VIEW_ID = "7"

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

_TITLE_RE = re.compile(
    r"((?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|"
    r"Dec(?:ember)?)\.?)\s+(\d{1,2}),\s+(\d{4})\s+Board of Supervisors",
    re.IGNORECASE,
)

_CLIP_RE = re.compile(r"clip_id=(\d+)")
_DOCID_RE = re.compile(r"doc_id=([0-9a-fA-F\-]{8,})")

# Item numbering at the start of a logical line, e.g. "1.", "4.a", "12.b", "6"
_ITEM_RE = re.compile(r"^\s*(\d+(?:\.[A-Za-z0-9]+)*)[.)]?\s+(.*\S)\s*$")

_PASS_RE = re.compile(
    r"\b(approved|adopted|carried|passed|concurred|granted)\b", re.IGNORECASE)
_FAIL_RE = re.compile(
    r"\b(denied|failed|rejected|defeated|not\s+approved)\b", re.IGNORECASE)
_UNAN_RE = re.compile(r"\bunanimous(?:ly)?\b", re.IGNORECASE)

_AYE_RE = re.compile(r"\bayes?\b[:\s]*?(\d+)", re.IGNORECASE)
_NAY_RE = re.compile(r"\b(?:nays?|noes?)\b[:\s]*?(\d+)", re.IGNORECASE)
_ABS_RE = re.compile(r"\babstain(?:ed|ing|s)?\b[:\s]*?(\d+)", re.IGNORECASE)
_ABSENT_RE = re.compile(r"\babsent\b[:\s]*?(\d+)", re.IGNORECASE)
_RECUSED_RE = re.compile(r"\brecused\b[:\s]*?(\d+)", re.IGNORECASE)

# Named dissent / abstention patterns.
_DISSENT_RE = re.compile(
    r"Supervisor\s+([A-Z][A-Za-z'\-]+)\s+(?:voting|voted)\s+"
    r'"?(no|nay|aye|yes)"?', re.IGNORECASE)
_OPPOSED_RE = re.compile(
    r"Supervisor\s+([A-Z][A-Za-z'\-]+)\s+(?:opposed|dissenting|dissented)",
    re.IGNORECASE)
_ABSTAIN_NAME_RE = re.compile(
    r"Supervisor\s+([A-Z][A-Za-z'\-]+)\s+abstain(?:ed|ing)?", re.IGNORECASE)


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
    """Return list of dicts {date, clip_id} newest-first from ViewPublisher."""
    try:
        html = rt.fetch_text(_VP_URL, params={"view_id": _VIEW_ID})
    except Exception:
        return []
    if not html:
        return []

    # Restrict to the archived-videos region so upcoming (video-less) meetings
    # do not get mis-paired with the first archived clip.
    idx = html.lower().find("archived video")
    region = html[idx:] if idx != -1 else html

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


def _fetch_document(rt, clip_id):
    """Fetch the richest available second-source agenda/minutes document.

    Prefer MinutesViewer (carries item-level action detail) when its doc_id
    can be discovered on the MediaPlayer page; otherwise fall back to the
    MediaPlayer agenda index. Returns (text, url) or (None, None).
    """
    mp_params = {"view_id": _VIEW_ID, "clip_id": clip_id}
    mp_url = "%s?view_id=%s&clip_id=%s" % (_MP_URL, _VIEW_ID, clip_id)
    try:
        mp_text = rt.fetch_text(_MP_URL, params=mp_params)
    except Exception:
        mp_text = None

    if mp_text:
        m = _DOCID_RE.search(mp_text)
        if m:
            doc_id = m.group(1)
            mv_params = {"view_id": _VIEW_ID, "clip_id": clip_id,
                         "doc_id": doc_id}
            mv_url = ("%s?view_id=%s&clip_id=%s&doc_id=%s"
                      % (_MV_URL, _VIEW_ID, clip_id, doc_id))
            try:
                mv_text = rt.fetch_text(_MV_URL, params=mv_params)
            except Exception:
                mv_text = None
            if mv_text:
                return mv_text, mv_url
        return mp_text, mp_url

    return None, None


def _clean_line(s):
    return re.sub(r"\s+", " ", s).strip()


def _iter_item_blocks(text):
    """Yield (item_key, block_text) for each numbered agenda item block."""
    lines = text.splitlines()
    cur_key = None
    cur_lines = []
    for raw in lines:
        line = _clean_line(raw)
        if not line:
            continue
        m = _ITEM_RE.match(line)
        if m:
            if cur_key is not None:
                yield cur_key, " ".join(cur_lines)
            token = m.group(1).rstrip(")").lower()
            cur_key = token
            cur_lines = [line]
        elif cur_key is not None:
            cur_lines.append(line)
    if cur_key is not None:
        yield cur_key, " ".join(cur_lines)


def _outcome_quote(block):
    """Return a verbatim substring (<=400 chars) of block that contains the
    outcome keyword, or None."""
    for rx in (_PASS_RE, _FAIL_RE):
        m = rx.search(block)
        if m:
            start = max(0, m.start() - 180)
            end = min(len(block), m.end() + 180)
            snippet = block[start:end]
            return snippet[:400]
    # tally-only evidence
    if _AYE_RE.search(block) or _NAY_RE.search(block):
        m = _AYE_RE.search(block) or _NAY_RE.search(block)
        start = max(0, m.start() - 180)
        end = min(len(block), m.end() + 180)
        return block[start:end][:400]
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
        name = m.group(1)
        stance = m.group(2).lower()
        stance = "aye" if stance in ("aye", "yes") else "no"
        positions[name] = stance
    for m in _OPPOSED_RE.finditer(block):
        positions.setdefault(m.group(1), "no")
    for m in _ABSTAIN_NAME_RE.finditer(block):
        positions[m.group(1)] = "abstain"
    return positions


def _analyze_block(block, doc_url):
    """Return an entry dict if the block records an explicit motion outcome."""
    quote = _outcome_quote(block)
    if not quote:
        return None

    is_pass = bool(_PASS_RE.search(block))
    is_fail = bool(_FAIL_RE.search(block))
    if is_pass and not is_fail:
        result = "pass"
    elif is_fail and not is_pass:
        result = "fail"
    else:
        # Ambiguous wording with only a tally: decide by nay count if present.
        counts_probe = _parse_counts(block)
        if counts_probe and counts_probe.get("no", 0) > counts_probe.get("aye", 0):
            result = "fail"
        elif counts_probe:
            result = "pass"
        else:
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

        text, doc_url = _fetch_document(rt, clip_id)
        if not text:
            continue  # missing/unparseable -> skip, do not crash

        items = {}
        for item_key, block in _iter_item_blocks(text):
            entry = _analyze_block(block, doc_url)
            if entry is None:
                continue
            items.setdefault(item_key, []).append(entry)
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
