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
import hashlib
import secrets
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel
from typing import Optional
from vote_parser_agent import parse_vote_references
from vote_fetcher_agent import fetch_house_votes, fetch_senate_votes
from vote_mapper_agent import map_house_votes, map_senate_votes
from bill_fetcher import fetch_bill, fetch_law, fetch_bill_text, fetch_related_bills, fetch_amendments, parse_amends_from_title, fetch_cosponsors
from committee_reports_fetcher import fetch_committee_reports_for_bill
from member_search_agent import (
    search_member,
    fetch_member_profile,
    fetch_member_legislation,
)
from query_expander_agent import expand_query
from search_logger import log_search, log_bill_opened, log_member_opened
from analyst_agent import analyze
from flag_logger import log_search_flag, log_bill_flag, get_flags
from feed_agent import fetch_feed
from civic_resolver import resolve_zip
from district_resolver import resolve_address, resolve_point, resolve_geoid
import search_cache
import httpx
import asyncio
import anthropic
import os
from dotenv import load_dotenv

load_dotenv()

from router_agent import route_query, extract_president_congress, fast_route, fast_route_state
from search_agent import search_bills, search_summaries
from title_search_agent import search_by_title
from bill_fetcher import fetch_bill
from translator_agent import translate_bill, translate_state_bill, translate_bill_core, resolve_bill_background
from historian_agent import (
    fetch_bill_actions,
    fetch_related_bills as historian_fetch_related_bills,
    summarize_history,
    structure_history,
)
from documentor_agent import log_action
from result_validator_agent import validate_results, validate_results_batch
from state_search_agent import (
    search_state_bills,
    get_recent_state_bills,
    ENABLED_STATES,
    fetch_state_bill_by_identifier,
    filter_enacted,
    get_state_validator_floor,
)
from state_bill_fetcher import (
    fetch_state_bill,
    fetch_state_bill_text,
    structure_state_actions,
)
from state_vote_mapper import select_floor_roll_call, map_roll_call
import legiscan_client as legiscan
from state_member_search_agent import (
    search_state_member,
    fetch_state_member_profile,
    fetch_state_member_bills,
)

from correspondence.router import router as correspondence_router
from correspondence.db import (
    list_known_elections as db_list_known_elections,
    add_known_election as db_add_known_election,
    delete_known_election as db_delete_known_election,
    get_bill_lobbying as db_get_bill_lobbying,
)
from elections_agent import (
    fetch_elections, fetch_election_detail, fetch_election_polling,
    fetch_election_finance,
)
from lda_client import search_entities as lda_search_entities, get_entity_profile as lda_get_entity_profile

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


class GeoidRequest(BaseModel):
    geoid: str


class StateBillRequest(BaseModel):
    ocd_id: str
    state_code: str
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
    entity = structured.get("entity_name", "")

    def fetch():
        import requests

        all_committees = []

        for chamber in ["senate", "house"]:
            url = "https://api.congress.gov/v3/committee"
            params = {
                "api_key": os.getenv("CONGRESS_API_KEY"),
                "format": "json",
                "limit": 250,
                "chamber": chamber,
            }
            r = requests.get(url, params=params, timeout=10)
            if r.status_code == 200:
                all_committees.extend(r.json().get("committees", []))

        name_lower = entity.lower()
        distinctive_words = [
            w
            for w in name_lower.split()
            if len(w) > 4
            and w
            not in {
                "committee",
                "senate",
                "house",
                "joint",
                "select",
                "special",
                "standing",
            }
        ]

        best = None
        best_score = 0

        for c in all_committees:
            cname = (c.get("name") or "").lower()
            score = sum(len(w) for w in distinctive_words if w in cname)
            if score > best_score:
                best_score = score
                best = c

        if not best:
            return None, []

        committee_name = best.get("name", "")
        search_payload = {
            "query": f'"{committee_name}" collection:BILLS congress:119 OR congress:118',
            "pageSize": 10,
            "offsetMark": "*",
            "sorts": [{"field": "publishdate", "sortOrder": "DESC"}],
        }

        import re

        search_r = requests.post(
            "https://api.govinfo.gov/search",
            json=search_payload,
            params={"api_key": os.getenv("GovInfo_API_KEY")},
            timeout=10,
        )

        bills = []
        if search_r.status_code == 200:
            for item in search_r.json().get("results", []):
                package_id = item.get("packageId", "")
                raw = package_id.replace("BILLS-", "")
                m = re.match(r"(\d+)([a-z]+)(\d+)", raw)
                if m:
                    bills.append(
                        {
                            "congress": int(m.group(1)),
                            "type": m.group(2),
                            "number": int(m.group(3)),
                            "title": item.get("title", ""),
                            "latest_action": "",
                            "date": item.get("dateIssued", "")[:10],
                        }
                    )

        return best, bills

    committee, bills = await loop.run_in_executor(None, fetch)

    if not committee:
        return {
            "query_type": "committee",
            "found": False,
            "confidence": structured.get("confidence"),
            "ambiguity_reason": structured.get("ambiguity_reason"),
        }

    return {
        "query_type": "committee",
        "found": True,
        "confidence": structured.get("confidence"),
        "ambiguity_reason": structured.get("ambiguity_reason"),
        "committee": {
            "name": committee.get("name"),
            "chamber": committee.get("chamber"),
            "system_code": committee.get("systemCode"),
            "url": committee.get("url"),
        },
        "bills": [b for b in bills if b.get("number")],
    }


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


async def handle_named_entity_search(structured, question, loop):
    named_entity = structured.get("named_entity") or question

    sc_key = None
    if not (structured.get("_bypass_search_cache") or search_cache.is_freshness_query(structured, question)):
        sc_key = search_cache.cache_key(
            {**structured, "keywords": [named_entity]},
            question,
            structured.get("result_count", 5),
        )
        cached = search_cache.get(sc_key)
        if cached:
            results = await loop.run_in_executor(None, search_cache.rehydrate, cached)
            log_search(
                query=question,
                query_type="named_entity",
                expanded_terms=[],
                results_count=len(results),
                result_ids=[f"{r.get('type', '')}{r.get('number', '')}" for r in results],
                confidence=structured.get("confidence", 1.0),
            )
            return {
                "query_type": "legislation",
                "confidence": structured.get("confidence", 1.0),
                "ambiguity_reason": structured.get("ambiguity_reason"),
                "query": structured,
                "results": results,
                "cached": True,
            }

    title_results = await loop.run_in_executor(None, search_by_title, named_entity, 3)

    if len(title_results) < 2:
        structured["expanded_terms"] = [named_entity]
        structured["original_question"] = question
        govinfo_results = await loop.run_in_executor(None, search_bills, structured)
        seen = {f"{r['congress']}{r['type']}{r['number']}" for r in title_results}
        for r in govinfo_results:
            key = f"{r['congress']}{r['type']}{r['number']}"
            if key not in seen:
                title_results.append(r)
                seen.add(key)
                if len(title_results) >= 4:
                    break

    # If the title search pinned an authoritative hit (hardcoded popular-names
    # table or scraped popular-names cache), the user typed a known short name —
    # surface that result first and let the validator only re-rank the rest.
    pinned = None
    pinned_sources = {"popular_names_hardcoded", "popular_names_cache"}
    if title_results and title_results[0].get("source") in pinned_sources:
        pinned = title_results[0]

    validated = await loop.run_in_executor(
        None, validate_results, question, title_results, get_client()
    )
    final = validated if validated else title_results
    if pinned:
        final = [pinned] + [r for r in final if not (
            r.get("congress") == pinned.get("congress")
            and r.get("type") == pinned.get("type")
            and r.get("number") == pinned.get("number")
        )]

    # Common-name disambiguation: when the same act name exists across 3+ different
    # congresses, surface the ambiguity rather than silently leading with the most recent.
    distinct_congresses = list({r.get("congress") for r in final if r.get("congress")})
    confidence = structured.get("confidence", 1.0)
    ambiguity_reason = structured.get("ambiguity_reason")
    if len(distinct_congresses) >= 3:
        from router_agent import congress_to_years
        years = sorted(
            [congress_to_years(c)[0] for c in distinct_congresses if c],
            reverse=True
        )[:4]
        year_list = ", ".join(str(y) for y in years)
        ambiguity_reason = (
            f"Multiple bills share this name across different Congresses "
            f"({year_list}). Showing the most relevant — add a year to your "
            f"search (e.g. \"{named_entity} {years[0]}\") to target a specific version."
        )
        confidence = min(confidence, 0.55)

    log_search(
        query=question,
        query_type="named_entity",
        expanded_terms=[],
        results_count=len(final),
        result_ids=[f"{r.get('type', '')}{r.get('number', '')}" for r in final],
        confidence=confidence,
    )

    if sc_key and final:
        await loop.run_in_executor(None, search_cache.store, sc_key, final)

    return {
        "query_type": "legislation",
        "confidence": confidence,
        "ambiguity_reason": ambiguity_reason,
        "query": structured,
        "results": final,
        "cached": False,
    }


async def handle_legislation_search(structured, question, loop):
    full_history = structured.get("full_history", False)
    max_results_override = structured.get("max_results_override")

    # Tier-1 cache: skip the expander + fetch + validator if we've seen this
    # query recently and it's not freshness-sensitive.
    cache_target = structured.get("result_count", 5)
    bypass = (
        full_history
        or structured.get("_bypass_search_cache")
        or search_cache.is_freshness_query(structured, question)
    )
    sc_key = None
    if not bypass:
        sc_key = search_cache.cache_key(structured, question, cache_target)
        cached = search_cache.get(sc_key)
        if cached:
            results = await loop.run_in_executor(None, search_cache.rehydrate, cached)
            log_search(
                query=question,
                query_type="legislation",
                expanded_terms=[],
                results_count=len(results),
                result_ids=[f"{r.get('type', '')}{r.get('number', '')}" for r in results],
                confidence=structured.get("confidence", 1.0),
            )
            return {
                "query_type": "legislation",
                "confidence": structured.get("confidence", 1.0),
                "ambiguity_reason": structured.get("ambiguity_reason"),
                "query": structured,
                "results": results,
                "cached": True,
            }

    expanded = await loop.run_in_executor(
        None,
        expand_query,
        structured.get("keywords", []),
        structured.get("topic", ""),
        get_client(),
    )
    structured["expanded_terms"] = expanded or []
    structured["original_question"] = question

    if full_history:
        max_results = max_results_override or 50
        govinfo_results = await loop.run_in_executor(
            None, search_bills, structured, max_results
        )
        summary_results = []
    elif structured.get("status") == "enacted":
        govinfo_results = await loop.run_in_executor(None, search_bills, structured)
        summary_results = []
    else:
        # Fetch 2× target so the validator has enough headroom without dropping below target
        fetch_count = min(structured.get("result_count", 5) * 2, 20)
        # Run a second keyword pass using the original (pre-expansion) keywords so that
        # explicitly named terms (like "340B") can't be lost if the expander drifts.
        orig_keywords = structured.get("keywords", [])
        keyword_structured = {**structured, "expanded_terms": orig_keywords}
        govinfo_results, govinfo_keyword, summary_results = await asyncio.gather(
            loop.run_in_executor(None, search_bills, structured, fetch_count),
            loop.run_in_executor(None, search_bills, keyword_structured, fetch_count // 2 or 3),
            loop.run_in_executor(
                None,
                search_summaries,
                " ".join(orig_keywords),
                structured.get("congress_numbers", [119, 118]),
            ),
        )
        # merge keyword pass into govinfo_results
        govinfo_results = govinfo_results + govinfo_keyword

    seen = set()
    merged = []
    for r in summary_results:
        key = f"{r.get('congress')}{r.get('type')}{r.get('number')}"
        if key not in seen and r.get("number"):
            seen.add(key)
            r["source"] = "summary"
            merged.append(r)
    for r in govinfo_results:
        key = f"{r.get('congress')}{r.get('type')}{r.get('number')}"
        if key not in seen and r.get("number"):
            seen.add(key)
            r["source"] = "govinfo"
            merged.append(r)

    # If a known-bill hint exists, prepend it to candidates so the validator
    # ranks it fairly — it gets a head start but can still lose to a better match.
    hint = structured.get("known_bill_hint")
    if hint and not full_history:
        hint_key = f"{hint.get('congress')}{hint.get('type')}{hint.get('number')}"
        if hint_key not in seen:
            hint_candidate = {
                "package_id": f"BILLS-{hint['congress']}{hint['type']}{hint['number']}",
                "title": f"{hint['type'].upper()} {hint['number']}",
                "date_issued": "",
                "congress": hint["congress"],
                "type": hint["type"],
                "number": hint["number"],
                "source": "hint",
            }
            merged.insert(0, hint_candidate)

    if full_history:
        # Run validator on history batch too — relaxed threshold (4 vs 5) so
        # "somewhat related" older bills pass, but tuna acts and impeachment
        # resolutions don't. Batched in groups of 20 to stay within token limits.
        raw_results = await loop.run_in_executor(
            None, validate_results_batch, question, merged, get_client(), 4
        )
    else:
        target = structured.get("result_count", 5)
        # Feed more candidates to the validator so filtering doesn't drop us below target
        candidates = merged[: target * 2]
        validated = await loop.run_in_executor(
            None, validate_results, question, candidates, get_client()
        )
        raw_results = validated[:target]

    log_search(
        query=question,
        query_type="legislation",
        expanded_terms=expanded,
        results_count=len(raw_results),
        result_ids=[f"{r.get('type', '')}{r.get('number', '')}" for r in raw_results],
        confidence=structured.get("confidence", 1.0),
    )

    log_action(
        agent_name="api",
        action="search",
        input_data={"question": question},
        output_data={"results_count": len(raw_results)},
    )

    if sc_key and raw_results:
        await loop.run_in_executor(None, search_cache.store, sc_key, raw_results)

    return {
        "query_type": "legislation",
        "confidence": structured.get("confidence", 1.0),
        "ambiguity_reason": structured.get("ambiguity_reason"),
        "query": structured,
        "results": raw_results,
        "cached": False,
    }


async def handle_named_entity_with_date(structured, question, loop):
    named_entity = structured.get("named_entity") or question
    congress_numbers = structured.get("congress_numbers", [119])

    all_results = await loop.run_in_executor(None, search_by_title, named_entity, 3)

    filtered = [r for r in all_results if r.get("congress") in congress_numbers]
    final = filtered if filtered else all_results

    validated = await loop.run_in_executor(
        None, validate_results, question, final, get_client()
    )
    final = validated if validated else final

    log_search(
        query=question,
        query_type="named_entity_with_date",
        expanded_terms=[],
        results_count=len(final),
        result_ids=[f"{r.get('type', '')}{r.get('number', '')}" for r in final],
        confidence=structured.get("confidence", 1.0),
    )

    return {
        "query_type": "legislation",
        "confidence": structured.get("confidence", 1.0),
        "ambiguity_reason": structured.get("ambiguity_reason"),
        "query": structured,
        "results": final,
    }


async def handle_concept_with_date(structured, question, loop):
    expanded = await loop.run_in_executor(
        None,
        expand_query,
        structured.get("keywords", []),
        structured.get("topic", ""),
        get_client(),
    )
    structured["expanded_terms"] = expanded or []
    structured["original_question"] = question

    govinfo_results = await loop.run_in_executor(None, search_bills, structured)

    raw_results = [r for r in govinfo_results if r.get("number") or r.get("law_number")][
        : structured.get("result_count", 5)
    ]

    validated = await loop.run_in_executor(
        None, validate_results, question, raw_results, get_client()
    )
    raw_results = validated if validated else raw_results

    log_search(
        query=question,
        query_type="concept_with_date",
        expanded_terms=expanded,
        results_count=len(raw_results),
        result_ids=[f"{r.get('type', '')}{r.get('number', '')}" for r in raw_results],
        confidence=structured.get("confidence", 1.0),
    )

    return {
        "query_type": "legislation",
        "confidence": structured.get("confidence", 1.0),
        "ambiguity_reason": structured.get("ambiguity_reason"),
        "query": structured,
        "results": raw_results,
    }


async def handle_law_search(structured, question, loop):
    structured["original_question"] = question
    structured["expanded_terms"] = structured.get("keywords", [])

    govinfo_results = await loop.run_in_executor(None, search_bills, structured)

    raw_results = [r for r in govinfo_results if r.get("number") or r.get("law_number")][
        : structured.get("result_count", 5)
    ]

    log_search(
        query=question,
        query_type="enacted",
        expanded_terms=structured.get("keywords", []),
        results_count=len(raw_results),
        result_ids=[f"{r.get('type', '')}{r.get('number', '')}" for r in raw_results],
        confidence=structured.get("confidence", 1.0),
    )

    return {
        "query_type": "legislation",
        "confidence": structured.get("confidence", 1.0),
        "ambiguity_reason": structured.get("ambiguity_reason"),
        "query": structured,
        "results": raw_results,
    }


async def handle_browse(structured, question, loop):
    structured["expanded_terms"] = []
    structured["keywords"] = []
    structured["original_question"] = question

    govinfo_results = await loop.run_in_executor(None, search_bills, structured)

    raw_results = [r for r in govinfo_results if r.get("number") or r.get("law_number")][
        : structured.get("result_count", 5)
    ]

    log_search(
        query=question,
        query_type="browse",
        expanded_terms=[],
        results_count=len(raw_results),
        result_ids=[f"{r.get('type', '')}{r.get('number', '')}" for r in raw_results],
        confidence=structured.get("confidence", 1.0),
    )

    return {
        "query_type": "legislation",
        "confidence": structured.get("confidence", 1.0),
        "ambiguity_reason": structured.get("ambiguity_reason"),
        "query": structured,
        "results": raw_results,
    }


async def handle_state_search(structured, question, loop):
    state_code = structured["state_code"]

    # ── Off-topic — same polite empty as federal ──
    if structured.get("query_type") == "off_topic":
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
            "state_code": state_code,
            "confidence": structured.get("confidence", 1.0),
            "ambiguity_reason": (
                "This doesn't look like a question about state legislation, "
                "legislators, or civic policy. Try a topic, bill ID (e.g. \"HB 1557\"), or a name."
            ),
            "query": structured,
            "results": [],
        }

    # ── Member query: route to legislator search instead of bill search ──
    if structured.get("query_type") == "member" and structured.get("entity_name"):
        member = await loop.run_in_executor(
            None, search_state_member, structured["entity_name"], state_code
        )
        if member:
            bills = await loop.run_in_executor(
                None, fetch_state_member_bills, member["ocd_person_id"], state_code, 10
            )
        else:
            bills = []
        return {
            "query_type": "state_member",
            "state_code": state_code,
            "confidence": structured.get("confidence", 1.0),
            "ambiguity_reason": structured.get("ambiguity_reason"),
            "member": member,
            "sponsored_bills": bills,
        }

    # ── Fast-path bill-ID (from fast_route_state) — session-aware ──
    fast_path = structured.get("_fast_path")
    specific_bill = structured.get("specific_bill") or {}
    if fast_path == "state_bill_id" and specific_bill.get("identifier"):
        identifier = specific_bill["identifier"]
        requested_session = structured.get("requested_session")
        ambiguity_note = None

        # Try requested session first when given, else current session
        if requested_session:
            direct = await loop.run_in_executor(
                None, fetch_state_bill_by_identifier, identifier, state_code, requested_session
            )
        else:
            direct = await loop.run_in_executor(
                None, fetch_state_bill_by_identifier, identifier, state_code
            )

        # Fall back to current session when explicit session lookup misses
        if not direct and requested_session:
            direct = await loop.run_in_executor(
                None, fetch_state_bill_by_identifier, identifier, state_code
            )
            if direct:
                ambiguity_note = (
                    f"No {identifier} found in the {requested_session} session — "
                    f"showing the current-session bill instead."
                )

        # No session given and current returned nothing — search across all sessions.
        # State legislatures reset numbering each session, so the user is likely
        # asking about a historical bill (e.g. FL HB 1557 from 2022).
        if not direct and not requested_session:
            any_session = await loop.run_in_executor(
                None, fetch_state_bill_by_identifier, identifier, state_code, "any"
            )
            if any_session:
                direct = any_session
                first = any_session[0] if any_session else {}
                sess = first.get("session") or "an earlier session"
                ambiguity_note = (
                    f"No {identifier} in the current session — showing {identifier} from {sess}. "
                    f"Add a year to your search (e.g. \"{identifier} from 2024\") to pin a specific version."
                )

        # Still nothing — likely a numbering-convention mismatch (e.g. Arkansas
        # House bills start at HB 1001, not HB 1). Fall through to a keyword
        # search using the identifier as the query, and tell the user.
        if not direct:
            fallback = await loop.run_in_executor(
                None, search_state_bills, identifier, state_code, None, 5
            )
            if fallback:
                ambiguity_note = (
                    f"No exact match for {identifier} in {state_code}. "
                    f"Showing the closest matches — your state may use a different numbering convention "
                    f"(e.g. Arkansas House bills start at HB 1001)."
                )
                direct = fallback
            else:
                ambiguity_note = (
                    f"No bill matching {identifier} found in {state_code}. "
                    f"Check the bill number — states use different numbering conventions."
                )

        return {
            "query_type": "state_legislation",
            "state_code": state_code,
            "confidence": 1.0,
            "ambiguity_reason": ambiguity_note,
            "query": structured,
            "results": direct or [],
        }

    # ── Legacy bill-ID fallback (when router didn't fast-path) ──
    bill_id_match = re.search(r"\b([HS][BJCR]?\s*\d+)\b", question, re.IGNORECASE)
    if bill_id_match:
        identifier = re.sub(r"\s+", " ", bill_id_match.group(1).upper().strip())
        direct = await loop.run_in_executor(
            None, fetch_state_bill_by_identifier, identifier, state_code
        )
        if direct:
            return {
                "query_type": "state_legislation",
                "state_code": state_code,
                "confidence": 1.0,
                "ambiguity_reason": None,
                "query": structured,
                "results": direct,
            }
        # fall through to text search if identifier not found

    target_count = max(structured.get("result_count", 5), 10)

    # ── Browse: "show me anything recent" / "latest bills" — bypass keyword
    # search and validator (there's no topic to score against). Goes straight
    # to the most-recently-updated state bills.
    if structured.get("query_subtype") == "browse":
        results = await loop.run_in_executor(
            None, get_recent_state_bills, state_code, target_count
        )
        if structured.get("status") == "enacted":
            enacted = filter_enacted(results or [])
            if enacted:
                results = enacted
        log_search(
            query=question, query_type="state_legislation", expanded_terms=[],
            results_count=len(results or []),
            result_ids=[r.get("identifier", "") for r in (results or [])],
            confidence=structured.get("confidence", 1.0),
        )
        return {
            "query_type": "state_legislation",
            "state_code": state_code,
            "confidence": structured.get("confidence", 1.0),
            "ambiguity_reason": None,
            "query": structured,
            "results": results or [],
        }

    # ── Search cache (per-state namespace, 30-min TTL) ──
    sc_key = None
    if not (structured.get("_bypass_search_cache") or search_cache.is_freshness_query(structured, question)):
        sc_key = search_cache.cache_key(
            structured, question, target_count,
            jurisdiction=f"state:{state_code.upper()}",
        )
        cached = search_cache.get(sc_key)
        if cached:
            results = await loop.run_in_executor(None, search_cache.rehydrate, cached)
            log_search(
                query=question, query_type="state_legislation", expanded_terms=[],
                results_count=len(results),
                result_ids=[r.get("identifier", "") for r in results],
                confidence=structured.get("confidence", 1.0),
            )
            return {
                "query_type": "state_legislation",
                "state_code": state_code,
                "confidence": structured.get("confidence", 1.0),
                "ambiguity_reason": None,
                "query": structured,
                "results": results,
                "cached": True,
            }

    # ── Search LegiScan with structured router output ──
    # Prefer named_entity (router's best guess at an act name); otherwise
    # synthesise a phrase from the keywords. Two-word topic phrases like
    # "housing affordability" often perform worse in the full-text index than
    # their canonical form ("affordable housing") — when the expander surfaces
    # a known synonym we'll let it ride at the front of the queue.
    named_entity = (structured.get("named_entity") or "").strip()
    keywords = structured.get("keywords", []) or []
    primary_term = named_entity or " ".join(keywords) or question

    expanded = await loop.run_in_executor(
        None, expand_query, keywords, structured.get("topic", ""), get_client()
    )

    # If the primary is just a keyword join (no named entity) and the expander
    # produced a synonym, use the synonym as the primary — they're often
    # canonical phrasings indexed more reliably.
    if not named_entity and expanded:
        primary_term = expanded[0]
        expanded = expanded[1:]

    seen = set()
    results = []

    # Primary term — retried once on empty/timeout (transient search slowness
    # is common on first-touch phrase queries).
    primary_results = await loop.run_in_executor(
        None, search_state_bills, primary_term, state_code, None, 20
    )
    if not primary_results:
        primary_results = await loop.run_in_executor(
            None, search_state_bills, primary_term, state_code, None, 20
        )
    for r in primary_results:
        key = r.get("ocd_id")
        if key and key not in seen:
            seen.add(key)
            results.append(r)

    # Enrichment from expanded synonyms is opportunistic. Skip entirely when
    # the primary already gave us a useful set (3+ hits), and stop on first
    # failure so one slow LegiScan response can't ruin the whole search.
    if len(results) < 3 and expanded:
        for term in expanded[:2]:
            try:
                term_results = await loop.run_in_executor(
                    None, search_state_bills, term, state_code, None, 20
                )
            except Exception:
                break
            if not term_results:
                continue
            for r in term_results:
                key = r.get("ocd_id")
                if key and key not in seen:
                    seen.add(key)
                    results.append(r)
            if len(results) >= target_count:
                break

    results = results[:target_count]

    if structured.get("status") == "enacted":
        enacted = filter_enacted(results)
        if enacted:
            results = enacted

    # Per-state validator floor (parametrised in state_search_agent.STATE_VALIDATOR_FLOOR)
    floor = get_state_validator_floor(state_code)
    results = await loop.run_in_executor(
        None, validate_results, question, results, get_client(), floor, True
    )

    if sc_key and results:
        await loop.run_in_executor(None, search_cache.store, sc_key, results)

    log_search(
        query=question,
        query_type="state_legislation",
        expanded_terms=expanded or [],
        results_count=len(results),
        result_ids=[r.get("identifier", "") for r in results],
        confidence=structured.get("confidence", 1.0),
    )

    return {
        "query_type": "state_legislation",
        "state_code": state_code,
        "confidence": structured.get("confidence", 1.0),
        "ambiguity_reason": None,
        "query": structured,
        "results": results,
        "cached": False,
    }


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


@app.post("/feed")
@limiter.limit("10/minute")
async def get_feed(request: Request, body: FeedRequest):
    """Returns personalized feed based on interests and representatives."""
    try:
        loop = asyncio.get_event_loop()
        items = await loop.run_in_executor(
            None,
            fetch_feed,
            body.interests,
            body.senator_bioguides,
            body.rep_bioguide,
            30,
            3,
            body.state_code,
        )
        return {"items": items, "count": len(items)}
    except HTTPException:
        raise
    except Exception as e:
        print(f"[API] Error generating feed: {e}")
        raise HTTPException(
            status_code=500, detail="Failed to generate feed. Please try again."
        )


# ── Search dispatcher helpers ──
#
# These are step-by-step transformations of a SearchRequest into a structured
# query, then a handler call. Each function does one thing so the /search
# endpoint can read top-to-bottom in 20 lines.

_PRESIDENTS = ("trump", "biden", "obama", "bush", "clinton", "reagan")
_PRESIDENTIAL_SIGNALS = ("signed", "passed", "under", "era", "administration", "presidency", "white house")
_CONGRESSIONAL_SIGNALS = ("voted", "sponsored", "senator", "representative", "voting record", "cosponsored")


def _resolve_routing(body: "SearchRequest") -> dict:
    """Pick the cheapest router that answers: state fast-path → federal fast-path → LLM."""
    structured = None
    if body.state_code and body.state_code.upper() in ENABLED_STATES:
        structured = fast_route_state(body.question, body.state_code)
    if structured is None:
        structured = fast_route(body.question)
    if structured is None:
        structured = route_query(body.question, get_client(), full_history=body.full_history)
    else:
        print(f"[ROUTER] fast-path hit: {structured.get('_fast_path')}")
    return structured


def _apply_request_flags(structured: dict, body: "SearchRequest") -> None:
    """Merge SearchRequest knobs (fresh, full_history, before_congress) into structured."""
    structured["full_history"] = body.full_history
    structured["max_results_override"] = body.max_results if body.full_history else None
    structured["_bypass_search_cache"] = bool(body.fresh)
    if body.full_history and body.before_congress:
        structured["before_congress"] = body.before_congress


def _apply_presidential_term_filter(structured: dict, question: str) -> None:
    """When the question references a president by era, restrict to their Congress numbers."""
    congresses = extract_president_congress(question)
    if congresses:
        structured["congress_numbers"] = congresses
        structured["time_range"] = "presidential term"


def _disambiguate_president_query(structured: dict, question: str) -> None:
    """Ex-presidents have both member records and signed legislation. When the
    user means the *legislation* (e.g. "trump signed border bills"), reclassify
    the member query as legislation. Trump defaults to legislation when
    ambiguous — historically he's queried more about laws than service."""
    if structured.get("query_type") != "member":
        return
    entity = (structured.get("entity_name") or "").lower()
    if not any(p in entity for p in _PRESIDENTS):
        return
    q = question.lower()
    has_pres = any(s in q for s in _PRESIDENTIAL_SIGNALS)
    has_cong = any(s in q for s in _CONGRESSIONAL_SIGNALS)
    if (has_pres and not has_cong) or (not has_cong and "trump" in entity):
        structured["query_type"] = "legislation"
        structured["entity_name"] = None


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


async def _dispatch_legislation_subtype(structured: dict, question: str, loop) -> dict:
    """Route the *legislation* family by subtype. Caller has already handled
    member, committee, specific_bill, off_topic, and state-jurisdiction."""
    subtype = structured.get("query_subtype", "concept")
    log_action(
        agent_name="dispatcher",
        action=subtype,
        input_data={"question": question, "congress": structured.get("congress_numbers")},
        output_data={},
    )
    if structured.get("full_history"):
        return await handle_legislation_search(structured, question, loop)
    if subtype == "named_entity":
        return await handle_named_entity_search(structured, question, loop)
    if subtype == "named_entity_with_date":
        return await handle_named_entity_with_date(structured, question, loop)
    if subtype == "concept_with_date":
        return await handle_concept_with_date(structured, question, loop)
    if subtype == "enacted" or structured.get("status") == "enacted":
        return await handle_law_search(structured, question, loop)
    if subtype == "browse":
        return await handle_browse(structured, question, loop)
    return await handle_legislation_search(structured, question, loop)


async def _dispatch(structured: dict, body: "SearchRequest", question: str, loop) -> dict:
    """Route a fully-prepared structured query to the right handler. Caller is
    responsible for all transformations on `structured` first."""
    suggested_jurisdiction = _route_jurisdiction(structured, body)

    # State queries take precedence over all other dispatch — once we're in a
    # state context we never route back to federal handlers.
    if structured.get("jurisdiction") == "state" and structured.get("state_code"):
        state_code = structured["state_code"]
        if state_code in ENABLED_STATES:
            result = await handle_state_search(structured, question, loop)
            if suggested_jurisdiction and isinstance(result, dict):
                result["suggested_jurisdiction"] = suggested_jurisdiction
            return result
        return {
            "query_type": "legislation",
            "confidence": 1.0,
            "ambiguity_reason": f"{state_code} state legislation is not yet available.",
            "query": structured,
            "results": [],
        }

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

    return await _dispatch_legislation_subtype(structured, question, loop)


@app.post("/search")
@limiter.limit("20/minute")
async def search(request: Request, body: SearchRequest):
    if not body.question.strip():
        raise HTTPException(status_code=400, detail="Question cannot be empty")

    try:
        loop = asyncio.get_event_loop()
        structured = _resolve_routing(body)
        _apply_request_flags(structured, body)
        _apply_presidential_term_filter(structured, body.question)
        _disambiguate_president_query(structured, body.question)
        return await _dispatch(structured, body, body.question, loop)
    except HTTPException:
        raise
    except Exception:
        import traceback
        print(f"[API] Full error: {traceback.format_exc()}")
        raise HTTPException(status_code=500, detail="Search failed. Please try again.")


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
        text_task    = asyncio.ensure_future(ex(fetch_bill_text, congress, bill_type, number))
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
            translation, refs = await ex(
                translate_bill_core, bill_data, get_client(), user_context, txt
            )
            return (translation or f"Translation unavailable for this {noun}.", refs or [])
        translate_core_task = asyncio.ensure_future(_translate_core())

        async def sec_meta():
            return {
                "section": "meta",
                "title": bill_title, "sponsors": sponsors, "became_law": became_law,
                **meta_extra,
            }

        async def sec_text():
            txt = await text_task
            return {"section": "bill_text", "bill_text": txt or None}

        async def sec_sponsors():
            cos = await cospon_task
            return {"section": "sponsors", "sponsors": sponsors, "cosponsors": cos or []}

        async def sec_translation():
            translation, refs = await translate_core_task
            return {
                "section": "translation",
                "translation": translation,
                "became_law": became_law,
                "has_background": bool(refs),
            }

        async def sec_background():
            _translation, refs = await translate_core_task
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
            (translation, _refs), related, amendments, reports = await asyncio.gather(
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
            import fec_client
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


@app.post("/bill")
@limiter.limit("30/minute")
async def get_bill(request: Request, body: BillRequest):
    """Streams bill detail as NDJSON — see _bill_detail_stream."""
    loop = asyncio.get_event_loop()

    # Base fetch is awaited up front so a genuinely missing bill still 404s
    # (once the stream body starts, the status code is already committed).
    bill_data = await loop.run_in_executor(
        None, fetch_bill, body.congress, body.bill_type, body.number
    )
    if not bill_data:
        raise HTTPException(status_code=404, detail="Bill not found or unavailable.")

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

    bill_data = await loop.run_in_executor(
        None, fetch_law, body.congress, body.law_number
    )

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
    state_code = body.state_code.upper()
    if state_code not in ENABLED_STATES:
        raise HTTPException(
            status_code=400,
            detail=f"{body.state_code} is not yet available. Check back soon.",
        )
    try:
        loop = asyncio.get_event_loop()

        # 1. Fast-path: regex match a state bill ID with optional session anchor.
        structured = fast_route_state(body.question, state_code)

        # 2. LLM router for everything else. We force jurisdiction=state since
        #    the caller already picked the state context.
        if structured is None:
            structured = await loop.run_in_executor(
                None, route_query, body.question, get_client()
            )
            structured["jurisdiction"] = "state"
            structured["state_code"] = state_code

        # 3. If the router thinks the user actually meant a federal query while
        #    sitting on a state context, surface that as a hint rather than a
        #    forced state interpretation. The frontend renders this as a nudge.
        suggested_jurisdiction = None
        if (
            structured.get("jurisdiction") == "federal"
            and not structured.get("_fast_path")
            and (structured.get("named_entity") or len(structured.get("keywords", [])) > 0)
        ):
            suggested_jurisdiction = "federal"

        structured["state_code"] = state_code
        structured["_bypass_search_cache"] = bool(getattr(body, "fresh", False))

        result = await handle_state_search(structured, body.question, loop)
        if suggested_jurisdiction:
            result["suggested_jurisdiction"] = suggested_jurisdiction
        return result

    except Exception as e:
        print(f"[API] State search error: {e}")
        raise HTTPException(status_code=500, detail="State search failed.")


def _state_bill_fingerprint(bill_data):
    """Short fingerprint of a state bill's mutable state, so the translation
    cache invalidates when the bill moves through the legislature. LegiScan's
    per-bill change_hash already captures this exactly; fall back to the latest
    action if it's ever missing."""
    change_hash = bill_data.get("change_hash")
    if change_hash:
        return str(change_hash)[:12]
    parts = [
        str(bill_data.get("latest_action_date") or ""),
        str(bill_data.get("latest_action_description") or ""),
    ]
    return hashlib.sha1("|".join(parts).encode()).hexdigest()[:12]


def _state_bill_stream(bill_data, state_code, ocd_id):
    """NDJSON section generator for /state/bill, mirroring the federal stream.
    The instant pieces (meta, votes, timeline, sponsors) come straight from the
    fetched bill; only the text fetch and Haiku translation are deferred, and
    they stream in as they resolve instead of blocking the whole page."""
    loop = asyncio.get_event_loop()

    def ex(fn, *args):
        return loop.run_in_executor(None, fn, *args)

    identifier = bill_data.get("identifier", "")
    title = bill_data.get("title", "")
    sources = [
        {"url": s.get("url"), "note": s.get("note") or ""}
        for s in (bill_data.get("sources") or []) if s.get("url")
    ]
    sponsors = [
        {"name": s.get("name", ""), "party": "", "state": state_code}
        for s in (bill_data.get("sponsorships") or []) if s.get("primary")
    ]
    fingerprint = _state_bill_fingerprint(bill_data)
    synthetic_bill_data = {
        "bill": {
            "congress": None, "type": state_code, "number": identifier, "title": title,
            "sponsors": [{"fullName": s["name"]} for s in sponsors],
            "latestAction": {"text": bill_data.get("latest_action_description", "")},
            "policyArea": {"name": ""},
        }
    }

    async def stream():
        text_task = asyncio.ensure_future(ex(fetch_state_bill_text, bill_data))

        async def sec_meta():
            return {
                "section": "meta", "identifier": identifier, "title": title,
                "state_code": state_code, "is_state_bill": True,
                "source_url": bill_data.get("url") or None,
                "sources": sources, "sponsors": sponsors,
            }

        async def sec_votes():
            # LegiScan getBill carries only roll-call *summaries*. Pick the floor
            # vote per chamber, then fetch its per-legislator detail (getRollCall)
            # and the session roster (getSessionPeople, cached) to label seats —
            # at most 2 roll-call queries + 1 cached roster per bill open.
            summaries = bill_data.get("votes") or []
            session_id = (bill_data.get("session_id")
                          or (bill_data.get("session") or {}).get("session_id"))
            selected = {
                "lower": select_floor_roll_call(summaries, "lower", state_code),
                "upper": select_floor_roll_call(summaries, "upper", state_code),
            }

            people_map = {}
            if any(selected.values()) and session_id:
                people_map = await ex(legiscan.get_session_people, session_id)

            async def build(chamber_class):
                sel = selected[chamber_class]
                if not sel or not sel.get("roll_call_id"):
                    return None
                rc = await ex(legiscan.get_roll_call, sel["roll_call_id"])
                if not rc:
                    return None
                return map_roll_call(rc, state_code, chamber_class, people_map)

            house, senate = await asyncio.gather(build("lower"), build("upper"))
            return {"section": "votes", "votes": {"house": house, "senate": senate}}

        async def sec_timeline():
            return {"section": "timeline", "timeline": "",
                    "timeline_events": structure_state_actions(bill_data)}

        async def sec_text():
            txt = await text_task
            return {"section": "bill_text", "bill_text": txt or None}

        async def sec_translation():
            txt = await text_task
            tr = await ex(translate_state_bill, synthetic_bill_data, txt, get_client(), fingerprint)
            return {"section": "translation", "translation": tr or "Translation unavailable for this bill."}

        producers = [
            asyncio.ensure_future(sec_meta()),
            asyncio.ensure_future(sec_votes()),
            asyncio.ensure_future(sec_timeline()),
            asyncio.ensure_future(sec_text()),
            asyncio.ensure_future(sec_translation()),
        ]
        for fut in asyncio.as_completed(producers):
            try:
                yield json.dumps(await fut) + "\n"
            except Exception as e:
                print(f"[API] state bill section error {ocd_id}: {e}")

        try:
            log_action(
                agent_name="api", action="get_state_bill",
                input_data={"ocd_id": ocd_id, "state": state_code},
                output_data={"status": "complete"},
            )
        except Exception as e:
            print(f"[API] state bill logging error {ocd_id}: {e}")
        yield json.dumps({"section": "done"}) + "\n"

    return stream


@app.post("/state/bill")
@limiter.limit("30/minute")
async def get_state_bill(request: Request, body: StateBillRequest):
    """Streams state bill detail as NDJSON — parity with the federal /bill."""
    loop = asyncio.get_event_loop()

    bill_data = await loop.run_in_executor(None, fetch_state_bill, body.ocd_id)
    if not bill_data:
        raise HTTPException(status_code=404, detail="State bill not found.")

    stream = _state_bill_stream(bill_data, body.state_code, body.ocd_id)
    return StreamingResponse(stream(), media_type="application/x-ndjson", headers=STREAM_HEADERS)


@app.post("/state/member/search")
async def state_member_search(request: Request, body: StateMemberSearchRequest):
    if not body.name.strip():
        raise HTTPException(status_code=400, detail="Name required")

    loop = asyncio.get_event_loop()
    member = await loop.run_in_executor(
        None, search_state_member, body.name, body.state_code
    )

    if not member:
        return {"found": False, "member": None}

    bills = await loop.run_in_executor(
        None, fetch_state_member_bills, member["ocd_person_id"], body.state_code, 10
    )

    return {
        "found": True,
        "member": member,
        "legislation": {
            "sponsored": bills,
            "sponsored_count": len(bills),
            "cosponsored_count": 0,
            "policy_areas": {},
        },
    }


@app.post("/member/search")
async def member_search(request: MemberSearchRequest):
    if not request.name.strip():
        raise HTTPException(status_code=400, detail="Name required")

    loop = asyncio.get_event_loop()

    member = await loop.run_in_executor(None, search_member, request.name)
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


@app.get("/member/photo/{bioguide_id}")
async def member_photo(bioguide_id: str):
    from fastapi.responses import Response

    bg = bioguide_id.strip()
    # The unitedstates/images repo is community-maintained and covers newer
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


@app.get("/")
@app.head("/")
async def root():
    return FileResponse("frontend/index.html")


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return Response(status_code=204)


@app.get("/test")
async def test_home():
    return FileResponse("frontend/test.html")


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
    from event_watcher import run_watcher
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
async def monitor_stream(request: Request):
    _require_monitor_auth(request)
    try:
        with open("agent_log.json", "r") as f:
            log = json.load(f)
        return log
    except:
        return []


@app.post("/monitor/clear-search-log")
async def clear_search_log(request: Request):
    _require_monitor_auth(request)
    with open("search_log.json", "w") as f:
        json.dump([], f)
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

_FOUNDRY_STORE = _pathlib.Path("foundry/data/store")


@app.get("/foundry")
async def foundry_page():
    return FileResponse("frontend/foundry.html")


@app.get("/api/foundry/data")
async def foundry_data():
    sources = {}
    capital_projects = {}
    elections = {}
    for path in sorted(_FOUNDRY_STORE.glob("*.json")):
        if ("item-facts" in path.name or "item-summaries" in path.name
                or path.name in ("upcoming.json", "meeting-digests.json")):
            continue
        store = json.loads(path.read_text())
        # Non-meetings record types (CIP capital projects, local elections)
        # ride in their own maps so the meetings timeline renderer never sees
        # a shape it can't read.
        kind = store.get("meta", {}).get("kind")
        if kind == "capital_projects":
            capital_projects[path.stem] = store
        elif kind == "elections":
            elections[path.stem] = store
        else:
            sources[path.stem] = store
    facts_path = _FOUNDRY_STORE / "loudoun-bos-item-facts.json"
    item_facts = json.loads(facts_path.read_text()) if facts_path.exists() else {}
    summaries_path = _FOUNDRY_STORE / "item-summaries.json"
    item_summaries = json.loads(summaries_path.read_text()) if summaries_path.exists() else {}
    upcoming_path = _FOUNDRY_STORE / "upcoming.json"
    upcoming = json.loads(upcoming_path.read_text()) if upcoming_path.exists() else {}
    digests_path = _FOUNDRY_STORE / "meeting-digests.json"
    digests = json.loads(digests_path.read_text()) if digests_path.exists() else {}
    if not sources and not capital_projects:
        raise HTTPException(status_code=404, detail="foundry store is empty — run foundry/backfill.py")
    return {"sources": sources, "item_facts": item_facts,
            "item_summaries": item_summaries, "upcoming": upcoming,
            "meeting_digests": digests, "capital_projects": capital_projects,
            "elections": elections}


# --- Foundry search-onboarding: probe a named jurisdiction, preview-extract
# where a known platform family matches. Jobs run in a thread; the console
# polls. Previews are ingest-only by construction and persisted so a repeat
# search is instant.

import re as _re
import sys as _sys
import threading as _threading
import uuid as _uuid

_sys.path.insert(0, "foundry")
import legistar_family as _legistar_family
import discover as _discover
import run_onboard as _run_onboard

_FOUNDRY_JOBS = {}
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
    _FOUNDRY_JOBS[job_id] = {"status": "running", "log": [], "result": None,
                             "progress": {"pct": 0, "stage": "queued"}}
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
