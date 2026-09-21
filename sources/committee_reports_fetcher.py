"""
Committee Reports — fetch & enrich the report records embedded in a bill.

Congress.gov's bill detail response carries a `committeeReports` array with just
`citation` + `url`. This module enriches each entry by calling the
`/committee-report/{congress}/{type}/{number}` detail endpoint so we can show
the committee, chamber, issue date, title, and direct link to the full text.

Wraps every call through `congress_breaker.congress_get` so the breaker trips
once if Congress.gov is down — no spammy retries.
"""

import os
from concurrent.futures import ThreadPoolExecutor, wait
from threading import RLock

from cachetools import TTLCache, cached
from dotenv import load_dotenv

from sources.congress_breaker import congress_get, CongressOutageError
from agents.documentor_agent import log_action

load_dotenv()

CONGRESS_API_KEY = os.getenv("CONGRESS_API_KEY")

# 30-day TTL — committee reports do not change after publication.
_report_cache = TTLCache(maxsize=512, ttl=60 * 60 * 24 * 30)
_PARALLEL_WORKERS = 4
_FANOUT_BUDGET_SECONDS = 10


def _build_full_url(congress: int, rpt_type: str, number: int) -> str:
    """
    Deterministic Congress.gov URL for a committee report's HTML.
    Example: https://www.congress.gov/118/crpt/hrpt125/generated/CRPT-118hrpt125.htm
    """
    t = (rpt_type or "").lower()
    return (
        f"https://www.congress.gov/{congress}/crpt/{t}{number}/"
        f"generated/CRPT-{congress}{t}{number}.htm"
    )


@cached(cache=_report_cache, lock=RLock())
def _fetch_single_report(congress: int, rpt_type: str, number: int) -> dict | None:
    """
    Hit the per-report detail endpoint and return a normalised dict, or None
    if the call fails or the response is missing the expected shape.
    """
    url = f"https://api.congress.gov/v3/committee-report/{congress}/{rpt_type}/{number}"
    try:
        r = congress_get(url, params={"api_key": CONGRESS_API_KEY, "format": "json"}, timeout=10)
    except CongressOutageError:
        return None
    if r.status_code != 200:
        return None
    try:
        items = r.json().get("committeeReports") or []
    except Exception:
        return None
    if not items:
        return None
    item = items[0]

    committees = item.get("committees") or []
    committee_name = committees[0].get("name") if committees else None

    return {
        "citation":             item.get("citation"),
        "title":                item.get("title"),
        "committee":            committee_name,
        "chamber":              item.get("chamber"),
        "issue_date":           (item.get("issueDate") or "")[:10],
        "is_conference_report": bool(item.get("isConferenceReport")),
        "report_type":          rpt_type.upper(),
        "number":               int(number),
        "part":                 item.get("part") or 1,
        "full_url":             _build_full_url(congress, rpt_type, number),
    }


def _parse_citation_to_endpoint(citation: str, url_hint: str | None = None) -> tuple[int, str, int] | None:
    """
    Resolve (congress, type, number) from either the embedded url or by parsing
    the citation string like "H. Rept. 118-125".
    """
    if url_hint:
        # url_hint looks like .../v3/committee-report/118/HRPT/125?format=json
        try:
            parts = url_hint.split("/v3/committee-report/")[1].split("?")[0].split("/")
            if len(parts) >= 3:
                return int(parts[0]), parts[1].upper(), int(parts[2])
        except Exception:
            pass

    if not citation:
        return None
    # "H. Rept. 118-125" → congress=118, type=HRPT, number=125
    import re
    m = re.match(
        r"^\s*(H\.|S\.|Ex\.)\s*Rept\.?\s*(\d+)\s*-\s*(\d+)",
        citation,
        flags=re.IGNORECASE,
    )
    if not m:
        return None
    prefix_map = {"H.": "HRPT", "S.": "SRPT", "Ex.": "ERPT"}
    prefix = m.group(1).rstrip(".") + "."
    rpt_type = prefix_map.get(prefix)
    if not rpt_type:
        return None
    return int(m.group(2)), rpt_type, int(m.group(3))


def fetch_committee_reports_for_bill(bill_data: dict) -> list[dict]:
    """
    Given the raw `bill` dict returned by `bill_fetcher.fetch_bill`, enrich each
    embedded committeeReport with detail-endpoint data and return a list ordered
    most-recent first. Returns [] when no reports exist or the breaker is open.
    """
    embedded = (bill_data or {}).get("committeeReports") or []
    if not embedded:
        return []

    triplets = []
    for entry in embedded:
        triplet = _parse_citation_to_endpoint(entry.get("citation", ""), entry.get("url", ""))
        if triplet:
            triplets.append(triplet)

    if not triplets:
        return []

    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=_PARALLEL_WORKERS) as ex:
        futures = [ex.submit(_fetch_single_report, c, t, n) for c, t, n in triplets]
        done, _ = wait(futures, timeout=_FANOUT_BUDGET_SECONDS)
        for f in done:
            try:
                v = f.result()
                if v:
                    results.append(v)
            except Exception:
                continue

    # Most recent first; reports without a date sort to the end.
    results.sort(key=lambda r: r.get("issue_date") or "", reverse=True)

    log_action(
        agent_name="committee_reports",
        action="fetch_committee_reports_for_bill",
        input_data={"reports_in_bill": len(embedded)},
        output_data={"reports_returned": len(results)},
    )

    return results


if __name__ == "__main__":
    # Quick smoke test against a bill known to have reports (NDAA FY24)
    from sources.bill_fetcher import fetch_bill
    data = fetch_bill(118, "hr", 2670)
    bill = (data or {}).get("bill") or {}
    out = fetch_committee_reports_for_bill(bill)
    print(f"reports: {len(out)}")
    for r in out:
        print(f"  {r['citation']} · {r['committee']} · {r['issue_date']} · {r['title'][:60]}")
        print(f"    {r['full_url']}")
