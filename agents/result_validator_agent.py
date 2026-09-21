import json
from agents.documentor_agent import log_action

def validate_results_batch(query, results, client, min_score=5, batch_size=20):
    """
    Like validate_results but handles large result sets by batching.
    Returns results sorted by score descending with junk filtered out.
    Fail-open: if a batch errors, those results pass through unscored.
    """
    if not results:
        return results

    scored_all = []
    for start in range(0, len(results), batch_size):
        batch = results[start:start + batch_size]
        validated = validate_results(query, batch, client, min_score=min_score, fail_open=True)
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
            max_tokens=800,
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