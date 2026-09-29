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


def test_ledger_survives_a_router_failure(app, monkeypatch):
    """router_agent.route is fail-open on /ledger: a model error leaves the
    question unstructured and the page an empty ledger, not a 500."""
    import json
    from agents import router_agent
    _api, client, _ = app

    def down(*a, **k):
        raise RuntimeError("model down")
    monkeypatch.setattr(router_agent, "structure_question", down)
    r = client.post("/ledger", json={"question": "voting rights bills"})
    assert r.status_code == 200
    plate = json.loads(r.text.splitlines()[0])
    assert (plate["plate"], plate["stories"]) == ("ledger", [])


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


def test_no_api_key_is_committed():
    """efce83a committed three live API keys inside replay cache keys, in a
    public repo. `replay._redact` strips them before a key is written; this
    fails if any fixture carries one again."""
    leaks = [p.relative_to(replay.GOLDEN).as_posix()
             for p in replay.GOLDEN.rglob("*.json") if "api_key" in p.read_text()]
    assert not leaks, f"api_key present in: {leaks}"


def test_recording_uses_only_a_local_fixture_database(monkeypatch):
    """The record-mode database is the one fence around "never point the
    harness at a real database": on this machine, named *_fixture, or
    nothing; a URL that breaks the rule raises instead of falling back."""
    monkeypatch.delenv("NOSPOPULI_RECORD_DB_URL", raising=False)
    assert replay.record_db_url() == ""
    for ok in ("postgresql://me@localhost:5432/nospopuli_fixture",
               "postgresql:///nospopuli_fixture?host=/var/run/postgresql"):
        monkeypatch.setenv("NOSPOPULI_RECORD_DB_URL", ok)
        assert replay.record_db_url() == ok
    for bad in ("postgresql://me@db.example.com/nospopuli_fixture",
                "postgresql:///nospopuli_fixture?host=db.example.com",
                "postgresql://me@localhost/nospopuli",
                "postgresql:///nospopuli?host=/var/run/postgresql",
                "postgresql://me@localhost/fixture_nospopuli"):
        monkeypatch.setenv("NOSPOPULI_RECORD_DB_URL", bad)
        with pytest.raises(RuntimeError):
            replay.record_db_url()


# ------------------------------- defects that need a fixture or the app itself
# The pure-logic defects (Radnor, the watchlist anchor) and the LA County control
# live in test_ledger.py::KnownDefects, with the other pure ledger_agent tests.
# The Virginia classification defect is fixed; its test is in ClassifyTests.
# Only the two that need a captured response or the api.py source belong here.

def test_virginia_answer_does_not_blame_virginia():
    """Promoted 2026-09-27 (state layer, step 15). Until then the federal
    override in `ledger_ask` rewrote *Healthcare bills in Virginia* to
    federal, searched Congress, found nothing, and answered "Nothing in
    Virginia matched that ask." — my gap blamed on Virginia, and federal
    House bills (hr10293 and others) offered as the answer to a state
    question.

    Now a state ask is searched in the state's own bills, and an empty
    answer must still not blame the jurisdiction.
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


def test_state_search_says_why_it_is_empty():
    """Replaces a LegiScan-era xfail (retired 2026-09-27 with the LegiScan
    code). A missing key used to come back as an ordinary zero, the same
    answer as "Virginia has no such bills". The state layer now reads files
    on this server, so the empty that can happen is a state not loaded, and
    it must say so rather than look like a real zero."""
    def body(name):
        return json.loads((replay.GOLDEN / f"{name}.json").read_text())["response"]["json"]

    tx = body("post_state_search__school_vouchers")
    assert tx["results"] == [] and tx["empty_reason"] == "state_not_loaded"
    assert "not loaded on this server" in tx["ambiguity_reason"]
    va = body("post_state_search__healthcare")
    assert va["results"], "a loaded state's topic search returns its own bills"
    assert all(r["jurisdiction"] == "ocd-division/country:us/state:va" for r in va["results"])
