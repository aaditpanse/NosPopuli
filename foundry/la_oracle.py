"""Second-source oracle for Los Angeles: the City Clerk's Council File
Management System (CFMS, cityclerk.lacity.org).

Produces assertions in the harness's shape, per meeting. Unlike Pittsburgh's
minutes (same vendor as the API), CFMS is a genuinely independent system
from PrimeGov — clerk's ColdFusion file registry vs the meeting-management
vendor — which makes this the strongest oracle in the lab so far.

Each council-file page carries "Council Vote Information" blocks: meeting
date, vote action, tally, and a member-by-member table. We keep the blocks
matching the meeting date and derive result from the tally (ayes > nays),
the same vendor-neutral rule the extractor contract uses.
"""

import html
import re

CFMS_URL = ("https://cityclerk.lacity.org/lacityclerkconnect/index.cfm"
            "?fa=ccfi.viewrecord&cfnumber={fn}")
VOTE_WORDS = {"YES": "aye", "NO": "no", "ABSENT": "absent",
              "RECUSED": "recused", "EXCUSED": "absent"}
FILE_NUMBER_LINE_RE = re.compile(r"^\s*\(\d+\)\s+(\d{2}-\d{4}(?:-S\d+)?)\s*$",
                                 re.MULTILINE)


def journal_file_numbers(journal_text):
    """Enumerate the file numbers a journal acts on — used only to know
    which CFMS pages to consult, never as assertion content."""
    seen, out = set(), []
    for fn in FILE_NUMBER_LINE_RE.findall(journal_text):
        if fn not in seen:
            seen.add(fn)
            out.append(fn)
    return out


def _tokens(page_html):
    text = re.sub(r"<[^>]+>", "|", page_html)
    return [t for t in (html.unescape(x).strip() for x in text.split("|")) if t]


def parse_vote_blocks(page_html):
    """All 'Council Vote Information' blocks on a CFMS page:
    [{date, action, tally, votes: [(NAME, vote_word)]}]"""
    tokens = _tokens(page_html)
    blocks, i = [], 0
    while i < len(tokens):
        if tokens[i] == "Meeting Date:" and "Vote Action:" in tokens[i:i + 12]:
            block = {"date": None, "action": None, "tally": None, "votes": []}
            m = re.match(r"(\d{2})/(\d{2})/(\d{4})", tokens[i + 1])
            if m:
                block["date"] = f"{m.group(3)}-{m.group(1)}-{m.group(2)}"
            j = i + 1
            while j < len(tokens) and tokens[j] != "Member Name":
                if tokens[j] == "Vote Action:":
                    block["action"] = re.sub(r"\s+", " ", tokens[j + 1]).strip()
                if tokens[j] == "Vote Given:":
                    block["tally"] = tokens[j + 1]
                j += 1
            # member table: NAME | CD | VOTE triplets after the header row
            j += 3  # skip "Member Name", "CD", "Vote"
            while j + 2 < len(tokens) and tokens[j + 2].upper() in VOTE_WORDS:
                block["votes"].append((tokens[j], tokens[j + 2].upper()))
                j += 3
            blocks.append(block)
            i = j
        else:
            i += 1
    return blocks


def extract_assertions(rt, meeting_date, file_numbers):
    """Assertions for one meeting, in the harness reconcile shape."""
    items = {}
    for fn in file_numbers:
        entries = []
        for block in parse_vote_blocks(rt.fetch_text(CFMS_URL.format(fn=fn))):
            if block["date"] != meeting_date or not block["votes"]:
                continue
            votes = {}
            for name, word in block["votes"]:
                pos = VOTE_WORDS[word]
                votes.setdefault(pos, {"count": 0, "members": []})
                votes[pos]["count"] += 1
                votes[pos]["members"].append(name)
            ayes = votes.get("aye", {}).get("count", 0)
            noes = votes.get("no", {}).get("count", 0)
            entries.append({"result": "pass" if ayes > noes else "fail",
                            "votes": votes, "enactment": None,
                            "cfms_action": block["action"]})
        if entries:
            items[fn] = entries
    # CFMS has no meeting-attendance concept; the reconciler skips empty
    # attendance. Date checks run per-vote via block filtering above.
    return {"date": meeting_date, "attendance": {}, "items": items}
