"""
Regex fast-path tests — federal and state bill IDs.

These paths short-circuit the LLM router for the highest-traffic query type
(bill identifiers). Breaking them silently turns every "HB 1234" query into
a slow, expensive LLM round-trip.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agents.router_agent import fast_route, fast_route_state


class FederalFastRoute(unittest.TestCase):
    """fast_route() — federal bill identifier patterns."""

    def assertHit(self, query, expected_type, expected_number):
        result = fast_route(query)
        self.assertIsNotNone(result, f"expected fast-path hit for {query!r}")
        self.assertEqual(result["_fast_path"], "bill_id")
        sb = result["specific_bill"]
        self.assertEqual(sb["type"], expected_type, f"wrong type for {query!r}")
        self.assertEqual(sb["number"], expected_number, f"wrong number for {query!r}")

    def assertMiss(self, query):
        result = fast_route(query)
        self.assertIsNone(result, f"expected fast-path miss for {query!r}")

    # House bills
    def test_house_bill_punctuated(self):     self.assertHit("H.R. 4838", "hr", 4838)
    def test_house_bill_compact(self):        self.assertHit("HR 1", "hr", 1)
    def test_house_bill_spaced(self):         self.assertHit("H. R. 1", "hr", 1)
    def test_house_bill_intro_show_me(self):  self.assertHit("show me hr 1", "hr", 1)
    def test_house_bill_intro_tell(self):     self.assertHit("tell me about HR 9999", "hr", 9999)

    # Senate bills
    def test_senate_bill_punctuated(self):    self.assertHit("S. 2", "s", 2)
    def test_senate_bill_compact(self):       self.assertHit("S 4771", "s", 4771)
    def test_senate_bill_lowercase(self):     self.assertHit("s 1", "s", 1)

    # Joint resolutions
    def test_house_joint_res(self):           self.assertHit("HJRES 12", "hjres", 12)
    def test_senate_joint_res_punct(self):    self.assertHit("S.J.Res. 5", "sjres", 5)

    # Concurrent resolutions
    def test_house_concurrent_res(self):      self.assertHit("HConRes 1", "hconres", 1)
    def test_senate_concurrent_res(self):     self.assertHit("S.Con.Res. 3", "sconres", 3)

    # Simple resolutions
    def test_house_res(self):                 self.assertHit("H.Res. 7", "hres", 7)
    def test_senate_res(self):                self.assertHit("SRes 7", "sres", 7)

    # Edge cases — these MUST NOT trigger fast-path
    def test_topic_query_misses(self):
        self.assertMiss("bills about healthcare")

    def test_member_query_misses(self):
        self.assertMiss("Ted Kennedy")

    def test_named_act_misses(self):
        self.assertMiss("Inflation Reduction Act")

    def test_bill_with_extra_topic_misses(self):
        # Don't fast-path when the bill ID is embedded in a larger question.
        self.assertMiss("what did Ted Kennedy do about S. 2208")

    def test_invalid_number_zero_misses(self):
        self.assertMiss("HR 0")

    def test_invalid_number_too_large_misses(self):
        self.assertMiss("HR 100000")

    def test_empty_query_misses(self):
        self.assertMiss("")

    def test_none_query_misses(self):
        self.assertMiss(None)

    def test_just_letters_misses(self):
        self.assertMiss("HR")

    def test_just_number_misses(self):
        self.assertMiss("1234")

    def test_returns_full_structured_envelope(self):
        """Fast-path must return a fully-formed router output, not a partial."""
        result = fast_route("HR 1")
        self.assertEqual(result["query_type"], "legislation")
        self.assertEqual(result["query_subtype"], "specific_bill")
        self.assertEqual(result["jurisdiction"], "federal")
        self.assertEqual(result["confidence"], 1.0)
        self.assertEqual(result["keywords"], [])
        self.assertEqual(result["status"], "any")
        self.assertIsNone(result["named_entity"])


class StateFastRoute(unittest.TestCase):
    """fast_route_state() — state bill identifiers, with optional session anchor."""

    def assertHit(self, query, expected_id, expected_session=None, state="FL"):
        result = fast_route_state(query, state)
        self.assertIsNotNone(result, f"expected state fast-path hit for {query!r}")
        self.assertEqual(result["_fast_path"], "state_bill_id")
        self.assertEqual(result["specific_bill"]["identifier"], expected_id)
        self.assertEqual(
            result["requested_session"], expected_session,
            f"wrong session for {query!r} (got {result['requested_session']!r})",
        )

    def assertMiss(self, query, state="FL"):
        self.assertIsNone(fast_route_state(query, state), f"expected miss for {query!r}")

    # Basic bill ID formats
    def test_house_bill(self):            self.assertHit("HB 1557", "HB 1557")
    def test_senate_bill(self):           self.assertHit("SB 1", "SB 1")
    def test_house_bill_lowercase(self):  self.assertHit("hb 100", "HB 100")
    def test_house_bill_punctuated(self): self.assertHit("H.B. 99", "HB 99")

    # NY/CA assembly conventions
    def test_california_ab(self):         self.assertHit("AB 414", "AB 414")
    def test_new_york_a(self):            self.assertHit("A 1234", "A 1234")

    # Nebraska / Maine / MN-IA conventions
    def test_nebraska_lb(self):           self.assertHit("LB 947", "LB 947")
    def test_maine_ld(self):              self.assertHit("LD 1", "LD 1")
    def test_minnesota_hf(self):          self.assertHit("HF 2", "HF 2")
    def test_iowa_sf(self):               self.assertHit("SF 100", "SF 100")

    # Intro prefixes
    def test_show_me_intro(self):         self.assertHit("show me hb 1557", "HB 1557")
    def test_find_intro(self):            self.assertHit("find SB 100", "SB 100")
    def test_tell_me_about_intro(self):   self.assertHit("tell me about AB 414", "AB 414")

    # Session anchors — 4-digit years
    def test_year_anchor_from(self):
        self.assertHit("AB 123 from 2023", "AB 123", expected_session="2023")

    def test_year_anchor_in(self):
        self.assertHit("find HF 2 in 2019", "HF 2", expected_session="2019")

    def test_year_anchor_comma(self):
        self.assertHit("HB 1, 2024", "HB 1", expected_session="2024")

    # Session anchors — ordinal sessions
    def test_ordinal_session(self):
        self.assertHit("HB 1 in the 88th session", "HB 1", expected_session="88")

    def test_ordinal_legislature(self):
        self.assertHit("SB 5 in the 90th legislature", "SB 5", expected_session="90")

    def test_ordinal_general_assembly(self):
        self.assertHit("HB 100 from the 95th general assembly", "HB 100", expected_session="95")

    # Edge cases
    def test_topic_query_misses(self):
        self.assertMiss("housing affordability")

    def test_trailing_state_name_misses(self):
        # "HB 1557 in California" — "in California" doesn't fit any session form.
        self.assertMiss("HB 1557 in California")

    def test_empty_misses(self):
        self.assertMiss("")

    def test_none_misses(self):
        self.assertMiss(None)

    def test_zero_number_misses(self):
        self.assertMiss("HB 0")

    def test_too_large_number_misses(self):
        self.assertMiss("HB 100000")

    def test_returns_state_code_uppercased(self):
        result = fast_route_state("HB 1", "fl")
        self.assertEqual(result["state_code"], "FL")

    def test_returns_specific_bill_with_type_and_number(self):
        result = fast_route_state("AB 414 from 2024", "CA")
        sb = result["specific_bill"]
        self.assertEqual(sb["type"], "AB")
        self.assertEqual(sb["number"], 414)
        self.assertEqual(sb["identifier"], "AB 414")

    def test_time_filter_set_only_when_session_anchored(self):
        without_anchor = fast_route_state("HB 1", "FL")
        with_anchor    = fast_route_state("HB 1 from 2022", "FL")
        self.assertFalse(without_anchor["time_filter"])
        self.assertTrue(with_anchor["time_filter"])


if __name__ == "__main__":
    unittest.main()
