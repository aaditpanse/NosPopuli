"""Presidential nominations, from Congress.gov, as files.

The Senate snapshots already hold the confirmation votes (document PN1020),
but not the nomination behind them: who was nominated to what, and what
happened. This batch job writes nominations-<congress>.json in
graph.DATA_DIR from the Congress.gov API: the list for the dates and the
latest action, one detail call for the nominee and the position. A detail
is fetched again only when the list shows a new updateDate, so a daily run
makes a few list calls. Never called while a user waits.

Congress.gov's nomination records start with the 97th Congress (1981).

    python -m sources.nominations            # 97th → current
    python -m sources.nominations 118 119
"""

import datetime
import json
import os
import pathlib
import sys
import time

_HERE = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_HERE))
import graph  # noqa: E402  — DATA_DIR, current_session

BASE = "https://api.congress.gov/v3/nomination"
FIRST_CONGRESS = 97
_UA = "NosPopuli bulk sync (nospopuli.org)"


def _get(s, url, key, params=None, errors=None):
    """One call, or None with the failure recorded as type + path: the key
    travels in the query string and must never reach a file."""
    try:
        r = s.get(url, params={"api_key": key, "format": "json", **(params or {})}, timeout=60)
        if r.status_code == 429:
            time.sleep(60)
            r = s.get(url, params={"api_key": key, "format": "json", **(params or {})}, timeout=60)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        errors.append(f"{url.split('?')[0]}: {type(e).__name__}")
        return None


def citation(item):
    """PN1020, or PN12-1 for one part of a nomination split in parts: the
    same form the Senate roll call uses for the document it voted on."""
    part = str(item.get("partNumber") or "00")
    return f"PN{item['number']}" + (f"-{int(part)}" if part not in ("00", "0") else "")


def record(item, detail):
    """List item + detail → the stored record. Pure."""
    d = detail or {}
    la = item.get("latestAction") or {}
    return {"citation": citation(item), "number": item["number"],
            "part": str(item.get("partNumber") or "00"), "received": item.get("receivedDate"),
            "military": bool((item.get("nominationType") or {}).get("isMilitary")),
            "organization": item.get("organization"),
            "description": d.get("description"),
            "positions": [{"title": n.get("positionTitle"), "organization": n.get("organization"),
                           "nominees": n.get("nomineeCount")} for n in d.get("nominees") or []],
            "latest_action": {"date": la.get("actionDate"), "text": la.get("text")},
            "updated": item.get("updateDate")}


def sync_congress(congress, key, s, pause=0.05):
    """Rewrite nominations-<congress>.json, fetching a detail only for a
    nomination that is new or has a new updateDate. Fail-closed on the list:
    if any list page fails, the file is left as it was, because a short
    list would read as nominations that never happened."""
    path = graph.DATA_DIR / f"nominations-{congress}.json"
    old = json.loads(path.read_text())["nominations"] if path.exists() else {}
    errors, items, offset = [], [], 0
    while True:
        page = _get(s, f"{BASE}/{congress}", key, {"limit": 250, "offset": offset}, errors)
        if page is None:
            return {"congress": congress, "errors": errors, "unchanged": True}
        items += page.get("nominations", [])
        offset += 250
        if offset >= (page.get("pagination") or {}).get("count", 0):
            break
    out, fetched = {}, 0
    for item in items:
        cit = citation(item)
        prev = old.get(cit)
        if prev and prev.get("updated") == item.get("updateDate"):
            out[cit] = prev
            continue
        detail = _get(s, f"{BASE}/{congress}/{item['number']}", key, errors=errors)
        time.sleep(pause)
        if detail is None:
            if prev:
                out[cit] = prev     # keep what we had; retried tomorrow
            continue
        out[cit] = record(item, detail.get("nomination"))
        fetched += 1
    meta = {"congress": congress, "fetched": datetime.date.today().isoformat(),
            "source": "api.congress.gov nomination", "counts": {"nominations": len(out), "listed": len(items)},
            "errors": errors}
    tmp = path.with_name(path.name + ".part")
    tmp.write_text(json.dumps({"meta": meta, "nominations": out}, separators=(",", ":"), sort_keys=True))
    tmp.replace(path)
    return meta | {"details_fetched": fetched}


def sync(congresses=None):
    import requests
    key = os.getenv("CONGRESS_API_KEY")
    if not key:
        raise RuntimeError("CONGRESS_API_KEY not set; nominations have no bulk source")
    s = requests.Session()
    s.headers["User-Agent"] = _UA
    return {c: sync_congress(c, key, s)
            for c in congresses or range(FIRST_CONGRESS, graph.current_session()[0] + 1)}


if __name__ == "__main__":
    for c, m in sync([int(a) for a in sys.argv[1:]] or None).items():
        print(c, "list failed; file unchanged" if m.get("unchanged") else
              f"{m['counts']['nominations']} nomination(s), {m['details_fetched']} detail(s) fetched",
              f"{len(m['errors'])} error(s)")
        for e in m["errors"][:5]:
            print("  -", e)
