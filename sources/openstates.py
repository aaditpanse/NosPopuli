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
"""

import datetime
import hashlib
import json
import os
import pathlib
import re
import subprocess
import sys

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
    root = sync_people([st]) if sync else _raw("openstates-people")
    base = root / "data" / st
    records, files = [], 0
    for folder in ("legislature", "retired", "executive"):
        for f in sorted((base / folder).glob("*.yml")):
            records.append(parse_person(yaml.safe_load(f.read_text()), st))
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
        # curl resumes a partial file (-C -) and streams to disk.
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
    """'HB 1' → 'hb/1', 'HJR 5' → 'hjr/5'; None for an identifier that is
    not letters then a number. Pure."""
    m = re.fullmatch(r"([A-Za-z]+)\s*0*(\d+)", (identifier or "").strip())
    return f"{m.group(1).lower()}/{m.group(2)}" if m else None


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
    first = graph.session_key(conf["first_session"])
    month = json.loads((_raw("openstates") / "manifest.json").read_text()).get("month") \
        if (_raw("openstates") / "manifest.json").exists() else None
    out = {}
    with psycopg.connect(dsn or f"dbname={SCRATCH_DB}", row_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute("SELECT id, classification, name FROM opencivicdata_organization WHERE jurisdiction_id = %s", (jid,))
        chambers = {r["id"]: r["classification"] if r["classification"] in ("upper", "lower", "legislature")
                    else f"committee:{r['name']}" for r in cur.fetchall()}
        cur.execute("SELECT id, identifier FROM opencivicdata_legislativesession WHERE jurisdiction_id = %s", (jid,))
        sessions = [r for r in cur.fetchall() if graph.session_key(r["identifier"]) and
                    graph.session_key(r["identifier"]) >= first]

        def by_bill(table, ids, order=""):
            cur.execute(f"SELECT * FROM opencivicdata_{table} WHERE bill_id = ANY(%s){order}", (ids,))
            return _group(cur.fetchall(), "bill_id")

        for s in sorted(sessions, key=lambda r: graph.session_key(r["identifier"])):
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
            records, keys, skipped = {}, {}, 0
            for b in bills:
                k = bill_key(b["identifier"])
                if not k:
                    skipped += 1
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
                                        {"meta": {**meta, "bills": len(records), "skipped": skipped}, "bills": records})
            wrote_v = _write_if_changed(graph.data_path("state_votes", state=st, session=sid),
                                        {"meta": {**meta, "votes": len(votes)}, "votes": votes})
            out[sid] = {"bills": len(records), "votes": len(votes), "skipped": skipped,
                        "written": [n for n, w in (("bills", wrote_b), ("votes", wrote_v)) if w]}
    return out


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
    else:
        print(__doc__)
