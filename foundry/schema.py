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
# 1.3: optional `evidence` on vote_event — the verbatim source passage the
# vote was derived from. Lesson of the Fairfax phantom votes: a quote that
# must literally appear in the fetched document is the cheapest fabrication
# check there is, and it makes every dispute auditable by reading.
# 1.4: new record type `capital_project` — a funded capital works project
# from a county Capital Improvement Program (CIP). The meetings/votes types
# capture what a body DECIDED; this captures what is being BUILT (budget,
# timeline, funding source, location). Separate spine, same provenance and
# certification discipline.
# 1.5: new record type `contest` — one local election (mayor, school board,
# council seat) with its candidate results. Closes the civic loop: the
# election that seated an official, alongside their votes and what's built.
SCHEMA_VERSION = "1.5"

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
    "capital_project": ["project_id", "title", "function", "fiscal_years",
                        "total", "source_url"],
    "contest": ["contest_id", "office", "jurisdiction", "year", "candidates"],
}

# Funding-source codes a capital_project may cite (CIP legend). Extractors
# normalize into this vocabulary; anything else is a parse overrun.
FUNDING_CODES = {"B", "F", "G", "HTF", "R", "S", "SF", "SR", "U", "X"}


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
        ev = record.get("evidence")
        if ev is not None and (
                not isinstance(ev, dict)
                or not isinstance(ev.get("quote"), str) or not ev["quote"].strip()
                or not all(k in ("quote", "doc_url") for k in ev)):
            errs.append("evidence must be {quote: nonempty str, doc_url?: str}")

    elif record_type == "capital_project":
        fy = record["fiscal_years"]
        if not isinstance(fy, dict) or not fy:
            errs.append("fiscal_years is not a non-empty {year -> amount} object")
        else:
            for y, amt in fy.items():
                if not re.match(r"^\d{4}$", str(y)):
                    errs.append(f"fiscal_years key {y!r} is not a 4-digit year")
                if not isinstance(amt, int) or amt < 0:
                    errs.append(f"fiscal_years[{y}]={amt!r} not a non-negative int")
        if not isinstance(record["total"], int) or record["total"] < 0:
            errs.append(f"total {record['total']!r} not a non-negative int")
        # funding_sources vocabulary is jurisdiction-specific (Fairfax uses
        # letter codes; other counties write "General Funds", "GO Bond"), so
        # the field must be a list of strings but its values are not policed.
        fs = record.get("funding_sources", [])
        if not isinstance(fs, list) or not all(isinstance(s, str) for s in fs):
            errs.append("funding_sources must be a list of strings")
        if not isinstance(record.get("districts", []), list):
            errs.append("districts is not a list")

    elif record_type == "contest":
        if not re.match(r"^\d{4}$", str(record["year"])):
            errs.append(f"year {record['year']!r} is not a 4-digit year")
        cands = record["candidates"]
        if not isinstance(cands, list) or not cands:
            errs.append("candidates is not a non-empty list")
        else:
            for c in cands:
                if not isinstance(c, dict) or not c.get("name"):
                    errs.append("a candidate is missing a name")
                v = c.get("votes")
                if v is not None and (not isinstance(v, int) or v < 0):
                    errs.append(f"candidate votes {v!r} not a non-negative int/null")
        tv = record.get("total_votes")
        if tv is not None and (not isinstance(tv, int) or tv < 0):
            errs.append(f"total_votes {tv!r} not a non-negative int/null")

    return errs
