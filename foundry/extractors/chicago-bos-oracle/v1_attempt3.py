"""Independent second-source assertion extractor for chicago-bos.

Second source: the Chicago City Clerk "Journal of the Proceedings of the City
Council" — the official statutory record, published as PDFs on public AWS S3
and indexed at chicityclerk.com.  This module NEVER touches the eLMS primary
API; every outcome is read from the Journal documents so it can independently
affirm or contradict the primary store.
"""

import re
from urllib.parse import quote

EXTRACTOR_VERSION = "1"

# Meetings the primary already extracted; our assertions must cover these.
STORE_MEETINGS = ["2026-07-15", "2026-06-17", "2026-05-20"]

# Second-source locations (navigation/index + document host).  NOT primary.
INDEX_URL = ("https://www.chicityclerk.com/legislation-records/"
             "journals-and-reports/journals-proceedings")
S3_BASE = "https://chicityclerk.s3.us-west-2.amazonaws.com"
S3_PREFIX = "/s3fs-public-1/reports/"

DASH = r"[—–\-]{1,2}"

# A printed agenda item heading:  "6. TITLE", "4.a) TITLE", "12.b. TITLE"
HEADING_RE = re.compile(
    r"(?m)^[ \t]*(?P<num>\d{1,3}(?:\.[A-Za-z])?)[\)\.][ \t]+(?P<title>\S.*)$"
)

# Roll-call tally block.
TALLY_RE = re.compile(
    r"Yeas\s*" + DASH + r"\s*(?P<ynames>.*?)"
    r"(?:\s*" + DASH + r"\s*(?P<yc>\d+))?\s*\.\s*"
    r"Nays\s*" + DASH + r"\s*(?P<nnames>.*?)"
    r"(?:\s*" + DASH + r"\s*(?P<nc>\d+))?\s*\.",
    re.S | re.I,
)

VOTE_NUM_RE = re.compile(
    r"vote of\s+(?P<a>\d+)\s+(?:to|" + DASH + r")\s+(?P<b>\d+)", re.I
)
AYES_NOES_RE = re.compile(
    r"(?P<a>\d+)\s*(?:ayes?|yeas?)\b.{0,25}?(?P<b>\d+)\s*(?:noes|nays?|nos)\b",
    re.I | re.S,
)

PASS_RE = re.compile(
    r"\b(passed|adopted|carried|prevailed|approved|concurred|"
    r"agreed to|was granted|do pass)\b", re.I,
)
FAIL_RE = re.compile(
    r"\b(failed|was lost|rejected|denied|defeated|"
    r"did not pass|not adopted|did not prevail)\b", re.I,
)

# Vocabulary the gate greps for — the evidence quote MUST contain one of these.
DECISION_RE = re.compile(
    r"\b(approved|denied|carried|prevailed|adopted|passed|failed|lost|"
    r"rejected|defeated|concurred|granted|moved|second(?:ed)?|"
    r"aye|ayes|yea|yeas|nay|nays|noes|unanim|do pass|agreed to)\b",
    re.I,
)

FILE_NO_RE = re.compile(r"\b([A-Z]{1,3}\d{4}-\d{2,7})\b")


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #
def _clean(s):
    return re.sub(r"\s+", " ", s or "").strip()


def _us_token(iso):
    y, m, d = iso.split("-")
    return "%s_%s_%s" % (y, m, d)


def _is_journal(text):
    if not text or len(text) < 500:
        return False
    low = text.lower()
    if "journal" in low or "proceedings" in low or "city council" in low:
        return True
    return bool(HEADING_RE.search(text) or TALLY_RE.search(text))


def _split_names(raw):
    raw = (raw or "").strip()
    if not raw or re.fullmatch(r"(?i)none\.?", raw):
        return []
    raw = re.sub(r"(?i)\bAlderm[ae]n\b", " ", raw)
    raw = re.sub(r"(?i)\bAld\.?\b", " ", raw)
    out = []
    for part in re.split(r",|\band\b", raw):
        p = part.strip().strip(".").strip()
        p = re.sub(r"\s+", " ", p)
        p = re.sub(r"\s*\d+$", "", p).strip()
        if not p or re.fullmatch(r"\d+", p) or len(p) > 40:
            continue
        out.append(p)
    return out


def _clean_title(t):
    t = _clean(t)
    t = re.sub(r"\s*\(\s*[A-Z]{1,3}\d{4}-\d+\s*\)\s*$", "", t)
    return t.strip()


# --------------------------------------------------------------------------- #
# document location
# --------------------------------------------------------------------------- #
def _index_pdfs(rt):
    try:
        html = rt.fetch_text(INDEX_URL)
    except Exception:
        return []
    if not html:
        return []
    urls = re.findall(r"https?://[^\s\"'<>]+\.pdf", html)
    seen, out = set(), []
    for u in urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def _constructed_urls(token):
    bases = [
        "%s Journal of the Proceedings.pdf",
        "%s Journal of the Proceedings Vol. I.pdf",
        "%s Journal of the Proceedings Vol. II.pdf",
        "%s Journal of the Proceedings Vol. III.pdf",
    ]
    return [S3_BASE + S3_PREFIX + quote(b % token) for b in bases]


def _candidate_urls(iso, index_pdfs):
    token = _us_token(iso)
    cands = [u for u in index_pdfs if token in u]
    for u in _constructed_urls(token):
        if u not in cands:
            cands.append(u)
    return cands


def _fetch_docs(rt, urls):
    docs = []
    seen = set()
    for u in urls:
        if u in seen:
            continue
        seen.add(u)
        try:
            txt = rt.fetch_text(u)
        except Exception:
            continue
        if _is_journal(txt):
            docs.append((u, txt))
    return docs


# --------------------------------------------------------------------------- #
# outcome parsing (per printed agenda item)
# --------------------------------------------------------------------------- #
def _parse_tally(block):
    """Return (counts, positions)."""
    m = TALLY_RE.search(block)
    if m:
        yn = _split_names(m.group("ynames"))
        nn = _split_names(m.group("nnames"))
        yc = int(m.group("yc")) if m.group("yc") else len(yn)
        nc = int(m.group("nc")) if m.group("nc") else len(nn)
        pos = {}
        for n in yn:
            pos[n] = "aye"
        for n in nn:
            pos[n] = "no"
        return {"aye": yc, "no": nc}, pos
    m = VOTE_NUM_RE.search(block)
    if m:
        return {"aye": int(m.group("a")), "no": int(m.group("b"))}, {}
    m = AYES_NOES_RE.search(block)
    if m:
        return {"aye": int(m.group("a")), "no": int(m.group("b"))}, {}
    return None, {}


def _parse_result(block):
    pm = PASS_RE.search(block)
    fm = FAIL_RE.search(block)
    if pm and not fm:
        return "pass", pm.start()
    if fm and not pm:
        return "fail", fm.start()
    if pm and fm:
        if fm.start() > pm.start():
            return "fail", fm.start()
        return "pass", pm.start()
    return None, None


def _outcome_for_block(block, doc_url, item_key, title):
    counts, positions = _parse_tally(block)
    result, kw_anchor = _parse_result(block)

    if result is None and counts is None:
        return None  # document is silent — assert nothing
    if result is None:
        result = "pass" if counts["aye"] >= counts["no"] else "fail"

    # Anchor the evidence quote on actual decision language so the gate can
    # grep an outcome word out of it.
    anchor = None
    if kw_anchor is not None:
        anchor = kw_anchor
    else:
        rm = TALLY_RE.search(block)
        if rm:
            anchor = rm.start()
        else:
            dm = DECISION_RE.search(block)
            if dm:
                anchor = dm.start()
    if anchor is None:
        return None  # cannot evidence the outcome with decision language

    low = block.lower()
    if "unanim" in low:
        unanimous = True
    elif counts is not None and counts.get("no", 0) > 0:
        unanimous = False
    elif counts is not None and counts.get("aye", 0) > 0 and counts.get("no", 0) == 0:
        unanimous = True
    else:
        unanimous = None

    qs = max(0, anchor - 140)
    qe = min(len(block), anchor + 260)
    quote = block[qs:qe]
    if len(quote) > 400:
        quote = quote[:400]

    if not DECISION_RE.search(quote):
        return None  # refuse to emit an unprovable outcome

    return {
        "title": title,
        "result": result,
        "counts": counts,
        "positions": positions,
        "unanimous": unanimous,
        "evidence": {"quote": quote, "doc_url": doc_url},
    }


def _parse_document(text, doc_url):
    """Segment by printed agenda number; attach each outcome to the item ABOVE."""
    items = {}
    heads = list(HEADING_RE.finditer(text))

    if heads:
        for i, h in enumerate(heads):
            b0 = h.end()
            b1 = heads[i + 1].start() if i + 1 < len(heads) else len(text)
            block = text[b0:b1]
            key = h.group("num").lower().rstrip(")")
            title = _clean_title(h.group("title")) or key
            entry = _outcome_for_block(block, doc_url, key, title)
            if entry is not None:
                items.setdefault(key, []).append(entry)
        return items

    # Fallback: no printed agenda numbering — key by legislative file number.
    for m in TALLY_RE.finditer(text):
        pre = text[max(0, m.start() - 1200):m.start()]
        fnos = FILE_NO_RE.findall(pre)
        if not fnos:
            continue
        key = fnos[-1].lower()
        block = text[max(0, m.start() - 400):min(len(text), m.end() + 100)]
        title = _clean_title(_clean(pre[-200:])) or key
        entry = _outcome_for_block(block, doc_url, key, title)
        if entry is not None:
            items.setdefault(key, []).append(entry)
    return items


def _parse_attendance(text):
    present, absent = [], []
    mp = re.search(
        r"Present\s*" + DASH + r"\s*(?:Alderm[ae]n\s+)?(?P<names>.*?)\s*"
        + DASH + r"\s*\d+", text, re.S | re.I,
    )
    if mp:
        present = _split_names(mp.group("names"))
    ma = re.search(
        r"Absent\s*" + DASH + r"\s*(?:Alderm[ae]n\s+)?(?P<names>.*?)\s*\.",
        text, re.S | re.I,
    )
    if ma:
        absent = _split_names(ma.group("names"))
    return {"present": present, "absent": absent}


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #
def extract(rt, args):
    try:
        max_meetings = int(args[0])
    except Exception:
        max_meetings = len(STORE_MEETINGS)

    dates = sorted(set(STORE_MEETINGS), reverse=True)
    index_pdfs = _index_pdfs(rt)

    assertions = {}
    without_document = []
    covered = 0

    for iso in dates:
        docs = _fetch_docs(rt, _candidate_urls(iso, index_pdfs))
        if not docs:
            without_document.append(iso)
            continue

        if covered >= max_meetings:
            continue  # has a document but beyond coverage window; not "missing"

        attendance = {"present": [], "absent": []}
        items = {}
        for url, text in docs:
            att = _parse_attendance(text)
            if not attendance["present"] and (att["present"] or att["absent"]):
                attendance = att
            for key, entries in _parse_document(text, url).items():
                items.setdefault(key, []).extend(entries)

        assertions[iso] = {"attendance": attendance, "items": items}
        covered += 1

    entry_count = 0
    for date in assertions:
        for key in assertions[date]["items"]:
            entry_count += len(assertions[date]["items"][key])

    run_meta = {
        "source_id": "chicago-bos-oracle",
        "extractor_version": EXTRACTOR_VERSION,
        "row_counts": {"meetings": len(assertions), "entries": entry_count},
        "meetings_without_document": sorted(set(without_document), reverse=True),
    }
    return assertions, run_meta
