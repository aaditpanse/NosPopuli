import json
from agents.documentor_agent import log_action

# Per-state validator floor. Default is 5 (matches federal); thin-metadata
# states drop to 4 so we don't return empty too aggressively. Tuned for
# LegiScan's metadata; revisit as each state loads from Open States.
_DEFAULT_STATE_VALIDATOR_FLOOR = 5
STATE_VALIDATOR_FLOOR = {
    # Thin metadata / small legislatures
    "WY": 4, "SD": 4, "ND": 4, "VT": 4, "NH": 4, "AK": 4, "DE": 4,
    "MT": 4, "RI": 4, "ME": 4, "ID": 4, "NE": 4, "HI": 4,
}


def get_state_validator_floor(state_code: str) -> int:
    return STATE_VALIDATOR_FLOOR.get((state_code or "").upper(), _DEFAULT_STATE_VALIDATOR_FLOOR)

# ---------------------------------------------------------------- Laya
#
# The relevance check at zero cost a search: Laya (Convai Innovations,
# Apache 2.0), a 421M encoder that answers typed questions with calibrated
# probabilities, on this server's CPU. Each candidate is one state ("Search:
# ... Bill: <title>") rated on four levels, all in one batch. On the Tier 2
# federal tuning rows (2026-09-29): nDCG@10 0.65 and recall@10 0.42, against
# 0.69 / 0.48 for the Haiku check and 0.64 / 0.37 for title words alone;
# titles beat titles with summaries (0.63). 30 bills take about 3 s on 8
# threads. The weights are pinned and loaded from the local copy only.
LAYA_REVISION = "55cf4c4ebb4ebe31b2550e8bdf3bd21b99753851"
_LAYA_QUESTION = {"rel": {"type": "score", "instructions": "How well does the bill answer the search?",
                          "criteria": ["unrelated", "mentions it", "partly about it", "directly about it"]}}
# Below "mentions it" a bill is dropped, so a search for nothing relevant
# still ends honestly empty, as the Haiku floor made it.
LAYA_FLOOR = 1.0
_laya = None
_laya_error = None      # why Laya would not load; kept, so a search does not retry it until restart


def _load_laya():
    """Laya from MODEL_DIR/laya@<revision> (fetched once, as setup), never
    from the network at query time. Imported lazily: torch and the weights
    cost seconds and about 2 GB, and only a search needs them."""
    global _laya, _laya_error
    if _laya_error:
        raise RuntimeError(_laya_error)
    if _laya is None:
        import os
        os.environ.setdefault("HF_HUB_OFFLINE", "1")
        try:
            import laya
            import torch
        except ImportError as e:
            _laya_error = f"ImportError: {e}"
            raise
        from search.bill_index import MODEL_DIR
        torch.set_num_threads(int(os.environ.get("NOSPOPULI_LAYA_THREADS") or 8))
        try:
            _laya = laya.load(str(MODEL_DIR / f"laya@{LAYA_REVISION}"), device="cpu")
        except Exception as e:
            _laya_error = f"{type(e).__name__}: {e}"[:300]
            raise
    return _laya


def laya_states(query, results):
    """The Laya input for each result: the search and the bill's title. Pure."""
    return [f"Search: {query}\nBill: {r.get('title') or r.get('identifier') or ''}" for r in results]


def laya_order(results, scores, floor=LAYA_FLOOR):
    """Results at or above the floor, best score first, ties in their search
    order; each carries `_laya` (0-3). Pure."""
    kept = [(s, i, r) for i, (r, s) in enumerate(zip(results, scores)) if s >= floor]
    kept.sort(key=lambda t: (-t[0], t[1]))
    return [{**r, "_laya": round(s, 3)} for s, _, r in kept]


def rank_relevant(query, results, client_fn, min_score=5):
    """The relevance check: Laya when it loads, else the Haiku check (logged,
    fail-open like it). `client_fn` builds the Anthropic client only when
    Haiku is needed."""
    if not results:
        return results
    try:
        agent = _load_laya()
        scores = [o["answers"]["rel"]["score"] for o in agent.predict_batch(laya_states(query, results), _LAYA_QUESTION)]
        return laya_order(results, scores)
    except Exception as e:                                   # noqa: BLE001 - the fallback is the point
        print(f"[VALIDATOR] Laya unavailable ({type(e).__name__}: {e}); the Haiku check instead")
    if len(results) > 20:
        return validate_results_batch(query, results, client_fn(), 4)
    return validate_results(query, results, client_fn(), min_score, True)


def validate_results_batch(query, results, client, min_score=5, batch_size=20):
    """
    Like validate_results but handles large result sets by batching.
    Returns results sorted by score descending with junk filtered out.
    Fail-open: if a batch errors, those results pass through unscored.
    """
    if not results:
        return results

    # The batches are independent calls: run together, a 30-result check
    # takes one Haiku round trip instead of two (federal search waited
    # 10-16 s on them in series, 2026-09-29). Order is the batches' order.
    from concurrent.futures import ThreadPoolExecutor
    batches = [results[start:start + batch_size] for start in range(0, len(results), batch_size)]
    with ThreadPoolExecutor(max_workers=len(batches)) as pool:
        done = list(pool.map(lambda b: validate_results(query, b, client, min_score=min_score, fail_open=True),
                             batches))
    scored_all = []
    for validated in done:
        # validate_results already sorts by score — preserve that order
        for r in validated:
            if r not in scored_all:
                scored_all.append(r)
    return scored_all


def validate_results(query, results, client, min_score=5, fail_open=True):
    """
    Scores each result for relevance to the query.
    Returns filtered list with obviously wrong results removed.

    min_score: keep results scoring >= this (0-10). Use 7 for state bill searches.
    fail_open: only affects the *exception* path (LLM/JSON error) — if True, return
    original unscored results so the user sees something. When the LLM scores
    successfully but no result clears min_score, this returns [] regardless;
    that's a real "no relevant match" verdict, not a transport failure.
    """
    print(f"[VALIDATOR] Running on {len(results)} results for: {query}")
    if not results:
        return results

    entries = []
    for i, r in enumerate(results):
        entry = {
            "index": i,
            "id": r.get("identifier") or f"{(r.get('type') or '').upper()}{r.get('number', '')}",
            "title": r.get("title", ""),
        }
        if r.get("abstract"):
            entry["abstract"] = r["abstract"][:250]
        if r.get("subjects"):
            entry["subjects"] = r["subjects"][:6]
        if r.get("latest_action"):
            entry["latest_action"] = r["latest_action"][:120]
        entries.append(entry)

    prompt = f"""
You are a search result validator for a legislative search engine.
The user searched for: "{query}"

Rate each result's relevance. Return ONLY valid JSON, no markdown.

Results:
{json.dumps(entries, indent=2)}

For each result, return a relevance score 0-10.
- 8-10: Directly relevant — bill is primarily about the searched topic
- 5-7: Somewhat related — bill touches the topic but isn't primarily about it
- 0-4: Not relevant — wrong topic, only mentions the term in passing, or unrelated

Return ONLY this JSON:
{{
    "scores": [
        {{"index": 0, "score": 8, "reason": "brief reason"}},
        ...
    ]
}}
"""

    try:
        message = client.messages.create(
            model="claude-haiku-4-5-20251001",
            # About 45 tokens a scored result with its reason. A flat 800 cut
            # the JSON off at 15 or more results, and the parse error then
            # failed open: every result kept, unchecked (found 2026-09-29).
            max_tokens=200 + 60 * len(entries),
            temperature=0,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = message.content[0].text.strip().replace("```json", "").replace("```", "")
        scored = json.loads(raw)

        kept = [(s["index"], s["score"]) for s in scored["scores"] if s["score"] >= min_score]
        kept.sort(key=lambda x: x[1], reverse=True)  # highest relevance first
        filtered = [results[i] for i, _ in kept]

        log_action(
            agent_name="result_validator",
            action="validate_results",
            input_data={"query": query, "result_count": len(results), "min_score": min_score},
            output_data={
                "kept": len(filtered),
                "dropped": len(results) - len(filtered),
                "top_score": kept[0][1] if kept else None,
            }
        )

        # When the LLM scored successfully but nothing clears the floor,
        # that's a real "no relevant match" verdict — return empty rather
        # than poisoning the UI with low-confidence results.
        return filtered

    except Exception as e:
        print(f"[VALIDATOR] Error: {e}")
        return results if fail_open else []  # transport failure — fail open by default