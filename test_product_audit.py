# -*- coding: utf-8 -*-
"""Product audit: Python pricing math, scoring/decision, JSON parsing and the
API run (model calls mocked).

Run with:  python -m unittest -v test_product_audit
"""
import json
import time
import unittest
from datetime import date, timedelta
from unittest import mock

import test_competitor as tc  # sets the test DB env before db is imported
import ai_client
import competitor
import price_fetch
import product_audit as pa
from db import db

WINNER = {"lifecycle": {"stage": "running_winner", "label": "✅ رابح مستمر", "market_score": 35}}

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
        for track, tier in (("imported", "high"), ("local", "low"), ("imported", "low")):
            sc = pa.scenarios(800 if tier == "low" else 2500, track, tier)
            for key, target in zip(("economic", "balanced", "high"), pa.TARGETS[tier]):
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
        c = pa.evaluate(FACTS, WINNER)
        self.assertEqual(c["scores"], {"market": 35, "margin": 20, "pain": 15, "quality": 15, "logistics": 10})
        self.assertEqual(c["decision"], "green")

    def test_market_proof_carries_an_average_product(self):
        """A gadget with doubtful reviews that the market bought for weeks is not red."""
        gadget = dict(FACTS, pain_score=8, research=dict(FACTS["research"], authenticity="doubtful"),
                      weight_g=600, dims_cm=[28, 22, 6], competitor_price_dzd=None)
        finished = {"lifecycle": {"stage": "finished_winner", "label": "🏁 رابح انتهى", "market_score": 32}}
        c = pa.evaluate(gadget, finished)
        self.assertIn(c["decision"], ("yellow", "green"), c["scores"])
        failed = {"lifecycle": {"stage": "failed_test", "label": "❌", "market_score": 0}}
        self.assertIn(pa.evaluate(gadget, failed)["decision"], ("orange", "red"))

    def test_low_ticket_tier_from_market_price(self):
        pen = dict(FACTS, purchase_price={"amount": 1.2, "currency": "USD"}, weight_g=60,
                   dims_cm=[16, 3, 3], competitor_price_dzd=None)
        c = pa.evaluate(pen, {"price": 2500, "lifecycle": {"market_score": 26}})
        self.assertEqual(c["tier"], "low")
        self.assertLess(c["scenarios"]["balanced"]["price"], 3000)
        self.assertGreater(c["market"]["profit"], 400)  # 2,500 DZD pays at air freight
        self.assertEqual(c["market_price_source"], "page/manual")

    def test_cheaper_local_source_wins(self):
        heavy = dict(FACTS, weight_g=450, sensitive_cargo=True, local_wholesale_dzd=700)
        c = pa.evaluate(heavy)
        self.assertEqual(c["source"], "local")
        self.assertEqual(c["landed_dzd"], 700)
        self.assertGreater(c["routes"]["imported"], 3000)  # 450 g × 6,500 + 1,250

    def test_kill_gate_forces_red(self):
        c = pa.evaluate(dict(FACTS, kill_gate="وهم كيميائي: بخاخ خدوش"), WINNER)
        self.assertEqual(c["decision"], "red")
        self.assertLessEqual(c["total"], 39)
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
        rows = [{"product": {"name_ar": "مصباح"}, "facts": FACTS, "calc": pa.evaluate(FACTS, WINNER)},
                {"product": {"name_ar": "بخاخ"}, "error": "HTTP 500"}]
        md = pa.render("shop.youcan.store", rows, web=True)
        self.assertIn("| # | المنتج | حالة السوق | القرار", md)
        self.assertIn("✅ رابح مستمر", md)
        self.assertIn("🟢 اختبر فوراً", md)
        self.assertIn("تعذر التدقيق", md)
        self.assertIn("متوازن ⭐", md)


TODAY = date(2026, 11, 13)


def _lad(start_days_ago, run_days, active=False, variants=1):
    st = TODAY - timedelta(days=start_days_ago)
    end = st + timedelta(days=run_days - 1)
    return {"start_date": st.isoformat(), "days_active": run_days, "is_active": 1 if active else 0,
            "stopped_at": None if active else end.isoformat(), "collation_count": variants}


class LifecycleTest(unittest.TestCase):
    """Cases from a real store (bxrrt.com) checked against the owner's live data."""

    def stage(self, ads):
        return competitor.product_lifecycle(ads, TODAY)["stage"]

    def test_pen_short_strong_test_is_a_likely_success(self):
        # 7 ads in 3 days, stopped 3 days ago: tested OK, waiting for stock.
        ads = [_lad(5, 3) for _ in range(7)]
        lc = competitor.product_lifecycle(ads, TODAY)
        self.assertEqual(lc["stage"], "test_success_waiting")
        self.assertEqual(lc["stopped_days_ago"], 3)  # ran T-5..T-3
        self.assertGreater(lc["market_score"], 20)

    def test_same_test_without_return_after_weeks(self):
        self.assertEqual(self.stage([_lad(40, 3) for _ in range(7)]), "test_no_return")

    def test_screen_long_run_then_stop_is_a_finished_winner(self):
        self.assertEqual(self.stage([_lad(90, 77 - i) for i in range(8)]), "finished_winner")

    def test_many_ads_29_days_is_a_finished_winner(self):
        self.assertEqual(self.stage([_lad(40, 29, variants=2) for _ in range(18)]), "finished_winner")

    def test_one_ad_long_run_is_small(self):
        self.assertEqual(self.stage([_lad(60, 29)]), "finished_small")

    def test_failed_test(self):
        self.assertEqual(self.stage([_lad(30, 2), _lad(30, 3)]), "failed_test")

    def test_relaunch_after_restock(self):
        ads = [_lad(20, 3) for _ in range(5)] + [_lad(6, 6, active=True) for _ in range(3)]
        lc = competitor.product_lifecycle(ads, TODAY)
        self.assertEqual(lc["stage"], "relaunched")
        self.assertGreaterEqual(lc["relaunch_gap_days"], 5)
        self.assertEqual(lc["market_score"], 40)

    def test_scaling_running_and_new(self):
        self.assertEqual(self.stage([_lad(19, 19, active=True) for _ in range(10)]
                                    + [_lad(3, 3, active=True) for _ in range(5)]), "scaling")
        self.assertEqual(self.stage([_lad(83, 83, active=True), _lad(40, 40, active=True)]), "running_winner")
        self.assertEqual(self.stage([_lad(2, 2, active=True)]), "new_test")

    def test_dossier_ranks_products_by_market_proof_and_keeps_prices(self):
        ads = []
        for i in range(7):
            ads.append(dict(_lad(5, 3), ad_id=f"p{i}", body=f"قلم {i}", link_url="https://s.com/pen"))
        ads.append(dict(_lad(30, 2), ad_id="x", body="فاشل", link_url="https://s.com/bad"))
        d = competitor.build_dossier(ads, {"host": "s.com", "domains": ["s.com"]}, today=TODAY,
                                     prices={"/pen": {"price": 2500, "source": "manual"}})
        self.assertEqual(d["products"][0]["path"], "/pen")
        self.assertEqual(d["products"][0]["price_dzd"], 2500)
        txt = competitor.dossier_text(d)
        self.assertIn("🧪 تست ناجح محتمل", txt)
        self.assertIn("2500 (manual)", txt)


class PriceExtractTest(unittest.TestCase):
    def test_sources(self):
        self.assertEqual(price_fetch.extract_price(
            '<script type="application/ld+json">{"@type":"Product","offers":[{"price":"2900.00"}]}</script>'),
            (2900, "json-ld"))
        self.assertEqual(price_fetch.extract_price('<meta property="og:price:amount" content="4,500">'),
                         (4500, "meta"))
        self.assertEqual(price_fetch.extract_price(
            '<b>3 900 دج</b> <s>5 900 دج</s> <span>3 900 DA</span>'), (3900, "text"))
        self.assertEqual(price_fetch.extract_price("<p>nothing</p>"), (None, ""))

    def test_challenge_page_is_blocked_not_bypassed(self):
        page = price_fetch._Resp(403, "<title>Just a moment...</title>")
        with mock.patch.object(price_fetch, "_get", return_value=page) as get:
            r = price_fetch.fetch_price("https://protected.example/x")
        self.assertEqual(r["status"], "blocked")
        self.assertIsNone(r["price"])
        self.assertEqual(get.call_count, 1)  # no retries, no other tricks


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


class GeminiExportTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import app as app_module
        cls.app = app_module.app

    def setUp(self):
        tc._clean()
        from werkzeug.security import generate_password_hash
        db.create_user("admin@test.dz", generate_password_hash("pass-12345"), role="admin")
        for i in range(130):  # more than the API max_texts default (120)
            db.save_ad(tc._ad(f"g{i}", "shiba.youcan.store", f"منتج رقم {i} بـ 2900 دج",
                              link_path=f"/products/p{i % 3}"), query="q", country="DZ")
        self.c = self.app.test_client()
        self.c.post("/api/auth/login", json={"email": "admin@test.dz", "password": "pass-12345"})

    def test_export_has_system_all_texts_and_links_without_ai_key(self):
        d = self.c.post("/api/analysis/export", json={"store": "shiba.youcan.store", "note": "ركز على المطبخ"}).get_json()
        self.assertIn("COD ALGERIA MASTER RISK", d["system"])
        self.assertIn("جدول القرارات", d["system"])
        self.assertEqual(d["unique_texts"], 130)
        self.assertIn("[T130]", d["data"])  # nothing cut
        self.assertIn("https://shiba.youcan.store/products/p1", d["data"])
        self.assertNotIn("utm_source", d["data"])
        self.assertIn("ركز على المطبخ", d["data"])
        self.assertIn("MARKET PROOF", d["data"])
        self.assertIn("الإعلان المتوقف ليس منتجاً فاشلاً", d["system"])
        d2 = self.c.post("/api/analysis/export", json={
            "store": "shiba.youcan.store", "prices": {"/products/p1": {"price": 2900, "source": "manual"}}}).get_json()
        self.assertIn("2900 (manual)", d2["data"])
        self.assertTrue(d["filename"].startswith("shiba.youcan.store-"))

    def test_prices_endpoint_reports_each_path(self):
        fake = [{"url": "https://shiba.youcan.store/products/p0", "price": 2900, "status": "ok", "how": "meta"},
                {"url": "https://shiba.youcan.store/products/p1", "price": None, "status": "blocked"}]
        with mock.patch.object(price_fetch, "fetch_prices", return_value=fake):
            d = self.c.post("/api/analysis/prices", json={"store": "shiba.youcan.store",
                                                          "paths": ["/products/p0", "/products/p1"]}).get_json()
        by = {x["path"]: x for x in d["prices"]}
        self.assertEqual(by["/products/p0"]["price"], 2900)
        self.assertEqual(by["/products/p1"]["status"], "blocked")

    def test_preview_lists_products_with_lifecycle(self):
        with mock.patch.object(ai_client, "model_info", return_value=None):
            p = self.c.post("/api/analysis/preview", json={"store": "shiba.youcan.store"}).get_json()
        self.assertEqual(len(p["products"]), 3)
        self.assertTrue(all(x["label"] and x["url"].startswith("https://") for x in p["products"]))

    def test_export_unknown_store(self):
        r = self.c.post("/api/analysis/export", json={"store": "nothing-here.youcan.store"})
        self.assertEqual(r.status_code, 404)

    def test_import_saves_report_and_is_not_counted_in_quota(self):
        r = self.c.post("/api/analysis/import", json={"store": "https://shiba.youcan.store/x",
                                                       "report": "| # | المنتج |\n|---|---|\n| 1 | مصباح |"}).get_json()
        row = self.c.get(f"/api/analysis/reports/{r['id']}").get_json()
        self.assertEqual((row["status"], row["kind"], row["provider"]), ("done", "audit", "manual"))
        self.assertIn("مصباح", row["report"])
        uid = db.get_ai_report(r["id"])["user_id"]
        self.assertEqual(db.count_ai_reports_since(uid, "2000-01-01"), 0)
        self.assertEqual(self.c.post("/api/analysis/import", json={"store": "shiba.youcan.store",
                                                                  "report": "x"}).status_code, 400)


if __name__ == "__main__":
    unittest.main()
