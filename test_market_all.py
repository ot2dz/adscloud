# -*- coding: utf-8 -*-
"""Whole market with no date: from today back until the active ads run out.

Run with:  python -m unittest -v test_market_all
"""
import json
import threading
import unittest
from unittest import mock

import test_competitor as tc  # sets the test DB env before db is imported
import scrape_manager as sm
from db import db
from engine import Ad, ScrapeResult


def _run(mgr, start="2026-10-01", end="2026-11-10", resume=None):
    mgr.state["running"] = False
    mgr.start(query="🔍 كل إعلانات السوق", mode=sm.MONTH_MODE, start_date_min=start, start_date_max=end,
              languages=["ar"], resume=resume, on_checkpoint=lambda cp: None, job_id=None,
              date_scope=sm.OPEN_SCOPE)
    mgr.thread.join(20)
    return mgr.state["outcome"]


class OpenEndedScanTest(unittest.TestCase):
    """Ads exist from 2026-11-01 on; every older day is empty."""

    def setUp(self):
        tc._clean()
        db.set_setting("meta_ads_proxy", "work:3128")
        db.set_setting("scrape_lanes", json.dumps([{"name": "A", "source": "scrape"}]))
        self.days, self.canaries = [], []
        self.lock = threading.Lock()
        p = mock.patch.object(sm, "MARKET_ALL_EMPTY_DAYS", 3)
        p.start()
        self.addCleanup(p.stop)

    def fake(self, canary_sees_ads=True):
        def scrape(query, **kw):
            day = kw["start_date_min"]
            canary = "on_checkpoint" not in kw
            with self.lock:
                (self.canaries if canary else self.days).append(day)
            busy = day >= "2026-11-01" and (canary_sees_ads or not canary)
            for n in range(5 if busy else 0):
                if kw["should_stop"]():
                    break
                kw["on_ad"](Ad(ad_id=f"ad-{day}-{n}", page_name="P", page_id="9", start_date=day, is_active=True,
                               link_url=f"https://s{n}.youcan.shop/products/{day}", store_domain=f"s{n}.youcan.shop",
                               body=f"عرض يوم {day}", cta_text="ORDER_NOW"))
            if not canary:
                kw["on_checkpoint"](None)
            return ScrapeResult(completed=True, scanned=10 if busy else 0)
        return scrape

    def test_stops_by_itself_when_the_ads_run_out(self):
        with mock.patch.object(sm, "scrape", side_effect=self.fake()):
            out = _run(sm.ScrapeManager())
        # 10 busy days (11-10 … 11-01) + 3 quiet ones, then it stops
        self.assertEqual(self.days[0], "2026-11-10")
        self.assertEqual(self.days[-1], "2026-10-29")
        self.assertEqual(len(self.days), 13)
        self.assertEqual(len(self.canaries), 1)
        self.assertGreaterEqual(self.canaries[0], "2026-11-01")  # checked on a day that had ads
        self.assertEqual(out["status"], "done")
        self.assertTrue(out["completed"])
        self.assertEqual(out["auto_stop_day"], "2026-10-29")
        self.assertEqual(out["slices_total"], 13)
        self.assertEqual(db._query_one("SELECT COUNT(*) AS n FROM meta_ads")["n"], 50)

    def test_a_soft_block_does_not_end_the_scan(self):
        with mock.patch.object(sm, "scrape", side_effect=self.fake(canary_sees_ads=False)):
            out = _run(sm.ScrapeManager())
        self.assertEqual(out["status"], "interrupted")  # the queue retries it later
        self.assertNotIn("auto_stop_day", out)
        self.assertEqual(out["checkpoint"]["done"], list(range(1, 11)))  # quiet days not trusted
        self.assertEqual(set(out["checkpoint"]["yield"]), {str(i) for i in range(1, 11)})

    def test_resume_keeps_the_day_counts(self):
        # 11-10 … 11-01 done earlier with ads, 10-31 and 10-30 quiet: one more quiet day ends it
        resume = {"v": 2, "done": list(range(1, 13)), "cursors": {}, "ok": 12,
                  "yield": {**{str(i): 5 for i in range(1, 11)}, "11": 0, "12": 0}}
        with mock.patch.object(sm, "scrape", side_effect=self.fake()):
            out = _run(sm.ScrapeManager(), resume=resume)
        self.assertEqual(self.days, ["2026-10-29"])
        self.assertEqual(out["auto_stop_day"], "2026-10-29")

    def test_dated_scan_is_unchanged(self):
        mgr = sm.ScrapeManager()
        mgr.state["running"] = False
        with mock.patch.object(sm, "scrape", side_effect=self.fake()):
            mgr.start(query="🔍 كل إعلانات السوق", mode=sm.MONTH_MODE, start_date_min="2026-10-25",
                      start_date_max="2026-10-31", languages=["ar"], on_checkpoint=lambda cp: None)
            mgr.thread.join(20)
        self.assertEqual(len(self.days), 7)  # every day, quiet or not
        self.assertEqual(self.canaries, [])
        self.assertNotIn("yield", mgr.state["outcome"]["checkpoint"])


class OpenEndedApiTest(unittest.TestCase):
    def test_start_queues_an_open_ended_job(self):
        import app as app_module
        from datetime import date, timedelta
        from werkzeug.security import generate_password_hash
        tc._clean()
        db._execute("DELETE FROM jobs")
        p = mock.patch.object(db, "claim_next_job", return_value=None)  # nothing runs it
        p.start()
        self.addCleanup(p.stop)
        db.create_user("admin@test.dz", generate_password_hash("pass-12345"), role="admin")
        c = app_module.app.test_client()
        c.post("/api/auth/login", json={"email": "admin@test.dz", "password": "pass-12345"})
        d = c.post("/api/start", json={"mode": "month_days", "date_preset": "all", "open_ended": True}).get_json()
        self.assertTrue(d["ok"])
        self.assertTrue(d["open_ended"])
        job = db.get_job(d["job_id"])
        self.assertEqual((job["mode"], job["date_scope"]), (sm.MONTH_MODE, sm.OPEN_SCOPE))
        span = date.fromisoformat(job["start_date_max"]) - date.fromisoformat(job["start_date_min"])
        self.assertEqual(span, timedelta(days=sm.MARKET_ALL_MAX_DAYS - 1))
        # without the flag a market scan still needs a window (this month by default)
        d = c.post("/api/start", json={"mode": "month_days", "date_preset": "all"}).get_json()
        self.assertEqual(db.get_job(d["job_id"])["date_scope"], "launch")


if __name__ == "__main__":
    unittest.main()
