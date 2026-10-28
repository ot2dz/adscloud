import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from typing import Optional

from db import db
from scrape_manager import manager, job_wakeup, DEFAULT_COUNTRY

logger = logging.getLogger("web_app")


def _queue_month_scrape(source: str, start: Optional[str] = None, end: Optional[str] = None):
    """Queue the whole-market scrape (default: 1st of this month -> today,
    Algeria time). Returns (job_id, queued_behind_another_job)."""
    from db import algeria_today
    today = algeria_today()
    start = start or today.replace(day=1).isoformat()
    end = end or today.isoformat()
    busy = bool(manager.state.get("running")) or db.has_pending_jobs()
    label = "كل إعلانات السوق (متاجر فقط)" if source == "manual" else "إعادة فحص اليوم والأمس"
    job_id = db.create_job(
        query=f"{label} {start} ← {end}", country=DEFAULT_COUNTRY, exact_phrase=True,
        stores_only=True, sort_mode="relevancy_monthly_grouped", source=source,
        start_date_min=start, start_date_max=end, date_scope="launch",
        mode=MONTH_MODE, languages=",".join(MONTH_LANGS))
    job_wakeup.set()
    manager._emit("log", {"text": f"🗓️ أُضيف سحب السوق يوماً بيوم ({start} ← {end})"
                                  + (" إلى قائمة الانتظار." if busy else ".")})
    return job_id, busy


def _queue_status_check(source: str):
    """Queue the "is it still running?" check. Returns (job_id, queued_behind)."""
    busy = bool(manager.state.get("running")) or db.has_pending_jobs()
    job_id = db.create_job(query="فحص حالة الإعلانات (نشط / متوقف)", country=DEFAULT_COUNTRY,
                           stores_only=True, source=source, mode=STATUS_MODE)
    job_wakeup.set()
    manager._emit("log", {"text": "🔎 أُضيف فحص حالة الإعلانات" + (" إلى قائمة الانتظار." if busy else ".")})
    return job_id, busy


def _status_check_due(settings: dict) -> bool:
    """Once per Algerian day, after the configured hour (default 03:00)."""
    from datetime import datetime as _dt
    from db import ALGERIA_TZ, algeria_today
    if settings.get("status_check_enabled", "true").lower() not in ("1", "true", "yes", "on"):
        return False
    try:
        hour = int(settings.get("status_check_hour", "3") or 3)
    except ValueError:
        hour = 3
    if _dt.now(ALGERIA_TZ).hour < hour:
        return False
    if settings.get("status_check_last", "") == algeria_today().isoformat():
        return False
    return not db.has_open_mode_job(STATUS_MODE)


def _month_rescan_due(settings: dict) -> bool:
    """Daily re-scan of today + yesterday: once per Algerian day, after the
    configured hour, unless a market scrape is already queued or running."""
    from datetime import datetime as _dt
    from db import ALGERIA_TZ, algeria_today
    if settings.get("month_rescan_enabled", "true").lower() not in ("1", "true", "yes", "on"):
        return False
    try:
        hour = int(settings.get("month_rescan_hour", "23") or 23)
    except ValueError:
        hour = 23
    if _dt.now(ALGERIA_TZ).hour < hour:
        return False
    if settings.get("month_rescan_last", "") == algeria_today().isoformat():
        return False
    return not db.has_open_mode_job(MONTH_MODE)


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
            if _month_rescan_due(s):
                y = (today_dz - timedelta(days=1)).isoformat()
                _queue_month_scrape("daily_rescan", start=y, end=today_dz.isoformat())
                db.set_setting("month_rescan_last", today_dz.isoformat())
            if _status_check_due(s):
                _queue_status_check("daily")
                db.set_setting("status_check_last", today_dz.isoformat())
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
            if job.get("stop_requested"):
                db.finish_job(jid, status="stopped", result_count=int(job.get("result_count") or 0))
                manager._emit("log", {"text": f"⏹ المهمة #{jid} أُلغيت قبل البدء."})
                continue
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
            manager._emit("log", {
                "text": f"▶ بدء المهمة #{jid}: {job.get('query') or job.get('url') or ''} "
                        f"(محاولة {attempts + 1})"})

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
                mode=job.get("mode") or "",
                languages=[x for x in (job.get("languages") or "").split(",") if x],
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

            # A stop request that arrived while the run was finishing wins.
            try:
                fresh = db.get_job(jid)
                if fresh and fresh.get("stop_requested") and status in ("interrupted", "done"):
                    status = "stopped"
            except Exception:
                pass

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
                manager._emit("log", {"text": f"⛔ فشلت المهمة #{jid}: {outcome.get('error', '')}"})
            elif status == "stopped":
                db.finish_job(jid, status="stopped", result_count=count)
                manager.state["error"] = ""
                manager._emit("log", {"text": f"⏹ أُوقفت المهمة #{jid}: {count} إعلان."})
            else:
                db.finish_job(jid, status="done", result_count=count)
                logger.info("job %s: done (%s ads)", jid, count)
                manager._emit("log", {"text": f"✅ انتهت المهمة #{jid}: {count} إعلان."})
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


class _SseLogHandler(logging.Handler):
    """Forward technical logs (rate limits, challenges, errors) to the live UI."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            text = self.format(record)
        except Exception:
            return
        item = {"type": "log", "data": {"text": text, "level": record.levelname}}
        for q in list(manager.subscribers):
            try:
                q.put_nowait(item)
            except Exception:
                pass


_sse_log_handler: Optional[logging.Handler] = None


def attach_sse_logging() -> None:
    """Attach the SSE log handler once (idempotent)."""
    global _sse_log_handler
    if _sse_log_handler is not None:
        return
    h = _SseLogHandler()
    h.setLevel(logging.INFO)
    h.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%H:%M:%S"))
    for name in ("meta_ads_collector", "engine", "web_app"):
        lg = logging.getLogger(name)
        lg.addHandler(h)
        if lg.level == logging.NOTSET or lg.level > logging.INFO:
            lg.setLevel(logging.INFO)
    _sse_log_handler = h


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
        attach_sse_logging()
    except Exception:
        logger.exception("SSE log handler failed")
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

