"""Ledger classifier, funnel, compact titles — no HTTP."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ledger_agent import (
    build_funnel,
    classify_question,
    compact_title,
    extract_state,
    fallback_headline,
    fallback_shelves,
    funnel_stage,
    merge_shelves,
    omit_empty_shelves,
    parse_bill_id,
    stories_from_results,
    build_shelves,
)
from router_agent import intents_from_structured


class ClassifyTests(unittest.TestCase):
    def test_empty_is_home(self):
        self.assertEqual(classify_question("")["plate"], "home")
        self.assertEqual(classify_question("   ")["plate"], "home")

    def test_watch_words(self):
        self.assertEqual(classify_question("watching")["plate"], "watching")
        self.assertEqual(classify_question("Watch")["plate"], "watching")

    def test_stafford_is_uncharted(self):
        out = classify_question("Stafford County")
        self.assertEqual(out["plate"], "uncharted")
        self.assertEqual(out["place"]["slug"], "stafford-bos")
        self.assertEqual(out["state_code"], "VA")

    def test_loudoun_is_uncharted(self):
        self.assertEqual(classify_question("Loudoun")["plate"], "uncharted")

    def test_hr_id_is_bill(self):
        out = classify_question("HR 4218")
        self.assertEqual(out["plate"], "bill")
        self.assertEqual(out["bill_type"], "hr")
        self.assertEqual(out["number"], 4218)

    def test_known_act_is_bill(self):
        out = classify_question("inflation reduction act")
        self.assertEqual(out["plate"], "bill")
        self.assertEqual(out["number"], 5376)

    def test_topic_is_ledger(self):
        out = classify_question("healthcare bills in Virginia")
        self.assertEqual(out["plate"], "ledger")
        self.assertEqual(out["state_code"], "VA")
        self.assertEqual(out["place_name"], "Virginia")

    def test_watch_inside_a_topic_stays_ledger(self):
        self.assertEqual(classify_question("bills I'm watching in Virginia")["plate"], "ledger")

    def test_election_words_are_elections(self):
        out = classify_question("when is the election in Texas")
        self.assertEqual(out["plate"], "elections")
        self.assertEqual(out["state_code"], "TX")
        self.assertEqual(out["place_name"], "Texas")
        self.assertEqual(classify_question("who's running for senate")["plate"], "elections")
        self.assertEqual(classify_question("register to vote")["plate"], "elections")

    def test_elections_take_state_from_context(self):
        out = classify_question("upcoming elections", state_code="va")
        self.assertEqual(out["plate"], "elections")
        self.assertEqual(out["state_code"], "VA")

    def test_election_bill_id_still_bill(self):
        # "HR 1 election" names a bill; the id wins over the election word.
        self.assertEqual(classify_question("HR 4218 election")["plate"], "bill")

    def test_election_topic_without_election_word_is_ledger(self):
        self.assertEqual(classify_question("voting rights bills")["plate"], "ledger")


class FunnelTests(unittest.TestCase):
    def test_stage_from_action(self):
        self.assertEqual(funnel_stage("Introduced in House"), "introduced")
        self.assertEqual(funnel_stage("Referred to the Committee on Energy."), "committee")
        self.assertEqual(funnel_stage("Passed House (218-209)"), "passed")
        self.assertEqual(funnel_stage("Became Public Law No: 119-1."), "law")
        self.assertEqual(funnel_stage("whatever", is_law=True), "law")

    def test_cascade_counts(self):
        rows = [
            {"latest_action": "Introduced in House"},
            {"latest_action": "Referred to committee"},
            {"latest_action": "Passed House"},
            {"latest_action": "Became Public Law", "is_law": True},
        ]
        funnel = {r["id"]: r["n"] for r in build_funnel(rows)}
        self.assertEqual(funnel["all"], 4)
        self.assertEqual(funnel["committee"], 3)
        self.assertEqual(funnel["passed"], 2)
        self.assertEqual(funnel["law"], 1)

    def test_stories_skip_incomplete(self):
        rows = [
            {"congress": 119, "type": "hr", "number": 1, "title": "A bill to require rural hospitals to keep an ER open, and for other purposes.", "latest_action": "Passed House"},
            {"title": "no identity"},
        ]
        stories = stories_from_results(rows)
        self.assertEqual(len(stories), 1)
        self.assertEqual(stories[0]["stage"], "passed")
        self.assertTrue(stories[0]["english_title"].startswith("Require rural hospitals"))
        self.assertEqual(stories[0]["english_text"], "Passed House")


class CompactTests(unittest.TestCase):
    def test_strips_preface(self):
        t = compact_title("A bill to require the Secretary to do a thing, and for other purposes.")
        self.assertTrue(t.startswith("Require"))
        self.assertNotIn("other purposes", t.lower())

    def test_headline_empty(self):
        self.assertIn("Nothing", fallback_headline("x", "Virginia", []))

    def test_extract_west_virginia_before_virginia(self):
        self.assertEqual(extract_state("bills in West Virginia"), "WV")

    def test_parse_senate(self):
        out = parse_bill_id("S. 388")
        self.assertEqual(out["bill_type"], "s")
        self.assertEqual(out["number"], 388)

    def test_missing_foundry_store_is_quarantined(self):
        from ledger_agent import foundry_place_coverage
        cov = foundry_place_coverage("not-a-real-place")
        self.assertFalse(cov["found"])
        self.assertTrue(cov["quarantined"])


class IntentTests(unittest.TestCase):
    def test_kennedy_healthcare_is_mixed(self):
        intents = intents_from_structured({
            "query_type": "legislation",
            "entity_name": "Ted Kennedy",
            "keywords": ["healthcare"],
            "topic": "Kennedy healthcare legislation",
            "confidence": 0.5,
        })
        kinds = {i["kind"] for i in intents}
        self.assertIn("member", kinds)
        self.assertIn("topic", kinds)
        member = next(i for i in intents if i["kind"] == "member")
        self.assertEqual(member["name"], "Ted Kennedy")

    def test_pure_member_is_not_a_topic(self):
        intents = intents_from_structured({
            "query_type": "member",
            "entity_name": "Ted Cruz",
        })
        self.assertEqual(len(intents), 1)
        self.assertEqual(intents[0]["kind"], "member")
        self.assertEqual(intents[0]["name"], "Ted Cruz")

    def test_topic_only(self):
        intents = intents_from_structured({
            "query_type": "legislation",
            "keywords": ["insulin"],
            "topic": "insulin pricing",
        })
        self.assertEqual([i["kind"] for i in intents], ["topic"])
        self.assertEqual(intents[0]["label"], "insulin pricing")

    def test_drops_member_intent_without_entity(self):
        intents = intents_from_structured({
            "query_type": "legislation",
            "entity_name": None,
            "keywords": ["healthcare"],
            "intents": [
                {"kind": "member", "name": "Ted Kennedy"},
                {"kind": "topic", "label": "healthcare"},
            ],
        })
        self.assertEqual([i["kind"] for i in intents], ["topic"])


class ShelfTests(unittest.TestCase):
    def _stories(self):
        return stories_from_results([
            {"congress": 119, "type": "hr", "number": 1, "title": "A bill to cap insulin prices", "latest_action": "Referred to committee"},
            {"congress": 119, "type": "hr", "number": 2, "title": "A bill to expand Medicare", "latest_action": "Passed House"},
        ])

    def test_fail_open_without_client(self):
        stories = self._stories()
        shelves = build_shelves("healthcare", stories, None, client=None)
        self.assertEqual(len(shelves), 1)
        self.assertEqual(shelves[0]["id"], "all")
        self.assertEqual(shelves[0]["item_ids"], ["hr1", "hr2"])

    def test_fail_open_when_haiku_raises(self):
        class Boom:
            class messages:
                @staticmethod
                def create(**kwargs):
                    raise RuntimeError("down")
        stories = self._stories()
        shelves = build_shelves("healthcare", stories, None, client=Boom())
        self.assertEqual(shelves[0]["label"], "These bills")
        self.assertEqual(set(shelves[0]["item_ids"]), {"hr1", "hr2"})

    def test_fields_are_policy_areas_ranked_inside(self):
        stories = [
            {"id": "hr9", "title": "Bridge naming", "policy_area": "Transportation and Public Works"},
            {"id": "s2", "title": "Insulin Price Cap Act", "policy_area": "Health"},
            {"id": "hr1", "title": "Medicare insulin coverage", "policy_area": "Health"},
            {"id": "hr4", "title": "Highway trust fund", "policy_area": "Transportation and Public Works"},
        ]
        shelves = build_shelves("insulin prices", stories)
        labels = [s["label"] for s in shelves]
        self.assertEqual(labels[0], "Health")
        self.assertEqual(shelves[0]["item_ids"], ["s2", "hr1"])
        self.assertEqual(labels[1], "Transportation and Public Works")
        self.assertEqual(shelves[1]["item_ids"], ["hr4", "hr9"])

    def test_singleton_fields_fold_into_also(self):
        stories = [
            {"id": "hr1", "title": "AI Regulation Act", "policy_area": "Science, Technology, Communications"},
            {"id": "hr2", "title": "Self-Improving AI Monitoring Act", "policy_area": "Science, Technology, Communications"},
            {"id": "s1", "title": "Farm bill AI section", "policy_area": "Agriculture and Food"},
        ]
        shelves = build_shelves("AI regulation", stories)
        by_id = {s["id"]: s for s in shelves}
        self.assertEqual(by_id["f0"]["item_ids"], ["hr1", "hr2"])
        self.assertEqual(by_id["also"]["item_ids"], ["s1"])

    def test_one_field_stays_a_single_ranked_list(self):
        stories = [
            {"id": "hr2", "title": "Medicare expansion", "policy_area": "Health"},
            {"id": "hr1", "title": "Insulin Price Cap Act", "policy_area": "Health"},
        ]
        shelves = build_shelves("insulin", stories)
        self.assertEqual(len(shelves), 1)
        self.assertEqual(shelves[0]["id"], "all")
        self.assertEqual(shelves[0]["item_ids"], ["hr1", "hr2"])

    def test_empty_topic_omitted_member_kept(self):
        shelves = omit_empty_shelves([
            {"id": "member", "kind": "member", "label": "Ted Kennedy", "item_ids": []},
            {"id": "empty", "kind": "topic", "label": "Nope", "item_ids": []},
            {"id": "ok", "kind": "topic", "label": "Insulin", "item_ids": ["hr1"]},
        ])
        self.assertEqual([s["id"] for s in shelves], ["member", "ok"])

    def test_leftovers_go_to_also(self):
        stories = self._stories()
        member = {"name": "Ted Kennedy", "bioguide_id": "K000105"}
        merged = merge_shelves(
            [{"id": "s1", "kind": "topic", "label": "Insulin", "item_ids": ["hr1"]}],
            stories,
            member,
        )
        kinds = [s["kind"] for s in merged]
        self.assertEqual(kinds[0], "member")
        by_id = {s["id"]: s for s in merged}
        self.assertEqual(by_id["s1"]["item_ids"], ["hr1"])
        self.assertEqual(by_id["also"]["item_ids"], ["hr2"])

    def test_funnel_still_counts_after_bucketing(self):
        rows = [
            {"latest_action": "Introduced in House"},
            {"latest_action": "Referred to committee"},
            {"latest_action": "Passed House"},
            {"latest_action": "Became Public Law", "is_law": True},
        ]
        stories = stories_from_results([
            {"congress": 119, "type": "hr", "number": i + 1, "title": "A bill", "latest_action": r["latest_action"], "is_law": r.get("is_law")}
            for i, r in enumerate(rows)
        ])
        merge_shelves(
            [{"label": "One", "item_ids": ["hr1", "hr2"]}],
            stories,
        )
        funnel = {r["id"]: r["n"] for r in build_funnel(rows)}
        self.assertEqual(funnel["all"], 4)
        self.assertEqual(funnel["law"], 1)

    def test_member_headline_names_the_person(self):
        from ledger_agent import member_headline
        member = {"name": "Ted Cruz", "party": "R", "state": "Texas"}
        rows = [
            {"latest_action": "Referred to committee"},
            {"latest_action": "Became Public Law", "is_law": True},
        ]
        h = member_headline(member, rows)
        self.assertTrue(h.startswith("Ted Cruz, Republican, Texas."))
        self.assertIn("2 recent bills", h)
        self.assertIn("One became law", h)
        self.assertNotIn("Nothing", h)

    def test_member_headline_no_bills(self):
        from ledger_agent import member_headline
        self.assertIn("No sponsored bills", member_headline({"name": "X"}, []))

    def test_fallback_keeps_member_with_no_bills(self):
        member = {"name": "Ted Cruz", "bioguide_id": "C001098"}
        shelves = fallback_shelves([], member)
        self.assertEqual(len(shelves), 1)
        self.assertEqual(shelves[0]["kind"], "member")


if __name__ == "__main__":
    unittest.main()
