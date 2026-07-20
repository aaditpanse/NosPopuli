"""Runtime v2 for extractor artifacts — source-agnostic (spec modules 3, 4).

Grown for M3: absolute URLs instead of a hardcoded Legistar base, and a
second capability rung — `fetch_text` downloads a document and returns its
text, converting PDFs via pdftotext. Artifacts receive a runtime object
with exactly these two methods; that stays their only I/O.

Usage (via run_artifact):
    python sandbox2.py <candidate.py> <out.json> <cache.json> <arg> [...]
Env: FOUNDRY_NO_LIVE=1 turns cache misses into errors.
"""

import importlib.util
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request


class Runtime:
    def __init__(self, cache, allow_live=True):
        self.cache = cache
        self.allow_live = allow_live
        self.trace = []  # every URL the artifact asked for, in order

    def _fetch(self, url, params, kind):
        key = url
        if params:
            key += "?" + urllib.parse.urlencode(sorted(params.items()))
        self.trace.append(key)
        if key not in self.cache:
            if not self.allow_live:
                raise RuntimeError(f"cache miss with live fetch disabled: {key}")
            req = urllib.request.Request(key, headers={"User-Agent": "nospopuli-foundry-lab"})
            body = urllib.request.urlopen(req, timeout=60).read()
            if kind == "json":
                data = json.loads(body)
            elif body[:5] == b"%PDF-":
                with tempfile.NamedTemporaryFile(suffix=".pdf") as tmp:
                    tmp.write(body)
                    tmp.flush()
                    data = subprocess.run(["pdftotext", "-layout", tmp.name, "-"],
                                          capture_output=True, text=True, check=True).stdout
            else:
                data = body.decode("utf-8", "replace")
            self.cache[key] = data
            time.sleep(0.3)
        return json.loads(json.dumps(self.cache[key]))

    def fetch_json(self, url, params=None):
        """GET an absolute URL, return parsed JSON."""
        return self._fetch(url, params, "json")

    def fetch_text(self, url, params=None):
        """GET an absolute URL, return text. PDFs are converted to layout text."""
        return self._fetch(url, params, "text")


def run_artifact(candidate_path, args, out_path, cache_path, allow_live=True):
    """Execute a v2 extractor artifact in a subprocess. Returns (records, error)."""
    env = dict(os.environ)
    if not allow_live:
        env["FOUNDRY_NO_LIVE"] = "1"
    try:
        proc = subprocess.run(
            [sys.executable, str(pathlib.Path(__file__).resolve()), str(candidate_path),
             str(out_path), str(cache_path)] + [str(a) for a in args],
            capture_output=True, text=True, timeout=600, env=env)
    except subprocess.TimeoutExpired:
        return None, ("TIMEOUT: the extractor did not finish within 600s. It is "
                      "fetching or parsing too much — fetch only the documents "
                      "needed for actions/votes and skip large packet PDFs.")
    if proc.returncode != 0:
        return None, proc.stderr
    return json.loads(pathlib.Path(out_path).read_text())["records"], None


def main():
    candidate_path, out_path, cache_path = sys.argv[1], sys.argv[2], sys.argv[3]
    args = [int(a) for a in sys.argv[4:]]
    cache_path = pathlib.Path(cache_path)
    allow_live = not os.environ.get("FOUNDRY_NO_LIVE")

    spec = importlib.util.spec_from_file_location("candidate", candidate_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    cache = json.loads(cache_path.read_text()) if cache_path.exists() else {}
    rt = Runtime(cache, allow_live)
    try:
        records, run_meta = module.extract(rt, args)
    finally:
        # Persist fetches and the trace even when the extractor crashes, so a
        # failed attempt still informs the repair loop (and doesn't refetch).
        if allow_live:
            cache_path.write_text(json.dumps(cache))
        pathlib.Path(str(out_path) + ".trace").write_text(json.dumps(rt.trace))
    pathlib.Path(out_path).write_text(json.dumps(
        {"records": records, "run_meta": run_meta}))


if __name__ == "__main__":
    main()
