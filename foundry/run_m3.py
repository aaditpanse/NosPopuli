"""M3 experiment: onboard a structurally different, non-Legistar source cold.

    python run_m3.py snapshot     freeze source data + CFMS oracle (network)
    python run_m3.py run          synthesis loop (calls the Anthropic API)

Source: Los Angeles City Council on PrimeGov. Structurally unlike
Pittsburgh in every way that matters: hidden JSON API for meetings, but
items and votes live only in the Journal PDF (the rendered/PDF rung);
file numbers are NN-NNNN(-Sn); the second source (City Clerk CFMS) is a
genuinely independent system, not the same vendor.

The M3-defining constraint: there is NO prior extractor, so no golden set
to reproduce. The gate is the harness alone — structural + consistency +
cross-source reconciliation against CFMS — plus presence floors. A passing
candidate's output is then frozen as the LA golden set after human
spot-check, closing the loop for this source's future M2-style repairs.
"""

import argparse
import json
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).parent))

import harness
import la_oracle
import sandbox2
import synthesize

FOUNDRY = pathlib.Path(__file__).parent
GOLDEN = FOUNDRY / "golden" / "losangeles"
ARTIFACTS = FOUNDRY / "extractors" / "la-primegov"
CACHE_PATH = GOLDEN / "http_cache.json"

MEETING_IDS = [18334, 18364]  # 2026-06-10 and 2026-06-16 regular Council meetings
LIST_URL = "https://lacity.primegov.com/api/v2/PublicPortal/ListArchivedMeetings"
DOC_URL = "https://lacity.primegov.com/Public/CompiledDocument/{id}"


def load_cache():
    return json.loads(CACHE_PATH.read_text()) if CACHE_PATH.exists() else {}


def journal_doc(meeting):
    return next(d for d in meeting["documentList"] if d["templateName"] == "Journal")


def snapshot():
    GOLDEN.mkdir(parents=True, exist_ok=True)
    cache = load_cache()
    rt = sandbox2.Runtime(cache)
    meetings = rt.fetch_json(LIST_URL, {"year": 2026})
    assertions, meta = {}, {"meeting_ids": MEETING_IDS, "meetings": {}}
    for mid in MEETING_IDS:
        meeting = next(m for m in meetings if m["id"] == mid)
        date = meeting["dateTime"][:10]
        text = rt.fetch_text(DOC_URL.format(id=journal_doc(meeting)["id"]))
        fns = la_oracle.journal_file_numbers(text)
        print(f"meeting {mid} ({date}): {len(fns)} file numbers in journal; "
              f"consulting CFMS...")
        a = la_oracle.extract_assertions(rt, date, fns)
        assertions[f"la-primegov-{mid}"] = a
        meta["meetings"][str(mid)] = {"date": date, "file_numbers": fns}
        voted = sum(len(v) for v in a["items"].values())
        print(f"  CFMS asserts {voted} recorded votes across {len(a['items'])} files")
    CACHE_PATH.write_text(json.dumps(cache))
    (GOLDEN / "assertions.json").write_text(json.dumps(assertions, indent=1))
    (GOLDEN / "meta.json").write_text(json.dumps(meta, indent=1))
    print(f"\nsnapshot written to {GOLDEN} ({len(cache)} cached responses)")


def build_messages(cache, meta):
    schema_src = (FOUNDRY / "schema.py").read_text()
    mid = MEETING_IDS[1]
    meetings = cache[f"{LIST_URL}?year=2026"]
    meeting = next(m for m in meetings if m["id"] == mid)
    journal_text = cache[DOC_URL.format(id=journal_doc(meeting)["id"])]
    lines = journal_text.splitlines()
    excerpt = "\n".join(lines[:110])
    # add a non-unanimous vote block if one exists, so the model sees named
    # Nays/Absent lists, not just "(0)"
    for i, ln in enumerate(lines):
        if "Nays:" in ln and "(0)" not in ln:
            excerpt += "\n[...]\n" + "\n".join(lines[max(0, i - 25):i + 6])
            break

    prompt = f"""## Source profile

Los Angeles City Council on PrimeGov. Two endpoints:

`GET {LIST_URL}?year=YYYY` — all meetings for a year (JSON). One entry
(trimmed) for meeting id {mid}:
```json
{json.dumps({k: v for k, v in meeting.items() if k in ('id', 'dateTime', 'title', 'location')}, indent=1)}
```
Each entry also carries `documentList`: objects with `id`, `templateName`
(e.g. "Agenda", "HTML Agenda", "Journal"), `compileOutputType`. The
**Journal** is the official record of what happened at the meeting.

`GET {DOC_URL.format(id='{documentId}')}` — a compiled document. The
runtime's `fetch_text` returns its text (PDFs are converted). Journal text
for meeting {mid} begins:
```
{excerpt}
```
The pattern continues: numbered items "(N)  file-number", item text, a
disposition line (e.g. "Adopted", "Adopted as Amended - FORTHWITH"), and
for recorded votes an "Ayes: ...; Nays: ...; Absent: ..." block where each
group has a parenthesized count and, when nonzero, a wrapped list of last
names. Beware: the PDF text contains stray zero-width characters inside
some names, and name lists wrap across lines.

## Domain schema (schema.py, verbatim)

```python
{schema_src}```

## Artifact contract

Write a complete Python module implementing an extractor for source
`la-primegov`.

- Define `EXTRACTOR_VERSION = "1"`.
- Define `extract(rt, meeting_ids) -> (records, run_meta)`.
- `rt` is the injected runtime: `rt.fetch_json(url, params=None)` and
  `rt.fetch_text(url, params=None)`, absolute URLs, the only I/O available.
- One meeting record per given PrimeGov meeting id, plus that meeting's
  agenda items and recorded votes, all extracted from the meeting list
  entry and its Journal text.
- `run_meta` = {{"source_id": "la-primegov", "extractor_version": ...,
  "schema_version": "{__import__('schema').SCHEMA_VERSION}", "event_ids": [...],
  "row_counts": {{type: count}}}}.

Target-schema conventions for this source:
- meeting_id = f"la-primegov-{{meetingId}}"
  item_id    = f"la-primegov-item-{{meetingId}}-{{itemNo}}"
  vote_id    = f"la-primegov-vote-{{meetingId}}-{{itemNo}}"
  where itemNo is the journal's item number (the N in "(N)").
- meeting: body "City Council", date from the meeting entry, attendance
  from the journal's Roll Call (Members Present / Absent), source_url and
  minutes_url both the Journal document URL.
- One agenda_item per numbered journal item that has a file number. title:
  the item's descriptive text (first sentence or paragraph is fine, must be
  non-empty). action: the disposition line. result: "pass" if the item's
  final vote block has Ayes > Nays, "fail" if it has Ayes <= Nays, None if
  no recorded vote.
- An item can be voted MORE THAN ONCE (e.g. a "Question Whether to
  Substitute" vote followed by the substitute motion's vote, each with its
  own disposition line and Ayes block). Emit one vote_event per Ayes block,
  in journal order, with vote_id = f"la-primegov-vote-{{meetingId}}-{{itemNo}}-{{seq}}"
  where seq is 1-based within the item. positions come from the named
  members in each group (names exactly as printed, minus wrapping), counts
  from the parenthesized numbers, result by the same Ayes > Nays rule.
- members: one record per distinct person seen (roll call and votes),
  sorted by name."""
    return [{"role": "user", "content": prompt}]


# Cross-source disagreements adjudicated by a human (spec: certify loop).
# These findings are TRUE — the sources genuinely disagree — so they must
# not fail the extractor's gate; the implicated records stay quarantined.
ADJUDICATED = {
    ("vote_mismatch", "la-primegov-18364/17-0714-S1"):
        "2026-07-09 human adjudication: Journal (item 55) records the 11-4 "
        "vote with McOsker=Aye, Soto-Martínez=Nay; CFMS records the same "
        "tally with McOsker=NO, Soto-Martínez=YES. The extractor matches "
        "the Journal text exactly; LA's two official systems disagree. "
        "Vote stays quarantined (ingested, never certified).",
    ("vote_mismatch", "la-primegov-17723/25-0916"):
        "2026-07-09 human adjudication: for the 2026-06-24 vote the Journal "
        "records John Lee as Absent (10-0-5); CFMS records him as NO "
        "(10-1-4). Extractor matches the Journal; sources disagree. "
        "Quarantined.",
    ("vote_coverage", "la-primegov-17723/26-0824"):
        "2026-07-09 human adjudication: the Journal records a vote but CFMS "
        "has no vote block for this file yet — second source missing, not "
        "an extractor error. Ingested, not certifiable until CFMS catches "
        "up or a spot-check clears it.",
}


def presence_findings(records):
    findings = []
    by_meeting = {}
    for ve in records.get("vote_events", []):
        by_meeting.setdefault(ve.get("meeting_id"), []).append(ve)
    meeting_ids = {m.get("meeting_id") for m in records.get("meetings", [])}
    for mid in MEETING_IDS:
        key = f"la-primegov-{mid}"
        if key not in meeting_ids:
            findings.append({"layer": "gate", "check": "missing_meeting",
                             "ref": key, "msg": "meeting record absent"})
        elif not by_meeting.get(key):
            findings.append({"layer": "gate", "check": "no_votes",
                             "ref": key, "msg": "no vote_events extracted for meeting"})
    if len(records.get("agenda_items", [])) < 10:
        findings.append({"layer": "gate", "check": "too_few_items", "ref": "run",
                         "msg": f"only {len(records.get('agenda_items', []))} agenda items"})
    return findings


def run_experiment(attempts):
    cache = load_cache()
    assertions = json.loads((GOLDEN / "assertions.json").read_text())
    meta = json.loads((GOLDEN / "meta.json").read_text())
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    out_path = FOUNDRY / "data" / "m3_out.json"
    out_path.parent.mkdir(exist_ok=True)

    messages = build_messages(cache, meta)
    usages, passed, records = [], False, None
    t0 = time.time()

    for attempt in range(1, attempts + 1):
        print(f"attempt {attempt}: synthesizing with {synthesize.MODEL}...")
        code, assistant_content, usage = synthesize.generate(messages)
        usages.append(usage)
        artifact = ARTIFACTS / f"v1_attempt{attempt}.py"
        artifact.write_text(code)
        print(f"  artifact: {artifact.relative_to(FOUNDRY)} "
              f"({len(code.splitlines())} lines, {usage.output_tokens} output tokens)")

        records, error = sandbox2.run_artifact(artifact, MEETING_IDS, out_path, CACHE_PATH)
        findings = []
        if error is None:
            findings = harness.run_all(records, assertions)
            if not any(f["check"] == "malformed_root" for f in findings):
                findings = presence_findings(records) + findings
            adjudicated = [f for f in findings
                           if (f["check"], f["ref"]) in ADJUDICATED]
            findings = [f for f in findings
                        if (f["check"], f["ref"]) not in ADJUDICATED]
            for f in adjudicated:
                print(f"  adjudicated source disagreement (stays quarantined): "
                      f"[{f['check']}] {f['ref']}")
        else:
            print("  execution failed")

        if error is None and not findings:
            passed = True
            print("  GATE PASSED: harness clean, reconciled against CFMS")
            break
        print(f"  gate failed: {len(findings)} findings")
        for f in findings[:6]:
            print(f"    [{f['layer']}/{f['check']}] {f['ref']}: {f['msg'][:130]}")
        messages.append({"role": "assistant", "content": assistant_content})
        messages.append(synthesize.feedback_message(error, findings))

    wall = time.time() - t0
    total_in = sum((u.input_tokens or 0) + (getattr(u, 'cache_creation_input_tokens', 0) or 0)
                   + (getattr(u, 'cache_read_input_tokens', 0) or 0) for u in usages)
    total_out = sum(u.output_tokens or 0 for u in usages)
    print(f"\nM3 metrics (onboarding a cold non-Legistar source, {synthesize.MODEL})")
    print(f"  attempts:      {len(usages)}")
    print(f"  tokens:        {total_in:,} in / {total_out:,} out")
    print(f"  LLM cost:      ${synthesize.cost_usd(usages):.2f}")
    print(f"  wall time:     {wall / 60:.1f} min")

    if passed:
        disputed = {f["ref"] for f in harness.reconcile(records, assertions)}
        certified = 0
        for ve in records["vote_events"]:
            ref = f"{ve['meeting_id']}/{ve['file_number']}"
            ok = ref not in disputed and ve["meeting_id"] not in disputed
            ve["certification"] = {
                "status": "certified" if ok else "quarantined",
                "method": "cross-source" if ok else None,
                "note": None if ok else ADJUDICATED.get(("vote_mismatch", ref))}
            certified += ok
        (GOLDEN / "records.json").write_text(json.dumps(records, indent=1))
        n_votes = len(records["vote_events"])
        asserted = sum(len(v) for a in assertions.values() for v in a["items"].values())
        print(f"\n  extracted: {len(records['meetings'])} meetings, "
              f"{len(records['agenda_items'])} items, {n_votes} vote events "
              f"({asserted} independently asserted by CFMS)")
        print(f"  certification: {certified}/{n_votes} vote events certified via "
              f"CFMS; {n_votes - certified} quarantined (incl. the adjudicated "
              f"Journal-vs-CFMS disagreement)")
        print(f"  candidate output frozen as {GOLDEN / 'records.json'} — "
              f"pending human spot-check before it becomes the golden set.")
        print("\n  spot-check sample (verify against the journal/CFMS):")
        for ve in records["vote_events"][:2]:
            print(f"   {ve['vote_id']} {ve['file_number']}: {ve['counts']} -> {ve['result']}")
        return 0
    print("\nverdict: FAILED — no candidate cleared the harness+CFMS gate.")
    return 1


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("snapshot")
    run = sub.add_parser("run")
    run.add_argument("--attempts", type=int, default=4)
    args = parser.parse_args()
    if args.cmd == "snapshot":
        snapshot()
        return 0
    return run_experiment(args.attempts)


if __name__ == "__main__":
    sys.exit(main())
