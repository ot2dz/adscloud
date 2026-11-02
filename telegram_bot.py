# -*- coding: utf-8 -*-
"""Telegram share bot: forward an ad link and queue a scrape.

Long polling, owner-only. The bot reuses the alert bot token and chat id
stored in Settings (telegram_token / telegram_chat_id).
"""

from __future__ import annotations

import html
import json
import logging
import os
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
    "/status حالة المحرك · /jobs آخر المهام · /stop إيقاف · /id رقم المحادثة"
)

_PAGE_CACHE: dict[str, str] = {}


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
CRAWLER_UA = "facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)"
LOGIN_MARKERS = ("connectez", "inscrivez", "log in", "log into", "sign up",
                 "تسجيل الدخول", "voir le contenu")


def _og_title(page_html: str) -> str:
    m = re.search(r'og:title" content="([^"]+)"', page_html)
    if m:
        return html.unescape(m.group(1)).strip()
    m = re.search(r"<title>(.*?)</title>", page_html, re.S)
    return html.unescape(m.group(1)).strip() if m else ""


def _og_meta(page_html: str, prop: str) -> str:
    m = re.search(rf'{prop}" content="([^"]+)"', page_html)
    return html.unescape(m.group(1)).strip() if m else ""


def _is_login_wall(title: str) -> bool:
    low = (title or "").lower()
    return any(marker in low for marker in LOGIN_MARKERS)


def _share_html(url: str):
    """Fetch a Facebook share page, preferring a UA that gets the ad preview."""
    last = (url, "")
    for ua in (CRAWLER_UA, MOBILE_UA):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": ua,
                                                       "Accept-Language": "ar,en;q=0.8",
                                                       "Accept": "text/html"})
            with urllib.request.urlopen(req, timeout=20) as resp:
                final = resp.geturl() or url
                body = resp.read(1200000).decode("utf-8", "ignore")
            last = (final, body)
            title = _og_title(body)
            if title and not _is_login_wall(title):
                return last
        except Exception:  # noqa: BLE001
            continue
    return last


def _ad_snippet(page_html: str) -> str:
    """Pull the shared ad copy out of the page metadata.

    The crawler variant prefixes the title with view/reaction counts, so the
    description is preferred and stats segments are dropped.
    """
    candidates = [_og_meta(page_html, "og:description"), _og_title(page_html)]
    for raw in candidates:
        if not raw or _is_login_wall(raw):
            continue
        parts = [p.strip() for p in re.split(r"[|·]", raw) if p.strip()]
        parts = [p for p in parts
                 if not re.search(r"(مشاهدة|تفاعل|ألف|views|reactions|likes)", p, re.I)]
        text = max(parts, key=len) if parts else raw
        text = re.sub(r"\bfacebook\b", " ", text, flags=re.I)
        text = re.sub(r"[^\w\s\u0600-\u06FF]", " ", text)
        words = [w for w in text.split() if w]
        snippet = " ".join(words[:10]).strip()
        if len(snippet) >= 12 and len(words) >= 3:
            return snippet
    return ""


def _reel_stats(page_html: str) -> dict:
    """Public reel counters available to a logged-out visitor."""
    views = re.search(r'"play_count"\s*:\s*(\d+)', page_html)
    reactions = re.search(r'"reaction_count"\s*:\s*\{\s*"count"\s*:\s*(\d+)', page_html)
    video = re.search(r'"video_id"\s*:\s*"(\d+)"', page_html)
    page = re.search(r'"owner_as_page"\s*:\s*\{[^}]*?"name"\s*:\s*"([^"]+)"[^}]*?"id"\s*:\s*"(\d+)"',
                     page_html)
    stats = {}
    if views:
        stats["views"] = int(views.group(1))
    if reactions:
        stats["reactions"] = int(reactions.group(1))
    if video:
        stats["video_id"] = video.group(1)
    if page:
        stats["page_name"] = html.unescape(page.group(1))
        stats["page_id"] = page.group(2)
    return stats


def _fmt_num(n: int) -> str:
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n/1_000:.1f}K"
    return str(n)


def _page_from_html(page_html: str) -> str | None:
    """Read the advertiser page id from the reel/post JSON (owner_as_page / page_id)."""
    m = re.search(r'"owner_as_page":\{[^}]*?"id":"(\d{6,})"', page_html)
    if m:
        return m.group(1)
    m = re.search(r'\\"page_id\\":\\?"?(\d{6,})', page_html)
    if m:
        return m.group(1)
    m = re.search(r'"page_id":"(\d{6,})"', page_html)
    return m.group(1) if m else None


def _page_for_snippet(snippet: str) -> str | None:
    """Find the advertiser page id by searching the ad copy in the library."""
    try:
        from engine import scrape
        ads = []
        deadline = time.monotonic() + 20
        scrape(query=snippet, country=DEFAULT_COUNTRY, max_results=8, estimate=False,
               should_stop=lambda: time.monotonic() > deadline,
               on_ad=lambda a: ads.append(a))
        counts: dict[str, int] = {}
        for ad in ads:
            d = ad.__dict__ if hasattr(ad, "__dict__") else ad
            pid = str(d.get("page_id") or "")
            if pid:
                counts[pid] = counts.get(pid, 0) + 1
        found = max(counts, key=counts.get) if counts else None
        if not found:
            try:
                from engine import meta_blocked
                if meta_blocked(None):
                    logger.warning("share snippet lookup: Meta soft-block")
                    return "__blocked__"
            except Exception:  # noqa: BLE001
                pass
        logger.info("share snippet=%r ads=%s page=%s", snippet[:40], len(ads), found)
        return found
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
            stats = _reel_stats(page_html)
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
            direct = _page_from_html(page_html)
            if direct:
                return {"kind": "page", "url": page_url(direct, DEFAULT_COUNTRY),
                        "page_id": direct, "label": f"صفحة {direct} (من بيانات الريل)",
                        "stats": stats}
            snippet = _ad_snippet(page_html)
            found = _PAGE_CACHE.get(url) or (_page_for_snippet(snippet) if snippet else None)
            if found == "__blocked__":
                return {"error": "Meta يحظر مؤقتًا هذا الاتصال (حظر خفيف). "
                                 "انتظر 10–30 دقيقة أو غيّر الشبكة ثم أعد الإرسال."}
            if found:
                _PAGE_CACHE[url] = found
                return {"kind": "page", "url": page_url(found, DEFAULT_COUNTRY),
                        "page_id": found, "label": f"صفحة {found} (من نص الإعلان)",
                        "stats": stats}
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


PREVIEW_MAX = 3
DASHBOARD_URL = os.environ.get("DASHBOARD_URL", "https://ads.botdz.com")


def _ads_for_job(job: dict, mode: str = "best", offset: int = 0, limit: int = 3) -> list[dict]:
    """Active ads of the job's target, ordered for the chosen view."""
    url = str(job.get("url") or "")
    query = str(job.get("query") or "").strip()
    p = db._ph()
    cols = ("ad_id, page_name, page_id, start_date, days_active, body, cta_text, "
            "preview_image_url, link_url, store_domain")
    sql = f"SELECT {cols} FROM meta_ads WHERE is_active=1"
    params: list = []
    m = re.search(r"(?:view_all_page_id|page_id)=(\d{6,})", url)
    if m:
        sql += f" AND page_id={p}"
        params.append(m.group(1))
    elif query and "." in query and " " not in query:
        sql += f" AND (store_domain={p} OR link_url LIKE {p})"
        params.extend([query, f"%{query}%"])
    elif query:
        sql += f" AND ad_id IN (SELECT ad_id FROM ad_keywords WHERE keyword={p})"
        params.append(query)
    else:
        return []
    order = "days_active DESC" if mode == "best" else "start_date DESC, days_active DESC"
    sql += f" ORDER BY {order} LIMIT {p} OFFSET {p}"
    params.extend([max(1, int(limit)), max(0, int(offset))])
    try:
        return [dict(row) for row in db._query(sql, tuple(params))]
    except Exception:  # noqa: BLE001
        logger.exception("bot preview ads query failed")
        return []


def _ad_card(ad: dict) -> tuple[str, dict | None]:
    body = re.sub(r"\s+", " ", str(ad.get("body") or "")).strip()
    if len(body) > 140:
        body = body[:140] + "…"
    caption = (f"🏪 <b>{html.escape(str(ad.get('page_name') or '-'))}</b>\n"
               f"📅 {html.escape(str(ad.get('start_date') or '-'))} · "
               f"⏱️ {ad.get('days_active') or 0} يوم\n{html.escape(body)}")
    ad_id = str(ad.get("ad_id") or "")
    store = str(ad.get("link_url") or "")
    if not store and ad.get("store_domain"):
        store = "https://" + str(ad["store_domain"])
    buttons = []
    if ad_id:
        buttons.append({"text": "↗ الإعلان",
                        "url": f"https://www.facebook.com/ads/library/?id={ad_id}"})
    if store.startswith("http"):
        buttons.append({"text": "🛒 المتجر", "url": store})
    keyboard = {"inline_keyboard": [buttons]} if buttons else None
    return caption, keyboard


def _send_ad_card(token: str, chat_id: str, ad: dict) -> None:
    """Send one ad: thumbnail on top, details below, link buttons under it."""
    caption, keyboard = _ad_card(ad)
    params = {"chat_id": chat_id, "caption": caption, "parse_mode": "HTML"}
    if keyboard:
        params["reply_markup"] = json.dumps(keyboard, ensure_ascii=False)
    photo = str(ad.get("preview_image_url") or "")
    if photo.startswith("http"):
        try:
            _api(token, "sendPhoto", {**params, "photo": photo}, timeout=30)
            return
        except Exception as exc:  # noqa: BLE001
            logger.warning("sendPhoto failed (%s); sending text", exc)
    params.pop("caption", None)
    params["text"] = caption
    params["disable_web_page_preview"] = "false"
    _api(token, "sendMessage", params, timeout=20)
def _digest_keyboard(job_id: int) -> dict:
    return {"inline_keyboard": [
        [{"text": "🔥 أفضل 3", "callback_data": f"best:{job_id}:0"},
         {"text": "🆕 الأحدث 3", "callback_data": f"new:{job_id}:0"}],
        [{"text": "🌐 فتح اللوحة", "url": DASHBOARD_URL}],
    ]}


def _send_digest(token: str, chat_id: str, job_id: int, label: str,
                 count: int, stores: int, elapsed: float) -> None:
    minutes, seconds = divmod(int(elapsed), 60)
    text = (f"📦 <b>{html.escape(str(label)[:60])}</b>\n"
            f"📥 الإعلانات: <b>{count}</b> · 🏪 المتاجر: <b>{stores}</b>\n"
            f"⏱️ {minutes}:{seconds:02d}\n\nاختر طريقة العرض:")
    _api(token, "sendMessage", {
        "chat_id": chat_id, "text": text, "parse_mode": "HTML",
        "reply_markup": json.dumps(_digest_keyboard(job_id), ensure_ascii=False),
    }, timeout=20)


def _send_more_button(token: str, chat_id: str, job_id: int, mode: str, offset: int) -> None:
    kb = {"inline_keyboard": [[
        {"text": "المزيد ▸", "callback_data": f"{mode}:{job_id}:{offset}"}]]}
    _api(token, "sendMessage", {"chat_id": chat_id, "text": "—",
                                "reply_markup": json.dumps(kb)}, timeout=20)


def _handle_callback(token: str, owner: str, callback: dict) -> None:
    try:
        chat_id = str((callback.get("message") or {}).get("chat", {}).get("id", ""))
        _api(token, "answerCallbackQuery",
             {"callback_query_id": callback.get("id", "")}, timeout=10)
        if not chat_id or chat_id != owner:
            return
        parts = str(callback.get("data") or "").split(":")
        if len(parts) != 3 or parts[0] not in ("best", "new"):
            return
        mode, job_id, offset = parts[0], int(parts[1]), int(parts[2])
        job = db.get_job(job_id)
        if not job:
            _send(token, chat_id, "المهمة غير موجودة.")
            return
        ads = _ads_for_job(job, mode=mode, offset=offset, limit=3)
        if not ads:
            _send(token, chat_id, "لا مزيد من الإعلانات.")
            return
        for ad in ads:
            _send_ad_card(token, chat_id, ad)
        if len(ads) == 3:
            _send_more_button(token, chat_id, job_id, mode, offset + len(ads))
    except Exception:  # noqa: BLE001
        logger.exception("bot callback failed")


def _watch_job(token: str, chat_id: str, job_id: int, label: str = "") -> None:
    """Follow a queued job: periodic progress, then a rich final summary."""
    started = time.time()
    last_ping = 0.0
    stores = 0
    deadline = started + 3600
    while time.time() < deadline:
        time.sleep(5)
        try:
            job = db.get_job(int(job_id))
        except Exception:  # noqa: BLE001
            return
        if not job:
            return
        status = str(job.get("status") or "")
        state = manager.state or {}
        same_job = str(state.get("job_id") or "") == str(job_id)
        if status in ("pending", "running", "stopping"):
            if same_job:
                stores = state.get("stores") or stores
                now = time.time()
                if now - last_ping >= 30:
                    last_ping = now
                    reported = state.get("reported")
                    pct = ""
                    if reported:
                        pct = f" ({min(100, round((state.get('scanned') or 0) * 100 / reported))}%)"
                    _send(token, chat_id,
                          f"⏳ <b>#{job_id}</b>{pct}\n"
                          f"📥 سُحب: {state.get('scraped', 0)} · 🔎 فُحص: {state.get('scanned', 0)}\n"
                          f"🏪 متاجر: {state.get('stores', 0)}")
            continue

        count = int(job.get("result_count") or 0)
        minutes, seconds = divmod(int(time.time() - started), 60)
        target = html.escape(label or str(job.get("query") or job.get("url") or "-")[:60])
        error = str(job.get("error") or "").strip()
        if not error and same_job:
            error = str(state.get("error") or "").strip()
        head = f"المهمة <b>#{job_id}</b> — {target}"
        if status == "failed" or (count == 0 and error):
            _send(token, chat_id,
                  f"⚠️ {head}\nالسبب: {html.escape(error or 'خطأ غير معروف')}\n"
                  f"⏱️ {minutes}:{seconds:02d}")
        elif status == "stopped":
            _send(token, chat_id,
                  f"⏹ {head}\nأُوقفت بعد {count} إعلانًا.\n⏱️ {minutes}:{seconds:02d}")
        elif count == 0:
            _send(token, chat_id,
                  f"ℹ️ {head}\nانتهت: <b>0</b> إعلان — لا توجد إعلانات نشطة لهذا الهدف.\n"
                  f"⏱️ {minutes}:{seconds:02d}")
        else:
            _send_digest(token, chat_id, job_id,
                         label or str(job.get("query") or job.get("url") or "المهمة"),
                         count, stores, time.time() - started)
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
    if cmd == "/jobs":
        try:
            jobs = db.list_jobs(limit=5)
        except Exception:  # noqa: BLE001
            jobs = []
        if not jobs:
            _send(token, chat_id, "لا توجد مهام بعد.")
            return
        lines = []
        for job in jobs:
            target = str(job.get("query") or job.get("url") or "-")[:50]
            lines.append(f"#{job.get('id')} · {job.get('status')} · "
                         f"{job.get('result_count') or 0} إعلان\n   {html.escape(target)}")
        _send(token, chat_id, "<b>آخر المهام</b>\n" + "\n".join(lines))
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
    threading.Thread(target=_watch_job,
                     args=(token, chat_id, job_id, plan.get("label", "")), daemon=True).start()
    _send(token, chat_id,
          f"✅ أُضيفت المهمة <b>#{job_id}</b>\nالنوع: {html.escape(plan['label'])}"
          + ("\n🕒 ستنتظر انتهاء السحب الحالي." if busy else "")
          + "\n\nستجد النتائج في بنك الإعلانات.")

    stats = plan.get("stats") or {}
    if stats:
        lines = ["📊 <b>إحصائيات المنشور</b>"]
        if stats.get("page_name"):
            lines.append(f"🏪 {html.escape(str(stats['page_name']))}")
        if stats.get("views") is not None:
            lines.append(f"👁️ المشاهدات: <b>{_fmt_num(int(stats['views']))}</b>")
        if stats.get("reactions") is not None:
            lines.append(f"👍 التفاعلات: <b>{_fmt_num(int(stats['reactions']))}</b>")
        lines.append("ℹ️ التعليقات والمشاركات غير متاحة للزوار (تتطلب تسجيل دخول).")
        _send(token, chat_id, "\n".join(lines))


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
                    if update.get("callback_query"):
                        _handle_callback(token, owner, update["callback_query"])
                    else:
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
