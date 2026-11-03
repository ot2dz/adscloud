# -*- coding: utf-8 -*-
"""Tests for the persisted Hunter Score (migration, save_ad, backfill, preset).

Run with:  python -m unittest -v test_hunter_score
"""
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

# Isolate the test from the production database before importing db.core.
_TMP_DIR = tempfile.mkdtemp(prefix="adscloud-hunter-")
os.environ["META_ADS_SQLITE"] = os.path.join(_TMP_DIR, "test.sqlite")
os.environ["DATABASE_URL"] = ""

import scoring  # noqa: E402
from db.core import Database  # noqa: E402


def _ad(ad_id, page_id, body, link, query):
    start = (datetime.now(timezone.utc) - timedelta(days=10)).date().isoformat()
    return {
        "ad_id": ad_id,
        "page_name": "Test Page",
        "page_id": page_id,
        "start_date": start,
        "is_active": True,
        "link_url": link,
        "store_domain": "test-store.dz",
        "body": body,
        "cta_text": "SHOP_NOW",
        "collation_count": 1,
    }, query


class HunterScoreTest(unittest.TestCase):
    def setUp(self):
        self.db = Database()
        for table in ("meta_ads", "ad_keywords", "ad_niches"):
            self.db._execute(f"DELETE FROM {table}")

    def _seed(self):
        # Five ads for one product, discovered by two different keywords.
        body = "منتج تجريبي للاختبار السريع"
        link = "https://example.com/p/winner"
        for i in range(5):
            ad, kw = _ad(f"win-{i}", "111", body, link, "kw-one" if i % 2 == 0 else "kw-two")
            self.assertTrue(self.db.save_ad(ad, query=kw))
        # A lone product found by a single keyword: below the bar.
        ad, kw = _ad("lone-1", "222", "منتج آخر مختلف تماما", "https://example.com/p/lone", "kw-one")
        self.assertTrue(self.db.save_ad(ad, query=kw))

    def test_save_ad_persists_score_and_winning_preset(self):
        self._seed()

        rows = self.db.get_ads(limit=100)
        self.assertEqual(len(rows), 6)
        for r in rows:
            self.assertIsNotNone(r.get("hunter_score"), "save_ad must persist hunter_score")

        winners = [r for r in rows if r["page_id"] == "111"]
        losers = [r for r in rows if r["page_id"] == "222"]
        self.assertTrue(all(r["hunter_score"] >= scoring.WINNING_THRESHOLD for r in winners))
        self.assertTrue(all(r["hunter_score"] < scoring.WINNING_THRESHOLD for r in losers))

        # Winner group: 2 keywords (+25), 5 active (+20), depth (+8), recent (+15), age (+15).
        self.assertEqual(winners[0]["hunter_score"], 83)

        self.assertEqual(self.db.count_ads(preset="winning"), 5)
        preset_rows = self.db.get_ads(preset="winning", limit=100)
        self.assertEqual(len(preset_rows), 5)
        self.assertTrue(all(str(r["page_id"]) == "111" for r in preset_rows))

    def test_attach_uses_persisted_score(self):
        self._seed()
        row = self.db.get_ads(page_id="222", limit=1)[0]
        attached = scoring.attach([dict(row)])[0]
        self.assertEqual(attached["hunter"]["score"], row["hunter_score"])

    def test_backfill_recomputes_null_scores(self):
        self._seed()
        self.db._execute("UPDATE meta_ads SET hunter_score = NULL")
        missing = self.db._query_one(
            "SELECT COUNT(*) AS c FROM meta_ads WHERE hunter_score IS NULL")["c"]
        self.assertEqual(int(missing), 6)

        scored = self.db.backfill_hunter_scores()
        self.assertGreaterEqual(scored, 6)
        still_null = self.db._query_one(
            "SELECT COUNT(*) AS c FROM meta_ads WHERE hunter_score IS NULL")["c"]
        self.assertEqual(int(still_null), 0)

        self.assertEqual(self.db.count_ads(preset="winning"), 5)

    def test_backfill_force_recomputes_everything(self):
        self._seed()
        scored = self.db.backfill_hunter_scores(force=True)
        self.assertGreaterEqual(scored, 6)
        self.assertEqual(self.db.count_ads(preset="winning"), 5)


if __name__ == "__main__":
    unittest.main()
