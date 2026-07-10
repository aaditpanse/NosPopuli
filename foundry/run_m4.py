"""M4: the full quarantine -> certify pipeline across the source set.

    python run_m4.py [--pgh-window 4] [--la-window 3]

For each registered source: discover the refresh window live, run the
PROMOTED extractor artifact (deterministic, no LLM), re-extract the second
source's assertions fresh, validate (structural / consistency / delta vs
prior refresh), certify every record, and write the certified export.
Validation findings that indicate extractor breakage (structural or
consistency layers) flag the source as a repair-loop candidate — repair
stays offline and gated, never part of the refresh itself.

Promotion note (spec module 6 human review gate): the synthesized
Pittsburgh artifact v2_attempt2 was human-reviewed against v1's output
(M1 golden reproduction) and is promoted here; LA v1_attempt3 was
human-spot-checked at M3. Both decisions are deliberate and recorded.
"""

import argparse
import json
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).parent))

import certify as certify_mod
import harness
import la_oracle
import minutes_extractor
import sandbox
import sandbox2
from run_m3 import ADJUDICATED, LIST_URL, DOC_URL, journal_doc

FOUNDRY = pathlib.Path(__file__).parent
M4 = FOUNDRY / "data" / "m4"

PGH_ARTIFACT = FOUNDRY / "extractors" / "pittsburgh-legistar" / "v2_attempt2.py"
# v2_repair_attempt2: first real repair (all-zero journal vote blocks),
# human-reviewed 2026-07-09 — the diff is the zero-block guard and nothing else.
LA_ARTIFACT = FOUNDRY / "extractors" / "la-primegov" / "v2_repair_attempt2.py"


def refresh_pittsburgh(window, artifact=None):
    import legistar_extractor as lx
    events = lx.recent_final_meetings(top=window)
    event_ids = [e["EventId"] for e in events]
    records, error = sandbox.run_artifact(
        artifact or PGH_ARTIFACT, event_ids, M4 / "pgh_out.json",
        cache_path=M4 / "pgh_http_cache.json")
    if error:
        return None, None, error
    # Oracle side, fresh each refresh: the clerk's minutes PDF per meeting.
    # Minutes URLs come from discovery, not from the artifact under test.
    rt = sandbox2.Runtime(_load(M4 / "pgh_oracle_cache.json"))
    assertions = {}
    for ev in events:
        if ev.get("EventMinutesFile"):
            text = rt.fetch_text(ev["EventMinutesFile"])
            assertions[f"pittsburgh-legistar-{ev['EventId']}"] = \
                minutes_extractor.extract_assertions(text)
    _save(M4 / "pgh_oracle_cache.json", rt.cache)
    return records, assertions, None


def refresh_losangeles(window, artifact=None):
    rt = sandbox2.Runtime(_load(M4 / "la_http_cache.json"))
    meetings = rt.fetch_json(LIST_URL, {"year": 2026})
    council = sorted(
        (m for m in meetings if m["title"].strip() == "City Council Meeting"
         and any(d["templateName"] == "Journal" for d in m["documentList"])),
        key=lambda m: m["dateTime"])[-window:]
    ids = [m["id"] for m in council]
    _save(M4 / "la_http_cache.json", rt.cache)
    records, error = sandbox2.run_artifact(
        artifact or LA_ARTIFACT, ids, M4 / "la_out.json", M4 / "la_http_cache.json")
    if error:
        return None, None, error
    rt = sandbox2.Runtime(_load(M4 / "la_http_cache.json"))
    assertions = {}
    for m in council:
        text = rt.fetch_text(DOC_URL.format(id=journal_doc(m)["id"]))
        fns = la_oracle.journal_file_numbers(text)
        assertions[f"la-primegov-{m['id']}"] = la_oracle.extract_assertions(
            rt, m["dateTime"][:10], fns)
    _save(M4 / "la_http_cache.json", rt.cache)
    return records, assertions, None


SOURCES = {
    "pittsburgh-legistar": (refresh_pittsburgh, PGH_ARTIFACT),
    "la-primegov": (refresh_losangeles, LA_ARTIFACT),
}


def _load(path):
    return json.loads(path.read_text()) if path.exists() else {}


def _save(path, obj):
    path.write_text(json.dumps(obj))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--pgh-window", type=int, default=4)
    parser.add_argument("--la-window", type=int, default=3)
    args = parser.parse_args()
    M4.mkdir(parents=True, exist_ok=True)

    windows = {"pittsburgh-legistar": args.pgh_window, "la-primegov": args.la_window}
    fleet = {"certified": 0, "total": 0}
    report = {}
    t0 = time.time()

    for source_id, (refresh, artifact) in SOURCES.items():
        print(f"=== {source_id} (artifact {artifact.name}) ===")
        records, assertions, error = refresh(windows[source_id])
        if error:
            print(f"  EXTRACTOR FAILED — repair-loop candidate:\n{error[-500:]}")
            report[source_id] = {"status": "extractor_failed"}
            continue

        prior_path = M4 / f"{source_id}_last_run.json"
        prior = _load(prior_path) or None
        validation = harness.structural(records) + harness.consistency(records, prior)
        _save(prior_path, {"row_counts": {k: len(v) for k, v in records.items()}})

        reconcile_findings, counts = certify_mod.certify(records, assertions, ADJUDICATED)
        known = [f for f in reconcile_findings if (f["check"], f["ref"]) in ADJUDICATED]
        new_disputes = [f for f in reconcile_findings if f not in known]

        out = M4 / f"certified_{source_id}.json"
        _save(out, {k: [r for r in records[k] if r.get("certification", {})
                        .get("status") == "certified"]
                    for k in ("meetings", "agenda_items", "vote_events")})

        n_meet, n_item, n_vote = (len(records[k]) for k in
                                  ("meetings", "agenda_items", "vote_events"))
        print(f"  extracted: {n_meet} meetings, {n_item} items, {n_vote} vote events")
        print(f"  validation: {len(validation)} extractor findings"
              + (" — REPAIR-LOOP CANDIDATE" if validation else ""))
        for f in validation[:5]:
            print(f"    [{f['layer']}/{f['check']}] {f['ref']}: {f['msg'][:110]}")
        print(f"  reconciliation: {len(new_disputes)} new disputes, "
              f"{len(known)} previously adjudicated")
        for f in new_disputes[:5]:
            print(f"    [{f['check']}] {f['ref']}: {f['msg'][:130]}")
        pct = counts["certified"] / counts["total"] if counts["total"] else 0
        print(f"  certifiable coverage: {counts['certified']}/{counts['total']} "
              f"records ({pct:.0%}) -> {out.name}\n")

        fleet["certified"] += counts["certified"]
        fleet["total"] += counts["total"]
        report[source_id] = {
            "records": counts["total"], "certified": counts["certified"],
            "validation_findings": len(validation),
            "new_disputes": [(f["check"], f["ref"]) for f in new_disputes],
            "adjudicated": len(known),
        }

    pct = fleet["certified"] / fleet["total"] if fleet["total"] else 0
    print(f"M4 fleet: {fleet['certified']}/{fleet['total']} records certified "
          f"({pct:.0%}) across {len(SOURCES)} sources in "
          f"{(time.time() - t0) / 60:.1f} min (no LLM calls)")
    report["fleet"] = fleet
    _save(M4 / "report.json", report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
