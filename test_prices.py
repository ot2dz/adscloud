# -*- coding: utf-8 -*-
"""Product prices: queue, saved results, filter and order.

Run with:  python -m unittest -v test_prices
"""
import unittest
from unittest import mock

import test_competitor as tc  # sets the test DB env before db is imported
import app as webapp
import price_fetch
from db import db
from web import prices
from werkzeug.security import generate_password_hash


def _save(ad_id, domain, path):
    a = tc._ad(ad_id, domain, "عرض منتج", link_path=path, page_id="9" + ad_id)
    a["is_active"] = True
    db.save_ad(a, query="", country="DZ")
    return db._query_one("SELECT product_key FROM meta_ads WHERE ad_id = ?", (ad_id,))["product_key"]


class PricesTest(unittest.TestCase):
    def setUp(self):
        tc._clean()
        db._execute("DELETE FROM product_prices")
        webapp.app.config["SESSION_COOKIE_SECURE"] = False
        for name, role in (("pa", "admin"), ("pu", "user")):
            if not db.get_user_by_email(f"{name}@test.dz"):
                db.create_user(f"{name}@test.dz", generate_password_hash("pass-12345"), role=role)
        self.c = self.login("pa")
        self.k = [_save("1", "a.youcan.store", "/p1"), _save("2", "a.youcan.store", "/p2"),
                  _save("3", "a.youcan.store", "/p3"), _save("4", "b.youcan.store", "/p4")]

    def login(self, name):
        c = webapp.app.test_client()
        r = c.post("/api/auth/login", json={"email": f"{name}@test.dz", "password": "pass-12345"})
        self.assertEqual(r.status_code, 200, r.data)
        return c

    def post(self, *rs):
        return self.c.post("/api/prices/results", json={"results": list(rs)}).get_json()

    def queue(self):
        return [i["key"] for i in self.c.get("/api/prices/queue").get_json()["items"]]

    def listed(self, args):
        out = []
        for g in ("0", "1"):
            d = self.c.get(f"/api/ads?group={g}&{args}").get_json()
            self.assertEqual(d["total"], len(d["ads"]))
            out.append([a["price_dzd"] for a in d["ads"]])
        self.assertEqual(out[0], out[1])
        return out[0]

    def test_queue_spreads_stores_and_drops_read_products(self):
        with mock.patch.object(price_fetch, "_get", side_effect=AssertionError("server fetch")):
            q = self.queue()
        self.assertEqual(len(q), 3)            # two of store a, one of b
        self.post({"key": q[0], "status": "ok", "price": 2900, "how": "meta"})
        self.assertNotIn(q[0], self.queue())

    def test_blocked_store_is_left_alone(self):
        self.post({"key": self.k[0], "status": "blocked"})
        self.assertEqual(self.queue(), [self.k[3]])

    def test_manual_price_wins_and_failed_read_keeps_old(self):
        self.post({"key": self.k[0], "status": "ok", "price": 3000})
        self.post({"key": self.k[0], "status": "error"})
        self.assertEqual(db.attach_prices([{"product_key": self.k[0]}])[0]["price_dzd"], 3000)
        self.c.post("/api/prices/manual", json={"key": self.k[0], "price": "4100"})
        self.post({"key": self.k[0], "status": "ok", "price": 9999})
        self.assertEqual(db.attach_prices([{"product_key": self.k[0]}])[0]["price_dzd"], 4100)
        self.assertEqual(self.c.post("/api/prices/manual", json={"key": self.k[0], "price": ""}).status_code, 200)
        self.assertEqual(db._query("SELECT * FROM product_prices"), [])

    def test_filter_and_order(self):
        for k, v in zip(self.k, (1500, 3500, 8000)):
            self.post({"key": k, "status": "ok", "price": v})
        self.assertEqual(self.listed("sort_by=price_desc"), [8000, 3500, 1500, None])
        self.assertEqual(self.listed("sort_by=price_asc"), [1500, 3500, 8000, None])
        db.rebuild_group_summary()             # same order from the summary
        self.assertEqual(self.listed("sort_by=price_desc"), [8000, 3500, 1500, None])
        self.assertEqual(self.listed("price_min=5000"), [8000])
        self.assertEqual(self.listed("price_min=2000&price_max=5000"), [3500])
        self.assertEqual(self.listed("price_state=none"), [None])
        self.assertEqual(len(self.listed("price_state=has")), 3)
        self.assertEqual(self.listed("price_min=5000&min_product_ads=2"), [])

    def test_bad_input_and_roles(self):
        self.assertEqual(self.post({"key": "nope", "status": "ok", "price": 3000}, "x", {"status": []})["saved"], 0)
        self.post({"key": self.k[0], "status": "ok", "price": 5})
        self.assertIsNone(db.attach_prices([{"product_key": self.k[0]}])[0]["price_dzd"])
        u = self.login("pu")
        self.assertEqual(u.get("/api/prices/queue").status_code, 403)
        self.assertEqual(u.post("/api/prices/results", json={}).status_code, 403)
        self.assertEqual(u.post("/api/prices/manual", json={}).status_code, 403)

    def test_public_url(self):
        self.assertEqual(prices.public_url("https://a.dz/p?id=3&utm_source=x&fbclid=y#z"), "https://a.dz/p?id=3")
        for bad in ("http://127.0.0.1/a", "http://localhost/a", "http://[::1]/", "ftp://a.dz/", "http://127.1/",
                    "http://u:p@a.dz/", "http://a.dz:8787/rotate", "http://x.local/"):
            self.assertEqual(prices.public_url(bad), "", bad)


if __name__ == "__main__":
    unittest.main()
