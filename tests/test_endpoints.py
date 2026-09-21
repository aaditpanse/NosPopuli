"""Golden-fixture tests over the HTTP surface — the oracle the suite lacked.

Everything here replays from `tests/golden/`: no network, no LLM, no Postgres.
The point is to turn "read api.py and reason about equivalence" into "make this
JSON match", so the `search_dispatcher.py` extraction out of api.py can be
verified instead of eyeballed.

Fixtures are captured once with `python -m tests.replay record` and committed.
A boundary with no fixture raises ReplayMiss rather than reaching the real
service, so a green run here cannot be secretly spending money.

Known defects are xfail(strict=True), not red tests. That records the bug, keeps
the suite exit-0, and — the actual point — fails loudly the day someone fixes
it, prompting promotion to a plain assertion.
"""

import json
import os
import sys
import pathlib

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from tests import replay  # noqa: E402


@pytest.fixture(scope="module")
def app():
    """Built once: `import api` is slow and `api.limiter` is a module global."""
    mp = pytest.MonkeyPatch()
    api, client, caches = replay.build_app(mp)
    yield api, client, caches
    mp.undo()


# ------------------------------------------------------------ the golden set

FIXTURES = replay.load_fixtures()


@pytest.mark.skipif(not FIXTURES, reason="no fixtures captured yet — run: "
                                        "NOSPOPULI_REPLAY=record python -m tests.replay record")
@pytest.mark.parametrize("name,fx", FIXTURES, ids=[n for n, _ in FIXTURES])
def test_golden(app, name, fx):
    _api, client, _caches = app
    req, want, meta = fx["request"], fx["response"], fx.get("meta", {})

    replay.clear_memory_caches()      # same path as the recorder took
    with replay.no_sockets():
        got = client.request(req["method"], req["path"],
                             json=req.get("body"), params=req.get("params"),
                             headers=req.get("headers"))

    assert got.status_code == want["status"], (
        f"{name}: status {want['status']} -> {got.status_code}\n{got.text[:400]}")

    volatile = meta.get("volatile", [])
    if "ndjson" in want:
        # Line order is contract: the frontend paints progressively, so a
        # reordering is a real regression, not a cosmetic one.
        lines = [json.loads(l) for l in got.text.splitlines() if l.strip()]
        diff = replay.golden_diff(lines, want["ndjson"], volatile)
    elif "shape" in want:
        # Payload too large to commit; its structure is pinned instead. See
        # replay.shape — a vanished key or an emptied list still fails.
        diff = replay.golden_diff(replay.shape(got.json()), want["shape"], volatile)
    elif "json" in want:
        diff = replay.golden_diff(got.json(), want["json"], volatile)
    else:
        diff = replay.golden_diff({"text": got.text[:2000]},
                                  {"text": want.get("text", "")[:2000]}, volatile)

    assert not diff, f"{name} drifted from its fixture:\n  " + "\n  ".join(diff)


# --------------------------------------------------- properties, not payloads

def test_health_is_free(app):
    """The one endpoint with no filesystem or network dependency at all."""
    _api, client, _ = app
    with replay.no_sockets():
        r = client.get("/health")
    assert r.status_code == 200


def test_catch_all_does_not_mask_typos(app):
    """`GET /{full_path:path}` (api.py:3697) is registered last and answers any
    unmatched path with 200 HTML. Assert content-type, or a typo'd test URL
    silently passes as a page."""
    _api, client, _ = app
    with replay.no_sockets():
        r = client.get("/definitely-not-a-route-zzz")
    assert "text/html" in r.headers.get("content-type", ""), (
        "catch-all stopped serving HTML — a route test asserting only on status "
        "would now be meaningless")


def test_streams_are_not_gzipped(app):
    """GZipMiddleware is on at api.py:141 (minimum_size=512) and STREAM_HEADERS
    (api.py:1736) exists solely to opt the 6 NDJSON routes out. If that opt-out
    regresses, progressive painting breaks and nothing else would notice."""
    _api, _client, _ = app
    import api as api_mod
    assert api_mod.STREAM_HEADERS.get("Content-Encoding") == "identity", (
        "STREAM_HEADERS no longer pins identity encoding")


def test_foundry_routes_refuse_testclient(app):
    """The 4 thread-spawning foundry routes are safe here by accident:
    TestClient's host is `testclient`, so the localhost bypass at api.py:3429
    does not fire. Pin it — if that bypass widens, a test run starts spending
    Opus money in a daemon thread."""
    _api, client, _ = app
    with replay.no_sockets():
        r = client.post("/api/foundry/onboard", json={"name": "replay-probe"})
    assert r.status_code in (401, 403), (
        f"foundry onboard accepted a TestClient request ({r.status_code}) — it "
        "would spawn a real synthesis thread")


def test_replay_miss_is_loud(app):
    """The property the whole tier rests on: an uncovered boundary must raise,
    never fall through to the live service."""
    _api, _client, caches = app
    with pytest.raises(replay.ReplayMiss):
        caches["llm"].fetch("llm nonexistent-key", lambda: None)


# ------------------------------- defects that need a fixture or the app itself
# The four pure-logic defects (LA County, Radnor, the watchlist anchor, the
# Virginia classification) live in test_ledger.py::KnownDefects, with the other
# pure ledger_agent tests. Only the two that need a captured response or the
# api.py source belong here.

@pytest.mark.xfail(strict=True, reason="api.py:1617-1621 rewrites the query to "
                                       "federal, so a Virginia ask is answered "
                                       "out of Congress and the empty state "
                                       "blames Virginia for my missing data")
def test_virginia_answer_does_not_blame_virginia():
    """Two defects in one captured response, neither of which "must not return
    zero" would have caught.

    The fixture (post_ledger__healthcare_bills_in.json) records
    `query_type: "legislation"` — rewritten from `state_legislation` at
    api.py:1617-1621 — alongside `state_code: "VA"`, and the headline
    "Nothing in Virginia matched that ask."

    So: it searched Congress, found nothing there, and reported that as
    Virginia having nothing. CLAUDE.md is explicit that missing data is framed
    as my gap, not the jurisdiction's, and this is the inverse. In the log's
    other six runs the same query returned hr10293/hr10287/hr10280 — federal
    House bills presented as the answer to a state question.
    """
    fx = json.loads((replay.GOLDEN / "post_ledger__healthcare_bills_in.json").read_text())
    plate = next(l for l in fx["response"]["ndjson"] if l.get("section") == "plate")

    assert plate["state_code"] == "VA"                      # the place survives
    assert plate["query_type"] != "legislation", (
        "state_legislation was rewritten to federal legislation, so the state "
        "layer was never searched")
    if not plate["stories"]:
        assert "in Virginia matched" not in plate["headline"], (
            f"blames the jurisdiction for my own gap: {plate['headline']!r}")


@pytest.mark.xfail(strict=True, reason="nothing in api.py consults "
                                       "legiscan.has_key(), so a missing key is "
                                       "served as an ordinary zero-result")
def test_state_search_says_why_it_is_empty():
    """`legiscan_client.search` returns [] whether the state genuinely has no
    matching bills or my API key is absent (`legiscan_client.py:62-63` logs to
    stdout and returns None before any HTTP). Those are not the same answer, and
    rule 12 says the empty one must say which: a missing key is my gap, not
    Virginia's.

    `has_key()` already exists at legiscan_client.py:55 and no caller uses it.
    The shape to copy is the graph route's, api.py:3050, which returns an
    explicit `empty_reason` when its backing store is unavailable.
    """
    from sources import legiscan_client
    import api as api_mod
    import inspect

    assert not legiscan_client.has_key(), (
        "a LegiScan key is configured now — re-pin this against a key-less run")
    # No network: _call short-circuits on the missing key before any request.
    assert legiscan_client.search("VA", "healthcare") == []
    assert "has_key" in inspect.getsource(api_mod), (
        "the state search path never asks whether a key exists, so it cannot "
        "tell a real zero from an unconfigured one")
