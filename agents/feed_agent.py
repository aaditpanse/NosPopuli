import os
import json
import hashlib
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from agents.documentor_agent import log_action
from correspondence.db import get_disk_cache, set_disk_cache

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


_FEED_TTL_SECONDS = 3600  # 1 hour

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
    return "feed:v10:" + hashlib.sha1(payload.encode()).hexdigest()


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
    # The state pool reads the legislature's files on this server; a state
    # with none is "unavailable", never an empty that looks like a quiet week.
    import graph
    fetch_state = bool(state_code and state_code.upper() in graph.loaded_states())
    if state_code and not fetch_state:
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
            graph.state_recent_bills, state_code.lower(), 5
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

# ── Local bill tables ──
#
# Since Phase 6 (2026-09-26) the feed reads bill_doc (every bill's title,
# latest action and sponsor, refreshed by the daily sync) and the graph's
# sponsored edges, not Congress.gov or GovInfo search. Fail-open: without
# the database a pool is empty and the feed shows what the others found.

_FEED_COLS = """d.congress, d.bill_type, d.number, d.title, d.introduced, d.latest_action,
                d.latest_action_date, d.is_law, d.law_numbers, d.sponsor_bioguide, d.sponsor_name"""


def _query(sql, args):
    if not os.getenv("DATABASE_URL"):
        return []
    try:
        from psycopg.rows import dict_row
        from correspondence.db import _get_pool
        with _get_pool().connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, args)
            return cur.fetchall()
    except Exception as e:
        print(f"[FEED] bill tables error: {type(e).__name__}: {e}")
        return []


def feed_row(r):
    """A bill_doc row in the feed's bill shape. Pure."""
    acted = r["latest_action_date"].isoformat() if r.get("latest_action_date") else ""
    laws = r.get("law_numbers") or []
    row = {"congress": r["congress"], "type": r["bill_type"], "number": r["number"], "title": r["title"] or "",
           "date": acted or (r["introduced"].isoformat() if r.get("introduced") else ""),
           "latest_action": r.get("latest_action") or "Recently introduced.", "latest_action_date": acted,
           "sponsor_bioguide": r.get("sponsor_bioguide"), "sponsor_name": r.get("sponsor_name") or ""}
    if laws:
        row["is_law"] = True
        row["law_number"] = laws[0].split("-")[-1]
    return row


def member_pool(rows, bioguide_ids):
    """Each member's own bills, newest action first; a member who sponsored
    nothing recent is represented by what they cosponsored. rows carry the
    member (`member`) and the role on the edge. Pure."""
    by_member = {}
    for r in rows:
        by_member.setdefault(r["member"], []).append(r)
    out = []
    for bg in bioguide_ids:
        mine = by_member.get(bg, [])
        pick = [r for r in mine if r["role"] == "sponsor"][:5] or \
            [r for r in mine if r["role"] != "sponsor" and not _is_ceremonial(r["title"])][:5]
        for r in pick:
            out.append({**feed_row(r), "sponsor_bioguide": bg,
                        "feed_rep_role": "sponsor" if r["role"] == "sponsor" else "cosponsor"})
    out.sort(key=lambda b: b.get("date", ""), reverse=True)
    return one_per_member(out)


def _fetch_rep_bills_parallel(bioguide_ids, days_back=180):
    if not bioguide_ids:
        return []
    import graph
    cutoff = (datetime.now() - timedelta(days=days_back)).date()
    ids = {graph.node_id("person", f"bioguide/{b}"): b for b in bioguide_ids}
    rows = _query(f"""SELECT {_FEED_COLS}, e.src, e.props->>'role' AS role FROM graph_edge e
                      JOIN bill_doc d ON d.instrument_id = e.dst
                      WHERE e.src = ANY(%s) AND e.predicate = 'sponsored' AND e.props->>'withdrawn' IS NULL
                        AND d.latest_action_date >= %s
                      ORDER BY d.latest_action_date DESC, d.instrument_id""", (list(ids), cutoff))
    for r in rows:
        r["member"] = ids[r["src"]]
    return member_pool(rows, list(bioguide_ids))


def _fetch_rep_bills(bioguide_ids, days_back=180):
    """Backwards-compatible wrapper for external callers."""
    return _fetch_rep_bills_parallel(bioguide_ids, days_back)


def _fetch_moving_bills(congress, days_back, now, interests):
    """Recent House and Senate bills by latest action, newest first."""
    since = (now - timedelta(days=days_back)).date()
    rows = _query(f"""SELECT {_FEED_COLS} FROM bill_doc d
                      WHERE d.congress = %s AND d.bill_type IN ('hr', 's') AND d.latest_action_date >= %s
                      ORDER BY d.latest_action_date DESC, d.instrument_id LIMIT 50""", (congress, since))
    return [feed_row(r) for r in rows]


def _search_interest_bills(interest, terms, max_results, days_back, now, congress):
    """This Congress's bills whose title, subjects or summary name one of
    the interest's phrases, most recently acted on first."""
    since = (now - timedelta(days=days_back)).date()
    expr = " || ".join("phraseto_tsquery('english', %s)" for _ in terms)
    rows = _query(f"""SELECT {_FEED_COLS} FROM bill_doc d
                      WHERE d.congress = %s AND d.tsv @@ ({expr})
                        AND coalesce(d.latest_action_date, d.introduced) >= %s
                      ORDER BY coalesce(d.latest_action_date, d.introduced) DESC, d.instrument_id LIMIT %s""",
                  # A wide pool: the title gate below keeps the few whose titles
                  # name the interest, and a summary match alone is loose.
                  (congress, *terms, since, max(max_results * 20, 60)))
    return select_interest_hits([feed_row(r) for r in rows], interest, max_results=max_results, fail_open=2)


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