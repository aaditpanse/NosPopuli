"""Historical backfill: point the promoted extractors backwards.

    python backfill.py pittsburgh [--window 12]
    python backfill.py losangeles [--window 6]
    python backfill.py loudoun [--years 2026]

Deterministic runtime + oracle re-extraction + certification, exactly like
an M4 refresh, but over a deep window. Results merge by record id into
data/store/<source>.json so repeated runs are idempotent and windows can
be widened incrementally. Loudoun has no second source wired yet, so its
records merge as ingest-only (every one quarantined).
"""

import argparse
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))

import certify as certify_mod
import harness
import loudoun_extractor
import run_m4
import sandbox2
from run_m3 import ADJUDICATED

FOUNDRY = pathlib.Path(__file__).parent
STORE = FOUNDRY / "data" / "store"


def merge(source_id, records, reconcile_findings_count):
    STORE.mkdir(parents=True, exist_ok=True)
    path = STORE / f"{source_id}.json"
    store = json.loads(path.read_text()) if path.exists() else \
        {"meetings": {}, "agenda_items": {}, "vote_events": {}, "members": {}}
    id_fields = {"meetings": "meeting_id", "agenda_items": "item_id",
                 "vote_events": "vote_id", "members": "name"}
    counts = {"added": 0, "replaced": 0, "unchanged": 0}
    for rtype, id_field in id_fields.items():
        for rec in records.get(rtype, []):
            key = rec[id_field]
            prior = store[rtype].get(key)
            if prior is None:
                counts["added"] += 1
            elif json.dumps(prior, sort_keys=True) == json.dumps(rec, sort_keys=True):
                # Byte-identical content: leave the stored record alone.
                # Several synthesized extractors build attendance from a set,
                # so re-serializing an unchanged record reorders its keys and
                # produces a daily commit that carries no data. Comparing
                # canonically keeps the diff honest about what actually moved.
                counts["unchanged"] += 1
                continue
            else:
                counts["replaced"] += 1
            store[rtype][key] = rec
    path.write_text(json.dumps(store, indent=1))
    totals = {k: len(v) for k, v in store.items()}
    certified = sum(1 for k in ("meetings", "agenda_items", "vote_events")
                    for r in store[k].values()
                    if r.get("certification", {}).get("status") == "certified")
    print(f"  store: +{counts['added']} new, {counts['replaced']} updated, "
          f"{counts['unchanged']} unchanged -> {totals} "
          f"({certified} certified total, {reconcile_findings_count} open disputes this run)")
    return counts


def backfill_pittsburgh(window):
    records, assertions, error = run_m4.refresh_pittsburgh(window)
    if error:
        raise SystemExit(f"extractor failed:\n{error[-400:]}")
    validation = harness.structural(records) + harness.consistency(records)
    findings, counts = certify_mod.certify(records, assertions, ADJUDICATED)
    new = [f for f in findings if (f["check"], f["ref"]) not in ADJUDICATED]
    _report("pittsburgh-legistar", records, validation, new, counts)
    merge("pittsburgh-legistar", records, len(new))


def backfill_losangeles(window):
    records, assertions, error = run_m4.refresh_losangeles(window)
    if error:
        raise SystemExit(f"extractor failed:\n{error[-400:]}")
    validation = harness.structural(records) + harness.consistency(records)
    findings, counts = certify_mod.certify(records, assertions, ADJUDICATED)
    new = [f for f in findings if (f["check"], f["ref"]) not in ADJUDICATED]
    _report("la-primegov", records, validation, new, counts)
    merge("la-primegov", records, len(new))


def backfill_loudoun(years, recertify=True):
    cache_path = FOUNDRY / "data" / "discovery" / "loudoun_http_cache.json"
    rt = sandbox2.Runtime(json.loads(cache_path.read_text())
                          if cache_path.exists() else {})
    records, run_meta = loudoun_extractor.extract(rt, years)
    cache_path.write_text(json.dumps(rt.cache))
    inconsistent = [v["vote_id"] for v in records["vote_events"]
                    if not v["tally_consistent"]]
    for rtype in ("meetings", "vote_events"):
        for rec in records[rtype]:
            rec["certification"] = {
                "status": "quarantined", "method": None,
                "note": "ingest-only until the promoted oracle recertifies"}
    print(f"  extracted {run_meta['row_counts']} | parser flags: "
          f"{run_meta['flags'] or 'none'} | tally-inconsistent: "
          f"{len(inconsistent)} ({', '.join(inconsistent[:4])})")
    merge("loudoun-bos", records, 0)
    # The merge above re-stamps every re-extracted record as quarantined, so
    # certification has to be re-earned in the same breath — otherwise a
    # routine backfill silently un-certifies a source that has a promoted
    # oracle. Local import: run_oracle imports run_onboard, which imports us.
    store = json.loads((STORE / "loudoun-bos.json").read_text())
    if recertify and store.get("meta", {}).get("oracle_artifact"):
        import run_oracle
        run_oracle.recertify("loudoun", print)


def _report(source_id, records, validation, new_disputes, counts):
    print(f"  extracted: {len(records['meetings'])} meetings, "
          f"{len(records['agenda_items'])} items, "
          f"{len(records['vote_events'])} vote events")
    if validation:
        print(f"  VALIDATION FINDINGS ({len(validation)}) — repair-loop candidate:")
        for f in validation[:4]:
            print(f"    [{f['layer']}/{f['check']}] {f['ref']}: {f['msg'][:100]}")
    for f in new_disputes[:6]:
        print(f"  new dispute [{f['check']}] {f['ref']}: {f['msg'][:120]}")
    pct = counts["certified"] / counts["total"] if counts["total"] else 0
    print(f"  certified this run: {counts['certified']}/{counts['total']} ({pct:.0%})")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("source",
                        choices=["pittsburgh", "losangeles", "loudoun"])
    parser.add_argument("--window", type=int, default=None)
    parser.add_argument("--years", type=int, nargs="+", default=[2026])
    args = parser.parse_args()
    print(f"=== backfill {args.source} ===")
    if args.source == "pittsburgh":
        backfill_pittsburgh(args.window or 12)
    elif args.source == "losangeles":
        backfill_losangeles(args.window or 6)
    else:
        backfill_loudoun(args.years)
    return 0


if __name__ == "__main__":
    sys.exit(main())
