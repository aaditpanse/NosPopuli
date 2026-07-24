"""Deterministic extractor for the City of Chicago Capital Improvement Program.

Source: 'City of Chicago 2025-2029 Capital Improvement Program (Build Better
Together)', a single layout-preserving PDF published by OBM. We parse the
'Project and Fund Source Detail Report' at the back of the document: it groups
funded projects under the CIP's top-level functional categories (Aviation,
Water System, Transportation, ...) and prints, per project, one row per fund
source with per-fiscal-year dollars and a project Total line.

Approach (adapted from the Fairfax reference, different layout):
  * fetch each CIP url once via rt.fetch_text (pdftotext -layout upstream);
  * walk lines, tracking the current top-level section heading as `function`;
  * find each cost table's fiscal-year header row, record the character offset
    of each year label (and the Total label), and read data rows by slicing at
    those offsets (nearest-column assignment with a spacing-derived tolerance);
  * sum fund-source rows into per-year totals; reconcile, in raw dollars,
    against the project's printed Total column before trusting a subtotal;
  * normalize every amount to integer thousands of dollars.
"""

import re

try:
    import schema
    SCHEMA_VERSION = schema.SCHEMA_VERSION
except Exception:
    SCHEMA_VERSION = "1.5"

EXTRACTOR_VERSION = "1"
SOURCE_ID = "chicago-cip"

SOURCE_URL = ("https://www.chicago.gov/city/en/depts/obm/supp_info/"
              "office-publications.html")
CIP_URLS = [
    "https://www.chicago.gov/content/dam/city/depts/obm/supp_info/CIP/"
    "City%20of%20Chicago%202025-2029%20CIP.pdf"
]

FY_FIRST, FY_LAST = 2025, 2029
FY_YEARS = list(range(FY_FIRST, FY_LAST + 1))

# Top-level functional categories, from the CIP's Table of Contents / funding
# summary. These are the section headings the detail report groups projects by;
# we track the current one and emit it as `function`.
FUNCTIONS = [
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

NUM_RE = re.compile(r"\$?\(?-?[\d,]*\d\)?")
FURNITURE = re.compile(
    r"(?i)city of chicago|capital improvement program|project and fund source"
    r"|^\s*fund source\s*$|table of contents|build better together"
    r"|^\s*page\b|^\s*\d{1,3}\s*$|grand total|program total|^\s*\.+\s*$")


def _norm(s):
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


FUNC_NORM = {_norm(f): f for f in FUNCTIONS}


def money(tok):
    tok = tok.strip()
    neg = tok.startswith("(") and tok.endswith(")")
    tok = tok.strip("()").replace("$", "").replace(",", "").replace(" ", "")
    if tok in ("", "-"):
        return 0
    try:
        v = int(tok)
    except ValueError:
        return 0
    return -v if neg else v


def match_function(s):
    """Return the canonical top-level function if `s` is one of its headings."""
    if len(s) > 60:
        return None
    s2 = re.sub(r"(?i)^(program|function|category|department of)\s*:?\s*", "", s)
    s2 = re.sub(r"(?i)\s+program\s*$", "", s2).strip(" :-")
    n = _norm(s2)
    return FUNC_NORM.get(n)


def find_header(line):
    """Detect a fiscal-year header row; return {year -> end offset, 'Total': off}."""
    pos = {}
    for y in FY_YEARS:
        m = re.search(r"(?<!\d)" + str(y) + r"(?!\d)", line)
        if m:
            pos[y] = m.end()
            continue
        m = re.search(r"(?i)FY\s*" + f"{y % 100:02d}" + r"(?!\d)", line)
        if m:
            pos[y] = m.end()
    if len(pos) < 3:
        return None
    tot = list(re.finditer(r"(?i)total", line))
    if tot:
        pos["Total"] = tot[-1].end()
    return pos


def parse_row(line, cols):
    """Slice a data row at the header offsets.

    Returns (year_values, total_value, first_number_start). Each number is
    assigned to the nearest column (year or Total) within a spacing-derived
    tolerance; numbers outside tolerance (e.g. a prior-year column) are left in
    the label region."""
    year_off = {y: cols[y] for y in FY_YEARS if y in cols}
    total_off = cols.get("Total")
    all_off = sorted(list(year_off.values()) + ([total_off] if total_off else []))
    if len(all_off) >= 2:
        gaps = [all_off[i + 1] - all_off[i] for i in range(len(all_off) - 1)]
        tol = max(4, min(gaps) // 2)
    else:
        tol = 8
    yv, total_val, first = {}, None, None
    for m in NUM_RE.finditer(line):
        g = m.group()
        if not re.search(r"\d", g):
            continue
        end = m.end()
        best_key, best_d = None, None
        for y, off in year_off.items():
            d = abs(off - end)
            if best_d is None or d < best_d:
                best_d, best_key = d, ("y", y)
        if total_off is not None:
            d = abs(total_off - end)
            if best_d is None or d < best_d:
                best_d, best_key = d, ("t", None)
        if best_d is not None and best_d <= tol:
            v = money(g)
            if best_key[0] == "y":
                yv[best_key[1]] = yv.get(best_key[1], 0) + v
            else:
                total_val = v
            if first is None or m.start() < first:
                first = m.start()
    return yv, total_val, first


def _new_project(function):
    return {"title_parts": [], "function": function or "Other",
            "funds": [], "fy": {y: 0 for y in FY_YEARS},
            "printed_total": None, "printed_years": None,
            "nrows": 0, "last_total": None}


def _titleish(s):
    return len(s) <= 90 and not s.rstrip().endswith(".")


def parse_projects(lines):
    projects = []
    cur = None
    function = None
    cols = None

    def flush():
        nonlocal cur
        if cur is not None and (any(cur["fy"].values()) or cur["funds"]
                                or cur["printed_total"] is not None):
            projects.append(cur)
        cur = None

    for raw in lines:
        line = raw.replace("\f", "")
        s = line.strip()
        if not s:
            continue

        fn = match_function(s)
        if fn:
            flush()
            function = fn
            continue

        hdr = find_header(line)
        if hdr:
            cols = hdr
            continue

        if FURNITURE.search(s):
            continue
        if cols is None:
            continue

        yv, total_val, first = parse_row(line, cols)
        has_nums = bool(yv) or total_val is not None
        label = line[:first].strip() if first is not None else s
        low = label.lower()

        if not has_nums:
            # text-only line: a project title (or a title continuation)
            if cur is None:
                cur = _new_project(function)
                cur["title_parts"].append(s)
            elif cur["funds"] or any(cur["fy"].values()):
                flush()
                cur = _new_project(function)
                cur["title_parts"].append(s)
            else:
                joined = " ".join(cur["title_parts"])
                if _titleish(s) and len(joined) < 100:
                    cur["title_parts"].append(s)
            continue

        if "total" in low:
            if cur is not None:
                cur["printed_total"] = total_val
                cur["printed_years"] = yv
                flush()
            continue

        # fund-source row
        if cur is None:
            cur = _new_project(function)
            if label:
                cur["title_parts"].append(label)
        for y, v in yv.items():
            cur["fy"][y] += v
        cur["nrows"] += 1
        cur["last_total"] = total_val
        if label and not label[0].isdigit() and len(label) < 80:
            if label not in cur["funds"]:
                cur["funds"].append(label)

    flush()
    return projects


def _clean_title(parts):
    t = " ".join(parts)
    t = re.sub(r"\.{2,}", " ", t)
    t = re.sub(r"\s+", " ", t).strip(" .-")
    return t


def _slug(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:80]


def build_records(projects, data_url, divisor):
    run_id = f"{SOURCE_ID}-{EXTRACTOR_VERSION}"
    records, seen = [], {}

    def scale(v):
        return int(round(v / divisor)) if divisor != 1 else int(v)

    for p in projects:
        title = _clean_title(p["title_parts"])
        if not title or len(title) < 3:
            continue

        raw_fy = dict(p["fy"])
        if not any(raw_fy.values()) and p["printed_years"]:
            raw_fy = {y: p["printed_years"].get(y, 0) for y in FY_YEARS}

        fy_k = {y: max(0, scale(raw_fy.get(y, 0))) for y in FY_YEARS}
        five = sum(fy_k.values())
        if five <= 0:
            continue

        # raw-dollar reconciliation before trusting any printed subtotal
        raw_sum = sum(raw_fy.get(y, 0) for y in FY_YEARS)
        raw_printed = p["printed_total"]
        if raw_printed is None and p["nrows"] == 1 and p["last_total"] is not None:
            raw_printed = p["last_total"]

        printed_sub = None
        if raw_printed is not None and raw_printed > 0 and raw_sum == raw_printed:
            target = scale(raw_printed)
            diff = target - five
            if diff != 0:
                ymax = max(FY_YEARS, key=lambda y: raw_fy.get(y, 0))
                if fy_k[ymax] + diff >= 0:
                    fy_k[ymax] += diff
                    five = sum(fy_k.values())
            if five == target:
                printed_sub = target

        total = printed_sub if printed_sub is not None else five

        base = _slug(f"{p['function']}-{title}")
        pid = base or "project"
        seen[pid] = seen.get(pid, 0) + 1
        if seen[pid] > 1:
            pid = f"{pid}-{seen[pid]}"

        rec = {
            "project_id": f"{SOURCE_ID}-{pid}",
            "title": title,
            "function": p["function"],
            "funding_sources": list(p["funds"]),
            "fiscal_years": {str(y): fy_k[y] for y in FY_YEARS},
            "five_year_total": five,
            "total": int(total),
            "districts": [],
            "unit": "usd_thousands",
            "source_url": SOURCE_URL,
            "data_source_url": data_url,
            "provenance": {
                "source_id": SOURCE_ID,
                "extractor_version": EXTRACTOR_VERSION,
                "run_id": run_id,
            },
        }
        if printed_sub is not None:
            rec["printed_subtotal"] = printed_sub
        records.append(rec)

    return records


def _detail_region(text):
    lines = text.splitlines()
    start = 0
    for idx, l in enumerate(lines):
        if "Project and Fund Source Detail Report" in l:
            start = idx
    return lines[start:], lines


def extract(rt, args):
    all_records = []
    data_url = CIP_URLS[0]
    for url in CIP_URLS:
        text = rt.fetch_text(url)
        divisor = 1 if re.search(r"(?i)in thousands|\(\$?000|\$000s", text) else 1000

        detail_lines, all_lines = _detail_region(text)
        projects = parse_projects(detail_lines)
        if len({p["function"] for p in projects
                if p["function"] != "Other"}) < 6 or len(projects) < 20:
            # fall back to walking the whole document
            whole = parse_projects(all_lines)
            if len(whole) > len(projects):
                projects = whole

        all_records.extend(build_records(projects, url, divisor))

    run_meta = {
        "source_id": SOURCE_ID,
        "extractor_version": EXTRACTOR_VERSION,
        "schema_version": SCHEMA_VERSION,
        "row_counts": {"capital_projects": len(all_records)},
    }
    return {"capital_projects": all_records}, run_meta
