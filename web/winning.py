"""Winning products: save a product from an ad card, follow it, keep notes."""
import logging

from flask import Blueprint, g, jsonify, request

from db import db
from scrape_manager import WINNING_MODE
from background import _queue_winning_check
from web.common import admin_required, login_required, _scope_user_id

logger = logging.getLogger("web_app")

bp = Blueprint("winning", __name__)


def _own_product(wid: int):
    """The saved product when the current user may change it, else None."""
    row = db.get_winning_product(wid)
    if not row:
        return None
    if g.user.get("role") == "admin" or int(row["user_id"]) == int(g.user["id"]):
        return row
    return None


@bp.route("/api/winning", methods=["GET"])
@login_required
def api_winning_list():
    s = db.get_settings()
    return jsonify({
        "products": db.list_winning_products(user_id=_scope_user_id()),
        "stages": list(db.WINNING_STAGES),
        "last_check": s.get("winning_check_last", ""),
        "check_open": db.has_open_mode_job(WINNING_MODE),
    })


@bp.route("/api/winning/keys", methods=["GET"])
@login_required
def api_winning_keys():
    """Products the current user saved (fills the ⭐ on ad cards)."""
    return jsonify({"keys": db.winning_product_keys(int(g.user["id"]))})


@bp.route("/api/winning", methods=["POST"])
@login_required
def api_winning_add():
    cfg = request.get_json(force=True, silent=True) or {}
    ad_id = str(cfg.get("ad_id") or "").strip()
    if not ad_id:
        return jsonify({"ok": False, "error": "ad_id مطلوب"}), 400
    row = db.add_winning_product(int(g.user["id"]), ad_id)
    if not row:
        return jsonify({"ok": False, "error": "الإعلان غير موجود في البنك"}), 404
    return jsonify({"ok": True, "product": row})


@bp.route("/api/winning/<int:wid>", methods=["PATCH"])
@login_required
def api_winning_update(wid: int):
    if not _own_product(wid):
        return jsonify({"ok": False, "error": "المنتج غير موجود"}), 404
    cfg = request.get_json(force=True, silent=True) or {}
    stage = cfg.get("stage")
    if stage is not None and stage not in db.WINNING_STAGES:
        return jsonify({"ok": False, "error": "حالة غير صالحة"}), 400
    db.update_winning_product(wid, notes=cfg.get("notes"), stage=stage)
    return jsonify({"ok": True, "product": db.get_winning_product(wid)})


@bp.route("/api/winning/<int:wid>", methods=["DELETE"])
@login_required
def api_winning_delete(wid: int):
    if not _own_product(wid):
        return jsonify({"ok": False, "error": "المنتج غير موجود"}), 404
    db.remove_winning_product(wid)
    return jsonify({"ok": True})


@bp.route("/api/winning/<int:wid>/events", methods=["GET"])
@login_required
def api_winning_events(wid: int):
    row = _own_product(wid)
    if not row:
        return jsonify({"ok": False, "error": "المنتج غير موجود"}), 404
    return jsonify({"ok": True, "events": db.winning_product_events(row["product_key"])})


@bp.route("/api/winning/check", methods=["POST"])
@admin_required
def api_winning_check_now():
    if not db.winning_pages():
        return jsonify({"ok": False, "error": "لا توجد منتجات رابحة محفوظة بعد"}), 400
    if db.has_open_mode_job(WINNING_MODE):
        return jsonify({"ok": False, "error": "فحص المنتجات الرابحة قيد التنفيذ أو في الانتظار"}), 409
    job_id, queued = _queue_winning_check("manual")
    return jsonify({"ok": True, "job_id": job_id, "queued": queued})
