# -*- coding: utf-8 -*-
"""Two workers: tablet checks beside the scrapes, and priorities between scrapes.

Run with:  python -m unittest -v test_two_workers
"""
import json
import threading
import time
import unittest
from unittest import mock

import test_competitor as tc  # sets the test DB env before db is imported
import background as bg
import notify
import scrape_manager as sm
from db import db
from engine import Ad, ScrapeResult


def _clean():
    tc._clean()
    db._execute("DELETE FROM jobs")


class PriorityTest(unittest.TestCase):
    def setUp(self):
        _clean()

    def test_levels_and_claim_order(self):
        expand = db.create_job(query="توسيع", source="page_expand", mode=sm.EXPAND_MODE)
        market = db.create_job(query="سوق", source="manual", mode=sm.MONTH_MODE)
        comp = db.create_job(query="منافس", source="schedule", target_id=5)
        kw = db.create_job(query="ساعة", source="manual")
        self.assertEqual([db.get_job(j)["priority"] for j in (kw, comp, market, expand)], [3, 2, 1, 0])
        self.assertEqual([db.claim_next_job("w")["id"] for _ in range(4)], [kw, comp, market, expand])

    def test_workers_split_the_queue(self):
        check = db.create_job(query="فحص", source="daily", mode=sm.STATUS_MODE)
        kw = db.create_job(query="ساعة", source="manual")
        self.assertEqual(db.claim_next_job("scrape", skip_modes=sm.CHECK_MODES)["id"], kw)
        self.assertIsNone(db.claim_next_job("scrape", skip_modes=sm.CHECK_MODES))
        self.assertEqual(db.claim_next_job("tablet", only_modes=sm.CHECK_MODES)["id"], check)

    def test_what_preempts_what(self):
        db.create_job(query="فحص", source="daily", mode=sm.STATUS_MODE)
        self.assertFalse(db.has_priority_pending(above=1, skip_modes=sm.CHECK_MODES))  # checks never
        db.create_job(query="سوق", source="manual", mode=sm.MONTH_MODE)
        self.assertTrue(db.has_priority_pending(above=0, skip_modes=sm.CHECK_MODES))   # market > expansion
        self.assertFalse(db.has_priority_pending(above=1, skip_modes=sm.CHECK_MODES))  # not > market
        db.create_job(query="منافس", source="schedule", target_id=5)
        self.assertTrue(db.has_priority_pending(above=1, skip_modes=sm.CHECK_MODES))   # competitor > market


class PreemptExpansionTest(unittest.TestCase):
    """A manual keyword arrives while page expansion runs."""

    def setUp(self):
        _clean()
        db.set_setting("meta_ads_proxy", "work:3128")
        self.calls = []
        real_claim = db.claim_next_job

        def only_this_test(worker_id, **kw):  # a worker thread from another module must not run the jobs
            return real_claim(worker_id, **kw) if worker_id == "w" else None
        for p in [mock.patch.object(bg, "JOB_CONTROL_CHECK_SEC", 0.05),
                  mock.patch.object(sm, "scrape", side_effect=self.fake),
                  mock.patch.object(db, "claim_next_job", side_effect=only_this_test)]:
            p.start()
            self.addCleanup(p.stop)
        bg.manager.state["running"] = False

    def fake(self, query, **kw):
        self.calls.append(kw.get("url") or query)
        for _ in range(200):  # blocks until stopped
            if kw["should_stop"]():
                return ScrapeResult(stopped=True, scanned=1)
            time.sleep(0.02)
        return ScrapeResult(completed=True, scanned=1)

    def test_expansion_pauses_for_a_manual_scan_and_keeps_its_page(self):
        jid = db.create_job(query="🔁 توسيع الصفحات", source="page_expand", mode=sm.EXPAND_MODE)
        cp = {"pages": [{"id": "111", "name": "A", "meta": 9, "have": 1},
                        {"id": "222", "name": "B", "meta": 8, "have": 1}], "i": 1, "found": 3}
        db.save_checkpoint(jid, json.dumps(cp))
        job = db.claim_next_job("w")
        # its own manager: a thread left by another test module may stop the shared one
        t = threading.Thread(target=bg.run_claimed_job, args=(job, sm.ScrapeManager()), daemon=True)
        t.start()
        while not self.calls:
            time.sleep(0.02)
        kw = db.create_job(query="ساعة", source="manual")
        t.join(10)
        j = db.get_job(jid)
        self.assertEqual((j["status"], j["paused_reason"]), ("pending", "auto"))
        self.assertEqual(json.loads(j["checkpoint"])["i"], 1)  # page B again on resume
        self.assertIn("222", self.calls[0])
        self.assertEqual(db.claim_next_job("w")["id"], kw)


class CheckWorkerTest(unittest.TestCase):
    def setUp(self):
        _clean()
        sm.check_manager.state["running"] = False

    def test_checks_get_their_own_worker_only_with_a_tablet_proxy(self):
        self.assertFalse(sm.checks_have_own_line())
        db.set_setting("status_check_proxy", "tablet:8080")
        self.assertTrue(sm.checks_have_own_line())
        self.assertFalse(sm.tablet_wanted_by_checks())
        db.create_job(query="فحص", source="daily", mode=sm.STATUS_MODE)
        self.assertTrue(sm.tablet_wanted_by_checks())

    def test_check_logs_reach_the_main_log_with_a_prefix(self):
        q = __import__("queue").Queue()
        sm.manager.subscribers.append(q)
        try:
            sm.check_manager._emit("log", {"text": "دفعة 3/10"})
            sm.check_manager._emit("scrape_progress", {"scraped": 5})  # not forwarded
            items = []
            while not q.empty():
                items.append(q.get_nowait())
        finally:
            sm.manager.subscribers.remove(q)
        self.assertEqual([i["type"] for i in items], ["log"])
        self.assertEqual(items[0]["data"]["text"], "[فحص التابلت] دفعة 3/10")

    def test_market_scan_leaves_the_tablet_lane_to_a_waiting_check(self):
        db.set_setting("meta_ads_proxy", "work:3128")
        db.set_setting("status_check_proxy", "tablet:8080")
        db.set_setting("scrape_lanes", json.dumps([{"name": "A", "source": "scrape"},
                                                   {"name": "B", "source": "status"}]))
        db.create_job(query="فحص", source="daily", mode=sm.STATUS_MODE)
        used = []

        def scrape(query, **kw):
            used.append(kw["proxy"])
            self.assertFalse(sm.tablet_lane_busy.is_set())
            day = kw["start_date_min"]
            kw["on_ad"](Ad(ad_id=f"ad-{day}", page_name="P", page_id="9", start_date=day, is_active=True,
                           link_url=f"https://s.youcan.shop/p/{day}", store_domain="s.youcan.shop",
                           body=f"عرض {day}", cta_text="ORDER_NOW"))
            kw["on_checkpoint"](None)
            return ScrapeResult(completed=True, scanned=5)
        mgr = sm.ScrapeManager()
        with mock.patch.object(sm, "scrape", side_effect=scrape):
            mgr.start(query="🔍 كل إعلانات السوق", mode=sm.MONTH_MODE, start_date_min="2026-11-01",
                      start_date_max="2026-11-04", languages=["ar"], on_checkpoint=lambda cp: None)
            mgr.thread.join(20)
        self.assertEqual(set(used), {"work:3128"})  # the tablet stayed free
        self.assertEqual(len(used), 4)
        self.assertEqual(mgr.state["outcome"]["status"], "done")


class WorkLineAlertTest(unittest.TestCase):
    def setUp(self):
        _clean()
        notify._last_alert.clear()

    def test_alert_once_an_hour_and_only_with_a_bot(self):
        with mock.patch.object(notify, "_telegram_send", return_value=True) as send:
            bg._alert_work_line_down(7, "blocked")
            send.assert_not_called()  # no bot configured
            db.set_setting("telegram_token", "t")
            db.set_setting("telegram_chat_id", "c")
            bg._alert_work_line_down(7, "blocked")
            bg._alert_work_line_down(8, "blocked")
        self.assertEqual(send.call_count, 1)
        self.assertIn("خط العمل", send.call_args[0][2])


if __name__ == "__main__":
    unittest.main()
