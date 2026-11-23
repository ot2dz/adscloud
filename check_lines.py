# -*- coding: utf-8 -*-
"""Which line a status / winning check goes out through.

The tablet line is the normal one. When it is down (proxy dead, Meta blocks
it), the check carries on through the scrape line, but only while that line is
idle: a scrape that arrives takes it back at once. As soon as the tablet
answers again the check moves back. Every move keeps the check's position.

A line that fails rests before it is tried again (5, 10, 20, 40, 60 min; back
to 5 after it makes progress). ``scrape_line_lock`` guarantees that the scrape
line never serves a scrape and a check together.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Optional

from db import db
import scrape_manager as sm

logger = logging.getLogger("check_lines")

TABLET, SCRAPE = "tablet", "scrape"
LABEL = {TABLET: "التابلت", SCRAPE: "خط السحب"}
REST_MIN = (5, 10, 20, 40, 60)
PROBE_EVERY_SEC = 60

# Held by the scrape worker while it claims and runs a job, and by the check
# worker while a check uses the scrape line.
scrape_line_lock = threading.Lock()

_rest_until: dict = {}
_fails: dict = {}
_probe: dict = {}     # line -> (time, ok)


def reset() -> None:
    for d in (_rest_until, _fails, _probe):
        d.clear()


def scrape_proxy() -> str:
    """The scrape proxy (never empty = never the server's own IP)."""
    return (sm._proxy_setting() or os.environ.get("META_ADS_PROXY") or "").strip()


def fallback_enabled(settings: Optional[dict] = None) -> bool:
    s = settings if settings is not None else db.get_settings()
    return str(s.get("status_check_fallback") or "true").lower() in ("1", "true", "yes", "on")


def egress(line: str) -> tuple:
    """(proxy, rotate_url, rotate_token) of a line."""
    if line == SCRAPE:
        return scrape_proxy(), None, None  # None = the scrape line's own rotator
    return sm._status_egress()


def failed(line: str) -> int:
    """The line failed: rest it. Returns the minutes."""
    n = _fails.get(line, 0)
    _fails[line] = n + 1
    minutes = REST_MIN[min(n, len(REST_MIN) - 1)]
    _rest_until[line] = time.time() + minutes * 60
    _probe.pop(line, None)
    return minutes


def worked(line: str) -> None:
    _fails.pop(line, None)
    _rest_until.pop(line, None)


def resting(line: str) -> bool:
    return _rest_until.get(line, 0) > time.time()


def _answers(line: str) -> bool:
    """Does the line's proxy reach the internet? (asked at most once a minute)"""
    from engine import _public_ip
    hit = _probe.get(line)
    if hit and time.time() - hit[0] < PROBE_EVERY_SEC:
        return hit[1]
    proxy = egress(line)[0]
    # Twice: one lost request must not move the check off its line.
    ok = bool(proxy) and bool(_public_ip(proxy) or _public_ip(proxy))
    _probe[line] = (time.time(), ok)
    return ok


def tablet_ready() -> bool:
    return (sm.checks_have_own_line() and not sm.tablet_lane_busy.is_set()
            and not resting(TABLET) and _answers(TABLET))


def scrape_wanted() -> bool:
    """A scrape waits for the scrape line."""
    return db.has_pending_jobs(skip_modes=sm.CHECK_MODES)


def _scrape_line_possible() -> bool:
    proxy = scrape_proxy()
    return bool(proxy) and proxy != sm._status_egress()[0] and fallback_enabled() \
        and not resting(SCRAPE) and not scrape_wanted()


def acquire() -> Optional[str]:
    """The line to use now, or None. SCRAPE comes with its lock held: call
    release() when the check leaves it."""
    if tablet_ready():
        return TABLET
    if sm.tablet_lane_busy.is_set() or not _scrape_line_possible():
        return None
    if not scrape_line_lock.acquire(blocking=False):
        return None  # a scrape is running
    if not scrape_wanted() and _answers(SCRAPE):
        return SCRAPE
    scrape_line_lock.release()
    return None


def release(line: Optional[str]) -> None:
    if line == SCRAPE:
        scrape_line_lock.release()


def should_leave(line: str) -> str:
    """Why a running check must leave its line now ('' = stay)."""
    if line != SCRAPE:
        return ""
    if scrape_wanted():
        return "وصل سحب يحتاج خط السحب"
    if tablet_ready():
        return "عاد خط التابلت"
    return ""
