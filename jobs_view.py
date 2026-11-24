# -*- coding: utf-8 -*-
"""What the jobs section shows: the jobs running now, the queue, and a history
split by kind so page expansion never hides the user's own scans and checks.

Every finished job gets one honest verdict: a job that "ended" may still have
been cut short or have brought nothing.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

PAGE = 30
# Scans and checks the user started (the rest starts by itself).
USER_SOURCES = ("manual", "manual_target", "new_competitor", "manual_global", "manual_global_all",
                "manual_all", "schedule_manual")
CHECKS = "('status_check', 'winning_check')"
MODE = "COALESCE(mode, '')"
_USER = "COALESCE(source, '') IN ({})".format(", ".join(f"'{s}'" for s in USER_SOURCES))
GROUP_SQL = {
    "mine": f"{MODE} <> 'page_expand' AND {_USER}",   # incl. the checks they started
    "checks": f"{MODE} IN {CHECKS}",
    "auto": f"{MODE} NOT IN ('page_expand', 'status_check', 'winning_check') AND NOT ({_USER})",
    "expand": f"{MODE} = 'page_expand'",
}
VERDICTS = ("ok", "partial", "empty", "failed", "stopped")
# Below this share of Meta's estimate, a run that Meta "finished" most likely
# hit Meta's pagination limit rather than the real end of the results.
COVERAGE_OK = 0.9
VERDICT_SQL = f"""CASE
  WHEN status = 'failed' THEN 'failed'
  WHEN status = 'stopped' THEN 'stopped'
  WHEN {MODE} IN ('', 'month_days') AND COALESCE(result_count, 0) = 0 THEN 'empty'
  WHEN completed = 0 THEN 'partial'
  WHEN {MODE} = '' AND reported_count > 0
       AND COALESCE(scanned_count, 0) < reported_count * {COVERAGE_OK} THEN 'partial'
  ELSE 'ok' END"""
FINISHED = "status IN ('done', 'failed', 'stopped')"
OPEN = "status IN ('pending', 'running', 'paused')"
WHEN = "COALESCE(finished_at, created_at)"
DZ = timezone(timedelta(hours=1))


def _page_of(job: dict) -> str:
    m = re.search(r"view_all_page_id=(\d+)", job.get("url") or "")
    return m.group(1) if m else ""


def decorate(db, jobs: list[dict]) -> list[dict]:
    """Add a readable title and kind; drop the bulky fields."""
    p = db._ph()
    labels, names = {}, {}
    tids = sorted({j["target_id"] for j in jobs if j.get("target_id")})
    if tids:
        for t in db._query(f"SELECT id, label, target_value FROM targets WHERE id IN ({', '.join([p] * len(tids))})",
                           tuple(tids)):
            labels[t["id"]] = t.get("label") or t.get("target_value") or ""
    pids = sorted({x for x in map(_page_of, jobs) if x})
    if pids:
        for r in db._query(f"SELECT page_id, MAX(page_name) AS n FROM meta_ads WHERE page_id IN "
                           f"({', '.join([p] * len(pids))}) GROUP BY page_id", tuple(pids)):
            names[str(r["page_id"])] = r.get("n") or ""
    return [_view(j, labels, names) for j in jobs]


def _view(j: dict, labels: dict, names: dict) -> dict:
    mode, pid = j.get("mode") or "", _page_of(j)
    q = (j.get("query") or "").strip()
    if mode == "month_days":
        kind, title = "market", q
    elif mode in ("status_check", "winning_check"):
        kind, title = "check", q
    elif mode == "page_expand":
        kind, title = "expand", "توسيع الصفحات"
    elif pid:
        kind = "page"
        title = labels.get(j.get("target_id")) or ""
        if not title or title == pid:
            title = names.get(pid) or f"صفحة {pid}"
    else:
        kind = "keyword" if q else "link"
        if re.fullmatch(r"[a-z0-9-]+(\.[a-z0-9-]+)+", q.lower()):
            kind = "store"
        title = labels.get(j.get("target_id")) or q or "رابط مكتبة إعلانات"
    try:
        summary = json.loads(j.get("summary") or "null")
    except ValueError:
        summary = None
    keep = ("id", "status", "source", "mode", "created_at", "finished_at", "result_count", "error", "retry_at",
            "attempts", "pause_requested", "paused_reason", "stop_requested", "reported_count", "scanned_count",
            "completed", "slices_total", "slices_ok", "start_date_min", "start_date_max", "verdict")
    out = {k: j.get(k) for k in keep}
    out.update(kind=kind, title=title, summary=summary, url=j.get("url") or "", market=j.get("market") or "dz",
               started_at=j.get("first_started_at") or j.get("started_at"),
               by_user=(j.get("source") or "") in USER_SOURCES, resumable=bool(j.get("checkpoint")))
    return out


def open_jobs(db) -> tuple[list, list]:
    """(running, waiting in the order they will run; paused by hand last)."""
    rows = decorate(db, db._query(f"SELECT * FROM jobs WHERE {OPEN} ORDER BY COALESCE(priority, 0) DESC, id"))
    running = [r for r in rows if r["status"] == "running"]
    waiting = [r for r in rows if r["status"] == "pending"] + [r for r in rows if r["status"] == "paused"]
    return running, waiting


def history(db, group: str = "mine", verdict: str = "", q: str = "", offset: int = 0, limit: int = PAGE) -> dict:
    p = db._ph()
    group = group if group in GROUP_SQL else "mine"
    where, params = f"{FINISHED} AND {GROUP_SQL[group]}", []
    if q.strip():
        like = f"%{q.strip().lower()}%"
        where += (f" AND (LOWER(COALESCE(query, '')) LIKE {p} OR LOWER(COALESCE(url, '')) LIKE {p} "
                  f"OR target_id IN (SELECT id FROM targets WHERE LOWER(COALESCE(label, '')) LIKE {p}))")
        params += [like] * 3
    counts = {r["verdict"]: int(r["n"]) for r in db._query(
        f"SELECT verdict, COUNT(*) AS n FROM (SELECT {VERDICT_SQL} AS verdict FROM jobs WHERE {where}) t "
        f"GROUP BY verdict", tuple(params))}
    total = sum(counts.values())
    if verdict in VERDICTS:
        where += f" AND ({VERDICT_SQL}) = {p}"
        params.append(verdict)
        total = counts.get(verdict, 0)
    rows = db._query(f"SELECT *, {VERDICT_SQL} AS verdict FROM jobs WHERE {where} "
                     f"ORDER BY {WHEN} DESC, id DESC LIMIT {p} OFFSET {p}",
                     tuple(params) + (max(1, min(200, int(limit))), max(0, int(offset))))
    return {"group": group, "jobs": decorate(db, rows), "counts": counts, "total": total}


def group_badges(db) -> dict:
    """Per group: finished jobs, and the failures of the last 48 hours."""
    since = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
    p = db._ph()
    cols = ", ".join(f"SUM(CASE WHEN {sql} THEN 1 ELSE 0 END) AS n_{g}, "
                     f"SUM(CASE WHEN {sql} AND status = 'failed' AND {WHEN} >= {p} THEN 1 ELSE 0 END) AS f_{g}"
                     for g, sql in GROUP_SQL.items())
    row = db._query_one(f"SELECT {cols} FROM jobs WHERE {FINISHED}", (since,) * len(GROUP_SQL)) or {}
    return {g: {"total": int(row.get(f"n_{g}") or 0), "failed": int(row.get(f"f_{g}") or 0)} for g in GROUP_SQL}


def expand_days(db, days: int = 14) -> list[dict]:
    """Page expansion as one line per Algerian day."""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    out: dict = {}
    for r in db._query(f"SELECT status, slices_ok, result_count, {WHEN} AS ended FROM jobs WHERE {FINISHED} "
                       f"AND {GROUP_SQL['expand']} AND {WHEN} >= {db._ph()}", (since,)):
        try:
            day = datetime.fromisoformat(r["ended"]).astimezone(DZ).date().isoformat()
        except (TypeError, ValueError):
            continue
        d = out.setdefault(day, {"day": day, "runs": 0, "pages": 0, "ads": 0, "failed": 0})
        d["runs"] += 1
        d["pages"] += int(r["slices_ok"] or 0)
        d["ads"] += int(r["result_count"] or 0)
        d["failed"] += int(r["status"] == "failed")
    return sorted(out.values(), key=lambda d: d["day"], reverse=True)


def live(*managers) -> dict:
    """{job id: what its run says right now} (from memory, not the DB)."""
    out = {}
    for m in managers:
        st = m.state
        if st.get("running") and st.get("job_id"):
            log = st.get("log") or []
            out[str(st["job_id"])] = {"scraped": st.get("scraped", 0), "scanned": st.get("scanned", 0),
                                      "reported": st.get("reported"), "steps": st.get("steps"),
                                      "last": str(log[-1]) if log else "", "line": m.check_line}
    return out


def _retry_source(job: dict) -> str:
    if job.get("mode") == "page_expand":
        return "manual_expand"
    if job.get("target_id") and not job.get("mode"):
        return "manual_global" if job.get("market") == "global" else "manual_target"
    return "manual"


def retry(db, job_id: int) -> tuple[Optional[int], str, bool]:
    """Run a finished job again: a failed one that kept its position carries on
    from it, any other starts over as a new job. (job id, error, resumed)."""
    j = db.get_job(job_id)
    if not j or j.get("status") not in ("done", "failed", "stopped"):
        return None, "المهمة لم تنتهِ بعد أو غير موجودة.", False
    mode = j.get("mode") or ""
    if mode in ("status_check", "winning_check", "page_expand") and db.has_open_mode_job(mode):
        return None, "مهمة من نفس النوع تعمل الآن أو في الانتظار.", False
    if db.has_open_job(j.get("target_id")):
        return None, "هذا الهدف له مهمة تعمل الآن أو في الانتظار.", False
    if j["status"] == "failed" and j.get("checkpoint"):
        db.reopen_job(job_id)
        return job_id, "", True
    new = db.create_job(
        query=j.get("query") or "", url=j.get("url") or "", country=j.get("country") or "DZ",
        exact_phrase=bool(j.get("exact_phrase")), stores_only=bool(j.get("stores_only")),
        sort_mode=j.get("sort_mode") or "total_impressions", source=_retry_source(j),
        target_id=j.get("target_id"), incremental=bool(j.get("incremental")),
        start_date_min=j.get("start_date_min") or "", start_date_max=j.get("start_date_max") or "",
        date_scope=j.get("date_scope") or "impressions", mode=mode, languages=j.get("languages") or "",
        market=j.get("market") or "dz")
    return new, "", False
