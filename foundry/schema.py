"""Foundry domain schema v1 — municipal legislative activity.

The schema is the durable asset; extractors are disposable. Every extractor,
regardless of source, must fill these shapes. Changes here are deliberate and
versioned so downstream data stays interpretable (spec: Feature module 2).

Records are plain dicts. Four record types:

  meeting     — one convening of a body
  agenda_item — one matter acted on at a meeting (procedural items like
                "ROLL CALL" are not records; only items with a file number)
  vote_event  — one recorded roll-call vote on an agenda item, with
                per-member positions AND the tally as separately-stored
                fields (so drift between them is detectable)
  member      — one person who can appear in a roster or a vote

Every record also carries provenance: source id, extractor version, run id,
and a certification block (spec: Feature module 8).
"""

import re

# 1.1: file_number widened for Los Angeles council files ("26-0008-S10");
# v1 assumed Legistar's NNNN-NNNN. First real M3 finding: jurisdiction id
# formats vary, and the schema owns how far that variance is allowed.
# 1.2: file_number OPTIONAL (format-checked when present) — Loudoun and
# Fairfax have no file-number system at all; requiring one made whole
# jurisdictions unrepresentable.
SCHEMA_VERSION = "1.2"

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
FILE_NUMBER_RE = re.compile(r"^\d{2,4}-\d{4}(-S\d+)?$")

# Positions a member can hold on a vote. Extractors normalize into this
# vocabulary; anything else is a structural violation.
POSITIONS = {"aye", "no", "abstain", "absent", "present", "recused"}

# (field, required) per record type. Types/formats are enforced in
# structural_errors below where they matter; a flat required-field list
# covers the rest.
REQUIRED_FIELDS = {
    "meeting": ["meeting_id", "body", "date", "attendance", "source_url"],
    "agenda_item": ["item_id", "meeting_id", "title", "action"],
    "vote_event": ["vote_id", "meeting_id", "positions", "counts", "result"],
    "member": ["name"],
}


def structural_errors(record_type, record):
    """Layer-1 validation: does this record conform to the schema?

    Returns a list of human-readable violation strings (empty = conformant).
    Catches malformed shapes, not wrong facts — a perfectly-formatted lie
    passes this layer by design.
    """
    errs = []
    for field in REQUIRED_FIELDS[record_type]:
        if field not in record or record[field] in (None, "", [], {}):
            errs.append(f"missing/empty required field '{field}'")
    if errs:
        return errs  # shape is broken; format checks would just cascade

    if record_type == "meeting":
        if not isinstance(record["date"], str) or not DATE_RE.match(record["date"]):
            errs.append(f"date {record['date']!r} not YYYY-MM-DD")
        if not isinstance(record["attendance"], dict):
            errs.append("attendance is not an object of name -> status")
        else:
            for name, status in record["attendance"].items():
                if status not in ("present", "absent"):
                    errs.append(f"attendance for '{name}' has bad status '{status}'")

    elif record_type == "agenda_item":
        fn = record.get("file_number")
        if fn is not None and (not isinstance(fn, str) or not FILE_NUMBER_RE.match(fn)):
            errs.append(f"file_number {fn!r} not NNNN-NNNN")
        if record.get("result") not in ("pass", "fail", None):
            errs.append(f"result '{record.get('result')}' not pass/fail/null")

    elif record_type == "vote_event":
        fn = record.get("file_number")
        if fn is not None and (not isinstance(fn, str) or not FILE_NUMBER_RE.match(fn)):
            errs.append(f"file_number {fn!r} not NNNN-NNNN")
        if record["result"] not in ("pass", "fail"):
            errs.append(f"result '{record['result']}' not pass/fail")
        if not isinstance(record["positions"], list) or \
                not all(isinstance(p, dict) for p in record["positions"]):
            errs.append("positions is not a list of {member, position} objects")
        else:
            for pos in record["positions"]:
                if pos.get("position") not in POSITIONS:
                    errs.append(f"position '{pos.get('position')}' not in vocabulary")
                if not pos.get("member"):
                    errs.append("position row with empty member")
        if not isinstance(record["counts"], dict):
            errs.append("counts is not an object of position -> count")
        else:
            for key, n in record["counts"].items():
                if key not in POSITIONS:
                    errs.append(f"counts key '{key}' not in vocabulary")
                if not isinstance(n, int) or n < 0:
                    errs.append(f"counts[{key}]={n!r} not a non-negative int")

    return errs
