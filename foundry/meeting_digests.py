"""Comprehension layer: a plain-English digest of each meeting.

    python meeting_digests.py [--model claude-haiku-4-5]

A meeting in the ledger is a list of votes; a resident wants the story —
what did the board actually do that night, what mattered, who disagreed.
This offline pass writes a 2–4 sentence digest per meeting from the items,
outcomes, tallies, and dissents already in the store (plus the item
summaries where they exist). Idempotent by meeting_id, so it can run after
every refresh.

Output is machine-derived enrichment (data/store/meeting-digests.json),
labeled as such in the console and never certified.
"""

import argparse
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))

import anthropic
from dotenv import load_dotenv

load_dotenv(pathlib.Path(__file__).parent.parent / ".env")

FOUNDRY = pathlib.Path(__file__).parent
STORE = FOUNDRY / "data" / "store"
OUT = STORE / "meeting-digests.json"
PRICES = {"claude-haiku-4-5": (1.00, 5.00), "claude-sonnet-4-6": (3.00, 15.00)}

SCHEMA = {
    "type": "object",
    "properties": {
        "digest": {"type": "string", "description":
                   "2-4 sentences a resident instantly understands: what the "
                   "board actually did at this meeting. Lead with what affects "
                   "people (money, land use, services, taxes); name dissent "
                   "and failed motions explicitly; skip ceremony unless it was "
                   "the whole meeting. Never invent facts not in the record."},
        "notable": {"type": "array", "items": {"type": "string"},
                    "description": "Up to 3 short 'worth knowing' lines: the "
                    "single biggest decision, any split vote with who opposed, "
                    "any failed motion. Empty when genuinely nothing stands out."},
    },
    "required": ["digest", "notable"],
    "additionalProperties": False,
}


def meeting_brief(store, meeting, summaries):
    """Assemble the record the model digests: every vote with its label,
    outcome, tally, and named exceptions."""
    titles = {i["item_id"]: i.get("title", "") for i in store["agenda_items"].values()}
    lines = []
    for ve in store["vote_events"].values():
        if ve["meeting_id"] != meeting["meeting_id"]:
            continue
        s = summaries.get(ve.get("item_id") or "") or summaries.get(ve["vote_id"])
        label = (s or {}).get("plain_english") or titles.get(ve.get("item_id"), "") \
            or (ve.get("motion") or "")[:140] or ve["vote_id"]
        exc = [f"{p['member']}:{p['position']}" for p in ve.get("positions", [])
               if p.get("position") not in ("aye",)]
        tally = " ".join(f"{v} {k}" for k, v in sorted(ve.get("counts", {}).items()))
        lines.append(f"- [{ve.get('result', '?')}] {label[:160]} ({tally}"
                     f"{'; ' + ', '.join(exc[:6]) if exc else ''})")
    absent = [n for n, st in (meeting.get("attendance") or {}).items()
              if st == "absent"]
    head = (f"{meeting['date']} — {meeting.get('body', 'governing body')}"
            f"{' — ABSENT: ' + ', '.join(absent) if absent else ''}")
    return head + "\n" + "\n".join(lines[:70])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="claude-haiku-4-5")
    args = parser.parse_args()
    summaries = json.loads((STORE / "item-summaries.json").read_text()) \
        if (STORE / "item-summaries.json").exists() else {}
    existing = json.loads(OUT.read_text()) if OUT.exists() else {}
    client = anthropic.Anthropic()
    cost, made = 0.0, 0

    for path in sorted(STORE.glob("*.json")):
        if path.stem in ("upcoming", "item-summaries", "meeting-digests") \
                or "item-facts" in path.stem:
            continue
        store = json.loads(path.read_text())
        if not isinstance(store.get("meetings"), dict):
            continue  # non-meetings stores (elections, CIP) have no meetings map
        for meeting in store["meetings"].values():
            mid = meeting["meeting_id"]
            if mid in existing:
                continue
            brief = meeting_brief(store, meeting, summaries)
            if brief.count("\n") < 1:
                continue  # no recorded votes — nothing to digest
            response = client.messages.create(
                model=args.model, max_tokens=800,
                output_config={"format": {"type": "json_schema", "schema": SCHEMA}},
                messages=[{"role": "user", "content":
                           "Digest this local-government meeting record for a "
                           "resident of the jurisdiction:\n\n" + brief}])
            out = json.loads(next(b.text for b in response.content
                                  if b.type == "text"))
            out["notable"] = out.get("notable", [])[:3]
            cost += (response.usage.input_tokens * PRICES[args.model][0]
                     + response.usage.output_tokens * PRICES[args.model][1]) / 1e6
            out.update({"derived_by": args.model,
                        "certification": {"status": "machine-derived", "method": None,
                                          "note": "LLM comprehension aid; never certified"}})
            existing[mid] = out
            made += 1
            if made % 10 == 0:
                print(f"  {made} digests (${cost:.2f})")
    OUT.write_text(json.dumps(existing, indent=1))
    if cost:
        import budget
        budget.record("enrichment", cost)
    print(f"done: {made} new, {len(existing)} total -> {OUT.name} (${cost:.2f})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
