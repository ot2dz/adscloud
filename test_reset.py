# -*- coding: utf-8 -*-
"""Selective database reset: only the chosen sections of ads are deleted.

Run with:  python -m unittest -v test_reset
"""
import unittest

import test_competitor as tc  # sets the test DB env before db is imported
from db import db


def _ad(ad_id, page_id="900", domain="plain.youcan.store", path="/p", body="نص"):
    a = tc._ad(ad_id, domain, body, link_path=path, page_id=page_id)
    if not domain:
        a["store_domain"] = ""
        a["link_url"] = ""
    return a


class ResetTest(unittest.TestCase):
    def setUp(self):
        tc._clean()
        for t in ("targets", "user_targets", "winning_products", "tracked_pages"):
            db._execute(f"DELETE FROM {t}")
        from werkzeug.security import generate_password_hash
        db.create_user("admin@test.dz", generate_password_hash("pass-12345"), role="admin")
        self.uid = int(db._query_one("SELECT id FROM users WHERE email='admin@test.dz'")["id"])
        # plain bank ads (one with no page, store or link at all)
        db.save_ad(_ad("bank1", body="عرض أول"), query="", country="DZ")
        db.save_ad(_ad("bank2", page_id="", domain="", body="عرض ثاني"), query="", country="DZ")
        # a competitor tracked by page, one by store domain
        db.add_target(type="page", target_value="777", country="DZ")
        db.save_ad(_ad("comp_page", page_id="777", domain="rival.youcan.store", body="عرض المنافس"), query="", country="DZ")
        db.add_target(type="domain", target_value="shop.rival.dz", country="DZ")
        db.save_ad(_ad("comp_dom", page_id="778", domain="shop.rival.dz", body="عرض المتجر"), query="", country="DZ")
        # a tracked keyword
        db.add_target(type="keyword", target_value="مكنسة", country="DZ")
        db.save_ad(_ad("kw1", page_id="901", domain="kw.youcan.store", body="مكنسة بخار"), query="مكنسة", country="DZ")
        # a followed winning product, and one that is also a tracked competitor's
        db.save_ad(_ad("win1", page_id="902", domain="win.youcan.store", path="/lamp", body="مصباح ليلي"),
                   query="", country="DZ")
        db.add_winning_product(self.uid, "win1")
        db.save_ad(_ad("both", page_id="777", domain="rival.youcan.store", path="/x", body="عرض مزدوج"),
                   query="", country="DZ")
        db.add_winning_product(self.uid, "both")
        # foreign market
        db.save_ad(_ad("glob1", page_id="903", domain="us.myshopify.com", body="عرض خارجي"),
                   query="", country="US", market="global")

    def ids(self):
        return {r["ad_id"] for r in db._query("SELECT ad_id FROM meta_ads")}

    def test_counts_per_section(self):
        c = db.reset_counts({"bank": True})
        self.assertEqual(c["bank"], 2)
        self.assertEqual(c["competitors"], 3)   # comp_page, comp_dom, both
        self.assertEqual(c["keywords"], 1)
        self.assertEqual(c["winning"], 2)       # win1, both
        self.assertEqual(c["global"], 1)
        self.assertEqual(c["to_delete"], 2)

    def test_bank_only_keeps_everything_else(self):
        self.assertEqual(db.reset_ads({"bank": True}), 2)
        self.assertEqual(self.ids(), {"comp_page", "comp_dom", "kw1", "win1", "both", "glob1"})
        # tags of the deleted ads are gone too, lists stay
        self.assertFalse(db._query("SELECT ad_id FROM ad_keywords WHERE ad_id IN ('bank1', 'bank2')"))
        self.assertEqual(len(db._query("SELECT id FROM targets")), 3)
        self.assertEqual(len(db._query("SELECT id FROM winning_products")), 2)

    def test_an_ad_in_two_sections_needs_both_chosen(self):
        db.reset_ads({"competitors": True})
        self.assertIn("both", self.ids())       # still a followed winning product
        self.assertNotIn("comp_page", self.ids())
        db.reset_ads({"competitors": True, "winning": True})
        self.assertNotIn("both", self.ids())

    def test_global_alone(self):
        self.assertEqual(db.reset_ads({"global": True}), 1)
        self.assertNotIn("glob1", self.ids())
        self.assertIn("bank1", self.ids())

    def test_nothing_chosen_deletes_nothing(self):
        self.assertEqual(db.reset_ads({}), 0)
        self.assertEqual(len(self.ids()), 8)

    def test_api(self):
        import app as app_module
        c = app_module.app.test_client()
        c.post("/api/auth/login", json={"email": "admin@test.dz", "password": "pass-12345"})
        p = c.post("/api/reset/preview", json={"bank": True}).get_json()
        self.assertEqual(p["to_delete"], 2)
        self.assertEqual(c.post("/api/reset", json={}).status_code, 400)
        r = c.post("/api/reset", json={"bank": True}).get_json()
        self.assertEqual(r["deleted"], 2)
        self.assertEqual(len(self.ids()), 6)


if __name__ == "__main__":
    unittest.main()
