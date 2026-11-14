# -*- coding: utf-8 -*-
"""User categories: nested, many per item, private to each user.

Run with:  python -m unittest -v test_categories
"""
import unittest

import test_competitor as tc  # sets the test DB env before db is imported
from db import db


class CategoriesTest(unittest.TestCase):
    def setUp(self):
        tc._clean()
        for t in ("categories", "category_items", "targets", "user_targets", "winning_products"):
            db._execute(f"DELETE FROM {t}")
        from werkzeug.security import generate_password_hash
        db.create_user("a@test.dz", generate_password_hash("pass-12345"), role="admin")
        db.create_user("b@test.dz", generate_password_hash("pass-12345"), role="user")
        self.a = int(db._query_one("SELECT id FROM users WHERE email='a@test.dz'")["id"])
        self.b = int(db._query_one("SELECT id FROM users WHERE email='b@test.dz'")["id"])

    def test_nested_and_many_per_item(self):
        kitchen = db.create_category(self.a, "مطبخ", "#059669")
        tools = db.create_category(self.a, "أدوات", parent_id=kitchen)
        knives = db.create_category(self.a, "سكاكين", parent_id=tools)
        hot = db.create_category(self.a, "منافسون خطيرون")
        db.set_item_categories(self.a, "target", 7, [knives, hot])
        d = db.list_categories(self.a)
        self.assertEqual(sorted(d["items"]["target"]["7"]), sorted([knives, hot]))
        self.assertEqual(db._category_descendants(self.a, kitchen), [kitchen, tools, knives])
        # replace, not append
        db.set_item_categories(self.a, "target", 7, [hot])
        self.assertEqual(db.list_categories(self.a)["items"]["target"]["7"], [hot])

    def test_depth_and_cycles(self):
        c1 = db.create_category(self.a, "1")
        c2 = db.create_category(self.a, "2", parent_id=c1)
        c3 = db.create_category(self.a, "3", parent_id=c2)
        c4 = db.create_category(self.a, "4", parent_id=c3)
        with self.assertRaises(ValueError):
            db.create_category(self.a, "5", parent_id=c4)  # 5 levels
        with self.assertRaises(ValueError):
            db.update_category(self.a, c1, parent_id=c3)   # into its own branch

    def test_delete_removes_branch_but_not_items(self):
        top = db.create_category(self.a, "top")
        sub = db.create_category(self.a, "sub", parent_id=top)
        db.set_item_categories(self.a, "winning", 3, [sub])
        self.assertEqual(db.delete_category(self.a, top), 2)
        self.assertEqual(db.list_categories(self.a), {"categories": [], "items": {"target": {}, "winning": {}}})

    def test_private_to_each_user(self):
        mine = db.create_category(self.a, "خاص بي")
        self.assertEqual(db.list_categories(self.b)["categories"], [])
        db.set_item_categories(self.b, "target", 7, [mine])  # not B's category: ignored
        self.assertEqual(db.list_categories(self.a)["items"]["target"], {})
        with self.assertRaises(ValueError):
            db.update_category(self.b, mine, name="x")
        self.assertEqual(db.delete_category(self.b, mine), 0)

    def test_api_only_for_followed_items(self):
        import app as app_module
        c = app_module.app.test_client()
        c.post("/api/auth/login", json={"email": "b@test.dz", "password": "pass-12345"})
        cid = c.post("/api/categories", json={"name": "مطبخ"}).get_json()["id"]
        sub = c.post("/api/categories", json={"name": "أواني", "parent_id": cid}).get_json()["id"]
        self.assertEqual(c.post("/api/categories", json={"name": " "}).status_code, 400)
        # B does not follow target 99 → refused
        r = c.put("/api/categories/assign", json={"kind": "target", "item_id": 99, "category_ids": [cid]})
        self.assertEqual(r.status_code, 404)
        db.add_target(type="page", target_value="555", country="DZ")
        tid = int(db._query_one("SELECT id FROM targets WHERE target_value='555'")["id"])
        db._execute("INSERT INTO user_targets (user_id, target_id) VALUES (?, ?)", (self.b, tid))
        r = c.put("/api/categories/assign", json={"kind": "target", "item_id": tid, "category_ids": [sub, cid]}).get_json()
        self.assertEqual(sorted(r["category_ids"]), sorted([sub, cid]))
        d = c.get("/api/categories").get_json()
        self.assertEqual(len(d["categories"]), 2)
        self.assertEqual(c.delete(f"/api/categories/{cid}").get_json()["deleted"], 2)


if __name__ == "__main__":
    unittest.main()
