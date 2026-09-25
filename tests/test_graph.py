"""Pure-logic tests for the government graph (graph.py).

Fixtures mirror the real Fairfax defects rather than reading the live store:
a duplicate roster spelling, a sentence fragment parsed as a member, one item
with three roll calls, a seat that changed hands with no contest on disk, and
a person who won two different bodies' seats in the same district.
"""

import pathlib
import sys
import unittest

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
        self.assertEqual(graph._seat_terms("Virginia's 11th district"), ["cd:11"])
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
        self.assertTrue(any("no sponsor records" in g for g in gaps))


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
