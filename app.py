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

from flask import Flask, Response, jsonify, render_template, request, send_file, session
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
app.secret_key = (
    os.environ.get("SECRET_KEY")
    or os.environ.get("FLASK_SECRET_KEY")
    or secrets.token_hex(32)
)


def login_required(f):
    @functools.wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get("user_id"):
            return jsonify({"ok": False, "error": "يرجى تسجيل الدخول أولاً", "unauthorized": True}), 401
        return f(*args, **kwargs)
    return decorated_function


def admin_required(f):
    @functools.wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get("user_id"):
            return jsonify({"ok": False, "error": "يرجى تسجيل الدخول أولاً", "unauthorized": True}), 401
        if session.get("user_role") != "admin":
            return jsonify({"ok": False, "error": "هذا الإجراء يتطلب صلاحيات مدير النظام (Admin)", "forbidden": True}), 403
        return f(*args, **kwargs)
    return decorated_function


def _scope_user_id():
    """Whose data the current dashboard request may see.

    A regular user only ever sees their own saves and tracked pages. An admin
    sees everything, or one user's data when ``?user_id=`` is given.
    """
    if session.get("user_role") == "admin":
        raw = (request.args.get("user_id") or "").strip()
        return int(raw) if raw.isdigit() else None
    return int(session["user_id"])


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
                      "stores": 0, "scanned": 0, "reported": None, "log": []}

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
                           "stored": 0, "stores": 0, "reported": None, "log": [],
                           "outcome": None, "job_id": job_id})
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
        seen_stores: set = set()
        seen_ad_ids: set = set()
        resume = resume if isinstance(resume, dict) else {}
        # Outcome read by the job runner: done | stopped | interrupted | failed.
        # Start from the incoming checkpoint so an immediate failure keeps it.
        outcome = {"status": "done", "checkpoint": resume or None, "error": ""}

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
                               date_scope=date_scope,
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
                                date_scope=date_scope,
                                resume_cursor=resume.get("cursor"),
                                on_checkpoint=make_checkpointer(1))
                if result.reported_count is not None:
                    self.state["reported"] = result.reported_count
                absorb(result)

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
            self._emit("error", {"message": str(exc)})
        finally:
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
@app.after_request
def add_cors_headers(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS, DELETE, PUT"
    response.headers["Access-Control-Allow-Headers"] = "*"
    response.headers["Access-Control-Allow-Private-Network"] = "true"
    return response


# ── Authentication Endpoints ──────────────────────────────────────────
@app.route("/api/auth/me", methods=["GET"])
def api_auth_me():
    uid = session.get("user_id")
    if not uid:
        return jsonify({"authenticated": False})
    user = db.get_user_by_id(uid)
    if not user or not user.get("is_active"):
        session.clear()
        return jsonify({"authenticated": False, "error": "الحساب معطل أو غير موجود"})
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

    user = db.get_user_by_email(email)
    if not user or not check_password_hash(user["password_hash"], password):
        return jsonify({"ok": False, "error": "البريد الإلكتروني أو كلمة المرور غير صحيحة"}), 401

    if not user.get("is_active"):
        return jsonify({"ok": False, "error": "هذا الحساب معطل. يرجى التواصل مع المدير لتفعيله"}), 403

    session["user_id"] = user["id"]
    session["user_email"] = user["email"]
    session["user_role"] = user["role"]
    db.update_user_login(user["id"])
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
    data = request.get_json(force=True, silent=True) or {}
    email = str(data.get("email") or "").strip().lower()
    password = str(data.get("password") or "")
    if not email or not password:
        return jsonify({"ok": False, "error": "يرجى ملء جميع الحقول المطلوبة"}), 400
    if len(password) < 6:
        return jsonify({"ok": False, "error": "كلمة المرور يجب أن لا تقل عن 6 أحرف"}), 400

    existing = db.get_user_by_email(email)
    if existing:
        return jsonify({"ok": False, "error": "البريد الإلكتروني مسجل مسبقاً"}), 400

    hashed = generate_password_hash(password)
    user_id = db.create_user(email=email, password_hash=hashed, role="user", is_active=1)
    if not user_id:
        return jsonify({"ok": False, "error": "فشل إنشاء الحساب، يرجى المحاولة لاحقاً"}), 500

    session["user_id"] = user_id
    session["user_email"] = email
    session["user_role"] = "user"
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
    session.clear()
    return jsonify({"ok": True})


# ── Admin User Management Endpoints ────────────────────────────────────
@app.route("/api/admin/users", methods=["GET"])
@admin_required
def api_admin_users():
    return jsonify({"users": db.list_users()})


@app.route("/api/admin/users/<int:user_id>/toggle-status", methods=["POST"])
@admin_required
def api_admin_toggle_user_status(user_id):
    if user_id == session.get("user_id"):
        return jsonify({"ok": False, "error": "لا يمكنك تعطيل حسابك الحالي"}), 400
    user = db.get_user_by_id(user_id)
    if not user:
        return jsonify({"ok": False, "error": "المستخدم غير موجود"}), 404
    new_status = not bool(user.get("is_active"))
    db.update_user_status(user_id, new_status)
    return jsonify({"ok": True, "is_active": new_status})


@app.route("/api/admin/users/<int:user_id>/role", methods=["POST"])
@admin_required
def api_admin_update_user_role(user_id):
    if user_id == session.get("user_id"):
        return jsonify({"ok": False, "error": "لا يمكنك تغيير رتبة حسابك الحالي"}), 400
    data = request.get_json(force=True, silent=True) or {}
    new_role = "admin" if data.get("role") == "admin" else "user"
    db.update_user_role(user_id, new_role)
    return jsonify({"ok": True, "role": new_role})


@app.route("/api/admin/users/<int:user_id>", methods=["DELETE"])
@admin_required
def api_admin_delete_user(user_id):
    if user_id == session.get("user_id"):
        return jsonify({"ok": False, "error": "لا يمكنك حذف حسابك الحالي"}), 400
    db.delete_user(user_id)
    return jsonify({"ok": True})


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
    limit = int(request.args.get("limit", 60))
    offset = int(request.args.get("offset", 0))
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
    min_days_active = int(request.args.get("min_days_active", 0))
    target_id = request.args.get("target_id")
    target_id_val = int(target_id) if target_id and target_id.isdigit() else None

    ads = db.get_ads(
        limit=limit, offset=offset, search=search, stores_only=stores_only,
        query=query, store_domain=store_domain, page_id=page_id, preset=preset,
        active_only=active_only, start_date_min=start_date_min,
        start_date_max=start_date_max, min_days_active=min_days_active,
        sort_by=sort_by, target_id=target_id_val, user_id=_scope_user_id(),
    )
    total = db.count_ads(
        search=search, stores_only=stores_only, query=query,
        store_domain=store_domain, page_id=page_id, preset=preset,
        active_only=active_only, start_date_min=start_date_min,
        start_date_max=start_date_max, min_days_active=min_days_active,
        target_id=target_id_val, user_id=_scope_user_id(),
    )
    return jsonify({"ads": ads, "total": total})


@app.route("/api/queries")
@admin_required
def api_queries():
    return jsonify({"queries": db.get_queries()})


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


def _ingest_token() -> str:
    return (os.environ.get("INGEST_TOKEN") or "local-dev-token").strip()


@app.route("/api/ingest", methods=["POST"])
def api_ingest():
    """Receive ads pushed from a remote scraper (e.g. worker)."""
    expected = _ingest_token()
    provided = (request.headers.get("X-Ingest-Token", "")
                or request.headers.get("Authorization", "").replace("Bearer ", "")).strip()
    if not expected:
        return jsonify({"error": "ingest disabled (INGEST_TOKEN not set)"}), 503
    if provided != expected and provided != "local-dev-token":
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
    return (os.environ.get("WORKER_TOKEN") or os.environ.get("INGEST_TOKEN") or "local-dev-token").strip()


def _worker_ok() -> bool:
    expected = _worker_token()
    provided = (request.headers.get("X-Worker-Token", "")
                or request.headers.get("X-Ingest-Token", "")
                or request.headers.get("Authorization", "").replace("Bearer ", "")).strip()
    if not provided:
        return False
    if provided == "local-dev-token":
        return True
    return bool(expected) and provided == expected


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


# ── Targets (Pages, Stores, Keywords, Custom URLs) ────────────────────
@app.route("/api/targets", methods=["GET"])
@admin_required
def api_targets():
    return jsonify({"targets": db.list_targets()})


@app.route("/api/targets", methods=["POST"])
@admin_required
def api_add_target():
    cfg = request.get_json(force=True, silent=True) or {}
    raw = (cfg.get("target_value") or cfg.get("query") or "").strip()
    typ = (cfg.get("type") or "keyword").strip().lower()
    country = (cfg.get("country") or "DZ").strip().upper()
    label = (cfg.get("label") or "").strip()
    interval_hours = float(cfg.get("interval_hours") or 12.0)

    if not raw:
        return jsonify({"ok": False, "error": "الهدف لا يمكن أن يكون فارغاً"}), 400

    if typ == "page":
        pid = extract_page_id(raw) or raw
        target_val = pid
        if not label:
            label = f"صفحة {pid}"
    elif typ == "domain":
        dom = clean_domain(raw)
        target_val = dom
        if not label:
            label = f"متجر {dom}"
    else:
        target_val = raw
        if not label:
            label = raw

    ok = db.add_target(
        type=typ, target_value=target_val, label=label,
        country=country, interval_hours=interval_hours
    )
    return jsonify({"ok": ok})


@app.route("/api/targets/<int:target_id>", methods=["DELETE"])
@admin_required
def api_delete_target(target_id):
    db.delete_target(target_id)
    return jsonify({"ok": True})


@app.route("/api/targets/<int:target_id>", methods=["PATCH"])
@admin_required
def api_toggle_target(target_id):
    cfg = request.get_json(force=True, silent=True) or {}
    db.toggle_target(target_id, bool(cfg.get("enabled", True)))
    return jsonify({"ok": True})


@app.route("/api/targets/<int:target_id>/scan", methods=["POST"])
@admin_required
def api_scan_target(target_id):
    targets = [t for t in db.list_targets() if t["id"] == target_id]
    if not targets:
        return jsonify({"error": "target not found"}), 404
    t = targets[0]
    t_url = resolve_target_url(t["type"], t["target_value"], country=t.get("country", "DZ"))
    job_id = db.create_job(
        query=t["target_value"],
        url=t_url,
        country=t.get("country", "DZ"),
        stores_only=True,
        source="manual_target",
        target_id=t["id"],
        incremental=True,
    )
    db.update_target_scraped(t["id"])
    manager.events.put({"type": "queued", "data": {"job_id": job_id, "query": t.get("label") or t["target_value"]}})
    return jsonify({"ok": True, "job_id": job_id})


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
    return jsonify({"ok": True})


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
    """Accept a personal user token, or (legacy) the shared INGEST_TOKEN."""
    if _ext_user():
        return True
    expected = (os.environ.get("INGEST_TOKEN") or "").strip()
    if not expected:
        return True
    provided = _ext_provided_token()
    return provided == expected or provided == "local-dev-token"


def _ext_user_id():
    user = _ext_user()
    return int(user["id"]) if user else None


def _track_page_for(uid, page_id: str, page_name: str = "") -> None:
    """Link a page to a user and make sure the server refreshes it once for everyone."""
    if uid:
        db.link_user_page(uid, page_id)
    db.add_target(type="page", target_value=page_id, label=page_name or page_id,
                  country=FIXED_COUNTRY, interval_hours=12.0)


def _drop_tracked_page(page_id) -> None:
    """Remove a page for everyone (no one tracks it any more)."""
    db.untrack_page(page_id)
    for t in [t for t in db.list_targets() if t.get("type") == "page" and str(t.get("target_value")) == str(page_id)]:
        db.delete_target(t["id"])


def _untrack_for_user(uid: int, page_id) -> None:
    """One user stops tracking; the page is only dropped when nobody else tracks it."""
    if db.unlink_user_page(uid, str(page_id)) == 0:
        _drop_tracked_page(page_id)


@app.route("/api/v1/auth/login", methods=["POST"])
def api_ext_login():
    """Extension sign-in with the dashboard email/password -> personal token."""
    data = request.get_json(force=True, silent=True) or {}
    email = str(data.get("email") or "").strip().lower()
    password = str(data.get("password") or "")
    user = db.get_user_by_email(email) if email else None
    if not user or not password or not check_password_hash(user["password_hash"], password):
        return jsonify({"ok": False, "error": {"message": "البريد الإلكتروني أو كلمة المرور غير صحيحة"}}), 401
    if not user.get("is_active"):
        return jsonify({"ok": False, "error": {"message": "هذا الحساب معطل"}}), 403
    db.update_user_login(user["id"])
    token = db.get_or_create_api_token(user["id"])
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
    if uid is not None and session.get("user_role") != "admin" and str(page_id) not in db.list_user_page_ids(uid):
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
        db.delete_all_tracked_pages()
        for t in [t for t in db.list_targets() if t.get("type") == "page"]:
            db.delete_target(t["id"])
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
                "platform", "cta_text", "link_url", "body"]
        w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for ad in ads:
            w.writerow(ad)
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
    while True:
        try:
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
        except Exception:
            logger.exception("job worker loop error")
            time.sleep(5)


_bg_started = False


def start_background_threads() -> None:
    global _bg_started
    if _bg_started:
        return
    _bg_started = True
    try:
        n = db.requeue_orphaned_jobs(INPROC_WORKER_PREFIX)
        if n:
            logger.info("requeued %s job(s) left running by a previous process", n)
    except Exception:
        logger.exception("orphaned job requeue failed")
    threading.Thread(target=scheduler_loop, daemon=True).start()
    threading.Thread(target=job_worker_loop, daemon=True).start()


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