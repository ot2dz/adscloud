# -*- coding: utf-8 -*-
"""Daily status check: which ads in the bank are still running on Meta.

Meta's Ad Library only lists ads that run *now*. Asking for every running ad
of 50 pages at once (one paginated search, ~10 ads per request) and comparing
with the ads we hold as "active" tells which ones stopped, at a fraction of
the cost of one request per ad.

Safety rules, because Meta answers a soft-blocked IP with empty results
(HTTP 200, zero ads) and a wrong "stopped" is worse than a late one:

1. Canary: before each batch, two pages known to run ads must still return ads;
   if not, the run pauses (interrupted) and nothing is marked.
2. Only a batch whose search reached Meta's last result without any error is
   used; an incomplete one is retried, then skipped.
3. A batch claiming that more than half of its (>= 10) ads stopped at once is
   treated as suspicious and not applied.
4. Ads missing from a batch are checked a second time a little later; only
   ads missing twice are marked stopped.

An ad counts as running when Meta returns it, another copy of the same
collation group (Meta shows one copy per group), or another ad of the same
page for the same product (product_key).
"""
from __future__ import annotations

import logging
import time
from typing import Callable, Optional

from engine import active_ads_of_pages, rotate_and_wait_ready
import ip_rotator
from store_filter import product_key

logger = logging.getLogger("status_check")

BATCH_PAGES = 50          # Meta refuses more page ids per request
SUSPICIOUS_RATIO = 0.5    # rule 3
SUSPICIOUS_MIN_ADS = 10
RECHECK_WAIT_SEC = 20     # rule 4
BATCH_PAUSE_SEC = 3


class Blocked(Exception):
    """Meta answers empty: stop and retry later, mark nothing."""


def _seen_keys(ads) -> tuple[set, set, set]:
    ids, colls, pkeys = set(), set(), set()
    for a in ads:
        ids.add(str(a.ad_id))
        if a.collation_id:
            colls.add(str(a.collation_id))
        pkeys.add(product_key(str(a.page_id or ""), a.link_url or "", a.body or "", a.store_domain or ""))
    return ids, colls, pkeys


def _missing(ours: list[dict], ads) -> tuple[dict, list]:
    """Split our active ads into (confirmed {ad_id: collation_id}, missing ids)."""
    ids, colls, pkeys = _seen_keys(ads)
    by_id = {str(a.ad_id): str(a.collation_id or "") for a in ads}
    confirmed, missing = {}, []
    for r in ours:
        aid = str(r["ad_id"])
        cid = str(r.get("collation_id") or "")
        if aid in ids or (cid and cid in colls) or (r.get("product_key") and r["product_key"] in pkeys):
            confirmed[aid] = by_id.get(aid, "")
        else:
            missing.append(aid)
    return confirmed, missing


def run(db, *, progress: Callable[[str], None], should_stop: Callable[[], bool],
        proxy: Optional[str] = None, start_batch: int = 1,
        on_checkpoint: Optional[Callable[[int], None]] = None,
        on_new_ad: Optional[Callable] = None, stats: Optional[dict] = None,
        fetch=active_ads_of_pages, sleep=time.sleep,
        rotate_url: Optional[str] = None, rotate_token: Optional[str] = None) -> str:
    """Check every page with active ads. Returns done | stopped | interrupted."""
    stats = stats if stats is not None else {}
    for k in ("pages", "batches", "requests", "confirmed", "stopped", "new", "skipped_batches"):
        stats.setdefault(k, 0)
    if not (proxy or "").strip():
        msg = "\u0644\u0627 \u064a\u0648\u062c\u062f \u0628\u0631\u0648\u0643\u0633\u064a \u0645\u0636\u0628\u0648\u0637 \u2014 \u0627\u0644\u0633\u062d\u0628 \u0645\u0646 IP \u0627\u0644\u0633\u064a\u0631\u0641\u0631 \u0645\u0639\u0637\u0651\u0644. \u0623\u0636\u0641 \u0627\u0644\u0628\u0631\u0648\u0643\u0633\u064a \u0641\u064a \u0627\u0644\u0625\u0639\u062f\u0627\u062f\u0627\u062a."
        progress(msg)
        stats["error"] = msg
        return "interrupted"

    pages = db.status_check_pages()
    batches = [pages[i:i + BATCH_PAGES] for i in range(0, len(pages), BATCH_PAGES)]
    stats["total_batches"] = len(batches)
    progress(f"🔎 فحص حالة الإعلانات: {len(pages)} صفحة لها إعلانات نشطة = {len(batches)} دفعة (50 صفحة لكل طلب).")
    if start_batch > 1:
        progress(f"↩️ استئناف من الدفعة {start_batch}/{len(batches)}.")
    # Two pages known to run many ads: one request, alive if either answers.
    canary: list[str] = db.top_active_pages(2)

    def fetch_ok(page_ids, max_results=None):
        ads, complete, reqs = fetch(page_ids, proxy=proxy, should_stop=should_stop, max_results=max_results)
        stats["requests"] += reqs
        return ads, complete

    def canary_alive() -> bool:
        if not canary:
            return True
        ads, _ = fetch_ok(canary, max_results=1)  # one request is enough
        return bool(ads)

    for idx, batch in enumerate(batches, 1):
        if idx < start_batch:
            continue
        if should_stop():
            return "stopped"
        ours = db.active_ads_of_pages(batch)
        if not ours:
            if on_checkpoint:
                on_checkpoint(idx + 1)
            continue
        # Rule 1: a page that ran ads in an earlier batch must still answer.
        if not canary_alive():
            # Same recovery as a normal scrape: ask the IP rotator for a fresh
            # public IP and wait until the tunnel is actually back (the re-dial
            # drops it for ~1 minute), then re-check on the new IP.
            rotated = rotate_and_wait_ready(proxy, progress, should_stop,
                                            rotate_url=rotate_url, rotate_token=rotate_token)
            if not rotated:
                sleep(RECHECK_WAIT_SEC)
            if not canary_alive():
                raise Blocked("Meta returns no ads even for a page known to run ads (IP soft-blocked?)")

        ads, complete = fetch_ok(batch)
        if not complete and not should_stop():  # rule 2: one retry
            sleep(RECHECK_WAIT_SEC)
            ads, complete = fetch_ok(batch)
        if should_stop():
            return "stopped"
        if not complete:
            stats["skipped_batches"] += 1
            progress(f"⚠️ الدفعة {idx}/{len(batches)} لم تكتمل — تُركت بدون تغيير.")
            if not ads:
                ip_rotator.rotate(url=rotate_url, token=rotate_token)
                raise Blocked("batch returned nothing twice")
            if on_checkpoint:
                on_checkpoint(idx + 1)
            continue

        confirmed, missing = _missing(ours, ads)
        if len(ours) >= SUSPICIOUS_MIN_ADS and len(missing) > SUSPICIOUS_RATIO * len(ours):
            stats["skipped_batches"] += 1
            progress(f"⚠️ الدفعة {idx}: {len(missing)}/{len(ours)} اختفت دفعة واحدة — نتيجة مشبوهة، لم تُطبّق.")
            db.apply_status_check(confirmed, [])
            stats["confirmed"] += len(confirmed)
            if on_checkpoint:
                on_checkpoint(idx + 1)
            continue

        stopped: list[str] = []
        if missing:  # rule 4: second look, only at the pages that lost ads
            sleep(RECHECK_WAIT_SEC)
            miss_set = set(missing)
            again_pages = sorted({str(r["page_id"]) for r in ours if str(r["ad_id"]) in miss_set})
            ads2, complete2 = fetch_ok(again_pages)
            # An empty second answer is trusted only if the canary still answers.
            if complete2 and (ads2 or canary_alive()):
                confirmed2, missing2 = _missing([r for r in ours if str(r["ad_id"]) in miss_set], ads2)
                confirmed.update(confirmed2)
                stopped = missing2
            ads = ads + ads2

        db.apply_status_check(confirmed, stopped)
        stats["confirmed"] += len(confirmed)
        stats["stopped"] += len(stopped)
        stats["batches"] += 1
        stats["pages"] += len(batch)

        if on_new_ad:
            known = {str(r["ad_id"]) for r in ours}
            for a in ads:
                if str(a.ad_id) not in known and a.store_domain and not db.ad_exists(a.ad_id):
                    known.add(str(a.ad_id))
                    if on_new_ad(a):
                        stats["new"] += 1

        # Next canary: the page of this batch that runs the most ads.
        counts: dict[str, int] = {}
        for a in ads:
            counts[str(a.page_id)] = counts.get(str(a.page_id), 0) + 1
        if counts:
            canary = sorted(counts, key=counts.get, reverse=True)[:2]

        progress(f"✅ دفعة {idx}/{len(batches)}: {len(confirmed)} مؤكد نشط، {len(stopped)} توقف"
                 f" — المجموع: {stats['confirmed']} نشط، {stats['stopped']} متوقف، {stats['requests']} طلب.")
        if on_checkpoint:
            on_checkpoint(idx + 1)
        sleep(BATCH_PAUSE_SEC)
    return "done"
