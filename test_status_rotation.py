# -*- coding: utf-8 -*-
"""Status check must ask for a fresh IP on a Meta block AND wait for the
proxy tunnel to come back before giving up.

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
    def _run(self, sleeps):
        def fake_fetch(page_ids, proxy=None, should_stop=None, max_results=None):
            return [], True, 1  # Meta answers empty: the canary is "dead"

        with self.assertRaises(status_check.Blocked):
            status_check.run(
                _FakeDB(), progress=lambda m: None, should_stop=lambda: False,
                proxy="http://127.0.0.1:3128", fetch=fake_fetch,
                sleep=lambda s: sleeps.append(s))

    def test_rotation_requested_when_canary_is_blocked(self):
        calls = []
        original = status_check.rotate_and_wait_ready
        status_check.rotate_and_wait_ready = lambda *a, **k: calls.append(1) or True
        try:
            self._run([])
            self.assertTrue(calls, "rotate_and_wait_ready() must be called on a block")
        finally:
            status_check.rotate_and_wait_ready = original

    def test_waits_for_tunnel_when_rotation_unavailable(self):
        sleeps = []
        original = status_check.rotate_and_wait_ready
        status_check.rotate_and_wait_ready = lambda *a, **k: False
        try:
            self._run(sleeps)
            self.assertIn(status_check.RECHECK_WAIT_SEC, sleeps,
                          "must wait the recheck delay when the rotator cannot rotate")
        finally:
            status_check.rotate_and_wait_ready = original


if __name__ == "__main__":
    unittest.main()
