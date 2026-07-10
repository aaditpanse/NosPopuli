# Foundry — M0–M4 prototype

Lab prototype of `docs/spec_self_building_pipelines.md`, milestones **M0 (the
meter)** through **M3 (generalization)**. Not deployed — the validation
harness is proven against one real source pair first, then an LLM
synthesizes the extractor, the source is deliberately broken to measure the
repair loop, and finally a structurally different city is onboarded cold.

Two sources so far:
- **Pittsburgh City Council** (Legistar Web API + clerk's minutes PDF) — M0–M2
- **Los Angeles City Council** (PrimeGov hidden JSON + Journal PDF, reconciled
  against the independent City Clerk CFMS) — M3

**Source pair:** Pittsburgh City Council.
- Records: Legistar Web API (`webapi.legistar.com/v1/pittsburgh`) via the
  hand-written `legistar_extractor.py` (extractor v1 — the artifact M1
  synthesis would generate and M2 repair would rewrite).
- Second source: the clerk's published Meeting Minutes PDF, parsed by
  `minutes_extractor.py` into independent assertions.

## Run it

```
cd foundry
../venv/bin/python run_m0.py snapshot   # fetch golden set (network; ~35s)
../venv/bin/python run_m0.py run        # M0 experiment (offline)
../venv/bin/python run_m1.py            # M1 synthesis (calls the Anthropic API)
../venv/bin/python run_m2.py            # M2 break/detect/repair (calls the Anthropic API)
../venv/bin/python run_m3.py snapshot   # M3 freeze LA source + CFMS oracle (network)
../venv/bin/python run_m3.py run        # M3 cold onboarding (calls the Anthropic API)
../venv/bin/python run_m4.py            # M4 fleet refresh + certify (network, no LLM)
../venv/bin/python repair_la.py         # rerun of the first real repair (calls the API)
```

Needs `pdftotext` (poppler) on PATH, `requests` from the project venv, and —
for M1 only — `ANTHROPIC_API_KEY` in the repo `.env`.

## Files

| file | role (spec module) |
|---|---|
| `schema.py` | domain schema v1: meeting / agenda_item / vote_event / member (2) |
| `legistar_extractor.py` | deterministic extractor artifact, source profile in docstring (1, 3, 4) |
| `minutes_extractor.py` | second-source assertions from the minutes PDF (5, layer 3 input) |
| `harness.py` | the meter: structural / consistency / reconcile layers (5) |
| `inject.py` | planted-error injector, 10 corruption types (5) |
| `run_m0.py` | snapshot + experiment + certification pass (7) |
| `synthesize.py` | LLM extractor synthesis: source profile + schema + contract -> code (3) |
| `sandbox.py` | candidate runtime: injected `fetch_json`, frozen HTTP cache, subprocess isolation (3, 4) |
| `run_m1.py` | synthesis attempt loop, golden gate, onboarding-cost metrics (3, 6) |
| `run_m2.py` | simulated source breaks, detection + repair loop, recovery/false-alarm metrics (6) |
| `sandbox2.py` | runtime v2: source-agnostic absolute URLs + `fetch_text` PDF rung (3, 4) |
| `la_oracle.py` | hand-built second-source oracle: City Clerk CFMS vote blocks (5, layer 3) |
| `run_m3.py` | cold onboarding of LA: snapshot, synthesis loop, harness-only gate, adjudication (3, 5, 7) |
| `certify.py` | shared quarantine -> certified pass over any source's records (7, 8) |
| `run_m4.py` | fleet refresh: discover, extract, validate, reconcile, certify, export (4, 5, 7) |
| `repair_la.py` | first real repair loop: all-zero journal vote blocks (6) |
| `data/m4/` | refresh caches, certified exports per source, fleet report |
| `extractors/pittsburgh-legistar/` | synthesized candidate artifacts, one file per attempt |
| `golden/pittsburgh/` | frozen golden set: records, assertions, minutes PDFs, HTTP cache |
| `data/quarantine.json` | experiment output: every record with certification status (7, 8) |

## Results (2026-07-09, 3 meetings: 98 items, 48 recorded votes)

- Fresh two-source data reconciled with **0 findings** — 48/48 recorded
  votes agree member-by-member between API and minutes, so the false-alarm
  baseline is zero.
- **Oracle recall 50/50 (100%)** across 10 corruption types × 5 seeds, each
  caught at (or above) its expected layer.
- The spec's central claim reproduced empirically: the three *silent*
  corruptions (`flip_vote_silent`, `shift_vote_column`, `date_drift`) are
  internally consistent and structurally perfect — **only the cross-source
  layer caught them**. Internal checks caught the clumsy variants; formats
  caught the malformed ones.
- Certifiable coverage: vote events 48/48 (100%); agenda items 55/98 (56%).
  The quarantined 43 are mostly "Read and referred" items with no final
  action — nothing affirms them, so they stay unpublished. That gap being
  visible is the product working, not a bug.

## M1 results (2026-07-09, claude-opus-4-8)

The model saw the source profile (real sample API responses), the schema
source, and the artifact contract — never extractor v1's code. The gate was
the full M0 harness plus exact golden-set reproduction on load-bearing
fields.

- **Passed on attempt 2 of 4.** Attempt 1 was harness-clean but named the
  member id field `legistar_person_id` instead of `person_id` — the schema
  only requires `name`, so only the golden-set comparison caught it; one
  feedback round (the field-level diff) fixed exactly that.
- **Onboarding cost: ~$0.45 in LLM tokens (24.7K in / 13.4K out), 2.6 min
  wall time, 0 human-minutes** — no human edit inside the loop.
- The synthesized artifact is genuinely deterministic: `re`/`json`/`hashlib`
  imports only, all I/O through the injected `fetch_json`, and a broader
  position-normalization map than the hand-written v1.
- Per the spec, the passing candidate is **staged, not promoted** — votes,
  dates, and identities are load-bearing fields, so replacing v1 goes through
  the human review gate. The artifacts sit in `extractors/pittsburgh-legistar/`
  as diffable files.
- One measurement caveat: the golden set was produced by v1, so "reproduce
  the golden set" partly encodes v1's conventions (id formats, which rows are
  records). Those conventions are spelled out in the contract as target-schema
  documentation, which is fair — but a fully cold source (M3) won't have a
  prior extractor to anchor the golden set, and will need the golden set
  built from the harness + minutes instead.

## M2 results (2026-07-09, claude-opus-4-8)

Seven simulated source changes, applied to a copy of the frozen HTTP cache
with live fetches disabled (a simulated break must not silently heal from
the real source). The minutes assertions stayed untouched — the second
source didn't change, which is the realistic case and what keeps the oracle
valid during a break. Repair input per the spec: old extractor + fresh
samples from the changed source + failing evidence.

- **Detection 4/4, recovery 4/4 zero-touch, false alarms 0/2.** Field
  renames (`VotePersonName`, `EventItemMatterFile`), vote-vocabulary changes
  (Aye→Yes), and a date-format flip were all caught — every one by the
  reconcile layer among others — and repaired within the attempt budget
  (three needed 1 attempt, one needed 2). Benign changes (extra fields, key
  reordering) correctly produced no signal. Cost: ~$1.01, 5.2 min, 0
  human-minutes.
- **One break was absorbed outright:** the synthesized v2 extractor already
  normalized `Passed`/`Failed`, so `passed_flag_rename` produced correct
  output and correctly no alarm — robustness, not a miss (the golden check
  guarantees a silent-wrong-output can't be classified that way).
- **M2 found a real bug in the meter itself:** a malformed repair candidate
  (positions as lists, not objects) *crashed* the harness instead of being
  reported by it. Shape guards were added throughout (a validator must never
  crash on the data it validates) and M0 re-verified at 100% recall. This is
  the kind of hardening the milestone exists to force.
- Repaired artifacts are staged in `extractors/pittsburgh-legistar/`, one
  file per attempt, human review gate before promotion.

## M3 results (2026-07-09, claude-opus-4-8)

Cold onboarding of Los Angeles City Council: PrimeGov hidden-JSON meeting
list + Journal **PDF** (the rendered-document rung — items and votes exist
nowhere else structured), reconciled against the **City Clerk's CFMS**, a
genuinely independent system (different vendor), making it the strongest
oracle in the lab. No prior extractor exists, so no golden set: the gate is
the full harness + CFMS reconciliation + presence floors, and the passing
candidate's output becomes the golden set after human spot-check.

- **Passed: 2 meetings, 88 items, 89 vote events — all 89 independently
  asserted by CFMS.** 88/89 certified cross-source; 1 quarantined (below).
- **Onboarding cost:** two loop runs. Run 1 ($1.35, 4 attempts) failed and
  exposed a *contract* flaw — LA items can be voted twice (substitution
  question + substitute motion), which the contract forbade. Run 2 with the
  amended contract passed in 3 attempts ($1.03, 6.6 min). Total ≈ **$2.38
  LLM + ~15 human-minutes** of diagnosis/adjudication — vs $0.45 / 0
  human-minutes for Pittsburgh (M1). Cold, unmodeled source types cost more,
  mostly in *contract iteration*, not extraction difficulty.
- **The harness caught a real error in LA's official records.** On council
  file 17-0714-S1 (2026-06-16, an 11-4 vote), the Journal records
  McOsker=Aye / Soto-Martínez=Nay while CFMS records McOsker=NO /
  Soto-Martínez=YES — same tally, two members transposed between the city's
  two systems. The extractor matches the Journal exactly; the sources
  themselves disagree. Per the spec this vote is **ingested, never
  certified**: it is adjudicated in `run_m3.ADJUDICATED` and frozen with
  `certification.status = "quarantined"`. This is the product working —
  only cross-source reconciliation can catch this class of error.
- **Schema survival (the M3 question):** schema v1 needed exactly one
  change — `file_number` widened for LA's `NN-NNNN-Sn` format (now schema
  1.1). Everything else (positions vocabulary, attendance, vote shape)
  held. Name canonicalization in the harness had to grow (case, diacritics,
  zero-width characters in PDF text, generational suffixes) — meter
  hardening, not schema fragmentation.
- Runtime grew a second capability rung: `fetch_text` (PDF→text). The
  malformed-root crash class was also fixed in `harness.run_all` itself.

## M4 results (2026-07-09)

Full quarantine -> certify pipeline across both sources on fresh data
(including a brand-new Pittsburgh meeting, 2026-07-07, and two LA meetings
never seen before). Deterministic refresh, no LLM in the path.

- **Fleet certifiable coverage: 439/505 records (87%)** — Pittsburgh 144/205
  (70%; the gap is "read and referred" items with no final action for the
  minutes to affirm), LA 295/300 (98%).
- **The first refresh caught two real problems, one on each side of the
  trust boundary.** (1) My hand-written Pittsburgh minutes oracle grew a
  ghost member named "Council" when July's minutes wrapped name lists
  differently — 16 false disputes, fixed in the oracle, June parsing
  regression-verified. (2) The LA extractor emitted an empty vote_event for
  a public-hearing item whose journal line is `Ayes: (0); Nays: (0);
  Absent: (0)` — flagged as a repair-loop candidate by the harness, and
  **repaired by the first real (non-simulated) repair run**: 2 attempts,
  $0.30, human-reviewed diff (a zero-block guard and nothing else),
  promoted as LA extractor v3.
- **A second genuine LA source disagreement surfaced**: on 25-0916
  (2026-06-24) the Journal records John Lee as Absent (10-0-5), CFMS as NO
  (10-1-4). Adjudicated and quarantined alongside 17-0714-S1. A third
  quarantine (26-0824) is a CFMS coverage gap — the second source simply
  has no record yet; ingested, not certifiable.
- Certified exports land in `data/m4/certified_<source>.json`; the
  quarantined records stay in the full outputs with their adjudication
  notes attached.

## Discovery agent + Loudoun County, VA (2026-07-09)

`discover.py` automates spec module 1 (the last human-only module): a
cheaper model (claude-sonnet-4-6) runs the probe loop with polite cached
HTTP tools and finishes by reporting a source profile. First target:
Loudoun County BOS. Run 1 blew its budget without reporting ($3.40 — fixed
with hard budget enforcement + a wrap-up nudge); run 2 produced a correct
profile: eScribe (brand-new migration, committees only so far), Laserfiche
WebLink as the real record system (per-meeting folders with Agenda +
Action Report PDFs, **RSS feeds per folder** — folder 98907 → year → meeting),
Legistar/PrimeGov/AgendaCenter correctly written off. It missed the
Granicus tenant run 1 had found. Verdict: usable, cheaper than Fable-tier
probing, needs the budget rails it now has.

A lab spot-extraction (`data/loudoun_sample.json`, not a Foundry artifact)
pulled the last 3 Business Meetings' Action Reports: 46 recorded votes with
per-member positions derived from roster-minus-named-exceptions, 44/46
tally-consistent (2 flagged honestly). All records are **ingested, not
certified** — no second source wired yet (Minutes lag; Copy Testes exist).
Schema note: Loudoun has no LA/Pittsburgh-style file numbers at all —
`file_number` needs to become per-source-profile validation before Loudoun
can be a real Foundry source.

## Data expansion layers (2026-07-09)

Beyond the certified vote pipeline, four more layers now exist:

- **`backfill.py`** — deep-window historical runs merging by record id into
  `data/store/<source>.json` (idempotent; widen windows incrementally).
  First pass: **33 meetings, 721 items, 662 vote events, 849 records
  certified** across the three sources. Deep windows surfaced new triage
  (meetings without roll-call items in both Pittsburgh and LA's older
  formats; three new disputes) — all correctly quarantined, logged as
  repair-loop candidates.
- **`loudoun_extractor.py`** — Loudoun formalized as a deterministic
  hand-written artifact (Laserfiche RSS tree -> Action Report PDFs ->
  roster-derived per-member positions). 2026: 15 meetings, 261 votes, all
  ingest-only pending a second source; 18 tally-inconsistent motions
  flagged rather than guessed.
- **`mine_items.py`** — content layer: staff-report PDFs -> structured
  facts (summary, type, fiscal impact, dollar amounts, districts, parties)
  via claude-haiku-4-5 with structured outputs. 10 Loudoun items cost
  $0.05 and surfaced e.g. the $356.5M November 2026 bond referendum.
  Marked machine-derived, never certified.
- **`transcripts.py`** — video layer without ASR: Granicus `/JSON.php?clip_id=N`
  carries the full closed-caption track (7,316 lines for one Loudoun
  meeting) with agenda-index markers; segmented into per-item transcripts
  (77 segments / 29K words for clip 8208). Live-caption quality — context
  only, never certified, but includes signals like late arrivals the
  Action Report omits.
- **Family scale-out probe:** Alexandria, VA has a live Legistar Web API
  (Fairfax's is disabled) — a ready next tenant for the Pittsburgh-family
  extractor.

## Honest caveats

- The "second source" is the clerk's minutes PDF hosted by the same vendor
  (Legistar) as the API. It is a separately authored document, which is what
  makes reconciliation meaningful, but it is a weaker oracle than a truly
  independent publisher. The source profile records this.
- The golden set was cross-verified between the two sources automatically
  and spot-checked by hand on a sample (e.g. 2026-0595, 2026-06-30 meeting),
  not exhaustively hand-verified.
- `shift_vote_column` on a unanimous vote with no absences is a no-op and
  would go undetected — which is also semantically harmless. Worth keeping
  in mind when reading recall numbers.
- Detection on `truncation` relies on the prior run's row counts
  (`run_meta.json`); a truncated *first* run has no delta to compare.

## Next (per the spec's gates)

M4 — certification at fleet level: run the full quarantine → certify
pipeline across both sources on a schedule, measure certifiable coverage as
sources refresh, and wire the repair loop (M2) to fire from real validation
failures instead of simulated ones. Also worth doing: report the
17-0714-S1 Journal/CFMS discrepancy to the LA City Clerk — it's a real
error in one of their systems.
