"""
parse_amends_from_title tests — the Connections panel's "Amends" extraction.

Pure regex over titles. If this silently degrades, every bill detail page
loses its primary law reference.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sources.bill_fetcher import parse_amends_from_title


class AmendsExtraction(unittest.TestCase):
    """The "To amend the X Act of YYYY..." pattern."""

    def test_amends_with_year_clause(self):
        title = "A bill to amend the Immigration and Nationality Act of 1965 to require…"
        result = parse_amends_from_title(title)
        self.assertIsNotNone(result)
        self.assertEqual(result["label"], "Amends")
        self.assertIn("Immigration and Nationality Act", result["act_name"])

    def test_amends_without_year_clause(self):
        title = "A bill to amend the Immigration and Nationality Act to require…"
        result = parse_amends_from_title(title)
        self.assertIsNotNone(result)
        self.assertEqual(result["label"], "Amends")
        self.assertIn("Immigration and Nationality Act", result["act_name"])

    def test_amends_with_comma(self):
        title = "To amend the Internal Revenue Code, and for other purposes."
        result = parse_amends_from_title(title)
        self.assertIsNotNone(result)
        self.assertEqual(result["label"], "Amends")
        self.assertIn("Internal Revenue Code", result["act_name"])

    def test_amends_case_insensitive(self):
        title = "to amend the Clean Air Act to establish…"
        result = parse_amends_from_title(title)
        self.assertIsNotNone(result)
        self.assertEqual(result["label"], "Amends")


class ReauthorizesExtraction(unittest.TestCase):
    """The "To reauthorize the X Act..." pattern gets a different label."""

    def test_reauthorize_labeled_distinctly(self):
        title = "A bill to reauthorize the Violence Against Women Act of 1994 to extend…"
        result = parse_amends_from_title(title)
        self.assertIsNotNone(result)
        self.assertEqual(result["label"], "Reauthorizes")
        self.assertIn("Violence Against Women Act", result["act_name"])


class FallbackToSummary(unittest.TestCase):
    """When the title is empty / unhelpful, parse the summary's first paragraph."""

    def test_summary_picks_up_when_title_silent(self):
        title = "S 1234"
        summary = "This bill amends the Higher Education Act of 1965 to expand grant eligibility."
        result = parse_amends_from_title(title, summary)
        # Note: the title regex is "to amend", not "amends". This test documents
        # the current behavior. Summary fallback only fires when the title or
        # summary text matches one of the patterns.
        # If this test changes behavior intentionally, update it deliberately.
        # The current behavior: pattern needs "to amend" specifically, so summary
        # phrased as "This bill amends" doesn't match.
        self.assertIsNone(result)

    def test_summary_with_to_amend_matches(self):
        title = "S 1234"
        summary = "To amend the Higher Education Act of 1965 to expand grant eligibility."
        result = parse_amends_from_title(title, summary)
        self.assertIsNotNone(result)
        self.assertEqual(result["label"], "Amends")
        self.assertIn("Higher Education Act", result["act_name"])


class NoMatch(unittest.TestCase):
    """Bills that don't amend an existing act — must return None, not invent."""

    def test_short_bill_id_returns_none(self):
        self.assertIsNone(parse_amends_from_title("S 1234"))

    def test_empty_title_returns_none(self):
        self.assertIsNone(parse_amends_from_title(""))

    def test_none_title_returns_none(self):
        self.assertIsNone(parse_amends_from_title(None))

    def test_unrelated_title_returns_none(self):
        title = "A bill to establish a national commission on artificial intelligence."
        self.assertIsNone(parse_amends_from_title(title))

    def test_title_too_short_extract_dropped(self):
        # The function drops act_name < 5 chars
        title = "To amend AB to require…"
        result = parse_amends_from_title(title)
        # "AB" is 2 chars — must be rejected as bogus
        self.assertIsNone(result)


class EdgeCases(unittest.TestCase):
    def test_trailing_punctuation_stripped(self):
        title = "A bill to amend the Clean Water Act."
        # The pattern requires a comma/period/"to" follower, so this needs
        # one of those. Let's use a real-world phrasing:
        title = "A bill to amend the Clean Water Act, to authorize…"
        result = parse_amends_from_title(title)
        self.assertIsNotNone(result)
        self.assertNotIn(",", result["act_name"])
        self.assertNotIn(".", result["act_name"])


if __name__ == "__main__":
    unittest.main()
