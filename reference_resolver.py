"""Resolve external references a bill mentions but does not define.

Bills routinely reference programs, funds, statutes, offices, or doctrines by
name without explaining them — e.g. "Anti-Weaponization Fund", "Section 230",
"Title IX". The translator catches the well-known ones from training data; this
module covers the long tail (newer programs, niche offices, recent doctrines)
via a single batched Haiku web search per bill.

Cost discipline:
- One resolver call per translator invocation, never more, regardless of how
  many terms are uncached. The call is batched: every uncached term goes in
  one prompt and the model returns them all together.
- Per-term cache (disk_cache, ~1-year TTL) keyed on a normalized term so
  repeat references across bills ("Section 230" shows up in dozens) only pay
  the resolver cost once, and stay warm long enough to actually amortize.
- Hard ceiling on terms per call (REF_HARD_LIMIT) so a runaway detection
  ("this bill mentions 30 acronyms") can't blow up the prompt.

Failure is non-fatal: if the resolver errors or returns malformed output,
callers fall through to translating without resolved-reference context —
strictly no worse than today's behavior.
"""

import hashlib
import json
import re

from correspondence.db import get_disk_cache, set_disk_cache
from documentor_agent import log_action


_REF_CACHE_PREFIX = "ref:v1:"
# Per-term definitions are stable historical facts ("Defense Production Act of
# 1950" doesn't change), and each term is resolved with a paid web
# search. A short TTL made the cache forget the common statutes weekly and
# re-pay to resolve the exact same terms — the dominant cost. Hold them ~1 year
# so the common-statute cache saturates and steady-state cost collapses toward
# the ~$0.01 Haiku core. A bill's own Background row (bg:) re-checks far sooner.
_REF_CACHE_TTL_SECONDS = 365 * 24 * 3600  # ~1 year
REF_HARD_LIMIT = 5  # max terms resolved per bill request


def _normalize_term(term: str) -> str:
    """Canonicalize a term for cache lookup so trivial phrasing differences hit
    one row. Conservative on purpose: we DON'T strip a trailing year, because
    "Civil Rights Act of 1964" and "Civil Rights Act of 1991" are different
    laws. We only fold case, whitespace, a leading article, a parenthetical
    acronym, surrounding quotes, and trailing punctuation."""
    t = (term or "").strip().lower()
    t = re.sub(r"^[\"'`]+|[\"'`]+$", "", t)            # surrounding quotes
    t = re.sub(r"\s*\([^)]*\)\s*$", "", t)             # trailing "(DPA)" acronym
    t = re.sub(r"^the\s+", "", t)                      # leading article
    t = re.sub(r"\s+", " ", t)                         # collapse whitespace
    t = t.rstrip(".,;: ")                               # trailing punctuation
    return t.strip()


def _cache_key(term: str) -> str:
    return _REF_CACHE_PREFIX + hashlib.sha1(_normalize_term(term).encode()).hexdigest()


def resolve_references(terms, client) -> dict:
    """Return {term: definition_text} for as many terms as possible.

    Terms already in the disk cache come back free. For uncached terms,
    issues exactly ONE Haiku web search call covering the entire missing
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
        definitions = _batch_resolve(missing, client)
        for term, body in definitions.items():
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
            "resolver_calls": 1 if missing else 0,
        },
        output_data={"resolved": len(resolved)},
    )
    return resolved


def _batch_resolve(terms: list, client) -> dict:
    """One batched Haiku web-search call for every uncached term. Returns
    {term: "summary + Source: url"} on success, {} on any failure.

    These are 2-3 sentence factual definitions with one citation — Haiku
    handles them well at ~3x lower input cost than Sonnet, and web search
    still supplies the authoritative source URL. Robust JSON extraction and
    the graceful empty-result fallback below absorb Haiku's occasional
    formatting slips."""
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
            model="claude-haiku-4-5-20251001",
            max_tokens=2000,
            # Cap fan-out: one batched call resolves up to REF_HARD_LIMIT (5)
            # terms, so a handful of searches suffices. Without a cap a cold
            # call can run many billed searches (fee + tokens per search).
            tools=[{"type": "web_search_20260209", "name": "web_search", "max_uses": 5}],
            messages=[{"role": "user", "content": prompt}],
            # Safety cap: successful web searches finish in ~75s, but a runaway
            # can otherwise hold the request (and the open /bill stream) for
            # minutes. On timeout this raises and we fall through to no
            # Background — strictly no worse than an empty result.
            timeout=100.0,
        )
    except Exception as e:
        print(f"[REF RESOLVER] resolver error: {e}")
        return {}

    raw = "".join(
        getattr(b, "text", "") for b in msg.content if getattr(b, "type", "") == "text"
    ).strip()
    # The model (esp. with web search) frequently prefaces the JSON with prose
    # ("Now I have enough information…") and/or wraps it in a ```json fence.
    # Strip a fence, then fall back to extracting the outermost {...} object;
    # strict=False tolerates literal newlines inside string values.
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z]*\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw).strip()
    if not raw.startswith("{"):
        start, end = raw.find("{"), raw.rfind("}")
        if start != -1 and end > start:
            raw = raw[start:end + 1]

    try:
        parsed = json.loads(raw, strict=False)
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
