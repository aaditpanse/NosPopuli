import re
from dotenv import load_dotenv

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
    is given; the caller reads the newest session on disk first.
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

    # Only the words around the bill number can name a year: HB 2019 is a
    # bill number, not the 2019 session.
    requested_session = _extract_state_session(user_question[:m.start("num")] + " " + user_question[m.end("num"):])
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


def intents_from_structured(structured):
    """Member + topic intents for the ledger. Never changes query_type.

    Haiku may already have filled `intents`. If not, derive from the exclusive
    routing fields so /search stays XOR while /ledger can fetch both.
    """
    def _clean(raw):
        out = []
        seen = set()
        for item in raw or []:
            if not isinstance(item, dict):
                continue
            kind = (item.get("kind") or "").strip().lower()
            if kind not in ("member", "topic", "committee"):
                continue
            name = (item.get("name") or item.get("label") or "").strip()
            if not name:
                continue
            key = (kind, name.lower())
            if key in seen:
                continue
            seen.add(key)
            if kind == "topic":
                out.append({"kind": "topic", "label": name})
            else:
                out.append({"kind": kind, "name": name})
        return out

    cleaned = _clean(structured.get("intents") if isinstance(structured, dict) else None)
    qtype = (structured or {}).get("query_type") or "legislation"
    entity = ((structured or {}).get("entity_name") or "").strip()
    if qtype != "member" and not entity:
        cleaned = [i for i in cleaned if i.get("kind") != "member"]
    if cleaned:
        return cleaned

    out = []
    topic = ((structured or {}).get("topic") or "").strip()
    keywords = (structured or {}).get("keywords") or []
    if qtype == "member" and entity:
        out.append({"kind": "member", "name": entity})
        if keywords:
            label = topic or " ".join(str(k) for k in keywords if k)
            if label:
                out.append({"kind": "topic", "label": label})
    elif qtype == "committee" and entity:
        out.append({"kind": "committee", "name": entity})
    elif qtype == "legislation":
        if entity:
            out.append({"kind": "member", "name": entity})
        label = topic or " ".join(str(k) for k in keywords if k) or "legislation"
        out.append({"kind": "topic", "label": label})
    return out


# ── One routing decision for /ledger, /search and /state/search ──
#
# route() is the only entry. The free checks (ledger_agent.classify_question)
# go first on /ledger, and a page that needs no search returns there, before
# any model call; everything else is structured here, the same way for all
# three routes, so they cannot disagree about what a question is — only
# about how they render the answer.

_PRESIDENTS = ("trump", "biden", "obama", "bush", "clinton", "reagan")
_PRESIDENTIAL_SIGNALS = ("signed", "passed", "under", "era", "administration", "presidency", "white house")
_CONGRESSIONAL_SIGNALS = ("voted", "sponsored", "senator", "representative", "voting record", "cosponsored")


def structure_question(question, state_code=None, *, full_history=False,
                       before_congress=None, max_results=None, fresh=False,
                       get_client):
    """Pick the cheapest router that answers: state fast-path → federal
    fast-path → LLM, then apply the request knobs and the president rules.

    `get_client` is a callable, called only when the LLM is needed, so a
    fast-path hit never builds a client. Fail-closed: an LLM error raises
    into the caller, which already turns it into its own error response."""
    structured = None
    import graph
    if state_code and state_code.upper() in graph.loaded_states():
        structured = fast_route_state(question, state_code)
    if structured is None:
        structured = fast_route(question)
    if structured is None:
        import graph
        entities = graph.link_entities(question)
        intent, how = question_intent(question, entities)
        structured = structure_free(question, intent, entities, full_history=full_history)
        structured["_intent_how"] = how
    else:
        print(f"[ROUTER] fast-path hit: {structured.get('_fast_path')}")
        structured.setdefault("_intent", "explain_law")
        structured.setdefault("_entities", [])
    structured["intents"] = intents_from_structured(structured)

    structured["full_history"] = full_history
    structured["max_results_override"] = max_results if full_history else None
    structured["_bypass_search_cache"] = bool(fresh)
    if full_history and before_congress:
        structured["before_congress"] = before_congress

    _apply_presidential_term_filter(structured, question)
    _disambiguate_president_query(structured, question)
    return structured


# ── What a question asks for (the graph search's intent) ──
#
# One of INTENTS, from the question's shape and the graph nodes it names
# (graph.link_entities), by rule; Laya answers only whether a question is
# about government at all. Laya alone read the intent right half the time on
# the Tier 2 tuning rows, mostly calling a bare topic ("healthcare") off
# topic; the rules decide what they can, and a bare topic is find_bills.

INTENTS = ("find_bills", "person_record", "how_voted", "who_voted", "money", "compare_states",
           "copied_bills", "seat_holder", "committee", "organization", "explain_law", "local_place",
           "elections", "off_topic")
_GRAPH_ASK_INTENT = {"votes": "how_voted", "voters": "who_voted", "holder": "seat_holder", "committee": "committee",
                     "reported": "committee", "referrals": "committee", "lobbied_on": "money", "funds": "money",
                     "org_lobbied": "organization", "sponsored": "person_record", "nominated": "person_record",
                     "signed_by": "person_record", "law": "explain_law", "sponsors": "find_bills",
                     "related": "find_bills"}
_VOTE_WORDS = re.compile(r"\bvot(e|es|ed|ing)\b", re.I)
_WHO_VOTED = re.compile(r"\b(who|which\b.*?)\s+(\w+\s+){0,3}vot(ed|e)\b|\bvoted (for|against|no|yes) on\b", re.I)
# "Fund" alone is a verb as often as money ("what did the law fund");
# "funded by", "who funds" and "funders" are money.
_MONEY = re.compile(r"\b(lobb\w*|donors?|donat\w*|pacs?|money|contribut\w*|raised?|raising|spen[dt]\w*|"
                    r"paid for|funders?|funded by|who funds|funds (him|her|them))\b", re.I)
_COMPARE_STATES = re.compile(r"\b(which|what) states\b|\bstates (that|with|where|banning|passed|have)\b|"
                             r"\bother states\b|\bacross (the )?states\b|\b(many|several|all) states\b", re.I)
_COPIED = re.compile(r"\b(copied|copycat|copy|model (bill|legislation)|same bill|identical)\b", re.I)
_SEAT = re.compile(r"\bwho (is|was|are|were) (my|the|our)\b|\bwho represents\b|\bwho held\b|\bwho chairs\b|"
                   r"\b(speaker|ranking member|chair(man|woman)?) of\b|\bwho (is|was) (the )?(governor|president|"
                   r"speaker|senator|mayor)\b|\bwho are my\b", re.I)
ABOUT_ME = re.compile(r"\b(my|our)\s+(reps?|representatives?|senators?|congress(wo)?man|congress(wo)?men|delegates?|"
                      r"state (senators?|legislators?|delegates?|representatives?)|county|district|legislators?)\b", re.I)
_EXPLAIN = re.compile(r"\b(what (does|did|is|was)|explain|tell me about)\b", re.I)


_PRESIDENTIAL = re.compile(r"\b(signed|passed|under|era|administration|presidency|white house|laws?|enacted)\b",
                           re.I)


def presidential(question, entities):
    """True when the question names a president for what passed in their
    time ("laws passed under trump"), not for the person's own record. Pure."""
    return any(e["kind"] == "person" and e.get("source") == "president" for e in entities or []) \
        and bool(_PRESIDENTIAL.search(question or ""))


def rule_intent(question, entities, plate=None, ask=None):
    """The intent the question's shape and named nodes decide, or None when
    they do not (a bare topic). `plate` and `ask` are classify_question's.
    Pure."""
    if plate == "graph" and ask:
        return _GRAPH_ASK_INTENT.get(ask.get("ask"), "find_bills")
    fixed = {"elections": "elections", "uncharted": "local_place", "home": "off_topic", "bill": "explain_law",
             "state_bill": "explain_law"}
    if plate in fixed:
        return fixed[plate]
    q = question or ""
    kinds = {e["kind"] for e in entities or []}
    if presidential(q, entities):
        return "find_bills"
    if plate is None:
        # /search and /state/search skip the free checks; a county or city
        # government is still no federal or state topic ("LA County").
        from agents.ledger_agent import _looks_local, match_foundry_place
        if match_foundry_place(q) or _looks_local(q):
            return "local_place"
    if _COPIED.search(q):
        return "copied_bills"
    if _COMPARE_STATES.search(q):
        return "compare_states"
    if _SEAT.search(q) and "bill" not in kinds:
        return "seat_holder"
    if _WHO_VOTED.search(q):
        return "who_voted"
    if ("person" in kinds or ABOUT_ME.search(q)) and _VOTE_WORDS.search(q):
        return "how_voted"
    if _MONEY.search(q):
        return "organization" if "organization" in kinds and "bill" not in kinds and not re.search(
            r"\b(donors?|pacs?|funds?|funders?|funded|raised?)\b", q, re.I) else "money"
    if "organization" in kinds:
        return "organization"
    if "committee" in kinds:
        return "committee"
    if "person" in kinds:
        return "person_record"
    if "bill" in kinds:
        named = sum(len(e["surface"].split()) for e in entities if e["kind"] == "bill")
        words = len(re.findall(r"[A-Za-z0-9]+", q))
        if _EXPLAIN.search(q) or named >= max(1, words - 2):
            return "explain_law"
    if "place" in kinds and re.search(r"\bcounty\b|\bcity\b|\btown\b", q, re.I):
        return "local_place"
    return None


# Below this, Laya's "is this about government" says off topic. Its
# probabilities run low: on the Tier 2 tuning rows real policy topics scored
# 0.04-0.17 and the one plainly off-topic ask 0.00 (2026-09-29).
OFF_TOPIC_FLOOR = 0.03
_GOV_QUESTION = {"gov": {"type": "noul",
                         "instructions": "Is this question about government, laws, politics, elections or public policy?"}}


def on_topic(question):
    """Laya's probability that a question is about government, or None when
    Laya does not load (the caller then assumes it is)."""
    from agents.result_validator_agent import _load_laya
    try:
        return _load_laya().predict(f"Question: {question}", _GOV_QUESTION)["answers"]["gov"]["noul"]
    except Exception as e:                                   # noqa: BLE001 - no model, no verdict
        print(f"[ROUTER] Laya unavailable for the topic check: {type(e).__name__}: {e}")
        return None


def question_intent(question, entities, plate=None, ask=None):
    """One of INTENTS: the rules, else off_topic when Laya says so, else
    find_bills. Returns (intent, how) where how names what decided."""
    got = rule_intent(question, entities, plate, ask)
    if got:
        return got, "rule"
    p = on_topic(question)
    if p is not None and p < OFF_TOPIC_FLOOR:
        return "off_topic", f"laya {p:.2f}"
    return "find_bills", "default" if p is None else f"laya {p:.2f}"


# ── The question structured without a model ──
#
# What the search handlers read (query_type, keywords, Congress window,
# enacted, named act, result count, jurisdiction, intents), from the rules,
# the linked names and the intent. It replaced the Haiku router (route_query)
# on 2026-09-29: a question costs nothing to understand.

# "Laws" alone means legislation as often as enacted law ("Florida abortion
# laws"); a law that passed is said so.
_ENACTED = re.compile(r"\b(enacted|signed( into law)?|became laws?|passed into law|laws? (that )?(passed|signed|enacted)"
                      r"|new laws?)\b", re.I)
_RECENT = re.compile(r"\b(recent(ly)?|latest|newest|this (year|congress|session)|current (congress|session))\b",
                     re.I)
_BROWSE = re.compile(r"^\s*(give me|show me|find me|find)?\s*(a|any|some|random)\s+(bill|law)s?\s*[?.!]*$|"
                     r"^\s*show me something\s*$", re.I)
_TOPIC_STOP = {"vote", "votes", "voted", "voting", "how", "did", "does", "who", "what", "which", "my", "our", "reps",
               "rep", "representative", "representatives", "senator", "senators", "congressman", "record",
               "sponsored", "lobbied", "lobbying", "lobby", "donors", "funds", "funded", "pacs", "pac", "money",
               "spend", "spent", "raised", "states", "state", "passed", "against", "for", "no", "yes", "on", "about",
               "do", "doing", "is", "are", "was", "were", "has", "have", "bills", "bill", "laws", "law", "under",
               "signed", "tell", "me", "explain", "the", "a", "an", "of", "in", "to", "and", "or", "that", "this",
               "trying", "anyone", "anything", "something", "stuff", "give", "show", "find"}


def _count(question):
    q = question.lower()
    if re.search(r"\b(a|one|single) (bill|law)\b|\ban example\b", q):
        return 1
    if re.search(r"\b(a few|some|several)\b", q):
        return 3
    m = re.search(r"\b(\d{1,2}) (bills|laws|results)\b", q)
    return min(int(m.group(1)), 20) if m else 5


def near_state(question):
    """A state name typed a letter or two off ("Viriginia"), or None: one
    long word against the one-word state names. Pure."""
    import difflib
    from agents.ledger_agent import US_STATES
    names = [n for n in US_STATES if " " not in n]
    for w in re.findall(r"[a-z]{6,}", (question or "").lower()):
        hit = difflib.get_close_matches(w, names, n=1, cutoff=0.85)
        if hit:
            return US_STATES[hit[0]]
    return None


def structure_free(question, intent, entities, full_history=False):
    """The structured question the search handlers read, by rule. Pure but
    for the clock (the current Congress)."""
    import datetime as _dt
    from agents.ledger_agent import extract_state
    q = question or ""
    person = next((e for e in entities if e["kind"] == "person"), None)
    committee = next((e for e in entities if e["kind"] == "committee"), None)
    bill = next((e for e in entities if e["kind"] == "bill"), None)
    # A bill's name is the topic ("vote for the infrastructure bill"); a
    # person's, committee's or organization's is not.
    covered = " ".join(e["surface"] for e in entities if e["kind"] != "bill")
    keywords = [w for w in re.findall(r"[a-z0-9][a-z0-9'-]+", q.lower())
                if w not in _TOPIC_STOP and w not in covered.split() and len(w) > 2 and not w.isdigit()]
    current = year_to_congress(_dt.datetime.now().year)
    years = sorted({int(y) for y in re.findall(r"\b((?:19|20)\d\d)\b", q)})
    pres = presidential(q, entities)
    if pres:
        congresses, time_filter, time_range = extract_president_congress(q) or [current], True, "presidential term"
    elif years:
        congresses, time_filter, time_range = sorted({year_to_congress(y) for y in years}), True, f"year:{years[0]}"
    elif _RECENT.search(q):
        congresses, time_filter, time_range = [current, current - 1], True, "last 2 years"
    else:
        congresses, time_filter, time_range = [current, current - 1], False, "last 5 years"
    if full_history:
        congresses = years_to_congress_numbers("last 5 years", all_congresses=True)
    status = "enacted" if _ENACTED.search(q) and intent != "explain_law" else "any"
    named = re.sub(r"^[A-Z][A-Za-z. ]*\d+: ", "", bill["name"]) if bill and intent == "explain_law" else None
    st = extract_state(q) or near_state(q)
    if intent in ("off_topic", "local_place"):
        # A local government this search has no source for is answered as
        # outside it, never with federal bills that mention "county".
        qtype = "off_topic"
    elif intent == "committee" and committee:
        qtype = "committee"
    elif person and not pres and intent in ("person_record", "how_voted") and not keywords and not bill:
        qtype = "member"
    else:
        qtype = "legislation"
    subtype = ("named_entity" if named else "browse" if _BROWSE.search(q) else
               "concept_with_date" if time_filter and not pres else "enacted" if status == "enacted" else "concept")
    ambiguous = [e for e in entities if e.get("ambiguous")]
    structured = {
        "query_type": qtype, "query_subtype": subtype, "named_entity": named, "time_filter": time_filter,
        "confidence": 0.6 if ambiguous else 1.0,
        "ambiguity_reason": (f"{ambiguous[0]['surface']!r} could name more than one: "
                             + ", ".join(c["name"] for c in ambiguous[0]["candidates"][:3]) + ".") if ambiguous else None,
        "entity_name": (person["name"] if person and not pres else committee["name"] if committee else None),
        "keywords": keywords, "topic": " ".join(keywords), "time_range": time_range, "bill_type": "all",
        "result_count": _count(q), "specific_bill": None, "status": status,
        "jurisdiction": "state" if st else "federal", "state_code": st, "congress_numbers": congresses,
        "_intent": intent, "_entities": entities,
    }
    # A bill number inside a longer question ("Virginia HB 1", "tell me about
    # HR 1234"): the fast paths match only a question that is the number.
    import graph
    sm = graph.find_state_bill(st.lower(), q) if st else None
    if sm:
        ident = re.sub(r"\s+", " ", sm.group(1).upper())
        structured.update(specific_bill={"identifier": ident}, _fast_path="state_bill_id", query_type="legislation")
    elif not st:
        from agents.ledger_agent import parse_bill_id
        fb = parse_bill_id(q)
        if fb:
            structured.update(specific_bill={"type": fb["bill_type"], "number": fb["number"],
                                             "congress": fb.get("congress")}, query_type="legislation")
    known = check_known_bills(q)
    if known:
        structured["known_bill_hint"] = known
    if person and not pres and keywords and intent in ("person_record", "how_voted"):
        structured["intents"] = [{"kind": "member", "name": person["name"]}, {"kind": "topic", "label": " ".join(keywords)}]
    else:
        structured["intents"] = intents_from_structured(structured)
    return structured


def route(question, state_code=None, *, get_client, home_state=None, plates=True, allow_graph=True,
          full_history=False, before_congress=None, max_results=None, fresh=False):
    """The routing decision: {"plate": ..., ...}.

    With `plates` (/ledger), classify_question's free checks run first —
    the watch list, the graph's question shapes, elections, a Foundry
    place, a bill number, a local place we have no source for — against
    the reader's `home_state`; any plate but "ledger" is the answer and no
    model is asked. The ledger plate then carries "structured": the
    question structured for `state_code` (the question's own state, when it
    is loaded), by the state fast path, the federal fast path, then Haiku.
    Without `plates` (/search, /state/search) only that last step runs.

    Fail-open on /ledger (a router error leaves "structured" None and the
    page shows an empty ledger); fail-closed without plates, where the
    route turns the error into its own 500."""
    kw = dict(full_history=full_history, before_congress=before_congress, max_results=max_results,
              fresh=fresh, get_client=get_client)
    if not plates:
        return {"plate": "ledger", "structured": structure_question(question, state_code, **kw)}
    from agents.ledger_agent import classify_question
    routed = classify_question(question, home_state, allow_graph=allow_graph)
    if routed.get("plate") != "ledger":
        return routed
    try:
        routed["structured"] = structure_question(question, state_code, **kw)
    except Exception as e:
        print(f"[ROUTER] structuring failed: {type(e).__name__}: {e}")
        routed["structured"] = None
    return routed


def _apply_presidential_term_filter(structured, question):
    """When the question references a president by era, restrict to their Congress numbers."""
    congresses = extract_president_congress(question)
    if congresses:
        structured["congress_numbers"] = congresses
        structured["time_range"] = "presidential term"


def _disambiguate_president_query(structured, question):
    """Ex-presidents have both member records and signed legislation. When the
    user means the *legislation* (e.g. "trump signed border bills"), reclassify
    the member query as legislation. Trump defaults to legislation when
    ambiguous — historically he's queried more about laws than service."""
    if structured.get("query_type") != "member":
        return
    entity = (structured.get("entity_name") or "").lower()
    if not any(p in entity for p in _PRESIDENTS):
        return
    q = question.lower()
    has_pres = any(s in q for s in _PRESIDENTIAL_SIGNALS)
    has_cong = any(s in q for s in _CONGRESSIONAL_SIGNALS)
    if (has_pres and not has_cong) or (not has_cong and "trump" in entity):
        structured["query_type"] = "legislation"
        structured["entity_name"] = None
