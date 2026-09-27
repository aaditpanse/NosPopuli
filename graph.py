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
import functools
import hashlib
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
# The files the app itself ships (not graph inputs); always in the checkout.
REPO_DATA = _HERE / "data"

# Every dataset, where it lives, and where it comes from. `raw/` holds the
# downloads exactly as published; `derived/` what the graph reads; `public/`
# the unitedstates project's files; `app/` the app's own files (checkout
# only). The checkout's data/ mirrors these relative paths, so the sync's
# copy-back of tracked files is a path join. `graph.py manifest` turns this
# into datasets.json with coverage and last sync. Fields a pattern names
# ({congress}) are filled by data_path; a glob leaves them as '*'.
DATASETS = {
    "bills": {"path": "derived/bills/bills-{congress}.json", "by": "sources/govinfo.py billstatus",
              "source": "GovInfo BILLSTATUS bulk data and CRPT package metadata",
              "licence": "public domain (U.S. government work)"},
    "votes": {"path": "derived/votes/congress-votes-{congress}-{session}.json",
              "by": "graph.py snapshot (118th on); sources/voteview.py (1st–117th)",
              "source": "House and Senate clerks' roll calls; Voteview (Lewis et al., UCLA)",
              "licence": "public domain (clerks); Voteview, cite Lewis et al."},
    "certification": {"path": "derived/certification/member-congress.json", "by": "graph.py certify-index",
                      "source": "derived from the older roll-call files", "licence": "derived"},
    "fec": {"path": "derived/fec/fec-{cycle}.json", "by": "sources/fec_client.py bulk",
            "source": "FEC bulk downloads (weball, cn, ccl, cm, pas2)",
            "licence": "public domain (U.S. government work)"},
    "fec_candidates": {"path": "derived/fec/candidates-{cycle}.json", "by": "sources/fec_client.py bulk",
                       "source": "FEC bulk downloads (cn, ccl, committee_summary, pas2)",
                       "licence": "public domain (U.S. government work)"},
    "nominations": {"path": "derived/nominations/nominations-{congress}.json", "by": "sources/nominations.py",
                    "source": "api.congress.gov nomination", "licence": "public domain (U.S. government work)"},
    "lobbying": {"path": "derived/lobbying/lobbying-{year}.json", "by": "sources/lda_client.py bulk",
                 "source": "lda.gov LDA filings API", "licence": "public record (Lobbying Disclosure Act)"},
    "state_people": {"path": "derived/states/{state}/people.json", "by": "sources/openstates.py people",
                     "source": "github.com/openstates/people", "licence": "CC0"},
    "state_bills": {"path": "derived/states/{state}/bills-{session}.json", "by": "sources/openstates.py extract",
                    "source": "Open States monthly Postgres dump (data.openstates.org)",
                    "licence": "public record, as scraped by Open States"},
    "state_votes": {"path": "derived/states/{state}/votes-{session}.json", "by": "sources/openstates.py extract",
                    "source": "Open States monthly Postgres dump (data.openstates.org)",
                    "licence": "public record, as scraped by Open States"},
    "state_sessions": {"path": "derived/states/{state}/sessions.json", "by": "sources/openstates.py extract",
                       "source": "Open States monthly Postgres dump (data.openstates.org), legislativesession",
                       "licence": "public record, as scraped by Open States"},
    "state_certification": {"path": "derived/states/{state}/member-session.json",
                            "by": "graph.py write_state_cert_index",
                            "source": "derived from the older sessions' roll calls", "licence": "derived"},
    "state_lis": {"path": "derived/states/{state}/lis-{session}.json", "by": "sources/lis.py sync",
                  "source": "Virginia LIS daily files: bills, history, roll calls joined to bills",
                  "licence": "public record (Virginia General Assembly)"},
    "lis": {"path": "raw/lis/{session}/{name}", "by": "sources/lis.py sync",
            "source": "Virginia LIS daily files (lis.blob.core.windows.net/lisfiles)",
            "licence": "public record (Virginia General Assembly)"},
    "state_text": {"path": "raw/states/{state}/text/{session}/{name}", "by": "sources/openstates.py text",
                   "source": "bill text versions from each version's link in the Open States record, as the "
                             "legislature publishes them",
                   "licence": "public record (each state legislature)"},
    "public": {"path": "public/{name}.json", "by": "graph.py fetch",
               "source": "unitedstates/congress-legislators", "licence": "CC0"},
    "images": {"path": "raw/unitedstates-images/congress/225x275/{bioguide}.jpg",
               "by": "nospopuli-sync (git sparse clone, Mondays)",
               "source": "github.com/unitedstates/images", "licence": "public domain (official portraits)"},
    "raw": {"path": "raw/{source}", "by": "each downloader",
            "source": "as published by each upstream; see the downloader", "licence": "as upstream"},
    "app": {"path": "app/{name}.json", "root": "repo", "by": "sources/house_stock_fetcher.py and others",
            "source": "the app's own files", "licence": "n/a"},
}


def migrate_layout():
    """Move a flat data dir (every file at the top) into DATASETS' layout.
    Idempotent. Fail-closed: a file present at both the old and the new
    place stops the move before anything changes, because one of the two
    is stale and only a person can say which. The app's own files found in
    the data dir are copies of the checkout's and are removed. Returns the
    moves made."""
    kinds = [("bills-", "bills"), ("congress-votes-", "votes"), ("fec-", "fec"),
             ("nominations-", "nominations"), ("lobbying-", "lobbying")]
    public = {"legislators-current", "legislators-historical", "executive", "committees-current",
              "committee-membership-current", "public-fetched"}
    app = {"house_stocks", "known_elections", "notable_trades", "zip3_to_state"}
    plan, stale = [], []
    for p in sorted(DATA_DIR.glob("*.json")):
        stem = p.stem
        if stem in app:
            stale.append(p)
            continue
        if stem in public:
            dest = data_path("public", name=stem)
        elif stem == "member-congress":
            dest = data_path("certification")
        else:
            kind = next((k for prefix, k in kinds if stem.startswith(prefix)), None)
            if kind is None:
                continue
            dest = DATA_DIR / pathlib.Path(DATASETS[kind]["path"]).parent / p.name
        if dest.exists():
            raise RuntimeError(f"{p.name} exists at {p} and at {dest}; nothing moved")
        plan.append((p, dest))
    for src, dest in plan:
        dest.parent.mkdir(parents=True, exist_ok=True)
        src.rename(dest)
    for p in stale:
        p.unlink()
    return [(str(s.relative_to(DATA_DIR)), str(d.relative_to(DATA_DIR))) for s, d in plan] + \
        [(str(p.relative_to(DATA_DIR)), "removed: a copy of the checkout's data/app/") for p in stale]


def write_manifest():
    """datasets.json: for each dataset, where it comes from, its licence,
    what it covers (the range of the field in its file names), how many
    files and bytes, and when a file of it last changed. Written by the
    sync after every run, so a reader can see what is on disk without
    listing 4 GB of files."""
    out = {"built": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"), "datasets": {}}
    for kind, d in DATASETS.items():
        root = REPO_DATA if d.get("root") == "repo" else DATA_DIR
        if kind == "raw":
            files = [f for f in (DATA_DIR / "raw").rglob("*") if f.is_file()] if (DATA_DIR / "raw").exists() else []
            coverage = sorted({f.relative_to(DATA_DIR / "raw").parts[0] for f in files})
        else:
            files = data_glob(kind)
            nums = sorted({int(m) for f in files for m in re.findall(r"-(\d+)", f.stem)[:1]})
            coverage = f"{nums[0]}–{nums[-1]}" if nums else None
        out["datasets"][kind] = {
            "path": str(root / d["path"]), "source": d["source"], "licence": d["licence"], "by": d["by"],
            "files": len(files), "bytes": sum(f.stat().st_size for f in files), "coverage": coverage,
            "last_changed": datetime.datetime.fromtimestamp(max(f.stat().st_mtime for f in files),
                                                            datetime.timezone.utc).isoformat(timespec="seconds")
            if files else None}
    path = DATA_DIR / "datasets.json"
    path.write_text(json.dumps(out, indent=1, ensure_ascii=False) + "\n")
    return out


_ROMAN = {"I": 1, "II": 2, "III": 3, "IV": 4, "V": 5}


def _session_id_key(session):
    """(year, special number) read off a Virginia-shaped id — "2026" is
    (2026, 0), "2026S1" (2026, 1), "2020specialI" (2020, 1) — or None. Pure."""
    m = re.fullmatch(r"(\d{4})(?:S(\d+)|special([IV]+))?", session or "")
    if not m:
        return None
    return int(m.group(1)), int(m.group(2)) if m.group(2) else _ROMAN.get(m.group(3), 0) if m.group(3) else 0


_SESSION_INDEX = {}


def state_session_index(st):
    """{session id: {years, key, name, classification, start, end, bills,
    votes, types}} from derived/states/<st>/sessions.json, cached per
    mtime; {} when the state has none on disk."""
    path = data_path("state_sessions", state=st)
    try:
        mtime = path.stat().st_mtime_ns
    except FileNotFoundError:
        return {}
    hit = _SESSION_INDEX.get(str(path))
    if not hit or hit[0] != mtime:
        hit = _SESSION_INDEX[str(path)] = (mtime, json.loads(path.read_text())["sessions"])
    return hit[1]


def session_key(session, st=None):
    """(first year, special number) of a state's session, for newest-first
    order: the session index `extract` writes (Open States ids come in ~60
    shapes, many with no year: "88", "103rd", "57th-2nd-regular"), else a
    Virginia-shaped id. None for a session neither knows. Pure but for the
    index file."""
    rec = state_session_index(st).get(session) if st else None
    return tuple(rec["key"]) if rec else _session_id_key(session)


def session_years(session, st=None):
    """(first, last) calendar year a session sat in: "20232024" is (2023,
    2024), a Virginia session one year. None when unknown."""
    rec = state_session_index(st).get(session) if st else None
    if rec:
        return tuple(rec["years"])
    key = _session_id_key(session)
    return (key[0], key[0]) if key else None


def in_session_year(session, st, year):
    years = session_years(session, st)
    return bool(years) and years[0] <= int(year) <= years[1]


def lis_session(st, session):
    """Virginia LIS's code for an Open States session ("2026" → "20261",
    "2026S1" → "20262"), or None when LIS publishes no files for it (before
    the legislature's `lis_from`, or a state without LIS). Pure."""
    conf = LEGISLATURES.get(st) or {}
    key = _session_id_key(session)
    if not conf.get("lis_from") or not key or key[0] < int(conf["lis_from"]):
        return None
    return f"{key[0]}{key[1] + 1}"


class _Glob(dict):
    def __missing__(self, key):
        return "*"


def data_path(kind, **fields):
    """The file for one dataset, e.g. data_path("bills", congress=119)."""
    d = DATASETS[kind]
    root = REPO_DATA if d.get("root") == "repo" else DATA_DIR
    return root / d["path"].format(**fields)


def data_glob(kind, **fields):
    """Every file of one dataset, sorted; the fields given narrow it."""
    d = DATASETS[kind]
    root = REPO_DATA if d.get("root") == "repo" else DATA_DIR
    return sorted(root.glob(d["path"].format_map(_Glob(fields))))

# The full predicate vocabulary. The loader refuses anything else so the
# graph cannot grow a new relation type by accident.
PREDICATES = ("contains", "has_body", "has_seat", "holds", "represents",
              "sponsored", "voted_on", "considered", "elected_in", "for_seat",
              "signed", "vetoed", "enacted_as", "member_of", "referred_to", "reported",
              "related_to", "campaign_committee", "nominated", "lobbied_on", "lobbied_for",
              "connected_committee")

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
# State legislatures to load, by postal code (see the sidecar's own comment).
LEGISLATURES = {k: v for k, v in _CONFIG.get("legislatures", {}).items() if not k.startswith("_")}
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

STATE_CODE = {name: code for code, name in DIVISION_NAMES.items()}

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


def _close_double_holds(g, holds_by_post, post_label, seats=None):
    """More open holders than a post has seats is a contradiction the
    resolver cannot answer. Close the earliest open one the day before the
    newcomer begins, and say that the bound is inferred. Leaving both open
    would be the silent kind of wrong. seats: {post: n}, 1 when absent; a
    multi-member district (New Hampshire's) seats several at once."""
    for post, rows in holds_by_post.items():
        rows.sort(key=lambda r: r["valid_from"])
        n = (seats or {}).get(post, 1)
        for i, later in enumerate(rows[1:], 1):
            open_ = [r for r in rows[:i] if r["valid_to"] is None]
            for earlier in open_[:max(0, len(open_) - n + 1)]:
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
                source_id, source_ref, lookup_bioguide, home=None):
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
    # The sponsor's home state is the edge's delete scope; a bill scope has
    # no person nodes of its own to read it from.
    home = home or (lambda pid: g["nodes"][pid]["props"]["jurisdiction"])
    for who in rec.get("sponsors", []):
        pid = lookup_bioguide(who)
        if pid:
            _edge(g, pid, "sponsored", iid, rec.get("introduced"), rec.get("introduced"),
                  "ingested", "congress.gov", ref, home(pid), {"role": "sponsor"})
    for co in rec.get("cosponsors", []):
        pid = lookup_bioguide(co["id"])
        if pid:
            _edge(g, pid, "sponsored", iid, co.get("date"), co.get("date"),
                  "ingested", "congress.gov", ref, home(pid),
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
            g["gaps"].append(f"the {_ordinal(congress)} Congress has no bill records (no bills-{congress}.json on disk); "
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


def build_related(snapshots, instrument_ids, known_ids=frozenset()):
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
                # known_ids: bills with their own record in another scope. A
                # title-only node for one would overwrite its name and props.
                if did not in instrument_ids and did not in known_ids and did not in g["nodes"]:
                    _node(g, did, "instrument", f"{b['type'].upper()} {b['number']}: {b.get('title') or ''}".strip(": "),
                          {"instrument_type": b["type"], "congress": b["congress"], "number": b["number"],
                           "title_only": True, "jurisdiction": US}, "congress.gov", f"us/{congress}/{label}/related")
                    title_only += 1
                _edge(g, iid, "related_to", did, None, None, "ingested", "congress.gov",
                      f"us/{congress}/{label}/related", US,
                      {"relationships": b.get("relationships") or [],
                       "types": sorted({r.get("type") for r in b.get("relationships") or [] if r.get("type")})})
    if title_only:
        g["gaps"].append(f"{title_only} related bill(s) have no record on disk (before the 108th Congress, "
                         f"or not published by GovInfo): title-only nodes, their own links not followed")
    return list(g["nodes"].values()), list(g["edges"].values()), g["gaps"]


# What a bill scope owns. Everything else is the skeleton: people, seats,
# the presidency, committees as the file lists them, money.
BILL_PREDICATES = frozenset({"sponsored", "voted_on", "considered", "referred_to", "reported",
                             "related_to", "enacted_as", "signed", "vetoed", "nominated"})


def partition(nodes, edges):
    """Split one build into (skeleton nodes, skeleton edges, bill nodes,
    bill edges). A committee that only a bill record names (renamed,
    abolished; source congress.gov) goes with the bills that name it. Pure."""
    def is_bill(n):
        return n["kind"] == "instrument" or (n["kind"] == "organization" and n["source_id"] == "congress.gov")
    return ([n for n in nodes if not is_bill(n)], [e for e in edges if e["predicate"] not in BILL_PREDICATES],
            [n for n in nodes if is_bill(n)], [e for e in edges if e["predicate"] in BILL_PREDICATES])


def completeness_gap(congress, meta, nodes):
    """"N of M bills": the bills that are nodes against Congress.gov's own
    count, the independent check on GovInfo's files. Pure."""
    cg = meta.get("congress_gov_counts")
    if not cg:
        return (f"the {_ordinal(congress)} Congress: {nodes:,} bills from GovInfo's {sum(meta.get('files', {}).values()):,} "
                f"files; not checked against Congress.gov's count")
    total = sum(v or 0 for v in cg.values())
    short = {t: cg[t] - meta.get("files", {}).get(t, 0) for t in cg
             if (cg[t] or 0) != meta.get("files", {}).get(t, 0)}
    return (f"the {_ordinal(congress)} Congress: {nodes:,} of {total:,} bills on Congress.gov "
            f"(checked {meta.get('congress_gov_checked')})"
            + (f"; differs by type: {json.dumps(short, sort_keys=True)}" if short else ""))


def build_bills_scope(congress, bills, legislators, executive, committees, observed, today=None):
    """One older Congress's bills → (nodes, edges, gaps) for its scope
    us/bills/<congress>: every bill, who sponsored and cosponsored it,
    referrals and reports, and what the President did. Roll calls and
    related-bill links stay in the files. People are keyed as the
    skeleton keys them, so the edges meet the skeleton's nodes without
    building it. Pure."""
    g = _graph()
    home, unknown = {}, {}
    for leg in legislators:
        bio = leg.get("id", {}).get("bioguide")
        if bio:
            home[node_id("person", f"bioguide/{bio}")] = f"{US}/state:{leg['terms'][-1]['state'].lower()}"

    def lookup(bio):
        pid = node_id("person", f"bioguide/{bio}")
        if pid in home:
            return pid
        unknown[bio] = unknown.get(bio, 0) + 1
        return None

    fetched = bills["meta"].get("fetched")
    for label, rec in bills["instruments"].items():
        itype, number = label.split("/")
        _instrument(g, f"instrument/us/{congress}/{itype}/{number}", itype, number, congress, rec, fetched,
                    f"{BILL_LABEL.get(itype, itype.upper())} {number}", None,
                    "govinfo", f"us/{congress}/{itype}/{number}", lookup, home.get)
    snap = {"meta": {"congress": congress, "instruments_fetched": fetched}, "votes": [],
            "instruments": bills["instruments"]}
    ids = set(g["nodes"])
    _, xe, _ = build_executive(executive, today)
    cn, ce, cg = build_committees(committees, {}, observed, [snap], {})
    en, ee, eg = build_enactment([snap], xe, ids)
    _, _, bn, be = partition(cn + en, ce + ee)
    nodes = list(g["nodes"].values()) + bn
    edges = list(g["edges"].values()) + be
    gaps = g["gaps"] + cg + eg + [completeness_gap(congress, bills["meta"], len(bills["instruments"]))]
    if unknown:
        gaps.append(f"{sum(unknown.values())} sponsor(s) of the {_ordinal(congress)} Congress's bills not in the "
                    f"legislators files ({', '.join(sorted(unknown)[:5])}); no edge for them")
    return nodes, edges, gaps


_NOMINEE_RE = re.compile(r"^(?P<nominee>[^,]+?), of (?P<of>[^,]+?), to be (?P<position>.+?)"
                         r"(?:,?\s+vice\s.*|\s*\(.*\)\.?|\.)?$", re.S)
_OUTCOMES = (("confirmed", "Confirmed"), ("withdrawn", "withdrawal"), ("returned", "Returned to the President"),
             ("failed", "sine die adjournment"))


def nomination_outcome(latest_action_text):
    """What became of a nomination, read from its latest action's words.
    'other' keeps any wording this does not know rather than guessing. Pure."""
    text = latest_action_text or ""
    return next((name for name, phrase in _OUTCOMES if phrase in text), "pending" if text else "other")


def build_nominations(congress, noms, exec_holds, senate_votes=()):
    """One Congress's nominations → (nodes, edges, gaps). A nomination is an
    instrument (instrument/us/<c>/pn/<number>[-<part>], the id a roll call on
    it already has); the nominee is data on it, never a person node, because
    Congress.gov gives a name and no identifier. `nominated` comes from the
    President who held office on the day the Senate received it.

    senate_votes: (document_name, vote_id, date) of the Congress's Senate
    roll calls on nominations, read from the files; they become the
    confirmation_votes prop, not edges (older roll calls stay in files).
    Voteview collapses split citations (PN78-10 → PN7810), so a document name
    that a split citation collapses to is never linked: PN78-1 would read as
    the real PN781. Pure."""
    g = _graph()
    president = [e for e in exec_holds if e["dst"] == node_id("post", EXECUTIVE_POSTS["prez"][0])]
    collapsed = {c.replace("-", "") for c in noms if "-" in c}
    votes_of, ambiguous = {}, 0
    for name, vote_id, date in senate_votes:
        if name in collapsed:
            ambiguous += 1
            continue
        if name in noms:
            votes_of.setdefault(name, []).append({"vote_id": vote_id, "date": date})
    no_president = 0
    for cit, rec in noms.items():
        number = cit[2:]
        iid = f"instrument/us/{congress}/pn/{number}"
        m = _NOMINEE_RE.match(rec.get("description") or "")
        pos = (rec.get("positions") or [{}])[0]
        props = {"instrument_type": "pn", "congress": congress, "number": number, "jurisdiction": US,
                 "positions": rec.get("positions") or [], "organization": rec.get("organization"),
                 "military": rec.get("military"), "received": rec.get("received"),
                 "latest_action": rec.get("latest_action"),
                 "outcome": nomination_outcome((rec.get("latest_action") or {}).get("text")),
                 "outcome_derived_by": "the words of Congress.gov's latest action"}
        if m and not rec.get("military"):
            props.update(nominee=m.group("nominee").strip(), nominee_of=m.group("of").strip(),
                         nominee_parsed_by="regex over Congress.gov's description")
            name = f"{cit}: {props['nominee']}, to be {m.group('position').strip().rstrip(',.')}"
        else:
            props.update(nominee=None, nominee_parse="list or unparsed")
            title = pos.get("title") or rec.get("description") or "nomination"
            name = f"{cit}: {title}" + (f" ({rec['organization']})" if rec.get("organization") else "")
        if cit in votes_of:
            props["confirmation_votes"] = sorted(votes_of[cit], key=lambda v: v["date"])
        _node(g, iid, "instrument", name[:300], props, "congress.gov", f"us/{congress}/pn/{number}")
        who = holders_as_of(president, rec["received"]) if rec.get("received") else []
        if len(who) != 1:
            no_president += 1
            continue
        _edge(g, who[0]["src"], "nominated", iid, rec["received"], rec["received"], "ingested",
              "congress.gov", f"us/{congress}/pn/{number}/nominated", US, {"received": rec["received"]})
    if no_president:
        g["gaps"].append(f"{no_president} nomination(s) of the {_ordinal(congress)} Congress: no single President "
                         f"held office on the day received; no `nominated` edge")
    if ambiguous:
        g["gaps"].append(f"{ambiguous} roll call(s) of the {_ordinal(congress)} Congress name a nomination whose split "
                         f"citation Voteview collapsed; not linked")
    return list(g["nodes"].values()), list(g["edges"].values()), g["gaps"]


def _ordinal(n):
    return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


_STATE_CODE_CI = {name.lower(): code for name, code in STATE_CODE.items()}
DISTRICTS_SOURCE = "lewis-ucla"


def district_features(path):
    """One Lewis et al. GeoJSON file → its features with the geometry as
    GeoJSON text: 9,445 features parsed into Python objects at once would
    take several GB, their text about 1.5 GB. The geometry goes to the
    loader, never into a node."""
    data = json.loads(pathlib.Path(path).read_text())
    feats = data.get("features") or []
    out = []
    for f in feats:
        p = f.get("properties") or {}
        st = _STATE_CODE_CI.get((p.get("statename") or "").lower())
        start, end = int(p.get("startcong") or 0), int(p.get("endcong") or 0)
        n = int(p.get("district") or 0)
        out.append({"state": st, "district": n, "start": start, "end": end, "lewis_id": p.get("id"),
                    "sole": len(feats) == 1, "file": pathlib.Path(path).name,
                    "geometry": json.dumps(f["geometry"], separators=(",", ":")) if f.get("geometry") else None})
    return out


def build_districts(features, house_post_ids):
    """District shapes → (nodes, edges, gaps, geometry rows). One jurisdiction
    node per shape and span of Congresses; the state `contains` it for that
    span; the House post `represents` it once per Congress, dated by
    congress_span. A district 0 is the at-large seat (cd:1, as post_for
    keys it) only when it is the state's sole shape in that span: a state
    with an at-large seat beside numbered ones would give cd:1 two shapes.
    Pure."""
    g = _graph()
    geoms, unmatched, mixed_at_large, no_post = [], 0, 0, 0
    for f in features:
        st, n = f["state"], f["district"]
        if st is None or not f["start"] or not f["end"] or not f["geometry"]:
            unmatched += 1
            continue
        if n == 0:
            if not f["sole"]:
                mixed_at_large += 1
                continue
            n = 1
        div = f"{US}/state:{st}"
        sid = f"{div}/cd:{n}/shape:{f['start']}-{f['end']}"
        first, _ = congress_span(f["start"])
        _, last = congress_span(f["end"])
        span = _ordinal(f["start"]) + (f"–{_ordinal(f['end'])}" if f["end"] != f["start"] else "")
        _node(g, sid, "jurisdiction", f"{st.upper()}-{n} ({span} Congress)",
              {"level": "district", "state": st, "district": n, "congress_from": f["start"],
               "congress_to": f["end"], "lewis_id": f["lewis_id"], "jurisdiction": div},
              DISTRICTS_SOURCE, f["file"])
        if div not in g["nodes"]:
            _node(g, div, "jurisdiction", DIVISION_NAMES.get(st, st.upper()),
                  {"level": "state", "jurisdiction": div}, DISTRICTS_SOURCE)
        _edge(g, div, "contains", sid, first, last, "ingested", DISTRICTS_SOURCE, f"lewis/{f['lewis_id']}",
              div, {"congress_from": f["start"], "congress_to": f["end"]})
        post = node_id("post", f"us/house/{st}/cd:{n}")
        if post in house_post_ids:
            for c in range(f["start"], f["end"] + 1):
                a, b = congress_span(c)
                _edge(g, post, "represents", sid, a, b, "ingested", DISTRICTS_SOURCE,
                      f"lewis/{f['lewis_id']}/{c}", div, {"congress": c})
        else:
            no_post += 1
        geoms.append((sid, f["geometry"]))
    if unmatched:
        g["gaps"].append(f"{unmatched} district shape(s) with no state, Congress or geometry; skipped")
    if mixed_at_large:
        g["gaps"].append(f"{mixed_at_large} at-large shape(s) beside numbered districts in the same Congresses; "
                         f"not loaded (the seat keys at-large as cd:1, which a numbered district also is)")
    if no_post:
        g["gaps"].append(f"{no_post} district shape(s) with no House seat on record (no member held it in the "
                         f"legislators files); the shape is loaded without `represents`")
    return list(g["nodes"].values()), list(g["edges"].values()), g["gaps"], geoms


def load_geometry(cur, scope, geoms):
    """District geometry for nodes already in this transaction's scope. The
    shapes are NAD83 (EPSG:4269) as published; stored as WGS84 multipolygons,
    with invalid rings repaired rather than the load refused."""
    cur.execute("DELETE FROM graph_geometry WHERE scope = %s", (scope,))
    cur.execute("CREATE TEMP TABLE stage_geom (node_id TEXT, geojson TEXT) ON COMMIT DROP")
    with cur.copy("COPY stage_geom FROM STDIN") as cp:
        for nid, geom in geoms:
            cp.write_row((nid, geom))
    cur.execute("""
        INSERT INTO graph_geometry (node_id, geom, scope)
        SELECT node_id, ST_Multi(ST_CollectionExtract(ST_MakeValid(
                   ST_Transform(ST_SetSRID(ST_GeomFromGeoJSON(geojson), 4269), 4326)), 3)), %s
        FROM stage_geom
        ON CONFLICT (node_id) DO UPDATE SET geom = excluded.geom, scope = excluded.scope""", (scope,))
    cur.execute("DROP TABLE stage_geom")


LOBBYING_SOURCE = "lda"
# Bump when lobby_key or the lobbying builders change what they emit.
NORMALIZER_VERSION = 1


def lobbying_org_id(key):
    return node_id("organization", f"lda/org/{key}")


def latest_reports(filings):
    """One report per (registrant, client, period): the latest posted. An
    amendment replaces its original, so summing both would count the
    income twice. Every report type counts (Q1–Q4, amendments 1A–4A,
    terminations 1T–4T, their no-activity …Y forms); registrations (RR, RA)
    describe no period of activity and do not. Pure."""
    latest = {}
    for f in filings:
        if (f.get("type") or "R").startswith("R"):
            continue
        k = ((f.get("registrant") or {}).get("id"), (f.get("client") or {}).get("id"), f.get("period"))
        if k not in latest or (f.get("posted") or "") > (latest[k].get("posted") or ""):
            latest[k] = f
    return list(latest.values())


def build_lobbying_orgs(year_filings, cm_rows=()):
    """Every organization the filings name, across all years at once so a
    name is chosen the same way whichever year loads first: one node per
    exact lobby_key, named by its most frequent spelling (alphabetically
    first on a tie), with every LDA id and spelling kept on it. A committee
    whose connected organization's name has exactly that key gets a
    `connected_committee` edge, advisory: a shared name is evidence, not a
    filing that says so. year_filings: iterable of filing lists, read one
    year at a time. Returns (nodes, edges, gaps). Pure."""
    spellings, client_ids, registrant_ids = {}, {}, {}
    for filings in year_filings:
        for f in filings:
            for side, ids in (("client", client_ids), ("registrant", registrant_ids)):
                e = f.get(side) or {}
                name = (e.get("name") or "").strip()
                key = lobby_key(name)
                if not key:
                    continue
                spellings.setdefault(key, {})
                spellings[key][name] = spellings[key].get(name, 0) + 1
                if e.get("id") is not None:
                    ids.setdefault(key, set()).add(e["id"])
    g = _graph()
    for key, names in spellings.items():
        name = sorted(names.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
        _node(g, lobbying_org_id(key), "organization", name,
              {"natural_key": f"lda/org/{key}", "lobby_key": key,
               "lda_client_ids": sorted(client_ids.get(key, ())),
               "lda_registrant_ids": sorted(registrant_ids.get(key, ())),
               "name_variants": sorted(names)[:25], "identity": "exact normalized LDA name",
               "jurisdiction": US}, LOBBYING_SOURCE, key)
    linked = 0
    for row in cm_rows:
        if row.get("CMTE_TP") not in ("Q", "N") or not row.get("CONNECTED_ORG_NM"):
            continue
        key = lobby_key(row["CONNECTED_ORG_NM"])
        if key not in spellings:
            continue
        cid = node_id("organization", f"fec/committee/{row['CMTE_ID']}")
        # The same node shape build_money gives a committee, so the two
        # never overwrite each other with different names or props.
        _node(g, cid, "organization", row.get("CMTE_NM") or row["CMTE_ID"],
              {"natural_key": f"fec/committee/{row['CMTE_ID']}", "fec_id": row["CMTE_ID"], "jurisdiction": US},
              "fec", row["CMTE_ID"])
        _edge(g, lobbying_org_id(key), "connected_committee", cid, None, None, "advisory", "fec",
              f"fec/connected/{row['CMTE_ID']}", US,
              {"connected_org_name": row["CONNECTED_ORG_NM"],
               "derived_by": "the committee's connected-organization name equals the LDA name's key"})
        linked += 1
    if linked:
        g["gaps"].append(f"{linked} FEC committee(s) linked to a lobbying organization by name alone (advisory)")
    return list(g["nodes"].values()), list(g["edges"].values()), g["gaps"]


def build_lobbying_year(year, filings, org_ids, bill_ids):
    """One year's reports → (edges, gaps). `lobbied_for` (firm → client,
    ingested: the firm's own filing says so) with the income summed;
    `lobbied_on` (client → bill, advisory: the bill number is read from the
    activity's free text and its Congress inferred from the filing year),
    only for a bill that is a node, never with an amount (a report's money
    is not per bill). A self-filer lobbies for itself: no lobbied_for.
    Pure."""
    g = _graph()
    congress = congress_of(f"{year}-06-01")
    hired, on, missing_bill, missing_org = {}, {}, set(), 0
    for f in latest_reports(filings):
        reg, cli = lobby_key((f.get("registrant") or {}).get("name")), lobby_key((f.get("client") or {}).get("name"))
        rid, cid = lobbying_org_id(reg), lobbying_org_id(cli)
        if rid not in org_ids or cid not in org_ids:
            missing_org += 1
            continue
        if rid != cid:
            h = hired.setdefault((rid, cid), {"income": 0.0, "reports": 0, "periods": set()})
            h["income"] += float(f.get("income") or 0)
            h["reports"] += 1
            h["periods"].add(f.get("period"))
        for a in f.get("activities") or []:
            for b in a.get("bills") or []:
                bid = f"instrument/us/{congress}/{b}"
                if bid not in bill_ids:
                    missing_bill.add(bid)
                    continue
                o = on.setdefault((cid, bid), {"registrants": set(), "issues": set(), "reports": 0, "filings": []})
                o["registrants"].add((f.get("registrant") or {}).get("name"))
                o["issues"].add(a.get("issue"))
                o["reports"] += 1
                if len(o["filings"]) < 20 and f["uuid"] not in o["filings"]:
                    o["filings"].append(f["uuid"])
    first, last = f"{year}-01-01", f"{year}-12-31"
    for (rid, cid), h in hired.items():
        _edge(g, rid, "lobbied_for", cid, first, last, "ingested", LOBBYING_SOURCE,
              f"lda/{year}/{rid}/{cid}", US,
              {"year": year, "income": round(h["income"], 2), "reports": h["reports"],
               "periods": sorted(p for p in h["periods"] if p)})
    for (cid, bid), o in on.items():
        _edge(g, cid, "lobbied_on", bid, first, last, "advisory", LOBBYING_SOURCE,
              f"lda/{year}/{cid}/{bid}", US,
              {"year": year, "registrants": sorted(r for r in o["registrants"] if r)[:10],
               "issues": sorted(i for i in o["issues"] if i), "reports": o["reports"], "filings": o["filings"],
               "derived_by": "bill number read from the filing's activity text; Congress from the filing year"})
    if missing_bill:
        g["gaps"].append(f"{len(missing_bill)} bill number(s) in {year}'s filings name no bill of the "
                         f"{_ordinal(congress)} Congress on disk (a typo, or a bill of another Congress); no edge")
    if missing_org:
        g["gaps"].append(f"{missing_org} report(s) of {year} with an unnamed registrant or client; skipped")
    return list(g["edges"].values()), g["gaps"]


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
    return [json.loads(p.read_text()) for p in data_glob("fec")]


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
    for p in data_glob("votes"):
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


# ------------------------------------------------ state records, per request
#
# The state bill page reads the files the sync keeps (plan step 13): the Open
# States record, its roll calls, the legislature's newer actions, the text.
# A session's votes file is tens of MB, so each parsed file is kept while its
# mtime stands, a few at a time.

_STATE_FILE_CACHE, _STATE_FILE_MAX = {}, 6


def _state_file(kind, st, session):
    """A parsed derived file, cached per (kind, state, session, mtime);
    None when it is not on disk."""
    path = data_path(kind, state=st, session=session) if kind != "state_people" else data_path(kind, state=st)
    try:
        mtime = path.stat().st_mtime_ns
    except FileNotFoundError:
        return None
    key = (kind, st, session)
    hit = _STATE_FILE_CACHE.get(key)
    if hit and hit[0] == mtime:
        return hit[1]
    data = json.loads(path.read_text())
    if kind == "state_votes":
        by_bill = {}
        for v in data["votes"]:
            by_bill.setdefault(v["bill"], []).append(v)
        data = {"meta": data["meta"], "by_bill": by_bill}
    if len(_STATE_FILE_CACHE) >= _STATE_FILE_MAX:
        _STATE_FILE_CACHE.pop(next(iter(_STATE_FILE_CACHE)))
    _STATE_FILE_CACHE[key] = (mtime, data)
    return data


def state_bill_page(st, session, key):
    """Everything the state bill page shows, from files: {bill, meta, votes,
    newer_actions, people}, or None when the bill is not on disk. newer_actions
    are the legislature's own history rows dated after the Open States
    record's latest action: advisory, as the plan says."""
    bills = _state_file("state_bills", st, session)
    if not bills or key not in bills["bills"]:
        return None
    bill = bills["bills"][key]
    votes = (_state_file("state_votes", st, session) or {"by_bill": {}})["by_bill"].get(key, [])
    lis = _state_file("state_lis", st, session)
    newer = []
    if lis:
        last = bill.get("latest_action_date") or ""
        newer = [dict(h, source="legislature's daily file", certification="advisory")
                 for h in lis.get("history", {}).get(key, []) if (h.get("date") or "") > last]
    people = {p["id"]: p for p in (_state_file("state_people", st, None) or {"people": []})["people"]}
    return {"bill": bill, "meta": bills["meta"], "votes": votes, "newer_actions": newer, "people": people,
            "lis_meta": (lis or {}).get("meta")}


def loaded_states():
    """The legislatures with bills on this server, upper-case. A state not
    here answers 'not loaded on this server', never another state's data."""
    return {st.upper() for st in LEGISLATURES if state_sessions(st)}


# The ledger funnel's stages (agents/ledger_agent.build_funnel), from the
# Open States action classifications. Virginia has no "became-law" action:
# a bill the Governor signs is a chapter, so the signature is the law.
_STAGES = ("introduced", "committee", "passed", "law")
_STAGE_OF_CLASS = {"became-law": "law", "executive-signature": "law", "passage": "passed", "enrolled": "passed",
                   "executive-receipt": "passed", "referral-committee": "committee",
                   "committee-passage": "committee", "committee-passage-favorable": "committee",
                   "committee-failure": "committee"}
# The legislature's newer history rows carry text only.
_LIS_STAGE = ((re.compile(r"approved by governor|acts of assembly chapter|signed by governor", re.I), "law"),
              (re.compile(r"passed (?:house|senate)|enrolled|communicated to governor", re.I), "passed"),
              (re.compile(r"referred to committee|reported from", re.I), "committee"))


def state_stage(actions, newer=()):
    """How far a state bill got, furthest wins: its classified actions, then
    the legislature's newer (advisory) rows, which can only move it on. Pure."""
    best = 0
    for a in actions or []:
        for c in a.get("classification") or []:
            if c in _STAGE_OF_CLASS:
                best = max(best, _STAGES.index(_STAGE_OF_CLASS[c]))
    for h in newer or []:
        for rx, stage in _LIS_STAGE:
            if rx.search(h.get("description") or ""):
                best = max(best, _STAGES.index(stage))
                break
    return _STAGES[best]


def _newer_actions(st, session, key, b):
    """The legislature's history rows for a bill dated after its Open States
    record: advisory, as on the bill page."""
    lis = _state_file("state_lis", st, session) or {}
    last = b.get("latest_action_date") or ""
    return [h for h in lis.get("history", {}).get(key, []) if (h.get("date") or "") > last]


def state_bill_row(st, session, key, b, newer=()):
    """A bills-file record as one state search result; `newer` is the
    legislature's newer rows, which move its stage as they do on the bill
    page and in the search index. Pure."""
    stage = state_stage(b.get("actions"), newer)
    itype, number = key.split("/")
    primary = next((s for s in b.get("sponsors") or [] if s.get("primary")), None)
    return {"ocd_id": b.get("openstates_id"), "identifier": b["identifier"], "title": b["title"],
            "subjects": b.get("subjects") or [], "state": st.upper(), "jurisdiction": state_div(st),
            "session": session, "type": itype, "number": bill_number(number), "chamber": b.get("chamber"),
            "latest_action": b.get("latest_action"), "latest_action_date": b.get("latest_action_date"),
            "date_issued": b.get("first_action_date") or "",
            "sponsor": primary["name"] if primary else None,
            "is_law": stage == "law",
            "stage": stage,
            "path": f"/state/{st.lower()}/{session}/{itype}/{number}", "is_state_bill": True,
            "source": "open states"}


def state_bill_lookup(st, identifier, year=None):
    """[(session, key, bill)] for a bill number, newest session first: every
    session of `year` when one is named, else every session on disk. States
    renumber each session, so HB 1 is a different bill each year."""
    key = bill_key_of(identifier)
    if not key:
        return []
    out = []
    for sid in reversed(state_sessions(st)):
        if year and not in_session_year(sid, st, year):
            continue
        bills = _state_file("state_bills", st, sid)
        if bills and key in bills["bills"]:
            out.append((sid, key, bills["bills"][key]))
    return out


def state_recent_bills(st, n):
    """The n bills with the latest action across the sessions of the last
    two years on disk: the newest file alone can be next year's prefiles
    while a special session is the one sitting."""
    sessions = state_sessions(st)
    last = max((session_key(x, st)[0] for x in sessions), default=0)
    rows = []
    for sid in [x for x in sessions if session_key(x, st)[0] >= last - 1]:
        bills = _state_file("state_bills", st, sid) or {"bills": {}}
        rows += [(b.get("latest_action_date") or "", sid, k, b) for k, b in bills["bills"].items()]
    rows.sort(key=lambda r: (r[0], r[1], r[2]), reverse=True)
    return [state_bill_row(st, sid, k, b, _newer_actions(st, sid, k, b)) for _, sid, k, b in rows[:n]]


def _roster_names(p):
    return {n.lower() for n in [p.get("name") or "", *(p.get("other_names") or [])] if n}


def state_member_lookup(st, name):
    """A state legislator by name from the roster file: {person} on one
    match, {candidates} on a tie, {} on none. An exact name (or an alias
    the roster lists) wins; then a surname, current members first. A
    guess between two people is never made."""
    people = [p for p in (_state_file("state_people", st, None) or {"people": []})["people"]
              if any(r.get("type") in state_chambers(st) for r in p.get("roles") or [])]
    q = re.sub(r"^(?:del(?:egate)?|sen(?:ator)?|rep(?:resentative)?|assembly(?:man|woman|member)|asm)\.?\s+", "", (name or "").strip(), flags=re.I).lower()
    if not q:
        return {}
    # A deep link names the person by id (/state/va/member/<uuid>).
    hits = [p for p in people if p["id"].lower() in (q, "ocd-person/" + q)] or [p for p in people if q in _roster_names(p)]
    if not hits:
        hits = [p for p in people if (p.get("family_name") or "").lower() == q.split()[-1]
                and (len(q.split()) == 1 or (p.get("given_name") or "").lower().startswith(q.split()[0][0]))]
        current = [p for p in hits if any(r.get("end") is None for r in p.get("roles") or [])]
        hits = current or hits
    if len(hits) == 1:
        return {"person": state_member_card(st, hits[0])}
    if hits:
        return {"candidates": [state_member_card(st, p) for p in hits]}
    return {}


def state_member_card(st, p):
    """A roster person in the shape the state member view reads. Pure."""
    roles = sorted((r for r in p.get("roles") or [] if r.get("type") in state_chambers(st.lower())),
                   key=lambda r: r.get("start") or "", reverse=True)
    top = roles[0] if roles else {}
    conf = LEGISLATURES.get(st.lower()) or {}
    name_of = lambda t: conf.get(t) or (conf.get("name") if t == "legislature" else t)  # noqa: E731
    chamber = name_of(top.get("type")) or ""
    return {"ocd_person_id": p["id"], "name": p.get("name"), "party": p.get("party"), "state": st.upper(),
            "chamber": chamber, "district": top.get("district"), "current": top.get("end") is None,
            "photo_url": p.get("image") or "", "lis_id": (p.get("ids") or {}).get("lis"),
            "terms": [{"chamber": name_of(r["type"]), "district": r.get("district"),
                       "start": r.get("start"), "end": r.get("end")} for r in roles],
            "is_state_legislator": True, "source": "open states people"}


# A simple resolution is mostly a commendation: after the bills in a
# member's list, never hidden.
_SIMPLE_RESOLUTIONS = {"hr", "sr"}


def state_member_bills(st, person_id, n):
    """Bills a legislator sponsored, newest session first, primary before
    co-sponsor within a session, bills before simple resolutions: (rows, sessions read). Reading stops once
    n are found and the current sessions are read (a prefiled next session
    alone is not the member's record), so a count covers those sessions only."""
    rows, read = [], []
    sessions = state_sessions(st)
    current = set(current_state_sessions(sessions, state_vote_counts(st), st))
    for sid in reversed(sessions):
        bills = _state_file("state_bills", st, sid)
        if not bills:
            continue
        read.append(sid)
        mine = {}
        for k, b in bills["bills"].items():
            for s in b.get("sponsors") or []:
                if s.get("person") == person_id:
                    mine[k] = (min(mine.get(k, (True,))[0], not s["primary"]), k, b)
        mine = mine.values()
        rows += [dict(state_bill_row(st, sid, k, b, _newer_actions(st, sid, k, b)),
                      sponsorship="primary" if not co else "cosponsor")
                 # Newest number first within each group; two stable sorts,
                 # since "34a" has no negative.
                 for co, k, b in sorted(sorted(mine, key=lambda t: _number_order(t[1].split("/")[1]), reverse=True),
                                        key=lambda t: (t[0], t[1].split("/")[0] in _SIMPLE_RESOLUTIONS))]
        if len(rows) >= n and current <= set(read):
            break
    return rows, read


# Furthest along first: the version a reader wants when none is named.
_VERSION_ORDER = ("chapter", "enrolled", "reenrolled", "engrossed", "substitute", "amendment", "committee",
                  "printed", "introduced")


def state_versions(st, session, bill):
    """The bill's text versions with whether each is on disk: [{name, date,
    url, media_type, file, text}] in the record's order. Pure but for
    reading the text manifest."""
    manifest_path = data_path("state_text", state=st, session=session, name="manifest.json")
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    from sources.openstates import pick_link, stored_name
    out = []
    for v in bill.get("versions") or []:
        link = pick_link(v["links"])
        name = stored_name(manifest, link) if link else None
        entry = manifest.get(name) or {}
        out.append({"name": v["name"], "date": v.get("date"), "url": link["url"] if link else None,
                    "media_type": link["media_type"] if link else None, "file": name,
                    "text": entry.get("status") == "ok" and (link["media_type"] == "text/html" or entry.get("text"))})
    return out


def default_version(versions):
    """The furthest version with text on disk, else the last one. Pure."""
    def rank(v):
        n = (v["name"] or "").lower()
        return next((i for i, w in enumerate(_VERSION_ORDER) if w in n), len(_VERSION_ORDER))
    with_text = [v for v in versions if v["text"]]
    pool = with_text or versions
    return min(pool, key=rank) if pool else None


def html_text(raw):
    """A stored bill page's text. Virginia's legacy pages wrap the bill in
    their site's frame (<div id="mainC">) and are cp1252; other pages are
    read from the body, less scripts, styles and navigation. Pure."""
    from bs4 import BeautifulSoup
    html = raw.decode("utf-8") if raw[:3] == b"\xef\xbb\xbf" or _is_utf8(raw) else raw.decode("cp1252", "replace")
    soup = BeautifulSoup(html, "html.parser")
    main = soup.find(id="mainC") or soup.body or soup
    for tag in main(["script", "style", "nav", "header", "footer"]):
        tag.decompose()
    # Lines break at blocks, not at every tag: "§ <b>40.1-28.10</b> of the
    # Code" is one line of the bill.
    for br in main.find_all("br"):
        br.replace_with("\n")
    for block in main.find_all(["p", "div", "tr", "li", "h1", "h2", "h3", "h4", "table", "center"]):
        block.insert_after("\n")
    lines = [re.sub(r"[ \t\xa0\r\n]+", " ", ln).strip() for ln in main.get_text("").split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _is_utf8(raw):
    try:
        raw.decode("utf-8")
        return True
    except UnicodeDecodeError:
        return False


def state_version_text(st, session, version):
    """The text of one version as stored, or None when it is not on disk."""
    import gzip
    if not version or not version.get("file"):
        return None
    root = data_path("state_text", state=st, session=session, name="manifest.json").parent
    if version["media_type"] == "application/pdf":
        p = root / (version["file"] + ".txt.gz")
        return gzip.decompress(p.read_bytes()).decode("utf-8", "replace") if p.exists() else None
    p = root / (version["file"] + ".gz")
    return html_text(gzip.decompress(p.read_bytes())) if p.exists() else None


def state_vote_counts(st):
    """{session: roll calls on disk}, from each votes file's meta. A session
    that has not sat has a file with none: it is not the current one."""
    out = {}
    for sid in state_sessions(st):
        vp = data_path("state_votes", state=st, session=sid)
        out[sid] = json.loads(vp.read_text())["meta"].get("votes", 0) if vp.exists() else 0
    return out


def state_snapshot_votes(st, persons, year, topic, limit):
    """A state legislator's votes in one year, read from the Open States
    votes files of that year's sessions — the ones not loaded as edges.
    None when the year's sessions are loaded (the graph answers). Same row
    shape as the graph's. Returns (rows, total, truncated, files read)."""
    sessions = [sid for sid in state_sessions(st) if in_session_year(sid, st, year)]
    if not sessions or set(sessions) & set(current_state_sessions(state_sessions(st), state_vote_counts(st), st)):
        return None
    ids = {p["openstates_id"]: p for p in persons if p.get("openstates_id")}
    rows, files = [], []
    for sid in sessions:
        vp = data_path("state_votes", state=st, session=sid)
        if not vp.exists():
            continue
        files.append(vp.name)
        bills = json.loads(data_path("state_bills", state=st, session=sid).read_text())["bills"]
        for v in json.loads(vp.read_text())["votes"]:
            # A two-year session's file holds both years' roll calls.
            if not v["bill"] or not (v["date"] or "").startswith(str(year)):
                continue
            iid = state_instrument_id(st, sid, v["bill"])
            b = bills.get(v["bill"]) or {}
            title = f"{b.get('identifier', v['bill'])}: {b.get('title') or ''}".strip(": ")
            if topic:
                sref = state_bill_ref(topic)
                if sref:
                    if v["bill"] != f"{sref[1]}/{sref[2]}":
                        continue
                elif split_scope(topic)[1].lower() not in title.lower() and not any(
                        split_scope(topic)[1].lower() in (x or "").lower() for x in b.get("subjects") or []):
                    continue
            for voter, _name, option in v["positions"]:
                person = ids.get(voter)
                if not person:
                    continue
                rows.append({"person_id": person["id"], "person": person["name"],
                             "position": _STATE_POSITION.get(option, option), "date": v["date"],
                             "certification": "ingested", "vote_id": v["id"], "question": v["motion"],
                             "item_id": iid, "title": title, "instrument_type": v["bill"].split("/")[0],
                             "jurisdiction": state_div(st), "topic": (b.get("subjects") or [None])[0],
                             "topic_derived_by": "open states subject" if b.get("subjects") else None,
                             "result": v["result"], "meeting_id": None})
    rows.sort(key=lambda r: (r["date"], r["item_id"]), reverse=True)
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
    p = data_path("fec", cycle=cycle)
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
    return [json.loads(p.read_text()) for p in data_glob("votes", congress=congress)]


def write_member_congress(loaded_congress):
    """Reduce every roll-call snapshot of a Congress that is not loaded as
    edges to member-congress.json, one file in memory at a time. Returns
    (members, files read)."""
    index, files = {}, []
    for p in data_glob("votes"):
        if _snapshot_congress_of(p) in (None, loaded_congress):
            continue
        member_congress_index([json.loads(p.read_text())], index)
        files.append(p.name)
    path = data_path("certification")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(
        {"meta": {"built": datetime.date.today().isoformat(), "files": files}, "members": index},
        separators=(",", ":"), sort_keys=True))
    return len(index), files


def _fetched_path():
    """When each public file was last downloaded. The membership file carries
    no dates at all, so the download date is the only honest bound it has."""
    return data_path("public", name="public-fetched")


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


_LEGISLATORS = {"sig": None, "rows": []}


def legislators():
    """Both legislators files, merged (merge_legislators), for a request:
    reread only when a file changes. Loads read the files themselves."""
    paths = [data_path("public", name=n) for n in (LEGISLATORS_SOURCE, HISTORICAL_SOURCE)]
    sig = tuple(p.stat().st_mtime_ns if p.exists() else 0 for p in paths)
    if _LEGISLATORS["sig"] != sig:
        current, historical = (json.loads(p.read_text()) if p.exists() else [] for p in paths)
        _LEGISLATORS.update(sig=sig, rows=merge_legislators(current, historical)[0])
    return _LEGISLATORS["rows"]


def fetch_public(names=PUBLIC_FILES):
    """Download the public data files into public/. The only network step for
    them; the loader reads the files. Returns {name: bytes written}."""
    import requests
    out, changed = {}, []
    for name in names:
        r = requests.get(PUBLIC_DATA_URL.format(name), timeout=60)
        r.raise_for_status()
        json.loads(r.content)   # fail-closed: never overwrite a good file with a bad one
        path = data_path("public", name=name)
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists() or path.read_bytes() != r.content:
            path.write_bytes(r.content)
            changed.append(name)
        out[name] = len(r.content)
    # The date a file's content was first seen, not the last download: a
    # committee seat observed in March is still observed in March.
    fetched = json.loads(_fetched_path().read_text()) if _fetched_path().exists() else {}
    fetched.update({name: datetime.date.today().isoformat() for name in changed})
    _fetched_path().write_text(json.dumps(fetched, indent=1, sort_keys=True) + "\n")
    return out


def _senate_pn_votes(congress):
    """(document name, vote id, date) of one Congress's Senate roll calls on
    nominations, read from its roll-call files one at a time."""
    out = []
    for p in data_glob("votes", congress=congress):
        for v in json.loads(p.read_text()).get("votes", []):
            if v.get("chamber") == "senate" and (v.get("document_type") or "").upper() == "PN":
                out.append((v.get("document_name") or f"PN{v.get('document_number')}", v["vote_id"], v["date"]))
    return out


def _bill_ids_on_disk(exclude=None):
    """Every bill id with a record in a bills-<c>.json, read one file at a
    time and only its keys: the bills that are nodes in their own scope."""
    ids = set()
    for p in data_glob("bills"):
        c = int(p.stem.split("-")[1])
        if c != exclude:
            ids.update(f"instrument/us/{c}/{k}" for k in json.loads(p.read_text())["instruments"])
    return ids


def build_source(source, states=None):
    """Read the inputs for one source off disk and build. Returns
    (nodes, edges, gaps, delete scopes)."""
    if source == "us-congress":
        current = json.loads(data_path("public", name=LEGISLATORS_SOURCE).read_text())
        hist_path = data_path("public", name=HISTORICAL_SOURCE)
        historical = json.loads(hist_path.read_text()) if hist_path.exists() else []
        legislators, merge_gaps = merge_legislators(current, historical)
        # The current Congress's roll calls are edges; older sessions stay in
        # their files (the events rule) and only certify terms, through the
        # index write_member_congress reduced them to.
        congress = current_session()[0]
        snaps = _congress_snapshots(congress)
        if not snaps:
            raise RuntimeError(f"no derived/votes/congress-votes-{congress}-*.json — "
                               f"run `python graph.py snapshot {congress} 2 2026`")
        bills_path = data_path("bills", congress=congress)
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
            merge_gaps.append(completeness_gap(congress, bills["meta"], len(bills["instruments"])))
        else:
            merge_gaps.append(f"no bills-{congress}.json: bill records come from the snapshots' own copy, "
                        f"if any (run `python -m sources.govinfo billstatus {congress}`)")
        cert_path = data_path("certification")
        cert = json.loads(cert_path.read_text()) if cert_path.exists() else None
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
        exec_path = data_path("public", name="executive")
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
            noms_path = data_path("nominations", congress=congress)
            if noms_path.exists():
                # The roll calls made thin PN nodes from their own labels;
                # the nomination record replaces their name and props, and
                # its votes stay the roll calls' edges.
                nn, ne, ng = build_nominations(congress, json.loads(noms_path.read_text())["nominations"], xe)
                rich = {n["id"]: n for n in nn}
                nodes = [rich.pop(n["id"]) if n["id"] in rich else n for n in nodes] + list(rich.values())
                edges += ne
                gaps += ng
            else:
                gaps.append(f"no derived/nominations/nominations-{congress}.json: nominations are named "
                            f"only by their roll calls")
        else:
            gaps.append("no public/executive.json: the presidency is not loaded (run `python graph.py fetch`)")
        cpath, mpath = data_path("public", name=COMMITTEES_SOURCE), data_path("public", name="committee-membership-current")
        if cpath.exists() and mpath.exists():
            fetched = json.loads(_fetched_path().read_text()) if _fetched_path().exists() else {}
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
            gaps.append("no committee files in public/: committees are not loaded (run `python graph.py fetch`)")
        rn, re_, rg = build_related(snaps, {n["id"] for n in nodes if n["kind"] == "instrument"},
                                    _bill_ids_on_disk(exclude=congress))
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
            gaps.append("no derived/fec/fec-*.json: campaign money is not loaded (run `python -m sources.fec_client bulk 2026`)")
        if not historical:
            gaps.append("no public/legislators-historical.json: former members are not loaded "
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


# Bump when the build changes what a scope contains, so every older
# Congress reloads once instead of keeping rows the new code would not emit.
# ------------------------------------------------------ state legislatures
#
# A state legislature from files (plan of 2026-09-27): the Open States people
# repo is the roster, its monthly dump the bills and roll calls
# (sources/openstates.py), and — where the legislature publishes its own —
# the state's files check them (sources/lis.py). Same predicates as Congress.
# Open States scrapes the legislature, so what it says is `ingested`; a term
# is `certified` only by the legislature's own roll call, and an Open States
# vote the legislature's record contradicts is `advisory`.

STATE_SOURCE = "openstates"
# Bumped when the state builders change what a scope holds, so the next
# load rebuilds the state scopes without touching the federal ones.
# 2: an ambiguous LIS match is no match; quoted nicknames match voters.
# 4: every state's shapes — one chamber, multi-member seats, a chapter's
# year from its action, the sidecar's name for the session laws.
STATE_SCOPE_VERSION = 4
STATE_CHAMBERS = {"upper": "Senate", "lower": "House", "legislature": "Legislative"}
# Open States' vote options → the one position vocabulary.
_STATE_POSITION = {"yes": "aye", "no": "no", "not voting": "absent", "abstain": "present", "other": "other"}


def state_div(st):
    return f"{US}/state:{st.lower()}"


def state_person_key(st, person_id):
    """The natural key of a state roster person: `openstates/<uuid>`, or the
    federal key the sidecar's `identities` bridges it to (a delegate who
    went to Congress is one node). An asserted record keeps its own id."""
    if person_id.startswith("asserted/"):
        return person_id
    key = f"openstates/{person_id.removeprefix('ocd-person/')}"
    return IDENTITIES.get(key, key)


def state_chambers(st):
    """The legislature's chambers: Nebraska's one, else upper and lower."""
    return ("legislature",) if (LEGISLATURES.get(st) or {}).get("unicameral") else ("upper", "lower")


def _state_post(st, chamber, district):
    # OCD's form: New Hampshire's "Rockingham 30" is sldl:rockingham_30.
    # Nebraska's one chamber has upper-house districts in OCD.
    district_kind = "sldl" if chamber == "lower" else "sldu"
    slug = re.sub(r"\s+", "_", str(district).strip().lower())
    return f"{st}/{chamber}/{district_kind}:{slug}", f"{state_div(st)}/{district_kind}:{slug}"


def district_seats(rows, multi, retired=frozenset()):
    """How many members one district seats. 1 unless its chamber is listed
    under the legislature's `multi_member` (New Hampshire's House seats up
    to ten per district); then the most holders it had at once, an open
    term running on. A retired person's open term is the
    contradiction to close, never a seat. Pure."""
    if not multi:
        return 1
    # An open term runs to today. New Hampshire's 2022 map reused district
    # names: the new Rockingham 11 seats four, one of whom left in 2026.
    # A term's last day is its successor's first (the handover): ends are
    # exclusive.
    spans = [(h["valid_from"], h["valid_to"] or "9999") for h in rows if h["valid_to"] or h["src"] not in retired]
    return max([1, *(sum(1 for f, t in spans if f <= day < t) for day, _ in spans)])


def _lis_vote_days(lis_sessions):
    """{member id: [(date, vote id, source)]} from the legislature's own
    roll calls (lis-<session>.json records). Pure."""
    out = {}
    for rec in lis_sessions:
        src = (rec.get("meta") or {}).get("source") or "LIS"
        for v in rec.get("votes") or []:
            for mid in v["positions"]:
                out.setdefault(mid, []).append((v["date"], v["id"], src))
    return out


def build_state_skeleton(st, people, lis_sessions=(), today=None):
    """people.json → the legislature's chambers, districts, seats, the
    Governor's post, every person and every term. A term is certified when
    the legislature's own roll call (lis_sessions) has the member voting
    inside it; an Open States term alone is ingested. Pure. Returns (nodes,
    edges, gaps)."""
    today = today or datetime.date.today().isoformat()
    conf = LEGISLATURES.get(st) or {}
    g = _graph()
    sdiv = state_div(st)
    # The country node too: a fresh load runs the states before Congress,
    # and an edge may not point at a node that is not there yet.
    _node(g, US, "jurisdiction", "United States", {"level": "country"}, STATE_SOURCE)
    _node(g, sdiv, "jurisdiction", DIVISION_NAMES.get(st, st.upper()),
          {"level": "state", "jurisdiction": sdiv}, STATE_SOURCE)
    _edge(g, US, "contains", sdiv, None, None, "ingested", STATE_SOURCE, "seed", sdiv, {"derived": "seed"})
    body = {}
    for chamber in state_chambers(st):
        bid = node_id("organization", f"{st}/{chamber}")
        body[chamber] = bid
        name = conf.get(chamber) or (conf.get("name") if chamber == "legislature" else
                                     f"{DIVISION_NAMES.get(st, st)} {STATE_CHAMBERS[chamber]}")
        _node(g, bid, "organization", name,
              {"natural_key": f"{st}/{chamber}", "chamber": chamber, "level": "state", "jurisdiction": sdiv},
              STATE_SOURCE)
        _edge(g, sdiv, "has_body", bid, None, None, "ingested", STATE_SOURCE, "seed", sdiv, {"derived": "seed"})
    gov = node_id("post", f"{st}/governor")
    _node(g, gov, "post", f"Governor of {DIVISION_NAMES.get(st, st.upper())}",
          {"natural_key": f"{st}/governor", "role": "Governor", "jurisdiction": sdiv}, STATE_SOURCE)
    _edge(g, gov, "represents", sdiv, None, None, "ingested", STATE_SOURCE, "seed", sdiv, {"derived": "seed"})

    def post_for(chamber, district):
        key, division = _state_post(st, chamber, district)
        pid = node_id("post", key)
        if pid not in g["nodes"]:
            label = f"{conf.get(chamber + '_short') or STATE_CHAMBERS[chamber]} District {district}"
            if division not in g["nodes"]:
                _node(g, division, "jurisdiction", f"{st.upper()} {label}",
                      {"level": "district", "jurisdiction": sdiv}, STATE_SOURCE)
                _edge(g, sdiv, "contains", division, None, None, "ingested", STATE_SOURCE, "seed", sdiv,
                      {"derived": "seed"})
            _node(g, pid, "post", f"{label}, {g['nodes'][body[chamber]]['name']}",
                  {"natural_key": key, "role": label, "chamber": chamber, "level": "state", "jurisdiction": sdiv},
                  STATE_SOURCE)
            _edge(g, body[chamber], "has_seat", pid, None, None, "ingested", STATE_SOURCE, "seed", sdiv,
                  {"derived": "seed"})
            _edge(g, pid, "represents", division, None, None, "ingested", STATE_SOURCE, "seed", sdiv,
                  {"derived": "seed"})
        return pid

    holds, post_label, lis_of, retired = {}, {}, {}, set()
    skipped_roles = backwards = 0
    for p in people:
        roles = [r for r in p["roles"] if r["type"] in (*state_chambers(st), "governor")]
        if not roles:
            continue
        nk = state_person_key(st, p["id"])
        pid = node_id("person", nk)
        if p.get("retired"):
            retired.add(pid)
        props = {"natural_key": nk, "aliases": p.get("other_names") or [], "party": p.get("party"),
                 "openstates_id": p["id"], "jurisdiction": sdiv}
        if p.get("ids", {}).get("lis"):
            props["lis_member"] = p["ids"]["lis"]
            lis_of[pid] = p["ids"]["lis"]
        if p.get("asserted_by"):
            props["asserted_by"] = p["asserted_by"]
        if nk != f"openstates/{p['id'].removeprefix('ocd-person/')}" and not nk.startswith("asserted/"):
            props["identity"], props["identity_asserted_by"] = nk, "the sidecar's identities"
        prior = g["nodes"].get(pid)
        if prior is None:
            _node(g, pid, "person", p["name"], props, STATE_SOURCE, p["id"])
        else:
            # Two Open States records of one legislator (the sidecar joined
            # them): keep every name, and the serving record's name and ids.
            serving = any(r["type"] in state_chambers(st) and not r.get("end") for r in roles)
            every = {prior["name"], p["name"], *prior["props"]["aliases"], *props["aliases"]}
            if serving:
                _node(g, pid, "person", p["name"], {**prior["props"], **props}, STATE_SOURCE, p["id"])
            g["nodes"][pid]["props"]["aliases"] = sorted(every - {g["nodes"][pid]["name"]})
        for r in roles:
            if not r["start"]:
                skipped_roles += 1
                continue
            if r["end"] and r["end"] < r["start"]:
                backwards += 1     # the people repo's duplicate of a term, ending before it begins
                continue
            if r["type"] == "governor":
                post = gov
                post_label[post] = "Governor"
            else:
                if not r["district"]:
                    skipped_roles += 1
                    continue
                post = post_for(r["type"], r["district"])
                post_label[post] = g["nodes"][post]["props"]["role"]
            expired = r["end"] and r["end"] <= today
            hp = {"bound_from": "exact", "bounds_source": "open states people repo", "party": p.get("party")}
            if expired:
                hp["bound_to"] = "exact"
            elif r["end"]:
                hp["term_expires"] = r["end"]
            if p.get("asserted_by"):
                hp["asserted_by"] = p["asserted_by"]
            row = _edge(g, pid, "holds", post, r["start"], r["end"] if expired else None, "ingested",
                        STATE_SOURCE, f"{p['id']}/{r['type']}/{r['start']}", sdiv, hp)
            holds.setdefault(post, []).append(row)
    multi = set(conf.get("multi_member") or ())
    seats = {post: district_seats(rows, g["nodes"][post]["props"].get("chamber") in multi, retired)
             for post, rows in holds.items() if post != gov}
    for post, n in seats.items():
        if n > 1:
            g["nodes"][post]["props"]["seats"] = n
    _close_double_holds(g, holds, post_label, seats)

    days = _lis_vote_days(lis_sessions)
    certified = uncertified = 0
    for rows in holds.values():
        for h in rows:
            mid = lis_of.get(h["src"])
            end = h["valid_to"] or "9999"
            hits = sorted(d for d in days.get(mid, []) if d[0] and h["valid_from"] <= d[0] <= end) if mid else []
            if hits:
                h["certification"] = "certified"
                h["props"]["certified_by"] = (f"cross-source: the legislature's roll call {hits[0][1]} on "
                                              f"{hits[0][0]} ({hits[0][2]}) has this member voting inside the "
                                              f"term")
                certified += 1
            elif h["dst"] != gov:
                uncertified += 1
    first_lis = conf.get("lis_from")
    g["gaps"].append(f"{certified} {st.upper()} legislative term(s) certified by the legislature's own roll calls; "
                     f"{uncertified} are not" + (f" (its files begin with the {first_lis} session, so earlier terms "
                                                  f"cannot be)" if first_lis else
                                                  " (no independent source for this legislature is loaded on this "
                                                  "server, so every term is ingested from Open States alone)"))
    if skipped_roles:
        g["gaps"].append(f"{skipped_roles} role(s) with no start date or district skipped")
    if backwards:
        g["gaps"].append(f"{backwards} role(s) that end before they begin skipped")
    return list(g["nodes"].values()), list(g["edges"].values()), g["gaps"]


def state_instrument_id(st, session, key):
    return f"instrument/{st}/{session}/{key}"


# A resolution's chapter ("Res. Chapter 12", California) is not a law's.
_CHAPTER = re.compile(r"(?<!Res\. )Chapter (\d+)", re.I)
# Open States tags actions that are not the Governor's as executive-
# signature: New Hampshire's "Conference Committee Report; Not Signed Off",
# Texas' "Transmitted to the Governor", Virginia's "Governor's
# recommendation adopted". A signature says it was signed or approved.
_SIGNED_WORDS = re.compile(r"\b(?:signed|approved)\b", re.I)
_NOT_SIGNED = re.compile(r"\bnot\s+signed\b", re.I)


def executive_act(action):
    """"signed", "vetoed" or None for one Open States action: its
    classification, confirmed by its words. Pure."""
    text = action.get("description") or ""
    if "executive-veto" in action["classification"] and re.search(r"veto", text, re.I):
        return "vetoed"
    if "executive-signature" in action["classification"] and _SIGNED_WORDS.search(text) \
            and not _NOT_SIGNED.search(text):
        return "signed"
    return None


def _sponsor_by_name(name, day, last, members):
    """(node id, how) for a sponsor Open States names without an id, or None.
    Legislators serving on the bill's first action date first, then any
    serving during its life (a delegate elected in November prefiles before
    the January swearing-in); the surname and first initial first, then a
    surname alone that only one of them has ("Bell", "Mundon King"). Two
    candidates at any step are refused, never guessed. Pure."""
    name = re.sub(r"\s+-\s+resigned\b.*$", "", name or "", flags=re.I).strip()
    if not name or "committee" in name.lower() or not day:
        return None     # a committee patron is not a person
    key = state_name_key(name)
    surname = (name.split(",")[0] if "," in name else name.split()[-1]).strip().lower()
    on_day = [m for m in members if m[2] <= day <= (m[3] or "9999")]
    in_life = [m for m in members if m[2] <= (last or day) and (m[3] or "9999") >= day]
    for pool, when in ((on_day, "on the bill's first action date"), (in_life, "during the bill's life")):
        hits = {m[0] for m in pool if key and key in name_keys(m[1])}
        if not hits:
            hits = {m[0] for m in pool if surname in {k[0] for k in name_keys(m[1])}}
            how = f"surname alone, the only legislator serving {when} with it"
        else:
            how = f"surname and first initial, among the legislators serving {when}"
        if len(hits) == 1:
            return hits.pop(), how
        if len(hits) > 1:
            return None
    return None


def build_state_bills(st, session, rec, person_ids, governor_holds, known_ids=frozenset(), roster=None):
    """One session's bills file → instruments, who sponsored them, what the
    Governor did, the Acts of Assembly chapter, and related bills already on
    file. person_ids maps an Open States person id to our node id. A sponsor
    Open States names without an id is matched by name (surname and first
    initial) among the legislators of either chamber serving on the bill's
    first action date, since a co-patron can sit in the other chamber; none
    or two is counted, never guessed. roster: {chamber: [(node id, names,
    from, to, lis id)]}, as build_state_votes takes. Pure. Returns (nodes,
    edges, gaps)."""
    g = _graph()
    sdiv = state_div(st)
    laws = (LEGISLATURES.get(st) or {}).get("session_laws") or "Session Laws"
    name_only = unknown = no_governor = by_name = 0
    ids = {state_instrument_id(st, session, k) for k in rec["bills"]} | set(known_ids)
    members = [m for ms in (roster or {}).values() for m in ms]
    skey, years = session_key(session, st) or (0, 0), session_years(session, st) or (None, None)
    for key, b in rec["bills"].items():
        iid = state_instrument_id(st, session, key)
        itype, number = key.split("/")
        # Session ids do not sort or carry a year ("88", "103rd"): the order
        # and the years a lookup filters on are their own props.
        props = {"instrument_type": itype, "session": session, "number": number, "identifier": b["identifier"],
                 "session_sort": f"{skey[0]:04d}-{skey[1]:02d}", "first_year": years[0], "last_year": years[1],
                 "openstates_id": b["openstates_id"], "chamber": b.get("chamber"), "jurisdiction": sdiv,
                 "introduced": b.get("first_action_date"), "latest_action": b.get("latest_action"),
                 "latest_action_date": b.get("latest_action_date")}
        if b.get("subjects"):
            props["topic"], props["topic_derived_by"] = b["subjects"][0], "open states subject"
        _node(g, iid, "instrument", f"{b['identifier']}: {b['title']}", props, STATE_SOURCE, b["openstates_id"])
        ref = f"{st}/{session}/{key}/sponsors"
        day, last = b.get("first_action_date") or "", b.get("latest_action_date") or b.get("first_action_date") or ""
        linked = set()
        for sp in b["sponsors"]:
            extra = {}
            if not sp["person"]:
                hit = _sponsor_by_name(sp["name"], day, last, members)
                if hit is None:
                    name_only += 1
                    continue
                pid, how = hit
                by_name += 1
                extra["sponsor_matched_by"] = how
            else:
                pid = person_ids.get(sp["person"])
                if pid is None:
                    unknown += 1
                    continue
            if pid in linked:
                continue    # listed twice (primary and co-sponsor): the first row, the primary, stands
            linked.add(pid)
            _edge(g, pid, "sponsored", iid, b.get("first_action_date"), b.get("first_action_date"), "ingested",
                  STATE_SOURCE, ref, sdiv, {"role": "sponsor" if sp["primary"] else "cosponsor",
                                            "date_inferred": "the bill's first action; Open States gives no "
                                                             "sponsorship dates", **extra})
        aref = f"{st}/{session}/{key}/actions"
        seen = set()
        for a in b["actions"]:
            pred = executive_act(a)
            if pred and (pred, a["date"]) in seen:
                pred = None     # one act, recorded twice (the chapter line and the approval line)
            if pred:
                seen.add((pred, a["date"]))
                who = holders_as_of(governor_holds, a["date"])
                if len(who) == 1:
                    _edge(g, who[0]["src"], pred, iid, a["date"], a["date"], "ingested", STATE_SOURCE, aref, sdiv,
                          {"role": pred, "text": a["description"]})
                else:
                    no_governor += 1    # the chapter below does not depend on who signed
            m = _CHAPTER.search(a["description"] or "")
            # The chapter's year is the year it became law: a two-year
            # session's laws are numbered each year.
            year = (a["date"] or "")[:4]
            if m and ("became-law" in a["classification"] or "executive-signature" in a["classification"]) and year:
                lid = f"instrument/{st}/acts/{year}/chap/{m.group(1)}"
                _node(g, lid, "instrument", f"{laws} {year}, Chapter {m.group(1)}",
                      {"instrument_type": "chapter", "number": m.group(1), "year": year, "jurisdiction": sdiv},
                      STATE_SOURCE, aref)
                _edge(g, iid, "enacted_as", lid, a["date"], None, "ingested", STATE_SOURCE, aref, sdiv,
                      {"law_type": f"{laws} chapter"})
        for r in b.get("related") or []:
            rkey = bill_key_of(r["identifier"])
            rsid = r["session"] or session
            did = state_instrument_id(st, rsid, rkey) if rkey else None
            # Only to this session or an earlier one: sessions load oldest
            # first, and a later session's bill is not a node yet.
            if did and did in ids and did != iid and (session_key(rsid, st) or (0,)) <= (session_key(session, st) or (0,)):
                _edge(g, iid, "related_to", did, None, None, "ingested", STATE_SOURCE, f"{st}/{session}/{key}/related",
                      sdiv, {"relationship": r["relation"]})
    linked = sum(1 for e in g["edges"].values() if e["predicate"] == "sponsored")
    if name_only or unknown or by_name:
        # Some sessions' Open States rows carry no person id for many
        # sponsors; say how many were matched by name and how many were not.
        g["gaps"].append(f"{st.upper()} {session}: {linked} sponsorship(s) linked for {len(rec['bills'])} bill(s), "
                         f"{by_name} of them by name; {name_only} named without a person id that matched no "
                         f"single legislator and {unknown} by a person not in the roster, no edge")
    if no_governor:
        g["gaps"].append(f"{st.upper()} {session}: {no_governor} Governor action(s) on a day with no single "
                         f"Governor on file; no edge")
    return list(g["nodes"].values()), list(g["edges"].values()), g["gaps"]


# Letters, then the number. The type may carry dots and spaces ("H.B. 1",
# Illinois' "HJR CA1"); Colorado prefixes the year ("HB 17-1001"); a letter
# after the number is a separate bill (Nebraska's "LB 1001A" funds LB 1001,
# Florida's "HB 1A" is a special session's); Michigan numbers joint
# resolutions with letters ("HJR A"). Iowa's bare "1024DP", Missouri's
# substitutes and Nevada's "AB 160-82" stay unkeyed.
_BILL_ID = re.compile(r"([A-Za-z][A-Za-z. ]*?)\.?(?:\s*(?:\d{2}-)?0*(\d+[A-Za-z]{0,2})|\s+([A-Za-z]{1,2}))")


def bill_number(number):
    """A bill number as the API gives it: an int, or a string when a letter
    is part of it (Nebraska's "1001a", Michigan's "a"). Pure."""
    return int(number) if str(number).isdigit() else str(number)


def _number_order(number):
    """Sort key for a bill number: 34 before 34a before 35. Pure."""
    m = re.match(r"(\d*)(.*)", str(number))
    return int(m.group(1) or 0), m.group(2)


def bill_key_of(identifier):
    """'HB 1' → 'hb/1', 'H.B. 1' → 'hb/1', 'LB 1001A' → 'lb/1001a', 'HJR A'
    → 'hjr/a'; None for anything else. Pure."""
    m = _BILL_ID.fullmatch((identifier or "").strip())
    if not m:
        return None
    return f"{re.sub(r'[^a-z]', '', m.group(1).lower())}/{(m.group(2) or m.group(3)).lower()}"


def _lis_match(v, lis_by_key):
    """The legislature's roll call that is this Open States vote: same bill,
    chamber and day, and the same yes and no counts (a bill can have
    several roll calls a day). None when none matches, and when two do (two
    unanimous votes on one bill in a day): a guess would flag the wrong
    members. Pure."""
    hits = []
    for lv in lis_by_key.get((v["bill"], v["chamber"], v["date"]), []):
        yes = sum(1 for x in lv["positions"].values() if x == "yes")
        no = sum(1 for x in lv["positions"].values() if x == "no")
        if yes == v["counts"].get("yes", -1) and no == v["counts"].get("no", -1):
            hits.append(lv)
    return hits[0] if len(hits) == 1 else None


# Marks a roll call adds to a name: Nebraska's footnote star, Texas' chair
# "(C)" (sometimes unclosed).
_VOTER_MARK = re.compile(r"\*|\(C\)?$")
# What Texas' scraped roll calls put in the name column that is no one.
_NOT_A_VOTER = re.compile(r"(?i)present|absent|excused|(?:mr\.?\s+)?speaker|[a-z]\.?|")


@functools.lru_cache(maxsize=65536)
def _fold(name):
    """A name lower-cased and without accents, for comparing. Pure."""
    import unicodedata
    return "".join(c for c in unicodedata.normalize("NFKD", name.lower()) if not unicodedata.combining(c)).strip()


def build_state_votes(st, session, votes, person_ids, roster, lis_rec=None):
    """The current session's roll calls → `considered` (the chamber) and
    `voted_on` (each member). A voter Open States names without an id is
    matched by name among that chamber's members on the day; none or two is
    a counted gap, never a guess. When the legislature's own roll call
    (lis_rec) is the same vote and records a member differently, that
    member's edge is advisory, with both positions on it. Pure. roster: {chamber:
    [(person node id, [names], valid_from, valid_to, lis member id)]}."""
    g = _graph()
    sdiv = state_div(st)
    lis_by_key = {}
    for lv in (lis_rec or {}).get("votes") or []:
        lis_by_key.setdefault((lv["bill"], lv["chamber"], lv["date"]), []).append(lv)
    matched = unmatched = disagree = by_name = unresolved = no_bill = not_people = 0
    for v in votes:
        if not v["bill"] or v["chamber"] not in state_chambers(st):
            no_bill += 1
            continue
        iid = state_instrument_id(st, session, v["bill"])
        committee = v["motion"][len("Reported from "):].strip() if (v["motion"] or "").startswith("Reported from") else None
        _edge(g, node_id("organization", f"{st}/{v['chamber']}"), "considered", iid, v["date"], v["date"], "ingested",
              STATE_SOURCE, v["id"], sdiv, {"question": v["motion"], "result": v["result"], "counts": v["counts"],
                                            "chamber": v["chamber"], "committee": committee,
                                            "dedupe_key": v.get("dedupe_key")})
        lv = _lis_match(v, lis_by_key) if lis_by_key else None
        if lis_by_key:
            matched += bool(lv)
            unmatched += not lv
        members = [m for m in roster.get(v["chamber"], []) if m[2] <= v["date"] <= (m[3] or "9999")]
        for voter, name, option in v["positions"]:
            pid = person_ids.get(voter) if voter else None
            if pid is None:
                name = _VOTER_MARK.sub("", name or "").strip()
                if _NOT_A_VOTER.fullmatch(name):
                    not_people += 1
                    continue
                key = state_name_key(name)
                hits = {m[0] for m in members if key and key in name_keys(m[1])}
                how = "name within the chamber on the day"
                if not hits:
                    # A surname alone, one word or two: Nebraska's "Meyer",
                    # Texas' "Rodríguez Ramos", California's "Ávila Farías".
                    tail = _fold(name)
                    hits = {m[0] for m in members for n in m[1] if _fold(n) == tail or _fold(n).endswith(" " + tail)}
                    how = "surname alone, within the chamber on the day"
                if not hits and len(name) >= 4 and " " not in name:
                    # Texas' roll calls cut long surnames ("Talaric", "Schofiel").
                    tail = _fold(name)
                    hits = {m[0] for m in members for n in m[1] if _fold(n).split()[-1:] and
                            _fold(n).split()[-1].startswith(tail)}
                    how = "the start of a surname the roll call cut off, within the chamber on the day"
                if len(hits) != 1:
                    unresolved += 1
                    continue
                pid = hits.pop()
                by_name += 1
            position = _STATE_POSITION.get(option, option)
            props = {"position": position, "vote_id": v["id"], "chamber": v["chamber"], "question": v["motion"]}
            if voter is None:
                props["voter_matched_by"] = how
            cert = "ingested"
            lis_member = next((m[4] for m in members if m[0] == pid), None)
            if lv and lis_member and lis_member in lv["positions"]:
                lis_pos = _STATE_POSITION.get(lv["positions"][lis_member], lv["positions"][lis_member])
                if lis_pos != position:
                    cert = "advisory"
                    props.update(lis_position=lis_pos, lis_vote=lv["id"],
                                 conflict="the legislature's own roll call records this member differently")
                    disagree += 1
            _edge(g, pid, "voted_on", iid, v["date"], v["date"], cert, STATE_SOURCE, v["id"], sdiv, props)
    if lis_by_key:
        g["gaps"].append(f"{st.upper()} {session}: {matched} roll call(s) matched to the legislature's own record, "
                         f"{unmatched} not; {disagree} member position(s) it contradicts are advisory")
    if by_name or unresolved:
        g["gaps"].append(f"{st.upper()} {session}: {by_name} voter(s) matched by name; {unresolved} could not be "
                         f"matched to exactly one member and were dropped")
    if not_people:
        g["gaps"].append(f"{st.upper()} {session}: {not_people} voter row(s) that name no member by name "
                         f"(\"Present\", \"Speaker\", a lone initial) not loaded")
    if no_bill:
        g["gaps"].append(f"{st.upper()} {session}: {no_bill} roll call(s) on no bill or no chamber not loaded")
    return list(g["nodes"].values()), list(g["edges"].values()), g["gaps"]


_NAME_SUFFIX = {"jr", "sr", "ii", "iii", "iv"}


def state_name_keys(names):
    """Every key a roster member answers to: each of their names, and a
    quoted nickname used as the first name ('Robert "Bob" Smith' is also
    Bob Smith). Pure."""
    keys = set()
    for n in names:
        keys.add(state_name_key(n))
        for nick in re.findall(r'"([^"]+)"', n or ""):
            rest = re.sub(r'"[^"]*"', " ", n).split()
            if rest:
                keys.add(state_name_key(f"{nick} {rest[-1]}"))
    keys.discard(None)
    return keys


@functools.lru_cache(maxsize=65536)
def _name_keys(names):
    return frozenset(state_name_keys(names))


def name_keys(names):
    """state_name_keys, cached: a roll call matches every voter against
    every member serving that day (52,000 voters in New Hampshire's 2026)."""
    return _name_keys(tuple(names))


def state_name_key(name):
    """(surname, first initial) of a name as roll calls and rosters write
    it: "Michael J. Jones", "Mike Jones" and "Jones, Michael, J." are one
    key; a suffix, middle names and a quoted nickname drop out. Two members
    sharing it in one chamber on one day are a tie the caller refuses.
    None for a name with no surname. Pure."""
    text = re.sub(r'"[^"]*"', " ", name or "")
    parts = [p.strip() for p in text.split(",") if p.strip()]
    parts = [p for p in parts if p.lower().strip(".") not in _NAME_SUFFIX]
    if len(parts) > 1:
        # "Last, First …" — the surname leads.
        last_part, first_part = parts[0], " ".join(parts[1:])
    else:
        words = parts[0].split() if parts else []
        words = [w for w in words if w.lower().strip(".") not in _NAME_SUFFIX]
        if len(words) < 2:
            return None
        last_part, first_part = words[-1], words[0]
    last = re.sub(r"[^a-z'-]", "", last_part.split()[-1].lower())
    first = re.sub(r"[^a-z]", "", first_part.lower())
    return (last, first[0]) if last and first else None


SCOPE_VERSION = 1
SKELETON, CURRENT = "us/skeleton", "us/current"


def load_scope(cur, scope, nodes, edges, fingerprint=None, orphan_kinds=None):
    """Replace one scope's rows, on the caller's cursor (so one transaction
    can hold many scopes). COPY into staging, then set-based upserts:
    nodes merge props and never change owner; edges take this scope. A
    node this scope no longer emits is deleted only if no edge of any
    scope still points at it. Fail-closed: an error rolls the caller's
    transaction back and the previous graph stays served."""
    from psycopg.types.json import Jsonb
    cur.execute("""CREATE TEMP TABLE stage_node (id TEXT, kind TEXT, name TEXT, props JSONB,
                                                 source_id TEXT, source_ref TEXT) ON COMMIT DROP""")
    cur.execute("""CREATE TEMP TABLE stage_edge (src TEXT, predicate TEXT, dst TEXT, valid_from DATE,
                                                 valid_to DATE, certification TEXT, source_id TEXT,
                                                 source_ref TEXT, props JSONB) ON COMMIT DROP""")
    with cur.copy("COPY stage_node FROM STDIN") as cp:
        for n in nodes:
            cp.write_row((n["id"], n["kind"], n["name"], Jsonb(n["props"]), n["source_id"], n["source_ref"]))
    with cur.copy("COPY stage_edge FROM STDIN") as cp:
        for e in edges:
            cp.write_row((e["src"], e["predicate"], e["dst"], e["valid_from"], e["valid_to"],
                          e["certification"], e["source_id"], e["source_ref"], Jsonb(e["props"])))
    cur.execute("DELETE FROM graph_edge WHERE scope = %s", (scope,))
    # Props merge rather than replace: a person both layers know keeps what
    # the other loader recorded about them.
    cur.execute("""
        INSERT INTO graph_node (id, kind, name, props, source_id, source_ref, scope)
        SELECT DISTINCT ON (id) id, kind, name, props, source_id, source_ref, %s FROM stage_node
        ON CONFLICT (id) DO UPDATE SET
            kind = excluded.kind, name = excluded.name, props = graph_node.props || excluded.props,
            source_id = excluded.source_id, source_ref = excluded.source_ref, updated_at = NOW(),
            scope = COALESCE(graph_node.scope, excluded.scope)""", (scope,))
    # An edge two scopes assert (a shared seed) belongs to whichever loaded
    # it last; either reload re-asserts it, so it is never lost.
    cur.execute("""
        INSERT INTO graph_edge (src, predicate, dst, valid_from, valid_to, certification,
                                source_id, source_ref, props, scope)
        SELECT DISTINCT ON (src, predicate, dst, source_ref)
               src, predicate, dst, valid_from, valid_to, certification, source_id, source_ref, props, %s
        FROM stage_edge
        ON CONFLICT (src, predicate, dst, source_ref) DO UPDATE SET
            valid_from = excluded.valid_from, valid_to = excluded.valid_to,
            certification = excluded.certification, source_id = excluded.source_id,
            props = excluded.props, scope = excluded.scope""", (scope,))
    # orphan_kinds: a bill scope deletes only its bills. A committee that
    # only bill records name is shared by several Congresses, and a later
    # scope's edges to it are not inserted yet when this one looks.
    cur.execute("""
        DELETE FROM graph_node n WHERE n.scope = %s AND (%s::text[] IS NULL OR n.kind = ANY(%s))
          AND NOT EXISTS (SELECT 1 FROM stage_node s WHERE s.id = n.id)
          AND NOT EXISTS (SELECT 1 FROM graph_edge e WHERE e.src = n.id)
          AND NOT EXISTS (SELECT 1 FROM graph_edge e WHERE e.dst = n.id)""",
                (scope, orphan_kinds, orphan_kinds))
    cur.execute("""
        INSERT INTO graph_scope (scope, fingerprint, loaded_at, nodes, edges) VALUES (%s, %s, NOW(), %s, %s)
        ON CONFLICT (scope) DO UPDATE SET fingerprint = excluded.fingerprint, loaded_at = NOW(),
            nodes = excluded.nodes, edges = excluded.edges""", (scope, fingerprint, len(nodes), len(edges)))
    # A --fresh run loads every scope in one transaction: drop now, not at commit.
    cur.execute("DROP TABLE stage_node, stage_edge")


def _us_fingerprint(congress):
    """What an older Congress's scope was built from: its bills file, the
    legislators files (a bioguide id moves sponsors), the committee and
    executive files, the build's version."""
    path = data_path("bills", congress=congress)
    bills = json.loads(path.read_text())["meta"]
    # The file itself, not only its meta: a same-day rebuild with the same
    # counts (a parser fix) keeps both, and must still reload.
    stat = path.stat()
    fetched = json.loads(_fetched_path().read_text()) if _fetched_path().exists() else {}
    # committees-current decides which committee a referral points at;
    # executive decides who signed. Either changing must reload the scope.
    return json.dumps({"v": SCOPE_VERSION, "bills": bills.get("fetched"), "counts": bills.get("counts"),
                       "file": [stat.st_size, stat.st_mtime_ns],
                       "inputs": {k: fetched.get(k) for k in (LEGISLATORS_SOURCE, HISTORICAL_SOURCE,
                                                              COMMITTEES_SOURCE, EXECUTIVE_SOURCE)}},
                      sort_keys=True)


def load_us(cur, only_current=False, force=False):
    """The federal graph, scope by scope, in dependency order: the
    skeleton (people must exist before anything points at them), each
    older Congress whose inputs changed, then the current Congress, whose
    related bills point into the older scopes. Returns {scope: (summary,
    gaps)}."""
    out = {}
    nodes, edges, gaps, _ = build_source("us-congress")
    skel_n, skel_e, cur_n, cur_e = partition(nodes, edges)
    if not only_current:
        load_scope(cur, SKELETON, skel_n, skel_e)
        closed = _close_holds_in_db(sorted({n["id"] for n in skel_n if n["kind"] == "person"}), cur)
        out[SKELETON] = (_summary(skel_n, skel_e),
                         [f"{c['name']}'s hold on {c['post']} closed at {c['valid_to']}: {c['why']}"
                          for c in closed])
        current = json.loads(data_path("public", name=LEGISLATORS_SOURCE).read_text())
        hist = data_path("public", name=HISTORICAL_SOURCE)
        legislators, _ = merge_legislators(current, json.loads(hist.read_text()) if hist.exists() else [])
        executive = json.loads(data_path("public", name="executive").read_text())
        committees = json.loads(data_path("public", name=COMMITTEES_SOURCE).read_text())
        observed = (json.loads(_fetched_path().read_text()) if _fetched_path().exists() else {}).get(
            "committee-membership-current") or datetime.date.today().isoformat()
        cur.execute("SELECT scope, fingerprint FROM graph_scope")
        loaded = dict(cur.fetchall())
        this = current_session()[0]
        out.update(_load_districts(cur, loaded, {n["id"] for n in skel_n if n["kind"] == "post"}, force))
        for p in data_glob("bills"):
            c = int(p.stem.split("-")[1])
            if c >= this:
                continue
            scope, fp = f"us/bills/{c}", _us_fingerprint(c)
            if not force and loaded.get(scope) == fp:
                continue
            bn, be, bg = build_bills_scope(c, json.loads(p.read_text()), legislators, executive,
                                           committees, observed)
            load_scope(cur, scope, bn, be, fp, orphan_kinds=["instrument"])
            out[scope] = (_summary(bn, be), bg)
        _, exec_holds, _ = build_executive(executive)
        for p in data_glob("nominations"):
            c = int(p.stem.split("-")[1])
            if c >= this:
                continue
            noms = json.loads(p.read_text())["nominations"]
            scope = f"us/nominations/{c}"
            fp = json.dumps({"v": SCOPE_VERSION, "n": len(noms),
                             "updated": max((r.get("updated") or "" for r in noms.values()), default=""),
                             "votes": [f.stat().st_mtime_ns for f in data_glob("votes", congress=c)],
                             "executive": (json.loads(_fetched_path().read_text()) if _fetched_path().exists()
                                           else {}).get(EXECUTIVE_SOURCE)}, sort_keys=True)
            if not force and loaded.get(scope) == fp:
                continue
            nn, ne, ng = build_nominations(c, noms, exec_holds, _senate_pn_votes(c))
            load_scope(cur, scope, nn, ne, fp, orphan_kinds=["instrument"])
            out[scope] = (_summary(nn, ne), ng)
    load_scope(cur, CURRENT, cur_n, cur_e, orphan_kinds=["instrument"])
    out[CURRENT] = (_summary(cur_n, cur_e), gaps)
    if not only_current:
        # Last: a report's bill may be one the current Congress introduced today.
        out.update(_load_lobbying(cur, loaded, force))
    return out


def _stat(path):
    st = path.stat()
    return [st.st_size, st.st_mtime_ns]


def state_sessions(st):
    """The Open States sessions on disk for a state, oldest first."""
    return sorted({p.stem.removeprefix("bills-") for p in data_glob("state_bills", state=st)
                   if session_key(p.stem.removeprefix("bills-"), st)}, key=lambda sid: session_key(sid, st))


def current_state_sessions(sessions, votes_count, st=None):
    """The sessions whose roll calls are graph edges: every session of the
    latest year that has any roll call (a regular session and its specials).
    Older sessions' votes stay in their files, as the events rule says.
    Pure."""
    years = [session_key(s, st)[0] for s in sessions if votes_count.get(s)]
    return [s for s in sessions if years and session_key(s, st)[0] == max(years)]


def state_scopes(st, today=None):
    """Every scope of one state from the files on disk, in load order — the
    skeleton, each session's bills, then the current session's roll calls —
    as {scope: (fingerprint, orphan_kinds, build)}. `build()` returns
    (nodes, edges, gaps); nothing but the small skeleton is built until it
    is called, so an unchanged scope costs a stat, not a build. The
    fingerprints are the inputs' size and mtime and the build's version."""
    people_path = data_path("state_people", state=st)
    if not people_path.exists():
        raise RuntimeError(f"no people file for {st}; run `python -m sources.openstates people {st}`")
    people = json.loads(people_path.read_text())["people"]
    lis_paths = {p.stem.removeprefix("lis-"): p for p in data_glob("state_lis", state=st)}
    lis = {sid: json.loads(p.read_text()) for sid, p in lis_paths.items()}
    base = {"v": SCOPE_VERSION, "state_v": STATE_SCOPE_VERSION, "people": _stat(people_path), "conf": LEGISLATURES.get(st),
            "identities": {k: v for k, v in IDENTITIES.items() if k.startswith("openstates/")}}
    skeleton = build_state_skeleton(st, people, list(lis.values()), today)
    sn, se, _ = skeleton
    out = {f"{st}/skeleton": (json.dumps({**base, "lis": {k: _stat(p) for k, p in lis_paths.items()}},
                                         sort_keys=True), None, lambda: skeleton)}
    person_ids = {p["id"]: node_id("person", state_person_key(st, p["id"])) for p in people}
    gov = node_id("post", f"{st}/governor")
    gov_holds = [e for e in se if e["predicate"] == "holds" and e["dst"] == gov]
    names = {n["id"]: [n["name"], *n["props"].get("aliases", [])] for n in sn if n["kind"] == "person"}
    lis_member = {n["id"]: n["props"].get("lis_member") for n in sn if n["kind"] == "person"}
    chamber_of = {n["id"]: n["props"].get("chamber") for n in sn if n["kind"] == "post"}
    roster = {}
    for e in se:
        if e["predicate"] == "holds" and chamber_of.get(e["dst"]):
            roster.setdefault(chamber_of[e["dst"]], []).append(
                (e["src"], names.get(e["src"], []), e["valid_from"], e["valid_to"], lis_member.get(e["src"])))
    sessions = state_sessions(st)
    votes_count, known = state_vote_counts(st), set()
    bills_paths = {sid: data_path("state_bills", state=st, session=sid) for sid in sessions}

    def known_ids():
        # Every session's bill ids, read once and only when a scope builds.
        if not known:
            for sid, path in bills_paths.items():
                known.update(state_instrument_id(st, sid, k) for k in json.loads(path.read_text())["bills"])
        return known

    for sid, path in bills_paths.items():
        out[f"{st}/bills/{sid}"] = (
            json.dumps({**base, "bills": _stat(path)}, sort_keys=True), ["instrument"],
            lambda sid=sid, path=path: build_state_bills(st, sid, json.loads(path.read_text()), person_ids,
                                                         gov_holds, known_ids(), roster))
    current = current_state_sessions(sessions, votes_count, st)
    inputs = {sid: [_stat(data_path("state_votes", state=st, session=sid)),
                    _stat(lis_paths[sid]) if sid in lis_paths else None] for sid in current}

    def build_current():
        cn, ce, cg = [], [], []
        for sid in current:
            vp = data_path("state_votes", state=st, session=sid)
            n, e, gaps = build_state_votes(st, sid, json.loads(vp.read_text())["votes"], person_ids, roster,
                                           lis.get(sid))
            cn += n
            ce += e
            cg += gaps
        cg.append(f"{st.upper()} roll calls before the current session ({', '.join(current) or 'none'}) stay in "
                  f"their files and are read at the leaf, not loaded as edges")
        return cn, ce, cg

    out[f"{st}/current"] = (json.dumps({**base, "votes": inputs}, sort_keys=True), [], build_current)
    return out


def build_state(st, today=None):
    """Every scope of one state, built: {scope: (nodes, edges, gaps)}. For
    a dry run; the loader builds only what changed."""
    return {scope: build() for scope, (_, _, build) in state_scopes(st, today).items()}


def load_state(cur, st, force=False):
    """One state's scopes, each only when its inputs changed. The skeleton
    first (people before anything points at them). Returns {scope:
    (summary, gaps)}."""
    if not data_path("state_people", state=st).exists():
        # Fail-open: the rest of the daily load goes on, and says why.
        return {f"{st}/skeleton": ({}, [f"{st.upper()} not loaded: no people file on disk "
                                        f"(python -m sources.openstates people {st})"])}
    cur.execute("SELECT scope, fingerprint FROM graph_scope")
    loaded = dict(cur.fetchall())
    out = {}
    scopes = state_scopes(st)
    for scope, (fp, orphan_kinds, build) in scopes.items():
        if not force and loaded.get(scope) == fp:
            continue
        nodes, edges, gaps = build()
        load_scope(cur, scope, nodes, edges, fp, orphan_kinds=orphan_kinds)
        if scope.endswith("/skeleton"):
            closed = _close_holds_in_db(sorted({n["id"] for n in nodes if n["kind"] == "person"}), cur)
            gaps = gaps + [f"{c['name']}'s hold on {c['post']} closed at {c['valid_to']}: {c['why']}" for c in closed]
        out[scope] = (_summary(nodes, edges), gaps)
    if out:
        # A person whose id changed (a new identity link) keeps the old node
        # while the bill and vote scopes still point at it, so the skeleton's
        # own orphan pass cannot drop it. Once every scope has moved, sweep
        # it; otherwise two nodes answer to one name.
        skel = f"{st}/skeleton"
        keep = [n["id"] for n in scopes[skel][2]()[0]]
        cur.execute("""
            DELETE FROM graph_node n WHERE n.scope = %s AND n.kind = 'person' AND NOT (n.id = ANY(%s))
              AND NOT EXISTS (SELECT 1 FROM graph_edge e WHERE e.src = n.id)
              AND NOT EXISTS (SELECT 1 FROM graph_edge e WHERE e.dst = n.id)""", (skel, keep))
        if cur.rowcount:
            summary, gaps = out.get(skel, ({}, []))
            out[skel] = (summary, gaps + [f"{cur.rowcount} person node(s) no longer emitted (a changed identity) "
                                          f"swept after every scope moved"])
    return out


DISTRICTS = "us/districts"
LOBBYING_ORGS = "us/lobbying/orgs"


def _load_lobbying(cur, loaded, force):
    """The organizations (all years at once) and then each year's edges,
    each only when its inputs changed. A year scope's fingerprint carries
    the organizations' too: new organizations can change its endpoints."""
    from sources.fec_client import _CM_COLS, _bulk_rows
    files = data_glob("lobbying")
    if not files:
        return {}
    years = [int(p.stem.split("-")[1]) for p in files]
    cm_zips = [z for c in sorted({y + y % 2 for y in years})
               for z in [DATA_DIR / "raw" / "fec" / str(c) / "cm.zip"] if z.exists()]
    metas = []
    for p in files:
        m = json.loads(p.read_text())["meta"]
        metas.append([p.name, m.get("newest_posted"), (m.get("counts") or {}).get("filings")])
    orgs_fp = json.dumps({"v": NORMALIZER_VERSION, "years": metas,
                          "cm": [z.stat().st_mtime_ns for z in cm_zips]}, sort_keys=True)
    out = {}
    org_ids = None
    if force or loaded.get(LOBBYING_ORGS) != orgs_fp:
        cm_rows = [r for z in cm_zips for r in _bulk_rows(z, _CM_COLS)]
        nodes, edges, gaps = build_lobbying_orgs(
            (json.loads(p.read_text())["filings"].values() for p in files), cm_rows)
        load_scope(cur, LOBBYING_ORGS, nodes, edges, orgs_fp, orphan_kinds=["organization"])
        out[LOBBYING_ORGS] = (_summary(nodes, edges), gaps)
        org_ids = {n["id"] for n in nodes}
    bills_fp = [(p.name, p.stat().st_mtime_ns) for p in data_glob("bills")]
    bill_ids = None
    for p, year, meta in zip(files, years, metas):
        scope = f"us/lobbying/{year}"
        fp = json.dumps({"v": NORMALIZER_VERSION, "year": meta, "orgs": orgs_fp, "bills": bills_fp},
                        sort_keys=True)
        if not force and loaded.get(scope) == fp:
            continue
        if org_ids is None:
            cur.execute("SELECT id FROM graph_node WHERE scope = %s", (LOBBYING_ORGS,))
            org_ids = {r[0] for r in cur.fetchall()}
        if bill_ids is None:
            bill_ids = _bill_ids_on_disk()
        edges, gaps = build_lobbying_year(year, json.loads(p.read_text())["filings"].values(), org_ids, bill_ids)
        load_scope(cur, scope, [], edges, fp)
        out[scope] = (_summary([], edges), gaps)
    return out


def _load_districts(cur, loaded, post_ids, force):
    """The district shapes scope, when PostGIS is there and the shapes or
    the legislators files changed. Without PostGIS it is skipped and says
    so: the rest of the federal load does not depend on it."""
    cur.execute("SELECT 1 FROM pg_extension WHERE extname = 'postgis'")
    if cur.fetchone() is None:
        return {DISTRICTS: ({"nodes": 0, "edges": 0, "by_kind": {}, "by_predicate": {}},
                            ["PostGIS is not installed: district shapes are not loaded"])}
    raw = DATA_DIR / "raw" / "districts"
    files = sorted(raw.glob("*.geojson"))
    manifest = raw / "manifest.json"
    fetched = json.loads(_fetched_path().read_text()) if _fetched_path().exists() else {}
    fp = json.dumps({"v": SCOPE_VERSION, "files": len(files),
                     "manifest": hashlib.sha1(manifest.read_bytes()).hexdigest() if manifest.exists() else None,
                     "legislators": [fetched.get(LEGISLATORS_SOURCE), fetched.get(HISTORICAL_SOURCE)]},
                    sort_keys=True)
    if not files or (not force and loaded.get(DISTRICTS) == fp):
        return {}
    features = [f for p in files for f in district_features(p)]
    nodes, edges, gaps, geoms = build_districts(features, post_ids)
    load_scope(cur, DISTRICTS, nodes, edges, fp, orphan_kinds=["jurisdiction"])
    load_geometry(cur, DISTRICTS, geoms)
    return {DISTRICTS: (_summary(nodes, edges), gaps)}


def load(source, states=None):
    """Rebuild one source in Postgres. A county is one scope (local/<source>);
    us-congress is the federal scopes (load_us); us/current is the current
    Congress alone, on a skeleton already loaded. One transaction: the
    federal scopes commit together, so a reader never sees a skeleton newer
    than its bills. Returns {scope: (summary, gaps)}."""
    from correspondence.db import _get_pool, init_db
    if states:
        raise ValueError("a delegation (--state) is for `build` only; the graph loads whole scopes")
    init_db()
    with _get_pool().connection() as conn:
        with conn.cursor() as cur:
            _refuse_unscoped(cur)
        if source in ("us-congress", "us/current"):
            with conn.cursor() as cur:
                return load_us(cur, only_current=source == "us/current")
        with conn.cursor() as cur:
            return {f"local/{source}": _load_local(cur, source)}


def _load_local(cur, source):
    nodes, edges, gaps, _ = build_source(source)
    load_scope(cur, f"local/{source}", nodes, edges)
    closed = _close_holds_in_db(sorted({n["id"] for n in nodes if n["kind"] == "person"}), cur)
    return _summary(nodes, edges), gaps + [f"{c['name']}'s hold on {c['post']} closed at {c['valid_to']}: "
                                           f"{c['why']}" for c in closed]


def _refuse_unscoped(cur):
    """Rows from before scopes (Phase 4) would never be deleted by a scope
    reload: the orphan guard only sees its own scope."""
    cur.execute("SELECT EXISTS (SELECT 1 FROM graph_node WHERE scope IS NULL)")
    if cur.fetchone()[0]:
        raise RuntimeError("graph_node has rows without a scope; run `python graph.py load all --fresh` once")


def _close_holds_in_db(person_ids, cur):
    """Apply close_holds_across to every hold of these people, across every
    source already loaded — the county loader cannot see a federal term
    and vice versa, so this runs where both are visible."""
    from psycopg.rows import dict_row
    from psycopg.types.json import Jsonb
    with cur.connection.cursor(row_factory=dict_row) as dcur:
        dcur.execute("""
            SELECT e.id, e.src, e.dst, e.valid_from, e.valid_to, e.props,
                   p.name, o.name AS post
            FROM graph_edge e JOIN graph_node p ON p.id = e.src
                              JOIN graph_node o ON o.id = e.dst
            WHERE e.predicate = 'holds' AND e.src = ANY(%s)""", (person_ids,))
        rows = dcur.fetchall()
        for r in rows:
            r["predicate"] = "holds"
            r["valid_from"] = r["valid_from"].isoformat() if r["valid_from"] else None
            r["valid_to"] = r["valid_to"].isoformat() if r["valid_to"] else None
        changed = close_holds_across(rows)
        for r in changed:
            dcur.execute("UPDATE graph_edge SET valid_to = %s::date, props = %s WHERE id = %s",
                         (r["valid_to"], Jsonb(r["props"]), r["id"]))
    return [{"name": r["name"], "post": r["post"], "valid_to": r["valid_to"],
             "why": r["props"]["inferred_from"]} for r in changed]


def load_all(fresh=False, force=False):
    """Every county in the sidecar, each in its own transaction, then the
    federal scopes in one. fresh: empty both graph tables and reload every
    scope in ONE transaction, so readers keep the old graph until the new
    one is whole (a multi-GB transaction; the WAL archive grows with it).
    Returns {scope: (summary, gaps)}."""
    from correspondence.db import _get_pool, init_db
    init_db()
    out = {}
    with _get_pool().connection() as conn:
        if fresh:
            with conn.transaction(), conn.cursor() as cur:
                cur.execute("DELETE FROM graph_edge")
                cur.execute("DELETE FROM graph_node")
                cur.execute("DELETE FROM graph_scope")
                for source in sorted(SOURCES):
                    out[f"local/{source}"] = _load_local(cur, source)
                for st in sorted(LEGISLATURES):
                    out.update(load_state(cur, st, force=True))
                out.update(load_us(cur, force=True))
            return out
        with conn.cursor() as cur:
            _refuse_unscoped(cur)
        conn.commit()
        for source in sorted(SOURCES):
            with conn.transaction(), conn.cursor() as cur:
                out[f"local/{source}"] = _load_local(cur, source)
        for st in sorted(LEGISLATURES):
            with conn.transaction(), conn.cursor() as cur:
                out.update(load_state(cur, st, force=force))
        with conn.transaction(), conn.cursor() as cur:
            out.update(load_us(cur, force=force))
    return out


def congress_span(n):
    """(first day, first day of the next Congress) of the nth Congress. The
    1st through the 73rd began on 4 March; the 20th Amendment moved the
    start to 3 January from the 74th (1935), so the 73rd ran short. The end
    is the next Congress's first day, the convention the terms already use
    for a handover. Pure."""
    def start(k):
        year = 1789 + 2 * (k - 1)
        return datetime.date(year, 3, 4) if k <= 73 else datetime.date(year, 1, 3)
    return start(n).isoformat(), start(n + 1).isoformat()


_LEGAL_SUFFIXES = {"INC", "INCORPORATED", "LLC", "LLP", "LP", "PLLC", "PLC", "PC", "LTD", "LIMITED",
                   "CORP", "CORPORATION", "CO", "COMPANY"}


def lobby_key(name):
    """The exact key two LDA names must share to be one organization:
    upper case, '&' as AND, dots dropped (L.L.C., U.S.), other punctuation
    as space, a leading THE and trailing legal suffixes removed. Nothing
    fuzzy: 'AMERICAN ASSOCIATION OF RETIRED PERSONS' and '... OF UNIVERSITY
    WOMEN' stay two organizations, and a misspelling stays its own. Pure."""
    t = (name or "").upper().replace("&", " AND ").replace(".", "")
    tokens = re.sub(r"[^A-Z0-9]+", " ", t).split()
    if len(tokens) > 1 and tokens[0] == "THE":
        tokens = tokens[1:]
    # "Merck & Co." loses CO and then the AND that joined it.
    while len(tokens) > 1 and (tokens[-1] in _LEGAL_SUFFIXES or tokens[-1] == "AND"):
        tokens = tokens[:-1]
    return " ".join(tokens)


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
               props->>'bioguide' AS bioguide, props->>'lis' AS lis,
               props->>'openstates_id' AS openstates_id, props->>'jurisdiction' AS jurisdiction
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
                       r"h\.?\s*con\.?\s*res|s\.?\s*con\.?\s*res)\.?\s*(\d+)"
                       r"(?:\s*,?\s*(?:in|of|from)\s+the\s+(\d+)(?:st|nd|rd|th)(?:\s+congress)?)?\s*$", re.I)


# A state bill number. "HR"/"SR" are also federal (H.R.), so without a
# state scope only types no Congress uses are state bills; with one ("va:HR
# 5", from "HR 5 in Virginia") every type that state's files use is.
_STATE_REF = re.compile(r"^\s*(?:([a-z]{2}):)?\s*([a-z][a-z.\s]{0,9}?\s*\d[\w-]*)"
                        r"(?:\s*,?\s*(?:in|of|from)\s+(?:the\s+)?(\d{4})(?:\s+session)?)?\s*$", re.I)
_STATE_ONLY_TYPES = {"hb", "sb", "hjr", "sjr", "ab", "lb", "ld", "hf", "sf"}
_DEFAULT_STATE_TYPES = {"hb", "sb", "hr", "sr", "hjr", "sjr", "hj", "sj"}


def state_bill_types(st):
    """The bill types a state's files use (the session index's), or the
    common ones when it has none on disk. Pure but for the index."""
    return {t for rec in state_session_index(st).values() for t in rec.get("types") or ()} or _DEFAULT_STATE_TYPES


def find_state_bill(st, text):
    """The first bill number in free text, as a match whose group 1 is
    the identifier ("LB 1001A", "H.B. 5"), from the types the state's files
    use; None when there is none. Pure but for the index."""
    types = sorted(state_bill_types(st), key=len, reverse=True)
    alt = "|".join(r"\.?\s*".join(map(re.escape, t)) for t in types)
    return re.search(rf"\b((?:{alt})\.?\s*\d+[a-z]{{0,2}})\b", text or "", re.I)


def state_bill_ref(topic):
    """(state or None, type, number, year or None) for "HB 1", "SB 5 in
    2022", "va:HR 5" or "ne:LB 1001A"; None for anything else, and for "HR
    5" with no state scope (that is H.R. 5). Pure but for the index."""
    m = _STATE_REF.match(topic or "")
    key = bill_key_of(m.group(2)) if m else None
    if not key:
        return None
    st = (m.group(1) or "").lower() or None
    typ, number = key.split("/")
    if typ not in (state_bill_types(st) if st else _STATE_ONLY_TYPES):
        return None
    return st, typ, number, int(m.group(3)) if m.group(3) else None


def split_scope(topic):
    """"va:housing" → ("va", "housing"): a topic scoped to one state's
    legislature by answer() when the question named it. Pure."""
    m = re.match(r"^([a-z]{2}):(.*)$", topic or "")
    return (m.group(1), m.group(2).strip()) if m and m.group(1) in LEGISLATURES else (None, topic)


def _bill_ref(topic):
    """(type, number, congress or None) for "HR 1" or "HR 1 in the 110th"."""
    m = _BILL_REF.match(topic or "")
    return (re.sub(r"[^a-z]", "", m.group(1).lower()), m.group(2),
            int(m.group(3)) if m.group(3) else None) if m else None


def _topic_sql(topic):
    """(clause, args) for the instrument alias `i`: a bill number by id,
    anything else by policy area or title."""
    sref = state_bill_ref(topic)
    if sref:
        # Every session has an HB 1: the newest one on record (in the year
        # asked, if one was), in the scoped state or any loaded one.
        st, typ, number, year = sref
        pattern = f"instrument/{st or '%'}/%/{typ}/{number}"
        in_year = (" AND (n.props->>'first_year')::int <= %s AND (n.props->>'last_year')::int >= %s"
                   if year else "")
        return (" i.id = (SELECT n.id FROM graph_node n WHERE n.kind = 'instrument' AND n.id LIKE %s"
                f" AND n.id NOT LIKE 'instrument/us/%%'{in_year} ORDER BY n.props->>'session_sort' DESC LIMIT 1)",
                [pattern, *([year, year] if year else [])])
    st, topic = split_scope(topic)
    if st:
        return (f" i.props->>'jurisdiction' = %s AND{_TOPIC_SQL}", [state_div(st), f"{topic}%", f"%{topic}%"])
    ref = _bill_ref(topic)
    if ref:
        # Every Congress has an H.R. 1: a bare number is the most recent one
        # on record, never all of them at once.
        # The candidate ids are listed, newest first, so the primary key
        # finds them; a LIKE with a leading wildcard would scan every bill.
        congresses = [ref[2]] if ref[2] else range(current_session()[0] + 1, 0, -1)
        ids = [f"instrument/us/{c}/{ref[0]}/{ref[1]}" for c in congresses]
        return (" i.id = (SELECT n.id FROM graph_node n WHERE n.id = ANY(%s)"
                " ORDER BY array_position(%s, n.id) LIMIT 1)", [ids, ids])
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


def congress_of(on_date):
    """The Congress sitting on a date (ISO), March starts included. Pure."""
    d = str(on_date)[:10]
    c = (int(d[:4]) - 1789) // 2 + 1
    return c - 1 if d < congress_span(c)[0] else c


def districts_at(lon, lat, as_of):
    """Who represented a point in the House on a date: the district shape
    of that date's Congress that covers the point, the seat that represents
    it in that Congress, and the seat's holder that day. No shape is said
    as such; two shapes (overlapping plans on disk) return a question, never
    a pick. Needs PostGIS; raises if the database is not there, and the
    caller fails open."""
    from psycopg.rows import dict_row
    from correspondence.db import _get_pool
    congress = congress_of(as_of)
    with _get_pool().connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("""
                SELECT n.id, n.name, n.props FROM graph_geometry gg JOIN graph_node n ON n.id = gg.node_id
                WHERE ST_Covers(gg.geom, ST_SetSRID(ST_Point(%s, %s), 4326))
                  AND (n.props->>'congress_from')::int <= %s AND (n.props->>'congress_to')::int >= %s""",
                        (lon, lat, congress, congress))
            shapes = cur.fetchall()
            if len(shapes) == 1:
                cur.execute("""SELECT src FROM graph_edge WHERE predicate = 'represents' AND dst = %s
                               AND (props->>'congress')::int = %s""", (shapes[0]["id"], congress))
                post = cur.fetchone()
    out = {"congress": congress, "as_of": str(as_of)[:10], "method": "district shapes (Lewis et al., UCLA)"}
    if not shapes:
        return out | {"error": f"no district shape on disk covers this point for the {_ordinal(congress)} Congress"}
    if len(shapes) > 1:
        return out | {"ambiguous": True, "candidates": [s["name"] for s in shapes],
                      "question": "this point falls inside more than one district shape on disk; which one?"}
    shape = shapes[0]
    out |= {"district": shape["name"], "shape_id": shape["id"]}
    if post is None:
        return out | {"holders": [], "empty_reason": "no House seat on record represents this shape"}
    holders = seat_holder(post["src"], str(as_of)[:10])
    return out | {"post_id": post["src"],
                  "holders": [{"name": h["name"], "valid_from": str(h["valid_from"]) if h["valid_from"] else None,
                               "valid_to": str(h["valid_to"]) if h["valid_to"] else None,
                               "certification": h["certification"]} for h in holders]}


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
_ASK_LOBBIED_ON = re.compile(
    r"^\s*(?:who|which\s+(?:organizations?|companies|groups|interests|lobbyists))\s+"
    r"(?:lobbied|lobbies|lobby)\s+(?:on|about|over)\s+(?:the\s+)?(?P<topic>.+?)\s*\??\s*$", re.I)
_ASK_ORG_LOBBIED = re.compile(
    r"^\s*(?:what|which\s+bills?)\s+(?:did|does|has|have)\s+(?P<org>.+?)\s+lobb(?:y|ied)"
    r"(?:\s+(?:on|for|about))?\s*\??\s*$", re.I)
_ASK_NOMINATED = re.compile(
    r"^\s*(?:who|whom)\s+(?:did|has)\s+(?:president\s+)?(?P<person>.+?)\s+nominated?"
    r"(?:\s+(?:for|to\s+be|as)\s+(?P<topic>.+?))?\s*\??\s*$", re.I)
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
    m = _ASK_LOBBIED_ON.match(q)
    if m:
        return {"ask": "lobbied_on", "topic": m.group("topic").strip()}
    m = _ASK_ORG_LOBBIED.match(q)
    if m:
        return {"ask": "org_lobbied", "org": m.group("org").strip()}
    m = _ASK_NOMINATED.match(q)
    if m:
        return {"ask": "nominated", "person": m.group("person").strip(),
                "topic": (m.group("topic") or "").strip() or None}
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
    chamber = "lower" if re.search(r"\b(delegates?|house of delegates|state house|assembly(man|woman|member)?)\b", q) \
        else "upper" if re.search(r"\bstate senat(e|or)s?\b", q) else None
    if chamber:
        kind = "sldl" if chamber == "lower" else "sldu"
        st = next((code for code in sorted(LEGISLATURES)
                   if re.search(rf"\b{re.escape(DIVISION_NAMES.get(code, code).lower())}\b|\b{code}\b", q)), None)
        m = re.search(r"\b(\d{1,3})(?:st|nd|rd|th)?\b", q)
        return ([f"{st}/{chamber}/"] if st else [f"/{chamber}/"]) + ([f"{kind}:{m.group(1)}"] if m else [])
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

    newest_of = {}

    def newest(ref):
        """The SQL's rule: the most recent Congress with this bill number."""
        if ref not in newest_of:
            hits = [n for n in nodes if n["kind"] == "instrument" and n["id"].startswith("instrument/us/")
                    and n["id"].endswith(f"/{ref[0]}/{ref[1]}")
                    and (ref[2] is None or n["id"].startswith(f"instrument/us/{ref[2]}/"))]
            newest_of[ref] = max(hits, key=lambda n: int(n["props"].get("congress") or 0))["id"] if hits else None
        return newest_of[ref]

    def topic_ok(i, topic):
        sref = state_bill_ref(topic)
        if sref:
            st, typ, number, year = sref
            hits = [n for n in nodes if n["kind"] == "instrument" and not n["id"].startswith("instrument/us/")
                    and re.fullmatch(rf"instrument/{st or '[a-z]{2}'}/[^/]+/{typ}/{number}", n["id"])
                    and (not year or (n["props"].get("first_year") or 0) <= year <= (n["props"].get("last_year") or 0))]
            return bool(hits) and i["id"] == max(hits, key=lambda n: n["props"].get("session_sort") or "")["id"]
        st, topic = split_scope(topic)
        if st and i["props"].get("jurisdiction") != state_div(st):
            return False
        ref = _bill_ref(topic)
        if ref:
            return i["id"] == newest(ref)
        t = topic.lower()
        return (i["props"].get("topic") or "").lower().startswith(t) or t in i["name"].lower()

    def committees(query):
        return committee_matches([n for n in nodes if n["kind"] == "organization"
                                  and n["props"].get("natural_key", "").startswith("us/committee/")], query)

    def items(topic, limit):
        return sorted(({"id": n["id"], "name": n["name"], "props": n["props"]} for n in nodes
                       if n["kind"] == "instrument" and topic_ok(n, topic)
                       and n["props"].get("instrument_type") not in _NOT_BILLS),
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
        # Federal and state bills: a county motion has no executive and no law.
        items = sorted((n for n in nodes if n["kind"] == "instrument" and topic_ok(n, topic)
                        and re.match(r"instrument/[a-z]{2}/", n["id"])
                        and n["props"].get("instrument_type") not in _NOT_BILLS),
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
                        "lis": n["props"].get("lis"), "openstates_id": n["props"].get("openstates_id"),
                        "jurisdiction": n["props"].get("jurisdiction")}
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

    def orgs(query):
        key = lobby_key(query)
        hits = [n for n in nodes if n["kind"] == "organization" and key
                and (n["props"].get("lobby_key") or "").find(key) >= 0]
        return sorted(({"id": n["id"], "name": n["name"], "props": n["props"]} for n in hits),
                      key=lambda o: (o["props"]["lobby_key"] != key, o["name"]))[:_MAX_CANDIDATES + 1]

    return {"persons": persons, "votes": votes_of, "posts": posts, "careers": careers,
            "holds": holds_of, "voters": voters, "laws": laws, "committees": committees,
            "items": items, "edges": edges_of, "orgs": orgs, "loaded": lambda: bool(nodes)}


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
                            ORDER BY i.id LIMIT %s""", [list(_NOT_BILLS)] + targs + [limit])
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
                    WHERE i.kind = 'instrument' AND i.id ~ '^instrument/[a-z]{{2}}/'
                      AND COALESCE(i.props->>'instrument_type', '') <> ALL(%s) AND {clause}
                    ORDER BY i.id LIMIT %s)
                SELECT it.id AS item_id, it.name AS title, it.actions_fetched,
                       e.predicate, e.valid_from AS date, e.certification, o.name AS other
                FROM items it
                LEFT JOIN graph_edge e
                  ON (e.src = it.id AND e.predicate = 'enacted_as')
                  OR (e.dst = it.id AND e.predicate IN ('signed', 'vetoed'))
                LEFT JOIN graph_node o ON o.id = CASE WHEN e.src = it.id THEN e.dst ELSE e.src END
                ORDER BY it.id""", [list(_NOT_BILLS)] + targs + [limit + 1])
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

    def orgs(query):
        key = lobby_key(query)
        if not key:
            return []

        def q(cur):
            cur.execute("""SELECT id, name, props FROM graph_node
                           WHERE kind = 'organization' AND props->>'lobby_key' LIKE %s
                           ORDER BY props->>'lobby_key' <> %s, name LIMIT %s""",
                        (f"%{key}%", key, _MAX_CANDIDATES + 1))
            return cur.fetchall()
        return run(q)

    def loaded():
        return run(lambda cur: (cur.execute("SELECT EXISTS (SELECT 1 FROM graph_node) AS any"),
                                cur.fetchone()["any"])[1])

    return {"persons": persons, "votes": votes_of, "posts": posts, "careers": careers,
            "holds": holds_of, "voters": voters, "laws": laws, "committees": committees,
            "items": items, "edges": edges_of, "loaded": loaded, "orgs": orgs}


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
    # "HB 1 in Virginia" leaves "HB 1 in": the preposition went with the place.
    while dropped and kept and kept[-1].lower() in ("in", "of", "for", "from", "the"):
        kept.pop()
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
_LAW_TYPES = ("pl", "pvtl", "chapter")
# Instruments that are not bills: laws, and nominations (a nomination has no
# law to become, so "law about X" must never list one as "no law").
_NOT_BILLS = _LAW_TYPES + ("pn",)


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


# An organization is one node per exact normalized LDA name: a grouping by
# name, which the answer reports as an advisory hop of its own.
_IDENTITY_HOP = {"predicate": "identity", "weakest": "advisory",
                 "note": "organizations are grouped by exact normalized LDA name, not by a registry"}


def _as_org_row(r):
    """A walk row whose source is an organization, not a person."""
    r = dict(r)
    r["organization_id"], r["organization"] = r.pop("person_id", None), r.pop("person", None)
    return r


_STATE_SCOPED_ASKS = {"voters", "sponsors", "law", "related", "referrals", "reported", "signed_by"}


def _state_scope(place):
    """The loaded legislature a dropped place word names ("Virginia", "VA"),
    or None. Pure."""
    words = {w.lower().strip(",'s") for w in (place or "").split()}
    return next((code for code in sorted(LEGISLATURES)
                 if code in words or DIVISION_NAMES.get(code, "").lower() in words), None)


def answer(parsed, backend, limit=200):
    """Run one parsed question against a backend. Every branch returns a
    dict with `ask`, `hops` / `weak_hops`, and an `empty_reason` when there
    is nothing — never a silent zero."""
    ask = parsed["ask"]
    if not backend["loaded"]():
        return {"ask": ask, "rows": [], "hops": [], "weak_hops": [],
                "empty_reason": "graph not loaded: run `python graph.py load fairfax-bos`"}
    topic, place = strip_place(parsed["topic"]) if parsed.get("topic") else (None, None)
    # A state whose legislature is loaded is a scope, not noise: "who voted
    # no on HB 5 in Virginia" asks about Virginia's bills. It scopes the
    # bill asks, and a person's votes only when the person sits there (a
    # senator asked about "housing in Virginia" keeps their own votes).
    scope = _state_scope(place)
    if scope and topic and ask in _STATE_SCOPED_ASKS:
        topic, place = f"{scope}:{topic}", None
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
    if ask == "lobbied_on":
        rows, truncated = backend["voters"](topic, None, limit, "lobbied_on")
        rows = [_as_org_row(r) for r in rows]
        out = shape_answer(rows, [{"name": "anyone"}], topic, topic, None, truncated, predicate="lobbied_on")
        out |= {"ask": ask, "organizations": sorted({r["organization"] for r in rows}), "persons": [],
                "place_ignored": place}
        if rows:
            out["hops"].append(_IDENTITY_HOP)
            out["weak_hops"] = [h for h in out["hops"] if h["weakest"] != "certified"]
        else:
            out["empty_reason"] = (f"no lobbying report on disk names anything matching {topic!r} "
                                   f"(filings from 2023 on; bill numbers are read from the filings' text)")
        return out
    if ask == "org_lobbied":
        orgs = backend["orgs"](parsed["org"])
        base = {"ask": ask, "query": parsed["org"], "persons": [], "hops": [], "weak_hops": [], "rows": []}
        if not orgs:
            return base | {"empty_reason": f"no lobbying organization on disk matches {parsed['org']!r}"}
        key = lobby_key(parsed["org"])
        exact = [o for o in orgs if o["props"].get("lobby_key") == key]
        if len(exact) != 1 and len(orgs) > 1:
            return base | {"ambiguous": True, "candidate_count": len(orgs),
                           "candidates": [{"id": o["id"], "name": o["name"],
                                           "lda_ids": len(o["props"].get("lda_client_ids") or [])
                                           + len(o["props"].get("lda_registrant_ids") or [])}
                                          for o in orgs[:_MAX_CANDIDATES]],
                           "empty_reason": f"{len(orgs)} organizations match {parsed['org']!r}; name one of them"}
        org = (exact or orgs)[0]
        rows, total, truncated = backend["votes"]([org["id"]], None, limit, "lobbied_on")
        rows = [_as_org_row(r) for r in rows]
        out = shape_answer(rows, [org], parsed["org"], None, total, truncated, predicate="lobbied_on")
        out |= {"ask": ask, "organization": {"id": org["id"], "name": org["name"],
                                             "name_variants": org["props"].get("name_variants")},
                "persons": []}
        if rows:
            out["hops"].append(_IDENTITY_HOP)
            out["weak_hops"] = [h for h in out["hops"] if h["weakest"] != "certified"]
        else:
            out["empty_reason"] = f"no lobbying report on disk has {org['name']} naming a bill"
        return out
    if ask == "nominated":
        persons = backend["persons"](parsed["person"])
        if not persons:
            return shape_answer([], [], parsed["person"], None) | {"ask": ask}
        which, persons = _one_person(persons, parsed["person"], topic, ask, backend)
        if which:
            return which
        rows, total, truncated = backend["votes"]([p["id"] for p in persons], topic, limit, "nominated")
        out = shape_answer(rows, persons, parsed["person"], topic, total, truncated, predicate="nominated")
        if not rows:
            out["empty_reason"] = (f"no nomination on disk by {', '.join(p['name'] for p in persons)}"
                                   + (f" matching {topic!r}" if topic else "")
                                   + " (Congress.gov's records start in 1981)")
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
        state_people = [p for p in persons if p.get("openstates_id")]
        legislature = lambda ps: next((code for code in LEGISLATURES for p in ps  # noqa: E731
                                       if p.get("jurisdiction") == state_div(code)), None)
        # A state legislator's own legislature, from their node, when the
        # question named none: "HR 2179" for a delegate is the House of
        # Delegates' resolution, not H.R. 2179. A person who also sat in
        # Congress (bridged by the sidecar's identities) has no single home:
        # HR is Congress's, and only a state-only type (HB, SJ) or a named
        # state sends the ask to the legislature.
        bridged = any(p.get("bioguide") for p in state_people)
        home = scope or legislature([p for p in state_people if not p.get("bioguide")])
        if not home and bridged and topic and state_bill_ref(topic) and not _bill_ref(topic):
            home = legislature(state_people)
        if home and topic and state_people:
            topic, place = f"{home}:{topic}", None
        if year and state_people:
            st = home or legislature(state_people)
            files_rows = state_snapshot_votes(st, state_people, year, topic, limit) if st else None
            # A bridged person's year may be a year in Congress: an empty
            # state file falls through to the federal roll calls.
            if files_rows is not None and (files_rows[0] or not bridged):
                rows, total, truncated, files = files_rows
                out = shape_answer(rows, persons, parsed["person"], topic, total, truncated) | {
                    "ask": ask, "place_ignored": place, "year": year, "from_snapshot": files}
                if not rows:
                    out["empty_reason"] = (f"no recorded vote by {', '.join(p['name'] for p in state_people)} "
                                           f"in {st.upper()}'s {year} roll calls on disk"
                                           + (f" on {topic!r}" if topic else ""))
                return out
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
    sources = sorted(SOURCES) + ["us-congress", "us/current", "all"]
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, help_ in (("build", "build in memory and print the summary; no database"),
                        ("load", "rebuild one source's slice in Postgres")):
        s = sub.add_parser(name, help=help_)
        s.add_argument("source", choices=sources)
        s.add_argument("--state", action="append",
                       help="build us-congress only: delegation(s), e.g. --state VA")
        s.add_argument("--fresh", action="store_true",
                       help="load all: empty the graph and reload every scope in one transaction")
        s.add_argument("--force", action="store_true",
                       help="load all: reload older Congresses even if their inputs did not change")
    sub.add_parser("fetch", help="download the public legislators, executive and committee files to data/")
    sub.add_parser("certify-index", help="reduce older roll-call snapshots to member-congress.json")
    sub.add_parser("migrate-layout", help="move a flat data dir into the DATASETS layout (idempotent)")
    sub.add_parser("manifest", help="write datasets.json: sources, licences, coverage, sizes")
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
    if a.cmd == "load":
        t0 = time.time()
        result = load_all(fresh=a.fresh, force=a.force) if a.source == "all" else load(a.source, a.state)
        for scope, (summary, gaps) in result.items():
            print(scope, summary["nodes"], "nodes", summary["edges"], "edges", json.dumps(summary["by_predicate"]))
            for g in gaps:
                print("  -", g)
        print(f"loaded {len(result)} scope(s) in {time.time() - t0:.0f} s")
    elif a.cmd == "build":
        nodes, edges, gaps, _ = build_source("us-congress" if a.source == "us/current" else a.source, a.state)
        print(json.dumps(_summary(nodes, edges), indent=1))
        print(f"{len(gaps)} gap(s):")
        for g in gaps:
            print("  -", g)
    elif a.cmd == "migrate-layout":
        moves = migrate_layout()
        for src, dest in moves:
            print(f"{src} → {dest}")
        print(f"{len(moves)} change(s)")
    elif a.cmd == "manifest":
        for kind, d in write_manifest()["datasets"].items():
            print(f"{kind:14} {d['files']:>6} file(s) {d['bytes'] / 2**20:>9.1f} MB  {d['coverage']}")
    elif a.cmd == "certify-index":
        members, files = write_member_congress(current_session()[0])
        print(f"{members:,} member id(s) from {len(files)} file(s) → {data_path('certification')}")
    elif a.cmd == "fetch":
        for name, n in fetch_public().items():
            print(f"{name}: {n:,} bytes")
    elif a.cmd == "snapshot":
        congress, session, year = (a.congress, a.session, a.year) if a.congress else current_session()
        out = data_path("votes", congress=congress, session=session)
        out.parent.mkdir(parents=True, exist_ok=True)
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
