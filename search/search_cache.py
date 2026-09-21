"""Two-tier search cache.

Tier 1: query → minimal result list (bill IDs + titles), persisted via
correspondence.db disk_cache with a short TTL.

Tier 2: on cache hit, latest_action / is_law / law_number are refreshed
per-bill via bill_fetcher.fetch_bill, which has its own in-memory TTL.

Freshness-sensitive queries bypass the cache.
"""
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor

from sources.bill_fetcher import fetch_bill
from correspondence.db import get_disk_cache, set_disk_cache, clear_disk_cache

SEARCH_CACHE_TTL_SECONDS = 1800  # 30 min
SEARCH_KEY_PREFIX = "search:v6:"


def clear():
    """Drop every cached search result. Returns rows deleted."""
    return clear_disk_cache(prefix=SEARCH_KEY_PREFIX)

_FRESHNESS_PHRASES = (
    "this week", "today", "yesterday", "this month",
    "passed today", "passed this week", "vote scheduled",
    "introduced this week", "introduced today", "just introduced",
    "recently passed", "currently moving", "moving this week",
)


def is_freshness_query(structured, question):
    if not structured:
        return False
    if structured.get("full_history"):
        return False  # full_history is its own bypass path
    if structured.get("status") in {"enacted_recent", "introduced_recent", "moving"}:
        return True
    q = (question or "").lower()
    return any(p in q for p in _FRESHNESS_PHRASES)


def cache_key(structured, question, max_results, jurisdiction="federal"):
    payload = json.dumps({
        "q": (question or "").strip().lower(),
        "kw": sorted(structured.get("keywords", []) or []),
        "exp": sorted(structured.get("expanded_terms", []) or []),
        "topic": structured.get("topic") or "",
        "cn": sorted(structured.get("congress_numbers", []) or []),
        "status": structured.get("status") or "",
        "n": int(max_results or 5),
        "j": jurisdiction,
    }, sort_keys=True)
    return SEARCH_KEY_PREFIX + hashlib.sha1(payload.encode()).hexdigest()


def get(key):
    return get_disk_cache(key, max_age_seconds=SEARCH_CACHE_TTL_SECONDS)


def store(key, results):
    if not results:
        return
    slim = [
        {
            "congress": r.get("congress"),
            "type": r.get("type"),
            "number": r.get("number"),
            "title": r.get("title"),
            "date": r.get("date") or r.get("date_issued") or "",
            "source": r.get("source") or "cache",
            "package_id": r.get("package_id") or "",
        }
        for r in results
        if r.get("number") and r.get("type") and r.get("congress")
    ]
    set_disk_cache(key, slim)


def _refresh_one(r):
    try:
        data = fetch_bill(r["congress"], r["type"], r["number"])
    except Exception:
        return r
    if not data:
        return r
    bill = data.get("bill") or {}
    la = bill.get("latestAction") or {}
    r["latest_action"] = la.get("text", "") or r.get("latest_action", "")
    r["latest_action_date"] = la.get("actionDate", "") or r.get("latest_action_date", "")
    laws = bill.get("laws") or []
    if laws:
        r["is_law"] = True
        law_num = (laws[0].get("number") or "").split("-")[-1]
        if law_num:
            r["law_number"] = law_num
    if bill.get("title") and not r.get("title"):
        r["title"] = bill["title"]
    return r


def rehydrate(results):
    """Re-fetch latest_action / law status for each cached result in parallel.

    fetch_bill is itself TTL-cached (1hr), so this is cheap after warmup.
    """
    if not results:
        return results
    with ThreadPoolExecutor(max_workers=8) as ex:
        list(ex.map(_refresh_one, results))
    return results
