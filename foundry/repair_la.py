"""First real (non-simulated) repair: LA journal format drift found by M4.

The 2026-06-26 journal contains a public hearing "no action" item whose
vote line is `Ayes: (0); Nays: (0); Absent: (0)`. The promoted extractor
emitted an empty vote_event (structural findings) and a phantom
CFMS-coverage dispute. Repair input per the spec: old artifact + fresh
sample of the changed source + failing evidence. Gate: the M4 refresh for
LA must come back with zero validation findings and no disputes beyond the
known set.
"""

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))

import harness
import run_m4
import synthesize
from run_m3 import ADJUDICATED

FOUNDRY = pathlib.Path(__file__).parent
ARTIFACTS = FOUNDRY / "extractors" / "la-primegov"

# Disputes that predate this repair and are under separate triage; the
# repair must not be blamed for them (nor silently resolve them).
KNOWN_DISPUTE_REFS = {"la-primegov-18364/17-0714-S1",
                      "la-primegov-17723/25-0916",
                      "la-primegov-17723/26-0824"}


def journal_excerpt():
    cache = json.loads((run_m4.M4 / "la_http_cache.json").read_text())
    text = next(v for k, v in cache.items()
                if isinstance(v, str) and "CompiledDocument" in k
                and "June 26, 2026" in v[:3000])
    lines = text.splitlines()
    i = next(j for j, l in enumerate(lines) if "26-0642" in l)
    return "\n".join(lines[i:i + 55])


def gate(artifact):
    records, assertions, error = run_m4.refresh_losangeles(3, artifact=artifact)
    if error:
        return None, error, []
    findings = harness.structural(records) + harness.consistency(records)
    disputes = [f for f in harness.reconcile(records, assertions)
                if (f["check"], f["ref"]) not in ADJUDICATED
                and f["ref"] not in KNOWN_DISPUTE_REFS]
    return records, None, findings + disputes


def main():
    old_code = run_m4.LA_ARTIFACT.read_text()
    _, _, evidence = gate(run_m4.LA_ARTIFACT)
    print(f"failing evidence on current artifact: {len(evidence)} findings")

    prompt = f"""The `la-primegov` extractor below mishandles a journal \
pattern discovered on fresh data. Repair it.

## Current extractor
```python
{old_code}
```

## The journal pattern it mishandles

Some items are hearings or announcements where no vote is taken; the
journal still prints a vote line with every group at zero:

```
{journal_excerpt()}
```

## Failing evidence from the validation harness

{chr(10).join(f"- [{f['layer']}/{f['check']}] {f['ref']}: {f['msg'][:160]}" for f in evidence[:8])}

## Requirements

An all-zero block (`Ayes: (0); Nays: (0); Absent: (0)`) means NO vote was
taken: emit no vote_event for it, and the agenda item's result must be
None (unless a later block for the same item records a real vote). All
other behavior must stay exactly as it is. Same artifact contract; bump
EXTRACTOR_VERSION. Return the complete module in one ```python block."""

    messages = [{"role": "user", "content": prompt}]
    usages = []
    for attempt in range(1, 4):
        print(f"repair attempt {attempt}: calling {synthesize.MODEL}...")
        code, assistant_content, usage = synthesize.generate(messages)
        usages.append(usage)
        candidate = ARTIFACTS / f"v2_repair_attempt{attempt}.py"
        candidate.write_text(code)
        records, error, findings = gate(candidate)
        if error is None and not findings:
            print(f"REPAIRED: {candidate.name} — refresh clean "
                  f"(cost ${synthesize.cost_usd(usages):.2f})")
            return 0
        print(f"  gate: {len(findings)} findings" + (" (crashed)" if error else ""))
        for f in (findings or [])[:4]:
            print(f"    [{f['layer']}/{f['check']}] {f['ref']}: {f['msg'][:110]}")
        messages.append({"role": "assistant", "content": assistant_content})
        messages.append(synthesize.feedback_message(error, findings))
    print("repair FAILED within budget")
    return 1


if __name__ == "__main__":
    sys.exit(main())
