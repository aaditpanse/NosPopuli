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
functions that touch Postgres; `snapshot_congress` and
`fetch_public` are the only ones that touch the network.

Deliberate limits, each reversible in one function: meetings are not nodes
(they are events; `considered` carries the meeting id); a local person is
keyed by seat + surname and a federal one by bioguide id, bridged by the
IDENTITIES table when a human has asserted they are the same; seat/district
seeds for a county are hand-written per source in SOURCES.
"""

import datetime
import json
import os
import pathlib
import re
import sys
import time
import uuid

_HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE / "foundry"))
from harness import member_key  # noqa: E402  — the certify canon; never fork it

STORE_DIR = _HERE / "foundry" / "data" / "store"
# The graph's inputs. On the server they live outside the checkout
# (/srv/bulk), because the bulk files are too large for git and the deploy's
# `git reset --hard` must never touch them.
DATA_DIR = pathlib.Path(os.environ.get("NOSPOPULI_DATA_DIR") or _HERE / "data")

# The full predicate vocabulary. The loader refuses anything else so the
# graph cannot grow a new relation type by accident.
PREDICATES = ("contains", "has_body", "has_seat", "holds", "represents",
              "sponsored", "voted_on", "considered", "elected_in", "for_seat",
              "signed", "vetoed", "enacted_as", "member_of", "referred_to", "reported",
              "related_to", "campaign_committee")

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
EXECUTIVE_SOURCE = "executive"
EXECUTIVE_KEY = "us/executive"
EXECUTIVE_POSTS = {"prez": ("us/president", "President of the United States"),
                   "viceprez": ("us/vice-president", "Vice President of the United States")}
# The public files `python graph.py fetch` downloads into data/. Congress
# itself does not publish these as data; the unitedstates project assembles
# them from the Biographical Directory and the clerks.
PUBLIC_DATA_URL = "https://raw.githubusercontent.com/unitedstates/congress-legislators/gh-pages/{}.json"
PUBLIC_FILES = ("legislators-current", "legislators-historical", "executive",
                "committees-current", "committee-membership-current")
# When each public file was last downloaded. The membership file carries no
# dates at all, so the download date is the only honest bound it has.
FETCHED_PATH = DATA_DIR / "public-fetched.json"
COMMITTEES_SOURCE = "committees-current"

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


def member_congress_index(snapshots, index=None):
    """Older roll calls → {"<id_kind>:<id>": {"<congress>/<chamber>":
    {"first": date, "first_vote": vote_id, "last": ..., "last_vote": ...,
    "source": source_id}}}. Only the two ends of each member's record in a
    Congress are kept: certifying from every position of every session
    since 1789 would hold millions of rows in memory on each load. Folds
    into `index` when given, so files can be read one at a time. Pure."""
    index = {} if index is None else index
    for snap in snapshots:
        for v in snap.get("votes", []):
            key = f"{v['congress']}/{v['chamber']}"
            kind = v.get("id_kind") or "bioguide"
            for ids in v.get("positions", {}).values():
                for mid in ids:
                    rec = index.setdefault(f"{kind}:{mid}", {}).get(key)
                    if rec is None:
                        index[f"{kind}:{mid}"][key] = {
                            "first": v["date"], "first_vote": v["vote_id"],
                            "last": v["date"], "last_vote": v["vote_id"], "source": v["source_id"]}
                        continue
                    if v["date"] < rec["first"]:
                        rec.update(first=v["date"], first_vote=v["vote_id"])
                    if v["date"] > rec["last"]:
                        rec.update(last=v["date"], last_vote=v["vote_id"])
    return index


BILL_LABEL = {"hr": "H.R.", "s": "S.", "hres": "H.Res.", "sres": "S.Res.", "hjres": "H.J.Res.",
              "sjres": "S.J.Res.", "hconres": "H.Con.Res.", "sconres": "S.Con.Res."}


def _instrument(g, iid, itype, number, congress, rec, fetched, label, fallback_title,
                source_id, source_ref, lookup_bioguide):
    """One bill's node and who wrote it, from its record. Instantaneous
    `sponsored` edges on the date each name went on the bill; a withdrawn
    cosponsor keeps the edge and the withdrawal date, because they did sign
    it once."""
    props = {"instrument_type": itype, "congress": congress, "number": number, "jurisdiction": US}
    if rec.get("policy_area"):
        # Congress.gov's own subject, not a model's reading.
        props["topic"] = rec["policy_area"]
        props["topic_derived_by"] = "congress.gov policyArea"
    if rec.get("introduced"):
        props["introduced"] = rec["introduced"]
    if "related" in rec:
        props["related_fetched"] = fetched
    if "committees" in rec:
        props["committees_fetched"] = fetched
    if "actions" in rec:
        # The date the presidential actions were read: "no law on record" is
        # only true as of then.
        props["actions_fetched"] = fetched
    name = f"{label}: {rec.get('title') or fallback_title or ''}".strip(": ")
    _node(g, iid, "instrument", name, props, source_id, source_ref)
    ref = f"us/{congress}/{itype}/{number}/sponsors"
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


def build_congress(legislators, snapshots, states=None, today=None, cert_index=None):
    """legislators-current.json + roll-call snapshots → (nodes, edges, gaps).

    states: iterable of two-letter codes to load (a delegation), or None
    for every member. snapshots become `voted_on` edges; cert_index (see
    member_congress_index: older sessions reduced to each member's first
    and last roll call per Congress) only certifies the terms those votes
    fall inside and emits no edges: the positions stay in their files and
    are read at the leaf. Pure; no I/O.
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

    # Every bill with a record is a node, voted on or not. A voted bill keeps
    # the label and the source of the first roll call on it, as before.
    first_vote = {}
    for snap in snapshots:
        for v in snap.get("votes", []):
            inst = _parse_instrument(v["chamber"], v)
            if inst and (v["congress"], *inst) not in first_vote:
                first_vote[(v["congress"], *inst)] = v
    by_congress = {}
    for snap in snapshots:
        congress = (snap.get("meta") or {}).get("congress") or \
            next((v["congress"] for v in snap.get("votes", [])), None)
        by_congress[congress] = by_congress.get(congress, False) or bool(snap.get("instruments"))
        fetched = (snap.get("meta") or {}).get("instruments_fetched")
        for label, rec in (snap.get("instruments") or {}).items():
            itype, number = label.split("/")
            iid = f"instrument/us/{congress}/{itype}/{number}"
            if iid in g["nodes"]:
                continue
            v = first_vote.get((congress, itype, number))
            if v:
                _instrument(g, iid, itype, number, congress, rec, fetched,
                            v.get("legis_num") or v.get("document_name") or f"{itype} {number}",
                            v.get("description"), v["source_id"], v["vote_id"], lookup_bioguide)
            else:
                _instrument(g, iid, itype, number, congress, rec, fetched,
                            f"{BILL_LABEL.get(itype, itype.upper())} {number}", None,
                            "govinfo", f"us/{congress}/{itype}/{number}", lookup_bioguide)
    for congress, has in by_congress.items():
        if not has:
            g["gaps"].append(f"the {congress}th Congress has no bill records (no bills-{congress}.json on disk); "
                             f"no `sponsored` edges from it")

    for snap in snapshots:
        for v in snap.get("votes", []):
            chamber = v["chamber"]
            inst = _parse_instrument(chamber, v)
            if inst is None:
                skipped[chamber] = skipped.get(chamber, 0) + 1
                continue
            itype, number = inst
            iid = f"instrument/us/{v['congress']}/{itype}/{number}"
            if iid not in g["nodes"]:
                # No record for it (a nomination, or a bill GovInfo lacks):
                # the roll call's own words name it.
                label = v.get("legis_num") or v.get("document_name") or f"{itype} {number}"
                _instrument(g, iid, itype, number, v["congress"], {}, None, label, v.get("description"),
                            v["source_id"], v["vote_id"], lookup_bioguide)
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
    # An older Congress contributes only its first and last roll call per
    # member. A term that holds neither date goes uncertified even if the
    # member voted inside it: the index can miss a certification, never
    # invent one.
    for mkey, congresses in (cert_index or {}).items():
        kind, _, mid = mkey.partition(":")
        pid = (by_lis if kind == "lis" else by_bioguide).get(mid)
        if pid is None:
            continue
        for rec in congresses.values():
            voted_days.setdefault(pid, []).append((rec["first"], rec["first_vote"], rec["source"]))
            if rec["last_vote"] != rec["first_vote"]:
                voted_days.setdefault(pid, []).append((rec["last"], rec["last_vote"], rec["source"]))
    certified_holds = 0
    for rows in holds.values():
        for h in rows:
            end = h["valid_to"] or "9999"
            hits = sorted(d for d in voted_days.get(h["src"], []) if h["valid_from"] <= d[0] <= end)
            if hits:
                h["certification"] = "certified"
                # No count: an older Congress contributes only its first and
                # last roll call, so a count would understate the record.
                h["props"]["certified_by"] = (f"cross-source: roll call {hits[0][1]} on {hits[0][0]} "
                                              f"({hits[0][2]}) by this member falls inside the term "
                                              f"and affirms it")
                certified_holds += 1
    if certified_holds:
        g["gaps"].append(f"{certified_holds} federal term(s) certified by roll calls on disk (the clerks', Voteview's); "
                         f"the rest have no vote inside them on disk")
    for chamber, n in skipped.items():
        g["gaps"].append(f"{n} {chamber} roll call(s) had no legislative instrument "
                         f"(quorum calls, Speaker elections, motions) and were not loaded")
    if unknown:
        g["gaps"].append(f"{sum(unknown.values())} vote position(s) by {len(unknown)} member "
                         f"id(s) not in the legislators files dropped")
    return list(g["nodes"].values()), list(g["edges"].values()), g["gaps"]


def build_executive(executive, today=None):
    """executive.json → the presidency: an executive organization under the
    country, the two posts, and every President and Vice President holding
    them. A person with a bioguide id gets the id Congress gives them, so
    Vance is one node whether he is asked about as senator or as Vice
    President; the thirteen with none are keyed by govtrack id. Pure."""
    today = today or datetime.date.today().isoformat()
    g = _graph()
    org = node_id("organization", EXECUTIVE_KEY)
    _node(g, US, "jurisdiction", "United States", {"level": "country"}, EXECUTIVE_SOURCE)
    _node(g, org, "organization", "Executive Branch of the United States",
          {"natural_key": EXECUTIVE_KEY, "jurisdiction": US}, EXECUTIVE_SOURCE)
    _edge(g, US, "has_body", org, None, None, "ingested", EXECUTIVE_SOURCE, "seed", US, {"derived": "seed"})
    for key, label in EXECUTIVE_POSTS.values():
        pid = node_id("post", key)
        _node(g, pid, "post", label, {"natural_key": key, "role": label, "jurisdiction": US},
              EXECUTIVE_SOURCE)
        _edge(g, org, "has_seat", pid, None, None, "ingested", EXECUTIVE_SOURCE, "seed", US, {"derived": "seed"})
        _edge(g, pid, "represents", US, None, None, "ingested", EXECUTIVE_SOURCE, "seed", US, {"derived": "seed"})
    for person in executive:
        ids = person.get("id", {})
        nk = f"bioguide/{ids['bioguide']}" if ids.get("bioguide") else f"govtrack/{ids.get('govtrack')}"
        if not ids.get("bioguide") and not ids.get("govtrack"):
            g["gaps"].append(f"{person['name'].get('last')} has neither bioguide nor govtrack id; skipped")
            continue
        pid = node_id("person", nk)
        name = person["name"].get("official_full") or \
            f"{person['name'].get('first', '')} {person['name'].get('last', '')}".strip()
        aliases = [f"{person['name']['nickname']} {person['name']['last']}"] if person["name"].get("nickname") else []
        _node(g, pid, "person", name,
              {"natural_key": nk, "aliases": aliases, "bioguide": ids.get("bioguide"),
               "external_ids": {k: ids[k] for k in ("govtrack", "wikidata") if k in ids},
               "jurisdiction": US}, EXECUTIVE_SOURCE, ids.get("bioguide") or str(ids.get("govtrack")))
        for t in person["terms"]:
            key, _ = EXECUTIVE_POSTS[t["type"]]
            expired = t.get("end") and t["end"] <= today
            props = {"bound_from": "exact", "party": t.get("party"), "how": t.get("how")}
            if expired:
                props["bound_to"] = "exact"
            else:
                props["term_expires"] = t.get("end")
            _edge(g, pid, "holds", node_id("post", key), t["start"], t["end"] if expired else None,
                  "ingested", EXECUTIVE_SOURCE, f"{nk}/{t['type']}/{t['start']}", US, props)
    return list(g["nodes"].values()), list(g["edges"].values()), g["gaps"]


def build_enactment(snapshots, hold_edges, instrument_ids):
    """What happened after the vote: the President who signed or vetoed a
    bill, resolved by the date of the action through `holders_as_of`, and the
    public law it became. Congress.gov's action says 'Signed by President.'
    and never names him; the name is a join, which is why it is an edge. The
    law is `ingested`: the law corpus reads the same publisher, so it cannot
    affirm it. Pure. Returns (nodes, edges, gaps)."""
    g = _graph()
    president = [e for e in hold_edges if e["dst"] == node_id("post", EXECUTIVE_POSTS["prez"][0])]
    no_actions = 0
    for snap in snapshots:
        congress = (snap.get("meta") or {}).get("congress")
        for label, rec in (snap.get("instruments") or {}).items():
            itype, number = label.split("/")
            iid = f"instrument/us/{congress}/{itype}/{number}"
            if iid not in instrument_ids:
                continue
            if "actions" not in rec:
                no_actions += 1
                continue
            ref = f"us/{congress}/{itype}/{number}/actions"
            seen = set()
            for a in rec["actions"]:
                text = a.get("text") or ""
                pred = "signed" if text.startswith("Signed by President") else \
                    "vetoed" if "Vetoed by President" in text else None
                if pred is None or (pred, a["date"]) in seen:
                    continue    # the House and the Library each record the one event
                seen.add((pred, a["date"]))
                who = holders_as_of(president, a["date"])
                if len(who) != 1:
                    g["gaps"].append(f"{label}: {pred} on {a['date']} but {len(who)} President(s) "
                                     f"held office that day; no edge")
                    continue
                _edge(g, who[0]["src"], pred, iid, a["date"], a["date"], "ingested", "congress.gov",
                      ref, US, {"role": pred, "action_code": a.get("code"), "text": text,
                                **({"pocket": True} if "Pocket" in text else {})})
            became = [a for a in rec["actions"] if a.get("type") == "BecameLaw"
                      and (a.get("text") or "").startswith("Became")]
            for law in rec.get("laws") or []:
                kind = "pl" if (law.get("type") or "").startswith("Public") else "pvtl"
                lid = f"instrument/us/{kind}/{law['number']}"
                _node(g, lid, "instrument", f"{law.get('type') or 'Public Law'} {law['number']}",
                      {"instrument_type": kind, "number": law["number"], "jurisdiction": US},
                      "congress.gov", ref)
                _edge(g, iid, "enacted_as", lid, became[0]["date"] if became else None,
                      None, "ingested", "congress.gov", ref, US, {"law_type": law.get("type")})
    if no_actions:
        g["gaps"].append(f"{no_actions} bill(s) have no action record on disk; whether they became "
                         f"law is unknown here, not 'no'")
    return list(g["nodes"].values()), list(g["edges"].values()), g["gaps"]


def build_related(snapshots, instrument_ids):
    """Bill-to-bill links from Congress.gov's related-bills record: the
    relationship ('Identical bill', 'Procedurally related') and who said so
    (House, Senate, CRS) ride on the edge. A related bill the session never
    voted on becomes a title-only node, one hop from a voted bill and never
    further: its own related bills are not fetched. Pure."""
    g = _graph()
    title_only = 0
    for snap in snapshots:
        congress = (snap.get("meta") or {}).get("congress")
        for label, rec in (snap.get("instruments") or {}).items():
            itype, number = label.split("/")
            iid = f"instrument/us/{congress}/{itype}/{number}"
            if iid not in instrument_ids or "related" not in rec:
                continue
            for b in rec["related"]:
                did = f"instrument/us/{b['congress']}/{b['type']}/{b['number']}"
                if did == iid:
                    continue
                if did not in instrument_ids and did not in g["nodes"]:
                    _node(g, did, "instrument", f"{b['type'].upper()} {b['number']}: {b.get('title') or ''}".strip(": "),
                          {"instrument_type": b["type"], "congress": b["congress"], "number": b["number"],
                           "title_only": True, "jurisdiction": US}, "congress.gov", f"us/{congress}/{label}/related")
                    title_only += 1
                _edge(g, iid, "related_to", did, None, None, "ingested", "congress.gov",
                      f"us/{congress}/{label}/related", US,
                      {"relationships": b.get("relationships") or [],
                       "types": sorted({r.get("type") for r in b.get("relationships") or [] if r.get("type")})})
    if title_only:
        g["gaps"].append(f"{title_only} related bill(s) had no recorded vote this session: title-only nodes, "
                         f"their own links not followed")
    return list(g["nodes"].values()), list(g["edges"].values()), g["gaps"]


def committee_id(code):
    """Congress.gov's systemCode ('hsju00', 'hsju10') is the thomas id
    lowercased plus the subcommittee id, '00' for the full committee."""
    return node_id("organization", f"us/committee/{code.lower()}")


def build_committees(committees, membership, observed, snapshots, person_ids):
    """Committees and subcommittees as organizations under their chamber
    (joint ones under the country), who sits on them, and which bills were
    referred to and reported by them.

    Membership is `member_of`, never `holds`: a member sits on several
    committees at once and a hold would close the others. The file carries
    no dates, so each seat starts on the day it was observed (`bound_from:
    observed`) and is open. Referrals and reports come from the snapshot's
    Congress.gov records. Pure. Returns (nodes, edges, gaps)."""
    g = _graph()
    parent_of = {"house": node_id("organization", HOUSE_KEY),
                 "senate": node_id("organization", SENATE_KEY), "joint": US}
    known = {}
    for c in committees:
        code = f"{c['thomas_id'].lower()}00"
        cid = committee_id(code)
        known[code] = cid
        _node(g, cid, "organization", c["name"],
              {"natural_key": f"us/committee/{code}", "committee_code": code, "chamber": c["type"],
               "jurisdiction": US}, COMMITTEES_SOURCE, c["thomas_id"])
        _edge(g, parent_of[c["type"]], "has_body", cid, None, None, "ingested", COMMITTEES_SOURCE,
              "seed", US, {"derived": "seed"})
        for sc in c.get("subcommittees") or []:
            scode = f"{c['thomas_id'].lower()}{sc['thomas_id']}"
            sid = committee_id(scode)
            known[scode] = sid
            _node(g, sid, "organization", f"{c['name']}: Subcommittee on {sc['name']}",
                  {"natural_key": f"us/committee/{scode}", "committee_code": scode, "chamber": c["type"],
                   "parent_code": code, "jurisdiction": US}, COMMITTEES_SOURCE, c["thomas_id"] + sc["thomas_id"])
            _edge(g, cid, "has_body", sid, None, None, "ingested", COMMITTEES_SOURCE, "seed", US,
                  {"derived": "seed"})
    unmatched = {}
    for thomas, seats in membership.items():
        code = f"{thomas.lower()}00" if len(thomas) == 4 else thomas.lower()
        cid = known.get(code)
        if cid is None:
            g["gaps"].append(f"membership lists committee {thomas}, which committees-current does not; skipped")
            continue
        for m in seats:
            pid = person_ids.get(m.get("bioguide"))
            if pid is None:
                unmatched[m.get("bioguide")] = m.get("name")
                continue
            _edge(g, pid, "member_of", cid, observed, None, "ingested", COMMITTEES_SOURCE,
                  f"{thomas}/{m.get('bioguide')}", US,
                  {"role": m.get("title") or "Member", "rank": m.get("rank"), "side": m.get("party"),
                   "bound_from": "observed",
                   "observed_note": f"seat observed on {observed}; the membership file carries no dates"})
    if unmatched:
        g["gaps"].append(f"{len(unmatched)} committee member(s) not in the legislators files "
                         f"({', '.join(sorted(v or k for k, v in unmatched.items())[:3])}…) skipped")

    created, unreported = set(), 0
    for snap in snapshots:
        congress = (snap.get("meta") or {}).get("congress")
        for label, rec in (snap.get("instruments") or {}).items():
            itype, number = label.split("/")
            iid = f"instrument/us/{congress}/{itype}/{number}"
            ref = f"us/{congress}/{itype}/{number}/committees"

            def unit(code, name=None, chamber=None):
                # A committee the bill names but the current file does not
                # (renamed, abolished) still exists for this bill's history.
                if code not in known:
                    cid = committee_id(code)
                    _node(g, cid, "organization", name or code.upper(),
                          {"natural_key": f"us/committee/{code}", "committee_code": code,
                           "chamber": (chamber or "").lower() or None, "jurisdiction": US},
                          "congress.gov", ref)
                    known[code] = cid
                    created.add(code)
                return known[code]

            reported_by = set()
            for rpt in rec.get("reports") or []:
                for code in rpt["committees"] or [None]:
                    if code is None:
                        unreported += 1
                        continue
                    _edge(g, unit(code), "reported", iid, rpt["date"] or None, rpt["date"] or None,
                          "ingested", "congress.gov", f"report/{rpt['citation']}", US,
                          {"citation": rpt["citation"]})
                    reported_by.add(code)
            for c in rec.get("committees") or []:
                if not c.get("code"):
                    continue
                for act in c["activities"]:
                    name = (act.get("name") or "").lower()
                    if name.startswith("referred to") or name == "referral":
                        _edge(g, iid, "referred_to", unit(c["code"], c.get("name"), c.get("chamber")),
                              act["date"] or None, act["date"] or None, "ingested", "congress.gov",
                              f"{ref}/{c['code']}/{act['date']}", US, {"activity": act.get("name")})
                    elif name.startswith("reported") and c["code"] not in reported_by:
                        # Reported without a written report on disk.
                        _edge(g, unit(c["code"], c.get("name"), c.get("chamber")), "reported", iid,
                              act["date"] or None, act["date"] or None, "ingested", "congress.gov",
                              f"{ref}/{c['code']}/{act['date']}", US,
                              {"citation": None, "activity": act.get("name")})
    if created:
        g["gaps"].append(f"{len(created)} committee(s) named by a bill are not in committees-current "
                         f"({', '.join(sorted(created)[:5])}); kept with the bill record's name")
    if unreported:
        g["gaps"].append(f"{unreported} committee report(s) name no committee; no `reported` edge for them")
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
                errors.append(f"{url}: {_redact(e)}")
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
                errors.append(f"{url}: {_redact(e)}")
            roll += 1
            time.sleep(pause)
    # The bill records come from GovInfo's BILLSTATUS (sources/govinfo.py →
    # bills-<congress>.json), joined at build time; the snapshot holds the
    # roll calls only.
    out = {"meta": {"congress": congress, "session": session, "year": year,
                    "fetched": datetime.datetime.now().isoformat(timespec="seconds"),
                    "counts": {c: sum(1 for v in votes if v["chamber"] == c)
                               for c in ("house", "senate")},
                    "errors": errors},
           "votes": votes}
    pathlib.Path(out_path).write_text(json.dumps(out, separators=(",", ":")))
    return out["meta"]


_BILL_TYPES = {"hr", "s", "hres", "sres", "hjres", "sjres", "hconres", "sconres"}


_API_KEY_RE = re.compile(r"api_key=[^&\s'\"]+")


def _redact(e):
    """An exception's text, safe to write into a snapshot that gets
    committed: requests puts the full URL, api_key included, in its
    messages."""
    return f"{type(e).__name__}: {_API_KEY_RE.sub('api_key=REDACTED', str(e))}"


def _presidential(action):
    """The actions kept: what the President did and what became law. The
    rest of a bill's history is unbounded and not a graph fact."""
    text = action.get("text") or ""
    return action.get("type") in ("President", "BecameLaw") or "Vetoed" in text


def _report_endpoint(citation, url):
    """'119/HRPT/106' from a report's url, else from its citation. The
    parser is the report fetcher's, which is pure; its fetch is not reused
    because it returns [] when its breaker is open."""
    from sources.committee_reports_fetcher import _parse_citation_to_endpoint
    ep = _parse_citation_to_endpoint(citation or "", url)
    return f"{ep[0]}/{ep[1]}/{ep[2]}" if ep else None


# ------------------------------------------------------------------ money

FEC_TOP_PACS = 25
# Conduits pass individual money through; they are not a PAC's choice.
_FEC_CONDUITS = ("ACTBLUE", "WINRED")


def fec_candidate_ids(leg):
    """The FEC candidate ids for the chamber the member sits in now: a
    senator who once ran for the House has both, and only the S id raises
    Senate money. Never a name search."""
    prefix = "S" if leg["terms"][-1]["type"] == "sen" else "H"
    return [c for c in leg["id"].get("fec", []) if c.startswith(prefix)]


def top_pacs(receipts, candidate_name, limit=FEC_TOP_PACS):
    """Schedule A line 11C rows → the PACs that gave the most, summed over
    every receipt. Conduits and the candidate's own committees drop out.
    Pure. Returns (top rows, total dollars, receipt count)."""
    own = {t.upper() for t in re.split(r"\W+", candidate_name or "") if len(t) > 2}
    agg = {}
    for r in receipts:
        name = (r.get("contributor_name") or "").strip()
        up, amt = name.upper(), r.get("contribution_receipt_amount") or 0
        if not name or r.get("entity_type") == "CAN" or any(c in up for c in _FEC_CONDUITS) \
                or any(t in up.split() for t in own):
            continue
        key = r.get("contributor_committee_id") or up
        a = agg.setdefault(key, {"name": name, "committee_id": r.get("contributor_committee_id"),
                                 "amount": 0.0, "receipts": 0})
        a["amount"] = round(a["amount"] + amt, 2)
        a["receipts"] += 1
    rows = sorted(agg.values(), key=lambda a: -a["amount"])
    return rows[:limit], round(sum(a["amount"] for a in rows), 2), sum(a["receipts"] for a in rows)


def _fec_snapshots():
    return [json.loads(p.read_text()) for p in sorted(DATA_DIR.glob("fec-*.json"))]


def build_money(fec_snapshots, person_ids):
    """Each member → their principal campaign committee, one edge per
    cycle, bounded by the cycle, with the committee's totals on the edge
    (an edge carries its source and its cycle; a node would not). PAC
    detail stays in the snapshot and is read at answer time. Pure."""
    g = _graph()
    missing = 0
    for snap in fec_snapshots:
        cycle = snap["meta"]["cycle"]
        for bio, rec in snap["members"].items():
            pid = person_ids.get(bio)
            if pid is None or not rec.get("committee_id"):
                missing += pid is not None
                continue
            cid = node_id("organization", f"fec/committee/{rec['committee_id']}")
            _node(g, cid, "organization", rec.get("committee_name") or rec["committee_id"],
                  {"natural_key": f"fec/committee/{rec['committee_id']}", "fec_id": rec["committee_id"],
                   "jurisdiction": US}, "fec", rec["committee_id"])
            _edge(g, pid, "campaign_committee", cid, f"{cycle - 1}-01-01", f"{cycle}-12-31", "ingested",
                  "fec", f"fec/{cycle}/{rec['committee_id']}", US,
                  {"cycle": cycle, "candidate_id": rec.get("candidate_id"), **(rec.get("totals") or {}),
                   "pac_total": rec.get("pac_total"), "pac_receipts": rec.get("pac_receipts")})
    if missing:
        g["gaps"].append(f"{missing} member-cycle(s) have no FEC principal committee on disk")
    return list(g["nodes"].values()), list(g["edges"].values()), g["gaps"]


def _instrument_matches(iid, title, topic, policy):
    """The memory backend's topic test, for a vote read from a file."""
    ref = _bill_ref(topic)
    if ref:
        return bool(iid) and iid.endswith(f"/{ref[0]}/{ref[1]}")
    t = topic.lower()
    return (policy or "").lower().startswith(t) or t in title.lower()


def _snapshot_congress_of(path):
    """The Congress in a snapshot's filename (congress-votes-<c>-<n>.json),
    so a caller can pick files without parsing any of them."""
    m = re.match(r"congress-votes-(\d+)-\d+\.json$", path.name)
    return int(m.group(1)) if m else None


def snapshot_votes(persons, year, topic, limit, loaded_congress):
    """A person's votes in one year, read from the roll-call snapshots of
    sessions that are not loaded as edges. Same row shape as the graph's.
    Only the files of the Congresses that can hold the year are opened: the
    one it falls in, and the one before, whose last session ran into March
    of an odd year until 1935. Returns (rows, total, truncated, files read)."""
    rows, files = [], []
    wanted = {(year - 1789) // 2 + 1, (year - 1789) // 2}
    for p in sorted(DATA_DIR.glob("congress-votes-*.json")):
        if _snapshot_congress_of(p) not in wanted:
            continue
        snap = json.loads(p.read_text())
        meta = snap.get("meta") or {}
        if meta.get("year") != year or meta.get("congress") == loaded_congress:
            continue
        files.append(p.name)
        recs = snap.get("instruments") or {}
        for v in snap.get("votes", []):
            for person in persons:
                mid = person.get("lis") if v.get("id_kind") == "lis" else person.get("bioguide")
                pos = next((k for k, ids in v.get("positions", {}).items() if mid and mid in ids), None)
                if pos is None:
                    continue
                inst = _parse_instrument(v["chamber"], v)
                iid = f"instrument/us/{v['congress']}/{inst[0]}/{inst[1]}" if inst else None
                rec = recs.get(f"{inst[0]}/{inst[1]}") or {} if inst else {}
                label = v.get("legis_num") or v.get("document_name") or ""
                title = f"{label}: {rec.get('title') or v.get('description') or ''}".strip(": ")
                if topic and not _instrument_matches(iid, title, topic, rec.get("policy_area")):
                    continue
                rows.append({"person_id": person["id"], "person": person["name"], "position": pos,
                             "date": v["date"], "certification": "ingested", "vote_id": v["vote_id"],
                             "question": v.get("question"), "item_id": iid, "title": title,
                             "instrument_type": inst[0] if inst else None, "jurisdiction": US,
                             "topic": rec.get("policy_area"),
                             "topic_derived_by": "congress.gov policyArea" if rec.get("policy_area") else None,
                             "result": v.get("result"), "meeting_id": None})
    rows.sort(key=lambda r: (r["date"], r["item_id"] or ""), reverse=True)
    return rows[:limit], len(rows), len(rows) > limit, files


def _pac_label(source):
    """Where a PAC row's dollars were reported. The bulk file counts what
    each PAC said it gave (its own filing, 24K); the API path counted what
    the campaign said it received (Schedule A line 11C). The two filings
    do not always agree, so the label names the one that was read."""
    if (source or "").startswith("FEC bulk"):
        return "FEC bulk pas2, 24K contributions filed by the PAC"
    return "FEC Schedule A line 11C"


def fec_detail(bioguide, cycle):
    """The top PACs for one member and cycle, read from the snapshot at
    answer time: contributions are events, and events stay at the leaf."""
    p = DATA_DIR / f"fec-{cycle}.json"
    if not p.exists():
        return None
    snap = json.loads(p.read_text())
    rec = snap["members"].get(bioguide)
    return rec and {**rec, "_source": snap["meta"].get("source") or ""}


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
    live = [e for e in hold_edges
            if e["valid_from"] is not None and str(e["valid_from"]) <= as_of
            and (e["valid_to"] is None or str(e["valid_to"]) >= as_of)]
    # Handover day: the legislators and executive files end a term on the
    # day the next one begins (3 January, 20 January at noon). The seat has
    # one holder that day, and it is the incoming one. Only a hold that
    # ends exactly when another on the same seat starts gives way, so an
    # inferred end ('the day before the successor') keeps its last day.
    starts = {(e.get("dst"), str(e["valid_from"])) for e in live}
    return [e for e in live
            if not (e["valid_to"] is not None and str(e["valid_to"]) == as_of
                    and (e.get("dst"), as_of) in starts and str(e["valid_from"]) != as_of)]


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


def _congress_snapshots(congress):
    return [json.loads(p.read_text()) for p in sorted(DATA_DIR.glob(f"congress-votes-{congress}-*.json"))]


MEMBER_CONGRESS_PATH = DATA_DIR / "member-congress.json"


def write_member_congress(loaded_congress):
    """Reduce every roll-call snapshot of a Congress that is not loaded as
    edges to member-congress.json, one file in memory at a time. Returns
    (members, files read)."""
    index, files = {}, []
    for p in sorted(DATA_DIR.glob("congress-votes-*.json")):
        if _snapshot_congress_of(p) in (None, loaded_congress):
            continue
        member_congress_index([json.loads(p.read_text())], index)
        files.append(p.name)
    MEMBER_CONGRESS_PATH.write_text(json.dumps(
        {"meta": {"built": datetime.date.today().isoformat(), "files": files}, "members": index},
        separators=(",", ":"), sort_keys=True))
    return len(index), files


def merge_legislators(current, historical):
    """One list, one record per bioguide id, each tagged with the file it
    came from. The current file wins a collision: it is maintained, and
    the historical one only receives a member after they leave. Pure.
    Returns (legislators, gaps)."""
    seen = {leg["id"].get("bioguide") for leg in current} - {None}
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
    out, changed = {}, []
    for name in names:
        r = requests.get(PUBLIC_DATA_URL.format(name), timeout=60)
        r.raise_for_status()
        json.loads(r.content)   # fail-closed: never overwrite a good file with a bad one
        path = DATA_DIR / f"{name}.json"
        if not path.exists() or path.read_bytes() != r.content:
            path.write_bytes(r.content)
            changed.append(name)
        out[name] = len(r.content)
    # The date a file's content was first seen, not the last download: a
    # committee seat observed in March is still observed in March.
    fetched = json.loads(FETCHED_PATH.read_text()) if FETCHED_PATH.exists() else {}
    fetched.update({name: datetime.date.today().isoformat() for name in changed})
    FETCHED_PATH.write_text(json.dumps(fetched, indent=1, sort_keys=True) + "\n")
    return out


def build_source(source, states=None):
    """Read the inputs for one source off disk and build. Returns
    (nodes, edges, gaps, delete scopes)."""
    if source == "us-congress":
        current = json.loads((DATA_DIR / "legislators-current.json").read_text())
        hist_path = DATA_DIR / "legislators-historical.json"
        historical = json.loads(hist_path.read_text()) if hist_path.exists() else []
        legislators, merge_gaps = merge_legislators(current, historical)
        # The current Congress's roll calls are edges; older sessions stay in
        # their files (the events rule) and only certify terms, through the
        # index write_member_congress reduced them to.
        congress = current_session()[0]
        snaps = _congress_snapshots(congress)
        if not snaps:
            raise RuntimeError(f"no data/congress-votes-{congress}-*.json — "
                               f"run `python graph.py snapshot {congress} 2 2026`")
        bills_path = DATA_DIR / f"bills-{congress}.json"
        if bills_path.exists():
            bills = json.loads(bills_path.read_text())
            # Every bill of the Congress is a node, voted on or not. The
            # records ride in one snapshot without votes, so each builder
            # reads each record once; the roll-call files' own copies (from
            # the old Congress.gov fetch) are dropped.
            for snap in snaps:
                snap.pop("instruments", None)
            snaps.insert(0, {"meta": {"congress": congress, "instruments_fetched": bills["meta"]["fetched"]},
                             "votes": [], "instruments": bills["instruments"]})
        else:
            merge_gaps.append(f"no bills-{congress}.json: bill records come from the snapshots' own copy, "
                        f"if any (run `python -m sources.govinfo billstatus {congress}`)")
        cert = json.loads(MEMBER_CONGRESS_PATH.read_text()) if MEMBER_CONGRESS_PATH.exists() else None
        nodes, edges, gaps = build_congress(legislators, snaps, states,
                                            cert_index=(cert or {}).get("members"))
        if cert:
            gaps.append(f"{len(cert['meta']['files'])} older session snapshot(s) certify terms through "
                        f"member-congress.json (built {cert['meta']['built']}) and answer votes from "
                        f"the file; not loaded as edges")
        else:
            gaps.append("no member-congress.json: no term outside the current Congress is certified "
                        "(run `python graph.py certify-index`)")
        gaps = merge_gaps + gaps
        exec_path = DATA_DIR / "executive.json"
        if exec_path.exists():
            xn, xe, xg = build_executive(json.loads(exec_path.read_text()))
            known = {n["id"] for n in nodes}
            nodes += [n for n in xn if n["id"] not in known]
            edges += xe
            gaps += xg
            en, ee, eg = build_enactment(snaps, xe, {n["id"] for n in nodes if n["kind"] == "instrument"})
            nodes += en
            edges += ee
            gaps += eg
        else:
            gaps.append("no data/executive.json: the presidency is not loaded (run `python graph.py fetch`)")
        cpath, mpath = DATA_DIR / "committees-current.json", DATA_DIR / "committee-membership-current.json"
        if cpath.exists() and mpath.exists():
            fetched = json.loads(FETCHED_PATH.read_text()) if FETCHED_PATH.exists() else {}
            observed = fetched.get("committee-membership-current")
            if not observed:
                observed = datetime.date.today().isoformat()
                gaps.append("committee membership has no recorded download date; observed as of today")
            person_ids = {n["props"]["bioguide"]: n["id"] for n in nodes
                          if n["kind"] == "person" and n["props"].get("bioguide")}
            cn, ce, cg = build_committees(json.loads(cpath.read_text()), json.loads(mpath.read_text()),
                                          observed, snaps, person_ids)
            nodes += cn
            edges += ce
            gaps += cg
        else:
            gaps.append("no committee files in data/: committees are not loaded (run `python graph.py fetch`)")
        rn, re_, rg = build_related(snaps, {n["id"] for n in nodes if n["kind"] == "instrument"})
        nodes += rn
        edges += re_
        gaps += rg
        fecs = _fec_snapshots()
        if fecs:
            person_ids = {n["props"]["bioguide"]: n["id"] for n in nodes
                          if n["kind"] == "person" and n["props"].get("bioguide")}
            mn, me, mg = build_money(fecs, person_ids)
            nodes += mn
            edges += me
            gaps += mg
        else:
            gaps.append("no data/fec-*.json: campaign money is not loaded (run `python -m sources.fec_client bulk 2026`)")
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
               props->>'bioguide' AS bioguide, props->>'lis' AS lis
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
# "HR 1", "H.R. 1", "S 3627", "HJRes 3" → (type, number). A bill number
# matches the instrument id, not the title: the clerk writes "H R 1", and
# "%H R 1%" would also match H R 10.
_BILL_REF = re.compile(r"^\s*(h\.?\s*r|s|h\.?\s*res|s\.?\s*res|h\.?\s*j\.?\s*res|s\.?\s*j\.?\s*res|"
                       r"h\.?\s*con\.?\s*res|s\.?\s*con\.?\s*res)\.?\s*(\d+)\s*$", re.I)


def _bill_ref(topic):
    m = _BILL_REF.match(topic or "")
    return (re.sub(r"[^a-z]", "", m.group(1).lower()), m.group(2)) if m else None


def _topic_sql(topic):
    """(clause, args) for the instrument alias `i`: a bill number by id,
    anything else by policy area or title."""
    ref = _bill_ref(topic)
    if ref:
        return " i.id LIKE %s", [f"instrument/us/%/{ref[0]}/{ref[1]}"]
    return _TOPIC_SQL, [f"{topic}%", f"%{topic}%"]


def _pg_votes(cur, person_ids, topic, limit, predicate="voted_on"):
    """(rows, total edges of this predicate by these people, truncated)."""
    cur.execute("SELECT COUNT(*) AS n FROM graph_edge "
                "WHERE predicate = %s AND src = ANY(%s)", (predicate, person_ids))
    total = cur.fetchone()["n"]
    sql, args = _VOTE_ROW_SQL.replace("'voted_on'", "%s") + " WHERE p.id = ANY(%s)", [predicate, person_ids]
    if topic:
        clause, targs = _topic_sql(topic)
        sql += " AND" + clause
        args += targs
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
_ASK_LAW = (
    re.compile(r"^\s*who\s+(?:signed|vetoed)\s+(?:the\s+)?(?P<topic>.+?)\s*\??\s*$", re.I),
    re.compile(r"^\s*(?:did|has|was|is)\s+(?:the\s+)?(?P<topic>.+?)\s+(?:become|became|made|(?:get\s+)?signed|"
               r"enacted|vetoed|(?:a\s+)?law)(?:\s+(?:a\s+)?law|\s+into\s+law)?\s*\??\s*$", re.I),
)
_ASK_SIGNED_BY = re.compile(
    r"^\s*(?:what|which)\s+(?:bills?\s+|laws?\s+)?(?:did|has)\s+(?:president\s+)?(?P<person>.+?)\s+"
    r"(?P<verb>sign|signed|veto|vetoed)\s*\??\s*$", re.I)
_ASK_COMMITTEE = (
    re.compile(r"^\s*who\s+(?P<role>chairs|chaired|leads|is\s+(?:the\s+)?(?:chair(?:man|woman)?|ranking\s+member)\s+(?:of|on))"
               r"\s+(?:the\s+)?(?P<committee>.+?)\s*\??\s*$", re.I),
    re.compile(r"^\s*(?:who\s+(?:sits|serves|is|are)\s+on|(?:the\s+)?members\s+of)\s+(?:the\s+)?"
               r"(?P<committee>.*?\bcommittee\b.*?)\s*\??\s*$", re.I),
)
_ASK_REFERRALS = re.compile(
    r"^\s*(?:what|which)\s+committees?\s+(?:has|have|had|did|is|was)\s+(?:the\s+)?(?P<topic>.+?)"
    r"(?:\s+(?:been\s+)?(?:referred\s+to|go\s+to|in|sent\s+to))?\s*\??\s*$", re.I)
_ASK_REPORTED = re.compile(
    r"^\s*what\s+(?:bills?\s+)?(?:did|has)\s+(?:the\s+)?(?P<committee>.+?)\s+report(?:ed)?\s*\??\s*$", re.I)
_ASK_RELATED = re.compile(
    r"^\s*(?:what|which)?\s*(?:other\s+)?(?:bills?|legislation)\s+(?:are\s+|is\s+)?(?:related|similar|linked)\s+to\s+"
    r"(?:the\s+)?(?P<topic>.+?)\s*\??\s*$", re.I)
_ASK_FUNDS = re.compile(
    r"^\s*(?:who\s+(?:funds|funded|finances|financed|gives?\s+(?:money\s+)?to|donates?\s+to|donated\s+to)|"
    r"(?:which|what)\s+pacs?\s+(?:fund|funded|give\s+to|gave\s+to|support|supported))\s+"
    r"(?P<person>.+?)(?:\s+in\s+(?P<cycle>\d{4}))?\s*\??\s*$", re.I)
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
    r"\b(seat|district|supervisor|representative|senator|chair(?:man|woman)?|delegate|president)s?\b"
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
    for rx in _ASK_COMMITTEE:
        m = rx.match(q)
        if m:
            role = (m.groupdict().get("role") or "").lower()
            if role.startswith("is") and "committee" not in m.group("committee").lower():
                break   # "who is the chair of the Board of Supervisors" is a seat
            return {"ask": "committee", "committee": m.group("committee").strip(),
                    "role": "ranking" if "ranking" in role else "chair" if role else None}
    m = _ASK_FUNDS.match(q)
    if m:
        cycle = int(m.group("cycle")) if m.group("cycle") else None
        return {"ask": "funds", "person": m.group("person").strip(),
                "cycle": cycle + cycle % 2 if cycle else None}
    m = _ASK_RELATED.match(q)
    if m:
        return {"ask": "related", "topic": m.group("topic").strip()}
    m = _ASK_REFERRALS.match(q)
    if m:
        return {"ask": "referrals", "topic": m.group("topic").strip()}
    m = _ASK_REPORTED.match(q)
    if m:
        return {"ask": "reported", "committee": m.group("committee").strip()}
    m = _ASK_SIGNED_BY.match(q)
    if m:
        return {"ask": "signed_by", "person": m.group("person").strip(),
                "predicate": "vetoed" if m.group("verb").lower().startswith("veto") else "signed"}
    for rx in _ASK_LAW:
        m = rx.match(q)
        if m:
            return {"ask": "law", "topic": m.group("topic").strip()}
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
    # "… in 2023": a year scopes a vote question, and may send it to a file.
    ym = re.search(r"\s+in\s+(\d{4})\s*\??\s*$", q)
    for rx in _ASK_VOTES:
        m = rx.match(q[:ym.start()] if ym else q)
        if m:
            topic = (m.group("topic") or "").strip() or None
            out = {"ask": "votes", "person": m.group("person").strip(), "topic": topic}
            return out | {"year": int(ym.group(1))} if ym else out
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
    # "President" is inside "Vice President": say which one is not meant.
    exclude = ["!vice"] if "president" in q and "vice" not in q else []
    return state + [t for t in re.split(r"[^a-z0-9]+", q) if t] + exclude


def _post_matches(post, terms):
    hay = f"{post['props'].get('natural_key', '')} {post['props'].get('role', '')} {post['name']}".lower()
    return all((t[1:] not in hay) if t.startswith("!") else (t in hay) for t in terms)


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
        ref = _bill_ref(topic)
        if ref:
            return i["id"].startswith("instrument/us/") and i["id"].endswith(f"/{ref[0]}/{ref[1]}")
        t = topic.lower()
        return (i["props"].get("topic") or "").lower().startswith(t) or t in i["name"].lower()

    def committees(query):
        return committee_matches([n for n in nodes if n["kind"] == "organization"
                                  and n["props"].get("natural_key", "").startswith("us/committee/")], query)

    def items(topic, limit):
        return sorted(({"id": n["id"], "name": n["name"], "props": n["props"]} for n in nodes
                       if n["kind"] == "instrument" and topic_ok(n, topic)
                       and n["props"].get("instrument_type") not in _LAW_TYPES),
                      key=lambda n: n["id"])[:limit]

    def edges_of(node_ids, predicate, direction):
        """direction 'out': node → other; 'in': other → node."""
        side = out if direction == "out" else inn
        return [{"src": e["src"], "src_name": by_id[e["src"]]["name"], "dst": e["dst"],
                 "dst_name": by_id[e["dst"]]["name"], "valid_from": e["valid_from"],
                 "valid_to": e["valid_to"], "certification": e["certification"],
                 "source_id": e["source_id"], "source_ref": e["source_ref"], "props": e["props"]}
                for nid in node_ids for e in side.get(nid, []) if e["predicate"] == predicate]

    def laws(topic, limit):
        # Federal bills only: a county motion has no President and no public law.
        items = sorted((n for n in nodes if n["kind"] == "instrument" and topic_ok(n, topic)
                        and n["id"].startswith("instrument/us/")
                        and n["props"].get("instrument_type") not in _LAW_TYPES),
                       key=lambda n: n["id"])[:limit + 1]
        raw = []
        for i in items:
            es = [e for e in out.get(i["id"], []) if e["predicate"] == "enacted_as"] + \
                 [e for e in inn.get(i["id"], []) if e["predicate"] in ("signed", "vetoed")]
            base = {"item_id": i["id"], "title": i["name"],
                    "actions_fetched": i["props"].get("actions_fetched")}
            raw += [base | {"predicate": e["predicate"], "date": e["valid_from"],
                            "certification": e["certification"],
                            "other": by_id[e["dst"] if e["src"] == i["id"] else e["src"]]["name"]}
                    for e in es] or [base | {"predicate": None}]
        return raw

    def persons(query):
        toks = _name_tokens(query) or [query.lower()]
        has_all = lambda text: all(t in text.lower() for t in toks)  # noqa: E731
        return sorted(({"id": n["id"], "name": n["name"], "aliases": n["props"].get("aliases", []),
                        "seat": n["props"].get("seat"), "bioguide": n["props"].get("bioguide"),
                        "lis": n["props"].get("lis")}
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
            "holds": holds_of, "voters": voters, "laws": laws, "committees": committees,
            "items": items, "edges": edges_of, "loaded": lambda: bool(nodes)}


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

    def committees(query):
        def q(cur):
            cur.execute("SELECT id, name, props FROM graph_node WHERE kind = 'organization' "
                        "AND props->>'natural_key' LIKE 'us/committee/%%'")
            return committee_matches(cur.fetchall(), query)
        return run(q)

    def items(topic, limit):
        clause, targs = _topic_sql(topic)

        def q(cur):
            cur.execute(f"""SELECT i.id, i.name, i.props FROM graph_node i
                            WHERE i.kind = 'instrument'
                              AND COALESCE(i.props->>'instrument_type', '') <> ALL(%s) AND {clause}
                            ORDER BY i.id LIMIT %s""", [list(_LAW_TYPES)] + targs + [limit])
            return cur.fetchall()
        return run(q)

    def edges_of(node_ids, predicate, direction):
        near, far = ("src", "dst") if direction == "out" else ("dst", "src")

        def q(cur):
            cur.execute(f"""
                SELECT e.src, s.name AS src_name, e.dst, d.name AS dst_name, e.valid_from, e.valid_to,
                       e.certification, e.source_id, e.source_ref, e.props
                FROM graph_edge e JOIN graph_node s ON s.id = e.src JOIN graph_node d ON d.id = e.dst
                WHERE e.predicate = %s AND e.{near} = ANY(%s)""", (predicate, list(node_ids)))
            rows = cur.fetchall()
            for r in rows:
                r["valid_from"] = r["valid_from"].isoformat() if r["valid_from"] else None
                r["valid_to"] = r["valid_to"].isoformat() if r["valid_to"] else None
            return rows
        return run(q)

    def laws(topic, limit):
        clause, targs = _topic_sql(topic)

        def q(cur):
            cur.execute(f"""
                WITH items AS (
                    SELECT i.id, i.name, i.props->>'actions_fetched' AS actions_fetched
                    FROM graph_node i
                    WHERE i.kind = 'instrument' AND i.id LIKE 'instrument/us/%%'
                      AND COALESCE(i.props->>'instrument_type', '') <> ALL(%s) AND {clause}
                    ORDER BY i.id LIMIT %s)
                SELECT it.id AS item_id, it.name AS title, it.actions_fetched,
                       e.predicate, e.valid_from AS date, e.certification, o.name AS other
                FROM items it
                LEFT JOIN graph_edge e
                  ON (e.src = it.id AND e.predicate = 'enacted_as')
                  OR (e.dst = it.id AND e.predicate IN ('signed', 'vetoed'))
                LEFT JOIN graph_node o ON o.id = CASE WHEN e.src = it.id THEN e.dst ELSE e.src END
                ORDER BY it.id""", [list(_LAW_TYPES)] + targs + [limit + 1])
            rows = cur.fetchall()
            for r in rows:
                r["date"] = r["date"].isoformat() if r["date"] else None
            return rows
        return run(q)

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
            clause, targs = _topic_sql(topic)
            sql = _VOTE_ROW_SQL.replace("'voted_on'", "%s") + " WHERE" + clause
            args = [predicate] + targs
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
            "holds": holds_of, "voters": voters, "laws": laws, "committees": committees,
            "items": items, "edges": edges_of, "loaded": loaded}


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
_COMMITTEE_STOP = {"the", "committee", "committees", "on", "of", "and", "for", "house", "senate",
                   "joint", "subcommittee", "select", "permanent", "u.s.", "us"}


def committee_matches(orgs, query):
    """Committee organizations whose name holds every word of the query.
    'House' or 'Senate' narrows the chamber; a full committee beats its
    subcommittees unless the query says 'subcommittee'. Pure; both
    backends feed it the committee rows."""
    q = query.lower()
    words = [w for w in re.split(r"[^a-z0-9.']+", q) if w and w not in _COMMITTEE_STOP]
    if not words:
        return []
    chamber = "house" if re.search(r"\bhouse\b", q) else "senate" if re.search(r"\bsenate\b", q) else \
        "joint" if re.search(r"\bjoint\b", q) else None
    hits = [o for o in orgs if all(w in o["name"].lower() for w in words)
            and (chamber is None or o["props"].get("chamber") == chamber)]
    if "subcommittee" not in q and any(not o["props"].get("parent_code") for o in hits):
        hits = [o for o in hits if not o["props"].get("parent_code")]
    return sorted(hits, key=lambda o: o["name"])
_LAW_TYPES = ("pl", "pvtl")


def law_rows(raw, limit):
    """The `laws` lookup's rows (one per instrument × edge) → one answer row
    per instrument: signed, vetoed, law, or which of the two unknowns it
    is. 'Not law' is only said as of the day the actions were read, and never
    for a bill whose actions were not read. Pure; both backends feed it."""
    by_item = {}
    for r in raw:
        by_item.setdefault(r["item_id"], []).append(r)
    rows = []
    for item_id, rs in list(by_item.items())[:limit]:
        got = {p: [r for r in rs if r["predicate"] == p] for p in ("enacted_as", "signed", "vetoed")}
        first, notes = rs[0], []
        for r in got["signed"]:
            notes.append(f"signed by {r['other']} on {r['date']}")
        for r in got["vetoed"]:
            notes.append(f"vetoed by {r['other']} on {r['date']}")
        edges = got["enacted_as"] + got["signed"] + got["vetoed"]
        if got["enacted_as"]:
            position = "law"
            notes.append(", ".join(r["other"] for r in got["enacted_as"])
                         + (" over the veto" if got["vetoed"] else ""))
        elif got["vetoed"]:
            position = "vetoed"
        elif first.get("actions_fetched"):
            position = "not law"
            notes.append(f"no law on record as of {first['actions_fetched']}")
        else:
            position = "unknown"
            notes.append("no action record on disk; whether it became law is unknown here")
        signer = got["signed"] or got["vetoed"]
        rows.append({"item_id": item_id, "title": first["title"], "position": position,
                     "person": signer[0]["other"] if signer else None,
                     "date": max((r["date"] for r in edges if r.get("date")), default=None),
                     "certification": min((r["certification"] for r in edges),
                                          key=CERTIFICATION_RANK.get, default="ingested"),
                     "law": got["enacted_as"][0]["other"] if got["enacted_as"] else None,
                     "question": "; ".join(notes), "jurisdiction": US})
    return rows, len(by_item) > limit


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
    if ask == "signed_by":
        pred = parsed["predicate"]
        persons = backend["persons"](parsed["person"])
        if not persons:
            return shape_answer([], [], parsed["person"], None) | {"ask": ask}
        which, persons = _one_person(persons, parsed["person"], None, ask, backend)
        if which:
            return which
        rows, total, truncated = backend["votes"]([p["id"] for p in persons], None, limit, pred)
        out = shape_answer(rows, persons, parsed["person"], None, total, truncated, predicate=pred)
        if not rows:
            out["empty_reason"] = (f"no bill on disk records {', '.join(p['name'] for p in persons)} as "
                                   f"having {pred} it (only bills with a recorded vote are loaded)")
        return out | {"ask": ask, "predicate": pred}
    if ask == "law":
        rows, truncated = law_rows(backend["laws"](topic, limit), limit)
        counts = {}
        for r in rows:
            counts[r["certification"]] = counts.get(r["certification"], 0) + 1
        hops = [{"predicate": "enacted_as", "weakest": min(counts, key=CERTIFICATION_RANK.get),
                 "counts": counts}] if rows else []
        return {"ask": ask, "query": topic, "topic": topic, "rows": rows, "count": len(rows),
                "truncated": truncated, "persons": sorted({r["person"] for r in rows if r["person"]}),
                "hops": hops, "weak_hops": [h for h in hops if h["weakest"] != "certified"],
                "advisory_fields": [], "place_ignored": place,
                "empty_reason": None if rows else f"no instrument in the graph matches {topic!r}"}
    if ask in ("committee", "reported"):
        cmts = backend["committees"](parsed["committee"])
        if not cmts:
            return {"ask": ask, "query": parsed["committee"], "rows": [], "hops": [], "weak_hops": [],
                    "empty_reason": f"no committee in the graph matches {parsed['committee']!r}"}
        names = {c["id"]: c["name"] for c in cmts}
        if ask == "committee":
            es = backend["edges"](list(names), "member_of", "in")
            role = parsed.get("role")
            if role == "chair":
                es = [e for e in es if (e["props"].get("role") or "").lower().startswith("chair")]
            elif role == "ranking":
                es = [e for e in es if (e["props"].get("role") or "").lower() == "ranking member"]
            es.sort(key=lambda e: (names[e["dst"]], e["props"].get("side") != "majority",
                                   e["props"].get("rank") or 999))
            rows = [{"person": e["src_name"], "title": names[e["dst"]], "position": e["props"].get("role"),
                     "date": e["valid_from"], "certification": e["certification"], "jurisdiction": US,
                     "question": f"{e['props'].get('side')} · rank {e['props'].get('rank')} · "
                                 f"{e['props'].get('observed_note')}"} for e in es[:limit]]
            pred = "member_of"
            empty = f"no {'chair' if role else 'member'} of {', '.join(names.values())} on disk"
        else:
            es = sorted(backend["edges"](list(names), "reported", "out"),
                        key=lambda e: (e["valid_from"] or "", e["dst"]), reverse=True)
            rows = [{"person": names[e["src"]], "title": e["dst_name"], "position": "reported",
                     "date": e["valid_from"], "certification": e["certification"], "jurisdiction": US,
                     "item_id": e["dst"], "question": e["props"].get("citation") or "no written report on disk"}
                    for e in es[:limit]]
            pred = "reported"
            empty = (f"no bill on disk reported by {', '.join(names.values())} "
                     f"(only bills with a recorded vote are loaded)")
        counts = {}
        for r in rows:
            counts[r["certification"]] = counts.get(r["certification"], 0) + 1
        hops = [{"predicate": pred, "weakest": min(counts, key=CERTIFICATION_RANK.get), "counts": counts}] if rows else []
        return {"ask": ask, "query": parsed["committee"], "committees": list(names.values()), "role": parsed.get("role"),
                "rows": rows, "count": len(rows), "truncated": len(es) > limit,
                "persons": sorted({r["person"] for r in rows}), "hops": hops,
                "weak_hops": [h for h in hops if h["weakest"] != "certified"],
                "empty_reason": None if rows else empty}
    if ask == "funds":
        persons = backend["persons"](parsed["person"])
        if not persons:
            return shape_answer([], [], parsed["person"], None) | {"ask": ask}
        which, persons = _one_person(persons, parsed["person"], None, ask, backend)
        if which:
            return which
        es = sorted(backend["edges"]([p["id"] for p in persons], "campaign_committee", "out"),
                    key=lambda e: -e["props"]["cycle"])
        if parsed.get("cycle"):
            es = [e for e in es if e["props"]["cycle"] == parsed["cycle"]]
        who = ", ".join(p["name"] for p in persons)
        if not es:
            return shape_answer([], persons, parsed["person"], None) | {
                "ask": ask, "empty_reason": f"no FEC record on disk for {who}"
                + (f" in the {parsed['cycle']} cycle" if parsed.get("cycle") else "")}
        e = es[0]
        bio = next((p.get("bioguide") for p in persons if p["id"] == e["src"]), None)
        detail = fec_detail(bio, e["props"]["cycle"]) or {}
        pacs = detail.get("top_pacs")
        rows = [{"person": who, "title": pac["name"], "position": f"${pac['amount']:,.0f}",
                 "date": f"{e['props']['cycle']} cycle", "certification": "ingested", "jurisdiction": US,
                 "question": f"{pac['receipts']} receipt(s) · {_pac_label(detail.get('_source'))}"}
                for pac in pacs or []]
        return {"ask": ask, "query": parsed["person"], "persons": persons, "rows": rows[:limit],
                "count": len(rows[:limit]), "truncated": len(rows) > limit,
                "committee": e["dst_name"], "cycle": e["props"]["cycle"],
                "totals": {k: e["props"].get(k) for k in ("receipts", "disbursements", "last_cash_on_hand_end_period",
                                                           "individual_contributions",
                                                           "other_political_committee_contributions",
                                                           "coverage_end_date", "pac_total")},
                "hops": [{"predicate": "campaign_committee", "weakest": "ingested", "counts": {"ingested": 1}}],
                "weak_hops": [{"predicate": "campaign_committee", "weakest": "ingested", "counts": {"ingested": 1}}],
                "empty_reason": None if rows else
                ("PAC detail not in the snapshot for this member" if pacs is None
                 else f"no PAC receipts on record for {e['dst_name']} in {e['props']['cycle']}")}
    if ask == "related":
        its = backend["items"](topic, limit)
        if not its:
            return {"ask": ask, "query": topic, "rows": [], "hops": [], "weak_hops": [],
                    "empty_reason": f"no instrument in the graph matches {topic!r}"}
        titles = {i["id"]: i["name"] for i in its}
        es = backend["edges"](list(titles), "related_to", "out") + \
            [e | {"src": e["dst"], "dst": e["src"], "dst_name": e["src_name"]}
             for e in backend["edges"](list(titles), "related_to", "in")]
        seen, rows = set(), []
        for e in es:
            if (e["src"], e["dst"]) in seen:
                continue
            seen.add((e["src"], e["dst"]))
            who = sorted({r.get("identified_by") for r in e["props"].get("relationships") or [] if r.get("identified_by")})
            rows.append({"person": titles[e["src"]], "title": e["dst_name"], "item_id": e["dst"],
                         "position": ", ".join(e["props"].get("types") or []) or "related",
                         "date": None, "certification": e["certification"], "jurisdiction": US,
                         "question": f"identified by {', '.join(who)}" if who else "identifier not recorded"})
        no_record = [i["name"] for i in its if "related_fetched" not in i["props"]]
        hops = [{"predicate": "related_to", "weakest": "ingested", "counts": {"ingested": len(rows)}}] if rows else []
        return {"ask": ask, "query": topic, "topic": topic, "rows": rows[:limit], "count": len(rows[:limit]),
                "truncated": len(rows) > limit, "persons": sorted(titles.values()), "hops": hops, "weak_hops": hops,
                "empty_reason": None if rows else
                (f"no related-bills record on disk for {', '.join(no_record)}" if no_record
                 else f"no bill on record is related to {', '.join(titles.values())}")}
    if ask == "referrals":
        its = backend["items"](topic, limit)
        if not its:
            return {"ask": ask, "query": topic, "rows": [], "hops": [], "weak_hops": [],
                    "empty_reason": f"no instrument in the graph matches {topic!r}"}
        titles = {i["id"]: i["name"] for i in its}
        es = sorted(backend["edges"](list(titles), "referred_to", "out"),
                    key=lambda e: (e["src"], e["valid_from"] or ""))
        rows = [{"person": e["dst_name"], "title": titles[e["src"]], "position": "referred",
                 "date": e["valid_from"], "certification": e["certification"], "jurisdiction": US,
                 "item_id": e["src"], "question": f"referred to {e['dst_name']}"} for e in es]
        # An original measure is reported by a committee without a referral.
        rows += [{"person": e["src_name"], "title": titles[e["dst"]], "position": "reported",
                  "date": e["valid_from"], "certification": e["certification"], "jurisdiction": US,
                  "item_id": e["dst"], "question": f"reported by {e['src_name']}"
                  + (f" ({e['props']['citation']})" if e["props"].get("citation") else "")}
                 for e in sorted(backend["edges"](list(titles), "reported", "in"),
                                 key=lambda e: (e["dst"], e["valid_from"] or ""))]
        no_record = [i["name"] for i in its if "committees_fetched" not in i["props"]]
        hops = [{"predicate": "referred_to", "weakest": "ingested", "counts": {"ingested": len(rows)}}] if rows else []
        rows = rows[:limit]
        return {"ask": ask, "query": topic, "topic": topic, "rows": rows, "count": len(rows), "truncated": False,
                "persons": sorted({r["person"] for r in rows}), "hops": hops, "weak_hops": hops,
                "empty_reason": None if rows else
                (f"no committee record on disk for {', '.join(no_record)}" if no_record
                 else f"{', '.join(titles.values())} went to no committee (none on record)")}
    if ask == "sponsors":
        rows, truncated = backend["voters"](topic, None, limit, "sponsored")
        out = shape_answer(rows, [{"name": "anyone"}], topic, topic, None, truncated, predicate="sponsored")
        out.update({"ask": ask, "persons": sorted({r["person"] for r in rows}), "place_ignored": place})
        if not rows:
            out["empty_reason"] = f"no sponsor on disk for anything matching {topic!r}"
        return out
    if ask == "votes":
        persons = backend["persons"](parsed["person"])
        if not persons:
            return shape_answer([], [], parsed["person"], topic) | {"ask": ask}
        which, persons = _one_person(persons, parsed["person"], topic, ask, backend)
        if which:
            return which | {"place_ignored": place}
        year = parsed.get("year")
        loaded = current_session()[0]
        if year and (year - 1789) // 2 + 1 != loaded:
            rows, total, truncated, files = snapshot_votes(persons, year, topic, limit, loaded)
            out = shape_answer(rows, persons, parsed["person"], topic, total, truncated) | {
                "ask": ask, "place_ignored": place, "year": year, "from_snapshot": files}
            if not files:
                out["empty_reason"] = f"no roll-call snapshot on disk for {year}"
            return out
        if year:
            # One Congress's votes by one person are a few thousand at most;
            # filter the whole set, not the first page of it.
            rows, total, _ = backend["votes"]([p["id"] for p in persons], topic, 10000)
            rows = [r for r in rows if str(r["date"]).startswith(str(year))]
            rows, truncated = rows[:limit], len(rows) > limit
        else:
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
    sub.add_parser("certify-index", help="reduce older roll-call snapshots to member-congress.json")
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
    elif a.cmd == "certify-index":
        members, files = write_member_congress(current_session()[0])
        print(f"{members:,} member id(s) from {len(files)} file(s) → {MEMBER_CONGRESS_PATH}")
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
