# -*- coding: utf-8 -*-
"""Store sales tracking (public numbers) + admin controls.

Regular users only ever see order counts. Every technical detail (how the
number is obtained, the raw order numbers, errors, proxy/rotation) stays behind
admin-only endpoints so a SaaS customer cannot learn the method.
"""
import logging
import os
import threading
import time

from flask import Blueprint, g, jsonify, request

import ip_rotator
import sales_tracker
from db import db
from web.common import _audit, admin_required, login_required

logger = logging.getLogger("sales_web")
bp = Blueprint("sales", __name__)

MAX_STORES_FREE = 5       # a user may follow up to 5 storefronts
MAX_ACTIVE_FREE = 1       # but only one is measured on the free plan

_PROBE_LOCK = threading.Lock()
_RUNNING: set[int] = set()
_RUNNING_LOCK = threading.Lock()

SETTING_PROXY = "sales_probe_proxy"
SETTING_AUTO = "sales_auto_probe"
SETTING_ROTATE = "sales_rotate_before"


def _is_admin() -> bool:
    return bool(getattr(g, "user", None) and g.user.get("role") == "admin")


def _user_id() -> int:
    return int(g.user["id"])


def _settings_vars() -> dict:
    s = db.get_settings()
    return {
        "proxy": (s.get(SETTING_PROXY) or "").strip(),
        "auto": (s.get(SETTING_AUTO) or "true").lower() in ("1", "true", "yes", "on"),
        "rotate": (s.get(SETTING_ROTATE) or "false").lower() in ("1", "true", "yes", "on"),
        "rotator": ip_rotator.enabled(),
    }


def _probe_proxy() -> str:
    """Egress for storefront calls: the admin-set proxy, else the scrape proxy.

    A datacenter IP is often blocked by the storefront's CDN (HTTP 403), so the
    production server reuses the residential proxy the scraper already has."""
    cfg = _settings_vars()
    return (cfg["proxy"] or (os.environ.get("META_ADS_PROXY") or "").strip())


def _is_running(store_id: int) -> bool:
    with _RUNNING_LOCK:
        return int(store_id) in _RUNNING


def _mark_running(store_id: int, on: bool) -> None:
    with _RUNNING_LOCK:
        if on:
            _RUNNING.add(int(store_id))
        else:
            _RUNNING.discard(int(store_id))


def _owned_store(store_id: int):
    """The store if the caller may see it (owner or admin)."""
    store = db.get_lf_store(int(store_id))
    if not store:
        return None
    if _is_admin():
        return store
    owner = store.get("created_by")
    if owner is not None and int(owner) == _user_id():
        return store
    return None


def _limits() -> dict:
    if _is_admin():
        return {"max_stores": None, "max_active": None,
                "stores_used": db.count_lf_stores(None), "active_used": 0, "admin": True}
    uid = _user_id()
    return {"max_stores": MAX_STORES_FREE, "max_active": MAX_ACTIVE_FREE,
            "stores_used": db.count_lf_stores(uid),
            "active_used": db.count_enabled_lf_stores(uid), "admin": False}


def _public_store(s: dict) -> dict:
    """Only what a customer may see: the store and its order counts."""
    return {
        "id": s.get("id"),
        "store_host": s.get("store_host"),
        "label": s.get("label") or "",
        "enabled": bool(s.get("enabled")),
        "orders_today": int(s.get("orders_today") or 0),
        "orders_window": int(s.get("orders_window") or 0),
        "window_days": int(s.get("window_days") or 7),
        "days": s.get("days") or {},
        "updated_at": s.get("last_checked_at") or s.get("last_probed_at") or "",
        "running": bool(s.get("running")),
    }


def _admin_store(s: dict) -> dict:
    row = dict(s)
    row["running"] = bool(s.get("running"))
    return row


def _public_overview(days: int) -> list[dict]:
    owner = None if _is_admin() else _user_id()
    stores = db.lf_sales_overview(days=days, owner_id=owner)
    for s in stores:
        s["running"] = _is_running(int(s["id"])) or s.get("last_probe_status") == "running"
    return stores


def _start_probe(store_id: int) -> bool:
    store = db.get_lf_store(store_id)
    if not store or _is_running(store_id):
        return False
    _mark_running(store_id, True)

    def run():
        try:
            probe_store(store)
        except Exception:  # noqa: BLE001
            logger.exception("probe thread crashed for %s", store.get("store_host"))
            _mark_running(store_id, False)

    threading.Thread(target=run, daemon=True).start()
    return True


def probe_store(store: dict) -> dict:
    """One measurement. Serialised across the process (system-side)."""
    store_id = int(store["id"])
    cfg = _settings_vars()
    with _PROBE_LOCK:
        db.update_lf_store(store_id, last_probe_status="running", last_error="")
        proxy = _probe_proxy() or None
        if cfg["rotate"] and cfg["rotator"]:
            ip_rotator.rotate(min_interval=0)
            time.sleep(2)
        try:
            res = sales_tracker.probe(store["product_url"], proxy=proxy)
            db.add_lf_probe(
                store_id, kind="probe", store_host=res["store_host"],
                account_id=res["account_id"], funnel_id=res["funnel_id"],
                order_name=res["order_name"], order_number=res["order_number"],
                order_id=res["order_id"], currency=res["currency"], total=res.get("total"),
                state=res.get("state", ""), city=res.get("city", ""),
                phone=res.get("phone", ""), ip=res.get("ip", ""),
                probed_at=res.get("probed_at", ""))
            logger.info("sales probe %s -> %s", store["store_host"], res["order_name"])
            return res
        except sales_tracker.SalesTrackError as exc:
            kind = {"cooldown": "cooldown", "otp": "otp"}.get(exc.code, "error")
            db.add_lf_probe(store_id, kind=kind, store_host=store["store_host"],
                            account_id=store.get("account_id") or "",
                            funnel_id=store.get("funnel_id") or "", error=str(exc)[:500])
            logger.warning("sales probe %s failed (%s)", store["store_host"], exc.code)
            return {"ok": False, "code": exc.code, "error": str(exc)}
        except Exception as exc:  # noqa: BLE001 — never leave the row stuck on "running"
            db.update_lf_store(store_id, last_probe_status="error", last_error=str(exc)[:500])
            logger.exception("sales probe crashed for %s", store.get("store_host"))
            return {"ok": False, "code": "error", "error": str(exc)}
        finally:
            _mark_running(store_id, False)


# ── customer routes (numbers only) ─────────────────────────────────────
@bp.route("/api/sales/overview", methods=["GET"])
@login_required
def api_overview():
    days = _days_arg(7, 90)
    stores = _public_overview(days)
    admin = _is_admin()
    return jsonify({
        "ok": True, "days": days, "admin": admin,
        "stores": [_admin_store(s) if admin else _public_store(s) for s in stores],
        "limits": _limits(),
        "config": _settings_vars() if admin else None,
    })


@bp.route("/api/sales/stores", methods=["POST"])
@login_required
def api_add_store():
    c = request.get_json(force=True, silent=True) or {}
    url = str(c.get("url") or c.get("store_url") or "").strip()
    if not url:
        return jsonify({"ok": False, "error": "أدخل رابط متجر أو رابط منتج"}), 400

    admin = _is_admin()
    uid = _user_id()
    if not admin and db.count_lf_stores(uid) >= MAX_STORES_FREE:
        return jsonify({"ok": False, "error": f"يمكنك متابعة {MAX_STORES_FREE} متاجر كحد أقصى", "limit": True}), 400

    cfg = None
    try:
        cfg = sales_tracker.detect(url)
    except sales_tracker.SalesTrackError as direct_err:
        proxy = _probe_proxy()
        if proxy:
            try:
                cfg = sales_tracker.detect(url, proxy=proxy)
            except sales_tracker.SalesTrackError as exc:
                direct_err = exc
        if cfg is None:
            # never explain the internals to a customer
            msg = str(direct_err) if admin else "تعذّر التعرف على هذا المتجر. تأكد من أنه رابط منتج صحيح."
            if admin and "403" in str(direct_err):
                msg = ("تعذّر الوصول إلى المتجر (HTTP 403). غالبًا الخادم محجوب من المتجر — "
                       "اضبط «بروكسي القياس» في الإعدادات أو تأكد من META_ADS_PROXY.")
            return jsonify({"ok": False, "error": msg,
                            "code": getattr(direct_err, "code", "invalid") if admin else "invalid"}), 400

    # free plan: only the first store is measured automatically
    enabled = 1 if admin or db.count_enabled_lf_stores(uid) == 0 else 0
    store_id = db.add_lf_store(
        store_host=cfg["host"], product_url=cfg["url"],
        label=str(c.get("label") or "").strip()[:60],
        account_id=cfg["account_id"], account_uid=cfg.get("account_uid", ""),
        account_name=cfg.get("account_name", ""), funnel_id=cfg.get("funnel_id", ""),
        currency=cfg.get("currency", ""), interval_hours=12.0,
        created_by=uid, enabled=enabled)
    if not store_id:
        return jsonify({"ok": False, "error": "هذا المتجر متابع بالفعل"}), 409
    _audit("sales_store_added", f"{cfg['host']} user={uid}")
    store = db.get_lf_store(store_id)
    if enabled:
        _start_probe(store_id)
    return jsonify({"ok": True, "store": _public_store(dict(store, orders_today=0, orders_window=0)),
                    "limits": _limits(),
                    "detected": cfg if admin else {"host": cfg["host"]}})


@bp.route("/api/sales/stores/<int:store_id>", methods=["DELETE"])
@login_required
def api_delete_store(store_id: int):
    store = _owned_store(store_id)
    if not store:
        return jsonify({"ok": False, "error": "المتجر غير موجود"}), 404
    db.delete_lf_store(store_id)
    _audit("sales_store_deleted", store.get("store_host", ""))
    return jsonify({"ok": True, "limits": _limits()})


@bp.route("/api/sales/stores/<int:store_id>/toggle", methods=["POST"])
@login_required
def api_toggle_store(store_id: int):
    store = _owned_store(store_id)
    if not store:
        return jsonify({"ok": False, "error": "المتجر غير موجود"}), 404
    enable = not int(store.get("enabled") or 0)
    if enable and not _is_admin():
        others = db.count_enabled_lf_stores(_user_id())
        if others >= MAX_ACTIVE_FREE:
            return jsonify({"ok": False, "error": "الخطة المجانية تتيح قياس متجر واحد. أوقف المتجر الآخر أولاً.",
                            "limit": True}), 409
    db.update_lf_store(store_id, enabled=1 if enable else 0)
    if enable and store.get("last_order_number") is None:
        _start_probe(store_id)  # first reading only; later ones come from the scheduler
    return jsonify({"ok": True, "enabled": enable, "limits": _limits()})


@bp.route("/api/sales/stores/<int:store_id>/series", methods=["GET"])
@login_required
def api_store_series(store_id: int):
    store = _owned_store(store_id)
    if not store:
        return jsonify({"ok": False, "error": "المتجر غير موجود"}), 404
    days = _days_arg(30, 365)
    probes = db.lf_store_probes(store_id, days=days)
    return jsonify({"ok": True, "days": db.lf_orders_by_day(probes)})


# ── admin routes (method + diagnostics) ────────────────────────────────
@bp.route("/api/sales/admin/stores/<int:store_id>/probe", methods=["POST"])
@admin_required
def api_admin_probe(store_id: int):
    if not db.get_lf_store(store_id):
        return jsonify({"ok": False, "error": "المتجر غير موجود"}), 404
    started = _start_probe(store_id)
    return jsonify({"ok": started, "error": "" if started else "القياس جارٍ بالفعل"}), (200 if started else 409)


@bp.route("/api/sales/admin/probe-all", methods=["POST"])
@admin_required
def api_admin_probe_all():
    stores = [s for s in db.list_lf_stores(enabled_only=True) if not _is_running(int(s["id"]))]
    if not stores:
        return jsonify({"ok": False, "error": "لا توجد متاجر مفعّلة"}), 409

    def run():
        for s in stores:
            try:
                probe_store(s)
            except Exception:  # noqa: BLE001
                logger.exception("probe-all failed for %s", s.get("store_host"))
            time.sleep(3)

    threading.Thread(target=run, daemon=True).start()
    _audit("sales_probe_all", f"{len(stores)} stores")
    return jsonify({"ok": True, "queued": len(stores)})


@bp.route("/api/sales/admin/stores/<int:store_id>/probes", methods=["GET"])
@admin_required
def api_admin_probes(store_id: int):
    if not db.get_lf_store(store_id):
        return jsonify({"ok": False, "error": "المتجر غير موجود"}), 404
    days = _days_arg(30, 365)
    probes = db.lf_store_probes(store_id, days=days)
    return jsonify({"ok": True, "probes": probes, "orders_by_day": db.lf_orders_by_day(probes)})


@bp.route("/api/sales/admin/config", methods=["POST"])
@admin_required
def api_admin_config():
    c = request.get_json(force=True, silent=True) or {}
    if "proxy" in c:
        db.set_setting(SETTING_PROXY, str(c.get("proxy") or "").strip()[:500])
    if "auto" in c:
        db.set_setting(SETTING_AUTO, "true" if c.get("auto") else "false")
    if "rotate" in c:
        db.set_setting(SETTING_ROTATE, "true" if c.get("rotate") else "false")
    return jsonify({"ok": True, "config": _settings_vars()})


def _days_arg(default: int, maximum: int) -> int:
    try:
        return max(1, min(maximum, int(request.args.get("days", default))))
    except (TypeError, ValueError):
        return default
