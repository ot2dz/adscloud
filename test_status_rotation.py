# -*- coding: utf-8 -*-
"""Status check must ask the IP rotator for a fresh IP on a Meta block.

Run with:  python -m unittest -v test_status_rotation
"""
import unittest

import status_check


class _FakeDB:
    def status_check_pages(self):
        return ["p1", "p2"]

    def top_active_pages(self, n=2):
        return ["p1"]

    def active_ads_of_pages(self, page_ids):
        return [{"ad_id": "a1", "page_id": "p1", "collation_id": "c1",
                 "product_key": "k1", "last_seen_active": ""}]


class StatusRotationTest(unittest.TestCase):
    def test_rotation_requested_when_canary_is_blocked(self):
        calls = []
        original = status_check.ip_rotator.rotate
        status_check.ip_rotator.rotate = lambda *a, **k: calls.append(1) or {"ok": False}
        try:
            def fake_fetch(page_ids, proxy=None, should_stop=None, max_results=None):
                return [], True, 1

            with self.assertRaises(status_check.Blocked):
                status_check.run(
                    _FakeDB(), progress=lambda m: None, should_stop=lambda: False,
                    proxy="http://127.0.0.1:3128", fetch=fake_fetch,
                    sleep=lambda s: None)
            self.assertTrue(calls, "ip_rotator.rotate() must be called on a block")
        finally:
            status_check.ip_rotator.rotate = original


if __name__ == "__main__":
    unittest.main()
