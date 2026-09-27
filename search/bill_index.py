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
US_DIV = "ocd-division/country:us"


def state_doc(st, session, key, b, people_names=None):
    """One state bill (a bills-<session>.json record) → its search
    document: identifier and title, other titles, subjects, and the
    legislature's own summary (Open States abstracts, the title one left
    out). Pure."""
    import graph
    itype, number = key.split("/")
    summaries = [a["abstract"] for a in b.get("abstracts") or [] if (a.get("note") or "") != "title"]
    summary = " ".join(summaries)[:12000]
    chapter = next((m.group(1) for a in b.get("actions") or []
                    for m in [re.search(r"Chapter (\d+)", a.get("description") or "", re.I)]
                    if m and ("became-law" in a["classification"] or "executive-signature" in a["classification"])), None)
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
    return {"instrument_id": graph.state_instrument_id(st, session, key), "congress": None,
            "bill_type": itype, "number": number, "title": b["title"], "introduced": b.get("first_action_date"),
            "policy_area": (b.get("subjects") or [None])[0], "subjects": b.get("subjects") or [],
            "is_law": bool(chapter), "law_numbers": [chapter] if chapter else [], "summary": summary, "doc": doc,
            "doc_sha": hashlib.sha1(doc.encode()).hexdigest(), "latest_action": b.get("latest_action"),
            "latest_action_date": b.get("latest_action_date"), "sponsor_bioguide": None,
            "sponsor_name": primary["name"] if primary else None,
            "jurisdiction": graph.state_div(st), "session": session}


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
            st_ = path.stat()
            fp = f"{DOCS_VERSION}\n{st_.st_size} {st_.st_mtime_ns}"
            scope = f"docs/{st}/{sid}"
            with conn.transaction(), conn.cursor() as cur:
                cur.execute("SELECT fingerprint FROM graph_scope WHERE scope = %s", (scope,))
                row = cur.fetchone()
                if row and row[0] == fp and not force:
                    continue
                bills = json.loads(path.read_text())["bills"]
                out[sid] = _write_docs(cur, (state_doc(st, sid, k, b) for k, b in bills.items()))
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
                   jurisdiction TEXT, session TEXT) ON COMMIT DROP""")
    n = 0
    with cur.copy("COPY stage_doc FROM STDIN") as cp:
        for d in docs:
            cp.write_row((d["instrument_id"], d["congress"], d["bill_type"], d["number"], d["title"],
                          d["introduced"], d["policy_area"], d["subjects"], d["is_law"], d["law_numbers"],
                          d["summary"], d["doc"], d["doc_sha"], d["latest_action"], d["latest_action_date"],
                          d["sponsor_bioguide"], d["sponsor_name"], d.get("jurisdiction") or US_DIV,
                          d.get("session")))
            n += 1
    # A new action changes a row without changing its document, so the
    # embedding (keyed on doc_sha) stays and only the columns move.
    cur.execute("""
        INSERT INTO bill_doc (instrument_id, congress, bill_type, number, title, introduced, policy_area,
                              subjects, is_law, law_numbers, summary, doc, doc_sha, latest_action,
                              latest_action_date, sponsor_bioguide, sponsor_name, jurisdiction, session, updated_at)
        SELECT DISTINCT ON (instrument_id) instrument_id, congress, bill_type, number, title, introduced,
               policy_area, subjects, is_law, law_numbers, summary, doc, doc_sha, latest_action,
               latest_action_date, sponsor_bioguide, sponsor_name, jurisdiction, session, NOW()
        FROM stage_doc
        ON CONFLICT (instrument_id) DO UPDATE SET
            title = excluded.title, introduced = excluded.introduced, policy_area = excluded.policy_area,
            subjects = excluded.subjects, is_law = excluded.is_law, law_numbers = excluded.law_numbers,
            summary = excluded.summary, doc = excluded.doc, doc_sha = excluded.doc_sha,
            latest_action = excluded.latest_action, latest_action_date = excluded.latest_action_date,
            sponsor_bioguide = excluded.sponsor_bioguide, sponsor_name = excluded.sponsor_name,
            jurisdiction = excluded.jurisdiction, session = excluded.session, updated_at = NOW()
        WHERE (bill_doc.doc_sha, bill_doc.latest_action, bill_doc.latest_action_date, bill_doc.sponsor_bioguide,
               bill_doc.sponsor_name, bill_doc.jurisdiction, bill_doc.session)
              IS DISTINCT FROM (excluded.doc_sha, excluded.latest_action, excluded.latest_action_date,
                                excluded.sponsor_bioguide, excluded.sponsor_name, excluded.jurisdiction,
                                excluded.session)""")
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
            "type": row["bill_type"], "number": int(row["number"]),
            "is_law": bool(row.get("is_law")), "chapter": laws[0] if laws else None,
            "policy_area": row.get("policy_area"), "is_state_bill": True,
            "latest_action": row.get("latest_action"),
            "latest_action_date": _iso(row.get("latest_action_date")),
            "sponsor": row.get("sponsor_name"),
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


def search(question, congresses=None, limit=10, laws_only=False, jurisdiction=US_DIV, sessions=None):
    """Bills for a question: full-text and nearest-vector lists over
    bill_doc, fused by rank. Local only; nothing leaves the server.

    The full-text list takes the bills with every content word first, then
    tops up with bills that have any of them: a plain AND drops every bill
    that lacks one incidental word of a spoken question, and a plain OR lets
    a long summary that repeats one common word ("regulation") outrank the
    bills about the whole question. Every search is one jurisdiction's
    (federal by default), so a state bill never answers a federal question
    or the reverse. Fail-closed: a database or model error raises, and the
    caller decides what the user sees."""
    from psycopg.rows import dict_row
    from correspondence.db import _get_pool
    terms = fts_terms(question)
    vec = json.dumps(embed_query([question])[0])
    where, args = " AND d.jurisdiction = %s", [jurisdiction]
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
        ids = rrf(fts, near)[:limit]
        cur.execute("""SELECT instrument_id, congress, bill_type, number, title, introduced,
                              policy_area, is_law, law_numbers, jurisdiction, session,
                              latest_action, latest_action_date, sponsor_name
                       FROM bill_doc WHERE instrument_id = ANY(%s)""", (ids,))
        rows = {r["instrument_id"]: r for r in cur.fetchall()}
    return [as_result(rows[i]) for i in ids if i in rows]


if __name__ == "__main__":
    cmd, args = (sys.argv[1] if len(sys.argv) > 1 else ""), sys.argv[2:]
    if cmd == "docs":
        print(json.dumps(load_docs([int(a) for a in args] or None)))
        import graph
        for st in ([] if args else sorted(graph.LEGISLATURES)):
            print(st, json.dumps(load_state_docs(st)))
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
