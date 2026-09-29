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



class IndexFilterTest(unittest.TestCase):
    """The router's question as the index's filters (api.handle_bill_search)."""

    def f(self, **structured):
        from search.bill_index import index_filters
        return index_filters(structured)

    def test_congresses_before_the_index_are_dropped_and_noted(self):
        self.assertEqual(self.f(congress_numbers=[119, 118]), ([119, 118], False, False, False))
        self.assertEqual(self.f(congress_numbers=[108, 107]), ([108], False, True, False))

    def test_a_question_wholly_before_2003_is_an_honest_empty(self):
        self.assertEqual(self.f(congress_numbers=[92]), (None, False, True, True))
        self.assertEqual(self.f(full_history=True, before_congress=100), (None, False, True, True))

    def test_full_history_searches_every_indexed_congress_and_says_what_it_missed(self):
        self.assertEqual(self.f(full_history=True, congress_numbers=[119]), (None, False, True, False))
        congresses, _, partly, wholly = self.f(full_history=True, before_congress=112)
        self.assertEqual((congresses, partly, wholly), ([108, 109, 110, 111], True, False))

    def test_no_congress_named_is_every_indexed_one(self):
        self.assertEqual(self.f(), (None, False, False, False))

    def test_a_named_act_ignores_the_default_window_but_not_a_named_year(self):
        self.assertEqual(self.f(query_subtype="named_entity", congress_numbers=[119, 118])[0], None)
        self.assertEqual(self.f(query_subtype="named_entity_with_date", congress_numbers=[117])[0], [117])

    def test_either_enacted_mark_means_laws_only(self):
        self.assertTrue(self.f(query_subtype="enacted")[1])
        self.assertTrue(self.f(query_subtype="concept", status="enacted")[1])
        self.assertFalse(self.f(query_subtype="concept", status="any")[1])


class LocalQueryTest(unittest.TestCase):
    """The index's two plain queries, over a fake cursor that keeps the SQL."""

    def run_query(self, fn):
        from unittest import mock
        from search import bill_index
        seen = {}

        class Cur:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def execute(self, sql, args):
                seen["sql"], seen["args"] = " ".join(sql.split()), args

            def fetchall(self):
                return [{"congress": 119, "bill_type": "hr", "number": "5", "title": "A bill", "introduced": None,
                         "law_numbers": None, "jurisdiction": bill_index.US_DIV}]

        class Conn:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def cursor(self, **kw):
                return Cur()

        with mock.patch("correspondence.db._get_pool", return_value=mock.Mock(connection=lambda: Conn())):
            rows = fn(bill_index)
        return rows, seen

    def test_newest_first_with_undated_bills_last(self):
        rows, seen = self.run_query(lambda b: b.recent(congresses=[119], laws_only=True, limit=3))
        self.assertIn("ORDER BY latest_action_date DESC NULLS LAST, instrument_id LIMIT %s", seen["sql"])
        self.assertIn("AND congress = ANY(%s) AND is_law", seen["sql"])
        self.assertEqual(seen["args"][1:], [[119], 3])
        self.assertEqual((rows[0]["type"], rows[0]["number"]), ("hr", 5))

    def test_the_law_of_a_name_is_a_title_match_with_the_wildcards_escaped(self):
        _, seen = self.run_query(lambda b: b.laws_named(" CHIPS_and 100% Act "))
        self.assertIn("is_law AND title ILIKE %s", seen["sql"])
        self.assertEqual(seen["args"][1:], [r"%CHIPS\_and 100 Act%", 3])
        rows, seen = self.run_query(lambda b: b.laws_named("  "))
        self.assertEqual((rows, seen), ([], {}))


class EvaluationTest(unittest.TestCase):
    """scripts/search_smoketest's Tier 2 scoring: pure, so the numbers the
    README reports can be trusted."""

    def test_the_held_out_slice_is_stable_and_about_a_fifth(self):
        from scripts.search_smoketest import eval_questions, held_out
        rows = eval_questions("/nonexistent")
        self.assertEqual(held_out("Tiktok  Ban"), held_out("tiktok ban"))
        self.assertTrue(0.1 < sum(held_out(r["question"]) for r in rows) / len(rows) < 0.3)
        self.assertEqual(len({r["question"].lower() for r in rows}), len(rows))

    def test_a_row_names_its_graph_node(self):
        from scripts.search_smoketest import bill_key
        self.assertEqual(bill_key({"congress": 117, "type": "HR", "number": 5376}), "instrument/us/117/hr/5376")
        self.assertEqual(bill_key({"state": "VA", "session": "2026", "type": "hb", "number": 1}),
                         "instrument/va/2026/hb/1")
        self.assertIsNone(bill_key({"title": "no bill"}))

    def test_todays_answers_map_onto_the_intents(self):
        from scripts.search_smoketest import INTENTS, intent_of
        cases = [({"plate": "graph", "ask": "votes"}, "how_voted"), ({"plate": "graph", "ask": "org_lobbied"}, "organization"),
                 ({"plate": "bill"}, "explain_law"), ({"plate": "uncharted"}, "local_place"),
                 ({"plate": "ledger", "query_type": "member"}, "person_record"),
                 ({"plate": "ledger", "query_type": "state_legislation"}, "find_bills"),
                 ({"plate": "off_topic"}, "off_topic")]
        for plate, want in cases:
            self.assertEqual(intent_of(plate), want, plate)
            self.assertIn(want, INTENTS)

    def test_a_resumed_labelling_run_keeps_every_row(self):
        import json
        import tempfile
        from unittest import mock
        import scripts.search_smoketest as st

        class Pool:
            def connection(self):
                return self

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def cursor(self):
                return self

        qs = [{"question": q, "source": "x", "reader": None} for q in "abcd"]
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump([{"question": "a"}, {"question": "c"}, {"question": "d"}], f)
        with mock.patch.object(st, "eval_questions", return_value=qs), \
                mock.patch.object(st, "label_question", lambda c, cur, row: {**row, "intent": "x", "bills": {}}), \
                mock.patch("correspondence.db._get_pool", return_value=Pool()), mock.patch("anthropic.Anthropic"):
            st.build_labels(f.name)
        self.assertEqual([r["question"] for r in json.load(open(f.name))], ["a", "b", "c", "d"])

    def test_recall_and_ndcg(self):
        from scripts.search_smoketest import ndcg_at, recall_at
        self.assertEqual(recall_at(["a", "b", "c"], ["b", "z"]), 0.5)
        self.assertIsNone(recall_at(["a"], []))
        self.assertEqual(ndcg_at(["a", "b"], {"a": 2, "b": 1}), 1.0)
        self.assertLess(ndcg_at(["b", "a"], {"a": 2, "b": 1}), 1.0)
        self.assertEqual(ndcg_at(["x"], {"a": 2}), 0.0)

    def test_the_score_reads_only_the_held_out_rows_by_default(self):
        from scripts.search_smoketest import score_eval
        labels = [{"question": "q1", "held_out": True, "intent": "find_bills",
                   "entities": [{"node_id": "p1"}], "bills": {"a": "yes", "b": "partly", "c": "no"}},
                  {"question": "q2", "held_out": False, "intent": "money", "entities": [], "bills": {}}]
        preds = {"q1": {"intent": "find_bills", "entities": ["p1"], "bills": ["b", "a"]},
                 "q2": {"intent": "find_bills", "entities": [], "bills": []}}
        s = score_eval(labels, preds)
        self.assertEqual((s["questions"], s["intent"], s["links"], s["recall@10"]), (1, (1.0, 1), (1.0, 1), (1.0, 1)))
        self.assertEqual(score_eval(labels, preds, only_held_out=False)["intent"], (0.5, 2))


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
        # A Massachusetts bill with no first action: no date, not "".
        d = state_doc("ma", "194th", "h/1", {**b, "first_action_date": "", "latest_action_date": ""})
        self.assertEqual((d["introduced"], d["latest_action_date"]), (None, None))

    def test_a_state_row_has_no_govinfo_package(self):
        import datetime
        from search.bill_index import as_result
        r = as_result({"jurisdiction": "ocd-division/country:us/state:va", "session": "2026", "congress": None,
                       "bill_type": "hb", "number": "1", "title": "Minimum wage", "is_law": True,
                       "law_numbers": ["350"], "introduced": datetime.date(2025, 11, 17)})
        self.assertNotIn("package_id", r)
        self.assertEqual((r["state"], r["identifier"], r["chapter"], r["is_state_bill"]), ("va", "HB 1", "350", True))
