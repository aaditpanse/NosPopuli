"""Deterministic extractor for source `fairfax-bos`.

Fairfax County, Virginia — Board of Supervisors.

Enumeration (profile "html -> pdf" rung):
  * Crawl the CMS index pages: board homepage, meetings archive, and — crucially
    — the Summaries Archive (board-meeting-summaries), which lists the LATEST
    Clerk's Board Summary PDFs (so CURRENT meetings are captured, not just an
    older archive section). Meeting dates are recovered both from individual
    meeting-page links (/boardofsupervisors/{mon}-{dd}-{yyyy}-meeting) and from
    the summary-PDF hrefs (.../meeting-materials/YYYY/MM-DD-YY Final Summary.pdf).
  * Newest first, for each meeting with an actions/minutes document, parse the
    Clerk's Board Summary PDF: attendance roster + narrative vote outcomes.
  * vote_events emitted only where the document explicitly records a motion
    outcome ("carried by ...", "failed by a vote"). Per-member positions are the
    present roster minus explicitly named exceptions (NAY / abstain / recuse /
    absent). counts are the tally of positions.

Fairfax has NO file-number system => file_number is null everywhere.

Stdlib only. All I/O via the injected runtime `rt`.
"""

import re
from collections import Counter

EXTRACTOR_VERSION = "1"

DOMAIN = "https://www.fairfaxcounty.gov"
ARCHIVE_URL = DOMAIN + "/boardofsupervisors/board-supervisors-meetings-archive"
SUMMARIES_URL = DOMAIN + "/boardofsupervisors/board-meeting-summaries"
HOMEPAGE_URL = DOMAIN + "/boardofsupervisors"
BODY_NAME = "Fairfax County Board of Supervisors"

MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}
MONTH_ABBR = {
    1: "jan", 2: "feb", 3: "mar", 4: "apr", 5: "may", 6: "jun",
    7: "jul", 8: "aug", 9: "sep", 10: "oct", 11: "nov", 12: "dec",
}

MEETING_LINK_RE = re.compile(
    r"/boardofsupervisors/([a-z]{3,5})-(\d{1,2})-(\d{4})-meeting"
)

INDEX_HINT_RE = re.compile(
    r"(meetings-schedule|meeting-schedule|meetings-archive|"
    r"meeting-summaries|summaries-archive|summaries|schedule)",
    re.I,
)

SUFFIX_SET = {"Jr", "Sr", "II", "III", "IV"}


# ---------------------------------------------------------------------------
# argument coercion (harness may pass a list / namespace / string)
# ---------------------------------------------------------------------------
def _coerce_int(v, default=5):
    if isinstance(v, bool):
        return default
    if isinstance(v, int):
        return v if v > 0 else default
    if isinstance(v, float):
        iv = int(v)
        return iv if iv > 0 else default
    if isinstance(v, str):
        m = re.search(r"\d+", v)
        if m:
            iv = int(m.group())
            return iv if iv > 0 else default
        return default
    if isinstance(v, (list, tuple)):
        for x in v:
            r = _coerce_int(x, None)
            if r is not None:
                return r
        return default
    for attr in ("max_meetings", "n", "count", "limit"):
        if hasattr(v, attr):
            return _coerce_int(getattr(v, attr), default)
    return default


# ---------------------------------------------------------------------------
# safe I/O helpers
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
def _gather_index_html(rt):
    seeds = [HOMEPAGE_URL, SUMMARIES_URL, ARCHIVE_URL]
    htmls = []
    fetched = set()
    for u in seeds:
        if u in fetched:
            continue
        fetched.add(u)
        h = _safe_text(rt, u)
        if h:
            htmls.append(h)

    extra = []
    for h in htmls:
        for href in re.findall(r'href=["\']([^"\']+)["\']', h):
            href2 = href.replace("&amp;", "&")
            if "/boardofsupervisors/" not in href2:
                continue
            if href2.lower().rstrip("/").endswith("meeting"):
                continue
            if ".pdf" in href2.lower():
                continue
            if INDEX_HINT_RE.search(href2):
                u = _abs_url(href2)
                if u not in fetched and u not in extra:
                    extra.append(u)

    # derive current-year schedule / summary slugs from years seen
    years = set()
    for h in htmls:
        for m in MEETING_LINK_RE.finditer(h):
            try:
                years.add(int(m.group(3)))
            except Exception:
                pass
        for m in re.finditer(r"/(20\d{2})/", h):
            years.add(int(m.group(1)))
    if years:
        top = max(years)
        for y in (top - 1, top, top + 1):
            for slug in ("%d-meetings-schedule" % y, "%d-meeting-schedule" % y,
                         "board-meeting-summaries-%d" % y):
                u = DOMAIN + "/boardofsupervisors/" + slug
                if u not in fetched and u not in extra:
                    extra.append(u)

    for u in extra[:16]:
        if u in fetched:
            continue
        fetched.add(u)
        h = _safe_text(rt, u)
        if h:
            htmls.append(h)

    return htmls


def _parse_pdf_date(href):
    year = None
    fm = re.search(r"/(20\d{2})/", href)
    if fm:
        year = int(fm.group(1))
    base = href.rsplit("/", 1)[-1].replace("%20", " ")
    dm = re.search(r"(\d{1,2})-(\d{1,2})-(\d{2,4})", base)
    if not dm:
        return None
    mon = int(dm.group(1))
    day = int(dm.group(2))
    yr = dm.group(3)
    if year is None:
        year = int(yr) if len(yr) == 4 else 2000 + int(yr)
    if not (1 <= mon <= 12) or not (1 <= day <= 31):
        return None
    if year < 1990 or year > 2100:
        return None
    date = "%04d-%02d-%02d" % (year, mon, day)
    return (year, mon, day, date)


def _mkc(year, mon, day, date):
    return {"date": date, "y": year, "m": mon, "d": day,
            "meeting_url": None, "pdf": None}


def _enumerate_meetings(rt):
    htmls = _gather_index_html(rt)
    cands = {}
    for html in htmls:
        for m in MEETING_LINK_RE.finditer(html):
            mon = MONTHS.get(m.group(1).lower())
            if not mon:
                continue
            day = int(m.group(2))
            year = int(m.group(3))
            if not (1 <= day <= 31) or year < 1990 or year > 2100:
                continue
            date = "%04d-%02d-%02d" % (year, mon, day)
            url = DOMAIN + "/boardofsupervisors/%s-%d-%d-meeting" % (
                m.group(1).lower(), day, year)
            c = cands.setdefault(date, _mkc(year, mon, day, date))
            if not c["meeting_url"]:
                c["meeting_url"] = url
        for href in re.findall(r'href=["\']([^"\']+)["\']', html):
            hl = href.lower()
            if ".pdf" not in hl or "summary" not in hl:
                continue
            dt = _parse_pdf_date(href)
            if not dt:
                continue
            year, mon, day, date = dt
            c = cands.setdefault(date, _mkc(year, mon, day, date))
            if not c["pdf"]:
                c["pdf"] = _abs_url(href)

    lst = list(cands.values())
    lst.sort(key=lambda c: (c["y"], c["m"], c["d"]), reverse=True)
    return lst


def _find_summary_pdf(rt, meeting_url):
    html = _safe_text(rt, meeting_url)
    if not html:
        return None
    hrefs = re.findall(r'href=["\']([^"\']+)["\']', html)
    pdfs = [h for h in hrefs if ".pdf" in h.lower() and "summary" in h.lower()]
    if not pdfs:
        return None
    for h in pdfs:
        if "final" in h.lower():
            return _abs_url(h)
    return _abs_url(pdfs[0])


def _slug_variants(c):
    variants = []
    months = [MONTH_ABBR[c["m"]]]
    if c["m"] == 9:
        months.append("sept")
    days = [str(c["d"]), "%02d" % c["d"]]
    seen = set()
    for mon in months:
        for day in days:
            slug = "%s-%s-%d-meeting" % (mon, day, c["y"])
            if slug not in seen:
                seen.add(slug)
                variants.append(slug)
    return variants


def _guess_meeting(rt, c):
    """Return (meeting_url, summary_pdf) by trying constructed slugs."""
    for slug in _slug_variants(c):
        u = DOMAIN + "/boardofsupervisors/" + slug
        pdf = _find_summary_pdf(rt, u)
        if pdf:
            return u, pdf
    return None, None


# ---------------------------------------------------------------------------
# summary parsing
# ---------------------------------------------------------------------------
_MEMBER_RE = re.compile(
    r"(Chairman|Supervisor)\s+([^,\n]+?)(?:,\s*(Jr\.?|Sr\.?|II|III|IV))?\s*,\s*([^\n]*)"
)


def _parse_attendance(text):
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
    first = re.sub(r"\s*\(\d{1,2}:\d{2}\s*[ap]\.?m\.?\)\s*$", "", first)
    parts = [first]
    for l in block_lines[1:]:
        s = l.strip()
        if not s:
            break
        if s[0].isdigit():
            break
        if re.search(r"[a-z]", s):
            break
        parts.append(s)
        if len(" ".join(parts)) > 220:
            break
    return re.sub(r"\s+", " ", " ".join(parts)).strip()


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

    return [{"member": n, "position": override.get(n, "aye")} for n in present]


_PASS_RE = re.compile(r"carried by", re.I)
_FAIL_RE = re.compile(
    r"motion (?:was )?(?:failed|defeated|lost)|"
    r"(?:failed|defeated|lost) by (?:a )?vote|did not carry",
    re.I,
)


def _find_vote_events(search_text, present, l2n):
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
    max_meetings = _coerce_int(max_meetings, 5)

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
    cap = max(150, max_meetings * 8 + 40)

    for c in candidates:
        if collected >= max_meetings:
            break
        if examined >= cap:
            break
        examined += 1

        try:
            date = c["date"]
            pdf = c["pdf"]
            meeting_url = c["meeting_url"]

            if meeting_url and not pdf:
                pdf = _find_summary_pdf(rt, meeting_url)
            if not meeting_url:
                gu, gp = _guess_meeting(rt, c)
                if gu:
                    meeting_url = gu
                    if not pdf:
                        pdf = gp

            if not pdf:
                continue

            # human-viewable page: the meeting's page if known, else the
            # Summaries Archive listing (never the raw PDF endpoint).
            source_url = meeting_url or SUMMARIES_URL

            text = _safe_text(rt, pdf)
            if not text:
                continue

            present, l2n, meta = _parse_attendance(text)
            if not present:
                continue

            meeting_id = "fairfax-bos-%s" % date

            attendance = {name: "present" for name in present}
            meetings.append({
                "meeting_id": meeting_id,
                "body": BODY_NAME,
                "date": date,
                "attendance": attendance,
                "file_number": None,
                "source_url": source_url,
                "data_source_url": pdf,
                "provenance": dict(prov),
            })

            for name in present:
                if name not in members_by_name:
                    m = meta.get(name, {})
                    rec = {"name": name, "provenance": dict(prov)}
                    if m.get("role"):
                        rec["role"] = m["role"]
                    if m.get("district"):
                        rec["district"] = m["district"]
                    members_by_name[name] = rec

            for blk in _block_split(text):
                title = _clean_title(blk["lines"])
                if not title:
                    continue
                search_text = re.sub(r"\s+", " ", " ".join(blk["lines"]))
                events = _find_vote_events(search_text, present, l2n)
                if not events:
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
                    "source_url": source_url,
                    "data_source_url": pdf,
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
                        "source_url": source_url,
                        "data_source_url": pdf,
                        "provenance": dict(prov),
                    })

            collected += 1

        except Exception:
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
