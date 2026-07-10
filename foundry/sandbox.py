"""Sandboxed runtime for candidate extractor artifacts (spec modules 3, 4).

A synthesized extractor gets exactly one I/O capability: `fetch_json`,
injected by this runner. All HTTP goes through a write-through disk cache
frozen alongside the golden set, so gate runs are reproducible and repeated
attempts don't hammer the source. Process isolation via subprocess is for
crash containment and timeouts, not a security boundary — lab use only.

Usage (invoked via run_artifact below):
    python sandbox.py <candidate.py> <out.json> <event_id> [event_id ...]
Env: FOUNDRY_CACHE overrides the cache path (M2 points this at a mutated
copy); FOUNDRY_NO_LIVE=1 makes a cache miss an error instead of a live fetch
(so a simulated break can't silently heal from the real source).
"""

import importlib.util
import json
import os
import pathlib
import subprocess
import sys
import time
import urllib.parse

API = "https://webapi.legistar.com/v1/pittsburgh"
CACHE_PATH = pathlib.Path(__file__).parent / "golden" / "pittsburgh" / "http_cache.json"


def load_cache():
    if CACHE_PATH.exists():
        return json.loads(CACHE_PATH.read_text())
    return {}


def save_cache(cache):
    CACHE_PATH.write_text(json.dumps(cache))


def make_fetch_json(cache, allow_live=True):
    def fetch_json(path, params=None):
        key = path
        if params:
            key += "?" + urllib.parse.urlencode(sorted(params.items()))
        if key not in cache:
            if not allow_live:
                raise RuntimeError(f"cache miss with live fetch disabled: {key}")
            import requests
            r = requests.get(API + path, params=params, timeout=30)
            r.raise_for_status()
            cache[key] = r.json()
            time.sleep(0.2)
        # round-trip so the candidate can't mutate the cache
        return json.loads(json.dumps(cache[key]))
    return fetch_json


def run_artifact(candidate_path, event_ids, out_path, cache_path=None, allow_live=True):
    """Execute an extractor artifact in a subprocess. Returns (records, error)."""
    env = dict(os.environ)
    if cache_path:
        env["FOUNDRY_CACHE"] = str(cache_path)
    if not allow_live:
        env["FOUNDRY_NO_LIVE"] = "1"
    proc = subprocess.run(
        [sys.executable, str(pathlib.Path(__file__).resolve()), str(candidate_path),
         str(out_path)] + [str(e) for e in event_ids],
        capture_output=True, text=True, timeout=300, env=env)
    if proc.returncode != 0:
        return None, proc.stderr
    return json.loads(pathlib.Path(out_path).read_text())["records"], None


def warm_cache(event_ids):
    """Pre-fetch the endpoints extractor v1 uses, so gate runs are mostly
    offline. A candidate calling novel endpoints/params still goes live once."""
    cache = load_cache()
    fetch = make_fetch_json(cache)
    for eid in event_ids:
        fetch(f"/events/{eid}")
        for item in fetch(f"/events/{eid}/eventitems"):
            if item.get("EventItemRollCallFlag"):
                fetch(f"/eventitems/{item['EventItemId']}/rollcalls")
            if item.get("EventItemMatterFile") and item.get("EventItemPassedFlagName") is not None:
                fetch(f"/eventitems/{item['EventItemId']}/votes")
    save_cache(cache)
    return cache


def main():
    candidate_path, out_path = sys.argv[1], sys.argv[2]
    event_ids = [int(e) for e in sys.argv[3:]]
    cache_path = pathlib.Path(os.environ.get("FOUNDRY_CACHE", CACHE_PATH))
    allow_live = not os.environ.get("FOUNDRY_NO_LIVE")

    spec = importlib.util.spec_from_file_location("candidate", candidate_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    cache = json.loads(cache_path.read_text()) if cache_path.exists() else {}
    records, run_meta = module.extract(make_fetch_json(cache, allow_live), event_ids)
    if allow_live:
        cache_path.write_text(json.dumps(cache))
    pathlib.Path(out_path).write_text(json.dumps(
        {"records": records, "run_meta": run_meta}))


if __name__ == "__main__":
    main()
