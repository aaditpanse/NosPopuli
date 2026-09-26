"""Voteview: every roll call of Congress since 1789, read from files.

Voteview (UCLA) publishes each Congress's roll calls, member positions and
member list as CSV. This module turns them into congress-votes-<c>-<n>.json
in graph.DATA_DIR, the same shape as the clerks' snapshots, so the graph
answers "how did X vote in 1995" from a file and certifies older terms
through member-congress.json. It never covers a Congress the clerks' files
cover (118th onward): Voteview does not certify a clerk record, and two
copies of one roll call would count twice.

Positions (voteview.com/articles/data_help_votes): 1–3 yea, paired yea,
announced yea → aye; 4–6 announced nay, paired nay, nay → no; 7–8 present;
9 not voting → absent; 0 not a member at the time → no position.

Run on the server:
    python -m sources.voteview            # 1st → 117th Congress
    python -m sources.voteview 110 111    # some Congresses
"""

import csv
import datetime
import io
import json
import pathlib
import re
import sys

_HERE = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_HERE))
import graph  # noqa: E402  — DATA_DIR

BASE = "https://voteview.com/static/data/out"
FIRST_CLERK_CONGRESS = 118
CHAMBERS = {"H": "house", "S": "senate"}
POSITION = {"1": "aye", "2": "aye", "3": "aye", "4": "no", "5": "no", "6": "no",
            "7": "present", "8": "present", "9": "absent"}
_UA = "NosPopuli bulk sync (nospopuli.org)"


def _raw(congress):
    p = graph.DATA_DIR / "raw" / "voteview" / f"{congress:03d}"
    p.mkdir(parents=True, exist_ok=True)
    return p


def download(congress, session_=None, errors=None):
    """The six CSVs of one Congress (votes, rollcalls, members × House,
    Senate), each fetched only if Voteview's copy is newer than ours
    (If-Modified-Since). Fail-open per file: the old copy stays and the
    failure is recorded. Returns the names downloaded."""
    import requests
    s = session_ or requests.Session()
    s.headers["User-Agent"] = _UA
    errors = [] if errors is None else errors
    raw = _raw(congress)
    manifest_path = raw / "manifest.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    fetched = []
    for kind in ("votes", "rollcalls", "members"):
        for ch in CHAMBERS:
            name = f"{ch}{congress:03d}_{kind}.csv"
            url = f"{BASE}/{kind}/{name}"
            headers = {"If-Modified-Since": manifest[name]} if name in manifest and (raw / name).exists() else {}
            try:
                r = s.get(url, headers=headers, timeout=300)
                if r.status_code == 304:
                    continue
                r.raise_for_status()
                if not r.content.startswith(b"congress,"):   # fail-closed: an error page is not a CSV
                    raise ValueError("not a Voteview CSV")
                tmp = raw / (name + ".part")
                tmp.write_bytes(r.content)
                tmp.replace(raw / name)
            except Exception as e:
                errors.append(f"{url}: {type(e).__name__}")
                continue
            manifest[name] = r.headers.get("Last-Modified", "")
            manifest_path.write_text(json.dumps(manifest, indent=1, sort_keys=True))
            fetched.append(name)
    return fetched


def icpsr_index(legislators):
    """{icpsr: bioguide} from the legislators files: the independent check
    on Voteview's own bioguide column."""
    return {str(leg["id"]["icpsr"]): leg["id"].get("bioguide")
            for leg in legislators if leg.get("id", {}).get("icpsr")}


def _bill(bill_number):
    """'HRES5' → ('HRES', '5'); None for an empty or unnumbered one."""
    m = re.match(r"^([A-Z]+)(\d+)$", (bill_number or "").strip().upper())
    return (m.group(1), m.group(2)) if m else None


def convert(congress, chamber_code, votes_csv, rollcalls_csv, members_csv, by_icpsr, gaps):
    """One chamber of one Congress → roll calls in the snapshot vote shape,
    keyed by calendar year. A member whose Voteview bioguide id disagrees
    with the legislators file's is dropped and named in gaps; one with no
    bioguide id at all is counted. Pure. Returns {year: [vote, ...]}."""
    chamber = CHAMBERS[chamber_code]
    ids, mismatched, unidentified = {}, [], 0
    for m in csv.DictReader(io.StringIO(members_csv)):
        if m["chamber"] == "President":
            continue
        bio = (m.get("bioguide_id") or "").strip()
        other = by_icpsr.get(m["icpsr"])
        if not bio:
            unidentified += 1
            continue
        if other and other != bio:
            mismatched.append(f"{m['icpsr']} ({bio} vs {other})")
            continue
        ids[m["icpsr"]] = bio
    positions = {}
    for v in csv.DictReader(io.StringIO(votes_csv)):
        pos = POSITION.get(v["cast_code"])
        bio = ids.get(v["icpsr"])
        if pos and bio:
            positions.setdefault(v["rollnumber"], {}).setdefault(pos, []).append(bio)
    by_year = {}
    for rc in csv.DictReader(io.StringIO(rollcalls_csv)):
        if rc["chamber"] == "President":
            continue
        roll = int(rc["rollnumber"])
        vote = {"vote_id": f"us/{congress}/voteview/{chamber}/{roll}", "chamber": chamber,
                "congress": congress, "roll": roll, "date": rc["date"],
                "question": rc.get("vote_question") or None, "result": rc.get("vote_result") or None,
                "description": rc.get("vote_desc") or rc.get("dtl_desc") or "",
                "id_kind": "bioguide", "positions": positions.get(rc["rollnumber"], {}),
                "source_id": "voteview",
                "source_url": f"https://voteview.com/rollcall/R{chamber_code}{congress:03d}{roll:04d}"}
        bill = _bill(rc.get("bill_number"))
        if chamber == "house":
            # graph._parse_instrument reads the House's "H RES 5" form; an
            # empty one is a vote on no instrument, as a quorum call is.
            vote["legis_num"] = f"{bill[0]} {bill[1]}" if bill else ""
        else:
            vote["document_type"], vote["document_number"] = bill if bill else ("", "")
            vote["document_name"] = "".join(bill) if bill else ""
        by_year.setdefault(rc["date"][:4], []).append(vote)
    if mismatched:
        gaps.append(f"{congress} {chamber}: {len(mismatched)} member(s) whose Voteview bioguide id "
                    f"disagrees with the legislators file, dropped: {', '.join(mismatched[:5])}")
    if unidentified:
        gaps.append(f"{congress} {chamber}: {unidentified} member(s) with no bioguide id in Voteview; "
                    f"their positions are not recorded")
    return by_year


def write_congress(congress, by_icpsr):
    """Convert one Congress from the CSVs on disk and write one file per
    calendar year, numbered by the year's order within the Congress. Files
    of this Congress that the new set does not replace are removed, so a
    stale year cannot linger. Returns meta per file."""
    raw = _raw(congress)
    gaps, years = [], {}
    for ch in CHAMBERS:
        paths = [raw / f"{ch}{congress:03d}_{k}.csv" for k in ("votes", "rollcalls", "members")]
        if not all(p.exists() for p in paths):
            gaps.append(f"{congress} {CHAMBERS[ch]}: Voteview files missing; not converted")
            continue
        for year, votes in convert(congress, ch, *(p.read_text(encoding="utf-8") for p in paths),
                                   by_icpsr, gaps).items():
            years.setdefault(year, []).extend(votes)
    written, out = set(), []
    today = datetime.date.today().isoformat()
    for n, year in enumerate(sorted(years), start=1):
        votes = sorted(years[year], key=lambda v: (v["date"], v["chamber"], v["roll"]))
        for v in votes:
            v["session"] = n
        meta = {"congress": congress, "session": n, "year": int(year), "fetched": today, "source": "voteview",
                "counts": {c: sum(1 for v in votes if v["chamber"] == c) for c in ("house", "senate")},
                "errors": [], "gaps": gaps}
        path = graph.DATA_DIR / f"congress-votes-{congress}-{n}.json"
        tmp = path.with_name(path.name + ".part")
        tmp.write_text(json.dumps({"meta": meta, "votes": votes}, separators=(",", ":")))
        tmp.replace(path)
        written.add(path.name)
        out.append(meta)
    for old in graph.DATA_DIR.glob(f"congress-votes-{congress}-*.json"):
        if old.name not in written:
            old.unlink()
    return out


def sync(congresses=None):
    """Download what changed and rewrite the files of each Congress that
    changed or has none. Returns {congress: [meta...] or "unchanged"}."""
    import requests
    congresses = congresses or range(1, FIRST_CLERK_CONGRESS)
    if any(c >= FIRST_CLERK_CONGRESS for c in congresses):
        raise ValueError(f"the {FIRST_CLERK_CONGRESS}th Congress onward comes from the clerks' files")
    current = json.loads((graph.DATA_DIR / "legislators-current.json").read_text())
    hist = graph.DATA_DIR / "legislators-historical.json"
    by_icpsr = icpsr_index(current + (json.loads(hist.read_text()) if hist.exists() else []))
    s = requests.Session()
    out = {}
    for c in congresses:
        errors = []
        fetched = download(c, s, errors)
        if fetched or not any(graph.DATA_DIR.glob(f"congress-votes-{c}-*.json")):
            out[c] = {"files": write_congress(c, by_icpsr), "download_errors": errors}
        else:
            out[c] = {"unchanged": True, "download_errors": errors}
    return out


if __name__ == "__main__":
    args = [int(a) for a in sys.argv[1:]]
    for c, r in sync(args or None).items():
        files = r.get("files") or []
        print(c, "unchanged" if r.get("unchanged") else
              f"{len(files)} file(s), {sum(sum(m['counts'].values()) for m in files)} roll call(s)",
              f"{len(r['download_errors'])} error(s)")
        for g in (files[0]["gaps"] if files else [])[:5]:
            print("  -", g)
        for e in r["download_errors"][:5]:
            print("  -", e)
