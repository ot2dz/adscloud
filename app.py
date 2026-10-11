# -*- coding: utf-8 -*-
"""
MetaAdsPlaywright web dashboard.

Flask API + SSE dashboard around the Playwright scraper. Ads are streamed live
and persisted to PostgreSQL (or SQLite locally).
"""
from __future__ import annotations

import csv
import io
import json
import logging
import os
import queue
import threading
import time
from datetime import datetime, timezone

from flask import Flask, Response, jsonify, render_template, request, send_file

from db import db
from engine import scrape
from pages import clean_domain, extract_page_id, resolve_target_url


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

# Targeting policy for this deployment: Arabic-only ads running in Algeria.
FIXED_COUNTRY = "DZ"
FIXED_LANGS = ["ar"]

# Local scraping needs a Chromium browser. On a lightweight dashboard host
# (e.g. Coolify without Chromium) set SCRAPE_ENABLED=false so the UI becomes
# display/ingest-only and queues scrape jobs for a remote worker instead.
SCRAPE_ENABLED = os.environ.get("SCRAPE_ENABLED", "true").strip().lower() in ("1", "true", "yes", "on")


class ScrapeManager:
    def __init__(self):
        self.thread = None
        self.stop_event = threading.Event()
        self.events = queue.Queue()
        self.state = {"running": False, "query": "", "scraped": 0, "stored": 0,
                      "stores": 0, "reported": None, "log": []}

    def _emit(self, kind: str, data: dict) -> None:
        self.events.put({"type": kind, "data": data})

    def start(self, query: str, exact: bool, stores_only: bool,
              sort_mode: str, sweep: bool = False, url: str = "",
              start_date_min: str = "", start_date_max: str = "", date_label: str = "") -> bool:
        if self.state["running"]:
            return False
        self.stop_event.clear()
        while not self.events.empty():
            self.events.get_nowait()
        self.state.update({"running": True, "query": query or url, "scraped": 0,
                           "stored": 0, "stores": 0, "reported": None, "log": []})
        self.thread = threading.Thread(
            target=self._run,
            args=(query, exact, stores_only, sort_mode, sweep, url, start_date_min, start_date_max, date_label),
            daemon=True)
        self.thread.start()
        return True

    def stop(self) -> None:
        self.stop_event.set()

    def _run(self, query, exact, stores_only, sort_mode, sweep, url, start_date_min, start_date_max, date_label) -> None:
        country = FIXED_COUNTRY
        languages = FIXED_LANGS
        seen_stores: set = set()
        seen_ad_ids: set = set()

        def progress(msg):
            print(f"  â–¶ [SCRAPER] {msg}", flush=True)
            self.state["log"].append(msg)
            self.state["log"] = self.state["log"][-200:]
            self._emit("log", {"text": msg})

        def on_reported(count):
            if count is not None:
                self.state["reported"] = count
                self._emit("reported", {"reported": count, "query": query or url})

        def on_ad(ad):
            if self.stop_event.is_set():
                return
            d = ad.to_dict()
            aid = d.get("ad_id")
            if aid and aid not in seen_ad_ids:
                seen_ad_ids.add(aid)
                stored = db.save_ad(d, query=query, country=country)
                self.state["scraped"] = len(seen_ad_ids)
                if stored:
                    self.state["stored"] += 1
                if ad.store_domain:
                    seen_stores.add(ad.store_domain)
                self.state["stores"] = len(seen_stores)
                self._emit("scrape_progress", {
                    "scraped": len(seen_ad_ids),
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
                progress(f"ًںڑ€ ط¨ط¯ط، ط§ظ„طھط؛ط·ظٹط© ط§ظ„ظ…ظˆط³ط¹ط© (Deep Sweep): طھظ‚ط³ظٹظ… ط§ظ„ط¨ط­ط« ط¥ظ„ظ‰ {total_passes} ط´ط±ظٹط­ط© ط´ظ‡ط±ظٹط©.")

                for idx, p in enumerate(passes, 1):
                    if self.stop_event.is_set():
                        progress("âڈ¹ طھظ… ط¥ظٹظ‚ط§ظپ ط§ظ„ط³ط­ط¨ ط¨ط·ظ„ط¨ ظ…ظ† ط§ظ„ظ…ط³طھط®ط¯ظ….")
                        break
                    progress(f"â†» ط´ط±ظٹط­ط© {idx}/{total_passes} (ط´ظ‡ط± {p['label']}) â€” ط¥ط¬ظ…ط§ظ„ظٹ ظ…ط¬ظ…ط¹: {len(seen_ad_ids)} ط¥ط¹ظ„ط§ظ†.")
                    self._emit("slice_update", {
                        "pass": idx,
                        "total_passes": total_passes,
                        "label": p["label"],
                        "accumulated": len(seen_ad_ids)
                    })
                    try:
                        scrape(query=query, country=country, url=p["url"],
                               stores_only=False, progress=progress, on_ad=on_ad,
                               on_reported=on_reported,
                               should_stop=lambda: self.stop_event.is_set())
                    except Exception as pass_err:
                        progress(f"âڑ ï¸ڈ طھط¬ط§ظˆط² ط§ظ„ط´ط±ظٹط­ط© {idx} ط¨ط³ط¨ط¨ ط®ط·ط£: {pass_err}")

            else:
                if date_label:
                    progress(f"ًں“… ظ†ط·ط§ظ‚ ط§ظ„ط³ط­ط¨ ط§ظ„ط²ظ…ظ†ظٹ: {date_label}")
                result = scrape(query, country=country, url=url or None, exact_phrase=exact,
                                sort_mode=sort_mode, languages=languages,
                                start_date_min=start_date_min or None,
                                start_date_max=start_date_max or None,
                                stores_only=stores_only,
                                progress=progress, on_ad=on_ad, on_reported=on_reported,
                                should_stop=lambda: self.stop_event.is_set())
                self.state["reported"] = result.reported_count

            self._emit("finished", {
                "query": query or url,
                "scraped": len(seen_ad_ids),
                "store_ads": len(seen_ad_ids),
                "stored": self.state["stored"],
                "stores": len(seen_stores),
                "reported": self.state["reported"],
            })
            logger.info("Scrape finished: %s ads, %s stores", len(seen_ad_ids), len(seen_stores))
        except Exception as exc:  # pragma: no cover
            logger.exception("scrape failed")
            self._emit("error", {"message": str(exc)})
        finally:
            self.state["running"] = False
            self._emit("done", {})


manager = ScrapeManager()


# â”€â”€ pages â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
@app.route("/")
def index():
    return render_template("index.html")


# â”€â”€ CORS & Private Network Access (Chrome Extensions support) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
@app.after_request
def add_cors_headers(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS, DELETE, PUT"
    response.headers["Access-Control-Allow-Headers"] = "*"
    response.headers["Access-Control-Allow-Private-Network"] = "true"
    return response


# â”€â”€ engine control â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
@app.route("/api/start", methods=["POST"])
def api_start():
    cfg = request.get_json(force=True, silent=True) or {}
    url = (cfg.get("url") or "").strip()
    query = (cfg.get("query") or "").strip()
    if not url and not query:
        return jsonify({"error": "query or url required"}), 400

    from slicing import resolve_date_range
    date_preset = cfg.get("date_preset", "all")
    raw_min = (cfg.get("start_date_min") or "").strip()
    raw_max = (cfg.get("start_date_max") or "").strip()
    start_date_min, start_date_max, date_label = resolve_date_range(date_preset, raw_min, raw_max)

    # No local browser on this host -> queue the job for a remote worker.
    if not SCRAPE_ENABLED:
        job_id = db.create_job(
            query=query,
            url=url,
            country=FIXED_COUNTRY,
            exact_phrase=bool(cfg.get("exact", False)),
            stores_only=bool(cfg.get("stores_only", False)),
            sweep=bool(cfg.get("sweep", False)),
            sort_mode=cfg.get("sort", "total_impressions"),
            source="manual",
            start_date_min=start_date_min,
            start_date_max=start_date_max,
        )
        manager.events.put({"type": "queued", "data": {"job_id": job_id, "query": query or url}})
        return jsonify({"ok": True, "queued": True, "job_id": job_id, "date_label": date_label})

    # If date window is specified, default to relevancy_monthly_grouped so Meta serves recent ads first
    chosen_sort = cfg.get("sort", "total_impressions")
    if (start_date_min or start_date_max) and chosen_sort == "total_impressions":
        chosen_sort = "relevancy_monthly_grouped"

    ok = manager.start(
        query=query,
        exact=bool(cfg.get("exact", False)),
        stores_only=bool(cfg.get("stores_only", False)),
        sort_mode=chosen_sort,
        sweep=bool(cfg.get("sweep", False)),
        url=url,
        start_date_min=start_date_min,
        start_date_max=start_date_max,
        date_label=date_label,
    )
    return jsonify({"ok": ok, "date_label": date_label})


@app.route("/api/stop", methods=["POST"])
def api_stop():
    manager.stop()
    return jsonify({"ok": True})


@app.route("/api/status")
def api_status():
    return jsonify({**manager.state, "scrape_enabled": SCRAPE_ENABLED, "db": db.get_stats()})


@app.route("/api/stream")
def api_stream():
    def gen():
        yield "retry: 3000\n\n"
        while True:
            try:
                item = manager.events.get(timeout=15)
                yield f"data: {json.dumps(item, ensure_ascii=False)}\n\n"
            except queue.Empty:
                yield "data: {\"type\": \"ping\"}\n\n"

    return Response(gen(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                             "Connection": "keep-alive"})


# â”€â”€ data â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
@app.route("/api/ads")
def api_ads():
    limit = int(request.args.get("limit", 60))
    offset = int(request.args.get("offset", 0))
    search = request.args.get("search", "")
    query = request.args.get("query", "")
    store_domain = request.args.get("store_domain", "")
    page_id = request.args.get("page_id", "")
    stores_only = request.args.get("stores_only", "0") == "1"
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
        sort_by=sort_by, target_id=target_id_val,
    )
    total = db.count_ads(
        search=search, stores_only=stores_only, query=query,
        store_domain=store_domain, page_id=page_id, preset=preset,
        active_only=active_only, start_date_min=start_date_min,
        start_date_max=start_date_max, min_days_active=min_days_active,
        target_id=target_id_val,
    )
    return jsonify({"ads": ads, "total": total})


@app.route("/api/queries")
def api_queries():
    return jsonify({"queries": db.get_queries()})


@app.route("/api/stores")
def api_stores():
    return jsonify({"stores": db.get_stores(search=request.args.get("search", ""))})


@app.route("/api/stats")
def api_stats():
    return jsonify(db.get_stats())


@app.route("/api/reset", methods=["POST"])
def api_reset():
    db.clear_ads()
    return jsonify({"ok": True})


def _ingest_token() -> str:
    return (os.environ.get("INGEST_TOKEN") or "local-dev-token").strip()


@app.route("/api/ingest", methods=["POST"])
def api_ingest():
    """Receive ads pushed from a remote scraper (e.g. the Windows machine)."""
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
    country = payload.get("country", FIXED_COUNTRY)
    stored = 0
    for ad in ads:
        if isinstance(ad, dict) and db.save_ad(ad, query=query, country=country):
            stored += 1
    logger.info("ingest: received=%s stored=%s query=%r", len(ads), stored, query)
    return jsonify({"ok": True, "received": len(ads), "stored": stored})


# â”€â”€ remote job queue â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
def _worker_token() -> str:
    return (os.environ.get("WORKER_TOKEN") or os.environ.get("INGEST_TOKEN") or "local-dev-token").strip()


def _worker_ok() -> bool:
    expected = _worker_token()
    provided = (request.headers.get("X-Worker-Token", "")
                or request.headers.get("X-Ingest-Token", "")
                or request.headers.get("Authorization", "").replace("Bearer ", "")).strip()
    if not provided:
        return False
    # Allow local-dev-token for seamless local development
    if provided == "local-dev-token":
        return True
    return bool(expected) and provided == expected


@app.route("/api/jobs", methods=["GET"])
def api_jobs():
    return jsonify({"jobs": db.list_jobs(limit=60)})


@app.route("/api/jobs", methods=["POST"])
def api_create_job():
    cfg = request.get_json(force=True, silent=True) or {}
    url = (cfg.get("url") or "").strip()
    query = (cfg.get("query") or "").strip()
    if not url and not query:
        return jsonify({"error": "query or url required"}), 400
    job_id = db.create_job(
        query=query,
        url=url,
        country=cfg.get("country", FIXED_COUNTRY),
        exact_phrase=bool(cfg.get("exact", False)),
        stores_only=bool(cfg.get("stores_only", False)),
        sweep=bool(cfg.get("sweep", False)),
        sort_mode=cfg.get("sort", "total_impressions"),
        source="manual",
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
    country = payload.get("country", FIXED_COUNTRY)

    new_ads = []
    for ad in ads:
        if not isinstance(ad, dict):
            continue
        if db.save_ad(ad, query=query, country=country):
            new_ads.append(ad)
        manager.events.put({"type": "ad", "data": ad})

    if job_id is not None:
        db.finish_job(int(job_id), status=status, result_count=len(ads), error=error)

    # Notify about newly discovered ads.
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
    """Worker reports live progress; response tells it whether to stop."""
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
def api_job_stop(job_id):
    db.request_stop(job_id)
    manager.events.put({"type": "job", "data": {"job_id": job_id, "status": "stopping"}})
    return jsonify({"ok": True})


# â”€â”€ targets (Pages, Stores, Keywords, Custom URLs) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
@app.route("/api/targets", methods=["GET"])
def api_targets():
    return jsonify({"targets": db.list_targets()})


@app.route("/api/targets", methods=["POST"])
def api_add_target():
    cfg = request.get_json(force=True, silent=True) or {}
    raw = (cfg.get("target_value") or cfg.get("query") or "").strip()
    typ = (cfg.get("type") or "keyword").strip().lower()
    country = (cfg.get("country") or "DZ").strip().upper()
    label = (cfg.get("label") or "").strip()
    interval_hours = float(cfg.get("interval_hours") or 12.0)

    if not raw:
        return jsonify({"ok": False, "error": "ط§ظ„ظ‡ط¯ظپ ظ„ط§ ظٹظ…ظƒظ† ط£ظ† ظٹظƒظˆظ† ظپط§ط±ط؛ط§ظ‹"}), 400

    if typ == "page":
        pid = extract_page_id(raw) or raw
        target_val = pid
        if not label:
            label = f"طµظپط­ط© {pid}"
    elif typ == "domain":
        dom = clean_domain(raw)
        target_val = dom
        if not label:
            label = f"ظ…طھط¬ط± {dom}"
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
def api_delete_target(target_id):
    db.delete_target(target_id)
    return jsonify({"ok": True})


@app.route("/api/targets/<int:target_id>", methods=["PATCH"])
def api_toggle_target(target_id):
    cfg = request.get_json(force=True, silent=True) or {}
    db.toggle_target(target_id, bool(cfg.get("enabled", True)))
    return jsonify({"ok": True})


@app.route("/api/targets/<int:target_id>/scan", methods=["POST"])
def api_scan_target(target_id):
    """Trigger an immediate scan for a specific target."""
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


# Backward compatibility for existing keyword routes
@app.route("/api/keywords", methods=["GET"])
def api_keywords():
    return jsonify({"keywords": db.list_keywords()})


@app.route("/api/keywords", methods=["POST"])
def api_add_keyword():
    return api_add_target()


@app.route("/api/keywords/<int:kw_id>", methods=["DELETE"])
def api_delete_keyword(kw_id):
    return api_delete_target(kw_id)


@app.route("/api/keywords/<int:kw_id>", methods=["PATCH"])
def api_toggle_keyword(kw_id):
    return api_toggle_target(kw_id)


# â”€â”€ settings / schedule / telegram â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
@app.route("/api/settings", methods=["GET"])
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
def api_telegram_test():
    """Test Telegram bot connection with immediate user feedback."""
    cfg = request.get_json(force=True, silent=True) or {}
    s = db.get_settings()
    token = (cfg.get("telegram_token") or s.get("telegram_token") or "").strip()
    chat_id = (cfg.get("telegram_chat_id") or s.get("telegram_chat_id") or "").strip()
    from notify import send_telegram_test
    ok, msg = send_telegram_test(token, chat_id)
    return jsonify({"ok": ok, "message": msg})


@app.route("/api/schedule/run-now", methods=["POST"])
def api_schedule_run_now():
    """Trigger an immediate scheduled pass across all active targets."""
    targets = [t for t in db.list_targets() if t.get("enabled")]
    count = 0
    for t in targets:
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
    manager.events.put({"type": "log", "data": {"text": f"âڑ، طھظ… ط¥ط·ظ„ط§ظ‚ ظپط­طµ ط´ط§ظ…ظ„ ظ„ظ€ {count} ط£ظ‡ط¯ط§ظپ ظ…ط¬ط¯ظˆظ„ط© ط§ظ„ط¢ظ†."}})
    return jsonify({"ok": True, "enqueued": count})


# â”€â”€ Chrome Extension (adlibrary) Integration API â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
@app.route("/api/v1/<path:path>", methods=["OPTIONS"])
def api_v1_options(path):
    """Direct 200 response for preflight OPTIONS requests from Chrome extensions."""
    return ("", 200)


@app.route("/api/v1/health", methods=["GET", "OPTIONS"])
def api_ext_health():
    """Health check for Chrome extension test connection."""
    payload = {"ok": True, "service": "adra-cloud", "version": "1.0.0"}
    return jsonify({"ok": True, "data": payload, **payload})


def _ext_token_ok() -> bool:
    """Optional auth for the extension API: open unless INGEST_TOKEN is set."""
    expected = (os.environ.get("INGEST_TOKEN") or "").strip()
    if not expected:
        return True  # no token configured -> open (local development)
    provided = (request.headers.get("X-Ingest-Token", "")
                or request.headers.get("Authorization", "").replace("Bearer ", "")).strip()
    return provided == expected or provided == "local-dev-token"


@app.route("/api/v1/ads", methods=["POST"])
def api_ext_save_ad():
    """Save an ad sent from the Chrome extension directly into the database."""
    if not _ext_token_ok():
        return jsonify({"ok": False, "error": {"message": "unauthorized"}}), 401
    data = request.get_json(force=True, silent=True) or {}
    ad_id = str(data.get("facebook_ad_id") or data.get("id") or "").strip()
    if not ad_id:
        return jsonify({"ok": False, "error": {"message": "facebook_ad_id required"}}), 400

    preview_img = data.get("preview_image_url") or data.get("previewImageUrl") or ""
    ad_dict = {
        "ad_id": ad_id,
        "page_id": str(data.get("page_id") or ""),
        "page_name": data.get("page_name") or "",
        "body": data.get("ad_text") or "",
        "link_url": data.get("landing_url") or "",
        "store_domain": data.get("landing_domain") or "",
        "platform": data.get("platform") or "",
        "cta_text": data.get("cta") or "",
        "start_date": data.get("start_date") or "",
        "is_active": True,
        "preview_image_url": preview_img,
    }
    is_new = db.save_ad(ad_dict, query="chrome_extension", country=FIXED_COUNTRY)
    manager.events.put({"type": "ad", "data": ad_dict})
    return jsonify({"ok": True, "data": {"saved": True, "is_new": is_new, "ad_id": ad_id}})


@app.route("/api/v1/pages/track", methods=["POST"])
def api_ext_track_page():
    """Add a competitor page to tracking targets from the extension."""
    if not _ext_token_ok():
        return jsonify({"ok": False, "error": {"message": "unauthorized"}}), 401
    data = request.get_json(force=True, silent=True) or {}
    page_id = str(data.get("facebook_page_id") or data.get("pageId") or "").strip()
    page_name = data.get("page_name") or f"طµظپط­ط© {page_id}"
    if not page_id:
        return jsonify({"ok": False, "error": {"message": "facebook_page_id required"}}), 400

    db.upsert_tracked_page(
        page_id=page_id, page_name=page_name,
        page_url=data.get("page_url") or "",
        page_likes=int(data.get("page_likes") or 0),
        platform=data.get("platform") or "",
        country=FIXED_COUNTRY, source="extension")
    db.add_target(
        type="page",
        target_value=page_id,
        label=page_name,
        country=FIXED_COUNTRY,
        interval_hours=12.0
    )
    logger.info("Chrome Extension tracked page: %s (%s)", page_name, page_id)
    return jsonify({"ok": True, "data": {"tracked": True, "page_id": page_id, "page_name": page_name}})


@app.route("/api/v1/pages/<page_id>/untrack", methods=["POST"])
def api_ext_untrack_page(page_id):
    """Untrack a page sent from the extension."""
    if not _ext_token_ok():
        return jsonify({"ok": False, "error": {"message": "unauthorized"}}), 401
    db.untrack_page(page_id)
    targets = [t for t in db.list_targets() if t.get("type") == "page" and str(t.get("target_value")) == str(page_id)]
    for t in targets:
        db.delete_target(t["id"])
    return jsonify({"ok": True, "data": {"untracked": True, "page_id": page_id}})


@app.route("/api/v1/pages", methods=["GET", "OPTIONS"])
def api_ext_list_pages():
    """List tracked pages with their latest counters (used by the extension)."""
    pages = db.list_tracked_pages()
    for pg in pages:
        pg.pop("snapshots", None)
    return jsonify({"ok": True, "data": {"pages": pages}})


@app.route("/api/v1/pages/<page_id>/snapshots", methods=["GET", "OPTIONS"])
def api_ext_page_snapshots(page_id):
    """Snapshot history for one tracked page."""
    return jsonify({"ok": True, "data": {"page_id": page_id, "snapshots": db.get_page_snapshots(page_id, limit=200)}})


@app.route("/api/v1/observations/page-snapshot", methods=["POST"])
def api_ext_page_snapshot():
    """Capture snapshot of visible ads on a page from the extension."""
    if not _ext_token_ok():
        return jsonify({"ok": False, "error": {"message": "unauthorized"}}), 401
    data = request.get_json(force=True, silent=True) or {}
    page_id = str(data.get("facebook_page_id") or "").strip()
    page_name = data.get("page_name") or f"طµظپط­ط© {page_id}"
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
        db.add_target(type="page", target_value=page_id, label=page_name, country=FIXED_COUNTRY, interval_hours=12.0)

    saved_count = 0
    for ad in ads:
        aid = str(ad.get("facebook_ad_id") or ad.get("id") or "").strip()
        if not aid:
            continue
        ad_dict = {
            "ad_id": aid,
            "page_id": page_id or str(ad.get("page_id") or ""),
            "page_name": page_name or ad.get("page_name") or "",
            "body": ad.get("ad_text") or "",
            "link_url": ad.get("landing_url") or "",
            "store_domain": ad.get("landing_domain") or "",
            "platform": ad.get("platform") or "",
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
    """Bulk sync saved ads from extension storage to server."""
    if not _ext_token_ok():
        return jsonify({"ok": False, "error": {"message": "unauthorized"}}), 401
    data = request.get_json(force=True, silent=True) or {}
    items = data.get("items") or []
    stored = 0
    for ad in items:
        aid = str(ad.get("facebook_ad_id") or ad.get("id") or "").strip()
        if not aid:
            continue
        ad_dict = {
            "ad_id": aid,
            "page_id": str(ad.get("page_id") or ""),
            "page_name": ad.get("page_name") or "",
            "body": ad.get("ad_text") or "",
            "link_url": ad.get("landing_url") or "",
            "store_domain": ad.get("landing_domain") or "",
            "platform": ad.get("platform") or "",
            "cta_text": ad.get("cta") or "",
            "start_date": ad.get("start_date") or "",
            "is_active": True,
        }
        if db.save_ad(ad_dict, query="sync_history", country=FIXED_COUNTRY):
            stored += 1
    return jsonify({"ok": True, "data": {"received": len(items), "stored": stored}})


@app.route("/api/v1/sync/pages", methods=["POST"])
def api_ext_sync_pages():
    """Bulk sync tracked pages from extension to targets table."""
    if not _ext_token_ok():
        return jsonify({"ok": False, "error": {"message": "unauthorized"}}), 401
    data = request.get_json(force=True, silent=True) or {}
    items = data.get("items") or []
    for page in items:
        pid = str(page.get("facebook_page_id") or "").strip()
        if pid:
            db.upsert_tracked_page(
                page_id=pid, page_name=page.get("page_name") or pid,
                page_url=page.get("page_url") or "",
                page_likes=int(page.get("page_likes") or 0),
                country=FIXED_COUNTRY, source="extension")
            db.add_target(type="page", target_value=pid, label=page.get("page_name") or pid, country=FIXED_COUNTRY)
    return jsonify({"ok": True, "data": {"synced": len(items)}})


# â”€â”€ Tracked pages (dashboard API) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
@app.route("/api/tracked-pages", methods=["GET"])
def api_tracked_pages():
    return jsonify({"pages": db.list_tracked_pages()})


@app.route("/api/tracked-pages/<page_id>/snapshots", methods=["GET"])
def api_tracked_page_snapshots(page_id):
    return jsonify({"page_id": page_id, "snapshots": db.get_page_snapshots(page_id, limit=200)})


@app.route("/api/tracked-pages/<page_id>", methods=["DELETE"])
def api_untrack_page(page_id):
    db.untrack_page(page_id)
    for t in [t for t in db.list_targets() if t.get("type") == "page" and str(t.get("target_value")) == str(page_id)]:
        db.delete_target(t["id"])
    return jsonify({"ok": True})


@app.route("/api/tracked-pages", methods=["DELETE"])
def api_untrack_all_pages():
    db.delete_all_tracked_pages()
    for t in [t for t in db.list_targets() if t.get("type") == "page"]:
        db.delete_target(t["id"])
    return jsonify({"ok": True})



@app.route("/api/export/<fmt>")
def api_export(fmt: str):
    ads = db.get_ads(limit=100000, stores_only=False)
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


def scheduler_loop() -> None:
    """Enqueue jobs for enabled targets based on their configured interval."""
    from datetime import timedelta
    while True:
        try:
            # Recover jobs abandoned by a crashed/stopped worker.
            n = db.requeue_stale_jobs(minutes=20)
            if n:
                manager.events.put({"type": "log", "data": {"text": f"â™»ï¸ڈ ط£ط¹ظٹط¯ {n} ط£ظ…ط± ط¹ط§ظ„ظ‚ ط¥ظ„ظ‰ ظ‚ط§ط¦ظ…ط© ط§ظ„ط§ظ†طھط¸ط§ط±."}})

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
                            incremental=True,  # smart early stopping
                        )
                        db.update_target_scraped(t["id"])
                        enqueued += 1

                if enqueued > 0:
                    db.set_setting("last_scheduled_at", now_utc.isoformat())
                    manager.events.put({
                        "type": "log",
                        "data": {"text": f"âڈ° ط¬ط¯ظˆظ„ط© ط°ظƒظٹط©: ط£ظڈط¶ظٹظپطھ {enqueued} ظ…ظ‡ظ…ط© ط³ط­ط¨ طھط²ط§ظٹط¯ظٹ ظ„ظ„ط£ظ‡ط¯ط§ظپ ط§ظ„ظ…ط³طھط­ظ‚ط©."}
                    })
        except Exception:
            logger.exception("scheduler error")
        time.sleep(60)


def job_worker_loop() -> None:
    """In-process job executor — replaces the separate Playwright worker.

    Claims pending jobs from the ``jobs`` table and runs them through the HTTP
    engine (no browser), updating progress/status in the DB.
    """
    worker_id = f"inproc-{os.getpid()}"
    while True:
        try:
            job = db.claim_next_job(worker_id)
            if not job:
                time.sleep(3)
                continue
            jid = job.get("id")
            logger.info("job %s: in-process scrape start", jid)
            db.update_job_progress(jid, 1, "بدء السحب عبر HTTP")
            started = manager.start(
                job.get("query") or "",
                bool(job.get("exact_phrase")),
                bool(job.get("stores_only")),
                job.get("sort_mode") or "total_impressions",
                False,
                job.get("url") or "",
                job.get("start_date_min") or "",
                job.get("start_date_max") or "",
                "",
            )
            if not started:
                # A manual scrape is already running; retry this job later.
                db._execute(f"UPDATE jobs SET status={db._ph()} WHERE id={db._ph()}", ("pending", jid))
                time.sleep(5)
                continue
            while manager.state.get("running"):
                if job.get("stop_requested"):
                    manager.stop()
                time.sleep(1)
            db.finish_job(jid, status="done", result_count=manager.state.get("scraped", 0))
            logger.info("job %s: done (%s ads)", jid, manager.state.get("scraped", 0))
        except Exception:  # noqa: BLE001
            logger.exception("job worker loop error")
            time.sleep(5)


_bg_started = False


def start_background_threads() -> None:
    """Start scheduler + in-process job executor exactly once."""
    global _bg_started
    if _bg_started:
        return
    _bg_started = True
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
    # When served by gunicorn/uwsgi (`app:app`), still start the in-process
    # scheduler + job executor.
    start_background_threads()
