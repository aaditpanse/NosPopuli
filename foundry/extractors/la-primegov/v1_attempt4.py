"""Deterministic extractor for source `la-primegov` (Los Angeles City Council,
PrimeGov public portal). Fills the Foundry domain schema v1 (schema.py).

All I/O goes through the injected runtime `rt` (fetch_json / fetch_text).
Stdlib only, no direct network or file access.
"""

import re
import hashlib
import unicodedata

EXTRACTOR_VERSION = "1"
SOURCE_ID = "la-primegov"
SCHEMA_VERSION = "1.1"

BASE = "https://lacity.primegov.com"
LIST_URL = BASE + "/api/v2/PublicPortal/ListArchivedMeetings"
DOC_URL = BASE + "/Public/CompiledDocument/{}"

# Search window for ListArchivedMeetings when locating requested meeting ids.
YEAR_MAX = 2035
YEAR_MIN = 2015

# Characters the PDF->text conversion sprinkles inside names / words.
ZERO_WIDTH_RE = re.compile(r"[\u200b\u200c\u200d\ufeff\u00ad]")

# Page footer: e.g. "Tuesday       - June 16, 2026 -       PAGE 2"
FOOTER_RE = re.compile(
    r"-\s+[A-Za-z]+\s+\d{1,2},\s+\d{4}\s+-\s+PAGE\s+\d+", re.IGNORECASE
)

# Numbered item marker with a file number: "(1)    26-0008-S10"
ITEM_MARKER_RE = re.compile(
    r"^[ \t]*\((\d+)\)\s+(\d{2,4}-\d{4}(?:-S\d+)?)", re.MULTILINE
)

# Fallback disposition detector (used only when there is no recorded vote).
DISPOSITION_RE = re.compile(
    r"^(Adopted|Received and Filed|Received and filed|Referred|Continued|"
    r"Approved|Denied|Failed|Substitute|Findings|Ordinance|Held|"
    r"Noted and Filed|Note and File|To the Mayor|Public Hearing)\b"
)

# Vote group labels -> schema position vocabulary.
LABEL_TO_POSITION = {
    "ayes": "aye",
    "nays": "no",
    "absent": "absent",
    "abstain": "abstain",
    "present": "present",
    "recused": "recused",
}
VOTE_GROUP_RE = re.compile(
    r"(Ayes|Nays|Absent|Abstain|Present|Recused)\s*:\s*(.*?)\((\d+)\)",
    re.IGNORECASE,
)

# Name suffixes dropped so member identity reconciles with the vote API,
# which canonicalizes to a bare upper-case surname (e.g. "Price Jr." -> PRICE).
NAME_SUFFIXES = {"JR", "SR", "II", "III", "IV", "V"}


def _clean_text(s):
    return ZERO_WIDTH_RE.sub("", s or "")


def _normalize_name(name):
    """Canonicalize a printed name to the reconciliation form: accent-free,
    period-free, suffix-free, upper-case surname text."""
    if not name:
        return ""
    n = unicodedata.normalize("NFKD", name)
    n = "".join(c for c in n if not unicodedata.combining(c))
    n = n.replace(".", " ").upper()
    tokens = [t for t in re.split(r"\s+", n) if t]
    while tokens and tokens[-1] in NAME_SUFFIXES:
        tokens.pop()
    return " ".join(tokens).strip()


def _strip_footers(text):
    lines = text.splitlines()
    kept = [ln for ln in lines if not FOOTER_RE.search(ln)]
    return "\n".join(kept)


def _split_names(s):
    """Split a comma-separated, possibly wrapped name list into clean,
    normalized names."""
    if not s:
        return []
    s = s.strip().rstrip(";").strip()
    if not s:
        return []
    out = []
    for part in s.split(","):
        raw = re.sub(r"\s+", " ", part).strip()
        # Repair hyphenated names split across a wrap ("Soto- Martinez").
        raw = raw.replace("- ", "-")
        name = _normalize_name(raw)
        if name:
            out.append(name)
    return out


def _pick_journal(doclist):
    for d in doclist or []:
        if (d.get("templateName") or "").strip().lower() == "journal":
            return d
    return None


def _find_meetings(rt, ids):
    """Locate the requested PrimeGov meeting ids across archived-year listings."""
    want = set(ids)
    found = {}
    for year in range(YEAR_MAX, YEAR_MIN - 1, -1):
        if not want:
            break
        try:
            data = rt.fetch_json(LIST_URL, params={"year": year})
        except Exception:
            continue
        if not isinstance(data, list):
            continue
        for entry in data:
            eid = str(entry.get("id"))
            if eid in want:
                found[eid] = entry
                want.discard(eid)
    return found


def _parse_rollcall(text):
    """Extract attendance {name: present|absent} from the Roll Call block."""
    flat = re.sub(r"\s+", " ", text)
    m = re.search(
        r"Members Present:\s*(.*?)\((\d+)\)\s*Absent:\s*(.*?)\((\d+)\)", flat
    )
    att = {}
    if not m:
        return att
    for name in _split_names(m.group(1)):
        att[name] = "present"
    for name in _split_names(m.group(3)):
        att[name] = "absent"
    return att


def _title_from_block(block_text):
    flat = re.sub(r"\s+", " ", block_text).strip()
    flat = re.sub(r"^CD\s+\d+\s+", "", flat)  # drop council-district column
    m = re.match(r"(.*?\.)(\s|$)", flat)
    if m and m.group(1).strip():
        return m.group(1).strip()
    return flat[:300].strip()


def _parse_vote(block_text):
    """Return dict with positions/counts/result, or None if no recorded vote.

    counts is stored only for groups that carry named members, so it stays
    consistent with the per-member position tally.
    """
    am = re.search(r"Ayes\s*:", block_text, re.IGNORECASE)
    if not am:
        return None
    region = block_text[am.start():]
    flat = re.sub(r"\s+", " ", region).strip()

    counts = {}
    positions = []
    names_seen = []
    printed = {}
    for gm in VOTE_GROUP_RE.finditer(flat):
        label = gm.group(1).lower()
        position = LABEL_TO_POSITION.get(label)
        if position is None:
            continue
        count = int(gm.group(3))
        printed[position] = printed.get(position, 0) + count
        names = _split_names(gm.group(2))
        if names:
            counts[position] = counts.get(position, 0) + len(names)
            for name in names:
                positions.append({"member": name, "position": position})
                names_seen.append(name)

    if not positions:
        return None

    aye = printed.get("aye", 0)
    no = printed.get("no", 0)
    result = "pass" if aye > no else "fail"
    return {
        "positions": positions,
        "counts": counts,
        "result": result,
        "names": names_seen,
    }


def _find_action(block_text, has_vote):
    lines = block_text.splitlines()
    if has_vote:
        for i, ln in enumerate(lines):
            if re.search(r"Ayes\s*:", ln, re.IGNORECASE):
                j = i - 1
                while j >= 0 and not lines[j].strip():
                    j -= 1
                if j >= 0:
                    return lines[j].strip()
                break
    for ln in lines:
        s = ln.strip()
        if DISPOSITION_RE.match(s):
            return s
    return None


def _parse_items(text):
    matches = list(ITEM_MARKER_RE.finditer(text))
    items = []
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        block_text = text[m.end():end]
        items.append((m.group(1), m.group(2), block_text))
    return items


def extract(rt, meeting_ids):
    ids = [str(m) for m in meeting_ids]
    run_id = "la-primegov-" + hashlib.sha1(
        "|".join(sorted(ids)).encode("utf-8")
    ).hexdigest()[:12]

    def provenance():
        return {
            "source_id": SOURCE_ID,
            "extractor_version": EXTRACTOR_VERSION,
            "run_id": run_id,
            "schema_version": SCHEMA_VERSION,
            "certification": {"status": "uncertified"},
        }

    entries = _find_meetings(rt, ids)

    meeting_records = []
    item_records = []
    vote_records = []
    member_names = set()
    event_ids = []

    for mid in ids:
        entry = entries.get(mid)
        if entry is None:
            continue

        journal = _pick_journal(entry.get("documentList"))
        if journal is None:
            continue

        doc_url = DOC_URL.format(journal.get("id"))
        text = _clean_text(rt.fetch_text(doc_url))
        text = _strip_footers(text)

        date = (entry.get("dateTime") or "")[:10]
        attendance = _parse_rollcall(text)
        for name in attendance:
            member_names.add(name)

        meeting_id = f"la-primegov-{mid}"
        event_ids.append(meeting_id)

        meeting = {
            "record_type": "meeting",
            "meeting_id": meeting_id,
            "body": "City Council",
            "date": date,
            "attendance": attendance,
            "source_url": doc_url,
            "minutes_url": doc_url,
        }
        meeting.update(provenance())
        meeting_records.append(meeting)

        seen_vote_files = set()  # dedupe recorded votes per file number

        for item_no, file_number, block_text in _parse_items(text):
            vote = _parse_vote(block_text)
            action = _find_action(block_text, vote is not None)
            title = _title_from_block(block_text)

            item_id = f"la-primegov-item-{mid}-{item_no}"
            item = {
                "record_type": "agenda_item",
                "item_id": item_id,
                "meeting_id": meeting_id,
                "file_number": file_number,
                "title": title,
                "action": action,
                "result": vote["result"] if vote else None,
            }
            item.update(provenance())
            item_records.append(item)

            if vote and file_number not in seen_vote_files:
                seen_vote_files.add(file_number)
                for name in vote["names"]:
                    member_names.add(name)
                vote_rec = {
                    "record_type": "vote_event",
                    "vote_id": f"la-primegov-vote-{mid}-{item_no}",
                    "meeting_id": meeting_id,
                    "item_id": item_id,
                    "file_number": file_number,
                    "positions": vote["positions"],
                    "counts": vote["counts"],
                    "result": vote["result"],
                }
                vote_rec.update(provenance())
                vote_records.append(vote_rec)

    member_records = []
    for name in sorted(member_names):
        rec = {"record_type": "member", "name": name}
        rec.update(provenance())
        member_records.append(rec)

    records = {
        "meetings": meeting_records,
        "agenda_items": item_records,
        "vote_events": vote_records,
        "members": member_records,
    }

    run_meta = {
        "source_id": SOURCE_ID,
        "extractor_version": EXTRACTOR_VERSION,
        "schema_version": SCHEMA_VERSION,
        "event_ids": event_ids,
        "row_counts": {
            "meeting": len(meeting_records),
            "agenda_item": len(item_records),
            "vote_event": len(vote_records),
            "member": len(member_records),
        },
    }

    return records, run_meta
