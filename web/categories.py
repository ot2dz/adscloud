"""User categories: nested folders (a category can have sub-categories) to
sort the competitors and winning products a user follows. One item can be in
several categories. Every category belongs to one user."""
from flask import Blueprint, g, jsonify, request

from db import db
from web.common import login_required

bp = Blueprint("categories", __name__)


def _uid() -> int:
    return int(g.user["id"])


def _owns_item(kind: str, item_id: int) -> bool:
    """The user follows this competitor / saved this winning product."""
    p = db._ph()
    if kind == "target":
        return bool(db._query_one(
            f"SELECT 1 AS ok FROM user_targets WHERE user_id={p} AND target_id={p}", (_uid(), int(item_id))))
    if kind == "winning":
        return bool(db._query_one(
            f"SELECT 1 AS ok FROM winning_products WHERE user_id={p} AND id={p}", (_uid(), int(item_id))))
    return False


@bp.route("/api/categories", methods=["GET"])
@login_required
def api_categories():
    return jsonify(db.list_categories(_uid()))


@bp.route("/api/categories", methods=["POST"])
@login_required
def api_category_create():
    c = request.get_json(force=True, silent=True) or {}
    try:
        cid = db.create_category(_uid(), c.get("name"), c.get("color") or "", c.get("parent_id") or None)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True, "id": cid})


@bp.route("/api/categories/<int:cid>", methods=["PATCH"])
@login_required
def api_category_update(cid: int):
    c = request.get_json(force=True, silent=True) or {}
    try:
        db.update_category(_uid(), cid, name=c.get("name"), color=c.get("color"),
                           parent_id=(c.get("parent_id") or None) if "parent_id" in c else "__keep__")
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True})


@bp.route("/api/categories/<int:cid>", methods=["DELETE"])
@login_required
def api_category_delete(cid: int):
    n = db.delete_category(_uid(), cid)
    if not n:
        return jsonify({"error": "not found"}), 404
    return jsonify({"ok": True, "deleted": n})


@bp.route("/api/categories/assign", methods=["PUT"])
@login_required
def api_category_assign():
    """Set the categories of one competitor (kind=target) or winning product."""
    c = request.get_json(force=True, silent=True) or {}
    kind = str(c.get("kind") or "")
    try:
        item_id = int(c.get("item_id"))
    except (TypeError, ValueError):
        return jsonify({"error": "item_id required"}), 400
    if kind not in db.CATEGORY_KINDS or not _owns_item(kind, item_id):
        return jsonify({"error": "not found"}), 404
    ids = db.set_item_categories(_uid(), kind, item_id, c.get("category_ids") or [])
    return jsonify({"ok": True, "category_ids": ids})
