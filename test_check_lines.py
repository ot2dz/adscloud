# -*- coding: utf-8 -*-
"""Checks keep going when the tablet is down: through the idle scrape line, from
the same position, back to the tablet when it returns.

Run with:  python -m unittest -v test_check_lines
"""
import json
import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

import test_competitor as tc  # sets the test DB env before db is imported
import background as bg
import check_lines as cl
import notify
import scrape_manager as sm
import status_check
from db import db

PAGES = [f"{i:03d}" for i in range(1, 7)]


def _meta(pid):
    return SimpleNamespace(ad_id=f"a{pid}", page_id=pid, collation_id="", store_domain="shop.dz",
                           link_url="https://shop.dz/p", body="x")


class Base(unittest.TestCase):
    def setUp(self):
        tc._clean()
        db._execute("DELETE FROM jobs")
        cl.reset()
        notify._last_alert.clear()
        sm.tablet_lane_busy.clear()
        db.set_setting("meta_ads_proxy", "work:3128")
        db.set_setting("status_check_proxy", "tablet:8080")
        for pid in PAGES:
            db.save_ad(tc._ad(f"a{pid}", "shop.dz", f"منتج {pid}", link_path=f"/p/{pid}", page_id=pid),
                       query="q", country="DZ")
        self.up = {"tablet:8080": True, "work:3128": True}
        self.calls = []          # (proxy, pages) of every batch
        self.hook = lambda proxy, pages: None
        for p in [mock.patch("engine._public_ip", side_effect=lambda px=None, **k: "1.2.3.4" if self.up.get(px) else ""),
                  mock.patch.object(status_check, "BATCH_PAGES", 1),
                  mock.patch.object(status_check, "BATCH_PAUSE_SEC", 0),
                  mock.patch.object(status_check, "RECHECK_WAIT_SEC", 0),
                  mock.patch.object(status_check, "rotate_and_wait_ready", return_value=False),
                  mock.patch.object(cl, "PROBE_EVERY_SEC", 0),
                  mock.patch.object(bg, "JOB_CONTROL_CHECK_SEC", 0),
                  mock.patch.object(bg.time, "sleep", side_effect=lambda s, r=time.sleep: r(min(s, 0.02))),
                  mock.patch.dict(status_check.run.__kwdefaults__, {"fetch": self.fetch})]:
            p.start()
            self.addCleanup(p.stop)

    def fetch(self, page_ids, proxy=None, should_stop=None, max_results=None):
        if max_results is None:
            self.calls.append((proxy, list(page_ids)))
            self.hook(proxy, page_ids)
        if should_stop():
            return [], False, 1
        if not self.up.get(proxy):
            return [], False, 1
        return [_meta(p) for p in page_ids], True, 1

    def job(self):
        return db.create_job(query="فحص", source="daily", mode=sm.STATUS_MODE)

    def step(self):
        return bg.check_worker_step("inproc-t-check")

    def checked(self):
        """Pages asked for and answered, in order."""
        return [p for _px, ps in self.calls for p in ps]


class LineTest(Base):
    def test_tablet_first_then_the_idle_scrape_line(self):
        self.assertEqual(cl.acquire(), cl.TABLET)
        self.up["tablet:8080"] = False
        line = cl.acquire()
        self.assertEqual(line, cl.SCRAPE)
        self.assertTrue(cl.scrape_line_lock.locked())
        cl.release(line)
        self.assertFalse(cl.scrape_line_lock.locked())

    def test_scrape_line_refused_when_not_free(self):
        self.up["tablet:8080"] = False
        with cl.scrape_line_lock:                       # a scrape runs
            self.assertIsNone(cl.acquire())
        kw = db.create_job(query="ساعة", source="manual")  # a scrape waits
        self.assertIsNone(cl.acquire())
        db.finish_job(kw, status="done")
        db.set_setting("status_check_fallback", "false")
        self.assertIsNone(cl.acquire())
        db.set_setting("status_check_fallback", "true")
        db.set_setting("meta_ads_proxy", "")            # never the server's own IP
        with mock.patch.dict("os.environ", {"META_ADS_PROXY": ""}):
            self.assertIsNone(cl.acquire())
        self.assertFalse(cl.scrape_line_lock.locked())

    def test_a_failed_line_rests_longer_each_time(self):
        self.assertEqual([cl.failed(cl.TABLET) for _ in range(6)], [5, 10, 20, 40, 60, 60])
        self.assertFalse(cl.tablet_ready())
        cl.worked(cl.TABLET)
        self.assertEqual(cl.failed(cl.TABLET), 5)


class FailoverTest(Base):
    def test_tablet_dies_and_the_check_ends_on_the_scrape_line(self):
        jid = self.job()

        def die(proxy, pages):
            if pages == ["003"]:
                self.up["tablet:8080"] = False
        self.hook = die
        self.assertTrue(self.step())
        j = db.get_job(jid)
        self.assertEqual((j["status"], int(j["attempts"] or 0)), ("pending", 0))  # not a failed try
        self.assertEqual(json.loads(j["checkpoint"])["after"], "002")
        n = len(self.calls)
        self.assertTrue(self.step())
        self.assertEqual({px for px, _ in self.calls[n:]}, {"work:3128"})
        self.assertEqual([p for _x, ps in self.calls[n:] for p in ps], ["003", "004", "005", "006"])
        self.assertEqual(db.get_job(jid)["status"], "done")
        s = json.loads(db.get_settings()["status_check_summary"])
        self.assertEqual((s["confirmed"], s["stopped"]), (6, 0))   # totals of both parts
        self.assertFalse(cl.scrape_line_lock.locked())

    def test_leaves_the_scrape_line_for_a_scrape_and_resumes(self):
        self.up["tablet:8080"] = False
        jid, kw = self.job(), []

        def scrape_arrives(proxy, pages):
            if pages == ["002"] and not kw:
                kw.append(db.create_job(query="ساعة", source="manual"))
                for _ in range(200):
                    if sm.check_manager.stop_event.wait(0.02):
                        break
        self.hook = scrape_arrives
        self.assertTrue(self.step())
        self.assertEqual(db.get_job(jid)["status"], "pending")
        self.assertFalse(cl.scrape_line_lock.locked())
        self.assertFalse(self.step())                    # the scrape goes first
        with mock.patch.object(bg, "run_claimed_job") as run:
            self.assertTrue(bg.job_worker_step("inproc-t"))
        self.assertEqual(run.call_args[0][0]["id"], kw[0])
        db.finish_job(kw[0], status="done")
        self.assertTrue(self.step())
        self.assertEqual(db.get_job(jid)["status"], "done")
        self.assertEqual(sorted(set(self.checked())), PAGES)

    def test_goes_back_to_the_tablet_when_it_returns(self):
        self.up["tablet:8080"] = False
        jid = self.job()

        def back(proxy, pages):
            if pages == ["003"] and proxy == "work:3128":
                self.up["tablet:8080"] = True
                for _ in range(200):
                    if sm.check_manager.stop_event.wait(0.02):
                        break
        self.hook = back
        self.step()
        n = len(self.calls)
        self.step()
        self.assertEqual({px for px, _ in self.calls[n:]}, {"tablet:8080"})
        self.assertEqual([p for _x, ps in self.calls[n:] for p in ps], ["003", "004", "005", "006"])
        self.assertEqual(db.get_job(jid)["status"], "done")

    def test_both_lines_down_waits_for_ever_without_failing(self):
        jid = self.job()
        self.up.update({"tablet:8080": False, "work:3128": False})
        for _ in range(8):
            self.assertFalse(self.step())
        self.assertEqual((db.get_job(jid)["status"], self.calls), ("pending", []))
        self.up["work:3128"] = True
        self.step()
        self.assertEqual(db.get_job(jid)["status"], "done")

    def test_user_stop_wins_over_a_line_change(self):
        jid = self.job()

        def die(proxy, pages):
            db.request_stop(jid)
            self.up["tablet:8080"] = False
        self.hook = die
        self.step()
        self.assertEqual(db.get_job(jid)["status"], "stopped")

    def test_scrape_waits_for_the_check_to_leave_the_line(self):
        cl.scrape_line_lock.acquire()
        db.create_job(query="ساعة", source="manual")
        with mock.patch.object(bg, "run_claimed_job") as run:
            t = threading.Thread(target=bg.job_worker_step, args=("inproc-t",), daemon=True)
            t.start()
            t.join(0.3)
            run.assert_not_called()
            cl.scrape_line_lock.release()
            t.join(5)
            run.assert_called_once()

    def test_tablet_alert_once(self):
        db.set_setting("telegram_token", "t")
        db.set_setting("telegram_chat_id", "c")
        self.up.update({"tablet:8080": False, "work:3128": False})
        self.job()
        with mock.patch.object(notify, "_telegram_send", return_value=True) as send:
            self.step()
            self.step()
        self.assertEqual(send.call_count, 1)
        self.assertIn("التابلت", send.call_args[0][2])


class PositionTest(Base):
    def test_pages_that_left_the_list_do_not_shift_the_resume(self):
        # 001-003 done, then 001 and 002 stopped and left the list.
        for pid in ("001", "002"):
            db._execute(f"UPDATE meta_ads SET is_active = 0 WHERE page_id = '{pid}'")
        st = {}
        status_check.run(db, progress=lambda m: None, should_stop=lambda: False, proxy="tablet:8080",
                         start_batch=4, after="003", stats=st, sleep=lambda s: None)
        self.assertEqual(self.checked(), ["004", "005", "006"])  # by number, 004 and 005 were skipped
        self.assertEqual(st["after"], "006")

    def test_a_line_dying_mid_batch_does_not_lose_the_batch(self):
        def fetch(page_ids, proxy=None, should_stop=None, max_results=None):
            if max_results is not None:
                return ([_meta("001")], True, 1) if not self.calls else ([], False, 1)
            self.calls.append((proxy, page_ids))
            return [_meta("001")], False, 1  # partial
        st = {}
        with self.assertRaises(status_check.Blocked):
            status_check.run(db, progress=lambda m: None, should_stop=lambda: False, proxy="p",
                             fetch=fetch, sleep=lambda s: None, stats=st)
        self.assertNotIn("after", st)


if __name__ == "__main__":
    unittest.main()
