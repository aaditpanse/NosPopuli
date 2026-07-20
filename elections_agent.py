import os
import re
import json
import asyncio
import datetime
import httpx
from threading import RLock
from cachetools import TTLCache
from dotenv import load_dotenv
from anthropic import AsyncAnthropic
from documentor_agent import log_action
from correspondence.db import get_elections_cache, set_elections_cache, get_disk_cache, set_disk_cache, get_known_elections

load_dotenv()

CIVIC_API_KEY = os.getenv("GOOGLE_CIVIC_API_KEY", "")
CIVIC_BASE = "https://www.googleapis.com/civicinfo/v2"

_elections_cache = TTLCache(maxsize=200, ttl=21600)   # 6hr per zip
_web_search_cache = TTLCache(maxsize=60, ttl=172800)  # 48hr per state — Claude web search
_polling_cache = TTLCache(maxsize=100, ttl=21600)     # 6hr per election — polling data
_cache_lock = RLock()

# In-flight request dedup. When multiple users land on the home page at the
# same time, we want one (cache_key) → one upstream fetch — not N concurrent
# fetches all racing to populate the same cache slot.
_inflight_locks: dict = {}
_inflight_locks_guard = RLock()


def _get_inflight_lock(key) -> asyncio.Lock:
    with _inflight_locks_guard:
        lock = _inflight_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            _inflight_locks[key] = lock
        return lock

PARTY_NORMALIZE = {
    "democratic": "Democratic",
    "democrat": "Democratic",
    "dem": "Democratic",
    "republican": "Republican",
    "rep": "Republican",
    "gop": "Republican",
    "independent": "Independent",
    "ind": "Independent",
    "libertarian": "Libertarian",
    "green": "Green",
    "nonpartisan": "Nonpartisan",
}

CHANNEL_ICONS = {
    "Twitter": "𝕏",
    "X": "𝕏",
    "Facebook": "fb",
    "YouTube": "yt",
    "GooglePlus": "g+",
}


def _normalize_party(raw):
    if not raw:
        return None
    return PARTY_NORMALIZE.get(raw.lower().strip(), raw.strip())


def _party_color(party):
    p = (party or "").lower()
    if "democrat" in p:
        return "dem"
    if "republican" in p or "gop" in p:
        return "rep"
    if "libertarian" in p:
        return "lib"
    if "green" in p:
        return "grn"
    return "ind"


def _format_candidate(c):
    channels = []
    for ch in c.get("channels", []):
        t = ch.get("type", "")
        channels.append({"type": t, "id": ch.get("id", ""), "icon": CHANNEL_ICONS.get(t, t[:2])})
    party = _normalize_party(c.get("party"))
    return {
        "name": c.get("name", ""),
        "party": party,
        "party_color": _party_color(party),
        "photo_url": c.get("photoUrl"),
        "candidate_url": c.get("candidateUrl"),
        "email": c.get("email"),
        "phone": c.get("phone"),
        "channels": channels,
    }


def _format_contest(contest):
    candidates = [_format_candidate(c) for c in contest.get("candidates", [])]
    return {
        "type": contest.get("type", ""),
        "office": contest.get("office", ""),
        "district": (contest.get("district") or {}).get("name"),
        "level": contest.get("level", []),
        "candidates": candidates,
    }


def _days_until(date_str):
    """Return days from today to date_str (YYYY-MM-DD). Negative = past."""
    try:
        target = datetime.date.fromisoformat(date_str)
        today = datetime.date.today()
        return (target - today).days
    except Exception:
        return None


def _ballotpedia_url(election_name, date_str):
    year = date_str[:4] if date_str else ""
    slug = election_name.replace(" ", "_")
    return f"https://ballotpedia.org/{slug},_{year}"


async def _search_elections_with_claude(state_name, state_code):
    """
    Use Claude with web search to find upcoming elections not covered by Google Civic.
    Result cached 24hr per state — called at most once per state per day.
    """
    today = datetime.date.today()
    try:
        anthropic_client = AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
        response = await anthropic_client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=512,
            tools=[{"type": "web_search_20260209", "name": "web_search"}],
            messages=[{
                "role": "user",
                "content": (
                    f"Search for all upcoming elections scheduled in {state_name} in 2026 and 2027. "
                    f"Today is {today}. Include state primaries, general elections, special elections, and runoffs. "
                    f"After searching, respond with ONLY a raw JSON array (no markdown fences, no explanation). "
                    f'Format: [{{"name": "Virginia Primary Election", "date": "2026-08-04", "type": "primary"}}, ...] '
                    f"Use YYYY-MM-DD for dates. If nothing found, respond with exactly: []"
                ),
            }],
        )
        text = "".join(b.text for b in response.content if hasattr(b, "text"))
        print(f"[ELECTIONS] Claude raw response for {state_code}: {text[:300]}")

        # Try parsing whole text as JSON first
        stripped = text.strip()
        try:
            parsed = json.loads(stripped)
            if isinstance(parsed, list):
                print(f"[ELECTIONS] Claude found {len(parsed)} elections for {state_code}")
                return parsed
        except json.JSONDecodeError:
            pass

        # Greedy regex — `.*?` (non-greedy) would stop at first `]` inside a nested object
        match = re.search(r"\[.*\]", stripped, re.DOTALL)
        if match:
            parsed = json.loads(match.group())
            print(f"[ELECTIONS] Claude found {len(parsed)} elections for {state_code} (regex)")
            return parsed

        print(f"[ELECTIONS] Claude returned no parseable JSON for {state_code}")
    except Exception as e:
        print(f"[ELECTIONS] Claude search error for {state_code}: {e}")
    return []


async def _fetch_all_elections(client):
    """Fetch the full list of elections from Google Civic."""
    if not CIVIC_API_KEY:
        return []
    try:
        r = await client.get(
            f"{CIVIC_BASE}/elections",
            params={"key": CIVIC_API_KEY},
            timeout=10,
        )
        if r.status_code != 200:
            print(f"[ELECTIONS] /elections error {r.status_code}: {r.text[:200]}")
            return []
        return r.json().get("elections", [])
    except Exception as e:
        print(f"[ELECTIONS] /elections exception: {e}")
        return []


async def _fetch_voter_info(client, election_id, address, semaphore):
    """Fetch ballot contests and voter info for one election."""
    async with semaphore:
        try:
            r = await client.get(
                f"{CIVIC_BASE}/voterinfo",
                params={
                    "key": CIVIC_API_KEY,
                    "address": address,
                    "electionId": election_id,
                },
                timeout=10,
            )
            if r.status_code == 200:
                return r.json()
            # 400 is common when the election has no data for that zip
            return None
        except Exception:
            return None


STATE_NAMES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas",
    "CA": "California", "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware",
    "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho",
    "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
    "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland",
    "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi",
    "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
    "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
    "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma",
    "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina",
    "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah",
    "VT": "Vermont", "VA": "Virginia", "WA": "Washington", "WV": "West Virginia",
    "WI": "Wisconsin", "WY": "Wyoming", "DC": "District of Columbia",
}


def _election_affects_user(election_name, contests, state_code):
    """
    Return True if this election is relevant to the user's state,
    or if it's a presidential / national election.
    """
    if not state_code:
        return False
    name_lower = (election_name or "").lower()
    if "presidential" in name_lower or "president" in name_lower:
        return True

    # Check if any returned contest is at federal/national level
    for c in contests:
        levels = c.get("level", [])
        if "federal" in levels or "country" in levels:
            return True

    # Match by state full name in election title (most reliable)
    state_name = STATE_NAMES.get(state_code.upper(), "")
    if state_name and state_name.lower() in name_lower:
        return True

    # Match by voterinfo contests scoped to user's state
    for c in contests:
        office = (c.get("office", "") + " " + (c.get("district") or "")).lower()
        if state_name.lower() in office or state_code.lower() in office:
            return True

    return False


async def fetch_elections(zip_code, state_code=None):
    """
    Main entry point. Returns {upcoming: [...], recent: [...]}.
    Results cached per zip for 6 hours.
    """
    cache_key = (zip_code or "national", state_code or "")
    # v2 prefix introduced after a poisoned empty result wedged the 6hr cache
    # for an entire region. Bumping invalidates v1 rows lazily.
    db_key = f"elections:v3:{zip_code or 'national'}:{state_code or ''}"
    with _cache_lock:
        if cache_key in _elections_cache:
            print(f"[ELECTIONS] Returning cached result for {cache_key}")
            return _elections_cache[cache_key]

    # Check disk cache — survives server restarts
    disk_result = get_disk_cache(db_key, max_age_seconds=21600)  # 6 hours
    if disk_result is not None:
        print(f"[ELECTIONS] Disk cache hit for {cache_key}")
        with _cache_lock:
            _elections_cache[cache_key] = disk_result
        return disk_result

    # In-flight dedup — if another request is already fetching this key, wait
    # for it instead of starting a parallel fetch. The first one in does the
    # work; everyone else picks up the cached result on the second pass.
    lock = _get_inflight_lock(cache_key)
    async with lock:
        # Re-check both caches now that we hold the lock — the previous holder
        # may have just populated them.
        with _cache_lock:
            if cache_key in _elections_cache:
                return _elections_cache[cache_key]
        disk_result = get_disk_cache(db_key, max_age_seconds=21600)
        if disk_result is not None:
            with _cache_lock:
                _elections_cache[cache_key] = disk_result
            return disk_result

        print(f"[ELECTIONS] Cache miss for {cache_key} — fetching fresh")

        result = await _compute_elections(zip_code, state_code)

        # Cache write happens inside the lock so the next waiter sees it on
        # their re-check. Only cache results that actually have content —
        # empty {upcoming: [], recent: []} from a transient upstream blip
        # used to wedge the 6hr TTL with garbage. A short in-memory dedup
        # entry is fine for thundering herds; persisting is not.
        has_content = bool(result and (result.get("upcoming") or result.get("recent")))
        with _cache_lock:
            if has_content:
                _elections_cache[cache_key] = result
        if has_content and not result.get("error"):
            set_disk_cache(db_key, result)
        return result


async def _compute_elections(zip_code, state_code):
    """The actual upstream-fetch path, separated so the cache wrapper above
    stays readable. Returns the same shape as fetch_elections."""
    address = f"{zip_code} USA" if zip_code else "Washington DC USA"
    today = datetime.date.today()
    cutoff_past = today - datetime.timedelta(days=60)
    cutoff_future = today + datetime.timedelta(days=548)  # ~18 months out

    relevant = []
    voter_infos = []
    if CIVIC_API_KEY:
        async with httpx.AsyncClient() as client:
            all_elections = await _fetch_all_elections(client)

            # Filter to relevant time window, excluding test/placeholder entries
            for e in all_elections:
                name = e.get("name", "")
                if "test" in name.lower() or "vip test" in name.lower():
                    continue
                date_str = e.get("electionDay", "")
                if not date_str:
                    continue
                try:
                    edate = datetime.date.fromisoformat(date_str)
                except Exception:
                    continue
                if edate >= cutoff_past and edate <= cutoff_future:
                    relevant.append((e, edate))

            # Fetch voter info for all relevant elections concurrently (max 5 at once)
            semaphore = asyncio.Semaphore(5)
            voter_info_tasks = [
                _fetch_voter_info(client, e["id"], address, semaphore)
                for e, _ in relevant
            ]
            voter_infos = await asyncio.gather(*voter_info_tasks)
    else:
        print("[ELECTIONS] CIVIC_API_KEY not set — skipping Google Civic, using curated data only")

    upcoming = []
    recent = []

    for (election, edate), vinfo in zip(relevant, voter_infos):
        name = election.get("name", "")
        date_str = election.get("electionDay", "")
        days = _days_until(date_str)
        contests = []
        registration_deadline = None
        voter_info_url = None

        if vinfo:
            contests = [_format_contest(c) for c in vinfo.get("contests", [])]
            # Registration info
            state_info = vinfo.get("state", [])
            if state_info:
                si = state_info[0]
                el_admin = (si.get("electionAdministrationBody") or {})
                registration_deadline = el_admin.get("electionRegistrationDeadlineText")
                voter_info_url = el_admin.get("electionInfoUrl") or el_admin.get("absenteeVotingInfoUrl")

        affects_user = _election_affects_user(name, contests, state_code)

        entry = {
            "id": election.get("id"),
            "name": name,
            "date": date_str,
            "affects_user": affects_user,
            "contests": contests,
            "registration_deadline": registration_deadline,
            "voter_info_url": voter_info_url,
            "ballotpedia_url": _ballotpedia_url(name, date_str),
        }

        if days is not None and days >= 0:
            entry["countdown_days"] = days
            upcoming.append(entry)
        else:
            entry["days_ago"] = abs(days) if days is not None else None
            recent.append(entry)

    # ── Supplement with known_elections (admin-curated) or Claude web search ──
    if state_code:
        state_name_full = STATE_NAMES.get(state_code.upper(), state_code)

        # Check the curated known_elections table first — admin-maintained, no API calls
        curated = get_known_elections(state_code)
        if curated:
            print(f"[ELECTIONS] Using curated known_elections for {state_code} ({len(curated)} entries)")
            claude_results = curated
        else:
            # No curated entries — fall back to the existing in-memory → DB → Claude chain
            with _cache_lock:
                claude_results = _web_search_cache.get(state_code)

            if claude_results is None:
                claude_results = get_elections_cache(state_code)
                if claude_results is not None:
                    print(f"[ELECTIONS] DB cache hit for {state_code} ({len(claude_results)} elections)")
                    with _cache_lock:
                        _web_search_cache[state_code] = claude_results
                else:
                    claude_results = await _search_elections_with_claude(state_name_full, state_code)
                    set_elections_cache(state_code, claude_results)
                    if claude_results:
                        with _cache_lock:
                            _web_search_cache[state_code] = claude_results

        # Merge — skip any date already covered by Google Civic (within 1 day)
        existing_dates = {e["date"] for e in upcoming + recent}
        today = datetime.date.today()
        cutoff_future = today + datetime.timedelta(days=548)

        for ce in claude_results:
            date_str = ce.get("date", "")
            if not date_str or date_str in existing_dates:
                continue
            try:
                edate = datetime.date.fromisoformat(date_str)
            except ValueError:
                continue
            if not (today - datetime.timedelta(days=60) <= edate <= cutoff_future):
                continue
            days = _days_until(date_str)
            entry = {
                "id": f"web_{state_code}_{date_str}",
                "name": ce.get("name", ""),
                "date": date_str,
                "affects_user": True,
                "contests": [],
                "registration_deadline": None,
                "voter_info_url": None,
                "ballotpedia_url": _ballotpedia_url(ce.get("name", ""), date_str),
                "source": "web_search",
            }
            if days is not None and days >= 0:
                entry["countdown_days"] = days
                upcoming.append(entry)
            else:
                entry["days_ago"] = abs(days) if days is not None else None
                recent.append(entry)

    # Sort: affects_user first, then soonest
    upcoming.sort(key=lambda e: (not e["affects_user"], e.get("countdown_days", 9999)))
    recent.sort(key=lambda e: (not e["affects_user"], e.get("days_ago", 9999)))

    result = {
        "zip": zip_code,
        "state": state_code,
        "upcoming": upcoming,
        "recent": recent,
    }

    log_action(
        agent_name="elections",
        action="fetch_elections",
        input_data={"zip": zip_code, "state": state_code},
        output_data={"upcoming": len(upcoming), "recent": len(recent)},
    )

    return result


async def _fetch_polling_with_claude(election_name, state_name, election_id):
    """
    Fetch polling data for a specific election via Claude web search → RealClearPolling.
    Cached 6hr per election in DB (shared across workers) and in-memory.
    Returns {} if no polling data found or on error.
    """
    db_key = f"poll_{election_id}"

    with _cache_lock:
        cached = _polling_cache.get(db_key)
    if cached is not None:
        return cached

    cached = get_elections_cache(db_key, max_age_seconds=21600)
    if cached is not None:
        print(f"[POLLING] DB cache hit for {election_id}")
        with _cache_lock:
            _polling_cache[db_key] = cached
        return cached

    print(f"[POLLING] Fetching polling for: {election_name}")
    try:
        client = AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
        response = await client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=600,
            tools=[{"type": "web_search_20260209", "name": "web_search"}],
            messages=[{"role": "user", "content": (
                f"Find the latest polling data for the {election_name}"
                f"{' in ' + state_name if state_name else ''} on realclearpolling.com. "
                f"Return ONLY a raw JSON object, no markdown, no explanation: "
                f'{{"leader": "Candidate name or null", '
                f'"margin": "X.X points or Too close to call or null", '
                f'"polls": [{{"candidate": "Name", "pct": 45.2, "source": "Pollster", "date": "YYYY-MM-DD"}}], '
                f'"summary": "One sentence on the state of the race"}} '
                f"If no polls found for this specific race, return exactly: {{}}"
            )}],
        )
        text = "".join(b.text for b in response.content if hasattr(b, "text")).strip()
        print(f"[POLLING] Claude raw response for {election_id}: {text[:200]}")

        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                set_elections_cache(db_key, parsed)
                with _cache_lock:
                    _polling_cache[db_key] = parsed
                return parsed
        except json.JSONDecodeError:
            pass

        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            parsed = json.loads(match.group())
            if isinstance(parsed, dict):
                set_elections_cache(db_key, parsed)
                with _cache_lock:
                    _polling_cache[db_key] = parsed
                return parsed

        print(f"[POLLING] No parseable JSON for {election_id}")
    except Exception as e:
        print(f"[POLLING] Error for {election_id}: {e}")

    return {}


def _determine_stage(election):
    name = (election.get("name", "") + " " + election.get("type", "")).lower()
    if "runoff" in name:
        return "runoff"
    if "special" in name:
        return "special"
    if "primary" in name:
        return "primary"
    return "general"


async def fetch_election_detail(election_id, zip_code=None, state_code=None):
    """
    Return full detail for a single election: base data + stage + polling.
    Reuses the fetch_elections cache wherever possible.
    """
    data = await fetch_elections(zip_code, state_code)
    all_elections = data.get("upcoming", []) + data.get("recent", [])

    election = next(
        (e for e in all_elections if str(e.get("id")) == str(election_id)),
        None,
    )
    if not election:
        return None

    import copy
    detail = copy.deepcopy(election)
    detail["stage"] = _determine_stage(detail)
    detail["is_past"] = "days_ago" in detail
    detail["polling"] = None  # fetched on demand via /api/elections/{id}/polling
    return detail


async def fetch_election_polling(election_id, state_code=None):
    """
    Fetch polling on demand — only for Google Civic elections.
    Web-search sourced elections (local races) won't appear on RCP; skip them.
    """
    # Fix 3: skip web_search sourced elections entirely
    if str(election_id).startswith("web_"):
        return {}

    state_name = STATE_NAMES.get((state_code or "").upper(), state_code or "")

    # Need the election name for the Claude prompt — look it up from any warm cache
    election_name = None
    with _cache_lock:
        for cached in _elections_cache.values():
            for e in cached.get("upcoming", []) + cached.get("recent", []):
                if str(e.get("id")) == str(election_id):
                    election_name = e.get("name", "")
                    break
            if election_name:
                break

    if not election_name:
        return {}

    return await _fetch_polling_with_claude(election_name, state_name, election_id)


# Cap the number of FEC candidate lookups per election so a crowded primary
# ballot can't fan out into dozens of API calls (each is disk-cached anyway).
_FINANCE_MAX_LOOKUPS = 16

_FED_OFFICE_LABEL = {"S": "U.S. Senate", "H": "U.S. House", "P": "President"}
# Names that are office-specific and NOT federal — don't let the generic
# "even-year statewide ballot" fallback attach a Senate race to them.
_NONFEDERAL_HINT = re.compile(r"govern|gubernatorial|mayor|council|school|attorney|"
                              r"treasurer|assembly|state senate|state house|legislat|"
                              r"county|city|judge|sheriff|ballot measure|proposition",
                              re.I)


def _federal_races_for(name, state, date_str):
    """Infer the federal race(s) a calendar election covers, as (office, state,
    cycle) tuples. FEC only has federal offices; House needs a district we don't
    have from a calendar entry, so we resolve statewide races only — Senate and
    President. A generic 'General'/'Primary' ballot in an even year gets the
    state's Senate seat (empty if none is up) plus President in a presidential
    year. Office-specific state races (governor, etc.) resolve to nothing."""
    n = (name or "").lower()
    st = (state or "").upper() or None
    try:
        year = int((date_str or "")[:4])
    except ValueError:
        return []
    cycle = year if year % 2 == 0 else year + 1  # FEC cycles are even years

    races = []
    if "president" in n or "presidential" in n:
        races.append(("P", None, cycle))
    if ("senate" in n or "senator" in n) and st:
        races.append(("S", st, cycle))
    if races:
        return races

    # Generic bundled ballot: only when it isn't an office-specific state race.
    if st and year % 2 == 0 and ("general" in n or "primary" in n) \
            and not _NONFEDERAL_HINT.search(n):
        races.append(("S", st, cycle))
        if cycle % 4 == 0:
            races.append(("P", None, cycle))
    return races


def _primary_party(name):
    """The party of a partisan primary, as an FEC-party substring, or None for
    general elections and open/nonpartisan primaries. A 'Republican Primary'
    should show only Republicans — not the whole bipartisan field."""
    n = (name or "").lower()
    if "primary" not in n:
        return None
    if "republican" in n or "gop" in n:
        return "republican"
    if "democratic" in n or "democrat" in n:
        return "democratic"
    return None


async def _finance_from_fec_roster(detail, state):
    """Fallback roster: when no candidate list reaches us (Google Civic's
    voterinfo is retired), build federal contests straight from the FEC's own
    candidate registry for the race the election name implies. For a partisan
    primary, the field is narrowed to that party."""
    import fec_client
    races = _federal_races_for(detail.get("name"), state, detail.get("date"))
    party = _primary_party(detail.get("name"))
    out_contests = []
    for office, st, cycle in races:
        # Pull a wide roster, then narrow by party so filtering doesn't clip the
        # field down to almost nothing.
        cands = await asyncio.to_thread(fec_client.race_candidates, office, st, cycle, 40)
        if party:
            cands = [f for f in cands if party in (f.get("party") or "").lower()]
        cands = cands[:12]
        rows = [{
            "name": _title_case(f.get("name")),
            "party": f.get("party"),
            "party_color": _party_color(f.get("party")),
            "finance": f,
        } for f in cands]
        if rows:
            label = _FED_OFFICE_LABEL.get(office, office)
            if party:
                label += f" — {party.capitalize()} primary"
            out_contests.append({
                "office": label,
                "district": None,
                "candidates": rows,
                "from_fec_roster": True,
            })
    return out_contests


_HONORIFIC = {"mr", "mr.", "mrs", "mrs.", "ms", "ms.", "dr", "dr.", "hon", "hon."}


def _cap_word(w):
    if "." in w:                       # initials like 'C.L.'
        return w.upper()
    if w.isupper() and len(w) <= 2:    # bare initial 'F'
        return w
    return w.capitalize()


def _title_case(name):
    """FEC stores names uppercase, surname-first, with embedded honorifics and
    trailing suffixes ('KENNEDY, ROBERT F JR'). Present them the way a reader
    expects — 'Robert F Kennedy Jr.': drop honorifics, move the surname to the
    front, and keep a generational suffix at the very end."""
    n = (name or "").strip()
    if "," in n:
        last, rest = n.split(",", 1)
    else:
        rest, last = n, ""
    tokens = rest.split()
    suffix = None
    if tokens and tokens[-1].lower().strip(".") in ("jr", "sr", "ii", "iii", "iv", "v"):
        suffix = tokens.pop().lower().strip(".")
    given = [t for t in tokens if t.lower() not in _HONORIFIC]
    parts = [_cap_word(w) for w in given]
    if last.strip():
        parts.append(_cap_word(last.strip()))
    if suffix:
        parts.append(suffix.upper() if suffix in ("ii", "iii", "iv", "v")
                     else suffix.capitalize() + ".")
    return " ".join(parts)


async def fetch_election_finance(election_id, zip_code=None, state_code=None):
    """Federal campaign finance for the candidates on this ballot (FEC).

    FEC only covers U.S. House, Senate, and President. Primary path uses the
    ballot's own candidate roster (Google Civic); when that's empty — which is
    now the norm, since Civic's voterinfo endpoint is retired — we fall back to
    the FEC's candidate registry for the federal race the election name implies.
    Returns {"contests": [...]} for whatever produced matches, else {}.
    """
    import fec_client

    detail = await fetch_election_detail(election_id, zip_code, state_code)
    if not detail:
        return {}
    state = (state_code or detail.get("state") or "").upper() or None

    out_contests, lookups = [], 0
    for contest in detail.get("contests", []):
        office = fec_client.office_from_contest(contest.get("office"), contest.get("level"))
        if not office:
            continue
        rows = []
        for cand in contest.get("candidates", []):
            if lookups >= _FINANCE_MAX_LOOKUPS:
                break
            name = cand.get("name")
            if not name:
                continue
            lookups += 1
            fin = await asyncio.to_thread(
                fec_client.candidate_finance, name, state, office
            )
            if fin and (fin.get("receipts") or fin.get("disbursements")):
                rows.append({
                    "name": cand.get("name"),
                    "party": cand.get("party"),
                    "party_color": cand.get("party_color"),
                    "finance": fin,
                })
        if rows:
            # Lead with the biggest war chest — that's the story the section tells.
            rows.sort(key=lambda r: r["finance"].get("receipts") or 0, reverse=True)
            out_contests.append({
                "office": contest.get("office"),
                "district": contest.get("district"),
                "candidates": rows,
            })

    # No roster reached us → derive the field from the FEC directly.
    if not out_contests:
        out_contests = await _finance_from_fec_roster(detail, state)

    if not out_contests:
        return {}
    return {"contests": out_contests}
