"""Certification pass: quarantine -> certified (spec modules 7, 8).

Every record lands quarantined. Promotion requires the second source to
affirm it: vote events by full member-level reconciliation, agenda items by
a certified vote or an affirmed outcome, meetings by clean date/attendance
reconciliation. Anything a reconcile finding touches stays quarantined —
including true source-vs-source disagreements, which are the point.

Returns (findings, summary). Mutates records in place, adding a
`certification` block to every meeting, agenda item, and vote event.
"""

import harness


def certify(records, assertions_by_meeting, adjudicated=None):
    findings = harness.reconcile(records, assertions_by_meeting)
    adjudicated = adjudicated or {}
    disputed = {f["ref"] for f in findings}

    def mark(rec, ok, ref=None):
        note = adjudicated.get(("vote_mismatch", ref)) if ref else None
        rec["certification"] = {"status": "certified" if ok else "quarantined",
                                "method": "cross-source" if ok else None,
                                "note": note}
        return ok

    n = {"certified": 0, "total": 0}
    for meeting in records["meetings"]:
        mid = meeting["meeting_id"]
        ok = mid in assertions_by_meeting and mid not in disputed
        n["certified"] += mark(meeting, ok)
        n["total"] += 1

    vote_status = {}
    for ve in records["vote_events"]:
        mid, ref = ve["meeting_id"], f"{ve['meeting_id']}/{ve['file_number']}"
        ok = mid in assertions_by_meeting and ref not in disputed and mid not in disputed
        n["certified"] += mark(ve, ok, ref)
        n["total"] += 1
        vote_status[(mid, ve["file_number"])] = ok

    for item in records["agenda_items"]:
        mid, fn = item["meeting_id"], item["file_number"]
        ok = vote_status.get((mid, fn), False)
        if not ok and item.get("result") is not None:
            entries = assertions_by_meeting.get(mid, {}).get("items", {}).get(fn, [])
            ok = any(e["result"] == item["result"] for e in entries)
        n["certified"] += mark(item, ok)
        n["total"] += 1

    return findings, n
