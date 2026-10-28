import os
import csv
import functools
import io
import json
import logging
import secrets
import time
from datetime import datetime, timezone
from typing import Callable, Optional

from flask import Blueprint, Response, g, jsonify, render_template, request, send_file, session
from werkzeug.security import check_password_hash, generate_password_hash

from db import db
from pages import clean_domain, extract_page_id, resolve_target_url
from store_filter import extract_store_domain
from scrape_manager import (
    manager, job_wakeup, DEFAULT_COUNTRY, FIXED_COUNTRY, FIXED_LANGS,
    MONTH_MODE, MONTH_LANGS, MARKET_WILDCARD, STATUS_MODE, SCRAPE_ENABLED,
)
from background import _queue_month_scrape, _queue_status_check
from web.common import (
    SESSION_DAYS, _start_session, _int_arg, _client_ip, _audit,
    _login_blocked, _check_credentials, _registration_open,
    login_required, admin_required, _scope_user_id,
    LOGIN_MAX_PER_EMAIL, LOGIN_MAX_PER_IP, LOGIN_WINDOW_MIN,
    REGISTER_MAX_PER_IP_HOUR, MIN_PASSWORD_LEN,
)

logger = logging.getLogger("web_app")

bp = Blueprint("auth", __name__)


bp = Blueprint("auth", __name__)


@bp.route("/api/auth/me", methods=["GET"])
def api_auth_me():
    user = getattr(g, "user", None)  # resolved from the server-side session
    if not user:
        return jsonify({"authenticated": False})
    return jsonify({
        "authenticated": True,
        "user": {
            "id": user["id"],
            "email": user["email"],
            "role": user["role"],
        }
    })


@bp.route("/api/auth/login", methods=["POST"])
def api_auth_login():
    data = request.get_json(force=True, silent=True) or {}
    email = str(data.get("email") or "").strip().lower()
    password = str(data.get("password") or "")
    if not email or not password:
        return jsonify({"ok": False, "error": "ط·آ§ط¸â€‍ط·آ¨ط·آ±ط¸ظ¹ط·آ¯ ط·آ§ط¸â€‍ط·آ¥ط¸â€‍ط¸ئ’ط·ع¾ط·آ±ط¸ث†ط¸â€ ط¸ظ¹ ط¸ث†ط¸ئ’ط¸â€‍ط¸â€¦ط·آ© ط·آ§ط¸â€‍ط¸â€¦ط·آ±ط¸ث†ط·آ± ط¸â€¦ط·آ·ط¸â€‍ط¸ث†ط·آ¨ط·آ§ط¸â€ "}), 400

    user, err, code = _check_credentials(email, password)
    if not user:
        return jsonify({"ok": False, "error": err}), code

    _start_session(user)
    db.update_user_login(user["id"])
    _audit("login_success", "dashboard", user=user)
    return jsonify({
        "ok": True,
        "user": {
            "id": user["id"],
            "email": user["email"],
            "role": user["role"],
        }
    })


@bp.route("/api/auth/register", methods=["POST"])
def api_auth_register():
    if not _registration_open():
        return jsonify({"ok": False, "error": "ط·آ§ط¸â€‍ط·ع¾ط·آ³ط·آ¬ط¸ظ¹ط¸â€‍ ط¸â€¦ط·ط›ط¸â€‍ط¸â€ڑ ط·آ­ط·آ§ط¸â€‍ط¸ظ¹ط·آ§ط¸â€¹. ط·آ§ط·آ·ط¸â€‍ط·آ¨ ط¸â€¦ط¸â€  ط·آ§ط¸â€‍ط¸â€¦ط·آ¯ط¸ظ¹ط·آ± ط¸ظ¾ط·ع¾ط·آ­ط¸â€، ط·آ£ط¸ث† ط·آ¥ط¸â€ ط·آ´ط·آ§ط·طŒ ط·آ­ط·آ³ط·آ§ط·آ¨ط¸ئ’."}), 403
    if db.count_attempts("register", 60, _client_ip()) >= REGISTER_MAX_PER_IP_HOUR:
        return jsonify({"ok": False, "error": "ط¸â€¦ط·آ­ط·آ§ط¸ث†ط¸â€‍ط·آ§ط·ع¾ ط·ع¾ط·آ³ط·آ¬ط¸ظ¹ط¸â€‍ ط¸ئ’ط·آ«ط¸ظ¹ط·آ±ط·آ© ط¸â€¦ط¸â€  ط¸â€،ط·آ°ط·آ§ ط·آ§ط¸â€‍ط·آ¬ط¸â€،ط·آ§ط·آ². ط·آ­ط·آ§ط¸ث†ط¸â€‍ ط¸â€‍ط·آ§ط·آ­ط¸â€ڑط·آ§ط¸â€¹."}), 429
    data = request.get_json(force=True, silent=True) or {}
    email = str(data.get("email") or "").strip().lower()
    password = str(data.get("password") or "")
    if not email or not password:
        return jsonify({"ok": False, "error": "ط¸ظ¹ط·آ±ط·آ¬ط¸â€° ط¸â€¦ط¸â€‍ط·طŒ ط·آ¬ط¸â€¦ط¸ظ¹ط·آ¹ ط·آ§ط¸â€‍ط·آ­ط¸â€ڑط¸ث†ط¸â€‍ ط·آ§ط¸â€‍ط¸â€¦ط·آ·ط¸â€‍ط¸ث†ط·آ¨ط·آ©"}), 400
    import re as _re
    if len(email) > 254 or not _re.fullmatch(r"[^@\s<>\"'`]+@[^@\s<>\"'`]+\.[A-Za-z]{2,}", email):
        return jsonify({"ok": False, "error": "ط·آ§ط¸â€‍ط·آ¨ط·آ±ط¸ظ¹ط·آ¯ ط·آ§ط¸â€‍ط·آ¥ط¸â€‍ط¸ئ’ط·ع¾ط·آ±ط¸ث†ط¸â€ ط¸ظ¹ ط·ط›ط¸ظ¹ط·آ± ط·آµط·آ§ط¸â€‍ط·آ­"}), 400
    if len(password) < MIN_PASSWORD_LEN:
        return jsonify({"ok": False, "error": f"ط¸ئ’ط¸â€‍ط¸â€¦ط·آ© ط·آ§ط¸â€‍ط¸â€¦ط·آ±ط¸ث†ط·آ± ط¸ظ¹ط·آ¬ط·آ¨ ط·آ£ط¸â€  ط¸â€‍ط·آ§ ط·ع¾ط¸â€ڑط¸â€‍ ط·آ¹ط¸â€  {MIN_PASSWORD_LEN} ط·آ£ط·آ­ط·آ±ط¸ظ¾"}), 400
    if password.lower() == email or password.lower() in email or len(set(password)) < 4:
        return jsonify({"ok": False, "error": "ط¸ئ’ط¸â€‍ط¸â€¦ط·آ© ط·آ§ط¸â€‍ط¸â€¦ط·آ±ط¸ث†ط·آ± ط·آ¶ط·آ¹ط¸ظ¹ط¸ظ¾ط·آ© ط·آ¬ط·آ¯ط·آ§ط¸â€¹"}), 400
    db.record_auth_attempt("register", email, _client_ip(), True)

    existing = db.get_user_by_email(email)
    if existing:
        return jsonify({"ok": False, "error": "ط·آ§ط¸â€‍ط·آ¨ط·آ±ط¸ظ¹ط·آ¯ ط·آ§ط¸â€‍ط·آ¥ط¸â€‍ط¸ئ’ط·ع¾ط·آ±ط¸ث†ط¸â€ ط¸ظ¹ ط¸â€¦ط·آ³ط·آ¬ط¸â€‍ ط¸â€¦ط·آ³ط·آ¨ط¸â€ڑط·آ§ط¸â€¹"}), 400

    hashed = generate_password_hash(password)
    user_id = db.create_user(email=email, password_hash=hashed, role="user", is_active=1)
    if not user_id:
        return jsonify({"ok": False, "error": "ط¸ظ¾ط·آ´ط¸â€‍ ط·آ¥ط¸â€ ط·آ´ط·آ§ط·طŒ ط·آ§ط¸â€‍ط·آ­ط·آ³ط·آ§ط·آ¨ط·إ’ ط¸ظ¹ط·آ±ط·آ¬ط¸â€° ط·آ§ط¸â€‍ط¸â€¦ط·آ­ط·آ§ط¸ث†ط¸â€‍ط·آ© ط¸â€‍ط·آ§ط·آ­ط¸â€ڑط·آ§ط¸â€¹"}), 500

    _start_session({"id": user_id, "email": email, "role": "user"})
    _audit("register", "", user={"id": user_id, "email": email})
    return jsonify({
        "ok": True,
        "user": {
            "id": user_id,
            "email": email,
            "role": "user",
        }
    })


@bp.route("/api/auth/logout", methods=["POST"])
def api_auth_logout():
    """Revokes the session on the server: a copied cookie stops working too."""
    sid = session.get("sid")
    if sid:
        db.revoke_session(sid)
        _audit("logout")
    session.clear()
    return jsonify({"ok": True})


@bp.route("/api/auth/logout-all", methods=["POST"])
@login_required
def api_auth_logout_all():
    """Log the current user out on every device / browser."""
    n = db.revoke_user_sessions(int(g.user["id"]))
    t = db.revoke_api_tokens(int(g.user["id"]))  # browser extensions too
    _audit("logout_all", f"{n} sessions, {t} extension tokens revoked")
    session.clear()
    return jsonify({"ok": True, "revoked": n, "extension_tokens": t})


@bp.route("/api/auth/config", methods=["GET"])
def api_auth_config():
    """Public: what the login screen may offer."""
    return jsonify({"registration_open": _registration_open(), "min_password_length": MIN_PASSWORD_LEN})


@bp.route("/api/auth/extension-tokens", methods=["GET"])
@login_required
def api_my_extension_tokens():
    return jsonify({"active": db.count_api_tokens(int(g.user["id"]))})


@bp.route("/api/auth/extension-tokens/revoke", methods=["POST"])
@login_required
def api_revoke_my_extension_tokens():
    """Disconnect every browser extension signed in to this account."""
    n = db.revoke_api_tokens(int(g.user["id"]))
    _audit("api_tokens_revoked", f"{n} tokens (self)")
    return jsonify({"ok": True, "revoked": n})


# أ¢â€‌â‚¬أ¢â€‌â‚¬ Admin User Management Endpoints أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬أ¢â€‌â‚¬
@bp.route("/api/admin/users", methods=["GET"])
@admin_required
def api_admin_users():
    return jsonify({"users": db.list_users()})


@bp.route("/api/admin/users/<int:user_id>/toggle-status", methods=["POST"])
@admin_required
def api_admin_toggle_user_status(user_id):
    if user_id == int(g.user["id"]):
        return jsonify({"ok": False, "error": "ط¸â€‍ط·آ§ ط¸ظ¹ط¸â€¦ط¸ئ’ط¸â€ ط¸ئ’ ط·ع¾ط·آ¹ط·آ·ط¸ظ¹ط¸â€‍ ط·آ­ط·آ³ط·آ§ط·آ¨ط¸ئ’ ط·آ§ط¸â€‍ط·آ­ط·آ§ط¸â€‍ط¸ظ¹"}), 400
    user = db.get_user_by_id(user_id)
    if not user:
        return jsonify({"ok": False, "error": "ط·آ§ط¸â€‍ط¸â€¦ط·آ³ط·ع¾ط·آ®ط·آ¯ط¸â€¦ ط·ط›ط¸ظ¹ط·آ± ط¸â€¦ط¸ث†ط·آ¬ط¸ث†ط·آ¯"}), 404
    new_status = not bool(user.get("is_active"))
    db.update_user_status(user_id, new_status)
    _audit("user_enabled" if new_status else "user_disabled", f"target={user['email']}")
    return jsonify({"ok": True, "is_active": new_status})


@bp.route("/api/admin/users/<int:user_id>/role", methods=["POST"])
@admin_required
def api_admin_update_user_role(user_id):
    if user_id == int(g.user["id"]):
        return jsonify({"ok": False, "error": "ط¸â€‍ط·آ§ ط¸ظ¹ط¸â€¦ط¸ئ’ط¸â€ ط¸ئ’ ط·ع¾ط·ط›ط¸ظ¹ط¸ظ¹ط·آ± ط·آ±ط·ع¾ط·آ¨ط·آ© ط·آ­ط·آ³ط·آ§ط·آ¨ط¸ئ’ ط·آ§ط¸â€‍ط·آ­ط·آ§ط¸â€‍ط¸ظ¹"}), 400
    data = request.get_json(force=True, silent=True) or {}
    new_role = "admin" if data.get("role") == "admin" else "user"
    target = db.get_user_by_id(user_id)
    if not target:
        return jsonify({"ok": False, "error": "ط·آ§ط¸â€‍ط¸â€¦ط·آ³ط·ع¾ط·آ®ط·آ¯ط¸â€¦ ط·ط›ط¸ظ¹ط·آ± ط¸â€¦ط¸ث†ط·آ¬ط¸ث†ط·آ¯"}), 404
    db.update_user_role(user_id, new_role)
    _audit("role_changed", f"target={target['email']} {target.get('role')}->{new_role}")
    return jsonify({"ok": True, "role": new_role})


@bp.route("/api/admin/users/<int:user_id>", methods=["DELETE"])
@admin_required
def api_admin_delete_user(user_id):
    if user_id == int(g.user["id"]):
        return jsonify({"ok": False, "error": "ط¸â€‍ط·آ§ ط¸ظ¹ط¸â€¦ط¸ئ’ط¸â€ ط¸ئ’ ط·آ­ط·آ°ط¸ظ¾ ط·آ­ط·آ³ط·آ§ط·آ¨ط¸ئ’ ط·آ§ط¸â€‍ط·آ­ط·آ§ط¸â€‍ط¸ظ¹"}), 400
    target = db.get_user_by_id(user_id)
    db.delete_user(user_id)
    _audit("user_deleted", f"target={(target or {}).get('email', user_id)}")
    return jsonify({"ok": True})


@bp.route("/api/admin/users", methods=["POST"])
@admin_required
def api_admin_create_user():
    """Admin adds an account (the way to onboard users while registration is closed)."""
    import re as _re
    data = request.get_json(force=True, silent=True) or {}
    email = str(data.get("email") or "").strip().lower()
    password = str(data.get("password") or "")
    role = "admin" if data.get("role") == "admin" else "user"
    if len(email) > 254 or not _re.fullmatch(r"[^@\s<>\"'`]+@[^@\s<>\"'`]+\.[A-Za-z]{2,}", email):
        return jsonify({"ok": False, "error": "ط·آ§ط¸â€‍ط·آ¨ط·آ±ط¸ظ¹ط·آ¯ ط·آ§ط¸â€‍ط·آ¥ط¸â€‍ط¸ئ’ط·ع¾ط·آ±ط¸ث†ط¸â€ ط¸ظ¹ ط·ط›ط¸ظ¹ط·آ± ط·آµط·آ§ط¸â€‍ط·آ­"}), 400
    if len(password) < MIN_PASSWORD_LEN:
        return jsonify({"ok": False, "error": f"ط¸ئ’ط¸â€‍ط¸â€¦ط·آ© ط·آ§ط¸â€‍ط¸â€¦ط·آ±ط¸ث†ط·آ± ط¸ظ¹ط·آ¬ط·آ¨ ط·آ£ط¸â€  ط¸â€‍ط·آ§ ط·ع¾ط¸â€ڑط¸â€‍ ط·آ¹ط¸â€  {MIN_PASSWORD_LEN} ط·آ£ط·آ­ط·آ±ط¸ظ¾"}), 400
    if db.get_user_by_email(email):
        return jsonify({"ok": False, "error": "ط·آ§ط¸â€‍ط·آ¨ط·آ±ط¸ظ¹ط·آ¯ ط·آ§ط¸â€‍ط·آ¥ط¸â€‍ط¸ئ’ط·ع¾ط·آ±ط¸ث†ط¸â€ ط¸ظ¹ ط¸â€¦ط·آ³ط·آ¬ط¸â€‍ ط¸â€¦ط·آ³ط·آ¨ط¸â€ڑط·آ§ط¸â€¹"}), 400
    uid = db.create_user(email=email, password_hash=generate_password_hash(password), role=role, is_active=1)
    _audit("user_created_by_admin", f"target={email} role={role}")
    return jsonify({"ok": True, "id": uid})


@bp.route("/api/admin/users/<int:user_id>/sign-out", methods=["POST"])
@admin_required
def api_admin_sign_out_user(user_id):
    """Force a user out everywhere: dashboard sessions and extension tokens."""
    target = db.get_user_by_id(user_id)
    if not target:
        return jsonify({"ok": False, "error": "ط·آ§ط¸â€‍ط¸â€¦ط·آ³ط·ع¾ط·آ®ط·آ¯ط¸â€¦ ط·ط›ط¸ظ¹ط·آ± ط¸â€¦ط¸ث†ط·آ¬ط¸ث†ط·آ¯"}), 404
    s, t = db.revoke_user_sessions(user_id), db.revoke_api_tokens(user_id)
    _audit("user_signed_out_by_admin", f"target={target['email']} sessions={s} tokens={t}")
    return jsonify({"ok": True, "sessions": s, "tokens": t})


@bp.route("/api/admin/security-events", methods=["GET"])
@admin_required
def api_admin_security_events():
    return jsonify({"events": db.list_security_events(limit=min(int(request.args.get("limit", 200) or 200), 1000),
                                                      event=(request.args.get("event") or "").strip())})




