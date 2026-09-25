"""The government graph: Fairfax County Board of Supervisors and Congress.

Two Postgres tables (graph_node, graph_edge — DDL in correspondence/db.py)
hold the *skeleton*: jurisdictions, bodies, seats, people, and the edges
between them, every edge time-bounded and carrying the certification of the
record that asserted it. Events stay in their stores; an edge points at its
vote_id / contest_id and the store is read at the leaf.

One ontology, two layers. A county board and a chamber of Congress are the
same shape here: an organization in a jurisdiction, with posts, held by
people, who vote on instruments. `build` reads a Foundry meetings store;
`build_congress` reads the legislators dataset plus a roll-call snapshot.
Both emit the same row dicts and the same predicates.

Everything that decides shape is a pure function over parsed JSON so it can
be tested without a database. `load`, `votes` and `seat_holder` are the only
functions that touch Postgres; `snapshot_congress` is the only one that
touches the network.

Deliberate limits, each reversible in one function: meetings are not nodes
(they are events; `considered` carries the meeting id); a local person is
keyed by seat + surname and a federal one by bioguide id, bridged by the
IDENTITIES table when a human has asserted they are the same; seat/district
seeds for a county are hand-written per source in SOURCES.
"""

import datetime
import json
import pathlib
import re
import sys
import time
import uuid

_HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE / "foundry"))
from harness import member_key  # noqa: E402  — the certify canon; never fork it

STORE_DIR = _HERE / "foundry" / "data" / "store"
DATA_DIR = _HERE / "data"

# The full predicate vocabulary. The loader refuses anything else so the
# graph cannot grow a new relation type by accident.
PREDICATES = ("contains", "has_body", "has_seat", "holds", "represents",
              "sponsored", "voted_on", "considered", "elected_in", "for_seat")

# Ordered weakest → strongest. An answer reports the weakest hop it crossed.
CERTIFICATION_RANK = {"advisory": 0, "ingested": 1, "certified": 2}

# Per-source facts the stores don't carry live in a sidecar next to the
# stores (foundry/data/store/_graph-sources.json): which jurisdiction a store
# is, which elections store seats its members, the statutory term, and the
# human-asserted identities. A county enters the graph by getting an entry
# there, not by editing this file.
_SOURCES_PATH = _HERE / "foundry" / "data" / "store" / "_graph-sources.json"
_CONFIG = json.loads(_SOURCES_PATH.read_text())
SOURCES = _CONFIG["sources"]
STATE_NAMES = _CONFIG["states"]
IDENTITIES = _CONFIG["identities"]
# Every state, DC and territory a member of Congress has sat for, by name.
# STATE_NAMES is the sidecar's list of states to *load*; this is the list of
# states that *exist*, so a node for Texas is never named "TX".
DIVISION_NAMES = {
    "al": "Alabama", "ak": "Alaska", "az": "Arizona", "ar": "Arkansas", "ca": "California",
    "co": "Colorado", "ct": "Connecticut", "de": "Delaware", "fl": "Florida", "ga": "Georgia",
    "hi": "Hawaii", "id": "Idaho", "il": "Illinois", "in": "Indiana", "ia": "Iowa",
    "ks": "Kansas", "ky": "Kentucky", "la": "Louisiana", "me": "Maine", "md": "Maryland",
    "ma": "Massachusetts", "mi": "Michigan", "mn": "Minnesota", "ms": "Mississippi",
    "mo": "Missouri", "mt": "Montana", "ne": "Nebraska", "nv": "Nevada", "nh": "New Hampshire",
    "nj": "New Jersey", "nm": "New Mexico", "ny": "New York", "nc": "North Carolina",
    "nd": "North Dakota", "oh": "Ohio", "ok": "Oklahoma", "or": "Oregon", "pa": "Pennsylvania",
    "ri": "Rhode Island", "sc": "South Carolina", "sd": "South Dakota", "tn": "Tennessee",
    "tx": "Texas", "ut": "Utah", "vt": "Vermont", "va": "Virginia", "wa": "Washington",
    "wv": "West Virginia", "wi": "Wisconsin", "wy": "Wyoming",
    "dc": "District of Columbia", "as": "American Samoa", "gu": "Guam",
    "mp": "Northern Mariana Islands", "pr": "Puerto Rico", "vi": "U.S. Virgin Islands",
    # Former territories that sent delegates (legislators-historical).
    "ol": "Territory of Orleans", "dk": "Dakota Territory", "pi": "Philippine Islands",
}

US = "ocd-division/country:us"
HOUSE_KEY, SENATE_KEY = "us/house", "us/senate"
CHAMBER_NAME = {"house": "U.S. House of Representatives", "senate": "U.S. Senate"}
LEGISLATORS_SOURCE = "legislators-current"
HISTORICAL_SOURCE = "legislators-historical"
# The public files `python graph.py fetch` downloads into data/. Congress
# itself does not publish these as data; the unitedstates project assembles
# them from the Biographical Directory and the clerks.
PUBLIC_DATA_URL = "https://raw.githubusercontent.com/unitedstates/congress-legislators/gh-pages/{}.json"
PUBLIC_FILES = ("legislators-current", "legislators-historical", "executive",
                "committees-current", "committee-membership-current")

_NS = uuid.uuid5(uuid.NAMESPACE_DNS, "nospopuli.org")
_OCD_PREFIX = {"organization": "ocd-organization", "post": "ocd-post",
               "person": "ocd-person"}
# Lowercase tokens that legitimately appear inside a person's name.
_NAME_PARTICLES = {"de", "da", "del", "della", "di", "du", "la", "le", "van",
                   "von", "der", "den", "y", "e"}
# Roll-call vocabularies → the Foundry position vocabulary (schema.POSITIONS),
# so a federal and a county vote answer with the same words.
_POSITION = {"yea": "aye", "aye": "aye", "yes": "aye", "nay": "no", "no": "no",
             "present": "present", "not voting": "absent"}


def node_id(kind, natural_key):
    """OCD-shaped id, deterministic in the natural key so reloads upsert
    instead of duplicating. Jurisdictions never come through here — they use
    their real OCD division id."""
    return f"{_OCD_PREFIX[kind]}/{uuid.uuid5(_NS, f'{kind}:{natural_key}')}"


def _slug(text):
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def _district_slug(district, cfg):
    if district is None or district == cfg["chair_district"]:
        return "chair"
    return _slug(re.sub(r"\s+district$", "", district.strip(), flags=re.I))


def _display_name(raw):
    """ENR writes 'Patrick S. "Pat" Herrity'; the roster writes 'Patrick S.
    Herrity'. Returns (name without the quoted nickname, nickname alias or
    None) so both spellings resolve to one person and the alias survives."""
    m = re.search(r'\s*"([^"]+)"\s*', raw)
    if not m:
        return re.sub(r"\s+", " ", raw).strip(), None
    name = re.sub(r"\s+", " ", raw[:m.start()] + " " + raw[m.end():]).strip()
    tokens = name.replace(",", "").split()
    surname = tokens[-2] if tokens[-1].rstrip(".").upper() in {"JR", "SR", "II", "III", "IV"} \
        and len(tokens) > 1 else tokens[-1]
    return name, f"{m.group(1)} {surname}"


def _person_key(name):
    # member_key keeps a trailing comma ('Bierman,' from 'Bierman, Jr.');
    # harmless for certify's own comparisons, wrong as an identity key.
    return member_key(name.replace(",", " "))


def _looks_like_name(name):
    """The floor the Fairfax roster should have had: 'McKay extended thoughts
    and prayers to the Fairfax family' is a sentence, not a supervisor."""
    tokens = name.replace(",", "").split()
    if not 1 <= len(tokens) <= 5:
        return False
    if len(tokens) == 1 and not (tokens[0][0].isupper() and len(tokens[0]) > 1):
        return False    # a bare surname (Prince William's roster) is a name; 'x' is not
    return not any(t.islower() and t not in _NAME_PARTICLES for t in tokens)


def _cert(record):
    """Store certification → edge certification. A record with no block at
    all (roster members, the legislators file, a roll call) is single-source
    by definition: ingested."""
    status = (record.get("certification") or {}).get("status")
    return "certified" if status == "certified" else "ingested"


def _general_election_date(year, month):
    """Virginia's November general is the first Tuesday after the first
    Monday. Any other month is a special election whose day the ENR store
    doesn't carry; the first of the month is used and the bound says so."""
    first = datetime.date(year, month, 1)
    if month != 11:
        return first.isoformat(), "month"
    first_monday = 1 + (0 - first.weekday()) % 7
    return datetime.date(year, 11, first_monday + 1).isoformat(), "exact"


# ------------------------------------------------------- the accumulator

def _graph():
    return {"nodes": {}, "edges": {}, "gaps": []}


def _node(g, id_, kind, name, props, source_id, source_ref=None):
    g["nodes"][id_] = {"id": id_, "kind": kind, "name": name, "props": props,
                       "source_id": source_id, "source_ref": source_ref}


def _edge(g, src, predicate, dst, valid_from, valid_to, certification,
          source_id, source_ref, jurisdiction, props):
    """jurisdiction is the delete scope: a reload of one county or one
    state's delegation removes exactly the edges tagged with it."""
    assert predicate in PREDICATES, predicate
    key = (src, predicate, dst, source_ref)
    row = {"src": src, "predicate": predicate, "dst": dst,
           "valid_from": valid_from, "valid_to": valid_to,
           "certification": certification, "source_id": source_id,
           "source_ref": source_ref, "props": {"jurisdiction": jurisdiction, **props}}
    if key in g["edges"] and g["edges"][key]["props"] != row["props"]:
        g["gaps"].append(f"conflicting assertions for {predicate} {src} → {dst} "
                         f"in {source_ref}; kept the first")
        return g["edges"][key]
    g["edges"][key] = row
    return row


def _close_double_holds(g, holds_by_post, post_label):
    """Two open holders of one seat is a contradiction the resolver cannot
    answer. Close the earlier one the day before the later one begins, and
    say that the bound is inferred. Leaving both open would be the silent
    kind of wrong."""
    for post, rows in holds_by_post.items():
        rows.sort(key=lambda r: r["valid_from"])
        for earlier, later in zip(rows, rows[1:]):
            if earlier["valid_to"] is None:
                day_before = (datetime.date.fromisoformat(later["valid_from"])
                              - datetime.timedelta(days=1)).isoformat()
                earlier["valid_to"] = day_before
                earlier["props"]["bound_to"] = "inferred"
                earlier["props"]["inferred_from"] = (
                    f"successor first observed {later['valid_from']}")
                g["gaps"].append(f"{g['nodes'][earlier['src']]['name']}'s hold on "
                                 f"{post_label[post]} closed at {day_before} by "
                                 f"inference from the successor")


# ---------------------------------------------------------------- identity

def _first_initial(name):
    tokens = name.replace(",", " ").split()
    return tokens[0][0].upper() if tokens and len(tokens) > 1 else ""


def _same_given_name(a, b):
    """'Pat' and 'Patrick S.' are one person; 'Pat' and 'Paul' are not.
    Given names agree when one is a prefix of the other (nicknames are
    usually truncations) — a full-name collision on surname + initial that
    fails this stays two people, and is reported."""
    ga, gb = a.replace(",", " ").split()[0].rstrip(".").lower(), b.replace(",", " ").split()[0].rstrip(".").lower()
    return ga.startswith(gb) or gb.startswith(ga)


def resolve_members(store, cfg):
    """Roster → people. Returns (persons, rejects).

    persons: [person], each a dict with name, aliases, key (the natural key
    inside the county: surname canon + first initial), seat (from the
    roster's district or role, or None when the roster carries neither —
    Loudoun's does not, and the contest that seated them supplies it later),
    raw_names (every spelling the store uses), first_seen (earliest meeting
    date any spelling appears).
    rejects: [(raw name, reason)] — never loaded, always reported.

    Identity is county + surname + first initial, never surname alone
    ('Smith' is not unique across a state) and never seat + surname (a
    person who changes seats is still one person).
    """
    first_seen = {}
    for m in store.get("meetings", {}).values():
        for raw in (m.get("attendance") or {}):
            first_seen[raw] = min(first_seen.get(raw, "9999"), m["date"])
    dates = {m["meeting_id"]: m["date"] for m in store.get("meetings", {}).values()}
    for ve in store.get("vote_events", {}).values():
        d = dates.get(ve["meeting_id"])
        if d:
            for p in ve.get("positions", []):
                first_seen[p["member"]] = min(first_seen.get(p["member"], "9999"), d)

    by_key, rejects = {}, []

    def surname_matches(surname):
        return [k for k in by_key if k.split("/")[0] == surname]

    # Full names first, so a bare surname ('Allen') can find the one person
    # it belongs to — or be refused when there are three Allens.
    members = sorted(store.get("members", {}).values(),
                     key=lambda m: (len(m["name"].replace(",", " ").split()) == 1, m["name"]))
    for m in members:
        raw = m["name"]
        if not _looks_like_name(raw):
            rejects.append((raw, "not a person's name (failed the name floor)"))
            continue
        role = (m.get("role") or "").lower()
        seat = "chair" if role.startswith("chair") else \
            _district_slug(m["district"], cfg) if m.get("district") else None
        name, alias = _display_name(raw)
        surname, initial = _person_key(name).lower(), _first_initial(name).lower()
        key = f"{surname}/{initial}"
        person = by_key.get(key)
        if not initial:
            hits = surname_matches(surname)
            if len(hits) == 1:
                key, person = hits[0], by_key[hits[0]]
            elif len(hits) > 1:
                rejects.append((raw, f"ambiguous: {len(hits)} people named {name} on this roster "
                                     f"({', '.join(by_key[h]['name'] for h in hits)})"))
                continue
        elif person and not _same_given_name(person["name"], name):
            rejects.append((raw, f"collides with {person['name']!r} on surname and initial "
                                 f"but is a different given name; not merged"))
            key = f"{key}/{name.split()[0].lower()}"
            person = by_key.get(key)
        person = person or by_key.setdefault(key, {
            "name": name, "aliases": [], "key": key, "seat": seat,
            "raw_names": [], "first_seen": None})
        person["raw_names"].append(raw)
        person["seat"] = person["seat"] or seat
        if alias:
            person["aliases"].append(alias)
        if len(name) > len(person["name"]):        # 'Patrick S.' over 'Pat'
            person["aliases"].append(person["name"])
            person["name"] = name
        elif name != person["name"]:
            person["aliases"].append(name)
        seen = [first_seen[r] for r in person["raw_names"] if r in first_seen]
        person["first_seen"] = min(seen) if seen else None
    persons = list(by_key.values())
    for p in persons:
        p["aliases"] = sorted(set(p["aliases"]) - {p["name"]})
    return persons, rejects


# ------------------------------------------------------------ build: county

def build(source_id, store, contests, summaries):
    """Parsed Foundry stores → (nodes, edges, gaps). Pure; no I/O.

    nodes/edges are lists of dicts shaped exactly like the table rows.
    gaps are sentences a reader can act on: what is missing and why.
    """
    cfg = SOURCES[source_id]
    state_id = f"{US}/state:{cfg['state']}"
    county_id = f"{state_id}/county:{cfg['county']}"
    body_id = node_id("organization", f"{cfg['state']}/{cfg['county']}/board-of-supervisors")
    g = _graph()
    seed = {"derived": "seed"}

    def edge(src, predicate, dst, valid_from, valid_to, certification, src_id, source_ref, props):
        return _edge(g, src, predicate, dst, valid_from, valid_to, certification,
                     src_id, source_ref, county_id, props)

    _node(g, state_id, "jurisdiction", STATE_NAMES.get(cfg["state"], cfg["state"].upper()),
          {"level": "state"}, source_id)
    _node(g, county_id, "jurisdiction", cfg["county_name"],
          {"level": "county", "jurisdiction": county_id}, source_id)
    _node(g, body_id, "organization", cfg["body_name"],
          {"natural_key": f"{cfg['state']}/{cfg['county']}/board-of-supervisors",
           "jurisdiction": county_id}, source_id)
    edge(state_id, "contains", county_id, None, None, "ingested", source_id, "seed", seed)
    edge(county_id, "has_body", body_id, None, None, "ingested", source_id, "seed", seed)

    persons, rejects = resolve_members(store, cfg)
    for raw, why in rejects:
        g["gaps"].append(f"roster entry {raw!r} not loaded: {why}")
    my_contests = [c for c in contests
                   if c.get("jurisdiction") == cfg["election_jurisdiction"]
                   and c.get("office") == cfg["election_office"]]

    # Seats: the union of what the roster and the contests name. A district
    # only one side knows about is still a seat; the other side is the gap.
    seats = {}
    for m in store.get("members", {}).values():
        if m.get("district"):
            seats[_district_slug(m["district"], cfg)] = m["district"]
    if any(p["seat"] == "chair" for p in persons):
        seats["chair"] = "Chair"
    for c in my_contests:
        slug = _district_slug(c.get("district"), cfg)
        seats.setdefault(slug, "Chair" if slug == "chair" else c["district"])

    post_ids = {}
    for seat, district in sorted(seats.items()):
        post_key = f"{cfg['state']}/{cfg['county']}/bos/{seat}"
        post_ids[seat] = pid = node_id("post", post_key)
        if seat == "chair":
            division, label = county_id, "Chair"
        else:
            division = f"{county_id}/council_district:{seat}"
            label = f"Supervisor, {district}"
            _node(g, division, "jurisdiction", district,
                  {"level": "district", "jurisdiction": county_id}, source_id)
            edge(county_id, "contains", division, None, None, "ingested",
                 source_id, "seed", seed)
        _node(g, pid, "post", f"{label}, {cfg['body_name']}",
              {"natural_key": post_key, "role": label, "jurisdiction": county_id}, source_id)
        edge(body_id, "has_seat", pid, None, None, "ingested", source_id, "seed", seed)
        edge(pid, "represents", division, None, None, "ingested", source_id, "seed", seed)

    def person_node(key, name, aliases, source_ref):
        nk = f"{cfg['state']}/{cfg['county']}/{key}"
        identity = IDENTITIES.get(nk)
        pid = node_id("person", identity or nk)
        props = {"natural_key": nk, "aliases": aliases, "jurisdiction": county_id}
        if identity:
            props["identity"] = identity
            props["identity_asserted_by"] = "manual (_graph-sources.json identities)"
        if pid in g["nodes"]:
            g["nodes"][pid]["props"]["aliases"] = sorted(
                set(g["nodes"][pid]["props"]["aliases"]) | set(aliases))
        else:
            _node(g, pid, "person", name, props, source_id, source_ref)
        return pid

    person_ids, raw_to_person, seat_of = {}, {}, {}
    for p in persons:
        pid = person_node(p["key"], p["name"], p["aliases"], p["raw_names"][0])
        person_ids[p["key"]] = pid
        seat_of[pid] = p["seat"]
        for raw in p["raw_names"]:
            raw_to_person[raw] = pid

    # Instruments and the votes on them. A store with agenda items votes on
    # the item; a store without (Loudoun records motions, not items) votes
    # on the motion itself, which is then the instrument. Topic is Haiku's
    # reading of the title (item-summaries.json); it travels as a property
    # and is labelled derived so no answer can present it as the clerk's.
    meetings = store.get("meetings", {})
    for item in store.get("agenda_items", {}).values():
        meeting = meetings.get(item["meeting_id"])
        if meeting is None:
            g["gaps"].append(f"agenda item {item['item_id']} names a meeting not in the store")
            continue
        iid = f"instrument/{item['item_id']}"
        props = {"instrument_type": "agenda_item", "action": item.get("action"),
                 "result": item.get("result"), "meeting_id": item["meeting_id"],
                 "date": meeting["date"], "jurisdiction": county_id}
        summary = summaries.get(item["item_id"]) or {}
        if summary.get("topic"):
            props["topic"] = summary["topic"]
            props["topic_derived_by"] = summary.get("derived_by", "model")
        _node(g, iid, "instrument", item["title"], props, source_id, item["item_id"])
        edge(body_id, "considered", iid, meeting["date"], meeting["date"],
             _cert(meeting), source_id, item["meeting_id"], {})

    unresolved = {}
    for ve in store.get("vote_events", {}).values():
        meeting = meetings.get(ve["meeting_id"])
        if meeting is None:
            g["gaps"].append(f"vote {ve['vote_id']} names a meeting not in the store")
            continue
        if ve.get("item_id"):
            iid = f"instrument/{ve['item_id']}"
            if iid not in g["nodes"]:
                g["gaps"].append(f"vote {ve['vote_id']} names agenda item {ve['item_id']} not in the store")
                continue
        elif ve.get("motion"):
            iid = f"instrument/{ve['vote_id']}"
            _node(g, iid, "instrument", ve["motion"].strip(),
                  {"instrument_type": "motion", "result": ve.get("result"),
                   "meeting_id": ve["meeting_id"], "date": meeting["date"],
                   "jurisdiction": county_id}, source_id, ve["vote_id"])
            edge(body_id, "considered", iid, meeting["date"], meeting["date"],
                 _cert(ve), source_id, ve["vote_id"], {})
        else:
            g["gaps"].append(f"vote {ve['vote_id']} has neither an agenda item nor a motion")
            continue
        for pos in ve.get("positions", []):
            pid = raw_to_person.get(pos["member"])
            if pid is None:
                unresolved[pos["member"]] = unresolved.get(pos["member"], 0) + 1
                continue
            edge(pid, "voted_on", iid, meeting["date"], meeting["date"],
                 _cert(ve), source_id, ve["vote_id"],
                 {"position": pos["position"], "vote_id": ve["vote_id"],
                  "meeting_id": ve["meeting_id"]})
    for raw, n in unresolved.items():
        g["gaps"].append(f"{n} vote position(s) by {raw!r} dropped: not a loaded person")

    # Elections. A contest is for this body (the caller filtered by office,
    # so a school-board win in the same district never seats anyone here)
    # and names a winner; the winner is matched on surname + initial within
    # the county. A winner the roster has never seen becomes a person too.
    holds, seated = {}, set()
    for c in my_contests:
        seat = _district_slug(c.get("district"), cfg)
        cid = f"contest/{c['contest_id']}"
        date, bound = _general_election_date(int(c["year"]), int(c.get("month") or 11))
        _node(g, cid, "contest",
              f"{c['office']}, {c.get('district') or cfg['county_name']}, {c['year']}",
              {"year": c["year"], "month": c.get("month"), "district": c.get("district"),
               "winner_names": c.get("winner_names", []), "source_url": c.get("source_url"),
               "jurisdiction": county_id}, cfg["elections_source"], c["contest_id"])
        edge(cid, "for_seat", post_ids[seat], date, date, _cert(c),
             cfg["elections_source"], c["contest_id"], {"bound_from": bound})
        for winner in c.get("winner_names", []):
            name, alias = _display_name(winner)
            key = f"{_person_key(name).lower()}/{_first_initial(name).lower()}"
            pid = person_ids.get(key)
            if pid is None:
                # A surname-only roster ('Gordy') meets a full-name contest
                # ('Thomas T. "Tom" Gordy'): match when the surname is unique.
                hits = [k for k in person_ids if k.split("/")[0] == key.split("/")[0]]
                if len(hits) == 1:
                    key, pid = hits[0], person_ids[hits[0]]
                    if len(name) > len(g["nodes"][pid]["name"]):
                        g["nodes"][pid]["props"]["aliases"].append(g["nodes"][pid]["name"])
                        g["nodes"][pid]["name"] = name
            if pid is None:
                pid = person_ids[key] = person_node(key, name, [alias] if alias else [], c["contest_id"])
                g["nodes"][pid]["source_id"] = cfg["elections_source"]
            else:
                # The contest's spelling ('Juli E. Briskman') joins the
                # roster's ('Juli Briskman') as an alias, so either finds her.
                known = g["nodes"][pid]["props"]["aliases"]
                for spelling in (alias, name):
                    if spelling and spelling != g["nodes"][pid]["name"] and spelling not in known:
                        known.append(spelling)
            if seat_of.get(pid) and seat_of[pid] != seat:
                g["gaps"].append(f"{name} won {c.get('district')} but the roster seats them "
                                 f"at {seats.get(seat_of[pid])}; both holds recorded")
            seat_of.setdefault(pid, seat)
            edge(pid, "elected_in", cid, date, date, _cert(c),
                 cfg["elections_source"], c["contest_id"], {"bound_from": bound})
            row = edge(pid, "holds", post_ids[seat], cfg["term_start"], None, _cert(c),
                       cfg["elections_source"], c["contest_id"],
                       {"bound_from": "term_statute", "term_expires": cfg["term_expires"]})
            holds.setdefault(seat, []).append(row)
            seated.add(pid)

    # Roster members no contest seated: they hold their roster seat from
    # the first meeting we saw them at. Observed, not asserted — and the gap
    # is named. A roster member with no seat at all can vote but holds
    # nothing, and that too is a gap.
    for p in persons:
        pid = person_ids[p["key"]]
        if pid in seated:
            continue
        seat = seat_of.get(pid)
        if seat is None:
            g["gaps"].append(f"{p['name']} is on the roster with no district, and no contest "
                             f"in {cfg['elections_source']} names them; they vote but hold no seat")
            continue
        g["gaps"].append(f"{p['name']} holds {seats[seat]} on the roster but "
                         f"{cfg['elections_source']} has no contest seating them "
                         f"(a special election not on disk?)")
        if p["first_seen"] is None:
            continue
        row = edge(pid, "holds", post_ids[seat], p["first_seen"], None, "ingested",
                   source_id, p["raw_names"][0], {"bound_from": "observed"})
        holds.setdefault(seat, []).append(row)

    _close_double_holds(g, holds, seats)
    return list(g["nodes"].values()), list(g["edges"].values()), g["gaps"]


# ---------------------------------------------------------- build: congress

def _parse_instrument(chamber, vote):
    """The roll call's legislative instrument as (type, number) in the
    Congress.gov vocabulary (hr, hres, hjres, hconres, s, sres, sjres,
    sconres; pn for nominations, whose numbers can be batched: PN12-1), or
    None for a vote on nothing — quorum calls, the election of the Speaker,
    motions to adjourn. A Senate vote on an amendment is a vote on the bill
    it amends; the amendment number rides on the edge."""
    if chamber == "house":
        legis = (vote.get("legis_num") or "").strip()
        m = re.match(r"^([A-Z][A-Z .]*?)\s*(\d+)$", legis)
        if not m:
            return None
        return "".join(m.group(1).split()).lower(), m.group(2)
    doc_type = (vote.get("document_type") or "").replace(".", "").replace(" ", "").lower()
    number = (vote.get("document_number") or "").strip()
    if not number and vote.get("amendment_to"):
        m = re.match(r"^([A-Za-z.]+)\s*(\d+)$", vote["amendment_to"].strip())
        if not m:
            return None
        doc_type, number = m.group(1).replace(".", "").lower(), m.group(2)
    if not doc_type or not re.match(r"^\d+(-\d+)?$", number):
        return None
    return doc_type, number


def build_congress(legislators, snapshots, states=None, today=None):
    """legislators-current.json + roll-call snapshots → (nodes, edges, gaps).

    states: iterable of two-letter codes to load (a delegation), or None
    for every member. Pure; no I/O.
    """
    today = today or datetime.date.today().isoformat()
    states = {s.upper() for s in states} if states else None
    g = _graph()
    house_id = node_id("organization", HOUSE_KEY)
    senate_id = node_id("organization", SENATE_KEY)
    body_of = {"house": house_id, "senate": senate_id}
    _node(g, US, "jurisdiction", "United States", {"level": "country"}, LEGISLATORS_SOURCE)
    for chamber, bid in body_of.items():
        _node(g, bid, "organization", CHAMBER_NAME[chamber],
              {"natural_key": f"us/{chamber}", "chamber": chamber, "jurisdiction": US},
              LEGISLATORS_SOURCE)
        _edge(g, US, "has_body", bid, None, None, "ingested", LEGISLATORS_SOURCE, "seed",
              US, {"derived": "seed"})

    def state_div(code):
        return f"{US}/state:{code.lower()}"

    def post_for(term):
        """One seat. House seats are districts (at-large is cd:1 in OCD);
        Senate seats are the three staggered classes."""
        st = term["state"]
        if term["type"] == "sen":
            key = f"us/senate/{st.lower()}/class:{term.get('class')}"
            label = f"U.S. Senator, {st} (class {term.get('class')})"
            division, chamber = state_div(st), "senate"
        elif term.get("district") == -1:
            # The historical file writes -1 when the district is not
            # recorded (general-ticket states, early Congresses). One post
            # per state holds them all; a district number would be a guess.
            key = f"us/house/{st.lower()}/cd:unrecorded"
            label = f"U.S. Representative, {st} (district not recorded)"
            division, chamber = state_div(st), "house"
        else:
            cd = term.get("district") or 1
            key = f"us/house/{st.lower()}/cd:{cd}"
            label = f"U.S. Representative, {st}-{cd}"
            division, chamber = f"{state_div(st)}/cd:{cd}", "house"
        pid = node_id("post", key)
        if pid not in g["nodes"]:
            if state_div(st) not in g["nodes"]:
                _node(g, state_div(st), "jurisdiction", DIVISION_NAMES.get(st.lower(), st),
                      {"level": "state", "jurisdiction": state_div(st)}, LEGISLATORS_SOURCE)
                _edge(g, US, "contains", state_div(st), None, None, "ingested",
                      LEGISLATORS_SOURCE, "seed", state_div(st), {"derived": "seed"})
            if division not in g["nodes"] and division != state_div(st):
                _node(g, division, "jurisdiction", f"{st}-{term.get('district') or 1}",
                      {"level": "district", "jurisdiction": state_div(st)}, LEGISLATORS_SOURCE)
                _edge(g, state_div(st), "contains", division, None, None, "ingested",
                      LEGISLATORS_SOURCE, "seed", state_div(st), {"derived": "seed"})
            _node(g, pid, "post", f"{label}, {CHAMBER_NAME[chamber]}",
                  {"natural_key": key, "role": label, "chamber": chamber,
                   "jurisdiction": state_div(st)}, LEGISLATORS_SOURCE)
            _edge(g, body_of[chamber], "has_seat", pid, None, None, "ingested",
                  LEGISLATORS_SOURCE, "seed", state_div(st), {"derived": "seed"})
            _edge(g, pid, "represents", division, None, None, "ingested",
                  LEGISLATORS_SOURCE, "seed", state_div(st), {"derived": "seed"})
        return pid

    by_bioguide, by_lis, holds, post_label = {}, {}, {}, {}
    for leg in legislators:
        terms = [t for t in leg["terms"] if states is None or t["state"] in states]
        if not terms:
            continue
        ids = leg.get("id", {})
        bioguide = ids.get("bioguide")
        if not bioguide:
            g["gaps"].append(f"{leg['name'].get('official_full')} has no bioguide id; skipped")
            continue
        current = leg["terms"][-1]
        home = state_div(current["state"])
        source = leg.get("_source", LEGISLATORS_SOURCE)
        nk = f"bioguide/{bioguide}"
        pid = node_id("person", nk)
        name = leg["name"].get("official_full") or \
            f"{leg['name'].get('first', '')} {leg['name'].get('last', '')}".strip()
        aliases = []
        if leg["name"].get("nickname"):
            aliases.append(f"{leg['name']['nickname']} {leg['name']['last']}")
        _node(g, pid, "person", name,
              {"natural_key": nk, "aliases": aliases, "bioguide": bioguide,
               "lis": ids.get("lis"), "party": current.get("party"),
               "external_ids": {k: ids[k] for k in ("govtrack", "opensecrets", "fec",
                                                    "wikidata") if k in ids},
               "jurisdiction": home}, source, bioguide)
        by_bioguide[bioguide] = pid
        if ids.get("lis"):
            by_lis[ids["lis"]] = pid
        for t in terms:
            post = post_for(t)
            post_label[post] = g["nodes"][post]["props"]["role"]
            expired = t.get("end") and t["end"] <= today
            props = {"bound_from": "exact", "party": t.get("party")}
            if expired:
                props["bound_to"] = "exact"
            else:
                props["term_expires"] = t.get("end")
            row = _edge(g, pid, "holds", post, t["start"], t["end"] if expired else None,
                        "ingested", source, f"{bioguide}/{t['start']}",
                        state_div(t["state"]), props)
            holds.setdefault(post, []).append(row)
    _close_double_holds(g, holds, post_label)

    # Roll calls. A member's id in the House record is the bioguide id; in
    # the Senate record it is the LIS id, bridged through the legislators
    # file. Votes by anyone outside the selected delegation are simply not
    # this load's business; votes by an id nobody has are a gap.
    skipped, unknown, considered_by = {}, {}, {}

    def lookup_bioguide(bid):
        pid = by_bioguide.get(bid)
        if pid is None and states is None:
            unknown[bid] = unknown.get(bid, 0) + 1
        return pid

    for snap in snapshots:
        if not snap.get("instruments"):
            g["gaps"].append(f"snapshot {snap.get('meta', {}).get('congress')}/{snap.get('meta', {}).get('session')} "
                             f"has no sponsor records (snapshot ran without CONGRESS_API_KEY); no `sponsored` edges from it")
        for v in snap.get("votes", []):
            chamber = v["chamber"]
            inst = _parse_instrument(chamber, v)
            if inst is None:
                skipped[chamber] = skipped.get(chamber, 0) + 1
                continue
            itype, number = inst
            iid = f"instrument/us/{v['congress']}/{itype}/{number}"
            label = v.get("legis_num") or v.get("document_name") or f"{itype} {number}"
            rec = (snap.get("instruments") or {}).get(f"{itype}/{number}") or {}
            if iid not in g["nodes"]:
                props = {"instrument_type": itype, "congress": v["congress"], "number": number,
                         "jurisdiction": US}
                if rec.get("policy_area"):
                    # Congress.gov's own subject, not a model's reading.
                    props["topic"] = rec["policy_area"]
                    props["topic_derived_by"] = "congress.gov policyArea"
                if rec.get("introduced"):
                    props["introduced"] = rec["introduced"]
                name = f"{label}: {rec.get('title') or v.get('description') or ''}".strip(": ")
                _node(g, iid, "instrument", name, props, v["source_id"], v["vote_id"])
                # Who wrote it. Instantaneous edges on the date each name
                # went on the bill; a withdrawn cosponsor keeps the edge and
                # the withdrawal date, because they did sign it once.
                ref = f"us/{v['congress']}/{itype}/{number}/sponsors"
                for who in rec.get("sponsors", []):
                    pid = lookup_bioguide(who)
                    if pid:
                        _edge(g, pid, "sponsored", iid, rec.get("introduced"), rec.get("introduced"),
                              "ingested", "congress.gov", ref, g["nodes"][pid]["props"]["jurisdiction"],
                              {"role": "sponsor"})
                for co in rec.get("cosponsors", []):
                    pid = lookup_bioguide(co["id"])
                    if pid:
                        _edge(g, pid, "sponsored", iid, co.get("date"), co.get("date"),
                              "ingested", "congress.gov", ref, g["nodes"][pid]["props"]["jurisdiction"],
                              {"role": "original cosponsor" if co.get("original") else "cosponsor",
                               "withdrawn": co.get("withdrawn")})
            _edge(g, body_of[chamber], "considered", iid, v["date"], v["date"], "ingested",
                  v["source_id"], v["vote_id"], US,
                  {"question": v.get("question"), "result": v.get("result"),
                   "roll": v["roll"], "chamber": chamber, "amendment": v.get("amendment")})
            lookup = by_lis if v.get("id_kind") == "lis" else by_bioguide
            for position, ids in v.get("positions", {}).items():
                for mid in ids:
                    pid = lookup.get(mid)
                    if pid is None:
                        if states is None:
                            unknown[mid] = unknown.get(mid, 0) + 1
                        continue
                    _edge(g, pid, "voted_on", iid, v["date"], v["date"], "ingested",
                          v["source_id"], v["vote_id"],
                          g["nodes"][pid]["props"]["jurisdiction"],
                          # roll and result are the roll call's, not the member's: the
                          # `considered` edge carries them once instead of 400 times.
                          {"position": position, "vote_id": v["vote_id"], "chamber": chamber,
                           "question": v.get("question"), "amendment": v.get("amendment")})
    # A member recorded voting on a date held the seat that day, and the
    # clerk who recorded it is independent of the legislators file that
    # asserted the term. That affirms the assertion (the term record), so
    # the whole holds edge is certified, per assertion — not a per-day
    # patchwork. The bounds keep their own precision; certification says the
    # term is real, not that its dates are.
    voted_days = {}
    for e in g["edges"].values():
        if e["predicate"] == "voted_on":
            voted_days.setdefault(e["src"], []).append((e["valid_from"], e["source_ref"], e["source_id"]))
    certified_holds = 0
    for rows in holds.values():
        for h in rows:
            end = h["valid_to"] or "9999"
            hits = sorted(d for d in voted_days.get(h["src"], []) if h["valid_from"] <= d[0] <= end)
            if hits:
                h["certification"] = "certified"
                h["props"]["certified_by"] = (f"cross-source: {len(hits)} roll call(s) by this member "
                                              f"inside the term, first {hits[0][1]} on {hits[0][0]} "
                                              f"({hits[0][2]}), affirm the term")
                certified_holds += 1
    if certified_holds:
        g["gaps"].append(f"{certified_holds} federal term(s) certified by the clerks' roll calls; "
                         f"the rest have no vote inside them on disk")
    for chamber, n in skipped.items():
        g["gaps"].append(f"{n} {chamber} roll call(s) had no legislative instrument "
                         f"(quorum calls, Speaker elections, motions) and were not loaded")
    if unknown:
        g["gaps"].append(f"{sum(unknown.values())} vote position(s) by {len(unknown)} member "
                         f"id(s) not in the legislators files dropped")
    return list(g["nodes"].values()), list(g["edges"].values()), g["gaps"]


# ---------------------------------------------------------------- snapshot

def _house_url(year, roll):
    return f"https://clerk.house.gov/evs/{year}/roll{roll:03d}.xml"


def _senate_url(congress, session, roll):
    return (f"https://www.senate.gov/legislative/LIS/roll_call_votes/"
            f"vote{congress}{session}/vote_{congress}_{session}_{roll:05d}.xml")


def parse_house_roll(xml_bytes, congress, session, url):
    import xml.etree.ElementTree as ET
    root = ET.fromstring(xml_bytes)
    md = root.find("vote-metadata")
    text = lambda tag: (md.findtext(tag) or "").strip()  # noqa: E731
    roll = int(text("rollcall-num"))
    date = datetime.datetime.strptime(text("action-date"), "%d-%b-%Y").date().isoformat()
    positions = {}
    for rv in root.findall(".//recorded-vote"):
        leg, vote = rv.find("legislator"), rv.find("vote")
        if leg is None or vote is None or not leg.get("name-id"):
            continue
        pos = _POSITION.get((vote.text or "").strip().lower(), "absent")
        positions.setdefault(pos, []).append(leg.get("name-id"))
    return {"vote_id": f"us/{congress}/{session}/house/{roll}", "chamber": "house",
            "congress": congress, "session": session, "roll": roll, "date": date,
            "legis_num": text("legis-num") or None, "question": text("vote-question"),
            "result": text("vote-result"), "description": text("vote-desc"),
            "id_kind": "bioguide", "positions": positions,
            "source_id": "clerk.house.gov", "source_url": url}


def parse_senate_roll(xml_bytes, congress, session, url):
    import xml.etree.ElementTree as ET
    root = ET.fromstring(xml_bytes)
    text = lambda tag: (root.findtext(tag) or "").strip()  # noqa: E731
    roll = int(text("vote_number"))
    date = datetime.datetime.strptime(text("vote_date").split(",")[0] + ","
                                      + text("vote_date").split(",")[1], "%B %d, %Y").date()
    doc, am = root.find("document"), root.find("amendment")
    amdt = lambda tag: (am.findtext(tag) or "").strip() if am is not None else ""  # noqa: E731
    positions = {}
    for m in root.findall(".//member"):
        lis = (m.findtext("lis_member_id") or "").strip()
        if not lis:
            continue
        pos = _POSITION.get((m.findtext("vote_cast") or "").strip().lower(), "absent")
        positions.setdefault(pos, []).append(lis)
    return {"vote_id": f"us/{congress}/{session}/senate/{roll}", "chamber": "senate",
            "congress": congress, "session": session, "roll": roll, "date": date.isoformat(),
            "document_type": (doc.findtext("document_type") or "").strip() if doc is not None else "",
            "document_number": (doc.findtext("document_number") or "").strip() if doc is not None else "",
            "document_name": (doc.findtext("document_name") or "").strip() if doc is not None else "",
            "question": text("question"), "result": text("vote_result"),
            "description": text("vote_title") or text("vote_document_text"),
            "amendment": amdt("amendment_number") or None,
            "amendment_to": amdt("amendment_to_document_number") or None,
            "amendment_purpose": amdt("amendment_purpose") or None,
            "id_kind": "lis", "positions": positions,
            "source_id": "senate.gov", "source_url": url}


def snapshot_congress(congress, session, year, out_path, max_misses=3, pause=0.15):
    """Fetch every roll call of one session from the two clerks' XML and
    write one JSON file. Numbering is sequential; the run ends after
    max_misses consecutive missing numbers in a chamber. The file is the
    store: the loader never touches the network."""
    import requests
    s = requests.Session()
    s.headers["User-Agent"] = "NosPopuli graph snapshot (nospopuli.org)"
    votes, errors = [], []
    for chamber in ("house", "senate"):
        roll, misses = 1, 0
        while misses < max_misses:
            url = _house_url(year, roll) if chamber == "house" else _senate_url(congress, session, roll)
            try:
                r = s.get(url, timeout=20, allow_redirects=False)
            except requests.RequestException as e:
                errors.append(f"{url}: {e}")
                misses += 1
                roll += 1
                continue
            if r.status_code != 200:
                misses += 1
                roll += 1
                continue
            misses = 0
            try:
                parse = parse_house_roll if chamber == "house" else parse_senate_roll
                votes.append(parse(r.content, congress, session, url))
            except Exception as e:  # one malformed record must not sink the run
                errors.append(f"{url}: {type(e).__name__}: {e}")
            roll += 1
            time.sleep(pause)
    instruments, sponsor_errors = fetch_sponsors(votes, s, congress)
    errors += sponsor_errors
    out = {"meta": {"congress": congress, "session": session, "year": year,
                    "fetched": datetime.datetime.now().isoformat(timespec="seconds"),
                    "counts": {c: sum(1 for v in votes if v["chamber"] == c)
                               for c in ("house", "senate")},
                    "errors": errors},
           "votes": votes, "instruments": instruments}
    out["meta"]["counts"]["instruments"] = len(instruments)
    pathlib.Path(out_path).write_text(json.dumps(out, separators=(",", ":")))
    return out["meta"]


_BILL_TYPES = {"hr", "s", "hres", "sres", "hjres", "sjres", "hconres", "sconres"}


def fetch_sponsors(votes, session_, congress, pause=0.1):
    """Congress.gov's record of who introduced and cosponsored each bill the
    session voted on: title, policy area, sponsor, cosponsors with dates.
    Needs CONGRESS_API_KEY; without it the snapshot carries votes only and
    says so. Returns ({"hr/5184": {...}}, errors)."""
    import os
    key = os.getenv("CONGRESS_API_KEY")
    wanted = {}
    for v in votes:
        inst = _parse_instrument(v["chamber"], v)
        if inst and inst[0] in _BILL_TYPES:
            wanted[f"{inst[0]}/{inst[1]}"] = inst
    if not key:
        return {}, [f"CONGRESS_API_KEY not set: {len(wanted)} bill(s) have no sponsor record"]
    out, errors = {}, []
    for label, (itype, number) in sorted(wanted.items()):
        base = f"https://api.congress.gov/v3/bill/{congress}/{itype}/{number}"
        try:
            r = session_.get(base, params={"api_key": key, "format": "json"}, timeout=20)
            if r.status_code != 200:
                errors.append(f"{base}: HTTP {r.status_code}")
                continue
            bill = r.json().get("bill", {})
            rec = {"title": bill.get("title"), "introduced": bill.get("introducedDate"),
                   "policy_area": (bill.get("policyArea") or {}).get("name"),
                   "sponsors": [sp.get("bioguideId") for sp in bill.get("sponsors", []) if sp.get("bioguideId")],
                   "cosponsors": []}
            url, params = base + "/cosponsors", {"api_key": key, "format": "json", "limit": 250}
            while url:
                r = session_.get(url, params=params, timeout=20)
                if r.status_code != 200:
                    errors.append(f"{url}: HTTP {r.status_code}")
                    break
                page = r.json()
                rec["cosponsors"] += [{"id": c.get("bioguideId"), "date": c.get("sponsorshipDate"),
                                       "original": bool(c.get("isOriginalCosponsor")),
                                       "withdrawn": c.get("sponsorshipWithdrawnDate")}
                                      for c in page.get("cosponsors", []) if c.get("bioguideId")]
                url, params = (page.get("pagination") or {}).get("next"), {"api_key": key}
            out[label] = rec
        except Exception as e:
            errors.append(f"{base}: {type(e).__name__}: {e}")
        time.sleep(pause)
    return out, errors


# ---------------------------------------------------------------- temporal

def close_holds_across(edges):
    """A person cannot hold two of these seats at once. When an open or
    inferred hold has another hold by the same person on a different post
    beginning inside it with an exact start, the earlier one closes the day
    before — that start is better evidence than a successor's first
    appearance (Walkinshaw left Braddock for VA-11 on 2025-09-10; the county
    loader alone could only see his successor in January). Mutates the
    `holds` rows in place; returns the rows it changed. Pure; runs over the
    union of every loaded source, so `load` applies it after each load."""
    holds = [e for e in edges if e["predicate"] == "holds"]
    by_person = {}
    for h in holds:
        by_person.setdefault(h["src"], []).append(h)
    changed = []
    for rows in by_person.values():
        for h in rows:
            if h["valid_to"] is not None and h["props"].get("bound_to") != "inferred":
                continue
            starts = sorted(o["valid_from"] for o in rows
                            if o is not h and o["dst"] != h["dst"] and o["valid_from"]
                            and o["props"].get("bound_from") == "exact"
                            and o["valid_from"] > h["valid_from"]
                            and (h["valid_to"] is None or o["valid_from"] <= h["valid_to"]))
            if not starts:
                continue
            day_before = (datetime.date.fromisoformat(starts[0]) - datetime.timedelta(days=1)).isoformat()
            if h["valid_to"] == day_before:
                continue
            h["valid_to"] = day_before
            h["props"]["bound_to"] = "inferred"
            h["props"]["inferred_from"] = f"took another seat on {starts[0]}"
            changed.append(h)
    return changed



def holders_as_of(hold_edges, as_of):
    """The `holds` edges in force on a date. NULL valid_to is open. Pure;
    `seat_holder` feeds it the rows from Postgres so the SQL and the tests
    share one definition of 'in force'."""
    as_of = str(as_of)
    return [e for e in hold_edges
            if e["valid_from"] is not None and str(e["valid_from"]) <= as_of
            and (e["valid_to"] is None or str(e["valid_to"]) >= as_of)]


# ------------------------------------------------------------------ answer

def shape_answer(rows, persons, query, topic=None, total_votes=None, truncated=False,
                 predicate="voted_on"):
    """The response the API returns. Every hop crossed is listed with the
    weakest certification seen on it; the topic filter is flagged advisory
    because the topic is a model's reading of the title; an empty result
    always says why."""
    counts = {}
    for r in rows:
        counts[r["certification"]] = counts.get(r["certification"], 0) + 1
    hops = []
    if rows:
        weakest = min(counts, key=CERTIFICATION_RANK.get)
        hops.append({"predicate": predicate, "weakest": weakest, "counts": counts})
    empty_reason = None
    if not persons:
        empty_reason = f"no person in the graph matches {query!r}"
    elif not rows and topic:
        empty_reason = (f"{total_votes or 0} recorded vote(s) by "
                        f"{', '.join(p['name'] for p in persons)}; none has a topic "
                        f"or title matching {topic!r}")
    elif not rows:
        empty_reason = f"no recorded votes by {', '.join(p['name'] for p in persons)}"
    return {
        "query": query, "topic": topic,
        "persons": persons, "rows": rows, "count": len(rows),
        "truncated": truncated,
        "hops": hops,
        "weak_hops": [h for h in hops if h["weakest"] != "certified"],
        # The topic filter is advisory only when a model produced the topic
        # it matched; Congress.gov's policy area is the record's own.
        "advisory_fields": ["topic"] if topic and any(
            "claude" in (r.get("topic_derived_by") or "") or (r.get("topic_derived_by") or "") == "model"
            for r in rows) else [],
        "topic_sources": sorted({r.get("topic_derived_by") for r in rows if r.get("topic_derived_by")}),
        "empty_reason": empty_reason,
    }


# -------------------------------------------------------------------- db

def _read_store(name):
    p = STORE_DIR / f"{name}.json"
    return json.loads(p.read_text()) if p.exists() else {}


def _congress_snapshots():
    return [json.loads(p.read_text()) for p in sorted(DATA_DIR.glob("congress-votes-*.json"))]


def merge_legislators(current, historical):
    """One list, one record per bioguide id, each tagged with the file it
    came from. The current file wins a collision: it is maintained, and
    the historical one only receives a member after they leave. Pure.
    Returns (legislators, gaps)."""
    seen = {leg["id"].get("bioguide") for leg in current}
    out = [{**leg, "_source": LEGISLATORS_SOURCE} for leg in current]
    dupes = []
    for leg in historical:
        if leg["id"].get("bioguide") in seen:
            dupes.append(leg["id"]["bioguide"])
            continue
        out.append({**leg, "_source": HISTORICAL_SOURCE})
    gaps = [f"{len(dupes)} member(s) in both legislators files ({', '.join(dupes[:5])}); "
            f"kept the current record"] if dupes else []
    return out, gaps


def fetch_public(names=PUBLIC_FILES):
    """Download the public data files into data/. The only network step for
    them; the loader reads the files. Returns {name: bytes written}."""
    import requests
    out = {}
    for name in names:
        r = requests.get(PUBLIC_DATA_URL.format(name), timeout=60)
        r.raise_for_status()
        json.loads(r.content)   # fail-closed: never overwrite a good file with a bad one
        (DATA_DIR / f"{name}.json").write_bytes(r.content)
        out[name] = len(r.content)
    return out


def build_source(source, states=None):
    """Read the inputs for one source off disk and build. Returns
    (nodes, edges, gaps, delete scopes)."""
    if source == "us-congress":
        current = json.loads((DATA_DIR / "legislators-current.json").read_text())
        hist_path = DATA_DIR / "legislators-historical.json"
        historical = json.loads(hist_path.read_text()) if hist_path.exists() else []
        legislators, merge_gaps = merge_legislators(current, historical)
        snaps = _congress_snapshots()
        if not snaps:
            raise RuntimeError("no data/congress-votes-*.json — run `python graph.py snapshot 119 2 2026`")
        nodes, edges, gaps = build_congress(legislators, snaps, states)
        gaps = merge_gaps + gaps
        if not historical:
            gaps.append("no data/legislators-historical.json: former members are not loaded "
                        "(run `python graph.py fetch`)")
        scopes = [f"{US}/state:{s.lower()}" for s in states] if states else \
            sorted({e["props"]["jurisdiction"] for e in edges})
        return nodes, edges, gaps, scopes
    cfg = SOURCES[source]
    store = _read_store(source)
    if not store.get("meetings"):
        raise RuntimeError(f"store {source} is empty or missing")
    contests = _read_store(cfg["elections_source"]).get("contests", [])
    nodes, edges, gaps = build(source, store, contests, _read_store("item-summaries"))
    return nodes, edges, gaps, [f"{US}/state:{cfg['state']}/county:{cfg['county']}"]


def _summary(nodes, edges):
    kinds, preds = {}, {}
    for n in nodes:
        kinds[n["kind"]] = kinds.get(n["kind"], 0) + 1
    for e in edges:
        preds[e["predicate"]] = preds.get(e["predicate"], 0) + 1
    return {"nodes": len(nodes), "edges": len(edges), "by_kind": kinds, "by_predicate": preds}


def load(source, states=None):
    """Rebuild one source's slice of the graph in a single transaction.
    Fail-closed: any error rolls back and the previous graph stays served.
    Returns (summary dict, gaps)."""
    from psycopg.types.json import Jsonb
    from correspondence.db import _get_pool, init_db

    nodes, edges, gaps, scopes = build_source(source, states)
    init_db()
    with _get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM graph_edge WHERE props->>'jurisdiction' = ANY(%s)", (scopes,))
            cur.execute("""DELETE FROM graph_node
                           WHERE props->>'jurisdiction' = ANY(%s) AND NOT (id = ANY(%s))""",
                        (scopes, [n["id"] for n in nodes]))
            # Props merge rather than replace: a person both layers know
            # keeps what the other loader recorded about them.
            cur.executemany("""
                INSERT INTO graph_node (id, kind, name, props, source_id, source_ref)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (id) DO UPDATE SET
                    kind = excluded.kind, name = excluded.name,
                    props = graph_node.props || excluded.props,
                    source_id = excluded.source_id, source_ref = excluded.source_ref,
                    updated_at = NOW()
            """, [(n["id"], n["kind"], n["name"], Jsonb(n["props"]),
                   n["source_id"], n["source_ref"]) for n in nodes])
            # Edges outside this load's scope but re-asserted by it (the
            # chambers' `considered` edges, shared seeds) are refreshed.
            cur.executemany("""
                INSERT INTO graph_edge (src, predicate, dst, valid_from, valid_to,
                                        certification, source_id, source_ref, props)
                VALUES (%s, %s, %s, %s::date, %s::date, %s, %s, %s, %s)
                ON CONFLICT (src, predicate, dst, source_ref) DO UPDATE SET
                    valid_from = excluded.valid_from, valid_to = excluded.valid_to,
                    certification = excluded.certification, source_id = excluded.source_id,
                    props = excluded.props
            """, [(e["src"], e["predicate"], e["dst"], e["valid_from"], e["valid_to"],
                   e["certification"], e["source_id"], e["source_ref"], Jsonb(e["props"]))
                  for e in edges])
    closed = _close_holds_in_db(sorted({n["id"] for n in nodes if n["kind"] == "person"}))
    for c in closed:
        gaps.append(f"{c['name']}'s hold on {c['post']} closed at {c['valid_to']}: {c['why']}")
    return _summary(nodes, edges), gaps


def _close_holds_in_db(person_ids):
    """Apply close_holds_across to every hold of these people, across every
    source already loaded — the county loader cannot see a federal term
    and vice versa, so this runs where both are visible."""
    from psycopg.rows import dict_row
    from psycopg.types.json import Jsonb
    from correspondence.db import _get_pool
    with _get_pool().connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("""
                SELECT e.id, e.src, e.dst, e.valid_from, e.valid_to, e.props,
                       p.name, o.name AS post
                FROM graph_edge e JOIN graph_node p ON p.id = e.src
                                  JOIN graph_node o ON o.id = e.dst
                WHERE e.predicate = 'holds' AND e.src = ANY(%s)""", (person_ids,))
            rows = cur.fetchall()
            for r in rows:
                r["predicate"] = "holds"
                r["valid_from"] = r["valid_from"].isoformat() if r["valid_from"] else None
                r["valid_to"] = r["valid_to"].isoformat() if r["valid_to"] else None
            changed = close_holds_across(rows)
            for r in changed:
                cur.execute("UPDATE graph_edge SET valid_to = %s::date, props = %s WHERE id = %s",
                            (r["valid_to"], Jsonb(r["props"]), r["id"]))
    return [{"name": r["name"], "post": r["post"], "valid_to": r["valid_to"],
             "why": r["props"]["inferred_from"]} for r in changed]


def load_all():
    """Every county in the sidecar, then every member of Congress. Returns
    {source: (summary, gaps)}."""
    out = {}
    for source in sorted(SOURCES):
        out[source] = load(source)
    out["us-congress"] = load("us-congress")
    return out


def current_session(today=None):
    """(congress, session, year) for a date: the 1st Congress met in 1789
    and each lasts two years; odd years are session 1."""
    today = today or datetime.date.today()
    congress = (today.year - 1789) // 2 + 1
    return congress, 1 if today.year % 2 else 2, today.year


def _name_tokens(query):
    """'Mark Warner' → ['mark', 'warner']. A name matches when it contains
    every token, so a middle initial ('Mark R. Warner') does not hide it."""
    return [t for t in re.split(r"[\s,.]+", query.lower()) if t]


def _pg_persons(cur, query):
    likes = [f"%{t}%" for t in _name_tokens(query)] or [f"%{query}%"]
    all_in = lambda col: " AND ".join(f"{col} ILIKE %s" for _ in likes)  # noqa: E731
    cur.execute(f"""
        SELECT id, name, props->'aliases' AS aliases, props->>'seat' AS seat,
               props->>'bioguide' AS bioguide
        FROM graph_node
        WHERE kind = 'person'
          AND (({all_in("name")}) OR EXISTS (
                SELECT 1 FROM jsonb_array_elements_text(props->'aliases') a
                WHERE {all_in("a")}))
        ORDER BY name
    """, likes + likes)
    return cur.fetchall()


_CAREERS_SQL = """
    SELECT e.src, o.name AS seat, e.valid_from, e.valid_to
    FROM graph_edge e JOIN graph_node o ON o.id = e.dst
    WHERE e.predicate = 'holds' AND e.src = ANY(%s)
"""


def careers_from_holds(rows):
    """holds rows (src, seat, valid_from, valid_to) → {person id: {seats,
    from, to}}, `to` None while a term is open. Pure; both backends feed it
    so a candidate list reads the same from memory and from Postgres."""
    out = {}
    for r in rows:
        c = out.setdefault(r["src"], {"seats": [], "from": None, "to": "", "_open": False})
        if r["seat"] not in c["seats"]:
            c["seats"].append(r["seat"])
        start, end = str(r["valid_from"] or ""), r["valid_to"]
        if start and (c["from"] is None or start < c["from"]):
            c["from"] = start
        if end is None:
            c["_open"] = True
        elif str(end) > c["to"]:
            c["to"] = str(end)
    for c in out.values():
        c["from"] = c["from"][:4] if c["from"] else None
        c["to"] = None if c.pop("_open") else (c["to"][:4] or None)
    return out


_VOTE_ROW_SQL = """
    SELECT p.id AS person_id, p.name AS person,
           COALESCE(e.props->>'position', e.props->>'role') AS position,
           e.valid_from AS date,
           e.certification, e.source_ref AS vote_id,
           e.props->>'question' AS question,
           i.id AS item_id, i.name AS title,
           i.props->>'instrument_type' AS instrument_type,
           i.props->>'jurisdiction' AS jurisdiction,
           i.props->>'topic' AS topic, i.props->>'topic_derived_by' AS topic_derived_by,
           i.props->>'result' AS result,
           i.props->>'meeting_id' AS meeting_id
    FROM graph_node p
    JOIN graph_edge e ON e.src = p.id AND e.predicate = 'voted_on'
    JOIN graph_node i ON i.id = e.dst
"""
_TOPIC_SQL = " (i.props->>'topic' ILIKE %s OR i.name ILIKE %s)"


def _pg_votes(cur, person_ids, topic, limit, predicate="voted_on"):
    """(rows, total edges of this predicate by these people, truncated)."""
    cur.execute("SELECT COUNT(*) AS n FROM graph_edge "
                "WHERE predicate = %s AND src = ANY(%s)", (predicate, person_ids))
    total = cur.fetchone()["n"]
    sql, args = _VOTE_ROW_SQL.replace("'voted_on'", "%s") + " WHERE p.id = ANY(%s)", [predicate, person_ids]
    if topic:
        sql += " AND" + _TOPIC_SQL
        args += [f"{topic}%", f"%{topic}%"]
    sql += " ORDER BY e.valid_from DESC, i.id LIMIT %s"
    cur.execute(sql, args + [limit + 1])
    rows = cur.fetchall()
    for r in rows:
        r["date"] = r["date"].isoformat()
    return rows[:limit], total, len(rows) > limit


def votes(person_query, topic=None, limit=200):
    """person → voted_on → instrument, optionally filtered by topic.
    One SQL statement for the traversal; the answer is shaped in Python."""
    from psycopg.rows import dict_row
    from correspondence.db import _get_pool

    with _get_pool().connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            persons = _pg_persons(cur, person_query)
            if not persons:
                # An empty graph and a missing person are different gaps:
                # one is mine (nothing loaded), the other is the reader's.
                cur.execute("SELECT EXISTS (SELECT 1 FROM graph_node) AS any")
                if not cur.fetchone()["any"]:
                    return shape_answer([], [], person_query, topic) | {
                        "empty_reason": "graph not loaded: run `python graph.py load fairfax-bos`"}
                return shape_answer([], [], person_query, topic)
            rows, total, truncated = _pg_votes(cur, [p["id"] for p in persons], topic, limit)
    return shape_answer(rows, persons, person_query, topic, total, truncated)


def seat_holder(post_id, as_of):
    """Who held a seat on a date, with the precision of each bound."""
    from psycopg.rows import dict_row
    from correspondence.db import _get_pool
    with _get_pool().connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("""
                SELECT e.src, e.dst, e.valid_from, e.valid_to, e.certification,
                       e.props, p.name
                FROM graph_edge e JOIN graph_node p ON p.id = e.src
                WHERE e.predicate = 'holds' AND e.dst = %s
            """, (post_id,))
            rows = cur.fetchall()
    return holders_as_of(rows, as_of)


# ------------------------------------------------------------------ search
#
# A typed question reaches the graph here. Same contract as
# router_agent.fast_route: a regex answers the unambiguous shapes for $0 and
# returns None otherwise, so a caller can fall through to whatever it did
# before. Three asks, which are the three traversals that exist:
#
#   votes    "how did Herrity vote on zoning"      person → voted_on → instrument
#   voters   "who voted no on the Affordable HOMES Act"   instrument ← voted_on ← person
#   holder   "who held the Braddock seat on 2025-11-18"   post ← holds ← person, as of
#
# The answer functions take a `backend`: five callables over the graph. The
# Postgres backend runs SQL; the memory backend runs the same lookups over
# the lists `build` returns, and is what the tests and the CLI's --memory
# flag use. One question layer, two storages, no LLM.

_ASK_HOLDER = re.compile(
    r"^\s*who\s+(?:is|was|holds|held|represents|represented|sits\s+in|sat\s+in)\s+"
    r"(?:the\s+)?(?P<seat>.+?)"
    r"(?:\s+(?:seat|district|supervisor|representative|senator|chair)s?)?"
    r"(?:\s+(?:as\s+of|on|in)\s+(?P<date>\d{4}(?:-\d{2}(?:-\d{2})?)?))?\s*\??\s*$", re.I)
_ASK_VOTERS = re.compile(
    r"^\s*who\s+voted\s+(?:(?P<position>aye|yes|yea|no|nay|present|abstain(?:ed)?)\s+)?"
    r"(?P<connector>on|for|against)\s+(?:the\s+)?(?P<topic>.+?)\s*\??\s*$", re.I)
_ASK_SPONSORS = re.compile(
    r"^\s*who\s+(?:sponsored|cosponsored|co-sponsored|wrote|introduced|authored)\s+(?:the\s+)?"
    r"(?P<topic>.+?)\s*\??\s*$", re.I)
_ASK_SPONSORED = (
    re.compile(r"^\s*(?:what|which\s+bills?)\s+(?:did|has|have)\s+(?P<person>.+?)\s+"
               r"(?:sponsor(?:ed)?|cosponsor(?:ed)?|introduce[d]?|write|written|author(?:ed)?)\s*\??\s*$", re.I),
    re.compile(r"^\s*(?P<person>[A-Z][A-Za-z.\-]*(?:'(?!s\b)[A-Za-z]+)?"
               r"(?:\s+[A-Z][A-Za-z.\-]*(?:'(?!s\b)[A-Za-z]+)?){0,3})(?:'s)?\s+"
               r"(?:bills|sponsorships|sponsored\s+bills)\s*\??\s*$"),
)
_ASK_VOTES = (
    re.compile(r"^\s*(?:how\s+did|how\s+has|how\s+does)\s+(?P<person>.+?)\s+voted?\s*"
               r"(?:on\s+(?P<topic>.+?))?\s*\??\s*$", re.I),
    re.compile(r"^\s*(?:what\s+did|what\s+has)\s+(?P<person>.+?)\s+voted?\s+(?:on|for)\s*"
               r"(?P<topic>.*?)\s*\??\s*$", re.I),
    # "Warner votes", "Pat Herrity's votes on zoning": a Name, capitalised,
    # so "register to vote" is not read as a person called "register to".
    re.compile(r"^\s*(?P<person>[A-Z][A-Za-z.\-]*(?:'(?!s\b)[A-Za-z]+)?"
               r"(?:\s+[A-Z][A-Za-z.\-]*(?:'(?!s\b)[A-Za-z]+)?){0,3})(?:'s)?"
               r"\s+votes?\s*(?:on\s+(?P<topic>.+?))?\s*\??\s*$"),
)
# A "who is/was …" question is a seat question only when it names a seat:
# a seat word, a district code (VA-11), or an ordinal district. "Who is Ted
# Cruz" is a member lookup and stays with the ledger.
_SEAT_SIGNAL = re.compile(
    r"\b(seat|district|supervisor|representative|senator|chair(?:man|woman)?|delegate)s?\b"
    r"|\b[a-z]{2}-?\d{1,2}\b|\b\d{1,2}(?:st|nd|rd|th)\b", re.I)
_POSITION_WORDS = {"aye": "aye", "yes": "aye", "yea": "aye", "for": "aye",
                   "no": "no", "nay": "no", "against": "no",
                   "present": "present", "abstain": "abstain", "abstained": "abstain",
                   "on": None}


def _complete_date(text, today):
    """'2025' → 2025-12-31, '2025-11' → end of that month, a full date as
    is, nothing → today. A question asks about a moment; a bare year means
    'by the end of it'."""
    if not text:
        return today
    if len(text) == 4:
        return f"{text}-12-31"
    if len(text) == 7:
        y, m = int(text[:4]), int(text[5:])
        last = (datetime.date(y + (m == 12), m % 12 + 1, 1) - datetime.timedelta(days=1)).day
        return f"{text}-{last:02d}"
    return text


def parse_question(question, today=None):
    """Question → {"ask", ...} or None when the shape is not one the graph
    answers. Pure. `today` is injectable so tests are stable."""
    today = today or datetime.date.today().isoformat()
    q = (question or "").strip()
    if not q:
        return None
    m = _ASK_SPONSORS.match(q)
    if m:
        return {"ask": "sponsors", "topic": m.group("topic").strip()}
    for rx in _ASK_SPONSORED:
        m = rx.match(q)
        if m:
            return {"ask": "sponsored", "person": m.group("person").strip()}
    m = _ASK_VOTERS.match(q)
    if m:
        # "voted against X" and "voted for X" carry the position in the
        # connector; "voted on X" carries none.
        pos = (m.group("position") or m.group("connector")).lower()
        return {"ask": "voters", "topic": m.group("topic").strip(),
                "position": _POSITION_WORDS.get(pos)}
    m = _ASK_HOLDER.match(q)
    if m and _SEAT_SIGNAL.search(q):
        return {"ask": "holder", "seat": m.group("seat").strip(),
                "as_of": _complete_date(m.group("date"), today)}
    for rx in _ASK_VOTES:
        m = rx.match(q)
        if m:
            topic = (m.group("topic") or "").strip() or None
            return {"ask": "votes", "person": m.group("person").strip(), "topic": topic}
    return None


def _seat_terms(seat_query):
    """'Braddock', 'VA-11', 'Virginia's 11th', 'Senate class 2' → tokens a
    post's natural key or role must contain."""
    q = seat_query.lower().replace("\u2019", "'")
    m = re.search(r"\b([a-z]{2})[- ]?(\d{1,2})\b", q)
    if m:
        return [f"us/house/{m.group(1)}/cd:{m.group(2)}"]
    # A state named in full becomes its code in the natural key, longest
    # name first: "Virginia" must not match "West Virginia"'s seats.
    state = []
    for code, name in sorted(DIVISION_NAMES.items(), key=lambda kv: -len(kv[1])):
        rx = rf"\b{re.escape(name.lower())}\b(?:'s)?"
        if re.search(rx, q):
            state = [f"/{code}/"]
            q = re.sub(rx, " ", q)
            break
    m = re.search(r"\b(\d{1,2})(?:st|nd|rd|th)\b", q)
    if m:
        return state + [f"cd:{m.group(1)}"]
    q = re.sub(r"\b(the|of|for|district|seat|county|board|supervisor|supervisors|from)\b", " ", q)
    return state + [t for t in re.split(r"[^a-z0-9]+", q) if t]


def _post_matches(post, terms):
    hay = f"{post['props'].get('natural_key', '')} {post['props'].get('role', '')} {post['name']}".lower()
    return all(t in hay for t in terms)


def memory_backend(nodes, edges):
    """The five lookups over in-memory rows. Mirrors the SQL exactly; this
    is the harness the tests run the question layer through."""
    by_id = {}
    for n in nodes:
        if n["id"] in by_id:
            by_id[n["id"]] = {**n, "props": {**by_id[n["id"]]["props"], **n["props"]}}
        else:
            by_id[n["id"]] = n
    nodes = list(by_id.values())
    seen, unique = set(), []
    for e in edges:
        key = (e["src"], e["predicate"], e["dst"], e["source_ref"])
        if key not in seen:
            seen.add(key)
            unique.append(e)
    edges = unique
    out, inn = {}, {}
    for e in edges:
        out.setdefault(e["src"], []).append(e)
        inn.setdefault(e["dst"], []).append(e)

    def vote_row(e):
        p, i = by_id[e["src"]], by_id[e["dst"]]
        return {"person_id": p["id"], "person": p["name"],
                "position": e["props"].get("position") or e["props"].get("role"),
                "date": e["valid_from"], "certification": e["certification"],
                "vote_id": e["source_ref"], "question": e["props"].get("question"),
                "item_id": i["id"], "title": i["name"],
                "instrument_type": i["props"].get("instrument_type"),
                "jurisdiction": i["props"].get("jurisdiction"), "topic": i["props"].get("topic"),
                "topic_derived_by": i["props"].get("topic_derived_by"),
                "result": i["props"].get("result"), "meeting_id": i["props"].get("meeting_id")}

    def topic_ok(i, topic):
        t = topic.lower()
        return (i["props"].get("topic") or "").lower().startswith(t) or t in i["name"].lower()

    def persons(query):
        toks = _name_tokens(query) or [query.lower()]
        has_all = lambda text: all(t in text.lower() for t in toks)  # noqa: E731
        return sorted(({"id": n["id"], "name": n["name"], "aliases": n["props"].get("aliases", []),
                        "seat": n["props"].get("seat"), "bioguide": n["props"].get("bioguide")}
                       for n in nodes if n["kind"] == "person"
                       and (has_all(n["name"]) or any(has_all(a) for a in n["props"].get("aliases", [])))),
                      key=lambda p: p["name"])

    def careers(person_ids):
        return careers_from_holds([{"src": e["src"], "seat": by_id[e["dst"]]["name"],
                                    "valid_from": e["valid_from"], "valid_to": e["valid_to"]}
                                   for pid in person_ids for e in out.get(pid, [])
                                   if e["predicate"] == "holds"])

    def votes_of(person_ids, topic, limit, predicate="voted_on"):
        mine = [e for pid in person_ids for e in out.get(pid, []) if e["predicate"] == predicate]
        rows = [vote_row(e) for e in mine if not topic or topic_ok(by_id[e["dst"]], topic)]
        rows.sort(key=lambda r: (r["date"], r["item_id"]), reverse=True)
        return rows[:limit], len(mine), len(rows) > limit

    def posts(seat_query):
        terms = _seat_terms(seat_query)
        return [{"id": n["id"], "name": n["name"], "natural_key": n["props"].get("natural_key")}
                for n in nodes if n["kind"] == "post" and terms and _post_matches(n, terms)]

    def holds_of(post_id):
        return [{"src": e["src"], "dst": e["dst"], "valid_from": e["valid_from"],
                 "valid_to": e["valid_to"], "certification": e["certification"],
                 "props": e["props"], "name": by_id[e["src"]]["name"]}
                for e in inn.get(post_id, []) if e["predicate"] == "holds"]

    def voters(topic, position, limit, predicate="voted_on"):
        rows = [vote_row(e) for n in nodes if n["kind"] == "instrument" and topic_ok(n, topic)
                for e in inn.get(n["id"], []) if e["predicate"] == predicate
                and (position is None or e["props"].get("position") == position)]
        rows.sort(key=lambda r: (r["date"], r["item_id"], r["person"]), reverse=True)
        return rows[:limit], len(rows) > limit

    return {"persons": persons, "votes": votes_of, "posts": posts, "careers": careers,
            "holds": holds_of, "voters": voters, "loaded": lambda: bool(nodes)}


def pg_backend():
    """The same five lookups as SQL. Each opens its own short connection."""
    from psycopg.rows import dict_row
    from correspondence.db import _get_pool

    def run(fn):
        with _get_pool().connection() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                return fn(cur)

    def persons(query):
        return run(lambda cur: _pg_persons(cur, query))

    def votes_of(person_ids, topic, limit, predicate="voted_on"):
        return run(lambda cur: _pg_votes(cur, person_ids, topic, limit, predicate))

    def careers(person_ids):
        return run(lambda cur: (cur.execute(_CAREERS_SQL, (person_ids,)),
                                careers_from_holds(cur.fetchall()))[1])

    def posts(seat_query):
        terms = _seat_terms(seat_query)
        if not terms:
            return []

        def q(cur):
            cur.execute("SELECT id, name, props->>'natural_key' AS natural_key, props "
                        "FROM graph_node WHERE kind = 'post'")
            return [{"id": r["id"], "name": r["name"], "natural_key": r["natural_key"]}
                    for r in cur.fetchall() if _post_matches(r, terms)]
        return run(q)

    def holds_of(post_id):
        def q(cur):
            cur.execute("""
                SELECT e.src, e.dst, e.valid_from, e.valid_to, e.certification, e.props, p.name
                FROM graph_edge e JOIN graph_node p ON p.id = e.src
                WHERE e.predicate = 'holds' AND e.dst = %s""", (post_id,))
            rows = cur.fetchall()
            for r in rows:
                r["valid_from"] = r["valid_from"].isoformat() if r["valid_from"] else None
                r["valid_to"] = r["valid_to"].isoformat() if r["valid_to"] else None
            return rows
        return run(q)

    def voters(topic, position, limit, predicate="voted_on"):
        def q(cur):
            sql = _VOTE_ROW_SQL.replace("'voted_on'", "%s") + " WHERE" + _TOPIC_SQL
            args = [predicate, f"{topic}%", f"%{topic}%"]
            if position:
                sql += " AND e.props->>'position' = %s"
                args.append(position)
            sql += " ORDER BY e.valid_from DESC, i.id, p.name LIMIT %s"
            cur.execute(sql, args + [limit + 1])
            rows = cur.fetchall()
            for r in rows:
                r["date"] = r["date"].isoformat()
            return rows[:limit], len(rows) > limit
        return run(q)

    def loaded():
        return run(lambda cur: (cur.execute("SELECT EXISTS (SELECT 1 FROM graph_node) AS any"),
                                cur.fetchone()["any"])[1])

    return {"persons": persons, "votes": votes_of, "posts": posts, "careers": careers,
            "holds": holds_of, "voters": voters, "loaded": loaded}


_PLACE_WORDS = None


def strip_place(topic):
    """'Fairfax zoning' → ('zoning', 'Fairfax'): a county or state name
    inside a topic is a scope, not a subject, and no title contains it."""
    global _PLACE_WORDS
    if _PLACE_WORDS is None:
        words = set()
        for cfg in SOURCES.values():
            words.add(cfg["county"])
            words.update(w.lower() for w in cfg["county_name"].split())
        words.update(n.lower() for n in STATE_NAMES.values())
        words.update(STATE_NAMES)
        words -= {"county"}
        _PLACE_WORDS = words
    kept, dropped = [], []
    for w in topic.split():
        (dropped if w.lower().strip(",'s") in _PLACE_WORDS or w.lower() == "county" else kept).append(w)
    return " ".join(kept) or topic, " ".join(dropped) or None


_MAX_CANDIDATES = 25


def _one_person(persons, query, topic, ask, backend):
    """None when the query names one person; otherwise the answer that asks
    which one. Merging the votes of every Warner since 1789 would answer a
    question nobody asked. An exact name or alias match settles it."""
    q = query.lower().strip()
    exact = [p for p in persons if p["name"].lower() == q
             or any(a.lower() == q for a in p.get("aliases") or [])]
    if len(exact) == 1 or len(persons) == 1:
        return None, exact or persons
    careers = backend["careers"]([p["id"] for p in persons])
    cands = [{"id": p["id"], "name": p["name"], **careers.get(p["id"], {"seats": [], "from": None, "to": None})}
             for p in persons]
    # Serving now first, then the most recent.
    cands.sort(key=lambda c: (c["to"] is not None, -int(c["to"] or 0), c["name"]))
    out = shape_answer([], persons, query, topic) | {
        "ask": ask, "ambiguous": True, "candidates": cands[:_MAX_CANDIDATES],
        "candidate_count": len(cands),
        "empty_reason": f"{len(cands)} people in the graph match {query!r}; name one of them"}
    return out, persons


def answer(parsed, backend, limit=200):
    """Run one parsed question against a backend. Every branch returns a
    dict with `ask`, `hops` / `weak_hops`, and an `empty_reason` when there
    is nothing — never a silent zero."""
    ask = parsed["ask"]
    if not backend["loaded"]():
        return {"ask": ask, "rows": [], "hops": [], "weak_hops": [],
                "empty_reason": "graph not loaded: run `python graph.py load fairfax-bos`"}
    topic, place = strip_place(parsed["topic"]) if parsed.get("topic") else (None, None)
    if ask == "sponsored":
        persons = backend["persons"](parsed["person"])
        if not persons:
            return shape_answer([], [], parsed["person"], None) | {"ask": ask}
        which, persons = _one_person(persons, parsed["person"], None, ask, backend)
        if which:
            return which
        rows, total, truncated = backend["votes"]([p["id"] for p in persons], None, limit, "sponsored")
        out = shape_answer(rows, persons, parsed["person"], None, total, truncated, predicate="sponsored")
        if not rows:
            out["empty_reason"] = (f"no bill on disk names {', '.join(p['name'] for p in persons)} "
                                   f"as sponsor or cosponsor (only bills with a recorded vote this session are loaded)")
        return out | {"ask": ask}
    if ask == "sponsors":
        rows, truncated = backend["voters"](topic, None, limit, "sponsored")
        out = shape_answer(rows, [{"name": "anyone"}], topic, topic, None, truncated, predicate="sponsored")
        out.update({"ask": ask, "persons": sorted({r["person"] for r in rows}), "place_ignored": place})
        if not rows:
            out["empty_reason"] = (f"no sponsor on disk for anything matching {topic!r} "
                                   f"(sponsors are loaded only for the delegation and for bills with a recorded vote)")
        return out
    if ask == "votes":
        persons = backend["persons"](parsed["person"])
        if not persons:
            return shape_answer([], [], parsed["person"], topic) | {"ask": ask}
        which, persons = _one_person(persons, parsed["person"], topic, ask, backend)
        if which:
            return which | {"place_ignored": place}
        rows, total, truncated = backend["votes"]([p["id"] for p in persons], topic, limit)
        return shape_answer(rows, persons, parsed["person"], topic, total, truncated) | {
            "ask": ask, "place_ignored": place}
    if ask == "voters":
        rows, truncated = backend["voters"](topic, parsed["position"], limit)
        out = shape_answer(rows, [{"name": "anyone"}], topic, topic, None, truncated)
        out.update({"ask": ask, "persons": sorted({r["person"] for r in rows}),
                    "position": parsed["position"], "place_ignored": place})
        if not rows:
            out["empty_reason"] = (f"no recorded vote{' ' + parsed['position'] if parsed['position'] else ''} "
                                   f"on anything matching {topic!r}")
        return out
    posts = backend["posts"](parsed["seat"])
    if not posts:
        return {"ask": ask, "seat": parsed["seat"], "as_of": parsed["as_of"], "seats": [],
                "holders": [], "hops": [], "weak_hops": [], "inferred_bounds": [],
                "empty_reason": f"no seat in the graph matches {parsed['seat']!r}"}
    holders, counts = [], {}
    for post in posts:
        for h in holders_as_of(backend["holds"](post["id"]), parsed["as_of"]):
            counts[h["certification"]] = counts.get(h["certification"], 0) + 1
            holders.append({"name": h["name"], "seat": post["name"],
                            "valid_from": h["valid_from"], "valid_to": h["valid_to"],
                            "certification": h["certification"],
                            "bound_from": h["props"].get("bound_from"),
                            "bound_to": h["props"].get("bound_to"),
                            "inferred_from": h["props"].get("inferred_from")})
    hops = []
    if holders:
        hops.append({"predicate": "holds", "weakest": min(counts, key=CERTIFICATION_RANK.get),
                     "counts": counts})
    out = {"ask": ask, "seat": parsed["seat"], "as_of": parsed["as_of"],
           "seats": [p["name"] for p in posts], "holders": holders, "hops": hops,
           "weak_hops": [h for h in hops if h["weakest"] != "certified"],
           "inferred_bounds": [h["name"] for h in holders
                               if "inferred" in (h["bound_from"], h["bound_to"])],
           "empty_reason": None}
    if not holders:
        out["empty_reason"] = (f"nobody in the graph held {', '.join(out['seats'])} on "
                               f"{parsed['as_of']} — either the seat was vacant or the "
                               f"holder's record is not on disk")
    return out


def search(question, backend=None, today=None):
    """The entry point: a typed question → an answer, or None when the
    question is not one the graph answers (caller falls through)."""
    parsed = parse_question(question, today)
    if parsed is None:
        return None
    return answer(parsed, backend or pg_backend())



if __name__ == "__main__":
    import argparse
    from dotenv import load_dotenv
    load_dotenv(_HERE / ".env")
    sources = sorted(SOURCES) + ["us-congress", "all"]
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, help_ in (("build", "build in memory and print the summary; no database"),
                        ("load", "rebuild one source's slice in Postgres")):
        s = sub.add_parser(name, help=help_)
        s.add_argument("source", choices=sources)
        s.add_argument("--state", action="append",
                       help="us-congress only: delegation(s) to load, e.g. --state VA")
    sub.add_parser("fetch", help="download the public legislators, executive and committee files to data/")
    s = sub.add_parser("snapshot", help="fetch one session's roll calls to data/ (no args: the current session)")
    s.add_argument("congress", type=int, nargs="?")
    s.add_argument("session", type=int, nargs="?")
    s.add_argument("year", type=int, nargs="?")
    s = sub.add_parser("votes", help="person → voted_on → instrument")
    s.add_argument("person")
    s.add_argument("--topic")
    s = sub.add_parser("ask", help="a typed question → the graph")
    s.add_argument("question")
    s.add_argument("--memory", action="store_true",
                   help="build every source in memory; no database")
    s = sub.add_parser("holder", help="who held a post on a date")
    s.add_argument("post_id")
    s.add_argument("as_of")
    a = ap.parse_args()
    if a.cmd == "load" and a.source == "all":
        for source, (summary, gaps) in load_all().items():
            print(source, json.dumps(summary["by_predicate"]))
            for g in gaps:
                print("  -", g)
    elif a.cmd in ("build", "load"):
        if a.cmd == "build":
            nodes, edges, gaps, _ = build_source(a.source, a.state)
            summary = _summary(nodes, edges)
        else:
            summary, gaps = load(a.source, a.state)
        print(json.dumps(summary, indent=1))
        print(f"{len(gaps)} gap(s):")
        for g in gaps:
            print("  -", g)
    elif a.cmd == "fetch":
        for name, n in fetch_public().items():
            print(f"{name}: {n:,} bytes")
    elif a.cmd == "snapshot":
        congress, session, year = (a.congress, a.session, a.year) if a.congress else current_session()
        out = DATA_DIR / f"congress-votes-{congress}-{session}.json"
        print(json.dumps(snapshot_congress(congress, session, year, out), indent=1))
        print("wrote", out)
    elif a.cmd == "votes":
        print(json.dumps(votes(a.person, a.topic), indent=1, default=str))
    elif a.cmd == "ask":
        backend = None
        if a.memory:
            nodes, edges = [], []
            for src in sorted(SOURCES):
                n, e, _, _ = build_source(src)
                nodes += n
                edges += e
            n, e, _, _ = build_source("us-congress")
            nodes += n
            edges += e
            close_holds_across(edges)
            backend = memory_backend(nodes, edges)
        out = search(a.question, backend)
        print(json.dumps(out if out is not None else
                         {"empty_reason": "not a question the graph answers (yet)"},
                         indent=1, default=str))
    else:
        print(json.dumps(seat_holder(a.post_id, a.as_of), indent=1, default=str))
    # The pool's worker threads outlive the script otherwise and psycopg
    # complains at interpreter exit; the server never gets here.
    import correspondence.db as _db
    if _db._pool is not None:
        _db._pool.close()
