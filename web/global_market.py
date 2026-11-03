# -*- coding: utf-8 -*-
"""Foreign market side tab: manual scans of non-Algerian stores.

Ads here live in the same bank with ``market='global'`` and never appear in
the Algerian dashboard. Everything (country=ALL, every language, every CTA)
is allowed, and there is no scheduled scanning — the user presses scan.
"""
from __future__ import annotations

import csv
import io
import logging
from datetime import datetime

from flask import Blueprint, g, jsonify, request, send_file

from db import db
from pages import clean_domain, resolve_target_url
from scrape_manager import job_wakeup, manager
from web.common import login_required, _scope_user_id

logger = logging.getLogger("web_app")

bp = Blueprint("global_market", __name__)

GLOBAL_MARKET = "global"
GLOBAL_COUNTRY = "ALL"


def _access(target_id: int):
    t = db.get_target(target_id)
    uid = _scope_user_id()
    if not t or (t.get("market") or "dz") != GLOBAL_MARKET:
        return None, uid
    if uid is not None and not db.user_has_target(uid, target_id):
        return None, uid
    return t, uid


def _queue_scan(t: dict, source: str):
    if db.has_open_job(t["id"]):
        return None
    url = resolve_target_url("domain", t["target_value"], country=GLOBAL_COUNTRY)
    job_id = db.create_job(
        query=t["target_value"], url=url, country=GLOBAL_COUNTRY,
        stores_only=True, source=source, target_id=t["id"],
        incremental=False, market=GLOBAL_MARKET,
    )
    db.update_target_scraped(t["id"])
    job_wakeup.set()
    return job_id


@bp.route("/api/global/targets", methods=["GET"])
@login_required
def api_global_targets():
    return jsonify({"targets": db.list_competitors(user_id=_scope_user_id(), market=GLOBAL_MARKET)})


@bp.route("/api/global/targets", methods=["POST"])
@login_required
def api_add_global_target():
    cfg = request.get_json(force=True, silent=True) or {}
    raw = (cfg.get("value") or cfg.get("target_value") or "").strip()
    if not raw or len(raw) > 500:
        return jsonify({"ok": False, "error": "اكتب دومين المتجر الخارجي"}), 400
    dom = clean_domain(raw)
    if not dom or "." not in dom:
        return jsonify({"ok": False, "error": "دومين غير صالح"}), 400
    label = (cfg.get("label") or "").strip()[:120] or dom
    uid = int(g.user["id"])
    tid, created = db.add_user_target(
        int(uid), "domain", dom, label=label, country=GLOBAL_COUNTRY,
        interval_hours=0, source="global_dashboard", market=GLOBAL_MARKET)
    if not created:
        return jsonify({"ok": False, "error": "هذا المتجر مضاف بالفعل في السوق الخارجي"}), 400
    return jsonify({"ok": True, "target_id": tid})


@bp.route("/api/global/targets/<int:target_id>", methods=["DELETE"])
@login_required
def api_delete_global_target(target_id):
    t, uid = _access(target_id)
    if not t:
        return jsonify({"ok": False, "error": "غير موجود"}), 404
    if uid is None:
        db.delete_target_everywhere(target_id)
    else:
        db.remove_user_target(uid, target_id)
    return jsonify({"ok": True})


@bp.route("/api/global/targets/<int:target_id>/scan", methods=["POST"])
@login_required
def api_scan_global_target(target_id):
    t, _uid = _access(target_id)
    if not t:
        return jsonify({"error": "target not found"}), 404
    job_id = _queue_scan(t, "manual_global")
    if job_id is None:
        return jsonify({"ok": True, "already_queued": True})
    manager.events.put({"type": "log", "data": {
        "text": f"🌍 أُضيف فحص «{t.get('label') or t['target_value']}» (السوق الخارجي) إلى قائمة الانتظار."}})
    return jsonify({"ok": True, "job_id": job_id})


@bp.route("/api/global/scan-all", methods=["POST"])
@login_required
def api_scan_all_global():
    count = 0
    for t in db.list_competitors(user_id=_scope_user_id(), market=GLOBAL_MARKET):
        if _queue_scan(t, "manual_global_all") is not None:
            count += 1
    return jsonify({"ok": True, "enqueued": count})


@bp.route("/api/global/stores", methods=["GET"])
@login_required
def api_global_stores():
    return jsonify({"stores": db.global_stores(search=request.args.get("search", ""))})


@bp.route("/api/global/products", methods=["GET"])
@login_required
def api_global_products():
    store = (request.args.get("store") or "").strip()
    search = (request.args.get("search") or "").strip()
    sort = request.args.get("sort", "most_collated")
    if sort == "ads":
        sort = "most_collated"
    def _int(name, default):
        try:
            return int(request.args.get(name, default) or default)
        except (TypeError, ValueError):
            return default
    limit = max(1, min(_int("limit", 60), 200))
    offset = max(0, _int("offset", 0))
    min_ads = max(0, _int("min_ads", 0))
    filters = dict(
        market=GLOBAL_MARKET, store_domain=store, search=search,
        sort_by=sort, min_product_ads=min_ads,
    )
    rows = db.attach_entity_stats(db.get_ad_groups(limit=limit, offset=offset, **filters))
    total_groups = db.count_ad_groups(**{k: v for k, v in filters.items() if k != "sort_by"})
    total_ads = db.count_ads(market=GLOBAL_MARKET, store_domain=store, search=search)
    return jsonify({"products": rows, "total": total_groups, "total_ads": total_ads,
                    "market": GLOBAL_MARKET})


@bp.route("/api/global/products.csv")
@login_required
def api_global_products_csv():
    store = (request.args.get("store") or "").strip()
    rows = db.attach_entity_stats(db.get_ad_groups(
        limit=2000, offset=0, sort_by="most_collated", market=GLOBAL_MARKET,
        store_domain=store))
    buf = io.StringIO()
    cols = ["product_key", "page_name", "store_domain", "body", "link_url",
            "ads_count", "active_ads", "first_launch", "last_launch",
            "max_days", "texts", "hunter_score"]
    w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
    w.writeheader()
    for r in rows:
        g = r.get("group") or {}
        w.writerow({
            "product_key": g.get("key", ""),
            "page_name": r.get("page_name", ""),
            "store_domain": r.get("store_domain", ""),
            "body": (r.get("body") or "").replace("\n", " "),
            "link_url": r.get("link_url", ""),
            "ads_count": g.get("count", 0),
            "active_ads": g.get("active", 0),
            "first_launch": g.get("first_launch", ""),
            "last_launch": g.get("last_launch", ""),
            "max_days": g.get("max_days", 0),
            "texts": g.get("texts", 0),
            "hunter_score": r.get("hunter_score", 0),
        })
    data = io.BytesIO(buf.getvalue().encode("utf-8-sig"))
    return send_file(data, mimetype="text/csv", as_attachment=True,
                     download_name=f"global_products_{int(datetime.now().timestamp())}.csv")
