"""Pure-logic tests for the Foundry health ledger (foundry/health.py).

Covers the two things the dashboard's honesty depends on: that `summarize`
derives the right status vocabulary from a store, and that the ledger stays
bounded and never raises into a pipeline run.
"""

import json
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "foundry"))

import health  # noqa: E402


def cert(status, note=None):
    return {"certification": {"status": status, "method": None, "note": note}}


def store(meetings=(), votes=(), items=(), meta=None):
    return {
        "meetings": {f"m{i}": {"meeting_id": f"m{i}", "date": d, **c}
                     for i, (d, c) in enumerate(meetings)},
        "agenda_items": {f"i{i}": {"item_id": f"i{i}", **c}
                         for i, c in enumerate(items)},
        "vote_events": {f"v{i}": {"vote_id": f"v{i}", **c}
                        for i, c in enumerate(votes)},
        "members": {},
        "meta": meta if meta is not None else {},
    }


class SummarizeTest(unittest.TestCase):
    def test_counts_and_percentage(self):
        s = store(meetings=[("2026-09-01", cert("certified"))],
                  votes=[cert("certified"), cert("quarantined")])
        out = health.summarize("x-bos", s, [], "2026-09-10")
        self.assertEqual(out["records"], {"meetings": 1, "agenda_items": 0,
                                          "vote_events": 2})
        self.assertEqual(out["total_certified"], 2)
        self.assertEqual(out["certified_pct"], 66.7)

    def test_fresh_stale_and_due(self):
        fresh = store(meetings=[("2026-09-01", cert("quarantined"))])
        self.assertEqual(
            health.summarize("x-bos", fresh, [], "2026-09-10")["staleness"],
            "fresh")
        self.assertEqual(
            health.summarize("x-bos", fresh, [], "2026-10-10")["staleness"],
            "stale")
        # A meeting the schedule promised has now passed and is newer than
        # anything stored: due, even though the store is not yet stale.
        upcoming = {"upcoming": [{"date": "2026-09-05"}, {"date": "2026-12-01"}]}
        out = health.summarize("x-bos", fresh, [], "2026-09-10", upcoming)
        self.assertEqual(out["staleness"], "due")
        self.assertEqual(out["next_expected"], "2026-12-01")

    def test_staleness_not_applicable_to_non_meeting_stores(self):
        cip = {"meta": {"kind": "capital_projects"}, "capital_projects": {}}
        out = health.summarize("x-cip", cip, [], "2026-09-10")
        self.assertEqual(out["staleness"], "n/a")
        self.assertEqual(out["kind"], "capital_projects")

    def test_quarantine_reasons_separate_lag_from_failure(self):
        s = store(votes=[
            cert("quarantined", "no second-source assertions for this meeting"),
            cert("quarantined", "ingest-only until the promoted oracle recertifies"),
            cert("quarantined", "primary says pass, second source says fail"),
            cert("quarantined"),
            cert("certified")])
        out = health.summarize("x-bos", s, [], "2026-09-10")
        self.assertEqual(out["quarantine_reasons"],
                         {"lag": 1, "uncertifiable": 1, "disputed": 1,
                          "unreached": 1})

    def test_votes_inherit_their_meetings_publication_lag(self):
        # A vote in a meeting the second source never reached has no note of
        # its own. Counting it as an unexplained gap would report the clerk's
        # publication lag as our failure.
        s = store(meetings=[("2026-09-01",
                             cert("quarantined",
                                  "no second-source assertions for this meeting"))],
                  votes=[cert("quarantined"), cert("quarantined")])
        for v in s["vote_events"].values():
            v["meeting_id"] = "m0"
        out = health.summarize("x-bos", s, [], "2026-09-10")
        self.assertEqual(out["quarantine_reasons"], {"lag": 3})

        # ...but a vote in a COVERED meeting that simply was not affirmed
        # stays unexplained — that one is ours to chase.
        s2 = store(meetings=[("2026-09-01", cert("certified"))],
                   votes=[cert("quarantined")])
        list(s2["vote_events"].values())[0]["meeting_id"] = "m0"
        out2 = health.summarize("x-bos", s2, [], "2026-09-10")
        self.assertEqual(out2["quarantine_reasons"], {"unreached": 1})

    def test_oracle_status_vocabulary(self):
        none_run = health.summarize("x-bos", store(), [], "2026-09-10")
        self.assertEqual(none_run["oracle"]["status"], "never-run")

        attempted = health.summarize(
            "x-bos", store(),
            [{"stage": "oracle", "verdict": "failed", "detail": {}}],
            "2026-09-10")
        self.assertEqual(attempted["oracle"]["status"], "failed")

        curated = health.summarize(
            "x-bos", store(votes=[cert("certified")]), [], "2026-09-10")
        self.assertEqual(curated["oracle"]["status"], "curated")

        # "we tried and missed the gate" and "this source cannot affirm a
        # vote at all" are different problems with different fixes, so they
        # must not share a status.
        blocked = health.summarize(
            "x-bos", store(),
            [{"stage": "oracle", "verdict": "blocked",
              "detail": {"reason": "second source carries no vote outcomes"}}],
            "2026-09-10")
        self.assertEqual(blocked["oracle"]["status"], "no-second-source")

        promoted = health.summarize(
            "x-bos", store(meta={"oracle_artifact": "a.py"}), [], "2026-09-10")
        self.assertEqual(promoted["oracle"]["status"], "promoted")

        drifted = health.summarize(
            "x-bos", store(meta={"oracle_artifact": "a.py"}),
            [{"stage": "recertify", "verdict": "error", "detail": {}}],
            "2026-09-10")
        self.assertEqual(drifted["oracle"]["status"], "promoted-drifted")

    def test_open_findings_only_surface_from_a_failing_refresh(self):
        findings = [{"check": "duplicate_vote", "ref": "r", "msg": "m"}]
        drifted = health.summarize(
            "x-bos", store(),
            [{"stage": "refresh", "verdict": "drift",
              "detail": {"findings": findings}}], "2026-09-10")
        self.assertEqual(drifted["open_findings"], findings)
        healthy = health.summarize(
            "x-bos", store(),
            [{"stage": "refresh", "verdict": "ok", "detail": {"added": 2}}],
            "2026-09-10")
        self.assertEqual(healthy["open_findings"], [])

    def test_unknown_when_no_meetings_recorded(self):
        out = health.summarize("x-bos", store(), [], "2026-09-10")
        self.assertEqual(out["staleness"], "unknown")
        self.assertIsNone(out["newest_meeting"])
        self.assertEqual(out["certified_pct"], 0.0)


class LedgerTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = pathlib.Path(self.dir.name) / "health.json"

    def test_repeat_events_collapse_into_a_counter(self):
        for day in range(1, 5):
            health.record("x-bos", "refresh", "drift", {"findings": []},
                          path=self.path, now=f"2026-09-0{day}T07:30:00")
        events = health.events_for(health.load(self.path), "x-bos")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["repeats"], 4)
        self.assertEqual(events[0]["first_ts"], "2026-09-01T07:30:00")
        self.assertEqual(events[0]["ts"], "2026-09-04T07:30:00")

    def test_a_changed_verdict_starts_a_new_event(self):
        health.record("x-bos", "refresh", "drift", path=self.path,
                      now="2026-09-01T07:30:00")
        health.record("x-bos", "refresh", "ok", path=self.path,
                      now="2026-09-02T07:30:00")
        events = health.events_for(health.load(self.path), "x-bos")
        self.assertEqual([e["verdict"] for e in events], ["drift", "ok"])

    def test_findings_are_trimmed(self):
        findings = [{"check": "c", "ref": f"r{i}", "msg": "x" * 400}
                    for i in range(20)]
        health.record("x-bos", "refresh", "drift", {"findings": findings},
                      path=self.path, now="2026-09-01T07:30:00")
        detail = health.events_for(health.load(self.path), "x-bos")[0]["detail"]
        self.assertEqual(len(detail["findings"]), health.MAX_FINDINGS)
        self.assertEqual(detail["findings_total"], 20)
        self.assertEqual(len(detail["findings"][0]["msg"]), health.MAX_MSG)

    def test_history_is_bounded_per_stage_and_per_source(self):
        for i in range(40):
            health.record("x-bos", "refresh", f"v{i}", path=self.path,
                          now=f"2026-09-01T00:00:{i:02d}")
        events = health.events_for(health.load(self.path), "x-bos")
        self.assertEqual(len(events), health.KEEP_PER_STAGE)
        self.assertEqual(events[-1]["verdict"], "v39")

    def test_a_corrupt_ledger_reads_empty_and_recording_still_works(self):
        self.path.write_text("{not json")
        self.assertEqual(health.load(self.path)["sources"], {})
        health.record("x-bos", "refresh", "ok", path=self.path,
                      now="2026-09-01T07:30:00")
        self.assertEqual(
            len(health.events_for(health.load(self.path), "x-bos")), 1)

    def test_record_never_raises_on_an_unwritable_path(self):
        bad = pathlib.Path(self.dir.name) / "nope.json" / "health.json"
        health.record("x-bos", "refresh", "ok", path=bad)  # must not raise

    def test_cycle_summary_round_trips(self):
        health.record_cycle({"x-bos": "ok"}, {"upcoming.py": "done"},
                            path=self.path, now="2026-09-01T07:30:00")
        last = health.load(self.path)["last_run"]
        self.assertEqual(last["results"], {"x-bos": "ok"})
        self.assertEqual(last["enrich"], {"upcoming.py": "done"})
        self.assertIn(last["host"], ("ci", "local"))

    def test_ledger_stays_under_the_size_cap(self):
        for source in range(12):
            for i in range(30):
                health.record(f"s{source}-bos", "refresh", f"v{i}",
                              {"findings": [{"check": "c", "ref": "r",
                                             "msg": "y" * 200}] * 10},
                              path=self.path, now=f"2026-09-01T00:00:{i:02d}")
        self.assertLessEqual(len(json.dumps(health.load(self.path))),
                             health.MAX_BYTES)


if __name__ == "__main__":
    unittest.main()
