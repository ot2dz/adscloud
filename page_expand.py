# -*- coding: utf-8 -*-
"""Page expansion: fetch every active ad of pages we only partly hold.

The entity counter (background.entity_stats_loop) already asks Meta how many
active ads each page runs. A page whose Meta total is well above the active
ads in the bank (a market scan caught 1 ad, Meta shows 26) is scraped by page
ID, which is exact: a store domain can be shared by many sellers (e.g. one
myecomsite.net subdomain used by 10 pages), a page cannot.

Settings (settings table):
  page_expand_enabled  "true"/"false" (default true)
  page_expand_daily    max pages per day (default 300) — protects the IP
  page_expand_min_gap  only pages missing at least this many ads (default 3)
  page_expand_max_page skip pages running more ads than this (default 300, 0 =
                       no limit): game apps and big brands, rarely COD stores,
                       each costing 100+ requests. The ad's button still pulls one.
  page_expand_cod_only "true" (default): only pages with an ad in the bank that
                       talks about delivery / cash on delivery, or a store on a
                       known COD platform — not apps, schools or airlines
  page_expand_day / page_expand_done   today's counter (Algeria day)
A page is expanded again only after RECHECK_DAYS and only if its Meta total grew.
"""
from __future__ import annotations

from typing import Optional

BATCH = 30            # pages per job: short jobs, so other scans never wait long
DEFAULT_DAILY = 300
DEFAULT_MIN_GAP = 3
DEFAULT_MAX_PAGE = 300
# What a COD store's ads say (Darija / Arabic / French) and where they link.
# Checked on the ads already in the bank: one is enough for the page.
COD_TEXT = ("توصيل", "التوصيل", "الدفع عند الاستلام", "عند الاستلام", "الدفع عند الإستلام",
            "ولاية", "الولايات", "livraison", "paiement à la livraison")
COD_HOSTS = ("youcan.shop", "youcan.store", "myecomsite.", "justsell.")
RECHECK_DAYS = 7.0
LABEL = "🔁 توسيع الصفحات"


def _today() -> str:
    from db import algeria_today
    return algeria_today().isoformat()


def _int(v, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(str(v))))
    except (TypeError, ValueError):
        return default


def config(settings: Optional[dict] = None) -> dict:
    if settings is None:
        from db import db
        settings = db.get_settings()
    s = settings
    day = str(s.get("page_expand_day") or "")
    return {
        "enabled": str(s.get("page_expand_enabled") or "true").lower() in ("1", "true", "yes", "on"),
        "daily": _int(s.get("page_expand_daily"), DEFAULT_DAILY, 0, 100000),
        "min_gap": _int(s.get("page_expand_min_gap"), DEFAULT_MIN_GAP, 1, 1000),
        "max_page": _int(s.get("page_expand_max_page"), DEFAULT_MAX_PAGE, 0, 1000000),
        "cod_only": str(s.get("page_expand_cod_only") or "true").lower() in ("1", "true", "yes", "on"),
        "done_today": _int(s.get("page_expand_done"), 0, 0, 10**9) if day == _today() else 0,
    }


def room_today(db) -> int:
    c = config(db.get_settings())
    return max(0, c["daily"] - c["done_today"])


def bump(db, pages: int = 1) -> None:
    today = _today()
    s = db.get_settings()
    done = _int(s.get("page_expand_done"), 0, 0, 10**9) if s.get("page_expand_day") == today else 0
    db.set_setting("page_expand_day", today)
    db.set_setting("page_expand_done", str(done + pages))


def too_big(meta_ads, settings: Optional[dict] = None) -> bool:
    cap = config(settings)["max_page"]
    try:
        return bool(cap) and int(meta_ads or 0) > cap
    except (TypeError, ValueError):
        return False


def _cod(c: dict) -> dict:
    return {"cod_text": COD_TEXT, "cod_hosts": COD_HOSTS} if c["cod_only"] else {}


def candidates(db, limit: int = BATCH) -> list[dict]:
    c = config(db.get_settings())
    return db.page_expand_candidates(limit=limit, min_gap=c["min_gap"], recheck_days=RECHECK_DAYS,
                                     max_ads=c["max_page"], **_cod(c))


def status(db) -> dict:
    c = config(db.get_settings())
    pending = db.page_expand_pending(min_gap=c["min_gap"], recheck_days=RECHECK_DAYS, max_ads=c["max_page"],
                                     **_cod(c))
    return dict(c, pending_pages=pending["pages"], missing_ads=pending["missing"],
                last=str(db.get_settings().get("page_expand_last") or ""))
