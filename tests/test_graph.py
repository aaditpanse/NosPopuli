"""Pure-logic tests for the government graph (graph.py).

Fixtures mirror the real Fairfax defects rather than reading the live store:
a duplicate roster spelling, a sentence fragment parsed as a member, one item
with three roll calls, a seat that changed hands with no contest on disk, and
a person who won two different bodies' seats in the same district.
"""

import json
import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import graph  # noqa: E402

CFG = graph.SOURCES["fairfax-bos"]
FRAGMENT = "McKay extended thoughts and prayers to the Fairfax family"


def cert(status):
    return {"certification": {"status": status, "method": None, "note": None}}


def member(name, role="Supervisor", district=None):
    return {"name": name, "role": role, "district": district}


def vote(vid, meeting_id, item_id, positions, status="quarantined"):
    return {"vote_id": vid, "meeting_id": meeting_id, "item_id": item_id,
            "positions": [{"member": m, "position": p} for m, p in positions],
            "counts": {}, "result": "pass", **cert(status)}


def store():
    members = [
        member("Jeffrey C. McKay", role="Chairman"),
        member("Patrick S. Herrity", district="Springfield District"),
        member("Pat Herrity", district="Springfield District"),
        member("Rachna Sizemore Heizer", district="Braddock District"),
        member(FRAGMENT, role="Chairman"),
    ]
    meetings = {
        "m1": {"meeting_id": "m1", "body": "BOS", "date": "2025-11-18",
               "attendance": {"Jeffrey C. McKay": "present",
                              "Patrick S. Herrity": "present"}, **cert("quarantined")},
        "m2": {"meeting_id": "m2", "body": "BOS", "date": "2026-01-13",
               "attendance": {"Jeffrey C. McKay": "present",
                              "Pat Herrity": "present",
                              "Rachna Sizemore Heizer": "present"}, **cert("certified")},
    }
    items = {
        "m1-i1": {"item_id": "m1-i1", "meeting_id": "m1", "title": "REZONE 12 ACRES",
                  "action": "Approved", "result": "pass", **cert("quarantined")},
        "m2-i7": {"item_id": "m2-i7", "meeting_id": "m2", "title": "PROCLAMATION FOR X",
                  "action": "Approved", "result": "pass", **cert("certified")},
    }
    votes = {
        "v1": vote("v1", "m1", "m1-i1",
                   [("Jeffrey C. McKay", "aye"), ("Patrick S. Herrity", "no"),
                    (FRAGMENT, "aye")]),
        "v2": vote("v2", "m2", "m2-i7",
                   [("Jeffrey C. McKay", "aye"), ("Pat Herrity", "aye"),
                    ("Rachna Sizemore Heizer", "aye")], status="certified"),
        "v3": vote("v3", "m2", "m2-i7",
                   [("Jeffrey C. McKay", "aye"), ("Pat Herrity", "abstain")],
                   status="certified"),
        "v4": vote("v4", "m2", "m2-i7",
                   [("Jeffrey C. McKay", "no"), ("Pat Herrity", "aye")],
                   status="certified"),
    }
    return {"members": {m["name"]: m for m in members}, "meetings": meetings,
            "agenda_items": items, "vote_events": votes}


def contest(cid, office, district, winner, status="quarantined",
            jurisdiction="Fairfax County"):
    return {"contest_id": cid, "office": office, "jurisdiction": jurisdiction,
            "state": "VA", "year": 2023, "month": 11, "district": district,
            "candidates": [{"name": winner, "winner": True}],
            "winner_names": [winner], **cert(status)}


CONTESTS = [
    contest("c-chair", "Board of Supervisors", "Fairfax County", "Jeffrey C. McKay"),
    contest("c-spring", "Board of Supervisors", "Springfield District",
            'Patrick S. "Pat" Herrity'),
    contest("c-brad", "Board of Supervisors", "Braddock District", "James R. Walkinshaw"),
    # Same person, same district, different body: must NOT seat her on the BOS.
    contest("c-brad-sb", "School Board", "Braddock District", "Rachna Sizemore Heizer"),
    contest("c-loudoun", "Board of Supervisors", "Algonkian District", "Someone Else",
            jurisdiction="Loudoun County"),
]
SUMMARIES = {"m1-i1": {"topic": "zoning", "derived_by": "claude-haiku-4-5",
                       "plain_english": "..."}}


def by_pred(edges, predicate):
    return [e for e in edges if e["predicate"] == predicate]


def person_named(nodes, name):
    return next(n for n in nodes if n["kind"] == "person" and n["name"] == name)


class ResolveMembersTest(unittest.TestCase):
    def test_duplicates_merge_and_fragment_is_rejected(self):
        persons, rejects = graph.resolve_members(store(), CFG)
        names = sorted(p["name"] for p in persons)
        self.assertEqual(names, ["Jeffrey C. McKay", "Patrick S. Herrity",
                                 "Rachna Sizemore Heizer"])
        self.assertEqual([r[0] for r in rejects], [FRAGMENT])
        herrity = next(p for p in persons if p["name"] == "Patrick S. Herrity")
        self.assertEqual(herrity["key"], "herrity/p")     # county + surname + initial, no seat
        self.assertEqual(herrity["seat"], "springfield")
        self.assertEqual(herrity["aliases"], ["Pat Herrity"])
        self.assertEqual(sorted(herrity["raw_names"]), ["Pat Herrity", "Patrick S. Herrity"])
        self.assertEqual(herrity["first_seen"], "2025-11-18")

    def test_first_seen_is_earliest_appearance_under_any_spelling(self):
        persons, _ = graph.resolve_members(store(), CFG)
        heizer = next(p for p in persons if p["seat"] == "braddock")
        self.assertEqual(heizer["first_seen"], "2026-01-13")

    def test_same_surname_and_initial_but_different_person_is_not_merged(self):
        st = store()
        st["members"]["Paul Herrity"] = member("Paul Herrity", district="Sully District")
        persons, rejects = graph.resolve_members(st, CFG)
        self.assertEqual(sorted(p["name"] for p in persons if "Herrity" in p["name"]),
                         ["Patrick S. Herrity", "Paul Herrity"])
        self.assertTrue(any("collides" in r[1] for r in rejects))

    def test_name_floor(self):
        self.assertTrue(graph._looks_like_name("James N. Bierman, Jr."))
        self.assertTrue(graph._looks_like_name("Ana de la Cruz"))
        self.assertFalse(graph._looks_like_name(FRAGMENT))
        self.assertTrue(graph._looks_like_name("Smith"))      # Prince William's roster is surnames
        self.assertFalse(graph._looks_like_name("smith"))
        self.assertFalse(graph._looks_like_name("X"))

    def test_display_name_strips_quoted_nickname_into_alias(self):
        self.assertEqual(graph._display_name('Patrick S. "Pat" Herrity'),
                         ("Patrick S. Herrity", "Pat Herrity"))
        self.assertEqual(graph._display_name('James N. "Jimmy" Bierman, Jr.'),
                         ("James N. Bierman, Jr.", "Jimmy Bierman"))
        self.assertEqual(graph._display_name("Kathy L. Smith"), ("Kathy L. Smith", None))


class BuildTest(unittest.TestCase):
    def setUp(self):
        self.nodes, self.edges, self.gaps = graph.build(
            "fairfax-bos", store(), CONTESTS, SUMMARIES)

    def test_predicates_are_from_the_vocabulary(self):
        self.assertTrue({e["predicate"] for e in self.edges} <= set(graph.PREDICATES))

    def test_three_roll_calls_yield_three_edges_per_person(self):
        herrity = person_named(self.nodes, "Patrick S. Herrity")
        on_i7 = [e for e in by_pred(self.edges, "voted_on")
                 if e["src"] == herrity["id"] and e["dst"] == "instrument/m2-i7"]
        self.assertEqual(sorted(e["source_ref"] for e in on_i7), ["v2", "v3", "v4"])
        self.assertEqual(sorted(e["props"]["position"] for e in on_i7),
                         ["abstain", "aye", "aye"])

    def test_merged_spellings_vote_as_one_person(self):
        # 'Pat Herrity' in v2..v4 and 'Patrick S. Herrity' in v1 are one node.
        persons = [n for n in self.nodes if n["kind"] == "person"]
        self.assertEqual(len([p for p in persons if "Herrity" in p["name"]]), 1)

    def test_fragment_votes_are_dropped_and_reported(self):
        self.assertFalse(any(FRAGMENT in e["src"] for e in self.edges))
        self.assertTrue(any("dropped" in g and FRAGMENT in g for g in self.gaps))

    def test_voted_on_is_instantaneous_on_the_meeting_date(self):
        for e in by_pred(self.edges, "voted_on"):
            self.assertEqual(e["valid_from"], e["valid_to"])
            self.assertIn(e["valid_from"], ("2025-11-18", "2026-01-13"))

    def test_certification_follows_the_asserting_record(self):
        # v1 is quarantined → ingested; v2–v4 certified → certified. Per
        # assertion: every edge from one vote event shares its status.
        by_ref = {}
        for e in by_pred(self.edges, "voted_on"):
            by_ref.setdefault(e["source_ref"], set()).add(e["certification"])
        self.assertEqual(by_ref, {"v1": {"ingested"}, "v2": {"certified"},
                                  "v3": {"certified"}, "v4": {"certified"}})

    def test_topic_is_a_labelled_derived_property(self):
        item = next(n for n in self.nodes if n["id"] == "instrument/m1-i1")
        self.assertEqual(item["props"]["topic"], "zoning")
        self.assertEqual(item["props"]["topic_derived_by"], "claude-haiku-4-5")
        other = next(n for n in self.nodes if n["id"] == "instrument/m2-i7")
        self.assertNotIn("topic", other["props"])

    def test_elected_in_is_scoped_to_the_seat_not_the_surname(self):
        heizer = person_named(self.nodes, "Rachna Sizemore Heizer")
        self.assertEqual([e for e in by_pred(self.edges, "elected_in")
                          if e["src"] == heizer["id"]], [])
        # The school-board contest and the other county's contest never
        # become nodes in this slice at all.
        self.assertEqual(sorted(n["id"] for n in self.nodes if n["kind"] == "contest"),
                         ["contest/c-brad", "contest/c-chair", "contest/c-spring"])
        herrity = person_named(self.nodes, "Patrick S. Herrity")
        mine = [e for e in by_pred(self.edges, "elected_in") if e["src"] == herrity["id"]]
        self.assertEqual([e["dst"] for e in mine], ["contest/c-spring"])
        self.assertEqual(mine[0]["valid_from"], "2023-11-07")   # first Tue after first Mon
        self.assertIn("Pat Herrity", herrity["props"]["aliases"])

    def test_holds_bounds_and_the_inferred_close(self):
        holds = {graph_node["name"]: e for e in by_pred(self.edges, "holds")
                 for graph_node in self.nodes if graph_node["id"] == e["src"]}
        self.assertEqual(holds["Jeffrey C. McKay"]["valid_from"], "2024-01-01")
        self.assertEqual(holds["Jeffrey C. McKay"]["props"]["bound_from"], "term_statute")
        self.assertIsNone(holds["Jeffrey C. McKay"]["valid_to"])
        heizer = holds["Rachna Sizemore Heizer"]
        self.assertEqual((heizer["valid_from"], heizer["props"]["bound_from"]),
                         ("2026-01-13", "observed"))
        walk = holds["James R. Walkinshaw"]
        self.assertEqual((walk["valid_from"], walk["valid_to"]), ("2024-01-01", "2026-01-12"))
        self.assertEqual(walk["props"]["bound_to"], "inferred")
        self.assertTrue(any("no contest seating" in g and "Sizemore Heizer" in g
                            for g in self.gaps))
        self.assertTrue(any("closed at 2026-01-12" in g for g in self.gaps))

    def test_ids_are_deterministic_and_ocd_shaped(self):
        nodes2, edges2, _ = graph.build("fairfax-bos", store(), CONTESTS, SUMMARIES)
        self.assertEqual([n["id"] for n in self.nodes], [n["id"] for n in nodes2])
        self.assertEqual(len(self.edges), len(edges2))
        ids = {n["kind"]: n["id"] for n in self.nodes}
        self.assertTrue(ids["person"].startswith("ocd-person/"))
        self.assertTrue(ids["post"].startswith("ocd-post/"))
        self.assertIn("ocd-division/country:us/state:va/county:fairfax/council_district:braddock",
                      {n["id"] for n in self.nodes})

    def test_edge_keys_are_unique(self):
        keys = [(e["src"], e["predicate"], e["dst"], e["source_ref"]) for e in self.edges]
        self.assertEqual(len(keys), len(set(keys)))


class MotionStoreTest(unittest.TestCase):
    """Loudoun records motions, not agenda items: the motion is the instrument."""

    def test_motion_becomes_the_instrument_and_roster_seats_come_from_contests(self):
        st = {"members": {"Phyllis J. Randall": member("Phyllis J. Randall"),
                          "Juli Briskman": member("Juli Briskman")},
              "meetings": {"l1": {"meeting_id": "l1", "body": "BOS", "date": "2026-01-06",
                                  "attendance": {"Phyllis J. Randall": "present"}, **cert("certified")}},
              "agenda_items": {},
              "vote_events": {"l1-m1": {"vote_id": "l1-m1", "meeting_id": "l1",
                                        "motion": "Chair Randall moved to approve the consent agenda.",
                                        "positions": [{"member": "Phyllis J. Randall", "position": "aye"},
                                                      {"member": "Juli Briskman", "position": "no"}],
                                        "counts": {}, "result": "pass", **cert("certified")}}}
        contests = [contest("lc-chair", "Board of Supervisors", "Loudoun County", "Phyllis J. Randall",
                            jurisdiction="Loudoun County", status="quarantined"),
                    contest("lc-alg", "Board of Supervisors", "Algonkian District", "Juli E. Briskman",
                            jurisdiction="Loudoun County")]
        nodes, edges, gaps = graph.build("loudoun-bos", st, contests, {})
        inst = [n for n in nodes if n["kind"] == "instrument"]
        self.assertEqual([(n["id"], n["props"]["instrument_type"]) for n in inst],
                         [("instrument/l1-m1", "motion")])
        voted = by_pred(edges, "voted_on")
        self.assertEqual({e["certification"] for e in voted}, {"certified"})   # the first certified hop
        randall = person_named(nodes, "Phyllis J. Randall")
        holds = [e for e in by_pred(edges, "holds") if e["src"] == randall["id"]]
        self.assertEqual(len(holds), 1)
        self.assertTrue(holds[0]["dst"] == next(n["id"] for n in nodes if n["kind"] == "post"
                                                 and n["props"]["natural_key"].endswith("/chair")))
        briskman = person_named(nodes, "Juli Briskman")
        self.assertIn("Juli E. Briskman", briskman["props"]["aliases"] + [briskman["name"]])
        self.assertFalse(any("vote but hold no seat" in g for g in gaps))


class CloseHoldsAcrossTest(unittest.TestCase):
    def test_a_federal_term_closes_the_county_hold_it_started_inside(self):
        ln, le, _ = graph.build("fairfax-bos", store(), CONTESTS, SUMMARIES)
        fn, fe, _ = graph.build_congress(LEGISLATORS, [SNAPSHOT], states=["VA"], today=TODAY)
        edges = le + fe
        changed = graph.close_holds_across(edges)
        walk = person_named(ln, "James R. Walkinshaw")
        county = next(e for e in changed if e["src"] == walk["id"])
        self.assertEqual(county["valid_to"], "2025-09-09")
        self.assertEqual(county["props"]["inferred_from"], "took another seat on 2025-09-10")
        # Exact-ended terms and the still-open federal one are untouched.
        self.assertEqual(len(changed), 1)
        self.assertEqual(graph.close_holds_across(edges), [])     # idempotent


class StripPlaceTest(unittest.TestCase):
    def test_place_words_leave_the_topic(self):
        self.assertEqual(graph.strip_place("Fairfax zoning"), ("zoning", "Fairfax"))
        self.assertEqual(graph.strip_place("zoning in Fairfax County"), ("zoning in", "Fairfax County"))
        self.assertEqual(graph.strip_place("Virginia housing"), ("housing", "Virginia"))
        self.assertEqual(graph.strip_place("zoning"), ("zoning", None))
        self.assertEqual(graph.strip_place("Fairfax"), ("Fairfax", "Fairfax"))   # never empty


class HoldersAsOfTest(unittest.TestCase):
    def setUp(self):
        nodes, edges, _ = graph.build("fairfax-bos", store(), CONTESTS, SUMMARIES)
        self.names = {n["id"]: n["name"] for n in nodes}
        braddock = next(n["id"] for n in nodes if n["kind"] == "post"
                        and n["props"]["natural_key"].endswith("/braddock"))
        self.holds = [e for e in edges if e["predicate"] == "holds" and e["dst"] == braddock]

    def holder(self, day):
        rows = graph.holders_as_of(self.holds, day)
        return [self.names[r["src"]] for r in rows]

    def test_braddock_resolves_to_exactly_one_person_on_every_day(self):
        self.assertEqual(self.holder("2025-11-18"), ["James R. Walkinshaw"])
        self.assertEqual(self.holder("2026-01-12"), ["James R. Walkinshaw"])
        self.assertEqual(self.holder("2026-01-13"), ["Rachna Sizemore Heizer"])
        self.assertEqual(self.holder("2026-03-03"), ["Rachna Sizemore Heizer"])
        self.assertEqual(self.holder("2023-12-31"), [])

    def test_on_handover_day_the_incoming_holder_has_the_seat(self):
        seat = "ocd-post/president"
        biden = {"src": "biden", "dst": seat, "valid_from": "2021-01-20", "valid_to": "2025-01-20"}
        trump = {"src": "trump", "dst": seat, "valid_from": "2025-01-20", "valid_to": None}
        elsewhere = {"src": "other", "dst": "ocd-post/elsewhere", "valid_from": "2025-01-20", "valid_to": None}
        rows = [biden, trump, elsewhere]
        on = lambda day: sorted(r["src"] for r in graph.holders_as_of(rows, day) if r["dst"] == seat)  # noqa: E731
        self.assertEqual(on("2025-01-19"), ["biden"])
        self.assertEqual(on("2025-01-20"), ["trump"])
        # A term that ends with nobody starting keeps its last day.
        self.assertEqual(on("2025-01-20") and sorted(r["src"] for r in graph.holders_as_of([biden], "2025-01-20")), ["biden"])
        # One person's consecutive terms: one row that day, the new term.
        griffith = [{"src": "g", "dst": "va9", "valid_from": "2023-01-03", "valid_to": "2025-01-03"},
                    {"src": "g", "dst": "va9", "valid_from": "2025-01-03", "valid_to": None}]
        self.assertEqual([r["valid_from"] for r in graph.holders_as_of(griffith, "2025-01-03")], ["2025-01-03"])


class ShapeAnswerTest(unittest.TestCase):
    P = [{"id": "ocd-person/x", "name": "Patrick S. Herrity"}]

    def test_weak_hop_is_reported(self):
        rows = [{"certification": "ingested", "topic_derived_by": "claude-haiku-4-5"},
                {"certification": "certified"}]
        out = graph.shape_answer(rows, self.P, "Herrity", topic="zoning")
        self.assertEqual(out["weak_hops"], [{"predicate": "voted_on", "weakest": "ingested",
                                             "counts": {"ingested": 1, "certified": 1}}])
        self.assertEqual(out["advisory_fields"], ["topic"])
        self.assertIsNone(out["empty_reason"])

    def test_all_certified_has_no_weak_hop(self):
        out = graph.shape_answer([{"certification": "certified"}], self.P, "Herrity")
        self.assertEqual(out["weak_hops"], [])
        self.assertEqual(out["advisory_fields"], [])

    def test_empty_states_say_why(self):
        self.assertIn("no person", graph.shape_answer([], [], "Nobody")["empty_reason"])
        out = graph.shape_answer([], self.P, "Herrity", topic="glue traps", total_votes=407)
        self.assertIn("407", out["empty_reason"])
        self.assertIn("glue traps", out["empty_reason"])
        self.assertIn("no recorded votes",
                      graph.shape_answer([], self.P, "Herrity")["empty_reason"])


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------- congress

def legislator(bioguide, first, last, terms, lis=None, nickname=None):
    ids = {"bioguide": bioguide, "govtrack": 1}
    if lis:
        ids["lis"] = lis
    name = {"first": first, "last": last, "official_full": f"{first} {last}"}
    if nickname:
        name["nickname"] = nickname
    return {"id": ids, "name": name, "terms": terms}


def rep(state, district, start, end, party="Democrat"):
    return {"type": "rep", "state": state, "district": district, "start": start,
            "end": end, "party": party}


LEGISLATORS = [
    legislator("W000831", "James R.", "Walkinshaw",
               [rep("VA", 11, "2025-09-10", "2027-01-03")]),
    legislator("G000568", "H. Morgan", "Griffith",
               [rep("VA", 9, "2023-01-03", "2025-01-03", "Republican"),
                rep("VA", 9, "2025-01-03", "2027-01-03", "Republican")]),
    legislator("W000805", "Mark R.", "Warner",
               [{"type": "sen", "state": "VA", "class": 2, "start": "2021-01-03",
                 "end": "2027-01-03", "party": "Democrat"}], lis="S327"),
    legislator("A000370", "Alma", "Adams", [rep("NC", 12, "2025-01-03", "2027-01-03")]),
]


def house_vote(roll, legis_num, positions, date="2026-01-09"):
    return {"vote_id": f"us/119/2/house/{roll}", "chamber": "house", "congress": 119,
            "session": 2, "roll": roll, "date": date, "legis_num": legis_num,
            "question": "On Passage", "result": "Passed", "description": "Some Act",
            "id_kind": "bioguide", "positions": positions, "source_id": "clerk.house.gov",
            "source_url": "u"}


SNAPSHOT = {"votes": [
    house_vote(12, "H R 5184", {"aye": ["W000831", "A000370"], "no": ["G000568"]}),
    house_vote(13, "H R 5184", {"aye": ["W000831"], "absent": ["G000568", "A000370"]}),
    house_vote(14, None, {"aye": ["W000831"]}),                 # quorum call: no instrument
    house_vote(15, "H RES 9", {"present": ["ZZ99999"]}),        # nobody we know
    {"vote_id": "us/119/2/senate/12", "chamber": "senate", "congress": 119, "session": 2,
     "roll": 12, "date": "2026-01-27", "document_type": "S.", "document_number": "3627",
     "document_name": "S. 3627", "question": "On Cloture", "result": "Rejected",
     "description": "Motion to Invoke Cloture", "id_kind": "lis",
     "positions": {"no": ["S327"]}, "source_id": "senate.gov", "source_url": "u"},
]}


class BuildCongressTest(unittest.TestCase):
    def setUp(self):
        self.nodes, self.edges, self.gaps = graph.build_congress(
            LEGISLATORS, [SNAPSHOT], states=["VA"], today="2026-09-20")
        self.by_id = {n["id"]: n for n in self.nodes}

    def test_delegation_filter(self):
        people = sorted(n["name"] for n in self.nodes if n["kind"] == "person")
        self.assertEqual(people, ["H. Morgan Griffith", "James R. Walkinshaw", "Mark R. Warner"])
        self.assertNotIn(f"{graph.US}/state:nc", self.by_id)

    def test_same_ontology_as_the_county(self):
        # Same kinds, same predicates, same position words as the Fairfax build.
        self.assertEqual({n["kind"] for n in self.nodes},
                         {"jurisdiction", "organization", "post", "person", "instrument"})
        self.assertTrue({e["predicate"] for e in self.edges} <= set(graph.PREDICATES))
        self.assertEqual({e["props"]["position"] for e in by_pred(self.edges, "voted_on")},
                         {"aye", "no", "absent"})

    def test_holds_have_exact_bounds_from_terms(self):
        griffith = person_named(self.nodes, "H. Morgan Griffith")
        mine = sorted((e for e in by_pred(self.edges, "holds") if e["src"] == griffith["id"]),
                      key=lambda e: e["valid_from"])
        self.assertEqual([(e["valid_from"], e["valid_to"]) for e in mine],
                         [("2023-01-03", "2025-01-03"), ("2025-01-03", None)])
        self.assertEqual(mine[0]["props"]["bound_to"], "exact")
        self.assertEqual(mine[1]["props"]["term_expires"], "2027-01-03")
        # A roll call inside the term affirms it from an independent source:
        # the current term is certified, the past one (no votes on disk) is not.
        self.assertEqual([e["certification"] for e in mine], ["ingested", "certified"])
        self.assertIn("roll call", mine[1]["props"]["certified_by"])
        # Consecutive terms on one seat are not a double hold: the earlier
        # one already had an exact end, so nothing is inferred.
        self.assertFalse(any("inference" in g for g in self.gaps))

    def test_seats_and_divisions_are_ocd_shaped(self):
        self.assertIn(f"{graph.US}/state:va/cd:11", self.by_id)
        senate_post = next(n for n in self.nodes if n["kind"] == "post"
                           and n["props"]["natural_key"] == "us/senate/va/class:2")
        rep_edge = next(e for e in by_pred(self.edges, "represents") if e["src"] == senate_post["id"])
        self.assertEqual(rep_edge["dst"], f"{graph.US}/state:va")

    def test_two_roll_calls_on_one_bill_are_two_edges(self):
        walk = person_named(self.nodes, "James R. Walkinshaw")
        on_bill = [e for e in by_pred(self.edges, "voted_on")
                   if e["src"] == walk["id"] and e["dst"] == "instrument/us/119/hr/5184"]
        self.assertEqual(sorted(e["source_ref"] for e in on_bill),
                         ["us/119/2/house/12", "us/119/2/house/13"])
        self.assertEqual(self.by_id["instrument/us/119/hr/5184"]["props"]["instrument_type"], "hr")

    def test_senate_lis_ids_bridge_to_bioguide(self):
        warner = person_named(self.nodes, "Mark R. Warner")
        mine = [e for e in by_pred(self.edges, "voted_on") if e["src"] == warner["id"]]
        self.assertEqual([(e["dst"], e["props"]["position"]) for e in mine],
                         [("instrument/us/119/s/3627", "no")])

    def test_votes_on_nothing_and_by_nobody_are_gaps_not_edges(self):
        # The chamber considered H.Res. 9 (a chamber-level fact, kept), but
        # nobody in this delegation is recorded voting on it.
        self.assertIn("instrument/us/119/hres/9", {e["dst"] for e in by_pred(self.edges, "considered")})
        self.assertNotIn("instrument/us/119/hres/9", {e["dst"] for e in by_pred(self.edges, "voted_on")})
        self.assertTrue(any("no legislative instrument" in g for g in self.gaps))
        # Unknown ids are only a gap when loading everyone; for one
        # delegation, the rest of the chamber is expected to be unknown.
        self.assertFalse(any("not in legislators" in g for g in self.gaps))
        _, _, gaps_all = graph.build_congress(LEGISLATORS, [SNAPSHOT], today="2026-09-20")
        self.assertTrue(any("ZZ99999" in g or "1 member id" in g for g in gaps_all))


class AllMembersTest(unittest.TestCase):
    """Every member, not one delegation: the vote edge is the row that
    multiplies, so its props are pinned, and every state node has a name."""
    def setUp(self):
        self.nodes, self.edges, _ = graph.build_congress(LEGISLATORS, [SNAPSHOT], today=TODAY)

    def test_vote_edge_props_are_exactly_the_members_own(self):
        for e in by_pred(self.edges, "voted_on"):
            self.assertEqual(set(e["props"]), {"jurisdiction", "position", "vote_id", "chamber",
                                               "amendment", "question"})
        # The roll call's own facts stay on the chamber's one edge.
        considered = by_pred(self.edges, "considered")
        self.assertTrue(considered and all({"roll", "result"} <= set(e["props"]) for e in considered))

    def test_a_vote_answer_row_still_carries_the_question(self):
        b = graph.memory_backend(self.nodes, self.edges)
        out = graph.answer({"ask": "votes", "person": "Adams", "topic": None}, b)
        self.assertTrue(out["rows"])
        self.assertTrue(all(r["question"] == "On Passage" for r in out["rows"]))

    def test_every_state_node_has_a_name_not_a_code(self):
        states = [n for n in self.nodes if n["props"].get("level") == "state"]
        self.assertEqual(sorted(n["name"] for n in states), ["North Carolina", "Virginia"])


HISTORICAL = [
    legislator("W000154", "John", "Warner",
               [{"type": "sen", "state": "VA", "class": 2, "start": "1979-01-02",
                 "end": "2009-01-03", "party": "Republican"}]),
    legislator("R000001", "Early", "Member",
               [rep("OL", -1, "1807-10-26", "1809-03-03", "Democratic-Republican")]),
    # Also in the current file: the current record must win.
    legislator("A000370", "Alma", "Adams-Stale", [rep("NC", 12, "2014-11-04", "2015-01-03")]),
]


class HistoricalMembersTest(unittest.TestCase):
    def setUp(self):
        legs, self.merge_gaps = graph.merge_legislators(LEGISLATORS, HISTORICAL)
        self.nodes, self.edges, _ = graph.build_congress(legs, [SNAPSHOT], today=TODAY)
        self.by_id = {n["id"]: n for n in self.nodes}

    def test_former_member_holds_have_exact_ends_and_their_source(self):
        john = person_named(self.nodes, "John Warner")
        self.assertEqual(john["source_id"], "legislators-historical")
        (h,) = [e for e in by_pred(self.edges, "holds") if e["src"] == john["id"]]
        self.assertEqual((h["valid_from"], h["valid_to"], h["props"]["bound_to"]),
                         ("1979-01-02", "2009-01-03", "exact"))
        self.assertEqual(h["source_id"], "legislators-historical")

    def test_current_record_wins_a_collision_and_says_so(self):
        adams = [n for n in self.nodes if n["props"].get("bioguide") == "A000370"]
        self.assertEqual([n["name"] for n in adams], ["Alma Adams"])
        self.assertEqual(adams[0]["source_id"], "legislators-current")
        self.assertTrue(any("A000370" in g and "kept the current" in g for g in self.merge_gaps))

    def test_unrecorded_district_and_former_territory(self):
        post = next(n for n in self.nodes if n["kind"] == "post"
                    and n["props"]["natural_key"] == "us/house/ol/cd:unrecorded")
        self.assertIn("district not recorded", post["name"])
        self.assertEqual(self.by_id[f"{graph.US}/state:ol"]["name"], "Territory of Orleans")
        self.assertNotIn(f"{graph.US}/state:ol/cd:-1", self.by_id)

    def test_same_surname_asks_which_person(self):
        b = graph.memory_backend(self.nodes, self.edges)
        out = graph.answer({"ask": "votes", "person": "Warner", "topic": None}, b)
        self.assertTrue(out["ambiguous"])
        self.assertEqual(out["rows"], [])
        self.assertIn("2 people", out["empty_reason"])
        # Serving now first; each candidate says where and when.
        self.assertEqual([(c["name"], c["from"], c["to"]) for c in out["candidates"]],
                         [("Mark R. Warner", "2021", None), ("John Warner", "1979", "2009")])
        self.assertIn("U.S. Senator, VA (class 2), U.S. Senate", out["candidates"][1]["seats"])
        # Given and family name settle it; the middle initial does not hide him.
        one = graph.answer({"ask": "votes", "person": "Mark Warner", "topic": None}, b)
        self.assertNotIn("ambiguous", one)
        self.assertEqual([p["name"] for p in one["persons"]], ["Mark R. Warner"])
        self.assertEqual(one["count"], 1)


EXECUTIVE = [
    {"id": {"govtrack": 412733}, "name": {"first": "Joseph", "last": "Biden", "official_full": "Joseph R. Biden"},
     "terms": [{"type": "prez", "start": "2021-01-20", "end": "2025-01-20", "party": "Democrat", "how": "election"}]},
    {"id": {"govtrack": 412734}, "name": {"first": "Donald", "last": "Trump", "official_full": "Donald J. Trump"},
     "terms": [{"type": "prez", "start": "2025-01-20", "end": "2029-01-20", "party": "Republican", "how": "election"}]},
    # Also a member of Congress: one node, the bioguide id, in both files.
    {"id": {"bioguide": "W000805", "govtrack": 412321}, "name": {"first": "Mark", "last": "Warner"},
     "terms": [{"type": "viceprez", "start": "2025-01-20", "end": "2029-01-20", "party": "Democrat"}]},
]


def acted(date, text, type_="President"):
    return {"date": date, "code": "E30000", "type": type_, "text": text}


def bill_rec(actions=None, laws=()):
    rec = {"title": "A bill", "sponsors": [], "cosponsors": [], "laws": list(laws)}
    if actions is not None:
        rec["actions"] = actions
    return rec


ENACTED = {**SNAPSHOT, "meta": {"congress": 119, "instruments_fetched": "2026-09-25"}, "instruments": {
    "hr/5184": bill_rec([acted("2026-02-01", "Signed by President."),
                         acted("2026-02-01", "Became Public Law No: 119-90.", "BecameLaw")],
                        [{"number": "119-90", "type": "Public Law"}]),
    "s/3627": bill_rec([acted("2025-01-10", "Vetoed by President.")]),
    "hres/9": bill_rec(None),               # the actions pass failed for this one
}}


class EnactmentTest(unittest.TestCase):
    def setUp(self):
        self.nodes, self.edges, _ = graph.build_congress(LEGISLATORS, [ENACTED], today=TODAY)
        xn, xe, self.xgaps = graph.build_executive(EXECUTIVE, today=TODAY)
        known = {n["id"] for n in self.nodes}
        self.nodes += [n for n in xn if n["id"] not in known]
        self.edges += xe
        en, ee, self.gaps = graph.build_enactment(
            [ENACTED], xe, {n["id"] for n in self.nodes if n["kind"] == "instrument"})
        self.nodes += en
        self.edges += ee
        self.b = graph.memory_backend(self.nodes, self.edges)

    def test_the_presidency_is_a_body_with_two_seats(self):
        pres = graph.node_id("post", "us/president")
        a = graph.answer({"ask": "holder", "seat": "President of the United States", "as_of": "2024-06-01"}, self.b)
        self.assertEqual([h["name"] for h in a["holders"]], ["Joseph R. Biden"])
        self.assertIn(pres, {e["dst"] for e in by_pred(self.edges, "has_seat")})
        # The Vice President who sat in the Senate is one node, keyed by bioguide.
        warner = [n for n in self.nodes if n["props"].get("bioguide") == "W000805"]
        self.assertEqual(len(warner), 1)
        self.assertEqual({e["dst"] for e in by_pred(self.edges, "holds") if e["src"] == warner[0]["id"]},
                         {graph.node_id("post", "us/senate/va/class:2"),
                          graph.node_id("post", "us/vice-president")})

    def test_the_signer_is_resolved_by_date(self):
        (s,) = by_pred(self.edges, "signed")
        self.assertEqual((s["src"], s["dst"], s["valid_from"]),
                         (graph.node_id("person", "govtrack/412734"), "instrument/us/119/hr/5184", "2026-02-01"))
        (v,) = by_pred(self.edges, "vetoed")
        self.assertEqual(v["src"], graph.node_id("person", "govtrack/412733"))   # Biden, 2025-01-10
        (law,) = by_pred(self.edges, "enacted_as")
        self.assertEqual((law["dst"], law["certification"]), ("instrument/us/pl/119-90", "ingested"))

    def test_questions(self):
        ask = lambda q: graph.search(q, self.b, today=TODAY)  # noqa: E731
        self.assertEqual(graph.parse_question("did HR 5184 become law"), {"ask": "law", "topic": "HR 5184"})
        self.assertEqual(graph.parse_question("who signed H.R. 5184?"), {"ask": "law", "topic": "H.R. 5184"})
        self.assertEqual(graph.parse_question("what did Biden veto"),
                         {"ask": "signed_by", "person": "Biden", "predicate": "vetoed"})
        law = ask("did HR 5184 become law")
        self.assertEqual([(r["position"], r["person"], r["law"]) for r in law["rows"]],
                         [("law", "Donald J. Trump", "Public Law 119-90")])
        self.assertEqual(ask("who signed S 3627")["rows"][0]["position"], "vetoed")
        vetoes = ask("what did Biden veto")
        self.assertEqual([r["item_id"] for r in vetoes["rows"]], ["instrument/us/119/s/3627"])

    def test_no_action_record_is_unknown_never_no(self):
        (row,) = graph.search("did H RES 9 become law", self.b, today=TODAY)["rows"]
        self.assertEqual(row["position"], "unknown")
        self.assertIn("no action record on disk", row["question"])
        self.assertTrue(any("1 bill(s) have no action record" in g for g in self.gaps))


COMMITTEES = [
    {"type": "house", "name": "House Committee on the Judiciary", "thomas_id": "HSJU",
     "subcommittees": [{"name": "Crime and Federal Government Surveillance", "thomas_id": "10"}]},
    {"type": "senate", "name": "Senate Committee on Finance", "thomas_id": "SSFI"},
    {"type": "joint", "name": "Joint Economic Committee", "thomas_id": "JSEC"},
]
MEMBERSHIP = {
    "HSJU": [{"name": "H. Morgan Griffith", "party": "majority", "rank": 1, "title": "Chairman", "bioguide": "G000568"},
             {"name": "James R. Walkinshaw", "party": "minority", "rank": 1, "title": "Ranking Member",
              "bioguide": "W000831"}],
    "HSJU10": [{"name": "H. Morgan Griffith", "party": "majority", "rank": 2, "bioguide": "G000568"}],
    "SSFI": [{"name": "Mark R. Warner", "party": "minority", "rank": 3, "bioguide": "W000805"}],
    "JSEC": [{"name": "Mark R. Warner", "party": "minority", "rank": 1, "bioguide": "W000805"}],
}
REFERRED = {**SNAPSHOT, "meta": {"congress": 119, "instruments_fetched": "2026-09-25"}, "instruments": {
    "hr/5184": {**bill_rec([]), "committees": [
        {"code": "hsju00", "name": "Judiciary Committee", "chamber": "House", "parent": None,
         "activities": [{"name": "Referred To", "date": "2026-01-02"}, {"name": "Reported By", "date": "2026-01-05"}]},
        {"code": "hsju10", "name": "Crime Subcommittee", "chamber": "House", "parent": "hsju00",
         "activities": [{"name": "Referred To", "date": "2026-01-03"}]},
        {"code": "hsif00", "name": "Energy and Commerce Committee", "chamber": "House", "parent": None,
         "activities": [{"name": "Referred To", "date": "2026-01-02"}]}],
        "reports": [{"endpoint": "119/HRPT/7", "citation": "H. Rept. 119-7", "date": "2026-01-06",
                     "committees": ["hsju00"]}]},
    "s/3627": {**bill_rec([]), "committees": [], "reports": []},
}}


class CommitteesTest(unittest.TestCase):
    def setUp(self):
        self.nodes, self.edges, _ = graph.build_congress(LEGISLATORS, [REFERRED], today=TODAY)
        people = {n["props"]["bioguide"]: n["id"] for n in self.nodes if n["props"].get("bioguide")}
        cn, ce, self.gaps = graph.build_committees(COMMITTEES, MEMBERSHIP, "2026-09-25", [REFERRED], people)
        self.nodes += cn
        self.edges += ce
        self.b = graph.memory_backend(self.nodes, self.edges)
        self.ask = lambda q: graph.search(q, self.b, today=TODAY)  # noqa: E731

    def test_committees_hang_under_their_chamber_and_joint_under_the_country(self):
        parent = {e["dst"]: e["src"] for e in by_pred(self.edges, "has_body")}
        self.assertEqual(parent[graph.committee_id("hsju00")], graph.node_id("organization", "us/house"))
        self.assertEqual(parent[graph.committee_id("hsju10")], graph.committee_id("hsju00"))
        self.assertEqual(parent[graph.committee_id("jsec00")], graph.US)

    def test_membership_is_observed_not_dated_and_never_a_hold(self):
        seats = by_pred(self.edges, "member_of")
        self.assertEqual(len(seats), 5)
        self.assertTrue(all(e["valid_from"] == "2026-09-25" and e["valid_to"] is None
                            and e["props"]["bound_from"] == "observed" for e in seats))
        self.assertFalse(any(e["dst"] == graph.committee_id("hsju00") for e in by_pred(self.edges, "holds")))

    def test_chair_and_members(self):
        chair = self.ask("who chairs the House Judiciary Committee")
        self.assertEqual([(r["person"], r["position"]) for r in chair["rows"]], [("H. Morgan Griffith", "Chairman")])
        self.assertIn("observed on 2026-09-25", chair["rows"][0]["question"])
        members = self.ask("who sits on the Judiciary Committee")
        self.assertEqual(members["committees"], ["House Committee on the Judiciary"])   # not the subcommittee
        self.assertEqual([r["person"] for r in members["rows"]], ["H. Morgan Griffith", "James R. Walkinshaw"])
        self.assertIn("no committee in the graph", self.ask("who chairs the Ways and Means Committee")["empty_reason"])

    def test_referrals_and_reports(self):
        ref = self.ask("what committee has HR 5184 been referred to")
        self.assertEqual([(r["person"], r["position"]) for r in ref["rows"]],
                         [("House Committee on the Judiciary", "referred"),
                          ("Energy and Commerce Committee", "referred"),
                          ("House Committee on the Judiciary: Subcommittee on Crime and Federal Government Surveillance",
                           "referred"),
                          # What came back out: an original measure has only this row.
                          ("House Committee on the Judiciary", "reported")])
        # A committee the bill names but the current file lacks is kept, and said.
        self.assertTrue(any("hsif00" in g for g in self.gaps))
        rep_ = self.ask("what did the House Judiciary Committee report")
        self.assertEqual([(r["item_id"], r["question"]) for r in rep_["rows"]],
                         [("instrument/us/119/hr/5184", "H. Rept. 119-7")])
        # A committee record on disk with nothing in it is a real 'none'.
        self.assertIn("no committee (none on record)", self.ask("which committee is S 3627 in")["empty_reason"])


RELATED = {**SNAPSHOT, "meta": {"congress": 119, "instruments_fetched": "2026-09-25"}, "instruments": {
    "hr/5184": {**bill_rec([]), "related": [
        {"congress": 119, "type": "s", "number": "3627", "title": "Voted on too",
         "relationships": [{"type": "Identical bill", "identified_by": "CRS"}]},
        {"congress": 119, "type": "hr", "number": "77", "title": "Never voted on",
         "relationships": [{"type": "Procedurally related", "identified_by": "House"},
                           {"type": "Related bill", "identified_by": "CRS"}]}]},
    # hr/77's own links would be two hops from a vote: never fetched, never built.
}}


class RelatedTest(unittest.TestCase):
    def setUp(self):
        self.nodes, self.edges, _ = graph.build_congress(LEGISLATORS, [RELATED], today=TODAY)
        ids = {n["id"] for n in self.nodes if n["kind"] == "instrument"}
        rn, re_, self.gaps = graph.build_related([RELATED], ids)
        self.nodes += rn
        self.edges += re_
        self.b = graph.memory_backend(self.nodes, self.edges)

    def test_type_and_identifier_are_kept_and_the_unvoted_bill_is_title_only(self):
        rel = {e["dst"]: e for e in by_pred(self.edges, "related_to")}
        self.assertEqual(set(rel), {"instrument/us/119/s/3627", "instrument/us/119/hr/77"})
        self.assertEqual(rel["instrument/us/119/hr/77"]["props"]["types"], ["Procedurally related", "Related bill"])
        (stub,) = [n for n in self.nodes if n["id"] == "instrument/us/119/hr/77"]
        self.assertTrue(stub["props"]["title_only"])
        self.assertFalse(any(e["src"] == stub["id"] for e in self.edges))

    def test_related_question_reads_both_directions(self):
        out = graph.search("bills related to HR 5184", self.b, today=TODAY)
        self.assertEqual([(r["item_id"], r["position"], r["question"]) for r in out["rows"]],
                         [("instrument/us/119/s/3627", "Identical bill", "identified by CRS"),
                          ("instrument/us/119/hr/77", "Procedurally related, Related bill", "identified by CRS, House")])
        back = graph.search("bills related to S 3627", self.b, today=TODAY)
        self.assertEqual([r["item_id"] for r in back["rows"]], ["instrument/us/119/hr/5184"])


FEC = {"meta": {"cycle": 2026}, "members": {
    "W000805": {"name": "Mark R. Warner", "candidate_id": "S6VA00093", "committee_id": "C00438713",
                "committee_name": "FRIENDS OF MARK WARNER", "complete": True,
                "totals": {"receipts": 100.0, "other_political_committee_contributions": 30.0},
                "top_pacs": [{"name": "A PAC", "committee_id": None, "amount": 20.0, "receipts": 2}],
                "pac_total": 30.0, "pac_receipts": 3},
    "G000568": {"name": "H. Morgan Griffith", "candidate_ids": [], "complete": True,
                "gap": "no FEC candidate id for this chamber in the legislators file"},
}}


class MoneyTest(unittest.TestCase):
    def setUp(self):
        self.nodes, self.edges, _ = graph.build_congress(LEGISLATORS, [SNAPSHOT], today=TODAY)
        people = {n["props"]["bioguide"]: n["id"] for n in self.nodes if n["props"].get("bioguide")}
        mn, me, self.gaps = graph.build_money([FEC], people)
        self.nodes += mn
        self.edges += me
        self.b = graph.memory_backend(self.nodes, self.edges)

    def test_the_chamber_picks_the_candidate_id(self):
        cantwell = {"id": {"fec": ["H2WA01054", "S8WA00194"]}, "terms": [{"type": "sen"}]}
        self.assertEqual(graph.fec_candidate_ids(cantwell), ["S8WA00194"])
        self.assertEqual(graph.fec_candidate_ids({**cantwell, "terms": [{"type": "rep"}]}), ["H2WA01054"])

    def test_top_pacs_sum_every_receipt_and_drop_conduits_and_self(self):
        rows, total, n = graph.top_pacs([
            {"contributor_name": "B PAC", "contribution_receipt_amount": 5, "entity_type": "PAC"},
            {"contributor_name": "A PAC", "contribution_receipt_amount": 3, "entity_type": "PAC"},
            {"contributor_name": "A PAC", "contribution_receipt_amount": 4, "entity_type": "PAC"},
            {"contributor_name": "ACTBLUE", "contribution_receipt_amount": 90, "entity_type": "PAC"},
            {"contributor_name": "WARNER VICTORY FUND", "contribution_receipt_amount": 90, "entity_type": "PAC"},
        ], "Mark R. Warner")
        self.assertEqual([(r["name"], r["amount"], r["receipts"]) for r in rows], [("A PAC", 7, 2), ("B PAC", 5, 1)])
        self.assertEqual((total, n), (12, 3))

    def test_committee_edge_is_bounded_by_the_cycle_with_totals_on_it(self):
        (e,) = by_pred(self.edges, "campaign_committee")
        self.assertEqual((e["valid_from"], e["valid_to"], e["props"]["receipts"]), ("2025-01-01", "2026-12-31", 100.0))
        self.assertNotIn("top_pacs", e["props"])     # detail stays at the leaf

    def test_funds_question_and_its_empty_state(self):
        with mock.patch.object(graph, "fec_detail", return_value=FEC["members"]["W000805"]):
            out = graph.search("who funds Mark Warner", self.b, today=TODAY)
        self.assertEqual([(r["title"], r["position"]) for r in out["rows"]], [("A PAC", "$20")])
        self.assertEqual(out["committee"], "FRIENDS OF MARK WARNER")
        none = graph.search("who funds Griffith", self.b, today=TODAY)
        self.assertEqual(none["empty_reason"], "no FEC record on disk for H. Morgan Griffith")

    def test_an_error_written_to_a_snapshot_never_carries_the_key(self):
        # requests puts the full URL in its messages; snapshots are committed.
        e = ConnectionError("Max retries exceeded with url: /v1/candidate/H0/committees/"
                            "?api_key=SECRET123&designation=P (Caused by NameResolutionError)")
        out = graph._redact(e)
        self.assertNotIn("SECRET123", out)
        self.assertIn("api_key=REDACTED&designation=P", out)
        self.assertTrue(out.startswith("ConnectionError: "))

    def test_the_pac_label_names_the_filing_that_was_read(self):
        self.assertEqual(graph._pac_label("FEC bulk downloads, cycle 2026: pas2 (…)"),
                         "FEC bulk pas2, 24K contributions filed by the PAC")
        self.assertEqual(graph._pac_label(None), "FEC Schedule A line 11C")


OLDER = {"meta": {"congress": 118, "session": 2, "year": 2024},
         "instruments": {"hr/99": {"title": "The Older Act", "policy_area": "Taxation"}},
         "votes": [house_vote(7, "H R 99", {"no": ["G000568"], "aye": ["A000370"]}, date="2024-03-01")
                   | {"vote_id": "us/118/2/house/7", "congress": 118, "session": 2}]}


class OlderSessionsTest(unittest.TestCase):
    def test_older_roll_calls_certify_terms_without_edges(self):
        nodes, edges, _ = graph.build_congress(LEGISLATORS, [SNAPSHOT], today=TODAY,
                                               cert_index=graph.member_congress_index([OLDER]))
        self.assertFalse(any(e["source_ref"].startswith("us/118/") for e in edges))
        self.assertNotIn("instrument/us/118/hr/99", {n["id"] for n in nodes})
        griffith = person_named(nodes, "H. Morgan Griffith")
        old_term = next(e for e in by_pred(edges, "holds")
                        if e["src"] == griffith["id"] and e["valid_from"] == "2023-01-03")
        # Uncertified without the file (see BuildCongressTest); certified by it.
        self.assertEqual(old_term["certification"], "certified")
        self.assertIn("us/118/2/house/7", old_term["props"]["certified_by"])

    def test_a_vote_question_in_an_older_year_reads_the_file(self):
        with tempfile.TemporaryDirectory() as d:
            pathlib.Path(d, "congress-votes-118-2.json").write_text(json.dumps(OLDER))
            with mock.patch.object(graph, "DATA_DIR", pathlib.Path(d)), \
                    mock.patch.object(graph, "current_session", return_value=(119, 2, 2026)):
                nodes, edges, _ = graph.build_congress(LEGISLATORS, [SNAPSHOT], today=TODAY)
                b = graph.memory_backend(nodes, edges)
                out = graph.search("how did Griffith vote on taxation in 2024", b, today=TODAY)
                self.assertEqual([(r["item_id"], r["position"], r["title"]) for r in out["rows"]],
                                 [("instrument/us/118/hr/99", "no", "H R 99: The Older Act")])
                self.assertEqual(out["from_snapshot"], ["congress-votes-118-2.json"])
                gone = graph.search("how did Griffith vote in 2020", b, today=TODAY)
                self.assertEqual(gone["empty_reason"], "no roll-call snapshot on disk for 2020")
                # A year inside the loaded Congress stays on the graph.
                now = graph.search("how did Griffith vote in 2026", b, today=TODAY)
                self.assertNotIn("from_snapshot", now)
                self.assertEqual({r["date"][:4] for r in now["rows"]}, {"2026"})

    def test_the_index_keeps_only_each_members_first_and_last_roll_call(self):
        more = {"votes": [house_vote(n, "H R 99", {"no": ["G000568"]}, date=d)
                          | {"vote_id": f"us/118/2/house/{n}", "congress": 118}
                          for n, d in ((8, "2024-05-01"), (9, "2024-01-15"), (10, "2024-09-30"))]}
        idx = graph.member_congress_index([OLDER, more])
        self.assertEqual(idx["bioguide:G000568"], {"118/house": {
            "first": "2024-01-15", "first_vote": "us/118/2/house/9",
            "last": "2024-09-30", "last_vote": "us/118/2/house/10", "source": "clerk.house.gov"}})

    def test_a_term_between_the_two_ends_is_not_certified(self):
        # Both ends fall outside the 2023–2025 term. The member may well have
        # voted inside it, but the index cannot show that: it misses the
        # certification rather than inventing one from the interval.
        straddle = {"bioguide:G000568": {"118/house": {
            "first": "2022-12-01", "first_vote": "a", "last": "2025-02-01", "last_vote": "b",
            "source": "clerk.house.gov"}}}
        nodes, edges, _ = graph.build_congress(LEGISLATORS, [SNAPSHOT], today=TODAY, cert_index=straddle)
        griffith = person_named(nodes, "H. Morgan Griffith")
        old_term = next(e for e in by_pred(edges, "holds")
                        if e["src"] == griffith["id"] and e["valid_from"] == "2023-01-03")
        self.assertEqual(old_term["certification"], "ingested")

    def test_an_older_year_opens_only_its_own_congress_files(self):
        with tempfile.TemporaryDirectory() as d:
            pathlib.Path(d, "congress-votes-118-2.json").write_text(json.dumps(OLDER))
            # Unparseable on purpose: opening it would raise.
            pathlib.Path(d, "congress-votes-50-1.json").write_text("{not json")
            with mock.patch.object(graph, "DATA_DIR", pathlib.Path(d)):
                rows, _, _, files = graph.snapshot_votes(
                    [{"id": "p", "name": "G", "bioguide": "G000568"}], 2024, None, 10, 119)
        self.assertEqual(files, ["congress-votes-118-2.json"])
        self.assertEqual([r["vote_id"] for r in rows], ["us/118/2/house/7"])


BILLSTATUS_XML = b"""<billStatus><version>3.0.0</version><bill>
<number>3633</number><type>HR</type><introducedDate>2025-05-29</introducedDate><congress>119</congress>
<committees><item><systemCode>hsag00</systemCode><name>Agriculture Committee</name><chamber>House</chamber>
  <activities><item><name>Referred to</name><date>2025-05-29T14:00:00Z</date></item></activities>
  <subcommittees><item><systemCode>hsag22</systemCode><name>Commodity Markets</name>
    <activities><item><name>Referred to</name><date>2025-06-02T09:00:00Z</date></item></activities></item></subcommittees></item></committees>
<committeeReports><committeeReport><citation>H. Rept. 119-168,Part 1</citation></committeeReport>
  <committeeReport><citation>H. Rept. 119-168,Part 2</citation></committeeReport></committeeReports>
<relatedBills><item><title>Digital Asset Act</title><congress>119</congress><number>4763</number><type>HR</type>
  <relationshipDetails><item><identifiedBy>CRS</identifiedBy><type>Related bill</type></item></relationshipDetails></item></relatedBills>
<actions><item><actionDate>2025-07-17</actionDate><text>Passed/agreed to in House.</text><type>Floor</type><actionCode>8000</actionCode></item>
  <item><actionDate>2025-07-20</actionDate><text>Signed by President.</text><type>President</type><actionCode>E30000</actionCode></item></actions>
<sponsors><item><bioguideId>H001072</bioguideId></item></sponsors>
<cosponsors><item><bioguideId>T000467</bioguideId><sponsorshipDate>2025-05-29</sponsorshipDate><isOriginalCosponsor>True</isOriginalCosponsor></item>
  <item><bioguideId>X000001</bioguideId><sponsorshipDate>2025-06-10</sponsorshipDate><isOriginalCosponsor>False</isOriginalCosponsor>
  <sponsorshipWithdrawnDate>2025-06-20</sponsorshipWithdrawnDate></item></cosponsors>
<laws><item><type>Public Law</type><number>119-99</number></item></laws>
<policyArea><name>Finance and Financial Sector</name></policyArea>
<title>Digital Asset Market Clarity Act of 2025</title></bill></billStatus>"""

CRPT_MODS = b"""<mods xmlns="http://www.loc.gov/mods/v3"><originInfo><dateIssued>2025-06-23</dateIssued></originInfo>
<relatedItem type="constituent" ID="p1"><extension><congCommittee authorityId="hsag00"/></extension>
  <part><detail><number>1</number></detail></part><titleInfo><partNumber>1</partNumber></titleInfo></relatedItem>
<relatedItem type="constituent" ID="p2"><extension><congCommittee authorityId="hsba00"/></extension>
  <titleInfo><partNumber>2</partNumber></titleInfo></relatedItem></mods>"""


class BillStatusTest(unittest.TestCase):
    """sources/govinfo.py: BILLSTATUS and CRPT metadata → the record shape the
    Congress.gov fetch produced (parity on 509 voted bills of the 119th,
    2026-09-26: see the commit that added this)."""

    def setUp(self):
        from sources import govinfo
        self.g = govinfo

    def test_a_bill_record_in_the_congress_gov_shape(self):
        label, rec = self.g.parse_billstatus(BILLSTATUS_XML)
        self.assertEqual(label, "hr/3633")
        self.assertEqual((rec["title"], rec["introduced"], rec["policy_area"], rec["sponsors"], rec["laws"]),
                         ("Digital Asset Market Clarity Act of 2025", "2025-05-29", "Finance and Financial Sector",
                          ["H001072"], [{"number": "119-99", "type": "Public Law"}]))
        self.assertEqual(rec["cosponsors"], [
            {"id": "T000467", "date": "2025-05-29", "original": True, "withdrawn": None},
            {"id": "X000001", "date": "2025-06-10", "original": False, "withdrawn": "2025-06-20"}])
        # Only what the President did is kept, as before.
        self.assertEqual([a["code"] for a in rec["actions"]], ["E30000"])
        self.assertEqual([(c["code"], c["parent"], c["activities"]) for c in rec["committees"]], [
            ("hsag00", None, [{"name": "Referred to", "date": "2025-05-29"}]),
            ("hsag22", "hsag00", [{"name": "Referred to", "date": "2025-06-02"}])])
        self.assertEqual(rec["related"], [{"congress": 119, "type": "hr", "number": "4763",
                                           "title": "Digital Asset Act",
                                           "relationships": [{"type": "Related bill", "identified_by": "CRS"}]}])
        # An empty list in the XML is a real zero; only reports wait for CRPT.
        self.assertEqual(rec["passes"], ["bill", "cosponsors", "actions", "committees", "related"])

    def test_each_part_of_a_report_keeps_its_own_committee(self):
        _, rec = self.g.parse_billstatus(BILLSTATUS_XML)
        meta = self.g.parse_crpt_mods(CRPT_MODS)
        errors = []
        self.g.attach_reports(rec, lambda ep: meta if ep == "119/HRPT/168" else None, errors)
        self.assertEqual(errors, [])
        self.assertEqual([(r["citation"], r["date"], r["committees"]) for r in rec["reports"]], [
            ("H. Rept. 119-168,Part 1", "2025-06-23", ["hsag00"]),
            ("H. Rept. 119-168,Part 2", "2025-06-23", ["hsba00"])])
        self.assertIn("reports", rec["passes"])

    def test_an_erratum_names_no_committee(self):
        meta = self.g.parse_crpt_mods(CRPT_MODS)
        self.assertEqual(self.g.report_meta("S. Rept. 119-39,Errata", meta), {"date": "", "committees": []})

    def test_an_unresolved_report_leaves_the_pass_out(self):
        # A partial report list would read as the whole list.
        _, rec = self.g.parse_billstatus(BILLSTATUS_XML)
        errors = []
        self.g.attach_reports(rec, lambda ep: None, errors)
        self.assertNotIn("reports", rec)
        self.assertNotIn("reports", rec["passes"])
        self.assertEqual(errors, ["CRPT 119/HRPT/168: no package metadata"])


VV_MEMBERS = """congress,chamber,icpsr,state_icpsr,district_code,state_abbrev,party_code,occupancy,last_means,bioname,bioguide_id,born,died
110,President,99910,99,0,USA,200,,,"BUSH, George Walker",,1946,
110,House,20750,40,9,VA,200,,,"BOUCHER, Frederick",B000657,1946,
110,House,29911,40,5,VA,200,,,"GOODE, Virgil",G000286,1946,
110,House,11111,40,1,VA,100,,,"NOBODY, Known",,1900,
"""
VV_ROLLCALLS = """congress,chamber,rollnumber,date,session,clerk_rollnumber,majority_requirement,yea_count,nay_count,nominate_mid_1,nominate_mid_2,nominate_spread_1,nominate_spread_2,nominate_log_likelihood,bill_number,vote_result,vote_desc,vote_question,dtl_desc
110,House,1,2007-01-04,,2,,,,,,,,,HRES5,Passed,,On Ordering the Previous Question,
110,House,2,2008-03-01,,3,,,,,,,,,,Passed,,Call of the House,
"""
VV_VOTES = """congress,chamber,rollnumber,icpsr,cast_code,prob
110,House,1,20750,1,99
110,House,1,29911,9,50
110,House,1,11111,6,50
110,House,2,20750,0,0
110,House,2,29911,5,50
"""


class VoteviewTest(unittest.TestCase):
    """sources/voteview.py. Checked 2026-09-26 against the clerks' 118th
    House files: every position agreed on 1,215 roll calls; the 12 that
    differed were Speaker elections, which the clerk records by name."""

    def setUp(self):
        from sources import voteview
        self.v = voteview

    def test_positions_ids_and_years(self):
        gaps = []
        by_year = self.v.convert(110, "H", VV_VOTES, VV_ROLLCALLS, VV_MEMBERS,
                                 {"20750": "B000657", "29911": "G000286"}, gaps)
        self.assertEqual(sorted(by_year), ["2007", "2008"])
        v = by_year["2007"][0]
        self.assertEqual((v["vote_id"], v["legis_num"], v["source_id"], v["id_kind"]),
                         ("us/110/voteview/house/1", "HRES 5", "voteview", "bioguide"))
        # 9 is "not voting", the clerk's absent — never present.
        self.assertEqual(v["positions"], {"aye": ["B000657"], "absent": ["G000286"]})
        self.assertEqual(graph._parse_instrument("house", v), ("hres", "5"))
        # 0 is "not a member then": no position at all.
        self.assertEqual(by_year["2008"][0]["positions"], {"no": ["G000286"]})
        self.assertIsNone(graph._parse_instrument("house", by_year["2008"][0]))
        self.assertEqual(gaps, ["110 house: 1 member(s) with no bioguide id in Voteview; "
                                "their positions are not recorded"])

    def test_an_id_the_legislators_file_contradicts_is_dropped(self):
        gaps = []
        by_year = self.v.convert(110, "H", VV_VOTES, VV_ROLLCALLS, VV_MEMBERS,
                                 {"20750": "B000657", "29911": "X999999"}, gaps)
        self.assertEqual(by_year["2007"][0]["positions"], {"aye": ["B000657"]})
        self.assertIn("29911 (G000286 vs X999999)", gaps[0])


class NominationAndLobbyingRecordTest(unittest.TestCase):
    def test_a_nomination_keeps_the_senates_document_name(self):
        from sources import nominations
        item = {"number": 12, "partNumber": "01", "receivedDate": "2023-01-03",
                "nominationType": {"isMilitary": False}, "organization": "The Judiciary",
                "latestAction": {"actionDate": "2023-02-01", "text": "Confirmed by the Senate by Voice Vote."},
                "updateDate": "2026-01-01T00:00:00Z"}
        rec = nominations.record(item, {"description": "Jane Doe, to be a judge.",
                                        "nominees": [{"positionTitle": "Judge", "organization": "The Judiciary",
                                                      "nomineeCount": 1}]})
        # The roll call names the document PN12-1; the record must too.
        self.assertEqual(rec["citation"], "PN12-1")
        self.assertEqual(nominations.citation({"number": 1020, "partNumber": "00"}), "PN1020")
        self.assertEqual(rec["positions"], [{"title": "Judge", "organization": "The Judiciary", "nominees": 1}])
        self.assertEqual(rec["latest_action"]["date"], "2023-02-01")

    def test_a_lobbying_filing_names_its_bills_and_keeps_the_text_it_read_them_from(self):
        from sources import lda_client
        rec = lda_client.bulk_record({
            "filing_uuid": "u1", "filing_type": "Q2", "filing_year": 2026, "filing_period": "second_quarter",
            "dt_posted": "2026-07-20T10:00:00-04:00", "income": None, "expenses": "30000.00",
            "registrant": {"id": 1, "name": "IANA"}, "client": {"id": 2, "name": "IANA"},
            "lobbying_activities": [{"general_issue_code": "TRA",
                                     "description": "H.R. 2853, S. 1404 and truck size rules"}]})
        self.assertEqual(rec["amount"], 30000.0)
        self.assertEqual(rec["activities"], [{"issue": "TRA", "bills": ["hr/2853", "s/1404"],
                                              "description": "H.R. 2853, S. 1404 and truck size rules"}])


class VocabularyTest(unittest.TestCase):
    def test_the_predicate_set_is_pinned(self):
        # A new relation type is a schema change: it lands here on purpose.
        self.assertEqual(set(graph.PREDICATES), {
            "contains", "has_body", "has_seat", "holds", "represents", "sponsored", "voted_on",
            "considered", "elected_in", "for_seat", "signed", "vetoed", "enacted_as", "member_of",
            "referred_to", "reported", "related_to", "campaign_committee"})


class ParseInstrumentTest(unittest.TestCase):
    def test_house_forms(self):
        for legis, want in (("H R 5184", ("hr", "5184")), ("H J RES 3", ("hjres", "3")),
                            ("S CON RES 4", ("sconres", "4")), ("QUORUM", None), ("", None)):
            self.assertEqual(graph._parse_instrument("house", {"legis_num": legis}), want)

    def test_senate_nominations_and_amendments(self):
        # A batched nomination keeps its dashed number; an amendment vote is
        # a vote on the bill it amends.
        self.assertEqual(graph._parse_instrument("senate", {"document_type": "PN",
                                                             "document_number": "12-1"}),
                         ("pn", "12-1"))
        self.assertEqual(graph._parse_instrument("senate", {"document_type": "S.Amdt.",
                                                             "document_number": "",
                                                             "amendment_to": "H.R. 7148"}),
                         ("hr", "7148"))
        self.assertIsNone(graph._parse_instrument("senate", {"document_type": "S.Amdt.",
                                                              "document_number": ""}))


class CrossLayerIdentityTest(unittest.TestCase):
    def test_walkinshaw_is_one_node_in_both_layers(self):
        # Fairfax keys him by seat + surname, Congress by bioguide id; the
        # IDENTITIES assertion makes both loaders write the same node id, so
        # a traversal from him reaches county and federal votes alike.
        local_nodes, local_edges, _ = graph.build("fairfax-bos", store(), CONTESTS, SUMMARIES)
        fed_nodes, fed_edges, _ = graph.build_congress(LEGISLATORS, [SNAPSHOT], states=["VA"],
                                                       today="2026-09-20")
        local = person_named(local_nodes, "James R. Walkinshaw")
        fed = person_named(fed_nodes, "James R. Walkinshaw")
        self.assertEqual(local["id"], fed["id"])
        self.assertEqual(local["props"]["identity"], "bioguide/W000831")
        self.assertIn("manual", local["props"]["identity_asserted_by"])
        holds = [e for e in local_edges + fed_edges
                 if e["predicate"] == "holds" and e["src"] == fed["id"]]
        self.assertEqual(len(holds), 2)   # Braddock District and VA-11
        # A person nobody has bridged stays keyed by seat + surname.
        heizer = person_named(local_nodes, "Rachna Sizemore Heizer")
        self.assertNotIn("identity", heizer["props"])


# ------------------------------------------------------------------ search

TODAY = "2026-09-20"


def harness():
    """Both layers of the fixture graph behind the memory backend — the
    same lookups the Postgres backend runs as SQL."""
    ln, le, _ = graph.build("fairfax-bos", store(), CONTESTS, SUMMARIES)
    fn, fe, _ = graph.build_congress(LEGISLATORS, [SNAPSHOT], states=["VA"], today=TODAY)
    return graph.memory_backend(ln + fn, le + fe)


class ParseQuestionTest(unittest.TestCase):
    def parse(self, q):
        return graph.parse_question(q, today=TODAY)

    def test_votes_shapes(self):
        self.assertEqual(self.parse("how did Herrity vote on zoning"),
                         {"ask": "votes", "person": "Herrity", "topic": "zoning"})
        self.assertEqual(self.parse("What did Pat Herrity vote on?"),
                         {"ask": "votes", "person": "Pat Herrity", "topic": None})
        self.assertEqual(self.parse("Walkinshaw's votes on housing"),
                         {"ask": "votes", "person": "Walkinshaw", "topic": "housing"})
        self.assertEqual(self.parse("Warner votes"), {"ask": "votes", "person": "Warner", "topic": None})

    def test_holder_shapes(self):
        self.assertEqual(self.parse("who held the Braddock seat on 2025-11-18"),
                         {"ask": "holder", "seat": "Braddock", "as_of": "2025-11-18"})
        self.assertEqual(self.parse("who represents VA-11 as of 2025-09"),
                         {"ask": "holder", "seat": "VA-11", "as_of": "2025-09-30"})
        self.assertEqual(self.parse("who was the Sully supervisor in 2024"),
                         {"ask": "holder", "seat": "Sully", "as_of": "2024-12-31"})
        self.assertEqual(self.parse("who is the Braddock supervisor")["as_of"], TODAY)

    def test_voters_shapes(self):
        self.assertEqual(self.parse("who voted no on the Affordable HOMES Act"),
                         {"ask": "voters", "topic": "Affordable HOMES Act", "position": "no"})
        self.assertEqual(self.parse("who voted against glue traps"),
                         {"ask": "voters", "topic": "glue traps", "position": "no"})
        self.assertEqual(self.parse("who voted on PN12-1"),
                         {"ask": "voters", "topic": "PN12-1", "position": None})

    def test_not_our_question_falls_through(self):
        # Same contract as fast_route: None means "not mine", never a guess.
        for q in ("What is HR 1234", "Virginia housing bills", "", "zoning in Fairfax"):
            self.assertIsNone(self.parse(q), q)

    def test_seat_terms(self):
        self.assertEqual(graph._seat_terms("VA-11"), ["us/house/va/cd:11"])
        # The state narrows the district: every state has an 11th.
        self.assertEqual(graph._seat_terms("Virginia's 11th district"), ["/va/", "cd:11"])
        self.assertEqual(graph._seat_terms("senator for West Virginia"), ["/wv/", "senator"])
        self.assertEqual(graph._seat_terms("the Braddock District seat"), ["braddock"])


class SearchTest(unittest.TestCase):
    def setUp(self):
        self.b = harness()

    def ask(self, q):
        return graph.search(q, self.b, today=TODAY)

    def test_person_votes_with_topic_cross_layer(self):
        a = self.ask("how did Herrity vote on rezone")
        self.assertEqual([p["name"] for p in a["persons"]], ["Patrick S. Herrity"])
        self.assertEqual([(r["title"], r["position"]) for r in a["rows"]], [("REZONE 12 ACRES", "no")])
        self.assertEqual(a["weak_hops"][0]["weakest"], "ingested")
        self.assertEqual(a["advisory_fields"], ["topic"])
        # A place inside the topic is scope, not subject.
        p = self.ask("how did Herrity vote on Fairfax rezone")
        self.assertEqual(p["count"], 1)
        self.assertEqual(p["place_ignored"], "Fairfax")
        # Alias resolves; topic miss says how many votes there were.
        miss = self.ask("how did Pat Herrity vote on glue traps")
        self.assertEqual(miss["count"], 0)
        self.assertIn("glue traps", miss["empty_reason"])
        self.assertIn("4 recorded vote", miss["empty_reason"])
        # One person, both layers: the federal vote is in the same answer.
        w = self.ask("Walkinshaw's votes")
        self.assertEqual({r["jurisdiction"] for r in w["rows"]}, {graph.US})

    def test_unknown_person(self):
        a = self.ask("how did Nobody vote on zoning")
        self.assertEqual(a["rows"], [])
        self.assertIn("no person", a["empty_reason"])

    def test_seat_holder_as_of(self):
        a = self.ask("who held the Braddock seat on 2025-11-18")
        self.assertEqual([h["name"] for h in a["holders"]], ["James R. Walkinshaw"])
        self.assertEqual(a["inferred_bounds"], ["James R. Walkinshaw"])
        self.assertEqual(a["weak_hops"][0]["predicate"], "holds")
        b = self.ask("who is the Braddock supervisor")
        self.assertEqual([h["name"] for h in b["holders"]], ["Rachna Sizemore Heizer"])
        self.assertEqual(b["holders"][0]["bound_from"], "observed")
        c = self.ask("who represents VA-11 as of 2025-09-01")
        self.assertEqual(c["holders"], [])
        self.assertIn("vacant", c["empty_reason"])
        d = self.ask("who represented VA-11 on 2026-03-03")
        self.assertEqual([h["name"] for h in d["holders"]], ["James R. Walkinshaw"])

    def test_unknown_seat(self):
        a = self.ask("who holds the Algonkian seat")
        self.assertEqual(a["holders"], [])
        self.assertIn("no seat", a["empty_reason"])

    def test_voters_on_an_instrument(self):
        a = self.ask("who voted no on REZONE")
        self.assertEqual(a["persons"], ["Patrick S. Herrity"])
        b = self.ask("who voted on Some Act")   # the H.R. 5184 description
        self.assertEqual(sorted(b["persons"]), ["H. Morgan Griffith", "James R. Walkinshaw"])
        self.assertEqual(b["weak_hops"][0]["weakest"], "ingested")
        c = self.ask("who voted aye on nothing here")
        self.assertEqual(c["rows"], [])
        self.assertIn("nothing here", c["empty_reason"])

    def test_falls_through_and_empty_graph(self):
        self.assertIsNone(self.ask("What is HR 1234"))
        empty = graph.memory_backend([], [])
        out = graph.search("how did Herrity vote on zoning", empty, today=TODAY)
        self.assertIn("graph not loaded", out["empty_reason"])


class LedgerRoutingTest(unittest.TestCase):
    """The graph sits in front of the ledger's place and topic guesses."""

    def classify(self, q, **kw):
        from agents.ledger_agent import classify_question
        return classify_question(q, **kw)

    def test_graph_shapes_get_the_graph_plate(self):
        for q in ("how did Herrity vote on Fairfax zoning", "who voted no on the Affordable HOMES Act",
                  "who held the Braddock seat on 2025-11-18", "who represents VA-11"):
            out = self.classify(q)
            self.assertEqual(out["plate"], "graph", q)
            self.assertIn("ask", out)

    def test_everything_else_is_untouched(self):
        self.assertEqual(self.classify("Stafford County")["plate"], "uncharted")
        self.assertEqual(self.classify("HR 1")["plate"], "bill")
        self.assertEqual(self.classify("healthcare bills")["plate"], "ledger")
        self.assertEqual(self.classify("")["plate"], "home")
        # "Who is <person>" is a member lookup, not a seat question.
        self.assertEqual(self.classify("Who is Ted Cruz")["plate"], "ledger")
        self.assertEqual(self.classify("who represents Braddock District")["plate"], "graph")
        self.assertEqual(self.classify("who was the senator for Virginia in 2010")["plate"], "graph")

    def test_fallback_when_the_graph_declines(self):
        out = self.classify("how did Herrity vote on Fairfax zoning", allow_graph=False)
        self.assertNotEqual(out["plate"], "graph")


# ---------------------------------------------------------------- sponsors

SNAPSHOT_WITH_SPONSORS = dict(SNAPSHOT, instruments={
    "hr/5184": {"title": "Affordable HOMES Act", "introduced": "2025-09-08",
                "policy_area": "Housing and Community Development",
                "sponsors": ["G000568"],
                "cosponsors": [{"id": "W000831", "date": "2025-10-01", "original": False, "withdrawn": None},
                               {"id": "A000370", "date": "2025-09-08", "original": True, "withdrawn": None},
                               {"id": "ZZ99999", "date": "2025-09-09", "original": False, "withdrawn": None}]},
})


class SponsoredTest(unittest.TestCase):
    def setUp(self):
        self.nodes, self.edges, self.gaps = graph.build_congress(
            LEGISLATORS, [SNAPSHOT_WITH_SPONSORS], states=["VA"], today=TODAY)

    def test_sponsor_and_cosponsor_edges_with_their_dates(self):
        by_person = {}
        for e in by_pred(self.edges, "sponsored"):
            by_person[next(n["name"] for n in self.nodes if n["id"] == e["src"])] = e
        self.assertEqual(sorted(by_person), ["H. Morgan Griffith", "James R. Walkinshaw"])  # Adams is NC
        self.assertEqual((by_person["H. Morgan Griffith"]["valid_from"], by_person["H. Morgan Griffith"]["props"]["role"]),
                         ("2025-09-08", "sponsor"))
        self.assertEqual((by_person["James R. Walkinshaw"]["valid_from"], by_person["James R. Walkinshaw"]["props"]["role"]),
                         ("2025-10-01", "cosponsor"))
        self.assertEqual(by_person["James R. Walkinshaw"]["source_id"], "congress.gov")

    def test_title_and_policy_area_come_from_the_record(self):
        inst = next(n for n in self.nodes if n["id"] == "instrument/us/119/hr/5184")
        self.assertEqual(inst["name"], "H R 5184: Affordable HOMES Act")
        self.assertEqual(inst["props"]["topic"], "Housing and Community Development")
        self.assertEqual(inst["props"]["topic_derived_by"], "congress.gov policyArea")

    def test_snapshot_without_sponsors_is_a_named_gap(self):
        _, edges, gaps = graph.build_congress(LEGISLATORS, [SNAPSHOT], states=["VA"], today=TODAY)
        self.assertEqual(by_pred(edges, "sponsored"), [])
        self.assertTrue(any("no bill records" in g for g in gaps))


class SponsorSearchTest(unittest.TestCase):
    def setUp(self):
        ln, le, _ = graph.build("fairfax-bos", store(), CONTESTS, SUMMARIES)
        fn, fe, _ = graph.build_congress(LEGISLATORS, [SNAPSHOT_WITH_SPONSORS], states=["VA"], today=TODAY)
        self.b = graph.memory_backend(ln + fn, le + fe)

    def ask(self, q):
        return graph.search(q, self.b, today=TODAY)

    def test_parse(self):
        self.assertEqual(graph.parse_question("who sponsored the Affordable HOMES Act", today=TODAY),
                         {"ask": "sponsors", "topic": "Affordable HOMES Act"})
        self.assertEqual(graph.parse_question("what did Griffith sponsor?", today=TODAY),
                         {"ask": "sponsored", "person": "Griffith"})
        self.assertEqual(graph.parse_question("Griffith's bills", today=TODAY),
                         {"ask": "sponsored", "person": "Griffith"})

    def test_who_sponsored(self):
        a = self.ask("who sponsored the Affordable HOMES Act")
        self.assertEqual(a["persons"], ["H. Morgan Griffith", "James R. Walkinshaw"])
        self.assertEqual({r["position"] for r in a["rows"]}, {"sponsor", "cosponsor"})
        self.assertEqual(a["hops"][0]["predicate"], "sponsored")
        # A policy-area topic is the record's own: the filter is not advisory.
        h = self.ask("who sponsored Housing")
        self.assertEqual(h["advisory_fields"], [])
        self.assertEqual(h["topic_sources"], ["congress.gov policyArea"])

    def test_what_did_they_sponsor_and_the_empty_state(self):
        a = self.ask("what did Griffith sponsor")
        self.assertEqual([(r["title"], r["position"]) for r in a["rows"]],
                         [("H R 5184: Affordable HOMES Act", "sponsor")])
        e = self.ask("what did Warner sponsor")
        self.assertEqual(e["rows"], [])
        self.assertIn("only bills with a recorded vote", e["empty_reason"])

    def test_county_topic_filter_is_still_advisory(self):
        a = self.ask("how did Herrity vote on zoning")
        self.assertEqual(a["advisory_fields"], ["topic"])


# ------------------------------------------------------- FEC bulk files

from sources import fec_client  # noqa: E402


def ccl_row(**kw):
    d = dict(CAND_ID="S6VA00093", CAND_ELECTION_YR="2026", FEC_ELECTION_YR="2026",
              CMTE_ID="C00438713", CMTE_TP="S", CMTE_DSGN="P", LINKAGE_ID="259083")
    d.update(kw)
    return d


def pas2_row(**kw):
    d = dict(CMTE_ID="C00451518", AMNDT_IND="N", RPT_TP="M3", TRANSACTION_PGI="P2026",
              IMAGE_NUM="1", TRANSACTION_TP="24K", ENTITY_TP="CCM", NAME="FRIENDS OF MARK WARNER",
              CITY="ALEXANDRIA", STATE="VA", ZIP_CODE="22314", EMPLOYER="", OCCUPATION="",
              TRANSACTION_DT="02242025", TRANSACTION_AMT="1000", OTHER_ID="C00438713",
              CAND_ID="S6VA00093", TRAN_ID="T1", FILE_NUM="1", MEMO_CD="", MEMO_TEXT="", SUB_ID="1")
    d.update(kw)
    return d


class FecBulkTest(unittest.TestCase):
    """Pure functions over small inline rows in the real `|`-column order —
    no network, no zip file. Real-download parity (weball totals for
    Warner, S6VA00093 -> C00438713) is reported separately, not pinned
    here, because it depends on a live FEC snapshot."""

    def test_principal_committee_needs_this_cycles_linkage(self):
        # A candidate's ccl row from a stale cycle (FEC_ELECTION_YR 2024) is
        # not this snapshot's principal committee, even though it's the
        # only 'P' row on file for the candidate.
        rows = [ccl_row(FEC_ELECTION_YR="2024")]
        self.assertEqual(fec_client.bulk_principal_committees(rows, [], 2026), {})
        rows = [ccl_row(FEC_ELECTION_YR="2026")]
        self.assertEqual(fec_client.bulk_principal_committees(rows, [], 2026),
                         {"S6VA00093": "C00438713"})

    def test_principal_committee_tie_break_is_highest_linkage_id(self):
        rows = [ccl_row(CMTE_ID="C1", LINKAGE_ID="100"),
                ccl_row(CMTE_ID="C2", LINKAGE_ID="200")]
        self.assertEqual(fec_client.bulk_principal_committees(rows, [], 2026),
                         {"S6VA00093": "C2"})

    def test_principal_committee_ignores_non_p_designations(self):
        rows = [ccl_row(CMTE_DSGN="J", CMTE_ID="C_JFC")]
        self.assertEqual(fec_client.bulk_principal_committees(rows, [], 2026), {})

    def test_principal_committee_falls_back_to_cn_cand_pcc_when_ccl_has_no_row(self):
        # Real 2026 case: Andy Barr (H0KY06104) has zero ccl rows for any
        # cycle, but cn's CAND_PCC (C00467571) matches the API-built
        # fec-2026.json's committee_id for him.
        cn_rows = [{"CAND_ID": "H0KY06104", "CAND_PCC": "C00467571"}]
        self.assertEqual(fec_client.bulk_principal_committees([], cn_rows, 2026),
                         {"H0KY06104": "C00467571"})

    def test_principal_committee_prefers_ccl_over_cn_when_both_exist(self):
        rows = [ccl_row()]
        cn_rows = [{"CAND_ID": "S6VA00093", "CAND_PCC": "C_STALE"}]
        self.assertEqual(fec_client.bulk_principal_committees(rows, cn_rows, 2026),
                         {"S6VA00093": "C00438713"})

    def test_committee_names(self):
        cm_rows = [{"CMTE_ID": "C00438713", "CMTE_NM": "FRIENDS OF MARK WARNER"}]
        self.assertEqual(fec_client.bulk_committee_names(cm_rows), {"C00438713": "FRIENDS OF MARK WARNER"})

    def test_totals_column_mapping_and_date_conversion(self):
        # Real weball26 row for S6VA00093 (2026-09-26 download), one field per
        # _WEBALL_COLS position, values matching the committed API snapshot.
        row = dict(zip(fec_client._WEBALL_COLS,
                       "S6VA00093|WARNER, MARK ROBERT|I|1|DEM|17889335.15|5792755.4|7327843.73|0|"
                       "5553233.82|16114725.24|0|0|0|0|0|0|9258155.93|VA|00||||||2117795|0|"
                       "07/15/2026|295824.5|14500".split("|")))
        totals = fec_client.bulk_totals([row])["S6VA00093"]
        self.assertEqual(totals, {
            "receipts": 17889335.15, "disbursements": 7327843.73,
            "last_cash_on_hand_end_period": 16114725.24, "individual_contributions": 9258155.93,
            "other_political_committee_contributions": 2117795.0,
            "coverage_end_date": "2026-07-15T00:00:00",
        })

    def test_totals_blank_money_fields_are_zero_not_missing(self):
        # 5 name/party columns + 13 blank money columns (TTL_RECEIPTS..
        # TTL_INDIV_CONTRIB) + CAND_OFFICE_ST/DISTRICT + 10 more blanks
        # (election flags through CMTE_REFUNDS) = 30, matching _WEBALL_COLS.
        row = dict(zip(fec_client._WEBALL_COLS, ["C1", "N", "C", "1", "DEM"] + [""] * 13 +
                                                 ["VA", "00"] + [""] * 10))
        totals = fec_client.bulk_totals([row])["C1"]
        self.assertEqual(totals["receipts"], 0.0)
        self.assertEqual(totals["individual_contributions"], 0.0)
        self.assertIsNone(totals["coverage_end_date"])

    def test_top_pacs_scopes_to_the_recipient_committee_and_24k(self):
        rows = [pas2_row(), pas2_row(OTHER_ID="C_OTHER", CMTE_ID="C_OTHER_PAC"),
                pas2_row(TRANSACTION_TP="15", CMTE_ID="C_NOT_24K")]
        cm_names = {"C00451518": "CROWE PAC"}
        top, total, n = fec_client.bulk_top_pacs(rows, "C00438713", "Mark Warner", cm_names)
        self.assertEqual([t["name"] for t in top], ["CROWE PAC"])
        self.assertEqual(total, 1000.0)
        self.assertEqual(n, 1)

    def test_top_pacs_drops_memo_rows(self):
        # MEMO_CD 'X': FEC's convention for a transaction already itemized
        # elsewhere (e.g. a joint fundraising transfer restated at the
        # receiving end) — counting it would double the dollars.
        rows = [pas2_row(TRANSACTION_AMT="5000", MEMO_CD="X"), pas2_row(TRANSACTION_AMT="1000")]
        top, total, n = fec_client.bulk_top_pacs(rows, "C00438713", "Mark Warner", {})
        self.assertEqual(total, 1000.0)
        self.assertEqual(n, 1)

    def test_top_pacs_reuses_graphs_conduit_and_self_name_exclusions(self):
        rows = [pas2_row(CMTE_ID="C_ACTBLUE", NAME="ACTBLUE"),
                pas2_row(CMTE_ID="C_SELF", TRANSACTION_AMT="500")]
        cm_names = {"C_ACTBLUE": "ACTBLUE", "C_SELF": "WARNER FOR SENATE"}
        top, total, n = fec_client.bulk_top_pacs(rows, "C00438713", "Mark Warner", cm_names)
        # ActBlue is a conduit and "WARNER" is the candidate's own name token —
        # graph.top_pacs excludes both, so nothing survives.
        self.assertEqual(top, [])
        self.assertEqual(total, 0.0)

    def test_amendment_rows_are_kept_and_a_same_key_reversal_nets_out(self):
        # A contribution and its later correction, filed under the same
        # (CMTE_ID, TRAN_ID) — both AMNDT_IND 'N' in the real 2026 pas2
        # file. Summed, not deduplicated: they net to the true balance.
        rows = [pas2_row(TRAN_ID="B1", TRANSACTION_AMT="2500"),
                pas2_row(TRAN_ID="B1", TRANSACTION_AMT="-2500", AMNDT_IND="N")]
        top, total, n = fec_client.bulk_top_pacs(rows, "C00438713", "Mark Warner",
                                                  {"C00451518": "CROWE PAC"})
        self.assertEqual(total, 0.0)
