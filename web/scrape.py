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

bp = Blueprint("scrape", __name__)


bp = Blueprint("scrape", __name__)


def _detect_scrape_input(raw: str) -> tuple[str, str]:
    """What the single scrape input holds -> (kind, value):
    link (Ad Library URL) | page (page id) | domain (store) | keyword.
    A Facebook page URL without a numeric id gives ("page", "")."""
    import re as _re
    v = (raw or "").strip()
    low = v.lower()
    if "facebook.com" in low and "/ads/library" in low:
        return "link", v
    if _re.fullmatch(r"\d{9,20}", v):
        return "page", v
    if "facebook.com/" in low or "fb.com/" in low:
        return "page", extract_page_id(v) or ""
    if " " not in v and _re.fullmatch(r"(https?://)?(www\.)?[a-z0-9-]+(\.[a-z0-9-]+)+(/\S*)?", low):
        return "domain", clean_domain(v)
    return "keyword", v


MARKET_MAX_DAYS = 62


@bp.route("/api/start", methods=["POST"])
@admin_required
def api_start():
    """Start a scrape: targeted (keyword / page / store / Ad Library link) or
    the whole Arabic market of Algeria, both limited to a launch-date window."""
    from datetime import date as _date
    from slicing import resolve_date_range
    cfg = request.get_json(force=True, silent=True) or {}
    mode = (cfg.get("mode") or "targeted").strip()
    try:
        d_min, d_max, label = resolve_date_range(cfg.get("date_preset") or "all",
                                                 (cfg.get("start_date_min") or "").strip(),
                                                 (cfg.get("start_date_max") or "").strip())
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400

    if mode in (MONTH_MODE, "market"):
        if not d_min:  # the whole market always needs a window
            d_min, d_max, label = resolve_date_range("this_month")
        days = (_date.fromisoformat(d_max) - _date.fromisoformat(d_min)).days + 1
        if days > MARKET_MAX_DAYS:
            return jsonify({"ok": False, "error": f"ط·آ§ط¸â€‍ط¸ظ¾ط·ع¾ط·آ±ط·آ© ط·آ·ط¸ث†ط¸ظ¹ط¸â€‍ط·آ© ط·آ¬ط·آ¯ط·آ§ط¸â€¹ ({days} ط¸ظ¹ط¸ث†ط¸â€¦ط·آ§ط¸â€¹). ط·آ§ط¸â€‍ط·آ­ط·آ¯ ط·آ§ط¸â€‍ط·آ£ط¸â€ڑط·آµط¸â€° {MARKET_MAX_DAYS} ط¸ظ¹ط¸ث†ط¸â€¦ط·آ§ط¸â€¹."}), 400
        job_id, queued = _queue_month_scrape("manual", start=d_min, end=d_max)
        return jsonify({"ok": True, "queued": queued, "job_id": job_id, "mode": MONTH_MODE,
                        "start_date_min": d_min, "start_date_max": d_max, "date_label": label, "days": days})

    raw = (cfg.get("query") or cfg.get("url") or "").strip()
    if not raw:
        return jsonify({"ok": False, "error": "ط·آ§ط¸ئ’ط·ع¾ط·آ¨ ط¸ئ’ط¸â€‍ط¸â€¦ط·آ© ط·آ£ط¸ث† ط·آ±ط·آ§ط·آ¨ط·آ· ط·آµط¸ظ¾ط·آ­ط·آ© ط·آ£ط¸ث† ط·آ¯ط¸ث†ط¸â€¦ط¸ظ¹ط¸â€  ط¸â€¦ط·ع¾ط·آ¬ط·آ± ط·آ£ط¸ث† ط·آ±ط·آ§ط·آ¨ط·آ· ط¸â€¦ط¸ئ’ط·ع¾ط·آ¨ط·آ© ط·آ¥ط·آ¹ط¸â€‍ط·آ§ط¸â€ ط·آ§ط·ع¾"}), 400
    if len(raw) > 2000:
        return jsonify({"ok": False, "error": "ط·آ§ط¸â€‍ط¸â€ ط·آµ ط·آ·ط¸ث†ط¸ظ¹ط¸â€‍ ط·آ¬ط·آ¯ط·آ§ط¸â€¹"}), 400
    country = (cfg.get("country") or DEFAULT_COUNTRY).strip().upper()
    if country != "ALL" and not (len(country) == 2 and country.isalpha()):
        country = DEFAULT_COUNTRY
    kind, value = _detect_scrape_input(raw)
    query, url, exact = "", "", bool(cfg.get("exact", False))
    if kind == "link":
        url = value
    elif kind == "page":
        if not value:
            return jsonify({"ok": False, "error": "ط·آ±ط·آ§ط·آ¨ط·آ· ط·آ§ط¸â€‍ط·آµط¸ظ¾ط·آ­ط·آ© ط¸â€‍ط·آ§ ط¸ظ¹ط·آ­ط·ع¾ط¸ث†ط¸ظ¹ ط·آ±ط¸â€ڑط¸â€¦ط¸â€،ط·آ§ (ID). ط·آ§ط¸â€‍ط·آµط¸â€ڑ ط·آ±ط¸â€ڑط¸â€¦ ط·آ§ط¸â€‍ط·آµط¸ظ¾ط·آ­ط·آ© ط·آ£ط¸ث† ط·آ±ط·آ§ط·آ¨ط·آ· ط¢آ«ط¸ئ’ط¸â€‍ ط·آ¥ط·آ¹ط¸â€‍ط·آ§ط¸â€ ط·آ§ط·ع¾ ط·آ§ط¸â€‍ط·آµط¸ظ¾ط·آ­ط·آ©ط¢آ» ط¸â€¦ط¸â€  ط¸â€¦ط¸ئ’ط·ع¾ط·آ¨ط·آ© ط·آ§ط¸â€‍ط·آ¥ط·آ¹ط¸â€‍ط·آ§ط¸â€ ط·آ§ط·ع¾."}), 400
        url = resolve_target_url("page", value, country=country)
    elif kind == "domain":
        query, exact = value, True   # every ad mentioning the store's domain
    else:
        query = value

    date_scope = "launch" if d_min else "impressions"
    sort_mode = "relevancy_monthly_grouped" if d_min else "total_impressions"
    busy = bool(manager.state.get("running")) or db.has_pending_jobs()
    job_id = db.create_job(
        query=query, url=url, country=country, exact_phrase=exact, stores_only=True,
        sweep=False, sort_mode=sort_mode, source="manual",
        start_date_min=d_min, start_date_max=d_max, date_scope=date_scope,
    )
    job_wakeup.set()
    if busy:
        manager._emit("log", {"text": f"ظ‹ع؛â€¢â€™ ط·آ£ط¸عˆط·آ¶ط¸ظ¹ط¸ظ¾ ط·آ§ط¸â€‍ط·آ³ط·آ­ط·آ¨ ط¢آ«{query or url}ط¢آ» ط·آ¥ط¸â€‍ط¸â€° ط¸â€ڑط·آ§ط·آ¦ط¸â€¦ط·آ© ط·آ§ط¸â€‍ط·آ§ط¸â€ ط·ع¾ط·آ¸ط·آ§ط·آ± ط¸ث†ط·آ³ط¸ظ¹ط·آ¨ط·آ¯ط·آ£ ط·آ¨ط·آ¹ط·آ¯ ط·آ§ط¸â€ ط·ع¾ط¸â€،ط·آ§ط·طŒ ط·آ§ط¸â€‍ط·آ³ط·آ­ط·آ¨ ط·آ§ط¸â€‍ط·آ­ط·آ§ط¸â€‍ط¸ظ¹."})
    return jsonify({"ok": True, "queued": busy, "job_id": job_id, "kind": kind, "value": value,
                    "start_date_min": d_min, "start_date_max": d_max, "date_label": label, "date_scope": date_scope})


@bp.route("/api/stop", methods=["POST"])
@admin_required
def api_stop():
    """Stop the running scrape AND cancel manual scrapes waiting to resume."""
    manager.stop()
    jid = manager.state.get("job_id")
    if jid:
        try:
            db.request_stop(int(jid))
        except Exception:
            logger.exception("request_stop failed for job %s", jid)
    cancelled = db.cancel_pending_jobs(source="manual")
    if cancelled:
        manager._emit("log", {"text": f"أ¢عˆآ¹ ط·آ£ط¸عˆط¸â€‍ط·ط›ط¸ظ¹ {cancelled} ط¸â€¦ط¸â€،ط¸â€¦ط·آ© ط¸ظ¾ط¸ظ¹ ط·آ§ط¸â€‍ط·آ§ط¸â€ ط·ع¾ط·آ¸ط·آ§ط·آ±."})
    manager._emit("log", {"text": f"أ¢عˆآ¹ ط·ع¾ط¸â€¦ ط·آ¥ط·آ±ط·آ³ط·آ§ط¸â€‍ ط·آ£ط¸â€¦ط·آ± ط·آ§ط¸â€‍ط·آ¥ط¸ظ¹ط¸â€ڑط·آ§ط¸ظ¾{f' ط¸â€‍ط¸â€‍ط¸â€¦ط¸â€،ط¸â€¦ط·آ© #{jid}' if jid else ''}."})
    return jsonify({"ok": True, "job_id": jid, "cancelled": cancelled})


@bp.route("/api/status")
@login_required
def api_status():
    return jsonify({**manager.state, "scrape_enabled": SCRAPE_ENABLED, "db": db.get_stats()})


@bp.route("/api/stream")
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


# أ¢â€‌â‚¬أ¢â€‌â‚¬ Data Endpoints أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬
def _token_matches(provided: str, expected: str) -> bool:
    """Constant-time compare; an unset secret never matches anything."""
    import hmac
    provided, expected = (provided or "").strip(), (expected or "").strip()
    return bool(provided) and bool(expected) and hmac.compare_digest(provided, expected)


def _ingest_token() -> str:
    return (os.environ.get("INGEST_TOKEN") or "").strip()


@bp.route("/api/ingest", methods=["POST"])
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


# أ¢â€‌â‚¬أ¢â€‌â‚¬ Remote Job Queue أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬
def _worker_token() -> str:
    return (os.environ.get("WORKER_TOKEN") or os.environ.get("INGEST_TOKEN") or "").strip()


def _worker_ok() -> bool:
    """Remote-worker API: closed unless WORKER_TOKEN / INGEST_TOKEN is set."""
    provided = (request.headers.get("X-Worker-Token", "")
                or request.headers.get("X-Ingest-Token", "")
                or request.headers.get("Authorization", "").replace("Bearer ", "")).strip()
    return _token_matches(provided, _worker_token())


@bp.route("/api/jobs", methods=["GET"])
@admin_required
def api_jobs():
    return jsonify({"jobs": db.list_jobs(limit=60)})


@bp.route("/api/jobs", methods=["POST"])
@admin_required
def api_create_job():
    cfg = request.get_json(force=True, silent=True) or {}
    url = (cfg.get("url") or "").strip()
    query = (cfg.get("query") or "").strip()
    if not url and query and "facebook.com" in query and "/ads/library" in query:
        url, query = query, ""
    if not url and not query:
        return jsonify({"error": "query or url required"}), 400
    job_id = db.create_job(
        query=query,
        url=url,
        country=cfg.get("country", DEFAULT_COUNTRY),
        exact_phrase=bool(cfg.get("exact", False)),
        stores_only=bool(cfg.get("stores_only", False)),
        sweep=False,  # Deep Sweep removed: use the day-by-day market mode
        sort_mode=cfg.get("sort", "total_impressions"),
        source="manual",
        date_scope=(cfg.get("date_scope") or "impressions"),
    )
    return jsonify({"ok": True, "job_id": job_id})


@bp.route("/api/worker/next")
def api_worker_next():
    if not _worker_ok():
        return jsonify({"error": "unauthorized"}), 401
    worker_id = request.args.get("worker_id", "worker")
    job = db.claim_next_job(worker_id)
    if not job:
        return ("", 204)
    return jsonify({"job": job})


@bp.route("/api/worker/result", methods=["POST"])
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


@bp.route("/api/worker/progress", methods=["POST"])
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


@bp.route("/api/jobs/<int:job_id>/stop", methods=["POST"])
@admin_required
def api_job_stop(job_id):
    db.request_stop(job_id)
    manager.events.put({"type": "job", "data": {"job_id": job_id, "status": "stopping"}})
    return jsonify({"ok": True})


# أ¢â€‌â‚¬أ¢â€‌â‚¬ Competitors (per user: Pages, Stores, Keywords, Custom URLs) أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬
@bp.route("/api/status-check/start", methods=["POST"])
@admin_required
def api_status_check_start():
    if db.has_open_mode_job(STATUS_MODE):
        return jsonify({"ok": False, "error": "ط¸ظ¾ط·آ­ط·آµ ط·آ§ط¸â€‍ط·آ­ط·آ§ط¸â€‍ط·آ© ط¸â€¦ط¸ث†ط·آ¬ط¸ث†ط·آ¯ ط¸ظ¾ط¸ظ¹ ط·آ§ط¸â€‍ط¸â€ڑط·آ§ط·آ¦ط¸â€¦ط·آ© ط·آ£ط¸ث† ط¸ظ¹ط·آ¹ط¸â€¦ط¸â€‍ ط·آ§ط¸â€‍ط·آ¢ط¸â€ ."}), 409
    job_id, queued = _queue_status_check("manual")
    return jsonify({"ok": True, "job_id": job_id, "queued": queued})




