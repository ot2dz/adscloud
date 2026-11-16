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


class CutAnswerTest(unittest.TestCase):
    """A model that stops mid-answer (token limit) must not lose the batch."""

    def setUp(self):
        NicheAITest.setUp(self)
        niche_ai._batch = niche_ai.BATCH
        niche_ai._tries.clear()

    niches_of = NicheAITest.niches_of

    def test_budget_and_salvage_of_a_cut_answer(self):
        k = self.slugs[0]
        calls = []

        def cut_chat(cfg, messages, max_tokens=None, **kw):
            calls.append(max_tokens)
            # first product complete, second cut in the middle
            text = '{"items":[{"id":"P1","niches":[{"slug":"%s","confidence":91}]},{"id":"P2","nich' % k
            return {"text": text, "prompt_tokens": 10, "completion_tokens": 10, "cost": 0.001,
                    "finish_reason": "length", "model": "m", "citations": []}
        with mock.patch.object(ai_client, "chat", side_effect=cut_chat):
            n = niche_ai.run_once(db)
        self.assertGreaterEqual(calls[0], 2 * niche_ai.TOKENS_PER_PRODUCT)  # generous budget
        self.assertEqual(n, 1)
        classified = db._query("SELECT DISTINCT product_key FROM meta_ads WHERE niche_ai_at IS NOT NULL")
        self.assertEqual(len(classified), 1)            # the complete item was saved
        self.assertEqual(niche_ai._batch, niche_ai.MIN_BATCH)  # next batch is smaller
        self.assertIn("1/2", db.get_settings()["ai_niche_error"])

    def test_a_product_that_always_fails_falls_back_to_rules(self):
        def empty_chat(cfg, messages, **kw):
            return {"text": '{"items":[]}', "prompt_tokens": 1, "completion_tokens": 1, "cost": 0,
                    "finish_reason": "stop", "model": "m", "citations": []}
        before = db._query("SELECT ad_id, niche_id FROM ad_niches WHERE method = 'rules' ORDER BY ad_id, niche_id")
        with mock.patch.object(ai_client, "chat", side_effect=empty_chat) as chat:
            for _ in range(niche_ai.MAX_TRIES):
                niche_ai.run_once(db)
            self.assertEqual(niche_ai.run_once(db), 0)  # nothing left to ask
            self.assertEqual(chat.call_count, niche_ai.MAX_TRIES)
        after = db._query("SELECT ad_id, niche_id FROM ad_niches WHERE method = 'rules' ORDER BY ad_id, niche_id")
        self.assertEqual([dict(r) for r in after], [dict(r) for r in before])  # keyword niches untouched
        self.assertEqual(db._query_one("SELECT COUNT(*) AS n FROM meta_ads WHERE niche_ai_at IS NULL")["n"], 0)


FREE = [{"id": "a/one:free", "price_in": 0, "price_out": 0, "output": ["text"]},
        {"id": "b/two:free", "price_in": 0, "price_out": 0, "output": ["text"]},
        {"id": "c/three:free", "price_in": 0, "price_out": 0, "output": ["text"]},
        {"id": "img/pic:free", "price_in": 0, "price_out": 0, "output": ["image"]},
        {"id": "paid/model", "price_in": 1.0, "price_out": 2.0, "output": ["text"]},
        {"id": "openrouter/free", "price_in": 0, "price_out": 0, "output": ["text"]}]


class FreeModelsTest(unittest.TestCase):
    """Every free OpenRouter model, one after another when one fails."""

    niches_of = NicheAITest.niches_of

    def setUp(self):
        NicheAITest.setUp(self)
        niche_ai._batch = niche_ai.BATCH
        niche_ai._tries.clear()
        niche_ai._rest.clear()
        db.set_setting("ai_niche_free", "1")
        db.set_setting("ai_niche_free_skip", "")
        db.set_setting("ai_niche_last_model", "")
        p = mock.patch.object(ai_client, "list_models", return_value=FREE)
        p.start()
        self.addCleanup(p.stop)

    def _chat(self, fail):
        """fail: {model: "429" | "error" | "empty"}; other models answer."""
        k, b = self.slugs[0], self.slugs[1]
        good = _by_text({"سكين": [k], "كريم": [b]})
        self.calls = []

        def chat(cfg, messages, **kw):
            self.calls.append(cfg["model"])
            how = fail.get(cfg["model"])
            if how == "429":
                raise ai_client.AIError("HTTP 429 from provider: rate limited")
            if how == "error":
                raise ai_client.AIError("HTTP 404 from provider: no endpoints")
            if how == "empty":
                return {"text": "sorry", "prompt_tokens": 1, "completion_tokens": 1, "cost": 0,
                        "finish_reason": "stop", "model": cfg["model"], "citations": []}
            return good(cfg, messages, **kw)
        return chat

    def test_chain_is_free_text_models_with_the_router_last(self):
        self.assertEqual(niche_ai.model_chain(niche_ai.config()),
                         ["a/one:free", "b/two:free", "c/three:free", "openrouter/free"])
        db.set_setting("ai_niche_free_skip", "b/two:free")
        db.set_setting("ai_niche_last_model", "c/three:free")
        self.assertEqual(niche_ai.model_chain(niche_ai.config()), ["c/three:free", "a/one:free", "openrouter/free"])

    def test_failed_models_rest_and_the_next_one_classifies(self):
        with mock.patch.object(ai_client, "chat", side_effect=self._chat({"a/one:free": "429", "b/two:free": "empty"})):
            self.assertEqual(niche_ai.run_once(db), 2)
        self.assertEqual(self.calls, ["a/one:free", "b/two:free", "c/three:free"])
        self.assertEqual(db.get_settings()["ai_niche_last_model"], "c/three:free")
        self.assertEqual(set(niche_ai.resting()), {"a/one:free", "b/two:free"})
        self.assertEqual({r["method"] for r in self.niches_of("a1")}, {"llm"})
        # next batch: the model that worked goes first, the resting ones are skipped
        db.save_ad(tc._ad("c1", "s3.youcan.store", "سكين آخر", link_path="/products/k2", page_id="702"),
                   query="q", country="DZ")
        with mock.patch.object(ai_client, "chat", side_effect=self._chat({})):
            self.assertEqual(niche_ai.run_once(db), 1)
        self.assertEqual(self.calls, ["c/three:free"])

    def test_every_model_failing_raises_and_keeps_the_products(self):
        fail = {m: "error" for m in ("a/one:free", "b/two:free", "c/three:free", "openrouter/free")}
        with mock.patch.object(ai_client, "chat", side_effect=self._chat(fail)):
            with self.assertRaises(ai_client.AIError):
                niche_ai.run_once(db)
            with self.assertRaises(ai_client.AIError):  # all resting now: no call at all
                niche_ai.run_once(db)
        self.assertEqual(len(self.calls), 4)
        self.assertEqual(db._query_one("SELECT COUNT(*) AS n FROM meta_ads WHERE niche_ai_at IS NULL")["n"], 3)

    def test_single_model_mode_is_unchanged(self):
        db.set_setting("ai_niche_free", "0")
        with mock.patch.object(ai_client, "chat", side_effect=self._chat({"analysis/model": "429"})):
            with self.assertRaises(ai_client.AIError):
                niche_ai.run_once(db)
        self.assertEqual(self.calls, ["analysis/model"])
        self.assertEqual(niche_ai.resting(), {})

    def test_api_saves_free_mode_and_skip_list(self):
        import app as app_module
        from werkzeug.security import generate_password_hash
        db.create_user("admin@test.dz", generate_password_hash("pass-12345"), role="admin")
        c = app_module.app.test_client()
        c.post("/api/auth/login", json={"email": "admin@test.dz", "password": "pass-12345"})
        d = c.post("/api/ai/niches", json={"free": True, "free_skip": ["a/one:free"]}).get_json()
        self.assertTrue(d["free"])
        self.assertEqual([m["id"] for m in d["free_models"] if not m["on"]], ["a/one:free"])
        self.assertEqual(d["effective_model"], "b/two:free")
        self.assertEqual(c.post("/api/ai/niches", json={"free_skip": "x"}).status_code, 400)


class ExtraProvidersTest(unittest.TestCase):
    """Gemini and Groq come before OpenRouter, each with its own key."""

    niches_of = NicheAITest.niches_of

    def setUp(self):
        FreeModelsTest.setUp(self)
        for name in niche_ai.EXTRA_PROVIDERS:
            for k in ("key", "models", "on"):
                db.set_setting(f"ai_niche_{name}_{k}", "")
        db.set_setting("ai_niche_gemini_key", ai_client.encrypt_secret("AIza-test"))
        db.set_setting("ai_niche_gemini_models", "gemini-flash,gemini-flash-lite")
        db.set_setting("ai_niche_groq_key", ai_client.encrypt_secret("gsk-test"))
        db.set_setting("ai_niche_groq_models", "gpt-oss-120b")

    _chat = FreeModelsTest._chat

    def test_chain_order_and_off_switch(self):
        self.assertEqual(niche_ai.model_chain(niche_ai.config())[:4],
                         ["gemini:gemini-flash", "gemini:gemini-flash-lite", "groq:gpt-oss-120b", "a/one:free"])
        db.set_setting("ai_niche_gemini_on", "0")
        db.set_setting("ai_niche_free", "0")
        self.assertEqual(niche_ai.model_chain(niche_ai.config()), ["groq:gpt-oss-120b", "analysis/model"])

    def test_each_provider_gets_its_own_url_key_and_model(self):
        seen = []
        k = self.slugs[0]
        good = _by_text({"سكين": [k], "كريم": [k]})

        def chat(cfg, messages, **kw):
            seen.append((cfg["base_url"], cfg["api_key"], cfg["model"]))
            if cfg["model"] == "gemini-flash":
                raise ai_client.AIError("HTTP 429 from provider: quota")
            return good(cfg, messages, **kw)
        with mock.patch.object(ai_client, "chat", side_effect=chat):
            self.assertEqual(niche_ai.run_once(db), 2)
        self.assertEqual(seen[0], (niche_ai.EXTRA_PROVIDERS["gemini"]["base_url"], "AIza-test", "gemini-flash"))
        self.assertEqual(seen[1], (niche_ai.EXTRA_PROVIDERS["gemini"]["base_url"], "AIza-test", "gemini-flash-lite"))
        self.assertEqual(db.get_settings()["ai_niche_last_model"], "gemini:gemini-flash-lite")

    def test_extra_provider_works_without_openrouter(self):
        db.set_setting("ai_niche_free", "0")
        db.set_setting("ai_api_key", "")
        db.set_setting("ai_niche_gemini_on", "0")
        with mock.patch.object(ai_client, "chat", side_effect=self._chat({})):
            self.assertEqual(niche_ai.run_once(db), 2)
        self.assertEqual(self.calls, ["groq:gpt-oss-120b".split(":", 1)[1]])

    def test_api_keys_are_encrypted_hidden_and_clearable(self):
        import app as app_module
        from werkzeug.security import generate_password_hash
        db.create_user("admin@test.dz", generate_password_hash("pass-12345"), role="admin")
        c = app_module.app.test_client()
        c.post("/api/auth/login", json={"email": "admin@test.dz", "password": "pass-12345"})
        d = c.post("/api/ai/niches", json={"groq_key": "gsk-new-secret-999", "groq_models": "m1, m2,m1",
                                            "groq_on": True}).get_json()
        self.assertNotIn("gsk-new-secret-999", json.dumps(d))
        self.assertNotEqual(db.get_settings()["ai_niche_groq_key"], "gsk-new-secret-999")
        self.assertEqual(niche_ai.config()["extra"]["groq"]["key"], "gsk-new-secret-999")
        self.assertEqual(d["providers"]["groq"]["models"], ["m1", "m2"])
        d = c.post("/api/ai/niches", json={"groq_clear": True}).get_json()
        self.assertFalse(d["providers"]["groq"]["key_set"])
        with mock.patch.object(ai_client, "list_models", return_value=[
                {"id": "models/gemini-x"}, {"id": "models/text-embedding-9"}]):
            r = c.post("/api/ai/niches/models", json={"provider": "gemini"}).get_json()
        self.assertEqual(r["models"], ["gemini-x"])
        self.assertEqual(c.post("/api/ai/niches/models", json={"provider": "x"}).status_code, 400)


if __name__ == "__main__":
    unittest.main()
