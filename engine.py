# -*- coding: utf-8 -*-
"""
HTTP scraping engine (no Playwright / no browser).

Thin adapter that exposes the same ``scrape(...)`` contract the dashboard
already expects, but implemented on top of the lightweight
``meta_ads_collector`` HTTP/GraphQL engine. Ads are returned as local ``Ad``
objects whose ``to_dict()`` matches what ``db.save_ad`` consumes.
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from typing import Callable, Optional
from urllib.parse import parse_qsl, urlsplit

from meta_ads_collector import MetaAdsCollector
from meta_ads_collector.constants import (
    SEARCH_EXACT,
    SEARCH_KEYWORD,
    SEARCH_PAGE,
    SORT_IMPRESSIONS,
    SORT_RELEVANCY_MONTHLY_GROUPED,
    STATUS_ACTIVE,
)

from pages import clean_domain  # noqa: F401  (kept for compatibility)
from store_filter import extract_store_domain

logger = logging.getLogger("engine")


@dataclass
class Ad:
    ad_id: str = ""
    page_name: str = ""
    page_id: str = ""
    start_date: str = ""
    is_active: bool = False
    link_url: str = ""
    store_domain: str = ""
    platform: str = ""
    body: str = ""
    cta_text: str = ""
    collation_count: int = 0
    publisher_platforms: list = field(default_factory=list)
    preview_image_url: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ScrapeResult:
    query: str = ""
    url: str = ""
    total_ads: int = 0
    reported_count: Optional[int] = None
    stopped: bool = False
    # True only when Meta reported no further pages (search exhausted).
    completed: bool = False
    # Set when the run gave up on a transient error; resume from `cursor`.
    interrupted: bool = False
    # Set when the error is permanent (retrying would just burn requests).
    failed: bool = False
    error: str = ""
    # Resume point: cursor of the next unfetched page (None = start/finished).
    cursor: Optional[str] = None
    # Ads returned by Meta in this run (before the store / date filters).
    scanned: int = 0


def _proxy_arg(spec: Optional[str] = None):
    """Proxy config: an explicit saved value wins over ``META_ADS_PROXY`` env.

    Accepts one ``host:port:user:pass`` or a comma-separated list (rotating
    pool). Returns ``None`` when nothing is configured.
    """
    raw = (spec or "").strip() or (os.environ.get("META_ADS_PROXY") or "").strip()
    if not raw:
        return None
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    return parts if len(parts) > 1 else parts[0]


def _env_float(name: str, default: float) -> float:
    try:
        return float((os.environ.get(name) or "").strip() or default)
    except ValueError:
        return default


# Meta answers in ~0.5s; the library default (2s + up to 1s jitter) spent
# ~80% of the run sleeping. Tunable per deployment.
REQUEST_DELAY = _env_float("META_ADS_DELAY", 0.3)
REQUEST_JITTER = _env_float("META_ADS_JITTER", 0.3)

# In-run recovery: after a transient failure, wait then resume from the last
# cursor with a fresh session (and a fresh proxy IP). Beyond these attempts the
# run returns `interrupted` and the job queue retries later.
IN_RUN_RETRY_WAITS = (30, 120)

# Launch-date scrapes: Meta returns the newest launches first, so once this
# many scanned ads IN A ROW were launched before the window, the in-window ads
# are exhausted and the rest of the scan is wasted requests. 0 disables it.
LAUNCH_EARLY_STOP = int(_env_float("META_ADS_LAUNCH_EARLY_STOP", 500))

# Errors that will not go away by retrying the same cursor.
_PERMANENT_ERRORS = ("Repeated pagination cursor", "consecutive empty pages")


def new_count_client(proxy: Optional[str] = None):
    """A bare Meta client for one-request result counts (uses the proxy pool)."""
    from meta_ads_collector.client import MetaAdsClient
    return MetaAdsClient(proxy=_proxy_arg(proxy))


def entity_ad_count(client, kind: str, key: str, country: str = "DZ") -> Optional[int]:
    """Active ads Meta reports for a whole page (all countries, like the
    page's Ad Library link) or for an exact store domain in ``country``."""
    if kind == "page":
        return client.get_result_count(country="ALL", page_id=str(key), active_status="ACTIVE",
                                       sort_mode=SORT_IMPRESSIONS)
    return client.get_result_count(country=country, query=f'"{key}"', active_status="ACTIVE",
                                   search_type=SEARCH_EXACT, sort_mode=SORT_IMPRESSIONS)


def _is_permanent(exc: Exception) -> bool:
    return any(m in str(exc) for m in _PERMANENT_ERRORS)


def _sleep_unless_stopped(seconds: float, should_stop) -> bool:
    """Sleep in 1s steps; return True if a stop was requested meanwhile."""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if should_stop and should_stop():
            return True
        time.sleep(1)
    return bool(should_stop and should_stop())


def _parse_ads_url(url: Optional[str]) -> dict:
    """Extract search parameters from a Meta Ad Library URL."""
    if not url:
        return {}
    try:
        q = dict(parse_qsl(urlsplit(url).query, keep_blank_values=True))
    except Exception:
        return {}
    out: dict = {}
    raw_q = (q.get("q") or "").strip()
    if raw_q:
        out["q"] = raw_q.strip('"')
    if q.get("country"):
        out["country"] = q["country"]
    pid = (q.get("view_all_page_id") or q.get("page_id") or "").strip()
    if pid.isdigit():
        out["page_id"] = pid
    if q.get("start_date[min]"):
        out["start_date_min"] = q["start_date[min]"]
    if q.get("start_date[max]"):
        out["start_date_max"] = q["start_date[max]"]
    return out


def _iso_plus_one(value: Optional[str]) -> Optional[str]:
    """Ad Library ``start_date[max]`` is exclusive -> add one day for end_date."""
    if not value:
        return None
    try:
        return (datetime.strptime(value[:10], "%Y-%m-%d").date() + timedelta(days=1)).isoformat()
    except Exception:
        return None


def _iso_minus_one(value: Optional[str]) -> Optional[str]:
    """Convert exclusive end_date back to inclusive date (value - 1 day)."""
    if not value:
        return None
    try:
        return (datetime.strptime(value[:10], "%Y-%m-%d").date() - timedelta(days=1)).isoformat()
    except Exception:
        return None


def _adapt(m) -> Ad:
    """Map a meta_ads_collector Ad -> local Ad."""
    cr = m.creatives[0] if getattr(m, "creatives", None) else None
    link = (getattr(cr, "link_url", "") if cr else "") or ""
    page = getattr(m, "page", None)
    start = getattr(m, "delivery_start_time", None)
    domain, platform = extract_store_domain(link)
    return Ad(
        ad_id=str(getattr(m, "id", "") or ""),
        page_name=(getattr(page, "name", "") if page else "") or "",
        page_id=str((getattr(page, "id", "") if page else "") or ""),
        start_date=(start.date().isoformat() if start else ""),
        is_active=bool(getattr(m, "is_active", False)),
        link_url=link,
        store_domain=domain or "",
        platform=platform or "",
        body=(getattr(cr, "body", "") if cr else "") or "",
        cta_text=(((getattr(cr, "cta_type", "") or getattr(cr, "cta_text", "")) if cr else "") or ""),
        collation_count=int(getattr(m, "collation_count", 0) or 1),
        publisher_platforms=list(getattr(m, "publisher_platforms", []) or []),
        preview_image_url=((getattr(cr, "image_url", "") or getattr(cr, "thumbnail_url", "")) if cr else "") or "",
    )


def scrape(
    query: str = "",
    country: str = "DZ",
    url: Optional[str] = None,
    exact_phrase: bool = False,
    sort_mode: str = "total_impressions",
    languages=None,
    start_date_min: Optional[str] = None,
    start_date_max: Optional[str] = None,
    stores_only: bool = False,
    progress: Optional[Callable[[str], None]] = None,
    on_ad: Optional[Callable[[Ad], None]] = None,
    on_reported: Optional[Callable[[Optional[int]], None]] = None,
    should_stop: Optional[Callable[[], bool]] = None,
    max_results: Optional[int] = None,
    date_scope: str = "impressions",
    on_scanned: Optional[Callable[[int], None]] = None,
    resume_cursor: Optional[str] = None,
    on_checkpoint: Optional[Callable[[Optional[str]], None]] = None,
    proxy: Optional[str] = None,
) -> ScrapeResult:
    """Collect ads for a keyword / page / domain / custom URL over HTTP.

    ``resume_cursor`` continues a previous run; ``on_checkpoint(cursor)`` is
    called after each fully-processed page (``None`` once exhausted).
    """
    progress = progress or (lambda _m: None)
    parsed = _parse_ads_url(url)

    q = (parsed.get("q") or query or "").strip()
    ctry = (parsed.get("country") or country or "DZ").upper()
    page_id = parsed.get("page_id")

    url_min = parsed.get("start_date_min")
    url_max = parsed.get("start_date_max")
    arg_min = start_date_min
    arg_max = start_date_max

    inc_min = url_min or arg_min
    inc_max = (_iso_minus_one(url_max) if url_max else arg_max)

    gql_min = inc_min
    gql_max = _iso_plus_one(inc_max) if inc_max else None

    search_type = SEARCH_PAGE if page_id else (SEARCH_EXACT if exact_phrase else SEARCH_KEYWORD)
    sort_by = (SORT_RELEVANCY_MONTHLY_GROUPED
               if str(sort_mode) in ("relevancy_monthly_grouped", "relevancy")
               else SORT_IMPRESSIONS)

    if not q and not page_id:
        progress("⚠️ لا توجد كلمة أو صفحة للسحب.")
        return ScrapeResult(query=q, url=url or "")

    progress(
        f"🚀 بدء السحب عبر HTTP (بدون متصفح): "
        f"{'page ' + page_id if page_id else 'q=' + repr(q)} | country={ctry} | "
        f"date={inc_min or '-'}..{inc_max or '-'} | scope={date_scope}"
    )

    res = ScrapeResult(query=q, url=url or "", cursor=resume_cursor or None)
    total = 0
    scanned = 0
    exhausted = False
    # Smart stop (launch scope only): consecutive ads launched before the window.
    early_stop_after = LAUNCH_EARLY_STOP if (date_scope == "launch" and inc_min) else 0
    older_streak = 0
    early_stopped = False

    def _checkpoint(next_cursor: Optional[str]) -> None:
        nonlocal exhausted
        res.cursor = next_cursor
        exhausted = next_cursor is None
        if on_checkpoint:
            on_checkpoint(next_cursor)

    if resume_cursor:
        progress("↩️ استئناف السحب من آخر نقطة محفوظة (بدون إعادة الصفحات السابقة).")

    attempt = 0
    while True:
        collector = MetaAdsCollector(proxy=_proxy_arg(proxy), rate_limit_delay=REQUEST_DELAY,
                                     jitter=REQUEST_JITTER)
        try:
            # Best-effort estimated count (one extra request; never fatal).
            # Skipped when resuming: it was already reported by the first run.
            if not res.cursor and attempt == 0:
                try:
                    res.reported_count = collector.count(
                        query=q, country=ctry, status=STATUS_ACTIVE, sort_by=sort_by,
                        start_date=gql_min, end_date=gql_max,
                    )
                    if res.reported_count is not None:
                        progress(f"📊 العدد التقديري من Meta: {res.reported_count}")
                        if on_reported:
                            on_reported(res.reported_count)
                except Exception as exc:  # noqa: BLE001
                    logger.debug("count failed: %s", exc)

            for m in collector.search(
                query=q,
                country=ctry,
                status=STATUS_ACTIVE,
                search_type=search_type,
                page_ids=[page_id] if page_id else None,
                sort_by=sort_by,
                start_date=gql_min,
                end_date=gql_max,
                max_results=max_results,
                start_cursor=res.cursor,
                on_checkpoint=_checkpoint,
            ):
                if should_stop and should_stop():
                    res.stopped = True
                    progress("⏹ تم إيقاف السحب بطلب من المستخدم.")
                    break
                scanned += 1
                if on_scanned and (scanned == 1 or scanned % 5 == 0):
                    on_scanned(scanned)
                ad = _adapt(m)

                if date_scope == "launch":
                    sd = ad.start_date or ""
                    if not sd:
                        continue
                    if inc_min and sd < inc_min:
                        older_streak += 1
                        if early_stop_after and older_streak >= early_stop_after:
                            early_stopped = True
                            progress(f"🧠 توقف ذكي: آخر {older_streak} إعلان كلها انطلقت قبل {inc_min} — "
                                     f"انتهت إعلانات الفترة المطلوبة (فُحص {scanned} إعلان).")
                            break
                        continue
                    older_streak = 0
                    if inc_max and sd > inc_max:
                        continue

                # Always store-only: never collect ads without a real store link.
                if not ad.store_domain:
                    continue
                total += 1
                if on_ad:
                    on_ad(ad)
                if total % 25 == 0:
                    progress(f"… تم جمع {total} إعلان")
            # Generator ended normally: exhausted, max_results reached, smart
            # stop, or user stop.
            if early_stopped and not res.stopped:
                _checkpoint(None)  # nothing left worth fetching for this window
            res.completed = (exhausted or early_stopped) and not res.stopped
            break
        except Exception as exc:  # noqa: BLE001
            res.error = str(exc)
            if should_stop and should_stop():
                res.stopped = True
                break
            if _is_permanent(exc):
                logger.error("HTTP scrape failed permanently: %s", exc)
                progress(f"⛔ خطأ دائم، لن تتم إعادة المحاولة: {exc}")
                res.failed = True
                break
            if attempt >= len(IN_RUN_RETRY_WAITS):
                logger.warning("HTTP scrape interrupted after %s retries: %s", attempt, exc)
                progress(f"⏸ انقطع السحب بعد {attempt} محاولات: {exc} — سيُستأنف لاحقاً من نفس النقطة.")
                res.interrupted = True
                break
            wait = IN_RUN_RETRY_WAITS[attempt]
            attempt += 1
            progress(f"⚠️ {exc} — انتظار {wait} ثانية ثم استئناف من آخر صفحة "
                     f"(محاولة {attempt}/{len(IN_RUN_RETRY_WAITS)}).")
            if _sleep_unless_stopped(wait, should_stop):
                res.stopped = True
                break
        finally:
            try:
                collector.close()
            except Exception:
                pass

    res.total_ads = total
    res.scanned = scanned
    if res.completed:
        progress(f"✅ انتهى السحب: {total} إعلان (بدون متصفح).")
    elif not (res.interrupted or res.failed):
        progress(f"✅ توقف السحب: {total} إعلان.")
    return res
