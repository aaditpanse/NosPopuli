"""M2 experiment: break the source deliberately, measure detection and repair.

    python run_m2.py [--attempts 3]

Each scenario mutates a copy of the frozen HTTP cache to simulate a real
Legistar format change (field renames, vocabulary changes, date formats) or
a benign change that should NOT trigger anything (extra fields, key order).
The staged extractor runs against the mutated cache with live fetches
disabled — a simulated break must not silently heal from the real source.

Detection = crash, harness finding, or golden regression. For breaking
scenarios, detection feeds the repair loop: (old extractor + fresh samples
from the changed source + failing evidence) -> LLM -> candidate, gated
exactly like M1 (full harness + golden reproduction, now against the
mutated source). Benign scenarios measure the false-alarm rate — an oracle
that cries wolf kills the automation as surely as one that misses.
"""

import argparse
import copy
import json
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).parent))

import harness
import sandbox
import synthesize
from run_m1 import golden_diff

FOUNDRY = pathlib.Path(__file__).parent
GOLDEN = FOUNDRY / "golden" / "pittsburgh"
ARTIFACTS = FOUNDRY / "extractors" / "pittsburgh-legistar"
DATA = FOUNDRY / "data"


def _rows(cache, url_fragment):
    for key, payload in cache.items():
        if url_fragment in key and isinstance(payload, list):
            for row in payload:
                yield row


def vote_person_rename(cache):
    """Votes endpoint renames VotePersonName -> VotePersonFullName."""
    for row in _rows(cache, "/votes"):
        row["VotePersonFullName"] = row.pop("VotePersonName")


def vote_value_rename(cache):
    """Vote/rollcall value vocabulary changes (Aye->Yes, Absent->Not Present)."""
    names = {"Aye": "Yes", "Absent": "Not Present", "Present": "In Attendance"}
    for row in _rows(cache, "/votes"):
        row["VoteValueName"] = names.get(row["VoteValueName"], row["VoteValueName"])
    for row in _rows(cache, "/rollcalls"):
        row["RollCallValueName"] = names.get(row["RollCallValueName"], row["RollCallValueName"])


def date_format_change(cache):
    """EventDate switches from ISO to US format."""
    for key, payload in cache.items():
        if "/events/" in key and isinstance(payload, dict) and payload.get("EventDate"):
            y, m, d = payload["EventDate"][:10].split("-")
            payload["EventDate"] = f"{m}/{d}/{y} 12:00:00 AM"


def matter_file_rename(cache):
    """Event items rename EventItemMatterFile -> EventItemFile."""
    for row in _rows(cache, "/eventitems"):
        if "EventItemMatterFile" in row:
            row["EventItemFile"] = row.pop("EventItemMatterFile")


def passed_flag_rename(cache):
    """Passed-flag vocabulary changes: Pass -> Passed, Fail -> Failed."""
    names = {"Pass": "Passed", "Fail": "Failed"}
    for row in _rows(cache, "/eventitems"):
        flag = row.get("EventItemPassedFlagName")
        if flag in names:
            row["EventItemPassedFlagName"] = names[flag]


def extra_fields(cache):
    """Benign: source adds new fields nothing consumes."""
    for row in _rows(cache, "/eventitems"):
        row["EventItemAuditGuid"] = "00000000-0000-0000-0000-000000000000"
    for row in _rows(cache, "/votes"):
        row["VoteAuditGuid"] = "00000000-0000-0000-0000-000000000000"


def key_reorder(cache):
    """Benign: JSON object key order changes."""
    def reorder(obj):
        if isinstance(obj, dict):
            return {k: reorder(obj[k]) for k in reversed(list(obj))}
        if isinstance(obj, list):
            return [reorder(v) for v in obj]
        return obj
    for key in list(cache):
        cache[key] = reorder(cache[key])


# name -> (mutation fn, is_breaking)
SCENARIOS = {
    "vote_person_rename": (vote_person_rename, True),
    "vote_value_rename": (vote_value_rename, True),
    "date_format_change": (date_format_change, True),
    "matter_file_rename": (matter_file_rename, True),
    "passed_flag_rename": (passed_flag_rename, True),
    "extra_fields": (extra_fields, False),
    "key_reorder": (key_reorder, False),
}


def staged_artifact():
    return sorted(ARTIFACTS.glob("v2_attempt*.py"))[-1]


def gate(records, error, assertions, golden, golden_meta):
    """Returns (findings, diff, signals). Empty signals = clean."""
    if error is not None:
        return [], [], ["crash"]
    findings = harness.run_all(records, assertions, prior_run_meta=golden_meta)
    diff = golden_diff(records, golden)
    signals = sorted({f["layer"] for f in findings}) + (["golden"] if diff else [])
    return findings, diff, signals


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--attempts", type=int, default=3)
    args = parser.parse_args()

    golden = json.loads((GOLDEN / "records.json").read_text())
    assertions = json.loads((GOLDEN / "assertions.json").read_text())
    golden_meta = json.loads((GOLDEN / "run_meta.json").read_text())
    event_ids = golden_meta["event_ids"]
    base_cache = sandbox.load_cache()
    artifact = staged_artifact()
    old_code = artifact.read_text()
    DATA.mkdir(exist_ok=True)

    print(f"staged extractor under test: {artifact.name}\n")
    results, usages = [], []
    t0 = time.time()

    for name, (mutate, breaking) in SCENARIOS.items():
        print(f"scenario {name} ({'breaking' if breaking else 'benign'})")
        broken = copy.deepcopy(base_cache)
        mutate(broken)
        cache_path = DATA / f"broken_cache_{name}.json"
        cache_path.write_text(json.dumps(broken))
        out_path = DATA / "m2_out.json"

        records, error = sandbox.run_artifact(
            artifact, event_ids, out_path, cache_path=cache_path, allow_live=False)
        findings, diff, signals = gate(records, error, assertions, golden, golden_meta)
        detected = bool(signals)
        # No signal AND correct output means the extractor absorbed the change
        # — robustness, not a detection miss. (The gate's "golden" signal
        # guarantees a silent-wrong-output miss can't be classified here.)
        robust = breaking and not detected
        print(f"  detection: {'DETECTED via ' + ', '.join(signals) if detected else 'clean'}"
              + (" — extractor absorbed the change, output still correct" if robust else ""))

        result = {"scenario": name, "breaking": breaking, "detected": detected,
                  "robust": robust, "signals": signals, "repaired": None, "attempts": 0}
        if breaking and detected:
            messages = synthesize.build_repair_messages(
                old_code, broken, event_ids,
                error=error, findings=findings, diff_lines=diff)
            for attempt in range(1, args.attempts + 1):
                result["attempts"] = attempt
                print(f"  repair attempt {attempt}: calling {synthesize.MODEL}...")
                code, assistant_content, usage = synthesize.generate(messages)
                usages.append(usage)
                candidate = ARTIFACTS / f"repair_{name}_attempt{attempt}.py"
                candidate.write_text(code)
                records, error = sandbox.run_artifact(
                    candidate, event_ids, out_path,
                    cache_path=cache_path, allow_live=False)
                findings, diff, signals = gate(records, error, assertions,
                                               golden, golden_meta)
                if not signals:
                    result["repaired"] = True
                    print(f"  REPAIRED: {candidate.name} passes harness + golden set")
                    break
                print(f"  gate still failing via {', '.join(signals)}")
                messages.append({"role": "assistant", "content": assistant_content})
                messages.append(synthesize.feedback_message(error, findings, diff))
            else:
                result["repaired"] = False
                print("  repair FAILED within attempt budget")
        results.append(result)
        print()

    breaking_results = [r for r in results if r["breaking"]]
    benign_results = [r for r in results if not r["breaking"]]
    needing_repair = [r for r in breaking_results if not r["robust"]]
    robust_n = sum(1 for r in breaking_results if r["robust"])
    detected_n = sum(1 for r in needing_repair if r["detected"])
    repaired_n = sum(1 for r in needing_repair if r["repaired"])
    false_alarms = sum(1 for r in benign_results if r["detected"])

    print(f"M2 metrics ({synthesize.MODEL})")
    print(f"  {'scenario':<22} {'type':<9} {'outcome':<34} attempts")
    for r in results:
        if r["robust"]:
            outcome = "absorbed (output still correct)"
        elif not r["breaking"]:
            outcome = "false alarm" if r["detected"] else "correctly ignored"
        else:
            outcome = (f"detected ({','.join(r['signals'])}) -> "
                       f"{ {True: 'repaired', False: 'NOT repaired', None: 'undetected'}[r['repaired']]}")
        print(f"  {r['scenario']:<22} {'breaking' if r['breaking'] else 'benign':<9} "
              f"{outcome:<34} {r['attempts'] or '—'}")
    print(f"\n  detection rate:  {detected_n}/{len(needing_repair)} damaging changes caught"
          f" ({robust_n} more absorbed outright)")
    print(f"  recovery rate:   {repaired_n}/{len(needing_repair)} repaired zero-touch")
    print(f"  false alarms:    {false_alarms}/{len(benign_results)} benign changes flagged")
    print(f"  LLM cost:        ${synthesize.cost_usd(usages):.2f} "
          f"across {len(usages)} calls")
    print(f"  wall time:       {(time.time() - t0) / 60:.1f} min")
    print("\n  repaired artifacts are STAGED (human review gate before promotion).")

    ok = (detected_n == len(needing_repair)
          and repaired_n == len(needing_repair) and false_alarms == 0)
    print(f"\nM2 GATE: {'PASSED' if ok else 'FAILED'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
