# -*- coding: utf-8 -*-
"""Each scrape job keeps Meta's estimate, the ads Meta really returned and
whether Meta said "no more pages", so a finished job can be checked.

Run with:  python -m unittest -v test_job_coverage
"""
import os
import tempfile
import unittest
from unittest import mock

_TMP_DIR = tempfile.mkdtemp(prefix="adscloud-coverage-")
os.environ.setdefault("META_ADS_SQLITE", os.path.join(_TMP_DIR, "test.sqlite"))
os.environ["DATABASE_URL"] = ""

import scrape_manager  # noqa: E402
from db.core import Database  # noqa: E402
from engine import ScrapeResult  # noqa: E402


class JobColumnsTest(unittest.TestCase):
    def setUp(self):
        self.db = Database()
        self.db._execute("DELETE FROM jobs")
        self.jid = self.db.create_job(query="التوصيل")

    def test_new_job_has_no_coverage_yet(self):
        j = self.db.get_job(self.jid)
        self.assertIsNone(j["reported_count"])
        self.assertEqual(j["scanned_count"], 0)
        self.assertIsNone(j["completed"])

    def test_reported_is_saved_and_none_is_ignored(self):
        self.db.set_job_reported(self.jid, 27000)
        self.db.set_job_reported(self.jid, None)
        self.assertEqual(self.db.get_job(self.jid)["reported_count"], 27000)

    def test_retry_then_finish_keeps_all_three(self):
        self.db.set_job_reported(self.jid, 5000)
        self.db.schedule_retry(self.jid, 5, '{"pass": 1, "cursor": "c"}', result_count=900,
                               scanned_count=1200)
        j = self.db.get_job(self.jid)
        self.assertEqual((j["scanned_count"], j["result_count"], j["status"]), (1200, 900, "pending"))
        self.db.finish_job(self.jid, status="done", result_count=3100, scanned_count=4870, completed=True)
        j = self.db.get_job(self.jid)
        self.assertEqual((j["reported_count"], j["scanned_count"], j["completed"]), (5000, 4870, 1))

    def test_finish_without_coverage_leaves_it_untouched(self):
        self.db.finish_job(self.jid, status="done", result_count=10, scanned_count=50, completed=False)
        self.db.finish_job(self.jid, status="stopped", result_count=10)
        j = self.db.get_job(self.jid)
        self.assertEqual((j["scanned_count"], j["completed"]), (50, 0))


class ManagerOutcomeTest(unittest.TestCase):
    def setUp(self):
        self.db = Database()
        self.db._execute("DELETE FROM jobs")
        self.jid = self.db.create_job(query="ساعة")
        patches = [mock.patch.object(scrape_manager, "db", self.db),
                   mock.patch.object(scrape_manager, "_proxy_setting", lambda: "")]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def _run(self, result, base_reported=None, report=None):
        def fake_scrape(*a, **k):
            if report is not None:
                k["on_reported"](report)
            return result

        m = scrape_manager.ScrapeManager()
        with mock.patch.object(scrape_manager, "scrape", fake_scrape):
            self.assertTrue(m.start("ساعة", job_id=self.jid, base_reported=base_reported))
            m.thread.join(10)
        return m

    def test_estimate_is_saved_on_the_job_as_soon_as_known(self):
        m = self._run(ScrapeResult(completed=True, scanned=40), report=27000)
        self.assertEqual(self.db.get_job(self.jid)["reported_count"], 27000)
        out = m.state["outcome"]
        self.assertEqual((out["completed"], out["scanned"]), (True, 40))

    def test_resumed_run_keeps_the_first_runs_estimate(self):
        m = self._run(ScrapeResult(completed=False, interrupted=True, scanned=7), base_reported=5000)
        self.assertEqual(m.state["reported"], 5000)
        self.assertFalse(m.state["outcome"]["completed"])


class MarketSlicesTest(unittest.TestCase):
    """Whole-market scan: 3 days x 1 language = 3 slices."""

    def setUp(self):
        patches = [mock.patch.object(scrape_manager, "db", mock.MagicMock()),
                   mock.patch.object(scrape_manager, "_proxy_setting", lambda: "")]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def _run(self, results, resume=None):
        replies = iter(results)

        def fake_scrape(*a, **k):
            r = next(replies)
            if r.completed:
                k["on_checkpoint"](None)  # the engine closes a finished slice
            return r

        m = scrape_manager.ScrapeManager()
        with mock.patch.object(scrape_manager, "scrape", fake_scrape):
            m.start("سوق", mode=scrape_manager.MONTH_MODE, start_date_min="2026-11-01",
                    start_date_max="2026-11-03", languages=["ar"], resume=resume, job_id=1)
            m.thread.join(10)
        return m.state["outcome"]

    def test_all_slices_reach_the_end(self):
        out = self._run([ScrapeResult(completed=True, scanned=10)] * 3)
        self.assertEqual((out["slices_ok"], out["slices_total"], out["completed"]), (3, 3, True))
        self.assertEqual(out["scanned"], 30)
        self.assertEqual(out["checkpoint"], {"pass": 4, "cursor": None, "ok": 3})

    def test_a_failed_slice_is_skipped_and_counted_missing(self):
        out = self._run([ScrapeResult(completed=True, scanned=5),
                         ScrapeResult(failed=True, error="boom", scanned=2),
                         ScrapeResult(completed=True, scanned=5)])
        self.assertEqual((out["slices_ok"], out["completed"]), (2, False))
        self.assertEqual(out["scanned"], 12)

    def test_resume_carries_the_ok_count(self):
        # Slice 1 missed in an earlier run; resume at slice 3.
        out = self._run([ScrapeResult(completed=True, scanned=1)],
                        resume={"pass": 3, "cursor": None, "ok": 1})
        self.assertEqual((out["slices_ok"], out["completed"]), (2, False))

    def test_old_checkpoint_without_ok_assumes_earlier_slices_finished(self):
        out = self._run([ScrapeResult(completed=True, scanned=1)], resume={"pass": 3, "cursor": None})
        self.assertEqual((out["slices_ok"], out["completed"]), (3, True))


if __name__ == "__main__":
    unittest.main()
