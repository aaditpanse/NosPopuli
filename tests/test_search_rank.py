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



class EvaluationTest(unittest.TestCase):
    def test_the_pool_order_is_blind_and_stable(self):
        from scripts.search_smoketest import blind_order
        a = blind_order("ai regulation", ["119-s-1", "118-hr-2", "119-s-1"])
        self.assertEqual(sorted(a), ["118-hr-2", "119-s-1"])
        self.assertEqual(a, blind_order("ai regulation", ["118-hr-2", "119-s-1"]))

    def test_recall_counts_only_yes_and_skips_half_judged_queries(self):
        from scripts.search_smoketest import score
        pool = [{"query": "q1", "old": ["a", "b"], "new": ["b", "c"], "pool": ["a", "b", "c"]},
                {"query": "q2", "old": ["x"], "new": ["y"], "pool": ["x", "y"]},
                {"query": "q3", "old": ["m"], "new": [], "pool": ["m"]}]
        judged = {"q1": {"a": "no", "b": "yes", "c": "yes"},
                  "q2": {"x": "partly"},                       # y unjudged
                  "q3": {"m": "no"}}                           # nothing relevant
        s = score(pool, judged)
        self.assertEqual((s["queries"], s["unjudged"], s["partly"]), (1, 1, 0))
        self.assertEqual((s["old"], s["new"]), (0.5, 1.0))
        self.assertIsNone(s["per_query"][1]["old"])


class StateSearchDocTest(unittest.TestCase):
    """State bills in bill_doc (plan step 11): a jurisdiction on every row,
    and a result that is not a GovInfo package."""

    def test_a_state_bill_document(self):
        from search.bill_index import state_doc
        b = {"identifier": "HB 1", "title": "Minimum wage; increases incrementally.",
             "other_titles": [], "subjects": ["Labor"], "first_action_date": "2025-11-17",
             "latest_action": "Approved", "latest_action_date": "2026-04-08",
             "abstracts": [{"abstract": "An Act to amend and reenact § 40.1-28.10", "note": "title"},
                           {"abstract": "Minimum wage. Increases the minimum wage to $15.00.", "note": None}],
             "sponsors": [{"name": "Jeion A. Ward", "person": "ocd-person/a", "primary": True}],
             "actions": [{"description": "Approved by Governor-Chapter 350", "classification": ["executive-signature"]}]}
        d = state_doc("va", "2026", "hb/1", b)
        self.assertEqual((d["instrument_id"], d["jurisdiction"], d["session"], d["congress"]),
                         ("instrument/va/2026/hb/1", "ocd-division/country:us/state:va", "2026", None))
        self.assertEqual((d["is_law"], d["law_numbers"], d["sponsor_name"]), (True, ["350"], "Jeion A. Ward"))
        # The title abstract repeats the enacting clause; only the summary is kept.
        self.assertEqual(d["summary"], "Minimum wage. Increases the minimum wage to $15.00.")
        self.assertTrue(d["doc"].startswith("HB 1: Minimum wage"))

    def test_a_state_row_has_no_govinfo_package(self):
        import datetime
        from search.bill_index import as_result
        r = as_result({"jurisdiction": "ocd-division/country:us/state:va", "session": "2026", "congress": None,
                       "bill_type": "hb", "number": "1", "title": "Minimum wage", "is_law": True,
                       "law_numbers": ["350"], "introduced": datetime.date(2025, 11, 17)})
        self.assertNotIn("package_id", r)
        self.assertEqual((r["state"], r["identifier"], r["chapter"], r["is_state_bill"]), ("va", "HB 1", "350", True))
