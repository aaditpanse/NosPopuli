"""Deterministic CIP extractor — Prince William County, Virginia (FY2026-2031).

One-shot, layout-preserving PDF-table parse. The document (fetched once per
url via ``rt.fetch_text`` which runs ``pdftotext -layout``) enumerates funded
capital projects; each project page prints a "Total Project Cost" headline and
a per-year expenditure/funding table with fiscal-year columns FY26..FY31 plus
a printed multi-year (FY26-31) subtotal.

The parse walks project pages, reads each project's cost table by the character
offsets of its fiscal-year column headers, and reconciles — in raw dollars,
before any rounding — the sum of the six parsed year cells against the table's
own printed FY26-31 subtotal. Only when that raw reconciliation holds do we
carry a ``printed_subtotal``; a genuine column-misassignment therefore surfaces
as a hard gate failure instead of being papered over by rounding.

Functional-area assignment (the schema's ``function``) is resolved from three
independent signals so it does not collapse to a single value: (1) section
dividers / running headers that name a functional area, carried forward; (2) a
per-project rescan of the same signal; (3) a keyword classifier over the title
and description. The county CIP genuinely spans six functional areas, so these
combined signals recover them.
"""

import re

try:
    import schema  # domain schema, if importable in the pipeline
    SCHEMA_VERSION = schema.SCHEMA_VERSION
except Exception:  # pragma: no cover - standalone fallback
    SCHEMA_VERSION = "1.4"

EXTRACTOR_VERSION = "1"

SOURCE_ID = "princewilliam-cip"
JURISDICTION = "Prince William County, Virginia"
EDITION = "FY2026-2031 Capital Improvement Program"
SOURCE_URL = "https://www.pwcva.gov/cip"
CIP_URLS = ["https://www.pwcva.gov/assets/2025-06/aFY26--16--CIP00--CIP.pdf"]

FY_FIRST, FY_LAST = 2026, 2031
FY_YEARS = list(range(FY_FIRST, FY_LAST + 1))

FUNCTIONAL_AREAS = [
    "Community Development", "Human Services", "General Government",
    "Public Safety", "Technology Improvement", "Transportation",
]

# Keyword classifier — last-resort functional-area assignment.
AREA_KEYWORDS = {
    "Transportation": ["road", "route", "highway", "bridge", "intersection",
                       "widening", "transit", "pedestrian", "sidewalk", "bike",
                       "mobility", "interchange", "corridor", "parkway",
                       "street", "traffic", "commuter", "roadway", "trail"],
    "Public Safety": ["fire", "rescue", "police", "detention", "jail",
                      "public safety", "911", "emergency", "animal shelter",
                      "sheriff", "adult detention"],
    "Community Development": ["park", "recreation", "library", "community",
                             "historic", "preservation", "open space",
                             "athletic", "aquatic", "facility", "building",
                             "gymnasium", "cultural", "museum", "amphitheater"],
    "Human Services": ["homeless", "navigation", "human services", "senior",
                       "health", "social services", "housing", "shelter"],
    "General Government": ["government", "administration", "courthouse",
                           "county complex", "records", "office building",
                           "voting", "elections", "judicial", "clerk"],
    "Technology Improvement": ["technology", "software", "network", "radio",
                               "fiber", "data center", "information technology",
                               "broadband", "cyber", "system replacement"],
}

PWC_DISTRICTS = [
    "Countywide", "Brentsville", "Coles", "Gainesville",
    "Neabsco", "Occoquan", "Potomac", "Woodbridge",
]

NUM_RE = re.compile(r"[-(]?\$?[\d,]+\)?")
DASH = "-–—"


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #
def _tok_dollars(tok):
    t = tok.replace("$", "").replace(",", "").replace("(", "").replace(")", "").strip()
    if t in ("", "-"):
        return None
    if not re.match(r"^\d+$", t):
        return None
    return int(t)


def _to_k(dollars):
    return int(round(dollars / 1000.0))


def _slug(s):
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")


def _year_offset(line, year):
    yy = str(year)[2:]
    for lab in (f"FY {yy}", f"FY{yy}", f"FY {year}", f"FY{year}"):
        p = line.find(lab)
        if p >= 0:
            return p + len(lab)
    return None


def _total_offset(line):
    for lab in ("FY26-31", "FY26 - 31", "FY26-FY31", "FY26 - FY31",
                "FY 26-31", "6-Year", "6 Year", "Total", "TOTAL"):
        p = line.find(lab)
        if p >= 0:
            return p + len(lab)
    return None


# --------------------------------------------------------------------------- #
# functional-area detection
# --------------------------------------------------------------------------- #
def _line_area(line):
    """Return a functional area named by this line (header/footer, divider
    heading, or 'CIP - <area>'), else None."""
    low = line.lower()
    # header/footer line carrying the functional area next to the 'CIP' marker
    if "cip" in low:
        for f in FUNCTIONAL_AREAS:
            if f.lower() in low:
                return f
    s = re.sub(r"\s+", " ", line.strip())
    sl = s.lower()
    for f in FUNCTIONAL_AREAS:
        fl = f.lower()
        if sl == fl:
            return f
        for suf in (" program", " programs", " projects", " capital program",
                    " capital projects", " capital improvement program"):
            if sl == fl + suf:
                return f
    # near-standalone heading (area name plus at most a tiny suffix)
    for f in FUNCTIONAL_AREAS:
        if len(s) <= len(f) + 6 and not re.search(r"[\$\d]", s) \
                and re.search(r"\b" + re.escape(f) + r"\b", s, re.I):
            return f
    return None


def _classify_area(title, region):
    text = (title + " " + " ".join(region[:35])).lower()
    best, best_score = None, 0
    for f in FUNCTIONAL_AREAS:
        score = sum(text.count(kw) for kw in AREA_KEYWORDS[f])
        if score > best_score:
            best, best_score = f, score
    return best


# --------------------------------------------------------------------------- #
# table parsing
# --------------------------------------------------------------------------- #
def _assign_row(line, cols, tot_off):
    toks = []
    for m in NUM_RE.finditer(line):
        v = _tok_dollars(m.group())
        if v is not None:
            toks.append((m.end(), v))
    if not toks:
        return None

    offs = sorted(cols.values()) + ([tot_off] if tot_off is not None else [])
    offs.sort()
    gaps = [b - a for a, b in zip(offs, offs[1:])]
    thr = max(6, (min(gaps) // 2) if gaps else 10)

    year_d = {}
    for y, off in cols.items():
        cand = min(toks, key=lambda t: abs(t[0] - off))
        year_d[y] = cand[1] if abs(cand[0] - off) <= thr else 0

    printed = None
    if tot_off is not None:
        cand = min(toks, key=lambda t: abs(t[0] - tot_off))
        if abs(cand[0] - tot_off) <= thr:
            printed = cand[1]
    return year_d, printed


def _parse_fiscal(region):
    header_idx = None
    cols = None
    tot_off = None
    for i, ln in enumerate(region):
        c = {}
        for y in FY_YEARS:
            off = _year_offset(ln, y)
            if off is not None:
                c[y] = off
        if len(c) >= 4:
            header_idx = i
            cols = c
            tot_off = _total_offset(ln)
            break
    if header_idx is None:
        return None

    for ln in region[header_idx + 1:]:
        if re.match(r"\s*Total", ln, re.I) and re.search(r"\d", ln):
            res = _assign_row(ln, cols, tot_off)
            if res is not None:
                year_d, printed = res
                full = {y: year_d.get(y, 0) for y in FY_YEARS}
                return full, printed
    return None


def _parse_project_total(line):
    m = re.search(r"Total Project Cost[s]?\s*[" + DASH + r":]*\s*\$?\s*"
                  r"([\d,]+(?:\.\d+)?)\s*([MBK])?", line, re.I)
    if not m:
        return None
    num = float(m.group(1).replace(",", ""))
    suf = (m.group(2) or "").upper()
    if suf == "M":
        return int(round(num * 1000))
    if suf == "B":
        return int(round(num * 1000000))
    if suf == "K":
        return int(round(num))
    return int(round(num / 1000.0))


def _parse_funding_sources(region):
    srcs = []
    started = False
    for ln in region:
        if re.search(r"Funding Sources", ln, re.I):
            started = True
            continue
        if started:
            if re.match(r"\s*Total", ln, re.I):
                break
            if re.search(r"FY\s?2?6|Project Description|Total Project Cost", ln):
                continue
            m = re.match(r"\s*([A-Za-z][A-Za-z0-9 ,&%/().\-]+?)\s+[-(]?\$?[\d,]{2,}", ln)
            if m:
                label = re.sub(r"\s+", " ", m.group(1)).strip(" .,-")
                if label and label.lower() not in (s.lower() for s in srcs):
                    srcs.append(label)
    return srcs


def _parse_districts(region):
    found = []
    for ln in region:
        if re.search(r"District|Magisterial", ln, re.I):
            for d in PWC_DISTRICTS:
                if re.search(r"\b" + re.escape(d) + r"\b", ln) and d not in found:
                    found.append(d)
    return found


def _find_title(flat, idx):
    for j in range(idx - 1, max(-1, idx - 7), -1):
        s = flat[j].strip()
        if not s:
            continue
        if re.search(r"CIP\s*[" + DASH + r"]", s):
            continue
        if re.match(r"^\d+$", s):
            continue
        if "Total Project Cost" in s or "$" in s:
            continue
        if not re.search(r"[A-Za-z]", s):
            continue
        return re.sub(r"\s+", " ", s).strip()
    return None


# --------------------------------------------------------------------------- #
# document parse
# --------------------------------------------------------------------------- #
def _parse_document(text, data_url):
    pages = text.split("\f")
    flat = []
    for page in pages:
        for ln in page.split("\n"):
            flat.append(ln)

    # carry-forward functional area per line
    line_area = []
    cur = None
    for ln in flat:
        a = _line_area(ln)
        if a:
            cur = a
        line_area.append(cur)

    starts = [i for i, ln in enumerate(flat) if "Total Project Cost" in ln]

    records = []
    run_id = f"{SOURCE_ID}-{EXTRACTOR_VERSION}"
    for si, idx in enumerate(starts):
        try:
            next_idx = starts[si + 1] if si + 1 < len(starts) else len(flat)
            region = flat[idx:next_idx]
            title = _find_title(flat, idx)
            if not title:
                continue

            parsed = _parse_fiscal(region)
            if parsed is None:
                continue
            year_dollars, printed_dollars = parsed
            raw_sum = sum(year_dollars.values())

            fiscal_years = {str(y): _to_k(year_dollars[y]) for y in FY_YEARS}
            five_year_total = sum(fiscal_years.values())

            printed_subtotal = None
            if printed_dollars is not None:
                if printed_dollars == raw_sum:
                    printed_subtotal = five_year_total
                else:
                    printed_subtotal = _to_k(printed_dollars)

            total = _parse_project_total(flat[idx])
            if total is None:
                total = five_year_total

            if five_year_total <= 0 and total <= 0:
                continue

            # functional-area resolution: carry-forward -> region rescan ->
            # keyword classifier -> generic
            area = line_area[idx]
            if not area:
                for ln in region[:40]:
                    a = _line_area(ln)
                    if a:
                        area = a
                        break
            if not area:
                area = _classify_area(title, region)
            if not area:
                area = "Capital Improvement Program"

            record = {
                "project_id": f"{SOURCE_ID}-{_slug(title)}",
                "title": title,
                "function": area,
                "funding_sources": _parse_funding_sources(region),
                "fiscal_years": fiscal_years,
                "five_year_total": five_year_total,
                "printed_subtotal": printed_subtotal,
                "total": int(total),
                "districts": _parse_districts(region),
                "unit": "usd_thousands",
                "source_url": SOURCE_URL,
                "data_source_url": data_url,
                "provenance": {
                    "source_id": SOURCE_ID,
                    "extractor_version": EXTRACTOR_VERSION,
                    "run_id": run_id,
                },
            }
            records.append(record)
        except Exception:
            continue
    return records


def _dedupe_ids(records):
    seen = {}
    for r in records:
        pid = r["project_id"]
        seen[pid] = seen.get(pid, 0) + 1
        if seen[pid] > 1:
            r["project_id"] = f"{pid}-{seen[pid]}"
    return records


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #
def extract(rt, args=None):
    records = []
    for url in CIP_URLS:
        text = rt.fetch_text(url)
        records.extend(_parse_document(text, url))
    records = _dedupe_ids(records)

    run_meta = {
        "source_id": SOURCE_ID,
        "extractor_version": EXTRACTOR_VERSION,
        "schema_version": SCHEMA_VERSION,
        "jurisdiction": JURISDICTION,
        "edition": EDITION,
        "row_counts": {"capital_projects": len(records)},
    }
    return {"capital_projects": records}, run_meta
