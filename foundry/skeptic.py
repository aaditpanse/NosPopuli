"""Post-gate semantic review: a cheap model reads the ledger as a reader.

Mechanical floors catch structure (counts, names, duplicates); they cannot
demand MEANING, and synthesized extractors reliably game any regex proxy for
it ("Approve Res 0529-2026" passes a vocabulary check and tells a reader
nothing). So after every mechanical check passes, one small model call
reviews sample rows the way a skeptical citizen would — does each row say
WHAT was decided, by whom, and how the vote went? — and files findings into
the same repair loop the floors use.

Fails open: if the call errors, onboarding proceeds on the mechanical gate
alone (logged). Runs only on would-pass output, so it adds one call per
onboarding, not per attempt. Cost is recorded as "skeptic"; never blocked.
"""

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))

MODEL = "claude-sonnet-4-6"
PRICES = {"claude-sonnet-4-6": (3.00, 15.00)}

SCHEMA = {
    "type": "object",
    "properties": {"findings": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "check": {"type": "string", "description":
                "short_snake_case defect class, e.g. 'uninformative_titles'"},
            "ref": {"type": "string", "description":
                "which rows exhibit it (file numbers or 'run')"},
            "msg": {"type": "string", "description":
                "the defect and what the extractor must do instead, "
                "concretely — this text is fed back to the code generator"},
        },
        "required": ["check", "ref", "msg"],
        "additionalProperties": False}}},
    "required": ["findings"],
    "additionalProperties": False,
}

PROMPT = """You are a skeptical citizen reviewing a municipal vote ledger \
before publication. The ledger's promise: from each row a reader learns WHAT \
was decided, by WHOM, and HOW the vote went. Report only defects that break \
that promise — for example:
- titles that carry no subject ("Approve Res 0529-2026", "Resolution be \
Approved by Roll Call" — a real title reads like "A Local Law to amend the \
administrative code, in relation to requiring...")
- rows indistinguishable from one another
- numbers that contradict each other or reality (attendance larger than the \
body, counts disagreeing with quoted tallies)
- evidence quotes that visibly belong to a different item

Do NOT report: quarantine/certification status, terse-but-real subjects, \
procedural motions being procedural (approving an agenda is honest), or \
style preferences. An EMPTY findings list is the correct answer for a good \
ledger.

MEETINGS:
{meetings}

SAMPLE ROWS (title · file number · counts · result · evidence quote):
{rows}"""


def review(records, source_id, log=print, sample=14):
    import anthropic
    import budget

    items = {i.get("item_id"): i for i in records.get("agenda_items", [])}
    rows = []
    for v in records.get("vote_events", []):
        it = items.get(v.get("item_id")) or {}
        rows.append({
            "title": (it.get("title") or v.get("motion") or "")[:170],
            "file_number": it.get("file_number") or v.get("file_number"),
            "counts": v.get("counts"), "result": v.get("result"),
            "quote": ((v.get("evidence") or {}).get("quote") or "")[:220],
        })
        if len(rows) >= sample:
            break
    meetings = [{"date": m.get("date"), "body": m.get("body", ""),
                 "present": sum(1 for s in (m.get("attendance") or {}).values()
                                if s == "present")}
                for m in records.get("meetings", [])]

    client = anthropic.Anthropic()
    resp = client.messages.create(
        model=MODEL, max_tokens=1500,
        output_config={"format": {"type": "json_schema", "schema": SCHEMA}},
        messages=[{"role": "user", "content": PROMPT.format(
            meetings=json.dumps(meetings),
            rows=json.dumps(rows, indent=1, ensure_ascii=False))}])
    cost = (resp.usage.input_tokens * PRICES[MODEL][0]
            + resp.usage.output_tokens * PRICES[MODEL][1]) / 1e6
    budget.record("skeptic", cost)
    findings = json.loads(next(b.text for b in resp.content
                               if b.type == "text"))["findings"]
    log(f"  skeptic ({MODEL}): {len(findings)} findings (${cost:.2f})")
    return [{"layer": "skeptic", **f} for f in findings[:6]]
