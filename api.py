from fastapi import FastAPI, HTTPException, Request, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.exceptions import RequestValidationError
from fastapi import Response
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
import json
import re
import html as html_lib
import pathlib
import hashlib
import secrets
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel
from typing import Optional, Union
from agents.vote_parser_agent import parse_vote_references
from agents.vote_fetcher_agent import fetch_house_votes, fetch_senate_votes
from agents.vote_mapper_agent import map_house_votes, map_senate_votes
from sources.bill_fetcher import fetch_bill, fetch_law, fetch_bill_text, fetch_related_bills, fetch_amendments, parse_amends_from_title, fetch_cosponsors
from sources.committee_reports_fetcher import fetch_committee_reports_for_bill
from agents.member_search_agent import (
    search_member,
    fetch_member_profile,
    fetch_member_legislation,
)
from search.search_logger import log_search, log_bill_opened, log_member_opened
from agents.analyst_agent import analyze
from search.flag_logger import log_search_flag, log_bill_flag, get_flags
from agents.feed_agent import fetch_feed
from resolvers.civic_resolver import resolve_zip
from resolvers.district_resolver import resolve_address, resolve_point, resolve_geoid
from search import search_cache
import httpx
import asyncio
import io
import anthropic
import os
from dotenv import load_dotenv

load_dotenv()

from agents.router_agent import route, intents_from_structured, _extract_state_session
from sources.bill_fetcher import fetch_bill
from agents.translator_agent import translate_bill, translate_state_bill, translate_bill_core, resolve_bill_background
from agents.historian_agent import (
    fetch_bill_actions,
    summarize_history,
    structure_history,
)
from agents.documentor_agent import log_action
from agents.result_validator_agent import validate_results, validate_results_batch, get_state_validator_floor
from search.search_rank import rank_by_relevance
from render.state_vote_mapper import select_floor_roll_call, map_roll_call
from agents.ledger_agent import (
    build_funnel,
    stories_from_results,
    enrich_stories,
    district_impact,
    district_impact_key,
    fallback_headline,
    foundry_place_coverage,
    STATE_NAMES,
    extract_state,
    build_shelves,
    shelf_cache_key,
    member_headline,
)
from correspondence.router import router as correspondence_router
from correspondence.db import (
    list_known_elections as db_list_known_elections,
    add_known_election as db_add_known_election,
    delete_known_election as db_delete_known_election,
    get_bill_lobbying as db_get_bill_lobbying,
    get_disk_cache,
    set_disk_cache,
)
from agents.elections_agent import (
    fetch_elections, fetch_election_detail, fetch_election_polling,
    fetch_election_finance,
)
from sources.lda_client import search_entities as lda_search_entities, get_entity_profile as lda_get_entity_profile

# Quiet uvicorn access logs for the high-frequency SSE monitor stream — it
# fires on every event and otherwise drowns out actually-useful request lines.
import logging as _logging
class _QuietMonitorStream(_logging.Filter):
    def filter(self, record):
        msg = record.getMessage()
        return "/monitor/stream" not in msg
_logging.getLogger("uvicorn.access").addFilter(_QuietMonitorStream())

app = FastAPI(title="NosPopuli API")

limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    print(f"[VALIDATION ERROR] {exc.errors()}")
    return JSONResponse(status_code=422, content={"detail": exc.errors()})


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    print(f"[API] Unhandled exception on {request.url.path}: {exc}")
    return JSONResponse(
        status_code=500,
        content={
            "error": "Something went wrong on our end.",
            "path": str(request.url.path),
        },
    )


app.add_middleware(GZipMiddleware, minimum_size=512)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(correspondence_router)

class CachedStaticFiles(StaticFiles):
    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        if response.status_code == 200:
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response

app.mount("/static", CachedStaticFiles(directory="frontend"), name="static")

client = None


def get_client():
    global client
    if client is None:
        client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    return client


_MONITOR_SECRET = os.getenv("MONITOR_SECRET", "")

def _require_monitor_auth(request: Request):
    """Raises 403 if the request lacks a valid monitor secret."""
    if not _MONITOR_SECRET:
        raise HTTPException(status_code=503, detail="Monitor not configured — set MONITOR_SECRET in .env")
    provided = (
        request.query_params.get("secret") or
        request.headers.get("X-Monitor-Secret", "")
    )
    if not secrets.compare_digest(provided, _MONITOR_SECRET):
        raise HTTPException(status_code=403, detail="Forbidden")


def _build_connections(related: dict, amendments: list, bill_title: str, translation: str, committee_reports: list | None = None) -> dict:
    amends = parse_amends_from_title(bill_title, translation or "")
    return {
        "amends":            amends,
        "committee_reports": committee_reports or [],
        "identical":         related.get("identical", []),
        "amended_by":        amendments,
        "related":           related.get("related", []),
        "superseded":        related.get("superseded", []),
    }


class SearchRequest(BaseModel):
    question: str
    max_results: int = 10
    full_history: bool = False
    state_code: Optional[str] = None
    before_congress: Optional[int] = None  # history starts before this congress number
    fresh: bool = False  # debug: bypass search cache for this request


class LedgerAsk(BaseModel):
    question: str
    state_code: Optional[str] = None
    max_results: int = 10


class BillRequest(BaseModel):
    congress: int
    bill_type: str
    number: int
    user_context: Optional[dict] = None


class LawRequest(BaseModel):
    congress: int
    law_number: int
    user_context: Optional[dict] = None


class MemberSearchRequest(BaseModel):
    name: str


class FeedRequest(BaseModel):
    interests: list
    senator_bioguides: list
    rep_bioguide: str = None
    state_code: str = None


class ZipRequest(BaseModel):
    zip_code: str


class AddressRequest(BaseModel):
    address: str


class PointRequest(BaseModel):
    lat: float
    lon: float
    date: Optional[str] = None   # ISO; before the current Congress, answered from district shapes


class GeoidRequest(BaseModel):
    geoid: str


class DistrictImpactRequest(BaseModel):
    geoid: str
    question: str
    stories: list = []


class StateBillRequest(BaseModel):
    state_code: str
    session: str
    bill_type: str
    number: Union[int, str]     # "1001a": Nebraska's A bills, Florida's special-session letters
    user_context: Optional[dict] = None


class StateSearchRequest(BaseModel):
    question: str
    state_code: str
    max_results: int = 5
    fresh: bool = False  # debug: bypass state search cache for this request


class StateMemberSearchRequest(BaseModel):
    name: str
    state_code: str


class KnownElectionRequest(BaseModel):
    state_code: str
    name: str
    date: str
    type: Optional[str] = None
    source_url: Optional[str] = None
    notes: Optional[str] = None


class SearchFlagRequest(BaseModel):
    query: str
    results_shown: list
    expanded_terms: list = []
    congress_numbers: list = []
    confidence: float = 1.0
    reason: str
    notes: str = ""


class BillFlagRequest(BaseModel):
    bill_id: str
    congress: int
    bill_type: str
    reason: str
    notes: str = ""
    flagged_section: str = "translation"


# ── Search handlers ──


async def handle_member_search(structured, question, loop):
    member = await loop.run_in_executor(None, search_member, structured["entity_name"])
    if member and member.get("candidates"):
        return {
            "query_type": "member",
            "found": False,
            "candidates": member["candidates"],
            "confidence": structured.get("confidence"),
            "ambiguity_reason": f"More than one member of Congress is named {structured['entity_name']!r}.",
        }

    if not member:
        log_search(
            query=question,
            query_type="member",
            expanded_terms=[],
            results_count=0,
            result_ids=[],
            confidence=structured.get("confidence", 1.0),
        )
        return {
            "query_type": "member",
            "found": False,
            "confidence": structured.get("confidence"),
            "ambiguity_reason": structured.get("ambiguity_reason"),
        }

    profile, legislation = await asyncio.gather(
        loop.run_in_executor(None, fetch_member_profile, member["bioguide_id"]),
        loop.run_in_executor(None, fetch_member_legislation, member["bioguide_id"], 10),
    )

    log_search(
        query=question,
        query_type="member",
        expanded_terms=[],
        results_count=1,
        result_ids=[member.get("bioguide_id", "")],
        confidence=structured.get("confidence", 1.0),
    )

    return {
        "query_type": "member",
        "found": True,
        "confidence": structured.get("confidence"),
        "ambiguity_reason": structured.get("ambiguity_reason"),
        "member": {**member, **(profile or {})},
        "legislation": legislation,
    }


async def handle_committee_search(structured, question, loop):
    """A committee and the bills it reported, newest first, from the graph
    on this server. Only bills with a recorded vote are loaded, so a quiet
    committee has fewer; the graph's empty reason says so and is passed on
    as it is. Fail-open to "not found": no database, no answer."""
    import graph
    entity = structured.get("entity_name", "")

    def ask():
        if not os.getenv("DATABASE_URL"):
            return None, {}
        out = graph.answer({"ask": "reported", "committee": entity}, graph.pg_backend(), limit=10)
        ids = [r["item_id"] for r in out.get("rows") or [] if r.get("item_id")]
        if not ids:
            return out, {}
        # A bill node's name is "H.R. 1234: title"; its Congress is a prop.
        from correspondence.db import _get_pool
        with _get_pool().connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT id, props FROM graph_node WHERE id = ANY(%s)", (ids,))
            return out, {i: p for i, p in cur.fetchall()}

    try:
        out, props = await loop.run_in_executor(None, ask)
    except Exception as e:
        print(f"[COMMITTEE] graph error: {e}")
        out, props = None, {}
    base = {"query_type": "committee", "confidence": structured.get("confidence"),
            "ambiguity_reason": structured.get("ambiguity_reason")}
    if not out or not out.get("committees"):
        return {**base, "found": False, "empty_reason": (out or {}).get("empty_reason")}
    bills = []
    for r in out["rows"]:
        p = props.get(r.get("item_id")) or {}
        if p.get("congress") and p.get("instrument_type") and str(p.get("number") or "").isdigit():
            bills.append({"congress": int(p["congress"]), "type": p["instrument_type"], "number": int(p["number"]),
                          "title": (r.get("title") or "").split(": ", 1)[-1], "latest_action": "",
                          "date": r.get("date") or ""})
    return {**base, "found": True, "committee": {"name": out["committees"][0], "chamber": None},
            "bills": bills, "empty_reason": out.get("empty_reason")}


async def handle_specific_bill(structured, question):
    specific = structured["specific_bill"]
    bill_type = specific["type"].lower()
    number = specific["number"]
    congress = specific.get("congress") or structured["congress_numbers"][0]

    return {
        "query_type": "legislation",
        "confidence": structured.get("confidence", 1.0),
        "ambiguity_reason": None,
        "query": structured,
        "results": [
            {
                "package_id": f"BILLS-{congress}{bill_type}{number}",
                "title": f"{bill_type.upper()} {number}",
                "date_issued": "",
                "congress": congress,
                "type": bill_type,
                "number": number,
            }
        ],
    }


async def handle_bill_search(structured, question, loop):
    """Federal bills for a question, from the index on this server, as
    state search already does: one hybrid search, one relevance check.
    Browse is the newest bills. A named act is searched by the router's
    canonical name ("Affordable Care Act" for "obamacare"), in any
    Congress. Fail-closed: an index error raises into the route."""
    from search import bill_index
    target = structured.get("result_count", 5)
    if structured.get("full_history"):
        target = structured.get("max_results_override") or 50
    congresses, laws_only, partly, wholly = bill_index.index_filters(structured)
    confidence = structured.get("confidence", 1.0)
    note = structured.get("ambiguity_reason")

    def reply(results, note, **extra):
        log_search(query=question, query_type="legislation", expanded_terms=[], results_count=len(results),
                   result_ids=[f"{r.get('type', '')}{r.get('number', '')}" for r in results],
                   confidence=confidence)
        return {"query_type": "legislation", "confidence": confidence, "ambiguity_reason": note,
                "query": structured, "results": results, "cached": False, **extra}

    if wholly:
        return reply([], bill_index.BEFORE_INDEX, empty_reason="before_index")
    if partly:
        note = " ".join(x for x in (note, bill_index.BEFORE_INDEX) if x)

    if structured.get("query_subtype") == "browse":
        results = await loop.run_in_executor(
            None, bill_index.recent, bill_index.US_DIV, congresses, laws_only, target)
        return reply(results, note)

    # A named act is searched by the router's canonical name alone:
    # "obamacare" as a word finds the bills that repeal it, the name
    # "Affordable Care Act" finds the law.
    named = structured.get("named_entity")
    text = named or question
    candidates = await loop.run_in_executor(
        None, lambda: bill_index.search(text, congresses, max(target * 3, 12), laws_only))
    hint = structured.get("known_bill_hint")
    if hint and not any((r["congress"], r["type"], r["number"]) == (hint["congress"], hint["type"], int(hint["number"]))
                        for r in candidates):
        candidates.insert(0, {"package_id": f"BILLS-{hint['congress']}{hint['type']}{hint['number']}",
                              "title": f"{hint['type'].upper()} {hint['number']}", "date_issued": "",
                              "congress": hint["congress"], "type": hint["type"], "number": int(hint["number"])})
    if len(candidates) > 20:
        validated = await loop.run_in_executor(None, validate_results_batch, question, candidates, get_client(), 4)
    else:
        validated = await loop.run_in_executor(None, validate_results, question, candidates, get_client())
    extra = (structured.get("keywords") or []) + ([named] if named else [])
    results = rank_by_relevance(validated, question, extra=extra)
    if named:
        # "Inflation Reduction Act" means the law, not the later bills that
        # reuse its name: the law of that name goes first.
        laws = await loop.run_in_executor(None, bill_index.laws_named, named)
        keys = {(r["congress"], r["type"], r["number"]) for r in laws}
        results = laws + [r for r in results if (r.get("congress"), r.get("type"), r.get("number")) not in keys]
    results = results[:target]
    return reply(results, note if results else " ".join(
        x for x in (note, "No federal bills on this server matched that question.") if x))


def _state_not_loaded(state_code):
    import graph
    loaded = ", ".join(sorted(graph.loaded_states())) or "none"
    return (f"{state_code} legislation is not loaded on this server yet. "
            f"States loaded: {loaded}.")


async def handle_state_search(structured, question, loop):
    """A state question, answered from the files and the search index on
    this server (Open States, plan step 14). Nothing is fetched live."""
    import graph
    state_code = structured["state_code"]
    st = state_code.lower()

    def reply(results, note=None, **extra):
        return {"query_type": "state_legislation", "state_code": state_code,
                "confidence": structured.get("confidence", 1.0), "ambiguity_reason": note,
                "query": structured, "results": results, **extra}

    # ── Off-topic — same polite empty as federal ──
    if structured.get("query_type") == "off_topic":
        log_search(query=question, query_type="off_topic", expanded_terms=[], results_count=0,
                   result_ids=[], confidence=structured.get("confidence", 1.0))
        return {
            "query_type": "off_topic",
            "state_code": state_code,
            "confidence": structured.get("confidence", 1.0),
            "ambiguity_reason": (
                "This doesn't look like a question about state legislation, "
                "legislators, or civic policy. Try a topic, bill ID (e.g. \"HB 1557\"), or a name."
            ),
            "query": structured,
            "results": [],
        }

    if state_code not in graph.loaded_states():
        return reply([], _state_not_loaded(state_code), empty_reason="state_not_loaded")

    # ── Member query: the roster file and the bills each person sponsored ──
    if structured.get("query_type") == "member" and structured.get("entity_name"):
        found = await loop.run_in_executor(None, graph.state_member_lookup, st, structured["entity_name"])
        member, bills, read = found.get("person"), [], []
        if member:
            bills, read = await loop.run_in_executor(
                None, graph.state_member_bills, st, member["ocd_person_id"], 10)
        return {
            "query_type": "state_member",
            "state_code": state_code,
            "confidence": structured.get("confidence", 1.0),
            # The router's own note is about Congress ("not a current member"), not this roster.
            "ambiguity_reason": (
                None if member else
                f"More than one {state_code} legislator matches {structured['entity_name']}." if found.get("candidates") else
                f"No {state_code} legislator named {structured['entity_name']} in the roster on this server."),
            "member": member,
            "candidates": found.get("candidates") or [],
            "sponsored_bills": bills[:10],
            "sessions_read": read,
        }

    # ── A bill number: newest session first, since states renumber each session ──
    specific_bill = structured.get("specific_bill") or {}
    identifier = specific_bill.get("identifier") if structured.get("_fast_path") == "state_bill_id" else None
    if not identifier:
        m = graph.find_state_bill(st, question)
        identifier = re.sub(r"\s+", " ", m.group(1).upper()) if m else None
    if identifier:
        # The language model's route carries no session; the question's own year does.
        # The bill number is cut out first, only where the question spells this bill.
        m = graph.find_state_bill(st, question)
        if m and graph.bill_key_of(m.group(1)) != graph.bill_key_of(identifier):
            m = None
        year = structured.get("requested_session") or _extract_state_session(
            question[:m.start()] + " " + question[m.end():] if m else question)
        year = year if year and len(year) == 4 else None
        hits = await loop.run_in_executor(None, graph.state_bill_lookup, st, identifier, year)
        note = None
        if not hits and year:
            hits = await loop.run_in_executor(None, graph.state_bill_lookup, st, identifier, None)
            if hits:
                note = f"No {identifier} in {year} on this server — showing {identifier} from other sessions."
        if hits:
            if not year and not note and len(hits) > 1:
                note = (f"{identifier} is a different bill each session; newest first. "
                        f"Add a year (e.g. \"{identifier} from {graph.session_years(hits[-1][0], st)[0]}\") to pin one.")
            return reply([graph.state_bill_row(st, s, k, b, graph._newer_actions(st, s, k, b))
                          for s, k, b in hits[:10]], note)
        if structured.get("_fast_path") == "state_bill_id":
            return reply([], f"No {identifier} in the {state_code} sessions on this server "
                             f"({', '.join(graph.state_sessions(st))}).", empty_reason="no_such_bill")

    target_count = max(structured.get("result_count", 5), 10)

    # ── Browse: "show me anything recent" — the latest actions, no topic to score ──
    if structured.get("query_subtype") == "browse":
        results = await loop.run_in_executor(None, graph.state_recent_bills, st, target_count * 3)
        if structured.get("status") == "enacted":
            results = [r for r in results if r["is_law"]] or results
        results = results[:target_count]
        log_search(query=question, query_type="state_legislation", expanded_terms=[],
                   results_count=len(results), result_ids=[r["identifier"] for r in results],
                   confidence=structured.get("confidence", 1.0))
        return reply(results)

    # ── Topic: the hybrid index over this state's bills (full text + Voyage) ──
    # The whole question goes in, as for federal: the index ranks meaning, so
    # the keyword expansion LegiScan needed has nothing to add.
    from search import bill_index
    results = await loop.run_in_executor(
        None, lambda: bill_index.search(question, limit=20, jurisdiction=graph.state_div(st),
                                        laws_only=structured.get("status") == "enacted"))
    results = results[:target_count]
    results = await loop.run_in_executor(
        None, validate_results, question, results, get_client(), get_state_validator_floor(state_code), True)

    log_search(query=question, query_type="state_legislation", expanded_terms=[],
               results_count=len(results), result_ids=[r.get("identifier", "") for r in results],
               confidence=structured.get("confidence", 1.0))
    # No search cache: search_cache.store keeps Congress rows only, and the
    # index answers from this server in well under a second.
    return reply(results, None if results else
                 f"No {state_code} bills on this server matched that question.")


@app.post("/resolve-zip")
@limiter.limit("10/minute")
async def resolve_zip_endpoint(request: Request, body: ZipRequest):
    """Takes a zip code, returns state and representatives."""
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, resolve_zip, body.zip_code)

    if not result:
        raise HTTPException(status_code=404, detail="Could not resolve zip code")

    return result


@app.post("/resolve-address")
@limiter.limit("10/minute")
async def resolve_address_endpoint(request: Request, body: AddressRequest):
    """Geocode an address to its exact congressional district + representatives.
    Precise where /resolve-zip is only state-level."""
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, resolve_address, body.address)
    if result.get("error"):
        raise HTTPException(status_code=404, detail=result["error"])
    return result


@app.post("/resolve-point")
@limiter.limit("20/minute")
async def resolve_point_endpoint(request: Request, body: PointRequest):
    """Resolve a lat/lon (browser geolocation or a click on the district map)
    to its congressional district + representatives."""
    loop = asyncio.get_event_loop()
    if body.date:
        import graph
        if graph.congress_of(body.date) < graph.current_session()[0]:
            # A past date: the district of that Congress, from the shapes.
            # Fail-open: without the database or PostGIS the answer says so.
            try:
                result = await loop.run_in_executor(None, graph.districts_at, body.lon, body.lat, body.date)
            except Exception as e:
                print(f"[API] districts_at failed: {type(e).__name__}: {e}")
                result = {"error": "district shapes are not available right now", "as_of": body.date}
            if result.get("error"):
                raise HTTPException(status_code=404, detail=result["error"])
            return result
    result = await loop.run_in_executor(None, resolve_point, body.lat, body.lon)
    if result.get("error"):
        raise HTTPException(status_code=404, detail=result["error"])
    return result


@app.post("/resolve-district")
@limiter.limit("30/minute")
async def resolve_district_endpoint(request: Request, body: GeoidRequest):
    """Resolve a district GEOID (from clicking a district on the map) to its
    representatives — no geocoding, just the district-to-member lookup."""
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, resolve_geoid, body.geoid)
    if result.get("error"):
        raise HTTPException(status_code=404, detail=result["error"])
    return result


@app.post("/ledger/district")
@limiter.limit("30/minute")
async def ledger_district(request: Request, body: DistrictImpactRequest):
    """Clicking a district on a ledger map: who represents it, and what the
    bills on the page would mean there. The rep lookup is local and instant;
    the impact paragraph is one Haiku call, cached a day per district+ask."""
    loop = asyncio.get_event_loop()
    district = await loop.run_in_executor(None, resolve_geoid, body.geoid)
    if district.get("error"):
        raise HTTPException(status_code=404, detail=district["error"])
    stories = [s for s in (body.stories or []) if isinstance(s, dict)][:10]
    impact = None
    if body.question.strip() and stories:
        key = district_impact_key(body.geoid, body.question, stories)
        try:
            impact = get_disk_cache(key, 86400)
        except Exception:
            impact = None
        if not impact:
            try:
                impact = await asyncio.wait_for(
                    loop.run_in_executor(
                        None, lambda: district_impact(body.question, district, stories, get_client())
                    ),
                    timeout=8.0,
                )
            except Exception as e:
                print(f"[LEDGER] district impact timed out or failed: {e}")
                impact = None
            if impact:
                try:
                    set_disk_cache(key, impact)
                except Exception:
                    pass
    return {**district, "question": body.question, "impact": impact}


@app.post("/feed")
@limiter.limit("10/minute")
async def get_feed(request: Request, body: FeedRequest):
    """Returns personalized feed based on interests and representatives."""
    try:
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None,
            fetch_feed,
            body.interests,
            body.senator_bioguides,
            body.rep_bioguide,
            60,
            3,
            body.state_code,
        )
        if isinstance(result, dict):
            items = result.get("items") or []
            return {
                "items": items,
                "count": len(items),
                "state_status": result.get("state_status", "skipped"),
            }
        return {"items": result, "count": len(result), "state_status": "skipped"}
    except HTTPException:
        raise
    except Exception as e:
        print(f"[API] Error generating feed: {e}")
        raise HTTPException(
            status_code=500, detail="Failed to generate feed. Please try again."
        )


# ── Search dispatcher helpers ──
#
# The routing decision itself is router_agent.route, shared with /ledger and
# /state/search. What stays here turns that decision into a handler call. Each
# function does one thing so the /search endpoint can read top-to-bottom.


def _route_jurisdiction(structured: dict, body: "SearchRequest") -> str | None:
    """Force state jurisdiction when state_code is set on the request, and report
    whether the router originally thought this was a federal query (so the UI
    can offer a 'Switch to Federal?' nudge)."""
    if not body.state_code:
        return None
    router_thought_federal = (
        structured.get("jurisdiction") == "federal"
        and not structured.get("_fast_path")
        and structured.get("query_type") != "off_topic"
        and (structured.get("named_entity") or len(structured.get("keywords", [])) > 0)
    )
    structured["jurisdiction"] = "state"
    structured["state_code"] = body.state_code.upper()
    return "federal" if router_thought_federal else None


def _off_topic_response(structured: dict, question: str) -> dict:
    log_search(
        query=question,
        query_type="off_topic",
        expanded_terms=[],
        results_count=0,
        result_ids=[],
        confidence=structured.get("confidence", 1.0),
    )
    return {
        "query_type": "off_topic",
        "confidence": structured.get("confidence", 1.0),
        "ambiguity_reason": (
            "This doesn't look like a question about legislation, members of Congress, "
            "or civic policy. Try a topic, bill ID (e.g. \"HR 4838\"), or a name."
        ),
        "query": structured,
        "results": [],
        "cached": False,
    }


async def _dispatch(structured: dict, body: "SearchRequest", question: str, loop) -> dict:
    """Route a fully-prepared structured query to the right handler. Caller is
    responsible for all transformations on `structured` first."""
    suggested_jurisdiction = _route_jurisdiction(structured, body)

    # State queries take precedence over all other dispatch — once we're in a
    # state context we never route back to federal handlers.
    if structured.get("jurisdiction") == "state" and structured.get("state_code"):
        state_code = structured["state_code"]
        # handle_state_search answers a state not loaded here honestly.
        result = await handle_state_search(structured, question, loop)
        if suggested_jurisdiction and isinstance(result, dict):
            result["suggested_jurisdiction"] = suggested_jurisdiction
        return result

    query_type = structured.get("query_type", "legislation")

    if query_type == "member" and structured.get("entity_name"):
        return await handle_member_search(structured, question, loop)
    if query_type == "committee" and structured.get("entity_name"):
        return await handle_committee_search(structured, question, loop)

    specific = structured.get("specific_bill")
    if specific and specific.get("number") and specific.get("type"):
        return await handle_specific_bill(structured, question)

    if query_type == "off_topic":
        return _off_topic_response(structured, question)

    return await handle_bill_search(structured, question, loop)


@app.post("/search")
@limiter.limit("20/minute")
async def search(request: Request, body: SearchRequest):
    if not body.question.strip():
        raise HTTPException(status_code=400, detail="Question cannot be empty")

    try:
        loop = asyncio.get_event_loop()
        structured = route(
            body.question, body.state_code, plates=False, full_history=body.full_history,
            before_congress=body.before_congress, max_results=body.max_results,
            fresh=body.fresh, get_client=get_client)["structured"]
        return await _dispatch(structured, body, body.question, loop)
    except HTTPException:
        raise
    except Exception:
        import traceback
        print(f"[API] Full error: {traceback.format_exc()}")
        raise HTTPException(status_code=500, detail="Search failed. Please try again.")


def _client_ip(request: Request) -> str:
    xff = request.headers.get("x-forwarded-for") or ""
    if xff:
        return xff.split(",")[0].strip()
    cf = request.headers.get("cf-connecting-ip")
    if cf:
        return cf.strip()
    return (request.client.host if request.client else "") or ""


@app.get("/geo/guess")
@limiter.limit("30/minute")
async def geo_guess(request: Request):
    """Best-effort state from the connection. Fail-open to unknown — never a gate."""
    ip = _client_ip(request)
    if not ip or ip.startswith(("127.", "10.", "192.168.", "0.")) or ip == "::1":
        return {"state": None, "state_code": None, "source": "unknown"}
    try:
        async with httpx.AsyncClient(timeout=2.5) as client:
            r = await client.get(
                f"http://ip-api.com/json/{ip}",
                params={"fields": "status,countryCode,regionName,region"},
            )
            data = r.json()
        if data.get("status") == "success" and data.get("countryCode") == "US" and data.get("region"):
            return {
                "state": data.get("regionName"),
                "state_code": data.get("region"),
                "source": "ip",
            }
    except Exception as e:
        print(f"[GEO] guess failed: {e}")
    return {"state": None, "state_code": None, "source": "unknown"}


def _ndjson_lines(*objs):
    async def gen():
        for obj in objs:
            yield json.dumps(obj) + "\n"
    return StreamingResponse(gen(), media_type="application/x-ndjson", headers=STREAM_HEADERS)


async def _ledger_shelves(question, stories, member):
    key = shelf_cache_key(question, stories, member)
    try:
        cached = get_disk_cache(key, 1800)
    except Exception:
        cached = None
    if cached:
        return cached
    shelves = build_shelves(question, stories, member)
    if shelves:
        try:
            set_disk_cache(key, shelves)
        except Exception:
            pass
    return shelves


async def _ledger_member_and_search(structured, search_body, question, loop):
    """XOR /search dispatch stays intact. Ledger may fetch member + bills together."""
    intents = structured.get("intents") or intents_from_structured(structured)
    member_name = next((i.get("name") for i in intents if i.get("kind") == "member" and i.get("name")), None)
    has_topic = any(i.get("kind") == "topic" for i in intents)
    qtype = structured.get("query_type")

    async def run_member():
        mstruct = dict(structured)
        mstruct["query_type"] = "member"
        mstruct["entity_name"] = member_name
        return await handle_member_search(mstruct, question, loop)

    async def run_legis():
        lstruct = dict(structured)
        lstruct["query_type"] = "legislation"
        return await handle_bill_search(lstruct, question, loop)

    member_out = None
    search_out = {"query_type": "legislation", "results": []}

    if qtype == "off_topic":
        return None, _off_topic_response(structured, question)
    if qtype == "committee" and not has_topic and not member_name:
        return None, await _dispatch(structured, search_body, question, loop)
    if member_name and has_topic:
        member_out, search_out = await asyncio.gather(run_member(), run_legis())
        return member_out, search_out
    if member_name and not has_topic:
        member_out = await run_member()
        return member_out, member_out or search_out
    if qtype == "member" and structured.get("entity_name"):
        member_out = await handle_member_search(structured, question, loop)
        return member_out, member_out or search_out
    search_out = await _dispatch(structured, search_body, question, loop)
    if search_out.get("query_type") == "member":
        return search_out, search_out
    return None, search_out


def _graph_plate(ask):
    """Run a parsed graph question. None means 'fall through': the graph
    is unavailable, empty, or does not know the person or seat named, and
    the ledger's older paths may. A topic miss or a vacant seat is a real
    answer and is returned as one. Fail-open; never raises into /ledger."""
    import graph
    if not os.getenv("DATABASE_URL"):
        return None
    try:
        out = graph.answer(ask, graph.pg_backend())
    except Exception as e:
        print(f"[LEDGER] graph error: {e}")
        return None
    reason = out.get("empty_reason") or ""
    if reason.startswith(("no person in the graph", "no seat in the graph", "no instrument in the graph",
                          "no committee in the graph",
                          "graph not loaded")):
        return None
    return out


@app.post("/ledger")
@limiter.limit("20/minute")
async def ledger_ask(request: Request, body: LedgerAsk):
    """Classify an ask, then stream plate → member → shelves so first paint is not blocked."""
    import graph
    # The question's own state, not the reader's home: "housing bills" from a
    # Virginian still means Congress unless the router says it is a state ask.
    named = (extract_state(body.question) or "").upper() or None
    search_state = named if named in graph.loaded_states() else None

    def routed(allow_graph=True):
        return route(body.question, search_state, home_state=body.state_code, allow_graph=allow_graph,
                     max_results=body.max_results or 10, get_client=get_client)

    classified = await asyncio.to_thread(routed)
    plate = classified.get("plate")
    if plate == "graph":
        answer = await asyncio.to_thread(_graph_plate, classified["ask"])
        if answer is not None:
            return _ndjson_lines({"section": "plate", "plate": "graph",
                                  "question": body.question, **answer}, {"section": "done"})
        # The graph parsed the shape but knows neither the person nor the
        # seat (or is not there at all): answer the way we did before.
        classified = await asyncio.to_thread(routed, False)
        plate = classified.get("plate")
    if plate in ("home", "watching"):
        return _ndjson_lines({"section": "plate", **classified}, {"section": "done"})
    if plate == "uncharted":
        place = classified["place"]
        classified["coverage"] = foundry_place_coverage(place["slug"])
        return _ndjson_lines({"section": "plate", **classified}, {"section": "done"})
    if plate == "bill":
        return _ndjson_lines({"section": "plate", **classified}, {"section": "done"})
    if plate == "elections":
        try:
            data = await fetch_elections(zip_code=None, state_code=classified.get("state_code"))
        except Exception as e:
            print(f"[LEDGER] elections error: {e}")
            data = {}
        return _ndjson_lines({
            "section": "plate", **classified,
            "upcoming": (data or {}).get("upcoming") or [],
            "recent": (data or {}).get("recent") or [],
        }, {"section": "done"})

    loop = asyncio.get_event_loop()
    state_code = classified.get("state_code") or body.state_code
    search_body = SearchRequest(
        question=body.question,
        max_results=body.max_results or 10,
        state_code=search_state,
    )
    search_out = {"query_type": "legislation", "results": []}
    member_out = None
    structured = {}
    searched = None     # the state whose bills were searched; None is Congress
    try:
        # route() is fail-open here: None means the router failed, and the
        # page shows an empty ledger rather than a 500.
        structured = classified.get("structured") or {}
        if not structured:
            raise RuntimeError("the router could not structure this question")
        state_ask = structured.get("jurisdiction") == "state"
        if state_ask:
            state_code = (structured.get("state_code") or named or body.state_code or "").upper() or None
        if state_ask and state_code:
            structured["state_code"] = state_code
            if structured.get("query_type") == "state_member":
                structured["query_type"] = "member"
            if state_code not in graph.loaded_states():
                return _ndjson_lines({
                    "section": "plate", "plate": "uncharted", "question": body.question,
                    "state_code": state_code,
                    "place": {"name": None, "body": f"{STATE_NAMES.get(state_code, state_code)} legislature",
                              "slug": "", "state": state_code},
                    "reason": _state_not_loaded(state_code),
                }, {"section": "done"})
            searched = state_code
            search_out = await handle_state_search(structured, body.question, loop)
            if search_out.get("query_type") == "state_member":
                member_out = {"query_type": "member", "found": bool(search_out.get("member")),
                              "member": search_out.get("member"),
                              "legislation": {"sponsored": search_out.get("sponsored_bills") or [],
                                              "sessions_read": search_out.get("sessions_read") or []}}
                search_out = {"query_type": "legislation", "state_code": state_code, "results": [],
                              "ambiguity_reason": search_out.get("ambiguity_reason")}
        else:
            structured["jurisdiction"] = "federal"
            structured.pop("state_code", None)
            if structured.get("query_type") in ("state_legislation", "state_member"):
                structured["query_type"] = "legislation"
            structured["intents"] = intents_from_structured(structured)
            member_out, search_out = await _ledger_member_and_search(
                structured, search_body, body.question, loop
            )
    except HTTPException:
        raise
    except Exception:
        import traceback
        print(f"[LEDGER] search error: {traceback.format_exc()}")

    if structured.get("_fast_path") in ("bill_id", "state_bill_id") or structured.get("specific_bill"):
        rows = search_out.get("results") or []
        # Straight to the bill only on an exact match: a stand-in from another
        # session goes to the list, with the note that says it is a stand-in.
        if len(rows) == 1 and rows[0].get("is_state_bill") and not search_out.get("ambiguity_reason"):
            r = rows[0]
            return _ndjson_lines({
                "section": "plate",
                "plate": "state_bill",
                "state_code": r["state"],
                "session": r["session"],
                "bill_type": r["type"],
                "number": graph.bill_number(r["number"]),
                "path": r["path"],
            }, {"section": "done"})
        if len(rows) == 1 and rows[0].get("congress") and rows[0].get("number"):
            r = rows[0]
            return _ndjson_lines({
                "section": "plate",
                "plate": "bill",
                "congress": r["congress"],
                "bill_type": (r.get("type") or "").lower(),
                "number": int(r["number"]),
            }, {"section": "done"})

    out_type = search_out.get("query_type")
    if out_type == "off_topic":
        return _ndjson_lines({
            "section": "plate", "plate": "off_topic", "question": body.question,
            "reason": search_out.get("ambiguity_reason") or "",
        }, {"section": "done"})

    committee = None
    if out_type == "committee":
        if not search_out.get("found"):
            return _ndjson_lines({
                "section": "plate", "plate": "off_topic", "question": body.question,
                "kind": "committee",
                "reason": "We could not match that to a House or Senate committee. Try its full name, like \"Senate Finance Committee\".",
            }, {"section": "done"})
        committee = search_out.get("committee") or {}
        # Committee hits arrive as `bills`, newest first; they are the stories.
        search_out = dict(search_out, results=search_out.get("bills") or [])

    results = search_out.get("results") or []
    stories = stories_from_results(results, limit=body.max_results or 10)
    # Search hits are an id and a title. One Congress.gov fetch per story
    # (parallel, cached, 3s budget) gives the cards something to say and makes
    # the funnel true instead of "all introduced".
    await enrich_stories(stories, fetch_bill, budget=3.0)
    extra = (structured.get("keywords") or []) + (structured.get("expanded_terms") or [])
    stories = rank_by_relevance(stories, body.question, extra=extra)
    if committee:
        for s in stories:
            if not s.get("latest_action"):
                s["stage"] = "unknown"  # still no action data; do not draw a stage
    # The funnel counts stages from the same enriched rows the cards show.
    funnel_rows = stories or results
    place_name = classified.get("place_name") or STATE_NAMES.get((state_code or "").upper())
    src = member_out if isinstance(member_out, dict) else {}
    member = None
    legislation = None
    if src.get("query_type") == "member" and src.get("found"):
        member = src.get("member")
        legislation = src.get("legislation")

    # A person-only ask has no topic hits. The funnel and headline then describe
    # the person's own recent bills; sponsored rows are not passed off as search hits.
    person_only = bool(member) and not results
    if person_only:
        sponsored = (legislation or {}).get("sponsored") or []
        funnel = build_funnel(sponsored)
        headline = member_headline(member, sponsored)
        deck = "The bars are how far this member's recent bills got."
    elif committee:
        # Every bill here left committee (it was reported), so a funnel of
        # them says nothing about where bills stop. Send none; the page
        # shows the list.
        funnel = []
        n = len(stories)
        headline = f"{committee.get('name') or 'This committee'}. {n} recent bill{'s' if n != 1 else ''}."
        deck = (search_out.get("empty_reason") if not stories else None) or (
            "The newest bills this committee reported that later had a recorded vote. "
            "Committees are where most bills stop.")
    else:
        funnel = build_funnel(funnel_rows)
        # Name what was searched: a federal search that finds nothing is not
        # the reader's state having nothing.
        headline = fallback_headline(body.question, STATE_NAMES.get(searched) if searched else None, stories)
        deck = (((searched or search_out.get("empty_reason") == "before_index") and search_out.get("ambiguity_reason"))
                or "Most bills never leave committee. Click a bar to read only that stage.")

    plate_payload = {
        "section": "plate",
        "plate": "ledger",
        "question": body.question,
        "state_code": state_code,
        "place_name": place_name,
        "person_only": person_only,
        "funnel": funnel,
        "headline": headline,
        "deck": deck,
        "stories": stories,
        "committee": committee,
        "query_type": out_type,
        "cached": search_out.get("cached"),
    }

    async def gen():
        yield json.dumps(plate_payload) + "\n"
        if member:
            yield json.dumps({
                "section": "member",
                "member": member,
                "legislation": legislation or {},
            }) + "\n"
        shelves = await _ledger_shelves(body.question, stories, member)
        yield json.dumps({"section": "shelves", "shelves": shelves}) + "\n"
        yield json.dumps({"section": "done"}) + "\n"

    return StreamingResponse(gen(), media_type="application/x-ndjson", headers=STREAM_HEADERS)


# Headers that opt a StreamingResponse out of gzip + proxy buffering, so the
# instant `meta` line reaches the client immediately. GZipMiddleware buffers a
# stream's small early chunks until it accumulates enough bytes, which would
# defeat progressive delivery for every gzip-capable client (i.e. all browsers).
# Declaring an explicit Content-Encoding makes GZipMiddleware pass the stream
# through untouched; the extra headers stop intermediary proxies buffering too.
STREAM_HEADERS = {
    "Content-Encoding": "identity",
    "Cache-Control": "no-cache, no-transform",
    "X-Accel-Buffering": "no",
}


def _bill_detail_stream(bill_data, meta_extra, user_context, *, log_kind, noun="bill"):
    """Shared NDJSON section generator behind both /bill and /law.

    Each line is one {"section": ...} object. Sections are computed by
    independent producers and flushed the instant each is ready
    (asyncio.as_completed), so the fast pieces (meta, sponsors, text, votes)
    never wait on the slow Haiku/Sonnet calls, and the vote pipeline and
    timeline no longer sit behind the translation. `meta_extra` carries the
    endpoint-specific identity fields (type/number vs law_number) merged into
    the meta section. Identifiers for the internal fetches are derived from the
    bill record, so a Public Law resolves to its underlying bill transparently.
    """
    loop = asyncio.get_event_loop()

    bill = bill_data.get("bill", {}) or {}
    congress = bill.get("congress")
    bill_type = (bill.get("type") or "").lower()
    number = int(bill.get("number") or 0)
    bill_title = bill.get("title", "")
    laws = bill.get("laws") or []
    became_law = laws[0] if laws else None
    sponsors = [
        {
            "name": s.get("fullName", ""),
            "first_name": s.get("firstName", ""),
            "last_name": s.get("lastName", ""),
            "party": s.get("party", ""),
            "state": s.get("state", ""),
            "bioguide_id": s.get("bioguideId", ""),
            "is_by_request": s.get("isByRequest", "N") == "Y",
        }
        for s in (bill.get("sponsors") or [])
    ]

    def ex(fn, *args):
        return loop.run_in_executor(None, fn, *args)

    async def stream():
        # Shared upstream tasks — kicked off once, awaited by whichever
        # sections need them. Actions feed both the timeline and the votes;
        # bill text feeds both the full-text section and the translation.
        # Fetch a generous slice for the on-page reader (the full bill lives one
        # click away on Congress.gov); the translator uses a bounded head of it.
        # Fetch a deep slice server-side so the translator's cost/scope digest
        # can reach appropriations buried late in long bills; the browser
        # preview is sliced down to 50k in sec_text, full text lazy-loads.
        text_task    = asyncio.ensure_future(ex(fetch_bill_text, congress, bill_type, number, 300000))
        actions_task = asyncio.ensure_future(ex(fetch_bill_actions, congress, bill_type, number))
        cospon_task  = asyncio.ensure_future(ex(fetch_cosponsors, congress, bill_type, number))
        related_task = asyncio.ensure_future(ex(fetch_related_bills, congress, bill_type, number))
        amend_task   = asyncio.ensure_future(ex(fetch_amendments, congress, bill_type, number))
        reports_task = asyncio.ensure_future(ex(fetch_committee_reports_for_bill, bill))

        # Translation is split: the fast Haiku core (~3s) and the slow Sonnet
        # web-search Background (~75s) stream as separate sections so the
        # plain-English explanation isn't held hostage by the reference lookup.
        async def _translate_core():
            txt = await text_task
            translation, refs, plate = await ex(
                translate_bill_core, bill_data, get_client(), user_context,
                txt,  # full slice; translate_bill_core digests it down itself
            )
            return (translation or f"Translation unavailable for this {noun}.", refs or [], plate or {})
        translate_core_task = asyncio.ensure_future(_translate_core())

        async def sec_meta():
            return {
                "section": "meta",
                "title": bill_title, "sponsors": sponsors, "became_law": became_law,
                **meta_extra,
            }

        async def sec_text():
            txt = await text_task
            # Stream a fast 50k preview; flag when there's more so the reader can
            # lazy-load the complete bill on expand (see /api/bill/.../text).
            # txt itself is a deep 300k slice for the translator digest — slice
            # it down here so the wire payload stays small.
            preview = txt[:50000] if txt else txt
            return {"section": "bill_text", "bill_text": preview or None,
                    "truncated": bool(txt) and len(txt) > 50000}

        async def sec_sponsors():
            cos = await cospon_task
            return {"section": "sponsors", "sponsors": sponsors, "cosponsors": cos or []}

        async def sec_translation():
            translation, refs, plate = await translate_core_task
            return {
                "section": "translation",
                "translation": translation,
                "became_law": became_law,
                "has_background": bool(refs),
                "plate": plate or {},
            }

        async def sec_background():
            _translation, refs, _plate = await translate_core_task
            items = await ex(resolve_bill_background, bill_data, refs, get_client())
            return {"section": "background", "items": items or []}

        async def sec_timeline():
            actions = await actions_task or []
            timeline = await ex(summarize_history, actions, get_client()) \
                or f"Timeline unavailable for this {noun}."
            return {
                "section": "timeline",
                "timeline": timeline,
                "timeline_events": structure_history(actions),
            }

        async def sec_votes():
            actions = await actions_task or []
            vote_refs = await ex(parse_vote_references, actions) or {}
            house_raw, senate_raw = await asyncio.gather(
                ex(fetch_house_votes, vote_refs.get("house")),
                ex(fetch_senate_votes, vote_refs.get("senate")),
            )
            return {
                "section": "votes",
                "votes": {
                    "house": map_house_votes(house_raw),
                    "senate": map_senate_votes(senate_raw),
                },
            }

        async def sec_connections():
            # "amends" is parsed from the translation text, so this waits on the
            # translation core (not the slow Background) plus the related fetches.
            (translation, _refs, _plate), related, amendments, reports = await asyncio.gather(
                translate_core_task, related_task, amend_task, reports_task
            )
            connections = _build_connections(
                related or {}, amendments or [], bill_title, translation, reports or []
            )
            return {"section": "connections", "connections": connections}

        async def sec_lobbying():
            # "Who's pushing this" — entities recorded lobbying this bill, from
            # the reverse index (a fast local DB query).
            try:
                rows = await ex(db_get_bill_lobbying, congress, bill_type, number, 12)
            except Exception as e:
                print(f"[API] bill lobbying lookup error {bill_type}{number}: {e}")
                rows = []
            entities = [
                {"name": r["entity_name"], "kind": r["entity_kind"],
                 "mentions": r["mentions"], "spend": r["entity_spend"],
                 "bill_spend": r.get("bill_spend")}
                for r in (rows or [])
            ]
            return {"section": "lobbying", "entities": entities}

        async def sec_sponsor_money():
            # "Money behind the sponsors" — the FEC campaign totals for whoever
            # introduced this bill, shown beside "Who's pushing this." Facts
            # side by side, no implied link (that's the OpenSecrets layer). FEC
            # is federal-only, so state-bill sponsors return nothing.
            from sources import fec_client
            out = []
            for s in (sponsors or [])[:3]:
                try:
                    fin = await ex(fec_client.sponsor_finance,
                                   s.get("name"), s.get("state"), bill_type)
                except Exception as e:
                    print(f"[API] sponsor finance error {s.get('name')!r}: {e}")
                    fin = None
                if fin and (fin.get("receipts") or fin.get("disbursements")):
                    out.append({
                        "name": s.get("name"), "party": s.get("party"),
                        "state": s.get("state"), "finance": fin,
                    })
            return {"section": "sponsor_money", "sponsors": out}

        producers = [
            asyncio.ensure_future(sec_meta()),
            asyncio.ensure_future(sec_text()),
            asyncio.ensure_future(sec_sponsors()),
            asyncio.ensure_future(sec_translation()),
            asyncio.ensure_future(sec_background()),
            asyncio.ensure_future(sec_timeline()),
            asyncio.ensure_future(sec_votes()),
            asyncio.ensure_future(sec_connections()),
            asyncio.ensure_future(sec_lobbying()),
            asyncio.ensure_future(sec_sponsor_money()),
        ]

        for fut in asyncio.as_completed(producers):
            try:
                payload = await fut
                yield json.dumps(payload) + "\n"
            except Exception as e:
                print(f"[API] {noun} section error {bill_type}{number}: {e}")

        # Logging runs inside the generator (after content), so a logging/DB
        # hiccup must not break the stream before the `done` marker.
        try:
            if log_kind == "bill":
                log_bill_opened(bill_id=f"{bill_type}{number}", title=bill_title, from_query="")
            log_action(
                agent_name="api",
                action=f"get_{log_kind}",
                input_data=dict(meta_extra),
                output_data={"status": "complete"},
            )
        except Exception as e:
            print(f"[API] {noun} logging error {bill_type}{number}: {e}")

        yield json.dumps({"section": "done"}) + "\n"

    return stream


def _not_synced(congress, bill_type, number):
    """Why a bill page is empty. From the 108th Congress on, bills come
    from GovInfo's bill-status files synced daily, so a missing bill is
    either newer than the last sync or not a bill: say which file was read,
    and never ask Congress.gov instead."""
    import graph
    from sources import govinfo
    if int(congress) < govinfo.FIRST_CONGRESS:
        return "Bill not found or unavailable."
    label = f"{bill_type.upper()} {number} of the {graph._ordinal(int(congress))} Congress"
    dated = govinfo.billstatus_date(congress, bill_type)
    if not dated:
        return f"{label} is not in the bill data on this server."
    return (f"{label} is not in GovInfo's bill status file dated {dated}. "
            f"A bill introduced since appears after the next daily sync.")


@app.post("/bill")
@limiter.limit("30/minute")
async def get_bill(request: Request, body: BillRequest):
    """Streams bill detail as NDJSON — see _bill_detail_stream."""
    loop = asyncio.get_event_loop()

    # Base fetch is awaited up front so a genuinely missing bill still 404s
    # (once the stream body starts, the status code is already committed).
    try:
        bill_data = await loop.run_in_executor(
            None, fetch_bill, body.congress, body.bill_type, body.number
        )
    except Exception as e:
        # A bill file on disk that does not parse: say so, never a bare 500.
        print(f"[API] bill data unreadable {body.bill_type}{body.number}: {type(e).__name__}: {e}")
        raise HTTPException(status_code=503, detail="The bill data on this server could not be read for this bill.")
    if not bill_data:
        raise HTTPException(status_code=404, detail=_not_synced(body.congress, body.bill_type, body.number))

    meta_extra = {"congress": body.congress, "type": body.bill_type, "number": body.number}
    stream = _bill_detail_stream(bill_data, meta_extra, body.user_context, log_kind="bill", noun="bill")
    return StreamingResponse(stream(), media_type="application/x-ndjson", headers=STREAM_HEADERS)


@app.post("/law")
@limiter.limit("30/minute")
async def get_law(request: Request, body: LawRequest):
    """Streams law detail as NDJSON, using the same generator as /bill.

    A Public Law resolves to its underlying bill, so the shared generator
    handles everything once fetch_law hands back the bill record.
    """
    loop = asyncio.get_event_loop()

    try:
        bill_data = await loop.run_in_executor(
            None, fetch_law, body.congress, body.law_number
        )
    except Exception as e:
        print(f"[API] law data unreadable {body.congress}-{body.law_number}: {type(e).__name__}: {e}")
        raise HTTPException(status_code=503, detail="The bill data on this server could not be read for this law.")

    if not bill_data:
        # Recently enacted laws may not be indexed on Congress.gov yet. Stream a
        # friendly placeholder so the detail page still renders cleanly rather
        # than erroring.
        async def notfound():
            yield json.dumps({
                "section": "meta",
                "title": f"Public Law {body.congress}-{body.law_number}",
                "congress": body.congress, "law_number": body.law_number,
                "sponsors": [], "became_law": None,
            }) + "\n"
            yield json.dumps({
                "section": "translation",
                "translation": "This law was recently enacted and its full details are not yet available in Congress.gov. Check back soon.",
                "became_law": None, "has_background": False,
            }) + "\n"
            yield json.dumps({
                "section": "timeline",
                "timeline": "Timeline unavailable — law not yet indexed.",
                "timeline_events": [],
            }) + "\n"
            yield json.dumps({"section": "done"}) + "\n"

        return StreamingResponse(notfound(), media_type="application/x-ndjson", headers=STREAM_HEADERS)

    meta_extra = {"congress": body.congress, "law_number": body.law_number}
    stream = _bill_detail_stream(bill_data, meta_extra, body.user_context, log_kind="law", noun="law")
    return StreamingResponse(stream(), media_type="application/x-ndjson", headers=STREAM_HEADERS)


@app.post("/state/search")
@limiter.limit("20/minute")
async def state_search(request: Request, body: StateSearchRequest):
    import graph
    state_code = body.state_code.upper()
    if state_code not in graph.loaded_states():
        # Answered before the router runs: no model call for a state with no data.
        return {"query_type": "state_legislation", "state_code": state_code, "confidence": 1.0,
                "ambiguity_reason": _state_not_loaded(state_code), "query": None, "results": [],
                "empty_reason": "state_not_loaded"}
    try:
        loop = asyncio.get_event_loop()

        # The router: the state fast path (a bill ID with an optional
        # session), then Haiku. The caller picked the state, so the question
        # is a state one whatever the router thought. (A "switch to federal?"
        # hint lived here; it was computed after this override and never
        # fired, and nothing renders it, so it went on 2026-09-29.)
        structured = await loop.run_in_executor(None, lambda: route(
            body.question, state_code, plates=False, get_client=get_client)["structured"])
        structured["jurisdiction"] = "state"
        structured["state_code"] = state_code
        structured["_bypass_search_cache"] = bool(getattr(body, "fresh", False))

        result = await handle_state_search(structured, body.question, loop)
        return result

    except Exception as e:
        print(f"[API] State search error: {e}")
        raise HTTPException(status_code=500, detail="State search failed.")


def _state_bill_fingerprint(bill, versions, session=""):
    """Short fingerprint of a state bill's mutable state — its latest action
    and its text versions — so the translation cache invalidates when the
    bill moves through the legislature. The session is in it too: the cache
    key names the bill number only, and HB 1 is a different bill each year."""
    parts = [str(session), str(bill.get("latest_action_date") or ""), str(bill.get("latest_action") or ""),
             "|".join(v["name"] or "" for v in versions)]
    return hashlib.sha1("|".join(parts).encode()).hexdigest()[:12]


def _state_not_synced(st, session, bill_type, number):
    """Why a state bill page is empty: the files read, never a live call."""
    import graph
    conf = graph.LEGISLATURES.get(st)
    label = f"{bill_type.upper()} {number} of {graph.DIVISION_NAMES.get(st, st.upper())}'s {session} session"
    if not conf:
        return f"{graph.DIVISION_NAMES.get(st, st.upper())}'s legislature is not loaded on this server."
    bills = graph.data_path("state_bills", state=st, session=session)
    if session not in graph.state_sessions(st) or not bills.exists():
        return f"{label} is not on this server: no record of that session is on disk."
    month = (json.loads(bills.read_text()).get("meta") or {}).get("dump_month")
    return (f"{label} is not in the Open States record of {month or 'the last extract'}. "
            f"A bill filed since appears after the next monthly extract.")


def _state_bill_stream(page, st, session, key, user_context=None):
    """NDJSON section generator for /state/bill, from the files the sync
    keeps (plan step 13): the Open States record, its roll calls, the
    legislature's newer actions (advisory), the stored text. Only the
    translation calls a model."""
    import graph
    loop = asyncio.get_event_loop()

    def ex(fn, *args):
        return loop.run_in_executor(None, fn, *args)

    bill, people = page["bill"], page["people"]
    state_name = graph.DIVISION_NAMES.get(st, st.upper())
    person = lambda pid: people.get(pid) or {}  # noqa: E731
    sponsors = [{"name": person(sp["person"]).get("name") or sp["name"], "party": person(sp["person"]).get("party") or "",
                 "state": st.upper(), "primary": sp["primary"], "openstates_id": sp["person"]}
                for sp in bill["sponsors"]]
    versions = graph.state_versions(st, session, bill)
    fingerprint = _state_bill_fingerprint(bill, versions, session)
    lis_code = graph.lis_session(st, session)
    lis_url = (f"https://lis.virginia.gov/bill-details/{lis_code}/{bill['identifier'].replace(' ', '')}"
               if st == "va" and lis_code else None)
    synthetic_bill_data = {
        "bill": {
            "congress": None, "type": st.upper(), "number": bill["identifier"], "title": bill["title"],
            "sponsors": [{"fullName": sp["name"]} for sp in sponsors if sp["primary"]],
            "latestAction": {"text": bill.get("latest_action") or ""},
            "policyArea": {"name": (bill.get("subjects") or [""])[0]},
        }
    }

    async def stream():
        chosen = graph.default_version(versions)
        text_task = asyncio.ensure_future(ex(graph.state_version_text, st, session, chosen))

        async def sec_meta():
            return {"section": "meta", "identifier": bill["identifier"], "title": bill["title"], "state_code": st.upper(),
                    "state_name": state_name, "session": session, "chamber": bill.get("chamber"),
                    "is_state_bill": True, "source_url": lis_url, "sources": [{"url": u} for u in bill["sources"]],
                    "stage": graph.state_stage(bill["actions"], page["newer_actions"]),
                    "sponsors": [sp for sp in sponsors if sp["primary"]], "record": {
                        "source": page["meta"].get("source"), "dump_month": page["meta"].get("dump_month"),
                        "certification": "ingested",
                        "note": "Open States' copy of the legislature's record; not affirmed by a second publisher"}}

        async def sec_sponsors():
            return {"section": "sponsors", "sponsors": [sp for sp in sponsors if sp["primary"]],
                    "cosponsors": [sp for sp in sponsors if not sp["primary"]]}

        async def sec_votes():
            people_map = {pid: {"name": p.get("name"), "party": (p.get("party") or "")[:1]} for pid, p in people.items()}
            out, names = {}, {}
            conf = graph.LEGISLATURES.get(st) or {}
            # The page has two seat maps, "house" and "senate"; Nebraska's one
            # chamber takes the first. Names: California's lower is the Assembly.
            for chamber in graph.state_chambers(st):
                label = "senate" if chamber == "upper" else "house"
                sel = select_floor_roll_call(page["votes"], chamber, st.upper())
                out[label] = map_roll_call(sel, st.upper(), chamber, people_map) if sel else None
                names[label] = conf.get(chamber + "_short") or ("Legislature" if chamber == "legislature" else
                                                              graph.STATE_CHAMBERS[chamber])
            # Only names the page does not already use (House, Senate).
            names = {k: v for k, v in names.items() if v != {"house": "House", "senate": "Senate"}[k]}
            return {"section": "votes", "votes": out, "roll_calls": len(page["votes"]),
                    **({"chamber_names": names} if names else {})}

        async def sec_timeline():
            events = [{"date": a["date"], "text": a["description"], "chamber": a.get("chamber"),
                       "classification": a.get("classification")} for a in bill["actions"]]
            events += [{"date": h["date"], "text": h["description"], "chamber": h.get("chamber"),
                        "source": h["source"], "certification": h["certification"]} for h in page["newer_actions"]]
            return {"section": "timeline", "timeline": "", "timeline_events": events}

        async def sec_text():
            txt = await text_task
            return {"section": "bill_text", "bill_text": (txt or None) and txt[:50000],
                    "truncated": bool(txt) and len(txt) > 50000, "version": chosen and chosen["name"],
                    "versions": [{k: v[k] for k in ("name", "date", "url", "text")} for v in versions],
                    "empty_reason": None if txt else ("no text version of this bill is on this server yet"
                                                      if versions else "the record lists no text version")}

        async def sec_translation():
            txt = await text_task
            tr = await ex(translate_state_bill, synthetic_bill_data, txt, get_client(), fingerprint)
            return {"section": "translation", "translation": tr or "Translation unavailable for this bill."}

        producers = [asyncio.ensure_future(f()) for f in
                     (sec_meta, sec_sponsors, sec_votes, sec_timeline, sec_text, sec_translation)]
        for fut in asyncio.as_completed(producers):
            try:
                yield json.dumps(await fut) + "\n"
            except Exception as e:
                print(f"[API] state bill section error {st}/{session}/{key}: {e}")
        try:
            log_action(agent_name="api", action="get_state_bill",
                       input_data={"state": st, "session": session, "bill": key}, output_data={"status": "complete"})
        except Exception as e:
            print(f"[API] state bill logging error {st}/{session}/{key}: {e}")
        yield json.dumps({"section": "done"}) + "\n"

    return stream


@app.post("/state/bill")
@limiter.limit("30/minute")
async def get_state_bill(request: Request, body: StateBillRequest):
    """Streams a state bill as NDJSON, read from the files on this server —
    parity with the federal /bill."""
    import graph
    st, key = body.state_code.lower(), f"{body.bill_type.lower()}/{str(body.number).lower()}"
    try:
        page = await asyncio.to_thread(graph.state_bill_page, st, body.session, key)
    except Exception as e:
        print(f"[API] state bill data unreadable {st}/{body.session}/{key}: {type(e).__name__}: {e}")
        raise HTTPException(status_code=503, detail="The bill data on this server could not be read for this bill.")
    if not page:
        raise HTTPException(status_code=404, detail=_state_not_synced(st, body.session, body.bill_type, body.number))
    stream = _state_bill_stream(page, st, body.session, key, body.user_context)
    return StreamingResponse(stream(), media_type="application/x-ndjson", headers=STREAM_HEADERS)


@app.get("/api/state/bill/{st}/{session}/{bill_type}/{number}/text")
@limiter.limit("30/minute")
async def state_bill_text(request: Request, st: str, session: str, bill_type: str, number: str,
                          version: Optional[str] = None):
    """One text version of a state bill, whole, from the stored copy."""
    import graph
    page = await asyncio.to_thread(graph.state_bill_page, st.lower(), session, f"{bill_type.lower()}/{number.lower()}")
    if not page:
        raise HTTPException(status_code=404, detail=_state_not_synced(st.lower(), session, bill_type, number))
    versions = graph.state_versions(st.lower(), session, page["bill"])
    chosen = next((v for v in versions if v["name"] == version), None) if version else graph.default_version(versions)
    text = await asyncio.to_thread(graph.state_version_text, st.lower(), session, chosen)
    return {"text": text or None, "version": chosen and chosen["name"],
            "versions": [{k: v[k] for k in ("name", "date", "url", "text")} for v in versions],
            "empty_reason": None if text else "this version's text is not on this server yet"}


@app.post("/state/member/search")
async def state_member_search(request: Request, body: StateMemberSearchRequest):
    if not body.name.strip():
        raise HTTPException(status_code=400, detail="Name required")

    import graph
    state_code = (body.state_code or "").upper()
    if state_code not in graph.loaded_states():
        return {"found": False, "member": None, "reason": _state_not_loaded(state_code or "That state")}
    loop = asyncio.get_event_loop()
    found = await loop.run_in_executor(None, graph.state_member_lookup, state_code.lower(), body.name)
    if not found.get("person"):
        return {"found": False, "member": None, "candidates": found.get("candidates") or [],
                "reason": None if found.get("candidates") else
                f"No {state_code} legislator named {body.name.strip()} in the roster on this server."}
    member = found["person"]
    bills, read = await loop.run_in_executor(
        None, graph.state_member_bills, state_code.lower(), member["ocd_person_id"], 10)
    return {
        "found": True,
        "member": member,
        "legislation": {
            "sponsored": bills[:10],
            "sponsored_count": sum(1 for b in bills if b["sponsorship"] == "primary"),
            "cosponsored_count": sum(1 for b in bills if b["sponsorship"] == "cosponsor"),
            "sessions_read": read,
            "policy_areas": {},
        },
    }


@app.post("/member/search")
async def member_search(request: MemberSearchRequest):
    if not request.name.strip():
        raise HTTPException(status_code=400, detail="Name required")

    loop = asyncio.get_event_loop()

    member = await loop.run_in_executor(None, search_member, request.name)
    if member and member.get("candidates"):
        return {"found": False, "member": None, "candidates": member["candidates"]}
    if not member:
        return {"found": False, "member": None}

    profile, legislation = await asyncio.gather(
        loop.run_in_executor(None, fetch_member_profile, member["bioguide_id"]),
        loop.run_in_executor(None, fetch_member_legislation, member["bioguide_id"], 10),
    )

    return {
        "found": True,
        "member": {**member, **profile} if profile else member,
        "legislation": legislation,
    }


@app.get("/api/member/{bioguide}")
async def member_by_bioguide(request: Request, bioguide: str):
    """Federal member profile by bioguide id — powers deep links to /member/{id}."""
    loop = asyncio.get_event_loop()
    profile, legislation = await asyncio.gather(
        loop.run_in_executor(None, fetch_member_profile, bioguide),
        loop.run_in_executor(None, fetch_member_legislation, bioguide, 10),
    )
    if not profile:
        return {"found": False, "member": None}
    return {"found": True, "member": profile, "legislation": legislation}


@app.get("/api/bill/{congress}/{bill_type}/{number}/text")
@limiter.limit("30/minute")
async def bill_full_text(request: Request, congress: int, bill_type: str, number: int):
    """The complete bill text, fetched on demand when the reader is expanded
    (the streamed detail carries only a fast 50k preview)."""
    loop = asyncio.get_event_loop()
    txt = await loop.run_in_executor(
        None, fetch_bill_text, congress, bill_type.lower(), number, 1_000_000)
    return {"text": txt or None}


@app.get("/bill/{congress}/{bill_type}/{number}/text")
@limiter.limit("30/minute")
async def bill_text_reader(request: Request, congress: int, bill_type: str, number: int):
    """The whole bill as a readable page: Congress.gov's typescript reflowed
    into paragraphs with its outline intact. This is where "open in a new tab"
    goes; the JSON endpoint above is for the in-app reader."""
    from render.bill_text_format import bill_text_page
    loop = asyncio.get_event_loop()
    bt = bill_type.lower()
    txt, bill = await asyncio.gather(
        loop.run_in_executor(None, fetch_bill_text, congress, bt, number, 1_000_000),
        loop.run_in_executor(None, fetch_bill, congress, bt, number),
    )
    title = (((bill or {}).get("bill") or {}).get("title") or "").strip() or None
    page = await asyncio.to_thread(bill_text_page, congress, bt, number, txt, title)
    return HTMLResponse(page, headers=_SHELL_HEADERS)


@app.get("/api/bill/{congress}/{bill_type}/{number}/market")
@limiter.limit("15/minute")
async def bill_market_link(request: Request, congress: int, bill_type: str, number: int):
    """Legislation ↔ market: the Congress-traded stocks this bill may touch, how
    they moved around its key date, and which members traded them (flagging
    trades near that action). Lazy-loaded — slow (sector classification + price
    lookups) — and strictly juxtaposition, never a causal claim."""
    from money import bill_market
    loop = asyncio.get_event_loop()
    bill_data = await loop.run_in_executor(
        None, fetch_bill, congress, bill_type.lower(), number)
    if not bill_data or not bill_data.get("bill"):
        return {"found": False, "stocks": []}
    bill = bill_data["bill"]
    latest = bill.get("latestAction") or {}
    # Anchor on the bill's latest action date (for an enacted bill this is the
    # enactment; for a passed bill, passage). Fingerprint by that action so the
    # sector tag re-evaluates as the bill advances.
    event_date = latest.get("actionDate")
    fingerprint = f"{event_date or ''}|{(latest.get('text') or '')[:40]}"
    result = await loop.run_in_executor(
        None, bill_market.linkage, bill, event_date, fingerprint)
    result["found"] = True
    result["event_action"] = latest.get("text")
    return result


@app.get("/api/stock/{ticker}/timeline")
@limiter.limit("30/minute")
async def stock_timeline(request: Request, ticker: str):
    """Stock-centric chart data: a ticker's daily price history, the enacted laws
    in its sector plotted as dated events, and the members who traded it.
    Juxtaposition only — a law on the chart is a dated marker, not a cause."""
    from money import bill_market
    from money import law_corpus
    from money import stock_perf
    import datetime
    tk = (ticker or "").strip().upper()
    loop = asyncio.get_event_loop()

    def build():
        sector = bill_market.sector_of_tickers([tk]).get(tk, "Other")
        _, companies = bill_market._holdings()
        laws = law_corpus.laws_in_sector(sector)
        # Price window spans the plotted laws (with padding), min ~2 years.
        today = datetime.date.today()
        law_dates = [datetime.date.fromisoformat(l["date"]) for l in laws if l.get("date")]
        start = min(law_dates) - datetime.timedelta(days=45) if law_dates \
            else today - datetime.timedelta(days=730)
        ser = stock_perf.series(tk, start, today)
        idx, _c = bill_market._holdings()
        trades = []
        for rec in idx.get(tk, {}).values():
            for t in rec["trades"]:
                trades.append({"date": t["date"], "type": t["type"],
                               "amount": t.get("amount"), "owner": t.get("owner"),
                               "member": rec["name"]})
        return {
            "ticker": tk, "company": companies.get(tk, tk), "sector": sector,
            "series": [[d.isoformat(), round(c, 2)] for d, c in ser],
            "laws": laws, "trades": trades,
            "disclaimer": bill_market._DISCLAIMER,
        }

    return await loop.run_in_executor(None, build)


@app.get("/api/stocks/traded")
async def stocks_traded():
    """The tickers Congress has disclosed trading, with a company label — powers
    the stock picker on the market chart. Excludes non-sector instruments."""
    from money import bill_market
    loop = asyncio.get_event_loop()

    def build():
        idx, companies = bill_market._holdings()
        secs = bill_market.sector_of_tickers(list(idx.keys()))
        out = []
        for tk in idx:
            if secs.get(tk, "Other") == "Other":
                continue
            out.append({"ticker": tk,
                        "company": (companies.get(tk, tk) or "").replace(" - Common Stock", ""),
                        "sector": secs.get(tk), "traders": len(idx[tk])})
        out.sort(key=lambda x: -x["traders"])
        return {"stocks": out}

    return await loop.run_in_executor(None, build)


@app.get("/member/photo/{bioguide_id}")
async def member_photo(bioguide_id: str):
    from fastapi.responses import Response

    import graph
    bg = bioguide_id.strip()
    if re.fullmatch(r"[A-Za-z]\d{6}", bg):
        local = graph.data_path("images", bioguide=bg.upper())
        if local.exists():
            return Response(content=local.read_bytes(), media_type="image/jpeg",
                            headers={"Cache-Control": "public, max-age=604800"})
    # Kept live: a member newer than the mirror's last weekly pull. The
    # unitedstates/images repo is community-maintained and covers newer
    # members that congress.gov's /img/member/*_200.jpg path is missing (e.g.
    # Suhas Subramanyam) — try it first, then fall back to congress.gov.
    sources = [
        f"https://unitedstates.github.io/images/congress/225x275/{bg.upper()}.jpg",
        f"https://www.congress.gov/img/member/{bg.lower()}_200.jpg",
    ]
    headers = {"Referer": "https://www.congress.gov/", "User-Agent": "Mozilla/5.0"}
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client_http:
        for url in sources:
            try:
                r = await client_http.get(url, headers=headers)
            except Exception:
                continue
            if r.status_code == 200 and r.headers.get("content-type", "").startswith("image"):
                return Response(content=r.content, media_type="image/jpeg",
                                headers={"Cache-Control": "public, max-age=604800"})
    raise HTTPException(status_code=404, detail="Photo not found")


@app.get("/member/finance")
@limiter.limit("20/minute")
async def member_finance_endpoint(
    request: Request,
    name: str,
    state: Optional[str] = None,
    chamber: Optional[str] = None,
):
    """FEC campaign finance for a sitting federal member: totals + the source
    composition (individuals/PACs/party/self). Empty object when there's no
    confident FEC match (e.g. state legislators, whom the FEC doesn't cover)."""
    from sources import fec_client
    try:
        fin = await asyncio.to_thread(fec_client.member_finance, name, state, chamber)
        if not fin:
            return {}
        if fin.get("candidate_id") and fin.get("cycle"):
            fin["top_pacs"] = await asyncio.to_thread(
                fec_client.top_pac_contributors,
                fin["candidate_id"], fin["cycle"], fin.get("name"))
        return fin
    except Exception as e:
        print(f"[API] Member finance error for {name!r}: {e}")
        return {}


@app.get("/member/industries")
@limiter.limit("15/minute")
async def member_industries_endpoint(request: Request, cid: str, cycle: int):
    """Estimated industry breakdown of a member's individual donors — raw FEC
    employers classified by a cached LLM pass. Lazy: called after the finance
    section renders, keyed by the candidate_id it already resolved."""
    from sources import fec_client
    try:
        return await asyncio.to_thread(fec_client.member_industries, cid, cycle)
    except Exception as e:
        print(f"[API] Member industries error for {cid}: {e}")
        return {"industries": [], "cycle": cycle}


@app.get("/member/pac-interests")
@limiter.limit("15/minute")
async def member_pac_interests_endpoint(request: Request, cid: str, cycle: int, name: str = ""):
    """A member's PAC money grouped by the interest each PAC represents —
    industries and single-issue causes — from factual PAC identity."""
    from sources import fec_client
    try:
        return await asyncio.to_thread(fec_client.member_pac_interests, cid, cycle, name)
    except Exception as e:
        print(f"[API] Member PAC interests error for {cid}: {e}")
        return {"cycle": cycle, "interests": []}


def _load_house_stocks():
    """The pre-built House stock-trade dataset, parsed once per process and
    shared with bill_market (which used to parse the same 2 MB file again)."""
    from money.bill_market import load_house_stocks
    return load_house_stocks()


@app.get("/member/stocks")
@limiter.limit("30/minute")
async def member_stocks_endpoint(request: Request, bioguide: str):
    """Disclosed House stock trades for a member (STOCK Act PTRs), served from a
    pre-built dataset. Empty when the member hasn't filed trades, is a senator
    (House-only for now), or paper-files (unparseable)."""
    data = _load_house_stocks()
    rec = (data.get("members") or {}).get((bioguide or "").upper()) \
        or (data.get("members") or {}).get(bioguide or "")
    if not rec:
        filed = set(data.get("filed") or [])
        return {
            "trades": [],
            # filed a PTR we couldn't parse (paper) vs. filed nothing (no trades)
            "filed": bool({(bioguide or "").upper(), bioguide or ""} & filed),
            "generated": data.get("generated"), "cycles": data.get("cycles"),
        }

    trades = rec.get("trades", [])
    # Top tickers by trade frequency (equities only — skip bonds/funds w/o ticker).
    freq = {}
    buys = sells = 0
    for t in trades:
        if t.get("type", "").startswith("buy"):
            buys += 1
        elif t.get("type", "").startswith("sell"):
            sells += 1
        tk = t.get("ticker")
        if tk:
            freq[tk] = freq.get(tk, 0) + 1
    top = [{"ticker": k, "count": v} for k, v in
           sorted(freq.items(), key=lambda kv: kv[1], reverse=True)[:8]]
    return {
        "name": rec.get("name"),
        "trade_count": rec.get("trade_count", len(trades)),
        "buys": buys, "sells": sells,
        "top_tickers": top,
        "trades": trades[:40],
        "generated": data.get("generated"),
        "cycles": data.get("cycles"),
    }


_all_trades_index = None


def _flatten_trades():
    """One-time date-sorted index of every disclosed trade: (name, bioguide,
    trade) tuples that point at the already-parsed dataset rather than a
    second copy of every trade dict. Rows are materialized per page by
    _trade_row."""
    global _all_trades_index
    if _all_trades_index is None:
        data = _load_house_stocks()
        rows = []
        for bg, m in (data.get("members") or {}).items():
            name = m.get("name", "")
            for t in m.get("trades", []):
                rows.append((name, bg, t))

        def key(d):
            try:
                mm, dd, yy = d.split("/")
                return (int(yy), int(mm), int(dd))
            except Exception:
                return (0, 0, 0)
        rows.sort(key=lambda r: key(r[2].get("date") or ""), reverse=True)
        _all_trades_index = rows
    return _all_trades_index


def _trade_row(entry):
    name, bg, t = entry
    return {
        "member": name, "bioguide": bg,
        "ticker": t.get("ticker"), "asset": t.get("asset"),
        "type": t.get("type"), "date": t.get("date"),
        "amount": t.get("amount"), "owner": t.get("owner", ""),
    }


@app.get("/stocks/notable")
@limiter.limit("30/minute")
async def stocks_notable_endpoint(request: Request):
    """The most dramatic trades: biggest post-trade moves in the trade's favor."""
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "app", "notable_trades.json")) as f:
            return json.load(f)
    except Exception:
        return {"trades": []}


@app.get("/stocks/all")
@limiter.limit("30/minute")
async def stocks_all_endpoint(request: Request, q: str = "", page: int = 0, page_size: int = 60):
    """Every disclosed House trade, newest first — searchable by member or ticker."""
    ql = (q or "").strip().lower()
    rows = _flatten_trades()
    if ql:
        rows = [r for r in rows if ql in (r[0] or "").lower()
                or ql in (r[2].get("ticker") or "").lower()]
    page = max(0, page)
    page_size = min(max(page_size, 10), 100)
    start = page * page_size
    return {"total": len(rows), "page": page, "page_size": page_size,
            "trades": [_trade_row(r) for r in rows[start:start + page_size]]}


@app.get("/stock/perf")
@limiter.limit("40/minute")
async def stock_perf_endpoint(request: Request, ticker: str, date: str):
    """How a stock moved after a disclosed trade (Yahoo daily closes): the
    percent change 1 week / 1 month / 3 months out. Empty when no price data."""
    from money import stock_perf
    try:
        return await asyncio.to_thread(stock_perf.perf, ticker, date)
    except Exception as e:
        print(f"[API] stock perf error {ticker} {date}: {e}")
        return {}


@app.get("/api/elections")
@limiter.limit("10/minute")
async def elections_endpoint(request: Request, zip: Optional[str] = None, state: Optional[str] = None):
    """Returns upcoming and recent elections, optionally personalized by zip/state."""
    try:
        data = await fetch_elections(zip_code=zip, state_code=state)
        return data
    except Exception as e:
        print(f"[API] Elections error: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch elections data.")


@app.get("/elections")
async def elections_page():
    return FileResponse("frontend/elections.html")


@app.get("/lobbying/search")
@limiter.limit("30/minute")
async def lobbying_search(request: Request, q: str = ""):
    """Search lobbying registrants (firms) and clients by name (Senate LDA)."""
    loop = asyncio.get_event_loop()
    try:
        results = await loop.run_in_executor(None, lda_search_entities, q)
        return {"results": results}
    except Exception as e:
        print(f"[API] Lobbying search error: {e}")
        raise HTTPException(status_code=502, detail="Lobbying data source unavailable.")


@app.get("/lobbying/entity")
@limiter.limit("30/minute")
async def lobbying_entity(request: Request, kind: str, name: str):
    """Aggregated lobbying profile for one entity — spend, issues, lobbyists,
    counterparties, and bills lobbied (Senate LDA)."""
    if kind not in ("client", "registrant"):
        raise HTTPException(status_code=400, detail="kind must be 'client' or 'registrant'.")
    loop = asyncio.get_event_loop()
    try:
        profile = await loop.run_in_executor(None, lda_get_entity_profile, kind, name)
    except Exception as e:
        print(f"[API] Lobbying entity error: {e}")
        raise HTTPException(status_code=502, detail="Lobbying data source unavailable.")
    if profile is None:
        raise HTTPException(status_code=404, detail="Entity not found.")
    return profile


@app.get("/api/elections/{election_id}")
@limiter.limit("20/minute")
async def election_detail_endpoint(
    request: Request,
    election_id: str,
    zip: Optional[str] = None,
    state: Optional[str] = None,
):
    try:
        detail = await fetch_election_detail(election_id, zip_code=zip, state_code=state)
        if not detail:
            raise HTTPException(status_code=404, detail="Election not found.")
        return detail
    except HTTPException:
        raise
    except Exception as e:
        print(f"[API] Election detail error for {election_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch election detail.")


@app.get("/api/elections/{election_id}/polling")
@limiter.limit("10/minute")
async def election_polling_endpoint(
    request: Request,
    election_id: str,
    state: Optional[str] = None,
):
    try:
        data = await fetch_election_polling(election_id, state_code=state)
        return data
    except Exception as e:
        print(f"[API] Polling error for {election_id}: {e}")
        return {}


@app.get("/api/elections/{election_id}/finance")
@limiter.limit("10/minute")
async def election_finance_endpoint(
    request: Request,
    election_id: str,
    zip: Optional[str] = None,
    state: Optional[str] = None,
):
    """Federal campaign finance (FEC) for the candidates on this ballot. Empty
    object when the ballot has no federal race — the section stays hidden."""
    try:
        return await fetch_election_finance(election_id, zip_code=zip, state_code=state)
    except Exception as e:
        print(f"[API] Finance error for {election_id}: {e}")
        return {}


@app.get("/elections/{election_id}")
async def election_detail_page(election_id: str):
    return FileResponse("frontend/election_detail.html")


# ── SEO / social meta ─────────────────────────────────────────
# The SPA serves one shell for every route, so link previews would all
# show the homepage card. The head carries a <!-- meta:start/end --> block that
# gets rewritten per route: bills/laws get their real title (bill_fetcher is
# TTL-cached, and crawlers are the main consumers of these paths), tab routes
# get static copy, everything else keeps the default.

_SITE = "https://nospopuli.org"
_META_BLOCK_RE = re.compile(r"<!-- meta:start.*?<!-- meta:end -->", re.S)
_BILL_PATH_RE = re.compile(r"^bill/(\d{2,3})/([a-z]+)/(\d+)$")
_LAW_PATH_RE = re.compile(r"^law/(\d{2,3})/(\d+)$")
_STATE_BILL_PATH_RE = re.compile(r"^state/([a-z]{2})/([0-9A-Za-z]+)/([a-z]+)/(\d+)$")

_TAB_META = {
    "trades": ("Congressional Stock Trades — NosPopuli",
               "House members' STOCK Act disclosures, ranked by how the stock "
               "moved after the trade — juxtaposition, never accusation."),
    "lobbying": ("Lobbying — NosPopuli",
                 "Who lobbies Congress, on what, and what they spend — Senate "
                 "LDA filings laid side-by-side with the legislation."),
    "elections": ("Upcoming Elections — NosPopuli",
                  "Upcoming federal, state, and local elections — dates, "
                  "registration deadlines, and what's on your ballot."),
}


def _meta_block(title: str, desc: str, path: str) -> str:
    t = html_lib.escape(title[:140], quote=True)
    d = html_lib.escape(desc[:300], quote=True)
    url = html_lib.escape(f"{_SITE}/{path.lstrip('/')}".rstrip("/") or _SITE)
    return (
        "<!-- meta:start -->\n"
        f"  <title>{t}</title>\n"
        f'  <meta name="description" content="{d}">\n'
        f'  <link rel="canonical" href="{url}">\n'
        '  <meta property="og:site_name" content="NosPopuli">\n'
        '  <meta property="og:type" content="website">\n'
        f'  <meta property="og:title" content="{t}">\n'
        f'  <meta property="og:description" content="{d}">\n'
        f'  <meta property="og:url" content="{url}">\n'
        f'  <meta property="og:image" content="{_SITE}/static/og-card.png">\n'
        '  <meta name="twitter:card" content="summary_large_image">\n'
        "<!-- meta:end -->"
    )


async def _route_meta(path: str):
    """(title, description) for a route, or None to keep the default block."""
    if path in _TAB_META:
        return _TAB_META[path]
    sm = _STATE_BILL_PATH_RE.match(path)
    if sm:
        import graph
        st, session, btype, number = sm.groups()
        page = await asyncio.to_thread(graph.state_bill_page, st, session, f"{btype}/{number}")
        if not page:
            return None
        b = page["bill"]
        name = STATE_NAMES.get(st.upper(), st.upper())
        desc = f"{b['title']} — a {name} bill in plain English, with its votes and text."
        if b.get("latest_action"):
            desc += f" Latest action: {b['latest_action'][:140]}"
        return (f"{name} {b['identifier']} ({session}): {b['title']} — NosPopuli", desc)
    m = _BILL_PATH_RE.match(path) or _LAW_PATH_RE.match(path)
    if not m:
        return None
    try:
        if len(m.groups()) == 3:
            congress, btype, number = m.groups()
            label = f"{btype.upper()} {number}"
            bill = await asyncio.wait_for(
                asyncio.to_thread(fetch_bill, congress, btype, number), 3.0)
        else:
            congress, number = m.groups()
            label = f"Public Law {congress}-{number}"
            bill = await asyncio.wait_for(
                asyncio.to_thread(fetch_law, congress, number), 3.0)
    except Exception:
        bill = None
    bill = (bill or {}).get("bill") or {}  # raw Congress.gov payload
    title = (bill.get("title") or "").strip()
    action = ((bill.get("latestAction") or {}).get("text") or "").strip()
    if not title:
        return (f"{label} · {congress}th Congress — NosPopuli",
                "Read this bill in plain English on NosPopuli — votes, "
                "sponsors, money, and how to write your representative.")
    desc = f"{title} — in plain English, with votes, sponsors, and the money around it."
    if action:
        desc += f" Latest action: {action[:140]}"
    return (f"{label}: {title} — NosPopuli", desc)


# The shell is tiny and carries the ?v= cache-buster for the JS, so it must
# always be revalidated; otherwise a browser keeps an old shell pointing at an
# old script and no deploy ever shows up.
_SHELL_HEADERS = {"Cache-Control": "no-cache"}


async def _ledger_with_meta(path: str) -> HTMLResponse:
    html = await asyncio.to_thread(
        pathlib.Path("frontend/test.html").read_text)
    meta = await _route_meta(path.strip("/"))
    if meta:
        html = _META_BLOCK_RE.sub(
            lambda _: _meta_block(meta[0], meta[1], path), html, count=1)
    return HTMLResponse(html, headers=_SHELL_HEADERS)


@app.get("/")
@app.head("/")
async def root():
    return await _ledger_with_meta("")


@app.get("/robots.txt", include_in_schema=False)
async def robots_txt():
    return Response(
        "User-agent: *\nAllow: /\nDisallow: /monitor\nDisallow: /admin/\n"
        f"Sitemap: {_SITE}/sitemap.xml\n", media_type="text/plain")


@app.get("/sitemap.xml", include_in_schema=False)
async def sitemap_xml():
    pages = ["", "elections", "trades", "lobbying", "foundry"]
    urls = "\n".join(f"  <url><loc>{_SITE}/{p}</loc></url>" for p in pages)
    return Response(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        f"{urls}\n</urlset>\n", media_type="application/xml")


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return Response(status_code=204)


@app.get("/test")
async def test_home():
    return await _ledger_with_meta("")


@app.post("/flag/search")
async def flag_search(request: SearchFlagRequest):
    try:
        log_search_flag(
            query=request.query,
            results_shown=request.results_shown,
            reason=request.reason,
            notes=request.notes,
        )
        return {"status": "flagged", "message": "Thank you for the feedback."}
    except Exception as e:
        raise HTTPException(status_code=500, detail="Failed to log flag")


@app.post("/flag/bill")
async def flag_bill(request: BillFlagRequest):
    try:
        log_bill_flag(
            bill_id=request.bill_id,
            congress=request.congress,
            bill_type=request.bill_type,
            reason=request.reason,
            notes=request.notes,
            flagged_section=request.flagged_section,
        )
        return {"status": "flagged", "message": "Thank you for the feedback."}
    except Exception as e:
        raise HTTPException(status_code=500, detail="Failed to log flag")


@app.get("/monitor/flags")
async def get_all_flags(request: Request):
    _require_monitor_auth(request)
    return get_flags()


@app.get("/health")
@app.head("/health")
async def health():
    return {"status": "ok"}


# ── Event watcher ──

WATCHER_SECRET = os.getenv("WATCHER_SECRET", "")


@app.post("/watcher/run")
async def watcher_run(request: Request):
    secret = request.headers.get("X-Watcher-Secret", "")
    if not WATCHER_SECRET or secret != WATCHER_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized")
    from scripts.event_watcher import run_watcher
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, run_watcher)
    return result


@app.get("/correspondence/unsubscribe-link")
async def unsubscribe_link(email: str, bill_id: str):
    """One-click unsubscribe from email notifications."""
    from correspondence.db import deactivate_subscription
    deactivate_subscription(email, bill_id)
    return HTMLResponse(
        "<html><body style='font-family:Georgia,serif;max-width:480px;margin:4rem auto;"
        "color:#0e0e0e;padding:2rem'>"
        f"<p style='color:#6b6355;font-size:0.75rem;text-transform:uppercase;"
        "letter-spacing:0.08em'>NosPopuli</p>"
        f"<h2>Unsubscribed</h2>"
        f"<p>You'll no longer receive updates on <strong>{bill_id}</strong>.</p>"
        "<p><a href='/' style='color:#8b1a1a'>Return to NosPopuli</a></p>"
        "</body></html>"
    )


@app.get("/admin/elections")
async def admin_list_elections(request: Request, state: Optional[str] = None):
    _require_monitor_auth(request)
    return {"elections": db_list_known_elections(state)}


@app.post("/admin/elections")
async def admin_add_election(request: Request, body: KnownElectionRequest):
    _require_monitor_auth(request)
    try:
        new_id = db_add_known_election(
            state_code=body.state_code,
            name=body.name,
            date=body.date,
            election_type=body.type,
            source_url=body.source_url,
            notes=body.notes,
        )
        return {"id": new_id, "status": "ok"}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.delete("/admin/elections/{election_id}")
async def admin_delete_election(request: Request, election_id: int):
    _require_monitor_auth(request)
    db_delete_known_election(election_id)
    return {"status": "deleted"}


@app.get("/admin/elections/ui", response_class=HTMLResponse)
async def admin_elections_ui(request: Request):
    _require_monitor_auth(request)
    return FileResponse("frontend/admin_elections.html")


@app.get("/monitor", response_class=HTMLResponse)
async def monitor(request: Request):
    _require_monitor_auth(request)
    return FileResponse("frontend/monitor.html")


@app.get("/monitor/stream")
async def monitor_stream(request: Request, after: int = 0):
    """Agent-log entries appended since byte offset `after`. The monitor page
    polls with the last offset it was given, so each poll carries only new
    lines instead of the whole (ever-growing) log."""
    _require_monitor_auth(request)
    from agents.documentor_agent import read_log
    entries, offset = await asyncio.to_thread(read_log, after)
    return {"entries": entries, "offset": offset}


@app.post("/monitor/clear-search-log")
async def clear_search_log(request: Request):
    _require_monitor_auth(request)
    from search import search_logger
    search_logger.clear_log()
    return {"status": "cleared"}


@app.post("/monitor/clear-search-cache")
async def clear_search_cache_endpoint(request: Request):
    _require_monitor_auth(request)
    n = search_cache.clear()
    return {"status": "cleared", "entries_removed": n}


@app.get("/monitor/analysis")
async def get_analysis(request: Request):
    _require_monitor_auth(request)
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, analyze, get_client())
    return result


# ---------------------------------------------------------------------------
# Foundry lab console (localhost only in spirit: lab-grade data viewer).
# Serves the quarantine-aware store built by foundry/backfill.py. Uncertified
# records are displayed with warnings, never silently mixed with certified.

import pathlib as _pathlib
import threading as _threading

_FOUNDRY_STORE = _pathlib.Path("foundry/data/store")


@app.get("/foundry")
async def foundry_page():
    return FileResponse("frontend/foundry.html")


_FOUNDRY_HEALTH_PATH = _pathlib.Path("foundry/data/health/health.json")
# Serialized /api/foundry/data body, keyed on the store's file signature. The
# store is ~26 MB of JSON that parses into ~100 MB of Python objects; building
# it per request ratcheted the process RSS up by that much and never gave it
# back. Only the bytes stay resident now; the object tree is dropped after
# one json.dumps.
_FOUNDRY_PAYLOAD = {"sig": None, "body": None}
_FOUNDRY_PAYLOAD_LOCK = _threading.Lock()


def _foundry_store_signature():
    """(name, mtime_ns, size) for every store file plus the health ledger —
    the whole input set of the public payload."""
    sig = []
    for p in sorted(_FOUNDRY_STORE.glob("*.json")):
        try:
            st = p.stat()
        except OSError:
            continue
        sig.append((p.name, st.st_mtime_ns, st.st_size))
    if _FOUNDRY_HEALTH_PATH.exists():
        st = _FOUNDRY_HEALTH_PATH.stat()
        sig.append(("health.json", st.st_mtime_ns, st.st_size))
    return tuple(sig)


def _foundry_payload_bytes():
    sig = _foundry_store_signature()
    with _FOUNDRY_PAYLOAD_LOCK:
        if _FOUNDRY_PAYLOAD["sig"] == sig and _FOUNDRY_PAYLOAD["body"] is not None:
            return _FOUNDRY_PAYLOAD["body"]
    body = _build_foundry_payload_bytes()
    with _FOUNDRY_PAYLOAD_LOCK:
        _FOUNDRY_PAYLOAD["sig"] = sig
        _FOUNDRY_PAYLOAD["body"] = body
    return body


def _invalidate_foundry_payload():
    with _FOUNDRY_PAYLOAD_LOCK:
        _FOUNDRY_PAYLOAD["sig"] = None
        _FOUNDRY_PAYLOAD["body"] = None


@app.get("/api/graph/votes")
async def graph_votes(person: str, topic: Optional[str] = None):
    """The graph's first traversal: person → voted_on → agenda_item, topic
    optional. Fail-open to an honest empty state: no database, or a graph
    nobody has loaded yet, is reported as such, never as a 500 and never as
    'no votes'."""
    import graph
    if not os.getenv("DATABASE_URL"):
        return graph.shape_answer([], [], person, topic) | {
            "empty_reason": "graph unavailable: no database configured"}
    try:
        return await asyncio.to_thread(graph.votes, person, topic)
    except Exception as e:
        print(f"[API] graph votes error: {e}")
        return graph.shape_answer([], [], person, topic) | {
            "empty_reason": f"graph unavailable: {type(e).__name__} — has `python graph.py load fairfax-bos` been run?"}


@app.get("/api/graph/search")
async def graph_search(q: str):
    """A typed question → the graph, or an honest 'not mine'. Same $0
    regex contract as fast_route; nothing here calls a model. Fail-open
    like /api/graph/votes: no database is reported, never a 500."""
    import graph
    parsed = graph.parse_question(q)
    if parsed is None:
        return {"ask": None, "query": q, "rows": [], "hops": [], "weak_hops": [],
                "empty_reason": "not a question the graph answers: try 'how did <person> vote "
                                "on <topic>', 'who voted no on <instrument>', or 'who held "
                                "<seat> on <date>'"}
    if not os.getenv("DATABASE_URL"):
        return {"ask": parsed["ask"], "query": q, "rows": [], "hops": [], "weak_hops": [],
                "empty_reason": "graph unavailable: no database configured"}
    try:
        return await asyncio.to_thread(graph.answer, parsed, graph.pg_backend())
    except Exception as e:
        print(f"[API] graph search error: {e}")
        return {"ask": parsed["ask"], "query": q, "rows": [], "hops": [], "weak_hops": [],
                "empty_reason": f"graph unavailable: {type(e).__name__} — has `python graph.py load fairfax-bos` been run?"}


@app.get("/api/foundry/data")
async def foundry_data():
    body = await asyncio.to_thread(_foundry_payload_bytes)
    return Response(content=body, media_type="application/json")


def _foundry_store_paths():
    """(source stores, sidecars) — the same partition the old per-request
    builder used. Leading underscore = a sidecar that is not a source. The
    store dir is globbed by seven code paths; this keeps a new one from
    being rendered as a jurisdiction."""
    return [p for p in sorted(_FOUNDRY_STORE.glob("*.json"))
            if not (p.name.startswith("_") or "item-facts" in p.name
                    or "item-summaries" in p.name
                    or p.name in ("upcoming.json", "meeting-digests.json"))]


def _read_store_json(name):
    p = _FOUNDRY_STORE / name
    return json.loads(p.read_text()) if p.exists() else {}


def _build_foundry_payload_bytes():
    """Serialize the public payload straight into a byte buffer, one store
    file at a time. Each store is parsed only to read its kind and compute
    certification, then dropped; the bytes that go out are the file's own
    JSON (already valid), so no second copy of the whole payload is ever
    built. Peak is one parsed store, not all of them. Shape is identical to
    _build_foundry_payload."""
    def dumps(o):
        return json.dumps(o, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

    buckets = {"sources": [], "capital_projects": [], "elections": []}
    certification = {}
    upcoming = _read_store_json("upcoming.json")
    try:
        doc = _health.load()
        today = _dt.date.today().isoformat()
    except Exception:
        doc = None
    for path in _foundry_store_paths():
        raw = path.read_bytes()
        store = json.loads(raw)
        kind = store.get("meta", {}).get("kind")
        bucket = ("capital_projects" if kind == "capital_projects"
                  else "elections" if kind == "elections" else "sources")
        buckets[bucket].append((path.stem, raw.strip()))
        # Per-source certification context for the reader. Uncertified
        # records are shown, never hidden — but "uncertified" on its own
        # tells a reader nothing, and the honest answer ("their minutes are
        # really an agenda", "the clerk has not published yet") is already
        # in the health ledger. Only reader-facing fields cross this
        # boundary: no artifact paths, no costs, no model names, no traces.
        if bucket == "sources" and doc is not None:
            try:
                events = _health.events_for(doc, path.stem)
                row = _health.summarize(path.stem, store, events, today,
                                        upcoming.get(path.stem))
                certification[path.stem] = {
                    "certified": row["total_certified"],
                    "total": row["total_records"],
                    "pct": row["certified_pct"],
                    "reasons": row["quarantine_reasons"],
                    "oracle_status": row["oracle"]["status"],
                    "note": _health.public_note(row, events),
                }
            except Exception:
                certification = {}  # the ledger is advisory; never fail the page
                doc = None
        del store, raw
    if not buckets["sources"] and not buckets["capital_projects"]:
        raise HTTPException(status_code=404, detail="foundry store is empty — run foundry/backfill.py")

    out = io.BytesIO()
    out.write(b"{")

    def write_bucket(name, first):
        if not first:
            out.write(b",")
        out.write(dumps(name) + b":{")
        for i, (stem, raw) in enumerate(buckets[name]):
            if i:
                out.write(b",")
            out.write(dumps(stem) + b":" + raw)
        out.write(b"}")
        buckets[name] = None

    write_bucket("sources", True)
    out.write(b',"item_facts":' + dumps(_read_store_json("loudoun-bos-item-facts.json")))
    out.write(b',"item_summaries":' + dumps(_read_store_json("item-summaries.json")))
    out.write(b',"upcoming":' + dumps(upcoming))
    out.write(b',"meeting_digests":' + dumps(_read_store_json("meeting-digests.json")))
    write_bucket("capital_projects", False)
    write_bucket("elections", False)
    out.write(b',"certification":' + dumps(certification) + b"}")
    return out.getvalue()


def _build_foundry_payload():
    """Reference (dict) builder, kept for tests and one-off scripts. The
    served endpoint uses _build_foundry_payload_bytes."""
    sources, capital_projects, elections = {}, {}, {}
    for path in _foundry_store_paths():
        store = json.loads(path.read_text())
        kind = store.get("meta", {}).get("kind")
        if kind == "capital_projects":
            capital_projects[path.stem] = store
        elif kind == "elections":
            elections[path.stem] = store
        else:
            sources[path.stem] = store
    item_facts = _read_store_json("loudoun-bos-item-facts.json")
    item_summaries = _read_store_json("item-summaries.json")
    upcoming = _read_store_json("upcoming.json")
    digests = _read_store_json("meeting-digests.json")
    if not sources and not capital_projects:
        raise HTTPException(status_code=404, detail="foundry store is empty — run foundry/backfill.py")
    certification = {}
    try:
        doc = _health.load()
        today = _dt.date.today().isoformat()
        for source_id, store in list(sources.items()):
            events = _health.events_for(doc, source_id)
            row = _health.summarize(source_id, store, events, today,
                                    upcoming.get(source_id))
            certification[source_id] = {
                "certified": row["total_certified"],
                "total": row["total_records"],
                "pct": row["certified_pct"],
                "reasons": row["quarantine_reasons"],
                "oracle_status": row["oracle"]["status"],
                "note": _health.public_note(row, events),
            }
    except Exception:
        certification = {}
    return {"sources": sources, "item_facts": item_facts,
            "item_summaries": item_summaries, "upcoming": upcoming,
            "meeting_digests": digests, "capital_projects": capital_projects,
            "elections": elections, "certification": certification}


# --- Foundry search-onboarding: probe a named jurisdiction, preview-extract
# where a known platform family matches. Jobs run in a thread; the console
# polls. Previews are ingest-only by construction and persisted so a repeat
# search is instant.

import re as _re
import sys as _sys
import threading as _threading
import uuid as _uuid

_sys.path.insert(0, "foundry")
# legistar_family / discover / run_onboard (and the whole synthesis + oracle
# pipeline behind run_onboard) are imported inside _foundry_onboard_job: they
# are only needed by the admin onboarding thread, not to serve pages.

_FOUNDRY_JOBS = {}
_FOUNDRY_JOBS_MAX = 20
_FOUNDRY_JOB_LOG_MAX = 500


def _new_foundry_job(job_id, **fields):
    """Register a job record, evicting the oldest finished jobs so the dict
    (and the per-job log lists) cannot grow for the life of the process."""
    finished = [jid for jid, j in _FOUNDRY_JOBS.items() if j.get("status") != "running"]
    while len(_FOUNDRY_JOBS) >= _FOUNDRY_JOBS_MAX and finished:
        _FOUNDRY_JOBS.pop(finished.pop(0), None)
    log = _BoundedLog(_FOUNDRY_JOB_LOG_MAX)
    _FOUNDRY_JOBS[job_id] = {"status": "running", "log": log, "result": None, **fields}
    return _FOUNDRY_JOBS[job_id]


class _BoundedLog(list):
    """A list that keeps only its last N lines. Job code only ever calls
    .append and reads it whole, so this stays a plain list to callers."""
    def __init__(self, maxlen):
        super().__init__()
        self._maxlen = maxlen

    def append(self, line):
        super().append(line)
        if len(self) > self._maxlen:
            del self[: len(self) - self._maxlen]
_FOUNDRY_PREVIEWS = _pathlib.Path("foundry/data/preview")


def _foundry_slugs(name):
    base = _re.sub(r"[^a-z ]", "", name.lower())
    words = [w for w in base.split() if w not in
             ("county", "city", "of", "town", "va", "pa", "ca", "virginia",
              "pennsylvania", "california", "maryland", "md")]
    joined = "".join(words)
    out = [joined, joined + "county", joined + "city", joined + "va"]
    return list(dict.fromkeys(s for s in out if s))


def _foundry_probe_primegov(slug):
    import requests as _rq
    try:
        r = _rq.get(f"https://{slug}.primegov.com/api/v2/PublicPortal/"
                    f"ListArchivedMeetings?year=2026", timeout=12,
                    headers={"User-Agent": "nospopuli-foundry-lab"})
        return r.json() if r.status_code == 200 else None
    except Exception:
        return None


def _foundry_probe_host(url):
    import requests as _rq
    try:
        _rq.get(url, timeout=10, headers={"User-Agent": "nospopuli-foundry-lab"})
        return True
    except Exception:
        return False


class _FoundryJobCancelled(Exception):
    pass


def _foundry_onboard_job(job_id, name):
    import legistar_family as _legistar_family
    import discover as _discover
    import run_onboard as _run_onboard
    job = _FOUNDRY_JOBS[job_id]
    _raw_log = job["log"]

    # Cooperative cancellation: every stage (probes, discovery tool calls,
    # synthesis attempts) reports through log/prog, so checking the flag here
    # kills a run at its next heartbeat. A model call already in flight
    # finishes (and is billed) — the flag stops the NEXT stage.
    class _CancellableLog:
        @staticmethod
        def append(line):
            if job.get("cancel"):
                raise _FoundryJobCancelled
            _raw_log.append(line)

    log = _CancellableLog()

    def prog(pct, stage):
        if job.get("cancel"):
            raise _FoundryJobCancelled
        job["progress"] = {"pct": round(min(pct, 100), 1), "stage": stage}
    try:
        prog(2, "starting")
        slugs = _foundry_slugs(name)
        def finish_onboarding(slug):
            """Final stage: profile -> synthesized extractor -> store."""
            prog(60, "synthesizing extractor from profile")
            log.append("profile in hand — synthesizing an extractor and "
                       "gating it (a few minutes)...")
            source_id = _run_onboard.onboard(
                slug, log=log.append,
                prog=lambda frac, stage: prog(60 + 38 * frac, stage))
            if source_id:
                job.update(status="done",
                           result={"onboarded": True, "source_id": source_id})
            else:
                job.update(status="done", result={"platform_only": True,
                    "message": "Discovery succeeded but no synthesized "
                    "extractor cleared the gate. The profile is saved; a "
                    "human-assisted onboarding pass is the next step."})

        for slug in slugs:
            if _pathlib.Path(f"foundry/data/store/{slug}-bos.json").exists():
                log.append(f"{slug}-bos is already onboarded")
                job.update(status="done", result={"onboarded": True,
                                                  "source_id": f"{slug}-bos"})
                return
            cached = _FOUNDRY_PREVIEWS / f"{slug}-legistar.json"
            if cached.exists():
                log.append(f"cached preview found for {slug}")
                job.update(status="done", result=json.loads(cached.read_text()))
                return
            profile_cache = _pathlib.Path(f"foundry/data/discovery/{slug}_profile.json")
            if profile_cache.exists():
                log.append(f"cached discovery profile found for {slug}")
                finish_onboarding(slug)
                return
        log.append(f"probing platform families for tenant slugs: {', '.join(slugs)}")
        for i, slug in enumerate(slugs):
            prog(5 + 10 * i / len(slugs), f"probing Legistar ({slug})")
            log.append(f"Legistar Web API: {slug}...")
            if _legistar_family.probe(slug):
                log.append(f"LIVE Legistar tenant '{slug}' — extracting 5 most "
                           "recent meetings (deterministic, no LLM)...")
                records = _legistar_family.preview(
                    slug, progress=lambda frac: prog(
                        20 + 75 * frac, "extracting recent meetings"))
                result = {"source_id": f"{slug}-legistar",
                          "title": name.title(),
                          "sub": f"Legistar Web API tenant '{slug}' · search preview, not onboarded",
                          "records": records}
                _FOUNDRY_PREVIEWS.mkdir(parents=True, exist_ok=True)
                (_FOUNDRY_PREVIEWS / f"{slug}-legistar.json").write_text(json.dumps(result))
                log.append(f"extracted {len(records['meetings'])} meetings, "
                           f"{len(records['agenda_items'])} items, "
                           f"{len(records['vote_events'])} recorded votes — all ingest-only")
                job.update(status="done", result=result)
                return
        for i, slug in enumerate(slugs):
            prog(15 + 5 * i / len(slugs), f"probing PrimeGov ({slug})")
            log.append(f"PrimeGov: {slug}...")
            meetings = _foundry_probe_primegov(slug)
            if meetings is not None:
                log.append(f"LIVE PrimeGov tenant '{slug}' ({len(meetings)} meetings in "
                           "2026) — meeting list only; votes need journal-parsing "
                           "onboarding (see LA/M3)")
                job.update(status="done", result={"platform_only": True,
                    "message": f"PrimeGov tenant '{slug}' found with "
                    f"{len(meetings)} meetings in 2026. Vote extraction requires "
                    "full onboarding (journal PDF parsing + oracle), like Los Angeles."})
                return
        found = []
        for slug in slugs:
            # NB: no Granicus probe here — granicus.com uses wildcard DNS, so
            # tenant presence can't be confirmed cheaply.
            if _foundry_probe_host(f"https://pub-{slug}.escribemeetings.com/"):
                found.append(f"eScribe tenant pub-{slug}")
        if found:
            log.append("platforms present but not preview-capable: " + "; ".join(found))
            job.update(status="done", result={"platform_only": True,
                "message": "Found: " + "; ".join(found) + ". These need full "
                "onboarding (run foundry/discover.py, then synthesis + oracle)."})
            return
        log.append("no known platform matched any slug — escalating to the "
                   "discovery agent (web search + live probing; a few minutes)")
        slug = slugs[0]
        profile_path = _pathlib.Path(f"foundry/data/discovery/{slug}_profile.json")
        if profile_path.exists():
            log.append("cached discovery profile found")
            profile = json.loads(profile_path.read_text())
        else:
            prog(25, "discovery agent: searching and probing")
            calls = {"n": 0}

            def agent_log(line):
                log.append(line)
                calls["n"] += 1
                prog(25 + min(65, 65 * calls["n"] / 35),
                     "discovery agent: searching and probing")
            profile = _discover.discover(
                f"{name} — governing body meeting records (agendas, minutes, votes)",
                slug, budget=30, model="claude-sonnet-4-6", log=agent_log)
        if profile:
            finish_onboarding(slug)
        else:
            job.update(status="done", result={"platform_only": True,
                "message": "The discovery agent could not produce a source "
                "profile within budget. Next rung: a records request to the "
                "clerk (planned)."})
    except _FoundryJobCancelled:
        _raw_log.append("cancelled by user — no further stages will run")
        job.update(status="cancelled")
    except Exception as exc:
        _raw_log.append(f"error: {exc}")
        job.update(status="error")


class _FoundryOnboardBody(BaseModel):
    name: str


@app.post("/api/foundry/onboard")
async def foundry_onboard(body: _FoundryOnboardBody, request: Request):
    # Onboarding spends real LLM dollars (discovery agent + synthesis), so on
    # a public deployment it stays off unless explicitly enabled. Localhost
    # lab use needs no setup.
    local = request.client and request.client.host in ("127.0.0.1", "::1")
    if not local and os.environ.get("FOUNDRY_ONBOARD") != "on":
        raise HTTPException(
            status_code=403,
            detail="foundry onboarding is disabled on this deployment — the "
                   "ledger is read-only here (set FOUNDRY_ONBOARD=on to allow "
                   "search-triggered pipeline runs)")
    job_id = _uuid.uuid4().hex[:12]
    _new_foundry_job(job_id, progress={"pct": 0, "stage": "queued"})
    _threading.Thread(target=_foundry_onboard_job, args=(job_id, body.name),
                      daemon=True).start()
    return {"job_id": job_id}


@app.get("/api/foundry/onboard/{job_id}")
async def foundry_onboard_status(job_id: str):
    job = _FOUNDRY_JOBS.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="unknown job")
    if job["status"] != "running":
        job["progress"] = {"pct": 100, "stage": job["status"]}
    return job


@app.post("/api/foundry/onboard/{job_id}/cancel")
async def foundry_onboard_cancel(job_id: str, request: Request):
    local = request.client and request.client.host in ("127.0.0.1", "::1")
    if not local and os.environ.get("FOUNDRY_ONBOARD") != "on":
        raise HTTPException(status_code=403, detail="onboarding is disabled "
                            "on this deployment")
    job = _FOUNDRY_JOBS.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="unknown job")
    if job["status"] == "running":
        job["cancel"] = True
        job["progress"]["stage"] = "cancelling — stops at the next stage"
    return {"status": job["status"], "cancelling": bool(job.get("cancel"))}


# ---------------------------------------------------------------------------
# Foundry scraper-health console (/admin/foundry).
#
# The pipeline's verdicts used to live in a CI log nobody reads and a jsonl
# the runner throws away. foundry/health.py now writes them to a committed
# ledger; this serves that ledger next to what the stores actually contain,
# so "is this scraper healthy, and why is this county not certified?" is one
# page instead of an archaeology session.
#
# Gating: reading is MONITOR_SECRET. The $0 actions (refresh, recertify) are
# too. Oracle synthesis spends Opus, so it additionally needs localhost or
# FOUNDRY_ONBOARD=on — the same rule the search-onboarding path uses.

import datetime as _dt
import subprocess as _subprocess

_sys.path.insert(0, "foundry")
import health as _health  # noqa: E402
import budget as _budget  # noqa: E402

_FOUNDRY_ACTIVE = set()   # source_ids with a job in flight
_FOUNDRY_HEALTH_SKIP = ("item-facts", "item-summaries")


def _foundry_llm_open(request: Request) -> bool:
    local = bool(request.client and request.client.host in ("127.0.0.1", "::1"))
    return local or os.environ.get("FOUNDRY_ONBOARD") == "on"


def _foundry_ci_runs(limit=8):
    """Recent scheduled-refresh runs, when the gh CLI is available. On prod
    it is not, and the honest answer is "unknown" rather than a guess."""
    try:
        proc = _subprocess.run(
            ["gh", "run", "list", "--workflow=foundry-refresh.yml",
             "--limit", str(limit), "--json",
             "status,conclusion,createdAt,url,displayTitle"],
            capture_output=True, text=True, timeout=20)
        if proc.returncode != 0:
            return None
        return {"available": True, "runs": json.loads(proc.stdout)}
    except (OSError, ValueError, _subprocess.SubprocessError):
        return None


@app.get("/admin/foundry", response_class=HTMLResponse)
async def foundry_health_page(request: Request):
    _require_monitor_auth(request)
    return FileResponse("frontend/foundry_health.html")


# Summaries only (small) — never the parsed stores — cached on the same
# store signature as the public payload.
_FOUNDRY_HEALTH_ROWS = {"sig": None, "rows": None}


def _foundry_health_rows(doc, today):
    sig = (_foundry_store_signature(), today)
    with _FOUNDRY_PAYLOAD_LOCK:
        if _FOUNDRY_HEALTH_ROWS["sig"] == sig:
            return _FOUNDRY_HEALTH_ROWS["rows"]
    upcoming_path = _FOUNDRY_STORE / "upcoming.json"
    upcoming = json.loads(upcoming_path.read_text()) if upcoming_path.exists() else {}
    rows = []
    for path in sorted(_FOUNDRY_STORE.glob("*.json")):
        if path.name.startswith("_") or path.stem in ("upcoming", "item-summaries",
                                                      "meeting-digests"):
            continue
        if any(skip in path.name for skip in _FOUNDRY_HEALTH_SKIP):
            continue
        try:
            store = json.loads(path.read_text())
        except ValueError:
            continue
        events = _health.events_for(doc, path.stem)
        row = _health.summarize(path.stem, store, events, today,
                                upcoming.get(path.stem))
        del store
        row["events"] = events
        rows.append(row)
    with _FOUNDRY_PAYLOAD_LOCK:
        _FOUNDRY_HEALTH_ROWS["sig"] = sig
        _FOUNDRY_HEALTH_ROWS["rows"] = rows
    return rows


@app.get("/admin/foundry/health")
async def foundry_health_data(request: Request):
    _require_monitor_auth(request)
    doc = _health.load()
    today = _dt.date.today().isoformat()
    sources = await asyncio.to_thread(_foundry_health_rows, doc, today)
    for row in sources:
        row["busy"] = row["source_id"] in _FOUNDRY_ACTIVE

    # Extractor directories with no store behind them: onboarding attempts
    # that never landed. Their only record today is an empty directory, so
    # the console names them rather than letting them disappear.
    landed = {row["source_id"] for row in sources}
    orphans = []
    extractors = _pathlib.Path("foundry/extractors")
    if extractors.is_dir():
        for d in sorted(extractors.iterdir()):
            if not d.is_dir() or d.name.endswith("-oracle"):
                continue
            if d.name in landed:
                continue
            attempts = sorted(f.name for f in d.glob("v*_attempt*.py"))
            orphans.append({"source_id": d.name, "attempts": len(attempts),
                            "last_attempt": attempts[-1] if attempts else None})

    ledger = _budget.LEDGER
    return {
        "generated_at": _dt.datetime.now().isoformat(timespec="seconds"),
        "today": today,
        "gates": {"local": bool(request.client
                                and request.client.host in ("127.0.0.1", "::1")),
                  "llm_open": _foundry_llm_open(request)},
        "budget": {"cap_usd": float(os.environ.get("FOUNDRY_DAILY_BUDGET",
                                                   _budget.DEFAULT_DAILY_USD)),
                   "today_usd": round(_budget.spent_since(today), 4),
                   "month_usd": round(_budget.spent_since(today[:7]), 4),
                   "ledger_present": ledger.exists()},
        "last_run": doc.get("last_run"),
        "ci": _foundry_ci_runs(),
        "sources": sources,
        "orphan_extractors": orphans,
        "jobs": {jid: {"status": j["status"],
                       "source_id": j.get("source_id"),
                       "action": j.get("action"),
                       "progress": j.get("progress")}
                 for jid, j in _FOUNDRY_JOBS.items() if j.get("action")},
    }


def _foundry_action_job(job_id, source_id, action, attempts):
    """Run one pipeline action in a thread, logging into the job record."""
    job = _FOUNDRY_JOBS[job_id]
    log = job["log"].append
    _sys.path.insert(0, "foundry")
    try:
        import refresh as _refresh
        import run_oracle as _run_oracle
        _refresh.load_env()
        slug = source_id[: -len("-bos")] if source_id.endswith("-bos") else source_id
        if action == "refresh":
            store = json.loads((_FOUNDRY_STORE / f"{source_id}.json").read_text())
            curated = (store.get("meta") is None
                       or source_id in ("pittsburgh-legistar", "la-primegov",
                                        "loudoun-bos"))
            ok = (_refresh.refresh_curated(source_id, log) if curated
                  else _refresh.refresh_generic(source_id, store, log))
            job.update(status="done", result={"ok": bool(ok)})
        elif action == "recertify":
            counts = _run_oracle.recertify(slug, log)
            job.update(status="done", result={"counts": counts})
        elif action == "oracle":
            counts = _run_oracle.run(slug, attempts=attempts, log=log)
            job.update(status="done", result={"counts": counts})
        else:
            job.update(status="error")
            log(f"unknown action {action}")
    except (Exception, SystemExit) as exc:
        log(f"error: {exc}")
        job.update(status="error")
    finally:
        _FOUNDRY_ACTIVE.discard(source_id)
        _invalidate_foundry_payload()


def _start_foundry_action(request: Request, source_id, action, attempts=3):
    if not (_FOUNDRY_STORE / f"{source_id}.json").exists():
        raise HTTPException(status_code=404, detail=f"unknown source {source_id}")
    if source_id in _FOUNDRY_ACTIVE:
        raise HTTPException(status_code=409,
                            detail=f"{source_id} already has a job running")
    local = bool(request.client and request.client.host in ("127.0.0.1", "::1"))
    job_id = _uuid.uuid4().hex[:12]
    _new_foundry_job(job_id, source_id=source_id, action=action,
                     progress={"pct": 0, "stage": action})
    _FOUNDRY_ACTIVE.add(source_id)
    _threading.Thread(target=_foundry_action_job,
                      args=(job_id, source_id, action, attempts),
                      daemon=True).start()
    # On Railway the store is a committed artifact served read-only: a write
    # here lives until the next deploy and no further. Say so rather than
    # letting the operator think they changed prod.
    return {"job_id": job_id, "ephemeral": not local}


@app.post("/admin/foundry/refresh/{source_id}")
async def foundry_admin_refresh(source_id: str, request: Request):
    _require_monitor_auth(request)
    return _start_foundry_action(request, source_id, "refresh")


@app.post("/admin/foundry/recertify/{source_id}")
async def foundry_admin_recertify(source_id: str, request: Request):
    _require_monitor_auth(request)
    return _start_foundry_action(request, source_id, "recertify")


@app.post("/admin/foundry/oracle/{source_id}")
async def foundry_admin_oracle(source_id: str, request: Request,
                               attempts: int = 3):
    _require_monitor_auth(request)
    if not _foundry_llm_open(request):
        raise HTTPException(
            status_code=403,
            detail="oracle synthesis spends Opus — allowed from localhost, "
                   "or set FOUNDRY_ONBOARD=on to enable it on this deployment")
    try:
        _budget.check("oracle")
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return _start_foundry_action(request, source_id, "oracle",
                                 attempts=max(1, min(attempts, 5)))


@app.get("/admin/foundry/jobs/{job_id}")
async def foundry_admin_job(job_id: str, request: Request):
    _require_monitor_auth(request)
    job = _FOUNDRY_JOBS.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="unknown job")
    if job["status"] != "running":
        job["progress"] = {"pct": 100, "stage": job["status"]}
    return job


@app.get("/{full_path:path}", include_in_schema=False)
async def spa_fallback(full_path: str):
    """Serve the ask SPA for every in-app route, so deep links and refresh work.

    The old tabbed app that used to own /newspaper, /trades, /lobbying,
    /notifications, /law/* and /state/* is gone; those capabilities are listed
    in README.md under "Rebuilding the newspaper capabilities" and their
    endpoints still work. Until each view is rebuilt in ledger.js, those paths
    land on the ask SPA, which does not know them yet.
    """
    if full_path.startswith(("api/", "static/")) or "." in full_path.split("/")[-1]:
        raise HTTPException(status_code=404, detail="Not found")
    return await _ledger_with_meta(full_path)
