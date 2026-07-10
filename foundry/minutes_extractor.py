"""Second-source extractor: the clerk's published Meeting Minutes PDF.

Produces *assertions*, not records: independent claims about what happened
at the meeting, in the minutes' own vocabulary (last names, textual results).
The harness reconciles these against the API-derived records (spec module 5,
layer 3). Keeping the two extractors' outputs shaped differently is
deliberate — a shared normalization step would be a place for one source's
bugs to contaminate the other.

Requires the `pdftotext` binary (poppler). Layout mode preserves the
indentation the parser keys on.
"""

import re
import subprocess

# "Aye: 6 - Council Member Coghill, ..." (item votes) and
# "Present 6 - Council Member ..." (roll call, no colon).
VOTE_LABEL_RE = re.compile(
    r"^\s*(Aye|No|Abstain|Absent|Present|Recused):?\s+(\d+)(?:\s*-\s*(.*))?$")
ITEM_START_RE = re.compile(r"^\s+(\d{4}-\d{4})\s+\S")
ENACTMENT_RE = re.compile(r"Enactment No:\s*(\S+)")
DATE_RE = re.compile(
    r"(January|February|March|April|May|June|July|August|September|October"
    r"|November|December)\s+(\d{1,2}),\s+(\d{4})")
MONTHS = {m: i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"], 1)}

POSITION_MAP = {"aye": "aye", "no": "no", "abstain": "abstain",
                "absent": "absent", "present": "present", "recused": "recused"}


def pdf_to_text(pdf_path):
    return subprocess.run(["pdftotext", "-layout", str(pdf_path), "-"],
                          capture_output=True, text=True, check=True).stdout


def _is_name_continuation(ln):
    """A wrapped member list continues on lines that contain nothing but
    names, commas, 'and', and Council Member/President titles — e.g. a lone
    'Warwick'. Anything with lowercase prose, digits, or punctuation is
    item text, not names."""
    s = re.sub(r"Council\s+(Member|President)", "", ln)
    s = re.sub(r"\band\b|,", " ", s)
    toks = s.split()
    return bool(toks) and all(re.fullmatch(r"[A-Z][A-Za-z.'\-]*", t) for t in toks)


TITLE_TOKENS = {"Council", "Member", "President"}


def _last_names(names_text):
    """'Council Member Coghill, ... and Council Member Wilson' -> ['Coghill',
    ..., 'Wilson']. Line wraps can split the title itself ('...and Council' /
    'Member Wilson'), so titles are dropped token-wise — a bare trailing
    'Council' must not become a ghost member."""
    names = []
    for part in re.split(r",|\band\b", names_text):
        tokens = [t for t in part.split() if t not in TITLE_TOKENS]
        if tokens:
            names.append(" ".join(tokens))
    return names


def extract_assertions(text):
    """Parse minutes text into a claims dict:

    {"date": "YYYY-MM-DD",
     "attendance": {"present": [last names], "absent": [...]},
     "items": {file_number: [ {result, votes: {position: {count, members}},
                               enactment} ]}}

    items maps to a *list* because one file number can be acted on more than
    once in a meeting (presented, then given final action).
    """
    lines = [ln.replace("\f", "") for ln in text.splitlines()]
    # Drop repeating page furniture so it can't be mistaken for content.
    lines = [ln for ln in lines
             if not re.match(r"^City Council\s+Meeting Minutes", ln)
             and not re.match(r"^City of Pittsburgh\s+Page \d+", ln)]

    date = None
    for ln in lines[:40]:  # cover page
        m = DATE_RE.search(ln)
        if m:
            date = f"{m.group(3)}-{MONTHS[m.group(1)]:02d}-{int(m.group(2)):02d}"
            break

    assertions = {"date": date, "attendance": {}, "items": {}}
    current = None          # vote-block dict currently being filled
    current_label = None    # which position's member list continuation lines extend
    in_rollcall = False

    def new_item_entry(file_number):
        entry = {"result": None, "votes": {}, "enactment": None}
        assertions["items"].setdefault(file_number, []).append(entry)
        return entry

    for ln in lines:
        if re.match(r"^\s*ROLL CALL\s*$", ln):
            in_rollcall = True
            continue

        m = ITEM_START_RE.match(ln)
        if m:
            current = new_item_entry(m.group(1))
            current_label = None
            in_rollcall = False
            continue

        m = VOTE_LABEL_RE.match(ln)
        if m:
            label, count, names_text = m.group(1).lower(), int(m.group(2)), m.group(3)
            names = _last_names(names_text) if names_text else []
            if in_rollcall:
                if label in ("present", "absent"):
                    assertions["attendance"][label] = names
                    current_label = ("rollcall", label)
            elif current is not None:
                pos = POSITION_MAP[label]
                current["votes"][pos] = {"count": count, "members": names}
                current_label = ("item", pos)
            continue

        # Continuation of a wrapped member list ("... and Council Member\n Warwick")
        if current_label and _is_name_continuation(ln):
            kind, key = current_label
            if kind == "rollcall":
                assertions["attendance"][key].extend(_last_names(ln))
            else:
                current["votes"][key]["members"].extend(_last_names(ln))
            continue
        current_label = None

        if current is not None:
            if "The motion carried" in ln:
                current["result"] = "pass"
            elif "The motion failed" in ln:
                current["result"] = "fail"
            m = ENACTMENT_RE.search(ln)
            if m:
                current["enactment"] = m.group(1)

    return assertions
