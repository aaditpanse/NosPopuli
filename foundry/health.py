"""Health ledger: what each pipeline stage did, last time it ran.

    from health import record, record_cycle, summarize

The pipeline already computes everything an operator needs to answer "is this
scraper healthy?" — the extractor's error, the gate's findings, the oracle's
agreement rate, what merged. It just threw all of it at stdout, where the
daily CI run is the only reader and nobody reads CI logs. This module writes
that same information to one committed JSON file so the dashboard (and a
human with `git log`) can see a source's history without re-running anything.

Two rules the rest of the pipeline depends on:

- **`record` never raises.** Observability that can break the thing it
  observes is worse than none. Every failure here is swallowed with one line
  to stderr.
- **`summarize` is pure.** Store dict + event list + today's date in, plain
  dict out. No clock, no filesystem — so the dashboard's status vocabulary is
  unit-testable without fixtures.

The file lives in `data/health/`, NOT in `data/store/`: seven code paths glob
`store/*.json` and at least `upcoming.py` would treat a stray file there as a
source to enrich. `.gitignore` whitelists this directory so the CI refresh
commits it along with the store it describes.
"""

import datetime
import json
import os
import pathlib
import sys
import threading

FOUNDRY = pathlib.Path(__file__).parent
HEALTH_PATH = FOUNDRY / "data" / "health" / "health.json"

# Per (source, stage) and per source. Small on purpose: this is a commited
# file, and the interesting history is "what changed recently", not an audit
# trail. Anything older lives in git.
KEEP_PER_STAGE = 6
KEEP_PER_SOURCE = 30
MAX_BYTES = 200_000
MAX_FINDINGS = 6
MAX_MSG = 160
MAX_ERROR = 300

_LOCK = threading.Lock()


def _now():
    return datetime.datetime.now().isoformat(timespec="seconds")


def _trim(detail):
    """Bound a detail payload before it is stored. Findings are the bulky
    part and the first few are the informative ones."""
    if not detail:
        return {}
    out = {}
    for key, value in detail.items():
        if key == "findings" and isinstance(value, list):
            out[key] = [
                {"check": f.get("check"), "ref": f.get("ref"),
                 "msg": str(f.get("msg", ""))[:MAX_MSG]}
                if isinstance(f, dict) else {"msg": str(f)[:MAX_MSG]}
                for f in value[:MAX_FINDINGS]]
            if len(value) > MAX_FINDINGS:
                out["findings_total"] = len(value)
        elif key == "error":
            out[key] = str(value)[:MAX_ERROR]
        elif isinstance(value, (dict, list)):
            out[key] = json.loads(json.dumps(value, default=str))
        else:
            out[key] = value
    return out


def _same(a, b):
    return json.dumps(a, sort_keys=True, default=str) == \
           json.dumps(b, sort_keys=True, default=str)


def compact(events):
    """Bound an event list. Pure: takes and returns a list.

    Collapses a repeat of the newest same-shaped event into a counter rather
    than appending — a source that has drifted the same way for 40 days is
    one fact, not 40, and the daily commit should say so in one line.
    """
    kept, seen = [], {}
    for event in reversed(events):  # newest first while filtering
        stage = event.get("stage")
        seen[stage] = seen.get(stage, 0) + 1
        if seen[stage] <= KEEP_PER_STAGE:
            kept.append(event)
        if len(kept) >= KEEP_PER_SOURCE:
            break
    return list(reversed(kept))


def _append(doc, source_id, event):
    source = doc.setdefault("sources", {}).setdefault(source_id, {"events": []})
    events = source["events"]
    newest = events[-1] if events else None
    if (newest and newest.get("stage") == event["stage"]
            and newest.get("verdict") == event["verdict"]
            and _same(newest.get("detail"), event["detail"])):
        newest["repeats"] = newest.get("repeats", 1) + 1
        newest["ts"] = event["ts"]
        return
    event["first_ts"] = event["ts"]
    event["repeats"] = 1
    events.append(event)
    source["events"] = compact(events)


def _shrink(doc):
    """Last-resort bound on the whole file: drop the oldest events across
    every source until it fits."""
    while len(json.dumps(doc)) > MAX_BYTES:
        oldest, owner = None, None
        for source_id, source in doc.get("sources", {}).items():
            if source["events"] and (oldest is None
                                     or source["events"][0]["ts"] < oldest):
                oldest, owner = source["events"][0]["ts"], source_id
        if owner is None:
            return doc
        doc["sources"][owner]["events"].pop(0)
    return doc


def load(path=HEALTH_PATH):
    """Read the ledger. A missing or corrupt file reads as an empty one —
    losing health history must never be an error a pipeline has to handle."""
    try:
        doc = json.loads(pathlib.Path(path).read_text())
        if not isinstance(doc, dict):
            raise ValueError("health ledger is not an object")
        doc.setdefault("version", 1)
        doc.setdefault("sources", {})
        return doc
    except (OSError, ValueError):
        return {"version": 1, "last_run": None, "sources": {}}


def _write(doc, path):
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(_shrink(doc), indent=1, sort_keys=True))
    os.replace(tmp, path)


def record(source_id, stage, verdict, detail=None, *, path=HEALTH_PATH,
           now=None):
    """Append one event. Stages: refresh, deepen, recertify, oracle, onboard.

    Never raises: a ledger write must not be able to fail a refresh.
    """
    try:
        with _LOCK:
            doc = load(path)
            _append(doc, source_id, {"ts": now or _now(), "stage": stage,
                                     "verdict": verdict,
                                     "detail": _trim(detail)})
            _write(doc, path)
    except Exception as exc:  # pragma: no cover - defensive by contract
        print(f"health: could not record {source_id}/{stage}: {exc}",
              file=sys.stderr)


def record_cycle(results, enrich=None, *, path=HEALTH_PATH, now=None):
    """Record the cycle summary — what ran, where, and how it came out."""
    try:
        with _LOCK:
            doc = load(path)
            doc["last_run"] = {
                "ts": now or _now(),
                # Distinguishes "the scheduled run produced this" from "I ran
                # it on my laptop", which matters because the prod store is
                # whatever CI last committed.
                "host": "ci" if os.environ.get("GITHUB_ACTIONS") else "local",
                "argv": " ".join(sys.argv[1:]),
                "results": results,
                "enrich": enrich or {}}
            _write(doc, path)
    except Exception as exc:  # pragma: no cover - defensive by contract
        print(f"health: could not record cycle: {exc}", file=sys.stderr)


def events_for(doc, source_id):
    return (doc.get("sources", {}).get(source_id) or {}).get("events", [])


# --- derivation -----------------------------------------------------------
# Everything below is pure: the dashboard and the CLI read the same status
# vocabulary, and tests can exercise it with dicts.

def _bucket(note):
    """Why is this record not certified? The distinction the console has to
    make is between OUR failure and the clerk's calendar."""
    text = (note or "").lower()
    if not text:
        return "unreached"
    if "no second-source" in text or "no minutes assertions" in text:
        return "lag"
    if "ingest-only" in text or "single-source" in text or "never certifiable" in text:
        return "uncertifiable"
    return "disputed"


def _latest(events, stage, verdicts=None):
    for event in reversed(events):
        if event.get("stage") == stage and (
                verdicts is None or event.get("verdict") in verdicts):
            return event
    return None


def _days_between(earlier, later):
    try:
        return (datetime.date.fromisoformat(later)
                - datetime.date.fromisoformat(earlier)).days
    except (TypeError, ValueError):
        return None


def public_note(summary, events):
    """One reader-facing sentence explaining why a source is not certified.

    The console's own copy is written for whoever runs the pipeline; this is
    written for whoever reads the ledger. It names OUR gap plainly and never
    leaks operator detail (costs, artifact paths, model names). A `public`
    string recorded on a blocked oracle event wins, because the specific
    reason ("their minutes are really an agenda") is always more useful than
    the generic one.
    """
    status = summary["oracle"]["status"]
    reasons = summary.get("quarantine_reasons") or {}
    latest = _latest(events, "oracle")
    explicit = ((latest or {}).get("detail") or {}).get("public")
    if explicit:
        return explicit
    if status == "no-second-source":
        return ("We could not find a second, independently produced record for "
                "this jurisdiction, so nothing here has been cross-checked yet. "
                "That is our gap, not theirs.")
    if status == "failed":
        return ("We built an independent cross-check for this jurisdiction and "
                "it did not agree with the primary record closely enough to "
                "trust, so we are not certifying anything here until it does.")
    if status == "never-run":
        return ("No independent second source is wired for this jurisdiction "
                "yet, so nothing here has been cross-checked.")
    # Written to stand alone: this sentence is shown on its own in the
    # reader's warning box, so it cannot lean on a preceding clause.
    if reasons.get("lag"):
        return (f"{reasons['lag']} records here are waiting on documents the "
                "jurisdiction has not published yet. Minutes are often "
                "approved months after the meeting itself.")
    if reasons.get("disputed"):
        return (f"The two sources disagree about {reasons['disputed']} records "
                "here. We hold those back rather than pick a winner; the "
                "disagreement is itself the finding.")
    return ("The remainder are items the second source recorded no outcome "
            "for: procedural motions, items carried on a consent agenda, and "
            "gaps in the minutes themselves.")


def summarize(source_id, store, events, today, upcoming=None, stale_days=21):
    """One source's health, derived from its store and its event history.

    Pure — no clock, no filesystem. `today` is an ISO date string, `upcoming`
    the entry for this source from upcoming.json (or None).
    """
    meta = store.get("meta") or {}
    meetings = store.get("meetings") or {}
    is_meetings_source = isinstance(meetings, dict) and "vote_events" in store

    # A vote in a meeting the second source never reached carries no note of
    # its own — only its MEETING records the "no second-source assertions"
    # finding. Counting those votes as an unexplained gap would overstate our
    # failures and hide the real one (the clerk has not published yet), so
    # inherit the meeting's reason.
    lag_meetings = set()
    for meeting in (store.get("meetings") or {}).values():
        block = (meeting or {}).get("certification") or {}
        if block.get("status") != "certified" and _bucket(block.get("note")) == "lag":
            lag_meetings.add(meeting.get("meeting_id"))

    counts, certified, reasons = {}, {}, {}
    for rtype in ("meetings", "agenda_items", "vote_events"):
        records = store.get(rtype) or {}
        records = list(records.values()) if isinstance(records, dict) else records
        counts[rtype] = len(records)
        certified[rtype] = 0
        for record_ in records:
            block = (record_ or {}).get("certification") or {}
            if block.get("status") == "certified":
                certified[rtype] += 1
                continue
            key = _bucket(block.get("note"))
            if (key == "unreached" and rtype != "meetings"
                    and (record_ or {}).get("meeting_id") in lag_meetings):
                key = "lag"
            reasons[key] = reasons.get(key, 0) + 1

    total = sum(counts.values())
    total_certified = sum(certified.values())

    dates = [m.get("date") for m in (meetings.values()
             if isinstance(meetings, dict) else meetings) if m.get("date")]
    newest = max(dates) if dates else None
    days_since = _days_between(newest, today) if newest else None

    next_expected, passed = None, []
    for entry in (upcoming or {}).get("upcoming", []) or []:
        date = entry.get("date")
        if not date:
            continue
        if date > today and (next_expected is None or date < next_expected):
            next_expected = date
        if newest and newest < date < today:
            passed.append(date)

    if not is_meetings_source:
        staleness = "n/a"
    elif newest is None:
        staleness = "unknown"
    elif passed:
        staleness = "due"
    elif days_since is not None and days_since > stale_days:
        staleness = "stale"
    else:
        staleness = "fresh"

    oracle_artifact = meta.get("oracle_artifact")
    last_oracle = _latest(events, "oracle")
    last_recert = _latest(events, "recertify")
    if oracle_artifact:
        drifted = last_recert is not None and last_recert.get("verdict") in (
            "error", "rejected")
        status = "promoted-drifted" if drifted else "promoted"
    elif total_certified:
        # Pittsburgh and LA were certified by hand-built oracles that predate
        # the synthesis path, so they carry no oracle_artifact. Reporting
        # them as "never-run" next to their certified records would be a
        # contradiction the operator has to decode.
        status = "curated"
    elif last_oracle is not None and last_oracle.get("verdict") == "blocked":
        # Distinct from "failed": synthesis was never attempted because the
        # profile's second source cannot carry vote outcomes at all. No
        # number of attempts fixes that — it needs a different source.
        status = "no-second-source"
    elif last_oracle is not None:
        status = "failed"
    else:
        status = "never-run"

    last_refresh = _latest(events, "refresh")
    open_findings = []
    if last_refresh and last_refresh.get("verdict") in ("drift", "error"):
        open_findings = (last_refresh.get("detail") or {}).get("findings", [])

    return {
        "source_id": source_id,
        "title": meta.get("title") or source_id,
        "platform": meta.get("platform"),
        "kind": meta.get("kind") or ("meetings" if is_meetings_source else "other"),
        "artifact": meta.get("artifact"),
        "records": counts,
        "certified": certified,
        "total_records": total,
        "total_certified": total_certified,
        "certified_pct": round(100 * total_certified / total, 1) if total else 0.0,
        "quarantine_reasons": reasons,
        "newest_meeting": newest,
        "days_since_newest": days_since,
        "next_expected": next_expected,
        "staleness": staleness,
        "oracle": {"artifact": oracle_artifact, "status": status,
                   "last_attempt": last_oracle, "last_recertify": last_recert},
        "last_refresh": last_refresh,
        "last_deepen": _latest(events, "deepen"),
        "open_findings": open_findings,
    }
