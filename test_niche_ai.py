# -*- coding: utf-8 -*-
"""AI niche classification: per product, only listed niches, wins over rules,
reused for later ads of the same product, daily limit, new scrapes only.

Run with:  python -m unittest -v test_niche_ai
"""
import json
import unittest
from unittest import mock

import test_competitor as tc  # sets the test DB env before db is imported
import ai_client
import niche_ai
from db import db


def _answer(*slugs_per_product):
    return {"text": json.dumps({"items": [
        {"id": f"P{i}", "niches": [{"slug": s, "confidence": 90} for s in slugs]}
        for i, slugs in enumerate(slugs_per_product, 1)]}),
        "prompt_tokens": 100, "completion_tokens": 20, "cost": 0.0012,
        "finish_reason": "stop", "model": "m/x", "citations": []}


def _by_text(rules):
    """A fake model that answers each [Pn] line from its text: {word: [slugs]}."""
    import re

    def chat(cfg, messages, **kw):
        user = messages[-1]["content"]
        items = []
        for m in re.finditer(r"\[P(\d+)\][^\n]*\n\s*(.*)", user):
            slugs = next((v for w, v in rules.items() if w in m.group(2)), [])
            items.append({"id": "P" + m.group(1), "niches": [{"slug": x, "confidence": 90} for x in slugs]})
        return {"text": json.dumps({"items": items}), "prompt_tokens": 100, "completion_tokens": 20,
                "cost": 0.0012, "finish_reason": "stop", "model": cfg["model"], "citations": []}
    return chat


class NicheAITest(unittest.TestCase):
    def setUp(self):
        tc._clean()
        for k in ("ai_niche_enabled", "ai_niche_since", "ai_niche_backfill", "ai_niche_daily",
                  "ai_niche_day", "ai_niche_done", "ai_niche_cost", "ai_niche_model"):
            db.set_setting(k, "")
        db.set_setting("ai_provider", "openrouter")
        db.set_setting("ai_api_key", ai_client.encrypt_secret("sk-or-test-123456"))
        db.set_setting("ai_model", "analysis/model")
        self.slugs = [n["slug"] for n in db.niche_list()][:3]
        # product 1: two ads of the same product page; product 2: one ad
        db.save_ad(tc._ad("a1", "s1.youcan.store", "سكين مطبخ حاد", link_path="/products/knife"), query="q", country="DZ")
        db.save_ad(tc._ad("a2", "s1.youcan.store", "سكين مطبخ عرض", link_path="/products/knife"), query="q", country="DZ")
        db.save_ad(tc._ad("b1", "s2.youcan.store", "كريم للوجه", link_path="/products/cream", page_id="701"),
                   query="q", country="DZ")
        db.set_setting("ai_niche_enabled", "1")
        db.set_setting("ai_niche_backfill", "1")

    def niches_of(self, ad_id):
        return db._query("SELECT n.slug, an.method FROM ad_niches an JOIN niches n ON n.id = an.niche_id "
                         f"WHERE an.ad_id = '{ad_id}'")

    def test_classifies_per_product_and_replaces_rules(self):
        k, b = self.slugs[0], self.slugs[1]
        with mock.patch.object(ai_client, "chat", side_effect=_by_text({"سكين": [k], "كريم": [b, "invented-slug"]})) as chat:
            n = niche_ai.run_once(db)
        self.assertEqual(n, 2)
        self.assertEqual(chat.call_count, 1)  # one batch for both products
        sent = chat.call_args[0][1][1]["content"]
        self.assertEqual(sent.count("[P"), 2)
        self.assertEqual(chat.call_args[0][0]["model"], "analysis/model")
        self.assertEqual({(r["slug"], r["method"]) for r in self.niches_of("a1")}, {(k, "llm")})
        self.assertEqual({(r["slug"], r["method"]) for r in self.niches_of("a2")}, {(k, "llm")})
        self.assertEqual([r["slug"] for r in self.niches_of("b1")], [b])  # invented slug dropped
        self.assertEqual(db._query_one("SELECT primary_niche FROM meta_ads WHERE ad_id='a1'")["primary_niche"], k)
        # a re-scan of the ad does not bring the keyword rules back
        db.save_ad(tc._ad("a1", "s1.youcan.store", "سكين مطبخ حاد", link_path="/products/knife"), query="q", country="DZ")
        self.assertEqual({r["method"] for r in self.niches_of("a1")}, {"llm"})
        self.assertEqual(db.get_settings()["ai_niche_done"], "2")

    def test_new_ad_of_known_product_is_free(self):
        k, b = self.slugs[0], self.slugs[1]
        with mock.patch.object(ai_client, "chat", side_effect=_by_text({"سكين": [k], "كريم": [b]})):
            niche_ai.run_once(db)
        db.save_ad(tc._ad("a3", "s1.youcan.store", "سكين جديد", link_path="/products/knife"), query="q", country="DZ")
        with mock.patch.object(ai_client, "chat") as chat:
            self.assertEqual(niche_ai.run_once(db), 1)
            chat.assert_not_called()
        self.assertEqual([r["slug"] for r in self.niches_of("a3")], [k])

    def test_separate_cheaper_model_and_daily_limit(self):
        db.set_setting("ai_niche_model", "cheap/model")
        db.set_setting("ai_niche_daily", "1")
        with mock.patch.object(ai_client, "chat", return_value=_answer([self.slugs[0]])) as chat:
            self.assertEqual(niche_ai.run_once(db), 1)
            self.assertEqual(chat.call_args[0][0]["model"], "cheap/model")
            self.assertEqual(niche_ai.run_once(db), 0)  # limit reached for today

    def test_new_scrapes_only_and_off_switch(self):
        db.set_setting("ai_niche_backfill", "0")
        db.set_setting("ai_niche_since", "2999-01-01T00:00:00+00:00")  # everything is older
        with mock.patch.object(ai_client, "chat") as chat:
            self.assertEqual(niche_ai.run_once(db), 0)
            chat.assert_not_called()
        db.set_setting("ai_niche_enabled", "0")
        db.set_setting("ai_niche_backfill", "1")
        with mock.patch.object(ai_client, "chat") as chat:
            self.assertEqual(niche_ai.run_once(db), 0)
            chat.assert_not_called()

    def test_parse_answer(self):
        valid = {"kitchen", "beauty"}
        out = niche_ai.parse_answer('```json\n{"items":[{"id":"P1","niches":[{"slug":"Kitchen","confidence":"88"}]},'
                                    '{"id":"P9","niches":[{"slug":"beauty"}]},{"id":"2","niches":[]}]}\n```', 2, valid)
        self.assertEqual(out, {1: [{"slug": "kitchen", "confidence": 88}], 2: []})

    def test_api(self):
        import app as app_module
        from werkzeug.security import generate_password_hash
        db.create_user("admin@test.dz", generate_password_hash("pass-12345"), role="admin")
        c = app_module.app.test_client()
        c.post("/api/auth/login", json={"email": "admin@test.dz", "password": "pass-12345"})
        db.set_setting("ai_niche_enabled", "0")
        db.set_setting("ai_niche_since", "")
        d = c.post("/api/ai/niches", json={"enabled": True, "model": "cheap/m", "daily": 500}).get_json()
        self.assertTrue(d["enabled"])
        self.assertTrue(db.get_settings()["ai_niche_since"])  # from now on only
        self.assertEqual(d["effective_model"], "cheap/m")
        self.assertEqual(c.post("/api/ai/niches", json={"daily": "x"}).status_code, 400)


if __name__ == "__main__":
    unittest.main()
