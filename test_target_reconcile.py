# -*- coding: utf-8 -*-
"""Tests for on-demand competitor re-scan reconciliation (stopped ads).

Run with:  python -m unittest -v test_target_reconcile
"""
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

_TMP_DIR = tempfile.mkdtemp(prefix="adscloud-target-")
os.environ.setdefault("META_ADS_SQLITE", os.path.join(_TMP_DIR, "test.sqlite"))
os.environ["DATABASE_URL"] = ""

from db.core import Database  # noqa: E402


def _ad(i):
    start = (datetime.now(timezone.utc) - timedelta(days=20)).date().isoformat()
    return {
        "ad_id": f"shop-{i}",
        "page_name": "Shop Test",
        "page_id": "500",
        "start_date": start,
        "is_active": True,
        "link_url": f"https://shop-test.dz/p/{i}",
        "store_domain": "shop-test.dz",
        "body": f"منتج رقم {i} للبيع",
        "cta_text": "SHOP_NOW",
        "collation_count": 1,
    }


class TargetReconcileTest(unittest.TestCase):
    def setUp(self):
        self.db = Database()
        for table in ("meta_ads", "ad_keywords", "ad_niches", "targets", "user_targets"):
            self.db._execute(f"DELETE FROM {table}")
        self.assertTrue(self.db.add_target("domain", "shop-test.dz", "Shop"))
        self.target = self.db._query_one(
            "SELECT * FROM targets WHERE type = ? AND target_value = ?",
            ("domain", "shop-test.dz"))

    def _seed(self, n=12):
        for i in range(n):
            self.assertTrue(self.db.save_ad(_ad(i), query="shop-test.dz"))

    def test_rescan_marks_missing_ads_stopped_with_duration(self):
        self._seed(12)
        self.assertEqual(self.db.count_ads(active_only=True), 12)

        # The fresh scan returned only the first five ads.
        seen = [f"shop-{i}" for i in range(5)]
        res = self.db.reconcile_target_ads(self.target, seen, [], [])

        self.assertEqual(res["checked"], 12)
        self.assertEqual(res["confirmed"], 5)
        self.assertEqual(res["stopped"], 7)
        self.assertEqual(self.db.count_ads(active_only=True), 5)
        self.assertEqual(self.db.count_ads(preset="stopped"), 7)

        stopped = self.db.get_ads(preset="stopped", limit=100)
        self.assertEqual(len(stopped), 7)
        for row in stopped:
            self.assertEqual(row["is_active"], 0)
            self.assertIsNotNone(row["stopped_at"])
            self.assertGreaterEqual(int(row["days_active"]), 20)

        still_active = {r["ad_id"] for r in self.db.get_ads(active_only=True, limit=100)}
        self.assertEqual(still_active, set(seen))

    def test_scan_returning_same_product_keeps_copies_alive(self):
        self._seed(3)
        # A copy of the same collation / product was returned: the original is
        # still running even though its exact ad_id is absent.
        ad = _ad(0)
        from store_filter import product_key
        pk = product_key(ad["page_id"], ad["link_url"], ad["body"], ad["store_domain"])
        self.db._execute("UPDATE meta_ads SET collation_id = ? WHERE ad_id = ?",
                         ("coll-1", "shop-0"))
        res = self.db.reconcile_target_ads(self.target, ["shop-1"], ["coll-1"], [pk])
        self.assertEqual(res["stopped"], 1)  # shop-2 only
        self.assertEqual(res["confirmed"], 2)  # shop-1 by id, shop-0 by collation/product

    def test_onward_ads_are_marked_stopped_individually(self):
        self._seed(2)
        res = self.db.reconcile_target_ads(self.target, ["shop-0"], [], [])
        self.assertEqual(res["stopped"], 1)
        self.assertEqual(res["confirmed"], 1)


if __name__ == "__main__":
    unittest.main()
