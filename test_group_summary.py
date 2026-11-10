# -*- coding: utf-8 -*-
"""The precomputed product summary must give exactly what the live GROUP BY
gives, for every sort and page, and stay correct after ads change.

Run with:  python -m unittest -v test_group_summary
"""
import os
import random
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

_TMP_DIR = tempfile.mkdtemp(prefix="adscloud-groups-")
os.environ["META_ADS_SQLITE"] = os.path.join(_TMP_DIR, "test.sqlite")
os.environ["DATABASE_URL"] = ""
os.environ["DB_CACHE_TTL"] = "0"  # compare raw answers, not cached ones

from db.core import Database  # noqa: E402

DEFAULT = dict(stores_only=True, search="", query="", store_domain="", page_id="", preset="",
               active_only=False, start_date_min="", start_date_max="", min_days_active=0,
               target_id=None, user_id=None, min_product_ads=0, keywords=[], niches=[])
SORTS = ("collected_at", "longest_running", "newest_launch", "most_collated", "biggest_page", "hunter")


def _ad(i, page, prod, active=True, cta="SHOP_NOW", days_ago=10):
    start = (datetime.now(timezone.utc) - timedelta(days=days_ago)).date().isoformat()
    return {
        "ad_id": f"ad-{i}", "page_name": f"صفحة {page}", "page_id": f"p{page}",
        "start_date": start, "is_active": active,
        "link_url": f"https://store{page}.dz/products/item-{prod}",
        "store_domain": f"store{page}.dz",
        "body": f"منتج رائع رقم {prod} توصيل مجاني لكل الولايات نسخة {i % 3}",
        "cta_text": cta, "collation_count": 1,
    }


class GroupSummaryTest(unittest.TestCase):
    def setUp(self):
        self.db = Database()
        for table in ("meta_ads", "ad_keywords", "ad_niches", "product_groups", "entity_stats"):
            self.db._execute(f"DELETE FROM {table}")
        rnd = random.Random(7)
        for i in range(240):
            page, prod = rnd.randint(1, 25), rnd.randint(1, 3)
            self.db.save_ad(_ad(i, page, prod, active=rnd.random() > 0.3,
                                cta=rnd.choice(["SHOP_NOW", "ORDER_NOW", "SIGN_UP"]),
                                days_ago=rnd.randint(1, 40)), query=rnd.choice(["kw1", "kw2"]))
        for page in range(1, 26, 2):
            self.db.save_entity_stat("page", f"p{page}", page * 3)
        self.db.mark_groups_stale()
        self.assertTrue(self.db.rebuild_group_summary())

    def _both(self, sort, limit=20, offset=0):
        """(summary answer, live GROUP BY answer) for the default view."""
        fast = self.db.get_ad_groups(limit=limit, offset=offset, sort_by=sort, **DEFAULT)
        self.db._summary_state()["stale"] = True          # force the live query
        self.db._summary_state()["building"] = True       # …without a rebuild
        slow = self.db.get_ad_groups(limit=limit, offset=offset, sort_by=sort, **DEFAULT)
        self.db._summary_state().update(stale=False, building=False)
        return fast, slow

    @staticmethod
    def _shape(items):
        return [(a["group"], a["ad_id"], a.get("keywords")) for a in items]

    def assertSame(self):
        for sort in SORTS:
            for offset in (0, 20, 40):
                fast, slow = self._both(sort, offset=offset)
                self.assertEqual(self._shape(fast), self._shape(slow), f"sort={sort} offset={offset}")
        n_fast = self.db.count_ad_groups(**DEFAULT)
        ads_fast = self.db.count_ads(**DEFAULT)
        self.db._summary_state().update(stale=True, building=True)
        n_slow = self.db.count_ad_groups(**DEFAULT)
        ads_slow = self.db.count_ads(**DEFAULT)
        self.db._summary_state().update(stale=False, building=False)
        self.assertEqual(n_fast, n_slow)
        self.assertEqual(ads_fast, ads_slow)
        self.assertGreater(n_fast, 20)

    def test_summary_matches_live_group_by(self):
        self.assertTrue(self.db.can_use_group_summary(DEFAULT))
        self.assertSame()

    def test_saved_ads_are_reflected_on_next_read(self):
        # A brand-new product, and an existing ad moved to another product.
        self.db.save_ad(_ad(9001, 3, 99, days_ago=0))
        self.db.save_ad(_ad(5, 4, 98, days_ago=2))
        top = self.db.get_ad_groups(limit=5, sort_by="collected_at", **DEFAULT)
        self.assertIn("ad-9001", [a["ad_id"] for a in top])
        self.assertSame()

    def test_bulk_change_falls_back_until_rebuilt(self):
        ids = [f"ad-{i}" for i in range(0, 240, 2)]
        self.db.apply_status_check({}, ids)              # half the ads stop
        self.assertFalse(self.db.can_use_group_summary(DEFAULT))
        # While stale, answers come from the live query, so they are current.
        stopped_shown = self.db.get_ad_groups(limit=500, sort_by="collected_at", **DEFAULT)
        self.assertTrue(stopped_shown)
        self.assertTrue(self.db.rebuild_group_summary())
        self.assertSame()

    def test_cta_setting_change_rebuilds(self):
        self.db.set_setting("cta_allowlist", "SHOP_NOW")
        self.assertFalse(self.db.can_use_group_summary(DEFAULT))
        self.assertTrue(self.db.rebuild_group_summary())
        self.assertSame()
        self.db.set_setting("cta_allowlist", ",".join(Database.DEFAULT_CTA_ALLOWLIST))

    def test_filtered_views_never_use_summary(self):
        self.assertFalse(self.db.can_use_group_summary(dict(DEFAULT, keywords=["kw1"])))
        self.assertFalse(self.db.can_use_group_summary(dict(DEFAULT, preset="active")))
        self.assertFalse(self.db.can_use_group_summary(dict(DEFAULT, user_id=3)))
        self.assertFalse(self.db.can_use_group_summary(dict(DEFAULT, stores_only=False)))


if __name__ == "__main__":
    unittest.main()
