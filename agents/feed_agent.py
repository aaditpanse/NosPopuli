import requests
import os
import json
import hashlib
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed, wait, FIRST_COMPLETED
from datetime import datetime, timedelta
from threading import RLock
from cachetools import TTLCache
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from dotenv import load_dotenv
from agents.documentor_agent import log_action
from agents.state_search_agent import get_recent_state_bills, ENABLED_STATES
from correspondence.db import get_disk_cache, set_disk_cache
from sources.legiscan_client import has_key as legiscan_has_key

# ── Shared HTTP session ──────────────────────────────────────
# Single Session with HTTP keep-alive + a small connection pool eliminates
# the cold-TLS handshake on every Congress.gov call. Costs ~200ms per call
# without it.
_session = requests.Session()
_adapter = HTTPAdapter(
    pool_connections=10,
    pool_maxsize=20,
    max_retries=Retry(total=0, backoff_factor=0),
)
_session.mount("https://", _adapter)
_session.mount("http://", _adapter)

# ── Curation patterns ─────────────────────────────────────────
# Ceremonial / procedural / honorary titles — never relevant to a citizen feed.
# Applied to rep bills, interest bills, and state bills before they enter the pool.
_CEREMONIAL_PATTERNS = (
    "celebrating the", "expressing support for the designation",
    "recognizing the", "honoring the", "congratulating",
    "acknowledging the", "commemorating", "proclaiming",
    "expressing the sense of", "to designate", "to redesignate",
    "to authorize the president to award", "to award a congressional gold medal",
    "to grant the congressional gold medal", "to name", "naming",
    "designating the week", "designating the month", "national day of",
    "national week of", "national month of",
)

# Appropriations bills — substantive but not front-page material.
# Allowed into the ranked list but excluded from the lede + 3-up slots
# by the frontend.
import re as _re
_APPROPRIATIONS_RE = _re.compile(
    r"(?:^|\s)(?:making\s+)?appropriations(?:\s+for|\s+act)|continuing\s+appropriations"
    r"|emergency\s+supplemental\s+appropriations|further\s+continuing\s+appropriations",
    _re.IGNORECASE,
)


def _is_ceremonial(title):
    t = (title or "").lower()
    return any(p in t for p in _CEREMONIAL_PATTERNS)


def _is_appropriations(title):
    return bool(_APPROPRIATIONS_RE.search(title or ""))


# Use the shared api.congress.gov breaker so a trip from any subsystem
# (feed, search, bill detail, member search) suspends every other call site.
from sources.congress_breaker import is_tripped as _breaker_tripped, trip as _trip_breaker, clear as _clear_breaker


# Per-rep sponsored-legislation cache.
#   Fresh tier (in-memory TTLCache, 12h): full hit, no network
#   Stale tier (in-memory dict, no TTL): fallback when live call fails
#   Disk tier (correspondence.db disk_cache, 30d): survives server restarts
# In-flight set prevents concurrent warmers for the same bioguide.
_REP_FRESH_TTL = 12 * 3600
_REP_DISK_TTL  = 30 * 86400
_rep_fresh_cache = TTLCache(maxsize=512, ttl=_REP_FRESH_TTL)
_rep_stale_cache = TTLCache(maxsize=512, ttl=_REP_DISK_TTL)  # was an unbounded dict
_rep_cache_lock = RLock()
_rep_inflight = set()
_rep_inflight_lock = RLock()


def _rep_disk_key(bioguide_id):
    return f"rep_bills:v1:{bioguide_id}"


def _load_rep_from_disk(bioguide_id):
    return get_disk_cache(_rep_disk_key(bioguide_id), max_age_seconds=_REP_DISK_TTL)


def _save_rep_to_disk(bioguide_id, bills):
    if bills:
        set_disk_cache(_rep_disk_key(bioguide_id), bills)

_FEED_TTL_SECONDS = 3600  # 1 hour

load_dotenv()

GOVINFO_API_KEY = os.getenv("GovInfo_API_KEY")
CONGRESS_API_KEY = os.getenv("CONGRESS_API_KEY")

INTEREST_TERMS = {
    "healthcare": [
        "Medicaid", "Medicare", "health insurance coverage",
        "Affordable Care Act", "prescription drug pricing",
        "hospital reimbursement", "healthcare access",
        "public health emergency", "mental health parity",
        "health savings account"
    ],
    "climate": [
        "greenhouse gas emissions", "carbon emissions",
        "clean energy", "renewable energy", "decarbonization",
        "Paris Agreement", "carbon tax", "net zero",
        "climate resilience", "clean electricity"
    ],
    "housing": [
        "affordable housing", "housing assistance",
        "rent stabilization", "homelessness prevention",
        "HUD funding", "eviction moratorium",
        "first-time homebuyer", "housing voucher",
        "public housing", "mortgage relief"
    ],
    "education": [
        "student loan forgiveness", "Pell Grant",
        "higher education funding", "FAFSA",
        "K-12 education", "teacher shortage",
        "school funding", "early childhood education",
        "vocational training", "college affordability"
    ],
    "veterans": [
        "veteran benefits", "VA healthcare",
        "GI Bill", "veteran disability",
        "PTSD treatment",
        "veteran homelessness", "veteran employment",
        "veteran mental health", "VA hospital", "veterans affairs"
    ],
    "economy": [
        "minimum wage", "unemployment insurance",
        "inflation reduction", "job creation",
        "small business loan", "economic growth",
        "Federal Reserve", "deficit reduction",
        "worker protections", "wage theft"
    ],
    "immigration": [
        "DACA", "asylum seeker", "border security",
        "immigration enforcement", "visa reform",
        "pathway to citizenship", "refugee resettlement",
        "immigration detention", "H-1B visa",
        "undocumented immigrant"
    ],
    "gun_policy": [
        "firearm background check", "assault weapon ban",
        "gun violence prevention", "Second Amendment",
        "concealed carry", "red flag law",
        "ghost gun", "bump stock",
        "school shooting", "ATF regulation"
    ],
    "foreign_policy": [
        "NATO alliance", "foreign military aid",
        "economic sanctions", "diplomatic relations",
        "foreign assistance", "defense authorization",
        "China competition", "Ukraine support",
        "arms control", "nuclear nonproliferation"
    ],
    "criminal_justice": [
        "criminal sentencing reform", "prison reform",
        "police accountability", "qualified immunity",
        "mass incarceration", "drug decriminalization",
        "juvenile justice", "reentry program",
        "mandatory minimum", "bail reform"
    ],
    "small_business": [
        "small business administration", "SBA loan",
        "small business tax", "entrepreneur support",
        "Main Street lending", "minority business",
        "small business relief", "startup funding",
        "small business regulation", "franchise"
    ],
    "agriculture": [
        "farm bill", "crop insurance", "USDA program",
        "agricultural subsidy", "rural development",
        "food security", "livestock regulation",
        "organic farming", "agricultural trade",
        "family farm"
    ],
}

# Words that appear in almost every bill title — never use them as topic stems.
_STEM_STOP = {
    "the", "and", "for", "of", "a", "an", "to", "on", "in", "or", "act", "bill",
    "law", "program", "national", "united", "states", "federal", "public",
    "access", "account", "savings", "coverage", "title", "code",
}

_TITLE_BLOCKLIST = (
    "national defense authorization",
    "ndaa",
    "continuing appropriations",
    "technical amendments to update statutory references",
)

# Extra title stems that the canned phrases miss but that are still precise.
_INTEREST_EXTRA_STEMS = {
    "healthcare": (
        "988", "9-8-8", "hospital", "medicaid", "medicare", "mental",
        "nursing", "patient", "physician", "clinic", "opioid", "insulin",
        "vaccine",
    ),
    "veterans": ("veteran", "veterans", "gi bill", "vha", "vba", "va hospital"),
    "housing": ("housing", "rent", "homeless", "eviction", "mortgage", "hud"),
    "climate": ("climate", "carbon", "emissions", "renewable", "decarbon"),
    "education": ("pell", "fafsa", "student loan", "school", "teacher", "skills"),
    "agriculture": ("farm", "usda", "crop", "livestock", "agriculture"),
    "immigration": ("immigration", "asylum", "daca", "border", "refugee"),
    "gun_policy": ("firearm", "gun", "second amendment", "atf"),
    "economy": ("minimum wage", "unemployment", "inflation", "worker"),
    "foreign_policy": ("nato", "sanctions", "ukraine", "arms control"),
    "criminal_justice": ("sentencing", "prison", "police", "bail reform"),
    "small_business": ("small business", "sba", "entrepreneur"),
}

_VA_STEM_RE = _re.compile(r"\bva\b", _re.IGNORECASE)


def current_congress(now=None):
    """House/Senate session number. A new Congress begins January 3 of odd years."""
    dt = now or datetime.now()
    year = dt.year
    if dt.month == 1 and dt.day < 3:
        year -= 1
    return (year - 1787) // 2


def _parse_date(value):
    if not value:
        return None
    raw = str(value).strip()[:10]
    try:
        return datetime.strptime(raw, "%Y-%m-%d")
    except ValueError:
        return None


def _days_since(value, now=None):
    parsed = _parse_date(value)
    if parsed is None:
        return None
    dt = now or datetime.now()
    return (dt.date() - parsed.date()).days


def _within_days(bill, days_back, now=None):
    if days_back is None:
        return True
    stamp = bill.get("latest_action_date") or bill.get("date")
    days = _days_since(stamp, now)
    if days is None:
        return True
    return 0 <= days <= days_back


def _title_blocked(title):
    t = (title or "").lower()
    if _is_appropriations(title) or _is_ceremonial(title):
        return True
    return any(p in t for p in _TITLE_BLOCKLIST)


def _stems_for(interest):
    stems = set(_INTEREST_EXTRA_STEMS.get(interest, ()))
    for phrase in INTEREST_TERMS.get(interest, [interest]):
        p = phrase.lower()
        stems.add(p)
        for w in p.replace("-", " ").split():
            if w not in _STEM_STOP and len(w) >= 5:
                stems.add(w)
    return stems


def title_matches_interest(title, interest):
    t = (title or "").lower()
    if not t:
        return False
    if interest == "veterans" and _VA_STEM_RE.search(t):
        return True
    if interest == "climate" and "waste" in t and any(
        w in t for w in ("jobs", "emissions", "convert", "converting")
    ):
        return True
    for stem in _stems_for(interest):
        if not stem:
            continue
        if len(stem) <= 3:
            if _re.search(r"\b" + _re.escape(stem) + r"\b", t):
                return True
        elif stem in t:
            return True
    return False


def one_per_member(bills):
    """Newest non-ceremonial sponsored bill per bioguide; do not backfill."""
    ordered = sorted(bills or [], key=lambda b: b.get("date") or "", reverse=True)
    seen = set()
    out = []
    for bill in ordered:
        bg = bill.get("sponsor_bioguide") or ""
        if not bg or bg in seen:
            continue
        seen.add(bg)
        out.append(bill)
    return out


def _is_passed_action(action):
    a = (action or "").lower()
    if "motion to reconsider" in a:
        return False
    if "passed house" in a or "agreed to in house" in a:
        return True
    if "passed senate" in a or "agreed to in senate" in a:
        return True
    if "agreed to in" in a or "concurred in" in a:
        return True
    return False


def _is_enacted_bill(bill):
    if bill.get("is_law"):
        return True
    a = (bill.get("latest_action") or "").lower()
    return (
        "became public law" in a
        or "became law" in a
        or "signed into law" in a
        or "signed by president" in a
        or "signed by the president" in a
    )


def _recency_points(bill, now=None):
    days = _days_since(bill.get("latest_action_date") or bill.get("date"), now)
    if days is None:
        return 0
    if days <= 7:
        return 40
    if days <= 30:
        return 25
    if days <= 90:
        return 10
    return 0


def _topic_points(bill, interests):
    reason = bill.get("feed_reason")
    if reason == "your_rep" or reason == "moving" or not reason:
        return 0
    if reason == "state_legislature":
        if any(title_matches_interest(bill.get("title"), i) for i in (interests or [])):
            return 30
        return 0
    if reason in INTEREST_TERMS:
        return 30 if title_matches_interest(bill.get("title"), reason) else 0
    return 0


def _stage_points(bill, now=None, congress=None):
    a = (bill.get("latest_action") or "").lower()
    if _is_enacted_bill(bill):
        days = _days_since(bill.get("latest_action_date") or bill.get("date"), now)
        if days is not None and days <= 30:
            return 12
        return 0
    if "vote scheduled" in a or "scheduled for" in a:
        return 20
    if "legislative calendar" in a or "placed on the calendar" in a:
        return 20
    if "reported by" in a or "ordered to be reported" in a:
        return 16
    if _is_passed_action(a):
        bill_congress = bill.get("congress")
        if congress is not None and bill_congress and int(bill_congress) != int(congress):
            return 0
        return 14
    if "referred" in a or "committee" in a:
        return 8
    if "introduced" in a:
        return 4
    return 0


def _person_points(bill, interests):
    if bill.get("feed_reason") != "your_rep":
        return 0
    if any(title_matches_interest(bill.get("title"), i) for i in (interests or [])):
        return 20
    return 15


def score_feed_item(bill, interests, now=None, congress=None):
    dt = now or datetime.now()
    cg = congress if congress is not None else current_congress(dt)
    recency = _recency_points(bill, dt)
    topic = _topic_points(bill, interests)
    stage = _stage_points(bill, dt, cg)
    person = _person_points(bill, interests)
    return recency + topic + stage + person, {
        "recency": recency,
        "topic": topic,
        "stage": stage,
        "person": person,
    }


def rank_feed_items(items, interests, now=None, congress=None):
    dt = now or datetime.now()
    cg = congress if congress is not None else current_congress(dt)
    scored = []
    for bill in items or []:
        row = dict(bill)
        score, signals = score_feed_item(row, interests, dt, cg)
        row["feed_score"] = score
        row["feed_signals"] = signals
        row["headline_eligible"] = is_headline_eligible(row, dt, cg)
        scored.append(row)
    def _date_ord(bill):
        parsed = _parse_date(bill.get("latest_action_date") or bill.get("date"))
        return parsed.timestamp() if parsed else 0.0

    scored.sort(key=lambda b: (
        -b["feed_score"],
        1 if b.get("is_appropriations") else 0,
        -_date_ord(b),
    ))
    return scored


def is_headline_eligible(bill, now=None, congress=None):
    if bill.get("is_appropriations") or _title_blocked(bill.get("title")):
        return False
    dt = now or datetime.now()
    cg = congress if congress is not None else current_congress(dt)
    if _is_enacted_bill(bill):
        bc = bill.get("congress")
        if bc and int(bc) < int(cg):
            return False
        days = _days_since(bill.get("latest_action_date") or bill.get("date"), dt)
        if days is None or days > 30:
            return False
    return True


def headline_slots(items):
    """Lede + 3-up from already-ranked items; appropriations and stale laws never headline.

    If a your_rep bill exists but is not in the top four, it takes the last
    3-up slot so the delegation is visible without stealing the lede.
    """
    eligible = [
        b for b in (items or [])
        if b.get("headline_eligible", not b.get("is_appropriations"))
    ]
    lede = eligible[0] if eligible else None
    top3 = list(eligible[1:4])
    front = [b for b in [lede, *top3] if b is not None]
    if front and not any(b.get("feed_reason") == "your_rep" for b in front):
        pick = next((b for b in eligible if b.get("feed_reason") == "your_rep"), None)
        if pick is not None and pick is not lede:
            if len(top3) < 3:
                if pick not in top3:
                    top3.append(pick)
            else:
                top3 = top3[:-1] + [pick]
    used = {id(b) for b in [lede, *top3] if b is not None}
    rest = [b for b in (items or []) if id(b) not in used]
    return lede, top3, rest


def order_for_layout(items):
    lede, top3, rest = headline_slots(items)
    front = [b for b in [lede, *top3] if b is not None]
    return front + rest


def _is_moving_action(action):
    a = (action or "").lower()
    if "motion to reconsider" in a:
        return False
    if "legislative calendar" in a or "placed on the calendar" in a:
        return True
    if "vote scheduled" in a or "scheduled for" in a:
        return True
    if "reported by" in a or "ordered to be reported" in a:
        return True
    return _is_passed_action(a)


def _title_key(title):
    t = (title or "").lower()
    if ";" in t:
        t = t.split(";")[-1]
    return _re.sub(r"[^a-z0-9]+", " ", t).strip()


def _stage_sort_value(bill):
    return _stage_points(bill)


def dedupe_companions(items):
    """Collapse House/Senate twins that share a short title; keep better stage, then newer."""
    groups = {}
    leftover = []
    for bill in items or []:
        key = _title_key(bill.get("title"))
        if len(key) < 12:
            leftover.append(bill)
            continue
        groups.setdefault(key, []).append(bill)
    out = []
    for group in groups.values():
        if len(group) == 1:
            out.append(group[0])
            continue
        def _newer(b):
            parsed = _parse_date(b.get("latest_action_date") or b.get("date"))
            return parsed.timestamp() if parsed else 0.0
        group.sort(key=lambda b: (-_stage_sort_value(b), -_newer(b)))
        out.append(group[0])
    return out + leftover


def select_interest_hits(rows, interest, max_results=3, fail_open=2):
    """Title-gated hits, or up to fail_open unblocked rows if the gate is empty."""
    gated = []
    opened = []
    for row in rows or []:
        title = row.get("title")
        if _title_blocked(title):
            continue
        if title_matches_interest(title, interest):
            if len(gated) < max_results:
                gated.append(row)
        elif len(opened) < fail_open:
            opened.append(row)
    return gated if gated else opened


def filter_state_bills(bills, interests, days_back=None, now=None):
    recent = []
    for bill in bills or []:
        if _is_ceremonial(bill.get("title")):
            continue
        if not _within_days(bill, days_back, now):
            continue
        recent.append(bill)
    if not interests:
        return recent
    matched = [
        b for b in recent
        if any(title_matches_interest(b.get("title"), i) for i in interests)
    ]
    return matched if matched else recent


def tag_moving_item(bill, interests):
    title = bill.get("title")
    if _title_blocked(title) or _is_ceremonial(title):
        return None
    matched = next((i for i in (interests or []) if title_matches_interest(title, i)), None)
    if not matched and not _is_moving_action(bill.get("latest_action")):
        return None
    row = dict(bill)
    if matched:
        row["feed_reason"] = matched
        row["feed_interest"] = matched
    else:
        row["feed_reason"] = "moving"
    return row


def _feed_cache_key(interests, senator_bioguides, rep_bioguide, state_code):
    payload = json.dumps({
        "i": sorted(interests or []),
        "s": sorted(senator_bioguides or []),
        "r": rep_bioguide or "",
        "st": state_code or "",
    }, sort_keys=True)
    return "feed:v9:" + hashlib.sha1(payload.encode()).hexdigest()


def fetch_feed(interests, senator_bioguides, rep_bioguide, days_back=60, max_per_interest=3, state_code=None):
    """
    Generates a personalized feed based on user interests and representatives.

    interests: list of interest keys e.g. ["healthcare", "climate"]
    senator_bioguides: list of senator bioguide IDs
    rep_bioguide: house rep bioguide ID

    Returns {"items": [...], "state_status": skipped|unavailable|empty|ok}.
    """
    now = datetime.now()
    congress = current_congress(now)
    db_key = _feed_cache_key(interests, senator_bioguides, rep_bioguide, state_code)
    cached = get_disk_cache(db_key, max_age_seconds=_FEED_TTL_SECONDS)
    if cached is not None:
        n = len(cached.get("items") or []) if isinstance(cached, dict) else len(cached)
        print(f"[FEED] Disk cache hit — {n} items")
        return cached

    feed_items = []
    seen_bills = set()
    state_status = "skipped"

    all_bioguides = list(senator_bioguides or []) + ([rep_bioguide] if rep_bioguide else [])
    state_enabled = bool(state_code and state_code.upper() in ENABLED_STATES)
    fetch_state = state_enabled and legiscan_has_key()
    if state_enabled and not legiscan_has_key():
        state_status = "unavailable"

    with ThreadPoolExecutor(max_workers=8) as ex:
        rep_future = ex.submit(_fetch_rep_bills_parallel, all_bioguides, 90)
        moving_future = ex.submit(_fetch_moving_bills, congress, days_back, now, interests or [])
        interest_futures = [
            ex.submit(
                _search_interest_bills,
                i,
                INTEREST_TERMS.get(i, [i]),
                max_per_interest,
                days_back,
                now,
                congress,
            )
            for i in (interests or [])
        ]
        state_future = ex.submit(
            get_recent_state_bills, state_code.upper(), 5
        ) if fetch_state else None

        rep_bills = rep_future.result()
        moving_raw = moving_future.result()
        interest_results = [f.result() for f in interest_futures]
        state_bills = state_future.result() if state_future else []

    def _key(bill):
        if bill.get("is_state_bill"):
            return f"state-{bill.get('identifier', '')}"
        return f"{bill.get('type','')}{bill.get('number','')}"

    rep_pool = []
    for bill in one_per_member(rep_bills):
        if _is_ceremonial(bill.get("title")):
            continue
        key = _key(bill)
        if key not in seen_bills:
            seen_bills.add(key)
            bill["feed_reason"] = "your_rep"
            bill.setdefault("feed_rep_role", "sponsor")
            if _is_appropriations(bill.get("title")):
                bill["is_appropriations"] = True
            rep_pool.append(bill)

    moving_pool = []
    for bill in moving_raw:
        tagged = tag_moving_item(bill, interests or [])
        if not tagged:
            continue
        key = _key(tagged)
        if key in seen_bills:
            continue
        seen_bills.add(key)
        moving_pool.append(tagged)
        if len(moving_pool) >= 8:
            break

    interest_pool = []
    for interest, bills in zip(interests or [], interest_results):
        for bill in bills:
            key = _key(bill)
            if key in seen_bills:
                continue
            seen_bills.add(key)
            bill["feed_reason"] = interest
            bill["feed_interest"] = interest
            interest_pool.append(bill)

    _enrich_latest_actions(rep_pool + interest_pool)
    interest_pool = [b for b in interest_pool if _within_days(b, days_back, now)]
    feed_items.extend(rep_pool)
    feed_items.extend(moving_pool)
    feed_items.extend(interest_pool)

    state_kept = filter_state_bills(state_bills, interests or [], days_back, now)
    kept_state = 0
    for bill in state_kept:
        key = _key(bill)
        if key in seen_bills:
            continue
        seen_bills.add(key)
        bill["feed_reason"] = "state_legislature"
        bill["feed_interest"] = "state"
        if _is_appropriations(bill.get("title")):
            bill["is_appropriations"] = True
        feed_items.append(bill)
        kept_state += 1

    if fetch_state:
        state_status = "ok" if kept_state else "empty"

    feed_items = dedupe_companions(feed_items)
    ranked = rank_feed_items(feed_items, interests, now=now, congress=congress)
    ranked = order_for_layout(ranked)

    log_action(
        agent_name="feed",
        action="fetch_feed",
        input_data={
            "interests": interests,
            "reps": all_bioguides,
            "state_code": state_code,
        },
        output_data={"total_items": len(ranked), "state_status": state_status}
    )

    payload = {"items": ranked, "state_status": state_status}
    if ranked:
        set_disk_cache(db_key, payload)
    return payload

def _fetch_bill_detail(congress, bill_type, number, timeout=8):
    url = f"https://api.congress.gov/v3/bill/{congress}/{bill_type}/{number}"
    try:
        r = requests.get(url, params={"api_key": CONGRESS_API_KEY, "format": "json"}, timeout=timeout)
        if r.status_code == 200:
            return r.json().get("bill") or {}
        return {"_error": r.status_code}
    except Exception as e:
        return {"_error": str(e.__class__.__name__)}


def _fetch_bill_detail_resilient(congress, bill_type, number):
    """Two attempts: fast first, equally short retry. A 15s second try rarely
    succeeds when the first 8s call already failed; it just inflates the tail."""
    detail = _fetch_bill_detail(congress, bill_type, number, timeout=6)
    if detail and "_error" not in detail:
        return detail
    detail = _fetch_bill_detail(congress, bill_type, number, timeout=6)
    if detail and "_error" not in detail:
        return detail
    return None


_ENRICH_BUDGET_SECONDS = 10


def _enrich_latest_actions(bills):
    """Populate latest_action, latest_action_date, is_law, law_number in parallel.

    Wall-clock budgeted: any future that hasn't returned by the deadline
    falls back to "Recently introduced" rather than blocking the whole feed.
    """
    if not bills:
        return
    with ThreadPoolExecutor(max_workers=6) as ex:
        futures = {
            ex.submit(_fetch_bill_detail_resilient, b.get("congress"), b.get("type"), b.get("number")): b
            for b in bills
        }
        done, not_done = wait(futures.keys(), timeout=_ENRICH_BUDGET_SECONDS)
        for fut in not_done:
            bill = futures[fut]
            if not bill.get("latest_action"):
                bill["latest_action"] = "Recently introduced."
                if not bill.get("latest_action_date") and bill.get("date"):
                    bill["latest_action_date"] = bill["date"]
            fut.cancel()
        for fut in done:
            bill = futures[fut]
            detail = fut.result()
            if not detail:
                if not bill.get("latest_action"):
                    bill["latest_action"] = "Recently introduced."
                    if not bill.get("latest_action_date") and bill.get("date"):
                        bill["latest_action_date"] = bill["date"]
                continue
            la = detail.get("latestAction") or {}
            bill["latest_action"] = la.get("text", "") or bill.get("latest_action", "")
            bill["latest_action_date"] = la.get("actionDate", "") or bill.get("latest_action_date", "")
            laws = detail.get("laws") or []
            if laws:
                bill["is_law"] = True
                law_num = (laws[0].get("number") or "").split("-")[-1]
                if law_num:
                    bill["law_number"] = law_num
            sponsors = detail.get("sponsors") or []
            if sponsors and not bill.get("sponsor_name"):
                s = sponsors[0]
                name = s.get("fullName") or s.get("directOrderName") or ""
                party = s.get("party") or ""
                state = s.get("state") or ""
                tag = f" ({party}-{state})" if party and state else ""
                bill["sponsor_name"] = f"{name}{tag}".strip()
                if not bill.get("sponsor_bioguide"):
                    bill["sponsor_bioguide"] = s.get("bioguideId")
            # Defensive: even if we got a 200 with no latestAction (rare), fall back
            if not bill.get("latest_action"):
                bill["latest_action"] = "Recently introduced."
                if not bill.get("latest_action_date") and bill.get("date"):
                    bill["latest_action_date"] = bill["date"]


def _parse_member_bills(raw, bioguide_id, role):
    out = []
    for bill in raw or []:
        if not (bill.get("title") and bill.get("number") and bill.get("type")):
            continue
        date = (bill.get("latestAction") or {}).get("actionDate", "")
        out.append({
            "congress": bill.get("congress"),
            "type": (bill.get("type") or "").lower(),
            "number": bill.get("number"),
            "title": bill.get("title", ""),
            "date": date,
            "sponsor_bioguide": bioguide_id,
            "latest_action": (bill.get("latestAction") or {}).get("text", ""),
            "feed_rep_role": role,
        })
    return out


def _live_fetch_rep(bioguide_id, timeout):
    """Single live call against Congress.gov. Returns list or raises."""
    url = f"https://api.congress.gov/v3/member/{bioguide_id}/sponsored-legislation"
    params = {"api_key": CONGRESS_API_KEY, "format": "json", "limit": 5}
    r = _session.get(url, params=params, timeout=timeout)
    if r.status_code != 200:
        raise RuntimeError(f"status {r.status_code}")
    return _parse_member_bills(r.json().get("sponsoredLegislation", []), bioguide_id, "sponsor")


def _store_rep(bioguide_id, bills):
    with _rep_cache_lock:
        _rep_fresh_cache[bioguide_id] = bills
        _rep_stale_cache[bioguide_id] = bills
    _save_rep_to_disk(bioguide_id, bills)


def _live_fetch_rep_no_limit(bioguide_id, timeout):
    """Fallback live call without limit param — in case the server-side
    'limit' handling is what's actually slow. Trims to 5 client-side."""
    url = f"https://api.congress.gov/v3/member/{bioguide_id}/sponsored-legislation"
    params = {"api_key": CONGRESS_API_KEY}
    r = _session.get(url, params=params, timeout=timeout)
    if r.status_code != 200:
        raise RuntimeError(f"status {r.status_code}")
    return _parse_member_bills(r.json().get("sponsoredLegislation", [])[:5], bioguide_id, "sponsor")


def _warm_rep_in_background(bioguide_id):
    """One-shot warmer: tries canonical + no-limit variants with short
    timeouts, trips the breaker on full failure, then exits. No spammy
    retry loops — the breaker keeps the next feed request from re-dispatching
    until the cooldown clears. Most outages clear within the 15-min window;
    if not, the user's next refresh after that dispatches a single fresh try."""
    if _breaker_tripped():
        return
    with _rep_inflight_lock:
        if bioguide_id in _rep_inflight:
            return
        _rep_inflight.add(bioguide_id)

    def attempt(label, fn, timeout):
        try:
            bills = fn(bioguide_id, timeout=timeout)
            _store_rep(bioguide_id, bills)
            _clear_breaker()
            print(f"[FEED] Warmer SUCCEEDED for {bioguide_id} via {label} ({len(bills)} bills)")
            return True
        except Exception as e:
            print(f"[FEED] Warmer {label} for {bioguide_id} failed: {type(e).__name__}")
            return False

    def runner():
        try:
            for to in (15, 30):
                if attempt(f"canonical t={to}s", _live_fetch_rep, to):
                    return
                if _breaker_tripped():
                    return
            for to in (15, 30):
                if attempt(f"no-limit t={to}s", _live_fetch_rep_no_limit, to):
                    return
                if _breaker_tripped():
                    return
            _trip_breaker()
        finally:
            with _rep_inflight_lock:
                _rep_inflight.discard(bioguide_id)

    t = threading.Thread(target=runner, daemon=True, name=f"rep-warm-{bioguide_id}")
    t.start()


def _fetch_one_rep(bioguide_id, cutoff):
    # Tier 1: in-memory fresh cache
    with _rep_cache_lock:
        cached = _rep_fresh_cache.get(bioguide_id)
    if cached is not None:
        return [b for b in cached if b.get("date", "") >= cutoff]

    # Tier 2: disk cache (survives restarts)
    disk_cached = _load_rep_from_disk(bioguide_id)
    if disk_cached is not None:
        with _rep_cache_lock:
            _rep_fresh_cache[bioguide_id] = disk_cached
            _rep_stale_cache[bioguide_id] = disk_cached
        # Disk hit — refresh in background so the next call has fresher data
        _warm_rep_in_background(bioguide_id)
        return [b for b in disk_cached if b.get("date", "") >= cutoff]

    # Tier 3: breaker check — if Congress.gov is in cooldown, don't try
    if _breaker_tripped():
        with _rep_cache_lock:
            stale = _rep_stale_cache.get(bioguide_id)
        if stale is not None:
            return [b for b in stale if b.get("date", "") >= cutoff]
        return []

    # Tier 4: try a short live call so first-ever loads have a shot
    try:
        bills = _live_fetch_rep(bioguide_id, timeout=8)
        _store_rep(bioguide_id, bills)
        _clear_breaker()
        return [b for b in bills if b.get("date", "") >= cutoff]
    except Exception as e:
        with _rep_cache_lock:
            stale = _rep_stale_cache.get(bioguide_id)
        _warm_rep_in_background(bioguide_id)
        if stale is not None:
            print(f"[FEED] Live rep fetch failed for {bioguide_id} ({type(e).__name__}); serving stale ({len(stale)} bills)")
            return [b for b in stale if b.get("date", "") >= cutoff]
        print(f"[FEED] Rep cold-load failed for {bioguide_id} ({type(e).__name__}); background warmer dispatched")
        return []


def _fetch_rep_bills_parallel(bioguide_ids, days_back=180):
    if not bioguide_ids:
        return []
    cutoff = (datetime.now() - timedelta(days=days_back)).strftime("%Y-%m-%d")
    bills = []
    with ThreadPoolExecutor(max_workers=min(8, len(bioguide_ids))) as ex:
        futures = [ex.submit(_fetch_one_rep, bg, cutoff) for bg in bioguide_ids]
        done, not_done = wait(futures, timeout=8)
        for f in not_done:
            f.cancel()
        for f in done:
            bills.extend(f.result())
    bills.sort(key=lambda b: b.get("date", ""), reverse=True)
    kept = one_per_member(bills)
    have = {b.get("sponsor_bioguide") for b in kept if b.get("sponsor_bioguide")}
    missing = [bg for bg in bioguide_ids if bg not in have]
    if missing and not _breaker_tripped():
        with ThreadPoolExecutor(max_workers=min(4, len(missing))) as ex:
            futures = [ex.submit(_fetch_one_cosponsor, bg, cutoff) for bg in missing]
            done, not_done = wait(futures, timeout=4)
            for f in not_done:
                f.cancel()
            extra = []
            for f in done:
                extra.extend(f.result())
            kept = one_per_member(kept + extra)
    return kept


def _fetch_one_cosponsor(bioguide_id, cutoff):
    try:
        bills = _live_fetch_cosponsor(bioguide_id, timeout=6)
    except Exception:
        return []
    return [
        b for b in bills
        if b.get("date", "") >= cutoff and not _is_ceremonial(b.get("title"))
    ]


def _live_fetch_cosponsor(bioguide_id, timeout):
    url = f"https://api.congress.gov/v3/member/{bioguide_id}/cosponsored-legislation"
    params = {"api_key": CONGRESS_API_KEY, "format": "json", "limit": 5}
    r = _session.get(url, params=params, timeout=timeout)
    if r.status_code != 200:
        raise RuntimeError(f"status {r.status_code}")
    return _parse_member_bills(r.json().get("cosponsoredLegislation", []), bioguide_id, "cosponsor")


def _fetch_rep_bills(bioguide_ids, days_back=180):
    """Backwards-compatible wrapper for external callers."""
    return _fetch_rep_bills_parallel(bioguide_ids, days_back)

def _fetch_moving_bills(congress, days_back, now, interests):
    """Recent House/Senate bills by latest action, not by publish date."""
    if _breaker_tripped():
        return []
    since = (now - timedelta(days=days_back)).strftime("%Y-%m-%dT00:00:00Z")

    def _one(btype):
        try:
            r = _session.get(
                f"https://api.congress.gov/v3/bill/{congress}/{btype}",
                params={
                    "api_key": CONGRESS_API_KEY,
                    "format": "json",
                    "limit": 25,
                    "fromDateTime": since,
                    "sort": "updateDate+desc",
                },
                timeout=8,
            )
        except Exception as e:
            print(f"[FEED] Moving list {btype} error: {type(e).__name__}")
            return []
        if r.status_code != 200:
            return []
        out = []
        for b in r.json().get("bills") or []:
            title = b.get("title") or ""
            if not (b.get("number") and b.get("type")):
                continue
            la = b.get("latestAction") or {}
            out.append({
                "congress": b.get("congress") or congress,
                "type": (b.get("type") or btype).lower(),
                "number": b.get("number"),
                "title": title,
                "date": la.get("actionDate", ""),
                "latest_action": la.get("text", ""),
                "latest_action_date": la.get("actionDate", ""),
            })
        return out

    bills = []
    with ThreadPoolExecutor(max_workers=2) as ex:
        futs = [ex.submit(_one, t) for t in ("hr", "s")]
        done, not_done = wait(futs, timeout=8)
        for f in not_done:
            f.cancel()
        for f in done:
            bills.extend(f.result())
    bills.sort(key=lambda b: b.get("latest_action_date") or b.get("date") or "", reverse=True)
    return bills


def _search_interest_bills(interest, terms, max_results, days_back, now, congress):
    """Search GovInfo for recent bills matching interest terms."""
    terms_query = " OR ".join(f'"{t}"' if " " in str(t) else str(t) for t in terms)
    full_query = f"({terms_query}) collection:BILLS congress:{congress}"
    payload = {
        "query": full_query,
        "pageSize": max(max_results * 5, 15),
        "offsetMark": "*",
        "sorts": [{"field": "publishdate", "sortOrder": "DESC"}]
    }

    try:
        response = requests.post(
            "https://api.govinfo.gov/search",
            json=payload,
            params={"api_key": GOVINFO_API_KEY},
            timeout=10
        )
        if response.status_code != 200:
            return []

        parsed = []
        for item in response.json().get("results", []):
            package_id = item.get("packageId", "")
            raw = package_id.replace("BILLS-", "")
            m = _re.match(r"(\d+)([a-z]+)(\d+)", raw)
            if not m:
                continue
            row = {
                "congress": int(m.group(1)),
                "type": m.group(2),
                "number": int(m.group(3)),
                "title": item.get("title", ""),
                "date": item.get("dateIssued", ""),
                "latest_action": "",
            }
            if not _within_days(row, days_back, now):
                continue
            parsed.append(row)
        return select_interest_hits(parsed, interest, max_results=max_results, fail_open=2)

    except Exception as e:
        print(f"[FEED] Search error: {e}")
        return []

if __name__ == "__main__":
    print("FEED AGENT TEST")
    print("-" * 40)
    
    # Simulate a Vermont user who cares about healthcare and climate
    result = fetch_feed(
        interests=["healthcare", "climate"],
        senator_bioguides=["S000033", "W000800"],  # Sanders, Welch
        rep_bioguide="B001311",                     # Balint
        days_back=60
    )
    items = result["items"] if isinstance(result, dict) else result
    print(f"Feed items: {len(items)}  state={result.get('state_status') if isinstance(result, dict) else '?'}")
    print()
    for item in items:
        print(f"  [{item['feed_reason']}] {item.get('type','').upper()}{item.get('number','')} — {item.get('title','')[:60]}")
        print(f"  Date: {item.get('date','')}  score={item.get('feed_score')}")
        print()