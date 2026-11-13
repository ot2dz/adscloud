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
    "name_ar": "سباتول الكوان", "name_en": "inside corner drywall trowel", "niche": "أداة بريكولاج",
    "fragile": False, "competitor_price_dzd": 3400,
    "research": {"summary": "ok", "avg_rating": 4.3, "authenticity": "real", "return_risk": "low",
                 "top_defect": "زوايا غير منتظمة", "sources": ["https://reddit.com/x"]},
    "kill_gate": None, "pain_score": 25, "perceived_value": "", "offer": "2-Pack",
    "search_1688": ["阴角抹子 不锈钢"], "meta_keywords": ["سباتول الكوان"], "one_line_reason": "حقيقي ومطلوب",
}
WINNER = {"lifecycle": {"stage": "scaling", "label": "🔥 يسكايل الآن", "market_score": 38}}
MY_COST = {"buy": 1, "cur": "USD", "weight_g": 170}  # the owner's real 1688 numbers


class MathTest(unittest.TestCase):
    def test_overhead_and_ceilings(self):
        # 100 confirmation + 200 return burden + 595 ads (1$ / 42%) per delivered order
        self.assertEqual(round(pa.overhead()), 895)
        self.assertEqual(pa.cost_ceilings(3400), {400: 2105, 800: 1705, 1500: 1005})
        self.assertIsNone(pa.cost_ceilings(None))

    def test_users_own_cost(self):
        mine = pa.landed_from_user(MY_COST, 4500)
        self.assertEqual((mine["buy_dzd"], mine["shipping_dzd"], mine["landed"]), (250, 765, 1015))
        self.assertEqual(pa.landed_from_user({"buy": 15, "cur": "RMB"}, 4500)["landed"], 600)  # no weight
        self.assertIsNone(pa.landed_from_user({"buy": ""}))

    def test_spatula_example(self):
        """3,400 DZD at the competitor, bought 1$ + 170 g at 4,500/kg → ~1,490 profit."""
        c = pa.evaluate(FACTS, dict(WINNER, price=3400, cost=MY_COST, kg_rate=4500))
        self.assertEqual(c["mine"]["landed"], 1015)
        self.assertEqual(c["market"]["profit"], 1490)
        self.assertEqual(c["margin_basis"], "real")
        self.assertEqual(c["tier"], "high")
        self.assertIsNotNone(c["scenarios"])
        self.assertEqual(c["decision"], "green")

    def test_no_cost_means_no_invented_numbers(self):
        c = pa.evaluate(FACTS, dict(WINNER, price=3400))
        self.assertIsNone(c["mine"])
        self.assertIsNone(c["scenarios"])
        self.assertIsNone(c["market"])
        self.assertEqual(c["margin_basis"], "ceiling")
        self.assertEqual(c["scores"]["margin"], 16)  # ceiling 1,705 ≥ 1,500
        self.assertEqual(c["scores"]["logistics"], pa.UNKNOWN_LOGISTICS_SCORE)

    def test_model_cost_guesses_are_ignored(self):
        guessed = dict(FACTS, purchase_price={"amount": 2.4, "currency": "USD"}, weight_g=288)
        self.assertIsNone(pa.evaluate(guessed, dict(WINNER, price=3400))["mine"])

    def test_scenarios_reach_their_targets(self):
        for tier in ("high", "low"):
            sc = pa.scenarios(1015, tier)
            for key, target in zip(("economic", "balanced", "high"), pa.TARGETS[tier]):
                self.assertGreaterEqual(sc[key]["profit"], target, (tier, key))
                self.assertEqual(sc[key]["price"] % 100, 0)

    def test_max_cpa_formula(self):
        m = pa.at_price(3400, pa.unit_costs(1015))
        self.assertAlmostEqual(m["max_cpa"], round((3400 - 1015 - 100 - 200) * 0.42 / 250, 2))

    def test_market_proof_carries_an_average_product(self):
        gadget = dict(FACTS, pain_score=8, research=dict(FACTS["research"], authenticity="doubtful"))
        finished = {"lifecycle": {"stage": "finished_winner", "market_score": 32}, "price": 5200}
        self.assertIn(pa.evaluate(gadget, finished)["decision"], ("yellow", "green"))
        failed = {"lifecycle": {"stage": "failed_test", "market_score": 0}, "price": 5200}
        self.assertIn(pa.evaluate(gadget, failed)["decision"], ("orange", "red"))

    def test_kill_gate_forces_red(self):
        c = pa.evaluate(dict(FACTS, kill_gate="وهم كيميائي: بخاخ خدوش"), WINNER)
        self.assertEqual(c["decision"], "red")
        self.assertLessEqual(c["total"], 39)
        c = pa.evaluate(dict(FACTS, research=dict(FACTS["research"], authenticity="fake")), WINNER)
        self.assertEqual(c["decision"], "red")
        self.assertTrue(c["kill_gate"])

    def test_cost_line_for_the_dossier(self):
        line = pa.cost_line(3400, MY_COST, 4500)
        self.assertIn("800: 1705", line)
        self.assertIn("landed 1015", line)
        self.assertIn("profit at competitor price 1490", line)
        self.assertEqual(pa.cost_line(None, None, 4500), "")

    def test_parse_json_with_fences_and_prose(self):
        self.assertEqual(pa.parse_json('Here:\n```json\n{"a": {"b": "}"}}\n```'), {"a": {"b": "}"}})
        self.assertEqual(pa.parse_json('ok {"x": 1} trailing'), {"x": 1})
        with self.assertRaises(ValueError):
            pa.parse_json("no json")

    def test_render_table(self):
        rows = [{"product": {"name_ar": "سباتول"}, "facts": FACTS,
                 "calc": pa.evaluate(FACTS, dict(WINNER, price=3400, cost=MY_COST))},
                {"product": {"name_ar": "قلم"}, "facts": FACTS, "calc": pa.evaluate(FACTS, dict(WINNER, price=2900))},
                {"product": {"name_ar": "بخاخ"}, "error": "HTTP 500"}]
        md = pa.render("shop.youcan.store", rows, web=True)
        self.assertIn("| # | المنتج | حالة السوق | القرار | الدرجة | سعر المنافس | سقف الكلفة", md)
        self.assertIn("ابحث في 1688", md)
        self.assertIn("الأسعار الثلاثة بكلفتك", md)
        self.assertIn("阴角抹子", md)
        self.assertIn("تعذر التدقيق", md)
        self.assertNotIn("الوزن الحجمي", md)


class PromptTest(unittest.TestCase):
    def test_no_cost_estimates_asked_from_the_model(self):
        for text in (pa.AUDIT_SYSTEM, open("prompts/gemini_audit_system.md", encoding="utf-8").read()):
            self.assertNotIn("4,800 DZD / kg", text)
            self.assertNotIn("volumetric_weight", text)
            self.assertIn("1688", text)
        self.assertNotIn('"weight_g"', pa.AUDIT_SYSTEM)
        self.assertNotIn('"purchase_price"', pa.AUDIT_SYSTEM)


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

    def test_lightfunnels_currency_first_and_embedded_json(self):
        # Shape of a real Lightfunnels page (bxrrt.com): JSON-LD without offers,
        # "DZ 5,200" with the currency first, and the product JSON in a script.
        page = ('<script type="application/ld+json">{"@type":"Product","name":"Screen"}</script>'
                '<span>DZ 5,200.00</span> <p>DZ&nbsp;5,200</p>'
                '<script>w={"price":5200,"compare_at_price":0,"order_bump":{"price":0}}</script>')
        self.assertEqual(price_fetch.extract_price(page), (5200, "text"))
        self.assertEqual(price_fetch.extract_price(
            '<script>p={"price":"3900.00","compare_at_price":0}</script>'), (3900, "page-json"))
        self.assertEqual(price_fetch.extract_price("<p>Total DA 2500</p>"), (2500, "text"))

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
            r = c.post("/api/analysis/run", json={
                "store": "shiba.youcan.store", "kind": "audit", "kg_rate": 4500,
                "prices": {"/products/lamp": {"price": 3400, "source": "manual", "cost": MY_COST}}}).get_json()
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
        self.assertIn("🔴 ارمه في البحر", md)
        self.assertIn("1,015", md)   # the owner's landed cost reached the report
        self.assertIn("1,490", md)   # and their profit at the competitor price
        # green product is listed before the killed one
        self.assertLess(md.index("سباتول الكوان"), md.index("بخاخ خدوش"))
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
