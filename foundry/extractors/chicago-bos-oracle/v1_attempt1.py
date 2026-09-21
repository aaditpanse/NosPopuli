"""Independent second-source assertion extractor for chicago-bos.

Second source: the Chicago City Clerk "Journal of the Proceedings of the City
Council" — the official statutory record, published as PDFs on public AWS S3
and indexed at chicityclerk.com.  This module NEVER touches the eLMS primary
API; it reads outcomes only from the Journal documents so it can independently
affirm or contradict the primary store.
"""

import re
from urllib.parse import quote

EXTRACTOR_VERSION = "1"

# Meetings the primary already extracted; our assertions must cover these.
STORE_MEETINGS = ["2026-07-15", "2026-06-17", "2026-05-20"]

# Second-source locations (navigation/index + document host).  These are NOT
# primary data endpoints.
INDEX_URL = ("https://www.chicityclerk.com/legislation-records/"
             "journals-and-reports/journals-proceedings")
S3_BASE = "https://chicityclerk.s3.us-west-2.amazonaws.com"
S3_PREFIX = "/s3fs-public-1/reports/"

DASH = r"[—–\-]{1,2}"

TALLY_RE = re.compile(
    r"Yeas\s*" + DASH + r"\s*(?P<ynames>.*?)"
    r"(?:\s*" + DASH + r"\s*(?P<yc>\d+))?\s*\.\s*"
    r"Nays\s*" + DASH + r"\s*(?P<nnames>.*?)"
    r"(?:\s*" + DASH + r"\s*(?P<nc>\d+))?\s*\.",
    re.S | re.I,
)

PRESENT_RE = re.compile(
    r"Present\s*" + DASH + r"\s*(?:Alderm[ae]n\s+)?(?P<names>.*?)\s*"
    + DASH + r"\s*\d+",
    re.S | re.I,
)
ABSENT_RE = re.compile(
    r"Absent\s*" + DASH + r"\s*(?:Alderm[ae]n\s+)?(?P<names>.*?)\s*\.",
    re.S | re.I,
)

FILE_NO_RE = re.compile(r"\b([A-Z]{1,3}\d{4}-\d{2,7})\b")
AGENDA_NO_RE = re.compile(r"(?m)^\s*(\d{1,3}(?:\.[A-Za-z])?)[\)\.]")

PASS_WORDS = ("was passed", "were passed", "was adopted", "were adopted",
              "prevailed", "was concurred", "carried", "do pass",
              "was approved", "was agreed to")
FAIL_WORDS = ("failed", "was lost", "was not passed", "were not passed",
              "was rejected", "did not prevail", "was defeated")


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _clean(s):
    return re.sub(r"\s+", " ", s or "").strip()


def _us_token(iso):
    y, m, d = iso.split("-")
    return "%s_%s_%s" % (y, m, d)


def _is_journal(text):
    if not text or len(text) < 2000:
        return False
    t = text.lower()
    return "journal" in t and "proceedings" in t


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
        if not p:
            continue
        if re.fullmatch(r"\d+", p):
            continue
        if len(p) > 40:  # runaway capture, not a name
            continue
        out.append(p)
    return out


def _locate_urls(rt, iso, index_pdfs):
    """Return (candidate_urls, listed_in_index)."""
    token = _us_token(iso)
    listed = [u for u in index_pdfs if token in u]
    if listed:
        return listed, True
    # Fallback: construct the documented S3 paths.
    names = [
        "%s Journal of the Proceedings.pdf" % token,
        "%s Journal of the Proceedings Vol. I.pdf" % token,
        "%s Journal of the Proceedings Vol. II.pdf" % token,
    ]
    return [S3_BASE + S3_PREFIX + quote(n) for n in names], False


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
        if "journal" in u.lower() and u not in seen:
            seen.add(u)
            out.append(u)
    return out


def _result_and_unanimous(ctx, yc, nc):
    low = ctx.lower()
    result = None
    if any(w in low for w in PASS_WORDS):
        result = "pass"
    elif any(w in low for w in FAIL_WORDS):
        result = "fail"
    if result is None:
        result = "pass" if yc >= nc else "fail"
    if "unanim" in low:
        unanimous = True
    elif nc > 0:
        unanimous = False
    else:
        unanimous = None
    return result, unanimous


def _item_key(context, counter):
    fnos = FILE_NO_RE.findall(context)
    if fnos:
        return fnos[-1].lower().rstrip(")")
    anos = AGENDA_NO_RE.findall(context)
    if anos:
        return anos[-1].lower().rstrip(")")
    return "item%d" % counter


def _parse_document(text, doc_url):
    entries = []
    counter = 0
    for m in TALLY_RE.finditer(text):
        counter += 1
        ynames = _split_names(m.group("ynames"))
        nnames = _split_names(m.group("nnames"))
        yc = int(m.group("yc")) if m.group("yc") else len(ynames)
        nc = int(m.group("nc")) if m.group("nc") else len(nnames)

        pre = text[max(0, m.start() - 600):m.start()]
        result, unanimous = _result_and_unanimous(pre + " " + text[m.start():m.end()],
                                                   yc, nc)

        positions = {}
        for n in ynames:
            positions[n] = "aye"
        for n in nnames:
            positions[n] = "no"

        title = _clean(pre[-240:]) or _clean(text[m.start():m.end()])[:200]
        key = _item_key(pre, counter)

        qs = max(0, m.start() - 160)
        qe = min(len(text), m.start() + 220)
        quote_txt = text[qs:qe]
        if len(quote_txt) > 400:
            quote_txt = quote_txt[:400]

        entries.append((key, {
            "title": title,
            "result": result,
            "counts": {"aye": yc, "no": nc},
            "positions": positions,
            "unanimous": unanimous,
            "evidence": {"quote": quote_txt, "doc_url": doc_url},
        }))
    return entries


def _parse_attendance(text):
    present, absent = [], []
    mp = PRESENT_RE.search(text)
    if mp:
        present = _split_names(mp.group("names"))
    ma = ABSENT_RE.search(text)
    if ma:
        absent = _split_names(ma.group("names"))
    return {"present": present, "absent": absent}


def _fetch_meeting_docs(rt, urls):
    """Fetch and validate journal docs; return list of (url, text)."""
    docs = []
    for u in urls:
        try:
            txt = rt.fetch_text(u)
        except Exception:
            continue
        if _is_journal(txt):
            docs.append((u, txt))
    return docs


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
        urls, listed = _locate_urls(rt, iso, index_pdfs)

        if covered >= max_meetings:
            # Already have enough coverage; only determine existence cheaply.
            if listed:
                continue
            docs = _fetch_meeting_docs(rt, urls)
            if not docs:
                without_document.append(iso)
            continue

        docs = _fetch_meeting_docs(rt, urls)
        if not docs:
            without_document.append(iso)
            continue

        attendance = {"present": [], "absent": []}
        items = {}
        for url, text in docs:
            att = _parse_attendance(text)
            if not attendance["present"] and (att["present"] or att["absent"]):
                attendance = att
            for key, entry in _parse_document(text, url):
                items.setdefault(key, []).append(entry)

        if not items:
            # Document exists but records no gradable outcome we can parse.
            # It still counts as "having a document" — do not mark missing.
            assertions[iso] = {"attendance": attendance, "items": {}}
            covered += 1
            continue

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
