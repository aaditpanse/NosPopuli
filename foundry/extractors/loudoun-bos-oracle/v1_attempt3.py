"""
Independent second-source assertion extractor for `loudoun-bos`.

Second source: the per-meeting Board *Minutes* PDFs published to Laserfiche
WebLink (produced by the Clerk, approved at a later meeting) — narrating each
motion independently of the Action Report the primary reads.

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
    "at", "hearing", "public", "moved", "seconded", "consent", "agenda",
}

PROC = [
    "adjourn", "recess", "closed session", "call to order", "roll call",
    "invocation", "pledge", "adoption of the agenda", "adoption of agenda",
    "approval of the minutes", "approval of minutes", "minutes of the",
    "approve the minutes", "certification of the closed",
]

_NAME_GROUP = r"([A-Z][A-Za-z\.\'\-]+(?:(?:,|,? and)\s+[A-Z][A-Za-z\.\'\-]+)*)"

OUTCOME_RE = re.compile(
    r"(?i)\b(?:the\s+)?"
    r"(?:main\s+|substitute\s+|amended\s+|original\s+|primary\s+)?"
    r"(?:motion|resolution|ordinance)\b[^.\n]{0,120}?"
    r"\b(carried|passed|prevailed|failed|defeated"
    r"|was\s+adopted|were\s+adopted|adopted\s+by|adopted\s*\("
    r"|was\s+approved|were\s+approved|approved\s+by|approved\s*\("
    r"|was\s+denied|were\s+denied|was\s+defeated)\b")

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
# headings
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


def _heading_before(hs, pos, integer_only=False):
    chosen = None
    for start, key, title in hs:
        if start < pos:
            if integer_only and ("." in key):
                continue
            chosen = (key, title)
        else:
            break
    return chosen


# ----------------------------------------------------------------------------
# vote detail
# ----------------------------------------------------------------------------
def _extract_counts(ctx):
    a = re.search(r"(?i)\bayes?\s*[:\-]\s*(\d{1,2})\b", ctx)
    n = re.search(r"(?i)\bnays?\s*[:\-]\s*(\d{1,2})\b", ctx)
    if a and n:
        c = {"aye": int(a.group(1)), "no": int(n.group(1))}
        ab = re.search(r"(?i)abstain(?:ed|ing|s|ers)?\s*[:\-]\s*(\d{1,2})\b", ctx)
        if ab:
            c["abstain"] = int(ab.group(1))
        av = re.search(r"(?i)\babsent\s*[:\-]\s*(\d{1,2})\b", ctx)
        if av:
            c["absent"] = int(av.group(1))
        return c
    d = re.search(
        r"(?i)(?:vote of|by a vote of|voted)\s*\(?\s*(\d{1,2})\s*-\s*(\d{1,2})\b", ctx)
    if not d:
        d = re.search(r"\(\s*(\d{1,2})\s*-\s*(\d{1,2})\s*\)", ctx)
    if d:
        return {"aye": int(d.group(1)), "no": int(d.group(2))}
    return None


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
    m = re.search(r"(?i)\bnays?\s*[:\-]\s*(supervisors?[^.;\n]{0,120})", ctx)
    if m and not re.search(r"(?i)\bnone\b", m.group(1)):
        for ln in _parse_roll(m.group(1)):
            pos[ln] = "no"
    m = re.search(r"(?i)\babstain[a-z]*\s*[:\-]\s*(supervisors?[^.;\n]{0,120})", ctx)
    if m and not re.search(r"(?i)\bnone\b", m.group(1)):
        for ln in _parse_roll(m.group(1)):
            pos.setdefault(ln, "abstain")
    return pos


def _make_quote(text, cstart, cend):
    s = max(0, cstart - 120)
    e = min(len(text), cend + 120)
    return text[s:e].strip()[:400]


def _parse_items(text, doc_url):
    hs = _headings(text)
    raw = [(m.start(), m.end(), m.group(1).lower()) for m in OUTCOME_RE.finditer(text)]
    raw.sort()

    clusters = []
    for s, e, v in raw:
        if clusters and s - clusters[-1]["end"] < 200:
            clusters[-1]["end"] = max(clusters[-1]["end"], e)
            clusters[-1]["verbs"].append(v)
        else:
            clusters.append({"start": s, "end": e, "verbs": [v]})

    items = {}
    seen = set()
    for cl in clusters:
        cstart, cend = cl["start"], cl["end"]

        # backward to this motion's own start (avoid bleeding into prior motion)
        back = text[max(0, cstart - 260):cstart]
        mstart = max(0, cstart - 60)
        last = None
        for mk in re.finditer(r"(?i)on\s+a\s+motion|on\s+motion|\bmoved\b", back):
            last = mk
        if last is not None:
            mstart = max(0, cstart - 260) + last.start()

        # forward boundary: stop at next motion / blank line / next heading
        after = text[cend:cend + 400]
        cut = len(after)
        for pat in [r"\n\s*\n", r"(?i)on\s+a\s+motion", r"(?i)on\s+motion"]:
            mm = re.search(pat, after)
            if mm:
                cut = min(cut, mm.start())
        boundary = cend + cut
        for st, k, t in hs:
            if cend < st < boundary:
                boundary = st
                break

        ctx = re.sub(r"\s+", " ", text[mstart:boundary])

        verbs = cl["verbs"]
        fail = any(("failed" in v or "defeated" in v or "denied" in v) for v in verbs)
        result = "fail" if fail else "pass"

        consent = bool(re.search(r"(?i)consent", ctx))
        hb = _heading_before(hs, cstart, integer_only=consent)
        if hb is None:
            hb = _heading_before(hs, cstart)
        if hb is None:
            continue
        key, htitle = hb
        if _is_procedural(htitle):
            continue

        counts = _extract_counts(ctx)
        positions = _extract_positions(ctx)
        if re.search(r"(?i)unanimous", ctx):
            unanimous = True
        elif any(v == "no" for v in positions.values()):
            unanimous = False
        else:
            unanimous = None

        quote = _make_quote(text, cstart, cend)
        sig = (key, result, quote)
        if sig in seen:
            continue
        seen.add(sig)

        items.setdefault(key, []).append({
            "title": htitle,
            "result": result,
            "counts": counts,
            "positions": positions,
            "unanimous": unanimous,
            "evidence": {"quote": quote, "doc_url": doc_url},
        })
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
        {x for x in without_document if x in set(STORE_MEETINGS)}, reverse=True)

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
