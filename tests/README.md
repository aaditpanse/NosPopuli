# tests/

Unit tests for the pure-logic parts of NosPopuli. No HTTP, no LLM mocking, no
database — just deterministic functions that can regress silently if a future
change breaks them.

## Running

```bash
# All tests
python -m unittest discover tests -v

# A single test file
python -m unittest tests.test_state_vote_mapper -v

# A single test class or method
python -m unittest tests.test_state_vote_mapper.NoFloorVoteCA -v
python -m unittest tests.test_router_fast_paths.StateFastRoute.test_year_anchor_from -v
```

No `pytest` install required — these use stdlib `unittest` only.

## What's covered

| File | What it guards | Why |
|---|---|---|
| `test_router_fast_paths.py` | `fast_route` (federal) + `fast_route_state` (state) regex paths | Highest-traffic query type; breaks silently turn every "HB 1234" into a slow LLM round-trip |
| `test_state_vote_mapper.py` | Committee-vs-floor vote disambiguation, participation threshold | Two real bugs caught in production this month (VA HB 191 committee tally, CA SB 1407 fake Assembly vote) |
| `test_parse_amends.py` | "To amend the X Act of YYYY" extraction in the Connections panel | Pure regex; if it silently degrades, every bill detail page loses its primary law reference |
| `test_foundry_health.py` | `foundry/health.py` — the scraper-health status vocabulary (`summarize`) and the ledger's bounds | The console at `/admin/foundry` reads nothing else; if `summarize` mislabels a source, the operator is told a scraper is healthy when it is not. Also pins the rule that a quarantine caused by publication lag is never reported as a failure |

## What's NOT covered (and why)

- **LLM-dependent code** (router LLM path, validator scoring, translator) — needs
  mocking that's its own design decision. The `search_smoketest.py` script in
  the repo root covers end-to-end LLM behavior against a running server.
- **HTTP-touching code** — same. `search_smoketest.py` is the end-to-end harness.
- **Frontend JS** — would need a separate JS test runner; out of scope for now.
- **Database / cache code** — would need fixtures + cleanup. Skipping until
  state-pollution bugs show up to motivate it.

## Adding a new test

1. Create `tests/test_<thing>.py`.
2. Add the `sys.path.insert` boilerplate at the top so imports resolve.
3. Subclass `unittest.TestCase` and write `test_*` methods.
4. Run `python -m unittest tests.test_<thing>` until it passes.
5. Update the table above.

If your test caught a real bug in the production code, **leave a comment in the
test method explaining what the bug was** — that's the test's strongest
justification, and it makes regressions easier to diagnose.

## Philosophy

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
