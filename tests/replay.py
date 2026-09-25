"""Hermetic replay harness for the HTTP surface.

The 197 pure-logic tests never import `api`, so nothing verifies a route. This
module makes `api` importable without a network, an LLM bill, or a Postgres
connection, so `test_endpoints.py` can compare whole responses against
committed fixtures.

The shape is lifted from `foundry/sandbox2.py`, which already solved this for
extractor gate runs. Three properties are carried over deliberately:

  - The cache key is the literal request string, not a hash, so a committed
    fixture is greppable and a diff is reviewable. (`search_cache.py` hashes
    instead, because nobody reads a production cache.)
  - A miss in replay mode raises. It never falls through to a live call, so a
    suite that looks green cannot secretly be spending money.
  - Reads hand back a deep copy, so a handler that mutates what it was given
    can't poison a later test in the same process.

Modes, via NOSPOPULI_REPLAY: "strict" (the default, and what CI runs) or
"record" (write-through against live credentials).

Recording is a subcommand of this file rather than a separate script: the
recorder and the player have to share `_key`, or they drift apart and the
fixtures rot silently.

    python tests/replay.py record         # capture fixtures + caches
    python tests/replay.py list           # what's captured

Out of scope on purpose: foundry's ~10 own Anthropic clients. No route under
test calls them synchronously — the four that touch foundry spawn threads and
403 under TestClient (see `test_endpoints.py`).
"""

import contextlib
import json
import os
import pathlib
import socket
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
GOLDEN = ROOT / "tests" / "golden"
REPLAY = GOLDEN / "_replay"
STORE = GOLDEN / "_store"          # frozen foundry/data/store subset — see _install
TODAY = "2026-09-24"               # the day _store was copied

MODE = os.getenv("NOSPOPULI_REPLAY", "strict").lower()
RECORDING = MODE == "record"


class ReplayMiss(RuntimeError):
    """A boundary was reached that no fixture covers.

    Deliberately loud. The alternative — falling through to the real call — is
    how a hermetic suite quietly stops being one.
    """


# ---------------------------------------------------------------- the cache

class Cache:
    """One JSON file, `key -> recorded value`. Flat and human-readable."""

    def __init__(self, name):
        self.path = REPLAY / f"{name}.json"
        self.entries = json.loads(self.path.read_text()) if self.path.exists() else {}
        self.misses = []
        self.hits = 0

    def fetch(self, key, produce):
        if key in self.entries:
            self.hits += 1
            return json.loads(json.dumps(self.entries[key]))
        if not RECORDING:
            self.misses.append(key)
            raise ReplayMiss(f"no fixture for: {key}")
        self.entries[key] = produce()
        return json.loads(json.dumps(self.entries[key]))

    def save(self):
        # Written even when a capture run crashes part way, so the fetches that
        # did land aren't thrown away — same reason sandbox2 writes in `finally`.
        if not RECORDING:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.entries, indent=1, sort_keys=True, default=str))


def _key(*parts):
    """Stable, readable key. Dicts are sorted so ordering can't split a key."""
    out = []
    for p in parts:
        if isinstance(p, (dict, list)):
            out.append(json.dumps(p, sort_keys=True, default=str))
        else:
            out.append(str(p))
    return " ".join(out)


# ------------------------------------------------------- the fake LLM client

class _Block:
    def __init__(self, d):
        self.type = d.get("type", "text")
        self.text = d.get("text", "")


class _Message:
    def __init__(self, blocks):
        self.content = [_Block(b) for b in blocks]
        self.stop_reason = "end_turn"


class _Messages:
    """Every agent reads `message.content[0].text`, so that is all we rebuild."""

    def __init__(self, cache, live):
        self._cache, self._live = cache, live

    def create(self, **kw):
        key = _key("llm", kw.get("model"), kw.get("max_tokens"), kw.get("temperature"),
                   kw.get("system", ""), kw.get("messages", []), kw.get("tools", []))

        def produce():
            msg = self._live().messages.create(**kw)
            return [{"type": getattr(b, "type", "text"), "text": getattr(b, "text", "")}
                    for b in msg.content]

        return _Message(self._cache.fetch(key, produce))


class ReplayAnthropic:
    def __init__(self, cache):
        self._cache = cache
        self.messages = _Messages(cache, self._real)

    def _real(self):
        import anthropic
        return anthropic.Anthropic(api_key=os.environ["NOSPOPULI_REAL_ANTHROPIC_KEY"])


# ----------------------------------------------------------- the fake cursor

class _Cursor:
    """Enough psycopg surface for `db.py`'s queries: execute + fetch.

    Keyed on the SQL text and its parameters, which is coarse but honest — a
    changed query is a cache miss and says so, rather than silently replaying
    the old rows.
    """

    def __init__(self, cache, live):
        self._cache, self._live, self._rows = cache, live, []
        self.rowcount = 0

    def execute(self, sql, params=None):
        flat = " ".join(str(sql).split())

        # Writes are not replayed. `set_disk_cache` passes time.time() as a
        # parameter (db.py:563), so keying a write would produce a brand-new key
        # on every run and therefore a guaranteed miss — which surfaced as a 502
        # on /lobbying/search. A write against a stubbed store has no observable
        # effect worth pinning, so it is accepted and dropped.
        if not flat[:6].upper().startswith(("SELECT", "WITH")):
            self._rows, self.rowcount = [], 0
            return

        key = _key("sql", flat, params or [])

        def produce():
            try:
                with self._live() as cur:
                    cur.execute(sql, params)
                    try:
                        rows = cur.fetchall()
                    except Exception:
                        rows = []      # INSERT/UPDATE/DDL: nothing to fetch
                    return {"rows": rows, "rowcount": cur.rowcount}
            except RuntimeError:
                # Recording without a database (see prepare_env: we will not run
                # DDL against production). An unreachable cache store means "not
                # cached", which is the honest answer and makes the route record
                # its uncached path — the one worth pinning anyway.
                return {"rows": [], "rowcount": 0, "_no_db": True}

        got = self._cache.fetch(key, produce)
        self._rows = got.get("rows") or []
        self.rowcount = got.get("rowcount") or 0

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)

    def __iter__(self):
        return iter(self._rows)


# ------------------------------------------------------------------ the seams

def _install(monkeypatch, caches):
    """Close every boundary the routes can reach. Five libraries, because the
    codebase has five and there is no shared wrapper to hook."""
    import api
    import requests
    import httpx
    import urllib.request

    llm, http, db = caches["llm"], caches["http"], caches["db"]

    # 1. LLM. `api.get_client()` is the production factory and 16 call sites
    #    take `client` as a parameter, so it covers most of the app. The
    #    self-constructing clients each need their own.
    fake = ReplayAnthropic(llm)
    monkeypatch.setattr(api, "client", fake, raising=False)
    monkeypatch.setattr(api, "get_client", lambda: fake)
    for mod, attr in (("industry_classifier", "_c"), ("bill_market", "_client")):
        with contextlib.suppress(ImportError, AttributeError):
            m = __import__(mod)
            monkeypatch.setattr(m, attr, lambda: fake)
    with contextlib.suppress(ImportError, AttributeError):
        import correspondence.router as crouter
        monkeypatch.setattr(crouter, "_claude", lambda: fake)
    with contextlib.suppress(ImportError, AttributeError):
        from agents import elections_agent
        monkeypatch.setattr(elections_agent, "AsyncAnthropic", lambda **kw: fake)

    # 2. requests. `requests.get/post` route through Session.request, so the
    #    one patch covers the bare calls and all ~10 module-level Sessions.
    real_request = requests.Session.request

    def fake_request(self, method, url, params=None, **kw):
        key = _key("http", method.upper(), url, params or {})

        def produce():
            r = real_request(self, method, url, params=params, **kw)
            body = {"status": r.status_code, "headers": dict(r.headers)}
            try:
                body["json"] = r.json()
            except ValueError:
                body["text"] = r.text[:200000]
            return body

        return _FakeResponse(http.fetch(key, produce), url)

    monkeypatch.setattr(requests.Session, "request", fake_request)

    # 3. httpx, async only. TestClient subclasses httpx.Client, so patching the
    #    sync client would break the harness itself.
    real_async_request = httpx.AsyncClient.request

    async def fake_async_request(self, method, url, **kw):
        key = _key("http", str(method).upper(), str(url), kw.get("params") or {})
        if key in http.entries or not RECORDING:
            return _FakeResponse(http.fetch(key, lambda: None), str(url))
        # Record mode: the real call has to be awaited, which a sync produce()
        # cannot do — so the write-through happens here instead.
        r = await real_async_request(self, method, url, **kw)
        rec = {"status": r.status_code, "headers": dict(r.headers)}
        try:
            rec["json"] = r.json()
        except Exception:                                # noqa: BLE001
            rec["text"] = r.text[:200000]
        http.entries[key] = rec
        return _FakeResponse(rec, str(url))

    monkeypatch.setattr(httpx.AsyncClient, "request", fake_async_request)

    # 4. urllib, the easily-missed third library: district_resolver._open feeds
    #    the Census geocoder and Nominatim for the three /resolve-* routes.
    real_urlopen = urllib.request.urlopen

    def fake_urlopen(url_or_req, *a, **kw):
        url = getattr(url_or_req, "full_url", url_or_req)
        key = _key("http", "GET", str(url), {})

        def produce():
            with real_urlopen(url_or_req, *a, **kw) as r:
                body = r.read()
            try:
                return {"status": 200, "json": json.loads(body)}
            except ValueError:
                return {"status": 200, "text": body.decode("utf-8", "replace")[:200000]}

        return _FakeRaw(http.fetch(key, produce))

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    # 5. Postgres. Every query funnels through `db._cursor`, which is a cleaner
    #    seam than the ~10 modules that import get/set_disk_cache by name.
    import correspondence.db as cdb
    real_cursor = cdb._cursor          # bind before patching, or record recurses

    @contextlib.contextmanager
    def fake_cursor():
        yield _Cursor(db, real_cursor)

    monkeypatch.setattr(cdb, "_cursor", fake_cursor)

    # 6. Not a network seam, but the reason a test run currently dirties the
    #    tree: log_action is called from most agents and appends to the repo.
    from agents import documentor_agent
    from search import search_logger
    monkeypatch.setattr(documentor_agent, "LOG_FILE", str(pathlib.Path(tempfile.gettempdir()) / "nospopuli-replay-discard.jsonl"))
    monkeypatch.setattr(documentor_agent, "log_action", lambda *a, **k: None)
    monkeypatch.setattr(search_logger, "SEARCH_LOG_FILE", str(pathlib.Path(tempfile.gettempdir()) / "nospopuli-replay-discard.jsonl"))

    # 7. The repo's own data and the clock are inputs too. The daily refresh
    #    commits a new foundry/data/store to main, so a route that reads it
    #    live drifts with no code change — /api/foundry/data went red that way
    #    on 2026-09-21. Routes read a frozen copy under golden/_store instead:
    #    whole files, never hand-edited, a subset chosen to stay reviewable.
    #    `today` is frozen to the day that copy was taken. The subset has no
    #    elections store and no item-facts, item-summaries or meeting-digests
    #    sidecars (~2.5 MB together), so those four payload keys are pinned
    #    empty — a gap, not a guarantee.
    import datetime as real_dt
    import types
    from agents import ledger_agent

    class _FrozenDate(real_dt.date):
        @classmethod
        def today(cls):
            return cls.fromisoformat(TODAY)

    frozen_dt = types.ModuleType("datetime")
    frozen_dt.__dict__.update(real_dt.__dict__)
    frozen_dt.date = _FrozenDate
    monkeypatch.setattr(api, "_dt", frozen_dt)
    monkeypatch.setattr(api, "_FOUNDRY_STORE", STORE)
    monkeypatch.setattr(ledger_agent, "_FOUNDRY_STORE", STORE)
    monkeypatch.setattr(api, "_FOUNDRY_HEALTH_PATH", STORE / "_health.json")
    real_health_load = api._health.load
    monkeypatch.setattr(api._health, "load",
                        lambda path=None: real_health_load(STORE / "_health.json"))
    api._FOUNDRY_PAYLOAD.update(sig=None, body=None)

    # 38 routes carry @limiter.limit over in-process storage on a module global
    # that never resets, and get_remote_address collapses every TestClient call
    # to one key — so a 10/min route 429s on the 11th test. RATELIMIT_ENABLED
    # does not work here (init_app is never called); .enabled short-circuits.
    api.limiter.enabled = False
    return api


class _FakeResponse:
    """The `requests`/`httpx` response surface the callers actually touch."""

    def __init__(self, rec, url):
        self.status_code = rec.get("status", 200)
        self.headers = rec.get("headers", {})
        self._json = rec.get("json")
        self.text = rec.get("text") or (
            json.dumps(self._json) if self._json is not None else "")
        self.url = url
        self.content = self.text.encode()

    def json(self):
        if self._json is None:
            raise ValueError("no json body recorded")
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(f"{self.status_code} for {self.url}")

    @property
    def ok(self):
        return self.status_code < 400

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeRaw:
    """urllib's file-like return value."""

    def __init__(self, rec):
        body = rec.get("text") or json.dumps(rec.get("json") or {})
        self._body = body.encode()
        self.status = rec.get("status", 200)

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


# ------------------------------------------------------------- the app itself

def prepare_env():
    """Must run before `import api`. Every item here is load-bearing.

    `load_dotenv(override=False)` skips a key already present in the
    environment, so presetting is enough to keep the committed .env out.
    """
    REPLAY.mkdir(parents=True, exist_ok=True)
    # Recording needs the real credentials, and api.py's own load_dotenv() runs
    # too late to be read here. Loading first is safe: the overrides below are
    # written after, and load_dotenv never clobbers what is already set.
    if RECORDING:
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env")
    # correspondence/router.py calls init_db() at import and api.py imports it,
    # so without this `import api` opens a real pool and runs DDL on production.
    # Left blank even when recording: capturing a fixture is not worth running
    # CREATE TABLE against production. DB-backed routes therefore record their
    # fail-open path, and say so in meta.notes.
    os.environ["SUPABASE_DB_URL"] = ""
    # flag_logger reads os.environ[...] directly: absence is a KeyError.
    os.environ.setdefault("SUPABASE_URL", "http://replay.invalid")
    os.environ.setdefault("SUPABASE_API_KEY", "replay")
    # Keep the real key reachable for record mode, then sentinel the live one so
    # a missed seam fails to authenticate instead of billing.
    real = os.environ.get("ANTHROPIC_API_KEY", "")
    if real and not real.startswith("replay-"):
        os.environ["NOSPOPULI_REAL_ANTHROPIC_KEY"] = real
    if not RECORDING:
        os.environ["ANTHROPIC_API_KEY"] = "replay-not-a-key"
    # api.py mounts StaticFiles(directory="frontend") and does
    # sys.path.insert(0, "foundry") — both relative to the CWD.
    os.chdir(ROOT)
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))


def clear_memory_caches():
    """Empty every in-process TTLCache before a request.

    There is a second cache tier above the seams — cachetools.TTLCache
    instances in bill_fetcher, legiscan_client, lda_client, feed_agent,
    elections_agent and bill_market. It made recording lie: pass one fetched
    live and warmed the module cache, so passes two and three never reached the
    seam and their keys were never written. A fresh test process then missed.

    Clearing makes every request take the same path in both modes, and removes
    TTL expiry as a source of drift inside a single run.
    """
    try:
        from cachetools import TTLCache
    except ImportError:
        return
    # Named explicitly rather than swept out of sys.modules: touching every
    # attribute of every loaded module pulls on deprecated re-exports and
    # buries the run in warnings.
    for name in ("bill_fetcher", "legiscan_client", "lda_client", "feed_agent",
                 "elections_agent", "bill_market", "search_cache", "stock_perf",
                 "translator_agent", "historian_agent", "industry_classifier"):
        mod = sys.modules.get(name)
        for attr in dir(mod) if mod else ():
            val = getattr(mod, attr, None)
            if isinstance(val, TTLCache):
                val.clear()


@contextlib.contextmanager
def no_sockets():
    """Belt and braces: turn a missed seam into a named failure rather than a
    live call. Only outbound connects are blocked; TestClient is in-process."""
    real_connect = socket.socket.connect

    def blocked(self, addr):
        raise ReplayMiss(f"live socket attempted: {addr}")

    socket.socket.connect = blocked
    try:
        yield
    finally:
        socket.socket.connect = real_connect


def build_app(monkeypatch):
    """Returns (api module, TestClient) with every boundary closed."""
    from starlette.testclient import TestClient
    prepare_env()
    caches = {n: Cache(n) for n in ("llm", "http", "db")}
    api = _install(monkeypatch, caches)
    # global_exception_handler turns everything into a generic 500, so asserting
    # on that body is the only stable behaviour available.
    return api, TestClient(api.app, raise_server_exceptions=False), caches


# ------------------------------------------------------------ golden compare

# Response paths whose list ORDER is not defined by the application, so pinning
# it would pin a coin flip. `member_search_agent.py:165` builds `chambers` as a
# set and returns `list(chambers)` at :186 — order therefore tracks
# PYTHONHASHSEED, which is stable inside one process and different between them.
# Contents are still compared; only the ordering is waived. Fixing this properly
# means sorting in the application, which is out of scope for a test change.
UNORDERED = ("member.chambers",)


def normalize(obj, prefix=""):
    """Sort the lists whose order the app leaves undefined. Applied by both the
    recorder and the test, so the two can never disagree about it."""
    if isinstance(obj, dict):
        return {k: normalize(v, f"{prefix}.{k}" if prefix else k)
                for k, v in obj.items()}
    if isinstance(obj, list):
        out = [normalize(v, prefix) for v in obj]
        if prefix in UNORDERED:
            return sorted(out, key=lambda x: json.dumps(x, sort_keys=True, default=str))
        return out
    return obj


def _prune(obj, paths, prefix=""):
    """Drop volatile dotted paths. `a.b` matches inside lists at any depth."""
    if isinstance(obj, dict):
        return {k: _prune(v, paths, f"{prefix}.{k}" if prefix else k)
                for k, v in obj.items()
                if (f"{prefix}.{k}" if prefix else k) not in paths}
    if isinstance(obj, list):
        return [_prune(v, paths, prefix) for v in obj]
    return obj


def golden_diff(actual, expected, volatile=()):
    """Readable difference lines, empty when clean.

    Same shape as `foundry/run_m1.py:golden_diff` — name the path that moved
    rather than dumping two blobs at the reader.
    """
    vol = set(volatile or ())
    return _diff(_prune(normalize(actual), vol),
                 _prune(normalize(expected), vol), "")[:12]


def _diff(a, b, path):
    if type(a) is not type(b) and not (isinstance(a, (int, float))
                                       and isinstance(b, (int, float))):
        return [f"{path or '<root>'}: type {type(b).__name__} -> {type(a).__name__}"]
    if isinstance(b, dict):
        lines = []
        for k in sorted(set(b) - set(a)):
            lines.append(f"{path}.{k}: missing (expected {json.dumps(b[k], default=str)[:80]})")
        for k in sorted(set(a) - set(b)):
            lines.append(f"{path}.{k}: unexpected ({json.dumps(a[k], default=str)[:80]})")
        for k in sorted(set(a) & set(b)):
            lines += _diff(a[k], b[k], f"{path}.{k}")
        return lines
    if isinstance(b, list):
        if len(a) != len(b):
            return [f"{path}: length {len(b)} -> {len(a)}"]
        out = []
        for i, (x, y) in enumerate(zip(a, b)):
            out += _diff(x, y, f"{path}[{i}]")
        return out
    if a != b:
        return [f"{path or '<root>'}: expected {json.dumps(b, default=str)[:120]} "
                f"got {json.dumps(a, default=str)[:120]}"]
    return []


# ------------------------------------------------------------------ fixtures

SHAPE_LIMIT = 256 * 1024        # bytes of JSON above which we pin shape, not payload


def shape(obj):
    """Types and collection sizes, recursively. Keeps a 30 MB response out of
    git while still failing if a key vanishes or a list empties out.

    `/api/foundry/data` returns the whole record store — 30 MB, and already
    committed under foundry/data/store/. Duplicating it as a fixture would make
    the diff unreadable, which defeats the point of committing fixtures at all.
    """
    if isinstance(obj, dict):
        # A dict keyed by thousands of record ids is data, not structure —
        # summarize it the way a list is summarized, or the "shape" is as
        # unreviewable as the payload it replaced.
        if len(obj) > 50:
            first = obj[sorted(obj)[0]]
            return {"__keys__": len(obj), "of": shape(first)}
        return {k: shape(v) for k, v in sorted(obj.items())}
    if isinstance(obj, list):
        return {"__list__": len(obj), "of": shape(obj[0]) if obj else None}
    return type(obj).__name__


def load_fixtures():
    """Every committed fixture, sorted so failures report in a stable order."""
    if not GOLDEN.exists():
        return []
    return [(p.stem, json.loads(p.read_text()))
            for p in sorted(GOLDEN.glob("*.json"))]




# ------------------------------------------------------------- the recorder

# Request bodies for the routes worth a golden fixture. Kept as a plain tuple of
# (method, path, body, params, headers, note) rather than a class — rule 3.
# `volatile` per fixture is derived below from what actually moved between two
# captures, so the list stays honest instead of aspirational.
CASES = (
    ("GET", "/health", None, None, None, "the only dependency-free route"),
    ("GET", "/robots.txt", None, None, None, "static text"),
    ("GET", "/sitemap.xml", None, None, None, "static text"),
    ("GET", "/stocks/notable", None, None, None, "reads data/notable_trades.json"),
    ("GET", "/stocks/all", None, {"page": 1, "page_size": 5}, None, "pure pagination"),
    # /api/stocks/traded is deliberately NOT pinned. It looks pure but calls
    # bill_market.sector_of_tickers, which classifies 1,249 tickers via Haiku in
    # batches built from a set comprehension (bill_market.py:150) — so the batch
    # composition, and every cache key derived from it, changes per process. One
    # cold request cost 44 Haiku calls when recording. Pinning it needs a sorted
    # batch order in application code, which is out of scope here.
    ("GET", "/member/stocks", None, {"bioguide": "C001075"}, None, "reads data/"),
    ("GET", "/api/foundry/data", None, None, None, "reads foundry/data/store"),
    ("GET", "/api/graph/votes", None, {"person": "Jeff McKay"}, None,
     "fails open to an honest empty state with no DB"),
    ("POST", "/resolve-zip", {"zip_code": "22030"}, None, None, "local JSON only"),
    ("POST", "/search", {"question": "HR 1234", "max_results": 5}, None, None,
     "federal bill-id fast path — the $0 regex route"),
    ("POST", "/search", {"question": "LA County", "max_results": 5}, None, None,
     "the off_topic defect, frozen"),
    ("POST", "/search", {"question": "Healthcare bills in Virginia",
                         "max_results": 5}, None, None, "state query via /search"),
    ("POST", "/ledger", {"question": "HR 1234"}, None, None, "bill-id plate"),
    ("POST", "/ledger", {"question": "Healthcare bills in Virginia"}, None, None,
     "forced federal at api.py:1618"),
    ("POST", "/ledger", {"question": "LA County"}, None, None, "uncharted plate"),
    ("POST", "/member/search", {"name": "Ted Cruz"}, None, None, "member lookup"),
    # Auth-gated: contract only. A fabricated 200 would be worse than nothing.
    ("GET", "/monitor", None, None, None, "contract: refuses without the secret"),
    ("GET", "/admin/foundry", None, None, None, "contract: refuses without the secret"),
    ("POST", "/api/foundry/onboard", {"name": "probe"}, None, None,
     "contract: must refuse a TestClient host or it spawns a synthesis thread"),
    ("GET", "/correspondence", None, None, None, "contract: JWT required"),
    # --- pure / local-file routes ---------------------------------------
    ("GET", "/", None, None, None, "frontend/test.html — production, despite the name"),
    ("GET", "/test", None, None, None, "same page, historical alias"),
    ("GET", "/elections", None, None, None, "static HTML shell"),
    ("GET", "/foundry", None, None, None, "static HTML shell"),
    ("GET", "/favicon.ico", None, None, None, "static asset"),
    ("GET", "/stocks/all", None, {"q": "AAPL", "page": 1, "page_size": 3}, None,
     "the search branch of the same pure pagination"),
    ("GET", "/api/foundry/onboard/replay-missing", None, None, None,
     "in-memory job store: an unknown id must not 500"),

    # --- external HTTP, replayed ----------------------------------------
    ("POST", "/resolve-address", {"address": "3701 Pender Dr, Fairfax VA"}, None, None,
     "Census geocoder via urllib"),
    ("POST", "/resolve-point", {"lat": 38.85, "lon": -77.30}, None, None, "reverse geocode"),
    ("POST", "/resolve-district", {"geoid": "5111"}, None, None, "district lookup"),
    ("GET", "/geo/guess", None, None, None, "ip-api.com via httpx"),
    ("GET", "/api/member/B001230", None, None, None, "member detail"),
    ("GET", "/member/finance", None, {"name": "Tammy Baldwin", "state": "WI",
                                      "chamber": "senate"}, None, "FEC"),
    ("GET", "/api/bill/119/hr/1234/text", None, None, None, "bill text fetch"),
    ("GET", "/lobbying/search", None, {"q": "Lockheed"}, None,
     "LDA — no key configured here, so this records the degraded path"),

    # --- search / ledger variants ---------------------------------------
    ("POST", "/search", {"question": "Ted Cruz", "max_results": 5}, None, None,
     "member route — 20 of the 73 logged searches were this"),
    ("POST", "/search", {"question": "best pizza in brooklyn", "max_results": 5}, None,
     None, "correctly off_topic — the control for the LA County defect"),
    ("POST", "/search", {"question": "Radnor County", "max_results": 5}, None, None,
     "off_topic @0.95 on a local-government ask"),
    ("POST", "/ledger", {"question": "show me what I'm watching"}, None, None,
     "the anchored _WATCH_RE defect, end to end"),
    ("POST", "/ledger", {"question": ""}, None, None, "empty is the home plate"),
    ("POST", "/state/search", {"question": "healthcare", "state_code": "VA",
                               "max_results": 5}, None, None,
     "the state layer directly — records the no-LegiScan-key path"),

    # --- auth-gated: contract only, no secrets in the fixture -----------
    ("GET", "/monitor/flags", None, None, None, "contract: refuses without the secret"),
    ("GET", "/monitor/stream", None, None, None, "contract: refuses without the secret"),
    ("GET", "/monitor/analysis", None, None, None, "contract: refuses (would call Haiku)"),
    ("GET", "/admin/elections", None, {"state": "VA"}, None, "contract: refuses"),
    ("GET", "/admin/elections/ui", None, None, None, "contract: refuses"),
    ("GET", "/admin/foundry/health", None, None, None,
     "contract: refuses — also shells out to `gh` when it does not"),
    ("GET", "/admin/foundry/jobs/replay-missing", None, None, None, "contract: refuses"),
    ("POST", "/watcher/run", None, None, None, "contract: refuses without WATCHER_SECRET"),
    ("POST", "/admin/foundry/refresh/fairfax-bos", None, None, None,
     "contract: refuses — would spawn a refresh thread"),
    ("GET", "/auth/me", None, None, None, "contract: JWT required"),
    ("POST", "/user/zip", {"zip_code": "22030"}, None, None, "contract: JWT required"),
    ("POST", "/correspondence/draft", {"bill_id": "hr1234", "stance": "support"}, None,
     None, "contract: JWT required"),
    ("GET", "/correspondence/subscriptions", None, {"email": "nobody@example.invalid"},
     None, "subscription listing"),
    ("GET", "/correspondence/unsubscribe-link", None,
     {"email": "nobody@example.invalid", "bill_id": "hr1234"}, None, "unsubscribe link"),

    # --- catch-all --------------------------------------------------------
    ("GET", "/definitely-not-a-route", None, None, None,
     "the SPA fallback answers 200 HTML for anything unmatched"),
)

_SLUG = str.maketrans({"/": "_", "{": "", "}": "", "?": "", "=": "_", " ": "_"})


def _slug(method, path, body, params=None):
    """Distinct per case, not just per route — two cases that differ only in
    their query string must not overwrite each other's fixture."""
    stem = f"{method}{path}".translate(_SLUG).strip("_").lower()
    hint = ""
    if isinstance(body, dict):
        q = str(body.get("question") or body.get("name") or "")
        hint = "__" + "_".join(q.lower().split()[:3]) if q else ""
    if params:
        hint += "__" + "_".join(f"{k}-{v}" for k, v in sorted(params.items()))
    return (stem + hint).translate(_SLUG).lower()


def record(argv):
    """Capture every case against live services and commit-ready JSON.

    Runs each case three times. The first pass populates the caches from live
    services; volatility is measured between passes two and three, which both
    replay from those caches.

    Measuring across the live pass instead would mark anything that merely
    varied upstream as volatile — on /ledger that meant `headline` and
    `stories`, i.e. exactly the fields worth pinning. What the fixture needs to
    know is whether the route is deterministic *under replay*, because that is
    the only condition CI ever runs it in.
    """
    if not RECORDING:
        print("refusing to record without NOSPOPULI_REPLAY=record", file=sys.stderr)
        return 2
    import pytest
    from starlette.testclient import TestClient

    mp = pytest.MonkeyPatch()
    prepare_env()
    caches = {n: Cache(n) for n in ("llm", "http", "db")}
    api = _install(mp, caches)
    client = TestClient(api.app, raise_server_exceptions=False)
    GOLDEN.mkdir(parents=True, exist_ok=True)

    only = argv[0] if argv else None
    written, skipped = [], []
    try:
        for method, path, body, params, headers, note in CASES:
            name = _slug(method, path, body, params)
            if only and only not in name:
                continue
            try:
                shots = []
                for _ in range(3):
                    clear_memory_caches()
                    shots.append(client.request(method, path, json=body,
                                                params=params, headers=headers))
            except Exception as e:                       # noqa: BLE001
                skipped.append(f"{name}: {type(e).__name__}: {e}")
                continue

            r = shots[-1]
            resp = {"status": r.status_code}
            ctype = r.headers.get("content-type", "")
            pair = []
            if "x-ndjson" in ctype or "stream" in ctype:
                for s in shots:
                    pair.append([json.loads(l) for l in s.text.splitlines() if l.strip()])
                resp["ndjson"] = pair[-1]
            elif "json" in ctype:
                for s in shots:
                    pair.append(s.json())
                if len(json.dumps(pair[-1], default=str)) > SHAPE_LIMIT:
                    resp["shape"] = shape(pair[-1])
                    pair = [shape(x) for x in pair]
                else:
                    resp["json"] = pair[-1]
            else:
                for s in shots:
                    pair.append({"text": s.text[:2000]})
                resp["text"] = r.text[:2000]

            volatile = sorted(_moved(pair[1], pair[2]))
            (GOLDEN / f"{name}.json").write_text(json.dumps({
                "request": {"method": method, "path": path,
                            **({"body": body} if body else {}),
                            **({"params": params} if params else {}),
                            **({"headers": headers} if headers else {})},
                "response": resp,
                "meta": {"captured": _now(), "app_sha": _sha(),
                         "volatile": volatile, "notes": note},
            }, indent=1, sort_keys=True, default=str) + "\n")
            written.append(f"{name}  [{r.status_code}]"
                           + (f"  volatile={volatile}" if volatile else ""))
    finally:
        for c in caches.values():
            c.save()
        mp.undo()

    print(f"\nwrote {len(written)} fixtures to {GOLDEN.relative_to(ROOT)}/")
    for w in written:
        print("  " + w)
    if skipped:
        print(f"\nskipped {len(skipped)} (recorded as nothing, not as a fake 200):")
        for s in skipped:
            print("  " + s)
    for n, c in caches.items():
        print(f"  cache {n}: {len(c.entries)} entries")
    return 0


def _moved(a, b, prefix=""):
    """Dotted paths whose value differs between two identical requests."""
    out = set()
    if isinstance(a, dict) and isinstance(b, dict):
        for k in set(a) | set(b):
            p = f"{prefix}.{k}" if prefix else k
            if k not in a or k not in b:
                out.add(p)
            else:
                out |= _moved(a[k], b[k], p)
    elif isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            out.add(prefix or "<root>")
        else:
            for x, y in zip(a, b):
                out |= _moved(x, y, prefix)
    elif a != b:
        out.add(prefix or "<root>")
    return out


def _now():
    from datetime import datetime
    return datetime.now().isoformat(timespec="seconds")


def _sha():
    import subprocess
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT,
                              capture_output=True, text=True).stdout.strip()
    except Exception:                                    # noqa: BLE001
        return "unknown"


def _list():
    for name, fx in load_fixtures():
        meta = fx.get("meta", {})
        print(f"{name:52} {fx['response']['status']}  {meta.get('notes', '')}")
    for n in ("llm", "http", "db"):
        c = Cache(n)
        print(f"cache {n}: {len(c.entries)} entries")
    return 0


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "list"
    sys.exit(record(sys.argv[2:]) if cmd == "record" else _list())
