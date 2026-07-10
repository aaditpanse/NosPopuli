"""Extractor artifact: Loudoun County BOS via Laserfiche WebLink (hand-written v1).

Walks the WebLink RSS tree (year folder -> meeting folders -> Action Report
PDF), parses motions with tallies from the Action Report text, and derives
per-member positions as roster-minus-named-exceptions — which is how the
clerk encodes them ("The motion passed 7-0-2: Supervisors Kershner and
Saines absent.").

Loudoun records carry no file numbers (schema gap logged at M3-Loudoun), so
vote_events use motion sequence ids and the records ship in the store as
ingest-only until a second source (Minutes / Copy Testes) is wired.
"""

import re

SOURCE_ID = "loudoun-bos"
EXTRACTOR_VERSION = "1"
PORTAL = "https://lfportal.loudoun.gov/LFPortalinternet"
YEAR_FOLDERS = {2026: 1966224, 2025: 1947831, 2024: 584995}

# 2024-2027 term roster (from loudoun.gov/86/Board-of-Supervisors).
# A wrong roster shows up as unknown-exception flags, not silent errors.
ROSTER = ["Phyllis J. Randall", "Michael R. Turner", "Juli Briskman",
          "Sylvia Glass", "Caleb Kershner", "Matthew F. Letourneau",
          "Kristen Umstattd", "Laura TeKrony", "Koran Saines"]
LAST = {n.split()[-1]: n for n in ROSTER}

MOTION_RE = re.compile(
    r"((?:Chair|Vice Chair|Supervisor)\s+[A-Z][a-zA-Z]+ (?:moved|made a motion)\b.{0,700}?)"
    r"\(Seconded by[^.)]*\.\s*The motion (passed|failed) (\d+)-(\d+)-(\d+)\s*([^)]*)\)")
EXCEPTION_RE = re.compile(
    r"Supervisors?\s+([A-Za-z ,.and]+?)\s+(absent|opposed|abstained|abstaining)")
STATUS = {"absent": "absent", "opposed": "no",
          "abstained": "abstain", "abstaining": "abstain"}


def rss_entries(rt, folder_id):
    xml = rt.fetch_text(f"{PORTAL}/rss/dbid/0/folder/{folder_id}/feed.rss")
    return re.findall(r"<item>\s*<title>([^<]*)</title>\s*<link>([^<]*)</link>",
                      xml)


def parse_action_report(text, date, doc_url):
    flat = re.sub(r"\s+", " ", text)
    votes, flags = [], []
    for i, m in enumerate(MOTION_RE.finditer(flat), 1):
        body, outcome, ayes, noes, other, blob = m.groups()
        positions = {}
        for names_text, status in EXCEPTION_RE.findall(blob):
            for token in re.split(r",|\band\b", names_text):
                last = token.strip().split()[-1] if token.strip() else ""
                if last in LAST:
                    positions[LAST[last]] = STATUS[status]
                elif last:
                    flags.append(f"m{i}: unknown member '{last}' in exceptions")
        for member in ROSTER:
            positions.setdefault(member, "aye")
        counts = {}
        for p in positions.values():
            counts[p] = counts.get(p, 0) + 1
        consistent = (counts.get("aye", 0), counts.get("no", 0)) == \
            (int(ayes), int(noes))
        votes.append({
            "vote_id": f"{SOURCE_ID}-{date}-m{i}",
            "meeting_id": f"{SOURCE_ID}-{date}",
            "motion": re.sub(r"\s+", " ", body)[:240],
            "positions": [{"member": k, "position": v}
                          for k, v in sorted(positions.items())],
            "counts": counts,
            "reported_tally": f"{ayes}-{noes}-{other}",
            "tally_consistent": consistent,
            "result": "pass" if outcome == "passed" else "fail",
            "source_document": doc_url,
        })
    return votes, flags


def extract(rt, years):
    meetings, vote_events, all_flags = [], [], []
    for year in years:
        for title, link in rss_entries(rt, YEAR_FOLDERS[year]):
            if "Business Meeting" not in title or "Joint" in title:
                continue
            folder = re.findall(r"startid=(\d+)", link)
            if not folder:
                continue
            ar = next(((t, l) for t, l in rss_entries(rt, folder[0])
                       if "Action Report" in t), None)
            if ar is None:
                continue  # meeting hasn't happened / report not posted yet
            doc_id = re.findall(r"id=(\d+)", ar[1])[0]
            doc_url = f"{PORTAL}/0/edoc/{doc_id}/ActionReport.pdf"
            d = re.search(r"(\d{2})-(\d{2})-(\d{2})", title)
            date = f"20{d.group(3)}-{d.group(1)}-{d.group(2)}"
            votes, flags = parse_action_report(rt.fetch_text(doc_url), date, doc_url)
            all_flags += flags
            absent_all = None
            for v in votes:
                absent = {p["member"] for p in v["positions"]
                          if p["position"] == "absent"}
                absent_all = absent if absent_all is None else absent_all & absent
            meetings.append({
                "meeting_id": f"{SOURCE_ID}-{date}",
                "body": "Board of Supervisors",
                "jurisdiction": "Loudoun County, VA",
                "date": date,
                "attendance": {n: ("absent" if n in (absent_all or set())
                                   else "present") for n in ROSTER},
                "source_url": f"{PORTAL}/docview.aspx?id={doc_id}&dbid=0",
                "minutes_url": doc_url,
            })
            vote_events += votes
    records = {"meetings": meetings, "agenda_items": [],
               "vote_events": vote_events,
               "members": [{"name": n} for n in ROSTER]}
    run_meta = {"source_id": SOURCE_ID, "extractor_version": EXTRACTOR_VERSION,
                "years": list(years), "flags": all_flags,
                "row_counts": {k: len(v) for k, v in records.items()}}
    return records, run_meta
