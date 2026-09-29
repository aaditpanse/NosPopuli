"""The bill search index: one document and one embedding per bill.

bill_doc holds each bill's search document (titles, policy area, subjects,
latest CRS summary, from GovInfo's BILLSTATUS), with a full-text column.
bill_embedding holds one vector per bill in Voyage 4's shared embedding
space: documents are embedded once by voyage-4-large through the API, and
a user's query is embedded on this server by voyage-4-nano (Apache 2.0,
open weights), so a question never leaves the machine and search never
waits on an outside API. Voyage 4's models share one space by design
(blog.voyageai.com/2026/01/15/voyage-4), which is what makes the split
possible; if the API ever goes, nano can re-embed the documents into the
same space.

    python -m search.bill_index docs [congress ...]   # load changed documents
    python -m search.bill_index embed                  # embed new or changed documents
    python -m search.bill_index salience               # each bill's weight from the graph
    python -m search.bill_index text [us|st ...]       # index each bill's latest text
    python -m search.bill_index query "text"           # nearest bills
    python -m search.bill_index search "text"          # full-text + nearest, fused
    python -m search.bill_index latency                # query embedding time here
"""

import hashlib
import json
import os
import pathlib
import re
import sys
import time

_HERE = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_HERE))

# One space, named once: every vector in bill_embedding with this tag is
# comparable with every other, whichever Voyage 4 model made it.
SPACE = "voyage-4@1024"
DIM = 1024
DOC_MODEL = "voyage-4-large"
QUERY_MODEL = "voyageai/voyage-4-nano"
# Pinned: the model runs remote code, and a new revision could move the space.
QUERY_REVISION = "67fabc9bef010dabc5f6024aa1b1b6b93410426f"
MODEL_DIR = pathlib.Path(os.environ.get("NOSPOPULI_MODEL_DIR") or "/srv/nospopuli/models")
VOYAGE_URL = "https://api.voyageai.com/v1/embeddings"
# Voyage's per-request caps are 1,000 inputs and about 120k tokens for the
# large model; a character estimate (3.5 a token) keeps well under both.
_BATCH_INPUTS, _BATCH_CHARS = 256, 280_000


def batches(docs, max_inputs=_BATCH_INPUTS, max_chars=_BATCH_CHARS):
    """Split (id, text) pairs into API-sized requests. A single document
    longer than the budget travels alone (the API truncates it). Pure."""
    out, cur, size = [], [], 0
    for item in docs:
        n = len(item[1])
        if cur and (len(cur) >= max_inputs or size + n > max_chars):
            out.append(cur)
            cur, size = [], 0
        cur.append(item)
        size += n
    if cur:
        out.append(cur)
    return out


# ---------------------------------------------------------------- documents

DOCS_VERSION = 2       # 2: latest action and sponsor columns, for the feed
STATE_DOCS_VERSION = 1  # 1: the funnel stage column
US_DIV = "ocd-division/country:us"


def state_doc(st, session, key, b, people_names=None, newer=()):
    """One state bill (a bills-<session>.json record) → its search
    document: identifier and title, other titles, subjects, and the
    legislature's own summary (Open States abstracts, the title one left
    out). Pure."""
    import graph
    itype, number = key.split("/")
    summaries = [a["abstract"] for a in b.get("abstracts") or [] if (a.get("note") or "") != "title"]
    summary = " ".join(summaries)[:12000]
    chapter = next((m.group(1) for a in b.get("actions") or []
                    for m in [graph._CHAPTER.search(a.get("description") or "")]
                    if m and ("became-law" in a["classification"] or graph.executive_act(a) == "signed")), None)
    primary = next((s for s in b.get("sponsors") or [] if s["primary"]), None)
    parts = [f"{b['identifier']}: {b['title']}"]
    others = [t for t in b.get("other_titles") or [] if t != b["title"]][:8]
    if others:
        parts.append("Also known as: " + "; ".join(others))
    if b.get("subjects"):
        parts.append("Subjects: " + "; ".join(b["subjects"][:40]))
    if summary:
        parts.append(f"Summary: {summary}")
    doc = "\n".join(parts)
    # A record with no date writes "" (a Massachusetts bill with no first
    # action); the date columns take None.
    return {"instrument_id": graph.state_instrument_id(st, session, key), "congress": None,
            "bill_type": itype, "number": number, "title": b["title"], "introduced": b.get("first_action_date") or None,
            "policy_area": (b.get("subjects") or [None])[0], "subjects": b.get("subjects") or [],
            "is_law": bool(chapter), "law_numbers": [chapter] if chapter else [], "summary": summary, "doc": doc,
            "doc_sha": hashlib.sha1(doc.encode()).hexdigest(), "latest_action": b.get("latest_action"),
            "latest_action_date": b.get("latest_action_date") or None, "sponsor_bioguide": None,
            "sponsor_name": primary["name"] if primary else None,
            "jurisdiction": graph.state_div(st), "session": session,
            "stage": graph.state_stage(b.get("actions"), newer)}


def load_state_docs(st, force=False):
    """The search documents of each of a state's sessions whose bills file
    changed since the last load (graph_scope row docs/<st>/<session>).
    Returns {session: rows written}."""
    import graph
    from correspondence.db import _get_pool, init_db
    init_db()
    out = {}
    with _get_pool().connection() as conn:
        for sid in graph.state_sessions(st):
            path = graph.data_path("state_bills", state=st, session=sid)
            lis_path = graph.data_path("state_lis", state=st, session=sid)
            st_ = path.stat()
            # The legislature's newer actions move a bill's stage, so its file is in the fingerprint.
            lis_fp = " ".join(map(str, graph._stat(lis_path))) if lis_path.exists() else "-"
            fp = f"{DOCS_VERSION}s{STATE_DOCS_VERSION}\n{st_.st_size} {st_.st_mtime_ns}\n{lis_fp}"
            scope = f"docs/{st}/{sid}"
            with conn.transaction(), conn.cursor() as cur:
                cur.execute("SELECT fingerprint FROM graph_scope WHERE scope = %s", (scope,))
                row = cur.fetchone()
                if row and row[0] == fp and not force:
                    continue
                bills = json.loads(path.read_text())["bills"]
                history = json.loads(lis_path.read_text()).get("history", {}) if lis_path.exists() else {}
                newer = lambda k, b: [h for h in history.get(k, [])
                                      if (h.get("date") or "") > (b.get("latest_action_date") or "")]
                out[sid] = _write_docs(cur, (state_doc(st, sid, k, b, newer=newer(k, b)) for k, b in bills.items()))
                cur.execute("""INSERT INTO graph_scope (scope, fingerprint, loaded_at, nodes, edges)
                               VALUES (%s, %s, NOW(), %s, 0)
                               ON CONFLICT (scope) DO UPDATE SET fingerprint = excluded.fingerprint,
                                   loaded_at = NOW(), nodes = excluded.nodes""", (scope, fp, out[sid]))
    return out


def load_docs(congresses=None, force=False):
    """Load the search documents of each Congress whose BILLSTATUS zips
    changed since the last load (graph_scope row docs/<c>). A document whose
    text is unchanged keeps its row and its embedding. Returns {congress:
    rows written}."""
    import graph
    from sources import govinfo
    from correspondence.db import _get_pool, init_db
    init_db()
    congresses = congresses or range(govinfo.FIRST_CONGRESS, graph.current_session()[0] + 1)
    out = {}
    with _get_pool().connection() as conn:
        for c in congresses:
            raw = graph.DATA_DIR / "raw" / "govinfo" / "BILLSTATUS" / str(c)
            manifest = raw / "manifest.json"
            if not manifest.exists():
                continue
            # DOCS_VERSION: a change to what a row holds reloads every Congress.
            fp = f"{DOCS_VERSION}\n{manifest.read_text()}"
            with conn.transaction(), conn.cursor() as cur:
                cur.execute("SELECT fingerprint FROM graph_scope WHERE scope = %s", (f"docs/{c}",))
                row = cur.fetchone()
                if row and row[0] == fp and not force:
                    continue
                out[c] = _write_docs(cur, govinfo.bill_docs(c))
                cur.execute("""INSERT INTO graph_scope (scope, fingerprint, loaded_at, nodes, edges)
                               VALUES (%s, %s, NOW(), %s, 0)
                               ON CONFLICT (scope) DO UPDATE SET fingerprint = excluded.fingerprint,
                                   loaded_at = NOW(), nodes = excluded.nodes""", (f"docs/{c}", fp, out[c]))
    return out


def _write_docs(cur, docs):
    cur.execute("""CREATE TEMP TABLE stage_doc (instrument_id TEXT, congress INT, bill_type TEXT, number TEXT,
                   title TEXT, introduced DATE, policy_area TEXT, subjects TEXT[], is_law BOOLEAN,
                   law_numbers TEXT[], summary TEXT, doc TEXT, doc_sha TEXT, latest_action TEXT,
                   latest_action_date DATE, sponsor_bioguide TEXT, sponsor_name TEXT,
                   jurisdiction TEXT, session TEXT, stage TEXT) ON COMMIT DROP""")
    n = 0
    with cur.copy("COPY stage_doc FROM STDIN") as cp:
        for d in docs:
            cp.write_row((d["instrument_id"], d["congress"], d["bill_type"], d["number"], d["title"],
                          d["introduced"], d["policy_area"], d["subjects"], d["is_law"], d["law_numbers"],
                          d["summary"], d["doc"], d["doc_sha"], d["latest_action"], d["latest_action_date"],
                          d["sponsor_bioguide"], d["sponsor_name"], d.get("jurisdiction") or US_DIV,
                          d.get("session"), d.get("stage")))
            n += 1
    # A new action changes a row without changing its document, so the
    # embedding (keyed on doc_sha) stays and only the columns move.
    cur.execute("""
        INSERT INTO bill_doc (instrument_id, congress, bill_type, number, title, introduced, policy_area,
                              subjects, is_law, law_numbers, summary, doc, doc_sha, latest_action,
                              latest_action_date, sponsor_bioguide, sponsor_name, jurisdiction, session, stage, updated_at)
        SELECT DISTINCT ON (instrument_id) instrument_id, congress, bill_type, number, title, introduced,
               policy_area, subjects, is_law, law_numbers, summary, doc, doc_sha, latest_action,
               latest_action_date, sponsor_bioguide, sponsor_name, jurisdiction, session, stage, NOW()
        FROM stage_doc
        ON CONFLICT (instrument_id) DO UPDATE SET
            title = excluded.title, introduced = excluded.introduced, policy_area = excluded.policy_area,
            subjects = excluded.subjects, is_law = excluded.is_law, law_numbers = excluded.law_numbers,
            summary = excluded.summary, doc = excluded.doc, doc_sha = excluded.doc_sha,
            latest_action = excluded.latest_action, latest_action_date = excluded.latest_action_date,
            sponsor_bioguide = excluded.sponsor_bioguide, sponsor_name = excluded.sponsor_name,
            jurisdiction = excluded.jurisdiction, session = excluded.session, stage = excluded.stage,
            updated_at = NOW()
        WHERE (bill_doc.doc_sha, bill_doc.latest_action, bill_doc.latest_action_date, bill_doc.sponsor_bioguide,
               bill_doc.sponsor_name, bill_doc.jurisdiction, bill_doc.session, bill_doc.stage)
              IS DISTINCT FROM (excluded.doc_sha, excluded.latest_action, excluded.latest_action_date,
                                excluded.sponsor_bioguide, excluded.sponsor_name, excluded.jurisdiction,
                                excluded.session, excluded.stage)""")
    cur.execute("DROP TABLE stage_doc")
    return n


# --------------------------------------------------------------- embeddings

def embed_documents(texts, key, session=None):
    """Document vectors from voyage-4-large. Raises on failure: a batch
    either lands whole or not at all, and the next run retries it."""
    import requests
    s = session or requests.Session()
    for attempt in range(5):
        r = s.post(VOYAGE_URL, headers={"Authorization": f"Bearer {key}"}, timeout=120,
                   json={"input": texts, "model": DOC_MODEL, "input_type": "document",
                         "output_dimension": DIM})
        if r.status_code == 429:
            time.sleep(20 * (attempt + 1))
            continue
        if r.status_code != 200:
            # The body, not the request: the key rides in a header and must
            # never reach a log.
            raise RuntimeError(f"Voyage embeddings: HTTP {r.status_code}: {r.text[:200]}")
        data = r.json()
        return [d["embedding"] for d in sorted(data["data"], key=lambda d: d["index"])], \
            (data.get("usage") or {}).get("total_tokens", 0)
    raise RuntimeError("Voyage embeddings: rate-limited five times")


def embed_missing(limit=None):
    """Embed every document with no vector in SPACE, or whose text changed
    since its vector was made. Commits per batch, so a stopped run resumes
    where it stopped. Returns (documents embedded, tokens used)."""
    import requests
    from correspondence.db import _get_pool
    key = (os.environ.get("VOYAGE_API_KEY") or "").strip()
    if not key:
        raise RuntimeError("VOYAGE_API_KEY not set; documents are not embedded")
    with _get_pool().connection() as conn, conn.cursor() as cur:
        cur.execute("""SELECT d.instrument_id, d.doc, d.doc_sha FROM bill_doc d
                       LEFT JOIN bill_embedding e ON e.instrument_id = d.instrument_id AND e.space = %s
                       WHERE e.instrument_id IS NULL OR e.text_sha <> d.doc_sha
                       ORDER BY d.congress DESC, d.instrument_id""" + (" LIMIT %s" if limit else ""),
                    (SPACE, limit) if limit else (SPACE,))
        todo = cur.fetchall()
    s = requests.Session()
    done = tokens = 0
    for batch in batches([(i, doc, sha) for i, doc, sha in todo]):
        vectors, used = embed_documents([b[1] for b in batch], key, s)
        with _get_pool().connection() as conn, conn.transaction(), conn.cursor() as cur:
            cur.executemany("""
                INSERT INTO bill_embedding (instrument_id, space, embedding, text_sha, embedded_by, embedded_at)
                VALUES (%s, %s, %s::halfvec, %s, %s, NOW())
                ON CONFLICT (instrument_id, space) DO UPDATE SET embedding = excluded.embedding,
                    text_sha = excluded.text_sha, embedded_by = excluded.embedded_by, embedded_at = NOW()""",
                            [(b[0], SPACE, json.dumps(v), b[2], DOC_MODEL) for b, v in zip(batch, vectors)])
        done += len(batch)
        tokens += used
    return done, tokens


_query_model = None


def _load_query_model():
    """voyage-4-nano on the CPU, from the local copy (fetch_model), never
    from the network at query time. Imported lazily: torch costs seconds and
    hundreds of MB, and only a search needs it."""
    global _query_model
    if _query_model is None:
        os.environ.setdefault("HF_HUB_OFFLINE", "1")
        from sentence_transformers import SentenceTransformer
        _query_model = SentenceTransformer(str(MODEL_DIR / f"voyage-4-nano@{QUERY_REVISION}"),
                                           trust_remote_code=True, device="cpu", truncate_dim=DIM)
    return _query_model


def embed_query(texts):
    """Query vectors in SPACE, made here by voyage-4-nano."""
    m = _load_query_model()
    return m.encode(list(texts), prompt_name="query", normalize_embeddings=True).tolist()


def fetch_model():
    """Download the pinned query model into MODEL_DIR once (setup, not a request)."""
    from huggingface_hub import snapshot_download
    target = MODEL_DIR / f"voyage-4-nano@{QUERY_REVISION}"
    snapshot_download(QUERY_MODEL, revision=QUERY_REVISION, local_dir=str(target))
    return target


def knn(query, k=20):
    """The k bills nearest a question: [(instrument_id, title, similarity)]."""
    from correspondence.db import _get_pool
    vec = json.dumps(embed_query([query])[0])
    with _get_pool().connection() as conn, conn.cursor() as cur:
        cur.execute("SET LOCAL hnsw.ef_search = 100")
        cur.execute("""SELECT e.instrument_id, d.title, 1 - (e.embedding <=> %s::halfvec) AS sim
                       FROM bill_embedding e JOIN bill_doc d USING (instrument_id)
                       WHERE e.space = %s ORDER BY e.embedding <=> %s::halfvec LIMIT %s""",
                    (vec, SPACE, vec, k))
        return cur.fetchall()


# ---------------------------------------------------------------- bill text

TEXT_VERSION = 1
# A tsvector holds at most 1 MB of lexemes and positions; the first 300,000
# characters of a bill hold its operative text, and an omnibus's tail is
# lists of amounts. Positions are kept: a phrase ("catalytic converter")
# needs them.
TEXT_CHARS = 300_000
_BILLS_HTM = re.compile(r"^BILLS-(\d+)([a-z]+)(\d+)([a-z]+)\.htm\.gz$")


def plain_text(html):
    """GovInfo's bill typescript (<pre> inside HTML) as plain words. Pure."""
    import html as html_lib
    return re.sub(r"\s+", " ", html_lib.unescape(re.sub(r"<[^>]+>", " ", html))).strip()


def federal_texts(congress):
    """(instrument_id, version, text) for the furthest stored version of each
    bill of one Congress (sources.bill_fetcher's stage order)."""
    import gzip
    import graph
    from sources.bill_fetcher import _LOCAL_STAGE_PRIORITY
    d = graph.DATA_DIR / "raw" / "govinfo" / "BILLS-htm" / str(congress)
    best = {}
    rank = {s: i for i, s in enumerate(_LOCAL_STAGE_PRIORITY)}
    for p in d.glob("BILLS-*.htm.gz"):
        m = _BILLS_HTM.match(p.name)
        if not m:
            continue
        key = (m.group(2), m.group(3))
        r = rank.get(m.group(4), len(rank))
        if key not in best or r < best[key][0]:
            best[key] = (r, m.group(4), p)
    for (itype, number), (_, stage, p) in sorted(best.items()):
        yield (f"instrument/us/{congress}/{itype}/{number}", stage,
               plain_text(gzip.decompress(p.read_bytes()).decode("utf-8", "replace")))


def state_texts(st, session):
    """(instrument_id, version, text) for each bill of one state session
    whose furthest stored version has text (graph.default_version)."""
    import graph
    bills = json.loads(graph.data_path("state_bills", state=st, session=session).read_text())["bills"]
    for key, b in bills.items():
        v = graph.default_version(graph.state_versions(st, session, b))
        if v and v["text"]:
            text = graph.state_version_text(st, session, v)
            if text:
                yield graph.state_instrument_id(st, session, key), v["name"], re.sub(r"\s+", " ", text)


def _write_texts(cur, rows):
    """Stage texts and index the new or changed ones; unchanged texts are not
    re-parsed. Returns (texts read, texts indexed)."""
    cur.execute("""CREATE TEMP TABLE stage_text (instrument_id TEXT, version TEXT, text_sha TEXT,
                   chars INT, body TEXT) ON COMMIT DROP""")
    n = 0
    with cur.copy("COPY stage_text FROM STDIN") as cp:
        for iid, version, text in rows:
            # A NUL byte (a few GovInfo files carry one) cannot enter a text field.
            text = text.replace("\x00", "")[:TEXT_CHARS]
            cp.write_row((iid, version, hashlib.sha1(text.encode()).hexdigest(), len(text), text))
            n += 1
    cur.execute("""
        INSERT INTO bill_text_doc (instrument_id, version, text_sha, chars, tsv, updated_at)
        SELECT s.instrument_id, s.version, s.text_sha, s.chars, to_tsvector('english', s.body), NOW()
        FROM stage_text s JOIN bill_doc d USING (instrument_id)
        WHERE NOT EXISTS (SELECT 1 FROM bill_text_doc b
                          WHERE b.instrument_id = s.instrument_id AND b.text_sha = s.text_sha)
        ON CONFLICT (instrument_id) DO UPDATE SET version = excluded.version, text_sha = excluded.text_sha,
            chars = excluded.chars, tsv = excluded.tsv, updated_at = NOW()""")
    indexed = cur.rowcount
    cur.execute("DROP TABLE stage_text")
    return n, indexed


def load_texts(which=None, force=False):
    """Index the latest text of every bill whose text files changed since the
    last run (graph_scope rows text/us/<c> and text/<st>/<session>). `which`:
    "us", state codes, or None for all. Prints a line per scope; returns
    {scope: (read, indexed)}. Its own unit: the first build reads every
    stored text and takes hours, so it stays out of the daily sync."""
    import graph
    from sources import govinfo
    from correspondence.db import _get_pool, init_db
    init_db()
    which = which or ["us"] + sorted(graph.LEGISLATURES)
    scopes = []
    if "us" in which:
        for c in range(govinfo.FIRST_CONGRESS, graph.current_session()[0] + 1):
            d = graph.DATA_DIR / "raw" / "govinfo" / "BILLS-htm" / str(c)
            if d.exists():
                files = list(d.glob("BILLS-*.htm.gz"))
                fp = f"{TEXT_VERSION}\n{len(files)} {max((f.stat().st_mtime_ns for f in files), default=0)}"
                scopes.append((f"text/us/{c}", fp, lambda c=c: federal_texts(c)))
    for st in [w for w in which if w != "us"]:
        for sid in graph.state_sessions(st):
            m = graph.data_path("state_text", state=st, session=sid, name="manifest.json")
            if not m.exists():
                continue
            b = graph.data_path("state_bills", state=st, session=sid).stat()
            fp = f"{TEXT_VERSION}\n{m.stat().st_size} {m.stat().st_mtime_ns}\n{b.st_size} {b.st_mtime_ns}"
            scopes.append((f"text/{st}/{sid}", fp, lambda st=st, sid=sid: state_texts(st, sid)))
    out = {}
    with _get_pool().connection() as conn:
        for scope, fp, rows in scopes:
            with conn.transaction(), conn.cursor() as cur:
                cur.execute("SELECT fingerprint FROM graph_scope WHERE scope = %s", (scope,))
                row = cur.fetchone()
                if row and row[0] == fp and not force:
                    continue
                t0 = time.time()
                out[scope] = _write_texts(cur, rows())
                cur.execute("""INSERT INTO graph_scope (scope, fingerprint, loaded_at, nodes, edges)
                               VALUES (%s, %s, NOW(), %s, 0)
                               ON CONFLICT (scope) DO UPDATE SET fingerprint = excluded.fingerprint,
                                   loaded_at = NOW(), nodes = excluded.nodes""", (scope, fp, out[scope][0]))
            print(json.dumps({"scope": scope, "read": out[scope][0], "indexed": out[scope][1],
                              "secs": round(time.time() - t0)}), flush=True)
    return out


# ----------------------------------------------------------------- salience

# How much each kind of edge says a bill matters. A law and a signature say
# the most; counts are logged, so the 300th cosponsor adds little and one
# roll call adds a lot. Hand-set, then checked on the Tier 2 set's tuning
# rows (never the held-out ones).
SALIENCE_WEIGHTS = {"law": 3.0, "signed": 1.0, "sponsors": 0.5, "rollcalls": 1.0, "lobbying": 1.0,
                    "related": 0.5, "referrals": 0.3, "reported": 0.5}


def salience_sql(w=SALIENCE_WEIGHTS):
    """The one statement that sets bill_doc.salience from the graph's edges
    into and out of each bill. Pure (it builds SQL)."""
    return f"""
        WITH inn AS (
            SELECT dst AS id,
                   count(*) FILTER (WHERE predicate = 'sponsored') AS sponsors,
                   count(*) FILTER (WHERE predicate = 'considered') AS rollcalls,
                   count(*) FILTER (WHERE predicate = 'signed') AS signed,
                   coalesce(sum(coalesce((props->>'reports')::int, 1)) FILTER (WHERE predicate = 'lobbied_on'), 0) AS lobbying,
                   count(*) FILTER (WHERE predicate = 'reported') AS reported,
                   count(*) FILTER (WHERE predicate = 'related_to') AS related
            FROM graph_edge
            WHERE predicate IN ('sponsored', 'considered', 'signed', 'lobbied_on', 'reported', 'related_to')
            GROUP BY dst),
        out_ AS (
            SELECT src AS id,
                   count(*) FILTER (WHERE predicate = 'referred_to') AS referrals,
                   count(*) FILTER (WHERE predicate IN ('related_to', 'enacted_as')) AS related
            FROM graph_edge WHERE predicate IN ('referred_to', 'related_to', 'enacted_as')
            GROUP BY src),
        s AS (
            SELECT d.instrument_id AS id,
                   {w['law']} * (d.is_law)::int
                   + {w['signed']} * least(coalesce(i.signed, 0), 1)
                   + {w['sponsors']} * ln(1 + coalesce(i.sponsors, 0))
                   + {w['rollcalls']} * ln(1 + coalesce(i.rollcalls, 0))
                   + {w['lobbying']} * ln(1 + coalesce(i.lobbying, 0))
                   + {w['related']} * ln(1 + coalesce(i.related, 0) + coalesce(o.related, 0))
                   + {w['referrals']} * ln(1 + coalesce(o.referrals, 0))
                   + {w['reported']} * ln(1 + coalesce(i.reported, 0)) AS salience
            FROM bill_doc d LEFT JOIN inn i ON i.id = d.instrument_id LEFT JOIN out_ o ON o.id = d.instrument_id)
        UPDATE bill_doc d SET salience = s.salience FROM s
        WHERE d.instrument_id = s.id AND d.salience IS DISTINCT FROM s.salience"""


def compute_salience():
    """Set every bill's salience from the graph. Run after the graph load
    and the documents (the daily sync). Returns rows changed."""
    from correspondence.db import _get_pool, init_db
    init_db()
    with _get_pool().connection() as conn, conn.transaction(), conn.cursor() as cur:
        cur.execute(salience_sql())
        return cur.rowcount


# ------------------------------------------------------------------- search

# Cormack et al.'s constant. Fusion reads only ranks, so the two lists need
# no common scale: a ts_rank and a cosine are not comparable numbers.
RRF_K = 60
# How deep each list goes before fusion. A bill ranked 50th in one list and
# absent from the other cannot reach a top 10 against k=60.
_DEPTH = 50


def rrf(*rankings, k=RRF_K):
    """Reciprocal-rank fusion of ranked id lists. Ties break on the id, so
    the same inputs give the same order in every process. Pure."""
    score = {}
    for ranking in rankings:
        for i, x in enumerate(ranking):
            score[x] = score.get(x, 0.0) + 1.0 / (k + i + 1)
    return sorted(score, key=lambda x: (-score[x], x))


def _iso(d):
    return d.isoformat() if hasattr(d, "isoformat") else (d or "")


def as_result(row):
    """A bill_doc row in the shape search_bills has always returned, so the
    ranker and validator downstream need no change. Pure."""
    import graph
    laws = row.get("law_numbers") or []
    jurisdiction = row.get("jurisdiction") or US_DIV
    if jurisdiction != US_DIV:
        # A state bill: its own id, session and chapter; no GovInfo package.
        st = jurisdiction.rsplit(":", 1)[-1]
        return {
            "state": st, "session": row.get("session"), "jurisdiction": jurisdiction,
            "identifier": f"{row['bill_type'].upper()} {row['number']}",
            "title": row.get("title") or f"{row['bill_type'].upper()} {row['number']}",
            "date_issued": row["introduced"].isoformat() if row.get("introduced") else "",
            "type": row["bill_type"], "number": graph.bill_number(row["number"]),
            "is_law": bool(row.get("is_law")), "chapter": laws[0] if laws else None,
            "policy_area": row.get("policy_area"), "is_state_bill": True,
            "latest_action": row.get("latest_action"),
            "latest_action_date": _iso(row.get("latest_action_date")),
            "sponsor": row.get("sponsor_name"), "stage": row.get("stage"),
            "path": f"/state/{st}/{row.get('session')}/{row['bill_type']}/{row['number']}",
        }
    return {
        "package_id": f"BILLS-{row['congress']}{row['bill_type']}{row['number']}",
        "title": row.get("title") or f"{row['bill_type'].upper()} {row['number']}",
        "date_issued": row["introduced"].isoformat() if row.get("introduced") else "",
        "congress": row["congress"],
        "type": row["bill_type"],
        "number": int(row["number"]),
        "is_law": bool(row.get("is_law")),
        "law_number": laws[0].split("-")[-1] if laws else None,
        "policy_area": row.get("policy_area"),
    }


def fts_terms(question):
    """The question's content words, each with the spellings a bill may use
    instead: search_rank folds "artificial intelligence" to "ai" for its
    title match, and a summary usually spells the phrase out. Pure."""
    from search.search_rank import _ALIASES, query_stems
    return [[s] + [long for long, short in _ALIASES if short == s] for s in query_stems(question)]


def _tsquery_sql(terms, op):
    """A tsquery expression over fts_terms, joined by op (&& or ||), with
    one placeholder per spelling."""
    one = lambda alts: "(" + " || ".join("phraseto_tsquery('english', %s)" for _ in alts) + ")"
    return f" {op} ".join(one(t) for t in terms), [a for t in terms for a in t]


def _text_hits(conn, cur, terms, where, args):
    """Bills whose text holds every content word of the question, weightiest
    first: the bill that only says "catalytic converter" in section 14.
    Ranked by salience, not ts_rank: ranking would read every matching
    text, and a common word matches a hundred thousand. Bounded at 1.5 s;
    past it, no text list (fail-open: the other lists still answer)."""
    import psycopg
    expr, targs = _tsquery_sql(terms, "&&")
    try:
        with conn.transaction():
            cur.execute("SET LOCAL statement_timeout = 1500")
            cur.execute(f"""
                SELECT t.instrument_id FROM bill_text_doc t JOIN bill_doc d USING (instrument_id),
                       (SELECT {expr} AS q) q
                WHERE t.tsv @@ q.q{where}
                ORDER BY d.salience DESC NULLS LAST, t.instrument_id LIMIT %s""", [*targs, *args, _DEPTH])
            hits = [r["instrument_id"] for r in cur.fetchall()]
            cur.execute("SET LOCAL statement_timeout = 0")
            return hits
    except (psycopg.errors.QueryCanceled, psycopg.errors.UndefinedTable):
        return []


def search(question, congresses=None, limit=10, laws_only=False, jurisdiction=US_DIV, sessions=None,
           with_salience=True, with_text=False):
    """Bills for a question: full-text and nearest-vector lists over
    bill_doc, fused by rank. Local only; nothing leaves the server.

    The full-text list takes the bills with every content word first, then
    tops up with bills that have any of them: a plain AND drops every bill
    that lacks one incidental word of a spoken question, and a plain OR lets
    a long summary that repeats one common word ("regulation") outrank the
    bills about the whole question. Every search is one jurisdiction's
    (federal by default), so a state bill never answers a federal question
    or the reverse. Fail-closed: a database or model error raises, and the
    caller decides what the user sees.

    with_text adds the bill-text list (_text_hits). Off until the Tier 2
    set shows it helps: the text index was still building when salience
    and the relevance check shipped (2026-09-29)."""
    from psycopg.rows import dict_row
    from correspondence.db import _get_pool
    terms = fts_terms(question)
    vec = json.dumps(embed_query([question])[0])
    # None is every jurisdiction at once: the eval's pool for "which states…"
    # asks, which name no single state. No route passes it.
    where, args = (" AND d.jurisdiction = %s", [jurisdiction]) if jurisdiction else ("", [])
    if sessions:
        where += " AND d.session = ANY(%s)"
        args.append(list(sessions))
    if congresses:
        where += " AND d.congress = ANY(%s)"
        args.append(list(congresses))
    if laws_only:
        where += " AND d.is_law"
    with _get_pool().connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        fts = []
        for op in (("&&", "||") if len(terms) > 1 else ("&&",)) if terms else ():
            expr, targs = _tsquery_sql(terms, op)
            cur.execute(f"""
                SELECT d.instrument_id FROM bill_doc d, (SELECT {expr} AS q) q
                WHERE d.tsv @@ q.q{where} AND NOT d.instrument_id = ANY(%s::text[])
                ORDER BY ts_rank_cd(d.tsv, q.q, 1) DESC, d.instrument_id LIMIT %s""",
                        [*targs, *args, fts, _DEPTH - len(fts)])
            fts += [r["instrument_id"] for r in cur.fetchall()]
            if len(fts) >= _DEPTH:
                break
        cur.execute("SET LOCAL hnsw.ef_search = 100")
        # A Congress or law filter removes most neighbours; iterative scan
        # keeps walking the graph until enough survive the filter.
        cur.execute("SET LOCAL hnsw.iterative_scan = relaxed_order")
        cur.execute(f"""
            SELECT e.instrument_id FROM bill_embedding e JOIN bill_doc d USING (instrument_id)
            WHERE e.space = %s{where}
            ORDER BY e.embedding <=> %s::halfvec LIMIT %s""", [SPACE, *args, vec, _DEPTH])
        near = [r["instrument_id"] for r in cur.fetchall()]
        lists = [fts, near]
        if with_text and terms:
            lists.append(_text_hits(conn, cur, terms, where, args))
        if with_salience and (fts or near):
            # The graph's weight, as a third ranking of the same candidates
            # only: salience knows nothing of the question, so over every
            # bill it would answer "housing" with the tax code.
            cur.execute("""SELECT instrument_id FROM bill_doc WHERE instrument_id = ANY(%s)
                           ORDER BY salience DESC NULLS LAST, instrument_id""", (list(set(fts) | set(near)),))
            lists.append([r["instrument_id"] for r in cur.fetchall()])
        ids = rrf(*lists)[:limit]
        cur.execute("""SELECT instrument_id, congress, bill_type, number, title, introduced,
                              policy_area, is_law, law_numbers, jurisdiction, session,
                              latest_action, latest_action_date, sponsor_name, stage
                       FROM bill_doc WHERE instrument_id = ANY(%s)""", (ids,))
        rows = {r["instrument_id"]: r for r in cur.fetchall()}
    return [as_result(rows[i]) for i in ids if i in rows]


# The index holds every bill from the 108th Congress (2003) on; before it
# only GovInfo and Congress.gov do, and search no longer asks them. A bill
# number still opens its page from Congress.gov.
FIRST_INDEXED_CONGRESS = 108
BEFORE_INDEX = ("Bill search covers the 108th Congress (2003) on; earlier bills are not "
                "searchable here yet. A bill number still opens that bill's page.")


def index_filters(structured):
    """The router's question as the index's filters: (congresses, laws_only,
    partly_before, wholly_before). Congresses before the index are dropped,
    and None means every Congress in it (a full-history ask). Pure."""
    if structured.get("full_history"):
        before = structured.get("before_congress")
        asked = list(range(1, before)) if before else None
    elif structured.get("query_subtype") == "named_entity" or not structured.get("time_filter"):
        # The router fills a window of Congresses for every question ("last 5
        # years", in practice the last two); only a question that names a time
        # (a year, a president, "recent") sets time_filter. Without one, every
        # Congress: on the Tier 2 tuning rows the window cut nDCG@10 from 0.56
        # to 0.51 and recall@10 from 0.35 to 0.22 (2026-09-29). A name picks
        # out its bill in any Congress (the 2022 Inflation Reduction Act).
        asked = None
    else:
        asked = list(structured.get("congress_numbers") or []) or None
    congresses = [c for c in asked if c >= FIRST_INDEXED_CONGRESS] if asked else None
    partly = bool(structured.get("full_history")) or any(c < FIRST_INDEXED_CONGRESS for c in asked or [])
    wholly = asked is not None and not congresses
    # The router marks an enacted ask with either field; one alone is enough.
    laws_only = structured.get("query_subtype") == "enacted" or structured.get("status") == "enacted"
    return congresses or None, laws_only, partly, wholly


def laws_named(name, jurisdiction=US_DIV, limit=3):
    """The laws whose title contains an act's name, oldest first: the
    answer to "the Affordable Care Act". The ranked search cannot promise
    it — a landmark law's document is long (H.R. 3590 lists hundreds of
    subjects), and both lists discount length, so for its own name it
    ranks below thirty bills that only mention it. Local; fail-closed."""
    from psycopg.rows import dict_row
    from correspondence.db import _get_pool
    if not (name or "").strip():
        return []
    with _get_pool().connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute("""SELECT instrument_id, congress, bill_type, number, title, introduced,
                              policy_area, is_law, law_numbers, jurisdiction, session,
                              latest_action, latest_action_date, sponsor_name, stage
                       FROM bill_doc WHERE jurisdiction = %s AND is_law AND title ILIKE %s
                       ORDER BY introduced NULLS LAST, instrument_id LIMIT %s""",
                    [jurisdiction, "%" + name.strip().replace("%", "").replace("_", r"\_") + "%", limit])
        return [as_result(r) for r in cur.fetchall()]


def recent(jurisdiction=US_DIV, congresses=None, laws_only=False, limit=10):
    """The newest bills of one jurisdiction by their latest action, for a
    question with no topic ("give me a bill"). A bill with no action date
    goes last, not first (Postgres sorts NULL first in DESC). Local only;
    fail-closed like search."""
    from psycopg.rows import dict_row
    from correspondence.db import _get_pool
    where, args = "jurisdiction = %s", [jurisdiction]
    if congresses:
        where += " AND congress = ANY(%s)"
        args.append(list(congresses))
    if laws_only:
        where += " AND is_law"
    with _get_pool().connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(f"""SELECT instrument_id, congress, bill_type, number, title, introduced,
                               policy_area, is_law, law_numbers, jurisdiction, session,
                               latest_action, latest_action_date, sponsor_name, stage
                        FROM bill_doc WHERE {where}
                        ORDER BY latest_action_date DESC NULLS LAST, instrument_id LIMIT %s""", [*args, limit])
        return [as_result(r) for r in cur.fetchall()]


if __name__ == "__main__":
    cmd, args = (sys.argv[1] if len(sys.argv) > 1 else ""), sys.argv[2:]
    if cmd == "docs":
        print(json.dumps(load_docs([int(a) for a in args] or None)))
        import graph
        for st in ([] if args else sorted(graph.LEGISLATURES)):
            print(st, json.dumps(load_state_docs(st)))
    elif cmd == "text":
        load_texts(args or None)
    elif cmd == "salience":
        t0 = time.time()
        print(f"{compute_salience()} bill(s) re-weighted in {time.time() - t0:.0f} s")
    elif cmd == "embed":
        n, t = embed_missing(int(args[0]) if args else None)
        print(f"{n} document(s) embedded, {t:,} token(s)")
    elif cmd == "fetch-model":
        print(fetch_model())
    elif cmd == "query":
        t0 = time.time()
        for iid, title, sim in knn(" ".join(args)):
            print(f"{sim:.3f}  {iid}  {title[:90]}")
        print(f"{(time.time() - t0) * 1000:.0f} ms")
    elif cmd == "search":
        embed_query(["warm up"])
        t0 = time.time()
        for r in search(" ".join(args)):
            print(f"{r['congress']} {r['type'].upper()} {r['number']:<6} {'LAW ' if r['is_law'] else ''}{r['title'][:90]}")
        print(f"{(time.time() - t0) * 1000:.0f} ms")
    elif cmd == "latency":
        embed_query(["warm up"])
        qs = ["what did congress do about housing affordability", "veterans health care",
              "who voted on the infrastructure bill", "tariffs on steel", "student loan forgiveness"] * 4
        ts = []
        for q in qs:
            t0 = time.time()
            embed_query([q])
            ts.append((time.time() - t0) * 1000)
        ts.sort()
        print(f"query embedding on this CPU: median {ts[len(ts) // 2]:.0f} ms, p95 {ts[int(len(ts) * .95)]:.0f} ms")
    else:
        print(__doc__)
