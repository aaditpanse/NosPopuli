"""
Independent second-source assertion extractor for `loudoun-bos`.

Second source (per the discovered profile): the per-meeting Board *Minutes*
documents published to Laserfiche WebLink.  These are produced by the Clerk
and approved by the Board at a later meeting, and narrate each motion
independently of the Action Report the primary reads.

Location path (navigation only):
  year folder RSS  -> per-meeting folder RSS -> "... Minutes" document
  /LFPortalinternet/rss/dbid/0/folder/{id}/feed.rss
  document PDF at  /LFPortalinternet/0/edoc/{doc_id}/x.pdf

We NEVER read the primary's data endpoints (escribe pages, Action Reports).
Only the Minutes PDFs are read for vote data.  Deterministic, stdlib only.
"""

import re
import html

EXTRACTOR_VERSION = "1"

BASE = "https://lfportal.loudoun.gov/LFPortalinternet"

# Year folder ids from the profile (yy -> Laserfiche folder id).
YEAR_FOLDERS = {"26": 1966224, "25": 1947831}

# Store meetings the primary already extracted; we must cover these.
STORE_MEETINGS = [
    "2026-07-07", "2026-06-16", "2026-06-02", "2026-05-19", "2026-05-05",
    "2026-05-04", "2026-04-21", "2026-04-07", "2026-03-17", "2026-03-03",
    "2026-02-18", "2026-02-11", "2026-02-03", "2026-01-21", "2026-01-06",
]

STOP = {
    "none", "board", "the", "and", "county", "district", "supervisor",
    "supervisors", "chair", "chairman", "vice", "large", "absent", "present",
    "ayes", "nays", "aye", "nay", "motion", "meeting", "business", "members",
    "at", "hearing", "public",
}


# ----------------------------------------------------------------------------
# small helpers
# ----------------------------------------------------------------------------
def _rss_url(fid):
    return "%s/rss/dbid/0/folder/%s/feed.rss" % (BASE, fid)


def _edoc_url(doc_id):
    return "%s/0/edoc/%s/x.pdf" % (BASE, doc_id)


def _parse_rss_items(text):
    items = []
    if not text:
        return items
    for block in re.findall(r"<item>(.*?)</item>", text, re.S | re.I):
        tm = re.search(r"<title>(.*?)</title>", block, re.S | re.I)
        lm = re.search(r"<link>(.*?)</link>", block, re.S | re.I)
        title = html.unescape(tm.group(1)).strip() if tm else ""
        link = html.unescape(lm.group(1)).strip() if lm else ""
        items.append((title, link))
    return items


def _parse_roll(seg):
    """Extract last names from a roll / name-list fragment."""
    seg = re.sub(r"\([^)]*\)", " ", seg)
    out = []
    for part in re.split(r"[;,\n]|\band\b", seg):
        p = part.strip()
        if not p:
            continue
        toks = re.findall(r"[A-Z][a-zA-Z'\-]+", p)
        cand = None
        for t in reversed(toks):
            if t.lower() in STOP:
                continue
            if len(t) >= 3:
                cand = t
                break
        if cand:
            out.append(cand)
    seen = []
    for x in out:
        if x not in seen:
            seen.append(x)
    return seen


def _clean_title(t):
    return re.sub(r"\s+", " ", t).strip()[:200]


# ----------------------------------------------------------------------------
# attendance
# ----------------------------------------------------------------------------
def _parse_attendance(text):
    present, absent = [], []
    head = text[:4000]
    mp = re.search(r"(?is)\bpresent\b\s*[:\.\-]\s*(.{10,700})", head)
    if mp:
        seg = mp.group(1)
        ai = re.search(r"(?i)\babsent\b", seg)
        pseg = seg[:ai.start()] if ai else seg[:500]
        present = _parse_roll(pseg)
    ma = re.search(r"(?is)\babsent\b\s*[:\.\-]\s*(.{0,400})", head)
    if ma:
        seg = re.split(r"\n\s*\n", ma.group(1))[0]
        seg = re.split(r"(?<=[a-z])\.\s+[A-Z]", seg)[0]
        absent = _parse_roll(seg)
    if not (3 <= len(present) <= 11):
        present = []
    if len(absent) > 6:
        absent = []
    return present, absent


# ----------------------------------------------------------------------------
# vote detail extraction from a local context window (whitespace-normalised)
# ----------------------------------------------------------------------------
def _extract_counts(ctx):
    a = re.search(r"(?i)\bayes?\s*[:\-]?\s*(\d{1,2})\b", ctx)
    n = re.search(r"(?i)\bnays?\s*[:\-]?\s*(\d{1,2})\b", ctx)
    if a and n:
        c = {"aye": int(a.group(1)), "no": int(n.group(1))}
        ab = re.search(r"(?i)abstain(?:ed|ing|s)?\s*[:\-]?\s*(\d{1,2})\b", ctx)
        if ab:
            c["abstain"] = int(ab.group(1))
        av = re.search(r"(?i)\babsent\s*[:\-]?\s*(\d{1,2})\b", ctx)
        if av:
            c["absent"] = int(av.group(1))
        return c
    d = re.search(
        r"(?i)(?:vote of|by a vote of|vote:|voted)\s*\(?\s*"
        r"(\d{1,2})\s*-\s*(\d{1,2})(?:\s*-\s*(\d{1,2}))?", ctx)
    if not d:
        d = re.search(
            r"\(\s*(\d{1,2})\s*-\s*(\d{1,2})(?:\s*-\s*(\d{1,2}))?\s*\)", ctx)
    if d:
        c = {"aye": int(d.group(1)), "no": int(d.group(2))}
        if d.group(3):
            c["abstain"] = int(d.group(3))
        return c
    return None


_NAME_GROUP = r"([A-Z][A-Za-z\.\'\-]+(?:(?:,|,? and)\s+[A-Z][A-Za-z\.\'\-]+)*)"


def _extract_positions(ctx):
    pos = {}
    for m in re.finditer(
            r"(?i)supervisors?\s+" + _NAME_GROUP +
            r"\s+(?:voting|voted)\s+(no|nay|against|aye|yes)", ctx):
        stance = "no" if m.group(2).lower() in ("no", "nay", "against") else "aye"
        for ln in _parse_roll(m.group(1)):
            pos[ln] = stance
    for m in re.finditer(
            r"(?i)supervisors?\s+" + _NAME_GROUP +
            r"\s+(?:abstain|abstained|abstaining)", ctx):
        for ln in _parse_roll(m.group(1)):
            pos.setdefault(ln, "abstain")
    for m in re.finditer(
            r"(?i)supervisors?\s+" + _NAME_GROUP +
            r"\s+(?:opposed|dissenting|in opposition|voting in the negative)", ctx):
        for ln in _parse_roll(m.group(1)):
            pos[ln] = "no"
    m = re.search(r"(?i)nays?\s*[:\-]\s*(supervisors?[^.\n]{0,120})", ctx)
    if m and not re.search(r"(?i)none", m.group(1)):
        for ln in _parse_roll(m.group(1)):
            pos[ln] = "no"
    m = re.search(r"(?i)abstain[a-z]*\s*[:\-]\s*(supervisors?[^.\n]{0,120})", ctx)
    if m and not re.search(r"(?i)none", m.group(1)):
        for ln in _parse_roll(m.group(1)):
            pos.setdefault(ln, "abstain")
    m = re.search(r"(?i)\babsent\s*[:\-]\s*(supervisors?[^.\n]{0,120})", ctx)
    if m and not re.search(r"(?i)none", m.group(1)):
        for ln in _parse_roll(m.group(1)):
            pos.setdefault(ln, "absent")
    return pos


# ----------------------------------------------------------------------------
# item / motion segmentation
# ----------------------------------------------------------------------------
_HEADING_RE = re.compile(
    r"(?m)^[ \t]*(\d{1,2}(?:\.[A-Za-z0-9]+)*)\.?[ \t]+(\S.*)$")

_KW_RE = re.compile(
    r"\b(carried|passed|failed|defeated|adopted|prevailed|approved)\b", re.I)

_FAIL = {"failed", "defeated"}


def _headings(text):
    hs = []
    for m in _HEADING_RE.finditer(text):
        label = m.group(1)
        title = m.group(2).strip()
        if len(title) < 4 or not title[0:1].isupper():
            continue
        key = label.lower().rstrip(")").rstrip(".")
        if not key:
            continue
        hs.append((m.start(), key, _clean_title(title)))
    hs.sort()
    return hs


def _heading_before(hs, pos):
    chosen = None
    for start, key, title in hs:
        if start < pos:
            chosen = (key, title)
        else:
            break
    return chosen


def _make_quote(text, pos):
    s = max(0, pos - 220)
    e = min(len(text), pos + 180)
    q = text[s:e].strip()
    return q[:400]


def _parse_items(text, doc_url):
    hs = _headings(text)
    matches = []
    for m in _KW_RE.finditer(text):
        pos = m.start()
        pre = text[max(0, pos - 180):pos].lower()
        if re.search(r"\b(motion|moved|resolution|seconded|vote|board|committee)\b", pre):
            matches.append((pos, m.group(1).lower()))
    matches.sort()

    clusters = []
    for pos, kw in matches:
        if clusters and pos - clusters[-1]["end"] < 160:
            clusters[-1]["kws"].append(kw)
            clusters[-1]["end"] = pos
        else:
            clusters.append({"start": pos, "end": pos, "kws": [kw]})

    items = {}
    for cl in clusters:
        hb = _heading_before(hs, cl["start"])
        if hb is None:
            continue
        key, htitle = hb
        kws = set(cl["kws"])
        if kws & _FAIL:
            result = "fail"
        else:
            result = "pass"

        cs = max(0, cl["start"] - 350)
        ce = min(len(text), cl["end"] + 250)
        ctx = re.sub(r"\s+", " ", text[cs:ce])

        counts = _extract_counts(ctx)
        positions = _extract_positions(ctx)

        if re.search(r"(?i)unanimous", ctx):
            unanimous = True
        elif any(v == "no" for v in positions.values()):
            unanimous = False
        elif counts and counts.get("no", 0) > 0:
            unanimous = False
        else:
            unanimous = None

        entry = {
            "title": htitle,
            "result": result,
            "counts": counts,
            "positions": positions,
            "unanimous": unanimous,
            "evidence": {
                "quote": _make_quote(text, cl["start"]),
                "doc_url": doc_url,
            },
        }
        items.setdefault(key, []).append(entry)
    return items


def _parse_meeting(text, doc_url):
    present, absent = _parse_attendance(text)
    items = _parse_items(text, doc_url)
    return {
        "attendance": {"present": present, "absent": absent},
        "items": items,
    }


# ----------------------------------------------------------------------------
# main extract
# ----------------------------------------------------------------------------
def extract(rt, args):
    max_meetings = int(args[0])

    def fetch(url):
        try:
            t = rt.fetch_text(url)
            return t
        except Exception:
            return None

    year_cache = {}

    def year_items(fid):
        if fid not in year_cache:
            year_cache[fid] = _parse_rss_items(fetch(_rss_url(fid)))
        return year_cache[fid]

    def locate(d):
        yy = d[2:4]
        fid = YEAR_FOLDERS.get(yy)
        if not fid:
            return None
        mmddyy = "%s-%s-%s" % (d[5:7], d[8:10], yy)
        startid = None
        for title, link in year_items(fid):
            t = title.strip()
            if t.startswith(mmddyy) and "business meeting" in t.lower():
                m = re.search(r"startid=(\d+)", link)
                if m:
                    startid = m.group(1)
                    break
        if not startid:
            return None
        folder_items = _parse_rss_items(fetch(_rss_url(startid)))
        for title, link in folder_items:
            if "minutes" in title.lower():
                link = html.unescape(link)
                m = re.search(r"[?&]id=(\d+)", link)
                if m:
                    return _edoc_url(m.group(1))
        return None

    store_desc = sorted(set(STORE_MEETINGS), reverse=True)

    located = []          # (date, doc_url) newest-first
    not_located = []      # store dates with no locatable minutes document

    for d in store_desc:
        try:
            url = locate(d)
        except Exception:
            url = None
        if url:
            located.append((d, url))
        else:
            not_located.append(d)

    without_document = list(not_located)

    assertions = {}
    covered = 0
    for d, url in located:
        if covered >= max_meetings:
            break
        text = fetch(url)
        if not text or len(text) < 200:
            without_document.append(d)
            continue
        try:
            parsed = _parse_meeting(text, url)
        except Exception:
            without_document.append(d)
            continue
        assertions[d] = parsed
        covered += 1

    without_document = sorted(
        {x for x in without_document if x in set(STORE_MEETINGS)},
        reverse=True)

    entry_count = 0
    for d in assertions:
        for lst in assertions[d]["items"].values():
            entry_count += len(lst)

    run_meta = {
        "source_id": "loudoun-bos-oracle",
        "extractor_version": EXTRACTOR_VERSION,
        "row_counts": {"meetings": len(assertions), "entries": entry_count},
        "meetings_without_document": without_document,
    }
    return assertions, run_meta
