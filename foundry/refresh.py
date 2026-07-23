"""Self-scheduling refresh: keep every source current, with no LLM in the loop.

    python refresh.py --due          # sources whose next meeting has now passed
    python refresh.py --all
    python refresh.py <source_id> [...]

Deterministic end to end: the promoted extractor artifact re-runs against the
live source, the harness + floors re-gate its output, and the promoted oracle
artifact re-certifies. Gate findings mean DRIFT — logged and skipped, never
merged; repair (re-synthesis) stays an offline, human-visible event. After a
refresh, item summaries and next-meeting lookups update (Haiku, cents).

Due detection uses upcoming.json: a meeting the schedule said was coming that
is now past AND newer than the store means new data may exist; publication
lag makes this retry daily (merges are idempotent) until the document lands.
Installed as a systemd user timer: foundry-refresh.timer, daily 07:30.
"""

import argparse
import datetime
import json
import os
import pathlib
import re
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))

FOUNDRY = pathlib.Path(__file__).parent
STORE = FOUNDRY / "data" / "store"
LOG_PATH = FOUNDRY / "data" / "refresh_log.jsonl"
STALE_DAYS = 21  # fallback when the schedule lookup knows nothing

SKIP = {"upcoming", "item-summaries"}


def load_env():
    env_file = FOUNDRY.parent / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        m = re.match(r"^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$", line.strip())
        if m:
            os.environ.setdefault(m.group(1), m.group(2).strip("'\""))


def source_ids():
    """Actual source stores — selected by shape (they carry a meetings map),
    not by name, so new enrichment sidecar files can't break the cycle."""
    out = []
    for p in sorted(STORE.glob("*.json")):
        if p.stem in SKIP or "item-facts" in p.stem:
            continue
        try:
            if isinstance(json.loads(p.read_text()).get("meetings"), dict):
                out.append(p.stem)
        except (ValueError, AttributeError):
            continue
    return out


def newest_meeting(store):
    return max((m["date"] for m in store["meetings"].values()), default=None)


def due_sources(today):
    upcoming = json.loads((STORE / "upcoming.json").read_text()) \
        if (STORE / "upcoming.json").exists() else {}
    due = []
    for sid in source_ids():
        store = json.loads((STORE / f"{sid}.json").read_text())
        newest = newest_meeting(store) or "0000"
        passed = [u["date"] for u in upcoming.get(sid, {}).get("upcoming", [])
                  if newest < u["date"] < today]
        stale = newest < (datetime.date.fromisoformat(today)
                          - datetime.timedelta(days=STALE_DAYS)).isoformat()
        if passed or stale:
            due.append((sid, f"meeting(s) {passed} have passed" if passed
                        else f"newest stored meeting {newest} is stale"))
    return due


def refresh_curated(source_id, log):
    import backfill
    if source_id == "pittsburgh-legistar":
        backfill.backfill_pittsburgh(4)
    elif source_id == "la-primegov":
        backfill.backfill_losangeles(3)
    elif source_id == "loudoun-bos":
        # evict the cached RSS listings so new documents are visible; the
        # bulky per-document entries stay cached
        cache_path = FOUNDRY / "data" / "discovery" / "loudoun_http_cache.json"
        if cache_path.exists():
            cache = json.loads(cache_path.read_text())
            fresh = {k: v for k, v in cache.items() if "rss" not in k.lower()}
            if len(fresh) != len(cache):
                cache_path.write_text(json.dumps(fresh))
        backfill.backfill_loudoun([datetime.date.today().year])
    else:
        return False
    return True


def refresh_generic(source_id, store, log):
    import harness
    import run_onboard
    import run_oracle
    import sandbox2
    from backfill import merge

    meta = store.get("meta", {})
    rel = meta.get("artifact")
    if not rel or not (FOUNDRY / rel).exists():
        log("  no promoted artifact recorded — cannot refresh")
        return False
    slug = source_id[: -len("-bos")]
    cache_path = FOUNDRY / "data" / "onboard" / f"{slug}_refresh_cache.json"
    cache_path.unlink(missing_ok=True)  # fresh listings every refresh
    out_path = FOUNDRY / "data" / "onboard" / f"{slug}_refresh_out.json"
    records, error = sandbox2.run_artifact(
        FOUNDRY / rel, [meta.get("meetings_arg", 3)], out_path, cache_path)
    if error is not None:
        log("  DRIFT: extractor failed — "
            + error.strip().splitlines()[-1][:140])
        log("  not merged; re-synthesize offline (run_onboard.py)")
        return False
    findings = harness.run_all(records)
    if not any(f["check"] == "malformed_root" for f in findings):
        findings += run_onboard.floors(
            records, json.loads(cache_path.read_text())
            if cache_path.exists() else {})
    if findings:
        log(f"  DRIFT: {len(findings)} gate findings — not merged")
        for f in findings[:4]:
            log(f"    [{f['layer']}/{f['check']}] {f['ref']}: {f['msg'][:110]}")
        return False
    note = ("synthesized from agent-discovered profile; single-source, no "
            "oracle wired — ingested, never certifiable as-is")
    for rtype in ("meetings", "agenda_items", "vote_events"):
        for rec in records[rtype]:
            rec["certification"] = {"status": "quarantined", "method": None,
                                    "note": note}
    merge(source_id, records, 0)
    if meta.get("oracle_artifact"):
        run_oracle.recertify(slug, log)
    return True


def enrich(log):
    for script in ("summarize_items.py", "meeting_digests.py", "upcoming.py"):
        proc = subprocess.run([sys.executable, str(FOUNDRY / script)],
                              capture_output=True, text=True, timeout=900)
        tail = (proc.stdout or proc.stderr).strip().splitlines()
        log(f"  {script}: {tail[-1][:110] if tail else f'exit {proc.returncode}'}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("sources", nargs="*")
    parser.add_argument("--due", action="store_true")
    parser.add_argument("--all", action="store_true")
    args = parser.parse_args()
    load_env()
    # Work dirs (http caches, run outputs) are gitignored, so a fresh checkout
    # (CI) doesn't have them; the extractors write into them unconditionally.
    for sub in ("m4", "onboard", "oracle"):
        (FOUNDRY / "data" / sub).mkdir(parents=True, exist_ok=True)
    today = datetime.date.today().isoformat()

    if args.all:
        todo = [(sid, "forced (--all)") for sid in source_ids()]
    elif args.sources:
        todo = [(sid, "forced") for sid in args.sources]
    else:
        todo = due_sources(today)
        if not todo:
            print(f"{today}: nothing due — every store is current against "
                  "the known schedule")
            return 0

    results = {}
    for sid, why in todo:
        print(f"refreshing {sid} ({why})")
        try:
            store = json.loads((STORE / f"{sid}.json").read_text())
            ok = (refresh_curated(sid, print) if store.get("meta") is None
                  or sid in ("pittsburgh-legistar", "la-primegov", "loudoun-bos")
                  else refresh_generic(sid, store, print))
            results[sid] = "ok" if ok else "drift-or-skipped"
        except (Exception, SystemExit) as exc:
            # backfill raises SystemExit on extractor failure — contain it so
            # one bad source never kills the rest of the cycle.
            print(f"  ERROR: {exc}")
            results[sid] = f"error: {str(exc)[:120]}"
    # History deepening rides the same cycle: deterministic, $0, and merges
    # only meetings the store doesn't have (never touches certified records).
    # A stalled/gate-failed source escalates to AT MOST ONE budget-gated
    # synthesis repair per cycle (skipped for stores holding certified
    # records; set FOUNDRY_AUTO_REPAIR=off to make stalls log-only). Sources
    # already at the target date return immediately, so steady state is free.
    import deepen as deepen_mod
    repairs_left = 0 if os.environ.get("FOUNDRY_AUTO_REPAIR") == "off" else 1
    for sid in sorted(
            p.stem for p in STORE.glob("*.json")
            if json.loads(p.read_text()).get("meta", {}).get("artifact")):
        try:
            verdict = deepen_mod.deepen(
                sid, f"{datetime.date.today().year}-01-01",
                repair=repairs_left > 0, log=print)
            if verdict == "repaired":
                repairs_left -= 1
            results[f"deepen:{sid}"] = verdict
        except Exception as exc:
            print(f"  deepen {sid} ERROR: {exc}")
            results[f"deepen:{sid}"] = f"error: {str(exc)[:120]}"

    # Enrichment runs LAST so refreshed AND deepened meetings get their
    # plain-English layer in the same cycle. Idempotent — costs ~$0 when
    # nothing is new.
    if todo or any(k.startswith("deepen:") for k in results):
        enrich(print)

    with LOG_PATH.open("a") as fh:
        fh.write(json.dumps({"ts": datetime.datetime.now().isoformat(
            timespec="seconds"), "results": results}) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
