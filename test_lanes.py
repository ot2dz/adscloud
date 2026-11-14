# -*- coding: utf-8 -*-
"""Parallel scrape lanes for the whole-market scan.

Run with:  python -m unittest -v test_lanes
"""
import json
import threading
import time
import unittest
from unittest import mock

import test_competitor as tc  # sets the test DB env before db is imported
import scrape_manager as sm
from db import db
from engine import Ad, ScrapeResult


def _run_month(mgr, start="2026-11-01", end="2026-11-06", resume=None):
    mgr.state["running"] = False
    mgr.start(query="🔍 كل إعلانات السوق", mode=sm.MONTH_MODE, start_date_min=start, start_date_max=end,
              languages=["ar"], resume=resume, on_checkpoint=lambda cp: None, job_id=None)
    mgr.thread.join(20)
    return mgr.state["outcome"]


class LaneConfigTest(unittest.TestCase):
    def test_default_is_one_lane_on_the_scrape_proxy(self):
        lanes = sm.scrape_lanes({"meta_ads_proxy": "1.1.1.1:8080"})
        self.assertEqual([(l["source"], l["proxy"]) for l in lanes], [("scrape", "1.1.1.1:8080")])

    def test_sources_dedupe_and_status_needs_its_own_line(self):
        cfg = {"meta_ads_proxy": "work:3128", "status_check_proxy": "tablet:8080",
               "status_check_rotate_url": "http://tablet:8787/rotate",
               "scrape_lanes": json.dumps([
                   {"name": "العمل", "source": "scrape"},
                   {"name": "المحل", "source": "status"},
                   {"name": "إضافي", "source": "custom", "proxy": "extra:1080", "rotate_url": ""},
                   {"name": "مكرر", "source": "custom", "proxy": "work:3128"},
                   {"name": "معطل", "source": "custom", "proxy": "off:1", "enabled": False}])}
        lanes = sm.scrape_lanes(cfg)
        self.assertEqual([l["name"] for l in lanes], ["العمل", "المحل", "إضافي"])
        self.assertEqual(lanes[1]["rotate_url"], "http://tablet:8787/rotate")
        self.assertEqual(lanes[2]["rotate_url"], "")  # no rotator: never rotates another line
        no_status = dict(cfg, status_check_proxy="")
        self.assertNotIn("المحل", [l["name"] for l in sm.scrape_lanes(no_status)])

    def test_resume_formats(self):
        self.assertEqual(sm._month_resume({"pass": 3, "cursor": "c3", "ok": 2}), ({1, 2}, {3: "c3"}, 2))
        self.assertEqual(sm._month_resume({"v": 2, "done": [1, 4], "cursors": {"2": "x"}, "ok": 1}),
                         ({1, 4}, {2: "x"}, 1))


class ParallelScanTest(unittest.TestCase):
    def setUp(self):
        tc._clean()
        db.set_setting("meta_ads_proxy", "work:3128")
        db.set_setting("status_check_proxy", "tablet:8080")
        db.set_setting("scrape_lanes", json.dumps([{"name": "A", "source": "scrape"},
                                                   {"name": "B", "source": "status"}]))
        self.calls = []
        self.lock = threading.Lock()

    def fake_scrape(self, blocked_proxy=None):
        def scrape(query, **kw):
            day, proxy = kw["start_date_min"], kw["proxy"]
            with self.lock:
                self.calls.append((day, proxy, kw.get("resume_cursor"), id(kw.get("pace"))))
            time.sleep(0.02)
            if proxy == blocked_proxy:
                return ScrapeResult(interrupted=True, error="blocked", scanned=0)
            ad = Ad(ad_id=f"ad-{day}", page_name="P", page_id="9", start_date=day, is_active=True,
                    link_url=f"https://s.youcan.shop/products/{day}", store_domain="s.youcan.shop",
                    body=f"عرض يوم {day}", cta_text="ORDER_NOW")
            kw["on_ad"](ad)
            kw["on_checkpoint"](None)
            return ScrapeResult(completed=True, scanned=10)
        return scrape

    def test_two_lanes_share_the_days(self):
        with mock.patch.object(sm, "scrape", side_effect=self.fake_scrape()):
            out = _run_month(sm.ScrapeManager())
        days = sorted(c[0] for c in self.calls)
        self.assertEqual(days, [f"2026-11-0{i}" for i in range(1, 7)])  # each day once
        self.assertEqual({c[1] for c in self.calls}, {"work:3128", "tablet:8080"})  # both lanes worked
        self.assertEqual(len({c[3] for c in self.calls}), 2)  # each lane its own pace
        self.assertEqual(out["status"], "done")
        self.assertTrue(out["completed"])
        self.assertEqual(out["checkpoint"]["done"], [1, 2, 3, 4, 5, 6])
        self.assertEqual(len(db._query("SELECT ad_id FROM meta_ads")), 6)

    def test_blocked_lane_hands_its_days_to_the_others(self):
        with mock.patch.object(sm, "scrape", side_effect=self.fake_scrape(blocked_proxy="tablet:8080")):
            out = _run_month(sm.ScrapeManager())
        ok_days = sorted(c[0] for c in self.calls if c[1] == "work:3128")
        self.assertEqual(ok_days, [f"2026-11-0{i}" for i in range(1, 7)])  # lane A did them all
        self.assertEqual(out["status"], "done")
        self.assertTrue(out["completed"])

    def test_all_lanes_blocked_keeps_the_job_for_a_retry(self):
        db.set_setting("scrape_lanes", json.dumps([{"name": "A", "source": "scrape"}]))
        with mock.patch.object(sm, "scrape", side_effect=self.fake_scrape(blocked_proxy="work:3128")):
            out = _run_month(sm.ScrapeManager())
        self.assertEqual(out["status"], "interrupted")
        self.assertEqual(out["checkpoint"]["done"], [])

    def test_resume_skips_done_days_and_continues_the_cursor(self):
        resume = {"v": 2, "done": [1, 2, 3], "cursors": {"4": "cur-4"}, "ok": 3}
        with mock.patch.object(sm, "scrape", side_effect=self.fake_scrape()):
            out = _run_month(sm.ScrapeManager(), resume=resume)
        days = {c[0]: c[2] for c in self.calls}
        # passes are newest first: 1=06, 2=05, 3=04 done; 4=03 resumes its cursor
        self.assertEqual(sorted(days), ["2026-11-01", "2026-11-02", "2026-11-03"])
        self.assertEqual(days["2026-11-03"], "cur-4")
        self.assertEqual(out["slices_ok"], 6)


class LanesApiTest(unittest.TestCase):
    def test_save_and_hide_proxy(self):
        import app as app_module
        from werkzeug.security import generate_password_hash
        tc._clean()
        db.create_user("admin@test.dz", generate_password_hash("pass-12345"), role="admin")
        c = app_module.app.test_client()
        c.post("/api/auth/login", json={"email": "admin@test.dz", "password": "pass-12345"})
        r = c.post("/api/scrape-lanes", json={"lanes": [
            {"name": "A", "source": "scrape"},
            {"name": "X", "source": "custom", "proxy": "h:1:user:SECRETPASS", "rotate_url": "http://r/rotate"}]})
        self.assertTrue(r.get_json()["ok"])
        d = c.get("/api/scrape-lanes").get_json()
        self.assertNotIn("SECRETPASS", json.dumps(d))
        self.assertEqual(d["lanes"][1]["proxy_hint"], "h:1")
        # saving again without retyping the hidden proxy keeps it
        lanes = d["lanes"]
        lanes[1]["keep_index"] = 1
        self.assertTrue(c.post("/api/scrape-lanes", json={"lanes": lanes}).get_json()["ok"])
        self.assertIn("SECRETPASS", db.get_settings()["scrape_lanes"])
        bad = c.post("/api/scrape-lanes", json={"lanes": [{"source": "custom", "proxy": "h:1", "rotate_url": "ftp://x"}]})
        self.assertEqual(bad.status_code, 400)


if __name__ == "__main__":
    unittest.main()
