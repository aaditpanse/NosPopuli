import anthropic
import json
import os
import hashlib
from supabase import create_client
from dotenv import load_dotenv
from documentor_agent import log_action
from state_search_agent import STATE_JURISDICTIONS
from reference_resolver import resolve_references, REF_HARD_LIMIT
from correspondence.db import get_disk_cache, set_disk_cache

load_dotenv()

supabase = create_client(
    os.getenv("SUPABASE_URL"),
    os.getenv("SUPABASE_API_KEY")
)

def _cache_key(congress, bill_type, bill_number, fingerprint=None):
    # v3 prefix forced re-translation after the enacted-status fix. The
    # fingerprint suffix is what keeps a cached translation honest as the bill
    # moves: the prompt bakes in "Latest Action" + the bill text, so a
    # translation cached at "introduced / in committee" would otherwise keep
    # describing that stage long after the bill passed a chamber, got amended,
    # or became law. When the bill's state changes the fingerprint changes, the
    # key misses, and we re-translate. Fingerprint-less form kept for callers
    # that don't have the bill record handy.
    base = f"BILLS-v3-{congress}{bill_type}{bill_number}"
    return f"{base}-{fingerprint}" if fingerprint else base


def _bill_fingerprint(bill):
    """Short hash of the bill's mutable state — the pieces the translation
    actually depends on. Changes whenever the bill moves through Congress, so
    the translation cache regenerates instead of serving a stale current-status.
    updateDateIncludingText is bumped by Congress.gov on any change (including
    text substitutions); latest action + enacted status are belt-and-suspenders.
    """
    la = bill.get("latestAction") or {}
    parts = [
        str(bill.get("updateDateIncludingText") or bill.get("updateDate") or ""),
        str(la.get("actionDate") or ""),
        str(la.get("text") or ""),
        "law" if bill.get("laws") else "bill",
    ]
    return hashlib.sha1("|".join(parts).encode()).hexdigest()[:12]


_BG_CACHE_PREFIX = "bg:v1:"
_BG_CACHE_TTL_SECONDS = 60 * 24 * 3600  # 60 days — Background references are
                                        # nearly always stable concepts (FHA,
                                        # HUD, Section 230). The per-term
                                        # ref:v1: cache catches volatile ones.


def _bg_cache_key(congress, bill_type, bill_number):
    return f"{_BG_CACHE_PREFIX}{congress}{bill_type}{bill_number}"


def _get_cached_bg(congress, bill_type, bill_number):
    try:
        return get_disk_cache(_bg_cache_key(congress, bill_type, bill_number), _BG_CACHE_TTL_SECONDS)
    except Exception as e:
        print(f"[TRANSLATOR] BG cache read error: {e}")
        return None


def _store_cached_bg(congress, bill_type, bill_number, bg_markdown):
    try:
        set_disk_cache(_bg_cache_key(congress, bill_type, bill_number), bg_markdown)
    except Exception as e:
        print(f"[TRANSLATOR] BG cache write error: {e}")

def _get_cached(congress, bill_type, bill_number, fingerprint=None):
    try:
        package_id = _cache_key(congress, bill_type, bill_number, fingerprint)
        result = supabase.table("bill_translations") \
            .select("translation") \
            .eq("package_id", package_id) \
            .execute()
        if result.data:
            print(f"[TRANSLATOR] Cache hit: {package_id}")
            return result.data[0]["translation"]
        return None
    except Exception as e:
        print(f"[TRANSLATOR] Cache read error: {e}")
        return None

def _store_cached(congress, bill_type, bill_number, translation, fingerprint=None):
    try:
        package_id = _cache_key(congress, bill_type, bill_number, fingerprint)
        print(f"[TRANSLATOR] Attempting cache write: {package_id}")
        result = supabase.table("bill_translations").upsert({
            "package_id": package_id,
            "congress": int(congress),
            "bill_type": str(bill_type),
            "bill_number": int(bill_number),
            "translation": translation,
            "jurisdiction": "federal",
            "state_code": None,
        }).execute()
        print(f"[TRANSLATOR] Cache write result: {result.data}")
        # Prune superseded rows for this bill — older fingerprints and the old
        # fingerprint-less v3 row — so the table keeps exactly one current row
        # per bill instead of one per historical state.
        if fingerprint:
            supabase.table("bill_translations") \
                .delete() \
                .eq("congress", int(congress)) \
                .eq("bill_type", str(bill_type)) \
                .eq("bill_number", int(bill_number)) \
                .eq("jurisdiction", "federal") \
                .neq("package_id", package_id) \
                .execute()
    except Exception as e:
        print(f"[TRANSLATOR] Cache write error: {e}")

def _get_cached_by_key(key):
    try:
        result = supabase.table("bill_translations") \
            .select("translation") \
            .eq("package_id", key) \
            .execute()
        if result.data:
            print(f"[TRANSLATOR] Cache hit: {key}")
            return result.data[0]["translation"]
        return None
    except Exception as e:
        print(f"[TRANSLATOR] Cache read error: {e}")
        return None


def _store_cached_by_key(key, translation, jurisdiction='federal', state_code=None):
    try:
        supabase.table("bill_translations").upsert({
            "package_id": key,
            "congress": 0,
            "bill_type": "state",
            "bill_number": 0,
            "translation": translation,
            "jurisdiction": jurisdiction,
            "state_code": state_code,
        }).execute()
        print(f"[TRANSLATOR] Cached: {key}")
    except Exception as e:
        print(f"[TRANSLATOR] Cache write error: {e}")


def translate_state_bill(bill_data, bill_text, client):
    """
    Translate a state bill. Uses actual bill text if available,
    falls back to metadata only.
    """
    bill = bill_data.get("bill", {})

    state_code = bill.get("type", "")
    identifier = bill.get("number", "")
    title = bill.get("title", "Unknown")
    sponsors = bill.get("sponsors", [{}])
    sponsor = sponsors[0].get("fullName", "Unknown") if sponsors else "Unknown"
    status = (bill.get("latestAction") or {}).get("text", "Unknown")

    cache_key = f"STATE-{state_code}-{identifier}"
    cached = _get_cached_by_key(cache_key)
    if cached:
        return cached

    if bill_text and len(bill_text) > 200:
        text_section = f"\nActual bill text (first 3000 characters):\n{bill_text[:3000]}"
    else:
        text_section = ""

    state_name = STATE_JURISDICTIONS.get(state_code, "state")
    prompt = f"""
You are a plain English translator for state legislation.
Explain this bill clearly to a {state_name} resident with no legal background.
Be concise but complete. No jargon.

Bill: {identifier} — {title}
Sponsor: {sponsor}
Current Status: {status}
{text_section}

Explain in these four sections:
1. What this bill does in one sentence
2. Who it affects and how (specific groups: taxpayers, agencies, industries, individuals)
3. Costs, trade-offs, and obligations — what does this cost, who pays, what is required or restricted, and what is given up. If unknown, say so briefly.
4. What its current status means
"""

    message = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1500,
        messages=[{"role": "user", "content": prompt}]
    )

    translation = message.content[0].text
    _store_cached_by_key(cache_key, translation, jurisdiction='state', state_code=state_code)

    log_action(
        agent_name="translator",
        action="translate_state_bill",
        input_data={"state": state_code, "identifier": identifier},
        output_data={"translation_preview": translation[:100]}
    )

    return translation


def _parse_translation_json(raw: str):
    """Parse Haiku's JSON output. Returns (translation_markdown, unknown_refs).

    Fail-open: malformed JSON falls back to using the raw text as the
    translation and an empty unknown_refs list. The user still gets the
    explanation; we just skip the Background section."""
    body = raw
    if body.startswith("```"):
        body = body.strip("`").lstrip("json").strip()
    try:
        parsed = json.loads(body)
        translation = (parsed.get("translation") or "").strip()
        unknown_refs = parsed.get("unknown_refs") or []
        if not isinstance(unknown_refs, list):
            unknown_refs = []
        unknown_refs = [str(t).strip() for t in unknown_refs if str(t).strip()]
        if not translation:
            return raw, []
        return translation, unknown_refs
    except json.JSONDecodeError:
        # Treat the whole response as the translation — better than nothing.
        return raw, []


def _format_background(resolutions: dict) -> str:
    """Render the Background section appended after the four-part explanation."""
    lines = ["## Background"]
    for term, body in resolutions.items():
        lines.append(f"**{term}** — {body}")
    return "\n\n".join(lines)


def translate_bill(bill_data, client, user_context=None, bill_text=None):
    """Full translation: core plain-English explanation + resolved Background.

    Convenience wrapper that returns the assembled result in one shot. The
    streaming /bill and /law endpoints don't use this — they call
    translate_bill_core and resolve_bill_background separately so the fast core
    can render in ~3s while the slow Background (a Sonnet web search) streams in
    behind it. Retained for any caller that wants the blocking, combined form.
    """
    translation, unknown_refs = translate_bill_core(
        bill_data, client, user_context, bill_text
    )
    bg = resolve_bill_background(bill_data, unknown_refs, client)
    return _assemble(translation, bg)


def translate_bill_core(bill_data, client, user_context=None, bill_text=None):
    """Fast half: the Haiku plain-English explanation only.

    Returns (translation_markdown, unknown_refs). Does NOT resolve references
    — that's the slow Sonnet web search, handled separately by
    resolve_bill_background. Hits the translation cache row when warm.
    """
    bill = bill_data["bill"]

    congress = bill.get("congress")
    bill_type = (bill.get("type") or "").lower()
    bill_number = bill.get("number")
    # Fingerprint the bill's current state so the cache invalidates when the
    # bill moves through Congress (new action, amended text, enacted) instead
    # of freezing the translation at whatever stage it was first viewed.
    fingerprint = _bill_fingerprint(bill)

    # Translation core and the Background section live in SEPARATE cache rows
    # so bumping one prefix does not force the other to regenerate.
    cached_payload = _get_cached(congress, bill_type, bill_number, fingerprint)
    cached_translation, cached_refs = _parse_cache_payload(cached_payload)

    if cached_translation:
        log_action(
            agent_name="translator",
            action="translate_bill_core_cached",
            input_data={"congress": congress, "type": bill_type, "number": bill_number},
            output_data={"source": "cache_translation"},
        )
        return cached_translation, (cached_refs or [])

    title = bill.get("title", "Unknown")
    sponsors = bill.get("sponsors", [{}])
    sponsor = sponsors[0].get("fullName", "Unknown") if sponsors else "Unknown"
    status = bill.get("latestAction", {}).get("text", "Unknown")
    policy_area = bill.get("policyArea", {}).get("name", "")

    # Authoritative enacted-status signal. The Congress.gov bill record only
    # populates `laws` once a Public Law number is actually assigned (signed
    # by the President or override of veto). Inferring "enacted" from action
    # text is unreliable — the model previously read procedural notations
    # like "Motion to reconsider laid on the table" as evidence of enactment.
    laws = bill.get("laws") or []
    is_law = bool(laws)
    law_number = laws[0].get("number") if laws else None

    status_signal = (
        f"ENACTED. Became Public Law {law_number}." if is_law
        else "NOT YET LAW. Use the latest action text below to describe the current stage in plain English (introduced, in committee, passed one chamber, passed both chambers awaiting presentment, sent to the President, etc.) — never state or imply the bill has been signed into law."
    )

    text_section = ""
    if bill_text and len(bill_text) > 200:
        text_section = f"\nActual bill text (excerpt):\n{bill_text[:8000]}"

    prompt = f"""
You are a plain English translator for legislation.
Your only job is to explain a bill clearly to an average person.
No legal jargon. No assumptions about their background.
Be concise but complete.
Base your explanation on the actual bill text when provided — do not infer or guess.

Bill Title: {title}
Sponsor: {sponsor}
Latest Action: {status}
Enacted Status: {status_signal}
Policy Area: {policy_area}
{text_section}

Return ONLY valid JSON, no markdown fences. Shape:
{{
  "translation": "<the plain-English explanation as markdown — see structure below>",
  "unknown_refs": ["<term>", ...]
}}

The translation field is markdown with these four sections, in order:
1. What this bill does in one sentence
2. Who it affects and how (specific groups: taxpayers, agencies, industries, individuals)
3. Costs, trade-offs, and obligations — what does this cost, who pays, what is required or restricted, and what is given up (e.g. federal spending, new mandates, regulatory burdens, loss of existing rights or programs). If costs or trade-offs are unknown or not specified in the bill, say so briefly.
4. What its current status means — the Enacted Status line above is authoritative. Procedural notations like "Motion to reconsider laid on the table", "Read twice", "Referred to Committee", or chamber-passage votes do NOT mean the bill is law. Only when Enacted Status begins with "ENACTED" may you describe the bill as law.

unknown_refs is a list of proper-noun programs, funds, statutes, offices, or
doctrines this bill references by name but does NOT itself define, AND that an
average reader would likely need explained. List at most {REF_HARD_LIMIT} terms;
omit common civics terms ("Congress", "Department of Justice") and anything you
yourself can adequately define in the translation body. Return [] when nothing
qualifies. Do NOT write "the bill does not explain X" in the translation body —
listed terms will be covered separately in a Background section.
"""

    message = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1500,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = message.content[0].text.strip()
    translation, unknown_refs = _parse_translation_json(raw)
    # Persist translation + refs as JSON so a future bg:vN bump can
    # regenerate Background without re-running Haiku.
    _store_cached(
        congress, bill_type, bill_number,
        json.dumps({"translation": translation, "unknown_refs": unknown_refs}),
        fingerprint,
    )

    log_action(
        agent_name="translator",
        action="translate_bill_core",
        input_data={
            "congress": congress,
            "type": bill_type,
            "number": bill_number,
            "title": title,
        },
        output_data={
            "translation_preview": translation[:100],
            "translation_source": "haiku",
        },
    )

    return translation, unknown_refs


def resolve_bill_background(bill_data, unknown_refs, client):
    """Slow half: resolve referenced programs/statutes into a Background block.

    This is the ~75s Sonnet web search. Returns Background markdown (or '' when
    there's nothing to resolve). Hits the Background cache row when warm, so
    repeat opens are instant.
    """
    bill = bill_data["bill"]
    congress = bill.get("congress")
    bill_type = (bill.get("type") or "").lower()
    bill_number = bill.get("number")

    cached_bg = _get_cached_bg(congress, bill_type, bill_number)
    if cached_bg is not None:
        return cached_bg

    if unknown_refs:
        try:
            resolutions = resolve_references(unknown_refs, client)
        except Exception as e:
            print(f"[TRANSLATOR] Reference resolver error: {e}")
            resolutions = {}
        bg = _format_background(resolutions) if resolutions else ""
    else:
        bg = ""

    _store_cached_bg(congress, bill_type, bill_number, bg)
    return bg


def _assemble(translation: str, bg: str) -> str:
    if not bg:
        return translation
    return translation.rstrip() + "\n\n" + bg


def _parse_cache_payload(raw):
    """Cache rows are now JSON {translation, unknown_refs}. Older rows from
    before this split were plain markdown — treat those as a translation-only
    hit with no known refs. Returns (translation_or_None, refs_list)."""
    if not raw:
        return None, []
    s = raw.strip() if isinstance(raw, str) else raw
    if isinstance(s, str) and s.startswith("{"):
        try:
            obj = json.loads(s)
            return (obj.get("translation") or None, list(obj.get("unknown_refs") or []))
        except json.JSONDecodeError:
            pass
    return s, []