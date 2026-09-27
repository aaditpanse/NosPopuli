"""State vote mapper tests — floor roll-call selection + seat mapping, on the
Open States votes file's shape (plan of 2026-09-27; the LegiScan shape is gone).

Two units:
- select_floor_roll_call(votes, chamber_class, state_code): picks the floor
  vote from a bill's roll calls, excluding committee and subcommittee votes
  (by motion) and guarding against low-turnout non-floor votes.
- map_roll_call(vote, state_code, chamber_class, people_map): turns one roll
  call into the semicircle seat structure.

Guards the real-world bugs we care about:
- A committee tally (e.g. 22-0) must not be shown as the chamber floor vote.
- A chamber with only committee votes must yield None (frontend hides the tile).
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from render.state_vote_mapper import select_floor_roll_call, map_roll_call


def _vote(*, chamber, motion, yes, no, nv=0, abstain=0, date="2026-01-01", vid="v1", result=None, positions=None):
    """One record of derived/states/<st>/votes-<session>.json."""
    return {"id": vid, "bill": "hb/191", "date": date, "motion": motion, "chamber": chamber,
            "result": result or ("pass" if yes > no else "fail"),
            "counts": {"yes": yes, "no": no, "not voting": nv, "abstain": abstain, "other": 0},
            "positions": positions or []}


class SelectFloorVsCommittee(unittest.TestCase):
    """VA HB 191 — committee report must not be picked over floor passage."""

    def setUp(self):
        self.votes = [
            _vote(chamber="lower", motion="Reported from Courts of Justice (22-Y 0-N)", yes=22, no=0,
                  vid="v10", date="2026-02-04"),
            _vote(chamber="lower", motion="H VOTE: Passage (98-Y 0-N)", yes=98, no=0, nv=2, vid="v11",
                  date="2026-02-10"),
            _vote(chamber="lower", motion="Subcommittee recommends reporting", yes=10, no=0, vid="v12",
                  date="2026-01-30"),
        ]

    def test_picks_floor_not_committee(self):
        sel = select_floor_roll_call(self.votes, "lower", "VA")
        self.assertIsNotNone(sel)
        self.assertEqual((sel["id"], sel["counts"]["yes"]), ("v11", 98))

    def test_a_conference_report_is_a_floor_vote(self):
        v = [_vote(chamber="lower", motion="Adopt Conference Committee Report", yes=60, no=38)]
        self.assertIsNotNone(select_floor_roll_call(v, "lower", "VA"))


class ImpossibleTally(unittest.TestCase):
    """TX HB 2, 89R: Open States files the House's 122-13 under the Senate
    too (2025-05-29). A 31-seat chamber cannot cast 136 votes."""

    def test_a_tally_larger_than_the_chamber_is_not_its_vote(self):
        votes = [_vote(chamber="upper", motion="passage", yes=31, no=0, vid="s1", date="2025-05-23"),
                 _vote(chamber="upper", motion="passage", yes=122, no=13, nv=1, vid="s2", date="2025-05-29")]
        self.assertEqual(select_floor_roll_call(votes, "upper", "TX")["id"], "s1")

    def test_nebraska_has_one_chamber_of_49(self):
        v = [_vote(chamber="legislature", motion="Final Reading", yes=43, no=2)]
        self.assertEqual(select_floor_roll_call(v, "legislature", "NE")["counts"]["yes"], 43)


class NoFloorVoteCA(unittest.TestCase):
    """CA SB 1407 — Assembly (lower) has only a committee vote → None."""

    def setUp(self):
        self.votes = [
            _vote(chamber="lower", motion="Assembly Revenue and Taxation Committee - Do pass", yes=8, no=0, vid="v20"),
            _vote(chamber="upper", motion="Senate Floor - Passage (39-0)", yes=39, no=0, nv=1, vid="v21"),
        ]

    def test_lower_returns_none(self):
        self.assertIsNone(select_floor_roll_call(self.votes, "lower", "CA"))

    def test_upper_picks_floor(self):
        sel = select_floor_roll_call(self.votes, "upper", "CA")
        self.assertEqual(sel["id"], "v21")


class CommitteeExclusion(unittest.TestCase):
    def test_reported_committee_excluded(self):
        v = [_vote(chamber="lower", motion="Reported from Judiciary", yes=12, no=0)]
        self.assertIsNone(select_floor_roll_call(v, "lower", "VA"))

    def test_subcommittee_excluded(self):
        v = [_vote(chamber="lower", motion="Subcommittee recommends laying on the table", yes=5, no=0)]
        self.assertIsNone(select_floor_roll_call(v, "lower", "VA"))


class ParticipationGuard(unittest.TestCase):
    """No floor marker + low turnout must not be promoted (VA House = 100)."""

    def test_below_50_pct_no_marker_returns_none(self):
        v = [_vote(chamber="lower", motion="Motion to recommit", yes=30, no=0)]
        self.assertIsNone(select_floor_roll_call(v, "lower", "VA"))

    def test_marker_passes_regardless(self):
        v = [_vote(chamber="lower", motion="Passage", yes=70, no=10)]
        self.assertIsNotNone(select_floor_roll_call(v, "lower", "VA"))


class WrongChamberAndEmpty(unittest.TestCase):
    def test_only_upper_none_for_lower(self):
        v = [_vote(chamber="upper", motion="Passage", yes=30, no=0)]
        self.assertIsNone(select_floor_roll_call(v, "lower", "VA"))

    def test_empty_none(self):
        self.assertIsNone(select_floor_roll_call([], "lower", "VA"))

    def test_none_none(self):
        self.assertIsNone(select_floor_roll_call(None, "lower", "VA"))


class MapRollCall(unittest.TestCase):
    def test_summary_aggregates_not_voting(self):
        result = map_roll_call(_vote(chamber="lower", motion="Passage", yes=50, no=30, nv=10, abstain=2),
                               "VA", "lower")
        self.assertEqual(result["summary"], {"yea": 50, "nay": 30, "present": 2, "not_voting": 10})

    def test_named_seats_from_people_and_the_roll_call(self):
        v = _vote(chamber="lower", motion="Passage", yes=1, no=1,
                  positions=[["ocd-person/a", "J. Doe", "yes"], [None, "John Roe", "no"]])
        result = map_roll_call(v, "VA", "lower", {"ocd-person/a": {"name": "Jane Doe", "party": "Democratic"}})
        named = {(s["name"], s["vote"]) for s in result["seats"] if s["name"]}
        # The roster names a known member; a name-only voter keeps the roll call's name.
        self.assertEqual(named, {("Jane Doe", "yes"), ("John Roe", "no")})

    def test_result_reflects_the_vote(self):
        self.assertEqual(map_roll_call(_vote(chamber="lower", motion="x", yes=1, no=0), "VA", "lower")["result"],
                         "Passed")
        self.assertEqual(map_roll_call(_vote(chamber="lower", motion="x", yes=0, no=1), "VA", "lower")["result"],
                         "Failed")

    def test_none_input_returns_none(self):
        self.assertIsNone(map_roll_call(None, "VA", "lower"))


if __name__ == "__main__":
    unittest.main()
