"""Map a state roll call (the Open States votes file) to the semicircle seats.

Two steps, as api.py drives them:
  1. `select_floor_roll_call(votes, chamber_class, state_code)` picks the
     chamber's floor vote on a bill from its roll calls, never a committee's.
  2. `map_roll_call(vote, state_code, chamber_class, people_map)` lays out the
     seats; people_map ({Open States person id: {name, party}}, from
     people.json) names them, and a name-only voter keeps the name the roll
     call gives.

A vote is a record of derived/states/<st>/votes-<session>.json:
{id, bill, date, motion, result, chamber, counts {yes, no, abstain, not
voting, other}, positions [[person id or None, name, option]]}.
"""

import math

# Seat counts per state chamber (lower = House equivalent, upper = Senate equivalent)
STATE_CHAMBERS = {
    "VA": {"lower": 100, "upper": 40},
    "TX": {"lower": 150, "upper": 31},
    "CA": {"lower":  80, "upper": 40},
    "NY": {"lower": 150, "upper": 63},
    "FL": {"lower": 120, "upper": 40},
    "PA": {"lower": 203, "upper": 50},
    "IL": {"lower": 118, "upper": 59},
    "OH": {"lower":  99, "upper": 33},
    "GA": {"lower": 180, "upper": 56},
    "NC": {"lower": 120, "upper": 50},
    "MI": {"lower": 110, "upper": 38},
    "NJ": {"lower":  80, "upper": 40},
    "WA": {"lower":  98, "upper": 49},
    "AZ": {"lower":  60, "upper": 30},
    "TN": {"lower":  99, "upper": 33},
    "MA": {"lower": 160, "upper": 40},
    "IN": {"lower": 100, "upper": 50},
}

STATE_VOTE_COLORS = {
    "yes":        "#2a6e2a",
    "no":         "#8b1a1a",
    "abstain":    "#8b7a1a",
    "absent":     "#c8bfaa",
    "excused":    "#c8bfaa",
    "not voting": "#c8bfaa",
    "other":      "#c8bfaa",
}

# Sort order: yes far-left, no far-right, abstentions in between
_VOTE_SORT = {"yes": 0, "abstain": 1, "other": 2, "not voting": 2,
              "excused": 3, "absent": 3, "no": 4}

def _compute_row_distribution(n_seats, n_rows):
    """Inner rows shorter, outer rows longer (mirrors a real chamber's arc geometry)."""
    weights = [1.0 + i / max(n_rows - 1, 1) for i in range(n_rows)]
    total_w = sum(weights)
    rows = []
    remaining = n_seats
    for w in weights[:-1]:
        count = max(1, round(n_seats * w / total_w))
        rows.append(count)
        remaining -= count
    rows.append(max(1, remaining))
    return rows


def _get_layout(n_seats):
    """Return (n_rows, svgW, svgH, r_start, r_step, cx, cy, dot_r) for given seat count."""
    if n_seats < 35:
        n_rows, r_start, r_step, dot_r = 3, 40, 24, 7.0
    elif n_seats < 70:
        n_rows, r_start, r_step, dot_r = 4, 42, 22, 6.0
    elif n_seats < 130:
        n_rows, r_start, r_step, dot_r = 5, 44, 20, 5.0
    elif n_seats < 200:
        n_rows, r_start, r_step, dot_r = 6, 50, 20, 4.5
    else:
        n_rows, r_start, r_step, dot_r = 8, 55, 20, 4.0

    max_r = r_start + (n_rows - 1) * r_step
    svgW = 2 * max_r + 40
    svgH = max_r + r_step + 15
    cx = svgW // 2
    cy = svgH - 5
    return n_rows, svgW, svgH, r_start, r_step, cx, cy, dot_r


def _semicircle_positions(row_counts, cx, cy, r_start, r_step, angle_padding=0.08):
    positions = []
    for row_idx, count in enumerate(row_counts):
        r = r_start + row_idx * r_step
        for i in range(count):
            t = 0.5 if count == 1 else i / (count - 1)
            angle = math.pi * (1 - angle_padding) - t * math.pi * (1 - 2 * angle_padding)
            x = cx + r * math.cos(angle)
            y = cy - r * math.sin(angle)
            positions.append((round(x, 2), round(y, 2)))
    return positions


# Virginia's committee and subcommittee votes, by how their motion begins.
# A floor motion can name a committee ("Adopt Conference Committee Report"),
# so the word alone is not enough.
_COMMITTEE_MOTIONS = ("reported from", "subcommittee", "continued to next session in", "passed by indefinitely in",
                      "failed to report", "incorporated into", "stricken at request", "committee")
_FLOOR_MARKERS = ("passage", "h vote", "third reading", "final", "concur", "conference report", "accede",
                  "governor's recommendation")


def _is_committee_vote(motion: str) -> bool:
    """Virginia's committee motions begin with a phrase; other states name
    the committee ("Assembly Revenue and Taxation Committee - Do pass"). A
    conference committee's report is voted on the floor."""
    m = (motion or "").strip().lower()
    return m.startswith(_COMMITTEE_MOTIONS) or ("committee" in m and "conference" not in m)


def _has_floor_marker(motion: str) -> bool:
    m = (motion or "").lower()
    return any(p in m for p in _FLOOR_MARKERS)


def _participation(v) -> int:
    return sum(v.get("counts", {}).values())


def select_floor_roll_call(votes, chamber_class, state_code=None):
    """The chamber's floor roll call on a bill, or None.

    Committee votes are excluded by their motion; the rest in the chamber
    rank by (floor marker, participation, date). A pick with no floor
    marker and under half the chamber's seats voting is not the chamber's
    verdict: None, and the page shows no seat map rather than a committee
    tally as the House's.
    """
    candidates = [v for v in votes or [] if v.get("chamber") == chamber_class
                  and not _is_committee_vote(v.get("motion"))]
    if not candidates:
        return None
    target = max(candidates, key=lambda v: (_has_floor_marker(v.get("motion")), _participation(v),
                                            v.get("date") or ""))
    if not _has_floor_marker(target.get("motion")):
        seats = STATE_CHAMBERS.get((state_code or "").upper(), {}).get(chamber_class, 0)
        if seats and _participation(target) < seats * 0.5:
            return None
    return target


def map_roll_call(vote, state_code, chamber_class, people_map=None):
    """A roll call → {seats, summary, svgW, svgH, dot_r, motion, result}, or
    None for no vote."""
    if not vote:
        return None
    people_map = people_map or {}
    counts = vote.get("counts") or {}
    summary = {"yea": counts.get("yes", 0), "nay": counts.get("no", 0),
               "present": counts.get("abstain", 0),
               "not_voting": counts.get("not voting", 0) + counts.get("other", 0)}

    state_data = STATE_CHAMBERS.get((state_code or "").upper(), {})
    n_seats = state_data.get(chamber_class, 0) or _participation(vote) or 40
    n_rows, svgW, svgH, r_start, r_step, cx, cy, dot_r = _get_layout(n_seats)
    positions = _semicircle_positions(_compute_row_distribution(n_seats, n_rows), cx, cy, r_start, r_step)

    individual = vote.get("positions") or []
    if individual:
        ordered = sorted(individual, key=lambda p: (_VOTE_SORT.get(p[2], 2), p[1] or ""))
        seats = []
        for i, (x, y) in enumerate(positions):
            if i < len(ordered):
                pid, name, option = ordered[i]
                person = people_map.get(pid) or {}
                seats.append({"x": x, "y": y, "name": person.get("name") or name or "",
                              "party": person.get("party") or "", "state": state_code, "vote": option,
                              "color": STATE_VOTE_COLORS.get(option, "#c8bfaa"), "source": "state"})
            else:
                seats.append({"x": x, "y": y, "name": "", "party": "", "state": state_code, "vote": "absent",
                              "color": "#c8bfaa", "source": "state"})
    else:
        # No per-member record: fill proportionally from the counts.
        buckets = ([("yes", STATE_VOTE_COLORS["yes"])] * summary["yea"] +
                   [("not voting", STATE_VOTE_COLORS["not voting"])] * summary["not_voting"] +
                   [("abstain", STATE_VOTE_COLORS["abstain"])] * summary["present"] +
                   [("no", STATE_VOTE_COLORS["no"])] * summary["nay"])
        seats = [{"x": x, "y": y, "name": "", "party": "", "state": state_code,
                  "vote": buckets[i][0] if i < len(buckets) else "absent",
                  "color": buckets[i][1] if i < len(buckets) else "#c8bfaa", "source": "state"}
                 for i, (x, y) in enumerate(positions)]

    return {"seats": seats, "summary": summary, "svgW": svgW, "svgH": svgH, "dot_r": dot_r,
            "motion": vote.get("motion") or "", "result": "Passed" if vote.get("result") == "pass" else "Failed",
            "date": vote.get("date"), "vote_id": vote.get("id")}
