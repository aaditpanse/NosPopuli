"""Autonomous CIP onboarding: profile -> synthesized extractor -> gate ->
quarantined capital_project records -> geocode. The public-works analogue of
run_onboard.py.

    python cip_onboard.py <profile.json> [--attempts 5]

Stage 0 proved CIPs are heterogeneous — the Fairfax parser extracts 0 from
Loudoun or Prince William because every county's budget document is laid out
differently. So the extractor cannot be shared; it must be SYNTHESIZED per
county. This module does that: it hands Opus the CIP's live text samples, the
capital_project schema, the gate, and the proven Fairfax extractor as a
reference, and asks it to write a deterministic extractor for THIS county's
layout. The gate — each project's parsed fiscal-year columns must reproduce
the document's own printed multi-year subtotal — rejects any wrong extractor,
so the loop is safe to run unsupervised. Every synthesis call is budget-gated.
"""

import argparse
import datetime
import json
import pathlib
import re
import subprocess
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).parent))

import budget
import sandbox2
import schema
import synthesize
import cip_extractor  # reused as the reference extractor + gate helpers

FOUNDRY = pathlib.Path(__file__).parent
STORE = FOUNDRY / "data" / "store"

CONTRACT = """## Artifact contract — Capital Improvement Program extractor

Write a complete deterministic Python module that extracts every FUNDED
capital project from the county CIP document(s) at the URL(s) in the profile.

- Define `EXTRACTOR_VERSION = "1"`.
- Define `extract(rt, args) -> (records, run_meta)`. Ignore `args`.
- `rt.fetch_text(url)` returns the document as layout-preserving text (a PDF
  is converted with `pdftotext -layout`, so columns stay aligned by character
  position). It is the only I/O. Fetch each CIP url ONCE.
- `records = {{"capital_projects": [ ... ]}}` — one record per funded capital
  project, in the domain schema (schema.py, given below). Per record:
    project_id            f"{source_id}-<stable id>" — use the document's own
                          project number if it prints one, else a slug of the
                          title.
    title                 the project's name (e.g. "Route 28 Widening").
    function              the project's TOP-LEVEL functional category — the
                          broad area a CIP summarizes spending by (e.g.
                          Transportation, Public Safety, Parks, General
                          Government, Community Development). These are usually
                          enumerated in a summary/overview table near the front
                          AND used as the section headings that group the
                          project pages. Track the current section heading as
                          you walk the document and use it here. Do NOT use a
                          finer per-project "Program"/sub-program label that
                          collapses many projects into one or two values — the
                          gate requires the document's real category diversity.
                          The summary table lists EVERY top-level category; your
                          output must include projects under all of them. If a
                          category from that summary is missing, you skipped its
                          section — small categories (e.g. General Government)
                          are easy to miss; walk the whole document.
    funding_sources       list of the revenue-source NAMES as strings, exactly
                          as the document labels them ("General Funds", "GO
                          Bond", "NVTA 30%", ...). Values are not policed.
    fiscal_years          {{"2026": <int>, "2027": <int>, ...}} for the CIP
                          window years the profile names (fiscal_first..last),
                          each the amount programmed that year.
    five_year_total       sum of those fiscal_years values.
    printed_subtotal      the document's OWN printed multi-year total for that
                          project over the same window (the column the CIP
                          prints, e.g. "FY26-FY31"). REQUIRED where the
                          document prints one — the gate reconciles against it.
    total                 the project's total/lifetime cost as the doc states.
    districts             magisterial district(s) if stated, else [].
    unit                  MUST be "usd_thousands" — normalize every amount to
                          integer thousands of dollars (a doc in whole dollars:
                          divide by 1000 and round; a doc already in $000s:
                          keep). Totals like "$32.4M" are 32400 thousands.
    source_url            a human-viewable page for the CIP.
    data_source_url       the document url you parsed.
    provenance            {{"source_id": source_id, "extractor_version": "1",
                          "run_id": f"{source_id}-1"}}

## The gate (every check runs mechanically; clear ALL of them)
- at least 20 capital projects.
- at least 6 distinct `function` values.
- project_id unique across all records.
- fiscal_years keys are 4-digit year strings; every amount a non-negative int.
- RECONCILIATION (the defining check): for every project that carries a
  `printed_subtotal`, `five_year_total` (your sum of the fiscal-year columns)
  MUST equal it exactly. A mismatch means a dollar landed in the wrong column
  — read the table by its printed column positions, not by guessing.

## How to parse
- pdftotext -layout preserves horizontal alignment: find each cost table's
  column header row, record the character offset of each fiscal-year label,
  then read each data row by slicing at those offsets. Titles and rows wrap
  across physical lines — group them.
- Different CIPs format differently (function-grouped cost-summary tables vs.
  one funding/expenditure schedule per project page). Study the SAMPLES below
  and the reference extractor, then write for THIS document's actual layout.
- Deterministic, stdlib only, no network beyond `rt`, no LLM. Runtime budget
  ~8 minutes.
- `run_meta = {{"source_id": source_id, "extractor_version": "1",
  "schema_version": schema_version, "row_counts": {{"capital_projects": N}}}}`."""


def build_messages(profile, rt):
    schema_src = (FOUNDRY / "schema.py").read_text()
    reference = (FOUNDRY / "cip_extractor.py").read_text()
    urls = profile["cip_urls"]
    samples = []
    for url in urls[:2]:
        try:
            text = rt.fetch_text(url)
        except Exception as exc:
            samples.append(f"`{url}` -> FETCH FAILED: {exc}")
            continue
        head = text[:4500]
        excerpts = []
        for marker in ("Total Project Cost", "Funding Sources", "Project Cost Summaries",
                       "of Funds"):
            i = text.find(marker)
            if i >= 0:
                excerpts.append(text[max(0, i - 200):i + 1400])
            if len(excerpts) >= 2:
                break
        samples.append(f"`{url}` ({len(text)} chars) — front matter:\n```\n{head}\n```\n"
                       + "\n".join(f"…project-table region:\n```\n{e}\n```" for e in excerpts))
    prompt = (f"## CIP profile\n\n```json\n{json.dumps(profile, indent=1)}\n```\n\n"
              f"## Live document samples\n\n" + "\n\n".join(samples) +
              f"\n\n## Domain schema (schema.py, verbatim)\n\n```python\n{schema_src}```\n\n"
              + CONTRACT.replace("source_id", f'"{profile["source_id"]}"')
                        .replace("{schema_version}", schema.SCHEMA_VERSION)
                        .replace("fiscal_first..last",
                                 f'{profile["fiscal_first"]}..{profile["fiscal_last"]}')
              + f"\n\n## Reference extractor (proven on Fairfax — a DIFFERENT layout; "
                f"adapt its column-offset table-parsing approach, do not copy its "
                f"markers)\n\n```python\n{reference}\n```")
    return [{"role": "user", "content": prompt}]


def cip_gate(records, profile):
    findings = []
    projects = records.get("capital_projects", []) if isinstance(records, dict) else []
    if not isinstance(records, dict) or "capital_projects" not in records:
        return [{"layer": "schema", "check": "malformed_root", "ref": "run",
                 "msg": "records must be {'capital_projects': [...]}"}]
    for p in projects:
        for e in schema.structural_errors("capital_project", p):
            findings.append({"layer": "schema", "check": "malformed",
                             "ref": p.get("project_id", "?"), "msg": e})
        printed = p.get("printed_subtotal")
        if isinstance(printed, int):
            got = sum(v for v in p.get("fiscal_years", {}).values() if isinstance(v, int))
            if printed != got:
                findings.append(
                    {"layer": "gate", "check": "subtotal_mismatch",
                     "ref": p.get("project_id", "?"),
                     "msg": f"printed multi-year subtotal {printed} != sum of parsed "
                            f"fiscal-year columns {got} — a dollar column was misassigned"})
    ids = [p.get("project_id") for p in projects]
    dupes = sorted({i for i in ids if ids.count(i) > 1 and i})
    if dupes:
        findings.append({"layer": "gate", "check": "duplicate_project_id",
                         "ref": ", ".join(dupes)[:120], "msg": "project ids must be unique"})
    if len(projects) < 20:
        findings.append({"layer": "gate", "check": "too_few_projects", "ref": "run",
                         "msg": f"only {len(projects)} projects — tables were missed"})
    funcs = sorted({p.get("function") for p in projects})
    if len(funcs) < 6:
        findings.append({"layer": "gate", "check": "too_few_functions", "ref": "run",
                         "msg": f"only {len(funcs)} distinct functions {funcs} — you are "
                                "using a per-project 'Program' label. Use the CIP's TOP-LEVEL "
                                "categories instead (the ones in its summary table and section "
                                "headers, e.g. Transportation / Public Safety / Community "
                                "Development); track the current section heading while parsing"})
    return findings[:12]


def onboard(profile, attempts=5, log=print):
    slug = profile["source_id"]
    cache_path = FOUNDRY / "data" / "onboard" / f"{slug}_http_cache.json"
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    rt = sandbox2.Runtime(json.loads(cache_path.read_text()) if cache_path.exists() else {})
    messages = build_messages(profile, rt)
    cache_path.write_text(json.dumps(rt.cache))

    artifacts = FOUNDRY / "extractors" / slug
    artifacts.mkdir(parents=True, exist_ok=True)
    out_path = FOUNDRY / "data" / "onboard" / f"{slug}_out.json"
    usages, records, passed, artifact = [], None, False, None
    t0 = time.time()

    for attempt in range(1, attempts + 1):
        try:
            budget.check("synthesis")
        except RuntimeError as exc:
            log(f"  {exc}")
            break
        log(f"attempt {attempt}: synthesizing with {synthesize.MODEL}…")
        try:
            code, assistant, usage = synthesize.generate(messages)
        except RuntimeError as exc:
            log(f"  synthesis failed: {str(exc)[:120]}")
            continue
        usages.append(usage)
        budget.record("synthesis", synthesize.cost_usd([usage]))
        artifact = artifacts / f"v1_attempt{attempt}.py"
        artifact.write_text(code)
        log(f"  artifact: {artifact.name} ({len(code.splitlines())} lines)")
        records, error = sandbox2.run_artifact(artifact, [1], out_path, cache_path)
        findings = [] if error else cip_gate(records, profile)
        if error:
            log("  execution failed: " + error.strip().splitlines()[-1][:120])
        elif not findings:
            passed = True
            log(f"  GATE PASSED — {len(records['capital_projects'])} projects")
            break
        else:
            log(f"  gate failed: {len(findings)} findings")
            for f in findings[:5]:
                log(f"    [{f['layer']}/{f['check']}] {f['ref']}: {f['msg'][:100]}")
        messages.append({"role": "assistant", "content": assistant})
        messages.append(synthesize.feedback_message(error, findings))

    log(f"cost: {len(usages)} attempts, ${synthesize.cost_usd(usages):.2f}, "
        f"{(time.time() - t0) / 60:.1f} min")
    if not passed:
        log("verdict: FAILED — no candidate cleared the gate")
        return None
    land(profile, records, artifact, log)
    return slug


def land(profile, records, artifact, log):
    note = ("synthesized from the county's published CIP; single-source, no "
            "oracle wired — ingested, never certifiable as-is. Reconciles "
            "per-project against the CIP's printed subtotals.")
    for p in records["capital_projects"]:
        p.setdefault("districts", [])
        p["certification"] = {"status": "quarantined", "method": None, "note": note}
    store = {"capital_projects": records["capital_projects"],
             "meta": {"title": f"{profile['jurisdiction']} — Capital Projects",
                      "sub": f"{profile.get('edition', 'CIP')} · "
                             f"{len(records['capital_projects'])} funded projects · "
                             "auto-synthesized, single-source",
                      "kind": "capital_projects", "jurisdiction": profile["jurisdiction"],
                      "edition": profile.get("edition", ""), "unit": "usd_thousands",
                      "extractor": str(artifact.relative_to(FOUNDRY)),
                      "source_url": profile.get("source_url", profile["cip_urls"][0]),
                      "data_source_url": profile["cip_urls"][0],
                      "generated": datetime.datetime.now().isoformat(timespec="seconds")}}
    path = STORE / f"{profile['source_id']}.json"
    path.write_text(json.dumps(store, indent=1))
    log(f"landed {len(records['capital_projects'])} projects -> {path.name}")
    proc = subprocess.run([sys.executable, str(FOUNDRY / "geocode_projects.py"),
                           "--source", profile["source_id"]],
                          capture_output=True, text=True, timeout=900)
    log("  geocode: " + ((proc.stdout or proc.stderr).strip().splitlines() or ["(no output)"])[-1][:120])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("profile", help="path to a CIP profile json")
    ap.add_argument("--attempts", type=int, default=5)
    args = ap.parse_args()
    profile = json.loads(pathlib.Path(args.profile).read_text())
    return 0 if onboard(profile, args.attempts) else 1


if __name__ == "__main__":
    sys.exit(main())
