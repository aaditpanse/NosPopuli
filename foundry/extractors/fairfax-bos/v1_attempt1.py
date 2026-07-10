"""Deterministic extractor for source `fairfax-bos`.

Fairfax County, Virginia — Board of Supervisors.

Enumeration path (per discovery profile, "html -> pdf" rung):
  1) Fetch the archive index page. It links to individual meeting pages whose
     URLs follow /boardofsupervisors/{mon}-{dd}-{yyyy}-meeting.
  2) For each meeting page (newest first), locate the "Final Meeting Summary"
     PDF (the Clerk's Board Summary — REPORT OF ACTIONS). That summary carries
     the attendance roster and the narrative vote outcomes.
  3) Parse attendance + per-item action narrative. Votes are prose ("carried
     by unanimous vote", "carried by a vote of nine, Supervisor X voting
     'NAY'"); per-member positions are derived from the present roster minus
     the explicitly named exceptions. A vote_event is emitted only where the
     document explicitly records a motion outcome.

Fairfax has NO file-number system => file_number is null everywhere.

Stdlib only. All I/O via the injected runtime `rt`.
"""

import re
from collections import Counter

EXTRACTOR_VERSION = "1"

DOMAIN = "https://www.fairfaxcounty.gov"
ARCHIVE_URL = DOMAIN + "/boardofsupervisors/board-supervisors-meetings-archive"
BODY_NAME = "Fairfax County Board of Supervisors"

MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}

MEETING_LINK_RE = re.compile(
    r"/boardofsupervisors/([a-z]{3,5})-(\d{1,2})-(\d{4})-meeting"
)

SUFFIX_SET = {"Jr", "Sr", "II", "III", "IV"}


# ---------------------------------------------------------------------------
# small safe I/O helpers
# ---------------------------------------------------------------------------
def _safe_text(rt, url):
    try:
        t = rt.fetch_text(url)
        if isinstance(t, str) and t.strip():
            return t
    except Exception:
        return None
    return None


def _abs_url(href):
    href = href.replace("&amp;", "&").strip()
    if href.startswith("http"):
        return href
    if href.startswith("/"):
        return DOMAIN + href
    return DOMAIN + "/" + href


# ---------------------------------------------------------------------------
# name utilities
# ---------------------------------------------------------------------------
def _last_name(name):
    toks = name.replace(".", "").replace(",", " ").split()
    toks = [t for t in toks if t and t not in SUFFIX_SET]
    return toks[-1] if toks else name


def _names_in(text, l2n):
    found = set()
    for ln, full in l2n.items():
        if re.search(r"\b" + re.escape(ln) + r"\b", text):
            found.add(full)
    return found


# ---------------------------------------------------------------------------
# enumeration
# ---------------------------------------------------------------------------
def _enumerate_meetings(rt):
    html = _safe_text(rt, ARCHIVE_URL)
    if not html:
        return []
    seen = {}
    for m in MEETING_LINK_RE.finditer(html):
        mon = MONTHS.get(m.group(1).lower())
        if not mon:
            continue
        day = int(m.group(2))
        year = int(m.group(3))
        if not (1 <= day <= 31) or year < 1990 or year > 2100:
            continue
        date = "%04d-%02d-%02d" % (year, mon, day)
        path = "/boardofsupervisors/%s-%d-%d-meeting" % (
            m.group(1).lower(), day, year,
        )
        url = DOMAIN + path
        if url not in seen:
            seen[url] = (year, mon, day, date, url)
    cands = list(seen.values())
    cands.sort(key=lambda c: (c[0], c[1], c[2]), reverse=True)
    return cands


def _find_summary_pdf(rt, meeting_url):
    html = _safe_text(rt, meeting_url)
    if not html:
        return None
    hrefs = re.findall(r'href=["\']([^"\']+)["\']', html)
    pdfs = [h for h in hrefs if ".pdf" in h.lower() and "summary" in h.lower()]
    if not pdfs:
        return None
    # prefer "final ... summary"
    for h in pdfs:
        if "final" in h.lower():
            return _abs_url(h)
    return _abs_url(pdfs[0])


# ---------------------------------------------------------------------------
# summary parsing
# ---------------------------------------------------------------------------
_MEMBER_RE = re.compile(
    r"(Chairman|Supervisor)\s+([^,\n]+?)(?:,\s*(Jr\.?|Sr\.?|II|III|IV))?\s*,\s*([^\n]*)"
)


def _parse_attendance(text):
    """Return (present_fullnames_ordered, l2n, member_meta)."""
    start = re.search(r"there were present", text, re.I)
    end = re.search(r"Others present", text, re.I)
    if start:
        region = text[start.end(): end.start() if end else start.end() + 3000]
    else:
        region = text[:4000]

    present = []
    l2n = {}
    meta = {}
    for m in _MEMBER_RE.finditer(region):
        role = m.group(1)
        base = m.group(2).strip()
        suffix = (m.group(3) or "").strip()
        descriptor = (m.group(4) or "").strip()
        if not base or not base[0].isupper() or len(base) < 3:
            continue
        # base must look like a person name (has a space => first + last)
        if " " not in base:
            continue
        full = base + (", " + suffix if suffix else "")
        if full in meta:
            continue
        ln = _last_name(full)
        district = None
        dm = re.search(r"([A-Za-z]+(?: [A-Za-z]+)* District)", descriptor)
        if dm:
            district = dm.group(1)
        present.append(full)
        l2n[ln] = full
        meta[full] = {"role": role, "district": district}
    return present, l2n, meta


def _block_split(text):
    """Split summary text into numbered item blocks."""
    lines = text.split("\n")
    blocks = []
    cur = None
    for line in lines:
        mm = re.match(r"\s*(\d{1,3})\.\s+(.+)", line)
        if mm:
            if cur:
                blocks.append(cur)
            first = mm.group(2).strip()
            cur = {"num": int(mm.group(1)), "lines": [first]}
        else:
            if cur is not None:
                cur["lines"].append(line.rstrip())
    if cur:
        blocks.append(cur)
    return blocks


def _clean_title(block_lines):
    first = block_lines[0].strip()
    # strip trailing time marker like (9:34 a.m.)
    first = re.sub(r"\s*\(\d{1,2}:\d{2}\s*[ap]\.?m\.?\)\s*$", "", first)
    parts = [first]
    for l in block_lines[1:]:
        s = l.strip()
        if not s:
            break
        if s[0].isdigit():
            break
        if re.search(r"[a-z]", s):  # narrative / mixed-case => end of title
            break
        parts.append(s)
        if len(" ".join(parts)) > 220:
            break
    title = re.sub(r"\s+", " ", " ".join(parts)).strip()
    return title


_NAMELIST = (
    r"([A-Z][\w.'\-]+(?:(?:,\s+|,?\s+and\s+)(?:Chairman\s+|Supervisors?\s+)?"
    r"[A-Z][\w.'\-]+)*)"
)
_PAT_NAY = re.compile(
    r"(?:Chairman|Supervisors?)\s+" + _NAMELIST +
    r"\s+voting\s*['\"\u201c\u201d]?\s*(?:NAY|NO|Nay|No|nay|no)\b"
)
_PAT_ABSTAIN = re.compile(
    r"(?:Chairman|Supervisors?)\s+" + _NAMELIST +
    r"\s+(?:abstained|abstaining|abstain)\b"
)
_PAT_RECUSE = re.compile(
    r"(?:Chairman|Supervisors?)\s+" + _NAMELIST + r"\s+recus(?:ed|ing)\b"
)
_PAT_ABSENT = re.compile(
    r"(?:Chairman|Supervisors?)\s+" + _NAMELIST + r"\s+being absent\b"
)


def _build_positions(present, window, l2n):
    override = {}

    def apply(pat, pos):
        for m in pat.finditer(window):
            for full in _names_in(m.group(1), l2n):
                override.setdefault(full, pos)

    apply(_PAT_NAY, "no")
    apply(_PAT_ABSTAIN, "abstain")
    apply(_PAT_RECUSE, "recused")
    apply(_PAT_ABSENT, "absent")

    positions = [{"member": n, "position": override.get(n, "aye")}
                 for n in present]
    return positions


_PASS_RE = re.compile(r"carried by", re.I)
_FAIL_RE = re.compile(
    r"motion (?:was )?(?:failed|defeated|lost)|"
    r"(?:failed|defeated|lost) by (?:a )?vote|did not carry",
    re.I,
)


def _find_vote_events(search_text, present, l2n):
    """Return list of (result, positions) tuples for a block."""
    hits = []
    for m in _PASS_RE.finditer(search_text):
        hits.append((m.start(), "pass"))
    for m in _FAIL_RE.finditer(search_text):
        hits.append((m.start(), "fail"))
    hits.sort(key=lambda h: h[0])

    events = []
    for pos, result in hits:
        window = search_text[max(0, pos - 30): pos + 320]
        positions = _build_positions(present, window, l2n)
        if not positions:
            continue
        events.append((result, positions))
    return events


# ---------------------------------------------------------------------------
# main extraction
# ---------------------------------------------------------------------------
def extract(rt, max_meetings):
    run_id = "fairfax-bos-%s" % EXTRACTOR_VERSION
    prov = {
        "source_id": "fairfax-bos",
        "extractor_version": EXTRACTOR_VERSION,
        "run_id": run_id,
    }

    meetings = []
    agenda_items = []
    vote_events = []
    members_by_name = {}

    try:
        candidates = _enumerate_meetings(rt)
    except Exception:
        candidates = []

    collected = 0
    examined = 0
    cap = max(60, int(max_meetings) * 5 + 20)

    for (year, mon, day, date, meeting_url) in candidates:
        if collected >= max_meetings:
            break
        if examined >= cap:
            break
        examined += 1

        try:
            pdf_url = _find_summary_pdf(rt, meeting_url)
            if not pdf_url:
                continue
            text = _safe_text(rt, pdf_url)
            if not text:
                continue

            present, l2n, meta = _parse_attendance(text)
            if not present:
                # can't build attendance => treat as unparseable, skip
                continue

            meeting_id = "fairfax-bos-%s" % date

            # meeting record
            attendance = {name: "present" for name in present}
            meetings.append({
                "meeting_id": meeting_id,
                "body": BODY_NAME,
                "date": date,
                "attendance": attendance,
                "file_number": None,
                "source_url": meeting_url,
                "data_source_url": pdf_url,
                "provenance": dict(prov),
            })

            # members
            for name in present:
                if name not in members_by_name:
                    m = meta.get(name, {})
                    rec = {"name": name, "provenance": dict(prov)}
                    if m.get("role"):
                        rec["role"] = m["role"]
                    if m.get("district"):
                        rec["district"] = m["district"]
                    members_by_name[name] = rec

            # agenda items + votes
            blocks = _block_split(text)
            for blk in blocks:
                title = _clean_title(blk["lines"])
                if not title:
                    continue
                search_text = re.sub(r"\s+", " ", " ".join(blk["lines"]))
                events = _find_vote_events(search_text, present, l2n)
                if not events:
                    # not an item acted on (procedural / heading) -> skip
                    continue

                num = blk["num"]
                item_id = "%s-item-%d" % (meeting_id, num)
                item_result = "pass" if any(e[0] == "pass" for e in events) \
                    else "fail"

                agenda_items.append({
                    "item_id": item_id,
                    "meeting_id": meeting_id,
                    "title": title[:500],
                    "action": "Approved" if item_result == "pass" else "Failed",
                    "result": item_result,
                    "file_number": None,
                    "source_url": meeting_url,
                    "data_source_url": pdf_url,
                    "provenance": dict(prov),
                })

                for i, (result, positions) in enumerate(events):
                    counts = dict(Counter(p["position"] for p in positions))
                    vote_events.append({
                        "vote_id": "%s-v%d" % (item_id, i + 1),
                        "meeting_id": meeting_id,
                        "item_id": item_id,
                        "positions": positions,
                        "counts": counts,
                        "result": result,
                        "file_number": None,
                        "source_url": meeting_url,
                        "data_source_url": pdf_url,
                        "provenance": dict(prov),
                    })

            collected += 1

        except Exception:
            # be robust: skip meetings that fail to parse
            continue

    records = {
        "meetings": meetings,
        "agenda_items": agenda_items,
        "vote_events": vote_events,
        "members": list(members_by_name.values()),
    }

    run_meta = {
        "source_id": "fairfax-bos",
        "extractor_version": EXTRACTOR_VERSION,
        "schema_version": "1.2",
        "row_counts": {
            "meetings": len(records["meetings"]),
            "agenda_items": len(records["agenda_items"]),
            "vote_events": len(records["vote_events"]),
            "members": len(records["members"]),
        },
    }
    return records, run_meta
