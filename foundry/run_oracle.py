"""Oracle synthesis: certify an auto-onboarded source from its second source.

    python run_oracle.py <slug> [--attempts 3]

The last hand-crafted step, automated. The discovery profile's
`second_source` goes to the synthesizer, which writes an INDEPENDENT
assertion extractor over the same meetings the primary already put in the
store. A deterministic reconciler joins the two by meeting date + printed
agenda-item number, compares outcomes, tallies, and named positions, and
promotes agreements to certified. Disagreements stay quarantined with the
finding and both sources' evidence attached — surfacing them is the product.
(spec modules 5, 7, 8)
"""

import argparse
import json
import pathlib
import re
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).parent))

import harness
import sandbox2
import synthesize
from run_onboard import _norm_ws

FOUNDRY = pathlib.Path(__file__).parent
STORE = FOUNDRY / "data" / "store"

ORACLE_CONTRACT = """## Oracle artifact contract

Write a complete Python module implementing an INDEPENDENT second-source
assertion extractor for `{source_id}`, using the second source described in
the profile above.

- Define `EXTRACTOR_VERSION = "1"`.
- Define `extract(rt, args) -> (assertions, run_meta)`. `args` is a LIST of
  the CLI arguments; `max_meetings = int(args[0])`.
- `rt.fetch_json(url, params=None)` / `rt.fetch_text(url, params=None)`
  (absolute URLs; PDFs converted to text) are the only I/O. Skip meetings
  whose document is missing or unparseable rather than crashing.
- Read vote data ONLY from the second-source documents. NEVER read the
  primary source's data endpoints ({primary_endpoints}). The whole value of
  this artifact is independence: it must affirm or contradict the primary
  from a separately produced document. Navigation/listing pages may be used
  to locate documents.
- Cover the most recent `max_meetings` COMPLETED meetings that have a
  second-source document, newest first.
- `assertions` = an object keyed by meeting date "YYYY-MM-DD":
  {{"attendance": {{"present": [last names], "absent": [last names]}}
      (empty lists when the document does not state attendance),
   "items": {{item_key: [entry, ...]}}}}
  item_key = the printed agenda number exactly as the document shows it,
  lowercased, with no trailing ")" (e.g. "4.a", "6", "12.b").
  entry = {{
    "title": "<the item/motion heading or subject exactly as the document
      states it — used to join against the primary source's item titles>",
    "result": "pass" | "fail"  (did the recorded motion carry),
    "counts": {{"aye"/"no"/"abstain"/"absent"/"recused": int}} or null when
      the document states no numeric tally,
    "positions": {{"LastName": "aye|no|abstain|absent|recused"}} for every
      member the document explicitly NAMES with a stance — dissenters,
      abstainers, and listed absentees belong here; movers/seconders do NOT
      imply aye unless the document says how they voted. {{}} if none named.
    "unanimous": true / false / null exactly as the document states,
    "evidence": {{"quote": "<verbatim passage, <=400 chars, copied EXACTLY
      from the fetched document text, containing the outcome — the gate
      literally greps for it>", "doc_url": "<that document's url>"}}}}
- Assert an item ONLY where the document explicitly records a motion
  outcome (APPROVED / DENIED / carried / failed / a tally). Never default
  or extrapolate: a missing assertion is correct when the document is
  silent.
- Segmentation discipline: narrative minutes print an outcome sentence at
  the END of an item's narrative, BEFORE the next heading — attach each
  outcome to the item ABOVE it, never to the heading that follows it. Emit
  one entry per recorded motion, and set `title` to that motion's own
  subject; never reuse a previous heading's title for later entries.
- Some meetings legitimately have no second-source document yet (clerks
  publish with lag, sometimes months). Cover every store meeting whose
  document exists; list the others in run_meta — never skip silently and
  never fabricate coverage.
- `run_meta` = {{"source_id": "{source_id}-oracle", "extractor_version": ...,
  "row_counts": {{"meetings": n, "entries": n}},
  "meetings_without_document": ["YYYY-MM-DD", ...] — every store meeting
  date whose second-source document does not exist or could not be located}}.
- Deterministic, stdlib only, no LLM, no network beyond `rt`.
- Budget ~8 minutes: fetch only what the assertions need."""

ITEM_KEY_RE = re.compile(r"^\s*(\d+[A-Za-z0-9.\-]*)\s*[).]")
CONSENT_RE = re.compile(r"items? on consent:?\s*([0-9a-zA-Z ,\-and]+?)(?:\.|\(|$)", re.I)


def derive_keys(title, vote):
    """Every join key a primary vote can answer to: its item's printed
    number, or — for consent motions that enumerate their items ("approve
    items 1a, 1b, 2a, 3, and 5") — each named item's parent number."""
    k = item_key_from_title(title)
    if k:
        return {k}
    m = CONSENT_RE.search(vote.get("motion") or "")
    if m:
        toks = [t.strip().lower() for t in re.split(r",|\band\b", m.group(1))]
        parents = {re.match(r"(\d+)[a-z]?$", t).group(1)
                   for t in toks if re.match(r"^\d+[a-z]?$", t)}
        if parents:
            return parents
    return {vote["vote_id"].rsplit("vote-", 1)[-1].replace("-", ".").lower()}
OUTCOME_RE = re.compile(
    r"approv|denie|deny|carrie|passe?d|fail|adopt|defer|motion|moved|"
    r"second|aye|nay|unanimous|vote", re.I)


def item_key_from_title(title):
    m = ITEM_KEY_RE.match(title or "")
    return m.group(1).rstrip(".").lower() if m else None


def store_records(store):
    return {rtype: list(store[rtype].values())
            for rtype in ("meetings", "agenda_items", "vote_events", "members")}


def build_messages(profile, slug, store, rt):
    second = profile.get("second_source") or {}
    primary = profile.get("primary_source", {})
    if not second.get("urls"):
        raise SystemExit("profile has no usable second_source.urls — nothing "
                         "independent to reconcile against")
    samples = []
    for url in second["urls"][:2]:
        try:
            body = rt.fetch_text(url)
        except Exception as exc:
            samples.append(f"`{url}` -> FETCH FAILED: {exc}")
            continue
        if "<html" in body[:2000].lower():
            body = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", body, flags=re.S | re.I)
            body = re.sub(r"<[^>]+>", " ", body)
            body = re.sub(r"[ \t]+", " ", body)
        samples.append(f"`{url}` (excerpt of {len(body)} chars):\n```\n{body[:6000]}\n```")

    dates = sorted((m["date"] for m in store["meetings"].values()), reverse=True)
    prompt = (f"## Source profile (agent-discovered)\n\n"
              f"```json\n{json.dumps(profile, indent=1)}\n```\n\n"
              f"## Second-source document samples (live)\n\n" + "\n\n".join(samples) +
              f"\n\n## Meetings already in the store (the primary extracted these; "
              f"your assertions must cover them)\n\n{json.dumps(dates)}\n\n" +
              ORACLE_CONTRACT
              .replace("{source_id}", f"{slug}-bos")
              .replace("{primary_endpoints}",
                       ", ".join(primary.get("base_urls", [])[:4])))
    return [{"role": "user", "content": prompt}]


def oracle_floors(assertions, store, cache, run_meta=None):
    findings = []
    if not isinstance(assertions, dict):
        return [{"layer": "gate", "check": "malformed_root", "ref": "run",
                 "msg": "assertions must be an object keyed by YYYY-MM-DD date"}]
    store_dates = {m["date"] for m in store["meetings"].values()}
    covered = store_dates & set(assertions)
    declared_missing = set((run_meta or {}).get("meetings_without_document") or [])
    if len(covered) < min(len(store_dates), 2):
        findings.append({"layer": "gate", "check": "oracle_coverage", "ref": "run",
                         "msg": f"assertions cover {sorted(covered)} but the store "
                                f"has meetings on {sorted(store_dates)} — cover at "
                                "least two of them"})
    unaccounted = store_dates - covered - declared_missing
    if not findings and unaccounted:
        findings.append({"layer": "gate", "check": "oracle_coverage", "ref": "run",
                         "msg": f"unaccounted store meetings {sorted(unaccounted)} — "
                                "cover each one or list it in "
                                "run_meta.meetings_without_document (clerks publish "
                                "with lag; a documented gap is honest, silence is not)"})
    haystack = [_norm_ws(v if isinstance(v, str) else json.dumps(v))
                for v in cache.values()]
    n_entries, informative, results = 0, 0, set()
    for date, asserted in assertions.items():
        if date not in store_dates:
            continue  # extra meetings feed nothing; don't gate on them
        if not isinstance(asserted, dict) or not isinstance(asserted.get("items"), dict):
            findings.append({"layer": "gate", "check": "malformed_meeting", "ref": date,
                             "msg": "asserted meeting needs {attendance, items} objects"})
            continue
        for key, entries in asserted["items"].items():
            for e in entries if isinstance(entries, list) else []:
                n_entries += 1
                ref = f"{date}/{key}"
                if not isinstance(e, dict) or e.get("result") not in ("pass", "fail"):
                    findings.append({"layer": "gate", "check": "bad_entry", "ref": ref,
                                     "msg": "entry.result must be pass|fail"})
                    continue
                results.add(e["result"])
                if e.get("counts") or e.get("positions") \
                        or e.get("unanimous") is not None:
                    informative += 1
                ev = e.get("evidence") or {}
                quote = _norm_ws(ev.get("quote") or "")
                if not quote:
                    findings.append({"layer": "gate", "check": "no_evidence", "ref": ref,
                                     "msg": "every assertion needs evidence.quote"})
                    continue
                if not any(quote in h for h in haystack):
                    findings.append({"layer": "gate", "check": "evidence_not_in_source",
                                     "ref": ref,
                                     "msg": f"evidence quote not found verbatim in any "
                                            f"fetched document: \"{quote[:90]}…\""})
                # provenance is not decision: the quote must RECORD an outcome,
                # not merely appear on the page (lesson: an oracle once passed
                # this gate quoting agenda-listing table markup)
                spoken = re.sub(r"<[^>]+>|&[a-z]+;", " ", ev.get("quote") or "")
                if not OUTCOME_RE.search(spoken):
                    findings.append({"layer": "gate", "check": "no_outcome_language",
                                     "ref": ref,
                                     "msg": "evidence quote contains no decision "
                                            "language (approved/denied/carried/moved/"
                                            "aye/unanimous...) — quote the passage that "
                                            "records the outcome, not the agenda listing"})
    if n_entries >= 20 and results == {"pass"} and informative == 0:
        findings.append({"layer": "gate", "check": "no_information", "ref": "run",
                         "msg": f"all {n_entries} assertions are bare 'pass' with no "
                                "tally, positions, or unanimity anywhere — this "
                                "contributes no independent information; extract the "
                                "actual outcome details or assert nothing"})
    dates_by_mid = {m["meeting_id"]: m["date"] for m in store["meetings"].values()}
    n_votes = sum(1 for ve in store["vote_events"].values()
                  if dates_by_mid.get(ve["meeting_id"]) in covered)
    if n_entries < n_votes // 2:
        findings.append({"layer": "gate", "check": "oracle_coverage", "ref": "run",
                         "msg": f"only {n_entries} asserted outcomes vs {n_votes} store "
                                "votes in the meetings you covered — those documents "
                                "record more than that"})
    return findings


def _tokens(s):
    s = re.sub(r"<[^>]+>|&[a-z]+;", " ", (s or "").lower())
    return set(re.findall(r"[a-z0-9]{3,}", s))


TALLY_RE = re.compile(
    r"(?:passed|carried|failed|approved)\s*[,:]?\s*(\d+)\s*-\s*(\d+)"
    r"(?:\s*-\s*(\d+))?(?:\s*-\s*(\d+))?", re.I)


def entry_counts(entry):
    """The entry's tally — as asserted, or parsed from its own verbatim
    quote ('The motion passed 7-0-1-1' is machine-readable: aye-no-absent-
    abstain in minutes convention)."""
    if entry.get("counts"):
        return {k: v for k, v in entry["counts"].items() if v}
    m = TALLY_RE.search((entry.get("evidence") or {}).get("quote") or "")
    if not m:
        return None
    parts = dict(zip(("aye", "no", "absent", "abstain"),
                     (int(g) for g in m.groups() if g is not None)))
    return {k: v for k, v in parts.items() if v}


def compatible(ve, entry):
    """Could this minutes entry be the record of this vote? Result must
    match; tally and named positions must not contradict."""
    if entry.get("result") != ve.get("result"):
        return False
    ec = entry_counts(entry)
    if ec and ec != {k: v for k, v in ve.get("counts", {}).items() if v}:
        return False
    if entry.get("unanimous") is True and ve.get("counts", {}).get("no"):
        return False
    positions = {harness.member_key(p["member"]): p["position"]
                 for p in ve.get("positions", [])}
    for name, stance in (entry.get("positions") or {}).items():
        got = positions.get(harness.member_key(name))
        if (got is None and stance != "absent") or (got is not None and got != stance):
            return False
    return True


def _title_ok(a, b):
    """Verify a numeric join with titles when both sides have them. Two
    documents can number things in unrelated namespaces (minutes number
    MOTIONS sequentially, agendas number ITEMS) — an exact key match across
    namespaces is a coincidence unless the words agree too."""
    if not a or not b:
        return True  # nothing to verify with; trust the number
    return len(a & b) >= 2 and len(a & b) / min(len(a), len(b)) >= 0.3


def match_meeting(store_votes, oracle_items, pk_titles):
    """Join oracle assertions to primary votes, three tiers, all deterministic:

    1. exact printed item number, title-verified when titles exist;
    2. parent/child rollup — sources record at different granularity: a
       votelog logs one vote per MOTION ("5" approves the whole consent
       agenda) while action minutes log one outcome per SUB-ITEM
       ("5.a".."5.z"); a child outcome affirms its parent's motion, and a
       vote on a pulled sub-item ("5.h") falls back to the en-bloc parent;
    3. entry-level title similarity — the same motion can carry different
       numbers in the two documents (or none in one); a strong token
       overlap between an entry's title/quote and a vote's title or motion
       text joins them one entry at a time."""
    mapping, unmatched = {}, []
    pk_tok = {pk: _tokens(t) for pk, t in pk_titles.items()}
    for k, entries in oracle_items.items():
        e_tok = _tokens(entries[0].get("title") or "")
        pk = next((c for c in (k, k.split(".")[0])
                   if c in store_votes and _title_ok(e_tok, pk_tok.get(c, set()))),
                  None)
        if pk is None:
            unmatched.append(k)
        else:
            mapping.setdefault(pk, []).extend(entries)
    for pk in store_votes:
        parent = pk.split(".")[0]
        if pk not in mapping and parent in oracle_items:
            e_tok = _tokens(oracle_items[parent][0].get("title") or "")
            if _title_ok(e_tok, pk_tok.get(pk, set())):
                mapping[pk] = list(oracle_items[parent])
    # tier 3: exclusive compatible assignment. Score every (entry, vote-key)
    # pair by word overlap, then assign greedily best-first — each entry
    # claims at most one key, each key takes at most as many entries as it
    # has votes, and only compatible pairings (result/tally/positions don't
    # contradict) are allowed. Near-identical motions (six committee
    # recommendations in a row) thus pair one-to-one instead of piling onto
    # whichever scored first.
    pairs = []
    for k in unmatched:
        for idx, e in enumerate(oracle_items[k]):
            qt = _tokens(e.get("title") or "") | \
                _tokens((e.get("evidence") or {}).get("quote"))
            for pk, toks in pk_tok.items():
                if pk in mapping or not qt or not toks:
                    continue
                score = len(qt & toks) / min(len(qt), len(toks))
                if score >= 0.3 and len(qt & toks) >= 2 and \
                        any(compatible(ve, e) for ve in store_votes[pk]):
                    pairs.append((score, k, idx, pk))
    pairs.sort(key=lambda p: -p[0])
    taken_entries, load = set(), {}
    for score, k, idx, pk in pairs:
        if (k, idx) in taken_entries or load.get(pk, 0) >= len(store_votes[pk]):
            continue
        taken_entries.add((k, idx))
        load[pk] = load.get(pk, 0) + 1
        # tier-3 pairs are tally-compatible but the quote window may be
        # misaligned with THIS item — mark them so certification doesn't
        # display an unaligned sentence as if it described this vote
        mapping.setdefault(pk, []).append({**oracle_items[k][idx], "_tier3": True})
    for k in list(unmatched):
        if all((k, i) in taken_entries for i in range(len(oracle_items[k]))):
            unmatched.remove(k)
    return mapping, unmatched


def reconcile(store, assertions, source_id):
    """Deterministic cross-source comparison, joined by date + printed item
    number with parent/child rollup. Same philosophy as harness.reconcile,
    generalized past file_number so single-source jurisdictions can graduate.

    Returns (findings, affirmed) where affirmed maps vote_id -> (ref,
    affirming oracle entry) for every primary vote the second source
    reached; certification is affirmed minus disputed."""
    findings = []
    affirmed = {}

    def find(check, ref, msg):
        findings.append({"layer": "reconcile", "check": check, "ref": ref, "msg": msg})

    titles = {i["item_id"]: i.get("title", "") for i in store["agenda_items"].values()}
    votes_by_meeting, key_titles = {}, {}
    for ve in store["vote_events"].values():
        title = titles.get(ve.get("item_id"), "")
        for key in sorted(derive_keys(title, ve)):
            votes_by_meeting.setdefault(ve["meeting_id"], {}).setdefault(key, []).append(ve)
            key_titles.setdefault(ve["meeting_id"], {}).setdefault(
                key, title or ve.get("motion", ""))

    for meeting in store["meetings"].values():
        mid, date = meeting["meeting_id"], meeting["date"]
        asserted = assertions.get(date)
        if asserted is None:
            find("no_second_source", mid, "no second-source assertions for this meeting")
            continue

        att = meeting.get("attendance") or {}
        store_att = {s: {harness.member_key(n) for n, st in att.items() if st == s}
                     for s in ("present", "absent")}
        oracle_att = {s: {harness.member_key(n)
                          for n in asserted.get("attendance", {}).get(s, [])}
                      for s in ("present", "absent")}
        # sources differ in what they record (a votelog may only know
        # attendees); only a direct present-vs-absent contradiction disputes
        conflicts = (oracle_att["present"] & store_att["absent"]) | \
                    (oracle_att["absent"] & store_att["present"])
        if conflicts:
            find("attendance_mismatch", mid,
                 f"sources contradict on {sorted(conflicts)}: primary "
                 f"present={sorted(store_att['present'])} absent={sorted(store_att['absent'])}, "
                 f"second source present={sorted(oracle_att['present'])} "
                 f"absent={sorted(oracle_att['absent'])}")

        store_votes = votes_by_meeting.get(mid, {})
        oracle_items = {k: v for k, v in asserted["items"].items() if v}
        mapping, unmatched = match_meeting(store_votes, oracle_items,
                                           key_titles.get(mid, {}))
        for key in unmatched:
            find("vote_coverage", f"{mid}/{key}",
                 "second source records an outcome but primary has no vote "
                 "on that item or its parent")
        for pk, in_store in store_votes.items():
            ref = f"{mid}/{pk}"
            entries = mapping.get(pk, [])
            if not entries:
                continue  # unmatched votes reported once each, below
            results = {ve["result"] for ve in in_store}
            for entry in entries:
                if entry["result"] not in results:
                    find("vote_mismatch", ref,
                         f"primary says {sorted(results)}, second source says "
                         f"{entry['result']} — \"{(entry.get('evidence') or {}).get('quote', '')[:120]}\"")
                if len(in_store) != 1:
                    continue  # tally/position checks need a 1:1 pairing
                ve = in_store[0]
                counts = entry_counts(entry)
                if counts:
                    a = {k: v for k, v in ve["counts"].items() if v}
                    b = {k: v for k, v in counts.items() if v}
                    if a != b:
                        find("vote_mismatch", ref,
                             f"tally disagrees: primary {a} vs second source {b}")
                if entry.get("unanimous") is True and ve["counts"].get("no"):
                    find("vote_mismatch", ref,
                         "second source says unanimous but primary records no-votes")
                positions = {harness.member_key(n): p["position"]
                             for p in ve["positions"] for n in [p["member"]]}
                for name, stance in (entry.get("positions") or {}).items():
                    got = positions.get(harness.member_key(name))
                    if got is None:
                        # a member the minutes list as absent won't appear in
                        # a voters-only primary record — consistent, not a dispute
                        if stance != "absent":
                            find("vote_mismatch", ref,
                                 f"second source has {name}:{stance}; primary does "
                                 "not list that member on this vote")
                    elif got != stance:
                        find("vote_mismatch", ref,
                             f"{name}: primary says {got}, second source says {stance}")
            for ve in in_store:
                affirmed[ve["vote_id"]] = (ref, entries[0])
        # one coverage finding per vote no key of which matched (a consent
        # vote registers under many keys; don't report it many times)
        reported = set()
        for vs in store_votes.values():
            for ve in vs:
                vid = ve["vote_id"]
                if vid in affirmed or vid in reported:
                    continue
                reported.add(vid)
                what = (titles.get(ve.get("item_id")) or ve.get("motion") or vid)
                find("vote_coverage", f"{mid}/{vid.split(mid + '-', 1)[-1]}",
                     "vote recorded by primary but second source shows no "
                     f"outcome — \"{what[:90]}\"")
    return findings, affirmed


def certify(store, assertions, findings, affirmed, method):
    disputed = {}
    for f in findings:
        disputed.setdefault(f["ref"], []).append(f["msg"])

    def mark(rec, ok, note=None, evidence=None):
        rec["certification"] = {"status": "certified" if ok else "quarantined",
                                "method": method if ok else None, "note": note}
        if evidence:
            rec["certification"]["evidence"] = evidence
        return ok

    n = {"certified": 0, "total": 0, "disputes": len(disputed)}
    for meeting in store["meetings"].values():
        mid = meeting["meeting_id"]
        ok = meeting["date"] in assertions and mid not in disputed
        n["certified"] += mark(meeting, ok, "; ".join(disputed.get(mid, [])) or None)
        n["total"] += 1

    vote_ok = {}
    for ve in store["vote_events"].values():
        mid = ve["meeting_id"]
        ref, entry = affirmed.get(ve["vote_id"], (None, None))
        ok = ref is not None and ref not in disputed and mid not in disputed
        tier3 = bool(entry and entry.get("_tier3"))
        n["certified"] += mark(ve, ok,
                               "; ".join(disputed.get(ref, [])) if ref else None,
                               evidence=None if tier3
                               else (entry or {}).get("evidence") if ok else None)
        if ok and tier3:
            ve["certification"]["method"] += \
                " — outcome-set (tally) match; no item-aligned quote"
        n["total"] += 1
        vote_ok[ve.get("item_id")] = vote_ok.get(ve.get("item_id"), True) and ok

    for item in store["agenda_items"].values():
        has_vote = item["item_id"] in vote_ok
        ok = vote_ok.get(item["item_id"], False)
        if has_vote or ok:
            n["certified"] += mark(item, ok)
            n["total"] += 1
    return n


def run(slug, attempts=3, log=print):
    source_id = f"{slug}-bos"
    store_path = STORE / f"{source_id}.json"
    store = json.loads(store_path.read_text())
    profile = json.loads(
        (FOUNDRY / "data" / "discovery" / f"{slug}_profile.json").read_text())

    cache_path = FOUNDRY / "data" / "oracle" / f"{slug}_http_cache.json"
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    rt = sandbox2.Runtime(json.loads(cache_path.read_text())
                          if cache_path.exists() else {})
    messages = build_messages(profile, slug, store, rt)
    cache_path.write_text(json.dumps(rt.cache))

    artifacts = FOUNDRY / "extractors" / f"{source_id}-oracle"
    artifacts.mkdir(parents=True, exist_ok=True)
    out_path = FOUNDRY / "data" / "oracle" / f"{slug}_assertions_raw.json"
    n_meetings = len(store["meetings"])
    usages, assertions, passed = [], None, False
    t0 = time.time()

    for attempt in range(1, attempts + 1):
        log(f"attempt {attempt}: synthesizing oracle with {synthesize.MODEL}...")
        try:
            code, assistant_content, usage = synthesize.generate(messages)
        except RuntimeError as exc:
            log(f"  synthesis failed: {str(exc)[:140]} — fresh attempt")
            continue
        usages.append(usage)
        artifact = artifacts / f"v1_attempt{attempt}.py"
        artifact.write_text(code)
        log(f"  artifact: {artifact.name} ({len(code.splitlines())} lines)")
        assertions, error = sandbox2.run_artifact(
            artifact, [n_meetings], out_path, cache_path)
        findings = []
        if error is None:
            run_meta = json.loads(out_path.read_text()).get("run_meta") or {}
            findings = oracle_floors(
                assertions, store,
                json.loads(cache_path.read_text()) if cache_path.exists() else {},
                run_meta)
        else:
            log("  execution failed: " + error.strip().splitlines()[-1][:120])
        if error is None and not findings:
            # the last gate layer IS the reconciliation: a syntactically
            # perfect oracle that can't agree with the primary on the
            # meetings it covered is misreading the document (misaligned
            # segmentation, wrong titles) — feed the disagreements back
            rec_findings, affirmed = reconcile(store, assertions, source_id)
            disputed = {f["ref"] for f in rec_findings}
            dates = {m["meeting_id"]: m["date"] for m in store["meetings"].values()}
            covered = [ve for ve in store["vote_events"].values()
                       if dates.get(ve["meeting_id"]) in assertions]
            clean = sum(1 for ve in covered
                        if ve["vote_id"] in affirmed
                        and affirmed[ve["vote_id"]][0] not in disputed
                        and ve["meeting_id"] not in disputed)
            rate = clean / len(covered) if covered else 0.0
            if rate >= 0.6:
                passed = True
                log(f"  ORACLE GATE PASSED (agreement rate {rate:.0%}: "
                    f"{clean}/{len(covered)} votes affirmed on covered meetings)")
                break
            findings = rec_findings[:8] + [{
                "layer": "gate", "check": "low_agreement", "ref": "run",
                "msg": f"only {clean}/{len(covered)} votes in covered meetings "
                       "were affirmed after reconciliation. Common causes: an "
                       "outcome sentence in minutes belongs to the item ABOVE "
                       "it — never attach the outcome that FOLLOWS a heading to "
                       "that heading; emit one entry per recorded motion, "
                       "titled with that motion's own subject (never reuse a "
                       "previous heading's title); include the mover's motion "
                       "text in the title when the section has no heading"}]
        log(f"  gate failed: {len(findings)} findings")
        for f in findings[:6]:
            log(f"    [{f['layer']}/{f['check']}] {f['ref']}: {f['msg'][:120]}")
        messages.append({"role": "assistant", "content": assistant_content})
        messages.append(synthesize.feedback_message(error, findings))

    log(f"oracle cost: {len(usages)} attempts, ${synthesize.cost_usd(usages):.2f}, "
        f"{(time.time() - t0) / 60:.1f} min")
    if not passed:
        log("verdict: FAILED — no oracle candidate cleared the gate")
        return None

    (FOUNDRY / "data" / "oracle" / f"{slug}_assertions.json").write_text(
        json.dumps(assertions, indent=1))
    findings, affirmed = reconcile(store, assertions, source_id)
    method = ("cross-source: primary extractor × independent second-source "
              f"document ({(profile.get('second_source') or {}).get('system', '')[:60]})")
    counts = certify(store, assertions, findings, affirmed, method)
    meta = store.get("meta", {})
    meta["sub"] = (meta.get("sub", "").replace(", single-source", "") +
                   " · cross-source certified").lstrip(" ·")
    meta["oracle_artifact"] = str(artifact.relative_to(FOUNDRY))
    store["meta"] = meta
    store_path.write_text(json.dumps(store, indent=1))

    log(f"reconcile: {len(findings)} findings across "
        f"{len({f['ref'] for f in findings})} refs")
    for f in findings[:10]:
        log(f"  [{f['check']}] {f['ref']}: {f['msg'][:130]}")
    pct = counts["certified"] / counts["total"] if counts["total"] else 0
    log(f"certified: {counts['certified']}/{counts['total']} ({pct:.0%}), "
        f"{counts['disputes']} disputed refs -> {store_path.name}")
    return counts


def recertify(slug, log=print):
    """Re-run the PROMOTED oracle artifact over the current store and refresh
    every certification. Deterministic — the hot path stays LLM-free. Returns
    counts, or None when there is no promoted oracle or it drifted."""
    source_id = f"{slug}-bos"
    store_path = STORE / f"{source_id}.json"
    store = json.loads(store_path.read_text())
    rel = store.get("meta", {}).get("oracle_artifact")
    if not rel or not (FOUNDRY / rel).exists():
        return None
    profile = json.loads(
        (FOUNDRY / "data" / "discovery" / f"{slug}_profile.json").read_text())
    # fresh cache every run: the write-through cache would otherwise pin
    # listing pages to the day they were first fetched
    cache_path = FOUNDRY / "data" / "oracle" / f"{slug}_recert_cache.json"
    cache_path.unlink(missing_ok=True)
    out_path = FOUNDRY / "data" / "oracle" / f"{slug}_assertions_raw.json"
    assertions, error = sandbox2.run_artifact(
        FOUNDRY / rel, [len(store["meetings"])], out_path, cache_path)
    if error is not None:
        log(f"  oracle artifact failed: {error.strip().splitlines()[-1][:120]}"
            " — DRIFT, needs re-synthesis (run_oracle.py)")
        return None
    findings, affirmed = reconcile(store, assertions, source_id)
    method = ("cross-source: primary extractor × independent second-source "
              f"document ({(profile.get('second_source') or {}).get('system', '')[:60]})")
    counts = certify(store, assertions, findings, affirmed, method)
    store_path.write_text(json.dumps(store, indent=1))
    log(f"  recertified {source_id}: {counts['certified']}/{counts['total']} "
        f"({counts['disputes']} disputed refs)")
    return counts


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("slug")
    parser.add_argument("--attempts", type=int, default=3)
    args = parser.parse_args()
    return 0 if run(args.slug, args.attempts) else 1


if __name__ == "__main__":
    sys.exit(main())
