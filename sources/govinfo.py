"""GovInfo bulk data: the federal bill records, read from files.

BILLSTATUS is GPO's publication of the same record Congress.gov serves one
call at a time: title, sponsors, cosponsors, actions, committees, reports,
related bills, laws. One zip per Congress and bill type, back to the 108th
(2003), all in schema 3.0.0. The sync downloads a zip only when GovInfo's
listing shows a new size or modification time, so a second run fetches
nothing. `derived/bills/bills-<congress>.json` is the output; the graph
reads it and never calls Congress.gov for a bill record.

A committee report's date and committees come from its CRPT package
metadata (MODS). BILLSTATUS carries only the citation. A report's metadata
does not change once published, so each is fetched once and kept.

Run on the server, not in a request:
    python -m sources.govinfo billstatus            # 108th → current
    python -m sources.govinfo billstatus 119 118    # some Congresses
    python -m sources.govinfo text                  # bill typescript, every version: the
                                                    # current and previous Congress
    python -m sources.govinfo text 108 109          # backfill older Congresses

The CRS summaries are in BILLSTATUS too, back to the 108th; the separate
BILLSUM collection starts at the 113th, so it is not downloaded.
"""

import datetime
import hashlib
import io
import json
import pathlib
import re
import sys
import threading
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


def _all(el, *paths):
    """The items at the first path that has any. BILLSTATUS schema 1.0.0
    (a few reserved bill numbers still carry it) nests its lists one level
    deeper than 3.0.0: committees/billCommittees/item, summaries/
    billSummaries/item, subjects/billSubjects/legislativeSubjects/item."""
    for p in paths:
        found = el.findall(p)
        if found:
            return found
    return []


def _bill_id(b):
    """(type, number): 3.0.0 names them type/number, 1.0.0 billType/billNumber."""
    return (_t(b, "type") or _t(b, "billType") or "").lower(), _t(b, "number") or _t(b, "billNumber")


def _t(el, path):
    v = el.findtext(path)
    return v.strip() if v and v.strip() else None


def parse_billstatus(xml_bytes):
    """One BILLSTATUS XML → ("hr/1", record) in the shape
    graph.fetch_instruments produced from Congress.gov, minus `reports`
    (see attach_reports). In the XML an empty list is a real zero, not a
    failed call, so every pass but `reports` is present. Pure."""
    b = ET.fromstring(xml_bytes).find("bill")
    itype, number = _bill_id(b)
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
    for c in _all(b, "committees/item", "committees/billCommittees/item"):
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


_SUMMARY_CHARS = 12000
_TAG = re.compile(r"<[^>]+>")


def parse_bill_doc(xml_bytes, congress):
    """One BILLSTATUS XML → the bill's search document: its titles, policy
    area, subjects and latest CRS summary as plain text, for full-text
    search and one embedding per bill. The summary is cut at 12,000
    characters: the opening states what the bill does, and the tail of a
    long one (H.R. 1 of the 119th runs to 174,000) is section-by-section
    detail. Pure."""
    b = ET.fromstring(xml_bytes).find("bill")
    itype, number = _bill_id(b)
    label = f"{graph.BILL_LABEL.get(itype, itype.upper())} {number}"
    title = _t(b, "title") or ""
    others = []
    for t in b.findall("titles/item"):
        text = _t(t, "title")
        kind = (_t(t, "titleType") or "").lower()
        if text and text != title and text not in others and ("short" in kind or "popular" in kind):
            others.append(text)
    subjects = [n for n in (_t(s, "name") for s in _all(b, "subjects/legislativeSubjects/item",
                                                          "subjects/billSubjects/legislativeSubjects/item")) if n]
    summaries = sorted(_all(b, "summaries/summary", "summaries/billSummaries/item"),
                       key=lambda s: (_t(s, "actionDate") or "", _t(s, "updateDate") or ""))
    summary = ""
    if summaries:
        raw = _t(summaries[-1], "text") or ""
        summary = re.sub(r"\s+", " ", _TAG.sub(" ", raw).replace("&nbsp;", " ")).strip()[:_SUMMARY_CHARS]
    policy = _t(b, "policyArea/name")
    laws = [_t(law, "number") for law in b.findall("laws/item") if _t(law, "number")]
    parts = [f"{label}: {title}"]
    if others:
        parts.append("Also known as: " + "; ".join(others[:8]))
    if policy:
        parts.append(f"Policy area: {policy}")
    if subjects:
        parts.append("Subjects: " + "; ".join(subjects[:40]))
    if summary:
        parts.append(f"Summary: {summary}")
    doc = "\n".join(parts)
    return {"instrument_id": f"instrument/us/{congress}/{itype}/{number}", "congress": congress,
            "bill_type": itype, "number": number, "title": title, "introduced": _t(b, "introducedDate"),
            "policy_area": policy, "subjects": subjects, "is_law": bool(laws), "law_numbers": laws,
            "summary": summary, "doc": doc,
            "doc_sha": hashlib.sha1(doc.encode()).hexdigest()}


def bill_docs(congress):
    """Every bill's search document for one Congress, read from the
    BILLSTATUS zips on disk one file at a time."""
    raw = _raw("govinfo", "BILLSTATUS", str(congress))
    for itype in BILL_TYPES:
        path = raw / f"BILLSTATUS-{congress}-{itype}.zip"
        if not path.exists():
            continue
        with zipfile.ZipFile(path) as z:
            for n in z.namelist():
                if n.endswith(".xml"):
                    yield parse_bill_doc(z.read(n), congress)


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


def congress_gov_counts(congress, session_=None):
    """Congress.gov's own count of bills per type in one Congress: the
    independent check that GovInfo's files are all of them (8 list calls,
    limit 1). None without CONGRESS_API_KEY or on any failure — a partial
    count would read as a shortfall."""
    import os
    key = os.getenv("CONGRESS_API_KEY")
    if not key:
        return None
    s = session_ or _session()
    out = {}
    for itype in BILL_TYPES:
        try:
            r = s.get(f"https://api.congress.gov/v3/bill/{congress}/{itype}",
                      params={"api_key": key, "format": "json", "limit": 1}, timeout=60)
            r.raise_for_status()
            out[itype] = (r.json().get("pagination") or {}).get("count")
        except Exception:
            return None
    return out


def add_counts(congress):
    """Put Congress.gov's counts into an existing bills-<c>.json."""
    path = graph.data_path("bills", congress=congress)
    data = json.loads(path.read_text())
    counts = congress_gov_counts(congress)
    if counts is None:
        return None
    data["meta"]["congress_gov_counts"] = counts
    data["meta"]["congress_gov_checked"] = datetime.date.today().isoformat()
    _write_atomic(path, json.dumps(data, separators=(",", ":"), sort_keys=True).encode())
    return counts


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
    cg = congress_gov_counts(congress, s)
    if cg is not None:
        out["meta"].update(congress_gov_counts=cg, congress_gov_checked=today)
    out_path = graph.data_path("bills", congress=congress)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    _write_atomic(out_path,
                  json.dumps(out, separators=(",", ":"), sort_keys=True).encode())
    return out["meta"]


_TEXT = "https://www.govinfo.gov/content/pkg/{pkg}/html/{pkg}.htm"
_SITEMAP = "https://www.govinfo.gov/sitemap/BILLS_{year}_sitemap.xml"
# The BILLS bulk listing starts at the 113th Congress; before it, GovInfo's
# yearly sitemaps are the only list of every published version.
FIRST_LISTED = 113
_PKG = re.compile(r"BILLS-(\d+)([a-z]+)(\d+)([a-z]+)")


def text_versions(congress, session_=None, errors=None):
    """Every published text version (package id, e.g. BILLS-108hr1ih) of
    one Congress's bills: from the bulk listing from the 113th on, from the
    sitemaps of the Congress's years (and the January after) before it.
    Fail-open per listing: a failed one is recorded and the rest counted."""
    s = session_ or _session()
    errors = [] if errors is None else errors
    pkgs = set()
    if congress >= FIRST_LISTED:
        for sess in (1, 2):
            for itype in BILL_TYPES:
                url = f"{LISTING}/BILLS/{congress}/{sess}/{itype}"
                try:
                    r = s.get(url, headers={"Accept": "application/json"}, timeout=60)
                    if r.status_code == 404:
                        continue    # a session not begun yet, or a type with no bills
                    r.raise_for_status()
                    pkgs.update(m.group(0) for f in r.json().get("files", [])
                                for m in [_PKG.match(f["name"])] if m)
                except Exception as e:
                    errors.append(_error(url, e))
        return pkgs
    first = 1789 + 2 * (congress - 1)
    for year in (first, first + 1, first + 2):
        url = _SITEMAP.format(year=year)
        try:
            r = s.get(url, timeout=120)
            r.raise_for_status()
            pkgs.update(m.group(0) for m in _PKG.finditer(r.text) if int(m.group(1)) == congress)
        except Exception as e:
            errors.append(_error(url, e))
    return pkgs


def sync_bill_text(congress, session_=None, errors=None, pause=0.1):
    """The typescript of every published version of every bill in one
    Congress: GPO's 70-column text, which render/bill_text_format.py parses
    and the BILLS XML cannot reproduce. A version never changes once
    published, so each is fetched once and kept gzipped. Fail-open per
    version: a failure is recorded and retried on the next run. Returns the
    number fetched."""
    import gzip
    s = session_ or _session()
    errors = [] if errors is None else errors
    out_dir = _raw("govinfo", "BILLS-htm", str(congress))
    have = {p.name[:-len(".htm.gz")] for p in out_dir.glob("*.htm.gz")}
    fetched = 0
    for pkg in sorted(text_versions(congress, s, errors) - have):
        try:
            t = s.get(_TEXT.format(pkg=pkg), timeout=120, allow_redirects=False)
            t.raise_for_status()
            if not t.content.lstrip().startswith(b"<html"):   # an error page is not a bill
                raise ValueError("not a typescript page")
            _write_atomic(out_dir / f"{pkg}.htm.gz", gzip.compress(t.content))
            fetched += 1
        except Exception as e:
            errors.append(_error(_TEXT.format(pkg=pkg), e))
        time.sleep(pause)
    return fetched


# ------------------------------------------------------- bill pages, local

def _i(v):
    """A count or number as Congress.gov types it: an int, when it is one."""
    return int(v) if v and v.isdigit() else v


def _person(el):
    """A sponsor or cosponsor in the Congress.gov API's keys and types."""
    p = {k: _t(el, k) for k in ("bioguideId", "fullName", "firstName", "middleName", "lastName",
                                "party", "state")}
    if _t(el, "district") is not None:
        p["district"] = _i(_t(el, "district"))
    return {k: v for k, v in p.items() if v is not None}


def _action(a):
    act = {k: _t(a, k) for k in ("actionDate", "actionTime", "text", "type", "actionCode")}
    act = {k: v for k, v in act.items() if v is not None}
    if a.find("sourceSystem") is not None:
        src = {"code": _i(_t(a, "sourceSystem/code")), "name": _t(a, "sourceSystem/name")}
        act["sourceSystem"] = {k: v for k, v in src.items() if v is not None}
    committees = [{"systemCode": _t(c, "systemCode"), "name": _t(c, "name")} for c in a.findall("committees/item")]
    if committees:
        act["committees"] = committees
    votes = [{"rollNumber": _i(_t(v, "rollNumber")), "url": _t(v, "url"), "chamber": _t(v, "chamber"),
              "congress": _i(_t(v, "congress")), "date": _t(v, "date"), "sessionNumber": _i(_t(v, "sessionNumber"))}
             for v in a.findall("recordedVotes/recordedVote")]
    if votes:
        act["recordedVotes"] = votes
    return act


def _latest(el):
    la = {"actionDate": _t(el, "latestAction/actionDate"), "text": _t(el, "latestAction/text")}
    return la if la["actionDate"] or la["text"] else None


def bill_json(xml_bytes):
    """One BILLSTATUS XML → what the bill page used to ask Congress.gov
    for, in the API's keys and types: {"bill", "actions", "cosponsors",
    "relatedBills", "amendments", "textVersions", "committees"}. The XML
    wraps its lists three ways (item, recordedVote, amendment), so each is
    read by name: a generic walk would drop the roll-call votes. Actions
    are newest first, as the API gave them. Pure."""
    b = ET.fromstring(xml_bytes).find("bill")
    itype, number = _bill_id(b)
    bill = {"congress": _i(_t(b, "congress")), "type": itype.upper(), "number": number,
            "title": _t(b, "title"), "introducedDate": _t(b, "introducedDate"),
            "originChamber": _t(b, "originChamber"), "originChamberCode": _t(b, "originChamberCode"),
            "updateDate": _t(b, "updateDate"), "updateDateIncludingText": _t(b, "updateDateIncludingText"),
            "latestAction": _latest(b),
            "laws": [{"type": _t(x, "type"), "number": _t(x, "number")} for x in b.findall("laws/item")],
            "sponsors": [{**_person(x), "isByRequest": _t(x, "isByRequest") or "N"}
                         for x in b.findall("sponsors/item")],
            "committeeReports": [{"citation": _t(x, "citation")}
                                 for x in b.findall("committeeReports/committeeReport") if _t(x, "citation")]}
    if _t(b, "policyArea/name"):
        bill["policyArea"] = {"name": _t(b, "policyArea/name")}
    bill = {k: v for k, v in bill.items() if v is not None}
    actions = [_action(a) for a in b.findall("actions/item")]
    actions.sort(key=lambda a: a.get("actionDate") or "", reverse=True)     # stable: XML order within a day
    cosponsor_items = b.findall("cosponsors/item")
    # The API's bill record carries the counts, not the list; the ledger's
    # story cards read bill.cosponsors.count.
    bill["cosponsors"] = {"count": sum(1 for c in cosponsor_items if not _t(c, "sponsorshipWithdrawnDate")),
                          "countIncludingWithdrawnCosponsors": len(cosponsor_items)}
    cosponsors = [{**_person(c), "sponsorshipDate": _t(c, "sponsorshipDate"),
                   "isOriginalCosponsor": _t(c, "isOriginalCosponsor") == "True",
                   **({"sponsorshipWithdrawnDate": _t(c, "sponsorshipWithdrawnDate")}
                      if _t(c, "sponsorshipWithdrawnDate") else {})}
                  for c in cosponsor_items]
    related = [{"congress": _i(_t(r, "congress")), "type": (_t(r, "type") or "").upper(),
                "number": _i(_t(r, "number")), "title": _t(r, "title"), "latestAction": _latest(r),
                "relationshipDetails": [{"type": _t(d, "type"), "identifiedBy": _t(d, "identifiedBy")}
                                        for d in r.findall("relationshipDetails/item")]}
               for r in b.findall("relatedBills/item")]
    amendments = []
    for a in b.findall("amendments/amendment"):
        acts = [x for x in a.findall("actions/actions/item") if _t(x, "text")]
        amendments.append({"congress": _i(_t(a, "congress")), "type": _t(a, "type"), "number": _t(a, "number"),
                           "description": _t(a, "description"), "purpose": _t(a, "purpose"),
                           "chamber": _t(a, "chamber"), "updateDate": _t(a, "updateDate"),
                           "latestAction": {"actionDate": _t(acts[0], "actionDate"), "text": _t(acts[0], "text")}
                           if acts else None})
    versions = [{"type": _t(v, "type"), "date": _t(v, "date"),
                 "formats": [{"url": _t(f, "url")} for f in v.findall("formats/item")]}
                for v in b.findall("textVersions/item")]
    committees = []
    for c in _all(b, "committees/item", "committees/billCommittees/item"):
        for unit in [c] + c.findall("subcommittees/item"):
            committees.append({"systemCode": (_t(unit, "systemCode") or "").lower(), "name": _t(unit, "name"),
                               "chamber": _t(c, "chamber")})
    return {"bill": bill, "actions": actions, "cosponsors": cosponsors, "relatedBills": related,
            "amendments": amendments, "textVersions": versions, "committees": committees}


_STATUS_CACHE, _STATUS_MAX = {}, 64
_status_lock = threading.Lock()


def billstatus_zip(congress, bill_type):
    return graph.DATA_DIR / "raw" / "govinfo" / "BILLSTATUS" / str(congress) / \
        f"BILLSTATUS-{congress}-{bill_type.lower()}.zip"


def bill_status(congress, bill_type, number):
    """The bill's record from the BILLSTATUS zip on disk (bill_json), or
    None when the zip or the bill is not there. One bill page runs six
    lookups at once, so a parsed record is kept per zip version: the first
    opens the zip (about 30 ms for the House's), the rest copy it."""
    import copy
    path = billstatus_zip(congress, bill_type)
    try:
        mtime = path.stat().st_mtime_ns
    except FileNotFoundError:
        return None
    key = (int(congress), bill_type.lower(), str(number))
    with _status_lock:
        hit = _STATUS_CACHE.get(key)
        if hit and hit[0] == mtime:
            return copy.deepcopy(hit[1])
        try:
            with zipfile.ZipFile(path) as z:
                raw = z.read(f"BILLSTATUS-{congress}{bill_type.lower()}{number}.xml")
        except KeyError:
            return None
        rec = bill_json(raw)
        if len(_STATUS_CACHE) >= _STATUS_MAX:
            _STATUS_CACHE.pop(next(iter(_STATUS_CACHE)))
        _STATUS_CACHE[key] = (mtime, rec)
        return copy.deepcopy(rec)


def billstatus_date(congress, bill_type):
    """GovInfo's date on the bill-status zip on disk for one Congress and
    bill type ("26-Sep-2026 12:16"), or None: what a bill page names when
    a bill is not in it."""
    m = billstatus_zip(congress, bill_type).parent / "manifest.json"
    if not m.exists():
        return None
    entry = json.loads(m.read_text()).get(f"BILLSTATUS-{congress}-{bill_type.lower()}.zip") or {}
    return entry.get("modified")


def bill_text_file(congress, bill_type, number, stages):
    """The stored typescript of the furthest version, in the order `stages`
    names (enrolled first), else the last other version on disk, else
    None."""
    d = graph.DATA_DIR / "raw" / "govinfo" / "BILLS-htm" / str(congress)
    base = f"BILLS-{congress}{bill_type.lower()}{number}"
    for stage in stages:
        p = d / f"{base}{stage}.htm.gz"
        if p.exists():
            return p
    # [a-z]: "hr1" must not match "hr10".
    others = sorted(d.glob(f"{base}[a-z]*.htm.gz"))
    return others[-1] if others else None


_LAWS_CACHE = {}


def bill_for_law(congress, law_number):
    """(type, number) of the bill that became Public Law <congress>-<n>,
    from the bills file, or None."""
    path = graph.data_path("bills", congress=congress)
    try:
        mtime = path.stat().st_mtime_ns
    except FileNotFoundError:
        return None
    with _status_lock:
        hit = _LAWS_CACHE.get(int(congress))
        if not hit or hit[0] != mtime:
            index = {}
            for key, rec in json.loads(path.read_text())["instruments"].items():
                for law in rec.get("laws") or []:
                    index[str(law.get("number") or "").split("-")[-1]] = tuple(key.split("/"))
            hit = _LAWS_CACHE[int(congress)] = (mtime, index)
    return hit[1].get(str(law_number))


def sync(congresses=None):
    """Download what changed, then rebuild the bills file of each Congress
    whose zips changed (or that has no file yet). Returns {congress: meta}."""
    congresses = congresses or range(FIRST_CONGRESS, graph.current_session()[0] + 1)
    s = _session()
    out = {}
    for c in congresses:
        errors = []
        fetched = sync_billstatus_zips(c, s, errors)
        raw = _raw("govinfo", "BILLSTATUS", str(c))
        missing = [t for t in BILL_TYPES if not (raw / f"BILLSTATUS-{c}-{t}.zip").exists()]
        if errors and missing:
            # A type whose listing failed and whose zip was never saved would
            # be silently absent: a Congress without its Senate bills reads
            # as one. Keep the old file and retry tomorrow.
            out[c] = {"congress": c, "unchanged": True, "download_errors": errors
                      + [f"not rebuilt: no zip on disk for {', '.join(missing)}"]}
            continue
        if fetched or not graph.data_path("bills", congress=c).exists():
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
    p = sub.add_parser("text", help="bill typescript, every version: the current and previous Congress, "
                                    "or the Congresses named (108 on)")
    p.add_argument("congress", type=int, nargs="*")
    p = sub.add_parser("counts", help="add Congress.gov's bill counts to existing bills-<c>.json")
    p.add_argument("congress", type=int, nargs="*")
    a = ap.parse_args()
    if a.cmd == "counts":
        for c in a.congress or range(FIRST_CONGRESS, graph.current_session()[0] + 1):
            print(c, add_counts(c))
    if a.cmd == "text":
        # Daily: the current Congress and the one before (a version can
        # still be published in its first weeks). An older Congress
        # publishes nothing new; name it to backfill.
        this = graph.current_session()[0]
        for c in a.congress or (this - 1, this):
            errs = []
            n = sync_bill_text(c, errors=errs)
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
