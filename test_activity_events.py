# -*- coding: utf-8 -*-
"""Tests for the competitor activity timeline (ad_events).

Run with:  python -m unittest -v test_activity_events
"""
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

_TMP_DIR = tempfile.mkdtemp(prefix="adscloud-events-")
os.environ.setdefault("META_ADS_SQLITE", os.path.join(_TMP_DIR, "test.sqlite"))
os.environ["DATABASE_URL"] = ""

from db.core import Database, _now  # noqa: E402


def _ad(ad_id, link="https://shop-test.dz/p/bag", active=True):
    start = (datetime.now(timezone.utc) - timedelta(days=10)).date().isoformat()
    return {
        "ad_id": ad_id, "page_name": "Shop Test", "page_id": "700",
        "start_date": start, "is_active": active,
        "link_url": link, "store_domain": "shop-test.dz",
        "body": "حقيبة جلدية للبيع", "cta_text": "SHOP_NOW",
        "collation_count": 1,
    }


class ActivityEventsTest(unittest.TestCase):
    def setUp(self):
        self.db = Database()
        for table in ("meta_ads", "ad_keywords", "ad_niches", "ad_events",
                      "targets", "user_targets"):
            self.db._execute(f"DELETE FROM {table}")
        self.db.add_target("domain", "shop-test.dz", "Shop")
        self.target = self.db._query_one(
            "SELECT * FROM targets WHERE type = ? AND target_value = ?",
            ("domain", "shop-test.dz"))

    def _events(self, event=None):
        sql = "SELECT * FROM ad_events"
        params = ()
        if event:
            sql += " WHERE event = ?"
            params = (event,)
        rows = self.db._query(sql + " ORDER BY id", params)
        for r in rows:
            import json
            r["detail"] = json.loads(r.get("detail") or "{}")
        return rows

    def test_launch_logged_once(self):
        self.db.save_ad(_ad("a1"), query="shop-test.dz")
        self.db.save_ad(_ad("a1"), query="shop-test.dz")  # same ad, still active
        launched = self._events("launched")
        self.assertEqual(len(launched), 1)
        self.assertEqual(launched[0]["ad_id"], "a1")
        self.assertTrue(launched[0]["detail"].get("start_date"))

    def test_stop_and_resume_timeline(self):
        self.db.save_ad(_ad("a1"), query="shop-test.dz")
        self.db.apply_status_check({}, ["a1"])
        stopped = self._events("stopped")
        self.assertEqual(len(stopped), 1)
        self.assertGreaterEqual(stopped[0]["detail"].get("days_active", 0), 10)

        # The ad comes back in a later scan -> resumed, not a second launch.
        self.db.save_ad(_ad("a1"), query="shop-test.dz")
        resumed = self._events("resumed")
        self.assertEqual(len(resumed), 1)
        self.assertIn("downtime_days", resumed[0]["detail"])
        self.assertEqual(len(self._events("launched")), 1)

    def test_relaunch_of_stopped_product(self):
        self.db.save_ad(_ad("a1", "https://shop-test.dz/p/bag"), query="shop-test.dz")
        self.db.apply_status_check({}, ["a1"])
        self.db.save_ad(_ad("a2", "https://shop-test.dz/p/bag"), query="shop-test.dz")
        launched = {e["ad_id"]: e for e in self._events("launched")}
        self.assertIn("a2", launched)
        self.assertEqual(launched["a2"]["detail"].get("relaunch_of"), "a1")
        self.assertEqual(launched["a2"]["detail"].get("downtime_days"), 0)

    def test_target_activity_summary_and_events(self):
        self.db.save_ad(_ad("a1"), query="shop-test.dz")
        self.db.save_ad(_ad("a2", "https://shop-test.dz/p/other"), query="shop-test.dz")
        self.db.apply_status_check({}, ["a2"])
        data = self.db.target_activity(self.target)
        self.assertEqual(data["summary"]["total_ads"], 2)
        self.assertEqual(data["summary"]["active_ads"], 1)
        self.assertEqual(data["summary"]["stopped_ads"], 1)
        kinds = {e["event"] for e in data["events"]}
        self.assertEqual(kinds, {"launched", "stopped"})
        self.assertTrue(any(e["ad_id"] == "a2" and e["event"] == "stopped"
                            for e in data["events"]))

    def test_backfill_creates_events_for_existing_ads(self):
        self.db.save_ad(_ad("a1"), query="shop-test.dz")
        self.db._execute("DELETE FROM ad_events")
        conn = self.db._connect()
        try:
            cur = conn.cursor()
            self.db._backfill_ad_events(cur, conn)
        finally:
            conn.close()
        self.assertEqual(len(self._events("launched")), 1)


if __name__ == "__main__":
    unittest.main()
