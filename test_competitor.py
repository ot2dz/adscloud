# -*- coding: utf-8 -*-
"""AI competitor analysis: link cleaning, store matching (subdomain platforms),
text de-duplication, dossier, encrypted key and the API (model call mocked).

Run with:  python -m unittest -v test_competitor
"""
import os
import tempfile
import time
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest import mock

_TMP_DIR = tempfile.mkdtemp(prefix="adscloud-competitor-")
os.environ["META_ADS_SQLITE"] = os.path.join(_TMP_DIR, "test.sqlite")
os.environ["DATABASE_URL"] = ""
os.environ.setdefault("ALLOW_INSECURE_SECRET_KEY", "1")
os.environ.setdefault("SECRET_KEY", "x" * 40)
os.environ["SESSION_COOKIE_SECURE"] = "0"
os.environ["AUTO_SCHEDULE"] = "off"
os.environ["TELEGRAM_BOT"] = "off"

import ai_client  # noqa: E402
import competitor  # noqa: E402
from db import db  # noqa: E402


def _ad(ad_id, domain, body, link_path="/products/lamp", active=True, days_ago=10, page_id="700"):
    start = (datetime.now(timezone.utc) - timedelta(days=days_ago)).date().isoformat()
    return {"ad_id": ad_id, "page_name": "Store", "page_id": page_id, "start_date": start,
            "is_active": active, "link_url": f"https://{domain}{link_path}?utm_source=fb",
            "store_domain": domain, "body": body, "cta_text": "ORDER_NOW", "collation_count": 1}


def _clean():
    for t in ("meta_ads", "ad_keywords", "ad_niches", "ad_events", "ai_reports", "users",
              "settings", "user_sessions", "auth_attempts", "security_events"):
        db._execute(f"DELETE FROM {t}")


def tearDownModule():
    _clean()


class CleanAndMatchTest(unittest.TestCase):
    def setUp(self):
        _clean()
        for i, dom in enumerate(["shiba.youcan.store", "other.youcan.store", "brand.com",
                                 "shop.brand.com", "x.myorderlek.com"]):
            db.save_ad(_ad(f"a{i}", dom, f"نص {i} الدفع عند الاستلام"), query="q", country="DZ")

    def test_clean_link(self):
        self.assertEqual(competitor.clean_store_host("https://WWW.Shiba.YouCan.store/products/x?utm=1#a"),
                         "shiba.youcan.store")
        self.assertEqual(competitor.clean_store_host("  brand.com/collections/all "), "brand.com")
        self.assertEqual(competitor.clean_store_host("http://brand.com:8080/a"), "brand.com")

    def test_platform_subdomain_is_its_own_store(self):
        s = competitor.resolve_store("https://shiba.youcan.store/products/abc", db)
        self.assertEqual(s["domains"], ["shiba.youcan.store"])  # never other.youcan.store
        self.assertEqual(s["platform"], "YouCan Shop")
        self.assertEqual(competitor.resolve_store("x.myorderlek.com", db)["domains"], ["x.myorderlek.com"])

    def test_platform_root_rejected(self):
        self.assertEqual(competitor.resolve_store("https://youcan.store", db)["error"], "platform_root")
        self.assertEqual(competitor.resolve_store("https://wa.me/213555", db)["error"], "not_a_store")
        self.assertEqual(competitor.resolve_store("hello", db)["error"], "invalid_link")

    def test_custom_domain_includes_its_subdomains(self):
        s = competitor.resolve_store("https://www.brand.com/p/1", db)
        self.assertEqual(sorted(s["domains"]), ["brand.com", "shop.brand.com"])
        self.assertIn("subdomains_included", s["notes"])

    def test_unknown_store_suggests_similar(self):
        s = competitor.resolve_store("brand.online", db)
        self.assertEqual(s["domains"], [])
        self.assertIn("brand.com", [x["store_domain"] for x in s["suggestions"]])


class DossierTest(unittest.TestCase):
    def test_duplicates_counted_and_ranked(self):
        today = date(2026, 11, 12)
        mk = lambda i, body, active, days, path: {  # noqa: E731
            "ad_id": str(i), "body": body, "is_active": active, "days_active": days,
            "start_date": (today - timedelta(days=days)).isoformat(),
            "link_url": f"https://s.dz{path}?fbclid=1", "cta_text": "ORDER_NOW",
            "collation_count": 2, "page_name": "P", "page_id": "1",
            "publisher_platforms": '["FACEBOOK", "INSTAGRAM"]'}
        hook = "🔥 ماكينة القهوة بـ 12000 دج! الدفع عند الاستلام، التوصيل لـ 58 ولاية"
        ads = [mk(1, hook, 1, 30, "/products/coffee"),
               mk(2, "  🔥🔥 ماكينة   القهوة بـ 12000 دج الدفع عند الاستلام التوصيل لـ 58 ولاية", 1, 20, "/products/coffee"),
               mk(3, hook, 0, 3, "/products/coffee"),
               mk(4, "Promo -30% livraison gratuite", 0, 2, "/products/juicer"),
               mk(5, "", 1, 5, "/")]
        d = competitor.build_dossier(ads, {"host": "s.dz", "platform": "Custom", "domains": ["s.dz"]},
                                     max_texts=2, today=today)
        t = d["totals"]
        self.assertEqual((t["ads"], t["active"], t["unique_texts"], t["duplicate_ads"]), (5, 3, 3, 2))
        top = d["texts"][0]
        self.assertEqual((top["id"], top["ads"], top["active"], top["max_days"], top["variants"]),
                         ("T1", 3, 2, 30, 6))
        self.assertEqual(top["products"], ["/products/coffee"])  # tracking dropped
        self.assertEqual(len(d["texts"]), 2)
        self.assertEqual(d["omitted_texts"]["count"], 1)
        self.assertEqual(d["prices_dzd"][0]["price"], 12000)
        for sig in ("cod", "delivery_wilayas", "discount", "free_delivery"):
            self.assertIn(sig, d["signals_in_texts"])
        self.assertEqual(d["placements"], {"FACEBOOK": 5, "INSTAGRAM": 5})
        txt = competitor.dossier_text(d)
        self.assertIn("[T1] ads=3", txt)
        msgs = competitor.build_messages(d, lang="fr", note="")
        self.assertIn("français", msgs[1]["content"])
        self.assertGreater(competitor.estimate_messages(msgs), 300)

    def test_token_estimate_arabic_costs_more(self):
        self.assertGreater(competitor.estimate_tokens("ا" * 400), competitor.estimate_tokens("a" * 400))


class KeyEncryptionTest(unittest.TestCase):
    def test_roundtrip_and_not_plain(self):
        enc = ai_client.encrypt_secret("sk-or-secret-123456")
        self.assertTrue(enc.startswith("enc:v1:"))
        self.assertNotIn("secret-123456", enc)
        self.assertEqual(ai_client.decrypt_secret(enc), "sk-or-secret-123456")
        self.assertEqual(ai_client.decrypt_secret("enc:v1:garbage"), "")


class AnalysisApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import app as app_module
        cls.app = app_module.app

    def setUp(self):
        _clean()
        from werkzeug.security import generate_password_hash
        db.create_user("admin@test.dz", generate_password_hash("pass-12345"), role="admin")
        for i in range(4):
            db.save_ad(_ad(f"s{i}", "shiba.youcan.store", "عرض خاص الدفع عند الاستلام"),
                       query="q", country="DZ")

    def _client(self):
        c = self.app.test_client()
        r = c.post("/api/auth/login", json={"email": "admin@test.dz", "password": "pass-12345"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        return c

    def test_settings_key_is_encrypted_and_hidden(self):
        c = self._client()
        self.assertTrue(c.post("/api/ai/config", json={
            "provider": "openrouter", "api_key": "sk-or-v1-abcdefghijklmnop", "model": "m/x"}).get_json()["ok"])
        raw = db.get_settings()["ai_api_key"]
        self.assertTrue(raw.startswith("enc:v1:"))
        cfg = c.get("/api/ai/config").get_json()
        self.assertTrue(cfg["key_set"])
        self.assertNotIn("abcdefghijklmnop", str(cfg))
        self.assertEqual(cfg["effective_base_url"], "https://openrouter.ai/api/v1")

    def test_preview_then_run_saves_report(self):
        c = self._client()
        c.post("/api/ai/config", json={"provider": "openrouter", "api_key": "sk-test-key-123456", "model": "m/x"})
        with mock.patch.object(ai_client, "model_info", return_value=None):
            p = c.post("/api/analysis/preview", json={"store": "https://shiba.youcan.store/x"}).get_json()
        self.assertEqual((p["ads_found"], p["unique_texts"], p["duplicate_ads"]), (4, 1, 3))
        self.assertGreater(p["tokens_in"], 0)
        self.assertIsNone(p["ready_error"])

        fake = {"text": "## Summary\nok", "prompt_tokens": 900, "completion_tokens": 50,
                "cost": 0.001, "finish_reason": "stop", "model": "m/x"}
        with mock.patch.object(ai_client, "chat", return_value=fake) as chat:
            r = c.post("/api/analysis/run", json={"store": "shiba.youcan.store", "note": "واش يربح؟"}).get_json()
            rid = r["id"]
            for _ in range(50):
                row = c.get(f"/api/analysis/reports/{rid}").get_json()
                if row["status"] != "running":
                    break
                time.sleep(0.05)
        self.assertEqual(row["status"], "done", row)
        self.assertEqual(row["report"], "## Summary\nok")
        self.assertEqual(row["dossier"]["totals"]["ads"], 4)
        sent = chat.call_args[0][1][1]["content"]
        self.assertIn("واش يربح؟", sent)
        self.assertEqual(sent.count("[T"), 1)  # 4 ads, one unique text
        self.assertEqual(len(c.get("/api/analysis/reports").get_json()["reports"]), 1)
        self.assertTrue(c.delete(f"/api/analysis/reports/{rid}").get_json()["ok"])

    def test_run_refused_without_key(self):
        c = self._client()
        r = c.post("/api/analysis/run", json={"store": "shiba.youcan.store"})
        self.assertEqual(r.status_code, 400)


if __name__ == "__main__":
    unittest.main()
