"""Content mining: turn item-level staff-report PDFs into structured facts.

    python mine_items.py [--years 2026] [--model claude-haiku-4-5]

The votes layer says what happened; the item PDFs say what it was about.
Consent-agenda motions ("approve items 1a, 1b, 2a...") are opaque without
this — the breakdown of what those letters mean lives only in the per-item
staff reports. This pass walks every Business Meeting folder, mines every
"Item NN" PDF through a cheap model with structured outputs, and keys the
facts by meeting date + item number so the console can expand consent votes
into their constituent items.

Idempotent by (date, item number) — rerun after each backfill. Facts are
machine-derived enrichment, never certified.
"""

import argparse
import json
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))

import anthropic
from dotenv import load_dotenv

import loudoun_extractor
import sandbox2

load_dotenv(pathlib.Path(__file__).parent.parent / ".env")

FOUNDRY = pathlib.Path(__file__).parent
STORE = FOUNDRY / "data" / "store"
OUT = STORE / "loudoun-bos-item-facts.json"
CACHE = FOUNDRY / "data" / "discovery" / "loudoun_http_cache.json"
PRICES = {"claude-haiku-4-5": (1.00, 5.00), "claude-sonnet-4-6": (3.00, 15.00)}

ITEM_TITLE_RE = re.compile(r"^Item\s+([0-9]{1,2}[a-z]?|[IR]-\d+)\s", re.I)

SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string",
                    "description": "1-2 plain-English sentences a resident instantly "
                    "understands: what this item actually does and for whom"},
        "item_type": {"type": "string",
                      "description": "e.g. contract, rezoning, budget, appointment, policy, report"},
        "fiscal_impact": {"type": ["string", "null"]},
        "dollar_amounts": {"type": "array", "items": {"type": "string"},
                           "description": "specific dollar figures with purpose"},
        "districts": {"type": "array", "items": {"type": "string"}},
        "parties": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["summary", "item_type", "fiscal_impact", "dollar_amounts",
                 "districts", "parties"],
    "additionalProperties": False,
}


def normalize_item_no(raw):
    return raw.lstrip("0").lower() if raw[0].isdigit() else raw.upper()


def meeting_folders(rt, years):
    for year in years:
        for title, link in loudoun_extractor.rss_entries(
                rt, loudoun_extractor.YEAR_FOLDERS[year]):
            if "Business Meeting" not in title or "Joint" in title:
                continue
            d = re.search(r"(\d{2})-(\d{2})-(\d{2})", title)
            folder = re.findall(r"startid=(\d+)", link)
            if d and folder:
                yield f"20{d.group(3)}-{d.group(1)}-{d.group(2)}", folder[0]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--years", type=int, nargs="+", default=[2026])
    parser.add_argument("--model", default="claude-haiku-4-5")
    args = parser.parse_args()

    rt = sandbox2.Runtime(json.loads(CACHE.read_text()) if CACHE.exists() else {})
    existing = json.loads(OUT.read_text()) if OUT.exists() else {}
    client = anthropic.Anthropic()
    cost, mined = 0.0, 0

    for date, folder in meeting_folders(rt, args.years):
        for title, link in loudoun_extractor.rss_entries(rt, folder):
            m = ITEM_TITLE_RE.match(title)
            if not m or "Presentation" in title:
                continue
            key = f"{date}:{normalize_item_no(m.group(1))}"
            if key in existing:
                continue
            doc_id = re.findall(r"id=(\d+)", link)[0]
            try:
                text = rt.fetch_text(
                    f"{loudoun_extractor.PORTAL}/0/edoc/{doc_id}/item.pdf")[:14000]
                response = client.messages.create(
                    model=args.model, max_tokens=1500,
                    output_config={"format": {"type": "json_schema", "schema": SCHEMA}},
                    messages=[{"role": "user", "content":
                               "Extract the facts from this county board agenda "
                               f"item staff report titled {title!r}:\n\n{text}"}])
            except Exception as e:
                print(f"  SKIP {key}: {str(e)[:80]}")
                continue
            facts = json.loads(next(b.text for b in response.content
                                    if b.type == "text"))
            cost += (response.usage.input_tokens * PRICES[args.model][0]
                     + response.usage.output_tokens * PRICES[args.model][1]) / 1e6
            facts.update({"item_document": title, "source_doc_id": doc_id,
                          "meeting_date": date,
                          "item_no": normalize_item_no(m.group(1)),
                          "derived_by": args.model,
                          "certification": {"status": "machine-derived", "method": None,
                                            "note": "LLM content enrichment; never certified"}})
            existing[key] = facts
            mined += 1
            if mined % 10 == 0:
                OUT.write_text(json.dumps(existing, indent=1))
                CACHE.write_text(json.dumps(rt.cache))
                print(f"  {mined} mined (${cost:.2f}) — latest: {key}")
    OUT.write_text(json.dumps(existing, indent=1))
    CACHE.write_text(json.dumps(rt.cache))
    print(f"done: +{mined} items (${cost:.2f}, {args.model}) -> "
          f"{OUT.name} ({len(existing)} total)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
