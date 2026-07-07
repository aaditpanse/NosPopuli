"""Resolve external references a bill mentions but does not define.

Bills routinely reference programs, funds, statutes, offices, or doctrines by
name without explaining them — e.g. "Anti-Weaponization Fund", "Section 230",
"Title IX". The translator catches the well-known ones from training data; this
module covers the long tail (newer programs, niche offices, recent doctrines)
via a single batched Sonnet web search per bill.

Cost discipline:
- One Sonnet call per translator invocation, never more, regardless of how
  many terms are uncached. The call is batched: every uncached term goes in
  one prompt and Sonnet returns them all together.
- Per-term cache (disk_cache, 7-day TTL) so repeat references across bills
  ("Section 230" shows up in dozens) only pay the Sonnet cost once.
- Hard ceiling on terms per call (REF_HARD_LIMIT) so a runaway detection
  ("this bill mentions 30 acronyms") can't blow up the prompt.

Failure is non-fatal: if Sonnet errors or returns malformed output, callers
fall through to translating without resolved-reference context — strictly no
worse than today's behavior.
"""

import hashlib
import json

from correspondence.db import get_disk_cache, set_disk_cache
from documentor_agent import log_action


_REF_CACHE_PREFIX = "ref:v1:"
_REF_CACHE_TTL_SECONDS = 7 * 24 * 3600  # 7 days
REF_HARD_LIMIT = 5  # max terms resolved per bill request


def _cache_key(term: str) -> str:
    return _REF_CACHE_PREFIX + hashlib.sha1(term.lower().strip().encode()).hexdigest()


def resolve_references(terms, client) -> dict:
    """Return {term: definition_text} for as many terms as possible.

    Terms already in the disk cache come back free. For uncached terms,
    issues exactly ONE Sonnet web search call covering the entire missing
    set. Empty input → empty dict, no API call.
    """
    if not terms:
        return {}

    # Dedup while preserving order, then enforce hard ceiling.
    seen = set()
    deduped = []
    for t in terms:
        key = (t or "").strip()
        if not key or key.lower() in seen:
            continue
        seen.add(key.lower())
        deduped.append(key)
    deduped = deduped[:REF_HARD_LIMIT]

    resolved = {}
    missing = []
    for term in deduped:
        try:
            hit = get_disk_cache(_cache_key(term), _REF_CACHE_TTL_SECONDS)
        except Exception:
            hit = None
        if hit:
            resolved[term] = hit
        else:
            missing.append(term)

    if missing:
        sonnet_definitions = _sonnet_batch_resolve(missing, client)
        for term, body in sonnet_definitions.items():
            try:
                set_disk_cache(_cache_key(term), body)
            except Exception as e:
                print(f"[REF RESOLVER] Cache write failed for {term!r}: {e}")
            resolved[term] = body

    log_action(
        agent_name="reference_resolver",
        action="resolve_references",
        input_data={
            "requested": len(deduped),
            "cache_hits": len(deduped) - len(missing),
            "sonnet_calls": 1 if missing else 0,
        },
        output_data={"resolved": len(resolved)},
    )
    return resolved


def _sonnet_batch_resolve(terms: list, client) -> dict:
    """One batched Sonnet web search call for every uncached term. Returns
    {term: "summary + Source: url"} on success, {} on any failure."""
    terms_block = "\n".join(f"- {t}" for t in terms)
    prompt = (
        "You are a legislative research assistant. Each item below is a "
        "program, fund, statute, office, doctrine, or initiative that a "
        "bill referenced but did not define. For each one, write 2-3 "
        "plain-English sentences explaining what it is, when/why it was "
        "created, and current status if relevant. Cite one authoritative "
        "source URL per term (gov, news, or Wikipedia).\n\n"
        f"Terms:\n{terms_block}\n\n"
        "Return ONLY valid JSON, no markdown fences. Format:\n"
        "{\n"
        '  "definitions": [\n'
        '    {"term": "<exact term from list>", "summary": "<2-3 sentences>", "source": "<url>"}\n'
        "  ]\n"
        "}"
    )

    try:
        msg = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=2000,
            tools=[{"type": "web_search_20260209", "name": "web_search"}],
            messages=[{"role": "user", "content": prompt}],
            # Safety cap: successful web searches finish in ~75s, but a runaway
            # can otherwise hold the request (and the open /bill stream) for
            # minutes. On timeout this raises and we fall through to no
            # Background — strictly no worse than an empty result.
            timeout=100.0,
        )
    except Exception as e:
        print(f"[REF RESOLVER] Sonnet error: {e}")
        return {}

    raw = "".join(
        getattr(b, "text", "") for b in msg.content if getattr(b, "type", "") == "text"
    ).strip()
    # Sonnet sometimes wraps in ```json … ``` despite the instruction.
    if raw.startswith("```"):
        raw = raw.strip("`").lstrip("json").strip()

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"[REF RESOLVER] JSON parse failed: {e} — body: {raw[:200]!r}")
        return {}

    out = {}
    requested_lower = {t.lower(): t for t in terms}
    for entry in parsed.get("definitions", []):
        term_raw = (entry.get("term") or "").strip()
        summary = (entry.get("summary") or "").strip()
        source = (entry.get("source") or "").strip()
        if not term_raw or not summary:
            continue
        # Match back to the originally-requested casing.
        canonical = requested_lower.get(term_raw.lower(), term_raw)
        body = summary + (f"\n\nSource: {source}" if source else "")
        out[canonical] = body
    return out
