# NosPopuli — Law for the People

> *Nos populus — Latin: "we the people"*

Live at **[nospopuli.org](https://nospopuli.org)**

I'm building a way to make American government legible to ordinary people. Not a
summarizer, not a news feed — a map of who governs you, what they decided, and what
it cost, that you can actually walk.

---

## Where this actually is

I want to be precise about the current state, because I've been burned by my own
optimistic documentation.

**What works well.** Federal legislation is fully wired: type a bill ID, a topic, a
named act, or a member's name and you get a real answer. Bill detail streams in
section-by-section — plain-English translation, timeline, roll-call vote maps, sponsors,
lobbying, sponsor money. Elections work. And Foundry — the self-building local-government pipeline — is the part
I'm proudest of: nine municipal sources ingested, certified against independent second
sources, with the uncertified records visibly quarantined rather than quietly published.

**What's half-built.** State legislation is fully implemented for all 50 states
through LegiScan — and the home page cannot reach it. `/ledger` forces every query to
federal unless it matches the state-bill-ID fast path (`api.py:1618`), so "Virginia
housing bills" sets my *place* for personalization and then searches Congress. The only
door to state search is the Federal/State picker on the orphaned `/newspaper`.

Money, lobbying and stock trades are the same story: implemented, live endpoints, and
unreachable by typing a question. Member finance and stocks at least open if you click
into a member; the lobbying directory and the stock explorer exist only on
`/newspaper`. Same for the geo resolvers, which are driven by map clicks and the place
picker but never by text.

Letter-writing is built and shipped *disabled*, because I don't have reliable email
addresses for representatives. And local search throws away your topic: "zoning in
Fairfax" and "Fairfax" return the identical page, because there's no index over the
local corpus yet.

**What's messy.** There is now one frontend: `frontend/test.html` + `js/ledger.js`,
served at `/`. Despite the filename, that is production.

The old tabbed app at `/newspaper` is deleted. It was the only route to state
legislation search, the lobbying directory, the stock explorer, public-law detail, the
personalized feed and structured feedback — so **those are offline until I rebuild
them.** Their endpoints all still work; only the views are gone. The full list, with
endpoints and the order I'll do them in, is under "Rebuilding the newspaper
capabilities" below. I chose to rebuild rather than port because I didn't want to carry
that UI forward.

The ask SPA has eight views: home, ledger, bill, uncharted, watching, member, offtopic,
elections.

---

## Where it's going

### What the map is for

A search engine can find you a document. It cannot tell you how the person you elected
voted, because that answer doesn't live in a document — it lives in a **join** between
a county vote record, a board roster, an election result and your address. Nobody has
made that join, so nobody can answer it, so people stop asking.

That is the end goal: **questions whose answers require crossing layers of government
become a walk across the map, and every step can show its source.**

Two things follow, and they're worth separating because they come from different places:

- **Specificity comes from the joins, not from volume.** More rows in more silos buys
  nothing. "Which supervisors approved the rezoning and who funded them" needs
  vote → person → seat → contest → money to be one connected path. That is what mapping
  buys, and it's the only thing that buys it.
- **Accuracy comes from certification, not from mapping.** A complete map can be
  confidently wrong — this app currently answers "LA County" as off-topic with 0.95
  confidence. So every edge carries whether an independent source affirms it, and every
  answer reports the weakest hop it crossed.

And note what is *not* the goal: anticipating the questions. I can't enumerate what
people will ask, and trying to is the mistake that produced the classifier. The
entity types and edge types are finite — a body, a seat, a person, an instrument, a
vote, a contest, about a dozen relations — while their **combinations** are unbounded.
Model the domain, and the question space takes care of itself.

The reason this is worth the effort is in the paragraph below: the local layer is the
one that touches people most and the one nobody has charted. A map of it does something
a search box can't — search answers the question you asked, a map shows you the
question you didn't know to ask, which is usually that the rezoning passed 7–3 and two
of the ayes were seated by 900 votes.

The original framing was "make an API wrapper, then escape the wrapper by generating my
own pipelines." Foundry did that. But building it taught me the wrapper was never the
real limit — **classification** was.

Every question used to get sorted into one bucket: legislation, or member, or place.
That's a projection, and it throws away the half of the question that made it
interesting. "Which supervisors approved the rezoning and who funded them" has no
single correct bucket. Adding more buckets doesn't fix it; it just makes the projection
finer.

So the direction now is to **model government as a temporal property graph with
provenance**, and answer questions by traversing it instead of classifying them.

Government is self-similar. The Fairfax Board of Supervisors is to Fairfax County what
Congress is to the United States: a body, in a jurisdiction, with seats, held by people,
who vote on instruments, at meetings, put there by elections. That repeats at every
layer, which means one ontology covers all three. Foundry's extraction schema converged
on that shape on its own, which is decent evidence the shape is real and not something
I'm imposing.

Three things make it tractable:

- **The skeleton is small.** Every jurisdiction, body, seat and officeholder in the
  United States is on the order of one to two million nodes. That's two Postgres tables,
  not a graph database.
- **Events stay where they are.** Individual votes and contributions run to hundreds of
  millions. Those don't go in the graph — the graph holds topology and identity, and
  dereferences facts at the leaves. A new source adds edges to an existing skeleton
  rather than inflating it.
- **Time is a column, not an afterthought.** Almost every edge is bounded. "Who voted
  for this" has to resolve seat-holders *as of the vote*. A snapshot model silently goes
  wrong, and I'd rather pay for that up front.

**The honest hard part is identity, and I've measured it.** Matching Fairfax supervisors
to the contests that elected them: exact string comparison gets 3 of 12. A ten-line
normalizer that strips nicknames and suffixes gets 9 of 12. The remaining three are one
genuine coverage gap, one duplicate of the same person inside one source, and one
extraction bug — a sentence fragment currently sitting in the board roster as if it were
a person. That's the work: mostly mechanical, with a residue that deserves a human.

I'm scoping v1 to Virginia, top to bottom — federal, the General Assembly, and the four
counties I already have — because all of that data is already on disk and it proves
cross-layer traversal for real people in one place before I scale sideways.

**Certification extends to edges.** Foundry certifies records today. A graph's claims
are edges, and a wrong edge is worse than a wrong record because everything traversing
it inherits the error. An edge nobody independently affirms is still traversable, but
every answer that crosses one says so and says which hop was weak.

### Prior art I'm deliberately not reinventing

Popolo and Open Civic Data already published essentially this ontology, and I'm already
carrying `ocd_id` in the state layer. I'd rather adopt their division IDs and naming than
invent my own.

Worth knowing why those projects went quiet: Sunlight Foundation, EveryPolitician, the
OCD scrapers. None of them died of a bad schema — the schemas still read correctly. They
died of ingestion maintenance. That's exactly the ceiling Foundry attacks, which is why
I think the graph is table stakes and the self-healing pipeline is the part that's
actually mine.

---

## What's next

In order. Each step is independently useful, so none of it is wasted if I stop partway.

**1. Get everything into git.** 6,341 lines of load-bearing source have never been
committed — `frontend/js/ledger.js`, `ledger_agent.py`, `foundry/health.py`,
`search_rank.py`, `bill_text_format.py` and four of my seven test files. Not
gitignored; never added. The production home page exists on one laptop with no history.
This is the only genuinely urgent item here.

**2. Golden fixtures.** Capture request → response JSON for all 70 endpoints from the
live app and commit them. Cheap to make, and they're what lets me refactor anything
afterward without guessing whether I changed behaviour.

**3. Unify the two routers.** `_resolve_routing` and `classify_question` disagree and
each has fast paths the other lacks. Extract `search_dispatcher.py` out of the
3,707-line `api.py` while I'm in there.

**4. Give the ask SPA a state plate.** Stop `/ledger` forcing federal, and add a state
bill view to `ledger.js`. This is the biggest live capability gap: the state layer is
built and paid for and most users can't reach it.

**5. Index the local corpus.** Postgres FTS over instrument titles and the 4,384 item
summaries, scoped by jurisdiction. This is what makes "zoning in Fairfax" stop
discarding the topic.

**6. The graph, Virginia first.** Two tables — `graph_node` and `graph_edge`, the latter
with `valid_from`/`valid_to` and a certification column. Federal, the General Assembly,
and the four counties already on disk. Person resolution via the normalizer that takes
supervisor↔contest matching from 3/12 to 9/12. Uncertified edges stay traversable and
badged.

*Slice one exists:* `graph.py` builds Fairfax BOS top to bottom — state → county →
districts → board → seats → people → agenda items, plus the 2023 contests that seated
them — and answers `person → voted_on → agenda_item` filtered by topic, at
`GET /api/graph/votes?person=&topic=`. `python graph.py build fairfax-bos` runs the
whole thing in memory with no database and prints the gaps; `load` writes it. What it
proved against real data: every Fairfax edge is `ingested` (single-source), so the
weak-hop badge is on every answer; edge identity has to include the asserting record
because one item carried three roll calls; and Braddock changed hands inside the window
with no contest on disk, which is where the time bounds earned their keep — the
displaced holder's `valid_to` is inferred from the successor's first appearance and
says so. Meetings are not nodes (they're events); topic is Haiku's reading of the title
and is labelled as such.

*Slice two is Congress, same two tables, same predicates.* `build_congress` reads
`data/legislators-current.json` (every current member, every term, exact dates, bioguide
ids) and a roll-call snapshot that `python graph.py snapshot 119 2 2026` writes to
`data/congress-votes-119-2.json` from the House clerk's and the Senate's XML — the
loader never touches the network. `load us-congress --state VA` loads one delegation;
without `--state` it loads all 535. What it proved: the ontology really is
self-similar — a chamber is an organization, a district or Senate class is a post, a
bill is an instrument, and a county agenda item is the same kind — so a federal and a
county vote answer with the same words. Federal people are keyed by bioguide id; local
people by county + surname + first initial (never by seat, so a person who changes
seats stays one node; a surname-and-initial collision with a different given name
stays two and is reported). The hand-asserted bridge between the two is `identities`
in `foundry/data/store/_graph-sources.json`, and Walkinshaw (Braddock supervisor, then
VA-11) is one node with a `holds` edge into each layer.

*Certification is real now.* Loudoun is the third source: its meetings are certified
against the clerk's minutes, so a Loudoun answer crosses certified and ingested hops in
one list and names the weakest. Loudoun records motions rather than agenda items, so
the motion is the instrument. Federal `holds` are certified by the clerks' roll calls
— a member recorded voting inside the term affirms the term from a source independent
of the legislators file; all 13 Virginia terms with a 2026 vote are certified, the
earlier ones are not. The bounds keep their own precision either way: certification
says the term is real, not that its dates are.

*Time, across sources.* A person cannot hold two of these seats at once. After every
load, `close_holds_across` looks at each person's holds from every source loaded and
closes an open or inferred hold the day before an exact-started hold on another post
begins. That is how the county loader, which can only see a successor's first
meeting, learns that Walkinshaw left Braddock on 2025-09-09 rather than in January.
Both bounds stay tagged `inferred`.

*All four counties are in.* Prince William's roster is surnames only ("Gordy"), so a
bare surname is a name, and a contest winner ("Thomas T. \"Tom\" Gordy") matches it when
the surname is unique on that board; two members whose special elections are not on
disk vote but hold no seat, and the answer says so. Stafford is the honest thin case:
it staggers its terms, the 2023 results cover three of its seven seats, and its roster
carries three different Allens, so a vote by "Allen" is refused as ambiguous rather
than guessed — 249 positions dropped and counted, not silently assigned.

*`sponsored` is the ninth predicate.* The snapshot now asks Congress.gov for every
bill the session voted on: title, policy area, sponsor, and each cosponsor with the
date they signed (`CONGRESS_API_KEY`; without it the snapshot carries votes only and
the loader names the gap). Sponsorship is an instantaneous edge on that date, with
the role on it. Titles now come from the record, and the policy area is the
instrument's topic — Congress.gov's own subject, so a topic filter that matched it
is not flagged advisory; only Haiku's county topics are. Two more question shapes:
"who sponsored the Affordable HOMES Act" and "what did Kaine sponsor". Sponsors
outside the loaded delegation are not loaded, and only bills with a recorded vote
this session are on disk, so "what did X sponsor" is a floor, not a count.

*A county enters the graph by an entry in `_graph-sources.json`* — its OCD slugs, the
elections store that seats its members, the statutory term — not by editing code.
`python graph.py load all` loads every entry and every state the sidecar names for
Congress; the refresh workflow runs a snapshot of the current session's roll calls and
`load all` daily when the database secret is set.

*The graph answers typed questions* at `GET /api/graph/search?q=` and
`python graph.py ask "…"` (`--memory` builds every source in memory, no database). Same
contract as `fast_route`: a regex answers three shapes for $0 and returns "not mine"
otherwise, so a caller can fall through. The shapes are the three traversals that
exist — "how did Herrity vote on zoning", "who voted no on the Affordable HOMES Act",
"who held the Braddock seat on 2025-11-18" (a bare year means the end of it; no date
means today). A county or state name inside a topic is treated as scope and dropped —
"Fairfax zoning" searches "zoning" and the answer says what it ignored. `parse_question` and `answer` are pure; `memory_backend` runs the same
five lookups over the lists `build` returns that `pg_backend` runs as SQL, which is
the harness `tests/test_graph.py` asks its questions through.

`/ledger` routes into it: `classify_question` tries `graph.parse_question` before the
place and topic guesses, so "how did Herrity vote on zoning" streams a `graph` plate
instead of going to Congress. The graph declines every other shape, and the caller
falls back with `allow_graph=False` when it parses a shape but knows neither the
person nor the seat — so nothing the ledger answered before is lost.

*Next for the graph, in order:* a money predicate, because "who funded them" is the
other half of the mixed question and the legislators file already carries FEC ids.
Then the General Assembly, and the other 49 delegations (`load us-congress` with no
`--state`; ~300k `voted_on` rows a session, which is a Supabase size decision).

**7. Rebuild what `/newspaper` did.** See the next section — I deleted the old tabbed
app rather than porting it, so these are rebuilds in `ledger.js`, not migrations. The
backends all still work; only the views are gone.

Staying in Python. I looked hard at porting the backend to Rust and decided against it:
no official Anthropic SDK, no comfortable BeautifulSoup equivalent, the synthesized
extractors have to stay Python anyway, and nothing a user can see gets better. Same
answer for a Leptos frontend — a WASM compile costs me edit-and-reload, and the one real
prize, shared types with a Rust backend, doesn't exist if the backend is Python.

---

## Rebuilding the newspaper capabilities

I deleted `frontend/index.html`, `js/index.js`, `js/lobbying.js` and
`js/correspondence.js` — the old tabbed app at `/newspaper`. I'd rather rebuild these
properly in the ask SPA than port UI I didn't like.

**Every backend endpoint below still exists and works.** Nothing server-side was
removed. What's gone is the only UI that reached them, so each item is a view to build
in `ledger.js` against a known, working API. The old implementation is in git history
if I ever want to look.

### Offline until rebuilt

| # | Capability | Endpoints | Notes |
|---|---|---|---|
| 1 | **State legislation search** | `POST /search` with `state_code` | Needs the Federal/State toggle and 50-state picker, and `/ledger` must stop forcing federal (`api.py:1618`). Biggest gap — the whole state layer is behind this. |
| 2 | **State bill detail** | `POST /state/bill` | Streams like `/bill`. Needs a state variant of `billView`. |
| 3 | **State member lookup** | `POST /state/member/search` | `memberView` already exists; needs the state branch. |
| 4 | **Public law detail** | `POST /law` + `/law/{congress}/{number}` routes | Same generator as `/bill`; the old app just picked the endpoint off `is_law`. Small. |
| 5 | **Lobbying directory** | `GET /lobbying/search`, `GET /lobbying/entity` | Type-ahead over ~14k entities → entity profile: spend, issues, lobbyists, bills pushed. Per-bill lobbying already renders inside `billView`; this is the standalone directory. |
| 6 | **Stock explorer** | `GET /stocks/notable`, `/stocks/all`, `/api/stocks/traded`, `/api/stock/{ticker}/timeline` | Member finance and per-member trades already exist in `memberView`. This is the cross-member browse and the ticker timeline. |
| 7 | **Personalized feed** | `POST /feed` | "What's Moving." The scoring is pure and unit-tested (`tests/test_feed_rank.py`, 40 cases) — only the rendering is gone. The home page is currently just a search box. |
| 8 | **Structured feedback** | `POST /flag/search`, `POST /flag/bill` | The flag modal with canned reasons. `ledger.js` has only a `mailto:` link, so the loop into `analyst_agent` and `/monitor` is dead. Cheap to rebuild, and it's how I learn what's broken. |
| 9 | **Notifications page** | `GET /correspondence`, `/correspondence/{id}/replies`, `POST /correspondence/unsubscribe` | Subscribe already works in `ledger.js`; listing, replies and unsubscribe don't. |
| 10 | **Letter writing** | `POST /correspondence/draft`, `/send`, `/followup/draft` | Was already shipped disabled — no reliable rep email addresses. Rebuild only if that's solved. |
| 11 | **Onboarding flow** | — | Multi-step intro on the old front page. Probably don't rebuild; the ask box is the onboarding now. |

### Order I'd do them in

1 → 2 → 3 (the state layer, together — it's one coherent chunk and the largest loss)
7 → 8 (feed and flags — both cheap, both restore signal I use)
4 (public law — small)
5 → 6 (lobbying and stocks — the money layer)
9 (notifications)
10, 11 (only if the underlying problem changes)

### Still live, don't re-delete

`elections_shared.js` is used by `elections.html` and `election_detail.html`.
`d3-geo`, `d3-array` and `topojson-client` are used by `ledger.js` and `foundry.js`.
`leaflet.js` is used by `foundry.js`.

---

## Architecture

No framework, no bundler, no ORM, no build step. FastAPI serving vanilla HTML/CSS/JS.
Edit a file, hit save, reload the browser.

```
Ask (production)     POST /ledger → NDJSON stream
                     classify → plate → stream plate, member, shelves
                     frontend/test.html + js/ledger.js

Search               POST /search → JSON
                     fast_route → fast_route_state → route_query (Haiku)
                     the $0 regex paths answer first; the model is last resort

Bill detail          POST /bill, /law → NDJSON
                     ~10 sections computed concurrently, flushed as each finishes
                     so the page paints progressively

Foundry              discover (Sonnet) → synthesize (Opus) → gate (deterministic)
                     → certify against an independent oracle → refresh ($0)
                     certified and uncertified data are never interchangeable

Logging              every agent action → agent_log.json
                     searches + confidence → search_log.jsonl
                     user "this is wrong" → flags
                     all three feed the analyst at /monitor
```

Two routers exist and disagree with each other — `_resolve_routing` in `api.py` for
`/search`, and `classify_question` in `ledger_agent.py` for `/ledger`. Unifying them is
the first thing the graph work should do.

---

## The rules I hold myself to

These aren't preferences. When I break one I say so in the commit message.

1. **I should be able to fit this in my head.** No framework, no bundler, no ORM.
2. **Edit-and-reload is sacred.** Anything that adds a build step is the wrong tool,
   even if it's individually better.
3. **Plain data structures unless I can't.** Dicts for config, tuples for ordered
   priority, sets for dedup. A class earns its place at the third method, not the first.
4. **One dispatch entrypoint per concept.** One `/search`, one `/bill`. Not five variants.
5. **Comments explain why, never what.** Hidden invariants, upstream bugs, deliberate
   trade-offs. Never a restatement of the line below.
6. **Three similar lines beat a premature helper.** Extract at the third real use case.
7. **Delete cleanly.** No backwards-compat shims for code only this app calls.
8. **Fail-open vs fail-closed is a decision, made explicitly, per function.**
9. **Degrade gracefully.** Circuit breakers, fallback sources, stale-while-revalidate.
10. **Wall-clock budgets, not per-call timeouts.** One slow call must not burn the wait.
11. **Namespace every cache** (`search:v6:`) so a version bump invalidates without a purge.
12. **Honest empty states.** When there's nothing, say why. Never a silent zero.
13. **Validate at the boundary.** Pydantic at the perimeter, loose dicts inside.
14. **DOM is a render target, not state.** Module variables hold the truth. Clear stale
    DOM on context switch.
15. **Cache-bust CSS** with `?v=` whenever styles change.
16. **No new files unless unavoidable.** Existing files grow; the codebase doesn't sprawl.
    (`api.py` is past 3,700 lines and has genuinely earned a split — extracting the search
    dispatcher is overdue.)

And the ones I won't negotiate on: **no silent failures**, **every external call wrapped
in cache + timeout + fallback + breaker**, and **certified data is never mixed with
uncertified data**. Missing data is my gap, not the jurisdiction's, and the UI says so.

Visual design is a separate contract — see `Styleguide.md`. Short version: it should look
like a 1940s legal newspaper, never a SaaS dashboard. No border-radius, no box-shadow,
no purple.

---

## Stack

```
Backend       Python · FastAPI · uvicorn · slowapi rate limiting
Models        Haiku  — routing, expansion, validation, translation (~98% of calls)
              Sonnet — web search for bill background, election polling
              Opus   — Foundry extractor synthesis only
Federal       Congress.gov · GovInfo (BILLS + PLAW) · clerk.house.gov + senate.gov XML
State         LegiScan  (all 50 states)
Local         Foundry — my own synthesized extractors, 9 sources
Civic         Google Civic (elections) · Census geocoder (districts) · FEC · Senate LDA
Storage       Postgres on Supabase (psycopg3 pool) — subscriptions, mail, disk_cache
              Foundry stores are JSON on disk under foundry/data/store/
Email         Gmail OAuth for user letters · SMTP for system notifications
Frontend      Vanilla HTML/CSS/JS. Playfair Display · Source Serif 4 · IBM Plex Mono
Deploy        Railway, auto-deploy from GitHub
```

---

## Running it

```bash
pip install -r requirements.txt
uvicorn api:app --reload        # → http://localhost:8000
python -m unittest discover tests   # pure-logic tests, no network, no pytest
```

Required in `.env`:

```
ANTHROPIC_API_KEY       CONGRESS_API_KEY        GovInfo_API_KEY
LEGISCAN_API_KEY        GOOGLE_CIVIC_API_KEY    SUPABASE_DB_URL
```

Optional, per feature: `FEC_API_KEY` and `LDA_API_KEY` (money and lobbying),
`GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` (letter writing), `SMTP_*` and
`NOTIFY_FROM_EMAIL` and `WATCHER_SECRET` (notification emails), `JWT_SECRET` (auth),
`MONITOR_SECRET` (the `/monitor` console), and the `FOUNDRY_*` set (pipeline
budgets and switches).

```bash
python event_watcher.py         # daily bill-state watcher
python clear_search_cache.py    # --all to include feed/elections
```

---

## Tests

Two tiers, kept apart on purpose.

**Tier 1 — regression.** Free, deterministic, and the only thing CI gates on
(`.github/workflows/tests.yml`). 268 tests in ~3s with no network, no LLM and no
database: the pure-logic tests, plus golden fixtures over 56 of the 85 routes —
request → exact response JSON, captured once and committed under `tests/golden/`.

**Tier 2 — quality eval.** Measures whether search is any *good*, not whether it
changed. Costs money, opt-in, never in CI. Not built yet; `search_smoketest.py` is
the seed.

```bash
pytest tests/                        # Tier 1, the whole thing
python -m unittest discover tests    # same pure-logic tests, no pytest needed
python -m unittest tests.test_state_vote_mapper -v
```

`pytest` is in `requirements.txt` for the golden suite, which needs strict xfail and
parametrize. The eight pure-logic files are still stdlib `unittest` and run under
either runner.

### Replay, and why a green run is trustworthy

`tests/replay.py` makes `api` importable without a network, an LLM bill or a Postgres
connection, so whole responses can be diffed against fixtures. The shape is lifted from
`foundry/sandbox2.py`, including the property that matters most: **a boundary with no
fixture raises, it never falls through to a live call.** A socket guard backs that up, so
a missed seam is a named failure rather than a surprise invoice.

Note it blanks `SUPABASE_DB_URL` before importing `api` — `correspondence/router.py`
calls `init_db()` at import time, so without that, merely importing the app runs
`CREATE TABLE IF NOT EXISTS` against production.

```bash
NOSPOPULI_REPLAY=record python -m tests.replay record          # re-capture, live
NOSPOPULI_REPLAY=record python -m tests.replay record ledger   # only matching cases
python -m tests.replay list                                    # what's pinned
```

Re-capture only for an *intended* behaviour change, and read the fixture diff before
committing it — that diff is the whole point.

### What's covered

| File | What it guards | Why |
|---|---|---|
| `test_router_fast_paths.py` | `fast_route` (federal) + `fast_route_state` (state) regex paths | Highest-traffic query type; breaks silently turn every "HB 1234" into a slow LLM round-trip |
| `test_state_vote_mapper.py` | Committee-vs-floor vote disambiguation, participation threshold | Two real bugs caught in production this month (VA HB 191 committee tally, CA SB 1407 fake Assembly vote) |
| `test_parse_amends.py` | "To amend the X Act of YYYY" extraction in the Connections panel | Pure regex; if it silently degrades, every bill detail page loses its primary law reference |
| `test_foundry_health.py` | `foundry/health.py` — the scraper-health status vocabulary (`summarize`) and the ledger's bounds | The console at `/admin/foundry` reads nothing else; if `summarize` mislabels a source, the operator is told a scraper is healthy when it is not. Also pins the rule that a quarantine caused by publication lag is never reported as a failure |
| `test_endpoints.py` | 56 routes, request → exact response, via `tests/replay.py` | The oracle the suite didn't have. Turns "read api.py and reason about equivalence" into "make this JSON match", which is what makes the planned `search_dispatcher.py` extraction verifiable instead of hopeful |
| `test_ledger.py` | `classify_question`, the funnel, compact titles, shelves — and `KnownDefects` | The defects below are pinned here as strict expected-failures, so they're recorded without leaving the suite red. Includes the control that `classify_question` gets "LA County" *right*, which is what makes `/search` answering `off_topic` a bug rather than an opinion |
| `test_feed_rank.py` | Feed scoring and the interest/blocklist gates | 40 cases; the scoring is pure and the rendering is currently offline, so this is all that holds it |
| `test_search_rank.py` | `rank_by_relevance` determinism and order stability | Ranking is re-sorted by the validator downstream, so a silent change here is invisible in the UI |
| `test_graph.py` | `graph.py` — identity resolution, node/edge building, the as-of seat resolver, and the answer shape | Fixtures replay the real Fairfax defects (duplicate spelling, sentence fragment as a member, three roll calls on one item, a seat that changed hands with no contest on disk, a person who won two bodies' seats in one district). Pins that certification follows the asserting record, that `elected_in` is scoped to the seat and never to the surname, and that an empty answer always says why |

### What's NOT covered (and why)

- **Search *quality*** — nothing measures it. Tier 1 proves the answer didn't change,
  not that it was ever right; the fixtures happily pin a wrong answer, and two of them
  do exactly that on purpose. That's Tier 2's job.
- **The 29 routes not pinned** — mostly the ones needing a seeded database, a real
  secret, or a per-route request body worth hand-writing. Auth-gated routes are pinned
  at their refusal contract only; a fabricated 200 would be worse than nothing.
- **`/api/stocks/traded`** — deliberately not pinned. It looks pure but classifies 1,249
  tickers through Haiku in batches built from a set comprehension
  (`bill_market.py:150`), so the batches, and every cache key derived from them, differ
  per process. One cold request cost 44 Haiku calls while recording.
- **Foundry's own LLM clients** — ~10 separate seams. No route under test calls them
  synchronously; the four that touch foundry spawn threads and correctly refuse a
  TestClient host.
- **Frontend JS** — would need a separate JS test runner; out of scope for now.

### Adding a new test

1. Create `tests/test_<thing>.py`.
2. Add the `sys.path.insert` boilerplate at the top so imports resolve.
3. Subclass `unittest.TestCase` and write `test_*` methods.
4. Run `python -m unittest tests.test_<thing>` until it passes.
5. Update the table above.
6. If it's a *known defect* rather than a guarantee, mark it `@unittest.expectedFailure`
   (or `@pytest.mark.xfail(strict=True)`) and say which defect in the docstring. Both
   runners exit 1 on an unexpected success, so the day it gets fixed the test breaks and
   asks to be promoted to a plain assertion. A permanently red suite just teaches
   everyone to ignore red.

If your test caught a real bug in the production code, **leave a comment in the
test method explaining what the bug was** — that's the test's strongest
justification, and it makes regressions easier to diagnose.

### Philosophy

Tests are scaffolding for *change*, not proof of correctness. Write tests when:

- The logic is non-trivial enough that a future edit could silently break it.
- The logic has caught a real production bug — encode the bug as a test so it
  can't come back.
- The logic crosses 30+ lines or has multiple branches.

Don't write tests when:

- The code is a thin wrapper over a library call that's already tested upstream.
- The "test" would just restate the implementation in pseudo-natural language.
- The function takes no arguments and has no return value (you have nothing to
  assert against — re-shape the code instead).

A failing test is a gift. When this happens (it happened twice while writing
this initial batch, finding a year-validation bug in the state regex and a
weak-signal participation gap in the vote mapper), don't reflexively change the
test to match. Read the code, decide whether the test or the code is wrong, and
fix the right one. Often the test is correct and the code needs the fix.

---

## Known defects

Live problems I know about and haven't fixed. Listed so nobody has to rediscover them.

- `"McKay extended thoughts and prayers to the Fairfax family"` is stored as a board
  member in `foundry/data/store/fairfax-bos.json`. A sentence fragment parsed as a
  person. A gate floor should have caught it.
- `Pat Herrity` and `Patrick S. Herrity` are duplicate members in that same store.
- `topic:` subscriptions are written by the frontend but nothing on the server ever
  polls them. People can subscribe to a topic and will never hear anything.
- The watchlist regex is anchored, so "show me what I'm watching" misses and falls
  through to federal bill search.
- Local search discards the topic (above).
- `/ledger` forces every query to federal (`api.py:1618`), so state legislation search
  is unreachable from the home page despite being fully built for all 50 states.
- Braddock District resolves to nobody from 2025-09-10 to 2026-01-12. Walkinshaw's hold
  now closes the day before his federal term (right), but Sizemore Heizer's begins at
  her first observed meeting because the special election that seated her is not on
  disk. The gap is real and the answer says "vacant or not on disk"; the fix is the
  2025 special-election results in `va-elections.json`.

Found while building the golden fixtures, all four now pinned as expected-failures:

- **A state query with no LegiScan key is a silent zero.** `legiscan_client._call`
  logs to stdout and returns `None` before any HTTP (`legiscan_client.py:62`), and
  `search` turns that into `[]` — indistinguishable from "Virginia genuinely has no
  matching bills". Nothing in `api.py` consults `legiscan_client.has_key()`, which
  already exists. The shape to copy is the graph route's `empty_reason`
  (`api.py:3050`).
- **"Nothing in Virginia matched that ask."** is what `/ledger` answers for
  *Healthcare bills in Virginia* — after `api.py:1617-1621` rewrote the query to
  federal and searched Congress. It blames the jurisdiction for my own gap, which is
  the inverse of the rule I care most about. Captured in
  `tests/golden/post_ledger__healthcare_bills_in.json`.
- **Confidence is inverted, not merely useless.** Across the 73 logged searches,
  `conf=0.95` is 9-of-11 zero-result while `conf=0.6` and `0.75` are 0-for-3. The most
  confident bucket is the least correct one. `/search "LA County"` returns
  `off_topic` at 0.95 while `classify_question` resolves it to `lacounty-bos`.
- **`/api/member/{bioguide}` returns `chambers` in an unstable order.**
  `member_search_agent.py:165` builds it as a `set` and returns `list(chambers)` at
  `:186`, so the order tracks `PYTHONHASHSEED` — stable within a process, different
  between them. Harmless today, but it means the field cannot be pinned; the fixture
  compares it unordered and says so.

---

## Legal

Public law is not copyrightable in the United States. All legislative text displayed
here is in the public domain. The plain-English translations are original works created
by the system. This displays information only — it is not legal advice, and every page
says so.

---

*NosPopuli — Law for the People*
