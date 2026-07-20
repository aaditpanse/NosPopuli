"""Generic profile-driven onboarding: discovery profile -> synthesized
extractor -> harness gate -> quarantined records in the store.

    python run_onboard.py <slug> [--meetings 3] [--attempts 4] [--resume]

Nothing jurisdiction-specific lives here. The LLM gets the agent-discovered
source profile verbatim, live samples of the documents the profile points
at, and the domain schema — and must write the extractor. The gate is the
harness WITHOUT the cross-source layer (no oracle is wired yet), plus
presence floors, so output can be wrong-but-flagged, never structurally
bunk. Every record lands quarantined: single-source jurisdictions are
ingested, never certified (spec, quarantine rule).
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
from backfill import merge

FOUNDRY = pathlib.Path(__file__).parent
STORE = FOUNDRY / "data" / "store"

CONTRACT = """## Artifact contract

Write a complete Python module implementing an extractor for source
`{source_id}`, using ONLY the source profile above (produced by a discovery
agent that verified those URLs) and the document samples.

- Define `EXTRACTOR_VERSION = "1"`.
- Define `extract(rt, args) -> (records, run_meta)`. `args` is a LIST of
  the CLI arguments; `max_meetings = int(args[0])`.
- `rt` is the injected runtime: `rt.fetch_json(url, params=None)` and
  `rt.fetch_text(url, params=None)` (absolute URLs; PDFs are converted to
  text). The only I/O available. Be robust: skip meetings whose documents
  are missing or unparseable rather than crashing.
- Enumerate meetings the way the profile describes, take the most recent
  `max_meetings` COMPLETED meetings that have an actions/minutes document,
  newest first. Listings routinely include FUTURE scheduled meetings —
  jurisdictions publish their calendar a year or more ahead, and those
  events have no minutes and usually no files at all. Compare meeting dates
  to datetime.date.today() and skip anything not strictly in the past;
  where the endpoint supports server-side date filtering (e.g. OData
  `$filter=startDateTime lt ...`), filter there instead of paging through
  months of empty future events. Listing endpoints PAGE their results and
  servers commonly cap page size far below the requested `$top` (15 rows is
  typical) — follow the pagination affordance (`@odata.nextLink`, skiptoken,
  page params) until you have enough qualifying past meetings, especially
  when many bodies share one calendar.
- `records` = {{"meetings": [...], "agenda_items": [...], "vote_events":
  [...], "members": [...]}} in the domain schema. id conventions:
  meeting_id = f"{source_id}-{{date}}" (date as YYYY-MM-DD),
  item_id / vote_id prefixed with meeting_id plus a stable suffix.
- meeting.attendance: derive from the documents (roll call / present-absent
  lists). If the jurisdiction has no file-number system, set file_number to
  null (allowed by schema {schema_version}).
- agenda_item.title must be the item's SUBJECT — the agenda heading or a
  concise description of what is being decided ("Rezoning RC23155357,
  Stafford Technology Campus data center"), typically found just BEFORE the
  motion in the document. Never use the motion sentence or tally text as a
  title; if no heading exists, derive a short subject from the motion
  ("Approve Resolution R26-241").
- meeting.source_url must be a HUMAN-VIEWABLE page (the meeting's public
  page or archive listing) — never a raw data/handler endpoint (.ashx,
  .asmx, api paths). Keep machine endpoints in a `data_source_url` field.
- vote_events need per-member positions. If votes are recorded as narrative
  prose (movers, seconders, "carried by unanimous vote", named dissenters/
  abstentions/absences), derive positions from the attendance roster minus
  the named exceptions — and only emit a vote_event where the document
  EXPLICITLY records a motion/action outcome for that item ("APPROVED",
  "carried", "ADOPTED", a tally). Never default a vote for section headings,
  recesses, presentations, or hearing listings that show no action language:
  a fabricated 9-0 is worse than no record. Parse the vote language of each
  motion — narrative minutes record dissent and split tallies ("carried by a
  vote of nine, Supervisor X voting 'NAY'", abstentions, absences); a run
  where every vote is a full-roster unanimous aye is rejected by the gate.
  counts must equal the tally of positions. result: "pass" if the motion
  carried, "fail" otherwise.
- Every vote_event derived from narrative text must carry `evidence`:
  {{"quote": the verbatim passage (<=400 chars) recording the motion and its
  outcome, copied EXACTLY from the document text you fetched — the gate
  literally greps for it — "doc_url": that document's url}}. Sources that
  are already structured data (JSON vote records) may omit evidence.
- members: one record per distinct person seen.
- `run_meta` = {{"source_id": "{source_id}", "extractor_version": ...,
  "schema_version": "{schema_version}", "row_counts": {{type: count}}}}.
- Deterministic, stdlib only, no LLM, no network beyond `rt`.
- Platform deployments differ only by tenant: derive every URL from
  module-level constants defined once at the top (e.g. `BASE`, `VIEW_ID`),
  so the artifact can be re-pointed at another tenant of the same platform.
- Be economical: total runtime budget is ~8 minutes. Fetch only what the
  extraction needs — where an actions/minutes summary exists, use it and do
  NOT download full agenda-packet documents (often hundreds of pages).

## The gate, in full (every check below runs mechanically; each failure
## costs a repair round — clear ALL of them on the first attempt)
- meetings: at least 2; newest within 120 days; all strictly in the past;
  the date PRINTED INSIDE each document must match the meeting's date
  (clerks misattach files); two meetings must never be built from
  byte-identical documents fetched from different URLs.
- votes: at least 8 across the run; never all-unanimous-full-roster;
  counts must equal the tally of positions; where the evidence quote
  itself states numbers ("Affirmative: 45", "Yea: (7)"), counts must
  reproduce them EXACTLY — losing members to bad name-splitting is the
  classic cause.
- positions: every member string is a PERSON'S NAME — no digits, newlines,
  or document vocabulary; split "A and B" pairs into two members; preserve
  accents and compound surnames.
- one roll call, one vote_event: two votes in a meeting must never cite an
  identical evidence quote — anchor each quote at ITS OWN motion line, not
  at a shared tally block.
- agenda_item titles carry the SUBJECT: after stripping action verbs,
  record types, and numbers, real words about what is decided must remain.
- every narrative-vote evidence quote greps verbatim against fetched text."""


# Order matters: "Granicus Legistar" must classify as legistar (the
# legislative suite), not granicus (the media/votelog platform) — Granicus
# owns Legistar, so profiles often name both.
PLATFORMS = ("legistar", "granicus", "laserfiche", "primegov", "escribe",
             "civicplus")


def platform_of(profile):
    system = (profile.get("primary_source", {}).get("system") or "").lower()
    return next((p for p in PLATFORMS if p in system), None)


def family_template(slug, profile):
    """A proven extractor for another tenant of the same platform, if one
    exists — handed to the synthesizer as a starting point so onboarding
    the Nth county on a platform costs one attempt, not four."""
    platform = platform_of(profile)
    if not platform:
        return None
    for store_path in sorted(STORE.glob("*-bos.json")):
        if store_path.stem == f"{slug}-bos":
            continue
        meta = json.loads(store_path.read_text()).get("meta", {})
        if meta.get("platform") == platform and meta.get("artifact"):
            artifact = FOUNDRY / meta["artifact"]
            if artifact.exists():
                return store_path.stem, platform, artifact.read_text()
    return None


def build_messages(profile, slug, rt):
    schema_src = (FOUNDRY / "schema.py").read_text()
    primary = profile.get("primary_source", {})
    urls = list(primary.get("base_urls", []))[:1] + \
        list(primary.get("sample_document_urls", []))[:3]
    samples = []
    for url in urls:
        if "{" in url:  # templated pattern, not fetchable
            continue
        try:
            body = rt.fetch_text(url)
        except Exception as exc:
            samples.append(f"`{url}` -> FETCH FAILED: {exc}")
            continue
        if "<html" in body[:2000].lower():
            body = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", body, flags=re.S | re.I)
            body = re.sub(r"<[^>]+>", " ", body)
            body = re.sub(r"[ \t]+", " ", body)
        if len(" ".join(body.split())) < 400:
            # A JS-app shell teaches the synthesizer nothing (and mis-teaches
            # plenty) — say what it is instead of showing it.
            samples.append(
                f"`{url}` -> JS APP SHELL: only {len(body)} chars server-side; "
                "the real content loads client-side. Do NOT parse pages like "
                "this — find the app's underlying data endpoint.")
            continue
        samples.append(f"`{url}` (excerpt of {len(body)} chars):\n```\n{body[:4500]}\n```")

    prompt = (f"## Source profile (agent-discovered, URLs verified)\n\n"
              f"```json\n{json.dumps(profile, indent=1)}\n```\n\n"
              f"## Live document samples\n\n" + "\n\n".join(samples) +
              f"\n\n## Domain schema (schema.py, verbatim)\n\n"
              f"```python\n{schema_src}```\n\n" +
              CONTRACT.replace("{source_id}", f"{slug}-bos")
                      .replace("{schema_version}",
                               __import__("schema").SCHEMA_VERSION))
    template = family_template(slug, profile)
    if template:
        src_id, platform, code = template
        prompt += (f"\n\n## Proven extractor for another {platform} tenant "
                   f"({src_id})\n\nThis module already passed the gate for a "
                   "different deployment of the same platform. Adapt it to "
                   "THIS profile's tenant: the platform mechanics are proven; "
                   "deployment specifics (hosts, view/folder ids, body names, "
                   "rosters, date formats) come from the profile above.\n"
                   f"```python\n{code}\n```")
    return [{"role": "user", "content": prompt}], template


# Decision/tally language in meeting documents. Used only to pick excerpt
# windows for the repair loop — vote phrasing varies by jurisdiction ("Ayes:",
# "Yea: (7) Allen, ...", "carried by a vote of 5-2") and the synthesizer can
# only parse a dialect it has actually seen.
DECISION_RE = re.compile(
    r"(?i)(?:voting[^.\n]{0,30}tally|\byeas?\s*[:(]|\bnays?\s*[:(]|"
    r"\bayes?\s*[:(]|motion\s+(?:carried|passed|failed)|carried\s+by|"
    r"roll[- ]?call\s+vote|vote\s+of\s+\d+\s*(?:to|[-–])\s*\d+"
    r"|\b\d{1,2}\s*[-–]\s*\d{1,2}\b)")


def _decision_excerpts(body, limit=3):
    out, last_end = [], -10**9
    for m in DECISION_RE.finditer(body):
        if m.start() < last_end:  # skip overlapping windows
            continue
        w = " ".join(body[max(0, m.start() - 120):m.start() + 200].split())
        out.append(w)
        last_end = m.start() + 200
        if len(out) >= limit:
            break
    return out


def fetch_trace(out_path, cache):
    """Compact per-attempt trace of what the candidate fetched — URL, size,
    response head, and (for long text documents) excerpt windows around
    decision language — so the repair loop can diagnose from actual responses
    (e.g. every 'meeting' is a future calendar entry, a portal URL returns a
    JS-app shell instead of the document, or votes are recorded in a local
    tally dialect the parser doesn't speak)."""
    trace_path = pathlib.Path(str(out_path) + ".trace")
    if not trace_path.exists():
        return None
    lines, seen, excerpts_left = [], set(), 8
    for url in json.loads(trace_path.read_text()):
        if url in seen:
            continue
        seen.add(url)
        raw = cache.get(url, "")
        is_text = isinstance(raw, str)
        body = raw if is_text else json.dumps(raw)
        head = " ".join(body[:400].split())[:220]
        # JSON shape summary — pagination affordances (@odata.nextLink etc.)
        # live at the tail of a payload, which the head excerpt never shows.
        shape = ""
        if isinstance(raw, dict):
            ks = [f"{k}[{len(v)}]" if isinstance(v, list) else k
                  for k, v in list(raw.items())[:12]]
            shape = f" · JSON object, keys: {', '.join(ks)}"
        elif isinstance(raw, list):
            shape = f" · JSON array, {len(raw)} rows"
        lines.append(f"- {url} · {len(body)} chars{shape} · {head!r}")
        if is_text and len(body) > 5000 and excerpts_left > 0:
            windows = _decision_excerpts(body, limit=min(3, excerpts_left))
            excerpts_left -= len(windows)
            for w in windows:
                lines.append(f"    decision-language excerpt: {w!r}")
        if len(lines) >= 60:
            lines.append(f"- … ({len(seen)} more URLs omitted)")
            break
    trace_path.unlink()
    return lines


def _norm_ws(s):
    return " ".join(s.split())


def evidence_floor(records, cache):
    """Every evidence quote must literally appear in a fetched document —
    the anti-fabrication check the Fairfax phantom votes taught us."""
    findings = []
    haystack = [_norm_ws(v if isinstance(v, str) else json.dumps(v))
                for v in cache.values()]
    for ve in records.get("vote_events", []):
        ev = ve.get("evidence") if isinstance(ve, dict) else None
        if not (isinstance(ev, dict) and isinstance(ev.get("quote"), str)):
            continue
        quote = _norm_ws(ev["quote"])
        if quote and not any(quote in h for h in haystack):
            findings.append(
                {"layer": "gate", "check": "evidence_not_in_source",
                 "ref": ve.get("vote_id", "?"),
                 "msg": f"evidence quote not found verbatim in any fetched "
                        f"document: \"{quote[:90]}…\" — copy the passage "
                        "exactly as it appears, do not paraphrase"})
    return findings


# Explicit tallies as documents state them ("Affirmative: 45", "Yea: (7)",
# "Abstentions: 2"). When a vote's evidence quote carries one, the parsed
# counts must reproduce it — a mismatch means the positions parser is
# losing or merging members, which no amount of internal consistency shows.
QUOTE_TALLY_RES = (
    (re.compile(r"(?i)\baffirmative\s*[:\-–]\s*(\d+)"), "aye"),
    (re.compile(r"(?i)\bnegative\s*[:\-–]\s*(\d+)"), "no"),
    (re.compile(r"(?i)\byeas?\s*[:(]+\s*(\d+)"), "aye"),
    (re.compile(r"(?i)\bnays?\s*[:(]+\s*(\d+)"), "no"),
    (re.compile(r"(?i)\babstentions?\s*[:\-–(]+\s*(\d+)"), "abstain"),
)


def quote_tally_floor(records):
    findings = []
    for ve in records.get("vote_events", []):
        quote = (ve.get("evidence") or {}).get("quote") or ""
        if not quote:
            continue
        counts = ve.get("counts") or {}
        for rx, key in QUOTE_TALLY_RES:
            m = rx.search(quote)
            if m and counts.get(key, 0) != int(m.group(1)):
                findings.append(
                    {"layer": "gate", "check": "quote_tally_mismatch",
                     "ref": ve.get("vote_id", "?"),
                     "msg": f"evidence quote states {key}={m.group(1)} but "
                            f"counts.{key}={counts.get(key, 0)} — the positions "
                            "parser is losing or merging members (compound "
                            "names, 'X and Y' pairs, accented names); counts "
                            "must reproduce the document's stated tally"})
                break  # one finding per vote
    return findings


_NOT_A_NAME = re.compile(r"[\n\t]|\d|(?i:attachment|exhibit|committee report"
                         r"|resolution|ordinance|a motion|meeting|agenda)")


def member_name_floor(records):
    """A position's member must look like a person's name. Multi-line blobs,
    digits, or document vocabulary mean the roll-call parser overran the name
    list into the surrounding text — and a garbage 'member' can still satisfy
    the stated tally, so the count floors never see it."""
    findings, seen = [], set()
    names = [(p.get("member") or "", ve.get("vote_id", "?"))
             for ve in records.get("vote_events", [])
             for p in ve.get("positions", [])]
    names += [(m.get("name") or "", "members") for m in records.get("members", [])]
    for n, ref in names:
        if n in seen:
            continue
        if len(n) > 60 or _NOT_A_NAME.search(n):
            seen.add(n)
            findings.append(
                {"layer": "gate", "check": "malformed_member_name", "ref": ref,
                 "msg": f"member {n[:70]!r} is not a person's name — the "
                        "roll-call parser ran past the end of the name list "
                        "into surrounding document text; bound each roll-call "
                        "segment before splitting names"})
            if len(findings) >= 8:
                break
    return findings


def duplicate_vote_floor(records):
    """One roll call, one record: two vote_events in the same meeting citing
    the IDENTICAL evidence quote are the same vote harvested from two document
    sections (e.g. committee report + stated meeting recap)."""
    findings, seen = [], {}
    for ve in records.get("vote_events", []):
        quote = _norm_ws((ve.get("evidence") or {}).get("quote") or "")
        if not quote:
            continue
        key = (ve.get("meeting_id"), quote)
        if key in seen:
            findings.append(
                {"layer": "gate", "check": "duplicate_vote",
                 "ref": f"{seen[key]}, {ve.get('vote_id', '?')}",
                 "msg": "two vote_events cite the identical evidence quote — "
                        "either the same roll call was emitted twice (it "
                        "appears in more than one document section; dedupe), "
                        "or two distinct votes were quoted by their shared "
                        "tally block alone (each quote must start at ITS "
                        "motion line, which makes quotes distinct)"})
        else:
            seen[key] = ve.get("vote_id", "?")
    return findings[:8]


_ATTACHMENT_VOCAB = re.compile(
    r"(?i)fiscal impact statement|committee report|hearing (?:testimony"
    r"|transcript)|stated meeting|exhibit [a-z]\b|memorandum in support"
    r"|\battachments?:")


_TITLE_STOP = re.compile(
    r"(?i)\b(approve[ds]?|adopt(?:ed)?|accept(?:ed)?|amend(?:ed)?|defer(?:red)?"
    r"|motion|res(?:olution)?|int(?:roduction)?|ord(?:inance)?|no|m|lu|slr"
    r"|item)\b|[\d\-–/.,:;()#]+")


def _title_has_subject(title):
    residue = _TITLE_STOP.sub(" ", title or "")
    return sum(1 for w in residue.split() if len(w) > 2) >= 2


def vague_title_floor(records):
    """Aggregate check: item titles must carry the item's SUBJECT. Two ways
    to fail it: attachment-list scrapes ('Res. No. 544, Fiscal Impact
    Statement, Committee Report...') and subject-free stubs ('Approve Res
    0529-2026') — stripping action verbs, record types, and numbers must
    leave actual words about WHAT is decided."""
    items = records.get("agenda_items", [])
    if len(items) < 5:
        return []
    vague = [i for i in items
             if _ATTACHMENT_VOCAB.search(i.get("title") or "")
             or not _title_has_subject(i.get("title"))]
    if len(vague) * 3 < len(items):  # tolerate occasional odd items
        return []
    return [{"layer": "gate", "check": "vague_titles", "ref": "run",
             "msg": f"{len(vague)}/{len(items)} agenda_item titles carry no "
                    "subject: attachment lists or verb+file-number stubs "
                    "('Approve Res 0529-2026'). The meeting documents print "
                    "each item's official legislative title (e.g. 'A Local "
                    "Law to amend..., in relation to ...') near its file "
                    "number — pair them up; a reader must learn WHAT each "
                    "item does from the title alone"}]


def floors(records, cache=None):
    import datetime
    findings = []
    findings += quote_tally_floor(records)
    findings += member_name_floor(records)
    findings += duplicate_vote_floor(records)
    findings += vague_title_floor(records)
    dates = sorted(m.get("date", "") for m in records.get("meetings", []))
    if dates and dates[-1] < (datetime.date.today()
                              - datetime.timedelta(days=120)).isoformat():
        findings.append({"layer": "gate", "check": "stale_meetings", "ref": "run",
                         "msg": f"newest meeting is {dates[-1]} — enumerate the "
                                "CURRENT meetings, not an older archive section"})
    if len(records.get("meetings", [])) < 2:
        findings.append({"layer": "gate", "check": "too_few_meetings", "ref": "run",
                         "msg": f"only {len(records.get('meetings', []))} meetings extracted"})
    if cache is not None:
        # Duplicate-document floor: clerks sometimes attach one meeting's
        # minutes to several events. Byte-identical documents backing
        # different meetings means duplicated records under a wrong date.
        import hashlib
        by_hash = {}
        for m in records.get("meetings", []):
            url = m.get("data_source_url") or m.get("source_url")
            body = cache.get(url)
            if body is None:
                continue
            body = body if isinstance(body, str) else json.dumps(body)
            h = hashlib.md5(body.encode()).hexdigest()
            by_hash.setdefault(h, []).append((m.get("meeting_id", "?"), url))
        for pairs in by_hash.values():
            ids = [mid for mid, _ in pairs]
            # one listing endpoint legitimately backs many meetings; the
            # clerk-error signature is DIFFERENT urls serving identical bytes
            if len(ids) > 1 and len({u for _, u in pairs}) > 1:
                findings.append(
                    {"layer": "gate", "check": "duplicate_document",
                     "ref": ", ".join(ids),
                     "msg": "these meetings were parsed from byte-identical "
                            "documents — the source attached the same file to "
                            "multiple events. Verify the date stated INSIDE "
                            "the document against the meeting's date; drop "
                            "mismatched meetings and keep enumerating until "
                            "you have enough correctly-dated ones"})
    if len(records.get("vote_events", [])) < 8:
        findings.append({"layer": "gate", "check": "too_few_votes", "ref": "run",
                         "msg": f"only {len(records.get('vote_events', []))} vote events"})
    votes = [v for v in records.get("vote_events", []) if isinstance(v, dict)]
    if len(votes) >= 20:
        positions = [p.get("position") for v in votes
                     for p in v.get("positions", []) if isinstance(p, dict)]
        if positions and all(pos == "aye" for pos in positions):
            findings.append(
                {"layer": "gate", "check": "uniform_votes", "ref": "run",
                 "msg": f"all {len(votes)} vote events are full-roster unanimous "
                        "ayes — narrative minutes record dissent ('voting NAY'), "
                        "abstentions, and absences; parse each motion's actual "
                        "vote language instead of defaulting the roster to aye, "
                        "and emit NO vote_event where no motion or tally is "
                        "explicitly recorded"})
    for m in records.get("meetings", []):
        if not m.get("attendance"):
            findings.append({"layer": "gate", "check": "no_attendance",
                             "ref": m.get("meeting_id", "?"),
                             "msg": "meeting has no derived attendance"})
    if cache is not None:
        findings += evidence_floor(records, cache)
    return findings


def onboard(slug, meetings=3, attempts=4, log=print, prog=None, resume=False):
    """Full profile-driven onboarding. Returns source_id on success, None on
    failure. Callable from the search pipeline or the CLI.

    resume=True picks up a previously failed onboard: the newest saved
    attempt is replayed against the cache (no LLM cost) and its gate
    findings + fetch trace seed the conversation, so new attempts continue
    the repair instead of re-deriving everything from scratch."""
    profile = json.loads(
        (FOUNDRY / "data" / "discovery" / f"{slug}_profile.json").read_text())
    source_id = f"{slug}-bos"
    cache_path = FOUNDRY / "data" / "onboard" / f"{slug}_http_cache.json"
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    rt = sandbox2.Runtime(json.loads(cache_path.read_text())
                          if cache_path.exists() else {})
    messages, template = build_messages(profile, slug, rt)
    if template:
        log(f"family reuse: adapting proven {template[1]} extractor "
            f"from {template[0]}")
    cache_path.write_text(json.dumps(rt.cache))

    artifacts = FOUNDRY / "extractors" / source_id
    artifacts.mkdir(parents=True, exist_ok=True)
    out_path = FOUNDRY / "data" / "onboard" / f"{slug}_out.json"
    usages, records, passed = [], None, False
    t0 = time.time()

    def gate(records, error):
        cache = json.loads(cache_path.read_text()) if cache_path.exists() else {}
        findings = []
        if error is None:
            findings = harness.run_all(records)  # no oracle: structural+consistency
            if not any(f["check"] == "malformed_root" for f in findings):
                findings += floors(records, cache)
        if error is None and not findings:
            # mechanical gate clear — last rung is the semantic reader check
            try:
                import skeptic
                findings = skeptic.review(records, source_id, log=log)
            except Exception as exc:  # fail open: mechanical gate stands alone
                log(f"  skeptic unavailable ({str(exc)[:80]}) — passing on "
                    "mechanical gate only")
        return findings, cache

    start, artifact = 1, None
    if resume:
        prior = sorted(artifacts.glob("v1_attempt*.py"),
                       key=lambda p: int(re.search(r"(\d+)$", p.stem).group(1)))
        if prior:
            start = int(re.search(r"(\d+)$", prior[-1].stem).group(1)) + 1
            # Seed with the last TWO attempts (each replayed for fresh
            # findings). Successive repairs can oscillate — attempt N fixes
            # what N-1 broke and re-breaks what N-1 fixed; seeing both codes
            # with both finding sets lets the model merge the fixes instead
            # of flip-flopping.
            for artifact in prior[-2:]:
                log(f"resume: replaying {artifact.name} against the cache")
                records, error = sandbox2.run_artifact(
                    artifact, [meetings], out_path, cache_path)
                findings, cache = gate(records, error)
                if error is None and not findings:
                    passed = True
                    log(f"  {artifact.name} passes the gate as-is")
                    break
                log(f"  {len(findings)} findings — seeding the repair conversation")
                messages.append({"role": "assistant",
                                 "content": f"```python\n{artifact.read_text()}\n```"})
                messages.append(synthesize.feedback_message(
                    error, findings, fetch_trace=fetch_trace(out_path, cache)))

    for attempt in [] if passed else range(start, start + attempts):
        if prog:
            prog((attempt - start) / attempts, f"synthesizing extractor (attempt {attempt})")
        log(f"attempt {attempt}: synthesizing with {synthesize.MODEL}...")
        try:
            code, assistant_content, usage = synthesize.generate(messages)
        except RuntimeError as exc:
            log(f"  synthesis failed: {str(exc)[:140]} — fresh attempt")
            continue
        usages.append(usage)
        artifact = artifacts / f"v1_attempt{attempt}.py"
        artifact.write_text(code)
        log(f"  artifact: {artifact.name} ({len(code.splitlines())} lines)")
        if prog:
            prog((attempt - start + 0.5) / attempts, f"running candidate (attempt {attempt})")
        records, error = sandbox2.run_artifact(
            artifact, [meetings], out_path, cache_path)
        findings, cache = gate(records, error)
        if error is not None:
            log("  execution failed: " + error.strip().splitlines()[-1][:120])
        if error is None and not findings:
            passed = True
            log("  GATE PASSED (structural + consistency + floors; NO oracle)")
            break
        log(f"  gate failed: {len(findings)} findings")
        for f in findings[:6]:
            log(f"    [{f['layer']}/{f['check']}] {f['ref']}: {f['msg'][:120]}")
        messages.append({"role": "assistant", "content": assistant_content})
        messages.append(synthesize.feedback_message(
            error, findings, fetch_trace=fetch_trace(out_path, cache)))

    log(f"onboarding cost: {len(usages)} attempts, "
        f"${synthesize.cost_usd(usages):.2f}, {(time.time() - t0) / 60:.1f} min")
    if not passed:
        log("verdict: FAILED — no candidate cleared the gate")
        return None

    note = ("synthesized from agent-discovered profile; single-source, no "
            "oracle wired — ingested, never certifiable as-is")
    for rtype in ("meetings", "agenda_items", "vote_events"):
        for rec in records[rtype]:
            rec["certification"] = {"status": "quarantined", "method": None,
                                    "note": note}
    merge(source_id, records, 0)
    store_path = STORE / f"{source_id}.json"
    store = json.loads(store_path.read_text())
    store["meta"] = {
        "title": profile.get("jurisdiction", slug.title()),
        "sub": f"{profile.get('primary_source', {}).get('system', '')[:80]} · "
               "auto-onboarded, single-source",
        "artifact": str(artifact.relative_to(FOUNDRY)),
        "platform": platform_of(profile),
        "meetings_arg": meetings}
    store_path.write_text(json.dumps(store, indent=1))
    log("spot-check sample:")
    for ve in records["vote_events"][:4]:
        exc = [f"{p['member']}:{p['position']}" for p in ve["positions"]
               if p["position"] != "aye"]
        log(f"  {ve['vote_id'][:46]}: {ve['counts']} -> {ve['result']} "
            f"| {', '.join(exc) or 'unanimous'}")
    return source_id


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("slug")
    parser.add_argument("--meetings", type=int, default=3)
    parser.add_argument("--attempts", type=int, default=4)
    parser.add_argument("--resume", action="store_true",
                        help="continue repairing from the newest saved attempt")
    args = parser.parse_args()
    return 0 if onboard(args.slug, args.meetings, args.attempts,
                        resume=args.resume) else 1


if __name__ == "__main__":
    sys.exit(main())
