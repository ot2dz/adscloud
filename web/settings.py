import os
import csv
import functools
import io
import json
import logging
import secrets
import time
from datetime import datetime, timezone
from typing import Callable, Optional

from flask import Blueprint, Response, g, jsonify, render_template, request, send_file, session
from werkzeug.security import check_password_hash, generate_password_hash

from db import db
from pages import clean_domain, extract_page_id, resolve_target_url
from store_filter import extract_store_domain
from scrape_manager import (
    manager, job_wakeup, DEFAULT_COUNTRY, FIXED_COUNTRY, FIXED_LANGS,
    MONTH_MODE, MONTH_LANGS, MARKET_WILDCARD, STATUS_MODE, SCRAPE_ENABLED, _proxy_setting,
)
from background import _queue_month_scrape, _queue_status_check
from web.common import (
    SESSION_DAYS, _start_session, _int_arg, _client_ip, _audit,
    _login_blocked, _check_credentials, _registration_open,
    login_required, admin_required, _scope_user_id,
    LOGIN_MAX_PER_EMAIL, LOGIN_MAX_PER_IP, LOGIN_WINDOW_MIN,
    REGISTER_MAX_PER_IP_HOUR, MIN_PASSWORD_LEN,
)

logger = logging.getLogger("web_app")

bp = Blueprint("settings", __name__)


bp = Blueprint("settings", __name__)


@bp.route("/api/settings", methods=["GET"])
@admin_required
def api_get_settings():
    s = db.get_settings()
    return jsonify({
        "schedule_enabled": s.get("schedule_enabled", "false"),
        "interval_hours": s.get("interval_hours", "12"),
        "last_scheduled_at": s.get("last_scheduled_at", ""),
        "notify_enabled": s.get("notify_enabled", "false"),
        "telegram_token": s.get("telegram_token", ""),
        "has_telegram_token": bool(s.get("telegram_token")),
        "telegram_chat_id": s.get("telegram_chat_id", ""),
        "notify_webhook": s.get("notify_webhook", ""),
        "entity_stats_enabled": s.get("entity_stats_enabled", "true"),
        "registration_open": s.get("registration_open", "false"),
        "meta_ads_proxy": s.get("meta_ads_proxy", ""),
        "has_meta_ads_proxy": bool(s.get("meta_ads_proxy")),
        "env_meta_ads_proxy": bool(os.environ.get("META_ADS_PROXY")),
        "blocked_domains": s.get("blocked_domains", ""),
        "cta_filter_enabled": s.get("cta_filter_enabled", "true"),
        "cta_allowlist": s.get("cta_allowlist", ",".join(db.DEFAULT_CTA_ALLOWLIST)),
        "month_rescan_enabled": s.get("month_rescan_enabled", "true"),
        "month_rescan_hour": s.get("month_rescan_hour", "23"),
        "month_rescan_last": s.get("month_rescan_last", ""),
        "status_check_enabled": s.get("status_check_enabled", "true"),
        "status_check_hour": s.get("status_check_hour", "3"),
        "status_check_last": s.get("status_check_last", ""),
        "status_check_summary": s.get("status_check_summary", ""),
    })


@bp.route("/api/settings", methods=["POST"])
@admin_required
def api_set_settings():
    cfg = request.get_json(force=True, silent=True) or {}
    if "schedule_enabled" in cfg:
        db.set_setting("schedule_enabled", "true" if cfg["schedule_enabled"] else "false")
    if "interval_hours" in cfg:
        db.set_setting("interval_hours", str(cfg["interval_hours"]))
    if "notify_enabled" in cfg:
        db.set_setting("notify_enabled", "true" if cfg["notify_enabled"] else "false")
    if "telegram_token" in cfg:
        db.set_setting("telegram_token", cfg["telegram_token"].strip())
    if "telegram_chat_id" in cfg:
        db.set_setting("telegram_chat_id", str(cfg["telegram_chat_id"]).strip())
    if "notify_webhook" in cfg:
        db.set_setting("notify_webhook", cfg["notify_webhook"].strip())
    if "entity_stats_enabled" in cfg:
        db.set_setting("entity_stats_enabled", "true" if cfg["entity_stats_enabled"] else "false")
    if "registration_open" in cfg:
        db.set_setting("registration_open", "true" if cfg["registration_open"] else "false")
    if "cta_filter_enabled" in cfg:
        db.set_setting("cta_filter_enabled", "true" if cfg["cta_filter_enabled"] else "false")
    if "cta_allowlist" in cfg:
        import re as _re
        items = _re.split(r"[\s,]+", str(cfg["cta_allowlist"] or "").upper())
        db.set_setting("cta_allowlist", ",".join(sorted({x for x in items if _re.fullmatch(r"[A-Z_]{2,40}", x)})))
    if "month_rescan_enabled" in cfg:
        db.set_setting("month_rescan_enabled", "true" if cfg["month_rescan_enabled"] else "false")
    if "month_rescan_hour" in cfg:
        try:
            db.set_setting("month_rescan_hour", str(max(0, min(23, int(cfg["month_rescan_hour"])))))
        except (TypeError, ValueError):
            pass
    if "status_check_enabled" in cfg:
        db.set_setting("status_check_enabled", "true" if cfg["status_check_enabled"] else "false")
    if "status_check_hour" in cfg:
        try:
            db.set_setting("status_check_hour", str(max(0, min(23, int(cfg["status_check_hour"])))))
        except (TypeError, ValueError):
            pass
    if "meta_ads_proxy" in cfg:
        db.set_setting("meta_ads_proxy", str(cfg["meta_ads_proxy"] or "").strip())
    purged = None
    if "blocked_domains" in cfg:
        db.set_setting("blocked_domains", str(cfg["blocked_domains"] or "").strip())
        db._load_blocked_domains()
        purged = db.purge_blocked_ads()
        if purged:
            manager._emit("log", {"text": f"ظ‹ع؛آ§آ¹ ط·آ­ط¸عˆط·آ°ط¸ظ¾ {purged} ط·آ¥ط·آ¹ط¸â€‍ط·آ§ط¸â€ ط¸â€¹ط·آ§ ط¸â€‍ط·آ´ط·آ±ط¸ئ’ط·آ§ط·ع¾ ط¸ئ’ط·آ¨ط·آ±ط¸â€° ط¸â€¦ط¸â€  ط·آ§ط¸â€‍ط¸â€ڑط·آ§ط·آ¹ط·آ¯ط·آ©."})
    _audit("settings_changed", ",".join(sorted(k for k in cfg if k not in ("telegram_token", "meta_ads_proxy"))) +
           (",telegram_token" if "telegram_token" in cfg else "") +
           (",meta_ads_proxy" if "meta_ads_proxy" in cfg else ""))
    return jsonify({"ok": True, "purged": purged})


@bp.route("/api/proxy/test", methods=["POST"])
@admin_required
def api_proxy_test():
    """Try the given (or saved) proxy against a tiny IP endpoint."""
    cfg = request.get_json(force=True, silent=True) or {}
    spec = (str(cfg.get("meta_ads_proxy") or "")).strip() or _proxy_setting() \
        or (os.environ.get("META_ADS_PROXY") or "").strip()
    if not spec:
        return jsonify({"ok": False, "error": "ط¸â€‍ط·آ§ ط¸ظ¹ط¸ث†ط·آ¬ط·آ¯ ط·آ¨ط·آ±ط¸ث†ط¸ئ’ط·آ³ط¸ظ¹ ط¸â€¦ط·آ¶ط·آ¨ط¸ث†ط·آ·"}), 400

    from meta_ads_collector.proxy_pool import parse_proxy, _redact_proxy
    from curl_cffi import requests as creq

    results = []
    for raw in [p.strip() for p in spec.split(",") if p.strip()][:5]:
        try:
            purl = parse_proxy(raw)
        except Exception as exc:  # noqa: BLE001
            results.append({"proxy": "proxy", "ok": False, "error": f"ط·آµط¸ظ¹ط·ط›ط·آ© ط·ط›ط¸ظ¹ط·آ± ط·آµط·آ­ط¸ظ¹ط·آ­ط·آ©: {exc}"})
            continue
        label = _redact_proxy(purl)
        try:
            r = creq.get("https://api.ipify.org?format=json",
                         proxies={"http": purl, "https": purl},
                         impersonate="chrome", timeout=25)
            if r.status_code == 200:
                try:
                    ip = (r.json() or {}).get("ip", "")
                except Exception:
                    ip = (r.text or "").strip()
                results.append({"proxy": label, "ok": True, "ip": ip})
            else:
                results.append({"proxy": label, "ok": False, "error": f"HTTP {r.status_code}"})
        except Exception as exc:  # noqa: BLE001
            results.append({"proxy": label, "ok": False, "error": str(exc)[:200]})
    return jsonify({"ok": any(x["ok"] for x in results), "results": results})


@bp.route("/api/telegram/test", methods=["POST"])
@admin_required
def api_telegram_test():
    cfg = request.get_json(force=True, silent=True) or {}
    s = db.get_settings()
    token = (cfg.get("telegram_token") or s.get("telegram_token") or "").strip()
    chat_id = (cfg.get("telegram_chat_id") or s.get("telegram_chat_id") or "").strip()
    from notify import send_telegram_test
    ok, msg = send_telegram_test(token, chat_id)
    return jsonify({"ok": ok, "message": msg})


@bp.route("/api/schedule/run-now", methods=["POST"])
@admin_required
def api_schedule_run_now():
    targets = [t for t in db.list_targets() if t.get("enabled")]
    count = 0
    for t in targets:
        if db.has_open_job(t.get("id")):
            continue
        t_url = resolve_target_url(t.get("type", "keyword"), t.get("target_value", ""), country=t.get("country", "DZ"))
        db.create_job(
            query=t.get("target_value", ""),
            url=t_url,
            country=t.get("country", "DZ"),
            stores_only=True,
            source="schedule_manual",
            target_id=t.get("id"),
            incremental=True,
        )
        db.update_target_scraped(t["id"])
        count += 1
    manager.events.put({"type": "log", "data": {"text": f"أ¢ع‘طŒ ط·ع¾ط¸â€¦ ط·آ¥ط·آ·ط¸â€‍ط·آ§ط¸â€ڑ ط¸ظ¾ط·آ­ط·آµ ط·آ´ط·آ§ط¸â€¦ط¸â€‍ ط¸â€‍ط¸â‚¬ {count} ط·آ£ط¸â€،ط·آ¯ط·آ§ط¸ظ¾ ط¸â€¦ط·آ¬ط·آ¯ط¸ث†ط¸â€‍ط·آ© ط·آ§ط¸â€‍ط·آ¢ط¸â€ ."}})
    return jsonify({"ok": True, "enqueued": count})


# أ¢â€‌â‚¬أ¢â€‌â‚¬ Chrome Extension (adlibrary) Integration API أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬



