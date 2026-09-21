"""Independent second-source assertion extractor for `stafford-bos`.

Second source: Hyland OnBase — Stafford County Government Public Records
Online Portal (https://pob.staffordcountyva.gov/PublicAccess/), which
independently hosts adopted Board of Supervisors Minutes (with per-item
vote records) that are produced separately from the CivicClerk primary.

This module NEVER touches the CivicClerk primary data endpoints. It reads
vote/attendance facts only from OnBase minutes documents. Where OnBase does
not expose (or we cannot locate) a document for a store meeting, that date
is reported honestly in run_meta.meetings_without_document — never faked.
"""

import re
import datetime

EXTRACTOR_VERSION = "1"

SOURCE_ID = "stafford-bos-oracle"

# Second source (OnBase Public Access Viewer) — the ONLY source we read from.
SECOND_SOURCE_BASE = "https://pob.staffordcountyva.gov/PublicAccess/"

# Store meetings the primary extracted; our assertions/coverage must speak to
# each of these (either cover it or list it as document-less).
STORE_MEETINGS = [
    "2026-06-16", "2026-06-02", "2026-05-26", "2026-05-19", "2026-05-05",
    "2026-04-28", "2026-04-21", "2026-04-16", "2026-04-07", "2026-03-24",
    "2026-03-03", "2026-02-03", "2026-01-20", "2026-01-06", "2025-12-16",
    "2025-12-02", "2025-11-18", "2025-11-06",
]

# Words that are titles/labels, not surnames.
_TITLES = {
    "supervisor", "supervisors", "chairman", "chair", "vice", "vice-chairman",
    "vicechairman", "mr", "mrs", "ms", "dr", "the", "and", "none", "county",
    "board", "members", "member", "present", "absent", "also", "district",
}


# --------------------------------------------------------------------------
# Safe I/O wrappers (all network goes through rt; never crash the pipeline).
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
# Date normalization helpers.
# --------------------------------------------------------------------------
def _to_iso(s):
    if not isinstance(s, str):
        return None
    s = s.strip()
    m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", s)
    if m:
        return "%04d-%02d-%02d" % (int(m.group(3)), int(m.group(1)), int(m.group(2)))
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return "%s-%s-%s" % (m.group(1), m.group(2), m.group(3))
    return None


# --------------------------------------------------------------------------
# Best-effort location of OnBase minutes documents.
#
# OnBase Public Access is a JS/form portal with no documented unauthenticated
# JSON API (per the source profile). We attempt a few plausible query
# endpoints defensively; anything that fails simply yields no index entry,
# which correctly routes the affected meeting to meetings_without_document.
# --------------------------------------------------------------------------
def _find_date_value(d):
    for k, v in d.items():
        if isinstance(v, str) and re.search(r"date", str(k), re.I):
            iso = _to_iso(v)
            if iso:
                return iso
    return None


def _abs_url(u):
    if not isinstance(u, str):
        return None
    if u.startswith("http"):
        return u
    return SECOND_SOURCE_BASE + u.lstrip("/")


def _harvest_docs(obj, index):
    stack = [obj]
    guard = 0
    while stack and guard < 100000:
        guard += 1
        cur = stack.pop()
        if isinstance(cur, dict):
            date = _find_date_value(cur)
            url = cur.get("url") or cur.get("link") or cur.get("href")
            docid = (cur.get("id") or cur.get("documentId") or cur.get("docId")
                     or cur.get("handle"))
            if date:
                if url:
                    au = _abs_url(url)
                    if au:
                        index.setdefault(date, au)
                elif docid is not None:
                    index.setdefault(
                        date,
                        SECOND_SOURCE_BASE + "GetDocument?docId=" + str(docid),
                    )
            for v in cur.values():
                if isinstance(v, (dict, list)):
                    stack.append(v)
        elif isinstance(cur, list):
            for v in cur:
                if isinstance(v, (dict, list)):
                    stack.append(v)


def _build_minutes_index(rt):
    """Return {iso_date: doc_url} for Board of Supervisors minutes, or {}."""
    index = {}
    # Touch the landing page (navigation/listing use is permitted) — the
    # public portal is a SPA, so this typically yields no document links.
    _safe_text(rt, SECOND_SOURCE_BASE)

    candidate_endpoints = (
        "api/document/query",
        "CustomQuery/GetResults",
        "api/documents",
        "api/query",
    )
    query_params = {
        "docType": "Board of Supervisors Minutes",
        "documentType": "Board of Supervisors Minutes",
    }
    for path in candidate_endpoints:
        data = _safe_json(rt, SECOND_SOURCE_BASE + path, query_params)
        if isinstance(data, (list, dict)):
            _harvest_docs(data, index)
    return index


# --------------------------------------------------------------------------
# Minutes text parsing.
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
        text, re.I | re.S,
    )
    if mp:
        present = _last_names(mp.group(1))
    ma = re.search(
        r"\bABSENT\b\s*:?\s*(.+?)(?=\n\s*\n|\bALSO\b|\bPRESENT\b)",
        text, re.I | re.S,
    )
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
            r"\b" + label + r"\b\s*[:\-]?\s*([A-Z][^\n.;]*)", ctx
        ):
            frag = m.group(1)
            # Skip pure numeric tallies (e.g. "Nays: 0").
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

        if re.search(r"denied|failed|defeated", word):
            result = "fail"
        else:
            result = "pass"

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
                quote, re.I,
            )
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
        in_index = date in index
        if covered < max_meetings:
            text = _safe_text(rt, index[date]) if in_index else None
            if not text:
                # Document does not exist or could not be located/retrieved.
                without.append(date)
                continue
            present, absent = _parse_attendance(text)
            items = _parse_items(text, index[date])
            assertions[date] = {
                "attendance": {"present": present, "absent": absent},
                "items": items,
            }
            covered += 1
        else:
            # We already have enough covered meetings; only meetings whose
            # document truly cannot be located are reported here.
            if not in_index:
                without.append(date)

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
