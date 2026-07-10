"""Validation harness — the meter (spec module 5).

Three layers, each returning findings:

  structural — schema conformance per record (delegates to schema.py)
  consistency — internal cross-record checks: referential integrity,
                vote arithmetic, result-vs-tally sanity, delta vs prior run
  reconcile  — cross-source: API-derived records vs minutes assertions.
                The only layer that can catch a perfectly-formatted,
                internally-consistent lie.

A finding is {"layer", "check", "ref", "msg"}. The harness only reports;
deciding what a finding means (fail the run, open a repair loop, block
certification) belongs to the caller.
"""

import unicodedata

import schema

# Row-count shrink beyond this fraction vs the prior run is suspicious
# (truncated page, half-loaded source) rather than normal meeting variance.
DELTA_SHRINK_TOLERANCE = 0.5


def _finding(layer, check, ref, msg):
    return {"layer": layer, "check": check, "ref": ref, "msg": msg}


def _position_rows(ve):
    """Well-shaped position rows only. Malformed shapes are the structural
    layer's finding; the deeper layers must not crash on them."""
    rows = ve.get("positions")
    if not isinstance(rows, list):
        return []
    return [p for p in rows
            if isinstance(p, dict) and p.get("member") and isinstance(p["member"], str)]


def structural(records):
    findings = []
    for rtype_plural, rtype in [("meetings", "meeting"), ("agenda_items", "agenda_item"),
                                ("vote_events", "vote_event"), ("members", "member")]:
        for rec in records[rtype_plural]:
            ref = rec.get(f"{rtype}_id") or rec.get("name") or "?"
            for err in schema.structural_errors(rtype, rec):
                findings.append(_finding("structural", "schema", f"{rtype}:{ref}", err))
    return findings


def consistency(records, prior_run_meta=None):
    findings = []
    meetings = {m["meeting_id"]: m for m in records["meetings"]}
    item_by_id = {i["item_id"]: i for i in records["agenda_items"]}

    for item in records["agenda_items"]:
        if item["meeting_id"] not in meetings:
            findings.append(_finding("consistency", "orphan_item", item["item_id"],
                                     f"meeting {item['meeting_id']} not in run"))

    for ve in records["vote_events"]:
        ref = ve["vote_id"]
        meeting = meetings.get(ve["meeting_id"])
        if meeting is None:
            findings.append(_finding("consistency", "orphan_vote", ref,
                                     f"meeting {ve['meeting_id']} not in run"))
            continue

        # Referential integrity: every voter must be on the meeting roster.
        # Compared by canonical key: the same person can be spelled with
        # different diacritics in the roll call vs a vote block of one document.
        roster = {member_key(n) for n in meeting["attendance"]} \
            if isinstance(meeting["attendance"], dict) else set()
        for pos in _position_rows(ve):
            if member_key(pos["member"]) not in roster:
                findings.append(_finding("consistency", "unknown_voter", ref,
                                         f"'{pos['member']}' voted but is not on the roster"))

        # Arithmetic: stored counts must equal the tally of stored positions.
        tally = {}
        for pos in _position_rows(ve):
            tally[pos.get("position")] = tally.get(pos.get("position"), 0) + 1
        if tally != ve["counts"]:
            findings.append(_finding("consistency", "count_mismatch", ref,
                                     f"counts {ve['counts']} != position tally {tally}"))

        # Result sanity: a majority of cast votes should match the outcome.
        ayes, noes = tally.get("aye", 0), tally.get("no", 0)
        if ve["result"] == "pass" and ayes <= noes:
            findings.append(_finding("consistency", "result_vs_tally", ref,
                                     f"result=pass but ayes {ayes} <= noes {noes}"))
        if ve["result"] == "fail" and ayes > noes:
            findings.append(_finding("consistency", "result_vs_tally", ref,
                                     f"result=fail but ayes {ayes} > noes {noes}"))

        # The vote must agree with its agenda item on the basics.
        item = item_by_id.get(ve["item_id"])
        if item is None:
            findings.append(_finding("consistency", "orphan_vote", ref,
                                     f"agenda item {ve['item_id']} not in run"))
        elif item["file_number"] != ve["file_number"]:
            findings.append(_finding("consistency", "file_number_mismatch", ref,
                                     f"vote says {ve['file_number']}, item says {item['file_number']}"))

    if prior_run_meta:
        prior = prior_run_meta["row_counts"]
        for rtype, count in ((k, len(v)) for k, v in records.items()):
            if prior.get(rtype, 0) and count < prior[rtype] * DELTA_SHRINK_TOLERANCE:
                findings.append(_finding("consistency", "row_count_delta", rtype,
                                         f"{count} rows vs {prior[rtype]} in prior run"))
    return findings


GENERATIONAL_SUFFIXES = {"JR", "SR", "II", "III", "IV"}


def member_key(name):
    """Canonical member identity for cross-source and intra-document
    comparison. Sources disagree on case ('BOB BLUMENFIELD' vs 'Blumenfield'),
    diacritics and stray zero-width characters ('Soto-Mart​ínez' in PDF
    text vs 'SOTO-MARTINEZ'), and suffixes ('Price Jr.' vs 'CURREN D. PRICE
    JR.'). Canon: strip invisibles and accents, uppercase, drop periods and
    generational suffixes, keep the last remaining token."""
    name = "".join(ch for ch in name if ch not in "​‌‍﻿")
    name = unicodedata.normalize("NFKD", name)
    name = "".join(ch for ch in name if not unicodedata.combining(ch))
    tokens = [t for t in name.upper().replace(".", "").split()
              if t not in GENERATIONAL_SUFFIXES]
    return tokens[-1] if tokens else ""


_last_name = member_key  # cross-source comparisons always use the canon


def reconcile(records, assertions_by_meeting):
    """Cross-source layer. assertions_by_meeting: {meeting_id: minutes assertions}.

    Compares dates, attendance, per-item vote tallies, per-member positions
    (by last name — the minutes' vocabulary), and outcomes. Also checks
    coverage both directions: a recorded vote in one source missing from the
    other is a finding, not a shrug.
    """
    findings = []
    items_by_meeting = {}
    for ve in records["vote_events"]:
        items_by_meeting.setdefault(ve["meeting_id"], {}) \
                        .setdefault(ve["file_number"], []).append(ve)

    for meeting in records["meetings"]:
        mid = meeting["meeting_id"]
        asserted = assertions_by_meeting.get(mid)
        if asserted is None:
            findings.append(_finding("reconcile", "no_second_source", mid,
                                     "no minutes assertions for this meeting"))
            continue

        if asserted["date"] and asserted["date"] != meeting["date"]:
            findings.append(_finding("reconcile", "date_mismatch", mid,
                                     f"api says {meeting['date']}, minutes say {asserted['date']}"))

        attendance = meeting["attendance"] if isinstance(meeting["attendance"], dict) else {}
        api_att = {s: {_last_name(n) for n, st in attendance.items() if st == s}
                   for s in ("present", "absent")}
        for status in ("present", "absent"):
            minutes_att = {member_key(n) for n in asserted["attendance"].get(status, [])}
            if minutes_att and api_att[status] != minutes_att:
                findings.append(_finding("reconcile", "attendance_mismatch", mid,
                                         f"{status}: api {sorted(api_att[status])} vs "
                                         f"minutes {sorted(minutes_att)}"))

        api_votes = items_by_meeting.get(mid, {})
        minutes_votes = {fn: [e for e in entries if e["votes"]]
                         for fn, entries in asserted["items"].items()}
        minutes_votes = {fn: es for fn, es in minutes_votes.items() if es}

        for fn in sorted(set(api_votes) | set(minutes_votes)):
            in_api, in_minutes = api_votes.get(fn, []), minutes_votes.get(fn, [])
            if not in_minutes:
                findings.append(_finding("reconcile", "vote_coverage", f"{mid}/{fn}",
                                         "recorded vote in api but no vote block in minutes"))
                continue
            if not in_api:
                findings.append(_finding("reconcile", "vote_coverage", f"{mid}/{fn}",
                                         "vote block in minutes but no recorded vote in api"))
                continue
            # One file number can be voted more than once; compare as multisets
            # of canonicalized votes. Positions the minutes assert with names
            # are compared by name; count-only lines (e.g. "No: 0") by count.
            if len(in_api) != len(in_minutes):
                findings.append(_finding("reconcile", "vote_multiplicity", f"{mid}/{fn}",
                                         f"{len(in_api)} api votes vs {len(in_minutes)} in minutes"))
                continue
            api_canon = sorted(_canon_api_vote(ve) for ve in in_api)
            minutes_canon = sorted(_canon_minutes_vote(e) for e in in_minutes)
            if api_canon != minutes_canon:
                findings.append(_finding("reconcile", "vote_mismatch", f"{mid}/{fn}",
                                         f"api {api_canon} vs minutes {minutes_canon}"))
    return findings


def _canon_api_vote(ve):
    by_pos = {}
    for pos in _position_rows(ve):
        by_pos.setdefault(pos.get("position"), set()).add(_last_name(pos["member"]))
    return _canon(ve["result"],
                  {p: (len(names), names) for p, names in by_pos.items()})


def _canon_minutes_vote(entry):
    return _canon(entry["result"],
                  {p: (v["count"], {member_key(m) for m in v["members"]})
                   for p, v in entry["votes"].items() if v["count"] > 0})


def _canon(result, by_pos):
    return (str(result or "?"),
            tuple(sorted((str(p), n, tuple(sorted(names))) for p, (n, names) in by_pos.items())))


def run_all(records, assertions_by_meeting=None, prior_run_meta=None):
    # A broken extractor can return anything at all; the meter reports that,
    # it never crashes on it.
    if not isinstance(records, dict):
        return [_finding("structural", "malformed_root", "records",
                         f"records is {type(records).__name__}; expected an object "
                         "with meetings/agenda_items/vote_events/members arrays")]
    shape = [_finding("structural", "malformed_root", key, "missing or not an array")
             for key in ("meetings", "agenda_items", "vote_events", "members")
             if not isinstance(records.get(key), list)]
    if shape:
        return shape

    findings = structural(records) + consistency(records, prior_run_meta)
    if assertions_by_meeting is not None:
        findings += reconcile(records, assertions_by_meeting)
    return findings
