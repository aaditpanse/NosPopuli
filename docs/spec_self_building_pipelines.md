# Self-Building Data Pipelines ("Foundry") — System Spec
**NosPopuli · Draft for review**

---

## What we're building

A system that **builds and maintains its own ingestion pipelines**. Given a data source (a city council portal, a county board site), Foundry discovers the source's structure, synthesizes a deterministic extractor against a target schema, validates the output against oracles, and repairs the extractor itself when the source changes — so a single person can maintain coverage that would otherwise require a team.

The purpose is to make NosPopuli's data layer bounded by what is **verifiable**, not by what is **maintainable**. That unlocks the domains no aggregator serves — above all local and municipal legislation — because the reason they're uncharted is maintenance cost, not difficulty. Foundry attacks the maintenance cost directly.

This is deliberately a **lab system first**. V1 is built and proven on a controlled set of sources and is **not deployed to users**. The charted domains keep their APIs (state → LegiScan, federal → Congress.gov); Foundry targets the gap.

---

## Core principles

> **1. Coverage is bounded by verifiability, not maintainability.** The system never publishes a record it cannot certify. "We can read it" and "we can swear it's true" are different states, tracked separately, and only the second is publishable.

> **2. The LLM writes and repairs the code; it never runs in the hot path.** Production is cheap deterministic extractor code. An LLM is invoked only to *synthesize* an extractor or *repair* a broken one — offline, gated, and versioned. No user request is ever served by a live agent driving a browser.

---

## Core flow

```
Source seed (URL / jurisdiction)
  └── Discovery — map the source, find the cheapest data rung
        (API > hidden JSON endpoint > static HTML > rendered/PDF > video)
        → source profile
  └── Extractor Synthesis — LLM emits deterministic extractor code
        targeting the domain schema → versioned extractor artifact
  └── Runtime (no LLM) — runs the extractor on a schedule
        → candidate records + run metadata (row counts, timings)
  └── Validation Harness (the meter)
        ├── structural invariants + delta checks + referential integrity
        ├── cross-source reconciliation (where a 2nd source exists)
        ├── PASS → records enter QUARANTINE (ingested, not yet certified)
        └── FAIL → Repair Loop
              └── (old extractor + new page + failing examples + schema)
                    → LLM proposes new extractor
                    → must pass golden set + validation
                    → load-bearing fields: human review gate
                    → back to Runtime
  └── Certification — quarantined records cleared by a 2nd source or
        human spot-check → publish-eligible
```

Two loops: a **build/repair loop** (does the extractor work?) and a **certify loop** (is the data true?). They are independent. An extractor can run perfectly and still produce uncertifiable data.

---

## Decisions

| Question | Answer |
|---|---|
| LLM in the production hot path? | **No.** Code-generation and repair only, offline. Deterministic code serves. |
| Deployed in V1? | **No.** Lab only. Proven against a controlled source set before any user sees output. |
| First target source | A source that has a **second independent source** — a Legistar city that *also* publishes its own minutes — so the oracle is real from day one. Never start on a single-source jurisdiction. |
| Publish single-source data? | **No.** No second source and no spot-check → the record is *ingested* but never *certified*, and never published. |
| Target schema | A domain-normalized model (municipal legislative item, meeting, agenda item, vote, member). The schema is the IP; extractors fill it, they don't define it. |
| Quarantine | Mandatory. All records land quarantined; only certification promotes them. |
| Oracle types | Cross-source reconciliation (preferred) · golden set (regression) · human spot-check (residual). |
| Human role | Review gate on regenerated extractors for load-bearing fields (votes, dates, identities). Low-stakes fields may auto-promote. |
| Determinism | Every extractor is a stored, versioned, diffable artifact. Self-rewrites are reviewable changes, not invisible ones. |
| Access failures (captcha / block) | Treated as "wrong source," not "solve the captcha." Pivot sources; never fight anti-bot to scrape at scale. |

---

## Feature modules

### 1. Source Discovery

Given a seed, produces a **source profile**: what the source is, where the data actually lives, and the cheapest extraction rung available. Prefers, in order: a real API, a hidden JSON/XHR endpoint the page already calls, static HTML, rendered HTML/PDF, and (future) audio/video. Records access constraints (auth, rate limits, robots/ToS posture) and whether a **second independent source** exists — the last being the single most important field, because it decides whether anything from this source can ever be certified.

### 2. Domain Schema (target model)

The normalized shape every extractor must fill. Domain-specific and versioned. Start scope: municipal legislative activity — `body`, `meeting`, `agenda_item`, `vote` (with per-member positions), `member`. This is the durable asset; extractors are disposable, the schema is not. Schema changes are deliberate and versioned so downstream data stays interpretable.

### 3. Extractor Synthesis

An LLM generates **deterministic extractor code** (not a runtime agent) mapping a source's structure to the domain schema, executed in a sandbox. Output is a versioned artifact with its source profile, target schema version, and generation provenance. Onboarding cost — human-minutes from seed to a passing extractor — is measured here; it's the number that decides whether breadth is economical.

### 4. Runtime

Runs extractors on a schedule. **No LLM.** Cheap, fast, deterministic. Emits candidate records plus run metadata (row counts, field fill rates, timings) that the validation harness consumes. Idempotent and safe to re-run.

### 5. Validation Harness (the meter)

The core research instrument, and the module that must exist from day one. Three layers:

- **Structural** — schema conformance, field types/formats, non-empty, monotonic dates, dates within the session/meeting window.
- **Consistency** — referential integrity (every voter exists in the roster), arithmetic (per-member votes sum to the reported total), row-count deltas vs. prior runs.
- **Cross-source reconciliation** — where a second source exists, the strongest oracle: independent sources that *should* agree, disagreeing, flags corruption with no human.

Ships with a **planted-error injector** (deliberately corrupts known-good records to test detection) and a **golden set** (hand-verified records per source used as a regression gate). These are the test instrument, not the deploy layer. If the harness can't catch planted corruption, nothing downstream is trustworthy.

### 6. Repair Loop

On validation failure, gathers `(old extractor, new source page, failing examples, target schema)` and asks the LLM to synthesize a replacement. The candidate must reproduce the **golden set** and pass validation before it can stage. Load-bearing fields require a **human review gate**; low-stakes fields may auto-promote. Recovery rate — % of source changes repaired with zero human touch — is measured here. False-alarm rate is measured alongside it: an oracle that cries wolf kills the automation as surely as one that misses.

### 7. Certification & Quarantine

Every record lands **quarantined** — ingested, not published. Promotion to publish-eligible requires either cross-source agreement or a human spot-check. The **ingested/certified boundary is explicit and surfaced**: for an accountability tool, being loud about "this county's data is read but not yet verified" is a feature, not an apology.

### 8. Provenance & Versioning

Every record carries its source, extractor version, run id, and certification status. Every extractor version is stored and diffable. A self-rewriting system that changes how it reads vote totals must never do so invisibly — the diff is auditable and, for dangerous fields, reviewed.

---

## The oracle problem (why this is the hard part)

Writing the extractor is nearly solved; **knowing the data is wrong is not.** The failure that hurts is not a crash — it's silent semantic drift: a column shifts, a date format flips, "nay" is read as "yea," and the pipeline emits internally-consistent, externally-false data. Structural checks pass. Only an external oracle catches it: a second independent source, or a human.

Consequence for scope: a jurisdiction with a second source can be **certified and published**; a single-source jurisdiction can be **ingested but not certified**, and stays unpublished until a spot-check or second source appears. No amount of scraper cleverness closes this gap — it is epistemic, not technical. The system's honesty about which side of the line each source sits on *is* the product.

There is also a physical wall unrelated to software: many jurisdictions publish nothing machine-readable (scanned PDFs, image agendas, meeting video, or nothing). Foundry's reach ends where extractable records end. Audio/video → transcription extends the frontier but is explicitly out of V1.

---

## How "good" is measured

The dials the research phase turns. No deployment decision without them:

| Metric | Definition | Why it matters |
|---|---|---|
| **Onboarding cost** | Human-minutes to add a new source cold | Decides whether "every verifiable jurisdiction" is economical |
| **Recovery rate** | % of source changes auto-repaired, zero human touch | The maintenance-collapse thesis, quantified |
| **Oracle precision / recall** | Catches planted corruption without false alarms | Both misses (breaks trust) and false alarms (breaks automation) are failures |
| **Certifiable coverage** | Fraction of ingested records provable via a 2nd source | Separates real coverage from unverifiable coverage |
| **Cost per source-cycle** | LLM + compute per source per refresh | Keeps breadth affordable |

---

## Build sequence (lab milestones)

Each milestone gates the next. Nothing deploys until the metrics clear a bar set at review.

- **M0 — The meter, alone.** Schema + validation harness + planted-error injector + golden set, on **one** Legistar city that also publishes minutes. No synthesis yet. Prove the harness catches injected corruption and reconciles the two sources. *If M0 fails, stop — everything else automates the confident production of wrong data.*
- **M1 — Synthesis.** LLM generates the extractor for that source. Measure onboarding cost.
- **M2 — Repair.** Break the source deliberately (or wait for a real change); measure recovery rate and false-alarm rate.
- **M3 — Generalization.** Add a structurally *different* source (a non-Legistar city). Measure whether onboarding cost holds on an unmodeled source type.
- **M4 — Certification.** Full quarantine → certify pipeline; measure certifiable coverage across the source set.

---

## What we are NOT building (V1)

- **Not deployed.** No user sees Foundry output in V1.
- **Not municipal-at-scale** — one to a few controlled sources, chosen for a second-source oracle.
- **No single-source publishing** — ingest-only until certifiable.
- **No LLM in the production hot path** — synthesis/repair offline only.
- **Not a replacement for LegiScan / Congress.gov** — charted domains keep their APIs.
- **No audio/video transcription** — a real future frontier, out of scope now.

---

## Scaling note

Cost scales with **sources onboarded × repair frequency**, not with user query volume — because the LLM is off the serving path. A source in steady state costs deterministic-runtime pennies; it only costs LLM tokens when it's first built or when it breaks. This is what makes wide, thin coverage of the long tail of local government economically conceivable in the first place.

---

## Open questions

- **Second-source availability at scale.** How many municipalities actually have an independent second source? This caps certifiable coverage more than anything technical. Needs a survey before committing to breadth targets.
- **Repair-loop false alarms.** What false-alarm rate makes the automation net-negative (more human triage than it saves)? Set a threshold during M2.
- **Schema generality.** Does one municipal schema survive contact with wildly different jurisdictions, or does it fragment into per-family variants? Watch at M3.
- **Governance of self-rewrites.** Which fields are "load-bearing" enough to always require human review, and can that set shrink as trust in the loop grows — or must it stay fixed like the notification bar in the Event Engine?
- **Legal / ToS posture.** Per-source scraping legality for municipal sites; a robots/ToS check belongs in the source profile before any extractor runs.
- **Cold-start truth for spot-checks.** For single-source jurisdictions, is there a cheap human-in-the-loop spot-check cadence that yields *some* certification, or do they stay permanently ingest-only?
