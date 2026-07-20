"""Plain-English layer: one-sentence summaries of what voted items actually are.

    python summarize_items.py [--model claude-haiku-4-5] [--batch 15]

Official titles are legalese ("Resolution amending Resolution 270 of 2026
authorizing the issuance of a warrant..."). This offline pass summarizes
every agenda item that has a recorded vote into one plain sentence + a topic
tag, batched through a cheap model with structured outputs. Idempotent —
already-summarized items are skipped, so it can run after every backfill.

Output is machine-derived enrichment (data/store/item-summaries.json),
labeled as such in the console and never certified.
"""

import argparse
import json
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))

import anthropic
from dotenv import load_dotenv

load_dotenv(pathlib.Path(__file__).parent.parent / ".env")

FOUNDRY = pathlib.Path(__file__).parent
STORE = FOUNDRY / "data" / "store"
OUT = STORE / "item-summaries.json"
PRICES = {"claude-haiku-4-5": (1.00, 5.00), "claude-sonnet-4-6": (3.00, 15.00)}

SCHEMA = {
    "type": "object",
    "properties": {"summaries": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "id": {"type": "string"},
            "plain_english": {"type": "string", "description":
                "One sentence a non-lawyer instantly understands: what this "
                "item actually does. No 'Resolution authorizing' framing — "
                "say the substance ('Pays $108,296 to settle a lawsuit...'). "
                "Use the [source context] block to identify the real subject "
                "(project, place, money, rule), but BEWARE: the context may "
                "include NEIGHBORING agenda items — describe only the item "
                "whose identifier matches. NEVER state vote counts, tallies, "
                "or outcomes (the ledger displays those; a wrong number here "
                "contradicts it). NEVER describe the act of voting or "
                "record-keeping. If the text genuinely does not say what the "
                "item is about, say exactly that: 'No subject stated in the "
                "minutes; see the official record.' — an honest gap beats a "
                "guess."},
            "topic": {"type": "string", "description":
                "2-3 word topic tag, e.g. 'legal settlement', 'zoning', "
                "'public safety', 'budget', 'appointments'"},
        },
        "required": ["id", "plain_english", "topic"],
        "additionalProperties": False}}},
    "required": ["summaries"],
    "additionalProperties": False,
}


def _source_docs(slug):
    """Cached document bodies for a source, for evidence-context lookup."""
    cache_path = FOUNDRY / "data" / "onboard" / f"{slug}_http_cache.json"
    if not cache_path.exists():
        return []
    cache = json.loads(cache_path.read_text())
    return [b for b in cache.values() if isinstance(b, str) and len(b) > 3000]


def _quote_context(quote, docs, before=900, after=200):
    """The source text surrounding an evidence quote. Extractor-made titles
    are often just the motion sentence; the subject matter (agenda heading,
    staff summary) sits immediately BEFORE the quote in the minutes."""
    if not quote or not docs:
        return None
    pat = re.compile(r"\s+".join(re.escape(w) for w in quote.split()[:12]))
    for doc in docs:
        m = pat.search(doc)
        if m:
            seg = doc[max(0, m.start() - before):m.end() + after]
            return " ".join(seg.split())
    return None


def voted_items():
    """id -> official text for everything a vote references: the agenda
    item's title where one exists (Pittsburgh, LA), else the motion text
    itself keyed by vote_id (Loudoun records votes as motions). Where the
    vote carries an evidence quote, source context around the quote is
    appended so the summary can name the actual subject."""
    out = {}
    for path in STORE.glob("*.json"):
        if path.name in (OUT.name,) or "item-facts" in path.name:
            continue
        store = json.loads(path.read_text())
        items = store.get("agenda_items", {})
        docs = _source_docs(path.stem.rsplit("-", 1)[0])
        for vote in store.get("vote_events", {}).values():
            item = items.get(vote.get("item_id"))
            if item and item.get("title"):
                key, text = item["item_id"], item["title"][:600]
            elif vote.get("motion"):
                key, text = vote["vote_id"], vote["motion"][:600]
            else:
                continue
            ctx = _quote_context((vote.get("evidence") or {}).get("quote"), docs)
            if ctx:
                text += ("\n[source context around this vote — the agenda "
                         "heading/subject usually appears here]\n" + ctx[:900])
            out[key] = text
    return out


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="claude-haiku-4-5")
    parser.add_argument("--batch", type=int, default=15)
    args = parser.parse_args()

    existing = json.loads(OUT.read_text()) if OUT.exists() else {}
    todo = {k: v for k, v in voted_items().items() if k not in existing}
    print(f"{len(todo)} voted items need summaries "
          f"({len(existing)} already done)")
    if not todo:
        return 0

    client = anthropic.Anthropic()
    pending = list(todo.items())
    cost = 0.0
    for i in range(0, len(pending), args.batch):
        chunk = pending[i:i + args.batch]
        response = client.messages.create(
            model=args.model, max_tokens=3000,
            output_config={"format": {"type": "json_schema", "schema": SCHEMA}},
            messages=[{"role": "user", "content":
                       "Summarize each municipal agenda item:\n\n" + json.dumps(
                           [{"id": k, "official_title": v} for k, v in chunk])}])
        cost += (response.usage.input_tokens * PRICES[args.model][0]
                 + response.usage.output_tokens * PRICES[args.model][1]) / 1e6
        data = json.loads(next(b.text for b in response.content
                               if b.type == "text"))
        for s in data["summaries"]:
            if s["id"] in todo:
                existing[s["id"]] = {"plain_english": s["plain_english"],
                                     "topic": s["topic"],
                                     "derived_by": args.model}
        OUT.write_text(json.dumps(existing, indent=1))
        print(f"  {min(i + args.batch, len(pending))}/{len(pending)} "
              f"(${cost:.2f})")
    if cost:
        import budget
        budget.record("enrichment", cost)
    print(f"done: {len(existing)} summaries -> {OUT.name} (${cost:.2f}, {args.model})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
