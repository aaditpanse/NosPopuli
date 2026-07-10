"""Generic profile-driven onboarding: discovery profile -> synthesized
extractor -> harness gate -> quarantined records in the store.

    python run_onboard.py <slug> [--meetings 3] [--attempts 4]

Nothing jurisdiction-specific lives here. The LLM gets the agent-discovered
source profile verbatim, live samples of the documents the profile points
at, and the domain schema — and must write the extractor. The gate is the
harness WITHOUT the cross-source layer (no oracle is wired yet), plus
presence floors, so output can be wrong-but-flagged, never structurally
bunk. Every record lands quarantined: single-source jurisdictions are
ingested, never certified (spec, quarantine rule).
"""

import argparse
import json
import pathlib
import re
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).parent))

import harness
import sandbox2
import synthesize
from backfill import merge

FOUNDRY = pathlib.Path(__file__).parent
STORE = FOUNDRY / "data" / "store"

CONTRACT = """## Artifact contract

Write a complete Python module implementing an extractor for source
`{source_id}`, using ONLY the source profile above (produced by a discovery
agent that verified those URLs) and the document samples.

- Define `EXTRACTOR_VERSION = "1"`.
- Define `extract(rt, max_meetings) -> (records, run_meta)`.
- `rt` is the injected runtime: `rt.fetch_json(url, params=None)` and
  `rt.fetch_text(url, params=None)` (absolute URLs; PDFs are converted to
  text). The only I/O available. Be robust: skip meetings whose documents
  are missing or unparseable rather than crashing.
- Enumerate meetings the way the profile describes, take the most recent
  `max_meetings` COMPLETED meetings that have an actions/minutes document,
  newest first.
- `records` = {{"meetings": [...], "agenda_items": [...], "vote_events":
  [...], "members": [...]}} in the domain schema. id conventions:
  meeting_id = f"{source_id}-{{date}}" (date as YYYY-MM-DD),
  item_id / vote_id prefixed with meeting_id plus a stable suffix.
- meeting.attendance: derive from the documents (roll call / present-absent
  lists). If the jurisdiction has no file-number system, set file_number to
  null (allowed by schema {schema_version}).
- meeting.source_url must be a HUMAN-VIEWABLE page (the meeting's public
  page or archive listing) — never a raw data/handler endpoint (.ashx,
  .asmx, api paths). Keep machine endpoints in a `data_source_url` field.
- vote_events need per-member positions. If votes are recorded as narrative
  prose (movers, seconders, "carried by unanimous vote", named dissenters/
  abstentions/absences), derive positions from the attendance roster minus
  the named exceptions — and only emit a vote_event where the document
  EXPLICITLY records a motion/action outcome for that item ("APPROVED",
  "carried", "ADOPTED", a tally). Never default a vote for section headings,
  recesses, presentations, or hearing listings that show no action language:
  a fabricated 9-0 is worse than no record. Parse the vote language of each
  motion — narrative minutes record dissent and split tallies ("carried by a
  vote of nine, Supervisor X voting 'NAY'", abstentions, absences); a run
  where every vote is a full-roster unanimous aye is rejected by the gate.
  counts must equal the tally of positions. result: "pass" if the motion
  carried, "fail" otherwise.
- members: one record per distinct person seen.
- `run_meta` = {{"source_id": "{source_id}", "extractor_version": ...,
  "schema_version": "{schema_version}", "row_counts": {{type: count}}}}.
- Deterministic, stdlib only, no LLM, no network beyond `rt`.
- Be economical: total runtime budget is ~8 minutes. Fetch only what the
  extraction needs — where an actions/minutes summary exists, use it and do
  NOT download full agenda-packet documents (often hundreds of pages)."""


def build_messages(profile, slug, rt):
    schema_src = (FOUNDRY / "schema.py").read_text()
    primary = profile.get("primary_source", {})
    urls = list(primary.get("base_urls", []))[:1] + \
        list(primary.get("sample_document_urls", []))[:3]
    samples = []
    for url in urls:
        if "{" in url:  # templated pattern, not fetchable
            continue
        try:
            body = rt.fetch_text(url)
        except Exception as exc:
            samples.append(f"`{url}` -> FETCH FAILED: {exc}")
            continue
        if "<html" in body[:2000].lower():
            body = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", body, flags=re.S | re.I)
            body = re.sub(r"<[^>]+>", " ", body)
            body = re.sub(r"[ \t]+", " ", body)
        samples.append(f"`{url}` (excerpt of {len(body)} chars):\n```\n{body[:4500]}\n```")

    prompt = (f"## Source profile (agent-discovered, URLs verified)\n\n"
              f"```json\n{json.dumps(profile, indent=1)}\n```\n\n"
              f"## Live document samples\n\n" + "\n\n".join(samples) +
              f"\n\n## Domain schema (schema.py, verbatim)\n\n"
              f"```python\n{schema_src}```\n\n" +
              CONTRACT.replace("{source_id}", f"{slug}-bos")
                      .replace("{schema_version}",
                               __import__("schema").SCHEMA_VERSION))
    return [{"role": "user", "content": prompt}]


def floors(records):
    import datetime
    findings = []
    dates = sorted(m.get("date", "") for m in records.get("meetings", []))
    if dates and dates[-1] < (datetime.date.today()
                              - datetime.timedelta(days=120)).isoformat():
        findings.append({"layer": "gate", "check": "stale_meetings", "ref": "run",
                         "msg": f"newest meeting is {dates[-1]} — enumerate the "
                                "CURRENT meetings, not an older archive section"})
    if len(records.get("meetings", [])) < 2:
        findings.append({"layer": "gate", "check": "too_few_meetings", "ref": "run",
                         "msg": f"only {len(records.get('meetings', []))} meetings extracted"})
    if len(records.get("vote_events", [])) < 8:
        findings.append({"layer": "gate", "check": "too_few_votes", "ref": "run",
                         "msg": f"only {len(records.get('vote_events', []))} vote events"})
    votes = [v for v in records.get("vote_events", []) if isinstance(v, dict)]
    if len(votes) >= 20:
        positions = [p.get("position") for v in votes
                     for p in v.get("positions", []) if isinstance(p, dict)]
        if positions and all(pos == "aye" for pos in positions):
            findings.append(
                {"layer": "gate", "check": "uniform_votes", "ref": "run",
                 "msg": f"all {len(votes)} vote events are full-roster unanimous "
                        "ayes — narrative minutes record dissent ('voting NAY'), "
                        "abstentions, and absences; parse each motion's actual "
                        "vote language instead of defaulting the roster to aye, "
                        "and emit NO vote_event where no motion or tally is "
                        "explicitly recorded"})
    for m in records.get("meetings", []):
        if not m.get("attendance"):
            findings.append({"layer": "gate", "check": "no_attendance",
                             "ref": m.get("meeting_id", "?"),
                             "msg": "meeting has no derived attendance"})
    return findings


def onboard(slug, meetings=3, attempts=4, log=print, prog=None):
    """Full profile-driven onboarding. Returns source_id on success, None on
    failure. Callable from the search pipeline or the CLI."""
    profile = json.loads(
        (FOUNDRY / "data" / "discovery" / f"{slug}_profile.json").read_text())
    source_id = f"{slug}-bos"
    cache_path = FOUNDRY / "data" / "onboard" / f"{slug}_http_cache.json"
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    rt = sandbox2.Runtime(json.loads(cache_path.read_text())
                          if cache_path.exists() else {})
    messages = build_messages(profile, slug, rt)
    cache_path.write_text(json.dumps(rt.cache))

    artifacts = FOUNDRY / "extractors" / source_id
    artifacts.mkdir(parents=True, exist_ok=True)
    out_path = FOUNDRY / "data" / "onboard" / f"{slug}_out.json"
    usages, records, passed = [], None, False
    t0 = time.time()

    for attempt in range(1, attempts + 1):
        if prog:
            prog((attempt - 1) / attempts, f"synthesizing extractor (attempt {attempt})")
        log(f"attempt {attempt}: synthesizing with {synthesize.MODEL}...")
        code, assistant_content, usage = synthesize.generate(messages)
        usages.append(usage)
        artifact = artifacts / f"v1_attempt{attempt}.py"
        artifact.write_text(code)
        log(f"  artifact: {artifact.name} ({len(code.splitlines())} lines)")
        if prog:
            prog((attempt - 0.5) / attempts, f"running candidate (attempt {attempt})")
        records, error = sandbox2.run_artifact(
            artifact, [meetings], out_path, cache_path)
        findings = []
        if error is None:
            findings = harness.run_all(records)  # no oracle: structural+consistency
            if not any(f["check"] == "malformed_root" for f in findings):
                findings += floors(records)
        else:
            log("  execution failed: " + error.strip().splitlines()[-1][:120])
        if error is None and not findings:
            passed = True
            log("  GATE PASSED (structural + consistency + floors; NO oracle)")
            break
        log(f"  gate failed: {len(findings)} findings")
        for f in findings[:6]:
            log(f"    [{f['layer']}/{f['check']}] {f['ref']}: {f['msg'][:120]}")
        messages.append({"role": "assistant", "content": assistant_content})
        messages.append(synthesize.feedback_message(error, findings))

    log(f"onboarding cost: {len(usages)} attempts, "
        f"${synthesize.cost_usd(usages):.2f}, {(time.time() - t0) / 60:.1f} min")
    if not passed:
        log("verdict: FAILED — no candidate cleared the gate")
        return None

    note = ("synthesized from agent-discovered profile; single-source, no "
            "oracle wired — ingested, never certifiable as-is")
    for rtype in ("meetings", "agenda_items", "vote_events"):
        for rec in records[rtype]:
            rec["certification"] = {"status": "quarantined", "method": None,
                                    "note": note}
    merge(source_id, records, 0)
    store_path = STORE / f"{source_id}.json"
    store = json.loads(store_path.read_text())
    store["meta"] = {
        "title": profile.get("jurisdiction", slug.title()),
        "sub": f"{profile.get('primary_source', {}).get('system', '')[:80]} · "
               "auto-onboarded, single-source"}
    store_path.write_text(json.dumps(store, indent=1))
    log("spot-check sample:")
    for ve in records["vote_events"][:4]:
        exc = [f"{p['member']}:{p['position']}" for p in ve["positions"]
               if p["position"] != "aye"]
        log(f"  {ve['vote_id'][:46]}: {ve['counts']} -> {ve['result']} "
            f"| {', '.join(exc) or 'unanimous'}")
    return source_id


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("slug")
    parser.add_argument("--meetings", type=int, default=3)
    parser.add_argument("--attempts", type=int, default=4)
    args = parser.parse_args()
    return 0 if onboard(args.slug, args.meetings, args.attempts) else 1


if __name__ == "__main__":
    sys.exit(main())
