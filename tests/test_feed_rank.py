"""
Feed ranker + title-gate — geography-agnostic invariants.

Frozen pools stand in for American delegation shapes. No HTTP.
"""

import os
import sys
import unittest
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agents.feed_agent import (
    _title_blocked,
    current_congress,
    dedupe_companions,
    headline_slots,
    one_per_member,
    rank_feed_items,
    select_interest_hits,
    title_matches_interest,
)

NOW = datetime(2026, 9, 5, 12, 0, 0)
CONGRESS = 119
VA10_INTERESTS = ["healthcare", "housing", "veterans"]


def _bill(**kwargs):
    row = {
        "congress": 119,
        "type": "hr",
        "number": 1,
        "title": "Untitled",
        "date": "2026-09-03",
        "latest_action": "Referred to the House Committee on Energy and Commerce.",
        "latest_action_date": "2026-09-03",
        "feed_reason": "healthcare",
        "is_appropriations": False,
        "is_law": False,
    }
    row.update(kwargs)
    return row


def _rank(pool, interests=None):
    return rank_feed_items(pool, interests or VA10_INTERESTS, now=NOW, congress=CONGRESS)


def _ids(bills):
    out = []
    for b in bills:
        if b.get("is_state_bill"):
            out.append(b.get("identifier"))
        else:
            out.append(f"{b.get('type')}{b.get('number')}")
    return out


def _front_ids(ranked):
    lede, top3, _rest = headline_slots(ranked)
    front = [b for b in [lede, *top3] if b]
    return _ids(front)


# ── Shared frozen bills (the Sep 2026 VA-10 audit payload, plus extras) ──

NDAA = _bill(
    number=5009, congress=118, type="hr",
    title="Servicemember Quality of Life Improvement and National Defense Authorization Act for Fiscal Year 2025",
    date="2024-12-28", latest_action_date="2024-12-23",
    latest_action="Became Public Law No: 118-159.",
    feed_reason="veterans", is_law=True, law_number="159",
)
TITLE25 = _bill(
    number=4584,
    title="An act to make technical amendments to update statutory references to certain provisions which were formerly classified to chapters 14 and 19 of title 25, United States Code, and to correct related technical errors.",
    date="2026-08-31", latest_action_date="2026-08-31",
    latest_action="Motion to reconsider laid on the table Agreed to without objection.",
    feed_reason="housing",
)
CR = _bill(
    number=6500,
    title="Continuing Appropriations and Extensions Act, 2027",
    date="2026-09-03", latest_action_date="2026-09-02",
    latest_action="Became Public Law No: 119-103.",
    feed_reason="healthcare", is_appropriations=True, is_law=True, law_number="103",
)
HOSPITAL = _bill(
    number=10293,
    title="Rural Emergency Hospital Designation Improvement Act",
    feed_reason="healthcare",
)
CRISIS_988 = _bill(
    number=10287,
    title="9-8-8 Crisis Response Act",
    feed_reason="healthcare",
)
HOUSING_CREDIT = _bill(
    number=5366, type="s",
    title="Affordable Housing Credit Carryback Act",
    date="2026-08-07", latest_action_date="2026-08-07",
    latest_action="Read twice and referred to the Committee on Finance.",
    feed_reason="housing",
)
TRUTH = _bill(
    number=37, type="hconres",
    title="Urging the establishment of a United States Commission on Truth, Racial Healing, and Transformation.",
    date="2025-06-12", latest_action_date="2025-06-12",
    latest_action="Referred to the House Committee on the Judiciary.",
    feed_reason="veterans",
)
GI_BILL = _bill(
    number=1725,
    title="Sgt. Isaac Woodard, Jr. and Sgt. Joseph H. Maddox GI Bill Restoration Act of 2025",
    date="2025-02-27", latest_action_date="2025-03-27",
    latest_action="Referred to the Subcommittee on Economic Opportunity.",
    feed_reason="veterans",
)
PAYDAYS = _bill(
    number=10290, title="No Pardon Paydays Act of 2026",
    feed_reason="your_rep", sponsor_bioguide="S001230",
)
TRAVEL = _bill(
    number=10289, title="Government Travel Transparency Act",
    feed_reason="your_rep", sponsor_bioguide="S001230",
)
JARED = _bill(
    number=10291, title="JARED Act",
    feed_reason="your_rep", sponsor_bioguide="S001230",
)
WARNER = _bill(
    number=8801, type="s", title="Virginia Port Competitiveness Act",
    date="2026-08-20", latest_action_date="2026-08-20",
    feed_reason="your_rep", sponsor_bioguide="W000805",
)
KAINE = _bill(
    number=8802, type="s", title="Chesapeake Restoration Act",
    date="2026-07-15", latest_action_date="2026-07-15",
    feed_reason="your_rep", sponsor_bioguide="K000384",
)


class CurrentCongress(unittest.TestCase):
    def test_mid_119(self):
        self.assertEqual(current_congress(datetime(2026, 9, 5)), 119)

    def test_day_before_120(self):
        self.assertEqual(current_congress(datetime(2027, 1, 2)), 119)

    def test_opening_day_120(self):
        self.assertEqual(current_congress(datetime(2027, 1, 3)), 120)

    def test_opening_day_119(self):
        self.assertEqual(current_congress(datetime(2025, 1, 3)), 119)

    def test_day_before_119(self):
        self.assertEqual(current_congress(datetime(2025, 1, 2)), 118)


class TitleGate(unittest.TestCase):
    def test_ndaa_blocked(self):
        self.assertTrue(_title_blocked(NDAA["title"]))

    def test_title25_blocked(self):
        self.assertTrue(_title_blocked(TITLE25["title"]))

    def test_cr_blocked(self):
        self.assertTrue(_title_blocked(CR["title"]))

    def test_truth_commission_not_veterans(self):
        self.assertFalse(title_matches_interest(TRUTH["title"], "veterans"))

    def test_hospital_is_healthcare(self):
        self.assertTrue(title_matches_interest(HOSPITAL["title"], "healthcare"))

    def test_988_is_healthcare(self):
        self.assertTrue(title_matches_interest(CRISIS_988["title"], "healthcare"))

    def test_housing_credit_is_housing(self):
        self.assertTrue(title_matches_interest(HOUSING_CREDIT["title"], "housing"))

    def test_gi_bill_is_veterans(self):
        self.assertTrue(title_matches_interest(GI_BILL["title"], "veterans"))

    def test_ndaa_not_veterans_via_servicemember(self):
        self.assertFalse(title_matches_interest(NDAA["title"], "veterans"))


class OnePerMember(unittest.TestCase):
    def test_house_intros_do_not_erase_senators(self):
        kept = one_per_member([PAYDAYS, TRAVEL, JARED, WARNER, KAINE])
        ids = {b["sponsor_bioguide"] for b in kept}
        self.assertEqual(ids, {"S001230", "W000805", "K000384"})
        house = [b for b in kept if b["sponsor_bioguide"] == "S001230"]
        self.assertEqual(len(house), 1)
        self.assertEqual(house[0]["number"], 10290)

    def test_dc_delegate_only(self):
        delegate = _bill(number=9, feed_reason="your_rep", sponsor_bioguide="N000147")
        kept = one_per_member([delegate])
        self.assertEqual(len(kept), 1)

    def test_no_backfill_second_house_bill(self):
        kept = one_per_member([PAYDAYS, TRAVEL, JARED])
        self.assertEqual(len(kept), 1)


class StatusParsing(unittest.TestCase):
    def test_calendar_outranks_referred(self):
        calendar = _bill(
            number=4097, type="s",
            title="State-Based Education Loan Awareness Act",
            date="2026-08-04", latest_action_date="2026-08-04",
            latest_action="Placed on Senate Legislative Calendar under General Orders. Calendar No. 539.",
            feed_reason="education",
        )
        referred = _bill(
            number=5157, type="s",
            title="Reimagining Education and Skills through Unified Longitudinal Talent Systems Act",
            date="2026-07-29", latest_action_date="2026-07-29",
            latest_action="Read twice and referred to the Committee on Health, Education, Labor, and Pensions.",
            feed_reason="education",
        )
        ranked = rank_feed_items(
            [referred, calendar], ["education"], now=NOW, congress=CONGRESS
        )
        self.assertEqual(ranked[0]["number"], 4097)
        self.assertGreater(ranked[0]["feed_score"], ranked[1]["feed_score"])

    def test_reconsider_is_not_passed(self):
        from agents.feed_agent import _is_passed_action, _stage_points
        action = "Motion to reconsider laid on the table Agreed to without objection."
        self.assertFalse(_is_passed_action(action))
        self.assertEqual(_stage_points(TITLE25, NOW, CONGRESS), 0)


class Invariants:
    """Shared checks applied to every persona's ranked list."""

    def assert_invariants(self, ranked, interests):
        lede, top3, rest = headline_slots(ranked)
        front = [b for b in [lede, *top3] if b]
        for bill in front:
            self.assertFalse(bill.get("is_appropriations"), bill.get("title"))
            self.assertFalse(_title_blocked(bill.get("title")), bill.get("title"))
            if bill.get("is_law") and bill.get("congress") and int(bill["congress"]) < CONGRESS:
                self.fail(f"previous-Congress law in headline: {bill.get('title')}")
        rep_bios = [
            b.get("sponsor_bioguide")
            for b in ranked
            if b.get("feed_reason") == "your_rep" and b.get("sponsor_bioguide")
        ]
        self.assertEqual(len(rep_bios), len(set(rep_bios)))
        for bill in ranked:
            reason = bill.get("feed_reason")
            if reason in interests and not bill.get("is_appropriations"):
                self.assertTrue(
                    title_matches_interest(bill.get("title"), reason),
                    f"{bill.get('title')} tagged {reason} without a title match",
                )
        if len(ranked) >= 2:
            scores = [b["feed_score"] for b in ranked]
            self.assertEqual(scores, sorted(scores, reverse=True))


class Va10Audit(unittest.TestCase, Invariants):
    def setUp(self):
        raw = [
            PAYDAYS, TRAVEL, JARED, CR, HOSPITAL, CRISIS_988,
            TITLE25, HOUSING_CREDIT, TRUTH, GI_BILL, NDAA, WARNER, KAINE,
        ]
        gated = [
            b for b in raw
            if b["feed_reason"] == "your_rep" or (
                not _title_blocked(b["title"])
                and title_matches_interest(b["title"], b["feed_reason"])
            )
        ]
        gated = [
            b for b in gated
            if b["feed_reason"] != "your_rep"
        ] + one_per_member([b for b in gated if b["feed_reason"] == "your_rep"])
        self.ranked = _rank(gated)

    def test_invariants(self):
        self.assert_invariants(self.ranked, VA10_INTERESTS)

    def test_ndaa_and_title25_not_lede(self):
        front = _front_ids(self.ranked)
        self.assertNotIn("hr5009", front)
        self.assertNotIn("hr4584", front)

    def test_on_topic_this_week_in_top4(self):
        front = _front_ids(self.ranked)
        self.assertTrue(
            {"hr10293", "hr10287"} & set(front),
            f"expected a this-week healthcare bill in the top 4, got {front}",
        )

    def test_old_law_below_new_referral(self):
        ranked = _rank([HOSPITAL, NDAA])
        by_num = {b["number"]: b for b in ranked}
        self.assertGreater(by_num[10293]["feed_score"], by_num[5009]["feed_score"])
        lede, top3, _ = headline_slots(ranked)
        front_nums = [b["number"] for b in [lede, *top3] if b]
        self.assertNotIn(5009, front_nums)


class BusyCalifornia(unittest.TestCase, Invariants):
    def test_three_house_intros_leave_both_senators(self):
        house = [
            _bill(number=n, title=f"California Process Act {n}",
                  feed_reason="your_rep", sponsor_bioguide="P000613")
            for n in (101, 102, 103)
        ]
        s1 = _bill(number=201, type="s", title="Padilla Water Act",
                   feed_reason="your_rep", sponsor_bioguide="P000145",
                   date="2026-08-01", latest_action_date="2026-08-01")
        s2 = _bill(number=202, type="s", title="Schiff Ethics Act",
                   feed_reason="your_rep", sponsor_bioguide="S001150",
                   date="2026-07-20", latest_action_date="2026-07-20")
        ranked = _rank(one_per_member(house + [s1, s2]) + [
            _bill(number=9, title="Rural Emergency Hospital Designation Improvement Act",
                  feed_reason="healthcare")
        ], ["healthcare"])
        self.assert_invariants(ranked, ["healthcare"])
        bios = {b["sponsor_bioguide"] for b in ranked if b.get("feed_reason") == "your_rep"}
        self.assertEqual(bios, {"P000613", "P000145", "S001150"})


class AtLargeWyoming(unittest.TestCase, Invariants):
    def test_at_large_still_three_people(self):
        pool = one_per_member([
            _bill(number=1, title="Wyoming Range Act", feed_reason="your_rep",
                  sponsor_bioguide="H001096"),
            _bill(number=2, type="s", title="Barrasso Energy Act", feed_reason="your_rep",
                  sponsor_bioguide="B001261"),
            _bill(number=3, type="s", title="Lummis Mining Act", feed_reason="your_rep",
                  sponsor_bioguide="L000571"),
        ]) + [
            _bill(number=40, title="Farm Bill Technical Corrections Act",
                  feed_reason="agriculture"),
        ]
        ranked = _rank(pool, ["agriculture"])
        self.assert_invariants(ranked, ["agriculture"])
        self.assertEqual(sum(1 for b in ranked if b.get("feed_reason") == "your_rep"), 3)


class DcNoSenators(unittest.TestCase, Invariants):
    def test_delegate_only_does_not_crash(self):
        pool = one_per_member([
            _bill(number=12, title="DC Statehood Neighbor Act",
                  feed_reason="your_rep", sponsor_bioguide="N000147"),
        ]) + [
            _bill(number=88, title="Affordable Housing Credit Carryback Act",
                  feed_reason="housing"),
        ]
        ranked = _rank(pool, ["housing"])
        self.assert_invariants(ranked, ["housing"])
        lede, top3, _ = headline_slots(ranked)
        self.assertIsNotNone(lede)


class ElPasoImmigration(unittest.TestCase, Invariants):
    def test_keeps_real_immigration_drops_title25(self):
        border = _bill(
            number=77, title="Border Security and Asylum Processing Act",
            feed_reason="immigration",
        )
        pool = [border, TITLE25, HOUSING_CREDIT]
        gated = [b for b in pool if not _title_blocked(b["title"])
                 and title_matches_interest(b["title"], b["feed_reason"])]
        ranked = _rank(gated, ["immigration", "housing"])
        self.assert_invariants(ranked, ["immigration", "housing"])
        ids = _ids(ranked)
        self.assertIn("hr77", ids)
        self.assertIn("s5366", ids)
        self.assertNotIn("hr4584", ids)


class RuralIowa(unittest.TestCase, Invariants):
    def test_farm_bill_survives_ndaa_does_not(self):
        farm = _bill(number=2, title="Farm Bill of 2026", feed_reason="agriculture")
        gated = [b for b in [farm, NDAA] if not _title_blocked(b["title"])]
        ranked = _rank(gated, ["agriculture", "economy"])
        self.assert_invariants(ranked, ["agriculture", "economy"])
        self.assertEqual(_ids(ranked), ["hr2"])


class MiamiClimateHousing(unittest.TestCase, Invariants):
    def test_recent_referral_beats_old_public_law(self):
        flood = _bill(
            number=44, title="Climate Resilience for Coastal Housing Act",
            feed_reason="climate",
        )
        ranked = _rank([flood, NDAA], ["climate", "housing"])
        self.assert_invariants(ranked, ["climate", "housing"])
        lede, _, _ = headline_slots(ranked)
        self.assertEqual(lede["number"], 44)


class AllTwelveTopics(unittest.TestCase, Invariants):
    def test_lede_is_not_appropriations(self):
        interests = list(__import__("agents.feed_agent", fromlist=["INTEREST_TERMS"]).INTEREST_TERMS.keys())
        pool = [
            CR,
            HOSPITAL,
            HOUSING_CREDIT,
            _bill(number=3, title="Clean Energy Standard Act", feed_reason="climate"),
        ]
        gated = [CR] + [
            b for b in pool
            if b is not CR
            and not _title_blocked(b["title"])
            and title_matches_interest(b["title"], b["feed_reason"])
        ]
        ranked = _rank(gated, interests)
        self.assert_invariants(ranked, [i for i in interests if i in {b["feed_reason"] for b in gated}])
        lede, _, _ = headline_slots(ranked)
        self.assertFalse(lede.get("is_appropriations"))
        self.assertNotEqual(lede["number"], 6500)


class OneTopicOffMember(unittest.TestCase, Invariants):
    def test_front_page_not_empty_and_not_false_positive(self):
        pool = [
            PAYDAYS,
            _bill(number=19, title="Firearm Background Check Expansion Act",
                  feed_reason="gun_policy"),
            TRUTH,
        ]
        gated = [PAYDAYS] + [
            b for b in pool[1:]
            if not _title_blocked(b["title"])
            and title_matches_interest(b["title"], b["feed_reason"])
        ]
        ranked = _rank(gated, ["gun_policy"])
        self.assert_invariants(ranked, ["gun_policy"])
        self.assertTrue(ranked)
        self.assertNotIn("hconres37", _ids(ranked))


class AppropriationsNotLede(unittest.TestCase):
    def test_cr_never_headline(self):
        ranked = _rank([CR, HOSPITAL])
        lede, top3, rest = headline_slots(ranked)
        self.assertNotEqual(lede["number"], 6500)
        self.assertTrue(any(b["number"] == 6500 for b in rest) or CR.get("is_appropriations"))
        self.assertFalse(any(b.get("is_appropriations") for b in [lede, *top3] if b))


class CalendarBeatsIntros(unittest.TestCase):
    def test_on_topic_calendar_outranks_referred(self):
        cal = _bill(
            number=4097, type="s",
            title="State-Based Education Loan Awareness Act",
            latest_action="Placed on Senate Legislative Calendar under General Orders. Calendar No. 539.",
            feed_reason="education",
        )
        intros = [
            _bill(number=n, title="Pell Grant Expansion Act", feed_reason="education")
            for n in (11, 12, 13)
        ]
        ranked = rank_feed_items(intros + [cal], ["education"], now=NOW, congress=CONGRESS)
        self.assertEqual(ranked[0]["number"], 4097)


class FailOpen(unittest.TestCase):
    def test_empty_gate_keeps_two_unblocked(self):
        vague = _bill(number=99, title="To improve certain departmental programs.")
        other = _bill(number=98, title="A bill for miscellaneous purposes.")
        hits = select_interest_hits([NDAA, TITLE25, vague, other], "education")
        self.assertEqual([h["number"] for h in hits], [99, 98])

    def test_blocklist_cannot_fail_open(self):
        hits = select_interest_hits([NDAA, TITLE25, CR], "veterans")
        self.assertEqual(hits, [])

    def test_gate_hit_skips_fail_open(self):
        real = _bill(number=7, title="Pell Grant Expansion Act")
        vague = _bill(number=99, title="To improve certain departmental programs.")
        hits = select_interest_hits([vague, real], "education")
        self.assertEqual([h["number"] for h in hits], [7])


class CompanionDedupe(unittest.TestCase):
    def test_house_senate_same_short_title_keeps_better_stage(self):
        house = _bill(
            number=10209,
            title="Granting Resources for Eliminating Emissions Now in Hospitals Act; GREEN Hospitals Act",
            feed_reason="climate",
        )
        senate = _bill(
            number=5292, type="s",
            title="Granting Resources for Eliminating Emissions Now in Hospitals Act; GREEN Hospitals Act",
            latest_action="Placed on Senate Legislative Calendar under General Orders.",
            feed_reason="climate",
        )
        out = dedupe_companions([house, senate])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["number"], 5292)


class RepHeadlineQuota(unittest.TestCase):
    def test_your_rep_takes_last_3up_not_lede(self):
        pool = [
            HOSPITAL, CRISIS_988,
            _bill(number=10254, title="Maximizing Opioid Recovery Emergency Savings Act", feed_reason="healthcare"),
            _bill(number=10212, title="PEPTIDES for Veterans Act", feed_reason="veterans"),
            PAYDAYS,
        ]
        ranked = _rank(pool)
        lede, top3, _ = headline_slots(ranked)
        front = [b for b in [lede, *top3] if b]
        self.assertNotEqual(lede.get("feed_reason"), "your_rep")
        self.assertTrue(any(b.get("feed_reason") == "your_rep" for b in front))
        self.assertEqual(lede["number"], 10293)


class ClimateWasteStem(unittest.TestCase):
    def test_jobs_not_waste_matches_climate(self):
        self.assertTrue(title_matches_interest("Jobs, Not Waste Act of 2026", "climate"))

    def test_waste_alone_does_not(self):
        self.assertFalse(title_matches_interest("Solid Waste Disposal Study", "climate"))


if __name__ == "__main__":
    unittest.main()
