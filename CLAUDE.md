# CLAUDE.md

Read this before anything else in the repo. If another file contradicts it, this wins
and the other file is a bug.

## What I'm building

NosPopuli makes American government legible — federal, state, and local. Not a
summarizer or a news feed: a map of who governs you, what they decided, and what it
cost, that you can walk.

Full picture in `README.md`. Visual rules in `Styleguide.md`. Foundry has its own
`foundry/README.md`. **Those three plus this file are the only documentation. There is
no `docs/` directory — I deleted it, deliberately, because a pile of stale pre-build
specs was giving agents a confident and wrong picture of the project.** Don't
reconstruct one. If something needs writing down, it goes in one of the four.

## Layout

Seven packages, `api.py` and `graph.py` at the root. Namespace packages — there
are no `__init__.py` files, because empty files that exist only to satisfy an
import system are not worth the tree.

```
api.py          the server: every route, the streaming, the routing seams
graph.py        the property graph: build, load, traverse

agents/         the LLM agents — router, ledger, search, feed, translator,
                the vote trio, documentor. If it prompts a model, it lives here
sources/        external data in: congress.gov, GovInfo bulk, Voteview, district
                shapes, nominations, Open States, Virginia LIS, FEC (API and bulk), Senate LDA,
                House disclosures. One module per upstream, plus the shared session
search/         the search plumbing that is not an agent: the bill index (full
                text + Voyage vectors), cache, logger, rank, and the user-flag log
resolvers/      location → jurisdiction: zip, address, point, district
money/          the money layer: bill↔market, stock performance, industry
                classification, the public-law corpus
render/         turning records into markup: bill typescript, vote semicircles
scripts/        run by hand or by cron, never imported by a route except
                event_watcher, which /watcher/run triggers
```

Run a script with `-m`, not by path — `python -m scripts.clear_search_cache`.
By path, `sys.path[0]` becomes `scripts/` and every local import fails.

## Things that will mislead you

- **`frontend/test.html` is production.** It's served at `/`. The name is historical.
  Treat edits to it as production changes.
- **One frontend now:** `test.html` + `js/ledger.js`, served at `/`. The old tabbed app
  (`index.html`, `index.js`, `lobbying.js`, `correspondence.js`) is deleted. Several
  capabilities went offline with it — state search, lobbying, stocks, public law, the
  feed, flags. **Their endpoints still work.** Before concluding an endpoint is dead or
  a feature was never built, read "Rebuilding the newspaper capabilities" in
  `README.md`; it lists each one with its endpoints.
- **State legislation is Open States plus Virginia LIS, not LegiScan.** LegiScan never
  issued a key and its code is deleted. The record is Open States' monthly dump and
  people repo (`sources/openstates.py`); Virginia's own daily files (`sources/lis.py`)
  certify holds, downgrade disagreeing votes to advisory and show newer actions as
  advisory rows, and never write the record. A state is loaded by an entry under
  `"legislatures"` in `foundry/data/store/_graph-sources.json`; only Virginia has one.
  Any other state answers "not loaded on this server".
- **`/ledger` sends a state question to the state layer only when the router says it is
  one.** The reader's home state alone does not: "housing bills" from a Virginian
  still searches Congress.
- **Routing is two layers.** `classify_question` in `agents/ledger_agent.py` is the $0
  regex layer (watch, graph, elections, place, bill ID, local). `structure_question` in
  `agents/router_agent.py` is the second layer (state and federal fast paths, then the
  LLM). `/ledger` runs both; `/search` runs only the second, so it still calls "LA
  County" off-topic. Don't polish either layer: the graph replaces both.
- **Lots of capability is built but unreachable by typing.** Member finance, stock
  trades, lobbying, the geo resolvers — all live endpoints, all click-only. "It doesn't
  work" usually means "nothing routes to it."

## Where it's heading

Away from classifying a question into one bucket, and toward a **temporal property graph
with provenance** that gets traversed. Classification is a projection and it discards
the half of a mixed question that made it interesting. v1 of the graph is Virginia top
to bottom plus the whole federal layer: every member since 1789, the presidency,
committees, the current Congress's votes, bills, laws and campaign committees. The
skeleton is small enough for two Postgres tables — no graph database. Every bill since
2003 is in it now, and the pages read the bulk files; README "What's next" has the
detail.

**What the map is for:** questions whose answers require crossing layers of government
— how the person I elected actually voted — become a walk across the map, with every
step able to show its source. Specificity comes from the **joins**, not from volume;
accuracy comes from **certification**, not from mapping. Anticipating the questions is
explicitly not the goal: the entity and edge types are finite, their combinations are
not. Model the domain, don't enumerate the asks. Longer version in `README.md` under
"What the map is for".

## Where it runs

One Hetzner server (`nospopuli-1`) behind Cloudflare: the app, Postgres 17, and the
federal bulk data in `NOSPOPULI_DATA_DIR` (`/srv/bulk`). Every dataset is named once in
`graph.DATASETS`; read and write through `graph.data_path`. README "Running it" has the
paths and the systemd units.

- **Pushing to `main` deploys.** The server pulls `origin/main` every five minutes.
- **The daily sync (11:30 UTC) commits to `main` too** — the tracked data files — so
  pull before you push.
- **A laptop has no database.** The code reads `DATABASE_URL`; a `.env` that still
  says `SUPABASE_DB_URL` gets none, which is deliberate. Graph loads and bulk syncs run
  on the server, as the `nospopuli` user with `/etc/nospopuli/env`.
- **The bill routes answer from files from the 108th Congress (2003) on**, with no
  live fallback; before it they still ask Congress.gov, and each such call says so.

## Rules

Full list in `README.md`. The ones most often broken:

- No framework, no bundler, no ORM, no build step. Edit-and-reload is sacred.
- Plain data structures. A class earns its place at the third method.
- Three similar lines beat a premature helper.
- Comments explain **why**, never what. No TODOs without a date and a condition.
- Fail-open vs fail-closed is an explicit per-function decision.
- Honest empty states. Never a silent zero — say why it's empty.
- No new files unless unavoidable. Existing files grow.
- Namespace caches (`search:v6:`) so a version bump invalidates cleanly.
- If you break a rule, say which one and why in the commit message.

**The one that matters most:** certified and uncertified data are never interchangeable.
A record no independent source affirms is ingested, never published, and the UI says so.
Missing data is framed as my gap, not the jurisdiction's. Don't "helpfully" fill a hole
with something adjacent — answering a Los Angeles County question with Los Angeles City
data is worse than answering nothing.

## Testing

Two tiers. Keep them apart: one is free and gates every change, the other costs money
and answers a different question. Conflating them is how a suite becomes something
people skip.

**Tier 1 — regression. Did behaviour change?** `pytest tests/ -q` — ~300 tests, ~3s,
no network, no LLM, no Postgres. Pure-logic tests plus golden fixtures over the route
surface: request → exact response JSON, committed under `tests/golden/`. This is the
only gate (`.github/workflows/tests.yml`). It cannot tell you an answer is *right*,
only that it is unchanged — two fixtures pin a wrong answer deliberately.

**Tier 2 — quality eval. Is search any good?** Costs money, opt-in, never in CI.
`search_smoketest.py` is the seed. Not built yet.

### The loop

Narrowest thing first; the full suite before you claim done.

```bash
pytest tests/ -q -k ledger          # while iterating
pytest tests/ -q                    # before declaring anything finished
python -m unittest discover tests   # same pure-logic tests, no pytest needed
```

Three seconds is cheap enough that there is never a reason to skip it. Run it *before*
you start editing too — if it is already red, that is not yours and you should say so
rather than absorb it.

### Non-negotiables

- **Never change application code to make a test pass.** A failing test is a finding.
  Establish which of the two is wrong before touching either, and say which.
- **Never delete or loosen a test to get green.** If it is a real defect, pin it (below).
- **A replay miss must raise.** `tests/replay.py` never falls through to a live call.
  Do not add a network fallback, do not widen a seam to "just work" — a suite that can
  reach the internet is a suite that can be green for the wrong reason.
- **Never point the harness at a real database.** `replay.py` blanks `DATABASE_URL`
  before importing `api` because `correspondence/router.py` runs `init_db()` at import.
  Undoing that runs DDL against production.
- **No secrets in CI.** If a test needs one, the seam it should replay through is
  missing. That is the bug.
- **Don't hand-edit fixtures.** They are captured output. Editing one by hand turns the
  oracle into an opinion.

### Pinning a known defect

Defects are recorded, never left red and never swept away. Mark them
`@pytest.mark.xfail(strict=True)`, or `@unittest.expectedFailure` in the stdlib files,
and name the defect and its file:line in the docstring.

Both runners exit 1 on an *unexpected success*. So the day the defect is fixed, its test
breaks and tells you to promote it to a plain assertion — which is the point. A
permanently red suite just teaches everyone to ignore red.

Add it to the README's known-defects list in the same change.

### Where a test goes

No new test files (rule 16). The homes already exist:

- Pure logic → the matching `tests/test_<area>.py`. These declare no HTTP, no LLM, no
  database; keep that true.
- Route behaviour → a fixture, via the recorder. **A new route without a fixture is
  unverifiable**, so add one in the same change.
- Anything needing the app object → `tests/test_endpoints.py`.

### Re-recording fixtures

```bash
NOSPOPULI_REPLAY=record python -m tests.replay record <substring>   # subset
python -m tests.replay list                                         # what's pinned
```

Only for an **intended** behaviour change, and read the resulting diff before committing
it — that diff is the entire value of the fixture. Re-recording to turn a test green
launders whatever the app does today into the expectation and destroys the oracle.

Record mode makes live calls and spends real money. Never use it to debug; use `-k` and
the failure message, which names the JSON path that moved.

### What not to pin

Some things cannot be honestly frozen, and pretending otherwise produces flaky tests
that get muted:

- **Order the app does not define.** A list built from a `set` varies with
  `PYTHONHASHSEED` — stable within a process, different between them. Compare it
  unordered (`replay.UNORDERED`) and say why.
- **Payloads too large to review.** Above 256 KB, pin the shape, not the bytes. A
  fixture nobody can read in a diff is not doing its job.
- **Anything auth-gated you cannot authenticate as.** Pin the refusal contract only. An
  honest partial fixture beats a fabricated 200 — the empty-states rule applies to
  fixtures too.

## Working here

- `pytest tests/ -q` — the whole suite, ~3s, free. Protocol in **Testing** above.
- `uvicorn api:app --reload` — that's the whole dev loop.
- Known live defects are listed at the bottom of `README.md`. Check there before
  reporting a bug as new.
- Ask me before adding a dependency, a file, or a layer of indirection.
