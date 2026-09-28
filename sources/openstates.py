"""Open States: state legislatures, read from files.

Open States (Plural) scrapes every state legislature into one schema. Two of
its products are free without an account, and this module reads both:

- the people repo (github.com/openstates/people, CC0): one YAML file per
  legislator and statewide officer, current and retired, with their roles
  (chamber, district, dates) and links. For Virginia the links carry the
  member id the legislature's own files use (sources/lis.py).
- the monthly Postgres dump (data.openstates.org, ~11 GB, every state):
  bills, actions, sponsorships, text versions and roll calls. It is data
  only, no table definitions, so `restore` creates each table from its COPY
  column list, every column text, into a scratch database; `extract` writes
  one state's sessions into our own files; the scratch database is dropped.

Open States is the record for every state (plan of 2026-09-27). What it
says is `ingested`: it scrapes the legislature, so nothing it says is
affirmed by a second publisher. Virginia's own files certify terms and flag
disagreements (sources/lis.py, graph.load_state).

Run on the server, not in a request:
    python -m sources.openstates people va       # pull the people repo, write people.json
    python -m sources.openstates dump            # this month's dump, if new (monthly unit)
    python -m sources.openstates restore         # as postgres: scratch database from the dump
    python -m sources.openstates extract va      # scratch database -> bills-/votes-<session>.json
    python -m sources.openstates drop            # as postgres: drop the scratch database
    python -m sources.openstates text va 2026    # text of one session's versions (all, newest first, if none named)
    python -m sources.openstates text --latest   # every text state's last two years of sessions (daily sync)
"""

import datetime
import gzip
import hashlib
import json
import os
import pathlib
import re
import subprocess
import sys
import time

_HERE = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_HERE))
import graph  # noqa: E402  — DATA_DIR, DATASETS, LEGISLATURES

DUMP_URL = "https://data.openstates.org/postgres/monthly/{month}-public.pgdump"
PEOPLE_REPO = "https://github.com/openstates/people"
SCRATCH_DB = os.environ.get("OPENSTATES_SCRATCH_DB") or "openstates_scratch"
# The role that extracts: the app's own, which may read the scratch database
# but may not create one (restore and drop run as postgres).
READER_ROLE = os.environ.get("OPENSTATES_READER_ROLE") or "nospopuli"
# The tables the state layer reads; the dump's events, people and search
# tables are left out.
TABLES = ("jurisdiction", "legislativesession", "organization", "bill", "billabstract", "billaction",
          "billidentifier", "billsource", "billsponsorship", "billtitle", "billversion",
          "billversionlink", "voteevent", "votecount", "personvote", "votesource", "relatedbill")
_UA = "NosPopuli bulk sync (nospopuli.org)"


def _raw(*parts):
    p = graph.DATA_DIR.joinpath("raw", *parts)
    p.mkdir(parents=True, exist_ok=True)
    return p


def _write_if_changed(path, obj):
    """Write JSON only when its content moved, so a file's mtime (which the
    graph fingerprints) changes only with its data. Returns True if written."""
    body = json.dumps(obj, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    if path.exists() and hashlib.sha256(path.read_bytes()).hexdigest() == \
            hashlib.sha256(body.encode()).hexdigest():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".part")
    tmp.write_text(body)
    os.replace(tmp, path)
    return True


# ------------------------------------------------------------------ people

# Member ids the legislature's own files use, read from a person's links. A
# pattern per state: Virginia's LIS has published three URL shapes over the
# years, and the oldest drops the zero padding (H124 is VOTE.CSV's H0124).
_STATE_IDS = {
    "va": {"lis": (re.compile(r"\+mbr\+([HS])(\d+)"),
                   re.compile(r"vga\.virginia\.gov/members/([HS])(\d+)"),
                   re.compile(r"memberpage\.php\?id=([HS])(\d+)"))},
}


def sync_people(states):
    """A sparse clone of the people repo holding only data/<st>/ for each
    state, pulled when it exists. Returns the clone's path."""
    root = _raw("openstates-people")
    if not (root / ".git").exists():
        subprocess.run(["git", "clone", "-q", "--depth", "1", "--filter=blob:none", "--sparse",
                        PEOPLE_REPO, str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "sparse-checkout", "set", *[f"data/{st}" for st in states]],
                   check=True)
    subprocess.run(["git", "-C", str(root), "pull", "-q", "--depth", "1"], check=True)
    return root


def parse_person(doc, st):
    """One people-repo YAML document → the person as the graph reads it.
    Roles keep the repo's own words (upper, lower, governor, lt_governor…)
    and dates; an open role has no end. Pure."""
    links = [x.get("url", "") for x in (doc.get("links") or []) + (doc.get("sources") or [])]
    ids = {}
    for kind, patterns in _STATE_IDS.get(st, {}).items():
        for url in links:
            for p in patterns:
                m = p.search(url)
                if m:
                    ids[kind] = f"{m.group(1)}{int(m.group(2)):04d}"
                    break
            if kind in ids:
                break
    parties = [p.get("name") for p in doc.get("party") or [] if p.get("name")]
    return {
        "id": doc["id"], "name": doc.get("name"), "given_name": doc.get("given_name"),
        "family_name": doc.get("family_name"),
        "other_names": sorted({n.get("name") for n in doc.get("other_names") or [] if n.get("name")}),
        "party": parties[-1] if parties else None,
        "roles": [{"type": r.get("type"), "district": str(r["district"]) if r.get("district") is not None else None,
                   "start": str(r["start_date"]) if r.get("start_date") else None,
                   "end": str(r["end_date"]) if r.get("end_date") else None}
                  for r in doc.get("roles") or []],
        "ids": ids, "image": doc.get("image"),
    }


def asserted_governors(st):
    """Governors the people repo lacks, asserted in the sidecar (graph.
    LEGISLATURES[st]["governors"]), as person records. Pure."""
    out = []
    for g in (graph.LEGISLATURES.get(st) or {}).get("governors") or []:
        slug = re.sub(r"[^a-z]+", "-", g["name"].lower()).strip("-")
        out.append({"id": f"asserted/{st}/{slug}", "name": g["name"], "given_name": None, "family_name": None,
                    "other_names": [], "party": None, "ids": {}, "image": None,
                    "roles": [{"type": "governor", "district": None, "start": g["start"], "end": g["end"]}],
                    "asserted_by": g.get("asserted_by")})
    return out


def people(st, sync=True):
    """Every legislator and statewide officer of one state, current and
    retired, written to derived/states/<st>/people.json. Returns its meta."""
    import yaml
    # Every sidecar state stays checked out: pulling one must not drop the
    # others' folders from the shared clone.
    root = sync_people(sorted({st, *graph.LEGISLATURES})) if sync else _raw("openstates-people")
    base = root / "data" / st
    records, files = [], 0
    for folder in ("legislature", "retired", "executive"):
        for f in sorted((base / folder).glob("*.yml")):
            # The repo keeps sitting legislators apart from retired ones; a
            # multi-member district's seats are counted from the sitting.
            records.append({**parse_person(yaml.safe_load(f.read_text()), st), "retired": folder == "retired"})
            files += 1
    records.extend(asserted_governors(st))
    records.sort(key=lambda p: p["id"])
    rev = subprocess.run(["git", "-C", str(root), "rev-parse", "--short", "HEAD"],
                         capture_output=True, text=True).stdout.strip()
    meta = {"state": st, "source": f"{PEOPLE_REPO} @ {rev}", "files": files, "people": len(records),
            "with_ids": sum(1 for p in records if p["ids"]),
            "fetched": datetime.date.today().isoformat()}
    _write_if_changed(graph.data_path("state_people", state=st), {"meta": meta, "people": records})
    return meta


# -------------------------------------------------------------------- dump

def dump_months(today):
    """The dump names to try, newest first: this month's, then last month's
    (a month's dump appears on the 1st and may not be there yet). Pure."""
    first = today.replace(day=1)
    last = first - datetime.timedelta(days=1)
    return [f"{first:%Y-%m}", f"{last:%Y-%m}"]


def sync_dump(today=None):
    """Download the newest monthly dump if it is not on disk, resuming a
    partial download. Only one dump is kept. Fail-closed: a dump whose size
    does not match the server's is not recorded. Returns the manifest."""
    import requests
    s = requests.Session()
    s.headers["User-Agent"] = _UA
    raw = _raw("openstates")
    manifest_path = raw / "manifest.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    for month in dump_months(today or datetime.date.today()):
        url = DUMP_URL.format(month=month)
        head = s.head(url, timeout=60, allow_redirects=True)
        if head.status_code != 200:
            continue
        size = int(head.headers.get("Content-Length") or 0)
        dest = raw / f"{month}-public.pgdump"
        if manifest.get("month") == month and dest.exists() and dest.stat().st_size == size:
            return {**manifest, "unchanged": True}
        if not (dest.exists() and dest.stat().st_size == size):
            # curl resumes a partial file (-C -) and streams to disk; a
            # complete one (downloaded by hand) is only recorded.
            subprocess.run(["curl", "-sS", "-L", "-C", "-", "-A", _UA, "-o", str(dest), url], check=True)
        if dest.stat().st_size != size:
            raise RuntimeError(f"{dest.name}: {dest.stat().st_size} bytes on disk, the server says {size}")
        digest = hashlib.sha256()
        with open(dest, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 24), b""):
                digest.update(chunk)
        manifest = {"month": month, "url": url, "size": size, "etag": head.headers.get("ETag"),
                    "last_modified": head.headers.get("Last-Modified"), "sha256": digest.hexdigest(),
                    "fetched": datetime.date.today().isoformat()}
        manifest_path.write_text(json.dumps(manifest, indent=1))
        for old in raw.glob("*-public.pgdump"):
            if old != dest:
                old.unlink()
        return manifest
    raise RuntimeError("no Open States dump for this month or the last")


def copy_columns(copy_line):
    """The column list of a pg_restore COPY line. Pure."""
    m = re.match(r"^COPY \S+ \((.*)\) FROM stdin;", copy_line)
    if not m:
        raise ValueError(f"not a COPY line: {copy_line[:80]!r}")
    return [c.strip() for c in m.group(1).split(",")]


def restore(dump=None):
    """As postgres: a fresh scratch database holding TABLES from the dump.
    Each table is created from its COPY column list with every column text
    (the dump carries no DDL), then filled in parallel; the reader role may
    select. Measured 2026-09-27: 19 tables in 89 s, 16 GB."""
    dump = dump or sorted(_raw("openstates").glob("*-public.pgdump"))[-1]
    subprocess.run(["dropdb", "--if-exists", SCRATCH_DB], check=True)
    subprocess.run(["createdb", SCRATCH_DB], check=True)
    ddl = []
    for t in TABLES:
        name = f"opencivicdata_{t}"
        proc = subprocess.Popen(["pg_restore", "-f", "-", "-t", name, str(dump)], stdout=subprocess.PIPE, text=True)
        line = next((ln for ln in proc.stdout if ln.startswith("COPY ")), None)
        proc.kill()
        proc.wait()
        if line is None:
            raise RuntimeError(f"{name}: no COPY line in {dump.name}")
        ddl.append(f"CREATE TABLE {name} ({', '.join(f'{c} text' for c in copy_columns(line))});")
    subprocess.run(["psql", "-q", "-v", "ON_ERROR_STOP=1", "-d", SCRATCH_DB, "-c", "\n".join(ddl)], check=True)
    subprocess.run(["pg_restore", "--no-owner", "--no-privileges", "--data-only", "-j", "4", "-d", SCRATCH_DB,
                    *[a for t in TABLES for a in ("-t", f"opencivicdata_{t}")], str(dump)], check=True)
    # Joins by id per session; without these every lookup is a scan of the
    # whole country's rows.
    idx = ["opencivicdata_bill(legislative_session_id)", "opencivicdata_billaction(bill_id)",
           "opencivicdata_billsponsorship(bill_id)", "opencivicdata_billversion(bill_id)",
           "opencivicdata_billversionlink(version_id)", "opencivicdata_billabstract(bill_id)",
           "opencivicdata_billtitle(bill_id)", "opencivicdata_billidentifier(bill_id)",
           "opencivicdata_billsource(bill_id)", "opencivicdata_relatedbill(bill_id)",
           "opencivicdata_voteevent(legislative_session_id)", "opencivicdata_votecount(vote_event_id)",
           "opencivicdata_personvote(vote_event_id)", "opencivicdata_votesource(vote_event_id)"]
    subprocess.run(["psql", "-q", "-v", "ON_ERROR_STOP=1", "-d", SCRATCH_DB, "-c",
                    "\n".join(f"CREATE INDEX ON {i};" for i in idx) +
                    f"\nGRANT SELECT ON ALL TABLES IN SCHEMA public TO {READER_ROLE};"], check=True)
    return {"database": SCRATCH_DB, "dump": dump.name, "tables": len(TABLES)}


def drop_scratch():
    subprocess.run(["dropdb", "--if-exists", SCRATCH_DB], check=True)


# ----------------------------------------------------------------- extract

def pg_array(text):
    """A Postgres array literal restored as text ('{filing,"a, b"}') → list.
    Pure."""
    if not text or text in ("{}", "NULL"):
        return []
    body, out, cur, quoted, esc = text[1:-1], [], "", False, False
    for ch in body:
        if esc:
            cur += ch
            esc = False
        elif ch == "\\":
            esc = True
        elif ch == '"':
            quoted = not quoted
        elif ch == "," and not quoted:
            out.append(cur)
            cur = ""
        else:
            cur += ch
    out.append(cur)
    return [x for x in out if x != ""]


def bill_key(identifier):
    """'HB 1' → 'hb/1'; the one rule is graph.bill_key_of. Pure."""
    return graph.bill_key_of(identifier)


def bill_record(bill, chambers, actions, sponsors, versions, abstracts, titles, identifiers, sources, related):
    """One bill's rows → our record. `chambers` maps an organization id to
    upper, lower or legislature; actions keep their order. Pure."""
    extras = json.loads(bill["extras"] or "{}")
    return {
        "identifier": bill["identifier"], "openstates_id": bill["id"], "extras": extras,
        "title": bill["title"], "classification": pg_array(bill["classification"]),
        "subjects": pg_array(bill["subject"]),
        "chamber": chambers.get(bill["from_organization_id"]),
        "first_action_date": bill["first_action_date"], "latest_action_date": bill["latest_action_date"],
        "latest_action": bill["latest_action_description"], "latest_passage_date": bill["latest_passage_date"],
        # Every list in a fixed order: the dump's row order is not stable
        # between restores, and a reordered file would reload its scope.
        "abstracts": sorted(({"abstract": a["abstract"], "note": a["note"] or None} for a in abstracts),
                            key=lambda a: (a["note"] or "", a["abstract"] or "")),
        "other_titles": sorted(t["title"] for t in titles),
        "other_identifiers": sorted(i["identifier"] for i in identifiers),
        "sources": sorted(s["url"] for s in sources),
        "sponsors": sorted(({"name": s["name"], "person": s["person_id"] or None, "primary": s["primary"] == "t",
                             "classification": s["classification"]} for s in sponsors),
                           key=lambda s: (not s["primary"], s["name"] or "", s["person"] or "")),
        "actions": [{"date": a["date"], "description": a["description"], "chamber": chambers.get(a["organization_id"]),
                     "classification": pg_array(a["classification"]), "order": int(a["order"] or 0)}
                    for a in sorted(actions, key=lambda a: (int(a["order"] or 0), a["date"] or "", a["id"]))],
        "versions": sorted(({"name": v["note"], "date": v["date"] or None,
                             "links": sorted(({"media_type": ln["media_type"], "url": ln["url"]} for ln in v["links"]),
                                             key=lambda ln: (ln["media_type"] or "", ln["url"] or ""))}
                            for v in versions),
                           key=lambda v: (v["date"] or "", v["links"][0]["url"] if v["links"] else "", v["name"] or "")),
        "related": sorted(({"identifier": r["identifier"], "session": r["legislative_session"],
                            "relation": r["relation_type"]} for r in related),
                          key=lambda r: (r["session"] or "", r["identifier"] or "", r["relation"] or "")),
    }


def vote_record(vote, chambers, bill_keys, counts, positions, sources):
    """One roll call's rows → our record, the bill as its key. Positions
    keep Open States' option words and the voter's person id when it has
    one; a name-only voter is kept by name for the loader to resolve.
    Pure."""
    return {
        "id": vote["id"], "bill": bill_keys.get(vote["bill_id"]), "date": (vote["start_date"] or "")[:10],
        "motion": vote["motion_text"], "motion_classification": pg_array(vote["motion_classification"]),
        "result": vote["result"], "chamber": chambers.get(vote["organization_id"]),
        "dedupe_key": vote["dedupe_key"] or None,
        "counts": {c["option"]: int(c["value"]) for c in counts},
        "positions": sorted(([p["voter_id"] or None, p["voter_name"], p["option"]] for p in positions),
                            key=lambda p: (p[1] or "", p[0] or "")),
        "sources": sorted(s["url"] for s in sources),
    }


def _year(date):
    return int(date[:4]) if date and date[:4].isdigit() else None


def session_index(sessions, first_actions, votes=None, types=None):
    """The legislature's sessions → {id: {years, key, name, classification,
    start, end, bills, votes, types}}. Pure.

    Open States ids come in ~60 shapes and many carry no year ("88",
    "103rd"), and its session dates are sometimes wrong (Texas's 87th
    starts "2019"). The first year is the id's own leading year when it has
    one ("20232024" is 2023, though it convenes 2022-12-05), else the year
    the median bill was filed, else the start date's. The last year is the
    id's second year ("2023-2024", "2023_24"), else the year by which nine
    bills in ten were filed (North Carolina's "2025" sits in 2026 too).
    The key is (first year, n): n 0 for the regular session (the primary or
    regular one, else the one with the most bills), specials 1, 2... by start
    date. `first_actions`: {id: [first action dates]}."""
    out, groups = {}, {}
    for s in sessions:
        sid, dates = s["identifier"], sorted(d for d in first_actions.get(s["identifier"], []) if d)
        m = re.match(r"(?:\D*?)((?:19|20)\d{2})(?:[-_]?((?:19|20)\d{2})|_(\d{2}))?", sid)
        first = int(m.group(1)) if m else _year(dates[len(dates) // 2]) if dates else _year(s["start_date"])
        if first is None:
            continue
        last = int(m.group(2)) if m and m.group(2) else 2000 + int(m.group(3)) if m and m.group(3) else first
        if dates:
            last = max(last, _year(dates[min(len(dates) - 1, len(dates) * 9 // 10)]) or last)
        out[sid] = {"years": [first, last], "name": s.get("name"), "classification": s.get("classification") or None,
                    "start": s.get("start_date") or None, "end": s.get("end_date") or None,
                    "bills": len(first_actions.get(sid, [])), "votes": (votes or {}).get(sid, 0), "types": sorted((types or {}).get(sid, ()))}
        groups.setdefault(first, []).append(sid)
    for first, sids in groups.items():
        regular = max(sids, key=lambda x: (out[x]["classification"] in ("primary", "regular"),
                                           out[x]["classification"] != "special", out[x]["bills"], x))
        rest = sorted((x for x in sids if x != regular), key=lambda x: (out[x]["start"] or "", x))
        for n, sid in enumerate([regular, *rest]):
            out[sid]["key"] = [first, n]
    return out


def _group(rows, key):
    out = {}
    for r in rows:
        out.setdefault(r[key], []).append(r)
    return out


def extract(st, dsn=None):
    """The scratch database → derived/states/<st>/bills-<session>.json and
    votes-<session>.json for every session from the legislature's
    first_session on. A file is rewritten only when its content changed.
    Returns {session: counts}."""
    import psycopg
    from psycopg.rows import dict_row
    conf = graph.LEGISLATURES[st]
    jid = f"ocd-jurisdiction/country:us/state:{st}/government"
    month = json.loads((_raw("openstates") / "manifest.json").read_text()).get("month") \
        if (_raw("openstates") / "manifest.json").exists() else None
    out = {}
    with psycopg.connect(dsn or f"dbname={SCRATCH_DB}", row_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute("SELECT id, classification, name FROM opencivicdata_organization WHERE jurisdiction_id = %s", (jid,))
        chambers = {r["id"]: r["classification"] if r["classification"] in ("upper", "lower", "legislature")
                    else f"committee:{r['name']}" for r in cur.fetchall()}
        cur.execute("SELECT * FROM opencivicdata_legislativesession WHERE jurisdiction_id = %s", (jid,))
        every = cur.fetchall()
        cur.execute("""SELECT s.identifier, b.first_action_date, b.identifier AS bill
                       FROM opencivicdata_bill b JOIN opencivicdata_legislativesession s ON s.id = b.legislative_session_id
                       WHERE s.jurisdiction_id = %s""", (jid,))
        first_actions, types = {}, {}
        for r in cur.fetchall():
            first_actions.setdefault(r["identifier"], []).append(r["first_action_date"])
            k = bill_key(r["bill"])
            if k:
                types.setdefault(r["identifier"], set()).add(k.split("/")[0])
        cur.execute("""SELECT s.identifier, count(*) AS n FROM opencivicdata_voteevent v
                       JOIN opencivicdata_legislativesession s ON s.id = v.legislative_session_id
                       WHERE s.jurisdiction_id = %s GROUP BY 1""", (jid,))
        votes_n = {r["identifier"]: r["n"] for r in cur.fetchall()}
        index = session_index(every, first_actions, votes_n, types)
        # first_session names one; first_year takes every session whose
        # first year is that or later (the index's year, not the id's).
        first = index[conf["first_session"]]["key"] if conf.get("first_session") else [conf["first_year"], 0]
        index = {sid: rec for sid, rec in index.items() if rec["key"] >= first}
        _write_if_changed(graph.data_path("state_sessions", state=st),
                          {"meta": {"state": st, "dump_month": month, "first_session": conf.get("first_session"),
                                    "first_year": conf.get("first_year"),
                                    "source": "Open States monthly Postgres dump (data.openstates.org)"},
                           "sessions": index})
        sessions = [r for r in every if r["identifier"] in index]

        def by_bill(table, ids, order=""):
            cur.execute(f"SELECT * FROM opencivicdata_{table} WHERE bill_id = ANY(%s){order}", (ids,))
            return _group(cur.fetchall(), "bill_id")

        for s in sorted(sessions, key=lambda r: index[r["identifier"]]["key"]):
            sid = s["identifier"]
            cur.execute("SELECT * FROM opencivicdata_bill WHERE legislative_session_id = %s", (s["id"],))
            bills = cur.fetchall()
            ids = [b["id"] for b in bills]
            actions, sponsors = by_bill("billaction", ids), by_bill("billsponsorship", ids)
            abstracts, titles = by_bill("billabstract", ids), by_bill("billtitle", ids)
            identifiers, sources, related = by_bill("billidentifier", ids), by_bill("billsource", ids), \
                by_bill("relatedbill", ids)
            cur.execute("""SELECT v.*, COALESCE(json_agg(json_build_object('media_type', l.media_type, 'url', l.url))
                                   FILTER (WHERE l.id IS NOT NULL), '[]') AS links
                           FROM opencivicdata_billversion v LEFT JOIN opencivicdata_billversionlink l ON l.version_id = v.id
                           WHERE v.bill_id = ANY(%s) GROUP BY v.id, v.note, v.date, v.bill_id, v.extras, v.classification""",
                        (ids,))
            versions = _group(cur.fetchall(), "bill_id")
            records, keys, skipped, twins = {}, {}, 0, 0
            for b in sorted(bills, key=lambda b: b["id"]):
                k = bill_key(b["identifier"])
                if not k:
                    skipped += 1
                    continue
                if k in records:
                    # Two identifiers one key ("H.B. 1" and "HB 1"): the
                    # first by Open States id is kept, the other counted.
                    twins += 1
                    continue
                keys[b["id"]] = k
                records[k] = bill_record(b, chambers, actions.get(b["id"], []), sponsors.get(b["id"], []),
                                         versions.get(b["id"], []), abstracts.get(b["id"], []),
                                         titles.get(b["id"], []), identifiers.get(b["id"], []),
                                         sources.get(b["id"], []), related.get(b["id"], []))
            cur.execute("SELECT * FROM opencivicdata_voteevent WHERE legislative_session_id = %s", (s["id"],))
            events = cur.fetchall()
            vids = [v["id"] for v in events]
            cur.execute("SELECT * FROM opencivicdata_votecount WHERE vote_event_id = ANY(%s)", (vids,))
            counts = _group(cur.fetchall(), "vote_event_id")
            cur.execute("SELECT * FROM opencivicdata_personvote WHERE vote_event_id = ANY(%s)", (vids,))
            positions = _group(cur.fetchall(), "vote_event_id")
            cur.execute("SELECT * FROM opencivicdata_votesource WHERE vote_event_id = ANY(%s)", (vids,))
            vsources = _group(cur.fetchall(), "vote_event_id")
            votes = sorted((vote_record(v, chambers, keys, counts.get(v["id"], []), positions.get(v["id"], []),
                                        vsources.get(v["id"], [])) for v in events),
                           key=lambda v: (v["date"], v["bill"] or "", v["id"]))
            meta = {"state": st, "session": sid, "session_name": s.get("name"), "dump_month": month,
                    "source": "Open States monthly Postgres dump (data.openstates.org)",
                    "extracted": datetime.date.today().isoformat()}
            wrote_b = _write_if_changed(graph.data_path("state_bills", state=st, session=sid),
                                        {"meta": {**meta, "bills": len(records), "skipped": skipped, "twins": twins},
                                         "bills": records})
            wrote_v = _write_if_changed(graph.data_path("state_votes", state=st, session=sid),
                                        {"meta": {**meta, "votes": len(votes)}, "votes": votes})
            out[sid] = {"bills": len(records), "votes": len(votes), "skipped": skipped, "twins": twins,
                        "written": [n for n, w in (("bills", wrote_b), ("votes", wrote_v)) if w]}
    return out



# -------------------------------------------------------------------- text

_EXT = {"text/html": ".html", "application/pdf": ".pdf"}


def link_kind(media_type):
    """text/html, application/pdf or None. Open States' media types carry
    typos (Minnesota's "applcation/pdf", North Dakota's bare "pdf"). Pure."""
    m = (media_type or "").lower()
    return "text/html" if "html" in m else "application/pdf" if "pdf" in m else None


def text_name(url, media_type=None):
    """The file a version link is stored as: Virginia's blob name or a
    readable name for its legacy CGI link, else a hash of the URL with the
    kind's extension. Pure."""
    m = re.search(r"/files/(\d+\.(?:HTML|PDF))$", url or "", re.I)
    if m:
        return m.group(1)
    m = re.search(r"legp604\.exe\?(\w+)\+ful\+(\w+)(?:\+(\w+))?", url or "")
    if m:
        return f"legp604-{m.group(1)}-{m.group(2)}{'-' + m.group(3) if m.group(3) else ''}.html"
    return hashlib.sha1((url or "").encode()).hexdigest()[:16] + _EXT.get(link_kind(media_type), ".bin")


def stored_name(manifest, link):
    """The name a version is kept under: text_name, or the hash name with
    ".bin" that Virginia's first fetch gave 514 files, when the manifest
    has that one. Pure."""
    name = text_name(link["url"], link["media_type"])
    legacy = name.rsplit(".", 1)[0] + ".bin"
    return legacy if name not in manifest and legacy in manifest else name


def pick_link(links):
    """The one link to store for a version: HTML, else PDF, else none (a
    Word or RTF file only). The link keeps its URL; its media type is the
    clean kind."""
    for kind in ("text/html", "application/pdf"):
        for ln in links:
            # An ftp:// link (Connecticut, Texas) is not one we can fetch.
            if link_kind(ln.get("media_type")) == kind and (ln.get("url") or "").startswith("http"):
                return {**ln, "media_type": kind}
    return None


def pdf_text(data):
    """A PDF's text, page by page, with pypdf. None when it has none (a
    scan)."""
    import io
    from pypdf import PdfReader
    try:
        text = "\n".join((p.extract_text() or "") for p in PdfReader(io.BytesIO(data)).pages).strip()
    except Exception:
        return None
    return text or None


def pdf_form(page):
    """California's "PDF" link (leginfo's billPdf.xhtml) answers with a page
    whose script posts a form back for the PDF. The form's fields, or None
    for any other page. Pure."""
    text = page.decode("utf-8", "replace")
    if 'name="downloadForm"' not in text:
        return None
    state = re.search(r'name="javax\.faces\.ViewState"[^>]*value="([^"]+)"', text)
    bill, version = re.search(r"'bill_id':'([^']+)'", text), re.search(r"'version':'([^']+)'", text)
    action = re.search(r'<form id="downloadForm"[^>]*action="([^"]+)"', text)
    if not (state and bill and version and action):
        return None
    return action.group(1), {"downloadForm": "downloadForm", "pdf_link2": "pdf_link2", "bill_id": bill.group(1),
                             "version": version.group(1), "javax.faces.ViewState": state.group(1)}


def text_root(st, sid):
    return graph.data_path("state_text", state=st, session=sid, name="manifest.json").parent


def sync_text(st, sid, session_=None, gap=1.0, limit=None, failing=None):
    """Fetch the text of every version of one session once, from the links
    the Open States record gives. Stored gzipped under
    raw/states/<st>/text/<session>/ with a manifest; a PDF also gets a
    .txt.gz of its extracted text. At most one request a second to each
    host (`gap`). A failure is recorded and retried on the next run; a
    stored file is never fetched again. A host that answers 429 is asked
    half as often (Retry-After is kept). `failing`: {host: failures in a
    row}, shared across one run's sessions so a dead host is left once.
    Returns counts."""
    import requests
    from urllib.parse import urlsplit
    if session_ is None:
        session_ = requests.Session()
        session_.headers["User-Agent"] = _UA
    bills = json.loads(graph.data_path("state_bills", state=st, session=sid).read_text())["bills"]
    root = text_root(st, sid)
    root.mkdir(parents=True, exist_ok=True)
    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    fetched = failed = kept = none = skipped = 0
    last, gaps = {}, {}
    failing = {} if failing is None else failing
    try:
        for key, b in sorted(bills.items()):
            for v in b["versions"]:
                link = pick_link(v["links"])
                if not link:
                    none += 1
                    continue
                name = stored_name(manifest, link)
                if manifest.get(name, {}).get("status") == "ok" and (root / (name + ".gz")).exists():
                    kept += 1
                    continue
                if limit is not None and fetched + failed >= limit:
                    raise StopIteration
                host = urlsplit(link["url"]).netloc
                if failing.get(host, 0) >= 10:
                    # Ten failures in a row: the host refuses us or is down.
                    # The rest of its links wait for the next run.
                    skipped += 1
                    continue
                wait = last.get(host, 0) + gaps.get(host, gap) - time.monotonic()
                if wait > 0:
                    time.sleep(wait)
                try:
                    r = session_.get(link["url"], timeout=(15, 60))
                    if getattr(r, "status_code", 200) == 429:
                        retry = r.headers.get("Retry-After") or ""
                        time.sleep(min(300, int(retry)) if retry.isdigit() else 30)
                        gaps[host] = min(10.0, max(gap, gaps.get(host, gap)) * 2)
                    r.raise_for_status()
                    form = pdf_form(r.content) if link["media_type"] == "application/pdf" and \
                        not r.content.startswith(b"%PDF") else None
                    if form:
                        # California: the page posts a form for the PDF. A
                        # second request to the same host, so it waits too.
                        from urllib.parse import urljoin
                        time.sleep(gaps.get(host, gap))
                        r = session_.post(urljoin(link["url"], form[0]), data=form[1], timeout=(15, 60))
                        r.raise_for_status()
                    if link["media_type"] == "application/pdf" and not r.content.startswith(b"%PDF"):
                        # Indiana's iga.in.gov answers with its app's page.
                        raise ValueError(f"not a PDF: {r.headers.get('content-type')}, {len(r.content)} bytes")
                    (root / (name + ".gz")).write_bytes(gzip.compress(r.content))
                    entry = {"status": "ok", "url": link["url"], "bill": key, "version": v["name"],
                             "media_type": link["media_type"], "bytes": len(r.content),
                             "fetched": datetime.date.today().isoformat()}
                    if link["media_type"] == "application/pdf":
                        text = pdf_text(r.content)
                        if text:
                            (root / (name + ".txt.gz")).write_bytes(gzip.compress(text.encode()))
                        entry["text"] = bool(text)
                    fetched += 1
                    failing[host] = 0
                except Exception as e:
                    entry = {"status": "error", "url": link["url"], "bill": key, "version": v["name"],
                             "error": type(e).__name__, "detail": str(e)[:200],
                             "http": getattr(getattr(e, "response", None), "status_code", None),
                             "tried": datetime.date.today().isoformat()}
                    failed += 1
                    # Only a host that does not answer, fails, or refuses
                    # counts toward leaving it: Iowa's run of dead links
                    # (404) left a host that was up.
                    status = entry["http"]
                    down = status is None and not isinstance(e, ValueError) or status == 403 or (status or 0) >= 500
                    failing[host] = failing.get(host, 0) + 1 if down else 0
                last[host] = time.monotonic()
                manifest[name] = entry
                if (fetched + failed) % 200 == 0:
                    manifest_path.write_text(json.dumps(manifest, sort_keys=True))
    except StopIteration:
        pass
    finally:
        manifest_path.write_text(json.dumps(manifest, sort_keys=True))
    return {"state": st, "session": sid, "fetched": fetched, "failed": failed, "kept": kept, "no_link": none,
            "host_skipped": skipped, "hosts_down": sorted(h for h, n in failing.items() if n >= 10)}


def text_lock(st):
    """An open lock file holding this state's text, or None when another
    process holds it: the daily sync's --latest skips a state whose
    backfill unit is fetching, rather than write its manifests twice."""
    import fcntl
    path = graph.DATA_DIR / "raw" / "states" / st / "text" / ".lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(path, "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fh.close()
        return None
    return fh


def backfilled(st):
    """True when every session of the state has a text manifest: its full
    fetch has run once. The daily --latest only tops these up; a state not
    yet backfilled is its backfill unit's job (on 2026-09-28 the daily sync
    spent four hours fetching Washington's and Wisconsin's first copies and
    held the graph load behind them)."""
    return all((text_root(st, sid) / "manifest.json").exists() for sid in graph.state_sessions(st))


def text_sessions(st, latest=False):
    """The sessions to fetch text for, newest first: every one on disk, or
    with `latest` those of the last two years (the sitting session and the
    prefiles for the next), for the daily sync."""
    sessions = graph.state_sessions(st)
    if latest:
        last = max((graph.session_key(x, st)[0] for x in sessions), default=0)
        sessions = [x for x in sessions if graph.session_key(x, st)[0] >= last - 1]
    return list(reversed(sessions))


if __name__ == "__main__":
    cmd, args = (sys.argv[1] if len(sys.argv) > 1 else ""), sys.argv[2:]
    if cmd == "people":
        for st in args or list(graph.LEGISLATURES):
            print(json.dumps(people(st)))
    elif cmd == "dump":
        print(json.dumps(sync_dump()))
    elif cmd == "restore":
        print(json.dumps(restore()))
    elif cmd == "extract":
        for st in args or list(graph.LEGISLATURES):
            for sid, c in extract(st).items():
                print(st, sid, json.dumps(c))
    elif cmd == "drop":
        drop_scratch()
    elif cmd == "text":
        # A stopped unit (SIGTERM) still writes its manifest: sync_text's
        # finally runs on SystemExit.
        import signal
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
        # text <st> [session ...]: those sessions, or every one on disk,
        # newest first. text --latest: each text state's last two years.
        if args[:1] == ["--latest"]:
            for st in [k for k, v in graph.LEGISLATURES.items() if v.get("text")]:
                if not backfilled(st):
                    print(json.dumps({"state": st, "skipped": "not backfilled yet; its full fetch runs apart"}))
                    continue
                lock = text_lock(st)
                if lock is None:
                    print(json.dumps({"state": st, "skipped": "another process is fetching this state's text"}))
                    continue
                failing = {}
                for sid in text_sessions(st, latest=True):
                    print(json.dumps(sync_text(st, sid, failing=failing)), flush=True)
                lock.close()
        else:
            st, sessions, failing = args[0], args[1:], {}
            lock = text_lock(st)
            if lock is None:
                sys.exit(f"{st}: another process is fetching this state's text")
            for sid in sessions or text_sessions(st):
                print(json.dumps(sync_text(st, sid, failing=failing)), flush=True)
    else:
        print(__doc__)
