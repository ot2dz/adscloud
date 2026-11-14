import json
import logging
import os
import queue
import threading
import time
from datetime import datetime, timezone
from typing import Callable, Optional

from db import db
from engine import scrape
from slicing import clean_scan_label
from store_filter import product_key

logger = logging.getLogger("web_app")


DEFAULT_COUNTRY = os.environ.get("DEFAULT_COUNTRY", "DZ").strip().upper() or "DZ"
FIXED_COUNTRY = DEFAULT_COUNTRY
FIXED_LANGS = ["ar"]

# Local scraping needs Chromium browser / worker or HTTP collector.
# Whole-market monthly scrape (see slicing.build_day_passes).
MONTH_MODE = "month_days"
MONTH_LANGS = ["ar"]      # Arabic-language ads only
MARKET_WILDCARD = '" "'   # Meta exact-phrase wildcard: matches every ad
STATUS_MODE = "status_check"  # daily "is it still running?" check (status_check.py)
WINNING_MODE = "winning_check"  # every 12 h: pages of the saved winning products

SCRAPE_ENABLED = os.environ.get("SCRAPE_ENABLED", "true").strip().lower() in ("1", "true", "yes", "on")

def _proxy_setting() -> str:
    """Proxy saved from Settings (empty -> engine falls back to META_ADS_PROXY)."""
    try:
        return (db.get_settings().get("meta_ads_proxy") or "").strip()
    except Exception:
        return ""

def _status_egress() -> tuple:
    """(proxy, rotate_url, rotate_token) of the status check.

    Without a dedicated ``status_check_proxy`` the check shares the scrape
    egress, exactly as before. With one, ``rotate_url`` is never None: an empty
    value means "no rotator", so a block on that egress cannot rotate the
    scrape line.
    """
    try:
        s = db.get_settings()
    except Exception:
        return "", None, None
    dedicated = (s.get("status_check_proxy") or "").strip()
    if not dedicated:
        return (s.get("meta_ads_proxy") or "").strip(), None, None
    return (dedicated, (s.get("status_check_rotate_url") or "").strip(),
            (os.environ.get("STATUS_CHECK_ROTATE_TOKEN") or "").strip() or None)


LANE_SOURCES = ("scrape", "status", "custom")


def scrape_lanes(settings: Optional[dict] = None) -> list[dict]:
    """Egress lanes of a whole-market scan, from the ``scrape_lanes`` setting.

    Each lane = {"name", "proxy", "rotate_url", "rotate_token", "source"}:
      source "scrape" -> the saved scrape proxy and its default rotator
      source "status" -> the status-check line (shop tablet); while a market
                         scan runs no status check runs (one job at a time)
      source "custom" -> any other proxy, with its own rotator URL and the
                         rotator token read from the env var ``token_env``
    No setting / nothing usable = one lane on the scrape proxy (as before).
    ``rotate_url=None`` means "the default rotator" (IP_ROTATE_URL); "" = none.
    """
    if settings is None:
        try:
            settings = db.get_settings()
        except Exception:
            settings = {}
    try:
        raw = json.loads(settings.get("scrape_lanes") or "[]")
    except (TypeError, ValueError):
        raw = []
    out, seen = [], set()
    for i, ln in enumerate(raw if isinstance(raw, list) else []):
        if not isinstance(ln, dict) or ln.get("enabled") is False:
            continue
        src = ln.get("source") if ln.get("source") in LANE_SOURCES else "custom"
        name = str(ln.get("name") or "").strip()[:40] or f"خط {i + 1}"
        if src == "scrape":
            lane = {"proxy": (settings.get("meta_ads_proxy") or "").strip(), "rotate_url": None, "rotate_token": None}
        elif src == "status":
            dedicated = (settings.get("status_check_proxy") or "").strip()
            if not dedicated:
                continue  # no separate status line: it would just repeat the scrape line
            lane = {"proxy": dedicated, "rotate_url": (settings.get("status_check_rotate_url") or "").strip(),
                    "rotate_token": (os.environ.get("STATUS_CHECK_ROTATE_TOKEN") or "").strip() or None}
        else:
            proxy = str(ln.get("proxy") or "").strip()
            if not proxy:
                continue
            env = str(ln.get("token_env") or "").strip()
            lane = {"proxy": proxy, "rotate_url": str(ln.get("rotate_url") or "").strip(),
                    "rotate_token": ((os.environ.get(env) or "").strip() or None) if env else None}
        key = lane["proxy"] or "<default>"
        if key in seen:
            continue
        seen.add(key)
        out.append(dict(lane, name=name, source=src))
    if not out:
        out = [{"name": "خط السحب", "source": "scrape", "proxy": (settings.get("meta_ads_proxy") or "").strip(),
                "rotate_url": None, "rotate_token": None}]
    return out


def _month_resume(resume: dict) -> tuple[set, dict, Optional[int]]:
    """(done pass numbers, {pass: cursor}, slices ok) from a checkpoint.

    v2 (lanes): {"v": 2, "done": [...], "cursors": {"n": cursor}, "ok": k}
    v1 (one lane): {"pass": n, "cursor": c, "ok": k} = passes before n done."""
    if not resume:
        return set(), {}, 0
    if resume.get("v") == 2:
        done = {int(x) for x in resume.get("done") or []}
        cursors = {int(k): v for k, v in (resume.get("cursors") or {}).items() if v}
        return done, cursors, int(resume.get("ok") or 0)
    start = max(1, int(resume.get("pass") or 1))
    cursors = {start: resume["cursor"]} if resume.get("cursor") else {}
    return set(range(1, start)), cursors, int(resume.get("ok", start - 1) or 0)


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
        # Keep the legacy queue bounded: /api/stream now uses subscribers.
        if self.events.qsize() < 1000:
            self.events.put(item)
        # Fan-out to every connected SSE client so no open tab misses events.
        for q in list(self.subscribers):
            try:
                q.put_nowait(item)
            except Exception:
                pass

    def start(self, query: str, country: str = DEFAULT_COUNTRY, exact: bool = False,
              stores_only: bool = False, sort_mode: str = "total_impressions",
              url: str = "", start_date_min: str = "",
              start_date_max: str = "", date_label: str = "",
              date_scope: str = "impressions", resume: Optional[dict] = None,
              on_checkpoint: Optional[Callable[[Optional[dict]], None]] = None,
              job_id: Optional[int] = None, base_count: int = 0,
              mode: str = "", languages: Optional[list] = None,
              market: str = "dz", egress: str = "scrape",
              base_reported: Optional[int] = None) -> bool:
        """Start a scrape thread.

        ``resume`` is a checkpoint ``{"pass": n, "cursor": "..."}`` from an
        earlier interrupted run; ``on_checkpoint`` receives the new one after
        every fully-processed page (``None`` once everything is done).
        ``base_count`` = ads already collected by earlier runs of the same
        job, so counters continue instead of restarting at zero.
        ``base_reported`` = Meta's estimate saved by the first run (a resumed
        run does not ask again), so the live bar keeps its total.
        """
        if self.state["running"]:
            return False
        self.stop_event.clear()
        while not self.events.empty():
            self.events.get_nowait()
        self.state.update({"running": True, "query": query or url, "scraped": base_count,
                           "stored": 0, "stores": 0, "scanned": 0, "reported": base_reported,
                           "error": "", "log": [], "outcome": None, "job_id": job_id})
        self.thread = threading.Thread(
            target=self._run,
            args=(query, country, exact, stores_only, sort_mode, url, start_date_min, start_date_max, date_label, date_scope),
            kwargs={"resume": resume, "on_checkpoint": on_checkpoint, "base_count": base_count,
                    "mode": mode, "month_languages": languages,
                    "market": "global" if str(market).lower() == "global" else "dz",
                    "egress": egress if egress in ("scrape", "status") else "scrape"},
            daemon=True)
        self.thread.start()
        return True

    def stop(self) -> None:
        self.stop_event.set()

    def _run(self, query, country, exact, stores_only, sort_mode, url, start_date_min, start_date_max, date_label, date_scope="impressions",
             resume: Optional[dict] = None, on_checkpoint=None, base_count: int = 0,
             mode: str = "", month_languages: Optional[list] = None,
             market: str = "dz", egress: str = "scrape") -> None:
        country = (country or DEFAULT_COUNTRY).strip().upper()
        market = "global" if str(market).lower() == "global" else "dz"
        # The foreign side tab keeps every language; the Algerian bank is Arabic.
        languages = [] if market == "global" else FIXED_LANGS
        # Egress the scrape goes out from: the saved scrape proxy by default,
        # or the dedicated status-check line (a competitor scan may be routed
        # there so its heavy checking never burns the scrape IP).
        if egress == "status":
            proxy_spec, check_rot_url, check_rot_token = _status_egress()
            rot_url, rot_token = check_rot_url, check_rot_token
        else:
            proxy_spec = _proxy_setting()
            rot_url = rot_token = None
        seen_stores: set = set()
        seen_ad_ids: set = set()
        seen_collations: set = set()
        seen_product_keys: set = set()
        resume = resume if isinstance(resume, dict) else {}
        # Outcome read by the job runner: done | stopped | interrupted | failed.
        # Start from the incoming checkpoint so an immediate failure keeps it.
        # "completed": True = Meta said there are no more pages (whole market:
        # every slice did), False = the run ended before that, None = not
        # tracked (status check). Market scans add slices_total / slices_ok.
        outcome = {"status": "done", "checkpoint": resume or None, "error": "", "scanned": 0, "ad_ids": [],
                   "completed": None}
        job_id = self.state.get("job_id")

        # Whole-market scans embed their date window in the job label; the ad
        # tag stays clean so one ad carries "كل إعلانات السوق" instead of one
        # variant per scanned window.
        tag = clean_scan_label(query) if mode == MONTH_MODE else query

        def make_checkpointer(pass_idx: int):
            def _cp(cursor):
                # Cursor None = this pass is exhausted -> next pass from scratch.
                cp = {"pass": pass_idx, "cursor": cursor} if cursor else {"pass": pass_idx + 1, "cursor": None}
                if "slices_ok" in outcome:
                    cp["ok"] = outcome["slices_ok"]
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
                self.state["error"] = result.error or "فشل السحب"
            elif result.interrupted:
                outcome.update(status="interrupted", error=result.error)
            else:
                return True
            return False

        def progress(msg):
            print(f"  ▶ [SCRAPER] {msg}", flush=True)
            self.state["log"].append(msg)
            self.state["log"] = self.state["log"][-500:]
            self._emit("log", {"text": msg})

        def on_reported(count):
            if count is not None:
                self.state["reported"] = count
                self._emit("reported", {"reported": count, "query": query or url})
                # Saved at once: an interrupted run resumes without re-asking.
                if job_id:
                    try:
                        db.set_job_reported(int(job_id), count)
                    except Exception:
                        logger.exception("saving reported count failed")

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

        ad_lock = threading.Lock()  # parallel lanes share the counters and seen-sets

        def on_ad(ad):
            if self.stop_event.is_set():
                return
            with ad_lock:
                _on_ad(ad)

        def _on_ad(ad):
            d = ad.to_dict()
            d["keywords"] = [tag] if tag else []
            d["market"] = market
            # Big marketplaces are purged from the bank — never ingest them.
            if ad.store_domain and db.is_blocked_domain(ad.store_domain):
                return
            # Drama apps / foreign sites / hidden CTAs: not collected or shown.
            if db.is_unwanted_ad(d):
                return
            # Identity of what Meta returned now: lets a re-scan tell which
            # previously active ads are gone (see reconcile_target_ads).
            if ad.collation_id:
                seen_collations.add(str(ad.collation_id))
            pk = product_key(str(ad.page_id or ""), ad.link_url or "",
                             ad.body or "", ad.store_domain or "")
            if pk:
                seen_product_keys.add(pk)
            aid = d.get("ad_id")
            if aid and aid not in seen_ad_ids:
                seen_ad_ids.add(aid)
                stored = db.save_ad(d, query=tag, country=country, market=market)
                now_iso = datetime.now(timezone.utc).isoformat()
                d["collected_at"] = now_iso
                if stored:
                    d["first_seen"] = now_iso
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

            if mode in (STATUS_MODE, WINNING_MODE):
                # Same safe page-batch check; the winning-products variant
                # covers the saved products' pages (stopped ones too) and
                # records ads that come back (relaunch / resume).
                import status_check
                winning = mode == WINNING_MODE
                st = {}

                def save_new(a):
                    if self.stop_event.is_set():
                        return False
                    d = a.to_dict()
                    d["keywords"] = []
                    d["market"] = market
                    stored = db.save_ad(d, query="", country=country, market=market)
                    if stored:
                        seen_ad_ids.add(a.ad_id)
                        self.state["stored"] += 1
                    return stored

                def status_cp(next_batch):
                    outcome["checkpoint"] = {"pass": next_batch, "cursor": None}
                    self.state["scraped"] = st.get("confirmed", 0) + st.get("stopped", 0)
                    if on_checkpoint:
                        on_checkpoint(outcome["checkpoint"])

                check_proxy, rotate_url, rotate_token = _status_egress()
                try:
                    outcome["status"] = status_check.run(
                        db, progress=progress, should_stop=lambda: self.stop_event.is_set(),
                        proxy=check_proxy, start_batch=max(1, int(resume.get("pass") or 1)),
                        on_checkpoint=status_cp, on_new_ad=save_new, stats=st,
                        rotate_url=rotate_url, rotate_token=rotate_token,
                        pages=db.winning_pages() if winning else None, watch_returns=winning)
                except status_check.Blocked as exc:
                    outcome.update(status="interrupted", error=f"Meta لا تُرجع نتائج (حجب مؤقت؟) — لم يُعلَّم أي إعلان: {exc}")
                    progress("⏸ " + outcome["error"])
                self.state["scraped"] = st.get("confirmed", 0) + st.get("stopped", 0)
                st["finished_at"] = datetime.now(timezone.utc).isoformat()
                st["status"] = outcome["status"]
                db.set_setting("winning_check_summary" if winning else "status_check_summary",
                               json.dumps(st, ensure_ascii=False))
                progress(f"🏁 {'فحص المنتجات الرابحة' if winning else 'فحص الحالة'}: "
                         f"{st.get('confirmed', 0)} نشط مؤكد، {st.get('stopped', 0)} توقف، "
                         f"{st.get('new', 0)} إعلان جديد، {st.get('requests', 0)} طلب.")

            elif mode == MONTH_MODE:
                # Whole market for a date range, one launch day x language per
                # pass (Meta's '" "' exact-phrase wildcard = every ad). Several
                # egress lanes work in parallel: each takes the next pass not
                # done yet, so a lane busy rotating its IP never holds the others.
                from engine import PACE, REQUEST_DELAY, Pace
                from slicing import build_day_passes
                passes = build_day_passes(start_date_min, start_date_max, month_languages or MONTH_LANGS)
                total_passes = len(passes)
                lanes = scrape_lanes() if egress == "scrape" else [
                    {"name": "خط الفحص", "source": "status", "proxy": proxy_spec,
                     "rotate_url": rot_url, "rotate_token": rot_token}]
                done, cursors, ok0 = _month_resume(resume)
                outcome["slices_total"] = total_passes
                outcome["slices_ok"] = ok0
                todo = [i for i in range(1, total_passes + 1) if i not in done]  # newest day first
                lock = threading.Lock()
                lane_errors: list = []
                self.state["lanes"] = {ln["name"]: {"pass": None, "label": "", "status": "waiting"} for ln in lanes}
                progress(f"🗓️ سحب السوق يوماً بيوم: {start_date_min} ← {start_date_max} "
                         f"({total_passes} شريحة = يوم × لغة، الأحدث أولاً، المتاجر فقط) — "
                         f"{len(lanes)} خط سحب: {'، '.join(ln['name'] for ln in lanes)}.")
                if done:
                    progress(f"↩️ استئناف: {len(done)}/{total_passes} شريحة منجزة سابقاً.")

                def persist_month():
                    cp = {"v": 2, "done": sorted(done), "ok": outcome["slices_ok"],
                          "cursors": {str(k): v for k, v in cursors.items() if v}}
                    outcome["checkpoint"] = cp
                    if on_checkpoint:
                        try:
                            on_checkpoint(cp)
                        except Exception:
                            logger.exception("checkpoint persist failed")

                def lane_state(name, **kw):
                    if "pass_" in kw:
                        kw["pass"] = kw.pop("pass_")
                    self.state["lanes"][name].update(kw)
                    self._emit("lanes", {"lanes": self.state["lanes"]})

                def run_lane(lane, pace):
                    name = lane["name"]

                    def lane_progress(msg, _n=name):
                        progress(f"[{_n}] {msg}" if len(lanes) > 1 else msg)

                    while not self.stop_event.is_set():
                        with lock:
                            idx = todo.pop(0) if todo else None
                        if idx is None:
                            lane_state(name, status="finished", pass_=None)
                            return
                        p = passes[idx - 1]
                        lane_state(name, status="running", pass_=idx, label=p["label"])
                        lane_progress(f"↪ شريحة {idx}/{total_passes}: إعلانات {p['label']} — إجمالي مجمع: "
                                      f"{(base_count + len(seen_ad_ids))} إعلان.")
                        self._emit("slice_update", {"pass": idx, "total_passes": total_passes, "label": p["label"],
                                                    "lane": name, "accumulated": (base_count + len(seen_ad_ids))})

                        def cp_fn(cursor, _i=idx):
                            with lock:
                                if cursor:
                                    cursors[_i] = cursor
                                persist_month()

                        r = scrape(MARKET_WILDCARD, country=country, exact_phrase=True,
                                   sort_mode="relevancy_monthly_grouped",
                                   start_date_min=p["day"], start_date_max=p["day"], date_scope="launch",
                                   content_languages=[p["lang"]] if p["lang"] else None, estimate=False,
                                   progress=lane_progress, on_ad=on_ad, on_scanned=on_scanned,
                                   should_stop=lambda: self.stop_event.is_set(), proxy=lane["proxy"],
                                   rotate_url=lane["rotate_url"], rotate_token=lane["rotate_token"], pace=pace,
                                   resume_cursor=cursors.get(idx), on_checkpoint=cp_fn)
                        with lock:
                            outcome["scanned"] += int(r.scanned or 0)
                            if r.stopped or self.stop_event.is_set():
                                todo.insert(0, idx)  # resumes from its cursor next run
                                persist_month()
                                return
                            if r.interrupted:
                                # This line is blocked / its proxy is down: give the
                                # pass back to the other lanes and leave.
                                todo.insert(0, idx)
                                lane_errors.append(f"{name}: {r.error}")
                                persist_month()
                        if r.interrupted:
                            lane_progress(f"⏸ هذا الخط توقف ({r.error}) — تكمل الخطوط الأخرى.")
                            lane_state(name, status="paused", pass_=None)
                            return
                        with lock:
                            done.add(idx)
                            cursors.pop(idx, None)
                            if r.completed:
                                outcome["slices_ok"] += 1
                            persist_month()
                        if r.failed:
                            lane_progress(f"⚠️ تجاوز الشريحة {idx} بسبب خطأ دائم: {r.error}")
                    lane_state(name, status="stopped", pass_=None)

                threads = []
                for i, lane in enumerate(lanes):
                    # The first lane keeps the process pace (as before); others get their own.
                    t = threading.Thread(target=run_lane, args=(lane, PACE if i == 0 else Pace(REQUEST_DELAY)),
                                         name=f"lane-{i}", daemon=True)
                    t.start()
                    threads.append(t)
                for t in threads:
                    t.join()

                if self.stop_event.is_set():
                    outcome["status"] = "stopped"
                    progress("⏹ تم إيقاف السحب بطلب من المستخدم.")
                elif todo:
                    # Every lane left with passes still to do: the job queue retries later.
                    outcome.update(status="interrupted", error=" | ".join(lane_errors)[:500]
                                   or "توقفت كل خطوط السحب")
                ok, total = outcome["slices_ok"], total_passes
                outcome["completed"] = ok >= total
                if outcome["status"] == "done":
                    progress(f"🧩 الشرائح المكتملة: {ok}/{total}"
                             + ("" if ok >= total else f" — {total - ok} شريحة لم تكتمل"))

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
                                rotate_url=rot_url, rotate_token=rot_token,
                                resume_cursor=resume.get("cursor"),
                                on_checkpoint=make_checkpointer(1))
                if result.reported_count is not None:
                    self.state["reported"] = result.reported_count
                outcome["completed"] = bool(result.completed)
                absorb(result)

            # Never finish silently on an empty run: Meta answers HTTP 200 with
            # zero results when the egress IP is soft-blocked, which looks like
            # "success" but means nothing was collected.
            if ((base_count + len(seen_ad_ids)) == 0 and mode not in (STATUS_MODE, WINNING_MODE)
                    # "interrupted" (proxy down / transient error) must stay
                    # interrupted so the job queue retries it, not end as "empty".
                    and outcome.get("status") not in ("stopped", "failed", "interrupted")
                    and not self.stop_event.is_set()):
                reported = self.state.get("reported")
                if reported is None or reported == 0:
                    if url and not (query or "").strip():
                        msg = ("بحث فارغ (بدون كلمة): Meta أعادت 0 نتيجة. قد لا تدعم مكتبة الإعلانات "
                               "البحث بدون كلمة، أو أن الـIP محجوب. جرّب كلمة/صفحة أو «🩺 تشخيص السحب».")
                    else:
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
            outcome["seen_collations"] = sorted(seen_collations)
            outcome["seen_product_keys"] = sorted(seen_product_keys)
            self.state["outcome"] = outcome
            self.state["running"] = False
            self._emit("done", {})


manager = ScrapeManager()
# Set when a job is queued so the runner claims it at once (no polling delay).
job_wakeup = threading.Event()
