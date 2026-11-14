"""Settings → scrape lanes: several proxies scrape the whole market in parallel."""
import json
import re

from flask import Blueprint, jsonify, request

from db import db
from scrape_manager import LANE_SOURCES, scrape_lanes
from web.common import _audit, admin_required

bp = Blueprint("lanes", __name__)

MAX_LANES = 20
_ENV_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")


def _mask(proxy: str) -> str:
    """host:port only — never send proxy passwords back to the page."""
    p = str(proxy or "")
    p = re.sub(r"^[a-z]+://", "", p)
    p = p.split("@")[-1]
    parts = p.split(":")
    return ":".join(parts[:2]) if parts and parts[0] else ""


@bp.route("/api/scrape-lanes", methods=["GET"])
@admin_required
def api_lanes():
    s = db.get_settings()
    try:
        raw = json.loads(s.get("scrape_lanes") or "[]")
    except ValueError:
        raw = []
    raw = raw if isinstance(raw, list) else []
    for ln in raw:  # the saved proxy of a custom lane stays on the server
        if isinstance(ln, dict) and ln.get("proxy"):
            ln["proxy_hint"] = _mask(ln["proxy"])
            ln["proxy"] = ""
    return jsonify({
        "lanes": raw,
        "active": [{"name": ln["name"], "source": ln["source"], "proxy": _mask(ln["proxy"]) or "(بروكسي البيئة)",
                    "rotator": ln["rotate_url"] is None or bool(ln["rotate_url"])} for ln in scrape_lanes(s)],
        "has_status_line": bool((s.get("status_check_proxy") or "").strip()),
    })


@bp.route("/api/scrape-lanes", methods=["POST"])
@admin_required
def api_lanes_save():
    c = request.get_json(force=True, silent=True) or {}
    lanes = c.get("lanes")
    if not isinstance(lanes, list) or len(lanes) > MAX_LANES:
        return jsonify({"error": f"قائمة خطوط غير صالحة (حد أقصى {MAX_LANES})"}), 400
    s = db.get_settings()
    try:
        old = {i: ln for i, ln in enumerate(json.loads(s.get("scrape_lanes") or "[]"))}
    except ValueError:
        old = {}
    out = []
    for i, ln in enumerate(lanes):
        if not isinstance(ln, dict):
            continue
        src = ln.get("source") if ln.get("source") in LANE_SOURCES else "custom"
        item = {"name": str(ln.get("name") or "").strip()[:40], "source": src,
                "enabled": ln.get("enabled") is not False}
        if src == "custom":
            proxy = str(ln.get("proxy") or "").strip()
            if not proxy and isinstance(old.get(ln.get("keep_index", -1)), dict):
                proxy = str(old[ln["keep_index"]].get("proxy") or "")  # unchanged (hidden) proxy
            if not proxy:
                return jsonify({"error": f"الخط «{item['name'] or i + 1}»: البروكسي مطلوب"}), 400
            rot = str(ln.get("rotate_url") or "").strip()
            if rot and not rot.startswith(("http://", "https://")):
                return jsonify({"error": f"الخط «{item['name'] or i + 1}»: رابط التدوير يجب أن يبدأ بـ http"}), 400
            env = str(ln.get("token_env") or "").strip()
            if env and not _ENV_RE.match(env):
                return jsonify({"error": "اسم متغير البيئة للرمز غير صالح (أحرف كبيرة وأرقام و _)"}), 400
            item.update(proxy=proxy[:500], rotate_url=rot[:300], token_env=env)
        out.append(item)
    db.set_setting("scrape_lanes", json.dumps(out, ensure_ascii=False))
    _audit("scrape_lanes_changed", f"{len(out)} lanes")
    return jsonify({"ok": True, "active": len(scrape_lanes())})
