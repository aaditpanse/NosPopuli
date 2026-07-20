"""Fetch and normalize state bill detail from LegiScan.

Replaces the OpenStates fetcher. `fetch_state_bill` returns the raw LegiScan
`getBill` payload augmented with a few legacy-named convenience keys
(`identifier`, `sponsorships`, `sources`, `latest_action_*`) so the streaming
contract in api.py (`_state_bill_stream`) keeps working unchanged. Raw LegiScan
keys (`texts`, `history`, `votes`, `sponsors`, `session_id`, `change_hash`) are
preserved for the text/timeline/vote paths.

Bill text arrives from `getBillText` as raw bytes (base64-decoded in the client)
— PDF for most states, HTML/plain for some — and is extracted here.
"""

import re
import io
from bs4 import BeautifulSoup
from cachetools import TTLCache
from threading import RLock

import legiscan_client as legiscan
from documentor_agent import log_action

_text_cache = TTLCache(maxsize=256, ttl=7200)  # extracted text by doc_id — stable
_text_lock  = RLock()


def _augment_bill(bill: dict) -> dict:
    """Add legacy-named keys the streaming layer reads, alongside raw LegiScan
    fields. Non-destructive: raw keys stay for text/timeline/vote handling."""
    bill["identifier"] = bill.get("bill_number", "")
    # sponsor_type_id == 1 is the primary sponsor in LegiScan's taxonomy.
    bill["sponsorships"] = [
        {"name": s.get("name", ""), "primary": s.get("sponsor_type_id") == 1}
        for s in (bill.get("sponsors") or [])
    ]
    # Sources: the LegiScan bill page and the official state document link.
    sources = []
    if bill.get("url"):
        sources.append({"url": bill["url"], "note": "LegiScan"})
    if bill.get("state_link"):
        sources.append({"url": bill["state_link"], "note": "Official state source"})
    bill["sources"] = sources
    # Latest action from the tail of history (chronological order).
    history = bill.get("history") or []
    if history:
        last = history[-1]
        bill["latest_action_description"] = last.get("action", "")
        bill["latest_action_date"] = last.get("date", "")
    else:
        bill["latest_action_description"] = ""
        bill["latest_action_date"] = ""
    return bill


def fetch_state_bill(ocd_id):
    """Fetch full bill detail from LegiScan by bill_id (carried in `ocd_id`)."""
    bill = legiscan.get_bill(ocd_id)
    if not bill:
        return None
    return _augment_bill(bill)


def _normalize_bill_text(text):
    """
    Clean and reformat raw legislative bill text for readable display.

    Formatting rules:
    1. Strip unicode whitespace artifacts (non-breaking spaces, etc.)
    2. Remove lines that are purely whitespace
    3. Join structural fragment lines — bare parentheticals "(2)" and section
       labels "Sec." that were split into their own elements by the HTML source
    4. Join prose lines that were wrapped at ~60 chars in the source HTML
    5. Preserve intentional structure: SECTION N, Sec., (N), (a) always start
       their own line; ALL-CAPS headers that end with . or : are treated as complete
    6. Add a single blank line before each major SECTION to aid scanning
    """
    # 1. Normalize unicode whitespace artifacts
    text = text.replace('\xa0', ' ').replace(' ', ' ').replace(' ', ' ')

    # 2. Strip each line; drop lines that are pure whitespace after stripping
    lines = [l.strip() for l in text.split('\n')]
    lines = [l for l in lines if l]

    # 3. Join structural fragment lines with the line that follows them.
    #    Fragments: bare parentheticals "(2)", bare "Sec.", bare section numbers "240.912."
    merged = []
    i = 0
    while i < len(lines):
        line = lines[i]
        nxt  = lines[i + 1] if i + 1 < len(lines) else None

        is_bare_paren = bool(re.match(r'^\([0-9a-zA-Z]+\)$', line))
        is_bare_sec   = bool(re.match(r'^Sec\.$', line, re.IGNORECASE))
        is_bare_num   = bool(re.match(r'^\d+\.\d[\d\.]*\.$', line))

        if nxt and (is_bare_paren or is_bare_sec or is_bare_num):
            merged.append(line + '  ' + nxt)
            i += 2
        else:
            merged.append(line)
            i += 1
    lines = merged

    # Patterns for structure detection
    SECTION_START = re.compile(
        r'^(SECTION\s+\d|SEC\.\s+\d|BE IT\s|AN ACT|A BILL\s|By:|'
        r'H\.[BCJ-SJ]\.(\s+No\.)?|S\.[BCJ]\.(\s+No\.)?|'
        r'TITLE\s+[IVX\d]|SUBTITLE\s+[A-Z]|'
        r'CHAPTER\s+\d|SUBCHAPTER\s+[A-Z]|ARTICLE\s+\d)',
        re.IGNORECASE
    )
    SUBSEC_START = re.compile(r'^\([0-9a-zA-Z]+\)\s')
    TERMINAL     = re.compile(r'[.;:?!]$')
    # All-caps header that is itself a complete line (ends with . or :)
    CAPS_HEADER  = re.compile(r'^[A-Z][A-Z0-9\s\-\.]+[.:]$')

    # 4. Join prose lines that were wrapped at the source
    result = []
    for line in lines:
        if not result:
            result.append(line)
            continue

        prev = result[-1]
        prev_complete  = bool(TERMINAL.search(prev)) or bool(CAPS_HEADER.match(prev))
        line_new_block = bool(SECTION_START.match(line)) or bool(SUBSEC_START.match(line))

        if not prev_complete and not line_new_block:
            result[-1] = prev + ' ' + line
        else:
            result.append(line)

    # 5. Add a blank line before each major SECTION N for visual separation
    MAJOR_SECTION = re.compile(r'^SECTION\s+\d', re.IGNORECASE)
    spaced = []
    for i, line in enumerate(result):
        if i > 0 and MAJOR_SECTION.match(line):
            spaced.append('')
        spaced.append(line)

    return '\n'.join(spaced).strip()


def _extract_text(record: dict) -> str | None:
    """Turn a getBillText record (mime + raw bytes) into normalized plain text.
    PDF via pypdf, HTML via BeautifulSoup, plain text decoded directly."""
    raw = record.get("bytes")
    mime = (record.get("mime") or "").lower()
    if not raw:
        return None

    try:
        if "pdf" in mime:
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(raw))
            text = "\n".join((page.extract_text() or "") for page in reader.pages)
        elif "html" in mime:
            soup = BeautifulSoup(raw, "html.parser")
            text = soup.get_text(separator="\n")
        elif "text" in mime:
            text = raw.decode("utf-8", errors="replace")
        else:
            # Word docs and other binaries — no clean extraction path.
            print(f"[STATE FETCHER] Unsupported text mime: {mime}")
            return None
    except Exception as e:
        print(f"[STATE FETCHER] Text extraction error ({mime}): {e}")
        return None

    result = _normalize_bill_text(text)
    return result or None


def fetch_state_bill_text(bill_data):
    """
    Fetch the best available bill text version from LegiScan and extract it.
    Prefers: Chaptered > Enacted > Enrolled > Engrossed > Introduced.
    """
    if not bill_data:
        return None

    texts = bill_data.get("texts") or []
    if not texts:
        return None

    VERSION_PRIORITY = [
        "chaptered", "enacted", "enrolled", "reenrolled",
        "engrossed", "introduced",
    ]

    selected = None
    for priority in VERSION_PRIORITY:
        for t in texts:
            if priority in (t.get("type") or "").lower():
                selected = t
                break
        if selected:
            break
    # Fallback — the most recent text version available.
    if not selected:
        selected = max(texts, key=lambda t: t.get("date") or "")

    doc_id = selected.get("doc_id")
    if not doc_id:
        return None

    with _text_lock:
        if doc_id in _text_cache:
            return _text_cache[doc_id]

    record = legiscan.get_bill_text(doc_id)
    if not record:
        return None

    result = _extract_text(record)
    if result:
        with _text_lock:
            _text_cache[doc_id] = result
    return result


def structure_state_actions(bill_data):
    """
    Convert LegiScan `history` entries to the federal-style timeline structure.
    """
    if not bill_data:
        return []

    history = bill_data.get("history") or []
    if not history:
        return []

    seen = set()
    structured = []

    for a in history:
        text = (a.get("action") or "").strip()
        date = a.get("date", "")
        key = f"{date}:{text[:80]}"

        if key in seen or not text:
            continue
        seen.add(key)

        body = (a.get("chamber") or "").upper()
        chamber = "House" if body == "H" else "Senate" if body == "S" else ""

        event_type = _classify_action(text)

        # LegiScan encodes vote tallies inline as "(46-0)" / "Passed (125-4)".
        yea = nay = None
        vote_match = re.search(r'\((\d+)\s*-\s*(\d+)\)', text)
        if vote_match:
            yea = int(vote_match.group(1))
            nay = int(vote_match.group(2))

        structured.append({
            "date": date,
            "text": make_state_event_title(text, event_type),
            "detail": text if len(text) > 80 else None,
            "chamber": chamber,
            "event_type": event_type,
            "yea": yea,
            "nay": nay,
        })

    return structured


def _classify_action(text: str) -> str:
    """Map a LegiScan action description to a timeline event type."""
    t = text.lower()
    if "veto" in t:
        return "vetoed"
    if any(k in t for k in ("signed by governor", "enacted", "chaptered",
                            "became law", "approved by governor", "signed into law")):
        return "signed"
    if any(k in t for k in ("third reading passed", "final passage", "passed")):
        return "passed"
    if any(k in t for k in ("committee", "favorable report", "reported")):
        return "committee"
    if any(k in t for k in ("referred", "first reading")):
        return "referred"
    if any(k in t for k in ("introduced", "filed", "prefiled", "read first time")):
        return "introduced"
    return "action"


def make_state_event_title(text, event_type):
    """Clean action text into a short readable title."""
    # Truncate at natural breakpoints
    for sep in [' (', '; ', ' - fiscal']:
        if sep in text:
            text = text[:text.index(sep)]

    text = text.rstrip('.,;').strip()

    if len(text) > 100:
        text = text[:100].rsplit(' ', 1)[0] + '…'

    return text
