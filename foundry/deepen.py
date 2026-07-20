"""Deterministic history deepening: run each source's proven extractor over a
larger meeting window and merge the gate-clean NEW records (quarantined).

    python deepen.py [slug ...] [--to 2026-01-01] [--max-n 60] [--repair]

$0 and no LLM on well-behaved sources — the artifact re-runs deterministically,
the harness + floors gate the output offline, and only meetings that don't
already exist in the store are merged (certified records are never touched;
`merge` overwrites by id, so pre-filtering to new meeting_ids is the safety).

Outcomes per source: "done" (target depth reached), "exhausted" (the source
has no more parseable history — a clerk who stopped posting minutes),
"stalled" (the artifact returned fewer meetings than asked while older ones
demonstrably exist — usually an enumeration bug like a server treating $top
as a total cap), "gate-failed", or "skipped" (curated source, no artifact).
Stalls and gate failures are where --repair escalates: one budget-gated
synthesis attempt through run_onboard's resume mode, which replays the newest
artifact and feeds the model the findings + fetch trace. Repair is refused
for sources holding certified records — onboarding's merge path is not
certification-aware.
"""

import argparse
import datetime
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))

import harness
import run_onboard
import sandbox2
from backfill import merge

FOUNDRY = pathlib.Path(__file__).parent
STORE = FOUNDRY / "data" / "store"
STEP = 10  # meetings added to the window per iteration

QUARANTINE_NOTE = ("synthesized from agent-discovered profile; single-source, "
                   "no oracle wired — ingested, never certifiable as-is")


def _slug(source_id):
    return source_id.rsplit("-", 1)[0]


def _new_only(records, store):
    """Restrict a run's output to meetings absent from the store, plus their
    items/votes. Existing records — including certified ones — stay exactly
    as they are."""
    new_mids = {m["meeting_id"] for m in records.get("meetings", [])
                if m["meeting_id"] not in store["meetings"]}
    return {
        "meetings": [m for m in records["meetings"]
                     if m["meeting_id"] in new_mids],
        "agenda_items": [i for i in records["agenda_items"]
                         if i["meeting_id"] in new_mids],
        "vote_events": [v for v in records["vote_events"]
                        if v["meeting_id"] in new_mids],
        "members": records.get("members", []),  # keyed by name; idempotent
    }, new_mids


def _certified_count(store):
    return sum(1 for k in ("meetings", "agenda_items", "vote_events")
               for r in store[k].values()
               if (r.get("certification") or {}).get("status") == "certified")


def deepen(source_id, target, max_n=60, repair=False, log=print):
    """Returns one of: done, exhausted, stalled, gate-failed, capped, skipped."""
    store_path = STORE / f"{source_id}.json"
    if not store_path.exists():
        log(f"{source_id}: no store — onboard it first")
        return "skipped"
    store = json.loads(store_path.read_text())
    meta = store.get("meta", {})
    if not meta.get("artifact"):
        log(f"{source_id}: curated source (no synthesized artifact) — its own "
            "backfill script owns history")
        return "skipped"
    artifact = FOUNDRY / meta["artifact"]
    if not artifact.exists():
        log(f"{source_id}: artifact {meta['artifact']} missing")
        return "skipped"

    dates = sorted(m["date"] for m in store["meetings"].values())
    oldest = dates[0] if dates else None
    if oldest and oldest <= target:
        log(f"{source_id}: oldest meeting {oldest} already at/before {target}")
        return "done"

    slug = _slug(source_id)
    cache_path = FOUNDRY / "data" / "onboard" / f"{slug}_http_cache.json"
    out_path = FOUNDRY / "data" / "onboard" / f"{slug}_deepen_out.json"
    n = len(dates)

    while True:
        n = min(n + STEP, max_n)
        log(f"{source_id}: running {artifact.name} with window {n} "
            f"(store oldest: {oldest})")
        records, error = sandbox2.run_artifact(artifact, [n], out_path, cache_path)
        if error is not None:
            log(f"  execution failed: {error.strip().splitlines()[-1][:140]}")
            return _escalate(source_id, slug, store, n, repair, log, "stalled")

        cache = json.loads(cache_path.read_text()) if cache_path.exists() else {}
        findings = harness.run_all(records)
        if not any(f["check"] == "malformed_root" for f in findings):
            findings += run_onboard.floors(records, cache)
        if findings:
            log(f"  gate failed ({len(findings)} findings) — store untouched")
            for f in findings[:4]:
                log(f"    [{f['layer']}/{f['check']}] {f['msg'][:100]}")
            return _escalate(source_id, slug, store, n, repair, log, "gate-failed")

        got = sorted(m["date"] for m in records["meetings"])
        fresh, new_mids = _new_only(records, store)
        for rtype in ("meetings", "agenda_items", "vote_events"):
            for rec in fresh[rtype]:
                rec["certification"] = {"status": "quarantined", "method": None,
                                        "note": QUARANTINE_NOTE}
        if new_mids:
            merge(source_id, fresh, 0)
            store = json.loads(store_path.read_text())
        oldest = got[0] if got else oldest
        log(f"  window returned {len(got)} meetings (oldest {oldest}), "
            f"{len(new_mids)} new merged")

        if oldest and oldest <= target:
            log(f"  reached {target}")
            return "done"
        if len(got) < n:
            # asked for n, extractor found fewer: either the source has no
            # deeper parseable history, or enumeration is silently capped
            log("  extractor returned fewer meetings than the window — "
                "source exhausted or enumeration stalled")
            return _escalate(source_id, slug, store, n, repair, log, "exhausted")
        if n >= max_n:
            log(f"  window cap {max_n} reached before {target}")
            return "capped"


def _escalate(source_id, slug, store, n, repair, log, verdict):
    if not repair:
        log(f"  verdict: {verdict} (re-run with --repair to spend one "
            "synthesis attempt on it)")
        return verdict
    if _certified_count(store):
        log(f"  verdict: {verdict} — repair REFUSED: source holds certified "
            "records and onboarding's merge is not certification-aware")
        return verdict
    log("  escalating to one budget-gated repair attempt (resume mode)")
    ok = run_onboard.onboard(slug, meetings=n, attempts=1, log=log, resume=True)
    return "repaired" if ok else verdict


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("sources", nargs="*",
                        help="source ids or slugs (default: every store "
                             "source with a synthesized artifact)")
    parser.add_argument("--to", default=f"{datetime.date.today().year}-01-01",
                        help="target oldest meeting date (YYYY-MM-DD)")
    parser.add_argument("--max-n", type=int, default=60)
    parser.add_argument("--repair", action="store_true",
                        help="escalate stalls/gate failures to one synthesis "
                             "attempt (LLM, budget-gated)")
    args = parser.parse_args()

    sources = [s if s.endswith("-bos") or "-" in s else f"{s}-bos"
               for s in args.sources]
    if not sources:
        sources = sorted(
            p.stem for p in STORE.glob("*.json")
            if json.loads(p.read_text()).get("meta", {}).get("artifact"))
    results = {}
    for source_id in sources:
        results[source_id] = deepen(source_id, args.to,
                                    max_n=args.max_n, repair=args.repair)
    print("\nsummary:")
    for sid, verdict in results.items():
        print(f"  {sid}: {verdict}")
    print("(new meetings have no digests/summaries yet — run "
          "summarize_items.py and meeting_digests.py to enrich, ~pennies)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
