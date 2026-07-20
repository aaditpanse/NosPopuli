"""Deterministic extractor for the Prince William County, Virginia
Capital Improvement Program (FY2026-2031).

One-shot layout-aware PDF-table parse. All I/O goes through rt.fetch_text.
The document prints a front-matter "Total Projected Expenditures by Functional
Area" table (the 6 top-level categories) and then a detail page per project
("Total Project Cost - $X", a description, and a per-year cost schedule).
We walk the pages tracking the current functional-area section (from page
footers / section headings), slice each project's cost table by the character
offsets of its fiscal-year column headers, and reconcile per project: where a
printed multi-year subtotal exists it must equal the sum of the parsed year
columns exactly.
"""

import re

try:
    import schema as _schema
    SCHEMA_VERSION = _schema.SCHEMA_VERSION
except Exception:
    SCHEMA_VERSION = "1.4"

EXTRACTOR_VERSION = "1"

SOURCE_ID = "princewilliam-cip"
JURISDICTION = "Prince William County, Virginia"
EDITION = "FY2026-2031 Capital Improvement Program"
CIP_URLS = ["https://www.pwcva.gov/assets/2025-06/aFY26--16--CIP00--CIP.pdf"]
SOURCE_URL = "https://www.pwcva.gov/cip"

FY_YEARS = list(range(2026, 2032))  # 2026..2031

FALLBACK_FUNCTIONS = ["Community Development", "Human Services",
                      "General Government", "Public Safety",
                      "Technology Improvement", "Transportation"]

DISTRICTS = ["Countywide", "Brentsville", "Coles", "Gainesville",
             "Neabsco", "Occoquan", "Potomac", "Woodbridge"]

FUNDING_PHRASES = [
    "General Obligation Bond", "GO Bond", "General Fund", "Pay-As-You-Go",
    "Pay-Go", "NVTA 70%", "NVTA 30%", "NVTA", "Fire Levy", "Proffers",
    "Recordation Tax", "State & Federal Revenue", "State Revenue",
    "Federal Revenue", "Service Authority", "Gas Tax", "Revenue Bond",
    "Developer Contribution", "Transportation Bond", "Cable Franchise",
    "Stormwater", "Impact Fee", "Grant",
]

TOTAL_PATS = ["FY26-31", "FY26 - 31", "FY 26-31", "FY26-FY31", "FY 26 - FY 31",
              "FY2026-2031", "FY2026-31", "6-Year Total", "6 Year Total",
              "Six-Year Total", "6-Yr Total", "6 Yr Total", "Total"]

VAL_RE = re.compile(r"\$?\d[\d,]*|[-\u2013\u2014]")
FOOTER_RE = re.compile(r"CIP\s*[-\u2013\u2014]\s*([A-Za-z][A-Za-z& ]+?)\s*$")
TPC_RE = re.compile(
    r"Total Project Cost.{0,30}?\$\s*([\d,]+(?:\.\d+)?)\s*"
    r"(billion|million|thousand|B|M|K)?", re.I)
THOUSANDS_RE = re.compile(r"\(\s*\$?000s?\s*\)|in thousands", re.I)

_FUNCS = list(FALLBACK_FUNCTIONS)


def _norm(s):
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def _collapse(s):
    return re.sub(r"\s+", " ", s).strip()


def _rnd(x):
    return int(x + 0.5)


# ---------------------------------------------------------------- summary

def parse_summary_functions(text):
    """Read the front-matter functional-area table to learn the exact
    category names the document uses (and thereby their spelling)."""
    funcs = []
    started = False
    for ln in text.split("\n"):
        if "Functional Area" in ln:
            started = True
            continue
        if not started:
            continue
        if "$" in ln:
            label = ln.split("$", 1)[0].strip()
            if label.lower().startswith("total"):
                break
            if label and not any(c.isdigit() for c in label):
                funcs.append(_collapse(label))
    out = []
    for f in funcs:
        if f not in out:
            out.append(f)
    return out if len(out) >= 4 else None


# ---------------------------------------------------------------- function

def _match_function(text):
    t = _norm(text)
    if not t:
        return None
    for f in _FUNCS:
        if t == _norm(f):
            return f
    for f in _FUNCS:
        nf = _norm(f)
        if nf in t or t in nf:
            return f
    return None


def detect_function(page_lines):
    # primary: page footer of the form "... CIP - <Section>"
    for ln in page_lines:
        m = FOOTER_RE.search(ln)
        if m:
            f = _match_function(m.group(1))
            if f:
                return f
    # secondary: a standalone section-heading line equal to a category name
    for ln in page_lines:
        if any(ch.isdigit() for ch in ln) or "$" in ln:
            continue
        s = _norm(ln)
        if not s:
            continue
        for f in _FUNCS:
            if s == _norm(f):
                return f
    return None


# ---------------------------------------------------------------- offsets

def find_offsets(line):
    """Character offsets (end of each fiscal-year label) in a header row."""
    two = 0
    four = 0
    for y in FY_YEARS:
        if re.search(r"FY\s?" + str(y)[2:] + r"(?!\d)", line):
            two += 1
        if ("FY " + str(y)) in line or ("FY" + str(y)) in line:
            four += 1
    use_two = two >= four
    offs = {}
    for y in FY_YEARS:
        found = -1
        pats = ((f"FY {str(y)[2:]}", f"FY{str(y)[2:]}") if use_two
                else (f"FY {y}", f"FY{y}"))
        for pat in pats:
            k = line.find(pat)
            if k != -1:
                found = k + len(pat)
                break
        if found != -1:
            offs[y] = found
    return offs


def find_total_off(line, yoffs):
    after = max(yoffs.values()) if yoffs else 0
    for pat in TOTAL_PATS:
        idx = line.find(pat)
        if idx != -1 and idx > after - 2:
            return idx + len(pat)
    return None


# ---------------------------------------------------------------- rows

def parse_row(line, yoffs, toff):
    cols = {}
    for y, o in yoffs.items():
        cols[("Y", y)] = o
    if toff is not None:
        cols[("T", None)] = toff
    assign = {}
    first = None
    for m in VAL_RE.finditer(line):
        tok = m.group()
        end = m.end()
        ck = min(cols, key=lambda c: abs(cols[c] - end))
        d = abs(cols[ck] - end)
        if d > 10:
            continue
        if tok in ("-", "\u2013", "\u2014"):
            val = 0
        else:
            val = int(tok.replace("$", "").replace(",", ""))
        if ck in assign and abs(cols[ck] - assign[ck][1]) <= d:
            continue
        assign[ck] = (val, end)
        if first is None:
            first = m.start()
    if not assign:
        return None
    year_raw = [assign[("Y", y)][0] if ("Y", y) in assign else 0
                for y in FY_YEARS]
    total_raw = assign[("T", None)][0] if ("T", None) in assign else None
    label = line[:first].strip() if first is not None else ""
    nfilled = sum(1 for y in FY_YEARS if ("Y", y) in assign)
    return (year_raw, total_raw, label, nfilled)


def select(cands):
    if not cands:
        return None
    recon = [c for c in cands
             if c[1] is not None and c[1] > 0 and sum(c[0]) == c[1]]
    if recon:
        lt = [c for c in recon if "total" in c[2].lower()]
        if lt:
            c = max(lt, key=lambda c: (c[3], c[1]))
        else:
            c = max(recon, key=lambda c: (c[1], c[3]))
        return (c[0], c[1], True)
    withyear = [c for c in cands if c[3] >= 1]
    if not withyear:
        return None
    lt = [c for c in withyear if "total" in c[2].lower()]
    pool = lt if lt else withyear
    c = max(pool, key=lambda c: (c[3], c[1] if c[1] is not None else -1))
    return (c[0], (c[1] if c[1] is not None else None), False)


def parse_table(block):
    hidx = None
    yoffs = None
    toff = None
    for i, ln in enumerate(block):
        offs = find_offsets(ln)
        if len(offs) >= 4:
            hidx = i
            yoffs = offs
            toff = find_total_off(ln, offs)
            break
    if hidx is None:
        return None
    cands = []
    for ln in block[hidx + 1:]:
        if len(find_offsets(ln)) >= 4:  # continuation header, skip
            continue
        r = parse_row(ln, yoffs, toff)
        if r:
            cands.append(r)
    return select(cands)


# ---------------------------------------------------------------- units

def largest_remainder(raw_years, raw_total):
    target = int(raw_total / 1000.0 + 0.5)
    floats = [r / 1000.0 for r in raw_years]
    floored = [int(f) for f in floats]
    res = floored[:]
    rem = target - sum(floored)
    order = sorted(range(len(floats)),
                   key=lambda i: (floats[i] - floored[i]), reverse=True)
    n = len(res)
    i = 0
    while rem > 0 and n > 0:
        res[order[i % n]] += 1
        rem -= 1
        i += 1
    k = 0
    while rem < 0 and n > 0:
        res[order[n - 1 - (k % n)]] -= 1
        rem += 1
        k += 1
    return res, target


def to_units(year_raw, total_raw, reconciled, thousands_mode):
    if thousands_mode:
        yk = [int(v) for v in year_raw]
        tk = int(total_raw) if total_raw is not None else None
        return yk, tk
    if reconciled:
        yk, tk = largest_remainder(year_raw, total_raw)
        if any(v < 0 for v in yk) or sum(yk) != tk:
            yk = [_rnd(v / 1000.0) for v in year_raw]
            tk = sum(yk)
        return yk, tk
    yk = [_rnd(v / 1000.0) for v in year_raw]
    tk = _rnd(total_raw / 1000.0) if total_raw is not None else None
    return yk, tk


# ---------------------------------------------------------------- misc parse

def parse_money_unit(numstr, unit):
    v = float(numstr.replace(",", ""))
    u = (unit or "").lower()
    if u in ("billion", "b"):
        return int(round(v * 1e9))
    if u in ("million", "m"):
        return int(round(v * 1e6))
    if u in ("thousand", "k"):
        return int(round(v * 1e3))
    return int(round(v))


def parse_districts(block_text):
    found = []
    for ln in block_text.split("\n"):
        if "District" not in ln:
            continue
        for d in DISTRICTS:
            if re.search(r"\b" + re.escape(d) + r"\b", ln) and d not in found:
                found.append(d)
    if not found and re.search(r"\bCountywide\b", block_text):
        found.append("Countywide")
    return found


def parse_funding(block_text):
    found = []
    for p in FUNDING_PHRASES:
        if re.search(r"\b" + re.escape(p) + r"\b", block_text, re.I):
            if p not in found:
                found.append(p)
    return found


def is_title_furniture(s):
    low = s.lower()
    if re.fullmatch(r"\d+", s.strip()):
        return True
    if "capital improvement program" in low:
        return True
    if "total project cost" in low:
        return True
    if "budget" == low.strip() or "fy2026 budget" in low or "fy 2026 budget" in low:
        return True
    if re.search(r"cip\s*[-\u2013\u2014]", low):
        return True
    if "functional area" in low:
        return True
    if _match_function(s) and _norm(s) == _norm(_match_function(s)):
        return True
    return False


def get_title(lines, si):
    parts = []
    steps = 0
    j = si - 1
    while j >= 0 and steps < 8:
        s = lines[j].strip()
        steps += 1
        j -= 1
        if not s:
            if parts:
                break
            continue
        if is_title_furniture(s):
            if parts:
                break
            continue
        parts.insert(0, _collapse(s))
    return _collapse(" ".join(parts)) or "Untitled Project"


# ---------------------------------------------------------------- records

def build_record(block, lines, si, func, url, seen_ids):
    title = get_title(lines, si)

    block_text = "\n".join(block)
    thousands_mode = bool(THOUSANDS_RE.search(block_text))

    parsed = parse_table(block)
    if not parsed:
        return None
    year_raw, total_raw, reconciled = parsed

    yk, tk = to_units(year_raw, total_raw, reconciled, thousands_mode)

    fiscal_years = {str(y): yk[i] for i, y in enumerate(FY_YEARS)}
    five_year_total = sum(yk)
    printed_subtotal = tk if reconciled else None

    # lifetime "Total Project Cost"
    tpc_thousands = None
    m = TPC_RE.search(_collapse(" ".join(block[:8])))
    if m:
        tpc_thousands = int(round(parse_money_unit(m.group(1), m.group(2)) / 1000.0))

    if tpc_thousands is not None:
        total = tpc_thousands
    elif printed_subtotal is not None:
        total = printed_subtotal
    else:
        total = five_year_total
    total = int(total) if total and total > 0 else int(five_year_total)
    if total < 0:
        total = 0

    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:60] or "project"
    n = seen_ids.get(slug, 0) + 1
    seen_ids[slug] = n
    pid = slug if n == 1 else f"{slug}-{n}"

    rec = {
        "project_id": f"{SOURCE_ID}-{pid}",
        "title": title,
        "function": func,
        "funding_sources": parse_funding(block_text),
        "fiscal_years": fiscal_years,
        "five_year_total": five_year_total,
        "total": total,
        "districts": parse_districts(block_text),
        "unit": "usd_thousands",
        "source_url": SOURCE_URL,
        "data_source_url": url,
        "provenance": {
            "source_id": SOURCE_ID,
            "extractor_version": EXTRACTOR_VERSION,
            "run_id": f"{SOURCE_ID}-{EXTRACTOR_VERSION}",
        },
    }
    if printed_subtotal is not None:
        rec["printed_subtotal"] = printed_subtotal
    return rec


def parse_document(text, url, seen_ids):
    pages = text.split("\f")
    page_funcs = []
    cur = None
    for p in pages:
        f = detect_function(p.split("\n"))
        if f:
            cur = f
        page_funcs.append(cur)

    lines = []
    lfunc = []
    for pi, p in enumerate(pages):
        for ln in p.split("\n"):
            lines.append(ln)
            lfunc.append(page_funcs[pi])

    starts = [i for i, ln in enumerate(lines) if "Total Project Cost" in ln]
    recs = []
    for k, si in enumerate(starts):
        ei = starts[k + 1] if k + 1 < len(starts) else len(lines)
        func = lfunc[si]
        if not func:
            continue
        block = lines[si:ei]
        try:
            rec = build_record(block, lines, si, func, url, seen_ids)
        except Exception:
            rec = None
        if rec:
            recs.append(rec)
    return recs


# ---------------------------------------------------------------- entrypoint

def extract(rt, args=None):
    global _FUNCS
    records = []
    seen_ids = {}
    for url in CIP_URLS:
        text = rt.fetch_text(url) or ""
        funcs = parse_summary_functions(text)
        _FUNCS = funcs if funcs else list(FALLBACK_FUNCTIONS)
        records.extend(parse_document(text, url, seen_ids))

    run_meta = {
        "source_id": SOURCE_ID,
        "extractor_version": EXTRACTOR_VERSION,
        "schema_version": SCHEMA_VERSION,
        "jurisdiction": JURISDICTION,
        "edition": EDITION,
        "row_counts": {"capital_projects": len(records)},
    }
    return {"capital_projects": records}, run_meta
