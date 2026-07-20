"""Deterministic extractor for a county Capital Improvement Program (CIP).

    python cip_extractor.py            # fetch, parse, gate, land in the store

A CIP is the authoritative annual enumeration of every funded capital
project — the real answer to "what public works are being built here, with
what budget, on what timeline." Unlike the meetings/votes sources, a CIP is
a single static budget document, so the extractor is a one-shot deterministic
PDF-table parse: no pagination dialects, no vote grammar, no per-meeting
drift. Synthesize once, and it re-runs free when next year's CIP is published.

This is the hand-built reference for a NEW record type (`capital_project`,
schema {v}). It establishes the durable assets — the schema shape, the gate,
and the parse pattern — the same way the first meetings sources (run_m0..m4)
were hand-built before onboarding was generalized. A future synthesis path
can target other counties' CIPs using this as the family template.

The parse: `pdftotext -layout` (poppler — the converter Foundry already uses
everywhere) preserves column alignment. Each functional area prints a
"Project Cost Summaries" table with a stable per-project number, funding
source codes, and per-year dollars ($000s). We walk those tables, assign each
dollar to a fiscal-year column by nearest header offset, and join a Supervisor
District from the document's "Projects by Function" index. The hard gate is
per-project reconciliation: each project's printed FY2027-2031 subtotal must
equal the sum of its parsed fiscal-year columns — proof the columns were read
and assigned correctly.
"""

import datetime
import json
import pathlib
import re
import subprocess
import sys
import tempfile
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import schema  # noqa: E402

FOUNDRY = pathlib.Path(__file__).parent
STORE = FOUNDRY / "data" / "store"
CACHE = FOUNDRY / "data" / "onboard" / "fairfax-cip_cache.pdf"

SOURCE_ID = "fairfax-cip"
EXTRACTOR_VERSION = "1"
JURISDICTION = "Fairfax County, Virginia"
FISCAL_TITLE = "FY 2027 - FY 2031 Advertised Capital Improvement Program"
# The human-viewable landing page and the machine document, kept separate the
# same way the meetings schema separates source_url from data_source_url.
SOURCE_URL = "https://www.fairfaxcounty.gov/budget/capital-improvement-program"
PDF_URL = ("https://www.fairfaxcounty.gov/budget/sites/budget/files/Assets/"
           "documents/fy2027/advertised/CIP.pdf")

FY_FIRST, FY_LAST = 2027, 2031  # the five-year CIP window this edition covers
FY_YEARS = list(range(FY_FIRST, FY_LAST + 1))
BUDGET_YEAR = FY_FIRST - 1  # "Budgeted or Expended Through FY 2026" column

# Funding-source legend, printed in every table's key. Codes are the schema
# vocabulary for capital_project.funding_sources; anything else is a parse
# overrun into an adjacent column.
FUNDING_SOURCES = {
    "B": "General Obligation Bonds",
    "F": "Federal grants",
    "G": "General Fund",
    "HTF": "Housing Trust Funds",
    "R": "Real Estate Tax Revenue",
    "S": "State funds",
    "SF": "Stormwater Fees",
    "SR": "System Revenues",
    "U": "Undetermined",
    "X": "Other (reimbursement/gift)",
}
DISTRICTS = {"Countywide", "Braddock", "Dranesville", "Franconia",
             "Hunter Mill", "Mason", "Mount Vernon", "Providence",
             "Springfield", "Sully", "TBD"}

VAL = re.compile(r"\$[\d,]+|(?<![^\s])C(?![^\s])")
IDRE = re.compile(r"\b[0-9][A-Z0-9]{2,4}-\d{3}-\d{3}\b|\bPR-\d{6}\b"
                  r"|\b[A-Z]{2}-\d{6}\b|\bFund \d+\b")
# Lines that are table furniture, not a project's title continuation.
FURNITURE = re.compile(
    r"Fairfax County, Virginia|CIP Period|Key: Source|Numbers in bold"
    r"|denotes a continuing|Project Title|\(\$000|Budgeted|Expended"
    r"|^\s*Through|^\s*or\s*$|Total\s+Total|FY 20\d\d\s+FY|Project Cost Summaries")
DISTRICT_ROW = re.compile(
    r"^(.*\S)\s{3,}(" + "|".join(sorted(DISTRICTS, key=len, reverse=True)) + r")\s*$")


def _money(tok):
    return 0 if tok == "C" else int(tok.replace("$", "").replace(",", ""))


def _norm(s):
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def fetch_text():
    """Download the CIP once (cached) and convert to layout-preserving text,
    returning the whole text plus the per-PDF-page list (for page provenance)."""
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    if not CACHE.exists():
        req = urllib.request.Request(PDF_URL, headers={"User-Agent": "foundry-cip/1"})
        with urllib.request.urlopen(req, timeout=120) as r:
            CACHE.write_bytes(r.read())
    text = subprocess.run(["pdftotext", "-layout", str(CACHE), "-"],
                          capture_output=True, text=True, timeout=180).stdout
    return text, text.split("\f")


def parse_districts(text):
    """Index the Projects-by-Function list, which carries each project's FULL
    name and district (the cost tables abbreviate — "Brookfield" for
    "Brookfield Elementary"). Returns:
      full  — norm("{function} - {title}")     -> [districts]   (exact join)
      tails — norm(facility name after "- ")    -> [display, [districts]]
    The tail index lets an abbreviated cost-table title (a prefix of the full
    facility name) recover its full name + district."""
    full, tails = {}, {}
    for line in text.splitlines():
        m = DISTRICT_ROW.match(line.replace("\f", ""))
        if not (m and " - " in m.group(1)):
            continue
        left, dist = m.group(1), m.group(2)
        full.setdefault(_norm(left), set()).add(dist)
        tail = left.rsplit(" - ", 1)[-1].strip()
        e = tails.setdefault(_norm(tail), [tail, set()])
        e[1].add(dist)
    return {"full": {k: sorted(v) for k, v in full.items()},
            "tails": {k: [v[0], sorted(v[1])] for k, v in tails.items()}}


_ABBREV = ((r"\bES\b", "Elementary"), (r"\bMS\b", "Middle"),
           (r"\bHS\b", "High"), (r"\bIS\b", "Intermediate"))


def _match_clean(title):
    t = re.sub(r"\s*-\s*\d{4}.*$|\bTBD\b", "", title)
    for pat, full in _ABBREV:  # school-name shorthand -> full, so tails match
        t = re.sub(pat, full, t)
    return _norm(t)


def enrich_title_district(title, section, di):
    """Return (display_title, districts): exact join on function+title, else a
    fuzzy join where the (possibly abbreviated) title is a whole-word prefix of
    a known facility name — which upgrades the title to the full name too."""
    exact = di["full"].get(_norm(f"{section} - {title}"))
    if exact is not None:
        return title, exact
    nt = _match_clean(title)
    if not nt:
        return title, []
    cands = [(disp, dists) for tnorm, (disp, dists) in di["tails"].items()
             if tnorm == nt or tnorm.startswith(nt + " ")]
    if not cands:
        return title, []
    exact_tail = [c for c in cands if _norm(c[0]) == nt]
    if exact_tail:
        pick = exact_tail[0]
    elif len(cands) == 1:
        pick = cands[0]
    else:  # only accept a uniquely-shortest fuller name; ambiguity -> keep as-is
        cands.sort(key=lambda c: len(c[0]))
        if len(cands[0][0]) == len(cands[1][0]):
            return title, []
        pick = cands[0]
    display = pick[0] if len(pick[0]) > len(title) else title
    return display, pick[1]


def parse_projects(pages):
    """Walk every 'Project Cost Summaries' table across page breaks."""
    lines, page_of = [], []
    for pi, pg in enumerate(pages, 1):
        for ln in pg.splitlines():
            lines.append(ln.replace("\f", ""))
            page_of.append(pi)

    projects, i, n = [], 0, len(lines)
    while i < n:
        if lines[i].strip() != "Project Cost Summaries":
            i += 1
            continue
        # section name: next non-blank, non-dollar line
        section, j = None, i + 1
        while j < n and section is None:
            s = lines[j].strip()
            if s and "$" not in s and not s.startswith("("):
                section = s
            j += 1
        # column header ("... of Funds FY 2026 FY 2027 ...")
        while j < n and not ("of Funds" in lines[j] and "FY 2026" in lines[j]):
            if lines[j].strip() == "Project Cost Summaries":
                break
            j += 1
        if j >= n or "of Funds" not in lines[j]:
            i += 1
            continue
        cols = {y: lines[j].find(f"FY {y}") + len(f"FY {y}")
                for y in [BUDGET_YEAR] + FY_YEARS}

        cur, page_start, k = None, page_of[j], j + 1
        while k < n:
            l = lines[k]
            if re.match(r"\s*Total\b", l) and VAL.search(l):
                if cur:
                    projects.append(cur)
                    cur = None
                k += 1
                break
            if "of Funds" in l and "FY 2026" in l:  # continuation-page header
                cols = {y: l.find(f"FY {y}") + len(f"FY {y}")
                        for y in [BUDGET_YEAR] + FY_YEARS}
                k += 1
                continue
            if "Notes:" in l:
                if cur:
                    projects.append(cur)
                    cur = None
                break
            m = re.match(r"\s*(\d+)\s+([A-Za-z].*)", l)
            vals = list(VAL.finditer(l))
            if m and vals and not re.match(r"\s*\d+\.", l):  # a project row
                if cur:
                    projects.append(cur)
                head = l[m.start(2):vals[0].start()].split()
                funds = []
                while head and head[-1].strip(",") in FUNDING_SOURCES:
                    funds.insert(0, head.pop().strip(","))
                fy, right = {}, []
                for v in vals:
                    end = v.end()
                    year, off = min(cols.items(), key=lambda kv: abs(kv[1] - end))
                    if abs(off - end) <= 9:
                        fy[year] = _money(v.group())
                    elif end > cols[FY_LAST] + 9:
                        right.append(_money(v.group()))
                cur = {"section": section, "title": " ".join(head),
                       "funds": funds, "ids": [],
                       "budget": fy.get(BUDGET_YEAR, 0), "continuing": "C" in l[:vals[0].end()] and fy.get(BUDGET_YEAR, 0) == 0,
                       "fy": {y: fy.get(y, 0) for y in FY_YEARS},
                       "printed_subtotal": right[0] if right else None,
                       "total": right[-1] if right else 0,
                       "future": right[1] if len(right) >= 3 else 0,
                       "page": page_of[k]}
            elif cur is not None and l.strip() and not FURNITURE.search(l):
                ids = IDRE.findall(l)
                if ids:
                    cur["ids"] += ids
                elif not VAL.search(l):  # title wrap
                    cur["title"] = (cur["title"] + " " + l.strip()).strip()
            k += 1
        if cur:
            projects.append(cur)
        i = k if k > i else i + 1
    return projects


_WORKTYPE_HEAD = ((re.compile(r"(?i)renovation"), "renovation"),
                  (re.compile(r"(?i)new construction|repurpos"), "new_construction"),
                  (re.compile(r"(?i)capacity|addition|modular"), "addition"))
_NARR_ENTRY = re.compile(r"^\s*\d+\.\s+(.*)")


def _name_key(s):
    return re.sub(r"\bschool\b", "", _norm(s)).strip()


def _title_work_type(title):
    t = title.lower()
    if re.search(r"renovation|moderniz|retrofit|rehabilit", t):
        return "renovation"
    if re.search(r"shell building|repurpos|\bnew\b", t):
        return "new_construction"
    if re.search(r"addition|expansion|capacity|modular|modification", t):
        return "addition"
    return None


def parse_narrative(lines):
    """The CIP's project descriptions are numbered entries grouped under
    work-type subheadings ("Renovation Program - Elementary Schools", "New
    Construction and/or Repurposing"), each stating a completion year and a
    funding status. Parse them keyed by facility name so the cost-table
    projects (bare names like "Brookfield Elementary") can learn whether they
    are a NEW build or a RENOVATION, when they finish, and if fully funded."""
    out, worktype, buf, buf_wt = {}, None, None, None

    def flush():
        if not buf:
            return
        text = " ".join(buf)
        name = re.split(r"\s*\(|\s*:", text, 1)[0].strip()
        comp = re.findall(r"completed in FY\s*(\d{4})(?:\D+(\d{4}))?", text)
        completion = max(int(y) for pair in comp for y in pair if y) if comp else None
        fund = ("partially_funded" if re.search(r"(?i)partially funded", text)
                else "funded" if re.search(r"(?i)\bfunded\b", text) else None)
        if name:
            out[_name_key(name)] = {"work_type": buf_wt, "completion_fy": completion,
                                    "funding_status": fund}

    for raw in lines:
        l = raw.replace("\f", "")
        s = l.strip()
        if "Project Cost Summaries" in l:  # narrative/table boundary — stop bleed
            worktype = None
        if s and not _NARR_ENTRY.match(l) and not FURNITURE.search(l) and len(s) < 60:
            for rx, wt in _WORKTYPE_HEAD:
                if rx.search(s):
                    worktype = wt
                    break
        m = _NARR_ENTRY.match(l)
        if m:
            flush()
            buf, buf_wt = [m.group(1)], worktype
        elif buf is not None and s and not FURNITURE.search(l):
            buf.append(s)
    flush()
    return out


def split_bond_years(title):
    """A trailing "- 2018" (or "- 2018 & 2024", "- 2030 TBD") on a title is the
    BOND REFERENDUM year that authorized the project, not a build/completion
    date — a persistent source of confusion. Pull it off the title and return
    it separately so the UI can label it explicitly."""
    m = re.search(r"\s*-\s*((?:\d{4}|TBD)(?:\s*(?:&|and|,|/)\s*(?:\d{4}|TBD))*)\s*$",
                  title)
    if not m:
        return title, []
    years = re.findall(r"\d{4}|TBD", m.group(1))
    return title[:m.start()].strip(), years


def project_status(budget, five_year, future, continuing):
    """Where a project sits in time, read from its funding columns:
      completed — money only in 'budgeted/expended through FY2026', nothing
                  programmed ahead, not a recurring program (already built);
      ongoing   — a continuing/recurring program (perpetual maintenance);
      active    — has FY2027+ funding: being built or renovated now/soon."""
    if five_year == 0 and future == 0 and budget > 0 and not continuing:
        return "completed"
    if continuing:
        return "ongoing"
    if five_year > 0 or future > 0:
        return "active"
    return "unspecified"


def extract():
    text, pages = fetch_text()
    districts = parse_districts(text)
    narrative = parse_narrative(text.splitlines())
    raw = parse_projects(pages)
    run_id = f"{SOURCE_ID}-{EXTRACTOR_VERSION}"
    records, seen = [], {}
    for p in raw:
        title, project_districts = enrich_title_district(p["title"], p["section"], districts)
        title, bond_years = split_bond_years(title)
        narr = narrative.get(_name_key(title), {})
        work_type = narr.get("work_type") or _title_work_type(title)
        key = _norm(f"{p['section']} - {title}")
        pid = p["ids"][0] if p["ids"] else "syn-" + re.sub(r"[^a-z0-9]+", "-", key).strip("-")
        seen[pid] = seen.get(pid, 0) + 1
        if seen[pid] > 1:
            pid = f"{pid}#{seen[pid]}"
        records.append({
            "project_id": f"{SOURCE_ID}-{pid}",
            "source_project_numbers": p["ids"],
            "title": title,
            "bond_years": bond_years,
            "work_type": work_type,
            "completion_fy": narr.get("completion_fy"),
            "funding_status": narr.get("funding_status"),
            "function": p["section"],
            "districts": project_districts,
            "funding_sources": p["funds"],
            "funding_source_labels": [FUNDING_SOURCES[c] for c in p["funds"]],
            "unit": "usd_thousands",
            "continuing": p["continuing"],
            "budgeted_through_fy2026": p["budget"],
            "fiscal_years": {str(y): p["fy"][y] for y in FY_YEARS},
            "five_year_total": sum(p["fy"].values()),
            "printed_five_year_total": p["printed_subtotal"],
            "future_total": p["future"],
            "total": p["total"],
            "status": project_status(p["budget"], sum(p["fy"].values()),
                                     p["future"], p["continuing"]),
            "source_url": SOURCE_URL,
            "data_source_url": PDF_URL,
            "pdf_page": p["page"],
            "provenance": {"source_id": SOURCE_ID,
                           "extractor_version": EXTRACTOR_VERSION,
                           "run_id": run_id},
        })
    run_meta = {"source_id": SOURCE_ID, "extractor_version": EXTRACTOR_VERSION,
                "schema_version": schema.SCHEMA_VERSION,
                "jurisdiction": JURISDICTION, "edition": FISCAL_TITLE,
                "row_counts": {"capital_projects": len(records)}}
    return {"capital_projects": records}, run_meta


def gate(records):
    """Project-appropriate gate. The hard, defining invariant is per-project
    reconciliation: where the document prints a project's FY2027-2031 subtotal,
    it must equal the sum of that project's parsed fiscal-year columns. A
    mismatch means a dollar landed in the wrong column — the projects analogue
    of the votes gate's 'counts must equal the tally of positions'."""
    findings = []
    projects = records["capital_projects"]
    for p in projects:
        errs = schema.structural_errors("capital_project", p)
        for e in errs:
            findings.append({"layer": "schema", "check": "malformed",
                             "ref": p.get("project_id", "?"), "msg": e})
        printed = p.get("printed_five_year_total")
        if printed is not None and printed != p["five_year_total"]:
            findings.append(
                {"layer": "gate", "check": "subtotal_mismatch",
                 "ref": p["project_id"],
                 "msg": f"printed FY{FY_FIRST}-{FY_LAST} subtotal ${printed} != "
                        f"sum of parsed years ${p['five_year_total']} — a dollar "
                        "column was misassigned"})
    ids = [p["project_id"] for p in projects]
    dupes = {i for i in ids if ids.count(i) > 1}
    if dupes:
        findings.append({"layer": "gate", "check": "duplicate_project_id",
                         "ref": ", ".join(sorted(dupes))[:120],
                         "msg": "project ids must be unique"})
    if len(projects) < 100:
        findings.append({"layer": "gate", "check": "too_few_projects", "ref": "run",
                         "msg": f"only {len(projects)} projects — a full county CIP "
                                "enumerates many more; tables were missed"})
    funcs = {p["function"] for p in projects}
    if len(funcs) < 8:
        findings.append({"layer": "gate", "check": "too_few_functions", "ref": "run",
                         "msg": f"only {len(funcs)} functional areas parsed"})
    return findings


def land(records, meta, log=print):
    note = ("parsed from the county's published Capital Improvement Program; "
            "single-source, no oracle wired — ingested, never certifiable "
            "as-is. Reconciles per-project against the CIP's printed subtotals.")
    for p in records["capital_projects"]:
        p["certification"] = {"status": "quarantined", "method": None, "note": note}
    store = {
        "capital_projects": records["capital_projects"],
        "meta": {
            "title": f"{JURISDICTION} — Capital Projects",
            "sub": f"{meta['edition']} · {len(records['capital_projects'])} "
                   "funded projects · auto-parsed from the CIP, single-source",
            "kind": "capital_projects",
            "jurisdiction": JURISDICTION,
            "edition": meta["edition"],
            "extractor": "cip_extractor.py",
            "extractor_version": EXTRACTOR_VERSION,
            "unit": "usd_thousands",
            "source_url": SOURCE_URL,
            "data_source_url": PDF_URL,
            "generated": datetime.datetime.now().isoformat(timespec="seconds"),
        },
    }
    path = STORE / f"{SOURCE_ID}.json"
    path.write_text(json.dumps(store, indent=1))
    log(f"landed {len(records['capital_projects'])} projects -> {path.name}")


def main():
    commit = "--commit" in sys.argv
    records, meta = extract()
    projects = records["capital_projects"]
    findings = gate(records)
    print(f"extracted {len(projects)} projects across "
          f"{len({p['function'] for p in projects})} functional areas")
    total = sum(p["total"] for p in projects)
    print(f"total programmed (grand totals, $000s): ${total:,} "
          f"(~${total / 1e6:.1f}B)")
    joined = sum(1 for p in projects if p["districts"])
    print(f"district-joined: {joined}/{len(projects)}")
    if findings:
        print(f"GATE: {len(findings)} findings")
        for f in findings[:12]:
            print(f"  [{f['layer']}/{f['check']}] {f['ref']}: {f['msg'][:110]}")
        return 1
    print("GATE PASSED (schema + per-project subtotal reconciliation)")
    if commit:
        land(records, meta)
    else:
        print("(dry run — pass --commit to land in the store)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
