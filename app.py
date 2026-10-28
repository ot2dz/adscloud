# -*- coding: utf-8 -*-
"""
MetaAdsPlaywright web dashboard.

Flask API + SSE dashboard around the HTTP scraper engine. Ads are streamed live
and persisted to PostgreSQL (or SQLite locally).
"""
from __future__ import annotations

import csv
import functools
import io
import json
import logging
import os
import queue
import secrets
import threading
import time
from datetime import datetime, timezone
from typing import Callable, Optional

from datetime import timedelta
from flask import Flask, Response, g, jsonify, render_template, request, send_file, session
from werkzeug.security import check_password_hash, generate_password_hash

from db import db
from web.common import (
    SESSION_DAYS, _start_session, _int_arg, _client_ip, _audit,
    _login_blocked, _check_credentials, _registration_open,
    login_required, admin_required, _scope_user_id,
    LOGIN_MAX_PER_EMAIL, LOGIN_MAX_PER_IP, LOGIN_WINDOW_MIN,
    REGISTER_MAX_PER_IP_HOUR, MIN_PASSWORD_LEN,
)
from engine import scrape
from pages import clean_domain, extract_page_id, resolve_target_url
from store_filter import extract_store_domain
from scrape_manager import (
    ScrapeManager, manager, job_wakeup, _proxy_setting,
    DEFAULT_COUNTRY, FIXED_COUNTRY, FIXED_LANGS, MONTH_MODE, MONTH_LANGS,
    MARKET_WILDCARD, STATUS_MODE, SCRAPE_ENABLED,
)
from background import _queue_month_scrape, _queue_status_check, start_background_threads


def enable_utf8_console() -> None:
    """Best-effort: make stdout/stderr UTF-8 so emoji/Arabic progress prints."""
    try:
        import sys
        for stream in (sys.stdout, sys.stderr):
            if hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8")
    except Exception:
        pass


# Progress lines contain emoji/Arabic; keep them printable on any console.
enable_utf8_console()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("web_app")

app = Flask(__name__)


def _load_secret_key() -> str:
    """The key that signs session cookies. Anyone who knows it can forge a
    cookie, so production refuses to start without a strong one."""
    key = (os.environ.get("SECRET_KEY") or os.environ.get("FLASK_SECRET_KEY") or "").strip()
    if len(key) >= 32:
        return key
    if os.environ.get("ALLOW_INSECURE_SECRET_KEY", "").strip().lower() in ("1", "true", "yes"):
        logging.getLogger("web_app").warning(
            "SECRET_KEY missing/short: using a random key (dev only; sessions reset on restart)")
        return secrets.token_hex(32)
    raise RuntimeError(
        "SECRET_KEY must be set to a random value of at least 32 characters "
        "(e.g. python -c 'import secrets; print(secrets.token_hex(32))'). "
        "For local development only, set ALLOW_INSECURE_SECRET_KEY=1.")


app.secret_key = _load_secret_key()
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,          # not readable from JavaScript
    SESSION_COOKIE_SAMESITE="Lax",          # not sent on cross-site POSTs (CSRF)
    # HTTPS only in production; set SESSION_COOKIE_SECURE=0 for plain-http local runs.
    SESSION_COOKIE_SECURE=os.environ.get("SESSION_COOKIE_SECURE", "1").strip().lower() not in ("0", "false", "no"),
    PERMANENT_SESSION_LIFETIME=timedelta(days=SESSION_DAYS),
)



@app.before_request
def _limit_request_size():
    """Refuse oversized bodies up front (before any handler reads them)."""
    limit = app.config.get("MAX_CONTENT_LENGTH")
    if limit and (request.content_length or 0) > limit:
        return jsonify({"ok": False, "error": "حجم الطلب كبير جداً"}), 413
    return None


@app.before_request
def _resolve_session() -> None:
    """Zero trust: every request re-checks its session in the database.

    A revoked / expired session, or a user that was disabled or deleted, is
    logged out right here; the role always comes from the database, so a
    demoted admin loses admin rights on the very next request. Cookies from
    before server-side sessions (no ``sid``) are rejected too.
    """
    g.user = None
    if not session:
        return
    sid = session.get("sid")
    user = db.get_session_user(sid) if sid else None
    if not user:
        session.clear()
        return
    g.user = user
    session["user_id"] = int(user["id"])
    session["user_email"] = user["email"]
    session["user_role"] = user["role"]
    # Keep last-seen fresh without a write on every request.
    last = (user["_session"].get("last_seen_at") or "")
    try:
        from datetime import datetime as _dt, timezone as _tz
        stale = (_dt.now(_tz.utc) - _dt.fromisoformat(last)).total_seconds() > 300
    except Exception:
        stale = True
    if stale:
        db.touch_session(sid)



# Default targeting policy (DZ by default, can be overridden per request).



# ── Pages ─────────────────────────────────────────────────────────────
def _asset_v() -> str:
    css = os.path.join(app.static_folder, "css", "app.css")
    js_dir = os.path.join(app.static_folder, "js")
    paths = [css] + [os.path.join(js_dir, f) for f in os.listdir(js_dir) if f.endswith(".js")]
    try:
        return str(int(max(os.path.getmtime(p) for p in paths)))
    except OSError:
        return "1"


@app.route("/")
def index():
    return render_template("index.html", asset_v=_asset_v())


# ── CORS & Private Network Access (Chrome Extensions support) ─────────
# Requests bigger than this are refused (extension syncs included).
app.config["MAX_CONTENT_LENGTH"] = int(os.environ.get("MAX_REQUEST_MB", "10") or 10) * 1024 * 1024

_CSP = "; ".join([
    "default-src 'self'",
    # inline handlers + Tailwind's CDN build are still used by the dashboard
    "script-src 'self' 'unsafe-inline' https://cdn.tailwindcss.com",
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
    "font-src 'self' https://fonts.gstatic.com data:",
    "img-src 'self' data: https:",
    "connect-src 'self'",
    "frame-ancestors 'none'",
    "base-uri 'self'",
    "form-action 'self'",
    "object-src 'none'",
])


@app.after_request
def add_security_headers(response):
    # CORS only for the browser-extension API; the dashboard is same-origin.
    if request.path.startswith("/api/v1/"):
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "Authorization, Content-Type, X-Ingest-Token"
        response.headers["Access-Control-Allow-Private-Network"] = "true"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers.setdefault("Content-Security-Policy", _CSP)
    if app.config.get("SESSION_COOKIE_SECURE"):
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    if request.path.startswith("/api/"):
        response.headers.setdefault("Cache-Control", "no-store")
    return response


@app.errorhandler(Exception)
def _unhandled_error(exc):
    """Never leak stack traces / internal messages to the client."""
    from werkzeug.exceptions import HTTPException
    if isinstance(exc, HTTPException):
        return jsonify({"ok": False, "error": exc.description or exc.name}), exc.code
    logger.exception("unhandled error on %s", request.path)
    return jsonify({"ok": False, "error": "حدث خطأ داخلي. حاول مرة أخرى."}), 500


# ── Authentication Endpoints ──────────────────────────────────────────
# ── Engine control ───────────────────────────────────────────────────

from web.auth import bp as auth_bp
from web.scrape import bp as scrape_bp
from web.ads import bp as ads_bp
from web.targets import bp as targets_bp
from web.settings import bp as settings_bp
from web.extension import bp as extension_bp

for _bp in (auth_bp, scrape_bp, ads_bp, targets_bp, settings_bp, extension_bp):
    app.register_blueprint(_bp)

if __name__ == "__main__":
    print("=" * 60)
    print(f"  MetaAds (HTTP, no browser) dashboard -> http://127.0.0.1:{os.environ.get('PORT', '5002')}")
    print(f"  DB backend: {'PostgreSQL' if db.is_postgres else 'SQLite'}")
    print(f"  Proxy: {'configured' if os.environ.get('META_ADS_PROXY') else 'none'}")
    print("=" * 60)
    start_background_threads()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5002")), debug=False)
else:
    start_background_threads()