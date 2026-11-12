# -*- coding: utf-8 -*-
"""Product audit: Python pricing math, scoring/decision, JSON parsing and the
API run (model calls mocked).

Run with:  python -m unittest -v test_product_audit
"""
import json
import time
import unittest
from unittest import mock

import test_competitor as tc  # sets the test DB env before db is imported
import ai_client
import product_audit as pa
from db import db

FACTS = {
    "name_ar": "مصباح ليلي", "name_en": "motion sensor night light", "niche": "حل مشكلة يومية",
    "track": "imported", "sensitive_cargo": False, "fragile": False,
    "purchase_price": {"amount": 5, "currency": "USD", "source": "AliExpress"},
    "weight_g": 200, "dims_cm": [10, 8, 5], "competitor_price_dzd": 5900,
    "research": {"summary": "ok", "avg_rating": 4.3, "authenticity": "real",
                 "top_defect": "battery", "sources": ["https://reddit.com/x"]},
    "kill_gate": None, "pain_score": 25, "perceived_value": "", "offer": "2-Pack",
    "meta_keywords": ["ضوء"], "wholesale_exit_price_dzd": 1500, "one_line_reason": "حقيقي ومطلوب",
}


class MathTest(unittest.TestCase):
    def test_logistics_and_landed(self):
        c = pa.evaluate(FACTS)
        self.assertEqual(c["logistics"]["volumetric_g"], 80)
        self.assertEqual(c["logistics"]["billed_g"], 200)
        self.assertEqual(c["logistics"]["shipping_dzd"], 960)       # 0.2 kg × 4800
        self.assertEqual(c["landed_dzd"], 2210)                       # 5$ × 250 + 960
        # break-even = 2210 + 100 + 200 (return burden) + 595.24 (ads) = 3105
        self.assertEqual(c["scenarios"]["break_even"], 3105)

    def test_volumetric_wins_and_sensitive_rate(self):
        log = pa.logistics(100, [30, 20, 10], "imported", sensitive=True)
        self.assertEqual(log["billed_g"], 1200)
        self.assertEqual(log["shipping_dzd"], 7800)  # 1.2 × 6500

    def test_scenarios_reach_their_targets(self):
        for track in ("imported", "local"):
            sc = pa.scenarios(800 if track == "local" else 2500, track)
            for key, target in zip(("economic", "balanced", "high"), pa.TARGETS[track]):
                self.assertGreaterEqual(sc[key]["profit"], target, (track, key))
                self.assertEqual(sc[key]["price"] % 100, 0)
            self.assertLess(sc["economic"]["price"], sc["balanced"]["price"])
            self.assertLess(sc["balanced"]["price"], sc["high"]["price"])

    def test_max_cpa_formula(self):
        c = pa.unit_costs(2210, "imported")
        m = pa.at_price(5900, c)
        expected = (5900 - 2210 - 100 - 200) * 0.42 / 250
        self.assertAlmostEqual(m["max_cpa"], round(expected, 2))

    def test_local_track_has_no_air_freight(self):
        c = pa.evaluate(dict(FACTS, track="local", purchase_price={"amount": 600, "currency": "DZD"}))
        self.assertEqual(c["logistics"]["shipping_dzd"], 0)
        self.assertEqual(c["landed_dzd"], 600)
        self.assertEqual(c["scenarios"]["costs"]["dr"], 0.70)

    def test_green_decision(self):
        c = pa.evaluate(FACTS)
        self.assertEqual(c["scores"], {"pain": 25, "authenticity": 25, "logistics": 25, "margin": 25})
        self.assertEqual(c["decision"], "green")

    def test_kill_gate_forces_red(self):
        c = pa.evaluate(dict(FACTS, kill_gate="وهم كيميائي: بخاخ خدوش"))
        self.assertEqual(c["decision"], "red")
        self.assertLessEqual(c["total"], 59)
        c = pa.evaluate(dict(FACTS, research=dict(FACTS["research"], authenticity="fake")))
        self.assertEqual(c["decision"], "red")
        self.assertTrue(c["kill_gate"])

    def test_missing_values_are_flagged(self):
        c = pa.evaluate({"name_ar": "x", "track": "imported"})
        self.assertIn("الوزن", c["assumed"])
        self.assertIn("سعر الشراء", c["assumed"])

    def test_parse_json_with_fences_and_prose(self):
        self.assertEqual(pa.parse_json('Here:\n```json\n{"a": {"b": "}"}}\n```'), {"a": {"b": "}"}})
        self.assertEqual(pa.parse_json('ok {"x": 1} trailing'), {"x": 1})
        with self.assertRaises(ValueError):
            pa.parse_json("no json")

    def test_render_table(self):
        rows = [{"product": {"name_ar": "مصباح"}, "facts": FACTS, "calc": pa.evaluate(FACTS)},
                {"product": {"name_ar": "بخاخ"}, "error": "HTTP 500"}]
        md = pa.render("shop.youcan.store", rows, web=True)
        self.assertIn("| # | المنتج | القرار", md)
        self.assertIn("🟢 اختبر فوراً", md)
        self.assertIn("تعذر التدقيق", md)
        self.assertIn("متوازن ⭐", md)


class AuditApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import app as app_module
        cls.app = app_module.app

    def setUp(self):
        tc._clean()
        from werkzeug.security import generate_password_hash
        db.create_user("admin@test.dz", generate_password_hash("pass-12345"), role="admin")
        for i in range(3):
            db.save_ad(tc._ad(f"s{i}", "shiba.youcan.store", f"مصباح ليلي بـ 5900 دج نص {i}"),
                       query="q", country="DZ")

    def test_audit_runs_and_saves_table(self):
        c = self.app.test_client()
        c.post("/api/auth/login", json={"email": "admin@test.dz", "password": "pass-12345"})
        c.post("/api/ai/config", json={"provider": "openrouter", "api_key": "sk-test-key-123456",
                                       "model": "m/x", "audit_max_products": 2})
        ident = {"products": [
            {"name_ar": "مصباح ليلي", "name_en": "night light", "paths": ["/products/lamp"],
             "text_ids": ["T1"], "competitor_price_dzd": 5900, "evidence": "active"},
            {"name_ar": "بخاخ خدوش", "name_en": "scratch remover spray", "paths": [], "text_ids": [],
             "competitor_price_dzd": None, "evidence": "test"}]}
        bad = dict(FACTS, name_ar="بخاخ خدوش", kill_gate="وهم كيميائي للسيارات")

        def fake_chat(cfg, messages, max_tokens=None, timeout=300, web_search=0):
            user = messages[-1]["content"]
            if "DOSSIER START" in user:
                text = json.dumps(ident, ensure_ascii=False)
            elif "scratch" in user:
                self.assertEqual(web_search, 6)
                text = "```json\n" + json.dumps(bad, ensure_ascii=False) + "\n```"
            else:
                text = json.dumps(FACTS, ensure_ascii=False)
            return {"text": text, "prompt_tokens": 100, "completion_tokens": 50, "cost": 0.01,
                    "finish_reason": "stop", "model": "m/x", "citations": ["https://amazon.com/r"]}

        with mock.patch.object(ai_client, "chat", side_effect=fake_chat) as chat:
            r = c.post("/api/analysis/run", json={"store": "shiba.youcan.store", "kind": "audit"}).get_json()
            self.assertEqual(r["kind"], "audit")
            for _ in range(100):
                row = c.get(f"/api/analysis/reports/{r['id']}").get_json()
                if row["status"] != "running":
                    break
                time.sleep(0.05)
        self.assertEqual(row["status"], "done", row)
        self.assertEqual(row["kind"], "audit")
        self.assertEqual(chat.call_count, 3)  # identify + 2 products
        self.assertEqual(row["prompt_tokens"], 300)
        md = row["report"]
        self.assertIn("🟢 اختبر فوراً", md)
        self.assertIn("🔴 ارمه في البحر", md)
        # green product is listed before the killed one
        self.assertLess(md.index("مصباح ليلي"), md.index("بخاخ خدوش"))
        listed = c.get("/api/analysis/reports").get_json()["reports"]
        self.assertEqual(listed[0]["kind"], "audit")


if __name__ == "__main__":
    unittest.main()
