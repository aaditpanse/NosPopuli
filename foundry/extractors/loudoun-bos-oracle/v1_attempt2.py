"""
Independent second-source assertion extractor for `loudoun-bos`.

Second source: the per-meeting Board *Minutes* PDFs published to Laserfiche
WebLink (produced by the Clerk, approved at a later meeting) — narrating each
motion independently of the Action Report the primary reads.

Location path (navigation only):
  year folder RSS -> per-meeting folder RSS -> "... Minutes" document
Document PDF: /LFPortalinternet/0/edoc/{doc_id}/x.pdf

We NEVER read the primary's data (escribe pages / Action Reports); only the
Minutes PDFs are read for vote data.  Deterministic, stdlib only.
"""

import re
import html

EXTRACTOR_VERSION = "1"

BASE = "https://lfportal.loudoun.gov/LFPortalinternet"
YEAR_FOLDERS = {"26": 1966224, "25": 1947831}

STORE_MEETINGS = [
    "2026-07-07", "2026-06-16", "2026-06-02", "2026-05-19", "2026-05-05",
    "2026-05-04", "2026-04-21", "2026-04-07", "2026-03-17", "2026-03-03",
    "2026-02-18", "2026-02-11", "2026-02-03", "2026-01-21", "2026-01-06",
]

STOP = {
    "none", "board", "the", "and", "county", "district", "supervisor",
    "supervisors", "chair", "chairman", "vice", "large", "absent", "present",
    "ayes", "nays", "aye", "nay", "motion", "meeting", "business", "members",
    "at", "hearing", "public", "moved", "seconded",
}

PROC = [
    "adjourn", "recess", "closed session", "call to order", "roll call",
    "invocation", "pledge", "adoption of the agenda", "adoption of agenda",
    "approval of the minutes", "approval of minutes", "minutes of the",
    "approve the minutes", "certification of the closed",
]

_FAIL = {"failed", "defeated"}
_NAME_GROUP = r"([A-Z][A-Za-z\.\'\-]+(?:(?:,|,? and)\s+[A-Z][A-Za-z\.\'\-]+)*)"
_KW_RE = re.compile(
    r"\b(carried|passed|failed|defeated|adopted|prevailed|approved)\b", re.I)
_HEADING_RE = re.compile(
    r"(?m)^[ \t]*(\d{1,2}(?:\.?[A-Za-z])?)\.?[ \t)]+(\S.*)$")


# ----------------------------------------------------------------------------
# helpers
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


def _is_procedural(title):
    t = title.lower()
    return any(p in t for p in PROC)


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
# headings / item segmentation
# ----------------------------------------------------------------------------
def _norm_key(label):
    k = label.lower().rstrip(").").strip()
    m = re.match(r"^(\d+)([a-z])$", k)
    if m:
        k = m.group(1) + "." + m.group(2)
    return k


def _headings(text):
    hs = []
    for m in _HEADING_RE.finditer(text):
        label = m.group(1)
        title = re.sub(r"\s+", " ", m.group(2).strip())
        if len(title) < 6 or not title[0:1].isupper():
            continue
        preceded_blank = bool(re.search(r"\n[ \t]*\n[ \t]*$", text[:m.start()]))
        has_letter = bool(re.search(r"[a-zA-Z]", label))
        allcaps = title.upper() == title and any(c.isalpha() for c in title)
        words = title.split()
        capwords = sum(1 for w in words[:8] if w[:1].isupper())
        if not (preceded_blank or has_letter or allcaps or capwords >= 3):
            continue
        key = _norm_key(label)
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
    return text[s:e].strip()[:400]


def _extract_positions(ctx):
    pos = {}
    for m in re.finditer(
            r"(?i)supervisors?\s+" + _NAME_GROUP +
            r"\s+(?:voting|voted)\s+(no|nay|against)", ctx):
        for ln in _parse_roll(m.group(1)):
            pos[ln] = "no"
    for m in re.finditer(
            r"(?i)supervisors?\s+" + _NAME_GROUP + r"\s+(?:opposed|dissenting)", ctx):
        for ln in _parse_roll(m.group(1)):
            pos[ln] = "no"
    for m in re.finditer(
            r"(?i)supervisors?\s+" + _NAME_GROUP + r"\s+abstain(?:ed|ing)?", ctx):
        for ln in _parse_roll(m.group(1)):
            pos.setdefault(ln, "abstain")
    return pos


def _parse_items(text, doc_url):
    hs = _headings(text)
    rel = []
    for m in _KW_RE.finditer(text):
        pos = m.start()
        pre = text[max(0, pos - 160):pos].lower()
        if re.search(r"\b(motion|moved|seconded|resolution)\b", pre):
            rel.append((pos, m.group(1).lower()))
    rel.sort()

    clusters = []
    for pos, kw in rel:
        if clusters and pos - clusters[-1]["end"] < 160:
            clusters[-1]["kws"].append(kw)
            clusters[-1]["end"] = pos
        else:
            clusters.append({"start": pos, "end": pos, "kws": [kw]})

    items = {}
    for i, cl in enumerate(clusters):
        hb = _heading_before(hs, cl["start"])
        if hb is None:
            continue
        key, htitle = hb
        if _is_procedural(htitle):
            continue
        kws = set(cl["kws"])
        result = "fail" if (kws & _FAIL) else "pass"

        prev_end = clusters[i - 1]["end"] if i > 0 else 0
        next_start = clusters[i + 1]["start"] if i + 1 < len(clusters) else len(text)
        cs = max(cl["start"] - 140, prev_end)
        ce = min(cl["end"] + 150, next_start)
        ctx = re.sub(r"\s+", " ", text[cs:ce])

        positions = _extract_positions(ctx)
        if re.search(r"(?i)unanimous", ctx):
            unanimous = True
        elif any(v == "no" for v in positions.values()):
            unanimous = False
        else:
            unanimous = None

        entry = {
            "title": htitle,
            "result": result,
            "counts": None,
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
    return {
        "attendance": {"present": present, "absent": absent},
        "items": _parse_items(text, doc_url),
    }


# ----------------------------------------------------------------------------
# main
# ----------------------------------------------------------------------------
def extract(rt, args):
    max_meetings = int(args[0])

    def fetch(url):
        try:
            return rt.fetch_text(url)
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
        for title, link in _parse_rss_items(fetch(_rss_url(startid))):
            if "minutes" in title.lower():
                link = html.unescape(link)
                m = re.search(r"[?&]id=(\d+)", link)
                if m:
                    return _edoc_url(m.group(1))
        return None

    store_desc = sorted(set(STORE_MEETINGS), reverse=True)

    located = []
    without_document = []

    for d in store_desc:
        try:
            url = locate(d)
        except Exception:
            url = None
        if url:
            located.append((d, url))
        else:
            without_document.append(d)

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
            assertions[d] = _parse_meeting(text, url)
        except Exception:
            without_document.append(d)
            continue
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
