# -*- coding: utf-8 -*-
"""Jobs section: groups, verdicts, queue order, retry.

Run with:  python -m unittest -v test_jobs_view
"""
import json
import unittest
from unittest import mock

import test_competitor as tc  # sets the test DB env before db is imported
import background as bg
import jobs_view as jv
import scrape_manager as sm
from db import db
from engine import ScrapeResult


class Base(unittest.TestCase):
    def setUp(self):
        tc._clean()
        db._execute("DELETE FROM jobs")

    def done(self, status="done", count=5, source="manual", mode="", query="ساعة", **kw):
        fin = {k: kw.pop(k) for k in ("scanned_count", "completed", "slices_total", "slices_ok", "error",
                                      "checkpoint", "summary") if k in kw}
        jid = db.create_job(query=query, source=source, mode=mode, **kw)
        db.finish_job(jid, status=status, result_count=count, **fin)
        return jid

    def ids(self, **kw):
        return [j["id"] for j in jv.history(db, **kw)["jobs"]]


class HistoryTest(Base):
    def test_expansion_never_hides_the_users_jobs(self):
        mine = self.done()
        chk = self.done(source="manual", mode=sm.STATUS_MODE, query="فحص")
        daily = self.done(source="daily", mode=sm.STATUS_MODE, query="فحص")
        auto = self.done(source="schedule")
        exp = [self.done(source="page_expand", mode=sm.EXPAND_MODE, slices_total=30, slices_ok=30) for _ in range(80)]
        self.assertEqual(sorted(self.ids()), [mine, chk])
        self.assertEqual(sorted(self.ids(group="checks")), [chk, daily])
        self.assertEqual(self.ids(group="auto"), [auto])
        self.assertEqual(jv.history(db, group="expand")["total"], len(exp))
        day = jv.expand_days(db)
        self.assertEqual((len(day), day[0]["pages"], day[0]["runs"]), (1, 2400, 80))
        self.assertEqual(jv.group_badges(db)["mine"], {"total": 2, "failed": 0})

    def test_every_source_lands_in_exactly_one_of_mine_and_auto(self):
        for s in jv.USER_SOURCES + ("schedule", "daily_rescan"):
            j = self.done(source=s)
            self.assertEqual((j in self.ids(limit=99)) + (j in self.ids(group="auto", limit=99)), 1, s)

    def test_verdicts(self):
        def verdict(reported=None, **kw):
            j = self.done(**kw)
            db.set_job_reported(j, reported)
            return next(x["verdict"] for x in jv.history(db, group=kw.get("group_", "mine"))["jobs"] if x["id"] == j)
        self.assertEqual(verdict(completed=True, scanned_count=95, reported=100), "ok")
        self.assertEqual(verdict(completed=True, scanned_count=50, reported=100), "partial")
        self.assertEqual(verdict(completed=False), "partial")
        self.assertEqual(verdict(count=0, completed=True), "empty")
        self.assertEqual(verdict(status="failed"), "failed")
        self.assertEqual(verdict(status="stopped"), "stopped")
        self.assertEqual(verdict(mode=sm.STATUS_MODE, count=0, completed=True), "ok")   # a check may find nothing
        self.assertEqual(verdict(mode=sm.STATUS_MODE, completed=False), "partial")
        self.assertEqual(verdict(mode=sm.MONTH_MODE, slices_total=4, slices_ok=3, completed=False), "partial")
        h = jv.history(db, verdict="failed")
        self.assertEqual((h["total"], len(h["jobs"]), h["counts"]["partial"]), (1, 1, 4))

    def test_search_titles_and_pages(self):
        t = db._execute("INSERT INTO targets (id, type, target_value, label, country, enabled) "
                        "VALUES (77, 'page', '123456789', 'منافس القهوة', 'DZ', 1)")
        self.addCleanup(db._execute, "DELETE FROM targets WHERE id = 77")
        a = self.done(query="123456789", target_id=77, url="https://x/?view_all_page_id=123456789", source="manual_target")
        self.done(query="shop.dz")
        h = jv.history(db, q="القهوة")
        self.assertEqual([j["id"] for j in h["jobs"]], [a])
        self.assertEqual((h["jobs"][0]["title"], h["jobs"][0]["kind"]), ("منافس القهوة", "page"))
        self.assertEqual({j["kind"] for j in jv.history(db)["jobs"]}, {"page", "store"})
        self.assertEqual(len(self.ids(limit=1)) + len(self.ids(limit=1, offset=1)), 2)


class OpenTest(Base):
    def test_running_and_queue_in_run_order(self):
        exp = db.create_job(query="e", source="page_expand", mode=sm.EXPAND_MODE)
        kw = db.create_job(query="ساعة", source="manual")
        held = db.create_job(query="قلم", source="manual")
        db.request_pause(held)
        run = db.create_job(query="كأس", source="manual", priority=9)
        db.claim_next_job("x")
        running, waiting = jv.open_jobs(db)
        self.assertEqual([j["id"] for j in running], [run])
        self.assertEqual([j["id"] for j in waiting], [kw, exp, held])

    def test_start_time_survives_a_resume(self):
        jid = db.create_job(query="x", source="manual")
        db.claim_next_job("x")
        first = db.get_job(jid)["first_started_at"]
        db.schedule_retry(jid, -1, None)
        db.claim_next_job("x")
        self.assertTrue(first)
        self.assertEqual(db.get_job(jid)["first_started_at"], first)


class RetryTest(Base):
    def test_failed_job_carries_on_from_its_position(self):
        jid = self.done(status="failed", checkpoint='{"pass": 1, "cursor": "c"}', error="x", query="ساعة")
        db.request_stop(jid)
        self.assertEqual(jv.retry(db, jid), (jid, "", True))
        j = db.get_job(jid)
        self.assertEqual((j["status"], j["attempts"], j["stop_requested"], j["finished_at"]), ("pending", 0, 0, None))
        self.assertIn("cursor", j["checkpoint"])
        self.assertIsNone(jv.retry(db, jid)[0])  # open now

    def test_other_jobs_start_over_as_a_new_one(self):
        jid = self.done(status="stopped", source="schedule", query="ساعة", exact_phrase=True)
        new, err, resumed = jv.retry(db, jid)
        self.assertNotEqual(new, jid)
        j = db.get_job(new)
        self.assertEqual((j["query"], j["exact_phrase"], j["source"], resumed), ("ساعة", 1, "manual", False))

    def test_no_second_check_at_once(self):
        jid = self.done(mode=sm.STATUS_MODE, source="daily")
        db.create_job(query="فحص", source="daily", mode=sm.STATUS_MODE)
        self.assertIsNone(jv.retry(db, jid)[0])


class RunTest(Base):
    def test_empty_run_keeps_its_reason(self):
        jid = db.create_job(query="ساعة", source="manual")
        job = db.claim_next_job("x")
        with mock.patch.object(sm, "scrape", return_value=ScrapeResult(completed=True, reported_count=0)):
            bg.run_claimed_job(job, sm.ScrapeManager())
        j = db.get_job(jid)
        self.assertEqual(j["status"], "done")
        self.assertIn("Meta", j["error"])
        self.assertEqual(jv.history(db)["jobs"][0]["verdict"], "empty")

    def test_check_keeps_its_numbers(self):
        import status_check

        def run(_db, **kw):
            kw["stats"].update(confirmed=7, stopped=2, new=1, pages=3, skipped_batches=1)
            return "done"
        db.set_setting("status_check_proxy", "t:1")
        jid = db.create_job(query="فحص", source="manual", mode=sm.STATUS_MODE)
        job = db.claim_next_job("x")
        with mock.patch.object(status_check, "run", side_effect=run):
            bg.run_claimed_job(job, sm.ScrapeManager())
        v = jv.history(db, group="checks")["jobs"][0]
        self.assertEqual((v["id"], v["summary"]["confirmed"], v["summary"]["stopped"]), (jid, 7, 2))
        self.assertEqual(v["verdict"], "partial")  # one batch was skipped


class ApiTest(Base):
    def test_list_retry_and_cancel_waiting(self):
        import app as app_module
        from werkzeug.security import generate_password_hash
        c = app_module.app.test_client()
        self.assertEqual(c.get("/api/jobs").status_code, 401)
        db.create_user("admin@test.dz", generate_password_hash("pass-12345"), role="admin")
        c.post("/api/auth/login", json={"email": "admin@test.dz", "password": "pass-12345"})
        done = self.done()
        wait = db.create_job(query="قلم", source="manual")
        d = c.get("/api/jobs").get_json()
        self.assertEqual([j["id"] for j in d["waiting"]], [wait])
        self.assertEqual([j["id"] for j in d["history"]["jobs"]], [done])
        self.assertEqual(c.get("/api/jobs?group=nope&verdict=x&limit=abc").status_code, 200)
        c.post(f"/api/jobs/{wait}/stop")
        self.assertEqual(db.get_job(wait)["status"], "stopped")  # at once, not when its turn comes
        self.assertTrue(c.post(f"/api/jobs/{done}/retry").get_json()["ok"])
        self.assertEqual(c.post("/api/jobs/999999/retry").status_code, 409)


if __name__ == "__main__":
    unittest.main()
