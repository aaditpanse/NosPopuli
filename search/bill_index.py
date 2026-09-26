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
    python -m search.bill_index latency                # query embedding time here
"""

import json
import os
import pathlib
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
            fp = manifest.read_text()
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
                   law_numbers TEXT[], summary TEXT, doc TEXT, doc_sha TEXT) ON COMMIT DROP""")
    n = 0
    with cur.copy("COPY stage_doc FROM STDIN") as cp:
        for d in docs:
            cp.write_row((d["instrument_id"], d["congress"], d["bill_type"], d["number"], d["title"],
                          d["introduced"], d["policy_area"], d["subjects"], d["is_law"], d["law_numbers"],
                          d["summary"], d["doc"], d["doc_sha"]))
            n += 1
    cur.execute("""
        INSERT INTO bill_doc (instrument_id, congress, bill_type, number, title, introduced, policy_area,
                              subjects, is_law, law_numbers, summary, doc, doc_sha, updated_at)
        SELECT DISTINCT ON (instrument_id) instrument_id, congress, bill_type, number, title, introduced,
               policy_area, subjects, is_law, law_numbers, summary, doc, doc_sha, NOW()
        FROM stage_doc
        ON CONFLICT (instrument_id) DO UPDATE SET
            title = excluded.title, introduced = excluded.introduced, policy_area = excluded.policy_area,
            subjects = excluded.subjects, is_law = excluded.is_law, law_numbers = excluded.law_numbers,
            summary = excluded.summary, doc = excluded.doc, doc_sha = excluded.doc_sha, updated_at = NOW()
        WHERE bill_doc.doc_sha IS DISTINCT FROM excluded.doc_sha""")
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


if __name__ == "__main__":
    cmd, args = (sys.argv[1] if len(sys.argv) > 1 else ""), sys.argv[2:]
    if cmd == "docs":
        print(json.dumps(load_docs([int(a) for a in args] or None)))
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
