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

import link_stats
from db import db
from pages import clean_domain, extract_page_id, page_url
from scrape_manager import DEFAULT_COUNTRY, job_wakeup, manager, _proxy_setting

logger = logging.getLogger("web_app")

URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
DOMAIN_RE = re.compile(r"^(?:https?://)?(?:www\.)?[a-z0-9-]+(?:\.[a-z0-9-]+)+(?:/\S*)?$", re.IGNORECASE)
FB_HOSTS = ("facebook.com", "fb.com", "fb.me", "fb.watch")
HELP_TEXT = (
    "<b>AdsCloud Bot</b>\n"
    "أرسل رابط إعلان أو عدة روابط في رسالة واحدة، أو رقم صفحة أو دومين متجر أو كلمة مفتاحية.\n\n"
    "• رابط مكتبة إعلانات ← سحب الرابط كما هو\n"
    "• رابط منشور/ريل أو رقم صفحة ← سحب كل إعلانات الصفحة\n"
    "• دومين مثل <code>shop.com</code> ← سحب إعلانات المتجر\n"
    "• نص مثل <code>مكنسة بخار</code> ← سحب بالكلمة\n\n"
    "التحكم من أزرار التقرير: افحص النتائج · متابعة/إيقاف المنافس · إلغاء آخر مهمة.\n"
    "/status حالة المحرك · /jobs آخر المهام · /stop إيقاف · /id رقم المحادثة"
)

_PAGE_CACHE: dict[str, str] = {}


def _api(token: str, method: str, params: dict | None = None, timeout: int = 30) -> dict:
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = urllib.parse.urlencode(params or {}).encode("utf-8")
    req = urllib.request.Request(url, data=data if params is not None else None)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _send(token: str, chat_id: str, text: str, reply_markup: dict | None = None) -> None:
    try:
        params = {
            "chat_id": chat_id,
            "text": text[:4000],
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        }
        if reply_markup:
            params["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
        _api(token, "sendMessage", params, timeout=20)
    except Exception as exc:  # noqa: BLE001
        logger.warning("telegram bot reply failed: %s", exc)


def _edit_markup(token: str, chat_id: str, message_id: int, keyboard: dict) -> None:
    try:
        _api(token, "editMessageReplyMarkup", {
            "chat_id": chat_id,
            "message_id": message_id,
            "reply_markup": json.dumps(keyboard, ensure_ascii=False),
        }, timeout=10)
    except Exception as exc:  # noqa: BLE001
        logger.warning("telegram bot markup edit failed: %s", exc)


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
               on_ad=lambda a: ads.append(a), proxy=_proxy_setting())
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


def _is_post_url(url: str) -> bool:
    return bool(re.search(r"/(?:reel|reels|share|watch|videos?|posts|permalink|photo)", url or "", re.I))


def _post_id_from_url(url: str) -> str:
    """Numeric post/reel/video id when the shared URL exposes one."""
    u = url or ""
    m = re.search(r"/(?:reel|reels|videos?)/(\d{6,})", u, re.I)
    if m:
        return m.group(1)
    m = re.search(r"[?&]v=(\d{6,})", u)
    if m:
        return m.group(1)
    m = re.search(r"/(?:posts|permalink)/(\d{6,})", u, re.I)
    if m:
        return m.group(1)
    m = re.search(r"[?&](?:story_fbid|fbid)=(\d{6,})", u)
    return m.group(1) if m else ""


def _media_type(*urls: str, stats: dict | None = None) -> str:
    joined = " ".join(u.lower() for u in urls if u)
    if "/reel" in joined:
        return "reel"
    if "/watch" in joined or "/videos/" in joined or (stats and stats.get("video_id")):
        return "video"
    if "/share" in joined:
        return "share"
    if "/photo" in joined:
        return "photo"
    if "/posts/" in joined or "/permalink/" in joined:
        return "post"
    return ""


def _snapshot_for(source_url: str, final_url: str, stats: dict, page_id: str) -> dict | None:
    """Public metrics of a shared reel/post, ready for link_stats.save_snapshot."""
    if not (_is_post_url(source_url) or _is_post_url(final_url)):
        return None
    post_id = _post_id_from_url(final_url) or _post_id_from_url(source_url) \
        or str(stats.get("video_id") or "")
    return {
        "source_url": source_url,
        "final_url": final_url,
        "page_id": str(page_id or stats.get("page_id") or ""),
        "page_name": str(stats.get("page_name") or ""),
        "post_id": post_id,
        "media_type": _media_type(source_url, final_url, stats=stats),
        "views": stats.get("views"),
        "reactions": stats.get("reactions"),
    }


def _match_ad(page_id: str, post_id: str) -> tuple[str, str]:
    """Link a post snapshot to a library ad only on page_id + post_id match."""
    if not page_id or not post_id:
        return "", "none"
    p = db._ph()
    try:
        rows = db._query(
            f"SELECT ad_id, link_url, video_url, video_sd_url FROM meta_ads"
            f" WHERE page_id={p} AND (market IS NULL OR market = 'dz')"
            f" AND (ad_id={p} OR link_url LIKE {p}"
            f" OR video_url LIKE {p} OR video_sd_url LIKE {p}) LIMIT 10",
            (str(page_id), str(post_id), f"%{post_id}%", f"%{post_id}%", f"%{post_id}%"))
    except Exception:  # noqa: BLE001
        logger.exception("ad match lookup failed")
        return "", "none"
    boundary = re.compile(rf"(?<!\d){re.escape(str(post_id))}(?!\d)")
    for row in rows:
        if str(row.get("ad_id") or "") == str(post_id):
            return str(row["ad_id"]), "post_id"
        for field in ("link_url", "video_url", "video_sd_url"):
            if boundary.search(str(row.get(field) or "")):
                return str(row["ad_id"]), "post_id"
    return "", "none"


def _save_link_snapshot(plan: dict) -> None:
    snapshots = []
    if plan.get("snapshot"):
        snapshots.append(plan["snapshot"])
    snapshots.extend(plan.get("extra_snapshots") or [])
    for snap in snapshots:
        page_id = str(snap.get("page_id") or plan.get("page_id") or "")
        post_id = str(snap.get("post_id") or "")
        ad_id, match_type = _match_ad(page_id, post_id)
        try:
            link_stats.save_snapshot(
                source_url=str(snap.get("source_url") or plan.get("url") or ""),
                page_id=page_id,
                page_name=str(snap.get("page_name") or ""),
                post_id=post_id,
                media_type=str(snap.get("media_type") or ""),
                views=snap.get("views"),
                reactions=snap.get("reactions"),
                ad_id=ad_id,
                match_type=match_type,
                raw={"final_url": snap.get("final_url", "")},
            )
        except Exception:  # noqa: BLE001
            logger.exception("link snapshot save failed")


def _ad_page_id_from_bank(ad_id: str) -> str:
    """The Facebook page of a bank ad by its library id (same-platform ads)."""
    try:
        row = db._query_one(
            f"SELECT page_id FROM meta_ads WHERE ad_id={db._ph()} "
            f"AND (market IS NULL OR market = 'dz') LIMIT 1", (str(ad_id),))
        pid = str((row or {}).get("page_id") or "")
        return pid if pid.isdigit() else ""
    except Exception:  # noqa: BLE001
        logger.exception("ad -> page lookup failed")
        return ""


def _plan_for_url(url: str) -> dict:
    """Turn one URL into a scrape plan (no side effects)."""
    low = url.lower()
    if "facebook.com/ads/library" in low:
        m = re.search(r"(?:view_all_page_id|page_id)=(\d{6,})", low)
        if m:
            return {"kind": "link", "url": url, "page_id": m.group(1),
                    "label": "رابط مكتبة إعلانات"}
        # A single-ad link (?id=...) has no page in the URL and the search
        # engine cannot query by ad id alone: resolve the advertiser's page
        # from our own bank, or hand the user a clear instruction.
        m_ad = re.search(r"[?&]id=(\d{6,})", low)
        if m_ad:
            ad_id = m_ad.group(1)
            pid = _ad_page_id_from_bank(ad_id)
            if pid:
                return {"kind": "page", "url": page_url(pid, DEFAULT_COUNTRY),
                        "page_id": pid,
                        "label": f"صفحة {pid} (صاحبة الإعلان {ad_id})"}
            return {"error": "هذا رابط إعلان واحد ولم أجد الإعلان في بنكنا. "
                             "أرسل رابط المنشور/الريل للإعلان، أو رابط مكتبة إعلانات "
                             "يحتوي view_all_page_id (كل إعلانات الصفحة)، أو رقم الصفحة مباشرة."}
        if re.search(r"[?&]q=", low):
            return {"kind": "link", "url": url, "label": "رابط مكتبة إعلانات"}
        return {"error": "رابط مكتبة إعلانات بلا بحث ولا صفحة ولا إعلان. "
                         "أرسل رابطًا يحتوي view_all_page_id أو q أو id."}
    if any(h in low for h in FB_HOSTS):
        final, page_html = _share_html(url)
        stats = _reel_stats(page_html)
        if "facebook.com/ads/library" in final.lower():
            plan = {"kind": "link", "url": final, "label": "رابط مكتبة إعلانات"}
            m = re.search(r"(?:view_all_page_id|page_id)=(\d{6,})", final)
            if m:
                plan["page_id"] = m.group(1)
            return plan
        page_id = None
        if not re.search(r"/(reel|share|watch|videos?|posts|permalink|photo)", final, re.I):
            page_id = extract_page_id(final)
            if not page_id:
                m = re.search(r"[?&]id=(\d{6,})", final)
                page_id = m.group(1) if m else None
        if page_id:
            plan = {"kind": "page", "url": page_url(page_id, DEFAULT_COUNTRY),
                    "page_id": page_id, "label": f"صفحة {page_id}"}
            snap = _snapshot_for(url, final, stats, page_id)
            if snap:
                plan["snapshot"] = snap
            return plan
        direct = _page_from_html(page_html)
        if direct:
            plan = {"kind": "page", "url": page_url(direct, DEFAULT_COUNTRY),
                    "page_id": direct, "label": f"صفحة {direct} (من بيانات الريل)"}
            snap = _snapshot_for(url, final, stats, direct)
            if snap:
                plan["snapshot"] = snap
            return plan
        snippet = _ad_snippet(page_html)
        found = _PAGE_CACHE.get(url) or (_page_for_snippet(snippet) if snippet else None)
        if found == "__blocked__":
            return {"error": "Meta يحظر مؤقتًا هذا الاتصال (حظر خفيف). "
                             "انتظر 10–30 دقيقة أو غيّر الشبكة ثم أعد الإرسال."}
        if found:
            _PAGE_CACHE[url] = found
            plan = {"kind": "page", "url": page_url(found, DEFAULT_COUNTRY),
                    "page_id": found, "label": f"صفحة {found} (من نص الإعلان)"}
            snap = _snapshot_for(url, final, stats, found)
            if snap:
                plan["snapshot"] = snap
            return plan
        return {"error": "رابط مشاركة/ريل بلا معرّف صفحة، ولم أجد الإعلان في المكتبة. "
                         "افتح «تفاصيل الإعلان» في فيسبوك وانسخ رابط مكتبة الإعلانات."}
    domain = clean_domain(url)
    if domain:
        return {"kind": "domain", "query": domain, "label": domain}
    return {"error": "رابط غير مدعوم."}


def plan_for_text(text: str) -> dict:
    """Turn a shared message into a scrape job plan (no side effects)."""
    raw = (text or "").strip()
    if not raw:
        return {"error": "أرسل رابطًا أو كلمة."}

    match = URL_RE.search(raw)
    if match:
        return _plan_for_url(match.group(0).rstrip(").,،؛"))

    if re.fullmatch(r"\d{6,20}", raw):
        return {"kind": "page", "url": page_url(raw, DEFAULT_COUNTRY),
                "page_id": raw, "label": f"صفحة {raw}"}

    if DOMAIN_RE.match(raw) and "." in raw:
        domain = clean_domain(raw)
        return {"kind": "domain", "query": domain, "label": domain}

    return {"kind": "keyword", "query": raw[:200], "label": raw[:60]}


def plans_for_text(text: str) -> list[dict]:
    """Plans for every link in the message, dropping repeated URLs and pages."""
    raw = (text or "").strip()
    if not raw:
        return [{"error": "أرسل رابطًا أو كلمة."}]
    urls: list[str] = []
    for match in URL_RE.finditer(raw):
        url = match.group(0).rstrip(").,،؛")
        if url and url not in urls:
            urls.append(url)
    if not urls:
        return [plan_for_text(raw)]
    plans: list[dict] = []
    seen_pages: dict[str, dict] = {}
    for url in urls:
        plan = _plan_for_url(url)
        page_id = str(plan.get("page_id") or "")
        if page_id and page_id in seen_pages:
            existing = seen_pages[page_id]
            snap = plan.get("snapshot")
            if snap:
                existing.setdefault("extra_snapshots", []).append(snap)
            continue
        if page_id:
            seen_pages[page_id] = plan
        plans.append(plan)
    return plans or [{"error": "لا روابط صالحة."}]


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


DASHBOARD_URL = os.environ.get("DASHBOARD_URL", "https://ads.botdz.com").rstrip("/")


def _page_name_for(page_id: str, plan: dict) -> str:
    pid = str(page_id or "")
    if pid:
        try:
            row = db._query_one(
                f"SELECT page_name FROM meta_ads WHERE page_id={db._ph()} "
                f"AND (market IS NULL OR market = 'dz') AND page_name<>'' LIMIT 1",
                (pid,))
            if row and row.get("page_name"):
                return str(row["page_name"])
        except Exception:  # noqa: BLE001
            logger.exception("page name lookup failed")
    snap = plan.get("snapshot") or {}
    return str(snap.get("page_name") or plan.get("page_name") or plan.get("label") or "-")


def _active_ads_count(page_id: str, job: dict) -> int:
    pid = str(page_id or "")
    if pid:
        try:
            row = db._query_one(
                f"SELECT COUNT(*) AS n FROM meta_ads WHERE page_id={db._ph()} "
                f"AND (market IS NULL OR market = 'dz') AND is_active=1",
                (pid,))
            if row is not None:
                return int(row.get("n") or 0)
        except Exception:  # noqa: BLE001
            logger.exception("page ad count failed")
    return int(job.get("result_count") or 0)


def _report_keyboard(chat_id: str, page_id: str) -> dict:
    target = f"{DASHBOARD_URL}/?page_id={page_id}" if page_id else DASHBOARD_URL
    rows = [[{"text": "افحص النتائج", "url": target}]]
    if page_id:
        if link_stats.is_followed(chat_id, page_id):
            rows.append([{"text": "إيقاف المتابعة", "callback_data": f"unf:{page_id}"}])
        else:
            rows.append([{"text": "متابعة المنافس", "callback_data": f"fol:{page_id}"}])
    rows.append([{"text": "إلغاء آخر مهمة", "callback_data": "can"}])
    return {"inline_keyboard": rows}


def _send_report(token: str, chat_id: str, job: dict, plan: dict) -> None:
    page_id = str(plan.get("page_id") or "")
    name = _page_name_for(page_id, plan)
    count = _active_ads_count(page_id, job)
    status = str(job.get("status") or "")
    error = str(job.get("error") or "").strip()
    head = "⏹" if status == "stopped" else ("⚠️" if status == "failed" else "🏪")
    text = f"{head} <b>{html.escape(name[:80])}</b>\n📥 الإعلانات: <b>{count}</b>"
    if status == "failed" and error:
        text += f"\n{html.escape(error[:160])}"
    _send(token, chat_id, text, reply_markup=_report_keyboard(chat_id, page_id))


def _cancel_last_job(token: str, chat_id: str) -> None:
    try:
        row = db._query_one(
            "SELECT id, status, result_count FROM jobs "
            "WHERE status IN ('pending', 'running') ORDER BY id DESC LIMIT 1")
    except Exception:  # noqa: BLE001
        logger.exception("cancel last job lookup failed")
        row = None
    if not row:
        _send(token, chat_id, "لا توجد مهمة معلّقة أو جارية.")
        return
    jid = int(row["id"])
    if str(row.get("status")) == "pending":
        db.finish_job(jid, status="stopped", result_count=int(row.get("result_count") or 0))
    else:
        db.request_stop(jid)
        if str((manager.state or {}).get("job_id") or "") == str(jid):
            manager.stop()
    _send(token, chat_id, f"⏹ أُلغيت المهمة <b>#{jid}</b>.")


def _handle_callback(token: str, owner: str, callback: dict) -> None:
    try:
        chat_id = str((callback.get("message") or {}).get("chat", {}).get("id", ""))
        _api(token, "answerCallbackQuery",
             {"callback_query_id": callback.get("id", "")}, timeout=10)
        if not chat_id or chat_id != owner:
            return
        data = str(callback.get("data") or "")
        message_id = (callback.get("message") or {}).get("message_id")
        if data == "can":
            _cancel_last_job(token, chat_id)
            return
        if data.startswith(("fol:", "unf:")):
            action, page_id = data.split(":", 1)
            if not page_id.isdigit():
                return
            if action == "fol":
                name = _page_name_for(page_id, {})
                link_stats.follow(chat_id, page_id, "" if name == "-" else name)
            else:
                link_stats.unfollow(chat_id, page_id)
            if message_id:
                _edit_markup(token, chat_id, int(message_id),
                             _report_keyboard(chat_id, page_id))
    except Exception:  # noqa: BLE001
        logger.exception("bot callback failed")


def _watch_job(token: str, chat_id: str, job_id: int, plan: dict) -> None:
    """Wait for the job, then send exactly one concise report."""
    started = time.time()
    while time.time() - started < 3600:
        time.sleep(5)
        try:
            job = db.get_job(int(job_id))
        except Exception:  # noqa: BLE001
            return
        if not job:
            return
        if str(job.get("status") or "") in ("pending", "running", "stopping"):
            continue
        _send_report(token, chat_id, job, plan)
        return
    try:
        job = db.get_job(int(job_id)) or {"id": job_id}
    except Exception:  # noqa: BLE001
        return
    _send_report(token, chat_id, job, plan)


def _emit_log(text: str, key: str = "", every_s: float = 300.0) -> None:
    """Surface bot state in the dashboard log panel (throttled, never fatal)."""
    now = time.time()
    if key:
        if now - _LAST_EMIT.get(key, 0.0) < every_s:
            return
        _LAST_EMIT[key] = now
    try:
        manager._emit("log", {"text": text})
    except Exception:  # noqa: BLE001
        pass


_LAST_EMIT: dict = {}


def bot_diagnostics(token: str) -> str:
    """One-line Telegram polling/webhook state for the settings test message."""
    token = (token or "").strip()
    if not token:
        return ""
    try:
        with urllib.request.urlopen(
                f"https://api.telegram.org/bot{token}/getWebhookInfo", timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace") or "{}")
        info = data.get("result") or {}
        hook = str(info.get("url") or "")
        pending = int(info.get("pending_update_count") or 0)
        if hook:
            return ("\n⚠️ يوجد <b>Webhook</b> مضبوط على البوت، ولهذا لا يستقبل الرسائل. "
                    "احذفه (deleteWebhook) ليستقبل البوت من جديد.")
        return f"\n✅ استقبال رسائل البوت جاهز (Webhook: لا يوجد، رسائل معلّقة: {pending})."
    except Exception as exc:  # noqa: BLE001
        return f"\n⚠️ تعذّر فحص حالة البوت: {html.escape(str(exc)[:80])}"


def _handle_update(token: str, owner: str, update: dict) -> None:
    msg = update.get("message") or update.get("channel_post") or {}
    chat_id = str((msg.get("chat") or {}).get("id", ""))
    text = (msg.get("text") or msg.get("caption") or "").strip()
    if not chat_id or not text:
        return
    cmd = text.split()[0].lower()
    is_owner = bool(owner) and chat_id == owner

    # /id works from ANY chat: it shows the exact number to paste in Settings,
    # which is the usual reason "the bot ignores me" (wrong chat id).
    if cmd == "/id":
        _send(token, chat_id, f"Chat ID: <code>{html.escape(chat_id)}</code>")
        return
    if cmd in ("/start", "/help") and not is_owner:
        _send(token, chat_id,
              "<b>AdsCloud Bot</b>\n"
              f"Chat ID لهذه المحادثة: <code>{html.escape(chat_id)}</code>\n"
              "إن لم يستجب البوت للروابط فضع هذا الرقم في إعدادات AdsCloud "
              "← <b>Telegram Chat ID</b>.")
        return
    if not owner:
        _send(token, chat_id,
              "أضف <b>Chat ID</b> في إعدادات AdsCloud أولًا. أرسل /id لمعرفة رقم هذه المحادثة.")
        return
    if not is_owner:
        logger.warning("telegram bot: ignored message from chat %s", chat_id)
        _send(token, chat_id,
              "هذه المحادثة غير مربوطة بالبوت.\n"
              f"Chat ID لها: <code>{html.escape(chat_id)}</code>\n"
              "ضع هذا الرقم في إعدادات AdsCloud ← Telegram Chat ID.")
        return

    if cmd in ("/start", "/help"):
        _send(token, chat_id, HELP_TEXT)
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

    plans = plans_for_text(text)
    valid = [p for p in plans if not p.get("error")]
    for plan in plans:
        if plan.get("error"):
            _send(token, chat_id, "⚠️ " + html.escape(plan["error"]))
    if not valid:
        return
    # Acknowledge at once: a single link used to stay silent until the scrape
    # ended, which looked like the bot ignored it (the queue can wait hours
    # behind a market scan).
    if len(valid) > 1:
        _send(token, chat_id, f"⏳ جاري السحب: {len(valid)} صفحة/هدف — النتيجة تصلك عند اكتمال كل واحدة.")
    else:
        leaf = str(valid[0].get("label") or valid[0].get("url") or "-")
        _send(token, chat_id, f"⏳ جاري السحب: {html.escape(leaf[:90])} — النتيجة تصلك عند الاكتمال.")
    for plan in valid:
        _save_link_snapshot(plan)
        try:
            job_id = _queue(plan)
        except Exception as exc:  # noqa: BLE001
            logger.exception("telegram bot queue failed")
            _send(token, chat_id, f"⚠️ تعذّر إنشاء المهمة: {html.escape(str(exc))}")
            continue
        threading.Thread(target=_watch_job,
                         args=(token, chat_id, job_id, plan), daemon=True).start()


def follow_watch_loop() -> None:
    """Notify once per batch of genuinely new ads for followed pages."""
    interval = int(os.environ.get("TELEGRAM_FOLLOW_INTERVAL_MIN", "10") or 10) * 60
    time.sleep(60)
    while True:
        try:
            token = (db.get_settings().get("telegram_token") or "").strip()
            if token:
                for row in link_stats.followed():
                    chat_id = str(row.get("chat_id") or "")
                    page_id = str(row.get("page_id") or "")
                    last = str(row.get("last_notified_at") or "")
                    if not chat_id or not page_id:
                        continue
                    hours = float(os.environ.get("TELEGRAM_FOLLOW_SCAN_HOURS", "6") or 6)
                    last_scan = str(row.get("last_scan_at") or "")
                    due = True
                    if last_scan:
                        try:
                            from datetime import datetime
                            age = time.time() - datetime.fromisoformat(last_scan).timestamp()
                            due = age >= hours * 3600
                        except Exception:  # noqa: BLE001
                            due = True
                    if due:
                        try:
                            _queue({"url": page_url(page_id, DEFAULT_COUNTRY),
                                    "label": f"صفحة {page_id}"})
                        except Exception:  # noqa: BLE001
                            logger.exception("follow scan queue failed")
                        link_stats.mark_scanned(chat_id, page_id)
                        continue
                    if not last:
                        link_stats.mark_notified(chat_id, page_id, "")
                        continue
                    p = db._ph()
                    found = db._query_one(
                        f"SELECT COUNT(*) AS n FROM meta_ads WHERE page_id={p} "
                        f"AND (market IS NULL OR market = 'dz') AND first_seen>{p}",
                        (page_id, last))
                    count = int((found or {}).get("n") or 0)
                    if count <= 0:
                        continue
                    name = row.get("page_name") or page_id
                    keyboard = {"inline_keyboard": [[{
                        "text": "افحص النتائج",
                        "url": f"{DASHBOARD_URL}/?page_id={page_id}"}]]}
                    _api(token, "sendMessage", {
                        "chat_id": chat_id,
                        "text": f"🆕 <b>{html.escape(str(name))}</b>: <b>{count}</b> إعلان جديد",
                        "parse_mode": "HTML",
                        "reply_markup": json.dumps(keyboard, ensure_ascii=False),
                    }, timeout=20)
                    link_stats.mark_notified(chat_id, page_id, "")
        except Exception as exc:  # noqa: BLE001
            logger.warning("follow watch loop: %s", exc)
        time.sleep(interval)


_SLOW_UPDATE_LOCK = threading.BoundedSemaphore(1)


def _process_update(token: str, owner: str, update: dict) -> None:
    """Run one update off the polling thread.

    A shared Facebook link can fetch pages + run a short scrape (tens of
    seconds); doing that inline froze getUpdates, so /start and every later
    message looked ignored. Commands stay instant; slow work is serialized.
    """
    try:
        if update.get("callback_query"):
            _handle_callback(token, owner, update["callback_query"])
            return
        msg = update.get("message") or update.get("channel_post") or {}
        text = (msg.get("text") or msg.get("caption") or "").strip()
        if text.startswith("/"):
            _handle_update(token, owner, update)
            return
        with _SLOW_UPDATE_LOCK:
            _handle_update(token, owner, update)
    except Exception:  # noqa: BLE001
        logger.exception("telegram bot: update failed")


def _saved_offset() -> int:
    try:
        return max(0, int(db.get_settings().get("telegram_last_update_id") or 0))
    except Exception:  # noqa: BLE001
        return 0


def telegram_bot_loop() -> None:
    """Long-poll Telegram and queue scrapes from the owner's share messages."""
    offset = _saved_offset()
    webhook_cleared = False
    announced = False
    while True:
        try:
            settings = db.get_settings()
            token = (settings.get("telegram_token") or "").strip()
            owner = (settings.get("telegram_chat_id") or "").strip()
            if not token:
                _emit_log("⚠️ بوت تلجرام: لا يوجد Bot Token في الإعدادات.", key="tg_no_token")
                time.sleep(60)
                continue
            if not webhook_cleared:
                try:
                    _api(token, "deleteWebhook", {"drop_pending_updates": "false"})
                    webhook_cleared = True
                except Exception as exc:  # noqa: BLE001
                    logger.warning("telegram bot: deleteWebhook failed: %s", exc)
            if not announced:
                _emit_log("🤖 بوت تلجرام يعمل ويستقبل الرسائل (long polling).",
                          key="tg_up", every_s=6 * 3600)
                announced = True
            data = _api(token, "getUpdates", {"offset": offset, "timeout": 25}, timeout=35)
            updates = data.get("result") or []
            for update in updates:
                offset = max(offset, int(update.get("update_id", 0)) + 1)
                threading.Thread(target=_process_update, args=(token, owner, update),
                                 daemon=True).start()
            if updates:
                # Remember the last update so a redeploy does not re-run old links.
                try:
                    db.set_setting("telegram_last_update_id", str(offset))
                except Exception:  # noqa: BLE001
                    pass
        except urllib.error.HTTPError as exc:
            if exc.code == 409:
                # A webhook or a second poller owns the updates: retry clearing.
                webhook_cleared = False
                logger.warning("telegram bot: 409 conflict (webhook or another poller)")
                _emit_log("⚠️ بوت تلجرام: تعارض 409 — يوجد Webhook مضبوط أو نسخة ثانية "
                          "من البوت تستقبل الرسائل. احذف الـ Webhook/النسخة المكررة.",
                          key="tg409", every_s=600)
            else:
                logger.warning("telegram bot HTTP %s: %s", exc.code, exc)
            time.sleep(15)
        except Exception as exc:  # noqa: BLE001
            logger.warning("telegram bot loop: %s", exc)
            time.sleep(10)
