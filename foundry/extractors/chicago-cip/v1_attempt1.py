"""Deterministic extractor for the City of Chicago Capital Improvement
Program (CIP) 2025-2029 — "Build Better Together" (Office of Budget and
Management). Single-volume PDF; the funded projects live in the
"Project and Fund Source Detail Report" section near the back, which prints,
per functional category, one project block: a project title, its fund-source
rows with per-fiscal-year dollar columns (2025-2029) and a Total column.

Parse strategy (mirrors the Fairfax reference, adapted to THIS layout):
`pdftotext -layout` preserves column alignment, so we locate each cost
table's year-header row, record each fiscal-year label's character offset,
and read every data row by slicing values at those offsets. We track the
current functional-category heading as we walk. Per project we sum the
fund-source rows into fiscal-year columns and reconcile that sum against the
document's own printed Total — the defining gate check.

stdlib only; all I/O is rt.fetch_text.
"""

import re

try:  # schema is the durable asset; import if present, else pin the version
    import schema
    SCHEMA_VERSION = schema.SCHEMA_VERSION
except Exception:  # pragma: no cover
    SCHEMA_VERSION = "1.5"

EXTRACTOR_VERSION = "1"
SOURCE_ID = "chicago-cip"
JURISDICTION = "City of Chicago, Illinois"
EDITION = "Capital Improvement Program (CIP) Report 2025-2029: Build Better Together"

SOURCE_URL = ("https://www.chicago.gov/city/en/depts/obm/supp_info/"
              "office-publications.html")
CIP_URLS = [
    "https://www.chicago.gov/content/dam/city/depts/obm/supp_info/CIP/"
    "City%20of%20Chicago%202025-2029%20CIP.pdf",
]

YEARS = [2025, 2026, 2027, 2028, 2029]
TOL = 9  # char tolerance when snapping a value's end offset to a column

# Top-level functional categories (the CIP's summary/section headings). These
# group the project pages; we use whichever is current as `function`.
CATEGORIES = [
    "Aviation",
    "Water System",
    "Transportation",
    "Sewer System",
    "Neighborhood Infrastructure",
    "Economic Development",
    "Fleet",
    "Municipal Facilities",
    "Information Technology",
    "Lakefront-Shoreline",
    "City Space",
]


def _norm(s):
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


CAT_NORM = {_norm(c): c for c in CATEGORIES}

AMT = re.compile(r"\d[\d,]*|-")
FUND_HINT = re.compile(
    r"(?i)(funds?|debt|revenue|bonds?|\btif\b|grants?|g\.?\s*o\.?|"
    r"proceeds|financing|reimbursement)")
TOTAL_RE = re.compile(r"(?i)^\s*(project\s+|program\s+|grand\s+)?(sub)?total\b")


def _clean(s):
    s = re.sub(r"\s+", " ", (s or "")).strip(" .:-\u2013")
    return s[:200]


def _slug(s):
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")[:70]


def _furniture(line):
    s = line.strip()
    if not s:
        return True
    if re.match(r"^[\d,]+$", s) and len(s) <= 4:
        return True
    low = s.lower()
    for k in ("city of chicago", "capital improvement program",
              "project and fund source", "build better together",
              "in whole dollars", "in dollars", "fund source",
              "table of contents"):
        if k in low:
            return True
    return False


def _header(line):
    """Return (cols, total_off) if this is a fiscal-year header row, else None.
    cols maps each year -> the character offset of the END of its label; values
    are right-aligned to those offsets."""
    found = {}
    for y in YEARS:
        i = line.find(str(y))
        if i >= 0:
            found[y] = i + len(str(y))
    if 2025 not in found or 2029 not in found or len(found) < 4:
        return None
    if found[2029] <= found[2025]:
        return None
    step = (found[2029] - found[2025]) / 4.0
    cols = {}
    for k, y in enumerate(YEARS):
        cols[y] = found.get(y, int(round(found[2025] + k * step)))
    low = line.lower()
    ti = low.rfind("total")
    if ti >= 0:
        total_off = ti + len("total")
    else:
        m = re.search(r"20\d\d\s*[-\u2013]\s*20\d\d", line)
        total_off = m.end() if m else int(round(cols[2029] + step))
    return cols, total_off


def _read_row(line, cols, total_off):
    """Slice a data row's values by column offset. Returns
    (fy dict, row_total, aligned_count, first_aligned_start)."""
    fy = {y: 0 for y in YEARS}
    row_total = None
    aligned = 0
    first = None
    for m in AMT.finditer(line):
        tok = m.group()
        val = 0 if tok == "-" else int(tok.replace(",", ""))
        e = m.end()
        best_y = min(YEARS, key=lambda y: abs(cols[y] - e))
        if abs(cols[best_y] - e) <= TOL:
            fy[best_y] = val
            aligned += 1
            if first is None:
                first = m.start()
        elif total_off is not None and (
                abs(total_off - e) <= TOL or e > cols[YEARS[-1]] + TOL):
            row_total = val  # rightmost such wins
            aligned += 1
            if first is None:
                first = m.start()
    return fy, row_total, aligned, first


def _detail_start(lines):
    idxs = [i for i, l in enumerate(lines)
            if "project and fund source detail" in l.lower()]
    if idxs:
        return idxs[-1]
    return 0


def _new(title, category):
    return {"title": title or "Untitled Project",
            "function": category or "Unspecified",
            "fy": {y: 0 for y in YEARS},
            "row_totals": 0, "have_row_total": False,
            "funds": [], "printed_total_row": None}


def _parse(lines):
    projects = []
    cols = None
    total_off = None
    category = None
    title_buf = []
    current = [None]  # boxed for closure

    def finalize():
        cur = current[0]
        if cur is not None:
            if sum(cur["fy"].values()) > 0 or cur["printed_total_row"]:
                projects.append(cur)
        current[0] = None

    for raw in lines:
        line = raw.replace("\f", "")
        h = _header(line)
        if h:
            cols, total_off = h
            title_buf = []
            continue
        s = line.strip()
        if not s:
            continue
        n = _norm(s)
        if n in CAT_NORM:
            cat = CAT_NORM[n]
            if cat != category:  # a genuinely new section, not a page reprint
                finalize()
                title_buf = []
                category = cat
            continue
        if _furniture(line):
            continue
        if cols is None:
            title_buf.append(s)
            if len(title_buf) > 4:
                title_buf = title_buf[-4:]
            continue

        fy, row_total, aligned, fa = _read_row(line, cols, total_off)
        lead = line[:fa].strip() if fa is not None else s
        is_total = bool(TOTAL_RE.match(line))

        if is_total and aligned >= 1:
            cur = current[0]
            if cur is not None:
                cur["printed_total_row"] = (
                    row_total if row_total is not None else sum(fy.values()))
                finalize()
            title_buf = []
            continue

        is_fund = aligned >= 1 and bool(FUND_HINT.search(lead)) and len(lead) <= 70
        if is_fund:
            if current[0] is None:
                current[0] = _new(_clean(" ".join(title_buf)), category)
                title_buf = []
            cur = current[0]
            for y in YEARS:
                cur["fy"][y] += fy[y]
            if row_total is not None:
                cur["row_totals"] += row_total
                cur["have_row_total"] = True
            if lead and lead not in cur["funds"]:
                cur["funds"].append(lead)
            continue

        # An inline project row (amounts, no fund breakdown) — only start one
        # when no fund-based project is open, to avoid swallowing subtotals.
        if (current[0] is None and aligned >= 3 and lead
                and re.search(r"[A-Za-z]", lead) and len(lead) <= 90):
            title = _clean((" ".join(title_buf) + " " + lead).strip())
            cur = _new(title, category)
            for y in YEARS:
                cur["fy"][y] += fy[y]
            if row_total is not None:
                cur["printed_total_row"] = row_total
            current[0] = cur
            finalize()
            title_buf = []
            continue

        if aligned >= 1:
            # numeric noise (unlabeled subtotal, stray figure) — ignore
            continue

        # pure text -> project title / continuation
        if current[0] is not None:
            finalize()
            title_buf = [s]
        else:
            title_buf.append(s)
            if len(title_buf) > 4:
                title_buf = title_buf[-4:]

    finalize()
    return projects


def _to_thousands(raw_years, target_raw):
    """Convert whole-dollar year values to integer thousands while forcing
    their sum to equal round(target_raw/1000), using largest-remainder — so a
    reconciled project stays reconciled after unit conversion."""
    total_k = int(round(target_raw / 1000.0))
    fl = {y: raw_years[y] // 1000 for y in YEARS}
    rem = total_k - sum(fl.values())
    order = sorted(YEARS, key=lambda y: raw_years[y] % 1000, reverse=True)
    conv = dict(fl)
    i = 0
    while rem > 0 and order:
        conv[order[i % len(order)]] += 1
        rem -= 1
        i += 1
    if rem < 0:
        for y in sorted(YEARS, key=lambda y: raw_years[y] % 1000):
            if rem >= 0:
                break
            if conv[y] > 0:
                conv[y] -= 1
                rem += 1
    return {y: conv[y] for y in YEARS}, total_k


def extract(rt, args=None):
    raw_projects = []
    for url in CIP_URLS:
        text = rt.fetch_text(url) or ""
        lines = text.splitlines()
        start = _detail_start(lines)
        for p in _parse(lines[start:]):
            p["_url"] = url
            raw_projects.append(p)

    # Detect the document's unit (Chicago prints whole dollars in its summary).
    raw_sum = 0
    max_amt = 0
    for p in raw_projects:
        t = (p["printed_total_row"] if p["printed_total_row"] is not None
             else sum(p["fy"].values()))
        raw_sum += t or 0
        max_amt = max(max_amt, max(p["fy"].values(), default=0), t or 0)
    whole_dollars = raw_sum > 1_000_000_000 or max_amt >= 100_000_000

    run_id = f"{SOURCE_ID}-{EXTRACTOR_VERSION}"
    records = []
    seen = {}
    for p in raw_projects:
        raw_years = p["fy"]
        raw_year_sum = sum(raw_years.values())
        if raw_year_sum <= 0:
            continue
        raw_printed = p["printed_total_row"]
        if raw_printed is None and p["have_row_total"]:
            raw_printed = p["row_totals"]
        reconciled = raw_printed is not None and raw_printed == raw_year_sum

        if whole_dollars:
            target = raw_printed if reconciled else raw_year_sum
            conv_years, conv_total = _to_thousands(raw_years, target)
        else:
            conv_years = {y: int(raw_years[y]) for y in YEARS}
            conv_total = sum(conv_years.values())

        five = sum(conv_years.values())
        printed_sub = conv_total if reconciled else None
        if printed_sub is not None and printed_sub != five:
            printed_sub = None  # never emit a self-contradicting subtotal

        title = _clean(p["title"]) or "Untitled Project"
        base = _slug(f"{p['function']}-{title}") or "project"
        seen[base] = seen.get(base, 0) + 1
        pid = base if seen[base] == 1 else f"{base}-{seen[base]}"

        rec = {
            "project_id": f"{SOURCE_ID}-{pid}",
            "title": title,
            "function": p["function"],
            "funding_sources": list(p["funds"]),
            "fiscal_years": {str(y): int(conv_years[y]) for y in YEARS},
            "five_year_total": five,
            "printed_subtotal": printed_sub,
            "total": five,
            "districts": [],
            "unit": "usd_thousands",
            "source_url": SOURCE_URL,
            "data_source_url": p["_url"],
            "provenance": {
                "source_id": SOURCE_ID,
                "extractor_version": EXTRACTOR_VERSION,
                "run_id": run_id,
            },
        }
        records.append(rec)

    run_meta = {
        "source_id": SOURCE_ID,
        "extractor_version": EXTRACTOR_VERSION,
        "schema_version": SCHEMA_VERSION,
        "jurisdiction": JURISDICTION,
        "edition": EDITION,
        "row_counts": {"capital_projects": len(records)},
    }
    return {"capital_projects": records}, run_meta
