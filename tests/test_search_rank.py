"""Deterministic lexical ranking — no HTTP, no LLM."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from search.search_rank import query_stems, rank_by_relevance, relevance_score


class StemTests(unittest.TestCase):
    def test_drops_stopwords_keeps_ai(self):
        stems = query_stems("AI regulation bills")
        self.assertIn("ai", stems)
        self.assertIn("regulation", stems)
        self.assertNotIn("bills", stems)

    def test_extra_terms_append(self):
        stems = query_stems("AI", extra=["artificial intelligence"])
        self.assertEqual(stems, ["ai"])


class ScoreTests(unittest.TestCase):
    def test_phrase_beats_a_passing_mention(self):
        stems = query_stems("AI regulation")
        about = relevance_score("Self-Improving AI Monitoring Act", stems)
        mention = relevance_score("Postal Service AI mailbox study", stems)
        named = relevance_score("Artificial Intelligence Regulation Act of 2026", stems)
        self.assertGreater(named, about)
        self.assertGreater(named, mention)

    def test_same_inputs_same_score(self):
        stems = query_stems("insulin prices")
        a = relevance_score("A bill to cap insulin prices", stems)
        b = relevance_score("A bill to cap insulin prices", stems)
        self.assertEqual(a, b)
        self.assertGreater(a, 0)


class RankTests(unittest.TestCase):
    def test_most_relevant_first_then_newer(self):
        rows = [
            {"type": "hr", "number": 9, "title": "Post office naming", "date": "2026-09-01"},
            {"type": "s", "number": 1, "title": "AI Regulation Act", "date": "2025-01-01"},
            {"type": "hr", "number": 2, "title": "Artificial Intelligence Regulation Act", "date": "2024-06-01"},
            {"type": "hr", "number": 3, "title": "Artificial Intelligence Regulation Act", "date": "2026-01-01"},
        ]
        ranked = rank_by_relevance(rows, "AI regulation bills")
        ids = [(r["type"], r["number"]) for r in ranked]
        self.assertEqual(ids[0], ("hr", 3))
        self.assertEqual(ids[1], ("s", 1))
        self.assertEqual(ids[2], ("hr", 2))
        self.assertEqual(ids[-1], ("hr", 9))

    def test_stable_across_input_order(self):
        rows = [
            {"type": "hr", "number": 1, "title": "Medicare insulin cap"},
            {"type": "hr", "number": 2, "title": "Bridge naming"},
            {"type": "s", "number": 8, "title": "Insulin Price Cap Act"},
        ]
        a = [r["number"] for r in rank_by_relevance(rows, "insulin prices")]
        b = [r["number"] for r in rank_by_relevance(list(reversed(rows)), "insulin prices")]
        self.assertEqual(a, b)
        self.assertEqual(a[0], 8)


class EmbeddingBatchTest(unittest.TestCase):
    def test_requests_respect_the_input_and_size_caps(self):
        from search.bill_index import batches
        docs = [(str(i), "x" * 100) for i in range(10)]
        self.assertEqual([len(b) for b in batches(docs, max_inputs=4, max_chars=10_000)], [4, 4, 2])
        self.assertEqual([len(b) for b in batches(docs, max_inputs=100, max_chars=250)], [2, 2, 2, 2, 2])
        # One document over the budget still travels, alone.
        self.assertEqual([len(b) for b in batches([("a", "x" * 999), ("b", "y")], max_chars=500)], [1, 1])


class FusionTest(unittest.TestCase):
    def test_a_bill_in_both_lists_beats_a_bill_first_in_one(self):
        from search.bill_index import rrf
        self.assertEqual(rrf(["a", "b", "c"], ["b", "d"]), ["b", "a", "d", "c"])

    def test_ties_break_on_the_id_whatever_the_input_order(self):
        from search.bill_index import rrf
        self.assertEqual(rrf(["y"], ["x"]), ["x", "y"])
        self.assertEqual(rrf(["x"], ["y"]), ["x", "y"])
        self.assertEqual(rrf([], []), [])

    def test_full_text_terms_spell_out_the_folded_phrases(self):
        from search.bill_index import fts_terms, _tsquery_sql
        self.assertEqual(fts_terms("AI regulation bills"),
                         [["ai", "artificial intelligence"], ["regulation"]])
        expr, args = _tsquery_sql(fts_terms("AI regulation"), "&&")
        self.assertEqual(expr.count("%s"), len(args))
        self.assertEqual(args, ["ai", "artificial intelligence", "regulation"])

    def test_a_row_keeps_the_shape_search_bills_returned(self):
        import datetime
        from search.bill_index import as_result
        r = as_result({"congress": 110, "bill_type": "hr", "number": "6", "title": "Energy Act",
                       "introduced": datetime.date(2007, 1, 12), "is_law": True,
                       "law_numbers": ["110-140"], "policy_area": "Energy"})
        self.assertEqual(r["package_id"], "BILLS-110hr6")
        self.assertEqual((r["number"], r["law_number"], r["date_issued"]), (6, "140", "2007-01-12"))
        self.assertIsNone(as_result({"congress": 119, "bill_type": "s", "number": "5",
                                     "title": None, "law_numbers": None})["law_number"])

