"""Source Discovery agent (spec module 1) — the last unautomated module.

    python discover.py "<seed>" <slug> [--budget 40] [--model claude-sonnet-4-6]

An LLM runs the probe loop a human otherwise runs by hand: given a seed
jurisdiction, it fetches real pages/endpoints through polite cached tools,
walks the platform playbook, and finishes by calling `report_profile` with
the source profile the rest of Foundry consumes. Discovery is navigation,
not frontier reasoning, so it runs on a cheaper model than synthesis.

Rules baked into the prompt: never fight anti-bot (record and pivot),
verify by fetching (never assert from memory), find where votes/minutes
actually live, and identify a second source — the field that decides
whether the jurisdiction can ever be certified.

All fetches land in data/discovery/<slug>_http_cache.json so extractor
synthesis can replay them.
"""

import argparse
import json
import pathlib
import re
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).parent))

import anthropic
from dotenv import load_dotenv

import sandbox2

load_dotenv(pathlib.Path(__file__).parent.parent / ".env")

FOUNDRY = pathlib.Path(__file__).parent
OUT = FOUNDRY / "data" / "discovery"

# $/MTok: input, cache write, cache read, output
PRICES = {
    "claude-sonnet-4-6": (3.00, 3.75, 0.30, 15.00),
    "claude-haiku-4-5": (1.00, 1.25, 0.10, 5.00),
    "claude-opus-4-8": (5.00, 6.25, 0.50, 25.00),
}

SYSTEM = """You are a source-discovery agent for Foundry, a system that \
builds ingestion pipelines for municipal legislative data (meetings, agenda \
items, votes). Given a seed jurisdiction, your job is to find where its \
governing body's records actually live, verified by real fetches — never \
assert an endpoint works without probing it.

Prefer the cheapest extraction rung, in order: real API > hidden JSON \
endpoint > static HTML > rendered document/PDF. Municipal platforms to \
recognize (probe the likely tenants for the jurisdiction):
- Legistar API: https://webapi.legistar.com/v1/{client}/bodies (only a 200 \
JSON list proves a tenant — the "LegistarConnectionString" 500 fires for \
EVERY unknown slug, and *.legistar.com is wildcard DNS, so neither implies \
the tenant exists)
- PrimeGov: https://{tenant}.primegov.com/api/v2/PublicPortal/ListArchivedMeetings?year=YYYY
- eScribe: https://pub-{tenant}.escribemeetings.com (JSON page methods via \
POST, e.g. /MeetingsCalendarView.aspx/GetCalendarMeetings with \
{"calendarStartDate": "...", "calendarEndDate": "..."})
- CivicPlus AgendaCenter: https://{www.site}/AgendaCenter
- Granicus: https://{tenant}.granicus.com/ViewPublisher.php?view_id=N
- Laserfiche WebLink: folder pages like /LFPortalinternet/0/fol/{id}/Row1.aspx
- The jurisdiction's own .gov pages usually link the real systems — follow them.
- You have web_search: when tenant-slug guessing fails, SEARCH for the
  jurisdiction's official website and its meeting/agenda/minutes pages, then
  fetch what you find. Never conclude "no source" without having searched.

Hard rules:
- If a host is behind Cloudflare/a captcha or blocks you, record it and \
pivot. Never attempt to bypass.
- Governments migrate systems; check that a source has CURRENT documents, \
not just an archive, and note the era each system covers.
- The most important field is the SECOND SOURCE: an independently produced \
record (minutes vs action report vs a registry) that could confirm votes. \
Say plainly if you cannot find one.
- Verify one concrete document of each claimed kind (fetch it and quote a \
line proving it contains items/votes).
- Budget is limited; be economical. When you know enough, call \
report_profile. Finishing with a partial-but-honest profile beats running \
out of budget."""

TOOLS = [
    # server-side web search: the agent must be able to FIND a jurisdiction's
    # official site and portals, not just guess tenant slugs. Basic variant:
    # the _20260209 one runs a code-execution container whose id would have
    # to be replayed on every turn of this manual loop.
    {"type": "web_search_20250305", "name": "web_search", "max_uses": 8},
    {"name": "http_get", "description": "GET a URL and return its text "
     "(PDFs converted to text). Response is truncated; use search_body for "
     "targeted digging in large pages.",
     "input_schema": {"type": "object", "properties": {
         "url": {"type": "string"}}, "required": ["url"],
         "additionalProperties": False}},
    {"name": "http_get_json", "description": "GET a URL and parse it as JSON.",
     "input_schema": {"type": "object", "properties": {
         "url": {"type": "string"}}, "required": ["url"],
         "additionalProperties": False}},
    {"name": "http_post_json", "description": "POST a JSON body (for "
     "ASP.NET page-method endpoints etc.) and return the response text.",
     "input_schema": {"type": "object", "properties": {
         "url": {"type": "string"}, "body": {"type": "object"}},
         "required": ["url", "body"], "additionalProperties": False}},
    {"name": "search_body", "description": "Regex-search the full cached "
     "body of a previously fetched URL; returns up to 25 matches with "
     "context. Use this to mine links/ids out of big pages.",
     "input_schema": {"type": "object", "properties": {
         "url": {"type": "string"}, "pattern": {"type": "string"}},
         "required": ["url", "pattern"], "additionalProperties": False}},
    {"name": "report_profile", "description": "Finish discovery by "
     "reporting the source profile. Call exactly once, when done.",
     "input_schema": {"type": "object", "properties": {
         "jurisdiction": {"type": "string"},
         "primary_source": {"type": "object", "description":
             "Where records live: {system, base_urls, rung (api|hidden-json|"
             "html|pdf), how_to_enumerate_meetings, sample_document_urls}"},
         "second_source": {"type": "object", "description":
             "{exists (bool), system, urls, what_it_affirms} — or "
             "exists=false with an explanation"},
         "votes_located": {"type": "string", "description":
             "Where per-item votes actually appear, with a quoted example line"},
         "systems_surveyed": {"type": "array", "items": {"type": "object"},
          "description": "Every system probed: {system, url, status, era_covered}"},
         "access_constraints": {"type": "string"},
         "open_questions": {"type": "array", "items": {"type": "string"}}},
         "required": ["jurisdiction", "primary_source", "second_source",
                      "votes_located", "systems_surveyed"],
         "additionalProperties": False}},
]

EXCERPT = 2800


def _strip(html_text):
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html_text,
                  flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"[ \t]+", " ", text)


def make_tools(rt):
    def http_get(url):
        body = rt.fetch_text(url)
        stripped = _strip(body) if "<html" in body[:2000].lower() else body
        return (f"[{len(body)} chars total; excerpt]\n{stripped[:EXCERPT]}")

    def http_get_json(url):
        return json.dumps(rt.fetch_json(url))[:EXCERPT]

    def http_post_json(url, body):
        import urllib.request
        key = f"POST {url} {json.dumps(body, sort_keys=True)}"
        if key not in rt.cache:
            req = urllib.request.Request(
                url, data=json.dumps(body).encode(), method="POST",
                headers={"User-Agent": "nospopuli-foundry-lab",
                         "Content-Type": "application/json"})
            rt.cache[key] = urllib.request.urlopen(req, timeout=30) \
                .read().decode("utf-8", "replace")
            time.sleep(0.5)
        return rt.cache[key][:EXCERPT]

    def search_body(url, pattern):
        body = None
        for key in (url, f"GET {url}"):
            if key in rt.cache:
                body = rt.cache[key]
        if body is None:
            body = rt.fetch_text(url)
        if not isinstance(body, str):
            body = json.dumps(body)
        hits = []
        for m in re.finditer(pattern, body):
            s = max(0, m.start() - 60)
            hits.append(body[s:m.end() + 60].replace("\n", " "))
            if len(hits) >= 25:
                break
        return "\n---\n".join(hits) or "(no matches)"

    return {"http_get": http_get, "http_get_json": http_get_json,
            "http_post_json": http_post_json, "search_body": search_body}


def cost_usd(usages, model):
    p = PRICES[model]
    fields = ("input_tokens", "cache_creation_input_tokens",
              "cache_read_input_tokens", "output_tokens")
    return sum((getattr(u, f, 0) or 0) * price / 1e6
               for u in usages for f, price in zip(fields, p))


def discover(seed, slug, budget, model, log=print):
    OUT.mkdir(parents=True, exist_ok=True)
    cache_path = OUT / f"{slug}_http_cache.json"
    rt = sandbox2.Runtime(json.loads(cache_path.read_text())
                          if cache_path.exists() else {})
    tools = make_tools(rt)
    client = anthropic.Anthropic()

    messages = [{"role": "user", "content":
                 f"Seed jurisdiction: {seed}. Discover its sources and "
                 f"report the profile. You have a budget of ~{budget} tool calls."}]
    usages, profile, calls = [], None, 0
    nudged = False
    t0 = time.time()

    for turn in range(budget * 2):
        response = client.messages.create(
            model=model, max_tokens=8000, thinking={"type": "adaptive"},
            system=SYSTEM, tools=TOOLS, messages=messages)
        usages.append(response.usage)
        for block in response.content:
            if block.type == "server_tool_use":
                log(f"  [search] {json.dumps(block.input)[:90]}")
        if response.stop_reason == "pause_turn":
            messages.append({"role": "assistant", "content": response.content})
            continue
        if response.stop_reason != "tool_use":
            if profile is None:
                # a max_tokens truncation can leave a dangling tool_use block,
                # which the API rejects on replay — drop unanswerable blocks
                content = [b for b in response.content if b.type != "tool_use"]
                if content:
                    messages.append({"role": "assistant", "content": content})
                messages.append({"role": "user", "content":
                                 "Finish by calling report_profile now."})
                continue
            break

        results = []
        messages.append({"role": "assistant", "content": response.content})
        for block in response.content:
            if block.type != "tool_use":
                continue
            calls += 1
            if calls > budget and block.name != "report_profile":
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                "content": "BUDGET EXHAUSTED. Call "
                                "report_profile immediately with what you know.",
                                "is_error": True})
                continue
            if block.name == "report_profile":
                profile = block.input
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                "content": "profile recorded"})
                continue
            try:
                out = tools[block.name](**block.input)
                log(f"  [{calls}] {block.name} "
                    f"{json.dumps(block.input)[:100]}")
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                "content": out})
            except Exception as e:
                log(f"  [{calls}] {block.name} ERROR {str(e)[:80]}")
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                "content": f"error: {e}", "is_error": True})
        if not nudged and calls >= budget * 0.7:
            nudged = True
            results.append({"type": "text", "text":
                            f"[budget notice: {calls}/{budget} tool calls "
                            "used — verify anything essential still "
                            "unverified, then call report_profile]"})
        messages.append({"role": "user", "content": results})
        cache_path.write_text(json.dumps(rt.cache))
        if profile is not None:
            break

    cache_path.write_text(json.dumps(rt.cache))
    if profile:
        (OUT / f"{slug}_profile.json").write_text(json.dumps(profile, indent=1))
    log(f"discovery metrics ({model}): {calls} tool calls, "
        f"${cost_usd(usages, model):.2f}, {(time.time() - t0) / 60:.1f} min")
    return profile


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("seed")
    parser.add_argument("slug")
    parser.add_argument("--budget", type=int, default=40)
    parser.add_argument("--model", default="claude-sonnet-4-6")
    args = parser.parse_args()
    profile = discover(args.seed, args.slug, args.budget, args.model)
    if profile:
        print(json.dumps(profile, indent=1))
        return 0
    print("discovery did not produce a profile within budget")
    return 1


if __name__ == "__main__":
    sys.exit(main())
