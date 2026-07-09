# NosPopuli — Law for the People

> *Nos populus — Latin: "we the people"*

A civic intelligence platform that makes American law accessible to every citizen regardless of legal background, education, or political affiliation. The law affects everyone but is practically readable by almost no one. NosPopuli fixes that.

Live at: **[nospopuli.org](https://nospopuli.org)**

---

## What It Does

A unified search bar accepts plain English questions and routes them intelligently:

- **"What has Congress done about student loans?"** → ranked relevant bills, translated into plain English
- **"Ted Kennedy"** → full member profile with career stats, policy breakdown, photo, recent bills
- **"Senate Judiciary Committee"** → committee page with recent referred legislation
- **"Give me a law that has been passed"** → filters to actually enacted legislation only
- **"HR 3590"** → goes directly to that specific bill
- **"GENIUS Act"** → directly resolves to S.1582, the Guiding and Establishing National Innovation for US Stablecoins Act
- **"3 gun rights bills under Biden"** → respects quantity and presidential term filtering
- **"Kennedy healthcare"** → flags ambiguity with 60% confidence, shows clarification options

The search bar carries a **Federal / State jurisdiction toggle**. The state side is a scrollable picker chip — hover to expand, scroll-wheel through 50 states, type a letter to jump to the first matching state. Federal queries hit Congress.gov + GovInfo; state queries hit OpenStates with the chosen state's current session.

Every bill detail page streams in section-by-section — each part renders the moment it's ready rather than waiting on the slowest — and includes:
- Plain English explanation **personalized to the user's state and interests**
- Legislative timeline showing exactly how it moved through Congress
- House and Senate chamber visualizations — every member's vote as a colored dot in a semicircle, hover for name/party/vote
- Voice votes hidden automatically, recorded votes shown
- Sponsor and cosponsor list with party + state tags
- Notify-me subscription, Public-Law lookup if enacted, and write-to-my-reps drafting

The home **front page** has three additional surfaces:
- **Your Delegation** — your senators + house rep with party, term years, and the most recent floor activity for each
- **Upcoming Elections** — countdown cards with jurisdiction tags ("State, Federal" / "Federal, Local" / "National"), registration deadlines, and ballot context
- **What's Moving** — a personalized ranked feed of bills mixing your reps' work + your topics + your state legislature; status badges, contextual "Why" lines (e.g. *"your senator (Warner) is the lead sponsor · Action today"*), collapsed to 5 with a *Show more →* expander

---

## The Real Goal — From API Wrapper to Self-Building Pipelines

Currently NosPopuli behaves like a multi-API wrapper — Congress.gov, GovInfo, LegiScan, Google Civic, and the Senate LDA all flow in, get translated and cross-referenced, and render as clean civic pages. That's the funnel. It is not the destination.

The trouble with wrapping APIs is that you only reach the places where someone *else* already did the aggregation work. Federal legislation is charted (Congress.gov). State legislation is charted (LegiScan — ~50 hand-built per-state pipelines, normalized into one schema and maintained for over a decade). But the map ends there. **Local and municipal government — city councils, county boards, school districts, the layer of government that touches people most directly — has no aggregator, because the per-source maintenance never scaled.** Thousands of jurisdictions, each with its own portal, format, and quirks; no team is large enough to keep tens of thousands of scrapers alive by hand. So no one has, and the most local layer of American democracy remains the least legible.

NosPopuli's data-aggregation goal is to remove that ceiling by **automating the creation and maintenance of the pipelines themselves.** An LLM-driven system that, given a source, synthesizes its own extractor, validates the output, and repairs itself when the source changes — so coverage is bounded by what is *verifiable*, not by what is *maintainable*. Where that works, the API dependency falls away and the uncharted layer of government becomes reachable for the first time. The APIs stay for the charted domains (state → LegiScan, federal → Congress.gov); the built-in-house effort points squarely at the gap no one serves.

**The honest hard part is not scraping — it is trust.** An LLM can read a page and write an extractor; that part is nearly solved. What it cannot do for free is *know when the data is wrong*. The dangerous failure is not a crash — it is silent semantic drift: a vote column shifts one over, a date format flips, "nay" gets read as "yea," and the pipeline confidently emits internally-consistent, externally-false data. Structural checks alone never catch that. The only cures are per-source oracles — a second independent source to reconcile against, or periodic human spot-checks. For an accountability tool, being loud about the line between *ingested* and *certified* is not a footnote; it is the product.

So the build sequence is deliberate — **make the system first, prove it in the lab, do not deploy, then research how to make it trustworthy:**

1. **Build the system with its own meter.** Version one ships alongside its measurement instrument — a planted-error injector, a golden set of hand-verified records, and a cross-source reconciler. That harness is not the deployment layer; it is how "is this good?" becomes a number instead of a feeling.
2. **Prototype where truth is checkable.** Start on a single source that has a *second* independent source (a Legistar city that also publishes its own minutes), so the oracle is real from day one. Prove the loop catches corruption you deliberately plant before trusting anything it produces.
3. **Then push toward the frontier and turn the dials:**
   - **Onboarding cost** — human-minutes to add a new source cold
   - **Recovery rate** — % of source changes auto-repaired with zero human touch
   - **Oracle precision / recall** — catches planted corruption without crying wolf
   - **Certifiable coverage** — fraction of ingested records provable via a second source

This is the line between a very good wrapper and civic infrastructure that did not exist before. The wrapper is how NosPopuli earns its first users. This is how it stops being replaceable.

---

## Architecture

NosPopuli is a multi-agent AI system. Each agent has one job. They communicate through a structured pipeline coordinated by a dispatcher.

```
User question
      ↓
Router agent          Classifies intent: legislation / member / committee / relational
                      Extracts keywords, time range, result count, entity name
                      Outputs confidence score (0.0–1.0) + ambiguity reason
                      Handles presidential terms: "under Biden" → congress 117, 118
      ↓
Dispatcher            Routes to the correct handler based on query_type
      ↓
─── search cache ─────────────────────────────────────────────
Two-tier cache        Tier 1 (Postgres disk_cache): query → bill ID list, 30-min TTL
                      Tier 2: per-bill latest_action / law status rehydrated
                              from bill_fetcher's in-memory TTLCache on hit
                      Bypassed for freshness-sensitive queries
                              ("this week", "vote scheduled", full_history)
                      Per-request bypass: send {"fresh": true} on /search
      ↓
─── federal legislation ──────────────────────────────────────
Query Expander        Haiku → expands keywords to legislative vocabulary
                      "opioid epidemic" → ["fentanyl", "naloxone", "CARA", "overdose"]
                      Acronym table: "GENIUS Act" → exact bill S.1582
                      Known bills table: bypasses search for famous named acts
      ↓
Title Search          Two-phase lookup for named acts
                      Popular names table: "Title IX", "Voting Rights Act", etc.
                      Falls back to Congress.gov title search + recent amendments
      ↓
Search agent          GovInfo API → full text search (BILLS or PLAW collection)
                      Congress.gov summaries → for named act lookups
                      Two-pass keyword strategy (expanded + raw) guards drift
                      Deduplicates by bill number across versions
      ↓
Result Validator      Haiku → scores each result 0–10 for query relevance
                      Drops results scoring below 5 (4 in full-history mode)
      ↓
[Per bill: /bill and /law stream each section as NDJSON the instant it's
 ready — independent producers run concurrently (asyncio.as_completed),
 so no section waits on the slowest]
Bill fetcher          Congress.gov API → raw bill data (TTL-cached)
Translator agent      Split: Haiku core (plain English, personalized to user's
                      state + interests) streams first; a Sonnet web-search
                      "Background" that resolves referenced statutes/programs
                      streams in behind it, capped at 100s
Historian agent       Congress.gov actions → legislative timeline (Haiku,
                      cached by a hash of the bill's actions)
Vote parser           Scans actions for roll call numbers (House + Senate)
Vote fetcher          House: Congress.gov v3 (118th+) or clerk.house.gov XML
                      Senate: senate.gov XML feed
Vote mapper           Semicircle seat coordinates
                      House: 435 seats, 8 rows · Senate: 100 seats, 4 rows
                      Democrats left, Republicans right

─── state legislation ────────────────────────────────────────
State Search          OpenStates v3 API → state bill search by keyword + session
                      All 50 states enabled (STATE_JURISDICTIONS map)
                      ~17 states have explicit session identifiers; the rest
                        fall back to searching across all available sessions
                      Filters enacted bills by governor signature actions
                      SKIP_PATTERNS filter ceremonial resolutions automatically
      ↓
State Bill Fetcher    Full bill detail: actions, versions, sponsorships, votes
                      Fetches bill text HTML → strips boilerplate via BeautifulSoup
                      Version preference: Chaptered > Enrolled > Engrossed > Introduced
      ↓
State Vote Mapper     State chamber visualization (seat count varies by state)
      ↓
Translator agent      Same Haiku translator, adapted for state bill context

─── member (federal) ─────────────────────────────────────────
Member search         Paginates Congress.gov member list (up to 2,500 members)
                      Nickname expansion: Ted→Edward, Bernie→Bernard
                      Scores by name match weight
                      Fetches profile, terms, photo, sponsored legislation
                      Policy area breakdown from 250 most recent bills

─── member (state) ───────────────────────────────────────────
State Member Search   OpenStates /people endpoint → state legislators by name + state
                      Returns normalized profile: chamber, party, district, role

─── committee ────────────────────────────────────────────────
Committee search      Fetches 500 committees across both chambers
                      Scores by distinctive word match (not common words)
                      GovInfo search for recent referred bills with titles

─── feed (home page) ─────────────────────────────────────────
Feed agent            Three streams: rep-sponsored bills + interest bills + state bills
                      Interest bills are GovInfo-searched then enriched in parallel
                      (Congress.gov detail per bill → latest_action, sponsor, law status)
                      1-hr disk cache; sponsor bioguide preserved for UI lookups

─── elections ────────────────────────────────────────────────
Elections agent       Google Civic API → upcoming elections by zip
                      Per-election: contests (offices + measures), polling place,
                                    registration deadline, absentee info
                      Optional polling data via Claude web search (Sonnet)
                      6-hr disk cache; affects-user filter scopes to user's state

─── notifications / correspondence ───────────────────────────
Event watcher         Daily job — detects bill state transitions, emails subscribers
                      State machine: introduced → in_committee → passed → signed/vetoed
                      Only fires on meaningful upgrades, not redundant actions
Correspondence        Gmail OAuth — user-authenticated letter drafting + sending
                      Threaded reply tracking; per-bill subscriptions in Postgres
                      Draft generator personalizes to user's state + bill stance

─── All actions logged ───────────────────────────────────────
Documentor            Thread-safe JSON logging of every agent action
Search Logger         User-facing event logging (searches, bill opens, member opens)
                      Includes confidence scores for quality tracking
Flag Logger           User feedback on incorrect results or translations
Analyst               Reads search + agent logs, generates plain English report
                      Surfaces zero-result queries, misclassifications, top topics
```

---

## Tech Stack

```
Backend:      Python, FastAPI, uvicorn
AI:           Anthropic API
              · Haiku — query expansion, validation, translation (~98% of calls)
              · Sonnet — web search: bill "Background" reference resolution
                and optional election polling/news
Data:         Congress.gov API — bills, members, votes, committees, laws
              GovInfo API — full text search (BILLS + PLAW collections)
              OpenStates v3 API — state legislation + state legislators (all 50)
              Google Civic API — elections, contests, polling places
              senate.gov XML — Senate roll call votes
              clerk.house.gov XML — House roll call votes (pre-118th)
              pgeocode + unitedstates/congress-legislators — zip→representative
Ingest:       VoyageAI voyage-law-2 — legal document embeddings
              Supabase pgvector — vector store for bill chunks
              BeautifulSoup — bill HTML text extraction
Storage:      Postgres on Supabase — subscriptions, drafts, sent mail,
              disk_cache table for search/feed/elections (namespaced TTLs).
              Connection via SUPABASE_DB_URL (psycopg3 pool).
Email:        Gmail OAuth (per-user) for outbound letters
              SMTP fallback for system notification emails (event_watcher)
Deployment:   Railway (auto-deploys from GitHub)
Frontend:     Vanilla HTML/CSS/JS (no framework), CSS and JS in separate files
Fonts:        Playfair Display · Source Serif 4 · IBM Plex Mono
Rate limiting: slowapi (20/min search, 30/min bill, 10/min feed)
```

---

## File Structure

```
/NosPopuli
  api.py                      FastAPI app — all endpoints, dispatcher pattern
                              /bill + /law stream detail as NDJSON via a shared
                              section generator (_bill_detail_stream)
  router_agent.py             Intent classification, confidence scoring,
                              presidential term handling, known bill lookup
  query_expander_agent.py     Keyword expansion to legislative vocabulary
                              Acronym table, known bills bypass
  title_search_agent.py       Two-phase named act lookup
                              Popular names table (Title IX, VRA, Civil Rights Act…)
                              Falls back to Congress.gov title search
  search_agent.py             GovInfo full text search + Congress.gov summaries
  search_cache.py             Two-tier search-result cache (NEW)
                              Tier 1: Postgres disk_cache, 30-min TTL on result list
                              Tier 2: in-memory rehydrate of latest_action/law
                              Freshness-query bypass + per-request {"fresh": true}
  clear_search_cache.py       CLI utility — `python clear_search_cache.py [--all]`
  result_validator_agent.py   Post-search relevance scoring via Haiku
                              Filters results below threshold before display
  bill_fetcher.py             Congress.gov bill and public law fetching
                              TTLCache: 1hr bill, 30min actions, 2hr text
  translator_agent.py         Plain English translation, personalized by user
                              state and interests (STATE_CONTEXT table). Split
                              into translate_bill_core (fast Haiku) and
                              resolve_bill_background (slow Sonnet web search)
  historian_agent.py          Legislative timeline generation (Haiku, cached
                              by a hash of the bill's actions)
  vote_parser_agent.py        Roll call number extraction from bill actions
  vote_fetcher_agent.py       House + Senate vote data (multiple sources)
  vote_mapper_agent.py        Federal semicircle seat position math
  state_vote_mapper.py        State chamber seat position math
  member_search_agent.py      Federal member lookup, profile, legislation, policy areas
  state_search_agent.py       OpenStates state bill search
                              ENABLED_STATES = all 50; STATE_SESSIONS pin
                              session identifiers for ~17 states
  state_bill_fetcher.py       OpenStates bill detail + HTML text extraction
  state_member_search_agent.py  State legislator lookup via OpenStates /people
  feed_agent.py               Personalized feed — rep bills + interests + state
                              Parallel enrichment of latest_action, sponsor,
                              and public-law status for each interest bill
                              INTEREST_TERMS map topics to legislative vocabulary
  elections_agent.py          Google Civic elections lookup + optional Claude
                              web-search polling pull (per election, 6hr cache)
  event_watcher.py            Daily watcher — detects bill state transitions and
                              emails active subscribers; run via POST /watcher/run
                              or standalone `python event_watcher.py`
  civic_resolver.py           Zip code → state → senators + representative + terms
                              Uses pgeocode + legislators-current.json + zip3_to_state.json
  ingest_bills.py             6-stage bill embedding pipeline (offline)
                              Discovery → Metadata → Text → Chunking → Embedding → Index
                              VoyageAI voyage-law-2 + Supabase pgvector
  documentor_agent.py         Thread-safe agent action logging
  search_logger.py            User-facing event logging with confidence scores
  flag_logger.py              User feedback logging (search + bill flags)
  analyst_agent.py            Usage pattern analysis, AI-generated report
  watch_agents.py             Terminal agent monitor — color-coded live log tail

  /correspondence
    router.py                 FastAPI sub-router mounted at app root
    db.py                     Postgres (psycopg3) helpers: users, drafts, sent mail,
                              subscriptions, replies, disk_cache (search/feed/
                              elections), known_elections
    auth.py                   Google OAuth — login, token storage, identity
    draft.py                  Letter draft generator (per-bill, per-rep,
                              state-personalized)
    gmail.py                  Gmail send + threaded reply ingestion

  /frontend
    index.html                Main app
                              - Sticky command bar with Federal/State picker
                              - Personalized feed (Front Page / Briefing /
                                What's Moving) + onboarding
                              - Bill detail, member, committee pages
                              - Notifications + watchlist pages
                              - Letter-writing panel + correspondence threads
                              - Clarification bar for low-confidence queries
    elections.html            Elections list + tracking
    election_detail.html      Individual election page
    admin_elections.html      Manually curated election entry UI
    test.html                 Full-fidelity design mockup of the redesigned
                              app — used to prototype layouts (state picker,
                              search dropdown, election detail, bill detail)
    monitor.html              Real-time agent monitor:
                              - Agent feed with color coding
                              - Pipeline flow visualization
                              - Analytics tab (AI-generated report)
                              - Flags tab (user feedback log)
    /css
      index.css               Main app styles
      test.css                Mockup styles
      monitor.css             Monitor page styles
    /js
      index.js                Main app logic
      correspondence.js       Letter-writing + reply thread handlers
      monitor.js              Monitor page logic

  /data
    legislators-current.json  All current US members (1.5MB)
                              Source: unitedstates/congress-legislators
    zip3_to_state.json        3-digit ZIP prefix → state (used by civic_resolver)
    known_elections.json      Curated election entries (admin UI)

  (no on-disk DB)             Postgres on Supabase — user subscriptions, drafts, sent mail,
                              disk_cache, known_elections, replies
  Styleguide.md               Complete design system reference
  Procfile                    Railway: uvicorn api:app --host 0.0.0.0 --port $PORT
  nixpacks.toml               Railway build configuration
  requirements.txt            Python dependencies
  .gitignore                  Excludes: .env, .venv, agent_log.json, search_log.json,
                              flags.json, __pycache__, .DS_Store
```

---

## API Endpoints

```
─ Search & content ─────────────────────────────────────────
POST /search                  Unified search — routes to legislation, member,
                              committee, named-entity, or law handlers.
                              Returns confidence + ambiguity_reason + cached flag.
                              Accepts {"fresh": true} to bypass search cache.
POST /bill                    Streams bill detail as NDJSON section-by-section
                              (translation, timeline, votes, sponsors, …)
                              Accepts optional user_context for personalization
POST /law                     Streams public-law detail as NDJSON (same
                              generator as /bill), by congress + law number
POST /state/search            State legislation search via OpenStates
                              Requires state_code; filters enacted bills only
POST /state/bill              Full state bill detail + translation
POST /state/member/search     State legislator lookup by name + state
POST /member/search           Federal member search
GET  /member/photo/{id}       Proxied Congress.gov member photo

─ Personalization ──────────────────────────────────────────
POST /feed                    Personalized feed — interests + reps + state
POST /resolve-zip             Zip → state + senators + representative + terms

─ Elections ────────────────────────────────────────────────
GET  /api/elections           Upcoming elections (Google Civic + curated)
GET  /api/elections/{id}      Full election detail (contests, info, links)
GET  /api/elections/{id}/polling   Optional polling data (Sonnet web search)
GET  /elections               Elections list page (HTML)
GET  /elections/{id}          Election detail page (HTML)

─ Correspondence (Gmail-authenticated letter writing) ──────
GET  /auth/google             Start Google OAuth
GET  /auth/google/callback    OAuth callback
GET  /auth/me                 Current authenticated user
POST /user/zip                Save user zip (server-side, when authenticated)
POST /correspondence/draft    Generate a letter draft for a bill + rep
POST /correspondence/send     Send a drafted letter via Gmail
GET  /correspondence          List user's sent correspondence
GET  /correspondence/{id}/replies   Threaded replies on a sent letter
POST /correspondence/subscribe        Subscribe to a bill (event_watcher)
POST /correspondence/unsubscribe      Unsubscribe
GET  /correspondence/subscription     Subscription state for a bill
POST /correspondence/followup/draft   Followup letter on reply received
GET  /correspondence/unsubscribe-link Public unsubscribe link (no auth)

─ Watcher & admin ──────────────────────────────────────────
POST /watcher/run             Trigger the daily bill-state watcher
                              (protected by WATCHER_SECRET)
GET  /admin/elections         Curated election list (admin UI)
POST /admin/elections         Add a curated election
DELETE /admin/elections/{id}  Remove a curated election
GET  /admin/elections/ui      Admin elections UI (HTML)

─ Feedback ─────────────────────────────────────────────────
POST /flag/search             Log user feedback on search results
POST /flag/bill               Log user feedback on bill translation/timeline

─ Monitor ──────────────────────────────────────────────────
GET  /monitor                 Real-time agent monitor UI
GET  /monitor/stream          Agent log as JSON (polled every 500ms)
GET  /monitor/analysis        AI-generated usage analysis
GET  /monitor/flags           All user flags
POST /monitor/clear-search-log    Clear the search log (admin)
POST /monitor/clear-search-cache  Clear the search-result cache (admin)

─ Misc ─────────────────────────────────────────────────────
GET  /                        Main app (index.html)
GET  /test                    Design mockup page (test.html)
GET  /health                  Service health check
GET  /favicon.ico             204 No Content (suppresses 404s)
```

---

## Personalized Feed

Anonymous by default. No account required for the feed. Stored in browser localStorage. Clearing cookies resets everything. (A signed-in Google account is only needed to *send* letters to reps; reading the feed and watching bills works without it.)

**Onboarding (2 steps):**
1. Enter zip code → resolves to your state, 2 senators, 1 house rep, plus their term years
2. Select interests from 12 topics → Healthcare, Climate, Housing, Education, Veterans, Economy, Immigration, Gun Policy, Foreign Policy, Criminal Justice, Small Business, Agriculture

**Feed generation:**
- **Rep bills** — most recent sponsored legislation from your 3 representatives (90-day window)
- **Interest bills** — GovInfo search using curated legislative term maps per topic, then enriched in parallel: each bill's latest action, sponsor (name + party + state), and public-law status are fetched from Congress.gov so status badges reflect real state
- **State bills** — recent bills from your state's legislature via OpenStates (if state is set)
- Designation/ceremonial resolutions filtered out automatically
- Bill translations personalized to your state context

**Rendering:**
- The feed is sorted by stage rank (enacted > passed > reported > committee > introduced)
- The top bill becomes the front-page **lede**, with sponsor line + actions
- The next 3 become a 3-up briefing
- The rest are a ranked list, collapsed to **5 with a Show more → expander**
- Each row's "Why" line is contextual: *"your senator (Warner) is the lead sponsor · Action today"* or *"your topic — Healthcare · Now public law"*

---

## Bill Watchlist & Correspondence

Users can **subscribe** to a bill and receive email updates when it changes state (passes committee, passes a chamber, gets signed/vetoed). Subscriptions live in the Supabase `subscriptions` table; `event_watcher.py` runs daily, detects meaningful transitions, and emails active subscribers via SMTP.

Authenticated users (Google OAuth) can also **write to their representatives** directly from a bill page. The draft agent generates a personalized letter using the bill's status, the user's state context, and the user's preferred position. Letters send via the user's own Gmail thread. Replies from reps land back in the user's inbox and are tracked as threaded conversations in the app — followup drafts can be generated against a received reply.

---

## Elections

The home page surfaces upcoming elections from the **Google Civic API** scoped to the user's zip. Each election card carries:
- Days until election (colored urgency: ≤30 / ≤90 / further)
- Jurisdiction tag derived from contest levels (Federal / State / Local / Ballot measures)
- Sub-line with registration deadline + contest count, or "Polls open ~N months" for far-out elections

The dedicated election detail page (and the redesigned mockup in `test.html`) goes further: candidates, party tags, sponsor of each contest, polling snapshot (multi-source, with pollster grades, margin trend), money & momentum (FEC-style), where-they-stand issue comparison, endorsements, voter sentiment top-issues, and a recent news / debate timeline. Polling content is currently mock-rendered pending a real polling-data integration; the rest is live wherever Google Civic + a curated overrides table can fill it.

---

## Realizing the Election Mockup

The election detail mockup in `test.html` is the design target. Most sections are placeholder data today. Cheapest-path plan for each, mapped to actual data sources:

```
SECTION                         REAL SOURCE                                 STATUS
─────────────────────────────── ─────────────────────────────────────────── ─────────
Candidates list                 Google Civic (federal) + Ballotpedia        partial
                                scrape + curated overrides table

Polling snapshot + trend        Wikipedia per-race polling tables           planned
                                (surprisingly current, surprisingly         (need
                                structured) → weekly LLM normalization      scraper +
                                pass to standardize columns                 normalizer)

                                Pollster grades: hard-coded table seeded
                                from FiveThirtyEight's archived ratings
                                (GitHub, last published 2024)

                                Aggregate "leader avg" = simple weighted
                                mean over polls in last 30 days

Money & momentum (FEC)          api.open.fec.gov — receipts, disbursements, planned
                                cash on hand, donor geography. All federal  (free API,
                                candidates covered, fully free, stable.     no auth)

                                Ad spend: FCC public files are the source
                                of truth but unstructured. Skip for v1.
                                Use FEC "media expenditures" line items
                                as a proxy.

Where they stand                Incumbents: ProPublica Congress API for     planned
(issue stances)                 vote history (free, deprecated but mirror
                                exists). Challengers: LLM extraction over
                                campaign sites + 2024-cycle questionnaires
                                from LCV, AFL-CIO, Planned Parenthood, NRA,
                                with citation links.

Endorsements                    No API exists. Ballotpedia per-race pages   planned
                                are the cleanest source. Combine with LLM   (scrape +
                                extraction over campaign sites. Manual      curate)
                                review for high-profile races.

Voter sentiment / top issues    Pew, Gallup, AP-NORC publish state-level    planned
                                topline PDFs ~monthly. PDF→text via         (cron +
                                pypdf, then LLM extraction to JSON.         LLM)
                                Refresh quarterly per state.

News & debate timeline          GDELT (gdeltproject.org) — free real-time   planned
                                news index, filter by candidate name +      (GDELT
                                state. Debate schedules: scrape Ballotpedia (free) +
                                race pages.                                 scraper)
```

**Order of build** (each unblocks the next):

1. **FEC** — easiest win, free API, immediate value. Powers the Money & Momentum card and the in-state-donor % figure. ~1 day.
2. **Wikipedia polls scraper + LLM normalizer** — second highest signal-to-effort. Powers both the poll table and the trend chart. ~3 days.
3. **GDELT news** — free, real-time, near-zero infra. ~1 day.
4. **Ballotpedia endorsements + candidate basics** — scrape, cache, LLM-extract. ~3 days.
5. **ProPublica votes + LLM stance extraction** — last because it's the most curation-heavy. ~1 week with manual review for the first few high-profile races.

Pollster grades, voter sentiment, and debate schedules are best treated as small curated tables seeded once and refreshed quarterly — not worth a live pipeline.

**Minimum viable election detail page**: FEC + Wikipedia polls + GDELT news. That covers ~70% of what the mockup shows, all free, all stable APIs. Endorsements and stances are the expensive remainder.

---

## Search Cache

Search latency is dominated by three LLM calls (router, expander, validator) and ~4 HTTP fetches. The two-tier cache cuts that to near-zero on repeat queries:

- **Tier 1** — Postgres-backed `disk_cache` keyed on a SHA1 of `(question, keywords, expanded_terms, topic, congress_numbers, status, max_results, jurisdiction)`. 30-minute TTL. Stores only minimal bill IDs + titles.
- **Tier 2** — On hit, each result's current `latest_action`, `latest_action_date`, `is_law`, `law_number` is re-fetched in parallel via `bill_fetcher.fetch_bill` (which has its own 1-hour in-memory TTLCache). The list is cached; the badges stay live.

**Bypass paths** so debugging stays sane:
- Per-request: `{"fresh": true}` on the `/search` body skips cache for that one call
- Per-query: phrases like *"this week"*, *"today"*, *"vote scheduled"*, *"just introduced"* are detected and bypass automatically
- Always-bypass: `full_history=true` (the *Show More* expansion)
- Manual flush: `python clear_search_cache.py` (or `--all` to clear feed + elections too), or `POST /monitor/clear-search-cache`

Cache prefix is namespaced (`search:v1:`) so future shape changes can bump the version without manual cleanup.

---

## Design System

Full reference in `Styleguide.md`. Key principles:

- Newspaper editorial aesthetic — aged paper, ink, serif typography
- **Never** use border-radius, box-shadow, or purple/blue/green
- Three fonts only: Playfair Display (headings), Source Serif 4 (body), IBM Plex Mono (labels)
- Colors: `--ink #0e0e0e` · `--paper #f5f0e8` · `--accent #8b1a1a` · `--muted #6b6355` · `--rule #c8bfaa`
- When in doubt: would this look at home in a 1940s legal newspaper?

The `frontend/test.html` route is a complete higher-fidelity mockup of the app — used to prototype the redesigned election detail page, the state picker, the search results dropdown, and the redesigned bill page before porting to production CSS.

---

## Confidence Scoring

The Router outputs a confidence score (0.0–1.0) for every query:

```
1.0  → Completely unambiguous: "HR 3590", "Ted Kennedy", "Senate Judiciary Committee"
0.85 → Clear with minor uncertainty: "healthcare bills", "what did Biden sign"
0.6  → Ambiguous: "Kennedy healthcare" — person or legislation?
<0.7 → Shows clarification bar with alternative search suggestions
```

Low-confidence queries are logged to `search_log.json` for Analyst review.

---

## Currently Running

https://nospopuli.org

## Running Locally

```bash
# Install dependencies
pip install -r requirements.txt

# Set up .env
ANTHROPIC_API_KEY=your_key
CONGRESS_API_KEY=your_key
GovInfo_API_KEY=your_key
OPENSTATES_API_KEY=your_key       # required for state legislation endpoints
GOOGLE_CIVIC_API_KEY=your_key     # required for elections endpoints
SUPABASE_DB_URL=postgresql://...  # Supabase Postgres pooler URI (Settings → Database → Connection pooling)

# Optional — only needed for letter writing + replies
GOOGLE_CLIENT_ID=...
GOOGLE_CLIENT_SECRET=...
GOOGLE_REDIRECT_URI=http://localhost:8000/auth/google/callback

# Optional — only needed for event_watcher email notifications
NOTIFY_FROM_EMAIL=...
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=...
SMTP_PASS=...
WATCHER_SECRET=...                # protects POST /watcher/run

# Optional — only needed if running ingest_bills.py
SUPABASE_URL=your_url
SUPABASE_KEY=your_key
VOYAGE_API_KEY=your_key

# Run
uvicorn api:app --reload

# Open
http://localhost:8000          Main app
http://localhost:8000/test     Design mockup
http://localhost:8000/monitor  Agent monitor

# Terminal agent monitor (live log tail with color coding)
python watch_agents.py

# Clear search cache (debugging)
python clear_search_cache.py
python clear_search_cache.py --all      # also clears feed/elections caches

# Bill embedding pipeline (run once offline to populate pgvector)
python ingest_bills.py

# Daily bill-state watcher (or schedule via cron / Railway scheduler)
python event_watcher.py
```

---

## Cost

Model routing philosophy: use the cheapest model that can reliably do the job. Haiku handles ~98% of all tasks — query expansion, validation, translation. Sonnet is only used for optional election polling/news web search and would be needed for relational query reasoning (planned). Opus is not used in the core pipeline.

The search cache and bill-fetcher TTL caches eliminate most repeat-query cost. Per-bill enrichment runs in parallel against Congress.gov rather than via LLM, so the marginal cost of richer status badges is HTTP, not tokens.

---

## Roadmap

```
SEARCH QUALITY
✓ Search-result cache (Postgres-backed, two-tier with rehydrate)
→ Relational queries: "How does X relate to Y" (Sonnet)
→ Member search filters (party, chamber, state)
→ RAG over embedded bill corpus (ingest pipeline built, query layer pending)

VISUALIZATIONS
→ Vote breakdown charts (party line vs independent)
→ Legislative knowledge graph (D3.js force-directed)
→ Sponsor map (geographic cosponsorship)
→ Bill progress tracker

STATE EXPANSION
✓ OpenStates v3 integration — state bills, state member search
✓ State bill search, detail, translation endpoints
✓ All 50 states enabled
✓ State picker UI on the search bar
→ State vote data + chamber visualizations
→ Session identifier coverage for remaining ~30 states

ELECTIONS
✓ Google Civic integration — upcoming elections by zip
✓ Election detail page + per-election polling agent (Sonnet web search)
✓ Front-page election cards with jurisdiction tags + countdown
→ Real polling data integration (currently mocked in detail mockup)
→ FEC fundraising integration (Q-by-Q candidate financials)
→ Endorsement extraction pipeline (Ballotpedia + campaign sites + LLM)

CORRESPONDENCE / NOTIFICATIONS
✓ Bill subscription + daily event watcher
✓ Gmail OAuth letter drafting + sending
✓ Threaded reply tracking + followup drafts
→ Multi-rep coordinated send (foundation for V2)
→ Reply analyzer agent (classify rep position from response text)

LOBBYING & MONEY-IN-POLITICS
  Dedicated section of the app — accessible like the Elections tab —
  surfacing who lobbies Congress, on what, and where the money goes.
  Lobbying data is public but practically opaque; the friction-collapsing
  move is putting it next to the legislation it influences.

→ Lobbying directory page: searchable index of every registered entity
  (client + lobbying firm). Click an entity → its lobbyists, total spend
  per quarter, bills lobbied, top recipients of donations from its PAC
  and employees, revolving-door staff who came from Congress.
→ Lobbyist profile: prior gov't employment, current clients, total
  filings since registration, members they cluster around.
→ Per-member view: every lobbying entity that has reported lobbying this
  member, every PAC/contribution from those entities, total dollars.
→ Per-bill panel on every bill detail page: "Who's pushing this" —
  top entities lobbying the bill, total reported spend in current quarter,
  contributions from those entities to the sponsors and committee members.
→ Coalition view: bills with overlapping lobbying coalitions
  (e.g. pharma + insurer + AMA all lobbying same bill).

  Sources:
  · Senate Office of Public Records LDA filings (https://lda.senate.gov)
    — structured quarterly XML, free, contains bill references
  · FEC API for campaign contributions
  · OpenSecrets API (free up to 200 req/day) for cleaned entity
    name normalization + revolving-door data
  · House/Senate financial disclosure forms (PDF → LLM extract) for
    member asset holdings

  Editorial principle: facts side-by-side, not implication. "$X lobbied,
  $Y given, this vote happened" without "therefore corruption." Trust the
  reader to draw their own line.

LOCAL / MUNICIPAL
→ Phase 1: Legistar API — covers 100+ major cities (NYC, Chicago, LA, Seattle, Boston, SF)
           Legistar is the dominant council management platform; unofficial API at webapi.legistar.com
→ Phase 2: Municode / American Legal Publishing — municipal code search for ordinances
→ Phase 3: Direct scraping for non-Legistar cities (high maintenance, low priority)
   Note: No equivalent of Congress.gov or OpenStates exists for local government.
         This is a genuine gap in civic infrastructure — building it is a multi-year effort.

COLLECTIVE ACTION (V2 CORE)
  The thesis: NosPopuli makes it so easy for a group to be heard that people
  will use it. The friction between "I care about this" and "my representative
  knows I care about this" is enormous for most people. NosPopuli collapses it.

  The unit of impact is the group, not the individual user.
  One letter is noise. A thousand letters from identifiable constituents in
  a single week on a bill in committee is signal a staffer briefs their boss on.

→ District signal: show how many users in a zip/district care about a bill
→ Fence-sitter map: identify representatives whose votes are genuinely persuadable
→ Committee timing: surface bills in committee now (when pressure matters most)
→ Momentum tracker: bills gaining constituent attention across districts
→ Coordinated send: synchronized letter campaigns on a bill + date window
→ Coalition view: same position across 40 districts carries structural weight
  Note: this is not ideology — it is infrastructure. A conservative and a
        progressive both benefit from a government that responds to constituents.

CODE HEALTH (REFACTORS)
  Internal cleanup, no user-visible change. These are the structural
  weaknesses identified in a codebase review — fix them before the team
  scales past 2 people, because each one compounds with size.

→ Extract /search dispatcher into _dispatch(structured, question, body, loop).
  The endpoint currently does 200+ lines of presidential overrides, state
  redirects, off-topic gating, and member disambiguation before delegating
  to a handler. Pure mechanical refactor, no behavior change.
→ Stop mutating the `structured` dict as scratchpad. handle_legislation_search
  adds expanded_terms, original_question, _bypass_search_cache to a dict the
  router produced — inputs should be immutable, intermediate state needs its
  own variable.
→ Split frontend/js/index.js. At ~2500 lines it's the source of every
  state-leak bug we've hit (federal sponsors leaking into state bill detail,
  stale stateName from prefs). Use native ES modules — multiple <script
  type="module"> tags, no bundler required. Suggested split: search.js,
  detail.js, state.js, feed.js, helpers.js.
→ Extract shared frontend helpers into helpers.js: escapeHtml, stateNameFromCode,
  _compactBillTitle, runSearch. These are effectively globals today.
→ Standardize search-style endpoint response envelope. Every search-type
  response should return {query_type, confidence, ambiguity_reason, results,
  cached, suggested_jurisdiction?}. Member and committee responses keep their
  own shape but share the same five-field meta header.
→ Move handle_committee_search into its own module (committee_search_agent.py).
  It's the only handler that doesn't follow the agent-per-module pattern; it
  even re-imports `requests` and `re` inline because it was clearly copy-pasted.

SELF-IMPROVEMENT PIPELINE
→ Prompt versioning + performance monitoring
→ Prompt improver agent (Opus)
→ Drift detector + arbitrator

DEPLOYMENT
✓ Railway auto-deploy from GitHub
✓ Custom domain (nospopuli.org)
→ CDN for frontend assets
```

---

## Legal

Public law is not copyrightable in the United States. All legislative text fetched and displayed by NosPopuli is in the public domain. Plain English translations are original works created by the system. NosPopuli displays information only — it is not legal advice. Every page includes a disclaimer to this effect.

---

*NosPopuli — Law for the People*
