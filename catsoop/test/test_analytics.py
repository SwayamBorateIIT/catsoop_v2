"""
Tests for the learning-analytics pipeline.

These follow the "ground-truth validation" approach: build synthetic student
behaviour whose correct analytics output is known in advance, run it through
the real pipeline, and assert the computed metrics match what the scripted
behaviour must produce.
"""

import os
import shutil
import tempfile
import unittest

from .. import base_context


class AnalyticsTestBase(unittest.TestCase):
    """Runs each test against a throwaway data root."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="catsoop-analytics-test-")
        self._old_root = base_context.cs_data_root
        base_context.cs_data_root = self.tmp
        os.makedirs(os.path.join(self.tmp, "_logs", "_courses"), exist_ok=True)

    def tearDown(self):
        base_context.cs_data_root = self._old_root
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write_actions(self, course, user, path, records):
        """Append records to a user's problemactions log, as CAT-SOOP would."""
        from .. import cslog

        for rec in records:
            cslog.update_log(user, [course] + list(path), "problemactions", rec)

    def write_state(self, course, user, path, state):
        from .. import cslog

        cslog.overwrite_log(user, [course] + list(path), "problemstate", state)


class TestNormalize(AnalyticsTestBase):
    def test_score_normalization(self):
        from ..analytics import normalize

        # Pass-fail question types record booleans; partial credit records floats.
        self.assertEqual(normalize.normalize_score(True), 1.0)
        self.assertEqual(normalize.normalize_score(False), 0.0)
        self.assertEqual(normalize.normalize_score(0.5), 0.5)
        # Out-of-range scores are clamped rather than propagated.
        self.assertEqual(normalize.normalize_score(1.7), 1.0)
        self.assertEqual(normalize.normalize_score(-2), 0.0)
        # "Not yet graded" must stay distinct from zero.
        self.assertIsNone(normalize.normalize_score(None))
        self.assertIsNone(normalize.normalize_score("nope"))

    def test_timestamp_parsing(self):
        from ..analytics import normalize

        ts = normalize.parse_timestamp("2026-09-18:22:57:33.706415")
        self.assertIsNotNone(ts)
        self.assertAlmostEqual(
            normalize.parse_timestamp("2026-09-18:22:57:34.706415") - ts, 1.0, places=3
        )
        self.assertIsNone(normalize.parse_timestamp("not a date"))
        self.assertIsNone(normalize.parse_timestamp(None))

    def test_impersonation_detected(self):
        from ..analytics import normalize

        info = {"username": "alice", "real_user": {"username": "prof"}}
        self.assertEqual(normalize.real_submitter(info), "prof")
        self.assertIsNone(normalize.real_submitter({"username": "alice"}))

        events, anomalies = normalize.normalize_actions(
            "c", "alice", ("c", "hw1"),
            [{
                "action": "submit", "timestamp": "2026-09-18:10:00:00.000000",
                "names": ["q1"], "scores": {"q1": True}, "user_info": info,
            }],
        )
        self.assertEqual(anomalies, [])
        self.assertEqual(len(events), 1)
        self.assertTrue(events[0].impersonated)

    def test_malformed_records_are_flagged_not_dropped(self):
        from ..analytics import normalize

        events, anomalies = normalize.normalize_actions(
            "c", "alice", ("c", "hw1"),
            [
                {"action": "submit", "timestamp": "garbage", "names": ["q1"]},
                {"action": "submit", "timestamp": "2026-09-18:10:00:00.000000"},
                {"__malformed__": True},
                {"timestamp": "2026-09-18:10:00:00.000000"},
            ],
        )
        self.assertEqual(events, [])
        kinds = sorted(a.kind for a in anomalies)
        self.assertEqual(
            kinds,
            ["attempt_without_question", "bad_timestamp", "missing_action",
             "unreadable_record"],
        )

    def test_staff_bookkeeping_is_not_an_event(self):
        from ..analytics import normalize

        events, anomalies = normalize.normalize_actions(
            "c", "alice", ("c", "hw1"),
            [{"action": a, "timestamp": "2026-09-18:10:00:00.000000"}
             for a in ("lock", "unlock", "grade", "revert")],
        )
        self.assertEqual(events, [])
        self.assertEqual(anomalies, [])


class TestIncrementalExtraction(AnalyticsTestBase):
    def test_resume_from_offset(self):
        from ..analytics import extract

        recs = [
            {"action": "submit", "timestamp": "2026-09-18:10:0%d:00.000000" % i,
             "names": ["q1"], "scores": {"q1": True}}
            for i in range(3)
        ]
        self.write_actions("c", "alice", ["hw1"], recs)

        first, offset, reset = extract.read_actions_since("alice", ["c", "hw1"], 0)
        self.assertEqual(len(first), 3)
        self.assertFalse(reset)
        self.assertGreater(offset, 0)

        # Nothing new: a second read at the stored offset returns nothing.
        again, offset2, _ = extract.read_actions_since("alice", ["c", "hw1"], offset)
        self.assertEqual(again, [])
        self.assertEqual(offset2, offset)

        # One more append is picked up on its own, not re-read from the start.
        self.write_actions("c", "alice", ["hw1"], [
            {"action": "submit", "timestamp": "2026-09-18:11:00:00.000000",
             "names": ["q2"], "scores": {"q2": False}}
        ])
        more, offset3, _ = extract.read_actions_since("alice", ["c", "hw1"], offset)
        self.assertEqual(len(more), 1)
        self.assertEqual(more[0]["names"], ["q2"])
        self.assertGreater(offset3, offset)

    def test_truncated_log_triggers_reset(self):
        from ..analytics import extract

        self.write_actions("c", "alice", ["hw1"], [
            {"action": "submit", "timestamp": "2026-09-18:10:00:00.000000",
             "names": ["q1"], "scores": {"q1": True}}
        ])
        _, offset, _ = extract.read_actions_since("alice", ["c", "hw1"], 0)
        # An offset beyond the end of the file means the log was rebuilt.
        records, new_offset, reset = extract.read_actions_since(
            "alice", ["c", "hw1"], offset + 10_000
        )
        self.assertTrue(reset)
        self.assertEqual(len(records), 1)
        self.assertEqual(new_offset, offset)


class TestGroundTruth(AnalyticsTestBase):
    """
    Four personas with deliberately different, known behaviour.  Every
    assertion below is something the scripted behaviour makes true by
    construction, so a failure means the pipeline is wrong, not the data.
    """

    COURSE = "gt"
    PATH = ["hw1"]

    def _submit(self, user, qname, score, when, impersonator=None):
        info = {"username": user}
        if impersonator:
            info["real_user"] = {"username": impersonator}
        return {
            "action": "submit",
            "timestamp": when,
            "names": [qname],
            "scores": {qname: score},
            "user_info": info,
        }

    def _seed(self):
        from .. import cslog

        # ace: both questions right, one attempt each.
        self.write_actions(self.COURSE, "ace", self.PATH, [
            self._submit("ace", "q1", True, "2026-09-18:10:00:00.000000"),
            self._submit("ace", "q2", True, "2026-09-18:10:01:00.000000"),
        ])
        self.write_state(self.COURSE, "ace", self.PATH, {
            "scores": {"q1": True, "q2": True},
            "nsubmits_used": {"q1": 1, "q2": 1},
            "last_submit_times": {
                "q1": "2026-09-18:10:00:00.000000",
                "q2": "2026-09-18:10:01:00.000000",
            },
        })

        # grinder: gets there, but needs many attempts on q2.
        self.write_actions(self.COURSE, "grinder", self.PATH, [
            self._submit("grinder", "q1", True, "2026-09-18:10:00:00.000000"),
            self._submit("grinder", "q2", False, "2026-09-18:10:02:00.000000"),
            self._submit("grinder", "q2", False, "2026-09-18:10:04:00.000000"),
            self._submit("grinder", "q2", True, "2026-09-18:10:06:00.000000"),
        ])
        self.write_state(self.COURSE, "grinder", self.PATH, {
            "scores": {"q1": True, "q2": True},
            "nsubmits_used": {"q1": 1, "q2": 3},
            "last_submit_times": {
                "q1": "2026-09-18:10:00:00.000000",
                "q2": "2026-09-18:10:06:00.000000",
            },
        })

        # struggler: attempts q2 repeatedly and never gets it.
        self.write_actions(self.COURSE, "struggler", self.PATH, [
            self._submit("struggler", "q2", False, "2026-09-18:10:0%d:00.000000" % i)
            for i in range(1, 5)
        ])
        self.write_state(self.COURSE, "struggler", self.PATH, {
            "scores": {"q2": False},
            "nsubmits_used": {"q2": 4},
            "last_submit_times": {"q2": "2026-09-18:10:04:00.000000"},
        })

        # ghost: on the roster, never touches anything.  No logs at all.

        # The instructor tests q1 while impersonating "ghost".  This must not
        # make ghost look active.
        self.write_actions(self.COURSE, "ghost", self.PATH, [
            self._submit("ghost", "q1", True, "2026-09-18:12:00:00.000000",
                         impersonator="prof"),
        ])

        # Roster and course structure.
        users_dir = os.path.join(self.tmp, "courses", self.COURSE, "__USERS__")
        os.makedirs(users_dir, exist_ok=True)
        for u, role in [("ace", "Student"), ("grinder", "Student"),
                        ("struggler", "Student"), ("ghost", "Student"),
                        ("prof", "Admin"), ("visitor", "Guest")]:
            with open(os.path.join(users_dir, "%s.py" % u), "w") as f:
                f.write("name = %r\nemail = %r\nrole = %r\n"
                        % (u.title(), "%s@example.edu" % u, role))

        cslog.overwrite_log(
            "_question_info", [self.COURSE] + self.PATH, "question_info",
            {
                "timestamp": "1789772600.0",
                "questions": repr({
                    "q1": {"qtype": "number", "csq_npoints": 1,
                           "csq_display_name": "q1"},
                    "q2": {"qtype": "expression", "csq_npoints": 1,
                           "csq_display_name": "q2"},
                }),
            },
        )

    def setUp(self):
        super().setUp()
        self._seed()
        from ..analytics import store, sync

        self.report = sync.sync_course(self.COURSE)
        self.assertIsNone(self.report.error, self.report.error)
        self.conn = store.connect()

    def tearDown(self):
        self.conn.close()
        super().tearDown()

    def test_cohort_excludes_staff_and_guests(self):
        from ..analytics import engine

        cohort = engine.students(self.conn, self.COURSE)
        self.assertEqual(
            sorted(cohort), ["ace", "ghost", "grinder", "struggler"]
        )

    def test_overview_matches_scripted_behaviour(self):
        from ..analytics import engine

        m = engine.course_overview(self.conn, self.COURSE)
        self.assertEqual(m["total_students"], 4)
        self.assertEqual(m["total_exercises"], 2)
        # ace 2 + grinder 2 = 4 mastered of 4 students x 2 questions = 8.
        self.assertEqual(m["mastered_pairs"], 4)
        self.assertAlmostEqual(m["completion"], 4 / 8)
        # Attempts: ace 1+1, grinder 1+3, struggler 4 = 10.
        self.assertEqual(m["total_attempts"], 10)

    def test_impersonated_submission_does_not_make_ghost_active(self):
        from ..analytics import engine

        rows = {r["username"]: r for r in engine.student_table(self.conn, self.COURSE)}
        self.assertIsNone(
            rows["ghost"]["last_seen"],
            "a staff test submission must not count as the student's activity",
        )
        self.assertEqual(rows["ghost"]["attempts"], 0)

        # ...but it is still there, and visible when explicitly included.
        incl = {r["username"]: r
                for r in engine.student_table(self.conn, self.COURSE, True)}
        self.assertIsNotNone(incl["ghost"]["last_seen"])

    def test_question_difficulty_ranks_the_hard_one_first(self):
        from ..analytics import engine

        rows = engine.question_difficulty(self.conn, self.COURSE)
        self.assertEqual(rows[0]["qname"], "q2")
        q2 = rows[0]
        q1 = [r for r in rows if r["qname"] == "q1"][0]
        self.assertGreater(q2["difficulty"], q1["difficulty"])
        # q2 scores: ace 1.0, grinder 1.0, struggler 0.0 -> mean 2/3.
        self.assertAlmostEqual(q2["mean_score"], 2 / 3)
        # Only struggler is below mastery.
        self.assertAlmostEqual(q2["fail_rate"], 1 / 3)
        # Attempts on q2: ace 1, grinder 3, struggler 4 -> mean 8/3.
        self.assertAlmostEqual(q2["mean_attempts"], 8 / 3)
        # q1 was answered right first time by everyone who tried it.
        self.assertAlmostEqual(q1["mean_score"], 1.0)
        self.assertAlmostEqual(q1["fail_rate"], 0.0)

    def test_module_progress_counts_only_full_completion(self):
        from ..analytics import engine

        rows = engine.module_progress(self.conn, self.COURSE)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        # ace and grinder have both questions; struggler has neither; ghost none.
        self.assertEqual(row["completed"], 2)
        # Reached = produced a non-impersonated event there.
        self.assertEqual(row["reached"], 3)

    def test_at_risk_separates_never_started_from_struggling(self):
        from ..analytics import engine

        flagged = {r["username"]: r["reasons"]
                   for r in engine.at_risk(self.conn, self.COURSE)}
        self.assertIn("ghost", flagged)
        self.assertIn("never started", flagged["ghost"])
        self.assertIn("struggler", flagged)
        self.assertNotIn("never started", flagged["struggler"])
        self.assertNotIn("ace", flagged)

    def test_sync_is_idempotent(self):
        from ..analytics import engine, sync

        before = engine.course_overview(self.conn, self.COURSE)
        second = sync.sync_course(self.COURSE)
        self.assertIsNone(second.error)
        self.assertEqual(second.records, 0, "a second sync must read no records")
        self.assertEqual(second.logs_read, 0, "unchanged logs must not be opened")
        after = engine.course_overview(self.conn, self.COURSE)
        self.assertEqual(before, after)

    def test_new_activity_is_picked_up_incrementally(self):
        from ..analytics import engine, sync

        self.write_actions(self.COURSE, "struggler", self.PATH, [
            self._submit("struggler", "q2", True, "2026-09-18:13:00:00.000000"),
        ])
        self.write_state(self.COURSE, "struggler", self.PATH, {
            "scores": {"q2": True},
            "nsubmits_used": {"q2": 5},
            "last_submit_times": {"q2": "2026-09-18:13:00:00.000000"},
        })
        report = sync.sync_course(self.COURSE, conn=self.conn)
        self.assertEqual(report.records, 1, "only the new record should be read")
        m = engine.course_overview(self.conn, self.COURSE)
        self.assertEqual(m["mastered_pairs"], 5)
        # The memoized tables must reflect the new record, not the pre-sync state.
        rows = {r["username"]: r
                for r in engine.student_table(self.conn, self.COURSE)}
        self.assertEqual(rows["struggler"]["mastered"], 1)
        self.assertNotIn(
            "struggler",
            {r["username"] for r in engine.at_risk(self.conn, self.COURSE)
             if "never started" in r["reasons"]},
        )


class TestConcurrency(AnalyticsTestBase):
    """
    Two syncs running at once must not double-count.

    This is a regression suite for a real defect: the store had no uniqueness
    constraint and the sync took no lock, so two staff opening the dashboard at
    the same moment each read the same records and each inserted them.  Only
    the event-derived metrics were affected (activity and time on task), since
    score and completion come from `problemstate`, which is overwritten rather
    than appended.
    """

    COURSE = "conc"
    PATH = ["hw1"]

    def _seed(self, n=3):
        from .. import cslog

        for i in range(n):
            cslog.update_log(
                "alice", [self.COURSE] + self.PATH, "problemactions",
                {
                    "action": "submit",
                    "timestamp": "2026-09-18:10:0%d:00.000000" % i,
                    "names": ["q1"],
                    "scores": {"q1": True},
                    "user_info": {"username": "alice"},
                },
            )

    def test_inserting_the_same_events_twice_does_not_duplicate(self):
        """The store itself refuses duplicates, independent of any lock."""
        from ..analytics import extract, normalize, store

        self._seed(3)
        records, _offset, _reset = extract.read_actions_since(
            "alice", [self.COURSE] + self.PATH, 0
        )
        events, _ = normalize.normalize_actions(
            self.COURSE, "alice", [self.COURSE] + self.PATH, records
        )
        self.assertEqual(len(events), 3)

        conn = store.connect()
        try:
            store.insert_events(conn, events)
            store.insert_events(conn, events)   # the overlapping sync
            conn.commit()
            n = conn.execute(
                "SELECT COUNT(*) FROM event WHERE course_id = ?", (self.COURSE,)
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(n, 3, "the same events were counted twice")

    def test_two_overlapping_workers_do_not_duplicate(self):
        """
        The exact failure that was observed: two connections each read the
        cursor before either committed, so both believed they had new records.
        """
        from ..analytics import extract, normalize, store

        self._seed(1)
        records, offset, _ = extract.read_actions_since(
            "alice", [self.COURSE] + self.PATH, 0
        )
        events, _ = normalize.normalize_actions(
            self.COURSE, "alice", [self.COURSE] + self.PATH, records
        )

        c1, c2 = store.connect(), store.connect()
        try:
            store.get_cursors(c1, self.COURSE)      # both read the same
            store.get_cursors(c2, self.COURSE)      # starting position
            for conn in (c1, c2):
                store.insert_events(conn, events)
                store.set_cursor(
                    conn, self.COURSE, "alice", [self.COURSE] + self.PATH,
                    "problemactions", offset, 1.0,
                )
                conn.commit()
            n = c1.execute(
                "SELECT COUNT(*) FROM event WHERE course_id = ?", (self.COURSE,)
            ).fetchone()[0]
        finally:
            c1.close()
            c2.close()
        self.assertEqual(n, 1, "one submission produced %d rows" % n)

    def test_activity_counts_are_not_inflated(self):
        """The metric that the defect actually corrupted."""
        from ..analytics import engine, store, sync

        self._seed(3)
        sync.sync_course(self.COURSE)
        sync.sync_course(self.COURSE)
        conn = store.connect()
        try:
            total = sum(
                d["attempts"]
                for d in engine.activity_series(conn, self.COURSE, 365, True)
            )
        finally:
            conn.close()
        self.assertEqual(total, 3, "activity was counted %d times" % total)

    def test_second_sync_is_skipped_while_the_lock_is_held(self):
        """A caller that cannot get the lock reports it instead of duplicating."""
        from ..analytics import sync

        self._seed(2)
        lock = sync._course_lock(self.COURSE)
        lock.acquire()
        try:
            report = sync.sync_course(self.COURSE, lock_timeout=0.1)
        finally:
            lock.release()

        self.assertTrue(report.skipped, "sync ran while the lock was held")
        self.assertEqual(report.records, 0)
        self.assertIsNone(report.error)
        self.assertIn("skipped", repr(report))

    def test_sync_works_once_the_lock_is_free(self):
        from ..analytics import sync

        self._seed(2)
        lock = sync._course_lock(self.COURSE)
        lock.acquire()
        lock.release()
        report = sync.sync_course(self.COURSE, lock_timeout=1.0)
        self.assertFalse(report.skipped)
        self.assertEqual(report.records, 2)

    def test_existing_duplicates_are_cleaned_up_on_open(self):
        """
        A store written before the fix may already hold duplicates.  Opening it
        removes them, keeping one of each, so the index can be created.
        """
        from ..analytics import store

        conn = store.connect()
        try:
            conn.execute("DROP INDEX IF EXISTS event_natural_key")
            row = (self.COURSE, "alice", "conc/hw1", "q1", "attempt",
                   "submit", 1.0, 1789772600.0, 0)
            for _ in range(3):
                conn.execute(
                    "INSERT INTO event (course_id, username, path, qname, kind,"
                    " action, score, ts, impersonated)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", row,
                )
            conn.commit()
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM event").fetchone()[0], 3
            )
        finally:
            conn.close()

        conn = store.connect()        # reopening runs the cleanup
        try:
            n = conn.execute("SELECT COUNT(*) FROM event").fetchone()[0]
            has_index = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='index'"
                " AND name='event_natural_key'"
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(n, 1, "duplicates were not cleaned up")
        self.assertIsNotNone(has_index, "unique index was not recreated")


class TestAccessControl(AnalyticsTestBase):
    def _ctx(self, perms):
        return {
            "cs_course": "gt",
            "cs_url_root": "http://localhost",
            "cs_path_info": ["gt", "analytics"],
            "cs_form": {},
            "cs_user_info": {"username": "u", "permissions": perms},
        }

    def test_only_admin_permission_passes(self):
        from ..analytics import page

        self.assertTrue(page.is_staff(self._ctx(["admin"])))
        self.assertFalse(page.is_staff(self._ctx(["view", "submit"])))
        self.assertFalse(page.is_staff(self._ctx(["grade", "whdw"])))
        self.assertFalse(page.is_staff(self._ctx([])))

    def test_denied_page_computes_no_metrics(self):
        from ..analytics import page

        html = page.render_dashboard(self._ctx(["view", "submit"]))
        self.assertIn("restricted to course staff", html)
        for leaked in ("Gradebook", "Completion", "needing attention"):
            self.assertNotIn(leaked, html)

    def test_staff_permission_is_configurable(self):
        from ..analytics import page

        ctx = self._ctx(["whdw"])
        ctx["cs_analytics_staff_permission"] = "whdw"
        self.assertTrue(page.is_staff(ctx))


if __name__ == "__main__":
    unittest.main()