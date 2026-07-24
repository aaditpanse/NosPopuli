"""Extractor synthesis via LLM (spec module 3) — offline, gated, never in
the hot path.

The model sees three things: the source profile (real sample responses from
the frozen HTTP cache), the domain schema source, and the artifact contract.
It does NOT see extractor v1 — the point of M1 is measuring synthesis from
the source, not paraphrase of existing code. Gate feedback (tracebacks,
harness findings, golden diffs) is appended as follow-up turns, so the
attempt loop is one conversation.
"""

import json
import os
import pathlib
import re
import types

import anthropic
from dotenv import load_dotenv

load_dotenv(pathlib.Path(__file__).parent.parent / ".env")

# The synthesis model is swappable so cheaper challengers (Kimi K3, DeepSeek)
# can be A/B'd against Opus with the gate as the judge — the gate makes model
# quality an objective pass/fail, so this is the safest slot to shop around.
# Anything not named claude-* goes through an OpenAI-compatible /chat/completions
# endpoint (FOUNDRY_SYNTH_BASE_URL + FOUNDRY_SYNTH_API_KEY). Default behavior
# with no env set is byte-identical to before: Opus 4.8 via the Anthropic SDK.
MODEL = os.environ.get("FOUNDRY_SYNTH_MODEL", "claude-opus-4-8")

# $/MTok: input, cache write (1.25x), cache read (0.1x), output. Unknown models
# fall back to Opus prices — a conservative OVERestimate so the budget governor
# can only err on the safe side. Override: FOUNDRY_SYNTH_PRICES="in,out".
_PRICE_TABLE = {
    "claude-opus-4-8": {"input_tokens": 5.00, "cache_creation_input_tokens": 6.25,
                        "cache_read_input_tokens": 0.50, "output_tokens": 25.00},
    "kimi-k3": {"input_tokens": 3.00, "output_tokens": 15.00},
}


def _prices():
    override = os.environ.get("FOUNDRY_SYNTH_PRICES")
    if override:
        inp, outp = (float(x) for x in override.split(","))
        return {"input_tokens": inp, "output_tokens": outp}
    return _PRICE_TABLE.get(MODEL) or _PRICE_TABLE["claude-opus-4-8"]


PRICES = _prices()

SYSTEM = """You write deterministic data-extractor code for a municipal-data \
pipeline system. You are given a source profile (sample API responses), a \
target domain schema, and an artifact contract. Reply with ONE complete \
Python module in a single ```python code block and nothing else outside it. \
The module must be plain deterministic code: stdlib only, no network or file \
I/O of its own — all HTTP goes through the fetch_json callable it receives."""

CONTRACT = """## Artifact contract

Write a complete Python module implementing an extractor for source
`pittsburgh-legistar` (Pittsburgh City Council on the Legistar Web API).

- Define `EXTRACTOR_VERSION = "2"`.
- Define `extract(fetch_json, event_ids) -> (records, run_meta)`.
- `fetch_json(path, params=None)` is injected by the runtime. It GETs
  `https://webapi.legistar.com/v1/pittsburgh{path}` and returns parsed JSON.
  It is the only I/O available to you.
- `records` is {"meetings": [...], "agenda_items": [...], "vote_events": [...],
  "members": [...]} in the domain schema below — one meeting record per given
  Legistar event id, plus that meeting's agenda items and recorded votes.
- `run_meta` is {"source_id": "pittsburgh-legistar", "extractor_version": ...,
  "schema_version": "1", "event_ids": [...], "row_counts": {type: count}}.

Target-schema conventions for this source:
- meeting_id = f"pittsburgh-legistar-{EventId}"
  item_id    = f"pittsburgh-legistar-item-{EventItemId}"
  vote_id    = f"pittsburgh-legistar-vote-{EventItemId}"
- Only agenda items that have a file number (a Legistar "matter file") become
  agenda_item records; procedural rows (roll call, section headings) do not.
- meeting `attendance` comes from the meeting's roll call.
- Vote and roll-call position names must be normalized into the schema's
  POSITIONS vocabulary.
- `members` holds one record per distinct person seen across the run
  (roll calls and votes), sorted by name, carrying the Legistar person id.
- A vote_event exists only where the source records an actual per-member vote."""


def _sample(cache, key, shrink=None):
    data = json.loads(json.dumps(cache[key]))
    if shrink:
        data = shrink(data)
    return json.dumps(data, indent=1)


def build_source_profile(cache, event_ids):
    """Real sample responses from the frozen cache — the model works from
    the source's actual shapes, not a description of them."""
    eid = event_ids[0]
    items_key = f"/events/{eid}/eventitems"
    items = cache[items_key]
    # a representative spread: procedural rows, referred items, final actions
    def item_spread(data):
        with_file = [i for i in data if i.get("EventItemMatterFile")]
        without = [i for i in data if not i.get("EventItemMatterFile")]
        passed = [i for i in with_file if i.get("EventItemPassedFlagName")]
        referred = [i for i in with_file if not i.get("EventItemPassedFlagName")]
        return without[:2] + referred[:1] + passed[:1]

    rollcall_item = next(i for i in items if i.get("EventItemRollCallFlag"))
    voted_item = next(i for i in items
                      if i.get("EventItemMatterFile") and i.get("EventItemPassedFlagName")
                      and f"/eventitems/{i['EventItemId']}/votes" in cache
                      and cache[f"/eventitems/{i['EventItemId']}/votes"])

    return f"""## Source profile

Base API: https://webapi.legistar.com/v1/pittsburgh (open, no key).
Endpoints discovered, with real sample responses:

`GET /events/{{event_id}}` — one meeting:
```json
{_sample(cache, f"/events/{eid}", lambda d: {k: v for k, v in d.items() if k != "EventItems"})}
```

`GET /events/{{event_id}}/eventitems` — agenda rows in meeting order
(sample of 4 rows out of {len(items)}; note which have a matter file and
which have a passed flag):
```json
{_sample(cache, items_key, item_spread)}
```

`GET /eventitems/{{event_item_id}}/rollcalls` — attendance rows for a
roll-call item (EventItemRollCallFlag == 1); sample of 3:
```json
{_sample(cache, f"/eventitems/{rollcall_item['EventItemId']}/rollcalls", lambda d: d[:3])}
```

`GET /eventitems/{{event_item_id}}/votes` — per-member recorded vote rows
for an acted-on item; empty list where no recorded vote exists; sample of 3:
```json
{_sample(cache, f"/eventitems/{voted_item['EventItemId']}/votes", lambda d: d[:3])}
```"""


def build_initial_messages(cache, event_ids):
    schema_src = (pathlib.Path(__file__).parent / "schema.py").read_text()
    prompt = (f"{build_source_profile(cache, event_ids)}\n\n"
              f"## Domain schema (schema.py, verbatim)\n\n```python\n{schema_src}```\n\n"
              f"{CONTRACT}")
    return [{"role": "user", "content": prompt}]


def build_repair_samples(cache, event_ids):
    """Fresh samples from a (possibly changed) source. Selection is by URL
    pattern and position only — never by field name, since field renames are
    exactly the breakage being sampled."""
    eid = event_ids[0]
    items = cache[f"/events/{eid}/eventitems"]
    rollcalls_key = next(k for k in sorted(cache) if "/rollcalls" in k and cache[k])
    votes_key = next(k for k in sorted(cache) if "/votes" in k and cache[k])
    return (f"`GET /events/{eid}`:\n```json\n"
            f"{json.dumps(cache[f'/events/{eid}'], indent=1)}\n```\n\n"
            f"`GET /events/{eid}/eventitems` (first 6 of {len(items)} rows):\n"
            f"```json\n{json.dumps(items[:6], indent=1)}\n```\n\n"
            f"`GET {rollcalls_key}` (first 3 rows):\n"
            f"```json\n{json.dumps(cache[rollcalls_key][:3], indent=1)}\n```\n\n"
            f"`GET {votes_key}` (first 3 rows):\n"
            f"```json\n{json.dumps(cache[votes_key][:3], indent=1)}\n```")


def build_repair_messages(old_code, cache, event_ids, error=None,
                          findings=None, diff_lines=None):
    """Repair loop kickoff (spec module 6): old extractor + new source
    samples + failing evidence -> replacement artifact."""
    evidence = feedback_message(error, findings, diff_lines)["content"]
    prompt = f"""The upstream source for extractor `pittsburgh-legistar` changed \
its response format, and the currently deployed extractor below now fails \
validation. Repair it.

## Current extractor
```python
{old_code}
```

## Fresh sample responses from the changed source

{build_repair_samples(cache, event_ids)}

## Failing evidence from the validation harness

{evidence}

## Requirements

Same artifact contract as the current extractor: `extract(fetch_json, \
event_ids) -> (records, run_meta)`, stdlib only, domain schema v1 unchanged \
(the schema did not change — only the source did). Bump EXTRACTOR_VERSION. \
The repaired extractor must reproduce the same records from the changed \
source. Return the complete module in one ```python block."""
    return [{"role": "user", "content": prompt}]


def feedback_message(error=None, findings=None, diff_lines=None, fetch_trace=None):
    parts = ["Your extractor failed the gate. Return the complete corrected "
             "module in one ```python block."]
    if error:
        parts.append(f"## Execution error\n```\n{error[-3000:]}\n```")
    if findings:
        parts.append("## Validation-harness findings\n" + "\n".join(
            f"- [{f['layer']}/{f['check']}] {f['ref']}: {f['msg'][:200]}"
            for f in findings[:10]))
    if fetch_trace:
        parts.append("## Fetch trace from your run (url · response chars · "
                     "response head)\nDiagnose from what you ACTUALLY got "
                     "back, not what you expected:\n" + "\n".join(fetch_trace))
    if diff_lines:
        parts.append("## Differences vs the hand-verified golden set\n" +
                     "\n".join(diff_lines[:30]))
    return {"role": "user", "content": "\n\n".join(parts)}


def generate(messages):
    """One synthesis call. Returns (code, assistant_content, usage)."""
    import budget
    budget.check("synthesis")
    if not MODEL.startswith("claude"):
        return _generate_compat(messages)
    client = anthropic.Anthropic()
    with client.messages.stream(
        model=MODEL,
        max_tokens=64000,  # adaptive thinking shares this budget with the code
        thinking={"type": "adaptive"},
        system=SYSTEM,
        cache_control={"type": "ephemeral"},  # profile+schema prefix reused across attempts
        messages=messages,
    ) as stream:
        msg = stream.get_final_message()
    text = "".join(b.text for b in msg.content if b.type == "text")
    blocks = re.findall(r"```python\n(.*?)```", text, re.DOTALL)
    if not blocks and msg.stop_reason != "max_tokens":
        # the model sometimes omits the closing fence on its final block;
        # only trust an unterminated block when the turn ended naturally
        blocks = re.findall(r"```python\n(.*)\Z", text, re.DOTALL)
    import budget
    budget.record("synthesis", cost_usd([msg.usage]))
    if not blocks:
        raise RuntimeError(f"no complete python code block "
                           f"(stop_reason={msg.stop_reason}): {text[:300]}")
    return max(blocks, key=len), msg.content, msg.usage


def _generate_compat(messages):
    """Synthesis via an OpenAI-compatible /chat/completions endpoint (Kimi K3,
    DeepSeek, any vLLM host). Same contract as the Anthropic path; the returned
    assistant_content is a plain string, which both this path and the Anthropic
    API accept when the attempt loop replays it as an assistant turn."""
    import requests

    base = os.environ.get("FOUNDRY_SYNTH_BASE_URL",
                          "https://api.moonshot.ai/v1").rstrip("/")
    key = os.environ.get("FOUNDRY_SYNTH_API_KEY")
    if not key:
        raise RuntimeError(
            f"FOUNDRY_SYNTH_MODEL={MODEL} needs FOUNDRY_SYNTH_API_KEY in .env")
    max_tokens = int(os.environ.get("FOUNDRY_SYNTH_MAX_TOKENS", "32768"))

    def _flatten(content):
        # A prior Anthropic-path turn stores content blocks; a compat run only
        # ever sees its own strings, but tolerate both.
        if isinstance(content, str):
            return content
        return "".join(getattr(b, "text", "") or (b.get("text", "") if isinstance(b, dict) else "")
                       for b in content)

    payload = {
        "model": MODEL,
        "max_tokens": max_tokens,
        "messages": [{"role": "system", "content": SYSTEM}] + [
            {"role": m["role"], "content": _flatten(m["content"])} for m in messages],
    }
    resp = requests.post(f"{base}/chat/completions", timeout=900,
                         headers={"Authorization": f"Bearer {key}"}, json=payload)
    resp.raise_for_status()
    data = resp.json()
    choice = data["choices"][0]
    text = choice["message"]["content"] or ""
    u = data.get("usage", {})
    usage = types.SimpleNamespace(input_tokens=u.get("prompt_tokens", 0),
                                  output_tokens=u.get("completion_tokens", 0))
    import budget
    budget.record("synthesis", cost_usd([usage]))
    blocks = re.findall(r"```python\n(.*?)```", text, re.DOTALL)
    if not blocks and choice.get("finish_reason") != "length":
        blocks = re.findall(r"```python\n(.*)\Z", text, re.DOTALL)
    if not blocks:
        raise RuntimeError(f"no complete python code block "
                           f"(finish_reason={choice.get('finish_reason')}): {text[:300]}")
    return max(blocks, key=len), text, usage


def cost_usd(usages):
    total = 0.0
    for u in usages:
        for field, price in PRICES.items():
            total += (getattr(u, field, 0) or 0) * price / 1e6
    return total
