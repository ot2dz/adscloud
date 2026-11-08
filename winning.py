# -*- coding: utf-8 -*-
"""Winning products: Telegram alerts when a saved product changes.

A saved product (same advertiser + same product page = product_key) gets
its launch / stop / resume events from the normal pipeline (scrapes, the
daily status check and the 12-hour winning-products check all write
``ad_events``). This module turns new events of saved products into one
Telegram message per product, then remembers the last event sent.
"""
from __future__ import annotations

import html
import logging
import os
from typing import Callable, Optional

logger = logging.getLogger("winning")

DASHBOARD_URL = os.environ.get("DASHBOARD_URL", "https://ads.botdz.com").rstrip("/")
WATERMARK_KEY = "winning_notify_last_id"


def _product_info(db, pkey: str) -> dict:
    p = db._ph()
    return db._query_one(
        f"SELECT page_name, store_domain, link_url, body FROM winning_products "
        f"WHERE product_key={p} LIMIT 1", (pkey,)) or {}


def build_messages(events: list[dict], info: Callable[[str], dict],
                   active_now: Callable[[str], int]) -> list[str]:
    """One HTML message per product, from its new events (oldest first)."""
    by_product: dict[str, list[dict]] = {}
    for e in events:
        by_product.setdefault(e["product_key"], []).append(e)
    out = []
    for pkey, evs in by_product.items():
        meta = info(pkey) or {}
        name = meta.get("page_name") or evs[-1].get("page_name") or "منتج"
        link = meta.get("link_url") or evs[-1].get("link_url") or ""
        body = " ".join(str(meta.get("body") or evs[-1].get("body") or "").split())
        relaunch = [e for e in evs if e["event"] == "launched" and (e.get("detail") or {}).get("relaunch_of")]
        launched = [e for e in evs if e["event"] == "launched" and e not in relaunch]
        resumed = [e for e in evs if e["event"] == "resumed"]
        stopped = [e for e in evs if e["event"] == "stopped"]
        lines = [f"🏆 <b>منتج رابح: {html.escape(str(name))}</b>"]
        if meta.get("store_domain"):
            lines.append(f"🏪 {html.escape(str(meta['store_domain']))}")
        if body:
            lines.append(f"📝 <i>{html.escape(body[:90] + ('…' if len(body) > 90 else ''))}</i>")
        lines.append("")
        if relaunch:
            days = (relaunch[0].get("detail") or {}).get("downtime_days")
            after = f" بعد توقف {days} يوم" if days not in (None, "") else ""
            lines.append(f"🔁 <b>أعاد إطلاق المنتج{after}</b>"
                         + (f" ({len(relaunch)} إعلان)" if len(relaunch) > 1 else ""))
        if launched:
            lines.append(f"🆕 أطلق <b>{len(launched)}</b> إعلان جديد")
        if resumed:
            lines.append(f"▶️ أعاد تشغيل <b>{len(resumed)}</b> إعلان")
        if stopped:
            lines.append(f"⏸️ أوقف <b>{len(stopped)}</b> إعلان")
        n = int(active_now(pkey) or 0)
        lines.append(f"🟢 يعمل الآن: <b>{n}</b> إعلان" if n > 0 else "🔴 <b>المنتج متوقف بالكامل الآن</b>")
        if link:
            lines.append(f"🔗 {html.escape(link)}")
        lines.append(f"📊 {DASHBOARD_URL}/?tab=winning")
        out.append("\n".join(lines))
    return out


def notify_new_events(db, send: Optional[Callable[[str, str, str], bool]] = None) -> int:
    """Send alerts for saved products' events since the last run.

    The first run only records where "now" is, so saving a product never
    replays its old history. Without Telegram settings the watermark still
    moves, so turning Telegram on later does not flood the chat.
    Returns the number of messages sent.
    """
    if send is None:
        from notify import _telegram_send as send
    s = db.get_settings()
    raw = str(s.get(WATERMARK_KEY) or "").strip()
    if not raw.isdigit():
        db.set_setting(WATERMARK_KEY, str(db.max_ad_event_id()))
        return 0
    events = db.winning_events_after(int(raw))
    if not events:
        return 0
    token = (s.get("telegram_token") or "").strip()
    chat = (s.get("telegram_chat_id") or "").strip()
    sent = failed = 0
    if token and chat:
        for text in build_messages(events, lambda k: _product_info(db, k), db.product_active_count):
            if send(token, chat, text):
                sent += 1
            else:
                failed += 1
    if failed and not sent:
        # Telegram unreachable: keep the events and retry on the next round.
        logger.warning("winning alerts: Telegram send failed, will retry")
        return 0
    db.set_setting(WATERMARK_KEY, str(events[-1]["id"]))
    return sent
