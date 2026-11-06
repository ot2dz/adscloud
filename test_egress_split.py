# -*- coding: utf-8 -*-
"""The status check egress must never rotate the scrape line, and leaving its
settings empty must keep the old single-egress behaviour.

Run with:  python -m unittest -v test_egress_split
"""
import datetime as dt
import json
import os
import unittest
from unittest import mock

import background
import engine
import ip_rotator
import scrape_manager
import status_check

WORK = "http://work/rotate"
TABLET = "http://tablet/rotate"


class _Resp:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return json.dumps({"accepted": True}).encode()


class RotatorTest(unittest.TestCase):
    def setUp(self):
        ip_rotator._states.clear()
        self.sent = []
        env = mock.patch.dict(os.environ, {"IP_ROTATE_URL": WORK})
        net = mock.patch("urllib.request.urlopen",
                         lambda req, timeout: self.sent.append(req) or _Resp())
        env.start(), net.start()
        self.addCleanup(env.stop)
        self.addCleanup(net.stop)

    def test_none_uses_env_and_empty_means_no_rotator(self):
        self.assertTrue(ip_rotator.enabled())
        self.assertFalse(ip_rotator.enabled(""))
        self.assertIsNone(ip_rotator.rotate(url=""))
        self.assertEqual(self.sent, [])
        ip_rotator.rotate()
        self.assertEqual(self.sent[0].full_url, WORK)

    def test_debounce_is_per_url(self):
        ip_rotator.rotate()
        self.assertIsNone(ip_rotator.rotate())
        self.assertTrue(ip_rotator.rotate(url=TABLET))
        self.assertEqual([r.full_url for r in self.sent], [WORK, TABLET])

    def test_token_header_only_when_given(self):
        ip_rotator.rotate()
        ip_rotator.rotate(url=TABLET, token="t")
        self.assertIsNone(self.sent[0].get_header("X-rotate-token"))
        self.assertEqual(self.sent[1].get_header("X-rotate-token"), "t")

    def test_wait_ready_without_rotator_does_not_touch_env_url(self):
        self.assertFalse(engine.rotate_and_wait_ready("http://p:1", rotate_url=""))
        self.assertEqual(self.sent, [])


class StatusEgressTest(unittest.TestCase):
    def _egress(self, settings):
        with mock.patch.object(scrape_manager.db, "get_settings", lambda: settings):
            return scrape_manager._status_egress()

    def test_empty_settings_keep_the_scrape_egress(self):
        self.assertEqual(self._egress({"meta_ads_proxy": "work"}), ("work", None, None))

    def test_rotate_url_alone_is_ignored(self):
        got = self._egress({"meta_ads_proxy": "work", "status_check_rotate_url": TABLET})
        self.assertEqual(got, ("work", None, None))

    def test_dedicated_proxy_without_url_has_no_rotator(self):
        self.assertEqual(self._egress({"status_check_proxy": "tab"})[1], "")

    def test_dedicated_egress_with_token(self):
        with mock.patch.dict(os.environ, {"STATUS_CHECK_ROTATE_TOKEN": "t"}):
            got = self._egress({"status_check_proxy": "tab", "status_check_rotate_url": TABLET})
        self.assertEqual(got, ("tab", TABLET, "t"))


class _DB:
    def status_check_pages(self):
        return ["p1", "p2"]

    def top_active_pages(self, n=2):
        return ["p1"]

    def active_ads_of_pages(self, page_ids):
        return [{"ad_id": "a1", "page_id": "p1", "collation_id": "c1",
                 "product_key": "k1", "last_seen_active": ""}]


class StatusCheckRotationTest(unittest.TestCase):
    def test_canary_block_uses_the_egress_rotator(self):
        seen = {}

        def fake(*a, **k):
            seen.update(k)
            return True

        with mock.patch.object(status_check, "rotate_and_wait_ready", fake):
            with self.assertRaises(status_check.Blocked):
                status_check.run(_DB(), progress=lambda m: None, should_stop=lambda: False,
                                 proxy="p", fetch=lambda *a, **k: ([], True, 1),
                                 sleep=lambda s: None, rotate_url=TABLET, rotate_token="t")
        self.assertEqual((seen["rotate_url"], seen["rotate_token"]), (TABLET, "t"))

    def test_empty_batch_uses_the_egress_rotator(self):
        calls = []
        replies = iter([([{"ad_archive_id": "x"}], True, 1)])

        def fetch(*a, **k):
            return next(replies, ([], False, 1))

        with mock.patch.object(status_check.ip_rotator, "rotate",
                               lambda **k: calls.append(k)):
            with self.assertRaises(status_check.Blocked):
                status_check.run(_DB(), progress=lambda m: None, should_stop=lambda: False,
                                 proxy="p", fetch=fetch, sleep=lambda s: None,
                                 rotate_url=TABLET, rotate_token="t")
        self.assertEqual(calls, [{"url": TABLET, "token": "t"}])


class IntervalTest(unittest.TestCase):
    def _due(self, days_ago, interval=None):
        today = dt.date(2026, 11, 6)
        s = {"status_check_hour": "0"}
        if days_ago is not None:
            s["status_check_last"] = (today - dt.timedelta(days=days_ago)).isoformat()
        if interval is not None:
            s["status_check_interval_days"] = str(interval)
        with mock.patch("db.algeria_today", lambda: today), \
                mock.patch.object(background.db, "has_open_mode_job", lambda m: False):
            return background._status_check_due(s)

    def test_default_stays_daily(self):
        self.assertFalse(self._due(0))
        self.assertTrue(self._due(1))
        self.assertTrue(self._due(None))

    def test_every_two_days(self):
        self.assertFalse(self._due(0, 2))
        self.assertFalse(self._due(1, 2))
        self.assertTrue(self._due(2, 2))
        self.assertTrue(self._due(None, 2))


if __name__ == "__main__":
    unittest.main()
