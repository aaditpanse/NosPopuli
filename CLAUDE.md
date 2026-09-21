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

## Things that will mislead you

- **`frontend/test.html` is production.** It's served at `/`. The name is historical.
  Treat edits to it as production changes.
- **One frontend now:** `test.html` + `js/ledger.js`, served at `/`. The old tabbed app
  (`index.html`, `index.js`, `lobbying.js`, `correspondence.js`) is deleted. Several
  capabilities went offline with it — state search, lobbying, stocks, public law, the
  feed, flags. **Their endpoints still work.** Before concluding an endpoint is dead or
  a feature was never built, read "Rebuilding the newspaper capabilities" in
  `README.md`; it lists each one with its endpoints.
- **`/ledger` forces every query to federal** (`api.py:1618`) unless it hits the
  state-bill-ID fast path. The state layer works for all 50 states and the home page
  cannot reach it. "Virginia housing bills" sets the place, then searches Congress.
- **State legislation is LegiScan, not OpenStates.** Any comment or identifier
  suggesting otherwise is stale. `grep -rl openstates *.py` is empty.
- **Two routers exist and disagree.** `_resolve_routing` in `api.py` serves `/search`;
  `classify_question` in `ledger_agent.py` serves `/ledger`. Each has fast paths the
  other lacks. Unifying them is planned work, not an accident to paper over.
- **Lots of capability is built but unreachable by typing.** Member finance, stock
  trades, lobbying, the geo resolvers — all live endpoints, all click-only. "It doesn't
  work" usually means "nothing routes to it."

## Where it's heading

Away from classifying a question into one bucket, and toward a **temporal property graph
with provenance** that gets traversed. Classification is a projection and it discards
the half of a mixed question that made it interesting. v1 of the graph is Virginia top
to bottom. The skeleton is small enough for two Postgres tables — no graph database.

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

## Working here

- `pytest tests/` — pure-logic tests, no network, no LLM. Keep them green.
- `uvicorn api:app --reload` — that's the whole dev loop.
- Known live defects are listed at the bottom of `README.md`. Check there before
  reporting a bug as new.
- Ask me before adding a dependency, a file, or a layer of indirection.
