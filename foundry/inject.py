"""Planted-error injector (spec module 5).

Deliberately corrupts known-good records to test whether the harness
detects it. Each corruption models a real extractor failure mode and is
tagged with the *weakest layer that should catch it* — the experiment's
job is to verify that layer (or a stronger one) actually fires.

The silent variants are the point of M0: they keep every record
internally consistent (counts recomputed, formats intact), which is
exactly the "confident, internally-consistent, externally-false" drift
the spec says only a second source can catch.
"""

import copy
import random


def _recount(ve):
    counts = {}
    for pos in ve["positions"]:
        counts[pos["position"]] = counts.get(pos["position"], 0) + 1
    ve["counts"] = counts


def _pick_vote(records, rng):
    return rng.choice(records["vote_events"])


def flip_vote_silent(records, rng):
    """'Nay read as yea' with the tally recomputed — pure semantic drift."""
    ve = _pick_vote(records, rng)
    row = rng.choice(ve["positions"])
    row["position"] = "no" if row["position"] == "aye" else "aye"
    _recount(ve)
    return f"{ve['vote_id']}: flipped {row['member']} (counts recomputed)"


def shift_vote_column(records, rng):
    """The classic: member→position mapping rotates by one; tallies unchanged."""
    ve = _pick_vote(records, rng)
    positions = [p["position"] for p in ve["positions"]]
    shifted = positions[1:] + positions[:1]
    for row, pos in zip(ve["positions"], shifted):
        row["position"] = pos
    return f"{ve['vote_id']}: rotated positions across members"


def date_drift(records, rng):
    """Meeting date off by one day — well-formed, plausible, wrong."""
    meeting = rng.choice(records["meetings"])
    y, m, d = meeting["date"].split("-")
    meeting["date"] = f"{y}-{m}-{int(d) + 1:02d}"
    return f"{meeting['meeting_id']}: date shifted to {meeting['date']}"


def flip_vote_clumsy(records, rng):
    """Position flipped but tally left stale — internally contradictory."""
    ve = _pick_vote(records, rng)
    row = rng.choice(ve["positions"])
    row["position"] = "no" if row["position"] == "aye" else "aye"
    return f"{ve['vote_id']}: flipped {row['member']} (counts left stale)"


def drop_voter(records, rng):
    """One member's vote row lost; tally left claiming the old total."""
    ve = _pick_vote(records, rng)
    row = ve["positions"].pop(rng.randrange(len(ve["positions"])))
    return f"{ve['vote_id']}: dropped {row['member']}'s row"


def unknown_voter(records, rng):
    """A vote attributed to someone who isn't on the meeting roster."""
    ve = _pick_vote(records, rng)
    row = rng.choice(ve["positions"])
    row["member"] = "Zebulon Quisling"
    _recount(ve)
    return f"{ve['vote_id']}: voter replaced with a name not on the roster"


def result_flip(records, rng):
    """Outcome inverted on both the vote and its agenda item."""
    ve = _pick_vote(records, rng)
    ve["result"] = "fail" if ve["result"] == "pass" else "pass"
    for item in records["agenda_items"]:
        if item["item_id"] == ve["item_id"]:
            item["result"] = ve["result"]
    return f"{ve['vote_id']}: result inverted to {ve['result']}"


def truncation(records, rng):
    """Page half-loaded: the back 60% of items (and their votes) vanish."""
    keep = len(records["agenda_items"]) * 2 // 5
    kept_ids = {i["item_id"] for i in records["agenda_items"][:keep]}
    records["agenda_items"] = records["agenda_items"][:keep]
    records["vote_events"] = [v for v in records["vote_events"] if v["item_id"] in kept_ids]
    return f"truncated to first {keep} agenda items"


def bad_file_number(records, rng):
    """File number mangled by a formatting change."""
    item = rng.choice(records["agenda_items"])
    item["file_number"] = item["file_number"].replace("-", "")
    return f"{item['item_id']}: file number mangled to {item['file_number']}"


def empty_field(records, rng):
    """A required field comes back blank."""
    item = rng.choice(records["agenda_items"])
    item["title"] = ""
    return f"{item['item_id']}: title blanked"


# name -> (fn, weakest layer expected to detect it)
CORRUPTIONS = {
    "flip_vote_silent": (flip_vote_silent, "reconcile"),
    "shift_vote_column": (shift_vote_column, "reconcile"),
    "date_drift": (date_drift, "reconcile"),
    "flip_vote_clumsy": (flip_vote_clumsy, "consistency"),
    "drop_voter": (drop_voter, "consistency"),
    "unknown_voter": (unknown_voter, "consistency"),
    "result_flip": (result_flip, "consistency"),
    "truncation": (truncation, "consistency"),
    "bad_file_number": (bad_file_number, "structural"),
    "empty_field": (empty_field, "structural"),
}


def plant(records, name, seed):
    """Deep-copy records, apply corruption `name`, return (corrupted, description)."""
    corrupted = copy.deepcopy(records)
    fn, _ = CORRUPTIONS[name]
    return corrupted, fn(corrupted, random.Random(seed))
