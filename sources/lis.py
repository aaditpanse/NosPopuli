"""Virginia LIS: the General Assembly's own files, and bill text.

The Legislative Information System publishes a session's bills, history and
roll calls as CSV, refreshed daily, from the 2024 session on
(lis.blob.core.windows.net/lisfiles/<code>/, the code from
graph.lis_session). Open States scrapes the same legislature and is the
record (sources/openstates.py); these files are the check on it
(plan of 2026-09-27):
- a member's term is certified when the legislature's own roll call has
  them voting inside it (graph.load_state);
- an Open States vote the LIS roll call contradicts becomes advisory;
- history rows newer than the monthly record show as advisory timeline
  rows.
They never write the record.

VOTE.CSV names no bill. HISTORY.CSV does: a row whose refid is a vote id
ties that vote to a bill, a date and a chamber (the description's first
letter). A vote no history row names is counted and left out of the check.

Bill text for every version Open States lists is fetched once, stored as
published (gzipped), and a PDF-only version also gets its text extracted.

Run on the server, not in a request:
    python -m sources.lis sync va          # CSVs for each LIS session -> derived/states/va/lis-<session>.json
    python -m sources.lis text va 2026     # text for one session's versions (all sessions if none named)
"""

import csv
import datetime
import gzip
import hashlib
import io
import json
import os
import pathlib
import re
import sys
import time

_HERE = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_HERE))
import graph  # noqa: E402  — DATA_DIR, LEGISLATURES, lis_session

BASE = "https://lis.blob.core.windows.net/lisfiles"
FILES = ("BILLS.CSV", "HISTORY.CSV", "VOTE.CSV")
_UA = "NosPopuli bulk sync (nospopuli.org)"
# LIS's vote letters: X is "not voting", A "abstain".
POSITION = {"Y": "yes", "N": "no", "X": "not voting", "A": "abstain"}


def _session():
    import requests
    s = requests.Session()
    s.headers["User-Agent"] = _UA
    return s


def sync_csvs(code, session_=None, errors=None):
    """The session's CSVs, each fetched only when LIS's copy is newer
    (If-Modified-Since). Fail-open per file: the old copy stays and the
    failure is recorded. Returns the names downloaded."""
    s = session_ or _session()
    errors = [] if errors is None else errors
    raw = graph.DATA_DIR / "raw" / "lis" / code
    raw.mkdir(parents=True, exist_ok=True)
    manifest_path = raw / "manifest.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    fetched = []
    for name in FILES:
        headers = {"If-Modified-Since": manifest[name]} if name in manifest and (raw / name).exists() else {}
        try:
            r = s.get(f"{BASE}/{code}/{name}", headers=headers, timeout=120)
            if r.status_code == 304:
                continue
            if r.status_code == 404 and name == "VOTE.CSV":
                # A session that has not sat yet (prefiling) has bills and
                # history but no roll calls: an empty file, not a failure.
                (raw / name).write_bytes(b"")
                continue
            r.raise_for_status()
        except Exception as e:
            errors.append(f"{code}/{name}: {type(e).__name__}")
            continue
        tmp = raw / (name + ".part")
        tmp.write_bytes(r.content)
        os.replace(tmp, raw / name)
        manifest[name] = r.headers.get("Last-Modified", "")
        manifest_path.write_text(json.dumps(manifest, indent=1, sort_keys=True))
        fetched.append(name)
    return fetched


def _date(text):
    """LIS's date → ISO. The 2025 files on write M/D/YYYY, the 2024 files
    2023-11-20T00:00:00. Pure."""
    text = (text or "").strip()
    for fmt, cut in (("%m/%d/%Y", None), ("%Y-%m-%d", 10)):
        try:
            return datetime.datetime.strptime(text[:cut] if cut else text, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _bill(lis_id):
    """'HB1' → 'hb/1' (Open States' key). Pure."""
    m = re.fullmatch(r"([A-Z]+)0*(\d+)", (lis_id or "").strip())
    return f"{m.group(1).lower()}/{m.group(2)}" if m else None


def session_record(bills_csv, history_csv, vote_csv):
    """The three CSVs → {"bills": {key: {…flags, text docs}}, "history":
    {key: [{date, chamber, description, refid}]}, "votes": [{id, bill,
    chamber, date, description, positions {member id: yes|no|…}}],
    "unlinked_votes": n}. A vote is linked to a bill through the history
    row that names it; chamber is that row's H or S. Pure."""
    bills = {}
    for r in csv.DictReader(io.StringIO(bills_csv), skipinitialspace=True):
        k = _bill(r["Bill_id"])
        if not k:
            continue
        docs = [r.get(f"Full_text_doc{i}") for i in range(1, 7)]
        bills[k] = {"description": r["Bill_description"], "patron": r["Patron_id"] or None,
                    "introduced": _date(r["Introduction_date"]), "chapter": r["Chapter_id"] or None,
                    "passed": r["Passed"] == "Y", "failed": r["Failed"] == "Y", "approved": r["Approved"] == "Y",
                    "vetoed": r["Vetoed"] == "Y", "carried_over": r["Carried_over"] == "Y",
                    "text_docs": [d for d in docs if d]}
    history, by_ref = {}, {}
    for r in csv.DictReader(io.StringIO(history_csv), skipinitialspace=True):
        k = _bill(r["Bill_id"])
        desc = (r["History_description"] or "").strip()
        m = re.match(r"^([HS])\s+(.*)$", desc)
        row = {"date": _date(r["History_date"]), "chamber": {"H": "lower", "S": "upper"}.get(m.group(1)) if m else None,
               # 2024's refids are space-padded to 20 characters.
               "description": m.group(2) if m else desc, "refid": (r["History_refid"] or "").strip() or None}
        if k:
            history.setdefault(k, []).append(row)
            if row["refid"]:
                by_ref.setdefault(row["refid"], (k, row))
    votes, unlinked, empty = [], 0, 0
    for r in csv.reader(io.StringIO(vote_csv), skipinitialspace=True):
        if len(r) < 3:
            # The file's first line is a count ("10458X"); any other short
            # row is a vote with no member recorded, counted, not dropped.
            empty += bool(r) and not r[0].endswith("X")
            continue
        linked = by_ref.get(r[0].strip())
        if not linked:
            unlinked += 1
            continue
        k, row = linked
        votes.append({"id": r[0], "bill": k, "chamber": row["chamber"], "date": row["date"],
                      "description": row["description"],
                      "positions": {m: POSITION.get(v, v) for m, v in zip(r[1::2], r[2::2]) if m}})
    votes.sort(key=lambda v: (v["date"] or "", v["bill"], v["id"]))
    return {"bills": bills, "history": history, "votes": votes, "unlinked_votes": unlinked, "empty_votes": empty}


def sync(st, today=None):
    """Every LIS session of the state's Open States sessions: download what
    changed and write derived/states/<st>/lis-<session>.json. Returns
    {session: meta}."""
    s = _session()
    out = {}
    sessions = sorted({p.stem.removeprefix("bills-") for p in graph.data_glob("state_bills", state=st)},
                      key=graph.session_key)
    for sid in sessions:
        code = graph.lis_session(st, sid)
        if not code:
            continue
        errors = []
        fetched = sync_csvs(code, s, errors)
        raw = graph.DATA_DIR / "raw" / "lis" / code
        dest = graph.data_path("state_lis", state=st, session=sid)
        if not all((raw / f).exists() for f in FILES):
            out[sid] = {"code": code, "errors": errors + ["not all three CSVs on disk; nothing written"]}
            continue
        if not fetched and dest.exists():
            out[sid] = {"code": code, "unchanged": True, "errors": errors}
            continue
        rec = session_record(*((raw / f).read_bytes().decode("latin-1") for f in FILES))
        manifest = json.loads((raw / "manifest.json").read_text())
        meta = {"state": st, "session": sid, "code": code, "source": f"{BASE}/{code}/",
                "files": {f: manifest.get(f) for f in FILES}, "bills": len(rec["bills"]),
                "votes": len(rec["votes"]), "unlinked_votes": rec["unlinked_votes"], "empty_votes": rec["empty_votes"],
                "extracted": (today or datetime.date.today()).isoformat()}
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".part")
        tmp.write_text(json.dumps({"meta": meta, **rec}, separators=(",", ":"), sort_keys=True))
        os.replace(tmp, dest)
        out[sid] = {**meta, "errors": errors}
    return out


# -------------------------------------------------------------------- text

def text_name(url):
    """The file a version link is stored as: the blob's own name, or a
    readable name for a legacy CGI link. Pure."""
    m = re.search(r"/files/(\d+\.(?:HTML|PDF))$", url or "", re.I)
    if m:
        return m.group(1)
    m = re.search(r"legp604\.exe\?(\w+)\+ful\+(\w+)(?:\+(\w+))?", url or "")
    if m:
        return f"legp604-{m.group(1)}-{m.group(2)}{'-' + m.group(3) if m.group(3) else ''}.html"
    return hashlib.sha1((url or "").encode()).hexdigest()[:16] + ".bin"


def pick_link(links):
    """The one link to store for a version: HTML, else PDF, else none."""
    for kind in ("text/html", "application/pdf"):
        for ln in links:
            if ln.get("media_type") == kind:
                return ln
    return None


def pdf_text(data):
    """A PDF's text, page by page, with the existing pypdf. None when it
    has none (a scan)."""
    from pypdf import PdfReader
    try:
        text = "\n".join((p.extract_text() or "") for p in PdfReader(io.BytesIO(data)).pages).strip()
    except Exception:
        return None
    return text or None


def sync_text(st, sid, session_=None, pause=0.2, limit=None):
    """Fetch the text of every version of one session once. Stored gzipped
    under raw/lis/text/<session>/ with a manifest; a PDF also gets a
    .txt.gz of its extracted text. A failure is recorded and retried on the
    next run; a stored file is never fetched again. Returns counts."""
    s = session_ or _session()
    bills = json.loads(graph.data_path("state_bills", state=st, session=sid).read_text())["bills"]
    root = graph.DATA_DIR / "raw" / "lis" / "text" / sid
    root.mkdir(parents=True, exist_ok=True)
    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    fetched = failed = kept = 0
    try:
        for key, b in sorted(bills.items()):
            for v in b["versions"]:
                link = pick_link(v["links"])
                if not link:
                    continue
                name = text_name(link["url"])
                if manifest.get(name, {}).get("status") == "ok" and (root / (name + ".gz")).exists():
                    kept += 1
                    continue
                if limit is not None and fetched + failed >= limit:
                    raise StopIteration
                try:
                    r = s.get(link["url"], timeout=60)
                    r.raise_for_status()
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
                except Exception as e:
                    entry = {"status": "error", "url": link["url"], "bill": key, "version": v["name"],
                             "error": type(e).__name__, "tried": datetime.date.today().isoformat()}
                    failed += 1
                manifest[name] = entry
                if (fetched + failed) % 200 == 0:
                    manifest_path.write_text(json.dumps(manifest, sort_keys=True))
                time.sleep(pause)
    except StopIteration:
        pass
    finally:
        manifest_path.write_text(json.dumps(manifest, sort_keys=True))
    return {"session": sid, "fetched": fetched, "failed": failed, "kept": kept}


if __name__ == "__main__":
    cmd, args = (sys.argv[1] if len(sys.argv) > 1 else ""), sys.argv[2:]
    if cmd == "sync":
        for st in args or [k for k, v in graph.LEGISLATURES.items() if v.get("lis_from")]:
            for sid, m in sync(st).items():
                print(st, sid, json.dumps({k: m[k] for k in ("code", "bills", "votes", "unlinked_votes", "empty_votes", "unchanged",
                                                               "errors") if k in m}))
    elif cmd == "text":
        st, sessions = args[0], args[1:]
        if not sessions:
            sessions = sorted({p.stem.removeprefix("bills-") for p in graph.data_glob("state_bills", state=st)},
                              key=graph.session_key)
        for sid in sessions:
            print(json.dumps(sync_text(st, sid)))
    else:
        print(__doc__)
