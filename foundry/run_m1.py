"""M1 experiment: synthesize the extractor, gate it against the golden set.

    python run_m1.py [--attempts 4]

Loop: LLM writes an extractor from the source profile + schema + contract
(never from v1's code) -> candidate runs in the sandbox over the same three
meetings -> gate = full M0 harness (structural / consistency / cross-source)
+ field-for-field golden-set reproduction. Failures go back to the model as
feedback; each round trip is one measured attempt.

Onboarding-cost outputs: attempts, tokens, dollars, wall time, human-minutes
(zero if the loop closes with no human edit). A passing artifact is STAGED,
not promoted — votes/dates/identities are load-bearing fields, so promotion
over v1 keeps the human review gate (spec module 6).
"""

import argparse
import json
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).parent))

import harness
import sandbox
import synthesize

FOUNDRY = pathlib.Path(__file__).parent
GOLDEN = FOUNDRY / "golden" / "pittsburgh"
ARTIFACTS = FOUNDRY / "extractors" / "pittsburgh-legistar"

# What "reproduce the golden set" means, per record type: (id field, fields
# compared). Optional presentation fields (time, location, minutes_url) are
# not load-bearing and not gated.
PROJECTION = {
    "meetings": ("meeting_id", ["body", "date", "attendance", "source_url"]),
    "agenda_items": ("item_id", ["meeting_id", "file_number", "title", "action", "result"]),
    "vote_events": ("vote_id", ["meeting_id", "item_id", "file_number", "counts", "result"]),
    "members": ("name", ["person_id"]),
}


def project(records):
    out = {}
    for rtype, (id_field, fields) in PROJECTION.items():
        out[rtype] = {}
        for rec in records.get(rtype, []):
            proj = {f: rec.get(f) for f in fields}
            if rtype == "vote_events":
                rows = rec.get("positions")
                proj["positions"] = sorted(
                    (str(p.get("member")), str(p.get("position"))) if isinstance(p, dict)
                    else ("<malformed row>", json.dumps(p)[:80])
                    for p in (rows if isinstance(rows, list) else []))
            out[rtype][rec.get(id_field)] = proj
    return out


def golden_diff(candidate, golden):
    lines = []
    cand, gold = project(candidate), project(golden)
    for rtype in PROJECTION:
        missing = sorted(set(gold[rtype]) - set(cand[rtype]))
        extra = sorted(set(cand[rtype]) - set(gold[rtype]))
        for rid in missing[:5]:
            lines.append(f"{rtype}: missing record {rid}")
        if len(missing) > 5:
            lines.append(f"{rtype}: ...and {len(missing) - 5} more missing")
        for rid in extra[:5]:
            lines.append(f"{rtype}: unexpected extra record {rid}")
        if len(extra) > 5:
            lines.append(f"{rtype}: ...and {len(extra) - 5} more extra")
        changed = 0
        for rid in sorted(set(gold[rtype]) & set(cand[rtype])):
            if gold[rtype][rid] != cand[rtype][rid]:
                changed += 1
                if changed <= 5:
                    for field in gold[rtype][rid]:
                        g, c = gold[rtype][rid][field], cand[rtype][rid][field]
                        if g != c:
                            lines.append(f"{rtype} {rid} .{field}: expected "
                                         f"{json.dumps(g)[:150]} got {json.dumps(c)[:150]}")
        if changed > 5:
            lines.append(f"{rtype}: ...and {changed - 5} more records differ")
    return lines


def run_candidate(candidate_path, event_ids):
    """Execute the artifact in the sandbox subprocess. Returns (records, error)."""
    out_path = FOUNDRY / "data" / "candidate_out.json"
    out_path.parent.mkdir(exist_ok=True)
    return sandbox.run_artifact(candidate_path, event_ids, out_path)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--attempts", type=int, default=4)
    args = parser.parse_args()

    golden = json.loads((GOLDEN / "records.json").read_text())
    assertions = json.loads((GOLDEN / "assertions.json").read_text())
    golden_meta = json.loads((GOLDEN / "run_meta.json").read_text())
    event_ids = golden_meta["event_ids"]
    cache = sandbox.warm_cache(event_ids)
    ARTIFACTS.mkdir(parents=True, exist_ok=True)

    messages = synthesize.build_initial_messages(cache, event_ids)
    usages, passed = [], False
    t0 = time.time()

    for attempt in range(1, args.attempts + 1):
        print(f"attempt {attempt}: synthesizing with {synthesize.MODEL}...")
        code, assistant_content, usage = synthesize.generate(messages)
        usages.append(usage)
        artifact = ARTIFACTS / f"v2_attempt{attempt}.py"
        artifact.write_text(code)
        print(f"  artifact: {artifact.relative_to(FOUNDRY)} "
              f"({len(code.splitlines())} lines, {usage.output_tokens} output tokens)")

        records, error = run_candidate(artifact, event_ids)
        findings, diff = [], []
        if error is None:
            findings = harness.run_all(records, assertions, prior_run_meta=golden_meta)
            diff = golden_diff(records, golden)
        else:
            print("  execution failed")

        if error is None and not findings and not diff:
            passed = True
            print("  GATE PASSED: harness clean + golden set reproduced exactly")
            break

        print(f"  gate failed: {len(findings)} harness findings, "
              f"{len(diff)} golden diffs" + (" (crashed)" if error else ""))
        for line in (diff or [])[:5]:
            print(f"    {line}")
        messages.append({"role": "assistant", "content": assistant_content})
        messages.append(synthesize.feedback_message(error, findings, diff))

    wall = time.time() - t0
    total_in = sum((u.input_tokens or 0) + (getattr(u, 'cache_creation_input_tokens', 0) or 0)
                   + (getattr(u, 'cache_read_input_tokens', 0) or 0) for u in usages)
    total_out = sum(u.output_tokens or 0 for u in usages)
    print(f"\nM1 metrics (onboarding cost, {synthesize.MODEL})")
    print(f"  attempts:      {len(usages)}")
    print(f"  tokens:        {total_in:,} in / {total_out:,} out")
    print(f"  LLM cost:      ${synthesize.cost_usd(usages):.2f}")
    print(f"  wall time:     {wall / 60:.1f} min")
    print(f"  human-minutes: 0 (no human edit inside the loop)")

    if passed:
        print("\nverdict: candidate STAGED — reproduces the golden set and passes "
              "the full harness. Promotion over extractor v1 requires the human "
              "review gate (votes/dates/identities are load-bearing fields).")
        return 0
    print("\nverdict: FAILED — no candidate cleared the gate within the attempt "
          "budget. Onboarding this source is not yet zero-touch.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
