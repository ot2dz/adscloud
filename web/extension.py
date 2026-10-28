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
    login_required, admin_required, _scope_user_id, _token_matches,
    LOGIN_MAX_PER_EMAIL, LOGIN_MAX_PER_IP, LOGIN_WINDOW_MIN,
    REGISTER_MAX_PER_IP_HOUR, MIN_PASSWORD_LEN,
)

logger = logging.getLogger("web_app")

bp = Blueprint("extension", __name__)


bp = Blueprint("extension", __name__)


@bp.route("/api/v1/<path:path>", methods=["OPTIONS"])
def api_v1_options(path):
    return ("", 200)


@bp.route("/api/v1/health", methods=["GET", "OPTIONS"])
def api_ext_health():
    payload = {"ok": True, "service": "adra-cloud", "version": "1.0.0"}
    return jsonify({"ok": True, "data": payload, **payload})


def _ext_provided_token() -> str:
    return (request.headers.get("X-Ingest-Token", "")
            or request.headers.get("Authorization", "").replace("Bearer ", "")).strip()


def _ext_user():
    """The active user whose personal extension token was sent, else None."""
    user = db.get_user_by_token(_ext_provided_token())
    if user and user.get("is_active"):
        return user
    return None


def _ext_token_ok() -> bool:
    """Accept a personal user token, or (legacy) the shared INGEST_TOKEN.
    Without either the extension API is closed (never open by default)."""
    if _ext_user():
        return True
    return _token_matches(_ext_provided_token(), os.environ.get("INGEST_TOKEN") or "")


def _ext_user_id():
    user = _ext_user()
    return int(user["id"]) if user else None


def _track_page_for(uid, page_id: str, page_name: str = "") -> None:
    """The extension tracks a page: it becomes one of the user's competitors
    (scraped by the server once for everyone who follows it)."""
    if uid:
        db.add_user_target(uid, "page", page_id, label=page_name or page_id,
                           country=FIXED_COUNTRY, source="extension")
    else:
        db.add_target(type="page", target_value=page_id, label=page_name or page_id,
                      country=FIXED_COUNTRY, interval_hours=12.0)


def _drop_tracked_page(page_id) -> None:
    """Remove a page for everyone (no one tracks it any more)."""
    t = db.find_target("page", str(page_id))
    if t:
        db.delete_target_everywhere(t["id"])
    else:
        db.untrack_page(page_id)


def _untrack_for_user(uid: int, page_id) -> None:
    """One user stops tracking; the page is only dropped when nobody else tracks it."""
    t = db.find_target("page", str(page_id))
    if t:
        db.remove_user_target(uid, t["id"])
    elif db.unlink_user_page(uid, str(page_id)) == 0:
        db.untrack_page(page_id)


@bp.route("/api/v1/auth/login", methods=["POST"])
def api_ext_login():
    """Extension sign-in with the dashboard email/password -> personal token."""
    data = request.get_json(force=True, silent=True) or {}
    email = str(data.get("email") or "").strip().lower()
    password = str(data.get("password") or "")
    user, err, code = _check_credentials(email, password)
    if not user:
        return jsonify({"ok": False, "error": {"message": err}}), code
    db.update_user_login(user["id"])
    token = db.create_api_token(user["id"])
    _audit("api_token_created", "extension login", user=user)
    return jsonify({"ok": True, "data": {"token": token, "email": user["email"], "role": user["role"]}})


@bp.route("/api/v1/auth/me", methods=["GET"])
def api_ext_me():
    user = _ext_user()
    if not user:
        return jsonify({"ok": False, "error": {"message": "unauthorized"}}), 401
    return jsonify({"ok": True, "data": {"email": user["email"], "role": user["role"]}})


@bp.route("/api/v1/ads", methods=["POST"])
def api_ext_save_ad():
    if not _ext_token_ok():
        return jsonify({"ok": False, "error": {"message": "unauthorized"}}), 401
    data = request.get_json(force=True, silent=True) or {}
    ad_id = str(data.get("facebook_ad_id") or data.get("id") or "").strip()
    if not ad_id:
        return jsonify({"ok": False, "error": {"message": "facebook_ad_id required"}}), 400

    preview_img = data.get("preview_image_url") or data.get("previewImageUrl") or ""
    link = data.get("landing_url") or ""
    dom, plat = extract_store_domain(link)
    ad_dict = {
        "ad_id": ad_id,
        "page_id": str(data.get("page_id") or ""),
        "page_name": data.get("page_name") or "",
        "body": data.get("ad_text") or "",
        "link_url": link,
        "store_domain": dom or "",
        "platform": data.get("platform") or plat or "",
        "cta_text": data.get("cta") or "",
        "start_date": data.get("start_date") or "",
        "is_active": True,
        "preview_image_url": preview_img,
    }
    is_new = db.save_ad(ad_dict, query="chrome_extension", country=FIXED_COUNTRY)
    uid = _ext_user_id()
    if uid:
        db.link_user_ad(uid, ad_id)
    manager.events.put({"type": "ad", "data": ad_dict})
    return jsonify({"ok": True, "data": {"saved": True, "is_new": is_new, "ad_id": ad_id}})


@bp.route("/api/v1/pages/track", methods=["POST"])
def api_ext_track_page():
    if not _ext_token_ok():
        return jsonify({"ok": False, "error": {"message": "unauthorized"}}), 401
    data = request.get_json(force=True, silent=True) or {}
    page_id = str(data.get("facebook_page_id") or data.get("pageId") or "").strip()
    page_name = data.get("page_name") or f"صفحة {page_id}"
    if not page_id:
        return jsonify({"ok": False, "error": {"message": "facebook_page_id required"}}), 400

    db.upsert_tracked_page(
        page_id=page_id, page_name=page_name,
        page_url=data.get("page_url") or "",
        page_likes=int(data.get("page_likes") or 0),
        platform=data.get("platform") or "",
        country=FIXED_COUNTRY, source="extension")
    _track_page_for(_ext_user_id(), page_id, page_name)
    logger.info("Chrome Extension tracked page: %s (%s)", page_name, page_id)
    return jsonify({"ok": True, "data": {"tracked": True, "page_id": page_id, "page_name": page_name}})


@bp.route("/api/v1/pages/<page_id>/untrack", methods=["POST"])
def api_ext_untrack_page(page_id):
    if not _ext_token_ok():
        return jsonify({"ok": False, "error": {"message": "unauthorized"}}), 401
    uid = _ext_user_id()
    if uid:
        _untrack_for_user(uid, page_id)
    else:
        _drop_tracked_page(page_id)
    return jsonify({"ok": True, "data": {"untracked": True, "page_id": page_id}})


@bp.route("/api/v1/pages", methods=["GET", "OPTIONS"])
def api_ext_list_pages():
    if not _ext_token_ok():
        return jsonify({"ok": False, "error": {"message": "unauthorized"}}), 401
    pages = db.list_tracked_pages(user_id=_ext_user_id())
    for pg in pages:
        pg.pop("snapshots", None)
    return jsonify({"ok": True, "data": {"pages": pages}})


@bp.route("/api/v1/pages/<page_id>/snapshots", methods=["GET", "OPTIONS"])
def api_ext_page_snapshots(page_id):
    if not _ext_token_ok():
        return jsonify({"ok": False, "error": {"message": "unauthorized"}}), 401
    return jsonify({"ok": True, "data": {"page_id": page_id, "snapshots": db.get_page_snapshots(page_id, limit=200)}})


@bp.route("/api/v1/observations/page-snapshot", methods=["POST"])
def api_ext_page_snapshot():
    if not _ext_token_ok():
        return jsonify({"ok": False, "error": {"message": "unauthorized"}}), 401
    data = request.get_json(force=True, silent=True) or {}
    page_id = str(data.get("facebook_page_id") or "").strip()
    page_name = data.get("page_name") or f"صفحة {page_id}"
    ads = data.get("ads") or []

    if page_id:
        active_count = sum(1 for a in ads if str(a.get("status") or "ACTIVE").upper() == "ACTIVE")
        db.upsert_tracked_page(
            page_id=page_id, page_name=page_name,
            page_url=data.get("page_url") or "",
            country=FIXED_COUNTRY, source="extension")
        db.add_page_snapshot(
            page_id=page_id,
            active_ads_count=active_count,
            total_ads_count=len(ads),
            ad_ids=[str(a.get("facebook_ad_id") or a.get("id") or "") for a in ads],
            source=data.get("source") or "extension_page_scan")
        _track_page_for(_ext_user_id(), page_id, page_name)

    saved_count = 0
    for ad in ads:
        aid = str(ad.get("facebook_ad_id") or ad.get("id") or "").strip()
        if not aid:
            continue
        link = ad.get("landing_url") or ""
        dom, plat = extract_store_domain(link)
        ad_dict = {
            "ad_id": aid,
            "page_id": page_id or str(ad.get("page_id") or ""),
            "page_name": page_name or ad.get("page_name") or "",
            "body": ad.get("ad_text") or "",
            "link_url": link,
            "store_domain": dom or "",
            "platform": ad.get("platform") or plat or "",
            "cta_text": ad.get("cta") or "",
            "start_date": ad.get("start_date") or "",
            "is_active": True,
            "preview_image_url": ad.get("preview_image_url") or ad.get("previewImageUrl") or "",
        }
        if db.save_ad(ad_dict, query=f"page:{page_id}", country=FIXED_COUNTRY):
            saved_count += 1
        manager.events.put({"type": "ad", "data": ad_dict})

    logger.info("Chrome Extension snapshot for page %s: %s ads received, %s new", page_id, len(ads), saved_count)
    return jsonify({
        "ok": True,
        "data": {
            "page_id": page_id,
            "observed_ads_count": len(ads),
            "new_saved": saved_count
        }
    })


@bp.route("/api/v1/sync/ads", methods=["POST"])
def api_ext_sync_ads():
    if not _ext_token_ok():
        return jsonify({"ok": False, "error": {"message": "unauthorized"}}), 401
    data = request.get_json(force=True, silent=True) or {}
    items = data.get("items") or []
    uid = _ext_user_id()
    stored = 0
    for ad in items:
        aid = str(ad.get("facebook_ad_id") or ad.get("id") or "").strip()
        if not aid:
            continue
        link = ad.get("landing_url") or ""
        dom, plat = extract_store_domain(link)
        ad_dict = {
            "ad_id": aid,
            "page_id": str(ad.get("page_id") or ""),
            "page_name": ad.get("page_name") or "",
            "body": ad.get("ad_text") or "",
            "link_url": link,
            "store_domain": dom or "",
            "platform": ad.get("platform") or plat or "",
            "cta_text": ad.get("cta") or "",
            "start_date": ad.get("start_date") or "",
            "is_active": True,
        }
        if db.save_ad(ad_dict, query="sync_history", country=FIXED_COUNTRY):
            stored += 1
        if uid:
            db.link_user_ad(uid, aid)
    return jsonify({"ok": True, "data": {"received": len(items), "stored": stored}})


@bp.route("/api/v1/sync/pages", methods=["POST"])
def api_ext_sync_pages():
    if not _ext_token_ok():
        return jsonify({"ok": False, "error": {"message": "unauthorized"}}), 401
    data = request.get_json(force=True, silent=True) or {}
    items = data.get("items") or []
    uid = _ext_user_id()
    for page in items:
        pid = str(page.get("facebook_page_id") or "").strip()
        if pid:
            db.upsert_tracked_page(
                page_id=pid, page_name=page.get("page_name") or pid,
                page_url=page.get("page_url") or "",
                page_likes=int(page.get("page_likes") or 0),
                country=FIXED_COUNTRY, source="extension")
            _track_page_for(uid, pid, page.get("page_name") or pid)
    return jsonify({"ok": True, "data": {"synced": len(items)}})


# ── Tracked Pages (Dashboard API) ─────────────────────────────────────
@bp.route("/api/tracked-pages", methods=["GET"])
@login_required
def api_tracked_pages():
    return jsonify({"pages": db.list_tracked_pages(user_id=_scope_user_id())})


@bp.route("/api/tracked-pages/<page_id>/snapshots", methods=["GET"])
@login_required
def api_tracked_page_snapshots(page_id):
    uid = _scope_user_id()
    if uid is not None and g.user.get("role") != "admin" and str(page_id) not in db.list_user_page_ids(uid):
        return jsonify({"ok": False, "error": "غير مسموح"}), 403
    return jsonify({"page_id": page_id, "snapshots": db.get_page_snapshots(page_id, limit=200)})


@bp.route("/api/tracked-pages/<page_id>", methods=["DELETE"])
@login_required
def api_untrack_page(page_id):
    uid = _scope_user_id()
    if uid is None:
        _drop_tracked_page(page_id)
    else:
        _untrack_for_user(uid, page_id)
    return jsonify({"ok": True})


@bp.route("/api/tracked-pages", methods=["DELETE"])
@login_required
def api_untrack_all_pages():
    uid = _scope_user_id()
    if uid is None:
        for t in [t for t in db.list_targets() if t.get("type") == "page"]:
            db.delete_target_everywhere(t["id"])
        db.delete_all_tracked_pages()
    else:
        for pid in db.list_user_page_ids(uid):
            _untrack_for_user(uid, pid)
    return jsonify({"ok": True})





