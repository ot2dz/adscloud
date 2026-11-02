# -*- coding: utf-8 -*-
"""Telegram share bot: forward an ad link and queue a scrape.

Long polling, owner-only. The bot reuses the alert bot token and chat id
stored in Settings (telegram_token / telegram_chat_id).
"""

from __future__ import annotations

import html
import json
import logging
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from db import db
from pages import clean_domain, extract_page_id, page_url
from scrape_manager import DEFAULT_COUNTRY, job_wakeup, manager

logger = logging.getLogger("web_app")

URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
DOMAIN_RE = re.compile(r"^(?:https?://)?(?:www\.)?[a-z0-9-]+(?:\.[a-z0-9-]+)+(?:/\S*)?$", re.IGNORECASE)
FB_HOSTS = ("facebook.com", "fb.com", "fb.me", "fb.watch")
HELP_TEXT = (
    "<b>AdsCloud Bot</b>\n"
    "أرسل رابط إعلان أو رقم صفحة أو دومين متجر أو كلمة مفتاحية، وأضيف سحبًا فوريًا.\n\n"
    "• رابط مكتبة إعلانات ← سحب الرابط كما هو\n"
    "• رابط منشور/مشاركة أو رقم صفحة ← سحب كل إعلانات الصفحة\n"
    "• دومين مثل <code>shop.com</code> ← سحب إعلانات المتجر\n"
    "• نص مثل <code>مكنسة بخار</code> ← سحب بالكلمة\n\n"
    "/status لحالة المحرك · /stop لإيقاف السحب · /id لمعرفة رقم المحادثة"
)


def _api(token: str, method: str, params: dict | None = None, timeout: int = 30) -> dict:
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = urllib.parse.urlencode(params or {}).encode("utf-8")
    req = urllib.request.Request(url, data=data if params is not None else None)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _send(token: str, chat_id: str, text: str) -> None:
    try:
        _api(token, "sendMessage", {
            "chat_id": chat_id,
            "text": text[:4000],
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        }, timeout=20)
    except Exception as exc:  # noqa: BLE001
        logger.warning("telegram bot reply failed: %s", exc)


MOBILE_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
             "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1")


def _share_html(url: str):
    """Fetch a Facebook share page: (final_url, html_text)."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": MOBILE_UA,
                                                   "Accept-Language": "ar,en;q=0.8"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.geturl() or url, resp.read(400000).decode("utf-8", "ignore")
    except Exception:  # noqa: BLE001
        return url, ""


def _ad_snippet(page_html: str) -> str:
    """Pull the shared ad copy out of the page metadata."""
    title = ""
    m = re.search(r'og:title" content="([^"]+)"', page_html)
    if m:
        title = html.unescape(m.group(1))
    else:
        m = re.search(r"<title>(.*?)</title>", page_html, re.S)
        if m:
            title = html.unescape(m.group(1))
    text = re.sub(r"[^\w\s\u0600-\u06FF]", " ", title)
    words = [w for w in text.split() if w]
    snippet = " ".join(words[:8]).strip()
    return snippet if len(snippet) >= 12 else ""


def _page_for_snippet(snippet: str) -> str | None:
    """Find the advertiser page id by searching the ad copy in the library."""
    try:
        from engine import scrape
        ads = []
        scrape(query=snippet, country=DEFAULT_COUNTRY, max_results=12, estimate=False,
               on_ad=lambda a: ads.append(a))
        counts: dict[str, int] = {}
        for ad in ads:
            d = ad.__dict__ if hasattr(ad, "__dict__") else ad
            pid = str(d.get("page_id") or "")
            if pid:
                counts[pid] = counts.get(pid, 0) + 1
        if counts:
            return max(counts, key=counts.get)
    except Exception as exc:  # noqa: BLE001
        logger.warning("share snippet lookup failed: %s", exc)
    return None


def plan_for_text(text: str) -> dict:
    """Turn a shared message into a scrape job plan (no side effects)."""
    raw = (text or "").strip()
    if not raw:
        return {"error": "أرسل رابطًا أو كلمة."}

    match = URL_RE.search(raw)
    if match:
        url = match.group(0).rstrip(").,،؛")
        low = url.lower()
        if "facebook.com/ads/library" in low:
            return {"kind": "link", "url": url, "label": "رابط مكتبة إعلانات"}
        if any(h in low for h in FB_HOSTS):
            final, page_html = _share_html(url)
            if "facebook.com/ads/library" in final.lower():
                return {"kind": "link", "url": final, "label": "رابط مكتبة إعلانات"}
            page_id = None
            if not re.search(r"/(reel|share|watch)", final, re.I):
                page_id = extract_page_id(final)
                if not page_id:
                    m = re.search(r"[?&]id=(\d{6,})", final)
                    page_id = m.group(1) if m else None
            if page_id:
                return {"kind": "page", "url": page_url(page_id, DEFAULT_COUNTRY),
                        "page_id": page_id, "label": f"صفحة {page_id}"}
            snippet = _ad_snippet(page_html)
            found = _page_for_snippet(snippet) if snippet else None
            if found:
                return {"kind": "page", "url": page_url(found, DEFAULT_COUNTRY),
                        "page_id": found, "label": f"صفحة {found} (من نص الإعلان)"}
            return {"error": "رابط مشاركة/ريل بلا معرّف صفحة، ولم أجد الإعلان في المكتبة. "
                             "افتح «تفاصيل الإعلان» في فيسبوك وانسخ رابط مكتبة الإعلانات."}
        domain = clean_domain(url)
        if domain:
            return {"kind": "domain", "query": domain, "label": domain}
        return {"error": "رابط غير مدعوم."}

    if re.fullmatch(r"\d{6,20}", raw):
        return {"kind": "page", "url": page_url(raw, DEFAULT_COUNTRY),
                "page_id": raw, "label": f"صفحة {raw}"}

    if DOMAIN_RE.match(raw) and "." in raw:
        domain = clean_domain(raw)
        return {"kind": "domain", "query": domain, "label": domain}

    return {"kind": "keyword", "query": raw[:200], "label": raw[:60]}


def _queue(plan: dict) -> int:
    job_id = db.create_job(
        query=plan.get("query", ""),
        url=plan.get("url", ""),
        country=DEFAULT_COUNTRY,
        exact_phrase=False,
        stores_only=True,
        sweep=False,
        sort_mode="total_impressions",
        source="manual",
        date_scope="impressions",
    )
    job_wakeup.set()
    return int(job_id)


def _watch_job(token: str, chat_id: str, job_id: int) -> None:
    """Report the final result of a queued job (0 ads vs N ads vs failure)."""
    deadline = time.time() + 1800
    while time.time() < deadline:
        time.sleep(5)
        try:
            job = db.get_job(int(job_id))
        except Exception:  # noqa: BLE001
            return
        if not job:
            return
        status = str(job.get("status") or "")
        if status in ("pending", "running", "stopping"):
            continue
        count = int(job.get("result_count") or 0)
        if status == "failed":
            error = html.escape(str(job.get("error") or "خطأ"))
            _send(token, chat_id, f"⚠️ المهمة #{job_id} فشلت: {error}")
        elif count == 0:
            _send(token, chat_id,
                  f"ℹ️ المهمة #{job_id} انتهت: 0 إعلان — الصفحة/الكلمة بلا إعلانات نشطة.")
        else:
            _send(token, chat_id, f"✅ المهمة #{job_id} انتهت: <b>{count}</b> إعلانًا.")
        return


def _handle_update(token: str, owner: str, update: dict) -> None:
    msg = update.get("message") or update.get("channel_post") or {}
    chat_id = str((msg.get("chat") or {}).get("id", ""))
    text = (msg.get("text") or msg.get("caption") or "").strip()
    if not chat_id or not text:
        return
    if not owner:
        _send(token, chat_id, "أضف <b>Chat ID</b> في إعدادات AdsCloud أولًا.")
        return
    if chat_id != owner:
        logger.warning("telegram bot: ignored message from chat %s", chat_id)
        return

    cmd = text.split()[0].lower()
    if cmd in ("/start", "/help"):
        _send(token, chat_id, HELP_TEXT)
        return
    if cmd == "/id":
        _send(token, chat_id, f"Chat ID: <code>{html.escape(chat_id)}</code>")
        return
    if cmd == "/status":
        st = manager.state or {}
        state = "يعمل" if st.get("running") else ("متوقف مؤقتًا" if st.get("paused") else "متوقف")
        _send(token, chat_id,
              f"المحرك: <b>{state}</b>\nسُحب: {st.get('scraped', 0)} · فُحص: {st.get('scanned', 0)}"
              f"\nالهدف: {html.escape(str(st.get('query') or '-'))}")
        return
    if cmd == "/stop":
        manager.stop()
        _send(token, chat_id, "⏹ أُرسل أمر الإيقاف.")
        return

    plan = plan_for_text(text)
    if plan.get("error"):
        _send(token, chat_id, "⚠️ " + html.escape(plan["error"]))
        return
    try:
        job_id = _queue(plan)
    except Exception as exc:  # noqa: BLE001
        logger.exception("telegram bot queue failed")
        _send(token, chat_id, f"⚠️ تعذّر إنشاء المهمة: {html.escape(str(exc))}")
        return
    busy = bool(manager.state.get("running")) or db.has_pending_jobs()
    threading.Thread(target=_watch_job, args=(token, chat_id, job_id), daemon=True).start()
    _send(token, chat_id,
          f"✅ أُضيفت المهمة <b>#{job_id}</b>\nالنوع: {html.escape(plan['label'])}"
          + ("\n🕒 ستنتظر انتهاء السحب الحالي." if busy else "")
          + "\n\nستجد النتائج في بنك الإعلانات.")


def telegram_bot_loop() -> None:
    """Long-poll Telegram and queue scrapes from the owner's share messages."""
    offset = 0
    webhook_cleared = False
    while True:
        try:
            settings = db.get_settings()
            token = (settings.get("telegram_token") or "").strip()
            owner = (settings.get("telegram_chat_id") or "").strip()
            if not token:
                time.sleep(60)
                continue
            if not webhook_cleared:
                try:
                    _api(token, "deleteWebhook", {"drop_pending_updates": "false"})
                except Exception:  # noqa: BLE001
                    pass
                webhook_cleared = True
            data = _api(token, "getUpdates", {"offset": offset, "timeout": 25}, timeout=35)
            for update in data.get("result") or []:
                offset = max(offset, int(update.get("update_id", 0)) + 1)
                try:
                    _handle_update(token, owner, update)
                except Exception:  # noqa: BLE001
                    logger.exception("telegram bot: update failed")
        except urllib.error.HTTPError as exc:
            if exc.code == 409:
                logger.warning("telegram bot: webhook conflict (delete the bot webhook)")
            time.sleep(15)
        except Exception as exc:  # noqa: BLE001
            logger.warning("telegram bot loop: %s", exc)
            time.sleep(10)
