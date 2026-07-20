import anthropic
import os
import re
import json
from dotenv import load_dotenv
from documentor_agent import log_action

load_dotenv()

# Static knowledge - never changes
def year_to_congress(year):
    if year < 1789:
        return None
    return ((year - 1789) // 2) + 1

def congress_to_years(congress):
    start = 1789 + (congress - 1) * 2
    return (start, start + 1)


PRESIDENT_TERMS = {
    "biden": [117, 118],
    "trump": [119, 116, 115],  # Current + first term
    "trump's first term": [115, 116],
    "first trump": [115, 116],
    "obama": [111, 112, 113, 114],
    "bush": [107, 108, 109, 110],
    "clinton": [103, 104, 105, 106],
    "reagan": [97, 98, 99, 100],
    "carter": [95, 96],
}

PRESIDENTIAL_CONTEXT = [
    "signed", "passed", "under", "era", "administration",
    "presidency", "president", "white house", "oval office"
]

CONGRESSIONAL_CONTEXT = [
    "voted", "sponsored", "senator", "representative", 
    "congress", "voting record", "cosponsored", "introduced"
]


def years_to_congress_numbers(year_range_str, all_congresses=False):
    import datetime
    import re

    current_year = datetime.datetime.now().year
    current_congress = year_to_congress(current_year)

    year_match = re.match(r"year:(\d{4})", year_range_str or "")
    if year_match:
        year = int(year_match.group(1))
        congress = year_to_congress(year)
        if not congress:
            return [current_congress]
        # full_history + specific year → all congresses up to and including that year
        if all_congresses:
            return list(range(congress, 0, -1))
        return [congress]

    if all_congresses:
        return list(range(current_congress, 0, -1))

    if "last 2 years" in (year_range_str or "").lower():
        start_year = current_year - 2
    elif "last 10 years" in (year_range_str or "").lower():
        start_year = current_year - 10
    else:
        start_year = current_year - 3

    start_congress = year_to_congress(start_year)
    return list(range(current_congress, start_congress - 1, -1))

KNOWN_BILLS = {
    # Current major legislation
    "one big beautiful bill": {"congress": 119, "type": "hr", "number": 1},
    "big beautiful bill": {"congress": 119, "type": "hr", "number": 1},
    "save act": {"congress": 119, "type": "hr", "number": 22},
    "safeguard american voter eligibility": {"congress": 119, "type": "hr", "number": 22},
    "genius act": {"congress": 119, "type": "s", "number": 1582},
    "genius": {"congress": 119, "type": "s", "number": 1582},
    "inflation reduction act": {"congress": 117, "type": "hr", "number": 5376},
    "chips act": {"congress": 117, "type": "hr", "number": 4346},
    "infrastructure investment": {"congress": 117, "type": "hr", "number": 3684},
    "bipartisan infrastructure": {"congress": 117, "type": "hr", "number": 3684},

    # Healthcare
    "affordable care act": {"congress": 111, "type": "hr", "number": 3590},
    "aca": {"congress": 111, "type": "hr", "number": 3590},
    "obamacare": {"congress": 111, "type": "hr", "number": 3590},

    # Historical major acts
    "patriot act": {"congress": 107, "type": "hr", "number": 3162},
    "usa patriot": {"congress": 107, "type": "hr", "number": 3162},
    "cara": {"congress": 114, "type": "s", "number": 524},
    "dodd frank": {"congress": 111, "type": "hr", "number": 4173},
    "dodd-frank": {"congress": 111, "type": "hr", "number": 4173},
    "citizens united": {"congress": 111, "type": "hr", "number": 2517},

    # Defense
    "ndaa": {"congress": 118, "type": "hr", "number": 2670},
    "national defense authorization": {"congress": 118, "type": "hr", "number": 2670},

    # Education
    "higher education act": {"congress": 89, "type": "hr", "number": 9567},

    # Civil rights
    "voting rights act": {"congress": 89, "type": "hr", "number": 6400},
    "civil rights act": {"congress": 88, "type": "hr", "number": 7152},
}

_KNOWN_BILL_DISQUALIFIERS = [
    "repeal", "amend", "replace", "successor", "alternative",
    "against", "oppose", "modify", "reform", "not", "anti-",
    "instead of", "similar to", "like the", "unlike",
]

def check_known_bills(question):
    import re as _re
    q = question.lower().strip()
    for name, bill in KNOWN_BILLS.items():
        if name not in q:
            continue
        # Reject if the query contains disqualifying context around the act name
        if any(d in q for d in _KNOWN_BILL_DISQUALIFIERS):
            continue
        # Reject if there's substantial additional context (the act name is a
        # substring of a longer, different request rather than the primary subject)
        remainder = q.replace(name, "").strip()
        remainder = _re.sub(r"^(show me|find|what is|tell me about|give me|search for|the|a|an)\s+", "", remainder)
        remainder = _re.sub(r"\s*(act|law|bill|legislation)$", "", remainder.strip())
        if len(remainder) > 20:
            continue
        return bill
    return None

def extract_president_congress(question):
    question_lower = question.lower()
    
    # More flexible matching
    PRESIDENT_PATTERNS = {
        "biden": [117, 118],
        "trump": [119, 115, 116],
        "obama": [111, 112, 113, 114],
        "bush": [107, 108, 109, 110],
        "clinton": [103, 104, 105, 106],
        "reagan": [97, 98, 99, 100],
    }
    
    for president, congresses in PRESIDENT_PATTERNS.items():
        if president in question_lower:
            # Avoid matching "trump" as a verb
            # Check it's used as a name by looking for context
            idx = question_lower.find(president)
            before = question_lower[max(0, idx-10):idx]
            # If preceded by "to " it's likely a verb
            if president == "trump" and before.strip().endswith("to"):
                continue
            return congresses
    
    return None
_BILL_ID_RE = re.compile(
    r"""
    ^\s*
    (?:show\ me\ |find\ |tell\ me\ about\ |what\ is\ |what's\ |open\ |bring\ up\ )?  # optional intro
    (?:the\ )?
    (?P<type>
        h\.?\s*r\.?                       # H.R. / HR / H. R.
      | s\.?                              # S. / S
      | h\.?\s*j\.?\s*res\.?              # H.J.Res / HJRES
      | s\.?\s*j\.?\s*res\.?              # S.J.Res / SJRES
      | h\.?\s*con\.?\s*res\.?            # HConRes
      | s\.?\s*con\.?\s*res\.?            # SConRes
      | h\.?\s*res\.?                     # HRes
      | s\.?\s*res\.?                     # SRes
    )
    \s*\.?\s*
    (?P<num>\d{1,5})
    \s*\.?\s*$
    """,
    re.IGNORECASE | re.VERBOSE,
)

_BILL_TYPE_NORMALIZE = [
    (re.compile(r"^h\.?\s*r\.?$",          re.I), "hr"),
    (re.compile(r"^s\.?$",                 re.I), "s"),
    (re.compile(r"^h\.?\s*j\.?\s*res\.?$", re.I), "hjres"),
    (re.compile(r"^s\.?\s*j\.?\s*res\.?$", re.I), "sjres"),
    (re.compile(r"^h\.?\s*con\.?\s*res\.?$", re.I), "hconres"),
    (re.compile(r"^s\.?\s*con\.?\s*res\.?$", re.I), "sconres"),
    (re.compile(r"^h\.?\s*res\.?$",        re.I), "hres"),
    (re.compile(r"^s\.?\s*res\.?$",        re.I), "sres"),
]


def _normalize_bill_type(raw):
    cleaned = re.sub(r"\s+", "", raw)  # collapse whitespace for matching
    raw_with_spaces = raw.strip()
    for pat, code in _BILL_TYPE_NORMALIZE:
        if pat.match(cleaned) or pat.match(raw_with_spaces):
            return code
    return None


def fast_route(user_question):
    """Regex fast-path for unambiguous queries. Returns a fully-formed
    structured dict on a confident match, else None — in which case the
    caller falls through to the LLM router. Only matches bill IDs that
    occupy the entire meaningful query, to avoid false positives like
    'what did Ted Kennedy do about S. 2208'."""
    if not user_question:
        return None
    m = _BILL_ID_RE.match(user_question)
    if not m:
        return None
    bill_type = _normalize_bill_type(m.group("type"))
    if not bill_type:
        return None
    try:
        number = int(m.group("num"))
    except ValueError:
        return None
    if number < 1 or number > 99999:
        return None
    return {
        "query_type": "legislation",
        "query_subtype": "specific_bill",
        "jurisdiction": "federal",
        "state_code": None,
        "specific_bill": {
            "congress": 119,
            "type": bill_type,
            "number": number,
        },
        "congress_numbers": [119],
        "keywords": [],
        "expanded_terms": [],
        "topic": "",
        "named_entity": None,
        "entity_name": None,
        "time_filter": False,
        "time_range": None,
        "status": "any",
        "result_count": 1,
        "confidence": 1.0,
        "ambiguity_reason": None,
        "full_history": False,
        "_fast_path": "bill_id",
    }


# State bill-ID fast-path. Each state has its own numbering convention; most
# fit one of HB/SB, HR/SR, AB/SB (NY/CA), LB (Nebraska unicameral), LD (Maine).
_STATE_BILL_ID_RE = re.compile(
    r"""
    ^\s*
    (?:show\ me\ |find\ |tell\ me\ about\ |what\ is\ |what's\ |open\ |bring\ up\ )?
    (?:the\ )?
    (?P<type>
        h\.?\s*b\.?               # HB / H.B.
      | s\.?\s*b\.?               # SB / S.B.
      | h\.?\s*r\.?               # HR / H.R. (some New England)
      | s\.?\s*r\.?               # SR / S.R.
      | a\.?\s*b\.?               # AB / A.B. (CA, NY)
      | a\.?                      # A (NY assembly)
      | s\.?                      # S
      | l\.?\s*b\.?               # LB (Nebraska)
      | l\.?\s*d\.?               # LD (Maine)
      | h\.?\s*f\.?               # HF (MN, IA)
      | s\.?\s*f\.?               # SF (MN, IA)
    )
    \s*\.?\s*
    (?P<num>\d{1,5})
    \s*\.?\s*
    # Optional trailing session anchor — captured for downstream resolution
    (?:
        (?:\s+from\s+|\s+in\s+|\s*,\s*)?
        (?:the\s+)?
        (?P<session_anchor>
            (?P<year>19|20)\d{2}
          | (?P<ord>\d{1,3})(?:st|nd|rd|th)\s+(?:session|legislature|general\s+assembly)
          | session\s+of\s+(?:19|20)\d{2}
        )
    )?
    \s*$
    """,
    re.IGNORECASE | re.VERBOSE,
)


def _extract_state_session(question: str) -> str | None:
    """
    Pull out an explicit session anchor from a state-bill query — a 4-digit
    year, an ordinal session ("88th session"), or "from {year}" / "in {year}".
    Returns the session identifier as a string, or None when the query has no
    anchor and we should default to current session.
    """
    if not question:
        return None
    q = question.strip()
    # 4-digit year anywhere in the query
    year_match = re.search(r"\b(19\d{2}|20\d{2})\b", q)
    if year_match:
        return year_match.group(1)
    # Ordinal session like "88th session"
    ord_match = re.search(r"\b(\d{1,3})(?:st|nd|rd|th)\s+(?:session|legislature|general\s+assembly)\b", q, re.I)
    if ord_match:
        return ord_match.group(1)
    return None


_STATE_BILL_TYPE_NORMALIZE = [
    (re.compile(r"^h\.?\s*b\.?$",  re.I), "HB"),
    (re.compile(r"^s\.?\s*b\.?$",  re.I), "SB"),
    (re.compile(r"^h\.?\s*r\.?$",  re.I), "HR"),
    (re.compile(r"^s\.?\s*r\.?$",  re.I), "SR"),
    (re.compile(r"^a\.?\s*b\.?$",  re.I), "AB"),
    (re.compile(r"^a\.?$",         re.I), "A"),
    (re.compile(r"^s\.?$",         re.I), "S"),
    (re.compile(r"^l\.?\s*b\.?$",  re.I), "LB"),
    (re.compile(r"^l\.?\s*d\.?$",  re.I), "LD"),
    (re.compile(r"^h\.?\s*f\.?$",  re.I), "HF"),
    (re.compile(r"^s\.?\s*f\.?$",  re.I), "SF"),
]


def _normalize_state_bill_type(raw: str) -> str | None:
    cleaned = re.sub(r"\s+", "", raw or "")
    for pat, code in _STATE_BILL_TYPE_NORMALIZE:
        if pat.match(cleaned):
            return code
    return None


def fast_route_state(user_question: str, state_code: str | None = None):
    """
    Regex fast-path for state bill IDs. Returns a structured dict on a confident
    match (with `requested_session` set when the query had an explicit year or
    ordinal anchor), else None. Defaults to the current session when no anchor
    is given; the caller resolves that via LegiScan.
    """
    if not user_question:
        return None
    m = _STATE_BILL_ID_RE.match(user_question)
    if not m:
        return None
    bill_type = _normalize_state_bill_type(m.group("type"))
    if not bill_type:
        return None
    try:
        number = int(m.group("num"))
    except (ValueError, TypeError):
        return None
    if number < 1 or number > 99999:
        return None

    requested_session = _extract_state_session(user_question)
    identifier = f"{bill_type} {number}"

    return {
        "query_type": "legislation",
        "query_subtype": "specific_bill",
        "jurisdiction": "state",
        "state_code": (state_code or "").upper() or None,
        "specific_bill": {
            "type": bill_type,
            "number": number,
            "identifier": identifier,
        },
        "requested_session": requested_session,
        "keywords": [],
        "expanded_terms": [],
        "topic": "",
        "named_entity": None,
        "entity_name": None,
        "time_filter": bool(requested_session),
        "time_range": None,
        "status": "any",
        "result_count": 1,
        "confidence": 1.0,
        "ambiguity_reason": None,
        "_fast_path": "state_bill_id",
    }


def route_query(user_question, client, full_history=False):
    """
    Takes a plain English question and returns a structured search query.
    This agent never fetches bills - it only extracts intent.
    """
    
    prompt = f"""
You are a query router for a legislative search system.
Extract search intent from a plain English question.
Return ONLY valid JSON, no markdown, no explanation.

CURRENT DATE CONTEXT (authoritative — do not use training-data assumptions):
- Today is 2026. Donald Trump is the current U.S. President (second term, began January 2025).
- The current Congress is the 119th (2025–2026).
- Biden served 2021–2025 (117th and 118th Congresses).
- Trump's first term was 2017–2021 (115th and 116th Congresses).
- When the user says "recently" or "now" in relation to Trump, they mean his CURRENT second term (119th Congress), not his first.

User question: {user_question}

Rules for query_type:
- A person's name, "Senator X", "Representative X", "what did X do", "X's record", "X's votes", "who is X" → "member"
- "X Committee", "committee on X", "House/Senate committee" → "committee"
- Queries that CLEARLY have nothing to do with US legislation, government policy, public law, or civic topics — e.g. "weather forecast tomorrow", "best pizza near me", "stock price of AAPL", "who won the game last night" → "off_topic". When in doubt, choose "legislation" — any phrase that could plausibly be the subject of a bill, regulation, or congressional hearing is on-topic.
- Short, ambiguous-sounding phrases that ARE active policy debates default to "legislation", not "off_topic". Examples that look tech-flavored but are real legislative topics: "age verification", "content moderation", "deepfakes", "facial recognition", "encryption backdoors", "data privacy", "section 230", "right to repair", "noncompetes", "PBMs", "surprise billing", "robocalls", "TikTok ban", "AI safety", "crypto regulation", "stablecoins".
- Everything else (any plausible civic / policy / legislative / governmental topic, even if broad like "marijuana" or "abortion") → "legislation"

Rules for jurisdiction:
- "Virginia bill", "Virginia legislature", "Richmond", "General Assembly", "Virginia delegate", "Virginia senator" → jurisdiction: "state", state_code: "VA"
- Any other US state name + bill/legislature/assembly → jurisdiction: "state", state_code: [2-letter code]
- Everything else → jurisdiction: "federal", state_code: null

Rules for query_subtype (legislation queries only):
- Proper noun law name, named act, roman numerals, known acronym → "named_entity"
- Trailing words like "bill", "legislation", "vote", "law", "act of congress" do NOT change the subtype. "Bipartisan Transparency for American Taxpayers Act bill" is still named_entity — extract "Bipartisan Transparency for American Taxpayers Act" as named_entity and strip the trailing word.
- Proper noun law name + specific year, president, or era → "named_entity_with_date"
- General topic + specific year, president, or era → "concept_with_date"
- "give me a bill", "show me something", "any bill", no specific topic → "browse"
- "laws", "enacted", "signed into law", "passed into law", "became law" → "enacted"
- Everything else → "concept"

Rules for time_filter:
- true if query contains: specific year, president name, era, "recent", "latest", "oldest"
- false otherwise

Rules for named_entity:
- If query_subtype is named_entity or named_entity_with_date: extract the official/formal name if you know it, otherwise extract as written
- "big beautiful bill" or "one big beautiful bill" → "One Big Beautiful Bill Act"
- "obamacare" → "Affordable Care Act"
- "chips act" → "CHIPS and Science Act"
- "save act" → "Safeguard American Voter Eligibility (SAVE) Act"
- "pact act" → "Honoring our PACT Act of 2022" (veterans toxic exposure, by far the most-discussed PACT Act today; cigarette trafficking PACT Act 2010 is rarely meant)
- "farm bill" → "Agriculture Improvement Act of 2018" (most recent enacted farm bill)
- "dodd frank" or "dodd-frank" → "Dodd-Frank Wall Street Reform and Consumer Protection Act"
- "patriot act" or "usa patriot act" → "USA PATRIOT Act" (the original 2001 statute — extensions and amendments are rarely what users mean)
- "freedom act" or "usa freedom act" → "USA FREEDOM Act of 2015"
- Otherwise: null

Rules for confidence (0.0 to 1.0):
- 1.0 → completely unambiguous. "HR 3590", "Ted Kennedy", "Senate Judiciary Committee"
- 0.8 → clear intent with minor uncertainty. "healthcare bills", "Bernie Sanders record"
- 0.6 → some ambiguity. "Kennedy healthcare" could be member or legislation
- 0.4 → significant ambiguity. Mixed signals, unclear intent
- 0.2 → very unclear. Could mean many things
- Always explain low confidence (below 0.7) in ambiguity_reason

Rules for ambiguity_reason:
- null if confidence >= 0.7
- One sentence explaining the ambiguity if confidence < 0.7

Rules for entity_name:
- For member queries: extract the person's name only
- For committee queries: extract the committee name
- For legislation queries: null

Rules for result_count:
- "a bill", "one bill", "a law", "an example" → 1
- "a few", "some" → 3
- No quantity mentioned → 5
- "many", "lots", explicit number → that number
- Maximum: 20

Rules for specific_bill:
- If user mentions a bill number like "HR 3590", "S 1234" → extract it
- Otherwise → null

Rules for status:
- "passed", "became law", "signed", "enacted", "a law", "laws" → "enacted"
- "failed", "rejected" → "failed"
- No status mentioned → "any"

Rules for keywords (legislation only):
- Extract ONLY subject matter nouns
- NEVER include: show, me, bills, find, a, one, some, what, has, done, about, related, to, from, the, give, senator, representative, laws, passed, signed, enacted

Rules for time_range:
- "in [year]", "from [year]", "[year] bill" → "year:YYYY"
- "this Congress", "current Congress", "this session" → "year:2025"
- "recent", "recently" → "last 2 years"
- "last 5 years" or nothing → "last 5 years"
- "last 10 years" → "last 10 years"
- Presidential terms handled separately

Examples:
"Title IX" →
{{"query_type": "legislation", "query_subtype": "named_entity", "named_entity": "Title IX", "time_filter": false, "confidence": 0.95, "ambiguity_reason": null, "entity_name": null, "keywords": ["Title IX"], "topic": "Title IX education legislation", "time_range": "last 5 years", "bill_type": "all", "result_count": 4, "specific_bill": null, "status": "any"}}

"Title IX in 1972" →
{{"query_type": "legislation", "query_subtype": "named_entity_with_date", "named_entity": "Title IX", "time_filter": true, "confidence": 0.95, "ambiguity_reason": null, "entity_name": null, "keywords": ["Title IX"], "topic": "Title IX 1972", "time_range": "year:1972", "bill_type": "all", "result_count": 4, "specific_bill": null, "status": "any"}}

"healthcare bills from 2017" →
{{"query_type": "legislation", "query_subtype": "concept_with_date", "named_entity": null, "time_filter": true, "confidence": 0.9, "ambiguity_reason": null, "entity_name": null, "keywords": ["healthcare"], "topic": "healthcare legislation 2017", "time_range": "year:2017", "bill_type": "all", "result_count": 5, "specific_bill": null, "status": "any"}}

"give me a bill" →
{{"query_type": "legislation", "query_subtype": "browse", "named_entity": null, "time_filter": false, "confidence": 0.8, "ambiguity_reason": null, "entity_name": null, "keywords": [], "topic": "recent legislation", "time_range": "last 2 years", "bill_type": "all", "result_count": 5, "specific_bill": null, "status": "any"}}

"laws passed under trump" →
{{"query_type": "legislation", "query_subtype": "enacted", "named_entity": null, "time_filter": true, "confidence": 0.9, "ambiguity_reason": null, "entity_name": null, "keywords": [], "topic": "enacted legislation trump", "time_range": "last 5 years", "bill_type": "all", "result_count": 5, "specific_bill": null, "status": "enacted"}}

"gun control bills" →
{{"query_type": "legislation", "query_subtype": "concept", "named_entity": null, "time_filter": false, "confidence": 0.9, "ambiguity_reason": null, "entity_name": null, "keywords": ["gun", "control"], "topic": "gun control legislation", "time_range": "last 5 years", "bill_type": "all", "result_count": 5, "specific_bill": null, "status": "any"}}

"HR 3590" →
{{"query_type": "legislation", "query_subtype": "concept", "named_entity": null, "time_filter": false, "confidence": 1.0, "ambiguity_reason": null, "entity_name": null, "keywords": [], "topic": "specific bill HR 3590", "time_range": "last 5 years", "bill_type": "hr", "result_count": 1, "specific_bill": {{"type": "hr", "number": 3590, "congress": null}}, "status": "any"}}

"Kennedy healthcare" →
{{"query_type": "legislation", "query_subtype": "concept", "named_entity": null, "time_filter": false, "confidence": 0.5, "ambiguity_reason": "Kennedy could refer to Senator Ted Kennedy or legislation named after Kennedy", "entity_name": null, "keywords": ["healthcare"], "topic": "Kennedy healthcare legislation", "time_range": "last 5 years", "bill_type": "all", "result_count": 5, "specific_bill": null, "status": "any"}}

"Ted Kennedy" →
{{"query_type": "member", "query_subtype": "concept", "named_entity": null, "time_filter": false, "confidence": 0.95, "ambiguity_reason": null, "entity_name": "Ted Kennedy", "keywords": [], "topic": "", "time_range": "last 5 years", "bill_type": "all", "result_count": 5, "specific_bill": null, "status": "any"}}

"Senate Judiciary Committee" →
{{"query_type": "committee", "query_subtype": "concept", "named_entity": null, "time_filter": false, "confidence": 1.0, "ambiguity_reason": null, "entity_name": "Senate Judiciary Committee", "keywords": [], "topic": "", "time_range": "last 5 years", "bill_type": "all", "result_count": 5, "specific_bill": null, "status": "any"}}

Return ONLY this JSON structure:
{{
    "query_type": "legislation",
    "query_subtype": "concept",
    "named_entity": null,
    "time_filter": false,
    "confidence": 0.9,
    "ambiguity_reason": null,
    "entity_name": null,
    "keywords": ["keyword1"],
    "topic": "description",
    "time_range": "last 5 years",
    "bill_type": "all",
    "result_count": 5,
    "specific_bill": null,
    "status": "any",
    "jurisdiction": "federal",
    "state_code": null
}}
"""
    
    message = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[
            {"role": "user", "content": prompt}
        ]
    )

    raw = message.content[0].text.strip()
    raw = raw.replace("```json", "").replace("```", "").strip()
    
    try:
        structured = json.loads(raw)
    except json.JSONDecodeError:
        # Fallback if AI returns malformed JSON
        structured = {
            "keywords": user_question.split()[:3],
            "topic": user_question,
            "time_range": "last 5 years",
            "bill_type": "all",
            "query_subtype": "concept",
            "named_entity": None,
        }
    
    # Known bills table: tight match only → hint, not bypass.
    # Sets known_bill_hint so the hinted bill is prepended to candidates
    # and ranked by the validator alongside search results.
    known = check_known_bills(user_question)
    if known:
        structured["known_bill_hint"] = known
        structured["query_type"] = "legislation"

    # Add congress numbers based on time range
    structured["congress_numbers"] = years_to_congress_numbers(structured.get("time_range", "last 5 years"), all_congresses=full_history)

    # Override: "this Congress" / "current Congress" → lock to 119th only
    import datetime as _dt
    _current_congress = year_to_congress(_dt.datetime.now().year)
    _THIS_CONGRESS_PATTERNS = [
        "this congress", "current congress", "119th congress",
        "this session", "current session", "this legislative session",
    ]
    if any(p in user_question.lower() for p in _THIS_CONGRESS_PATTERNS):
        structured["congress_numbers"] = [_current_congress]
        structured["time_range"] = f"year:{_dt.datetime.now().year}"

    # Override congress_numbers if a president was mentioned
    president_congresses = extract_president_congress(user_question)
    if president_congresses:
        structured["congress_numbers"] = president_congresses
        structured["time_range"] = "presidential term"

    # When user lists multiple explicit mechanisms/topics, boost result_count
    # so we can surface bills across all of them rather than collapsing to one.
    _MECHANISM_SIGNALS = [
        "negotiation", "price cap", "importation", "import", "transparency",
        "340b", "rebate", "formulary", "out-of-pocket", "out of pocket",
        "copay", "deductible", "competitive", "generic", "biosimilar",
    ]
    _mechanism_count = sum(1 for s in _MECHANISM_SIGNALS if s in user_question.lower())
    if _mechanism_count >= 3 and structured.get("result_count", 5) < 10:
        structured["result_count"] = 10

    # Detect invalid Roman numeral in "Title X" citations and suggest corrections.
    # "IIX" is not a real Roman numeral — user likely meant VIII or IX.
    import re as _re_roman
    _VALID_ROMAN = {
        "I","II","III","IV","V","VI","VII","VIII","IX","X",
        "XI","XII","XIII","XIV","XV","XVI","XVII","XVIII","XIX","XX",
    }
    _title_match = _re_roman.search(r'\bTitle\s+([IVXivx]+)\b', user_question)
    if _title_match:
        cited = _title_match.group(1).upper()
        if cited not in _VALID_ROMAN:
            # Find closest valid alternatives
            _COMMON_TITLES = {
                "VIII": "Title VIII — Fair Housing Act",
                "IX":   "Title IX — Education Amendments (sex discrimination)",
                "VII":  "Title VII — Civil Rights Act (employment discrimination)",
                "XI":   "Title XI",
            }
            suggestions = []
            for roman, desc in _COMMON_TITLES.items():
                if cited.replace("I","").replace("X","").replace("V","") == "" :
                    # crude proximity — both share characters
                    if set(cited) & set(roman):
                        suggestions.append(desc)
            suggestion_str = " or ".join(suggestions[:2]) if suggestions else "Title VIII or Title IX"
            structured["confidence"] = min(structured.get("confidence", 1.0), 0.4)
            structured["ambiguity_reason"] = (
                f'"{_title_match.group(0)}" doesn\'t match a standard Title citation '
                f'("{cited}" is not a valid Roman numeral). Did you mean {suggestion_str}?'
            )

    PRESIDENTS = ["trump", "biden", "obama", "bush", "clinton", "reagan", "carter"]

    question_lower = user_question.lower()

    presidential_signals = ["signed", "passed", "under", "era",
                            "administration", "presidency", "president",
                            "white house"]

    congressional_signals = ["voted", "sponsored", "senator",
                             "representative", "voting record",
                             "cosponsored", "introduced"]

    entity = (structured.get("entity_name") or "").lower()

    if structured.get("query_type") == "member":
        if any(p in entity for p in PRESIDENTS):
            has_presidential = any(s in question_lower for s in presidential_signals)
            has_congressional = any(s in question_lower for s in congressional_signals)

            if has_presidential and not has_congressional:
                structured["query_type"] = "legislation"
                structured["entity_name"] = None

                stop_words = {"what", "are", "the", "latest", "bills", "that",
                              "has", "passed", "signed", "under", "laws", "legislation",
                              "trump", "biden", "obama", "bush", "clinton", "reagan"}

                words = user_question.lower().split()
                meaningful = [w for w in words if w not in stop_words and len(w) > 3]

                if meaningful:
                    structured["keywords"] = meaningful
                else:
                    structured["keywords"] = ["enacted", "signed"]
            elif has_congressional and not has_presidential:
                pass  # Keep as member — they were in Congress
            else:
                # Ambiguous — Trump defaults to legislation; Biden/Obama stay as member
                if "trump" in entity:
                    structured["query_type"] = "legislation"
                    structured["entity_name"] = None

    log_action(
    agent_name="router",
    action="route_query",
    input_data={"question": user_question},
    output_data={
        "query_type": structured.get("query_type"),
        "confidence": structured.get("confidence"),
        "ambiguity_reason": structured.get("ambiguity_reason"),
        "keywords": structured.get("keywords"),
        "entity_name": structured.get("entity_name"),
    }
)
    
    return structured

if __name__ == "__main__":
    import anthropic
    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    result = route_query("senate judiciary committee", client)
    print(result['query_type'])
    print(result['entity_name'])