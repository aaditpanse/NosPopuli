"""Upcoming-meeting finder: when is each jurisdiction's NEXT meeting?

    python upcoming.py [slug-or-source_id ...]   # default: every store source

Same recipe for every source, no jurisdiction knowledge here: start from the
URLs the discovery profile already verified (or the platform API endpoint for
the two curated pre-discovery sources), follow one hop of schedule-looking
links, and let a cheap Haiku call pull out the dates. Results are advisory
display metadata (machine-derived, never certified) written to
data/store/upcoming.json for the console header.
"""

import argparse
import datetime
import html as html_mod
import json
import pathlib
import re
import sys
import urllib.parse
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).parent))

import anthropic

FOUNDRY = pathlib.Path(__file__).parent
STORE = FOUNDRY / "data" / "store"
OUT = STORE / "upcoming.json"
MODEL = "claude-haiku-4-5"
PRICES = {"claude-haiku-4-5": (1.0, 5.0)}

# The two milestone sources predate the discovery agent, so no profile file
# exists to read schedule URLs from; their platform calendars are queried
# directly. {today} is substituted at fetch time.
CURATED_URLS = {
    "pittsburgh-legistar": ["https://webapi.legistar.com/v1/pittsburgh/events"
                            "?$filter=EventDate+ge+datetime'{today}'"
                            "&$orderby=EventDate&$top=12"],
    "la-primegov": ["https://lacity.primegov.com/api/v2/PublicPortal/"
                    "ListUpcomingMeetings"],
}

SCHEDULE_LINK_RE = re.compile(
    r'href="([^"]+)"[^>]*>([^<]{0,120})', re.I)
SCHEDULE_HINT_RE = re.compile(
    r"schedule|calendar|upcoming|20\d\d[ -]?(board|meetings)|meetings?[ -]?20\d\d", re.I)

SCHEMA = {
    "type": "object",
    "properties": {
        "upcoming": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "date": {"type": "string", "pattern": r"^\d{4}-\d{2}-\d{2}$"},
                    "time": {"type": ["string", "null"]},
                    "body": {"type": "string"},
                },
                "required": ["date", "time", "body"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["upcoming"],
    "additionalProperties": False,
}


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "nospopuli-foundry-lab"})
    with urllib.request.urlopen(req, timeout=25) as resp:
        return resp.read().decode("utf-8", "replace")


def strip_html(body):
    body = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", body, flags=re.S | re.I)
    body = re.sub(r"<[^>]+>", " ", body)
    return re.sub(r"\s+", " ", html_mod.unescape(body))


def profile_urls(profile):
    """Every concrete http(s) URL anywhere in the profile, in document order.
    URLs may carry a {today} placeholder (substituted at fetch time), so
    profiles can point at date-filtered schedule/API queries."""
    urls, seen = [], set()
    today = datetime.date.today().isoformat()
    for m in re.finditer(r'https?://[^\s"\\]+', json.dumps(profile)):
        url = m.group().rstrip(".,)").rstrip("/").replace("{today}", today)
        if url not in seen:
            seen.add(url)
            urls.append(url)
    return urls[:6]


def slim_json(text, limit=9000):
    """Compact a verbose JSON API payload to schedule-relevant fields —
    platform event objects run to ~100 keys, and at full size only a couple
    of events fit the excerpt budget."""
    try:
        data = json.loads(text)
    except Exception:
        return None
    keep = re.compile(r"(?i)name|date|time|body|categ|title|locat|type|status")

    def slim(o):
        if isinstance(o, dict):
            out = {}
            for k, v in o.items():
                if isinstance(v, (dict, list)):  # keep containers ("value", …)
                    s = slim(v)
                    if s:
                        out[k] = s
                elif keep.search(k) and isinstance(v, (str, int, float)):
                    out[k] = v
            return out
        if isinstance(o, list):
            return [s for s in (slim(x) for x in o[:40]) if s]
        return o
    return json.dumps(slim(data))[:limit]


def tracked_body(source_id):
    """The body this store actually tracks (most common meeting body), so a
    shared county calendar doesn't promote another commission's meeting."""
    path = STORE / f"{source_id}.json"
    if not path.exists():
        return None
    meetings = json.loads(path.read_text()).get("meetings", {})
    names = {}
    for m in meetings.values():
        b = m.get("body")
        if b:
            names[b] = names.get(b, 0) + 1
    return max(names, key=names.get) if names else None


def schedule_hop(base_url, body):
    """Links on a fetched page that look like a meeting schedule/calendar."""
    hops = []
    for href, text in SCHEDULE_LINK_RE.findall(body):
        if SCHEDULE_HINT_RE.search(href) or SCHEDULE_HINT_RE.search(text):
            hops.append(urllib.parse.urljoin(base_url, html_mod.unescape(href)))
    return list(dict.fromkeys(hops))[:3]


def gather_text(source_id, log):
    today = datetime.date.today().isoformat()
    if source_id in CURATED_URLS:
        urls = [u.replace("{today}", today) for u in CURATED_URLS[source_id]]
    else:
        slug = source_id.rsplit("-", 1)[0]
        profile_path = FOUNDRY / "data" / "discovery" / f"{slug}_profile.json"
        if not profile_path.exists():
            log(f"  no discovery profile for {source_id}, skipping")
            return None
        urls = profile_urls(json.loads(profile_path.read_text()))

    chunks, hops = [], []
    for url in urls:
        if url.lower().endswith((".pdf", ".ashx")):
            continue
        try:
            body = fetch(url)
        except Exception as exc:
            log(f"  fetch failed {url[:70]}: {str(exc)[:60]}")
            continue
        if "<html" in body[:2000].lower():
            hops += schedule_hop(url, body)
            body = strip_html(body)
        else:
            body = slim_json(body) or body
        chunks.append(f"[from {url}]\n{body[:9000]}")
    for url in [h for h in hops if h not in urls][:3]:
        try:
            body = fetch(url)
        except Exception as exc:
            log(f"  hop failed {url[:70]}: {str(exc)[:60]}")
            continue
        chunks.append(f"[from {url}]\n{strip_html(body)[:9000]}")
    return "\n\n".join(chunks)[:36000] if chunks else None


def find_upcoming(client, source_id, log=print):
    text = gather_text(source_id, log)
    if not text:
        return None, 0.0
    today = datetime.date.today().isoformat()
    body_name = tracked_body(source_id)
    body_hint = (f" This source tracks the body '{body_name}' — list every "
                 f"stated future '{body_name}' meeting before any other "
                 "body's." if body_name else "")
    response = client.messages.create(
        model=MODEL, max_tokens=1200,
        output_config={"format": {"type": "json_schema", "schema": SCHEMA}},
        messages=[{"role": "user", "content":
                   f"Today is {today}. The text below comes from the official "
                   f"meeting-schedule pages of the local government behind the "
                   f"data source `{source_id}`. List the governing-body meetings "
                   "scheduled STRICTLY AFTER today, soonest first, at most 5."
                   + body_hint +
                   " Only meetings with an explicitly stated future date — never "
                   "guess or extrapolate a pattern; an empty list is the correct "
                   "answer when none are stated.\n\n" + text}])
    upcoming = json.loads(next(b.text for b in response.content
                               if b.type == "text"))["upcoming"]
    upcoming = sorted((u for u in upcoming if u["date"] > today),
                      key=lambda u: u["date"])[:5]
    for u in upcoming:  # "17:00:00Z" -> "17:00" (platforms mislabel local as UTC)
        if u.get("time"):
            u["time"] = re.sub(r"^(\d{1,2}:\d{2})(:\d{2})?Z?$", r"\1", u["time"])
    # "next" prefers the tracked body: on shared county calendars the soonest
    # meeting is often another commission's.
    nxt = upcoming[0] if upcoming else None
    if body_name:
        toks = {t for t in re.findall(r"[a-z]+", body_name.lower()) if len(t) > 3}
        for u in upcoming:
            uts = set(re.findall(r"[a-z]+", u["body"].lower()))
            if toks and len(toks & uts) * 2 >= len(toks):
                nxt = u
                break
    cost = (response.usage.input_tokens * PRICES[MODEL][0]
            + response.usage.output_tokens * PRICES[MODEL][1]) / 1e6
    return {"upcoming": upcoming, "next": nxt,
            "checked": today, "derived_by": MODEL,
            "note": "machine-derived schedule lookup; advisory, never certified"}, cost


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("sources", nargs="*",
                        help="source ids (default: every store source)")
    args = parser.parse_args()
    sources = args.sources or sorted(
        p.stem for p in STORE.glob("*.json")
        if p.stem not in ("upcoming", "item-summaries")
        and "item-facts" not in p.stem)
    client = anthropic.Anthropic()
    out = json.loads(OUT.read_text()) if OUT.exists() else {}
    total = 0.0
    for source_id in sources:
        print(f"{source_id}:")
        entry, cost = find_upcoming(client, source_id, log=print)
        total += cost
        if entry is None:
            continue
        out[source_id] = entry
        nxt = entry["next"]
        print(f"  next: {nxt['date']} {nxt.get('time') or ''} {nxt['body']}"
              if nxt else "  no future meetings stated on the schedule pages")
    OUT.write_text(json.dumps(out, indent=1))
    if total:
        import budget
        budget.record("enrichment", total)
    print(f"done -> {OUT.name} (${total:.3f})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
