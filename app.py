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
from engine import scrape
from pages import clean_domain, extract_page_id, resolve_target_url
from store_filter import extract_store_domain


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
SESSION_DAYS = float(os.environ.get("SESSION_DAYS", "7") or 7)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,          # not readable from JavaScript
    SESSION_COOKIE_SAMESITE="Lax",          # not sent on cross-site POSTs (CSRF)
    # HTTPS only in production; set SESSION_COOKIE_SECURE=0 for plain-http local runs.
    SESSION_COOKIE_SECURE=os.environ.get("SESSION_COOKIE_SECURE", "1").strip().lower() not in ("0", "false", "no"),
    PERMANENT_SESSION_LIFETIME=timedelta(days=SESSION_DAYS),
)


def _proxy_setting() -> str:
    """Proxy saved from Settings (empty -> engine falls back to META_ADS_PROXY)."""
    try:
        return (db.get_settings().get("meta_ads_proxy") or "").strip()
    except Exception:
        return ""


def _start_session(user: dict) -> None:
    """Log a user in: a fresh server-side session (never reuse an old id)."""
    session.clear()
    session.permanent = True
    session["sid"] = db.create_session(
        int(user["id"]), days=SESSION_DAYS,
        ip=request.headers.get("X-Forwarded-For", request.remote_addr or "").split(",")[0].strip(),
        user_agent=request.headers.get("User-Agent", ""))
    session["user_id"] = int(user["id"])
    session["user_email"] = user["email"]
    session["user_role"] = user["role"]


def _int_arg(name: str, default: int) -> int:
    try:
        return int(request.args.get(name, default) or default)
    except (TypeError, ValueError):
        return default


def _client_ip() -> str:
    """Client address (first X-Forwarded-For hop behind the Coolify proxy)."""
    return (request.headers.get("X-Forwarded-For", "") or request.remote_addr or "").split(",")[0].strip()[:64]


def _audit(event: str, detail: str = "", user: Optional[dict] = None, email: str = "") -> None:
    u = user if user is not None else getattr(g, "user", None)
    db.log_security_event(event, user_id=(int(u["id"]) if u else None),
                          email=email or (u or {}).get("email", ""), ip=_client_ip(), detail=detail)


# Brute-force limits (failed logins in a sliding window).
LOGIN_MAX_PER_EMAIL = int(os.environ.get("LOGIN_MAX_PER_EMAIL", "5") or 5)
LOGIN_MAX_PER_IP = int(os.environ.get("LOGIN_MAX_PER_IP", "20") or 20)
LOGIN_WINDOW_MIN = int(os.environ.get("LOGIN_WINDOW_MIN", "15") or 15)
REGISTER_MAX_PER_IP_HOUR = 5
MIN_PASSWORD_LEN = 10


def _login_blocked(email: str) -> bool:
    ip = _client_ip()
    return (db.count_failed_attempts("login", LOGIN_WINDOW_MIN, email=email) >= LOGIN_MAX_PER_EMAIL
            or db.count_failed_attempts("login", LOGIN_WINDOW_MIN, ip=ip) >= LOGIN_MAX_PER_IP)


def _check_credentials(email: str, password: str):
    """Shared by dashboard and extension login: (user, error, status).
    Every attempt is throttled and recorded; failures are audited."""
    ip = _client_ip()
    if _login_blocked(email):
        _audit("login_blocked", "too many failed attempts", user=None, email=email)
        return None, f"محاولات كثيرة فاشلة. حاول مجدداً بعد {LOGIN_WINDOW_MIN} دقيقة.", 429
    user = db.get_user_by_email(email) if email else None
    if not user or not password or not check_password_hash(user["password_hash"], password):
        db.record_auth_attempt("login", email, ip, False)
        _audit("login_failed", "", user=None, email=email)
        return None, "البريد الإلكتروني أو كلمة المرور غير صحيحة", 401
    if not user.get("is_active"):
        db.record_auth_attempt("login", email, ip, False)
        _audit("login_refused_disabled", "", user=user)
        return None, "هذا الحساب معطل. يرجى التواصل مع المدير لتفعيله", 403
    db.record_auth_attempt("login", email, ip, True)
    return user, None, 200


def _registration_open() -> bool:
    return db.get_settings().get("registration_open", "false").lower() in ("1", "true", "yes", "on")


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


def login_required(f):
    @functools.wraps(f)
    def decorated_function(*args, **kwargs):
        if not getattr(g, "user", None):
            return jsonify({"ok": False, "error": "يرجى تسجيل الدخول أولاً", "unauthorized": True}), 401
        return f(*args, **kwargs)
    return decorated_function


def admin_required(f):
    @functools.wraps(f)
    def decorated_function(*args, **kwargs):
        if not getattr(g, "user", None):
            return jsonify({"ok": False, "error": "يرجى تسجيل الدخول أولاً", "unauthorized": True}), 401
        if g.user.get("role") != "admin":
            return jsonify({"ok": False, "error": "هذا الإجراء يتطلب صلاحيات مدير النظام (Admin)", "forbidden": True}), 403
        return f(*args, **kwargs)
    return decorated_function


def _scope_user_id():
    """Whose data the current dashboard request may see.

    A regular user only ever sees their own saves and tracked pages. An admin
    sees everything, or one user's data when ``?user_id=`` is given.
    """
    # Identity and role come from the server-side session (g.user), never
    # from the request: only an admin may pick another user's view.
    if g.user.get("role") == "admin":
        raw = (request.args.get("user_id") or "").strip()
        return int(raw) if raw.isdigit() else None
    return int(g.user["id"])


# Default targeting policy (DZ by default, can be overridden per request).
DEFAULT_COUNTRY = os.environ.get("DEFAULT_COUNTRY", "DZ").strip().upper() or "DZ"
FIXED_COUNTRY = DEFAULT_COUNTRY
FIXED_LANGS = ["ar"]

# Local scraping needs Chromium browser / worker or HTTP collector.
SCRAPE_ENABLED = os.environ.get("SCRAPE_ENABLED", "true").strip().lower() in ("1", "true", "yes", "on")


class ScrapeManager:
    def __init__(self):
        self.thread = None
        self.stop_event = threading.Event()
        self.events = queue.Queue()
        self.subscribers: list = []
        self.state = {"running": False, "query": "", "scraped": 0, "stored": 0,
                      "stores": 0, "scanned": 0, "reported": None, "error": "",
                      "log": []}

    def _emit(self, kind: str, data: dict) -> None:
        item = {"type": kind, "data": data}
        self.events.put(item)
        # Fan-out to every connected SSE client so no open tab misses events.
        for q in list(self.subscribers):
            try:
                q.put_nowait(item)
            except Exception:
                pass

    def start(self, query: str, country: str = DEFAULT_COUNTRY, exact: bool = False,
              stores_only: bool = False, sort_mode: str = "total_impressions",
              sweep: bool = False, url: str = "", start_date_min: str = "",
              start_date_max: str = "", date_label: str = "",
              date_scope: str = "impressions", resume: Optional[dict] = None,
              on_checkpoint: Optional[Callable[[Optional[dict]], None]] = None,
              job_id: Optional[int] = None, base_count: int = 0) -> bool:
        """Start a scrape thread.

        ``resume`` is a checkpoint ``{"pass": n, "cursor": "..."}`` from an
        earlier interrupted run; ``on_checkpoint`` receives the new one after
        every fully-processed page (``None`` once everything is done).
        ``base_count`` = ads already collected by earlier runs of the same
        job, so counters continue instead of restarting at zero.
        """
        if self.state["running"]:
            return False
        self.stop_event.clear()
        while not self.events.empty():
            self.events.get_nowait()
        self.state.update({"running": True, "query": query or url, "scraped": base_count,
                           "stored": 0, "stores": 0, "reported": None, "error": "",
                           "log": [], "outcome": None, "job_id": job_id})
        self.thread = threading.Thread(
            target=self._run,
            args=(query, country, exact, stores_only, sort_mode, sweep, url, start_date_min, start_date_max, date_label, date_scope),
            kwargs={"resume": resume, "on_checkpoint": on_checkpoint, "base_count": base_count},
            daemon=True)
        self.thread.start()
        return True

    def stop(self) -> None:
        self.stop_event.set()

    def _run(self, query, country, exact, stores_only, sort_mode, sweep, url, start_date_min, start_date_max, date_label, date_scope="impressions",
             resume: Optional[dict] = None, on_checkpoint=None, base_count: int = 0) -> None:
        country = (country or DEFAULT_COUNTRY).strip().upper()
        languages = FIXED_LANGS
        proxy_spec = _proxy_setting()
        seen_stores: set = set()
        seen_ad_ids: set = set()
        resume = resume if isinstance(resume, dict) else {}
        # Outcome read by the job runner: done | stopped | interrupted | failed.
        # Start from the incoming checkpoint so an immediate failure keeps it.
        outcome = {"status": "done", "checkpoint": resume or None, "error": "", "scanned": 0, "ad_ids": []}

        def make_checkpointer(pass_idx: int):
            def _cp(cursor):
                # Cursor None = this pass is exhausted -> next pass from scratch.
                cp = {"pass": pass_idx, "cursor": cursor} if cursor else {"pass": pass_idx + 1, "cursor": None}
                outcome["checkpoint"] = cp
                if on_checkpoint:
                    try:
                        on_checkpoint(cp)
                    except Exception:
                        logger.exception("checkpoint persist failed")
            return _cp

        def absorb(result) -> bool:
            """Fold a ScrapeResult into the outcome; True = keep going."""
            outcome["scanned"] += int(getattr(result, "scanned", 0) or 0)
            if result.stopped:
                outcome["status"] = "stopped"
            elif result.failed:
                outcome.update(status="failed", error=result.error)
            elif result.interrupted:
                outcome.update(status="interrupted", error=result.error)
            else:
                return True
            return False

        def progress(msg):
            print(f"  ▶ [SCRAPER] {msg}", flush=True)
            self.state["log"].append(msg)
            self.state["log"] = self.state["log"][-200:]
            self._emit("log", {"text": msg})

        def on_reported(count):
            if count is not None:
                self.state["reported"] = count
                self._emit("reported", {"reported": count, "query": query or url})

        def on_scanned(n):
            self.state["scanned"] = n
            self._emit("scrape_progress", {
                "scraped": (base_count + len(seen_ad_ids)),
                "scanned": n,
                "reported": self.state["reported"],
                "stores": len(seen_stores),
                "stored": self.state["stored"],
                "query": query or url,
            })

        def on_ad(ad):
            if self.stop_event.is_set():
                return
            d = ad.to_dict()
            d["keywords"] = [query] if query else []
            aid = d.get("ad_id")
            if aid and aid not in seen_ad_ids:
                seen_ad_ids.add(aid)
                stored = db.save_ad(d, query=query, country=country)
                self.state["scraped"] = (base_count + len(seen_ad_ids))
                if stored:
                    self.state["stored"] += 1
                if ad.store_domain:
                    seen_stores.add(ad.store_domain)
                self.state["stores"] = len(seen_stores)
                self._emit("scrape_progress", {
                    "scraped": (base_count + len(seen_ad_ids)),
                    "scanned": self.state.get("scanned", 0),
                    "reported": self.state["reported"],
                    "stores": len(seen_stores),
                    "stored": self.state["stored"],
                    "query": query or url,
                })
                if not stores_only or ad.store_domain:
                    self._emit("ad", d)

        try:
            self._emit("started", {"query": query or url, "country": country})

            if sweep and not (start_date_min or start_date_max):
                from slicing import build_deep_passes
                passes = build_deep_passes(query=query, url=url, country=country,
                                           exact=exact, sort_mode=sort_mode, months_back=12)
                total_passes = len(passes)
                progress(f"🚀 بدء التغطية الموسعة (Deep Sweep): تقسيم البحث إلى {total_passes} شريحة شهرية.")
                start_pass = max(1, int(resume.get("pass") or 1))
                if start_pass > 1:
                    progress(f"↩️ استئناف التغطية من الشريحة {start_pass}/{total_passes}.")

                for idx, p in enumerate(passes, 1):
                    if idx < start_pass:
                        continue
                    if self.stop_event.is_set():
                        outcome["status"] = "stopped"
                        progress("⏹ تم إيقاف السحب بطلب من المستخدم.")
                        break
                    progress(f"↪ شريحة {idx}/{total_passes} (شهر {p['label']}) — إجمالي مجمع: {(base_count + len(seen_ad_ids))} إعلان.")
                    self._emit("slice_update", {
                        "pass": idx,
                        "total_passes": total_passes,
                        "label": p["label"],
                        "accumulated": (base_count + len(seen_ad_ids))
                    })
                    r = scrape(query=query, country=country, url=p["url"],
                               stores_only=False, progress=progress, on_ad=on_ad,
                               on_reported=on_reported, on_scanned=on_scanned,
                               date_scope=date_scope, proxy=proxy_spec,
                               should_stop=lambda: self.stop_event.is_set(),
                               resume_cursor=(resume.get("cursor") if idx == start_pass else None),
                               on_checkpoint=make_checkpointer(idx))
                    if r.failed:
                        # A broken slice must not block the rest of the sweep.
                        progress(f"⚠️ تجاوز الشريحة {idx} بسبب خطأ دائم: {r.error}")
                        make_checkpointer(idx)(None)
                        continue
                    if not absorb(r):
                        break

            else:
                if date_label:
                    progress(f"📅 نطاق السحب الزمني: {date_label}")
                result = scrape(query, country=country, url=url or None, exact_phrase=exact,
                                sort_mode=sort_mode, languages=languages,
                                start_date_min=start_date_min or None,
                                start_date_max=start_date_max or None,
                                stores_only=stores_only,
                                progress=progress, on_ad=on_ad, on_reported=on_reported,
                                on_scanned=on_scanned,
                                should_stop=lambda: self.stop_event.is_set(),
                                date_scope=date_scope, proxy=proxy_spec,
                                resume_cursor=resume.get("cursor"),
                                on_checkpoint=make_checkpointer(1))
                if result.reported_count is not None:
                    self.state["reported"] = result.reported_count
                absorb(result)

            # Never finish silently on an empty run: Meta answers HTTP 200 with
            # zero results when the egress IP is soft-blocked, which looks like
            # "success" but means nothing was collected.
            if (base_count + len(seen_ad_ids)) == 0:
                reported = self.state.get("reported")
                if reported is None or reported == 0:
                    msg = ("Meta أعادت 0 نتيجة. الأسباب المحتملة: (1) IP أو البروكسي محجوب من مكتبة "
                           "الإعلانات، (2) البروكسي لا يغيّر عنوان الخروج (نفس IP السيرفر)، أو (3) لا "
                           "توجد نتائج مطابقة للكلمة/الفترة. استخدم «🩺 تشخيص السحب» في الإعدادات.")
                else:
                    msg = (f"Meta ترى {reported} إعلانًا لكن لم يُحفظ أي منها — كلها استُبعدت بفلتر "
                           f"المتاجر أو نطاق تاريخ الإطلاق.")
                self.state["error"] = msg
                outcome["status"] = "empty"
                outcome["error"] = msg
                self._emit("error", {"message": msg})

            self._emit("finished", {
                "status": outcome["status"],
                "error": outcome["error"],
                "query": query or url,
                "scraped": (base_count + len(seen_ad_ids)),
                "store_ads": (base_count + len(seen_ad_ids)),
                "stored": self.state["stored"],
                "stores": len(seen_stores),
                "reported": self.state["reported"],
            })
            logger.info("Scrape finished: %s ads, %s stores", (base_count + len(seen_ad_ids)), len(seen_stores))
        except Exception as exc:  # pragma: no cover
            logger.exception("scrape failed")
            outcome.update(status="interrupted", error=str(exc))
            self.state["error"] = str(exc)
            self._emit("error", {"message": str(exc)})
        finally:
            outcome["ad_ids"] = sorted(seen_ad_ids)
            self.state["outcome"] = outcome
            self.state["running"] = False
            self._emit("done", {})


manager = ScrapeManager()
# Set when a job is queued so the runner claims it at once (no polling delay).
job_wakeup = threading.Event()


# ── Pages ─────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html")


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
@app.route("/api/auth/me", methods=["GET"])
def api_auth_me():
    user = getattr(g, "user", None)  # resolved from the server-side session
    if not user:
        return jsonify({"authenticated": False})
    return jsonify({
        "authenticated": True,
        "user": {
            "id": user["id"],
            "email": user["email"],
            "role": user["role"],
        }
    })


@app.route("/api/auth/login", methods=["POST"])
def api_auth_login():
    data = request.get_json(force=True, silent=True) or {}
    email = str(data.get("email") or "").strip().lower()
    password = str(data.get("password") or "")
    if not email or not password:
        return jsonify({"ok": False, "error": "البريد الإلكتروني وكلمة المرور مطلوبان"}), 400

    user, err, code = _check_credentials(email, password)
    if not user:
        return jsonify({"ok": False, "error": err}), code

    _start_session(user)
    db.update_user_login(user["id"])
    _audit("login_success", "dashboard", user=user)
    return jsonify({
        "ok": True,
        "user": {
            "id": user["id"],
            "email": user["email"],
            "role": user["role"],
        }
    })


@app.route("/api/auth/register", methods=["POST"])
def api_auth_register():
    if not _registration_open():
        return jsonify({"ok": False, "error": "التسجيل مغلق حالياً. اطلب من المدير فتحه أو إنشاء حسابك."}), 403
    if db.count_attempts("register", 60, _client_ip()) >= REGISTER_MAX_PER_IP_HOUR:
        return jsonify({"ok": False, "error": "محاولات تسجيل كثيرة من هذا الجهاز. حاول لاحقاً."}), 429
    data = request.get_json(force=True, silent=True) or {}
    email = str(data.get("email") or "").strip().lower()
    password = str(data.get("password") or "")
    if not email or not password:
        return jsonify({"ok": False, "error": "يرجى ملء جميع الحقول المطلوبة"}), 400
    import re as _re
    if len(email) > 254 or not _re.fullmatch(r"[^@\s<>\"'`]+@[^@\s<>\"'`]+\.[A-Za-z]{2,}", email):
        return jsonify({"ok": False, "error": "البريد الإلكتروني غير صالح"}), 400
    if len(password) < MIN_PASSWORD_LEN:
        return jsonify({"ok": False, "error": f"كلمة المرور يجب أن لا تقل عن {MIN_PASSWORD_LEN} أحرف"}), 400
    if password.lower() == email or password.lower() in email or len(set(password)) < 4:
        return jsonify({"ok": False, "error": "كلمة المرور ضعيفة جداً"}), 400
    db.record_auth_attempt("register", email, _client_ip(), True)

    existing = db.get_user_by_email(email)
    if existing:
        return jsonify({"ok": False, "error": "البريد الإلكتروني مسجل مسبقاً"}), 400

    hashed = generate_password_hash(password)
    user_id = db.create_user(email=email, password_hash=hashed, role="user", is_active=1)
    if not user_id:
        return jsonify({"ok": False, "error": "فشل إنشاء الحساب، يرجى المحاولة لاحقاً"}), 500

    _start_session({"id": user_id, "email": email, "role": "user"})
    _audit("register", "", user={"id": user_id, "email": email})
    return jsonify({
        "ok": True,
        "user": {
            "id": user_id,
            "email": email,
            "role": "user",
        }
    })


@app.route("/api/auth/logout", methods=["POST"])
def api_auth_logout():
    """Revokes the session on the server: a copied cookie stops working too."""
    sid = session.get("sid")
    if sid:
        db.revoke_session(sid)
        _audit("logout")
    session.clear()
    return jsonify({"ok": True})


@app.route("/api/auth/logout-all", methods=["POST"])
@login_required
def api_auth_logout_all():
    """Log the current user out on every device / browser."""
    n = db.revoke_user_sessions(int(g.user["id"]))
    t = db.revoke_api_tokens(int(g.user["id"]))  # browser extensions too
    _audit("logout_all", f"{n} sessions, {t} extension tokens revoked")
    session.clear()
    return jsonify({"ok": True, "revoked": n, "extension_tokens": t})


@app.route("/api/auth/config", methods=["GET"])
def api_auth_config():
    """Public: what the login screen may offer."""
    return jsonify({"registration_open": _registration_open(), "min_password_length": MIN_PASSWORD_LEN})


@app.route("/api/auth/extension-tokens", methods=["GET"])
@login_required
def api_my_extension_tokens():
    return jsonify({"active": db.count_api_tokens(int(g.user["id"]))})


@app.route("/api/auth/extension-tokens/revoke", methods=["POST"])
@login_required
def api_revoke_my_extension_tokens():
    """Disconnect every browser extension signed in to this account."""
    n = db.revoke_api_tokens(int(g.user["id"]))
    _audit("api_tokens_revoked", f"{n} tokens (self)")
    return jsonify({"ok": True, "revoked": n})


# ── Admin User Management Endpoints ────────────────────────────────────
@app.route("/api/admin/users", methods=["GET"])
@admin_required
def api_admin_users():
    return jsonify({"users": db.list_users()})


@app.route("/api/admin/users/<int:user_id>/toggle-status", methods=["POST"])
@admin_required
def api_admin_toggle_user_status(user_id):
    if user_id == int(g.user["id"]):
        return jsonify({"ok": False, "error": "لا يمكنك تعطيل حسابك الحالي"}), 400
    user = db.get_user_by_id(user_id)
    if not user:
        return jsonify({"ok": False, "error": "المستخدم غير موجود"}), 404
    new_status = not bool(user.get("is_active"))
    db.update_user_status(user_id, new_status)
    _audit("user_enabled" if new_status else "user_disabled", f"target={user['email']}")
    return jsonify({"ok": True, "is_active": new_status})


@app.route("/api/admin/users/<int:user_id>/role", methods=["POST"])
@admin_required
def api_admin_update_user_role(user_id):
    if user_id == int(g.user["id"]):
        return jsonify({"ok": False, "error": "لا يمكنك تغيير رتبة حسابك الحالي"}), 400
    data = request.get_json(force=True, silent=True) or {}
    new_role = "admin" if data.get("role") == "admin" else "user"
    target = db.get_user_by_id(user_id)
    if not target:
        return jsonify({"ok": False, "error": "المستخدم غير موجود"}), 404
    db.update_user_role(user_id, new_role)
    _audit("role_changed", f"target={target['email']} {target.get('role')}->{new_role}")
    return jsonify({"ok": True, "role": new_role})


@app.route("/api/admin/users/<int:user_id>", methods=["DELETE"])
@admin_required
def api_admin_delete_user(user_id):
    if user_id == int(g.user["id"]):
        return jsonify({"ok": False, "error": "لا يمكنك حذف حسابك الحالي"}), 400
    target = db.get_user_by_id(user_id)
    db.delete_user(user_id)
    _audit("user_deleted", f"target={(target or {}).get('email', user_id)}")
    return jsonify({"ok": True})


@app.route("/api/admin/users", methods=["POST"])
@admin_required
def api_admin_create_user():
    """Admin adds an account (the way to onboard users while registration is closed)."""
    import re as _re
    data = request.get_json(force=True, silent=True) or {}
    email = str(data.get("email") or "").strip().lower()
    password = str(data.get("password") or "")
    role = "admin" if data.get("role") == "admin" else "user"
    if len(email) > 254 or not _re.fullmatch(r"[^@\s<>\"'`]+@[^@\s<>\"'`]+\.[A-Za-z]{2,}", email):
        return jsonify({"ok": False, "error": "البريد الإلكتروني غير صالح"}), 400
    if len(password) < MIN_PASSWORD_LEN:
        return jsonify({"ok": False, "error": f"كلمة المرور يجب أن لا تقل عن {MIN_PASSWORD_LEN} أحرف"}), 400
    if db.get_user_by_email(email):
        return jsonify({"ok": False, "error": "البريد الإلكتروني مسجل مسبقاً"}), 400
    uid = db.create_user(email=email, password_hash=generate_password_hash(password), role=role, is_active=1)
    _audit("user_created_by_admin", f"target={email} role={role}")
    return jsonify({"ok": True, "id": uid})


@app.route("/api/admin/users/<int:user_id>/sign-out", methods=["POST"])
@admin_required
def api_admin_sign_out_user(user_id):
    """Force a user out everywhere: dashboard sessions and extension tokens."""
    target = db.get_user_by_id(user_id)
    if not target:
        return jsonify({"ok": False, "error": "المستخدم غير موجود"}), 404
    s, t = db.revoke_user_sessions(user_id), db.revoke_api_tokens(user_id)
    _audit("user_signed_out_by_admin", f"target={target['email']} sessions={s} tokens={t}")
    return jsonify({"ok": True, "sessions": s, "tokens": t})


@app.route("/api/admin/security-events", methods=["GET"])
@admin_required
def api_admin_security_events():
    return jsonify({"events": db.list_security_events(limit=min(int(request.args.get("limit", 200) or 200), 1000),
                                                      event=(request.args.get("event") or "").strip())})


# ── Engine control ───────────────────────────────────────────────────
@app.route("/api/start", methods=["POST"])
@admin_required
def api_start():
    cfg = request.get_json(force=True, silent=True) or {}
    url = (cfg.get("url") or "").strip()
    query = (cfg.get("query") or "").strip()
    country = (cfg.get("country") or DEFAULT_COUNTRY).strip().upper()
    if not url and not query:
        return jsonify({"error": "query or url required"}), 400

    from slicing import resolve_date_range
    date_preset = cfg.get("date_preset", "all")
    raw_min = (cfg.get("start_date_min") or "").strip()
    raw_max = (cfg.get("start_date_max") or "").strip()
    start_date_min, start_date_max, date_label = resolve_date_range(date_preset, raw_min, raw_max)

    # date_scope:
    #   "launch"      -> keep only ads whose LAUNCH date (delivery_start_time) is
    #                    inside the selected window (exact date(s)).
    #   "impressions" -> Meta's native "impressions by date" filter (what the Ad
    #                    Library URL start_date[min]/[max] does).
    # Default to "launch" whenever a date window is given so the user gets
    # exactly the dates they picked.
    date_scope = (cfg.get("date_scope") or "").strip().lower()
    if date_scope not in ("impressions", "launch"):
        date_scope = "launch" if (start_date_min or start_date_max) else "impressions"

    # If date window is specified, default to relevancy_monthly_grouped so Meta serves recent ads first
    chosen_sort = cfg.get("sort", "total_impressions")
    if (start_date_min or start_date_max) and chosen_sort == "total_impressions":
        chosen_sort = "relevancy_monthly_grouped"

    # Every scrape from the main button is a saved job: its progress is
    # checkpointed, so a dropped connection or a server restart resumes it
    # from the same page instead of starting over. The live view is unchanged
    # (the job runs through the same ScrapeManager and stream).
    busy = bool(manager.state.get("running")) or db.has_pending_jobs()
    job_id = db.create_job(
        query=query,
        url=url,
        country=country,
        exact_phrase=bool(cfg.get("exact", False)),
        stores_only=bool(cfg.get("stores_only", False)),
        sweep=bool(cfg.get("sweep", False)),
        sort_mode=chosen_sort,
        source="manual",
        start_date_min=start_date_min,
        start_date_max=start_date_max,
        date_scope=date_scope,
    )
    job_wakeup.set()
    if busy:
        manager._emit("log", {"text": f"🕒 أُضيف السحب «{query or url}» إلى قائمة الانتظار وسيبدأ بعد انتهاء السحب الحالي."})
    return jsonify({"ok": True, "queued": busy, "job_id": job_id,
                    "date_label": date_label, "date_scope": date_scope})


@app.route("/api/stop", methods=["POST"])
@admin_required
def api_stop():
    """Stop the running scrape and cancel manual scrapes waiting to resume."""
    manager.stop()
    jid = manager.state.get("job_id")
    if jid and manager.state.get("running"):
        db.request_stop(jid)
    cancelled = db.cancel_pending_jobs(source="manual")
    if cancelled:
        manager._emit("log", {"text": f"⏹ أُلغي {cancelled} سحب كان ينتظر الاستئناف."})
    return jsonify({"ok": True, "cancelled": cancelled})


@app.route("/api/status")
@login_required
def api_status():
    return jsonify({**manager.state, "scrape_enabled": SCRAPE_ENABLED, "db": db.get_stats()})


@app.route("/api/stream")
@admin_required
def api_stream():
    def gen():
        client_q: queue.Queue = queue.Queue()
        manager.subscribers.append(client_q)
        try:
            yield "retry: 3000\n\n"
            while True:
                try:
                    item = client_q.get(timeout=15)
                    yield f"data: {json.dumps(item, ensure_ascii=False)}\n\n"
                except queue.Empty:
                    yield "data: {\"type\": \"ping\"}\n\n"
        finally:
            try:
                manager.subscribers.remove(client_q)
            except ValueError:
                pass

    return Response(gen(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                             "Connection": "keep-alive"})


# ── Data Endpoints ────────────────────────────────────────────────────
@app.route("/api/ads")
@login_required
def api_ads():
    limit = max(1, min(_int_arg("limit", 60), 500))
    offset = max(0, _int_arg("offset", 0))
    search = request.args.get("search", "")
    query = request.args.get("query", "")
    store_domain = request.args.get("store_domain", "")
    page_id = request.args.get("page_id", "")
    stores_only = True
    preset = request.args.get("preset", "")
    active_only = request.args.get("active_only", "0") == "1"
    start_date_min = request.args.get("start_date_min", "")
    start_date_max = request.args.get("start_date_max", "")
    sort_by = request.args.get("sort_by", "collected_at")
    min_days_active = max(0, _int_arg("min_days_active", 0))
    min_product_ads = max(0, _int_arg("min_product_ads", 0))
    target_id = request.args.get("target_id")
    target_id_val = int(target_id) if target_id and target_id.isdigit() else None
    keywords = [k.strip() for k in (request.args.get("keywords") or "").split(",") if k.strip()]
    niches = [x.strip() for x in (request.args.get("niche") or "").split(",") if x.strip()]

    filters = dict(
        search=search, stores_only=stores_only, query=query,
        store_domain=store_domain, page_id=page_id, preset=preset,
        active_only=active_only, start_date_min=start_date_min,
        start_date_max=start_date_max, min_days_active=min_days_active,
        target_id=target_id_val, user_id=_scope_user_id(),
        min_product_ads=min_product_ads, keywords=keywords, niches=niches,
    )
    total_ads = db.count_ads(**filters)
    if request.args.get("group", "0") == "1":
        # One card per product (same advertiser + same product link); paging
        # and ``total`` count products, ``total_ads`` the ads behind them.
        items = db.attach_entity_stats(
            db.get_ad_groups(limit=limit, offset=offset, sort_by=sort_by, **filters))
        return jsonify({"ads": items, "total": db.count_ad_groups(**filters),
                        "total_ads": total_ads, "grouped": True})
    ads = db.attach_entity_stats(db.get_ads(limit=limit, offset=offset, sort_by=sort_by, **filters))
    return jsonify({"ads": ads, "total": total_ads, "total_ads": total_ads, "grouped": False})


@app.route("/api/ads/group")
@login_required
def api_ads_group():
    """Every ad of one product group, for the "show all N ads" view."""
    key = (request.args.get("key") or "").strip()
    if not key:
        return jsonify({"error": "key required"}), 400
    return jsonify({"ads": db.attach_entity_stats(db.get_group_ads(key, user_id=_scope_user_id()))})


@app.route("/api/video")
@login_required
def api_video():
    """Stream a Facebook video through our own origin (signed fbcdn URLs expire
    / are blocked cross-origin by some browsers). Only Meta CDN hosts are
    allowed, so this cannot be used as an open proxy."""
    from urllib.parse import urlsplit
    raw = (request.args.get("u") or "").strip()
    if not raw:
        return jsonify({"error": "u required"}), 400
    parts = urlsplit(raw)
    host = (parts.hostname or "").lower()
    allowed = host.endswith(".fbcdn.net") or host == "fbcdn.net" or host.endswith(".facebook.com")
    if parts.scheme not in ("http", "https") or not allowed:
        return jsonify({"error": "host not allowed"}), 400

    from curl_cffi import requests as creq
    up_headers = {}
    rng = request.headers.get("Range")
    if rng:
        up_headers["Range"] = rng
    try:
        upstream = creq.get(raw, impersonate="chrome", timeout=90, stream=True,
                            headers=up_headers, allow_redirects=True)
    except Exception as exc:  # noqa: BLE001
        logger.warning("video proxy failed: %s", exc)
        return jsonify({"error": "upstream unavailable"}), 502
    if upstream.status_code not in (200, 206):
        upstream.close()
        return jsonify({"error": f"upstream {upstream.status_code}"}), 502

    def gen():
        try:
            for chunk in upstream.iter_content(65536):
                if chunk:
                    yield chunk
        finally:
            upstream.close()

    resp = Response(gen(), status=upstream.status_code, direct_passthrough=True)
    resp.headers["Content-Type"] = upstream.headers.get("Content-Type", "video/mp4")
    for h in ("Content-Length", "Accept-Ranges", "Content-Range"):
        if upstream.headers.get(h):
            resp.headers[h] = upstream.headers[h]
    resp.headers["Cache-Control"] = "private, max-age=1800"
    return resp


@app.route("/api/diagnose")
@admin_required
def api_diagnose():
    """One-shot diagnosis: egress IP + a live Meta Ad Library probe.

    Explains why a scrape returns nothing (blocked IP/proxy vs empty results)."""
    from curl_cffi import requests as creq
    from meta_ads_collector.proxy_pool import parse_proxy, _redact_proxy

    saved = _proxy_setting()
    env_spec = (os.environ.get("META_ADS_PROXY") or "").strip()
    spec = saved or env_spec
    use_proxy = request.args.get("proxy", "1") != "0"
    if not use_proxy:
        spec = ""
    out = {"proxy_configured": bool(spec),
           "source": ("settings" if saved else ("env" if env_spec else "none")) if use_proxy else "disabled",
           "using_proxy": bool(spec),
           "proxy": "", "egress_ip": None, "direct_ip": None,
           "proxy_changes_ip": None, "meta_count": None,
           "meta_error": "", "verdict": ""}

    proxies = None
    proxy_arg = None
    if spec:
        parts = [p.strip() for p in spec.split(",") if p.strip()]
        try:
            purl = parse_proxy(parts[0])
            out["proxy"] = _redact_proxy(purl)
        except Exception as exc:  # noqa: BLE001
            out["verdict"] = f"صيغة البروكسي غير صحيحة: {exc}"
            return jsonify(out)
        proxies = {"http": purl, "https": purl}
        try:
            r = creq.get("https://api.ipify.org?format=json", proxies=proxies,
                         impersonate="chrome", timeout=25)
            out["egress_ip"] = (r.json() or {}).get("ip")
        except Exception as exc:  # noqa: BLE001
            out["verdict"] = f"تعذّر الاتصال بالبروكسي: {str(exc)[:180]}"
            return jsonify(out)
        proxy_arg = parts if len(parts) > 1 else parts[0]

    # Direct server IP (never through the proxy), to compare.
    try:
        r0 = creq.get("https://api.ipify.org?format=json", impersonate="chrome", timeout=15)
        out["direct_ip"] = (r0.json() or {}).get("ip")
    except Exception:  # noqa: BLE001
        out["direct_ip"] = None
    out["proxy_changes_ip"] = bool(
        proxies and out["direct_ip"] and out["egress_ip"]
        and out["direct_ip"] != out["egress_ip"])

    # Live Meta Ad Library probe (same path the scraper uses).
    try:
        from meta_ads_collector import MetaAdsCollector
        c = MetaAdsCollector(proxy=proxy_arg)
        try:
            out["meta_count"] = c.count(
                query="livraison", country="DZ", status="ACTIVE",
                sort_by="SORT_BY_RELEVANCY_MONTHLY_GROUPED")
        finally:
            try:
                c.close()
            except Exception:
                pass
    except Exception as exc:  # noqa: BLE001
        out["meta_error"] = str(exc)[:200]

    if out.get("meta_count") and int(out["meta_count"]) > 0:
        where = "عبر البروكسي" if proxies else "من IP السيرفر المباشر"
        out["verdict"] = f"✅ يعمل {where}: Meta أعادت نتائج فعلية."
    elif proxies and out.get("direct_ip") and out.get("egress_ip") == out.get("direct_ip"):
        out["verdict"] = ("⛔ البروكسي لا يغيّر عنوان الخروج (egress = IP السيرفر المباشر). "
                          "هذا بروكسي محلي/شفاف ولا يفيد ضد حجب Meta.")
    elif not proxies:
        out["verdict"] = ("⛔ Meta أعادت 0 نتيجة من IP السيرفر المباشر — الـIP محجوب مؤقتًا من "
                          "مكتبة الإعلانات (سيُرفع الحجب تلقائيًا بعد فترة).")
    else:
        out["verdict"] = ("⛔ Meta أعادت 0 نتيجة عبر البروكسي — عنوان خروج البروكسي محجوب مؤقتًا. "
                          "جرّب بروكسي residential/mobile آخر ثم أعد التشخيص.")
    return jsonify(out)


@app.route("/api/queries")
@admin_required
def api_queries():
    return jsonify({"queries": db.get_queries()})


@app.route("/api/ad-keywords")
@login_required
def api_ad_keywords():
    """Per-keyword counters for the keyword filter bar."""
    return jsonify({"keywords": db.get_keywords_overview(user_id=_scope_user_id())})


@app.route("/api/niches")
@login_required
def api_niches():
    """Niche taxonomy with per-niche ad counts (for the filter bar)."""
    return jsonify({"niches": db.niche_counts(user_id=_scope_user_id())})


@app.route("/api/ads/<ad_id>/niche", methods=["POST"])
@admin_required
def api_set_ad_niche(ad_id):
    """Manual niche override for one ad (method='manual' beats the rules)."""
    cfg = request.get_json(force=True, silent=True) or {}
    slug = (cfg.get("slug") or "").strip()
    if not slug:
        return jsonify({"error": "slug required"}), 400
    ok = db.set_manual_niche(ad_id, slug, add=bool(cfg.get("add", True)))
    return jsonify({"ok": ok})


@app.route("/api/stores")
@login_required
def api_stores():
    return jsonify({"stores": db.get_stores(search=request.args.get("search", ""), user_id=_scope_user_id())})


@app.route("/api/stats")
@login_required
def api_stats():
    return jsonify(db.get_stats(user_id=_scope_user_id()))


@app.route("/api/reset", methods=["POST"])
@admin_required
def api_reset():
    db.clear_ads()
    return jsonify({"ok": True})


def _token_matches(provided: str, expected: str) -> bool:
    """Constant-time compare; an unset secret never matches anything."""
    import hmac
    provided, expected = (provided or "").strip(), (expected or "").strip()
    return bool(provided) and bool(expected) and hmac.compare_digest(provided, expected)


def _ingest_token() -> str:
    return (os.environ.get("INGEST_TOKEN") or "").strip()


@app.route("/api/ingest", methods=["POST"])
def api_ingest():
    """Receive ads pushed from a remote scraper (e.g. worker)."""
    expected = _ingest_token()
    provided = (request.headers.get("X-Ingest-Token", "")
                or request.headers.get("Authorization", "").replace("Bearer ", "")).strip()
    if not expected:
        return jsonify({"error": "ingest disabled (INGEST_TOKEN not set)"}), 503
    if not _token_matches(provided, expected):
        return jsonify({"error": "unauthorized"}), 401

    payload = request.get_json(force=True, silent=True) or {}
    ads = payload.get("ads") or []
    query = payload.get("query", "")
    country = payload.get("country", DEFAULT_COUNTRY)
    stored = 0
    for ad in ads:
        if isinstance(ad, dict) and db.save_ad(ad, query=query, country=country):
            stored += 1
    logger.info("ingest: received=%s stored=%s query=%r", len(ads), stored, query)
    return jsonify({"ok": True, "received": len(ads), "stored": stored})


# ── Remote Job Queue ──────────────────────────────────────────────────
def _worker_token() -> str:
    return (os.environ.get("WORKER_TOKEN") or os.environ.get("INGEST_TOKEN") or "").strip()


def _worker_ok() -> bool:
    """Remote-worker API: closed unless WORKER_TOKEN / INGEST_TOKEN is set."""
    provided = (request.headers.get("X-Worker-Token", "")
                or request.headers.get("X-Ingest-Token", "")
                or request.headers.get("Authorization", "").replace("Bearer ", "")).strip()
    return _token_matches(provided, _worker_token())


@app.route("/api/jobs", methods=["GET"])
@admin_required
def api_jobs():
    return jsonify({"jobs": db.list_jobs(limit=60)})


@app.route("/api/jobs", methods=["POST"])
@admin_required
def api_create_job():
    cfg = request.get_json(force=True, silent=True) or {}
    url = (cfg.get("url") or "").strip()
    query = (cfg.get("query") or "").strip()
    if not url and not query:
        return jsonify({"error": "query or url required"}), 400
    job_id = db.create_job(
        query=query,
        url=url,
        country=cfg.get("country", DEFAULT_COUNTRY),
        exact_phrase=bool(cfg.get("exact", False)),
        stores_only=bool(cfg.get("stores_only", False)),
        sweep=bool(cfg.get("sweep", False)),
        sort_mode=cfg.get("sort", "total_impressions"),
        source="manual",
        date_scope=(cfg.get("date_scope") or "impressions"),
    )
    return jsonify({"ok": True, "job_id": job_id})


@app.route("/api/worker/next")
def api_worker_next():
    if not _worker_ok():
        return jsonify({"error": "unauthorized"}), 401
    worker_id = request.args.get("worker_id", "worker")
    job = db.claim_next_job(worker_id)
    if not job:
        return ("", 204)
    return jsonify({"job": job})


@app.route("/api/worker/result", methods=["POST"])
def api_worker_result():
    if not _worker_ok():
        return jsonify({"error": "unauthorized"}), 401
    payload = request.get_json(force=True, silent=True) or {}
    job_id = payload.get("job_id")
    status = payload.get("status", "done")
    error = payload.get("error", "")
    ads = payload.get("ads") or []
    query = payload.get("query", "")
    country = payload.get("country", DEFAULT_COUNTRY)

    new_ads = []
    for ad in ads:
        if not isinstance(ad, dict):
            continue
        if db.save_ad(ad, query=query, country=country):
            new_ads.append(ad)
        manager.events.put({"type": "ad", "data": ad})

    if job_id is not None:
        db.finish_job(int(job_id), status=status, result_count=len(ads), error=error)

    if new_ads:
        try:
            from notify import notify_new_ads
            job = db.get_job(int(job_id)) if job_id is not None else None
            target = query or (job.get("url") if job else "") or ""
            res = notify_new_ads(db.get_settings(), target, new_ads, db.count_ads())
            if res.get("sent"):
                logger.info("notification sent for %s new ads", len(new_ads))
        except Exception:
            logger.exception("notification error")

    manager.events.put({"type": "job", "data": {
        "job_id": job_id, "status": status, "stored": len(ads), "new": len(new_ads)}})
    logger.info("worker result: job=%s status=%s ads=%s new=%s", job_id, status, len(ads), len(new_ads))
    return jsonify({"ok": True, "stored": len(ads), "new": len(new_ads)})


@app.route("/api/worker/progress", methods=["POST"])
def api_worker_progress():
    if not _worker_ok():
        return jsonify({"error": "unauthorized"}), 401
    payload = request.get_json(force=True, silent=True) or {}
    job_id = payload.get("job_id")
    progress = int(payload.get("progress", 0))
    reported = payload.get("reported")
    note = payload.get("note", "")
    if job_id is not None:
        db.update_job_progress(int(job_id), progress, note)
    stop = False
    if job_id is not None:
        job = db.get_job(int(job_id))
        stop = bool(job and job.get("stop_requested"))
    manager.events.put({"type": "progress",
                        "data": {"job_id": job_id, "progress": progress, "reported": reported, "note": note, "stop": stop}})
    return jsonify({"ok": True, "stop": stop})


@app.route("/api/jobs/<int:job_id>/stop", methods=["POST"])
@admin_required
def api_job_stop(job_id):
    db.request_stop(job_id)
    manager.events.put({"type": "job", "data": {"job_id": job_id, "status": "stopping"}})
    return jsonify({"ok": True})


# ── Competitors (per user: Pages, Stores, Keywords, Custom URLs) ──────
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


@app.route("/api/targets", methods=["GET"])
@login_required
def api_targets():
    s = db.get_settings()
    return jsonify({"targets": db.list_competitors(user_id=_scope_user_id()),
                    "schedule_enabled": s.get("schedule_enabled", "false") == "true"})


@app.route("/api/targets", methods=["POST"])
@login_required
def api_add_target():
    cfg = request.get_json(force=True, silent=True) or {}
    raw = (cfg.get("target_value") or cfg.get("query") or "").strip()
    typ = (cfg.get("type") or "keyword").strip().lower()
    country = (cfg.get("country") or "DZ").strip().upper()
    label = (cfg.get("label") or "").strip()
    interval_hours = max(1.0, float(cfg.get("interval_hours") or 12.0))

    if not raw:
        return jsonify({"ok": False, "error": "الهدف لا يمكن أن يكون فارغاً"}), 400
    if typ not in ("page", "domain", "keyword", "custom_url"):
        return jsonify({"ok": False, "error": "نوع غير صالح"}), 400
    if len(raw) > 500 or len(label) > 120:
        return jsonify({"ok": False, "error": "القيمة طويلة جداً"}), 400

    if typ == "page":
        pid = extract_page_id(raw) or raw
        target_val = pid
        if not label:
            label = f"صفحة {pid}"
        db.upsert_tracked_page(page_id=pid, page_name=label, country=country, source="dashboard")
    elif typ == "domain":
        dom = clean_domain(raw)
        target_val = dom
        if not label:
            label = f"متجر {dom}"
    else:
        target_val = raw
        if not label:
            label = raw

    tid, created = db.add_user_target(
        int(g.user["id"]), typ, target_val, label=label,
        country=country, interval_hours=interval_hours, source="dashboard")
    if not created:
        return jsonify({"ok": False, "error": "هذا المنافس موجود في قائمتك بالفعل"}), 400
    # First scan right away so the competitor's ads show up without waiting.
    _queue_target_scan(db.get_target(tid), "new_competitor")
    return jsonify({"ok": True, "target_id": tid})


@app.route("/api/targets/<int:target_id>", methods=["DELETE"])
@login_required
def api_delete_target(target_id):
    t, uid = _competitor_access(target_id)
    if not t:
        return jsonify({"ok": False, "error": "غير موجود"}), 404
    if uid is None:
        db.delete_target_everywhere(target_id)   # admin overview: remove for everyone
    else:
        db.remove_user_target(uid, target_id)
    return jsonify({"ok": True})


@app.route("/api/targets/<int:target_id>", methods=["PATCH"])
@login_required
def api_toggle_target(target_id):
    t, uid = _competitor_access(target_id)
    if not t:
        return jsonify({"ok": False, "error": "غير موجود"}), 404
    cfg = request.get_json(force=True, silent=True) or {}
    db.set_user_target_enabled(uid, target_id, bool(cfg.get("enabled", True)))
    return jsonify({"ok": True})


@app.route("/api/targets/<int:target_id>/scan", methods=["POST"])
@login_required
def api_scan_target(target_id):
    t, _uid = _competitor_access(target_id)
    if not t:
        return jsonify({"error": "target not found"}), 404
    job_id = _queue_target_scan(t, "manual_target")
    if job_id is None:
        return jsonify({"ok": True, "already_queued": True})
    manager._emit("log", {"text": f"⚡ أُضيف فحص «{t.get('label') or t['target_value']}» إلى قائمة الانتظار."})
    return jsonify({"ok": True, "job_id": job_id})


@app.route("/api/targets/scan-all", methods=["POST"])
@login_required
def api_scan_all_targets():
    """Scan every enabled competitor of the current user (all of them for an admin)."""
    count = 0
    for t in db.list_competitors(user_id=_scope_user_id()):
        if t.get("enabled") and _queue_target_scan(t, "manual_all") is not None:
            count += 1
    return jsonify({"ok": True, "enqueued": count})


# Backward compatibility for keywords
@app.route("/api/keywords", methods=["GET"])
@admin_required
def api_keywords():
    return jsonify({"keywords": db.list_keywords()})


@app.route("/api/keywords", methods=["POST"])
@admin_required
def api_add_keyword():
    return api_add_target()


@app.route("/api/keywords/<int:kw_id>", methods=["DELETE"])
@admin_required
def api_delete_keyword(kw_id):
    return api_delete_target(kw_id)


@app.route("/api/keywords/<int:kw_id>", methods=["PATCH"])
@admin_required
def api_toggle_keyword(kw_id):
    return api_toggle_target(kw_id)


# ── Settings / Schedule / Telegram ────────────────────────────────────
@app.route("/api/settings", methods=["GET"])
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
    })


@app.route("/api/settings", methods=["POST"])
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
    if "meta_ads_proxy" in cfg:
        db.set_setting("meta_ads_proxy", str(cfg["meta_ads_proxy"] or "").strip())
    _audit("settings_changed", ",".join(sorted(k for k in cfg if k not in ("telegram_token", "meta_ads_proxy"))) +
           (",telegram_token" if "telegram_token" in cfg else "") +
           (",meta_ads_proxy" if "meta_ads_proxy" in cfg else ""))
    return jsonify({"ok": True})


@app.route("/api/proxy/test", methods=["POST"])
@admin_required
def api_proxy_test():
    """Try the given (or saved) proxy against a tiny IP endpoint."""
    cfg = request.get_json(force=True, silent=True) or {}
    spec = (str(cfg.get("meta_ads_proxy") or "")).strip() or _proxy_setting() \
        or (os.environ.get("META_ADS_PROXY") or "").strip()
    if not spec:
        return jsonify({"ok": False, "error": "لا يوجد بروكسي مضبوط"}), 400

    from meta_ads_collector.proxy_pool import parse_proxy, _redact_proxy
    from curl_cffi import requests as creq

    results = []
    for raw in [p.strip() for p in spec.split(",") if p.strip()][:5]:
        try:
            purl = parse_proxy(raw)
        except Exception as exc:  # noqa: BLE001
            results.append({"proxy": "proxy", "ok": False, "error": f"صيغة غير صحيحة: {exc}"})
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


@app.route("/api/telegram/test", methods=["POST"])
@admin_required
def api_telegram_test():
    cfg = request.get_json(force=True, silent=True) or {}
    s = db.get_settings()
    token = (cfg.get("telegram_token") or s.get("telegram_token") or "").strip()
    chat_id = (cfg.get("telegram_chat_id") or s.get("telegram_chat_id") or "").strip()
    from notify import send_telegram_test
    ok, msg = send_telegram_test(token, chat_id)
    return jsonify({"ok": ok, "message": msg})


@app.route("/api/schedule/run-now", methods=["POST"])
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
    manager.events.put({"type": "log", "data": {"text": f"⚡ تم إطلاق فحص شامل لـ {count} أهداف مجدولة الآن."}})
    return jsonify({"ok": True, "enqueued": count})


# ── Chrome Extension (adlibrary) Integration API ───────────────────────
@app.route("/api/v1/<path:path>", methods=["OPTIONS"])
def api_v1_options(path):
    return ("", 200)


@app.route("/api/v1/health", methods=["GET", "OPTIONS"])
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


@app.route("/api/v1/auth/login", methods=["POST"])
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


@app.route("/api/v1/auth/me", methods=["GET"])
def api_ext_me():
    user = _ext_user()
    if not user:
        return jsonify({"ok": False, "error": {"message": "unauthorized"}}), 401
    return jsonify({"ok": True, "data": {"email": user["email"], "role": user["role"]}})


@app.route("/api/v1/ads", methods=["POST"])
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


@app.route("/api/v1/pages/track", methods=["POST"])
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


@app.route("/api/v1/pages/<page_id>/untrack", methods=["POST"])
def api_ext_untrack_page(page_id):
    if not _ext_token_ok():
        return jsonify({"ok": False, "error": {"message": "unauthorized"}}), 401
    uid = _ext_user_id()
    if uid:
        _untrack_for_user(uid, page_id)
    else:
        _drop_tracked_page(page_id)
    return jsonify({"ok": True, "data": {"untracked": True, "page_id": page_id}})


@app.route("/api/v1/pages", methods=["GET", "OPTIONS"])
def api_ext_list_pages():
    if not _ext_token_ok():
        return jsonify({"ok": False, "error": {"message": "unauthorized"}}), 401
    pages = db.list_tracked_pages(user_id=_ext_user_id())
    for pg in pages:
        pg.pop("snapshots", None)
    return jsonify({"ok": True, "data": {"pages": pages}})


@app.route("/api/v1/pages/<page_id>/snapshots", methods=["GET", "OPTIONS"])
def api_ext_page_snapshots(page_id):
    if not _ext_token_ok():
        return jsonify({"ok": False, "error": {"message": "unauthorized"}}), 401
    return jsonify({"ok": True, "data": {"page_id": page_id, "snapshots": db.get_page_snapshots(page_id, limit=200)}})


@app.route("/api/v1/observations/page-snapshot", methods=["POST"])
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


@app.route("/api/v1/sync/ads", methods=["POST"])
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


@app.route("/api/v1/sync/pages", methods=["POST"])
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
@app.route("/api/tracked-pages", methods=["GET"])
@login_required
def api_tracked_pages():
    return jsonify({"pages": db.list_tracked_pages(user_id=_scope_user_id())})


@app.route("/api/tracked-pages/<page_id>/snapshots", methods=["GET"])
@login_required
def api_tracked_page_snapshots(page_id):
    uid = _scope_user_id()
    if uid is not None and g.user.get("role") != "admin" and str(page_id) not in db.list_user_page_ids(uid):
        return jsonify({"ok": False, "error": "غير مسموح"}), 403
    return jsonify({"page_id": page_id, "snapshots": db.get_page_snapshots(page_id, limit=200)})


@app.route("/api/tracked-pages/<page_id>", methods=["DELETE"])
@login_required
def api_untrack_page(page_id):
    uid = _scope_user_id()
    if uid is None:
        _drop_tracked_page(page_id)
    else:
        _untrack_for_user(uid, page_id)
    return jsonify({"ok": True})


@app.route("/api/tracked-pages", methods=["DELETE"])
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


@app.route("/api/export/<fmt>")
@login_required
def api_export(fmt: str):
    ads = db.get_ads(limit=100000, stores_only=False, user_id=_scope_user_id())
    if fmt == "csv":
        buf = io.StringIO()
        cols = ["ad_id", "page_name", "page_id", "start_date", "store_domain",
                "platform", "cta_text", "link_url", "keywords", "niches",
                "video_url", "body"]
        w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for ad in ads:
            row = dict(ad)
            row["keywords"] = " | ".join(row.get("keywords") or [])
            row["niches"] = " | ".join(x.get("slug", "") for x in (row.get("niches") or []))
            w.writerow(row)
        data = io.BytesIO(buf.getvalue().encode("utf-8-sig"))
        return send_file(data, mimetype="text/csv", as_attachment=True,
                         download_name=f"meta_ads_{int(datetime.now().timestamp())}.csv")
    if fmt == "json":
        data = io.BytesIO(json.dumps(ads, ensure_ascii=False, indent=2).encode("utf-8"))
        return send_file(data, mimetype="application/json", as_attachment=True,
                         download_name=f"meta_ads_{int(datetime.now().timestamp())}.json")
    return jsonify({"error": "unsupported format"}), 400


@app.route("/api/maintenance/reclassify-stores", methods=["POST"])
@admin_required
def api_reclassify_stores():
    rows = db._query("SELECT ad_id, link_url, store_domain FROM meta_ads")
    changed = cleared = 0
    for r in rows:
        dom, plat = extract_store_domain(r.get("link_url") or "")
        new = dom or ""
        old = r.get("store_domain") or ""
        if new != old:
            db._execute(
                f"UPDATE meta_ads SET store_domain={db._ph()}, platform={db._ph()} WHERE ad_id={db._ph()}",
                (new, (plat or ""), r["ad_id"]))
            changed += 1
            if old and not new:
                cleared += 1
    logger.info("reclassify-stores: checked=%s changed=%s cleared=%s", len(rows), changed, cleared)
    return jsonify({"ok": True, "checked": len(rows), "changed": changed, "cleared_non_store": cleared})


def scheduler_loop() -> None:
    from datetime import timedelta
    from db import algeria_today
    days_refreshed_for = None
    while True:
        try:
            # Ad age ("Active: X days") keeps counting: recompute it at
            # startup and again each time the date changes in Algeria.
            today_dz = algeria_today()
            if days_refreshed_for != today_dz:
                changed = db.refresh_days_active()
                db.purge_old_sessions()
                db.purge_auth_attempts()
                days_refreshed_for = today_dz
                logger.info("days_active refreshed for %s: %s ads updated", today_dz, changed)

            n = db.requeue_stale_jobs(minutes=20)
            if n:
                manager.events.put({"type": "log", "data": {"text": f"♻️ أُعيد {n} أمر عالق إلى قائمة الانتظار."}})

            s = db.get_settings()
            if s.get("schedule_enabled", "false").lower() in ("1", "true", "yes", "on"):
                targets = [t for t in db.list_targets() if t.get("enabled")]
                now_utc = datetime.now(timezone.utc)
                enqueued = 0

                for t in targets:
                    interval_h = float(t.get("interval_hours") or 12.0)
                    last_str = t.get("last_scraped_at") or ""
                    due = True
                    if last_str:
                        try:
                            due = (now_utc - datetime.fromisoformat(last_str)) >= timedelta(hours=interval_h)
                        except Exception:
                            due = True

                    if due and db.has_open_job(t.get("id")):
                        # Still queued / running / waiting to resume: don't
                        # start the same work twice.
                        due = False

                    if due:
                        t_url = resolve_target_url(
                            t.get("type", "keyword"),
                            t.get("target_value", ""),
                            country=t.get("country", "DZ")
                        )
                        db.create_job(
                            query=t.get("target_value", ""),
                            url=t_url,
                            country=t.get("country", "DZ"),
                            stores_only=True,
                            source="schedule",
                            target_id=t.get("id"),
                            incremental=True,
                        )
                        db.update_target_scraped(t["id"])
                        enqueued += 1

                if enqueued > 0:
                    db.set_setting("last_scheduled_at", now_utc.isoformat())
                    manager.events.put({
                        "type": "log",
                        "data": {"text": f"⏰ جدولة ذكية: أُضيفت {enqueued} مهمة سحب تزايدي للأهداف المستحقة."}
                    })
        except Exception:
            logger.exception("scheduler error")
        time.sleep(60)


def _snapshot_page_target(job: dict, outcome: dict) -> None:
    """After a complete scrape of a page competitor, record how many ads the
    page runs right now, so its activity history grows without the extension."""
    try:
        tid = job.get("target_id")
        t = db.get_target(int(tid)) if tid else None
        if not t or t.get("type") != "page" or job.get("sweep"):
            return
        pid = str(t["target_value"])
        db.upsert_tracked_page(page_id=pid, page_name=t.get("label") or "", country=t.get("country") or "DZ",
                               source="server")
        scanned = int(outcome.get("scanned") or 0)
        db.add_page_snapshot(page_id=pid, active_ads_count=scanned, total_ads_count=scanned,
                             ad_ids=outcome.get("ad_ids") or [], source="server_scrape")
    except Exception:
        logger.exception("page snapshot failed")


# Job-level resume: an interrupted job goes back to the queue and continues
# from its checkpoint after these delays (minutes); then it is marked failed.
JOB_RETRY_DELAYS_MIN = (5, 20, 60)
CHECKPOINT_EVERY_SEC = 10
INPROC_WORKER_PREFIX = "inproc-"


def job_worker_loop() -> None:
    worker_id = f"{INPROC_WORKER_PREFIX}{os.getpid()}"
    while True:
        try:
            job = db.claim_next_job(worker_id)
            if not job:
                job_wakeup.wait(3)
                job_wakeup.clear()
                continue
            jid = job.get("id")
            try:
                resume = json.loads(job.get("checkpoint") or "null")
            except ValueError:
                resume = None
            attempts = int(job.get("attempts") or 0)
            # Ads already collected by earlier (interrupted) runs of this job.
            base_count = int(job.get("result_count") or 0) if resume else 0
            d_min, d_max = job.get("start_date_min") or "", job.get("start_date_max") or ""
            logger.info("job %s: in-process scrape start (attempt %s, resume=%s)", jid, attempts + 1, resume)
            db.update_job_progress(jid, 1, "استئناف من آخر نقطة" if resume else "بدء السحب عبر HTTP")

            last_write = [0.0]

            def persist(cp, _jid=jid):
                # Throttled: a few re-fetched pages after a crash are cheap,
                # a DB write per page is not. The final checkpoint is always
                # written below from the outcome.
                now = time.monotonic()
                if now - last_write[0] >= CHECKPOINT_EVERY_SEC:
                    last_write[0] = now
                    db.save_checkpoint(_jid, json.dumps(cp) if cp else None,
                                       result_count=manager.state.get("scraped", 0))  # cumulative

            started = manager.start(
                query=job.get("query") or "",
                country=job.get("country") or DEFAULT_COUNTRY,
                exact=bool(job.get("exact_phrase")),
                stores_only=bool(job.get("stores_only")),
                sort_mode=job.get("sort_mode") or "total_impressions",
                sweep=bool(job.get("sweep")),
                url=job.get("url") or "",
                start_date_min=job.get("start_date_min") or "",
                start_date_max=job.get("start_date_max") or "",
                date_label=(f"{d_min or 'البداية'} ← {d_max or 'اليوم'}" if (d_min or d_max) else ""),
                date_scope=job.get("date_scope") or "impressions",
                resume=resume,
                on_checkpoint=persist,
                job_id=jid,
                base_count=base_count,
            )
            if not started:
                db.release_job(jid)
                time.sleep(5)
                continue

            last_stop_check = 0.0
            while manager.state.get("running"):
                # Re-read the job: the stop flag is set by another request.
                if time.monotonic() - last_stop_check >= 3:
                    last_stop_check = time.monotonic()
                    fresh = db.get_job(jid)
                    if fresh and fresh.get("stop_requested"):
                        manager.stop()
                time.sleep(1)

            outcome = manager.state.get("outcome") or {"status": "done"}
            count = manager.state.get("scraped", 0)
            cp = outcome.get("checkpoint")
            cp_json = json.dumps(cp) if cp else None
            status = outcome.get("status")

            if status == "interrupted":
                if attempts < len(JOB_RETRY_DELAYS_MIN):
                    delay = JOB_RETRY_DELAYS_MIN[attempts]
                    db.schedule_retry(jid, delay, cp_json, result_count=count,
                                      error=outcome.get("error", ""))
                    manager._emit("paused", {
                        "job_id": jid, "scraped": count, "delay_min": delay,
                        "attempt": attempts + 1, "max_attempts": len(JOB_RETRY_DELAYS_MIN),
                        "error": outcome.get("error", "")})
                    manager._emit("log", {
                        "text": f"⏸ انقطع السحب بعد {count} إعلان وسيُستأنف تلقائياً من نفس النقطة بعد {delay} دقيقة "
                                f"(محاولة {attempts + 1}/{len(JOB_RETRY_DELAYS_MIN)})."})
                    logger.info("job %s: interrupted, retry in %s min from %s", jid, delay, cp)
                else:
                    db.finish_job(jid, status="failed", result_count=count,
                                  error=outcome.get("error", ""), checkpoint=cp_json)
                    manager._emit("log", {"text": f"⛔ توقف السحب نهائياً بعد {attempts + 1} محاولات: {outcome.get('error', '')}"})
                    logger.warning("job %s: failed after %s attempts", jid, attempts + 1)
            elif status == "failed":
                db.finish_job(jid, status="failed", result_count=count,
                              error=outcome.get("error", ""), checkpoint=cp_json)
            elif status == "stopped":
                db.finish_job(jid, status="stopped", result_count=count)
            else:
                db.finish_job(jid, status="done", result_count=count)
                logger.info("job %s: done (%s ads)", jid, count)
                if not resume:  # a resumed run only saw part of the page's ads
                    _snapshot_page_target(job, outcome)
        except Exception:
            logger.exception("job worker loop error")
            time.sleep(5)


def _entity_stats_enabled() -> bool:
    # ENTITY_STATS=off disables the counter outright (env wins over the setting).
    if os.environ.get("ENTITY_STATS", "").strip().lower() in ("0", "off", "false", "no"):
        return False
    return db.get_settings().get("entity_stats_enabled", "true").lower() in ("1", "true", "yes", "on")


def entity_stats_loop() -> None:
    """Low-priority background counter: how many ads each page / store domain
    in the bank runs in total on Meta (one request each, refreshed daily).
    Yields to scraping: it only works while no scrape runs or waits."""
    import random
    from engine import entity_ad_count, new_count_client

    def busy() -> bool:
        return bool(manager.state.get("running")) or db.has_pending_jobs()

    while True:
        try:
            if not _entity_stats_enabled():
                time.sleep(300)
                continue
            if busy():
                time.sleep(20)
                continue
            batch = db.stale_entities(limit=20)
            if not batch:
                time.sleep(600)
                continue
            client = new_count_client(proxy=_proxy_setting() or None)
            errors = 0
            try:
                for kind, key in batch:
                    if busy():
                        break
                    try:
                        n = entity_ad_count(client, kind, key, country=DEFAULT_COUNTRY)
                    except Exception as exc:  # noqa: BLE001
                        logger.debug("entity count %s %s failed: %s", kind, key, exc)
                        n = None
                    db.save_entity_stat(kind, key, n, "ok" if n is not None else "error")
                    errors = errors + 1 if n is None else 0
                    if errors >= 3:
                        logger.warning("entity counts: repeated failures, pausing 10 min")
                        time.sleep(600)
                        break
                    time.sleep(1 + random.random())
            finally:
                try:
                    client.close()
                except Exception:
                    pass
        except Exception:
            logger.exception("entity stats loop error")
            time.sleep(60)


_bg_started = False


def niches_loop() -> None:
    """Classify ads that have no niche yet (backfill + every new scrape)."""
    while True:
        try:
            n = db.classify_pending(limit=500)
            time.sleep(1 if n else 120)
        except Exception:
            logger.exception("niches loop error")
            time.sleep(30)


def start_background_threads() -> None:
    global _bg_started
    if _bg_started:
        return
    _bg_started = True
    try:
        n = db.migrate_plain_api_tokens()
        if n:
            logger.info("extension tokens moved to hashed storage: %s", n)
    except Exception:
        logger.exception("token migration failed")
    try:
        moved = db.migrate_user_targets()
        if moved:
            logger.info("competitors migrated to per-user lists: %s links", moved)
    except Exception:
        logger.exception("competitor migration failed")
    try:
        n = db.requeue_orphaned_jobs(INPROC_WORKER_PREFIX)
        if n:
            logger.info("requeued %s job(s) left running by a previous process", n)
    except Exception:
        logger.exception("orphaned job requeue failed")
    threading.Thread(target=scheduler_loop, daemon=True).start()
    threading.Thread(target=entity_stats_loop, daemon=True).start()
    threading.Thread(target=job_worker_loop, daemon=True).start()
    threading.Thread(target=niches_loop, daemon=True).start()


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