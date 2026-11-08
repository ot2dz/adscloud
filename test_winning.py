# -*- coding: utf-8 -*-
"""Winning products: save from an ad, follow it (stopped pages included),
Telegram alerts on launch / stop / relaunch, and the API.

Run with:  python -m unittest -v test_winning
"""
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

_TMP_DIR = tempfile.mkdtemp(prefix="adscloud-winning-")
os.environ["META_ADS_SQLITE"] = os.path.join(_TMP_DIR, "test.sqlite")
os.environ["DATABASE_URL"] = ""
os.environ.setdefault("ALLOW_INSECURE_SECRET_KEY", "1")
os.environ["SESSION_COOKIE_SECURE"] = "0"
os.environ["AUTO_SCHEDULE"] = "off"
os.environ["TELEGRAM_BOT"] = "off"

import status_check  # noqa: E402
import winning  # noqa: E402
from db import db  # noqa: E402


def _ad(ad_id, link="https://shop.dz/products/lamp", page_id="700", active=True, body="مصباح ذكي"):
    start = (datetime.now(timezone.utc) - timedelta(days=6)).date().isoformat()
    return {"ad_id": ad_id, "page_name": "Lamp Store", "page_id": page_id, "start_date": start,
            "is_active": active, "link_url": link, "store_domain": "shop.dz", "body": body,
            "cta_text": "SHOP_NOW", "collation_count": 1}


def _clean():
    for t in ("meta_ads", "ad_keywords", "ad_niches", "ad_events", "winning_products", "users", "settings",
              "user_sessions", "auth_attempts", "security_events"):
        db._execute(f"DELETE FROM {t}")


def tearDownModule():
    # Other test modules may share this SQLite file when run together.
    _clean()


class WinningDbTest(unittest.TestCase):
    def setUp(self):
        _clean()
        db.save_ad(_ad("a1"), query="lamp", country="DZ")
        db.save_ad(_ad("a2"), query="lamp", country="DZ")  # same product, 2nd creative
        db.save_ad(_ad("b1", link="https://shop.dz/products/fan"), query="fan", country="DZ")

    def test_save_is_per_product_and_idempotent(self):
        w1 = db.add_winning_product(1, "a1")
        w2 = db.add_winning_product(1, "a2")  # other ad, same product
        self.assertEqual(w1["id"], w2["id"])
        self.assertEqual(db.winning_product_keys(1), {w1["product_key"]: w1["id"]})
        self.assertIsNone(db.add_winning_product(1, "nope"))

    def test_list_has_live_numbers(self):
        db.add_winning_product(1, "a1")
        [p] = db.list_winning_products(user_id=1)
        self.assertEqual((p["total_ads"], p["active_ads"]), (2, 2))
        self.assertEqual(p["week"]["launched"], 2)
        self.assertEqual(p["stage"], "watching")
        self.assertEqual(db.list_winning_products(user_id=2), [])

    def test_notes_and_stage(self):
        w = db.add_winning_product(1, "a1")
        db.update_winning_product(w["id"], notes="سعر الشراء 800", stage="testing")
        db.update_winning_product(w["id"], stage="bogus")  # ignored
        row = db.get_winning_product(w["id"])
        self.assertEqual((row["notes"], row["stage"]), ("سعر الشراء 800", "testing"))

    def test_stopped_product_page_stays_followed(self):
        w = db.add_winning_product(1, "a1")
        db.apply_status_check({}, ["a1", "a2", "b1"])
        self.assertEqual(db.status_check_pages(), [])   # daily check drops the page
        self.assertEqual(db.winning_pages(), ["700"])   # winning check keeps it
        events = db.winning_product_events(w["product_key"])
        self.assertEqual(sorted(e["event"] for e in events), ["launched", "launched", "stopped", "stopped"])


class WinningCheckTest(unittest.TestCase):
    """The winning check scans a page with no running ads and records returns."""

    def setUp(self):
        _clean()
        db.save_ad(_ad("a1"), query="lamp", country="DZ")
        self.pkey = db.add_winning_product(1, "a1")["product_key"]
        db.apply_status_check({}, ["a1"])

    def _run(self, ads):
        returned = []

        def fetch(page_ids, **k):
            return ads, True, 1

        def on_new(a):
            returned.append(a.ad_id)
            d = _ad(a.ad_id, link=a.link_url)
            return db.save_ad(d, query="", country="DZ")

        status_check.run(db, progress=lambda m: None, should_stop=lambda: False, proxy="p",
                         fetch=fetch, sleep=lambda s: None, on_new_ad=on_new,
                         pages=db.winning_pages(), watch_returns=True)
        return returned

    def _meta_ad(self, ad_id):
        return SimpleNamespace(ad_id=ad_id, page_id="700", collation_id="", store_domain="shop.dz",
                               link_url="https://shop.dz/products/lamp", body="مصباح ذكي")

    def test_relaunch_is_detected(self):
        self.assertEqual(self._run([self._meta_ad("a9")]), ["a9"])
        ev = [e for e in db.winning_events_after(0) if e["ad_id"] == "a9"]
        self.assertEqual(ev[0]["event"], "launched")
        self.assertEqual(ev[0]["detail"].get("relaunch_of"), "a1")

    def test_stopped_ad_coming_back_is_resumed(self):
        self._run([self._meta_ad("a1")])
        self.assertEqual(db.winning_events_after(0)[-1]["event"], "resumed")
        self.assertEqual(db.product_active_count(self.pkey), 1)

    def test_default_check_still_skips_pages_without_active_ads(self):
        calls = []
        status_check.run(db, progress=lambda m: None, should_stop=lambda: False, proxy="p",
                         fetch=lambda *a, **k: calls.append(1) or ([], True, 1),
                         sleep=lambda s: None, pages=["700"])
        self.assertEqual(calls, [])


class WinningNotifyTest(unittest.TestCase):
    def setUp(self):
        _clean()
        db.save_ad(_ad("a1"), query="lamp", country="DZ")
        db.add_winning_product(1, "a1")
        db.set_setting("telegram_token", "t")
        db.set_setting("telegram_chat_id", "c")
        self.sent = []

    def _send(self, ok=True):
        def send(token, chat, text):
            self.sent.append(text)
            return ok
        return send

    def test_first_run_only_sets_the_watermark(self):
        self.assertEqual(winning.notify_new_events(db, send=self._send()), 0)
        self.assertEqual(self.sent, [])
        self.assertEqual(db.get_settings()[winning.WATERMARK_KEY], str(db.max_ad_event_id()))

    def test_stop_then_relaunch_messages(self):
        winning.notify_new_events(db, send=self._send())
        db.apply_status_check({}, ["a1"])
        self.assertEqual(winning.notify_new_events(db, send=self._send()), 1)
        self.assertIn("أوقف <b>1</b> إعلان", self.sent[-1])
        self.assertIn("المنتج متوقف بالكامل", self.sent[-1])
        db.save_ad(_ad("a2"), query="lamp", country="DZ")
        winning.notify_new_events(db, send=self._send())
        self.assertIn("أعاد إطلاق المنتج", self.sent[-1])
        self.assertIn("يعمل الآن: <b>1</b>", self.sent[-1])
        self.assertEqual(winning.notify_new_events(db, send=self._send()), 0)  # nothing new

    def test_other_products_do_not_alert(self):
        winning.notify_new_events(db, send=self._send())
        db.save_ad(_ad("x1", link="https://other.dz/p/x", page_id="900"), query="x", country="DZ")
        self.assertEqual(winning.notify_new_events(db, send=self._send()), 0)

    def test_failed_send_is_retried(self):
        winning.notify_new_events(db, send=self._send())
        db.apply_status_check({}, ["a1"])
        self.assertEqual(winning.notify_new_events(db, send=self._send(ok=False)), 0)
        self.assertEqual(winning.notify_new_events(db, send=self._send()), 1)


class WinningApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import app as app_module
        cls.app = app_module.app

    def setUp(self):
        _clean()
        from werkzeug.security import generate_password_hash
        db.create_user("u1@test.dz", generate_password_hash("pass-12345"))
        db.create_user("u2@test.dz", generate_password_hash("pass-12345"))
        db.save_ad(_ad("a1"), query="lamp", country="DZ")

    def _client(self, email):
        c = self.app.test_client()
        r = c.post("/api/auth/login", json={"email": email, "password": "pass-12345"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        return c

    def test_save_list_note_delete(self):
        c = self._client("u1@test.dz")
        r = c.post("/api/winning", json={"ad_id": "a1"}).get_json()
        self.assertTrue(r["ok"])
        wid = r["product"]["id"]
        self.assertIn(r["product"]["product_key"], c.get("/api/winning/keys").get_json()["keys"])
        self.assertTrue(c.patch(f"/api/winning/{wid}", json={"notes": "جيد", "stage": "ordered"}).get_json()["ok"])
        [p] = c.get("/api/winning").get_json()["products"]
        self.assertEqual((p["notes"], p["stage"], p["active_ads"]), ("جيد", "ordered", 1))
        self.assertEqual(c.get(f"/api/winning/{wid}/events").get_json()["events"][0]["event"], "launched")
        self.assertTrue(c.delete(f"/api/winning/{wid}").get_json()["ok"])
        self.assertEqual(c.get("/api/winning").get_json()["products"], [])

    def test_users_cannot_touch_each_others_products(self):
        wid = self._client("u1@test.dz").post("/api/winning", json={"ad_id": "a1"}).get_json()["product"]["id"]
        other = self._client("u2@test.dz")
        self.assertEqual(other.get("/api/winning").get_json()["products"], [])
        self.assertEqual(other.patch(f"/api/winning/{wid}", json={"notes": "x"}).status_code, 404)
        self.assertEqual(other.delete(f"/api/winning/{wid}").status_code, 404)

    def test_bad_input(self):
        c = self._client("u1@test.dz")
        self.assertEqual(c.post("/api/winning", json={}).status_code, 400)
        self.assertEqual(c.post("/api/winning", json={"ad_id": "zzz"}).status_code, 404)
        wid = c.post("/api/winning", json={"ad_id": "a1"}).get_json()["product"]["id"]
        self.assertEqual(c.patch(f"/api/winning/{wid}", json={"stage": "bogus"}).status_code, 400)


if __name__ == "__main__":
    unittest.main()
