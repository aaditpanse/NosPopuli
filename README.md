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
Embeddings    VoyageAI voyage-law-2 → pgvector  (ingest built, query layer not wired)
Email         Gmail OAuth for user letters · SMTP for system notifications
Frontend      Vanilla HTML/CSS/JS. Playfair Display · Source Serif 4 · IBM Plex Mono
Deploy        Railway, auto-deploy from GitHub
```

---

## Running it

```bash
pip install -r requirements.txt
uvicorn api:app --reload        # → http://localhost:8000
pytest tests/                   # pure-logic tests, no network
```

Required in `.env`:

```
ANTHROPIC_API_KEY       CONGRESS_API_KEY        GovInfo_API_KEY
LEGISCAN_API_KEY        GOOGLE_CIVIC_API_KEY    SUPABASE_DB_URL
```

Optional, per feature: `FEC_API_KEY` and `LDA_API_KEY` (money and lobbying),
`GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` (letter writing), `SMTP_*` and
`NOTIFY_FROM_EMAIL` and `WATCHER_SECRET` (notification emails), `JWT_SECRET` (auth),
`MONITOR_SECRET` (the `/monitor` console), `VOYAGE_API_KEY` and `SUPABASE_URL`
(the embedding pipeline), and the `FOUNDRY_*` set (pipeline budgets and switches).

```bash
python event_watcher.py         # daily bill-state watcher
python clear_search_cache.py    # --all to include feed/elections
python ingest_bills.py          # offline: populate pgvector
```

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
- `/newspaper` is orphaned — nothing links to it — but is the only route to state
  search, the lobbying directory, the stock explorer, and `/law/*` and `/state/*` deep
  links. Don't delete it before those are ported.
- `watch_agents.py` references an undefined `log` in its loop, swallowed by a bare
  `except`.

---

## Legal

Public law is not copyrightable in the United States. All legislative text displayed
here is in the public domain. The plain-English translations are original works created
by the system. This displays information only — it is not legal advice, and every page
says so.

---

*NosPopuli — Law for the People*
