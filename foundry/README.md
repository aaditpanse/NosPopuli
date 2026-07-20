# Foundry — self-building municipal-data pipelines

Foundry turns the manual work of "figure out where a jurisdiction publishes
its records, write a scraper, keep it honest" into a repeatable pipeline: an
LLM **discovers** the source, **synthesizes** a deterministic extractor,
a **gate** validates the output, an **oracle** certifies it against a second
source, and a deterministic **refresh** keeps it current. The durable asset
is the schema and the gate; extractors are disposable. It began as the M0–M4
prototype of `docs/spec_self_building_pipelines.md` (Pittsburgh + LA, origin
notes at the bottom) and has since grown into three data domains served by a
lab console at **`/foundry`**.

The one rule everything obeys: **certified and uncertified data are never
interchangeable.** A record that no independent source affirms is *ingested,
never published* — and the UI always says so. Missing data is framed as our
gap, explicitly, not the jurisdiction's.

## Three domains

| domain | record types (schema.py) | what it answers |
|---|---|---|
| **Legislation** | `meeting`, `agenda_item`, `vote_event`, `member` | what a governing body decided, and how each member voted |
| **Public works** | `capital_project` | what's being built — budget, timeline, funding source, location |
| **Elections** | `contest` (+ candidates) | who was elected — mayor & school board, with turnout signal |

Together they close a civic loop: the *election* that seated an official → their
*votes* → what's *built* in their district. Domain schema is **v1.5**.

## The pipeline (how a source becomes data)

```
discover  →  synthesize  →  gate  →  certify  →  refresh / deepen  →  enrich
(Sonnet)     (Opus)         (det.)   (oracle)     (deterministic)     (Haiku)
```

- **discover.py** — a cheap model (Sonnet) runs the probe loop a human
  otherwise runs by hand: fetch real pages, walk the platform playbook, report
  a source profile. Budget-enforced; never fights anti-bot (records and pivots).
- **run_onboard.py** — hands Opus the profile + live samples + `schema.py` +
  the contract, loops synthesize → run-in-sandbox → gate → repair. The **gate**
  is the harness (structural + consistency) plus **floors** (stale/too-few,
  duplicate-document, quote-tally reconciliation, member-name sanity,
  one-roll-call-one-vote, vague-title, evidence-grep) plus **skeptic.py**
  (a Sonnet pass that reads sample rows as a citizen). Bad extractors fail the
  gate; they never land wrong data.
- **certify** (`certify.py`, `run_oracle.py`) — a second, independently
  authored source (the clerk's minutes, a different vendor's vote log)
  reconciles the records. Only cross-affirmed records become *certified*;
  disagreements are quarantined with adjudication notes.
- **refresh.py / deepen.py** — deterministic upkeep, **$0, no LLM**: re-run the
  promoted extractor against the live source, re-gate, merge only what passes
  (gate findings mean *drift* — logged, never merged). `deepen.py` widens the
  window to backfill history, merging only new meeting ids (never touches
  certified records). A stalled source may escalate to **one** budget-gated
  synthesis repair per cycle (`FOUNDRY_AUTO_REPAIR=off` to disable).
- **enrichment** (`summarize_items.py`, `meeting_digests.py`, `upcoming.py`) —
  the plain-English layer (Haiku, pennies): item summaries, per-meeting
  digests, next-meeting schedules. Machine-derived, advisory, never certified.

**budget.py** is the spend governor: a JSONL ledger + a daily cap
(`FOUNDRY_DAILY_BUDGET` in `.env`). Synthesis and discovery check it before
starting; enrichment is recorded but never blocked. Known gap: the cap is
checked *before* each attempt, so one large attempt can overshoot it — a
per-attempt ceiling is owed.

## Sources

**Legislation** (`data/store/<id>.json`):

| source | platform | certified? |
|---|---|---|
| Pittsburgh City Council | Legistar API + clerk minutes | yes (oracle wired) |
| Los Angeles City Council | PrimeGov Journal + City Clerk CFMS | yes (strongest oracle) |
| Loudoun County BOS | Laserfiche Action Reports | ingest-only |
| Fairfax / Prince William / Stafford County BOS | CivicClerk / Granicus | quarantined (single-source) |
| New York City Council | Legistar (InSite calendar scrape) | quarantined |

**Public works** (CIP): **Fairfax** (`cip_extractor.py`, the hand-built
reference — 273 projects, ~$15.4B) and **Prince William** (synthesized by
`cip_onboard.py` — 43 projects, 5/6 functional categories). Geocoded to map
pins (`geocode_projects.py`, OSM Nominatim), enriched with status
(completed/recurring/planned), work type (renovation/new/addition), bond year,
and estimated completion.

**Elections**: `local-elections.json` — the American Local Government Elections
Database (national, **7,021 mayor + school-board contests, 721 jurisdictions,
1989–2021**, CC-BY) — plus `va-elections.json` — Virginia ENR (**2023 local
winners**, board of supervisors + school board, all VA counties incl. Stafford).

## The console (`/foundry`)

Read-only lab viewer (`frontend/foundry.html` + `foundry.js`), served by
`api.py` at `/api/foundry/data`. Navigation is a **US coverage map** (d3-geo +
vendored Census TopoJSON): drill a state → click any county. A covered county
opens a **dashboard** — meetings/legislation in the main column, a
public-works **map** in the top-right, an **election tracker** (next election +
most recent result) bottom-right; each panel summarizes, with "view all →"
opening a modal. Clicking an *un-onboarded* county opens an honest empty
dashboard centered on it. Elections are matched to any of the 721 jurisdictions
by county name, not a hardcoded list.

## Run it

```
cd foundry
../.venv/bin/python run_onboard.py <slug> --meetings 3       # onboard a meetings source (Opus)
../.venv/bin/python cip_discover.py "<County, ST>" <slug>    # find a CIP (Sonnet + web)
../.venv/bin/python cip_onboard.py data/discovery/<slug>-cip_profile.json   # synthesize a CIP extractor
../.venv/bin/python geocode_projects.py --source <slug>-cip  # map the projects ($0)
../.venv/bin/python ingest_ledb.py --commit                  # national historical elections ($0)
../.venv/bin/python ingest_va_enr.py --commit                # VA 2023 recent elections ($0)
../.venv/bin/python refresh.py --all                         # deterministic refresh + deepen + enrich
python budget.py                                             # today's / this month's spend
```

Needs `pdftotext` (poppler) on PATH, the project venv, and `ANTHROPIC_API_KEY`
in the repo `.env` for the LLM paths (synthesis/discovery/enrichment). The
deterministic paths (refresh, deepen, geocoding, elections ingest) cost $0.

## Files

| file | role |
|---|---|
| `schema.py` | domain schema v1.5: meeting/agenda_item/vote_event/member, capital_project, contest |
| `discover.py` | source-discovery agent (Sonnet + cached fetch tools) |
| `run_onboard.py` | generic meetings onboarding: contract, floors, gate, resume/repair |
| `synthesize.py` · `sandbox2.py` | Opus extractor synthesis · sandbox runtime (fetch_json/fetch_text, PDF→text) |
| `harness.py` · `certify.py` · `run_oracle.py` | the meter (structural/consistency) · quarantine→certify · re-certify |
| `skeptic.py` | semantic "read it as a citizen" gate pass (Sonnet, fail-open) |
| `refresh.py` · `deepen.py` | deterministic refresh + drift detection · $0 history deepening |
| `budget.py` | spend ledger + daily cap |
| `cip_extractor.py` | Fairfax CIP reference extractor (pdftotext table parse, subtotal reconciliation) |
| `cip_discover.py` · `cip_onboard.py` | autonomous CIP discovery · synthesis loop |
| `geocode_projects.py` | project → lat/lon via OSM Nominatim, per-county bbox |
| `ingest_ledb.py` · `ingest_va_enr.py` | national historical elections (CC-BY) · VA recent (ENR CDN) |
| `summarize_items.py` · `meeting_digests.py` · `upcoming.py` | Haiku enrichment: summaries · digests · schedules |
| `backfill.py` | curated/deep-window historical merges (Pittsburgh/LA/Loudoun) |
| `legistar_*`, `minutes_extractor.py`, `loudoun_extractor.py`, `la_oracle.py` | hand-built reference extractors/oracles |
| `run_m0..m4.py`, `inject.py`, `sandbox.py`, `repair_la.py` | the origin M0–M4 milestone harness |
| `data/store/` | the served stores (one per source) + enrichment sidecars |
| `extractors/<source>/` | synthesized candidate artifacts, one file per attempt |

## Honest caveats

- **Most data is single-source, quarantined.** Only Pittsburgh and LA have a
  wired oracle. CIP and elections have *no* second source yet — the certified
  canvass (elections) and meeting-vote reconciliation (CIP) are the natural
  oracles, not yet built.
- **Elections coverage = the dataset's coverage:** >50k-population places,
  1989–2021 for the national layer; recent data is Virginia-only (Clarity, the
  national vendor family, turned anti-bot, so recent results are state-by-state).
  Turnout shown is ballots-vs-population, a proxy, not a registration
  denominator.
- **CIP is heterogeneous.** No platform families like Legistar — each county's
  budget document differs, so extractors are synthesized per county (Prince
  William landed 5/6 categories). Enrichment tags (work type, bond year) rely
  on Fairfax/PW phrasing and degrade elsewhere.
- **NYC can't backfill past ~June** — its Legistar calendar hides history behind
  Telerik postback pagination the GET-only sandbox can't reach; the fix is its
  Socrata second source, not built.
- **Refresh isn't automated on prod.** The store is committed static files
  served read-only; refresh runs offline and is pushed. The systemd timer is
  local-only (no systemd on Railway/containers); the UI's next-meeting guard
  makes any staleness *honest* rather than wrong.

## Provenance & licensing

- Election data: American Local Government Elections Database (de
  Benedictis-Kessner, Lee, Velez & Warshaw, 2023; **CC-BY 4.0**;
  OSF `10.17605/OSF.IO/MV5E6`) and Virginia Dept. of Elections (public ENR CDN).
- Map boundaries: US Census cartographic files via `us-atlas` TopoJSON and a
  119th-Congress conversion; rendered with vendored d3-geo / topojson-client /
  Leaflet + OpenStreetMap tiles.
- CIP + meetings data: each jurisdiction's own published records; stores carry
  source URLs and per-record provenance.

---

## Origin: the M0–M4 milestones (2026-07-09)

Foundry began by proving the meter on one real source pair before letting an
LLM near it. Condensed results (full detail in git history):

- **M0 (the meter):** Pittsburgh two-source data reconciled with 0 findings
  (48/48 votes agree API-vs-minutes); oracle recall **50/50 (100%)** across 10
  corruption types × 5 seeds. The three *silent* corruptions were caught
  **only** by the cross-source layer — the spec's central claim, reproduced.
- **M1 (synthesis):** Opus wrote a passing Pittsburgh extractor on attempt 2/4
  from profile + schema + contract alone. ~$0.45, 2.6 min, 0 human-minutes.
- **M2 (break/repair):** 7 simulated source changes — detection 4/4, recovery
  4/4 zero-touch, false alarms 0/2. Found a real bug in the harness itself (a
  malformed candidate crashed the validator; shape guards added).
- **M3 (cold generalization):** Los Angeles onboarded cold (PrimeGov Journal
  PDF vs City Clerk CFMS — a genuinely independent oracle). Caught a real
  transposition error *in LA's official records* (Journal vs CFMS disagree on
  file 17-0714-S1); ingested, never certified. Schema v1 needed exactly one
  change.
- **M4 (fleet):** deterministic quarantine→certify across both sources, 87%
  certifiable coverage; the first real (non-simulated) repair fired from a live
  validation failure.

The through-line from M0 to the current three domains is the same: **build the
gate once, on the hardest source; make everything else fail safe into it.**
