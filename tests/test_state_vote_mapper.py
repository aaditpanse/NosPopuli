"""State vote mapper tests — LegiScan roll-call selection + seat mapping.

Two units:
- select_floor_roll_call(summaries, chamber_class, state_code): picks the floor
  vote from getBill.votes summaries, excluding committee/subcommittee votes (by
  desc) and guarding against low-turnout non-floor votes being promoted.
- map_roll_call(roll_call, state_code, chamber_class, people_map): turns a
  getRollCall payload into the semicircle seat structure.

Guards the real-world bugs we care about:
- A committee tally (e.g. 22-0) must not be shown as the chamber floor vote.
- A chamber with only committee votes must yield None (frontend hides the tile).
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from state_vote_mapper import select_floor_roll_call, map_roll_call


def _summary(*, chamber, desc, yea, nay, nv=0, absent=0, date="2026-01-01", rid=1):
    """A LegiScan getBill.votes summary record."""
    return {
        "roll_call_id": rid,
        "chamber": chamber,          # "H" (lower) / "S" (upper)
        "desc": desc,
        "yea": yea, "nay": nay, "nv": nv, "absent": absent,
        "total": yea + nay + nv + absent,
        "date": date,
        "passed": 1 if yea > nay else 0,
    }


def _roll_call(*, yea, nay, nv=0, absent=0, desc="Floor Vote", passed=1, votes=None):
    """A LegiScan getRollCall payload."""
    return {
        "roll_call_id": 1, "yea": yea, "nay": nay, "nv": nv, "absent": absent,
        "total": yea + nay + nv + absent, "desc": desc, "passed": passed,
        "votes": votes or [],
    }


class SelectFloorVsCommittee(unittest.TestCase):
    """VA HB 191 — committee report must not be picked over floor passage."""

    def setUp(self):
        self.summaries = [
            _summary(chamber="H", desc="House Courts of Justice Committee - Reported (22-0)",
                     yea=22, nay=0, rid=10, date="2026-02-04"),
            _summary(chamber="H", desc="House Floor - Third Reading Passed (98-0)",
                     yea=98, nay=0, nv=2, rid=11, date="2026-02-10"),
            _summary(chamber="H", desc="House Human Services Subcommittee: DO PASS",
                     yea=10, nay=0, rid=12, date="2026-01-30"),
        ]

    def test_picks_floor_not_committee(self):
        sel = select_floor_roll_call(self.summaries, "lower", "VA")
        self.assertIsNotNone(sel)
        self.assertEqual(sel["roll_call_id"], 11)
        self.assertEqual(sel["yea"], 98)

    def test_floor_marker_present_in_pick(self):
        sel = select_floor_roll_call(self.summaries, "lower", "VA")
        self.assertIn("floor", sel["desc"].lower())


class NoFloorVoteCA(unittest.TestCase):
    """CA SB 1407 — Assembly (lower) has only a committee vote → None."""

    def setUp(self):
        self.summaries = [
            _summary(chamber="H", desc="Assembly Revenue and Taxation Committee - Do pass",
                     yea=8, nay=0, rid=20),
            _summary(chamber="S", desc="Senate Floor - Passage (39-0)",
                     yea=39, nay=0, nv=1, rid=21),
        ]

    def test_lower_returns_none(self):
        self.assertIsNone(select_floor_roll_call(self.summaries, "lower", "CA"))

    def test_upper_picks_floor(self):
        sel = select_floor_roll_call(self.summaries, "upper", "CA")
        self.assertIsNotNone(sel)
        self.assertEqual(sel["roll_call_id"], 21)


class CommitteeExclusion(unittest.TestCase):
    def test_reported_committee_excluded(self):
        s = [_summary(chamber="H", desc="Reported from Judiciary Committee", yea=12, nay=0)]
        self.assertIsNone(select_floor_roll_call(s, "lower", "VA"))

    def test_subcommittee_excluded(self):
        s = [_summary(chamber="H", desc="Subcommittee recommends do pass", yea=5, nay=0)]
        self.assertIsNone(select_floor_roll_call(s, "lower", "VA"))


class ParticipationGuard(unittest.TestCase):
    """No floor marker + low turnout must not be promoted (VA House = 100)."""

    def test_below_50_pct_no_marker_returns_none(self):
        s = [_summary(chamber="H", desc="Motion to recommit", yea=30, nay=0)]
        self.assertIsNone(select_floor_roll_call(s, "lower", "VA"))

    def test_marker_passes_regardless(self):
        s = [_summary(chamber="H", desc="House Floor Passage", yea=70, nay=10)]
        sel = select_floor_roll_call(s, "lower", "VA")
        self.assertIsNotNone(sel)


class WrongChamberAndEmpty(unittest.TestCase):
    def test_only_upper_none_for_lower(self):
        s = [_summary(chamber="S", desc="Senate Floor Passage", yea=30, nay=0)]
        self.assertIsNone(select_floor_roll_call(s, "lower", "VA"))

    def test_empty_none(self):
        self.assertIsNone(select_floor_roll_call([], "lower", "VA"))

    def test_none_none(self):
        self.assertIsNone(select_floor_roll_call(None, "lower", "VA"))


class MapRollCall(unittest.TestCase):
    def test_summary_aggregates_nv_and_absent(self):
        rc = _roll_call(yea=50, nay=30, nv=5, absent=5)
        result = map_roll_call(rc, "VA", "lower")
        self.assertEqual(result["summary"],
                         {"yea": 50, "nay": 30, "present": 0, "not_voting": 10})

    def test_named_seats_from_people_map(self):
        rc = _roll_call(yea=1, nay=1, votes=[
            {"people_id": 100, "vote_text": "Yea"},
            {"people_id": 200, "vote_text": "Nay"},
        ])
        people = {100: {"name": "Jane Doe", "party": "D"},
                  200: {"name": "John Roe", "party": "R"}}
        result = map_roll_call(rc, "VA", "lower", people)
        names = {s["name"] for s in result["seats"] if s["name"]}
        self.assertIn("Jane Doe", names)
        self.assertIn("John Roe", names)

    def test_result_reflects_passed_flag(self):
        self.assertEqual(map_roll_call(_roll_call(yea=1, nay=0, passed=1), "VA", "lower")["result"], "Passed")
        self.assertEqual(map_roll_call(_roll_call(yea=0, nay=1, passed=0), "VA", "lower")["result"], "Failed")

    def test_none_input_returns_none(self):
        self.assertIsNone(map_roll_call(None, "VA", "lower"))


if __name__ == "__main__":
    unittest.main()
