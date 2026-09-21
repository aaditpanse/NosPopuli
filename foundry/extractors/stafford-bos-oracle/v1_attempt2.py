"""Independent second-source assertion extractor for `stafford-bos`.

Second source: Hyland OnBase — Stafford County Government Public Records
Online Portal (https://pob.staffordcountyva.gov/PublicAccess/), which
independently hosts adopted Board of Supervisors Minutes (with per-item
vote records) produced separately from the CivicClerk primary.

This module NEVER touches the CivicClerk primary data endpoints. It reads
vote/attendance facts only from OnBase minutes documents. Navigation /
listing endpoints are used solely to locate documents. Where no OnBase
document can be located for a store meeting, that date is reported honestly
in run_meta.meetings_without_document.
"""

import json
import re

EXTRACTOR_VERSION = "1"

SOURCE_ID = "stafford-bos-oracle"

# Second source (OnBase Public Access Viewer) — the ONLY source we read from.
OB_BASE = "https://pob.staffordcountyva.gov/PublicAccess/"
OB_HOST = "https://pob.staffordcountyva.gov/"

# Store meetings the primary extracted; our assertions/coverage must speak to
# each of these (cover it, or list it as document-less).
STORE_MEETINGS = [
    "2026-06-16", "2026-06-02", "2026-05-26", "2026-05-19", "2026-05-05",
    "2026-04-28", "2026-04-21", "2026-04-16", "2026-04-07", "2026-03-24",
    "2026-03-03", "2026-02-03", "2026-01-20", "2026-01-06", "2025-12-16",
    "2025-12-02", "2025-11-18", "2025-11-06",
]

_TITLES = {
    "supervisor", "supervisors", "chairman", "chair", "vice", "vice-chairman",
    "vicechairman", "mr", "mrs", "ms", "dr", "the", "and", "none", "county",
    "board", "members", "member", "present", "absent", "also", "district",
    "of", "a", "an", "for", "to", "by",
}

_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12, "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}


# --------------------------------------------------------------------------
# Safe I/O wrappers.
# --------------------------------------------------------------------------
def _safe_json(rt, url, params=None):
    try:
        return rt.fetch_json(url, params)
    except Exception:
        return None


def _safe_text(rt, url, params=None):
    try:
        return rt.fetch_text(url, params)
    except Exception:
        return None


# --------------------------------------------------------------------------
# Date helpers.
# --------------------------------------------------------------------------
def _iso(y, m, d):
    return "%04d-%02d-%02d" % (int(y), int(m), int(d))


def _extract_any_date(s):
    if not isinstance(s, str):
        return None
    m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", s)
    if m:
        return _iso(m.group(3), m.group(1), m.group(2))
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return _iso(m.group(1), m.group(2), m.group(3))
    m = re.search(r"([A-Za-z]{3,9})\.?\s+(\d{1,2}),?\s+(\d{4})", s)
    if m and m.group(1).lower() in _MONTHS:
        return _iso(m.group(3), _MONTHS[m.group(1).lower()], m.group(2))
    return None


def _abs_url(u):
    if not isinstance(u, str):
        return None
    u = u.strip()
    if u.startswith("http"):
        return u
    if u.startswith("/"):
        return OB_HOST.rstrip("/") + u
    return OB_BASE + u


# --------------------------------------------------------------------------
# Locate OnBase minutes documents (listing/search only — never primary).
# --------------------------------------------------------------------------
def _find_date_value(d):
    for k, v in d.items():
        if isinstance(v, str) and re.search(r"date", str(k), re.I):
            iso = _extract_any_date(v)
            if iso:
                return iso
    return None


def _harvest_json(obj, index):
    stack = [obj]
    guard = 0
    while stack and guard < 200000:
        guard += 1
        cur = stack.pop()
        if isinstance(cur, dict):
            date = _find_date_value(cur)
            url = cur.get("url") or cur.get("link") or cur.get("href") \
                or cur.get("downloadUrl") or cur.get("documentUrl")
            docid = (cur.get("id") or cur.get("documentId") or cur.get("docId")
                     or cur.get("handle") or cur.get("documentHandle"))
            if date:
                if url and _abs_url(url):
                    index.setdefault(date, _abs_url(url))
                elif docid is not None:
                    index.setdefault(
                        date,
                        OB_BASE + "GetDocument?docId=" + str(docid),
                    )
            for v in cur.values():
                if isinstance(v, (dict, list)):
                    stack.append(v)
        elif isinstance(cur, list):
            for v in cur:
                if isinstance(v, (dict, list)):
                    stack.append(v)


def _harvest_html(html, index):
    for m in re.finditer(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', html,
                         re.I | re.S):
        href = m.group(1)
        label = re.sub(r"<[^>]+>", " ", m.group(2))
        iso = _extract_any_date(href) or _extract_any_date(label)
        if iso and re.search(
                r"minute|\.pdf|docpop|document|getfile|getdocument|stream",
                href, re.I):
            au = _abs_url(href)
            if au:
                index.setdefault(iso, au)
    for row in re.finditer(r"<tr[^>]*>(.*?)</tr>", html, re.I | re.S):
        rowhtml = row.group(1)
        iso = _extract_any_date(rowhtml)
        hm = re.search(r'href="([^"]+)"', rowhtml, re.I)
        if iso and hm:
            au = _abs_url(hm.group(1))
            if au:
                index.setdefault(iso, au)


def _harvest_text(rt, url, params, index):
    data = _safe_json(rt, url, params)
    if isinstance(data, (list, dict)):
        _harvest_json(data, index)
    txt = _safe_text(rt, url, params)
    if txt:
        stripped = txt.strip()
        if stripped[:1] in ("{", "["):
            try:
                _harvest_json(json.loads(stripped), index)
            except Exception:
                pass
        if "<" in txt:
            _harvest_html(txt, index)


def _build_minutes_index(rt):
    """Return {iso_date: doc_url} for Board of Supervisors minutes."""
    index = {}
    dt = "Board of Supervisors Minutes"
    params_variants = (
        {"docType": dt, "documentType": dt, "keyword": dt},
        {"q": "Board of Supervisors Minutes"},
        None,
    )
    endpoints = (
        OB_BASE,
        OB_BASE + "api/documents",
        OB_BASE + "api/document/query",
        OB_BASE + "api/customqueries",
        OB_BASE + "api/customquery/execute",
        OB_BASE + "api/publicaccess/documents",
        OB_BASE + "CustomQuery/GetResults",
        OB_BASE + "CustomQuery",
        OB_BASE + "search",
        OB_BASE + "api/search",
    )
    for ep in endpoints:
        for pv in params_variants:
            _harvest_text(rt, ep, pv, index)
    return index


def _candidate_doc_urls(date):
    y, m, d = date.split("-")
    mdy = "%s/%s/%s" % (m, d, y)
    dt = "Board of Supervisors Minutes"
    return [
        (OB_BASE + "docpop/docpop.aspx",
         {"clienttype": "html", "dt": dt, "KT_Meeting_Date": mdy}),
        (OB_HOST + "AppNet/docpop/docpop.aspx",
         {"clienttype": "html", "dt": dt, "KT_Meeting_Date": mdy}),
        (OB_BASE + "api/documents",
         {"documentType": dt, "date": mdy}),
        (OB_BASE + "GetDocument", {"docType": dt, "date": mdy}),
    ]


def _looks_like_minutes(text):
    if not text or len(text) < 200:
        return False
    low = text.lower()
    hits = sum(1 for kw in ("board of supervisors", "minutes", "motion",
                            "present", "supervisor", "carried", "aye")
               if kw in low)
    return hits >= 2


def _locate_text(rt, date, index):
    url = index.get(date)
    if url:
        t = _safe_text(rt, url)
        if _looks_like_minutes(t):
            return url, t
    for u, p in _candidate_doc_urls(date):
        t = _safe_text(rt, u, p)
        if _looks_like_minutes(t):
            return (u if not p else u), t
    return None, None


# --------------------------------------------------------------------------
# Minutes parsing.
# --------------------------------------------------------------------------
def _last_names(fragment):
    if not fragment:
        return []
    frag = fragment.strip()
    if re.match(r"^\s*none\b", frag, re.I):
        return []
    names = []
    for chunk in re.split(r",|\band\b|;", frag):
        words = re.findall(r"[A-Z][A-Za-z'\-]+", chunk)
        words = [w for w in words if w.lower() not in _TITLES]
        if words:
            names.append(words[-1])
    seen = set()
    out = []
    for n in names:
        if n not in seen:
            seen.add(n)
            out.append(n)
    return out


def _parse_attendance(text):
    present, absent = [], []
    mp = re.search(
        r"\bPRESENT\b\s*:?\s*(.+?)(?=\bABSENT\b|\bALSO\b|\n\s*\n)",
        text, re.I | re.S)
    if mp:
        present = _last_names(mp.group(1))
    ma = re.search(
        r"\bABSENT\b\s*:?\s*(.+?)(?=\n\s*\n|\bALSO\b|\bPRESENT\b)",
        text, re.I | re.S)
    if ma:
        absent = _last_names(ma.group(1))
    return present, absent


def _parse_counts(ctx):
    counts = {}
    for label, key in (
        ("ayes", "aye"), ("aye", "aye"),
        ("nays", "no"), ("noes", "no"), ("nay", "no"),
        ("abstentions", "abstain"), ("abstained", "abstain"),
        ("abstain", "abstain"),
        ("absent", "absent"),
        ("recused", "recused"),
    ):
        m = re.search(r"\b" + label + r"\b\s*[:\-]?\s*(\d+)", ctx, re.I)
        if m and key not in counts:
            counts[key] = int(m.group(1))
    return counts or None


def _parse_positions(ctx):
    pos = {}
    for label, stance in (
        ("nays", "no"), ("noes", "no"), ("nay", "no"),
        ("abstentions", "abstain"), ("abstaining", "abstain"),
        ("abstained", "abstain"), ("abstain", "abstain"),
        ("recused", "recused"), ("recusing", "recused"),
    ):
        for m in re.finditer(
                r"\b" + label + r"\b\s*[:\-]?\s*([A-Z][^\n.;]*)", ctx):
            frag = m.group(1)
            if re.match(r"^\s*\d", frag):
                continue
            for nm in _last_names(frag):
                pos.setdefault(nm, stance)
    return pos


_RESULT_RE = re.compile(
    r"([^.\n][^.]*?\b"
    r"(carried|approved|adopted|passed|denied|failed|defeated|"
    r"was\s+approved|was\s+adopted|was\s+denied)\b[^.]*\.)",
    re.I,
)


def _motions_in_segment(segment, heading, doc_url):
    entries = []
    seen = set()
    for m in _RESULT_RE.finditer(segment):
        quote = m.group(1).strip()
        word = m.group(2).lower()
        ctx = segment[m.start():min(len(segment), m.end() + 300)]

        result = "fail" if re.search(r"denied|failed|defeated", word) else "pass"
        counts = _parse_counts(ctx)
        positions = _parse_positions(ctx)

        unanimous = None
        if re.search(r"unanimous", ctx, re.I):
            unanimous = True
        elif counts and counts.get("no", 0) > 0:
            unanimous = False

        title = heading
        if entries:
            subj = re.search(
                r"\bto\s+(.{4,80}?)(?:,|\bwas\b|\bby the following\b)",
                quote, re.I)
            if subj:
                title = subj.group(1).strip()

        q = quote[:400]
        dedupe = (title, q)
        if dedupe in seen:
            continue
        seen.add(dedupe)

        entries.append({
            "title": title,
            "result": result,
            "counts": counts,
            "positions": positions,
            "unanimous": unanimous,
            "evidence": {"quote": q, "doc_url": doc_url},
        })
    return entries


_HEADER_RE = re.compile(
    r"(?m)^[ \t]*(\d{1,2}(?:\.[A-Za-z])?)[\.\)]?[ \t]+(\S.*)$"
)


def _parse_items(text, doc_url):
    items = {}
    headers = list(_HEADER_RE.finditer(text))
    for i, h in enumerate(headers):
        num = h.group(1)
        heading = h.group(2).strip()
        start = h.end()
        end = headers[i + 1].start() if i + 1 < len(headers) else len(text)
        segment = text[start:end]
        entries = _motions_in_segment(segment, heading, doc_url)
        if entries:
            key = num.lower().rstrip(")")
            items.setdefault(key, []).extend(entries)
    return items


# --------------------------------------------------------------------------
# Entry point.
# --------------------------------------------------------------------------
def extract(rt, args):
    max_meetings = int(args[0]) if args else 0

    index = _build_minutes_index(rt)

    dates = sorted(set(STORE_MEETINGS), reverse=True)
    assertions = {}
    without = []
    covered = 0

    for date in dates:
        if covered >= max_meetings:
            # Only need to determine document existence for reporting.
            doc_url, text = _locate_text(rt, date, index)
            if not text:
                without.append(date)
            continue

        doc_url, text = _locate_text(rt, date, index)
        if not text:
            without.append(date)
            continue

        present, absent = _parse_attendance(text)
        items = _parse_items(text, doc_url)
        assertions[date] = {
            "attendance": {"present": present, "absent": absent},
            "items": items,
        }
        covered += 1

    entries = sum(
        len(entry_list)
        for a in assertions.values()
        for entry_list in a["items"].values()
    )

    run_meta = {
        "source_id": SOURCE_ID,
        "extractor_version": EXTRACTOR_VERSION,
        "row_counts": {"meetings": len(assertions), "entries": entries},
        "meetings_without_document": without,
    }
    return assertions, run_meta
