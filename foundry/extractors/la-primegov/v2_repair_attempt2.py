"""Deterministic extractor for source `la-primegov` (Los Angeles City Council
on PrimeGov). Produces Foundry domain schema v1.1 records (meeting,
agenda_item, vote_event, member) from the PrimeGov meeting list entry and its
Journal document. Stdlib only; all I/O goes through the injected runtime `rt`.
"""

import re
import hashlib

EXTRACTOR_VERSION = "3"
SOURCE_ID = "la-primegov"

LIST_URL = "https://lacity.primegov.com/api/v2/PublicPortal/ListArchivedMeetings"
DOC_URL = "https://lacity.primegov.com/Public/CompiledDocument/{}"

# Zero-width / soft-hyphen noise that shows up inside names in the PDF text.
_ZW = dict.fromkeys(map(ord, "\u200b\u200c\u200d\u2060\ufeff\u00ad"), None)

# Page footer, e.g. "Tuesday        - June 16, 2026 -        PAGE 1"
_FOOTER_RE = re.compile(r"^\s*[A-Za-z]+\s+-\s+.*-\s+PAGE\s+\d+\s*$")

# Numbered journal item start: "(N)  <file-number>"
_ITEM_RE = re.compile(r"\((\d+)\)\s+(\d{2,4}-\d{4}(?:-S\d+)?)")

# Recorded vote block: Ayes ... (n) ; Nays ... (n) ; Absent ... (n)
_VOTE_RE = re.compile(
    r"Ayes:(.*?)\((\d+)\)\s*;?\s*Nays:(.*?)\((\d+)\)\s*;?\s*Absent:(.*?)\((\d+)\)",
    re.S,
)

# Roll call: "Members Present: ... (n) Absent: ... (n)"
_ROLL_RE = re.compile(
    r"Members Present:(.*?)\((\d+)\)\s*Absent:(.*?)\((\d+)\)", re.S
)

# Fallback disposition detector for items without a recorded vote.
_DISPO_RE = re.compile(
    r"^(Adopted|Approved|Received|Referred|Continued|Denied|Failed|Findings|"
    r"Substitute|Ordinance|Noted?|To Continue)\b",
    re.I,
)


def _clean(t):
    return (t or "").translate(_ZW)


def _strip_footers(t):
    return "\n".join(ln for ln in t.split("\n") if not _FOOTER_RE.match(ln))


def _dewrap(region):
    """Join wrapped lines. A trailing hyphen (a name split like 'Soto-\\nMartinez')
    joins with no separator; everything else joins with a single space."""
    out = ""
    for line in region.split("\n"):
        line = line.strip()
        if not line:
            continue
        if out.endswith("-"):
            out += line
        elif out:
            out += " " + line
        else:
            out = line
    return out


def _names(region):
    text = _dewrap(region)
    res = []
    for part in text.split(","):
        n = part.strip().strip(";").strip()
        if n:
            res.append(n)
    return res


def _collapse(line):
    return re.sub(r"\s+", " ", line).strip()


def _title(body):
    for para in re.split(r"\n\s*\n", body):
        p = _collapse(para)
        if p:
            return p
    return ""


def _last_line_before(body, pos):
    seg = body[:pos]
    for line in reversed(seg.split("\n")):
        s = _collapse(line)
        if s:
            return s
    return ""


def _fallback_dispo(body):
    found = ""
    for line in body.split("\n"):
        s = _collapse(line)
        if s and _DISPO_RE.match(s):
            found = s
    return found


def _parse_attendance(text):
    attendance = {}
    m = _ROLL_RE.search(text)
    if m:
        for n in _names(m.group(1)):
            attendance[n] = "present"
        for n in _names(m.group(3)):
            attendance[n] = "absent"
    return attendance


def _is_all_zero(vm):
    """An all-zero vote block (Ayes: (0); Nays: (0); Absent: (0)) means no vote
    was actually taken (e.g. hearings, announcements)."""
    return (
        int(vm.group(2)) == 0
        and int(vm.group(4)) == 0
        and int(vm.group(6)) == 0
    )


def extract(rt, meeting_ids):
    index = {}
    searched = set()

    def ensure_year(y):
        if y in searched:
            return
        searched.add(y)
        try:
            data = rt.fetch_json(LIST_URL, params={"year": y})
        except Exception:
            data = None
        if isinstance(data, list):
            for e in data:
                if isinstance(e, dict) and "id" in e:
                    index.setdefault(e["id"], e)

    def find(mid):
        if mid in index:
            return index[mid]
        for y in range(2015, 2036):
            ensure_year(y)
            if mid in index:
                return index[mid]
        return None

    run_id = hashlib.sha256(
        (SOURCE_ID + "|" + ",".join(str(m) for m in meeting_ids)).encode("utf-8")
    ).hexdigest()[:16]

    prov = {
        "source_id": SOURCE_ID,
        "extractor_version": EXTRACTOR_VERSION,
        "run_id": run_id,
    }

    meetings = []
    agenda_items = []
    vote_events = []
    event_ids = []
    member_names = set()

    for raw_mid in meeting_ids:
        try:
            mid = int(raw_mid)
        except (TypeError, ValueError):
            mid = raw_mid

        entry = find(mid)
        if entry is None:
            continue

        meeting_id = f"la-primegov-{mid}"
        event_ids.append(meeting_id)

        # Locate the Journal document.
        journal_id = None
        for d in entry.get("documentList") or []:
            if (d.get("templateName") or "").strip().lower() == "journal":
                journal_id = d.get("id")
                break
        journal_url = DOC_URL.format(journal_id) if journal_id is not None else None

        text = ""
        if journal_url:
            try:
                text = _clean(rt.fetch_text(journal_url) or "")
            except Exception:
                text = ""
        text = _strip_footers(text)

        dt = entry.get("dateTime") or ""
        date = dt.split("T")[0] if "T" in dt else dt[:10]

        attendance = _parse_attendance(text)
        for n in attendance:
            member_names.add(n)

        meeting_rec = dict(prov)
        meeting_rec.update({
            "type": "meeting",
            "meeting_id": meeting_id,
            "body": "City Council",
            "date": date,
            "title": entry.get("title") or "City Council Meeting",
            "attendance": attendance,
            "source_url": journal_url,
            "minutes_url": journal_url,
        })
        meetings.append(meeting_rec)

        # Numbered journal items with file numbers.
        matches = list(_ITEM_RE.finditer(text))
        for i, m in enumerate(matches):
            item_no = m.group(1)
            file_number = m.group(2)
            start = m.end()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            body = text[start:end]

            item_id = f"la-primegov-item-{mid}-{item_no}"
            title = _title(body)

            # All matched vote blocks (including all-zero "no vote" blocks).
            all_blocks = list(_VOTE_RE.finditer(body))
            # Real recorded votes only: all-zero blocks mean no vote was taken,
            # so they never become vote_events and never affect the result.
            votes = [vm for vm in all_blocks if not _is_all_zero(vm)]

            item_votes = []
            for seq, vm in enumerate(votes, 1):
                ayes = _names(vm.group(1))
                nays = _names(vm.group(3))
                absent = _names(vm.group(5))
                a_ct = int(vm.group(2))
                n_ct = int(vm.group(4))
                ab_ct = int(vm.group(6))

                dispo = _last_line_before(body, vm.start())

                positions = (
                    [{"member": x, "position": "aye"} for x in ayes]
                    + [{"member": x, "position": "no"} for x in nays]
                    + [{"member": x, "position": "absent"} for x in absent]
                )
                for x in ayes + nays + absent:
                    member_names.add(x)

                # counts mirror the named tally; omit groups with no members
                # (a zero-count group has no named members, so recording a
                # "0" key would spuriously diverge from the position tally).
                counts = {}
                if a_ct:
                    counts["aye"] = a_ct
                if n_ct:
                    counts["no"] = n_ct
                if ab_ct:
                    counts["absent"] = ab_ct

                vote_rec = dict(prov)
                vote_rec.update({
                    "type": "vote_event",
                    "vote_id": f"la-primegov-vote-{mid}-{item_no}-{seq}",
                    "meeting_id": meeting_id,
                    "item_id": item_id,
                    "file_number": file_number,
                    "motion": dispo,
                    "positions": positions,
                    "counts": counts,
                    "result": "pass" if a_ct > n_ct else "fail",
                })
                item_votes.append(vote_rec)

            if votes:
                # A real recorded vote: result/action come from the last vote.
                last = votes[-1]
                action = _last_line_before(body, last.start())
                a_ct = int(last.group(2))
                n_ct = int(last.group(4))
                item_result = "pass" if a_ct > n_ct else "fail"
            elif all_blocks:
                # Only all-zero (no-vote) blocks: no result, but the
                # disposition line before the block still describes the action
                # (e.g. "Public hearing held, no action").
                action = _last_line_before(body, all_blocks[-1].start())
                item_result = None
            else:
                action = _fallback_dispo(body)
                item_result = None

            item_rec = dict(prov)
            item_rec.update({
                "type": "agenda_item",
                "item_id": item_id,
                "meeting_id": meeting_id,
                "file_number": file_number,
                "title": title,
                "action": action,
                "result": item_result,
            })
            agenda_items.append(item_rec)
            vote_events.extend(item_votes)

    # Members: one per distinct person seen, sorted by name.
    members = []
    for name in sorted(member_names):
        rec = dict(prov)
        rec.update({"type": "member", "name": name})
        members.append(rec)

    records = {
        "meetings": meetings,
        "agenda_items": agenda_items,
        "vote_events": vote_events,
        "members": members,
    }

    run_meta = {
        "source_id": SOURCE_ID,
        "extractor_version": EXTRACTOR_VERSION,
        "schema_version": "1.1",
        "event_ids": event_ids,
        "row_counts": {
            "meeting": len(meetings),
            "agenda_item": len(agenda_items),
            "vote_event": len(vote_events),
            "member": len(members),
        },
    }
    return records, run_meta
