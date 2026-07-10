"""Deterministic extractor for source `princewilliams-bos`.

Prince William County, Virginia — Board of County Supervisors.

Primary (and only confirmed) system: Granicus tenant `pwcgov`, view_id=23.
  - ViewPublisher.php?view_id=23  enumerates meetings (year panels). Each
    completed meeting with minutes exposes a "Briefs" link that is a
    MinutesViewer.php?...clip_id=NNNN URL.
  - MinutesViewer.php?view_id=23&clip_id=NNNN  ("Briefs") carries per-item
    dispositions and votes in narrative prose, e.g.:

        3.A. APPROVED: Approve the Minutes of May 19, 2026
        1: Bailey 2: Boddye / Unanimous / ABSENT FROM MEETING: Vega Res. No. 26-378

    encoding item number, disposition, title, mover (1:), seconder (2:),
    aggregate outcome, named absentees, and a resolution number.

The county uses resolution numbers (e.g. "26-378") which do NOT match the
schema's file_number format (NNNN-NNNN); per schema v1.2 file_number is set
to null. The resolution number is preserved in an auxiliary field.

Votes are prose: positions are derived from the meeting roster (union of all
named participants) minus named exceptions (absent/nay/abstain). Agenda-packet
PDFs are deliberately NOT fetched.
"""

import re
import html as _html
import hashlib
from collections import Counter

EXTRACTOR_VERSION = "1"
SOURCE_ID = "princewilliams-bos"
SCHEMA_VERSION = "1.2"

VIEW_PUBLISHER_URL = "https://pwcgov.granicus.com/ViewPublisher.php?view_id=23"
MINUTES_URL = "https://pwcgov.granicus.com/MinutesViewer.php?view_id=23&clip_id={clip_id}"

BODY = "Board of County Supervisors"

MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

# ViewPublisher parsing ------------------------------------------------------
_MINUTES_LINK_RE = re.compile(r"MinutesViewer\.php\?[^\"'> ]*clip_id=(\d+)")
_DATE_RE = re.compile(r"([A-Za-z]{3,9})\.?\s+(\d{1,2}),\s+(\d{4})")

# MinutesViewer parsing ------------------------------------------------------
_MOVER_RE = re.compile(
    r"1:\s*(?P<mover>[A-Z][A-Za-z\.\-']+)\s+2:\s*(?P<seconder>[A-Z][A-Za-z\.\-']+)"
)
_HEADER_RE = re.compile(
    r"(?P<item>\d+\.[A-Za-z0-9]+\.?)\s+(?P<disp>[A-Z][A-Z][A-Z /&\-]{1,40}?):"
)
_NAME_TOKEN_RE = re.compile(r"[A-Z][A-Za-z\.\-']+")

_NAME_STOP = {
    "Res", "No", "And", "Meeting", "From", "Absent", "Nays", "Nay", "Ayes",
    "Aye", "The", "Motion", "Carried", "Unanimous", "Vote", "Abstain",
    "Abstained", "Abstains", "Opposed", "Against", "Present", "Roll", "Call",
    "Approved", "Adopted", "Denied", "Deferred", "Failed",
}

_NEG_DISP = {"DENIED", "DEFEATED", "FAILED", "REJECTED", "LOST", "NOT"}


def _clean_text(raw):
    raw = re.sub(r"(?is)<script.*?</script>", " ", raw)
    raw = re.sub(r"(?is)<style.*?</style>", " ", raw)
    raw = re.sub(r"(?s)<[^>]+>", " ", raw)
    raw = _html.unescape(raw)
    raw = raw.replace("\xa0", " ")
    raw = re.sub(r"\s+", " ", raw)
    return raw.strip()


def _parse_date(month, day, year):
    key = month.strip().lower()[:3]
    if key not in MONTHS:
        return None
    try:
        return "%04d-%02d-%02d" % (int(year), MONTHS[key], int(day))
    except (ValueError, TypeError):
        return None


def _enumerate_meetings(raw_html):
    """Return list of (date_str, clip_id) for completed meetings with Briefs,
    newest-first, de-duplicated by date."""
    if not raw_html:
        return []
    H = raw_html.replace("&nbsp;", " ").replace("\xa0", " ")

    date_marks = [(m.start(), m.group(1), m.group(2), m.group(3))
                  for m in _DATE_RE.finditer(H)]
    if not date_marks:
        return []

    found = []  # (date_str, clip_id)
    seen_dates = set()
    for lm in _MINUTES_LINK_RE.finditer(H):
        pos = lm.start()
        clip_id = lm.group(1)
        # nearest preceding date mark = this row's meeting date
        best = None
        for dpos, mon, day, yr in date_marks:
            if dpos < pos:
                if best is None or dpos > best[0]:
                    best = (dpos, mon, day, yr)
            else:
                break
        if best is None:
            continue
        date_str = _parse_date(best[1], best[2], best[3])
        if not date_str:
            continue
        if date_str in seen_dates:
            continue
        seen_dates.add(date_str)
        found.append((date_str, clip_id))

    found.sort(key=lambda x: x[0], reverse=True)
    return found


def _split_names(s):
    if not s:
        return set()
    out = set()
    for tok in _NAME_TOKEN_RE.findall(s):
        if len(tok) < 2:
            continue
        if tok in _NAME_STOP:
            continue
        out.add(tok)
    return out


def _parse_minutes(text):
    """Parse cleaned MinutesViewer text.

    Returns dict {roster:set, absent:set, votes:[...]} or None.
    Each vote: {item, disp, title, mover, seconder, nays:set, absts:set,
                res, outcome}.
    """
    votes = []
    for m in _MOVER_RE.finditer(text):
        p = m.start()
        mover = m.group("mover")
        seconder = m.group("seconder")

        pre = text[max(0, p - 700):p]
        header = None
        for hm in _HEADER_RE.finditer(pre):
            header = hm
        if header is None:
            continue
        item = header.group("item")
        disp = header.group("disp").strip()
        title = pre[header.end():].strip(" :")
        if not title:
            title = "%s %s" % (item, disp)

        tail = text[m.end():m.end() + 300]

        om = re.match(r"\s*/\s*([^/]*)", tail)
        outcome = om.group(1).strip() if om else ""

        am = re.search(
            r"ABSENT FROM MEETING:\s*([^/]*?)(?:\s*Res\.?\s*No|$|/)", tail, re.I)
        absent_str = am.group(1) if am else ""

        nm = re.search(
            r"(?:NAYS?|NO|OPPOSED|AGAINST):\s*([^/]*?)(?:\s*Res\.?\s*No|$|/|ABSENT)",
            tail, re.I)
        nay_str = nm.group(1) if nm else ""

        abm = re.search(
            r"ABSTAIN(?:ED|S)?:\s*([^/]*?)(?:\s*Res\.?\s*No|$|/|ABSENT)", tail, re.I)
        abst_str = abm.group(1) if abm else ""

        rm = re.search(r"Res\.?\s*No\.?\s*([0-9A-Za-z\-]+)", tail, re.I)
        res = rm.group(1) if rm else None

        votes.append({
            "item": item,
            "disp": disp,
            "title": title[:500],
            "mover": mover,
            "seconder": seconder,
            "nays": _split_names(nay_str),
            "absts": _split_names(abst_str),
            "absent": _split_names(absent_str),
            "outcome": outcome,
            "res": res,
        })

    if not votes:
        return None

    meeting_absent = set()
    roster = set()
    for v in votes:
        roster.add(v["mover"])
        roster.add(v["seconder"])
        roster |= v["nays"]
        roster |= v["absts"]
        roster |= v["absent"]
        meeting_absent |= v["absent"]
    roster |= meeting_absent
    # movers/seconders were clearly present; they cannot be meeting-absent
    for v in votes:
        meeting_absent.discard(v["mover"])
        meeting_absent.discard(v["seconder"])

    return {"roster": roster, "absent": meeting_absent, "votes": votes}


def _make_provenance(run_id, source_url):
    return {
        "source_id": SOURCE_ID,
        "extractor_version": EXTRACTOR_VERSION,
        "run_id": run_id,
        "source_url": source_url,
        "certification": {
            "schema_version": SCHEMA_VERSION,
            "method": "granicus-minutesviewer-html",
            "deterministic": True,
        },
    }


def _norm_suffix(s):
    s = re.sub(r"[^A-Za-z0-9]+", "-", s).strip("-")
    return s or "x"


def extract(rt, max_meetings):
    records = {"meetings": [], "agenda_items": [], "vote_events": [], "members": []}

    try:
        view_html = rt.fetch_text(VIEW_PUBLISHER_URL)
    except Exception:
        view_html = ""

    candidates = _enumerate_meetings(view_html)

    run_seed = "%s|%s|%s" % (
        SOURCE_ID, EXTRACTOR_VERSION,
        ",".join(c[1] for c in candidates[:max_meetings]) if candidates else "",
    )
    run_id = "run-" + hashlib.sha1(run_seed.encode("utf-8")).hexdigest()[:12]

    members_seen = {}
    count = 0

    for date_str, clip_id in candidates:
        if count >= max_meetings:
            break

        url = MINUTES_URL.format(clip_id=clip_id)
        try:
            mt_raw = rt.fetch_text(url)
        except Exception:
            continue
        if not mt_raw:
            continue

        text = _clean_text(mt_raw)
        parsed = _parse_minutes(text)
        if not parsed or not parsed["roster"]:
            continue

        roster = parsed["roster"]
        meeting_absent = parsed["absent"]

        attendance = {
            name: ("absent" if name in meeting_absent else "present")
            for name in roster
        }
        if not attendance:
            continue

        meeting_id = "%s-%s" % (SOURCE_ID, date_str)
        prov = _make_provenance(run_id, url)

        meeting = {
            "meeting_id": meeting_id,
            "body": BODY,
            "date": date_str,
            "attendance": attendance,
            "source_url": url,
            "file_number": None,
            "provenance": prov,
        }
        records["meetings"].append(meeting)

        for name in roster:
            if name not in members_seen:
                members_seen[name] = {"name": name, "provenance": prov}

        used_suffixes = {}
        roster_sorted = sorted(roster)

        for v in parsed["votes"]:
            base = _norm_suffix(v["item"])
            n = used_suffixes.get(base, 0)
            used_suffixes[base] = n + 1
            suffix = base if n == 0 else "%s-%d" % (base, n)

            item_id = "%s-item-%s" % (meeting_id, suffix)
            vote_id = "%s-vote-%s" % (meeting_id, suffix)

            positions = []
            for name in roster_sorted:
                if name in meeting_absent:
                    pos = "absent"
                elif name in v["nays"]:
                    pos = "no"
                elif name in v["absts"]:
                    pos = "abstain"
                else:
                    pos = "aye"
                positions.append({"member": name, "position": pos})

            counts = dict(Counter(p["position"] for p in positions))

            ayes = counts.get("aye", 0)
            nos = counts.get("no", 0)
            disp_up = v["disp"].upper()
            outcome_low = v["outcome"].lower()
            negative = any(w in disp_up.split() for w in _NEG_DISP) \
                or "fail" in outcome_low or "defeat" in outcome_low
            if negative or nos > ayes:
                result = "fail"
            else:
                result = "pass"

            agenda_item = {
                "item_id": item_id,
                "meeting_id": meeting_id,
                "title": v["title"],
                "action": v["disp"],
                "result": result,
                "file_number": None,
                "resolution_number": v["res"],
                "provenance": prov,
            }
            records["agenda_items"].append(agenda_item)

            vote_event = {
                "vote_id": vote_id,
                "meeting_id": meeting_id,
                "item_id": item_id,
                "positions": positions,
                "counts": counts,
                "result": result,
                "file_number": None,
                "resolution_number": v["res"],
                "provenance": prov,
            }
            records["vote_events"].append(vote_event)

        count += 1

    records["members"] = [members_seen[n] for n in sorted(members_seen)]

    run_meta = {
        "source_id": SOURCE_ID,
        "extractor_version": EXTRACTOR_VERSION,
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "row_counts": {
            "meetings": len(records["meetings"]),
            "agenda_items": len(records["agenda_items"]),
            "vote_events": len(records["vote_events"]),
            "members": len(records["members"]),
        },
    }

    return records, run_meta
