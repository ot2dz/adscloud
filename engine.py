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


def _proxy_arg():
    """Proxy config from META_ADS_PROXY (single host:port:user:pass, or comma list)."""
    spec = (os.environ.get("META_ADS_PROXY") or "").strip()
    if not spec:
        return None
    parts = [p.strip() for p in spec.split(",") if p.strip()]
    return parts if len(parts) > 1 else parts[0]


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
) -> ScrapeResult:
    """Collect ads for a keyword / page / domain / custom URL over HTTP."""
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

    collector = MetaAdsCollector(proxy=_proxy_arg())
    reported: Optional[int] = None
    total = 0
    scanned = 0
    stopped = False
    try:
        # Best-effort estimated count (one extra request; never fatal).
        try:
            reported = collector.count(
                query=q, country=ctry, status=STATUS_ACTIVE, sort_by=sort_by,
                start_date=gql_min, end_date=gql_max,
            )
            if reported is not None:
                progress(f"📊 العدد التقديري من Meta: {reported}")
                if on_reported:
                    on_reported(reported)
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
        ):
            if should_stop and should_stop():
                stopped = True
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
                    continue
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
    except Exception as exc:  # noqa: BLE001
        logger.exception("HTTP scrape failed")
        progress(f"⚠️ خطأ أثناء السحب: {exc}")
    finally:
        try:
            collector.close()
        except Exception:
            pass

    progress(f"✅ انتهى السحب: {total} إعلان (بدون متصفح).")
    return ScrapeResult(query=q, url=url or "", total_ads=total, reported_count=reported, stopped=stopped)
