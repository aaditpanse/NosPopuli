"""Map LegiScan roll-call votes to the semicircle seat format.

Two-step, mirroring how api.py drives it:
  1. `select_floor_roll_call(summaries, chamber_class)` picks the floor vote for
     a chamber from the lightweight `getBill.votes` summaries (no query).
  2. api.py fetches that roll call's detail via `getRollCall` and passes it to
     `map_roll_call(roll_call, state_code, chamber_class, people_map)`.

LegiScan's roll call gives per-legislator `people_id` + `vote_text` but no names,
so callers pass a `people_map` (from `getSessionPeople`, cached) to label seats.
"""

import math
from documentor_agent import log_action

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

# LegiScan vote_text → our seat option vocabulary
_VOTE_TEXT_MAP = {
    "yea": "yes", "yes": "yes", "aye": "yes",
    "nay": "no", "no": "no",
    "nv": "not voting", "not voting": "not voting",
    "absent": "absent", "excused": "excused",
}

# H/S body codes → our chamber classification
_BODY_TO_CLASS = {"H": "lower", "S": "upper"}


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


def _is_committee_vote(desc: str) -> bool:
    d = (desc or "").lower()
    return any(m in d for m in ("committee", "subcommittee"))


def _has_floor_marker(desc: str) -> bool:
    d = (desc or "").lower()
    return any(p in d for p in (
        "floor", "third reading", "final passage", "final reading", "passage",
    ))


def _participation(v) -> int:
    return v.get("total") or (v.get("yea", 0) + v.get("nay", 0)
                              + v.get("nv", 0) + v.get("absent", 0))


def select_floor_roll_call(summaries, chamber_class, state_code=None):
    """Pick the chamber's floor roll-call summary from getBill.votes, or None.

    Excludes committee/subcommittee votes (by desc), then ranks the rest in the
    target chamber by (floor-marker present, participation, date). Guards against
    promoting a low-turnout non-floor vote to 'the chamber's verdict': if the pick
    has no explicit floor marker and participation is under 50% of the chamber's
    seats (when known), returns None.
    """
    if not summaries:
        return None

    body = "H" if chamber_class == "lower" else "S" if chamber_class == "upper" else None

    candidates = [
        v for v in summaries
        if (not body or (v.get("chamber") or "").upper() == body)
        and not _is_committee_vote(v.get("desc"))
    ]
    if not candidates:
        return None

    def _score(v):
        marker = 1 if _has_floor_marker(v.get("desc")) else 0
        return (marker, _participation(v), v.get("date", ""))

    target = max(candidates, key=_score)

    if not _has_floor_marker(target.get("desc")):
        seats = STATE_CHAMBERS.get((state_code or "").upper(), {}).get(chamber_class, 0)
        if seats and _participation(target) < seats * 0.5:
            return None

    return target


def map_roll_call(roll_call, state_code, chamber_class, people_map=None):
    """
    Build a semicircle seat map from a LegiScan getRollCall payload.

    roll_call: {yea, nay, nv, absent, total, desc, passed, votes:[{people_id, vote_text}]}
    people_map: {people_id: {name, party, ...}} for labeling seats (optional).

    Returns {seats, summary, svgW, svgH, dot_r, motion, result} or None.
    """
    if not roll_call:
        return None

    people_map = people_map or {}

    yea    = roll_call.get("yea", 0)
    nay    = roll_call.get("nay", 0)
    nv     = roll_call.get("nv", 0)
    absent = roll_call.get("absent", 0)
    summary = {"yea": yea, "nay": nay, "present": 0, "not_voting": nv + absent}

    # Seat count — state lookup, fall back to this vote's total participation.
    state_data = STATE_CHAMBERS.get((state_code or "").upper(), {})
    n_seats = state_data.get(chamber_class, 0)
    if not n_seats:
        n_seats = roll_call.get("total") or (yea + nay + nv + absent) or 40

    n_rows, svgW, svgH, r_start, r_step, cx, cy, dot_r = _get_layout(n_seats)
    row_counts = _compute_row_distribution(n_seats, n_rows)
    positions = _semicircle_positions(row_counts, cx, cy, r_start, r_step)

    individual = roll_call.get("votes") or []

    if individual:
        def _option(v):
            return _VOTE_TEXT_MAP.get((v.get("vote_text") or "").strip().lower(), "other")

        sorted_votes = sorted(individual, key=lambda v: _VOTE_SORT.get(_option(v), 2))
        seats = []
        for i, (x, y) in enumerate(positions):
            if i < len(sorted_votes):
                v = sorted_votes[i]
                option = _option(v)
                person = people_map.get(v.get("people_id"), {})
                name = person.get("name", "")
                party = person.get("party", "")
                color = STATE_VOTE_COLORS.get(option, "#c8bfaa")
            else:
                option, name, party, color = "absent", "", "", "#c8bfaa"
            seats.append({
                "x": x, "y": y,
                "name": name,
                "party": party,
                "state": state_code,
                "vote": option,
                "color": color,
                "source": "state",
            })
    else:
        # No per-legislator detail — fill proportionally from the counts.
        buckets = (
            [("yes",        STATE_VOTE_COLORS["yes"])]        * yea    +
            [("not voting", STATE_VOTE_COLORS["not voting"])] * nv     +
            [("absent",     STATE_VOTE_COLORS["absent"])]     * absent +
            [("no",         STATE_VOTE_COLORS["no"])]         * nay
        )
        seats = []
        for i, (x, y) in enumerate(positions):
            if i < len(buckets):
                option, color = buckets[i]
            else:
                option, color = "absent", "#c8bfaa"
            seats.append({
                "x": x, "y": y,
                "name": "", "party": "", "state": state_code,
                "vote": option, "color": color, "source": "state",
            })

    log_action(
        agent_name="state_vote_mapper",
        action="map_roll_call",
        input_data={"state": state_code, "chamber": chamber_class, "n_seats": n_seats},
        output_data=summary,
    )

    return {
        "seats":   seats,
        "summary": summary,
        "svgW":    svgW,
        "svgH":    svgH,
        "dot_r":   dot_r,
        "motion":  roll_call.get("desc", ""),
        "result":  "Passed" if roll_call.get("passed") == 1 else "Failed",
    }
