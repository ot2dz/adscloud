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
    MONTH_MODE, MONTH_LANGS, MARKET_WILDCARD, STATUS_MODE, SCRAPE_ENABLED,
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

bp = Blueprint("targets", __name__)


bp = Blueprint("targets", __name__)


def _competitor_access(target_id: int):
    """(target, scope_user_id) when the current user may act on it, else (None, _)."""
    t = db.get_target(target_id)
    uid = _scope_user_id()
    if not t:
        return None, uid
    if uid is not None and not db.user_has_target(uid, target_id):
        return None, uid
    return t, uid


def _queue_target_scan(t: dict, source: str) -> Optional[int]:
    """Queue one scrape for a competitor unless one is already waiting/running."""
    if db.has_open_job(t["id"]):
        return None
    t_url = resolve_target_url(t["type"], t["target_value"], country=t.get("country", "DZ"))
    job_id = db.create_job(
        query=t["target_value"], url=t_url, country=t.get("country", "DZ"),
        stores_only=True, source=source, target_id=t["id"], incremental=True,
    )
    db.update_target_scraped(t["id"])
    job_wakeup.set()
    return job_id


@bp.route("/api/targets", methods=["GET"])
@login_required
def api_targets():
    s = db.get_settings()
    return jsonify({"targets": db.list_competitors(user_id=_scope_user_id()),
                    "schedule_enabled": s.get("schedule_enabled", "false") == "true"})


@bp.route("/api/targets", methods=["POST"])
@login_required
def api_add_target():
    cfg = request.get_json(force=True, silent=True) or {}
    raw = (cfg.get("target_value") or cfg.get("query") or "").strip()
    typ = (cfg.get("type") or "keyword").strip().lower()
    country = (cfg.get("country") or "DZ").strip().upper()
    label = (cfg.get("label") or "").strip()
    interval_hours = max(1.0, float(cfg.get("interval_hours") or 12.0))

    if not raw:
        return jsonify({"ok": False, "error": "ط·آ§ط¸â€‍ط¸â€،ط·آ¯ط¸ظ¾ ط¸â€‍ط·آ§ ط¸ظ¹ط¸â€¦ط¸ئ’ط¸â€  ط·آ£ط¸â€  ط¸ظ¹ط¸ئ’ط¸ث†ط¸â€  ط¸ظ¾ط·آ§ط·آ±ط·ط›ط·آ§ط¸â€¹"}), 400
    if typ not in ("page", "domain", "keyword", "custom_url"):
        return jsonify({"ok": False, "error": "ط¸â€ ط¸ث†ط·آ¹ ط·ط›ط¸ظ¹ط·آ± ط·آµط·آ§ط¸â€‍ط·آ­"}), 400
    if len(raw) > 500 or len(label) > 120:
        return jsonify({"ok": False, "error": "ط·آ§ط¸â€‍ط¸â€ڑط¸ظ¹ط¸â€¦ط·آ© ط·آ·ط¸ث†ط¸ظ¹ط¸â€‍ط·آ© ط·آ¬ط·آ¯ط·آ§ط¸â€¹"}), 400

    if typ == "page":
        pid = extract_page_id(raw) or raw
        target_val = pid
        if not label:
            label = f"ط·آµط¸ظ¾ط·آ­ط·آ© {pid}"
        db.upsert_tracked_page(page_id=pid, page_name=label, country=country, source="dashboard")
    elif typ == "domain":
        dom = clean_domain(raw)
        target_val = dom
        if not label:
            label = f"ط¸â€¦ط·ع¾ط·آ¬ط·آ± {dom}"
    else:
        target_val = raw
        if not label:
            label = raw

    tid, created = db.add_user_target(
        int(g.user["id"]), typ, target_val, label=label,
        country=country, interval_hours=interval_hours, source="dashboard")
    if not created:
        return jsonify({"ok": False, "error": "ط¸â€،ط·آ°ط·آ§ ط·آ§ط¸â€‍ط¸â€¦ط¸â€ ط·آ§ط¸ظ¾ط·آ³ ط¸â€¦ط¸ث†ط·آ¬ط¸ث†ط·آ¯ ط¸ظ¾ط¸ظ¹ ط¸â€ڑط·آ§ط·آ¦ط¸â€¦ط·ع¾ط¸ئ’ ط·آ¨ط·آ§ط¸â€‍ط¸ظ¾ط·آ¹ط¸â€‍"}), 400
    # First scan right away so the competitor's ads show up without waiting.
    _queue_target_scan(db.get_target(tid), "new_competitor")
    return jsonify({"ok": True, "target_id": tid})


@bp.route("/api/targets/<int:target_id>", methods=["DELETE"])
@login_required
def api_delete_target(target_id):
    t, uid = _competitor_access(target_id)
    if not t:
        return jsonify({"ok": False, "error": "ط·ط›ط¸ظ¹ط·آ± ط¸â€¦ط¸ث†ط·آ¬ط¸ث†ط·آ¯"}), 404
    if uid is None:
        db.delete_target_everywhere(target_id)   # admin overview: remove for everyone
    else:
        db.remove_user_target(uid, target_id)
    return jsonify({"ok": True})


@bp.route("/api/targets/<int:target_id>", methods=["PATCH"])
@login_required
def api_toggle_target(target_id):
    t, uid = _competitor_access(target_id)
    if not t:
        return jsonify({"ok": False, "error": "ط·ط›ط¸ظ¹ط·آ± ط¸â€¦ط¸ث†ط·آ¬ط¸ث†ط·آ¯"}), 404
    cfg = request.get_json(force=True, silent=True) or {}
    db.set_user_target_enabled(uid, target_id, bool(cfg.get("enabled", True)))
    return jsonify({"ok": True})


@bp.route("/api/targets/<int:target_id>/scan", methods=["POST"])
@login_required
def api_scan_target(target_id):
    t, _uid = _competitor_access(target_id)
    if not t:
        return jsonify({"error": "target not found"}), 404
    job_id = _queue_target_scan(t, "manual_target")
    if job_id is None:
        return jsonify({"ok": True, "already_queued": True})
    manager._emit("log", {"text": f"أ¢ع‘طŒ ط·آ£ط¸عˆط·آ¶ط¸ظ¹ط¸ظ¾ ط¸ظ¾ط·آ­ط·آµ ط¢آ«{t.get('label') or t['target_value']}ط¢آ» ط·آ¥ط¸â€‍ط¸â€° ط¸â€ڑط·آ§ط·آ¦ط¸â€¦ط·آ© ط·آ§ط¸â€‍ط·آ§ط¸â€ ط·ع¾ط·آ¸ط·آ§ط·آ±."})
    return jsonify({"ok": True, "job_id": job_id})


@bp.route("/api/targets/scan-all", methods=["POST"])
@login_required
def api_scan_all_targets():
    """Scan every enabled competitor of the current user (all of them for an admin)."""
    count = 0
    for t in db.list_competitors(user_id=_scope_user_id()):
        if t.get("enabled") and _queue_target_scan(t, "manual_all") is not None:
            count += 1
    return jsonify({"ok": True, "enqueued": count})


# Backward compatibility for keywords
@bp.route("/api/keywords", methods=["GET"])
@admin_required
def api_keywords():
    return jsonify({"keywords": db.list_keywords()})


@bp.route("/api/keywords", methods=["POST"])
@admin_required
def api_add_keyword():
    return api_add_target()


@bp.route("/api/keywords/<int:kw_id>", methods=["DELETE"])
@admin_required
def api_delete_keyword(kw_id):
    return api_delete_target(kw_id)


@bp.route("/api/keywords/<int:kw_id>", methods=["PATCH"])
@admin_required
def api_toggle_keyword(kw_id):
    return api_toggle_target(kw_id)


# أ¢â€‌â‚¬أ¢â€‌â‚¬ Settings / Schedule / Telegram أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬


