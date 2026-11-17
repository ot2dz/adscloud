# -*- coding: utf-8 -*-
"""Page expansion: fetch every active ad of pages we only partly hold.

Run with:  python -m unittest -v test_page_expand
"""
import unittest
from unittest import mock

import test_competitor as tc  # sets the test DB env before db is imported
import background as bg
import page_expand
import scrape_manager as sm
from db import db
from engine import Ad, ScrapeResult


def _setup():
    tc._clean()
    for t in ("entity_stats", "page_expand", "jobs"):
        db._execute(f"DELETE FROM {t}")
    # page 501: 1 ad here, 26 on Meta -> gap 25
    db.save_ad(tc._ad("p1-a", "shop.myecomsite.net", "عرض 1", page_id="501"), query="q", country="DZ")
    # page 502: 5 here, 6 on Meta -> gap 1 (below the minimum)
    for i in range(5):
        db.save_ad(tc._ad(f"p2-{i}", "b.youcan.shop", f"منتج {i}", link_path=f"/p/{i}", page_id="502"),
                   query="q", country="DZ")
    # page 503: 2 here, 9 on Meta -> gap 7
    for i in range(2):
        db.save_ad(tc._ad(f"p3-{i}", "c.youcan.shop", f"سلعة {i}", link_path=f"/c/{i}", page_id="503"),
                   query="q", country="DZ")
    # page 504: no Meta count yet -> never a candidate
    db.save_ad(tc._ad("p4-a", "d.youcan.shop", "شيء", page_id="504"), query="q", country="DZ")
    db.save_entity_stat("page", "501", 26)
    db.save_entity_stat("page", "502", 6)
    db.save_entity_stat("page", "503", 9)


class CandidatesTest(unittest.TestCase):
    def setUp(self):
        _setup()

    def test_biggest_gap_first_and_minimum_gap(self):
        rows = db.page_expand_candidates(limit=10, min_gap=3)
        self.assertEqual([(r["page_id"], int(r["meta_ads"]), int(r["have"])) for r in rows],
                         [("501", 26, 1), ("503", 9, 2)])
        self.assertEqual(db.page_expand_pending(min_gap=3), {"pages": 2, "missing": 32})

    def test_expanded_page_comes_back_only_when_meta_grows_after_the_wait(self):
        db.mark_page_expanded("501", 26, 25)
        self.assertEqual([r["page_id"] for r in db.page_expand_candidates(min_gap=3)], ["503"])
        db.save_entity_stat("page", "501", 40)  # new launches, but expanded just now
        self.assertEqual([r["page_id"] for r in db.page_expand_candidates(min_gap=3)], ["503"])
        db._execute("UPDATE page_expand SET expanded_at = '2000-01-01T00:00:00+00:00' WHERE page_id = '501'")
        self.assertIn("501", [r["page_id"] for r in db.page_expand_candidates(min_gap=3)])
        db.save_entity_stat("page", "501", 26)  # older and not grown: stays out
        self.assertNotIn("501", [r["page_id"] for r in db.page_expand_candidates(min_gap=3)])


class ExpandJobTest(unittest.TestCase):
    def setUp(self):
        _setup()
        self.urls = []

    def fake(self, stop_after=None):
        def scrape(query, **kw):
            url = kw["url"]
            self.urls.append(url)
            pid = "501" if "501" in url else "503"
            for n in range(3):
                kw["on_ad"](Ad(ad_id=f"new-{pid}-{n}", page_name="P", page_id=pid, start_date="2026-11-01",
                               is_active=True, link_url=f"https://x{n}.youcan.shop/p/{pid}",
                               store_domain=f"x{n}.youcan.shop", body=f"إعلان {n}", cta_text="ORDER_NOW"))
            if stop_after and len(self.urls) >= stop_after:
                return ScrapeResult(stopped=True, scanned=3)
            return ScrapeResult(completed=True, scanned=3)
        return scrape

    def _run(self, resume=None):
        mgr = sm.ScrapeManager()
        mgr.state["running"] = False
        cps = []
        mgr.start(query=page_expand.LABEL, mode=sm.EXPAND_MODE, stores_only=True, resume=resume,
                  on_checkpoint=cps.append)
        mgr.thread.join(20)
        return mgr.state["outcome"], cps

    def test_expands_each_page_by_id_and_records_it(self):
        with mock.patch.object(sm, "scrape", side_effect=self.fake()):
            out, cps = self._run()
        self.assertEqual(len(self.urls), 2)
        self.assertIn("view_all_page_id=501", self.urls[0])  # biggest gap first
        self.assertEqual(out["status"], "done")
        self.assertEqual((out["slices_ok"], out["slices_total"]), (2, 2))
        self.assertEqual(db._query_one("SELECT COUNT(*) AS n FROM meta_ads WHERE ad_id LIKE 'new-%'")["n"], 6)
        self.assertEqual({r["page_id"] for r in db._query("SELECT page_id FROM page_expand")}, {"501", "503"})
        self.assertEqual(page_expand.config()["done_today"], 2)
        self.assertEqual(db.page_expand_candidates(min_gap=3), [])

    def test_stop_keeps_the_page_list_and_resumes_after_it(self):
        with mock.patch.object(sm, "scrape", side_effect=self.fake(stop_after=1)):
            out, cps = self._run()
        self.assertEqual(out["status"], "stopped")
        cp = out["checkpoint"]
        self.assertEqual(([p["id"] for p in cp["pages"]], cp["i"]), (["501", "503"], 0))
        self.urls.clear()
        with mock.patch.object(sm, "scrape", side_effect=self.fake()):
            out, _ = self._run(resume=cp)
        self.assertEqual(len(self.urls), 2)  # the stopped page again, then the next
        self.assertEqual(out["status"], "done")

    def test_nothing_to_expand_is_not_an_error(self):
        db.mark_page_expanded("501", 26, 0)
        db.mark_page_expanded("503", 9, 0)
        with mock.patch.object(sm, "scrape") as scrape:
            out, _ = self._run()
        scrape.assert_not_called()
        self.assertEqual(out["status"], "done")


class ScheduleTest(unittest.TestCase):
    def setUp(self):
        _setup()
        bg.manager.state["running"] = False

    def test_due_only_when_idle_enabled_and_under_the_cap(self):
        self.assertTrue(bg._page_expand_due())
        db.set_setting("page_expand_enabled", "false")
        self.assertFalse(bg._page_expand_due())
        db.set_setting("page_expand_enabled", "true")
        db.set_setting("page_expand_daily", "2")
        page_expand.bump(db, 2)
        self.assertFalse(bg._page_expand_due())  # cap reached today
        db.set_setting("page_expand_daily", "300")
        db.create_job(query="ساعة", source="manual")
        self.assertFalse(bg._page_expand_due())  # another scan waits: it goes first

    def test_api_status_and_run_now(self):
        import app as app_module
        from werkzeug.security import generate_password_hash
        p = mock.patch.object(db, "claim_next_job", return_value=None)  # nothing runs it
        p.start()
        self.addCleanup(p.stop)
        db.create_user("admin@test.dz", generate_password_hash("pass-12345"), role="admin")
        c = app_module.app.test_client()
        c.post("/api/auth/login", json={"email": "admin@test.dz", "password": "pass-12345"})
        d = c.get("/api/page-expand").get_json()
        self.assertEqual((d["pending_pages"], d["missing_ads"], d["daily"]), (2, 32, 300))
        r = c.post("/api/page-expand/run").get_json()
        self.assertTrue(r["ok"])
        self.assertEqual(db.get_job(r["job_id"])["mode"], sm.EXPAND_MODE)
        self.assertEqual(c.post("/api/page-expand/run").status_code, 409)
        c.post("/api/settings", json={"page_expand_daily": 50, "page_expand_min_gap": 10})
        d = c.get("/api/page-expand").get_json()
        self.assertEqual((d["daily"], d["min_gap"], d["pending_pages"]), (50, 10, 1))


if __name__ == "__main__":
    unittest.main()
