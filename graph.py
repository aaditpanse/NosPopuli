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

# Per-source facts the stores don't carry: which jurisdiction a store is,
# which elections store seats its members, and the statutory term. Virginia
# county supervisors take office January 1 after the November general
# (Va. Code § 24.2-217 / § 15.2-1400), four-year terms.
SOURCES = {
    "fairfax-bos": {
        "state": "va",
        "county": "fairfax",
        "county_name": "Fairfax County",
        "body_name": "Fairfax County Board of Supervisors",
        "elections_source": "va-elections",
        "election_jurisdiction": "Fairfax County",
        "election_office": "Board of Supervisors",
        "chair_district": "Fairfax County",   # the at-large contest's "district"
        "term_start": "2024-01-01",
        "term_expires": "2027-12-31",
    },
}

# Cross-layer identity, asserted by a human. A local person has no external
# id, so the loader keys them by seat + surname; when that person also holds
# a federal seat, this maps the local natural key to the bioguide key so both
# loaders write ONE node. This is the residue identity matching leaves for a
# person to decide, and it is recorded on the node as a manual assertion.
IDENTITIES = {
    # Braddock District supervisor 2024–2025, VA-11 from 2025-09-10.
    "va/fairfax/bos/braddock/walkinshaw": "bioguide/W000831",
}

US = "ocd-division/country:us"
HOUSE_KEY, SENATE_KEY = "us/house", "us/senate"
CHAMBER_NAME = {"house": "U.S. House of Representatives", "senate": "U.S. Senate"}
LEGISLATORS_SOURCE = "legislators-current"

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
    if not 1 < len(tokens) <= 5:
        return False
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

def resolve_members(store, cfg):
    """Roster → people. Returns (persons, rejects).

    persons: {seat_slug: [person]} where a person is a dict with name,
    aliases, key (surname canon), seat, raw_names (every spelling the store
    uses), first_seen (earliest meeting date any spelling appears).
    rejects: [(raw name, reason)] — never loaded, always reported.

    Identity is seat + surname, never surname alone: member_key is a
    last-name canon and 'Smith' is not unique across a state.
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
    for m in store.get("members", {}).values():
        raw = m["name"]
        if not _looks_like_name(raw):
            rejects.append((raw, "not a person's name (failed the name floor)"))
            continue
        role = (m.get("role") or "").lower()
        if role.startswith("chair"):
            seat = "chair"
        elif m.get("district"):
            seat = _district_slug(m["district"], cfg)
        else:
            rejects.append((raw, "no seat: neither chair nor a district"))
            continue
        name, alias = _display_name(raw)
        key = (seat, _person_key(name))
        person = by_key.setdefault(key, {
            "name": name, "aliases": [], "key": key[1], "seat": seat,
            "raw_names": [], "first_seen": None})
        person["raw_names"].append(raw)
        if alias:
            person["aliases"].append(alias)
        if len(name) > len(person["name"]):        # 'Patrick S.' over 'Pat'
            person["aliases"].append(person["name"])
            person["name"] = name
        elif name != person["name"]:
            person["aliases"].append(name)
        seen = [first_seen[r] for r in person["raw_names"] if r in first_seen]
        person["first_seen"] = min(seen) if seen else None

    persons = {}
    for (seat, _), p in by_key.items():
        p["aliases"] = sorted(set(p["aliases"]) - {p["name"]})
        persons.setdefault(seat, []).append(p)
    return persons, rejects


def _local_person_id(natural_key):
    """A local person's node id — or the federal node's id when IDENTITIES
    says they are the same person, so the two layers meet at one node."""
    identity = IDENTITIES.get(natural_key)
    return node_id("person", identity or natural_key), identity


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

    _node(g, state_id, "jurisdiction", "Virginia", {"level": "state"}, source_id)
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
    for seat, ps in persons.items():
        seats[seat] = next((m.get("district") for m in store["members"].values()
                            if _district_slug(m.get("district"), cfg) == seat
                            and m.get("district")), None) or "Chair"
    for c in my_contests:
        seats.setdefault(_district_slug(c.get("district"), cfg),
                         "Chair" if _district_slug(c.get("district"), cfg) == "chair"
                         else c["district"])

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

    def person_node(seat, key, name, aliases, source_ref):
        nk = f"{cfg['state']}/{cfg['county']}/bos/{seat}/{key.lower()}"
        pid, identity = _local_person_id(nk)
        props = {"natural_key": nk, "aliases": aliases, "seat": seat,
                 "jurisdiction": county_id}
        if identity:
            props["identity"] = identity
            props["identity_asserted_by"] = "manual (graph.py IDENTITIES)"
        _node(g, pid, "person", name, props, source_id, source_ref)
        return pid

    person_ids, raw_to_person = {}, {}
    for seat, ps in persons.items():
        for p in ps:
            pid = person_node(seat, p["key"], p["name"], p["aliases"], p["raw_names"][0])
            person_ids[(seat, p["key"])] = pid
            for raw in p["raw_names"]:
                raw_to_person[raw] = pid

    # Instruments and the votes on them. Topic is Haiku's reading of the
    # title (item-summaries.json); it travels as a property and is labelled
    # derived so no answer can present it as the clerk's classification.
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
        iid = f"instrument/{ve.get('item_id')}"
        if meeting is None or iid not in g["nodes"]:
            g["gaps"].append(f"vote {ve['vote_id']} has no meeting or agenda item in the store")
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

    # Elections. A contest seats a person only when it is for this exact
    # seat AND the surname matches — never by surname across the county,
    # because the same person can win a school-board seat in the same
    # district (Sizemore Heizer, Braddock, 2023).
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
            key = (seat, _person_key(name))
            pid = person_ids.get(key)
            if pid is None:
                pid = person_ids[key] = person_node(seat, key[1], name,
                                                    [alias] if alias else [], c["contest_id"])
                g["nodes"][pid]["source_id"] = cfg["elections_source"]
            elif alias and alias not in g["nodes"][pid]["props"]["aliases"]:
                g["nodes"][pid]["props"]["aliases"].append(alias)
            edge(pid, "elected_in", cid, date, date, _cert(c),
                 cfg["elections_source"], c["contest_id"], {"bound_from": bound})
            row = edge(pid, "holds", post_ids[seat], cfg["term_start"], None, _cert(c),
                       cfg["elections_source"], c["contest_id"],
                       {"bound_from": "term_statute", "term_expires": cfg["term_expires"]})
            holds.setdefault(seat, []).append(row)
            seated.add(pid)

    # Roster members no contest seated: they hold the seat from the first
    # meeting we saw them at. Observed, not asserted — and the gap is named.
    for seat, ps in persons.items():
        for p in ps:
            pid = person_ids[(seat, p["key"])]
            if pid in seated:
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
        else:
            cd = term.get("district") or 1
            key = f"us/house/{st.lower()}/cd:{cd}"
            label = f"U.S. Representative, {st}-{cd}"
            division, chamber = f"{state_div(st)}/cd:{cd}", "house"
        pid = node_id("post", key)
        if pid not in g["nodes"]:
            if state_div(st) not in g["nodes"]:
                _node(g, state_div(st), "jurisdiction", st,
                      {"level": "state", "jurisdiction": state_div(st)}, LEGISLATORS_SOURCE)
                _edge(g, US, "contains", state_div(st), None, None, "ingested",
                      LEGISLATORS_SOURCE, "seed", state_div(st), {"derived": "seed"})
            if division not in g["nodes"]:
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
               "jurisdiction": home}, LEGISLATORS_SOURCE, bioguide)
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
                        "ingested", LEGISLATORS_SOURCE, f"{bioguide}/{t['start']}",
                        state_div(t["state"]), props)
            holds.setdefault(post, []).append(row)
    _close_double_holds(g, holds, post_label)

    # Roll calls. A member's id in the House record is the bioguide id; in
    # the Senate record it is the LIS id, bridged through the legislators
    # file. Votes by anyone outside the selected delegation are simply not
    # this load's business; votes by an id nobody has are a gap.
    skipped, unknown, considered_by = {}, {}, {}
    for snap in snapshots:
        for v in snap.get("votes", []):
            chamber = v["chamber"]
            inst = _parse_instrument(chamber, v)
            if inst is None:
                skipped[chamber] = skipped.get(chamber, 0) + 1
                continue
            itype, number = inst
            iid = f"instrument/us/{v['congress']}/{itype}/{number}"
            label = v.get("legis_num") or v.get("document_name") or f"{itype} {number}"
            if iid not in g["nodes"]:
                _node(g, iid, "instrument", f"{label}: {v.get('description') or ''}".strip(": "),
                      {"instrument_type": itype, "congress": v["congress"], "number": number,
                       "jurisdiction": US}, v["source_id"], v["vote_id"])
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
                          {"position": position, "vote_id": v["vote_id"], "chamber": chamber,
                           "roll": v["roll"], "question": v.get("question"),
                           "result": v.get("result"), "amendment": v.get("amendment")})
    for chamber, n in skipped.items():
        g["gaps"].append(f"{n} {chamber} roll call(s) had no legislative instrument "
                         f"(quorum calls, Speaker elections, motions) and were not loaded")
    if unknown:
        g["gaps"].append(f"{sum(unknown.values())} vote position(s) by {len(unknown)} member "
                         f"id(s) not in {LEGISLATORS_SOURCE} (left office mid-session?) dropped")
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
    out = {"meta": {"congress": congress, "session": session, "year": year,
                    "fetched": datetime.datetime.now().isoformat(timespec="seconds"),
                    "counts": {c: sum(1 for v in votes if v["chamber"] == c)
                               for c in ("house", "senate")},
                    "errors": errors},
           "votes": votes}
    pathlib.Path(out_path).write_text(json.dumps(out, separators=(",", ":")))
    return out["meta"]


# ---------------------------------------------------------------- temporal

def holders_as_of(hold_edges, as_of):
    """The `holds` edges in force on a date. NULL valid_to is open. Pure;
    `seat_holder` feeds it the rows from Postgres so the SQL and the tests
    share one definition of 'in force'."""
    as_of = str(as_of)
    return [e for e in hold_edges
            if e["valid_from"] is not None and str(e["valid_from"]) <= as_of
            and (e["valid_to"] is None or str(e["valid_to"]) >= as_of)]


# ------------------------------------------------------------------ answer

def shape_answer(rows, persons, query, topic=None, total_votes=None, truncated=False):
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
        hops.append({"predicate": "voted_on", "weakest": weakest, "counts": counts})
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
        "advisory_fields": ["topic"] if topic else [],
        "empty_reason": empty_reason,
    }


# -------------------------------------------------------------------- db

def _read_store(name):
    p = STORE_DIR / f"{name}.json"
    return json.loads(p.read_text()) if p.exists() else {}


def _congress_snapshots():
    return [json.loads(p.read_text()) for p in sorted(DATA_DIR.glob("congress-votes-*.json"))]


def build_source(source, states=None):
    """Read the inputs for one source off disk and build. Returns
    (nodes, edges, gaps, delete scopes)."""
    if source == "us-congress":
        legislators = json.loads((DATA_DIR / "legislators-current.json").read_text())
        snaps = _congress_snapshots()
        if not snaps:
            raise RuntimeError("no data/congress-votes-*.json — run `python graph.py snapshot 119 2 2026`")
        nodes, edges, gaps = build_congress(legislators, snaps, states)
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
    return _summary(nodes, edges), gaps


def _pg_persons(cur, query):
    like = f"%{query}%"
    cur.execute("""
        SELECT id, name, props->'aliases' AS aliases, props->>'seat' AS seat,
               props->>'bioguide' AS bioguide
        FROM graph_node
        WHERE kind = 'person'
          AND (name ILIKE %s OR EXISTS (
                SELECT 1 FROM jsonb_array_elements_text(props->'aliases') a
                WHERE a ILIKE %s))
        ORDER BY name
    """, (like, like))
    return cur.fetchall()


_VOTE_ROW_SQL = """
    SELECT p.id AS person_id, p.name AS person,
           e.props->>'position' AS position, e.valid_from AS date,
           e.certification, e.source_ref AS vote_id,
           e.props->>'question' AS question,
           i.id AS item_id, i.name AS title,
           i.props->>'instrument_type' AS instrument_type,
           i.props->>'jurisdiction' AS jurisdiction,
           i.props->>'topic' AS topic, i.props->>'result' AS result,
           i.props->>'meeting_id' AS meeting_id
    FROM graph_node p
    JOIN graph_edge e ON e.src = p.id AND e.predicate = 'voted_on'
    JOIN graph_node i ON i.id = e.dst
"""
_TOPIC_SQL = " (i.props->>'topic' ILIKE %s OR i.name ILIKE %s)"


def _pg_votes(cur, person_ids, topic, limit):
    """(rows, total votes by these people, truncated)."""
    cur.execute("SELECT COUNT(*) AS n FROM graph_edge "
                "WHERE predicate = 'voted_on' AND src = ANY(%s)", (person_ids,))
    total = cur.fetchone()["n"]
    sql, args = _VOTE_ROW_SQL + " WHERE p.id = ANY(%s)", [person_ids]
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
_ASK_VOTES = (
    re.compile(r"^\s*(?:how\s+did|how\s+has|how\s+does)\s+(?P<person>.+?)\s+voted?\s*"
               r"(?:on\s+(?P<topic>.+?))?\s*\??\s*$", re.I),
    re.compile(r"^\s*(?:what\s+did|what\s+has)\s+(?P<person>.+?)\s+voted?\s+(?:on|for)\s*"
               r"(?P<topic>.*?)\s*\??\s*$", re.I),
    re.compile(r"^\s*(?P<person>[A-Za-z.'\- ]+?)(?:'s)?\s+votes?\s*(?:on\s+(?P<topic>.+?))?\s*\??\s*$", re.I),
)
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
    m = _ASK_VOTERS.match(q)
    if m:
        # "voted against X" and "voted for X" carry the position in the
        # connector; "voted on X" carries none.
        pos = (m.group("position") or m.group("connector")).lower()
        return {"ask": "voters", "topic": m.group("topic").strip(),
                "position": _POSITION_WORDS.get(pos)}
    m = _ASK_HOLDER.match(q)
    if m:
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
    m = re.search(r"\b(\d{1,2})(?:st|nd|rd|th)\b", q)
    if m:
        return [f"cd:{m.group(1)}"]
    q = re.sub(r"\b(the|of|for|district|seat|county|board|supervisor|supervisors|from)\b", " ", q)
    return [t for t in re.split(r"[^a-z0-9]+", q) if t]


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
        return {"person_id": p["id"], "person": p["name"], "position": e["props"].get("position"),
                "date": e["valid_from"], "certification": e["certification"],
                "vote_id": e["source_ref"], "question": e["props"].get("question"),
                "item_id": i["id"], "title": i["name"],
                "instrument_type": i["props"].get("instrument_type"),
                "jurisdiction": i["props"].get("jurisdiction"), "topic": i["props"].get("topic"),
                "result": i["props"].get("result"), "meeting_id": i["props"].get("meeting_id")}

    def topic_ok(i, topic):
        t = topic.lower()
        return (i["props"].get("topic") or "").lower().startswith(t) or t in i["name"].lower()

    def persons(query):
        q = query.lower()
        return sorted(({"id": n["id"], "name": n["name"], "aliases": n["props"].get("aliases", []),
                        "seat": n["props"].get("seat"), "bioguide": n["props"].get("bioguide")}
                       for n in nodes if n["kind"] == "person"
                       and (q in n["name"].lower()
                            or any(q in a.lower() for a in n["props"].get("aliases", [])))),
                      key=lambda p: p["name"])

    def votes_of(person_ids, topic, limit):
        mine = [e for pid in person_ids for e in out.get(pid, []) if e["predicate"] == "voted_on"]
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

    def voters(topic, position, limit):
        rows = [vote_row(e) for n in nodes if n["kind"] == "instrument" and topic_ok(n, topic)
                for e in inn.get(n["id"], []) if e["predicate"] == "voted_on"
                and (position is None or e["props"].get("position") == position)]
        rows.sort(key=lambda r: (r["date"], r["item_id"], r["person"]), reverse=True)
        return rows[:limit], len(rows) > limit

    return {"persons": persons, "votes": votes_of, "posts": posts,
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

    def votes_of(person_ids, topic, limit):
        return run(lambda cur: _pg_votes(cur, person_ids, topic, limit))

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

    def voters(topic, position, limit):
        def q(cur):
            sql, args = _VOTE_ROW_SQL + " WHERE" + _TOPIC_SQL, [f"{topic}%", f"%{topic}%"]
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

    return {"persons": persons, "votes": votes_of, "posts": posts,
            "holds": holds_of, "voters": voters, "loaded": loaded}


def answer(parsed, backend, limit=200):
    """Run one parsed question against a backend. Every branch returns a
    dict with `ask`, `hops` / `weak_hops`, and an `empty_reason` when there
    is nothing — never a silent zero."""
    ask = parsed["ask"]
    if not backend["loaded"]():
        return {"ask": ask, "rows": [], "hops": [], "weak_hops": [],
                "empty_reason": "graph not loaded: run `python graph.py load fairfax-bos`"}
    if ask == "votes":
        persons = backend["persons"](parsed["person"])
        if not persons:
            return shape_answer([], [], parsed["person"], parsed["topic"]) | {"ask": ask}
        rows, total, truncated = backend["votes"]([p["id"] for p in persons], parsed["topic"], limit)
        return shape_answer(rows, persons, parsed["person"], parsed["topic"], total, truncated) | {"ask": ask}
    if ask == "voters":
        rows, truncated = backend["voters"](parsed["topic"], parsed["position"], limit)
        out = shape_answer(rows, [{"name": "anyone"}], parsed["topic"], parsed["topic"], None, truncated)
        out.update({"ask": ask, "persons": sorted({r["person"] for r in rows}),
                    "position": parsed["position"]})
        if not rows:
            out["empty_reason"] = (f"no recorded vote{' ' + parsed['position'] if parsed['position'] else ''} "
                                   f"on anything matching {parsed['topic']!r}")
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
    sources = sorted(SOURCES) + ["us-congress"]
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, help_ in (("build", "build in memory and print the summary; no database"),
                        ("load", "rebuild one source's slice in Postgres")):
        s = sub.add_parser(name, help=help_)
        s.add_argument("source", choices=sources)
        s.add_argument("--state", action="append",
                       help="us-congress only: delegation(s) to load, e.g. --state VA")
    s = sub.add_parser("snapshot", help="fetch one session's roll calls to data/")
    s.add_argument("congress", type=int)
    s.add_argument("session", type=int)
    s.add_argument("year", type=int)
    s = sub.add_parser("votes", help="person → voted_on → instrument")
    s.add_argument("person")
    s.add_argument("--topic")
    s = sub.add_parser("ask", help="a typed question → the graph")
    s.add_argument("question")
    s.add_argument("--memory", action="store_true",
                   help="build fairfax-bos + us-congress(VA) in memory; no database")
    s = sub.add_parser("holder", help="who held a post on a date")
    s.add_argument("post_id")
    s.add_argument("as_of")
    a = ap.parse_args()
    if a.cmd in ("build", "load"):
        if a.cmd == "build":
            nodes, edges, gaps, _ = build_source(a.source, a.state)
            summary = _summary(nodes, edges)
        else:
            summary, gaps = load(a.source, a.state)
        print(json.dumps(summary, indent=1))
        print(f"{len(gaps)} gap(s):")
        for g in gaps:
            print("  -", g)
    elif a.cmd == "snapshot":
        out = DATA_DIR / f"congress-votes-{a.congress}-{a.session}.json"
        print(json.dumps(snapshot_congress(a.congress, a.session, a.year, out), indent=1))
        print("wrote", out)
    elif a.cmd == "votes":
        print(json.dumps(votes(a.person, a.topic), indent=1, default=str))
    elif a.cmd == "ask":
        backend = None
        if a.memory:
            nodes, edges = [], []
            for src, st in (("fairfax-bos", None), ("us-congress", ["VA"])):
                n, e, _, _ = build_source(src, st)
                nodes += n
                edges += e
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
