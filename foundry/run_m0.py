"""M0 experiment runner.

  python run_m0.py snapshot [--meetings N]   fetch golden set (network)
  python run_m0.py run [--trials K]          run the experiment (offline)

snapshot pulls the N most recent Pittsburgh City Council meetings with
final minutes through extractor v1, downloads each meeting's minutes PDF,
extracts second-source assertions, and freezes everything under
golden/pittsburgh/. That frozen set is the golden set after hand
spot-checking (see README).

run is M0 proper, entirely offline from the snapshot:
  1. harness on clean golden data -> false-alarm measurement
  2. planted-corruption trials    -> oracle recall, per layer
  3. certification pass           -> quarantine -> certified, coverage %

Exit code 1 if the meter misses any planted corruption — per the spec,
if M0 fails, stop.
"""

import argparse
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))

import harness
import inject
import schema

FOUNDRY = pathlib.Path(__file__).parent
GOLDEN = FOUNDRY / "golden" / "pittsburgh"
DATA = FOUNDRY / "data"


def snapshot(n_meetings):
    import requests

    import legistar_extractor as lx
    import minutes_extractor as mx

    events = lx.recent_final_meetings(top=n_meetings)
    print(f"fetching {len(events)} meetings via {lx.SOURCE_ID} "
          f"extractor v{lx.EXTRACTOR_VERSION}...")
    records, run_meta = lx.extract([e["EventId"] for e in events])
    print(f"  rows: {run_meta['row_counts']} in {run_meta['elapsed_seconds']}s")

    minutes_dir = GOLDEN / "minutes"
    minutes_dir.mkdir(parents=True, exist_ok=True)
    assertions = {}
    for meeting in records["meetings"]:
        pdf_path = minutes_dir / f"{meeting['meeting_id']}.pdf"
        if not pdf_path.exists():
            r = requests.get(meeting["minutes_url"], timeout=60)
            r.raise_for_status()
            pdf_path.write_bytes(r.content)
        assertions[meeting["meeting_id"]] = mx.extract_assertions(mx.pdf_to_text(pdf_path))
        print(f"  minutes for {meeting['date']}: "
              f"{sum(1 for es in assertions[meeting['meeting_id']]['items'].values() for e in es if e['votes'])} vote blocks")

    (GOLDEN / "records.json").write_text(json.dumps(records, indent=1))
    (GOLDEN / "assertions.json").write_text(json.dumps(assertions, indent=1))
    (GOLDEN / "run_meta.json").write_text(json.dumps(run_meta, indent=1))

    findings = harness.run_all(records, assertions, prior_run_meta=run_meta)
    print(f"\nsnapshot harness check: {len(findings)} findings on fresh data")
    for f in findings:
        print(f"  [{f['layer']}/{f['check']}] {f['ref']}: {f['msg']}")
    print(f"\ngolden set written to {GOLDEN}")


def load_golden():
    records = json.loads((GOLDEN / "records.json").read_text())
    assertions = json.loads((GOLDEN / "assertions.json").read_text())
    run_meta = json.loads((GOLDEN / "run_meta.json").read_text())
    return records, assertions, run_meta


def finding_key(f):
    return (f["layer"], f["check"], f["ref"])


def certify(records, assertions):
    """Quarantine -> certified. A record is certified only if the second
    source affirms it (spec module 7); everything else stays quarantined."""
    reconcile_refs = {f["ref"] for f in harness.reconcile(records, assertions)}

    for ve in records["vote_events"]:
        ref = f"{ve['meeting_id']}/{ve['file_number']}"
        ok = ve["meeting_id"] in assertions and ref not in reconcile_refs \
            and ve["meeting_id"] not in reconcile_refs
        ve["certification"] = {"status": "certified" if ok else "quarantined",
                               "method": "cross-source" if ok else None}

    votes_by_ref = {f"{v['meeting_id']}/{v['file_number']}": v["certification"]["status"]
                    for v in records["vote_events"]}
    for item in records["agenda_items"]:
        status, method = "quarantined", None
        ref = f"{item['meeting_id']}/{item['file_number']}"
        if votes_by_ref.get(ref) == "certified":
            status, method = "certified", "cross-source"  # vote reconciled member-by-member
        elif item["result"] is not None:
            entries = assertions.get(item["meeting_id"], {}).get("items", {}) \
                                .get(item["file_number"], [])
            if any(e["result"] == item["result"] for e in entries):
                status, method = "certified", "cross-source"  # outcome affirmed by minutes
        item["certification"] = {"status": status, "method": method}


def run_experiment(n_trials):
    records, assertions, run_meta = load_golden()

    print(f"golden set: {run_meta['row_counts']} "
          f"(schema v{schema.SCHEMA_VERSION}, extractor v{run_meta['extractor_version']})\n")

    baseline = harness.run_all(records, assertions, prior_run_meta=run_meta)
    baseline_keys = {finding_key(f) for f in baseline}
    print(f"1. clean-data pass: {len(baseline)} findings (false alarms)")
    for f in baseline:
        print(f"   [{f['layer']}/{f['check']}] {f['ref']}: {f['msg']}")

    print(f"\n2. planted-corruption trials ({n_trials} seeds per type)")
    print(f"   {'corruption':<20} {'expected':<12} {'detected':<10} layers that fired")
    total = caught = expected_hits = 0
    for name, (_, expected_layer) in inject.CORRUPTIONS.items():
        detected = 0
        layers = set()
        for seed in range(n_trials):
            corrupted, _desc = inject.plant(records, name, seed)
            findings = harness.run_all(corrupted, assertions, prior_run_meta=run_meta)
            new = [f for f in findings if finding_key(f) not in baseline_keys]
            if new:
                detected += 1
                layers |= {f["layer"] for f in new}
        total += n_trials
        caught += detected
        if expected_layer in layers:
            expected_hits += 1
        print(f"   {name:<20} {expected_layer:<12} {detected}/{n_trials:<8} "
              f"{', '.join(sorted(layers)) or '—'}")

    recall = caught / total
    print(f"\n   oracle recall: {caught}/{total} = {recall:.0%}"
          f" | expected layer fired for {expected_hits}/{len(inject.CORRUPTIONS)} types"
          f" | false alarms on clean data: {len(baseline)}")

    print("\n3. certification (quarantine -> certified)")
    certify(records, assertions)
    DATA.mkdir(exist_ok=True)
    (DATA / "quarantine.json").write_text(json.dumps(
        {k: records[k] for k in ("agenda_items", "vote_events")}, indent=1))
    for rtype, label in [("vote_events", "vote events"),
                         ("agenda_items", "agenda items")]:
        certified = sum(1 for r in records[rtype]
                        if r["certification"]["status"] == "certified")
        n = len(records[rtype])
        print(f"   {label}: {certified}/{n} certified ({certified / n:.0%});"
              f" {n - certified} remain quarantined (never published)")
    print(f"   quarantine store written to {DATA / 'quarantine.json'}")

    if recall < 1.0:
        print("\nM0 GATE: FAILED — the meter missed planted corruption. "
              "Per the spec: stop; do not build synthesis on top of this.")
        return 1
    print("\nM0 GATE: PASSED — every planted corruption was detected.")
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    snap = sub.add_parser("snapshot")
    snap.add_argument("--meetings", type=int, default=3)
    run = sub.add_parser("run")
    run.add_argument("--trials", type=int, default=5)
    args = parser.parse_args()

    if args.cmd == "snapshot":
        snapshot(args.meetings)
        return 0
    return run_experiment(args.trials)


if __name__ == "__main__":
    sys.exit(main())
