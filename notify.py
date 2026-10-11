# -*- coding: utf-8 -*-
"""Notifications: Enhanced Telegram bot and webhook alert engine for Ad Spy."""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from typing import Tuple

logger = logging.getLogger("notify")


def send_telegram_test(token: str, chat_id: str) -> Tuple[bool, str]:
    """Send an immediate test message to verify Telegram credentials."""
    token = (token or "").strip()
    chat_id = (chat_id or "").strip()
    if not token or not chat_id:
        return False, "يرجى كتابة كل من Bot Token و Chat ID أولاً."

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    text = (
        "🚀 <b>تم ربط بوت التلجرام بنجاح!</b>\n\n"
        "منصة <b>MetaAds Spy</b> متصلة وجاهزة الآن.\n"
        "ستصلك هنا فوراً:\n"
        "• 🆕 أي إعلانات أو منتجات جديدة يطلقها المنافسون.\n"
        "• 🏆 تنبيهات المنتجات الرابحة (التي تجاوزت 5 أيام تشغيل).\n"
        "• ⏸️ إشعارات إطفاء أو توقف الحملات.\n\n"
        "🇩🇿 <i>مراقبة السوق الجزائري نشطة على مدار الساعة.</i>"
    )
    data = urllib.parse.urlencode({
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true"
    }).encode("utf-8")

    try:
        req = urllib.request.Request(url, data=data)
        with urllib.request.urlopen(req, timeout=12) as r:
            if 200 <= r.status < 300:
                return True, "تم إرسال الرسالة التجريبية إلى حسابك/قناتك في تلجرام بنجاح!"
            return False, f"فشل الإرسال (رمز الاستجابة: {r.status})"
    except urllib.error.HTTPError as e:
        err_msg = e.read().decode("utf-8", errors="replace")
        return False, f"خطأ من تلجرام (رمز {e.code}): تحقق من التوكن أو تأكد من مراسلة البوت أولاً (/start)."
    except Exception as exc:
        return False, f"فشل الاتصال: {str(exc)}"


def _telegram_send(token: str, chat_id: str, text: str) -> bool:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true"
    }).encode("utf-8")
    try:
        req = urllib.request.Request(url, data=data)
        with urllib.request.urlopen(req, timeout=15) as r:
            return 200 <= r.status < 300
    except Exception as exc:
        logger.warning("telegram send failed: %s", exc)
        return False


def _webhook_send(url: str, payload: dict) -> bool:
    try:
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST"
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            return 200 <= r.status < 300
    except Exception as exc:
        logger.warning("webhook send failed: %s", exc)
        return False


def notify_new_ads(settings: dict, target: str, new_ads: list[dict], total: int) -> dict:
    """Format and send a structured spy alert when new ads are detected."""
    if settings.get("notify_enabled", "false").lower() not in ("1", "true", "yes", "on"):
        return {"sent": False, "reason": "disabled"}
    if not new_ads:
        return {"sent": False, "reason": "no new ads"}

    token = settings.get("telegram_token", "").strip()
    chat_id = settings.get("telegram_chat_id", "").strip()
    webhook = settings.get("notify_webhook", "").strip()

    count = len(new_ads)
    first_ad = new_ads[0]
    page_name = first_ad.get("page_name") or "صفحة غير معروفة"
    store_dom = first_ad.get("store_domain") or ""
    platform = first_ad.get("platform") or ""
    link_url = first_ad.get("link_url") or ""
    body = (first_ad.get("body") or "").strip().replace("<", "&lt;").replace(">", "&gt;")
    body_snippet = (body[:140] + "…") if len(body) > 140 else body

    lines = [
        f"🚨 <b>رصد إعلان جديد من المنافس!</b>",
        f"🎯 الهدف: <b>{target}</b>",
        f"📄 الصفحة: <b>{page_name}</b>",
    ]
    if store_dom:
        lines.append(f"🏪 المتجر: <b>{store_dom}</b> {f'({platform})' if platform else ''}")
    if link_url:
        lines.append(f"🔗 رابط المنتج: {link_url}")
    if body_snippet:
        lines.append(f"\n📝 <i>{body_snippet}</i>\n")

    if count > 1:
        lines.append(f"📦 <i>وتم رصد {count - 1} إعلانات أخرى في هذا الفحص.</i>")

    lines.append(f"📊 إجمالي بنك الإعلانات: <b>{total}</b>")
    text = "\n".join(lines)

    result = {"sent": False}
    if token and chat_id:
        result["telegram"] = _telegram_send(token, chat_id, text)
        result["sent"] = result["sent"] or result["telegram"]

    if webhook:
        result["webhook"] = _webhook_send(webhook, {
            "event": "new_ads",
            "target": target,
            "count": count,
            "total": total,
            "sample_ad": first_ad,
        })
        result["sent"] = result["sent"] or result["webhook"]

    return result