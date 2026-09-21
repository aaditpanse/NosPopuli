import anthropic
import os
import json
from datetime import datetime, timedelta
from collections import Counter
from dotenv import load_dotenv
from search.search_logger import get_log
from agents.documentor_agent import log_action

load_dotenv()

def analyze(client, days=7):
    """
    Reads search and agent logs.
    Generates a plain English report of patterns, issues, recommendations.
    """
    search_log = get_log()
    
    if not search_log:
        return {"report": "No search data yet.", "stats": {}}
    
    # Filter to last N days
    cutoff = datetime.now() - timedelta(days=days)
    recent = [
        e for e in search_log
        if datetime.fromisoformat(e["timestamp"]) > cutoff
    ]
    
    if not recent:
        return {"report": "No recent search data.", "stats": {}}
    
    # ── Raw stats ──
    searches = [e for e in recent if e["event"] == "search"]
    bill_opens = [e for e in recent if e["event"] == "bill_opened"]
    member_opens = [e for e in recent if e["event"] == "member_opened"]
    
    # Most searched queries
    query_counts = Counter(e["query"].lower() for e in searches)
    top_queries = query_counts.most_common(10)
    
    # Zero result searches
    zero_results = [
        e["query"] for e in searches
        if e.get("results_count", 0) == 0
    ]
    
    # Most opened bills
    bill_counts = Counter(
        f"{e['bill_id']} — {e.get('title','')[:50]}"
        for e in bill_opens
        if e.get("bill_id")
    )
    top_bills = bill_counts.most_common(5)
    
    # Query type breakdown
    type_counts = Counter(e.get("query_type", "unknown") for e in searches)
    
    # Search volume by day
    daily = Counter(
        e["timestamp"][:10] for e in searches
    )
    
    stats = {
        "total_searches": len(searches),
        "total_bill_opens": len(bill_opens),
        "total_member_opens": len(member_opens),
        "zero_result_searches": zero_results,
        "top_queries": top_queries,
        "top_bills": top_bills,
        "query_type_breakdown": dict(type_counts),
        "daily_volume": dict(daily),
        "period_days": days,
    }
    
    # ── AI report ──
    prompt = f"""
    You are an analyst for NosPopuli, a civic legislation platform.
    Analyze this usage data and write a concise plain English report.
    
    Data from last {days} days:
    - Total searches: {len(searches)}
    - Bill detail pages opened: {len(bill_opens)}
    - Member profiles opened: {len(member_opens)}
    
    Top searched queries:
    {json.dumps(top_queries, indent=2)}
    
    Searches with zero results (needs fixing):
    {json.dumps(zero_results[:10], indent=2)}
    
    Most opened bills:
    {json.dumps(top_bills, indent=2)}
    
    Query type breakdown:
    {json.dumps(dict(type_counts), indent=2)}
    
    Write a short report with these sections:
    1. Summary (2-3 sentences on overall usage)
    2. What users are looking for (top topics and patterns)
    3. Search quality issues (zero result queries, likely misclassifications)
    4. Recommendations (2-3 specific actionable improvements)
    
    Be direct and specific. No fluff.
    """
    
    message = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=800,
        messages=[{"role": "user", "content": prompt}]
    )
    
    report = message.content[0].text
    
    log_action(
        agent_name="analyst",
        action="analyze",
        input_data={"days": days, "searches_analyzed": len(searches)},
        output_data={"report_length": len(report)}
    )
    
    return {"report": report, "stats": stats}

if __name__ == "__main__":
    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    result = analyze(client)
    print("ANALYST REPORT")
    print("=" * 50)
    print(result["report"])
    print()
    print("RAW STATS:")
    print(json.dumps(result["stats"], indent=2))