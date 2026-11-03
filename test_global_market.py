# -*- coding: utf-8 -*-
"""Tests for the foreign market side tab (market='global').

Run with:  python -m unittest -v test_global_market
"""
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

_TMP_DIR = tempfile.mkdtemp(prefix="adscloud-global-")
os.environ.setdefault("META_ADS_SQLITE", os.path.join(_TMP_DIR, "test.sqlite"))
os.environ["DATABASE_URL"] = ""

from db.core import Database  # noqa: E402


def _ad(ad_id, link, body="Premium leather bag", page_id="900"):
    start = (datetime.now(timezone.utc) - timedelta(days=8)).date().isoformat()
    return {
        "ad_id": ad_id,
        "page_name": "Flolyeb",
        "page_id": page_id,
        "start_date": start,
        "is_active": True,
        "link_url": link,
        "store_domain": "flolyeb.com",
        "body": body,
        "cta_text": "SHOP_NOW",
        "collation_count": 1,
    }


class GlobalMarketTest(unittest.TestCase):
    def setUp(self):
        self.db = Database()
        for table in ("meta_ads", "ad_keywords", "ad_niches", "targets", "user_targets", "jobs"):
            self.db._execute(f"DELETE FROM {table}")

    def test_global_ads_are_isolated_from_the_dz_bank(self):
        self.assertTrue(self.db.save_ad(_ad("g1", "https://flolyeb.com/p/1"), query="flolyeb.com",
                                        country="ALL", market="global"))
        self.assertTrue(self.db.save_ad(_ad("g2", "https://flolyeb.com/p/2"), query="flolyeb.com",
                                        country="ALL", market="global"))
        # The Algerian dashboard (default market) must not see them.
        self.assertEqual(self.db.get_ads(limit=50), [])
        self.assertEqual(self.db.count_ads(), 0)
        # The global tab sees them.
        self.assertEqual(self.db.count_ads(market="global"), 2)
        self.assertEqual(len(self.db.get_ads(market="global", limit=50)), 2)

    def test_non_arabic_purge_keeps_global_ads(self):
        self.db.save_ad(_ad("g1", "https://flolyeb.com/p/1"), query="flolyeb.com",
                        country="ALL", market="global")
        self.db.purge_non_arabic_ads()
        self.db.purge_junk_ads()
        self.assertEqual(self.db.count_ads(market="global"), 1)

    def test_products_group_ads_per_product(self):
        for i in range(3):
            self.db.save_ad(_ad(f"a{i}", "https://flolyeb.com/p/same-bag"),
                            query="flolyeb.com", country="ALL", market="global")
        for i in range(2):
            self.db.save_ad(_ad(f"b{i}", "https://flolyeb.com/p/other-bag",
                                body="Steel water bottle"),
                            query="flolyeb.com", country="ALL", market="global")
        groups = self.db.get_ad_groups(limit=10, sort_by="most_collated",
                                       market="global", store_domain="flolyeb.com")
        self.assertEqual(len(groups), 2)
        self.assertEqual(groups[0]["group"]["count"], 3)
        self.assertEqual(groups[1]["group"]["count"], 2)
        self.assertEqual(self.db.count_ad_groups(market="global", store_domain="flolyeb.com"), 2)
        self.assertEqual(self.db.global_stores()[0]["total_ads"], 5)

    def test_targets_and_reconcile_are_market_scoped(self):
        self.db.add_user_target(1, "domain", "flolyeb.com", label="Flolyeb",
                                country="ALL", market="global")
        self.assertEqual(self.db.list_competitors(user_id=1, market="dz"), [])
        g = self.db.list_competitors(user_id=1, market="global")
        self.assertEqual(len(g), 1)
        target = g[0]

        for i in range(3):
            self.db.save_ad(_ad(f"a{i}", "https://flolyeb.com/p/same-bag"),
                            query="flolyeb.com", country="ALL", market="global")
        res = self.db.reconcile_target_ads(target, ["a0", "a1"], [], [])
        self.assertEqual(res["stopped"], 1)
        self.assertEqual(self.db.count_ads(market="global", active_only=True), 2)

    def test_status_check_does_not_touch_global_ads(self):
        self.db.save_ad(_ad("g1", "https://flolyeb.com/p/1"), query="flolyeb.com",
                        country="ALL", market="global")
        self.assertEqual(self.db.status_check_pages(), [])

    def test_job_carries_market(self):
        jid = self.db.create_job(query="flolyeb.com", country="ALL", target_id=None,
                                 market="global")
        job = self.db.get_job(jid)
        self.assertEqual(job["market"], "global")

    def test_collector_accepts_all_country(self):
        from meta_ads_collector.collector import MetaAdsCollector
        from meta_ads_collector.constants import AD_TYPE_ALL, STATUS_ACTIVE, SEARCH_KEYWORD
        # country=ALL is what the public Ad Library URL uses for the global tab.
        MetaAdsCollector._validate_params(
            AD_TYPE_ALL, STATUS_ACTIVE, SEARCH_KEYWORD, None, "ALL")
        with self.assertRaises(Exception):
            MetaAdsCollector._validate_params(
                AD_TYPE_ALL, STATUS_ACTIVE, SEARCH_KEYWORD, None, "ALLX")


if __name__ == "__main__":
    unittest.main()
