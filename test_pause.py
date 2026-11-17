# -*- coding: utf-8 -*-
"""Pause / resume of scrape jobs, and a manual scan that pauses a running
whole-market scan and lets it carry on from the same point afterwards.

Run with:  python -m unittest -v test_pause
"""
import json
import threading
import time
import unittest
from unittest import mock

import test_competitor as tc  # sets the test DB env before db is imported
import background as bg
import scrape_manager as sm
from db import db
from engine import Ad, ScrapeResult


def _market_job():
    return db.create_job(query="🔍 كل إعلانات السوق", source="manual", mode=sm.MONTH_MODE,
                         start_date_min="2026-11-01", start_date_max="2026-11-04", languages="ar",
                         stores_only=True, exact_phrase=True, date_scope="launch")


class QueueTest(unittest.TestCase):
    def setUp(self):
        tc._clean()
        db._execute("DELETE FROM jobs")

    def test_manual_single_scan_has_priority_and_is_claimed_first(self):
        market = _market_job()
        status = db.create_job(query="فحص", source="schedule", mode=sm.STATUS_MODE)
        kw = db.create_job(query="ساعة", source="manual")
        self.assertEqual(db.get_job(market)["priority"], 1)
        self.assertEqual(db.get_job(status)["priority"], 2)
        self.assertEqual(db.get_job(kw)["priority"], 3)
        self.assertTrue(db.has_priority_pending())
        self.assertEqual([db.claim_next_job("w")["id"] for _ in range(3)], [kw, status, market])

    def test_pause_queued_job_then_resume(self):
        jid = db.create_job(query="ساعة", source="schedule")
        self.assertEqual(db.request_pause(jid), "paused")
        self.assertIsNone(db.claim_next_job("w"))  # parked jobs are not claimed
        self.assertTrue(db.resume_job(jid))
        self.assertEqual(db.claim_next_job("w")["id"], jid)
        self.assertFalse(db.resume_job(jid))  # running, not paused

    def test_pause_running_job_sets_the_flag(self):
        jid = db.create_job(query="ساعة", source="schedule")
        db.claim_next_job("w")
        self.assertEqual(db.request_pause(jid), "pausing")
        self.assertEqual(db.get_job(jid)["pause_requested"], "manual")

    def test_stop_all_keeps_a_market_scan_paused_for_a_priority_scan(self):
        market = _market_job()
        db.claim_next_job("w")
        db.pause_job(market, "auto", '{"v": 2, "done": [1]}', result_count=50)
        other = db.create_job(query="ساعة", source="manual")
        self.assertEqual(db.cancel_pending_jobs(source="manual"), 1)
        self.assertEqual(db.get_job(other)["status"], "stopped")
        self.assertEqual(db.get_job(market)["status"], "pending")

    def test_a_paused_market_scan_still_counts_as_open(self):
        market = _market_job()
        db.request_pause(market)
        self.assertTrue(db.has_open_mode_job(sm.MONTH_MODE))  # the schedule won't queue another


class PreemptTest(unittest.TestCase):
    """A manual keyword scan arrives while the market scan runs."""

    def setUp(self):
        tc._clean()
        db._execute("DELETE FROM jobs")
        db.set_setting("meta_ads_proxy", "work:3128")
        db.set_setting("scrape_lanes", json.dumps([{"name": "A", "source": "scrape"}]))
        self.days = []
        self.release = threading.Event()
        for p in [mock.patch.object(bg, "JOB_CONTROL_CHECK_SEC", 0.05),
                  mock.patch.object(sm, "scrape", side_effect=self.fake_scrape)]:
            p.start()
            self.addCleanup(p.stop)
        bg.manager.state["running"] = False

    def fake_scrape(self, query, **kw):
        day = kw.get("start_date_min")
        self.days.append((day, kw.get("resume_cursor")))
        kw["on_ad"](Ad(ad_id=f"ad-{day}", page_name="P", page_id="9", start_date=day, is_active=True,
                       link_url=f"https://s.youcan.shop/products/{day}", store_domain="s.youcan.shop",
                       body=f"عرض يوم {day}", cta_text="ORDER_NOW"))
        if day == "2026-11-03":  # the second day blocks until stopped, half way
            kw["on_checkpoint"]("cur-half")
            for _ in range(200):
                if kw["should_stop"]():
                    return ScrapeResult(stopped=True, scanned=3)
                time.sleep(0.02)
        kw["on_checkpoint"](None)
        return ScrapeResult(completed=True, scanned=5)

    def _run(self, job):
        t = threading.Thread(target=bg.run_claimed_job, args=(job,), daemon=True)
        t.start()
        return t

    def test_market_pauses_for_the_manual_scan_then_resumes_where_it_stopped(self):
        market = _market_job()
        t = self._run(db.claim_next_job("w"))
        while ("2026-11-03", None) not in self.days:
            time.sleep(0.02)
        kw = db.create_job(query="ساعة", source="manual")
        t.join(10)
        self.assertFalse(t.is_alive())

        j = db.get_job(market)
        self.assertEqual((j["status"], j["paused_reason"]), ("pending", "auto"))
        cp = json.loads(j["checkpoint"])
        self.assertEqual(cp["done"], [1])                 # 2026-11-04 finished
        self.assertEqual(cp["cursors"], {"2": "cur-half"})  # 2026-11-03 half way
        self.assertEqual(int(j["attempts"] or 0), 0)       # a pause is not a failed try

        nxt = db.claim_next_job("w")
        self.assertEqual(nxt["id"], kw)                    # the manual scan runs first
        self.assertEqual(nxt["query"], "ساعة")
        db.finish_job(kw, status="done")

        back = db.claim_next_job("w")
        self.assertEqual(back["id"], market)
        self.assertEqual(back["paused_reason"], "")
        self.days.clear()
        t = self._run(back)
        t.join(10)
        self.assertEqual(self.days[0], ("2026-11-03", "cur-half"))  # same point
        self.assertNotIn("2026-11-04", [d for d, _ in self.days])  # not fetched twice
        j = db.get_job(market)
        self.assertEqual(j["status"], "done")
        self.assertEqual(j["slices_ok"], 4)

    def test_manual_pause_waits_for_resume(self):
        market = _market_job()
        t = self._run(db.claim_next_job("w"))
        while ("2026-11-03", None) not in self.days:
            time.sleep(0.02)
        self.assertEqual(db.request_pause(market), "pausing")
        t.join(10)
        j = db.get_job(market)
        self.assertEqual((j["status"], j["paused_reason"]), ("paused", "manual"))
        self.assertIsNone(db.claim_next_job("w"))
        self.assertTrue(db.resume_job(market))
        self.assertEqual(db.claim_next_job("w")["id"], market)

    def test_stop_during_a_pause_request_wins(self):
        market = _market_job()
        t = self._run(db.claim_next_job("w"))
        while ("2026-11-03", None) not in self.days:
            time.sleep(0.02)
        db.request_pause(market)
        db.request_stop(market)
        t.join(10)
        self.assertEqual(db.get_job(market)["status"], "stopped")


class PauseApiTest(unittest.TestCase):
    def test_pause_resume_and_stop_a_paused_job(self):
        import app as app_module
        from werkzeug.security import generate_password_hash
        tc._clean()
        db._execute("DELETE FROM jobs")
        # A worker thread started by another test module must not run the job.
        p = mock.patch.object(db, "claim_next_job", return_value=None)
        p.start()
        self.addCleanup(p.stop)
        db.create_user("admin@test.dz", generate_password_hash("pass-12345"), role="admin")
        c = app_module.app.test_client()
        c.post("/api/auth/login", json={"email": "admin@test.dz", "password": "pass-12345"})
        jid = db.create_job(query="ساعة", source="schedule")
        self.assertEqual(c.post(f"/api/jobs/{jid}/pause").get_json()["status"], "paused")
        self.assertEqual(db.get_job(jid)["status"], "paused")
        self.assertTrue(c.post(f"/api/jobs/{jid}/resume").get_json()["ok"])
        self.assertEqual(db.get_job(jid)["status"], "pending")
        self.assertEqual(c.post(f"/api/jobs/{jid}/resume").status_code, 409)
        c.post(f"/api/jobs/{jid}/pause")
        c.post(f"/api/jobs/{jid}/stop")
        self.assertEqual(db.get_job(jid)["status"], "stopped")  # closed at once, nothing runs it
        self.assertEqual(c.post("/api/pause").status_code, 409)  # nothing running


if __name__ == "__main__":
    unittest.main()
