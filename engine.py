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
    video_url: str = ""
    video_sd_url: str = ""  # light copy for weak connections
    collation_id: str = ""  # Meta groups identical copies of one ad

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

# Smart slowdown. Meta answers a rate-limited IP with *empty* results (HTTP 200)
# instead of an error, which used to end a scrape as if it were finished. When
# a run ends on an empty page (or on endless empty pages), one probe request
# for "any ad in Algeria" tells a real end from a block. On a block the pace
# slows down, the run pauses, then resumes from the last good page.
BLOCK_PAUSES_MIN = tuple(int(x) for x in (os.environ.get("META_ADS_BLOCK_PAUSES_MIN") or "20,40").split(",") if x.strip())
PACE_MAX_DELAY = 6.0
PACE_AFTER_BLOCK = 1.5      # never faster than this once a block was seen


class Pace:
    """Delay between requests, shared by every scrape of this process."""

    def __init__(self, base: float):
        self.base = base
        self.delay = base
        self.blocks = 0
        self._ok_pages = 0

    def slow_down(self) -> float:
        self.blocks += 1
        self._ok_pages = 0
        self.delay = min(PACE_MAX_DELAY, max(PACE_AFTER_BLOCK, self.delay * 2))
        return self.delay

    def page_ok(self) -> float:
        """Every 20 good pages, creep back 10% toward the base pace."""
        self._ok_pages += 1
        if self._ok_pages >= 20 and self.delay > self.base:
            self._ok_pages = 0
            floor = PACE_AFTER_BLOCK if self.blocks else self.base
            self.delay = max(floor, round(self.delay * 0.9, 2))
        return self.delay


PACE = Pace(REQUEST_DELAY)


def meta_blocked(proxy: Optional[str] = None) -> bool:
    """One request for any ad running in Algeria: none at all = blocked."""
    collector = MetaAdsCollector(proxy=_proxy_arg(proxy), rate_limit_delay=0, jitter=0)
    try:
        for _ in collector.search(query='" "', country="DZ", status=STATUS_ACTIVE, search_type=SEARCH_EXACT,
                                  sort_by=SORT_RELEVANCY_MONTHLY_GROUPED, max_results=1):
            return False
        return True
    except Exception as exc:  # noqa: BLE001
        logger.info("block probe failed: %s", exc)
        return True
    finally:
        try:
            collector.close()
        except Exception:
            pass


# Errors that will not go away by retrying the same cursor.
_PERMANENT_ERRORS = ("Repeated pagination cursor", "consecutive empty pages")


def new_count_client(proxy: Optional[str] = None):
    """A bare Meta client for one-request result counts (uses the proxy pool)."""
    from meta_ads_collector.client import MetaAdsClient
    return MetaAdsClient(proxy=_proxy_arg(proxy))


def active_ads_of_pages(page_ids: list, country: str = "DZ", proxy: Optional[str] = None,
                        delay: float = 1.2, jitter: float = 0.8, should_stop=None,
                        max_results: Optional[int] = None) -> tuple[list, bool, int]:
    """Every ad still running for up to 50 pages, in one paginated search.

    Returns (ads, complete, requests). ``complete`` is True only when Meta
    said "no more results" with no error on the way; anything less must not
    be used to decide that an ad stopped.
    """
    from meta_ads_collector.constants import SEARCH_EXACT as _EXACT
    collector = MetaAdsCollector(proxy=_proxy_arg(proxy), rate_limit_delay=max(delay, PACE.delay), jitter=jitter)
    ads: list = []
    complete = False
    try:
        for m in collector.search(query="", country=country, status=STATUS_ACTIVE, search_type=_EXACT,
                                  page_ids=[str(p) for p in page_ids], sort_by=SORT_IMPRESSIONS, page_size=30,
                                  max_results=max_results):
            ads.append(_adapt(m))
            if should_stop and should_stop():
                break
        else:
            complete = collector.stats.get("errors", 0) == 0
    except Exception as exc:  # noqa: BLE001
        logger.warning("status batch failed: %s", exc)
    return ads, complete, int(collector.stats.get("requests_made", 0) or 0)


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
    inner = raw_q.strip('"').strip()
    if inner:
        out["q"] = inner
    elif raw_q:
        # ``q=" "`` (quoted whitespace) is Meta's wildcard for "all ads".
        # Keep the quotes so GraphQL receives the same wildcard query.
        out["q"] = raw_q
    if q.get("country"):
        out["country"] = q["country"]
    pid = (q.get("view_all_page_id") or q.get("page_id") or "").strip()
    if pid.isdigit():
        out["page_id"] = pid
    langs = [v.strip().lower() for k, v in sorted(q.items())
             if k.startswith("content_languages") and v.strip()]
    if langs:
        out["content_languages"] = langs
    st = (q.get("search_type") or "").strip().lower()
    if st == "keyword_exact_phrase":
        out["exact_phrase"] = True
    if q.get("sort_data[mode]"):
        out["sort_mode"] = q["sort_data[mode]"].strip()
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
        video_url=(((getattr(cr, "video_url", "") or getattr(cr, "video_hd_url", "")
                     or getattr(cr, "video_sd_url", "")) if cr else "") or ""),
        video_sd_url=((getattr(cr, "video_sd_url", "") if cr else "") or ""),
        collation_id=str(getattr(m, "collation_id", "") or ""),
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
    content_languages: Optional[list] = None,
    estimate: bool = True,
) -> ScrapeResult:
    """Collect ads for a keyword / page / domain / custom URL over HTTP.

    ``resume_cursor`` continues a previous run; ``on_checkpoint(cursor)`` is
    called after each fully-processed page (``None`` once exhausted).
    """
    progress = progress or (lambda _m: None)

    # The dashboard may send a pasted Ad Library URL as ``query`` (it has a
    # single input). Treat it as a URL so its q/page/date params are parsed
    # instead of searching for the URL text itself.
    if not url and query and "facebook.com" in query and "/ads/library" in query:
        url, query = query.strip(), ""
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

    # A pasted Ad Library link wins: its exact-phrase type, sort and
    # language filter are honoured like on facebook.com.
    exact_phrase = bool(parsed.get("exact_phrase") or exact_phrase)
    sort_mode = parsed.get("sort_mode") or sort_mode
    langs = parsed.get("content_languages") or [x for x in (content_languages or []) if x]
    search_type = SEARCH_PAGE if page_id else (SEARCH_EXACT if exact_phrase else SEARCH_KEYWORD)
    sort_by = (SORT_RELEVANCY_MONTHLY_GROUPED
               if str(sort_mode) in ("relevancy_monthly_grouped", "relevancy")
               else SORT_IMPRESSIONS)

    # An Ad Library URL may legitimately carry an empty q (browse / all ads):
    # allow it and let Meta decide; only reject when there is nothing at all.
    if not q and not page_id and not url:
        msg = "لا توجد كلمة أو صفحة أو رابط للسحب."
        progress("⚠️ " + msg)
        return ScrapeResult(query=q, url=url or "", failed=True, error=msg)

    progress(
        f"🚀 بدء السحب عبر HTTP (بدون متصفح): "
        f"{'page ' + page_id if page_id else 'q=' + repr(q)} | country={ctry} | "
        f"date={inc_min or '-'}..{inc_max or '-'} | scope={date_scope}"
        f"{' | lang=' + ','.join(langs) if langs else ''}"
    )

    res = ScrapeResult(query=q, url=url or "", cursor=resume_cursor or None)
    total = 0
    scanned = 0
    exhausted = False
    # Smart stop (launch scope only): consecutive ads launched before the window.
    early_stop_after = LAUNCH_EARLY_STOP if (date_scope == "launch" and inc_min) else 0
    older_streak = 0
    early_stopped = False

    collector = None

    def _checkpoint(next_cursor: Optional[str]) -> None:
        nonlocal exhausted
        res.cursor = next_cursor
        exhausted = next_cursor is None
        if collector is not None:
            collector.rate_limit_delay = PACE.page_ok()
        if on_checkpoint:
            on_checkpoint(next_cursor)

    block_pauses = 0

    def _on_block() -> bool:
        """Slow down and pause; False = out of pauses (run is interrupted)."""
        nonlocal block_pauses
        delay = PACE.slow_down()
        if block_pauses >= len(BLOCK_PAUSES_MIN):
            res.interrupted = True
            res.error = "حظر مؤقت من Meta (نتائج فارغة) — سيُستأنف لاحقاً من نفس النقطة"
            progress(f"⏸ {res.error}.")
            return False
        wait_min = BLOCK_PAUSES_MIN[block_pauses]
        block_pauses += 1
        progress(f"🐢 Meta بدأت تُرجع نتائج فارغة (حظر مؤقت) — إبطاء السحب إلى {delay:.1f} ث بين الطلبات، "
                 f"استراحة {wait_min} دقيقة ثم استئناف من آخر صفحة ({block_pauses}/{len(BLOCK_PAUSES_MIN)}).")
        return True

    if resume_cursor:
        progress("↩️ استئناف السحب من آخر نقطة محفوظة (بدون إعادة الصفحات السابقة).")

    attempt = 0
    while True:
        collector = MetaAdsCollector(proxy=_proxy_arg(proxy), rate_limit_delay=PACE.delay,
                                     jitter=REQUEST_JITTER)
        try:
            # Best-effort estimated count (one extra request; never fatal).
            # Skipped when resuming: it was already reported by the first run.
            if estimate and not res.cursor and attempt == 0:
                try:
                    res.reported_count = collector.count(
                        query=q, country=ctry, status=STATUS_ACTIVE, sort_by=sort_by,
                        start_date=gql_min, end_date=gql_max, search_type=search_type,
                        content_languages=langs or None,
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
                content_languages=langs or None,
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
            # stop, user stop - or an empty page that may be a soft block.
            ended_on_empty_page = (not exhausted and not early_stopped and not res.stopped
                                   and not (max_results and scanned >= max_results))
            if ended_on_empty_page:
                if meta_blocked(proxy):
                    if not _on_block():
                        break
                    if _sleep_unless_stopped(BLOCK_PAUSES_MIN[block_pauses - 1] * 60, should_stop):
                        res.stopped = True
                        break
                    continue
                _checkpoint(None)  # Meta answers normally: this search is really over
            if early_stopped and not res.stopped:
                _checkpoint(None)  # nothing left worth fetching for this window
            res.completed = (exhausted or early_stopped) and not res.stopped
            break
        except Exception as exc:  # noqa: BLE001
            res.error = str(exc)
            if should_stop and should_stop():
                res.stopped = True
                break
            if "consecutive empty pages" in str(exc) and meta_blocked(proxy):
                res.error = ""
                if not _on_block():
                    break
                if _sleep_unless_stopped(BLOCK_PAUSES_MIN[block_pauses - 1] * 60, should_stop):
                    res.stopped = True
                    break
                continue
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
