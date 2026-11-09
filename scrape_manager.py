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

        def on_ad(ad):
            if self.stop_event.is_set():
                return
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
                # Whole market for a date range, one launch day x language at
                # a time (Meta's '" "' exact-phrase wildcard = every ad).
                from slicing import build_day_passes
                passes = build_day_passes(start_date_min, start_date_max, month_languages or MONTH_LANGS)
                total_passes = len(passes)
                progress(f"🗓️ سحب السوق يوماً بيوم: {start_date_min} ← {start_date_max} "
                         f"({total_passes} شريحة = يوم × لغة، الأحدث أولاً، المتاجر فقط).")
                start_pass = max(1, int(resume.get("pass") or 1))
                outcome["slices_total"] = total_passes
                # Slices that reached Meta's end, carried in the checkpoint
                # across resumes (older checkpoints: assume the earlier ones did).
                outcome["slices_ok"] = int(resume.get("ok", start_pass - 1) or 0)
                if start_pass > 1:
                    progress(f"↩️ استئناف من الشريحة {start_pass}/{total_passes}.")
                for idx, p in enumerate(passes, 1):
                    if idx < start_pass:
                        continue
                    if self.stop_event.is_set():
                        outcome["status"] = "stopped"
                        progress("⏹ تم إيقاف السحب بطلب من المستخدم.")
                        break
                    progress(f"↪ شريحة {idx}/{total_passes}: إعلانات {p['label']} — إجمالي مجمع: "
                             f"{(base_count + len(seen_ad_ids))} إعلان.")
                    self._emit("slice_update", {"pass": idx, "total_passes": total_passes, "label": p["label"],
                                                "accumulated": (base_count + len(seen_ad_ids))})
                    r = scrape(MARKET_WILDCARD, country=country, exact_phrase=True,
                               sort_mode="relevancy_monthly_grouped",
                               start_date_min=p["day"], start_date_max=p["day"], date_scope="launch",
                               content_languages=[p["lang"]] if p["lang"] else None, estimate=False,
                               progress=progress, on_ad=on_ad, on_scanned=on_scanned,
                               should_stop=lambda: self.stop_event.is_set(), proxy=proxy_spec,
                               rotate_url=rot_url, rotate_token=rot_token,
                               resume_cursor=(resume.get("cursor") if idx == start_pass else None),
                               on_checkpoint=make_checkpointer(idx))
                    if r.failed:
                        outcome["scanned"] += int(r.scanned or 0)
                        progress(f"⚠️ تجاوز الشريحة {idx} بسبب خطأ دائم: {r.error}")
                        make_checkpointer(idx)(None)
                        continue
                    if not absorb(r):
                        break
                    if r.completed:
                        outcome["slices_ok"] += 1
                        make_checkpointer(idx)(None)  # persist the new "ok" count
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
