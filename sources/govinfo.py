"""GovInfo bulk data: the federal bill records, read from files.

BILLSTATUS is GPO's publication of the same record Congress.gov serves one
call at a time: title, sponsors, cosponsors, actions, committees, reports,
related bills, laws. One zip per Congress and bill type, back to the 108th
(2003), all in schema 3.0.0. The sync downloads a zip only when GovInfo's
listing shows a new size or modification time, so a second run fetches
nothing. `bills-<congress>.json` in graph.DATA_DIR is the output; the graph
reads it and never calls Congress.gov for a bill record.

A committee report's date and committees come from its CRPT package
metadata (MODS). BILLSTATUS carries only the citation. A report's metadata
does not change once published, so each is fetched once and kept.

Run on the server, not in a request:
    python -m sources.govinfo billstatus            # 108th → current
    python -m sources.govinfo billstatus 119 118    # some Congresses
    python -m sources.govinfo text                  # bill typescript: all versions of the
                                                    # 118th–119th, enacted text back to the 108th

The CRS summaries are in BILLSTATUS too, back to the 108th; the separate
BILLSUM collection starts at the 113th, so it is not downloaded.
"""

import datetime
import io
import json
import pathlib
import re
import sys
import time
import xml.etree.ElementTree as ET
import zipfile

_HERE = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_HERE))
import graph  # noqa: E402  — DATA_DIR, _presidential, _report_endpoint, current_session

BULK = "https://www.govinfo.gov/bulkdata"
LISTING = "https://www.govinfo.gov/bulkdata/json"
MODS = "https://www.govinfo.gov/metadata/pkg/CRPT-{c}{kind}{n}/mods.xml"
FIRST_CONGRESS = 108
BILL_TYPES = ("hr", "s", "hres", "sres", "hjres", "sjres", "hconres", "sconres")
_UA = "NosPopuli bulk sync (nospopuli.org)"


def _raw(*parts):
    p = graph.DATA_DIR.joinpath("raw", *parts)
    p.mkdir(parents=True, exist_ok=True)
    return p


def _session():
    import requests
    s = requests.Session()
    s.headers["User-Agent"] = _UA
    return s


def _error(url, e):
    """What a failure records: the exception type and the URL path. Never
    the query string, which is where a key would travel."""
    return f"{url.split('?')[0]}: {type(e).__name__}"


def _write_atomic(path, data):
    """A crash mid-write must leave the previous good file, not half a
    new one."""
    tmp = path.with_name(path.name + ".part")
    tmp.write_bytes(data)
    tmp.replace(path)


# ------------------------------------------------------------------ download

def sync_billstatus_zips(congress, session_=None, errors=None):
    """Download each BILLSTATUS-<c>-<type>.zip whose listing entry (size,
    modification time) differs from the manifest. Fail-open per file: one
    failed zip is recorded and the others still sync; the file on disk is
    never emptied. Returns the names downloaded."""
    s = session_ or _session()
    errors = [] if errors is None else errors
    raw = _raw("govinfo", "BILLSTATUS", str(congress))
    manifest_path = raw / "manifest.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    fetched = []
    for itype in BILL_TYPES:
        url = f"{LISTING}/BILLSTATUS/{congress}/{itype}"
        try:
            r = s.get(url, headers={"Accept": "application/json"}, timeout=60)
            r.raise_for_status()
            files = r.json().get("files", [])
        except Exception as e:
            errors.append(_error(url, e))
            continue
        name = f"BILLSTATUS-{congress}-{itype}.zip"
        entry = next((f for f in files if f.get("name") == name), None)
        if entry is None:
            if files:   # a type with bills but no zip is GovInfo's gap, and ours to say
                errors.append(f"{url}: {len(files)} file(s) listed but no {name}")
            continue
        stamp = {"size": entry.get("size"), "modified": entry.get("formattedLastModifiedTime")}
        if manifest.get(name) == stamp and (raw / name).exists():
            continue
        try:
            z = s.get(entry["link"], timeout=600)
            z.raise_for_status()
            zipfile.ZipFile(io.BytesIO(z.content)).testzip()   # fail-closed: never keep a bad zip
            _write_atomic(raw / name, z.content)
        except Exception as e:
            errors.append(_error(entry["link"], e))
            continue
        manifest[name] = stamp
        manifest_path.write_text(json.dumps(manifest, indent=1, sort_keys=True))
        fetched.append(name)
    return fetched


def crpt_meta(endpoint, session_=None):
    """{"date", "committees"} for one committee report, from its CRPT
    package metadata, cached on disk for good. endpoint is "119/HRPT/106".
    Returns None when the metadata cannot be read (the caller records it);
    a 404 is cached as a miss so it is not asked again every day."""
    c, kind, n = endpoint.split("/")
    raw = _raw("govinfo", "CRPT", c)
    path = raw / f"{kind.lower()}{n}.json"
    if path.exists():
        cached = json.loads(path.read_text())
        if cached.get("missing"):
            return None
        if cached.get("v") == 2:
            return cached
    url = MODS.format(c=c, kind=kind.lower(), n=n)
    r = (session_ or _session()).get(url, timeout=60)
    if r.status_code == 404:
        path.write_text(json.dumps({"missing": True, "checked": datetime.date.today().isoformat()}))
        return None
    r.raise_for_status()
    meta = parse_crpt_mods(r.content)
    _write_atomic(path, json.dumps(meta).encode())
    time.sleep(0.2)
    return meta


# ------------------------------------------------------------------ parse

_M = "{http://www.loc.gov/mods/v3}"


def _committees(el):
    out = []
    for cc in el.iter(f"{_M}congCommittee"):
        code = (cc.get("authorityId") or "").lower()
        if code and code not in out:
            out.append(code)
    return out


def parse_crpt_mods(xml_bytes):
    """A report's issue date and committees, whole and per part. A report
    in parts is one package, and each part can come from a different
    committee (H. Rept. 119-168: Part 1 Agriculture, Part 2 Financial
    Services), so the whole package's committee list must not be given to
    every part. Pure."""
    root = ET.fromstring(xml_bytes)
    date = (root.findtext(f"{_M}originInfo/{_M}dateIssued") or "")[:10]
    parts = {}
    for ri in root.findall(f"{_M}relatedItem[@type='constituent']"):
        n = ri.findtext(f".//{_M}partNumber")
        if n:
            parts[n.strip()] = {"date": (ri.findtext(f".//{_M}dateIssued") or date)[:10],
                                "committees": _committees(ri)}
    return {"v": 2, "date": date, "committees": _committees(root), "parts": parts}


_PART = re.compile(r",\s*(?:Part|Book)\s+(\d+)\s*$", re.I)


def report_meta(citation, meta):
    """The date and committees for one citation of a report. An erratum
    names no committee of its own; it gets none, as Congress.gov gave it,
    so it is never a `reported` edge. A citation without a part number is
    the first part."""
    if re.search(r",\s*Errata\s*$", citation, re.I):
        return {"date": "", "committees": []}
    parts = meta.get("parts") or {}
    m = _PART.search(citation)
    part = parts.get(m.group(1) if m else "1")
    return part if part is not None else {"date": meta["date"], "committees": meta["committees"]}


def _t(el, path):
    v = el.findtext(path)
    return v.strip() if v and v.strip() else None


def parse_billstatus(xml_bytes):
    """One BILLSTATUS XML → ("hr/1", record) in the shape
    graph.fetch_instruments produced from Congress.gov, minus `reports`
    (see attach_reports). In the XML an empty list is a real zero, not a
    failed call, so every pass but `reports` is present. Pure."""
    b = ET.fromstring(xml_bytes).find("bill")
    itype = (_t(b, "type") or "").lower()
    number = _t(b, "number")
    rec = {"title": _t(b, "title"), "introduced": _t(b, "introducedDate"),
           "policy_area": _t(b, "policyArea/name"),
           "sponsors": [x for x in (_t(sp, "bioguideId") for sp in b.findall("sponsors/item")) if x],
           "laws": [{"number": _t(law, "number"), "type": _t(law, "type")} for law in b.findall("laws/item")],
           "passes": ["bill", "cosponsors", "actions", "committees", "related"]}
    rec["cosponsors"] = [{"id": _t(c, "bioguideId"), "date": _t(c, "sponsorshipDate"),
                          "original": _t(c, "isOriginalCosponsor") == "True",
                          "withdrawn": _t(c, "sponsorshipWithdrawnDate")}
                         for c in b.findall("cosponsors/item") if _t(c, "bioguideId")]
    actions = []
    for a in b.findall("actions/item"):
        act = {"date": _t(a, "actionDate"), "code": _t(a, "actionCode"),
               "type": _t(a, "type"), "text": _t(a, "text")}
        if graph._presidential(act):
            actions.append(act)
    rec["actions"] = actions
    rec["committees"] = []
    for c in b.findall("committees/item"):
        units = [(c, None)] + [(sc, _t(c, "systemCode")) for sc in c.findall("subcommittees/item")]
        for unit, parent in units:
            rec["committees"].append({
                "code": (_t(unit, "systemCode") or "").lower(), "name": _t(unit, "name"),
                "chamber": _t(c, "chamber"), "parent": parent,
                "activities": [{"name": _t(a, "name"), "date": (_t(a, "date") or "")[:10]}
                               for a in unit.findall("activities/item")]})
    rec["related"] = [{"congress": int(_t(r, "congress")) if _t(r, "congress") else None,
                       "type": (_t(r, "type") or "").lower(), "number": _t(r, "number"),
                       "title": _t(r, "title"),
                       "relationships": [{"type": _t(d, "type"), "identified_by": _t(d, "identifiedBy")}
                                         for d in r.findall("relationshipDetails/item")]}
                      for r in b.findall("relatedBills/item")]
    rec["_report_citations"] = [_t(cr, "citation") for cr in b.findall("committeeReports/committeeReport")
                                if _t(cr, "citation")]
    return f"{itype}/{number}", rec


def attach_reports(rec, meta_for, errors):
    """Resolve the record's report citations through meta_for(endpoint) →
    {"date", "committees"} or None. One entry per citation, as Congress.gov
    gave them: "Part 2" and "Book 1" are their own entries (a `reported`
    edge each) and share the report's package metadata. `reports` and its
    pass are set only when every citation resolved: a partial list would
    read as the whole."""
    reports, ok, metas = [], True, {}
    for citation in rec.pop("_report_citations", []):
        ep = graph._report_endpoint(citation, None)
        if ep is None:
            errors.append(f"unparsed report citation {citation!r}")
            ok = False
            continue
        if ep not in metas:
            try:
                metas[ep] = meta_for(ep)
            except Exception as e:
                errors.append(f"CRPT {ep}: {type(e).__name__}")
                metas[ep] = None
            if metas[ep] is None:
                errors.append(f"CRPT {ep}: no package metadata")
        if metas[ep] is None:
            ok = False
            continue
        m = report_meta(citation, metas[ep])
        reports.append({"endpoint": ep, "citation": citation,
                        "date": m["date"], "committees": m["committees"]})
    if ok:
        rec["reports"] = reports
        rec["passes"].append("reports")
    return rec


def build_bills(congress, session_=None):
    """Every bill of one Congress from the zips on disk → bills-<c>.json.
    Fail-closed: a zip that cannot be read stops the Congress and the old
    file stays, because a file missing a whole bill type would read as a
    Congress without it. Returns meta."""
    raw = _raw("govinfo", "BILLSTATUS", str(congress))
    s = session_ or _session()
    instruments, errors, files = {}, [], {}
    for itype in BILL_TYPES:
        path = raw / f"BILLSTATUS-{congress}-{itype}.zip"
        if not path.exists():
            continue
        with zipfile.ZipFile(path) as z:
            names = [n for n in z.namelist() if n.endswith(".xml")]
            files[itype] = len(names)
            for n in names:
                label, rec = parse_billstatus(z.read(n))
                instruments[label] = attach_reports(rec, lambda ep: crpt_meta(ep, s), errors)
    if not instruments:
        raise RuntimeError(f"no BILLSTATUS zips on disk for the {congress}th Congress")
    today = datetime.date.today().isoformat()
    out = {"meta": {"congress": congress, "fetched": today, "source": "govinfo BILLSTATUS + CRPT MODS",
                    "files": files, "counts": {"instruments": len(instruments)}, "errors": errors},
           "instruments": instruments}
    _write_atomic(graph.DATA_DIR / f"bills-{congress}.json",
                  json.dumps(out, separators=(",", ":"), sort_keys=True).encode())
    return out["meta"]


TEXT_CONGRESSES = (118, 119)
_TEXT = "https://www.govinfo.gov/content/pkg/{pkg}/html/{pkg}.htm"


def sync_bill_text(congress, session_=None, errors=None, pause=0.1):
    """The typescript of every published version of every bill in one
    Congress: GPO's 70-column text, which render/bill_text_format.py parses
    and the BILLS XML cannot reproduce. A version never changes once
    published, so each is fetched once and kept gzipped; the BILLS bulk
    listing names the versions. Fail-open per version: a failure is
    recorded and retried on the next run. Returns the number fetched."""
    import gzip
    s = session_ or _session()
    errors = [] if errors is None else errors
    out_dir = _raw("govinfo", "BILLS-htm", str(congress))
    have = {p.name[:-len(".htm.gz")] for p in out_dir.glob("*.htm.gz")}
    fetched = 0
    for sess in (1, 2):
        for itype in BILL_TYPES:
            url = f"{LISTING}/BILLS/{congress}/{sess}/{itype}"
            try:
                r = s.get(url, headers={"Accept": "application/json"}, timeout=60)
                if r.status_code == 404:
                    continue    # a session not begun yet, or a type with no bills
                r.raise_for_status()
                names = [f["name"] for f in r.json().get("files", [])]
            except Exception as e:
                errors.append(_error(url, e))
                continue
            for name in names:
                m = re.match(r"^(BILLS-\d+[a-z]+\d+[a-z]+)\.xml$", name)
                if not m or m.group(1) in have:
                    continue
                pkg = m.group(1)
                try:
                    t = s.get(_TEXT.format(pkg=pkg), timeout=120, allow_redirects=False)
                    t.raise_for_status()
                    if not t.content.lstrip().startswith(b"<html"):   # an error page is not a bill
                        raise ValueError("not a typescript page")
                    _write_atomic(out_dir / f"{pkg}.htm.gz", gzip.compress(t.content))
                    have.add(pkg)
                    fetched += 1
                except Exception as e:
                    errors.append(_error(_TEXT.format(pkg=pkg), e))
                time.sleep(pause)
    return fetched


def sync_law_text(congress, session_=None, errors=None, pause=0.1):
    """The enrolled typescript of every bill that became law in one
    Congress, for the Congresses before TEXT_CONGRESSES: the text of what
    was enacted, not of every draft. The BILLS bulk listing starts at the
    113th, so the laws are read from bills-<congress>.json instead; GPO
    publishes the enrolled typescript back to the 103rd. Stored beside the
    other versions, fetched once. Returns the number fetched."""
    import gzip
    s = session_ or _session()
    errors = [] if errors is None else errors
    bills_path = graph.DATA_DIR / f"bills-{congress}.json"
    if not bills_path.exists():
        raise RuntimeError(f"no bills-{congress}.json; run `python -m sources.govinfo billstatus {congress}` first")
    laws = [label for label, rec in json.loads(bills_path.read_text())["instruments"].items() if rec.get("laws")]
    out_dir = _raw("govinfo", "BILLS-htm", str(congress))
    fetched = 0
    for label in sorted(laws):
        itype, number = label.split("/")
        pkg = f"BILLS-{congress}{itype}{number}enr"
        if (out_dir / f"{pkg}.htm.gz").exists():
            continue
        try:
            t = s.get(_TEXT.format(pkg=pkg), timeout=120, allow_redirects=False)
            t.raise_for_status()
            if not t.content.lstrip().startswith(b"<html"):
                raise ValueError("not a typescript page")
            _write_atomic(out_dir / f"{pkg}.htm.gz", gzip.compress(t.content))
            fetched += 1
        except Exception as e:
            errors.append(_error(_TEXT.format(pkg=pkg), e))
        time.sleep(pause)
    return fetched


def sync(congresses=None):
    """Download what changed, then rebuild the bills file of each Congress
    whose zips changed (or that has no file yet). Returns {congress: meta}."""
    congresses = congresses or range(FIRST_CONGRESS, graph.current_session()[0] + 1)
    s = _session()
    out = {}
    for c in congresses:
        errors = []
        fetched = sync_billstatus_zips(c, s, errors)
        if fetched or not (graph.DATA_DIR / f"bills-{c}.json").exists():
            meta = build_bills(c, s)
            meta["download_errors"] = errors
            out[c] = meta
        else:
            out[c] = {"congress": c, "unchanged": True, "download_errors": errors}
    return out


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="GovInfo bulk sync")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("billstatus", help="sync BILLSTATUS zips and write bills-<c>.json")
    p.add_argument("congress", type=int, nargs="*")
    p = sub.add_parser("text", help="bill typescript: every version (118th, 119th), enacted text before")
    p.add_argument("congress", type=int, nargs="*")
    a = ap.parse_args()
    if a.cmd == "text":
        # Every version for the recent Congresses; only the enacted text before them.
        for c in a.congress or range(FIRST_CONGRESS, graph.current_session()[0] + 1):
            errs = []
            n = sync_bill_text(c, errors=errs) if c in TEXT_CONGRESSES else sync_law_text(c, errors=errs)
            print(c, f"{n} version(s) fetched", f"{len(errs)} error(s)")
            for e in errs[:10]:
                print("  -", e)
    if a.cmd == "billstatus":
        for c, meta in sync(a.congress or None).items():
            errs = meta.get("errors", []) + meta.get("download_errors", [])
            print(c, json.dumps({k: meta[k] for k in ("counts", "files", "unchanged") if k in meta}),
                  f"{len(errs)} error(s)")
            for e in errs[:10]:
                print("  -", e)
