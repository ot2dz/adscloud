# -*- coding: utf-8 -*-
"""LightFunnels sales tracker: page parsing, captcha PoW, the probe pipeline
and the daily-orders maths.

Run with:  python -m unittest -v test_sales_tracker
"""
import base64
import json
import unittest
from datetime import datetime, timedelta, timezone

import test_competitor as tc  # noqa: F401  (sets the test DB env + imports db)
import sales_tracker as st
from db import db


def _fixture_html() -> str:
    data = {
        "account": {"id": 89823, "uid": "acc_TEST", "name": "Luxe Nature"},
        "funnel": {},
        "steps": [
            {"id": 4159438, "slug": "Ni_Gfoeo4", "type": "product_page", "uid": "step_a"},
            {"id": 4159439, "slug": "4VcW8KfdN", "type": "thank_you_page", "uid": "step_b"},
        ],
        "state": {"checkout": {"body": [{"price_bundle": None, "variants": [{"id": 4301579665, "quantity": 1}]}],
                               "currency": "DZD"}},
        "payment_methods": [{"uid": "pg_1", "value": "cod", "label": "Cash on Delivery"}],
        "events": "CODE_BLOB",
        "visitor": {"country": "DZ"},
    }
    env = {"token": "envtoken", "CAPTCHA_URL": "https://api.lightfunnels.com/storefront/captcha"}
    tree = {"id": "root", "t": "Root", "p": {"children": [
        {"t": "Button", "id": "btn-1", "iref": "AAA%3D", "p": {"action": "process-payment"}},
    ]}}
    lf = {"account": {"id": "acc_TEST"}, "funnel": {"id": "fun_TEST"}, "product": {"id": "prod_X"}}
    return ("<html><body><script>\n"
            f"var data = {json.dumps(data)};\n"
            f"var env = {json.dumps(env)};\n"
            f"var tree = {json.dumps(tree)};\n"
            f"var Lightfunnels = {json.dumps(lf)};\n"
            "</script></body></html>")


class FakeResp:
    def __init__(self, payload=None, text="", status=200):
        self._payload = payload
        self.text = text
        self.status_code = status

    def json(self):
        return self._payload


class FakeSession:
    """Answers the storefront API in call order."""

    def __init__(self, html, *, cooldown=False):
        self.html = html
        self.cooldown = cooldown
        self.otp = False
        self.queries = []
        self.url = "https://store.myecomsite.net/Ni_Gfoeo4"

    def get(self, url, **kw):
        if "captcha" in url:
            return FakeResp({"parameters": {"nonce": "00", "salt": "11", "cost": 1,
                                            "keyPrefix": "00", "keyLength": 32}})
        return FakeResp(text=self.html)

    def post(self, url, json=None, headers=None, timeout=None, **kw):
        query = (json or {}).get("query", "")
        self.queries.append(query[:60])
        if "shipping_regions" in query:
            return FakeResp({"data": {"shipping_regions": [{
                "id": "r1", "region": "Alger", "custom_shipping_rates": [{"id": "rate-1", "price": 300}],
                "cities": [{"city": "Alger Centre", "id": "c1"}]}]}})
        if "CREATE_CHECKOUT" in query:
            return FakeResp({"data": {"checkout": {"slug": "slug-1"}}})
        if "EXECUTE_CHECKOUT" in query:
            if self.cooldown:
                return FakeResp({"data": {"checkout": None},
                                 "errors": [{"key": "errors.cod_order_too_soon", "message": "too soon"}]})
            if self.otp:
                return FakeResp({"data": {"checkout": None},
                                 "errors": [{"key": "validation.invalid_otp_phone_number", "message": "otp"}]})
            return FakeResp({"data": {"checkout": {"slug": "slug-1", "destination": "https://x/y"}}})
        if "fetchOrder" in query:
            return FakeResp({"data": {"order": {"id": 36422854, "name": "#10185", "total": 2500,
                                                "currency": "DZD", "created_at": "2026-11-24 09:10:53"}}})
        return FakeResp({"data": {}})


class ParseTest(unittest.TestCase):
    def test_detects_lightfunnels_and_reads_config(self):
        cfg = st.parse_store(_fixture_html(), "https://store.myecomsite.net/Ni_Gfoeo4")
        self.assertEqual(cfg["account_id"], "89823")
        self.assertEqual(cfg["funnel_id"], "fun_TEST")
        self.assertEqual(cfg["funnel_slug"], "Ni_Gfoeo4")
        self.assertEqual(cfg["thank_slug"], "4VcW8KfdN")
        self.assertEqual(cfg["variant_id"], 4301579665)
        self.assertEqual(cfg["gateway_uid"], "pg_1")
        self.assertEqual(cfg["button"], {"id": "btn-1", "iref": "AAA%3D"})

    def test_rejects_non_lightfunnels(self):
        with self.assertRaises(st.NotLightfunnels):
            st.parse_store("<html><body>shopify</body></html>", "https://x.com/")

    def test_json_after_survives_braces_in_strings(self):
        html = '<script>var data = {"a": "x}y{z", "account": {"id": 1}};</script>'
        self.assertEqual(st._json_after(html, "var data =")["a"], "x}y{z")


class CaptchaTest(unittest.TestCase):
    def test_solves_the_proof_of_work(self):
        import hashlib
        nonce = bytes.fromhex("0102030405060708")
        salt = bytes.fromhex("aabbccdd")
        counter = 7
        key = hashlib.pbkdf2_hmac("sha256", nonce + counter.to_bytes(4, "big"), salt, 50, dklen=32)
        challenge = {"parameters": {"nonce": nonce.hex(), "salt": salt.hex(), "cost": 50,
                                    "keyLength": 32, "keyPrefix": key.hex()[:8]},
                     "signature": "sig"}
        solved = st.solve_captcha(challenge)
        self.assertEqual(solved["counter"], counter)
        token = json.loads(base64.b64decode(solved["token"]))
        self.assertEqual(token["solution"]["counter"], counter)
        self.assertEqual(token["challenge"]["signature"], "sig")

    def test_version_is_echoed_in_the_token(self):
        import hashlib
        nonce = bytes.fromhex("0102030405060708")
        salt = bytes.fromhex("aabbccdd")
        key = hashlib.pbkdf2_hmac("sha256", nonce + (3).to_bytes(4, "big"), salt, 20, dklen=32)
        challenge = {"parameters": {"nonce": nonce.hex(), "salt": salt.hex(), "cost": 20,
                                    "keyLength": 32, "keyPrefix": key.hex()[:6]}, "signature": "s"}
        solved = st.solve_captcha(challenge, version="vm-TEST")
        token = json.loads(base64.b64decode(solved["token"]))
        self.assertEqual(token["version"], "vm-TEST")


class ProbeTest(unittest.TestCase):
    def test_probe_reads_the_order_number(self):
        sess = FakeSession(_fixture_html())
        res = st.probe("https://store.myecomsite.net/Ni_Gfoeo4", session=sess,
                       solver=lambda ch: {"token": "captcha-token"})
        self.assertEqual(res["order_number"], 10185)
        self.assertEqual(res["order_name"], "#10185")
        self.assertEqual(res["account_id"], "89823")
        self.assertEqual(res["funnel_id"], "fun_TEST")
        self.assertTrue(any("EXECUTE_CHECKOUT" in q for q in sess.queries))

    def test_cooldown_is_reported(self):
        sess = FakeSession(_fixture_html(), cooldown=True)
        with self.assertRaises(st.CooldownError):
            st.probe("https://store.myecomsite.net/Ni_Gfoeo4", session=sess,
                     solver=lambda ch: {"token": "t"})


class OtpTest(unittest.TestCase):
    def test_otp_is_reported(self):
        sess = FakeSession(_fixture_html())
        sess.otp = True
        with self.assertRaises(st.OtpRequired):
            st.probe("https://store.myecomsite.net/Ni_Gfoeo4", session=sess,
                     solver=lambda ch: {"token": "t"})


class OrdersMathTest(unittest.TestCase):
    def test_each_probe_costs_one_order(self):
        self.assertEqual(st.orders_between(None, 100), 0)
        self.assertEqual(st.orders_between(100, 110), 9)   # 10 jump - our probe
        self.assertEqual(st.orders_between(100, 100), 0)


class DbTest(unittest.TestCase):
    def setUp(self):
        db._execute("DELETE FROM lf_probes")
        db._execute("DELETE FROM lf_stores")

    def test_store_crud_and_daily_orders(self):
        sid = db.add_lf_store(store_host="store.myecomsite.net",
                              product_url="https://store.myecomsite.net/Ni_Gfoeo4",
                              account_id="89823", funnel_id="fun_1", currency="DZD")
        self.assertGreater(sid, 0)
        self.assertEqual(db.get_lf_store(sid)["account_id"], "89823")

        now = datetime.now(timezone.utc)
        db.add_lf_probe(sid, kind="probe", order_name="#100", order_number=100,
                        probed_at=(now - timedelta(days=1)).isoformat())
        db.add_lf_probe(sid, kind="probe", order_name="#105", order_number=105,
                        probed_at=(now - timedelta(hours=6)).isoformat())
        db.add_lf_probe(sid, kind="probe", order_name="#110", order_number=110,
                        probed_at=now.isoformat())
        probes = db.lf_store_probes(sid, days=3)
        by_day = db.lf_orders_by_day(probes)
        self.assertEqual(sum(by_day.values()), 8)  # (105-100-1) + (110-105-1)
        self.assertEqual(db.get_lf_store(sid)["last_order_number"], 110)

        overview = db.lf_sales_overview(days=7)
        row = next(r for r in overview if r["id"] == sid)
        self.assertEqual(row["orders_window"], 8)

    def test_failed_probe_does_not_change_the_number(self):
        sid = db.add_lf_store(store_host="s2.myecomsite.net", product_url="https://s2.myecomsite.net/P",
                              account_id="1")
        db.add_lf_probe(sid, kind="probe", order_number=50, probed_at=datetime.now(timezone.utc).isoformat())
        db.add_lf_probe(sid, kind="cooldown", error="too soon",
                        probed_at=datetime.now(timezone.utc).isoformat())
        self.assertEqual(db.get_lf_store(sid)["last_order_number"], 50)
        self.assertEqual(db.lf_orders_by_day(db.lf_store_probes(sid, 2)), {})

    def test_daily_series_splits_overnight_and_marks_unmeasured(self):
        from datetime import date
        probes = [
            {"kind": "probe", "order_number": 10, "probed_at": "2026-11-23T22:00:00+00:00"},
            {"kind": "probe", "order_number": 21, "probed_at": "2026-11-24T08:00:00+00:00"},
        ]
        # jump = 21-10-1 = 10 orders; the interval crosses the 23:00 UTC boundary
        # (midnight in Algeria) so 1h lands on 23 Nov and 9h on 24 Nov.
        rows = db.lf_daily_series(probes, days=3, today=date(2026, 11, 25))
        by_date = {r["date"]: r for r in rows}
        self.assertEqual(by_date["2026-11-25"]["orders"], 0)
        self.assertFalse(by_date["2026-11-25"]["measured"])
        self.assertEqual((by_date["2026-11-23"]["orders"], by_date["2026-11-23"]["measured"]), (1, True))
        self.assertEqual((by_date["2026-11-24"]["orders"], by_date["2026-11-24"]["measured"]), (9, True))
        self.assertEqual(sum(r["orders"] for r in rows), 10)
        self.assertEqual(db.lf_orders_by_day(probes),
                         {"2026-11-23": 1, "2026-11-24": 9})

    def test_daily_rounding_keeps_the_total(self):
        from datetime import date
        probes = [
            {"kind": "probe", "order_number": 100, "probed_at": "2026-11-22T23:30:00+00:00"},
            {"kind": "probe", "order_number": 111, "probed_at": "2026-11-24T01:30:00+00:00"},
        ]  # jump 10 over 26h crossing midnight(s) -> split over the days, total kept
        rows = db.lf_daily_series(probes, days=4, today=date(2026, 11, 25))
        self.assertEqual(sum(r["orders"] for r in rows), 10)
        self.assertEqual(len([r for r in rows if r["measured"]]), 2)


class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import app as app_module
        cls.app = app_module.app

    def setUp(self):
        db._execute("DELETE FROM lf_probes")
        db._execute("DELETE FROM lf_stores")
        db._execute("DELETE FROM users WHERE email IN (?,?)", ("sales@test.dz", "user@test.dz"))
        from werkzeug.security import generate_password_hash
        db.create_user("sales@test.dz", generate_password_hash("pass-12345"), role="admin")
        db.create_user("user@test.dz", generate_password_hash("pass-12345"), role="user")

    def _client(self, email="sales@test.dz"):
        c = self.app.test_client()
        r = c.post("/api/auth/login", json={"email": email, "password": "pass-12345"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        return c

    @staticmethod
    def _detected(url):
        from urllib.parse import urlparse
        host = urlparse(url).netloc or "store.test"
        return {"host": host, "url": url, "account_id": "89823", "account_uid": "acc_X",
                "account_name": "Store", "funnel_id": "fun_1", "currency": "DZD"}

    def test_add_overview_and_delete(self):
        from unittest import mock
        c = self._client()
        with mock.patch("web.sales.sales_tracker.detect", side_effect=self._detected), \
             mock.patch("web.sales._start_probe", return_value=True):
            r = c.post("/api/sales/stores", json={"url": "https://store.myecomsite.net/P"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        sid = r.get_json()["store"]["id"]

        ov = c.get("/api/sales/overview?days=7").get_json()
        self.assertTrue(ov["ok"])
        self.assertTrue(ov["admin"])
        self.assertEqual([s["id"] for s in ov["stores"]], [sid])
        self.assertIn("last_order_number", ov["stores"][0])

        with mock.patch("web.sales._start_probe", return_value=True):
            self.assertEqual(c.post(f"/api/sales/admin/stores/{sid}/probe").status_code, 200)

        self.assertTrue(c.delete(f"/api/sales/stores/{sid}").get_json()["ok"])
        self.assertEqual(c.get("/api/sales/overview").get_json()["stores"], [])

    def test_requires_login(self):
        c = self.app.test_client()
        self.assertEqual(c.get("/api/sales/overview").status_code, 401)

    def test_rejects_non_lightfunnels(self):
        c = self._client()
        from unittest import mock
        with mock.patch("web.sales.sales_tracker.detect",
                        side_effect=st.NotLightfunnels("ليس LightFunnels")):
            r = c.post("/api/sales/stores", json={"url": "https://shop.example.com/p"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.get_json()["code"], "not_lightfunnels")

    def test_customer_sees_numbers_only(self):
        from unittest import mock
        c = self._client("user@test.dz")
        with mock.patch("web.sales.sales_tracker.detect", side_effect=self._detected), \
             mock.patch("web.sales._start_probe", return_value=True):
            r = c.post("/api/sales/stores", json={"url": "https://a.myecomsite.net/P"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        sid = r.get_json()["store"]["id"]
        db.add_lf_probe(sid, kind="probe", order_name="#500", order_number=500,
                        probed_at=datetime.now(timezone.utc).isoformat())
        db.add_lf_probe(sid, kind="probe", order_name="#503", order_number=503,
                        probed_at=datetime.now(timezone.utc).isoformat())
        ov = c.get("/api/sales/overview").get_json()
        self.assertFalse(ov["admin"])
        row = ov["stores"][0]
        self.assertNotIn("last_order_number", row)
        self.assertNotIn("probes_window", row)
        self.assertNotIn("last_probe_status", row)
        self.assertNotIn("account_id", row)
        self.assertIn("orders_today", row)
        self.assertIn("days", row)
        # customer cannot hit admin endpoints
        self.assertEqual(c.post(f"/api/sales/admin/stores/{sid}/probe").status_code, 403)

    def test_quotas_stores_and_active(self):
        from unittest import mock
        c = self._client("user@test.dz")
        ids = []
        with mock.patch("web.sales.sales_tracker.detect", side_effect=self._detected), \
             mock.patch("web.sales._start_probe", return_value=True):
            for i in range(5):
                r = c.post("/api/sales/stores", json={"url": f"https://s{i}.myecomsite.net/P"})
                self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
                ids.append(r.get_json()["store"]["id"])
            r6 = c.post("/api/sales/stores", json={"url": "https://s6.myecomsite.net/P"})
            self.assertEqual(r6.status_code, 400)
            self.assertTrue(r6.get_json().get("limit"))
            # only the first store is measured on the free plan
            lim = c.get("/api/sales/overview").get_json()["limits"]
            self.assertEqual((lim["stores_used"], lim["active_used"]), (5, 1))
            # enabling a second one is refused
            r = c.post(f"/api/sales/stores/{ids[1]}/toggle")
            self.assertEqual(r.status_code, 409)
            # disabling the active one frees the slot
            self.assertTrue(c.post(f"/api/sales/stores/{ids[0]}/toggle").get_json()["ok"])
            self.assertEqual(c.post(f"/api/sales/stores/{ids[1]}/toggle").status_code, 200)

    def test_customer_cannot_touch_other_users_store(self):
        from unittest import mock
        admin = self._client()
        with mock.patch("web.sales.sales_tracker.detect", side_effect=self._detected), \
             mock.patch("web.sales._start_probe", return_value=True):
            sid = admin.post("/api/sales/stores", json={"url": "https://owned.myecomsite.net/P"}).get_json()["store"]["id"]
        other = self._client("user@test.dz")
        self.assertEqual(other.delete(f"/api/sales/stores/{sid}").status_code, 404)
        self.assertEqual(other.post(f"/api/sales/stores/{sid}/toggle").status_code, 404)
        self.assertEqual(other.get("/api/sales/overview").get_json()["stores"], [])


if __name__ == "__main__":
    unittest.main()
