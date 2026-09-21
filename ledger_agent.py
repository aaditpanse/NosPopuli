"""Search-ledger: classify an ask, funnel a result set, English list titles.

Fail-open: if Haiku is down, compacted official titles still render a ledger.
"""
import hashlib
import json
import pathlib
import re

from router_agent import check_known_bills
from search_rank import rank_by_relevance

_FOUNDRY_STORE = pathlib.Path("foundry/data/store")
_STATE_ABBR = {"virginia": "VA", "pennsylvania": "PA", "california": "CA",
               "illinois": "IL", "washington": "WA", "new york": "NY"}

US_STATES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
    "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID",
    "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN", "mississippi": "MS",
    "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY",
    "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK",
    "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
    "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT",
    "vermont": "VT", "virginia": "VA", "washington": "WA", "west virginia": "WV",
    "wisconsin": "WI", "wyoming": "WY", "district of columbia": "DC",
}

STATE_NAMES = {code: name.title() for name, code in US_STATES.items()}
STATE_NAMES["DC"] = "District of Columbia"
STATE_NAMES["NH"] = "New Hampshire"
STATE_NAMES["NJ"] = "New Jersey"
STATE_NAMES["NM"] = "New Mexico"
STATE_NAMES["NY"] = "New York"
STATE_NAMES["NC"] = "North Carolina"
STATE_NAMES["ND"] = "North Dakota"
STATE_NAMES["RI"] = "Rhode Island"
STATE_NAMES["SC"] = "South Carolina"
STATE_NAMES["SD"] = "South Dakota"
STATE_NAMES["WV"] = "West Virginia"

FOUNDRY_PLACES = [
    {"keys": ["stafford"], "name": "Stafford County, Virginia", "slug": "stafford-bos", "state": "VA", "body": "Board of Supervisors"},
    {"keys": ["loudoun"], "name": "Loudoun County, Virginia", "slug": "loudoun-bos", "state": "VA", "body": "Board of Supervisors"},
    {"keys": ["fairfax"], "name": "Fairfax County, Virginia", "slug": "fairfax-bos", "state": "VA", "body": "Board of Supervisors"},
    {"keys": ["prince william", "princewilliam"], "name": "Prince William County, Virginia", "slug": "princewilliam-bos", "state": "VA", "body": "Board of Supervisors"},
    {"keys": ["chicago"], "name": "Chicago, Illinois", "slug": "chicago-bos", "state": "IL", "body": "City Council"},
    {"keys": ["seattle"], "name": "Seattle, Washington", "slug": "seattle-bos", "state": "WA", "body": "City Council"},
    {"keys": ["pittsburgh"], "name": "Pittsburgh, Pennsylvania", "slug": "pittsburgh-legistar", "state": "PA", "body": "City Council"},
    {"keys": ["los angeles", "la city"], "name": "Los Angeles, California", "slug": "la-primegov", "state": "CA", "body": "City Council"},
    # LA County (Board of Supervisors) is a different government from LA City
    # (City Council) — la-primegov is lacity.primegov.com. We cover the city,
    # not the county, and must never answer a county question with city data.
    {"keys": ["los angeles county", "la county"], "name": "Los Angeles County, California",
     "slug": "lacounty-bos", "state": "CA", "body": "Board of Supervisors",
     "sibling": {"slug": "la-primegov", "name": "Los Angeles", "body": "City Council"}},
    {"keys": ["new york city", "nyc"], "name": "New York City, New York", "slug": "newyork-bos", "state": "NY", "body": "City Council"},
]

_BILL_ID_RE = re.compile(
    r"\b(?:h\.?\s*r\.?|hr)\s*\.?\s*(\d+)\b|\b(s)\s*\.?\s*(\d+)\b",
    re.I,
)
_WATCH_RE = re.compile(r"^\s*(watch(?:ing)?|following|my watch list)\s*$", re.I)


def compact_title(raw, limit=140):
    if not raw:
        return ""
    t = str(raw).strip()
    t = re.sub(r"^(a bill to|an act to|to)\s+", "", t, flags=re.I)
    t = re.sub(r"[,;]?\s*(and\s+)?for\s+other\s+purposes\.?\s*$", "", t, flags=re.I)
    t = re.sub(r"[\s.,;:]+$", "", t).strip()
    if t:
        t = t[0].upper() + t[1:]
    if len(t) > limit:
        t = t[:limit].rsplit(" ", 1)[0]
    return t


def extract_state(question):
    q = (question or "").lower()
    for name, code in sorted(US_STATES.items(), key=lambda kv: -len(kv[0])):
        if re.search(r"\b" + re.escape(name) + r"\b", q):
            return code
        if len(name) == 2 and re.search(r"\b" + name + r"\b", q):
            return code
    return None


def parse_bill_id(question):
    q = question or ""
    known = check_known_bills(q)
    if known:
        return {
            "congress": known.get("congress") or 119,
            "bill_type": known["type"],
            "number": int(known["number"]),
        }
    m = _BILL_ID_RE.search(q)
    if not m:
        return None
    if m.group(1):
        return {"congress": 119, "bill_type": "hr", "number": int(m.group(1))}
    if m.group(3):
        return {"congress": 119, "bill_type": "s", "number": int(m.group(3))}
    return None


_LOCAL_GOV_RE = re.compile(
    r"\b(county|city council|board of supervisors|town council|"
    r"city hall|board of aldermen|borough council)\b",
    re.I,
)


def match_foundry_place(question):
    """Longest key wins, on word boundaries.

    Longest-first matters: "los angeles county" must beat "los angeles", or a
    county question silently resolves to the city store. Word boundaries matter
    because keys are short — a bare substring "la" would match "legislation".
    """
    q = (question or "").lower()
    best, best_len = None, 0
    for place in FOUNDRY_PLACES:
        for k in place["keys"]:
            if len(k) > best_len and re.search(r"\b" + re.escape(k) + r"\b", q):
                best, best_len = place, len(k)
    return dict(best) if best else None


_ELECTION_RE = re.compile(
    r"\b(elections?|ballot|polling place|register(ing)? to vote|voter registration|"
    r"midterms?|primar(y|ies)|who('s| is) running|early voting|election day)\b",
    re.I,
)


# Words that can precede "county" without naming one ("for county programs").
_COUNTY_STOPWORDS = {
    "for", "the", "a", "an", "of", "on", "in", "to", "and", "or", "any", "all",
    "my", "our", "your", "this", "that", "these", "those", "per", "by", "with",
    "rural", "urban", "local", "state", "federal", "each", "every", "some",
}
_BODY_RE = re.compile(
    r"\b(city council|board of supervisors|town council|city hall|"
    r"board of aldermen|borough council|county board|county supervisors)\b",
    re.I,
)
_NAMED_COUNTY_RE = re.compile(r"\b([a-z']+)\s+county\b", re.I)


def _looks_local(question):
    """True when the ask is about a local governing body, not Congress."""
    q = (question or "").lower()
    if _BODY_RE.search(q):
        return True
    m = _NAMED_COUNTY_RE.search(q)
    return bool(m and m.group(1) not in _COUNTY_STOPWORDS)


def classify_question(question, state_code=None, allow_graph=True):
    q = (question or "").strip()
    if not q:
        return {"plate": "home"}
    if _WATCH_RE.match(q):
        return {"plate": "watching"}
    # The graph answers three question shapes ("how did X vote on Y", "who
    # voted no on Y", "who held SEAT on DATE") and says "not mine" to the
    # rest, so it goes before the place and topic guesses: "how did Herrity
    # vote on Fairfax zoning" is a traversal, not an uncharted county. The
    # caller falls back here with allow_graph=False when the graph turns
    # out not to know the person or seat, so nothing the ledger answered
    # before is lost.
    if allow_graph:
        import graph
        ask = graph.parse_question(q)
        if ask:
            return {"plate": "graph", "question": q, "ask": ask}
    if _ELECTION_RE.search(q) and not parse_bill_id(q):
        st = (extract_state(q) or state_code or "").upper() or None
        return {
            "plate": "elections",
            "question": q,
            "state_code": st,
            "place_name": STATE_NAMES.get(st) if st else None,
        }
    place = match_foundry_place(q)
    if place:
        return {"plate": "uncharted", "place": place, "state_code": place["state"]}
    bill = parse_bill_id(q)
    if bill:
        return {"plate": "bill", **bill}
    if _looks_local(q):
        # A local-government question we have no source for. Answering it from
        # Congress returns confident nonsense ("LA county" -> federal bills
        # mentioning LA), so say plainly that this layer is missing instead.
        st = (extract_state(q) or state_code or "").upper() or None
        return {
            "plate": "uncharted",
            "place": {"name": None, "body": "Local government", "slug": "", "state": st},
            "state_code": st,
            "question": q,
        }
    st = (state_code or extract_state(q) or "").upper() or None
    return {
        "plate": "ledger",
        "state_code": st,
        "place_name": STATE_NAMES.get(st) if st else None,
        "question": q,
    }


def funnel_stage(latest_action, is_law=False):
    if is_law:
        return "law"
    a = (latest_action or "").lower()
    if any(s in a for s in ("became public law", "enacted", "signed by president", "signed into law", "became law")):
        return "law"
    if any(s in a for s in ("passed house", "passed senate", "agreed to in house", "agreed to in senate")):
        return "passed"
    if any(s in a for s in ("committee", "referred", "reported", "ordered to be reported")):
        return "committee"
    return "introduced"


def build_funnel(results):
    stages = [funnel_stage(r.get("latest_action"), r.get("is_law")) for r in (results or [])]
    n = len(stages)
    n_committee = sum(1 for s in stages if s in ("committee", "passed", "law"))
    n_passed = sum(1 for s in stages if s in ("passed", "law"))
    n_law = sum(1 for s in stages if s == "law")
    denom = n or 1
    return [
        {"id": "all", "label": "Introduced", "n": n, "width": "100%"},
        {"id": "committee", "label": "Waiting in committee", "n": n_committee, "width": f"{round(100 * n_committee / denom)}%"},
        {"id": "passed", "label": "Passed one chamber", "n": n_passed, "width": f"{round(100 * n_passed / denom)}%"},
        {"id": "law", "label": "Became law", "n": n_law, "width": f"{round(100 * n_law / denom)}%"},
    ]


def stories_from_results(results, limit=8):
    out = []
    for r in (results or [])[:limit]:
        congress = r.get("congress")
        btype = (r.get("type") or "").lower()
        number = r.get("number")
        if not (congress and btype and number):
            continue
        stage = funnel_stage(r.get("latest_action"), r.get("is_law"))
        title = r.get("title") or f"{btype.upper()} {number}"
        out.append({
            "id": f"{btype}{number}",
            "congress": congress,
            "type": btype,
            "number": number,
            "title": title,
            "english_title": compact_title(title),
            "english_text": r.get("latest_action") or "",
            "latest_action": r.get("latest_action") or "",
            "is_law": bool(r.get("is_law")),
            "stage": stage,
            "meta": f"{btype.upper()} {number}" + (f" · {r.get('latest_action')}" if r.get("latest_action") else ""),
        })
    return out


def _short_name(full):
    """'Rep. Arrington, Jodey C. [R-TX-19]' -> 'Jodey C. Arrington'."""
    s = re.sub(r"\s*\[[^\]]*\]\s*$", "", full or "")
    s = re.sub(r"^(Rep|Sen|Del|Resident Commissioner)\.?\s+", "", s)
    m = re.match(r"^([^,]+),\s*(.+)$", s)
    return f"{m.group(2).strip()} {m.group(1).strip()}" if m else s.strip()


def enrich_story(story, bill_data):
    """Fill a search story from a raw Congress.gov bill record (in place).

    Search hits arrive as an id and a title. One bill fetch supplies what a
    card needs to say something: the latest action and its date, who wrote
    it and their party, the policy area, when it was introduced, how many
    signed on, and whether it became law.
    """
    b = (bill_data or {}).get("bill") or {}
    if not b:
        return story
    la = b.get("latestAction") or {}
    action = (la.get("text") or "").strip()
    laws = b.get("laws") or []
    sp = (b.get("sponsors") or [{}])[0]
    if action:
        story["latest_action"] = action
        story["latest_action_date"] = la.get("actionDate")
    if laws:
        story["is_law"] = True
        story["law_number"] = laws[0].get("number")
    story["stage"] = funnel_stage(story.get("latest_action"), story.get("is_law"))
    if sp.get("fullName"):
        story["sponsor"] = _short_name(sp.get("fullName"))
        story["sponsor_party"] = sp.get("party")
        story["sponsor_state"] = sp.get("state")
        story["sponsor_bioguide"] = sp.get("bioguideId")
    if (b.get("policyArea") or {}).get("name"):
        story["policy_area"] = b["policyArea"]["name"]
    if b.get("introducedDate"):
        story["introduced"] = b["introducedDate"]
    if isinstance(b.get("cosponsors"), dict) and b["cosponsors"].get("count") is not None:
        story["cosponsors"] = b["cosponsors"]["count"]
    if b.get("title") and not story.get("title"):
        story["title"] = b["title"]
        story["english_title"] = compact_title(b["title"])
    story["meta"] = f"{story['type'].upper()} {story['number']}" + (f" · {action}" if action else "")
    return story


async def enrich_stories(stories, fetch_bill, budget=3.0):
    """Enrich every story in parallel, within a time budget. Whatever has not
    returned by the deadline is left as it came; nothing here can fail the ask."""
    import asyncio
    if not stories:
        return stories
    loop = asyncio.get_event_loop()

    async def one(s):
        try:
            data = await loop.run_in_executor(None, fetch_bill, s["congress"], s["type"], s["number"])
            enrich_story(s, data)
        except Exception as e:
            print(f"[LEDGER] enrich {s.get('id')}: {e}")

    tasks = [asyncio.ensure_future(one(s)) for s in stories]
    try:
        await asyncio.wait_for(asyncio.shield(asyncio.gather(*tasks, return_exceptions=True)), timeout=budget)
    except asyncio.TimeoutError:
        print(f"[LEDGER] enrich: budget of {budget}s hit; {sum(1 for t in tasks if not t.done())} still pending")
    return stories


LEDGER_SHELF_PREFIX = "ledger:v2:"


def member_present(member):
    if not isinstance(member, dict):
        return False
    if member.get("found") is False:
        return False
    return bool(member.get("name") or member.get("bioguide_id"))


def omit_empty_shelves(shelves):
    out = []
    for sh in shelves or []:
        kind = sh.get("kind")
        ids = [i for i in (sh.get("item_ids") or []) if i]
        if kind == "member":
            out.append({**sh, "item_ids": ids})
        elif ids:
            out.append({**sh, "item_ids": ids})
    return out


def fallback_shelves(stories, member=None, question=""):
    shelves = []
    if member_present(member):
        shelves.append({
            "id": "member",
            "kind": "member",
            "label": member.get("name") or "This member",
            "item_ids": [],
        })
    ranked = rank_by_relevance(stories, question) if question else list(stories or [])
    ids = [s["id"] for s in ranked if s.get("id")]
    if ids:
        shelves.append({
            "id": "all",
            "kind": "topic",
            "label": "These bills",
            "item_ids": ids,
        })
    return shelves


def field_shelves(question, stories, member=None):
    """Group by Congress.gov policy area; rank bills inside each field.

    A field is a real classification, not a Haiku guess. Membership and order
    are therefore the same every time for the same bills and ask. Areas with
    only one bill fold into Also so the page does not grow a shelf per card.
    """
    ranked = rank_by_relevance(stories, question)
    groups = []
    index = {}
    for s in ranked:
        sid = s.get("id")
        if not sid:
            continue
        area = (s.get("policy_area") or "").strip()
        key = area.lower()
        if key not in index:
            index[key] = len(groups)
            groups.append({"key": key, "label": area, "item_ids": []})
        groups[index[key]]["item_ids"].append(sid)

    named = [g for g in groups if g["key"]]
    unnamed_ids = []
    for g in groups:
        if not g["key"]:
            unnamed_ids.extend(g["item_ids"])

    shelves = []
    if member_present(member):
        shelves.append({
            "id": "member",
            "kind": "member",
            "label": member.get("name") or "This member",
            "item_ids": [],
        })

    # One named field, or none: a single ranked list is clearer than a
    # one-shelf page wearing a policy-area label the cards already show.
    if len(named) <= 1:
        ids = [s["id"] for s in ranked if s.get("id")]
        if ids:
            shelves.append({
                "id": "all",
                "kind": "topic",
                "label": "These bills",
                "item_ids": ids,
            })
        return omit_empty_shelves(shelves)

    leftover = list(unnamed_ids)
    topic_n = 0
    for i, g in enumerate(named):
        if topic_n >= 5:
            leftover.extend(g["item_ids"])
            continue
        if len(g["item_ids"]) < 2:
            leftover.extend(g["item_ids"])
            continue
        shelves.append({
            "id": f"f{i}",
            "kind": "topic",
            "label": g["label"][:48],
            "item_ids": g["item_ids"],
        })
        topic_n += 1
    if leftover:
        # Keep leftover in relevance order, not area-append order.
        order = {s["id"]: n for n, s in enumerate(ranked) if s.get("id")}
        leftover = sorted(set(leftover), key=lambda i: order.get(i, 10**6))
        shelves.append({
            "id": "also",
            "kind": "topic",
            "label": "Also" if topic_n else "These bills",
            "item_ids": leftover,
        })
    return omit_empty_shelves(shelves)


def merge_shelves(proposed, stories, member=None):
    known = {s["id"] for s in (stories or []) if s.get("id")}
    shelves = []
    if member_present(member):
        shelves.append({
            "id": "member",
            "kind": "member",
            "label": member.get("name") or "This member",
            "item_ids": [],
        })
    assigned = set()
    topic_n = 0
    for i, sh in enumerate(proposed or []):
        if not isinstance(sh, dict) or sh.get("kind") == "member":
            continue
        label = (sh.get("label") or "").strip()
        if not label:
            continue
        item_ids = []
        for sid in sh.get("item_ids") or []:
            sid = str(sid)
            if sid in known and sid not in assigned:
                item_ids.append(sid)
                assigned.add(sid)
        if not item_ids:
            continue
        shelves.append({
            "id": str(sh.get("id") or f"s{i}"),
            "kind": "topic",
            "label": label[:48],
            "item_ids": item_ids,
        })
        topic_n += 1
        if topic_n >= 5:
            break
    leftover = [s["id"] for s in (stories or []) if s.get("id") and s["id"] not in assigned]
    if leftover:
        shelves.append({
            "id": "also",
            "kind": "topic",
            "label": "Also" if assigned else "These bills",
            "item_ids": leftover,
        })
    return omit_empty_shelves(shelves)


def shelf_cache_key(question, stories, member=None):
    ids = [s.get("id") for s in (stories or []) if s.get("id")]
    bg = ""
    if isinstance(member, dict):
        bg = member.get("bioguide_id") or ""
    payload = json.dumps({
        "q": (question or "").strip().lower(),
        "ids": ids,
        "m": bg,
    }, sort_keys=True)
    return LEDGER_SHELF_PREFIX + hashlib.sha1(payload.encode()).hexdigest()


def build_shelves(question, stories, member=None, client=None):
    """Shelves are Congress policy areas, bills inside them ranked by the ask.

    `client` is accepted so callers and tests keep the old signature; grouping
    no longer asks Haiku which bill belongs where.
    """
    return field_shelves(question, stories, member) or fallback_shelves(stories, member, question)


def district_impact_key(geoid, question, stories):
    ids = ",".join(sorted(s.get("id", "") for s in (stories or [])))
    h = hashlib.sha1(f"{geoid}|{(question or '').strip().lower()}|{ids}".encode()).hexdigest()[:20]
    return f"ledger:district:v3:{h}"


def district_impact(question, district, stories, client):
    """One Haiku call: what this ask means for one congressional district.

    `district` is the resolve_geoid payload (label, representative, senators).
    Grounded in the bills on the page and the representative's sponsorships;
    the district colour (towns, economy) comes from the model's knowledge and
    is asked for only when it is confident. Fail-open to None.
    """
    if not client or not district:
        return None
    label = district.get("district_label") or ""
    rep = district.get("representative") or {}
    rep_bg = rep.get("bioguide_id")
    rep_name = rep.get("name") or "the representative"
    rep_bit = f"{rep_name} ({rep.get('party', '')}-{district.get('state', '')})" if rep else "no current representative"
    ranked = rank_by_relevance(stories, question)
    sponsored = [s for s in ranked if rep_bg and s.get("sponsor_bioguide") == rep_bg]
    lead = ranked[:2]
    payload = [
        {
            "id": s.get("id"),
            "title": (s.get("english_title") or s.get("title") or "")[:140],
            "stage": s.get("stage"),
            "sponsor": s.get("sponsor"),
            "policy_area": s.get("policy_area"),
        }
        for s in ranked[:10]
    ]
    lead_ids = [s.get("id") for s in lead if s.get("id")]
    prompt = f"""You write for a resident of one congressional district. Plain words, no jargon, no hype.

District: {label} — represented by {rep_bit}.
The person asked about: {question}
Most relevant bills for that ask, already ranked. Name at most these two, in this order: {json.dumps(lead_ids)}.
Bills on the page (JSON): {json.dumps(payload)}
Bills this representative sponsored, from that list: {json.dumps([s.get("id") for s in sponsored])}

Return ONLY JSON:
{{
  "known_for": "one short phrase: the region or counties this district covers, then what its economy is known for, e.g. 'Loudoun County and the outer Northern Virginia suburbs; the world's largest cluster of data centers'. Prefer counties over towns; districts get redrawn, so do not list towns unless certain. Empty string if you are not confident.",
  "impact": "two or three sentences on how the ranked bills would matter in this district specifically. Name them by short title, in the order given. Tie them to what the district is known for when you can. If none would matter much here, say so plainly.",
  "rep_line": "one sentence on where {rep_name} stands on this, using only the sponsorship facts given; if none, say they have not sponsored any of these."
}}

Rules: never invent a bill, a vote, or a quote. Do not pick a bill that is not in the ranked list. Do not say 'as an AI'. Keep the whole thing under 90 words."""
    try:
        message = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=400,
            temperature=0,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = (message.content[0].text or "").strip()
        if raw.startswith("```"):
            raw = re.sub(r"^```[a-zA-Z]*\s*", "", raw)
            raw = re.sub(r"\s*```$", "", raw).strip()
        obj = json.loads(raw, strict=False)
        out = {
            "known_for": str(obj.get("known_for") or "").strip(),
            "impact": str(obj.get("impact") or "").strip(),
            "rep_line": str(obj.get("rep_line") or "").strip(),
            "sponsored_ids": [s.get("id") for s in sponsored],
            "lead_ids": lead_ids,
        }
        return out if out["impact"] else None
    except Exception as e:
        print(f"[LEDGER] district impact failed: {e}")
        return None


def fallback_headline(question, place_name, stories):
    n = len(stories)
    n_passed = sum(1 for s in stories if s["stage"] in ("passed", "law"))
    n_law = sum(1 for s in stories if s["stage"] == "law")
    where = place_name or "Congress"
    if n == 0:
        return f"Nothing in {where} matched that ask."
    law_bit = "None are law." if n_law == 0 else (f"{n_law} became law." if n_law != 1 else "One became law.")
    passed_bit = "None passed a chamber." if n_passed == 0 else (
        "One passed a chamber." if n_passed == 1 else f"{n_passed} passed a chamber."
    )
    noun = "bill" if n == 1 else "bills"
    return f"{n} {noun} matched. {passed_bit} {law_bit}"


PARTY_WORD = {"D": "Democrat", "R": "Republican", "I": "Independent"}


def member_headline(member, sponsored):
    """Person-only ask: the headline is about the person, not a missing topic."""
    name = (member or {}).get("name") or "This member"
    party = str((member or {}).get("party") or "")
    party_word = PARTY_WORD.get(party[:1].upper(), party) if party else ""
    state = (member or {}).get("state") or ""
    who = name
    tail = ", ".join(x for x in (party_word, state) if x)
    if tail:
        who = f"{name}, {tail}."
    else:
        who = f"{name}."
    rows = sponsored or []
    n = len(rows)
    if n == 0:
        return f"{who} No sponsored bills on record."
    n_law = sum(1 for r in rows if funnel_stage(r.get("latest_action"), r.get("is_law")) == "law")
    noun = "recent bill" if n == 1 else "recent bills"
    law_bit = "None are law." if n_law == 0 else ("One became law." if n_law == 1 else f"{n_law} became law.")
    return f"{who} {n} {noun} shown. {law_bit}"


def summarize_ledger(question, place_name, stories, client=None):
    """One Haiku call for a headline + English titles. Fail-open to compact titles."""
    headline = fallback_headline(question, place_name, stories)
    deck = "Most bills never make it out of committee. The bars below are how far these actually got. Click a bar to read only those."
    if not client or not stories:
        return {"headline": headline, "deck": deck, "stories": stories}
    payload = [
        {"id": s["id"], "title": s["title"], "action": s["latest_action"]}
        for s in stories
    ]
    prompt = f"""You write newspaper headlines for a civic site. No jargon. Do not invent facts.

The person asked: {question}
Place shown: {place_name or "the United States"}

Bills (JSON): {json.dumps(payload)}

Return ONLY JSON:
{{
  "headline": "one or two short sentences: how many bills, how far the furthest got, whether any are law",
  "deck": "two sentences: most bills die in committee; click a bar to filter",
  "stories": [{{"id": "hr1", "title": "English headline, no bill number", "text": "one sentence of what it would do, from the official title only"}}]
}}
Use every id. If you cannot tell what a bill does from its title, say so in the text. Never claim a bill is law unless the action says so."""
    try:
        message = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1200,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = (message.content[0].text or "").strip()
        if raw.startswith("```"):
            raw = re.sub(r"^```[a-zA-Z]*\s*", "", raw)
            raw = re.sub(r"\s*```$", "", raw).strip()
        obj = json.loads(raw, strict=False)
        by_id = {row["id"]: row for row in (obj.get("stories") or []) if row.get("id")}
        merged = []
        for s in stories:
            extra = by_id.get(s["id"]) or {}
            merged.append({
                **s,
                "english_title": extra.get("title") or s["english_title"],
                "english_text": extra.get("text") or s["english_text"],
            })
        return {
            "headline": (obj.get("headline") or headline).strip(),
            "deck": (obj.get("deck") or deck).strip(),
            "stories": merged,
        }
    except Exception as e:
        print(f"[LEDGER] summarize failed: {e}")
        return {"headline": headline, "deck": deck, "stories": stories}


# Item titles come out of synthesized extractors and some are fragments —
# a clause that starts mid-sentence, or the boilerplate line that introduces
# a tally. Showing "d. The Voting Board tally was:" as a motion title reads
# as broken software. Trim the boilerplate, and when what remains cannot
# stand on its own, say so plainly rather than print a shard.
_TALLY_TAIL = re.compile(r"\s*the voting board tally was:?\s*$", re.I)
_LEADING_JUNK = re.compile(r"^[^A-Za-z(]*")


def _tidy_motion(title, vote):
    text = _TALLY_TAIL.sub("", (title or "").strip())
    text = _LEADING_JUNK.sub("", text).strip()
    if text[:9].lower() == "motioned,":
        text = text[9:].strip()
    if len(text) < 12 or " " not in text:
        return None
    return text[0].upper() + text[1:]


def _place_records(store, summaries=None, digests=None,
                   limit_meetings=8, limit_votes=12):
    """The actual record, trimmed for a page: recent meetings newest first,
    each with the votes taken and how each member voted.

    This exists because showing a jurisdiction NOTHING while holding 18 of
    its meetings is worse than showing the record with an honest warning.
    Certification decides what we VOUCH for, not what we display.
    """
    summaries, digests = summaries or {}, digests or {}
    items = {i["item_id"]: i for i in (store.get("agenda_items") or {}).values()}
    by_meeting = {}
    for vote in (store.get("vote_events") or {}).values():
        by_meeting.setdefault(vote["meeting_id"], []).append(vote)

    out = []
    meetings = sorted((store.get("meetings") or {}).values(),
                      key=lambda m: m.get("date") or "", reverse=True)
    for meeting in meetings[:limit_meetings]:
        votes = []
        for vote in by_meeting.get(meeting["meeting_id"], [])[:limit_votes]:
            summary = summaries.get(vote.get("item_id")) or {}
            title = _tidy_motion(
                (items.get(vote.get("item_id")) or {}).get("title"), vote)
            positions = vote.get("positions") or []
            against = [p["member"] for p in positions
                       if p.get("position") in ("no", "nay")]
            abstain = [p["member"] for p in positions
                       if p.get("position") == "abstain"]
            votes.append({
                "title": title,
                # The plain-English line is machine-written and advisory, but
                # it is the only readable description some extractors give
                # us. Keep it labelled, never promote it to the record.
                "plain": summary.get("plain_english"),
                "topic": summary.get("topic"),
                "result": vote.get("result"),
                "counts": vote.get("counts") or {},
                "against": against,
                "abstain": abstain,
                "certified": (vote.get("certification") or {}).get("status") == "certified",
                "source_url": vote.get("source_document") or meeting.get("source_url"),
            })
        attendance = meeting.get("attendance") or {}
        out.append({
            "date": meeting.get("date"),
            "body": meeting.get("body") or "",
            "digest": (digests.get(meeting["meeting_id"]) or {}).get("digest"),
            "present": [n for n, st in attendance.items() if st == "present"],
            "absent": [n for n, st in attendance.items() if st != "present"],
            "source_url": meeting.get("source_url"),
            "certified": (meeting.get("certification") or {}).get("status") == "certified",
            "votes": votes,
            "vote_total": len(by_meeting.get(meeting["meeting_id"], [])),
        })
    return out


def _norm_j(text):
    return re.sub(r"\s+", " ",
                  re.sub(r"[^a-z0-9 ]+", " ", (text or "").lower())).strip()


def _place_capital_projects(slug, limit=8):
    """The public-works half of the civic loop: what is being built.

    Lives in its own `<slug>-cip` store. Returns the largest projects by
    total, because a page that lists 273 of them in source order answers no
    question anyone asked.
    """
    path = _FOUNDRY_STORE / f"{slug.split('-')[0]}-cip.json"
    if not path.exists():
        return None
    try:
        store = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    projects = store.get("capital_projects") or []
    projects = list(projects.values()) if isinstance(projects, dict) else projects
    if not projects:
        return None
    meta = store.get("meta") or {}
    scale = 1000 if meta.get("unit") == "usd_thousands" else 1
    ranked = sorted(projects, key=lambda x: x.get("total") or 0, reverse=True)
    return {
        "edition": meta.get("edition") or meta.get("sub") or "",
        "source_url": meta.get("source_url"),
        "count": len(projects),
        "total_usd": sum((x.get("total") or 0) for x in projects) * scale,
        "projects": [{
            "title": x.get("title"),
            "function": x.get("function"),
            "districts": x.get("districts") or [],
            "status": x.get("status"),
            "work_type": x.get("work_type"),
            "total_usd": (x.get("total") or 0) * scale,
        } for x in ranked[:limit]],
    }


def _place_elections(county_name, state, limit=6):
    """Who was elected here. Matched by county name against the elections
    stores, the same join the lab console uses."""
    base = _norm_j(county_name).replace(" county", "")
    if not base or not state:
        return None
    hits = []
    for path in sorted(_FOUNDRY_STORE.glob("*elections*.json")):
        try:
            store = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        contests = store.get("contests") or []
        contests = list(contests.values()) if isinstance(contests, dict) else contests
        for contest in contests:
            if contest.get("state") != state:
                continue
            j = _norm_j(contest.get("jurisdiction"))
            if j in (base, base + " county", base + " unified"):
                hits.append(contest)
    if not hits:
        return None
    hits.sort(key=lambda c: (c.get("year") or 0, c.get("month") or 0), reverse=True)
    newest = hits[0].get("year")
    recent = [c for c in hits if c.get("year") == newest]
    return {
        "total": len(hits),
        "latest_year": newest,
        "contests": [{
            "office": c.get("office"),
            "district": c.get("district"),
            "year": c.get("year"),
            "winners": c.get("winner_names") or [],
            "n_candidates": c.get("n_candidates"),
            "source_url": c.get("source_url"),
        } for c in recent[:limit]],
    }


def _enrichment(slug):
    """The Haiku layer: plain-English item summaries and per-meeting digests.

    These are machine-derived and advisory, never certified, but they are
    what makes a synthesized extractor's output readable — Stafford's raw
    item titles are clause fragments, while its summaries are sentences.
    """
    out = {}
    for name in ("item-summaries", "meeting-digests"):
        path = _FOUNDRY_STORE / f"{name}.json"
        try:
            blob = json.loads(path.read_text())
        except (OSError, ValueError):
            blob = {}
        out[name] = {k: v for k, v in blob.items() if k.startswith(slug)}
    return out["item-summaries"], out["meeting-digests"]


def _board_roster(store, elections):
    """Who sits on this body, how often they are there, and how often they
    are with the majority.

    "With the majority" is arithmetic, not judgement, and the page says so:
    a board that agrees is not thereby wrong.
    """
    votes = list((store.get("vote_events") or {}).values())
    meetings = list((store.get("meetings") or {}).values())
    seats = {}
    for meeting in meetings:
        for name, status in (meeting.get("attendance") or {}).items():
            row = seats.setdefault(name, {"name": name, "present": 0,
                                          "meetings": 0, "with": 0, "cast": 0})
            row["meetings"] += 1
            row["present"] += 1 if status == "present" else 0
    for vote in votes:
        counts = vote.get("counts") or {}
        winning = "aye" if (counts.get("aye") or 0) >= (counts.get("no") or 0) else "no"
        for pos in vote.get("positions") or []:
            stance = pos.get("position")
            if stance not in ("aye", "no"):
                continue
            row = seats.setdefault(pos["member"],
                                   {"name": pos["member"], "present": 0,
                                    "meetings": 0, "with": 0, "cast": 0})
            row["cast"] += 1
            row["with"] += 1 if stance == winning else 0

    # District and office come from the election that seated them, matched
    # on surname because the two sources spell names differently.
    # Only the contest for THIS body seats these people. Surname alone
    # matched a county supervisor to a town mayoralty, which is a different
    # office in a different government.
    body_words = ("supervisor", "council")
    by_surname = {}
    for contest in (elections or {}).get("contests", []):
        office = (contest.get("office") or "").lower()
        if not any(w in office for w in body_words) or "school" in office:
            continue
        for winner in contest.get("winners") or []:
            key = _norm_j(winner).split()[-1] if winner else ""
            if key:
                by_surname.setdefault(key, contest)
    # Extractors record the same person twice — "Guy" from a tally line and
    # "Margaret Guy" from an attendance list — so a seven-seat board arrives
    # as sixteen members. Merge on surname and keep the fullest spelling.
    merged = {}
    for row in seats.values():
        surname = _norm_j(row["name"]).split()[-1] if row["name"] else ""
        if not surname:
            continue
        prior = merged.get(surname)
        if prior is None:
            merged[surname] = dict(row)
            continue
        for field in ("present", "meetings", "with", "cast"):
            prior[field] += row[field]
        if len(row["name"]) > len(prior["name"]):
            prior["name"] = row["name"]

    roster = []
    for surname, row in merged.items():
        seat = by_surname.get(surname) or {}
        roster.append({
            "name": row["name"],
            "district": seat.get("district"),
            "office": seat.get("office"),
            "with_majority": round(100 * row["with"] / row["cast"]) if row["cast"] else None,
            "votes_cast": row["cast"],
            "attendance": round(100 * row["present"] / row["meetings"]) if row["meetings"] else None,
        })
    roster.sort(key=lambda r: (-(r["votes_cast"] or 0), r["name"]))
    return roster[:15]


def _year_stats(store, year):
    meetings = [m for m in (store.get("meetings") or {}).values()
                if str(m.get("date") or "").startswith(str(year))]
    ids = {m["meeting_id"] for m in meetings}
    votes = [v for v in (store.get("vote_events") or {}).values()
             if v.get("meeting_id") in ids]
    unanimous = sum(1 for v in votes if not (v.get("counts") or {}).get("no"))
    return {
        "year": year,
        "meetings": len(meetings),
        "votes": len(votes),
        "unanimous_pct": round(100 * unanimous / len(votes)) if votes else None,
        "failed": sum(1 for v in votes if v.get("result") == "fail"),
    }


def _public_cert_note(slug, store):
    """The reader-facing reason this place is or is not cross-checked, from
    the health ledger. Advisory: never fail the page over it."""
    try:
        import sys
        sys.path.insert(0, "foundry")
        import health
        import datetime
        doc = health.load()
        events = health.events_for(doc, slug)
        summary = health.summarize(slug, store, events,
                                   datetime.date.today().isoformat())
        return health.public_note(summary, events), summary
    except Exception:
        return "", None


# Coverage summaries keyed on (slug, store mtime, upcoming mtime). This runs on
# the public search path and used to re-parse a multi-MB store file per query
# just to count meetings; the summary itself is a few KB.
_COVERAGE_CACHE = {}
_COVERAGE_CACHE_MAX = 64


def _mtime(path):
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return None


def foundry_place_coverage(slug):
    path = _FOUNDRY_STORE / f"{slug}.json"
    if not path.exists():
        return {"found": False, "slug": slug, "meetings": 0, "certified": False, "quarantined": True}
    key = (slug, _mtime(path), _mtime(_FOUNDRY_STORE / "upcoming.json"))
    hit = _COVERAGE_CACHE.get(key)
    if hit is not None:
        return hit
    out = _foundry_place_coverage_uncached(slug, path)
    if len(_COVERAGE_CACHE) >= _COVERAGE_CACHE_MAX:
        _COVERAGE_CACHE.pop(next(iter(_COVERAGE_CACHE)))
    _COVERAGE_CACHE[key] = out
    return out


def _foundry_place_coverage_uncached(slug, path):
    store = json.loads(path.read_text())
    meetings = store.get("meetings") or {}
    first = next(iter(meetings.values()), {}) if meetings else {}
    certs = [(m.get("certification") or {}) for m in meetings.values()]
    quarantined = any(c.get("status") == "quarantined" for c in certs) or not certs
    certified = any(c.get("status") == "certified" for c in certs)
    note = (first.get("certification") or {}).get("note") or ""
    upcoming = {}
    up_path = _FOUNDRY_STORE / "upcoming.json"
    if up_path.exists():
        try:
            upcoming = json.loads(up_path.read_text())
        except json.JSONDecodeError:
            upcoming = {}
    next_meeting = None
    blob = upcoming if isinstance(upcoming, dict) else {}
    for key, val in blob.items():
        if slug.split("-")[0] in str(key).lower() or slug in str(key).lower():
            next_meeting = val
            break
    public_note, summary = _public_cert_note(slug, store)
    item_summaries, digests = _enrichment(slug)
    records = _place_records(store, item_summaries, digests)
    # Elections are read once and used only to seat the board: which district
    # each member represents. The contest list itself is not shown, because
    # the roster already carries the districts and the raw list mixed school
    # board results into a page about the board of supervisors.
    seated_by = None
    # Stores disagree about where the jurisdiction is written: the curated
    # ones put "Loudoun County, VA" on each meeting, the synthesized ones put
    # "Fairfax County, Virginia, USA — Board of Supervisors" in meta.title
    # and leave the field empty. Read both.
    jurisdiction = (first.get("jurisdiction")
                    or (store.get("meta") or {}).get("title") or "")
    jurisdiction = jurisdiction.split("—")[0].strip()
    parts = [x.strip() for x in jurisdiction.split(",") if x.strip()
             and x.strip().lower() != "usa"]
    county = parts[0] if parts else slug.split("-")[0]
    state = parts[1] if len(parts) > 1 else ""
    state = _STATE_ABBR.get(state.lower(), state.upper() if len(state) == 2 else "")
    seated_by = _place_elections(county, state, limit=40)
    return {
        "found": True,
        "slug": slug,
        "meetings": len(meetings),
        "certified": certified,
        "quarantined": quarantined and not certified,
        "note": note,
        "public_note": public_note,
        "certified_records": (summary or {}).get("total_certified", 0),
        "total_records": (summary or {}).get("total_records", 0),
        "certified_pct": (summary or {}).get("certified_pct", 0),
        "newest_meeting": (summary or {}).get("newest_meeting"),
        "records": records,
        # The rest of the civic loop: what is being built here, and who was
        # elected to decide it. These used to be visible only in the lab
        # console, which meant a reader searching for their county saw votes
        # and nothing else.
        "capital": _place_capital_projects(slug),
        "board": _board_roster(store, seated_by),
        "stats": _year_stats(store, (records[0]["date"] or "2026")[:4] if records else "2026"),
        "next_meeting_list": (next_meeting or {}).get("upcoming") or [],
        "source_url": first.get("source_url") or first.get("data_source_url"),
        "body": first.get("body") or "Board of Supervisors",
        "next_meeting": next_meeting,
    }
