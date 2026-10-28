import functools
import os
from typing import Optional

from flask import g, jsonify, request, session
from werkzeug.security import check_password_hash

from db import db


SESSION_DAYS = float(os.environ.get("SESSION_DAYS", "7") or 7)


def _start_session(user: dict) -> None:
    """Log a user in: a fresh server-side session (never reuse an old id)."""
    session.clear()
    session.permanent = True
    session["sid"] = db.create_session(
        int(user["id"]), days=SESSION_DAYS,
        ip=request.headers.get("X-Forwarded-For", request.remote_addr or "").split(",")[0].strip(),
        user_agent=request.headers.get("User-Agent", ""))
    session["user_id"] = int(user["id"])
    session["user_email"] = user["email"]
    session["user_role"] = user["role"]


def _int_arg(name: str, default: int) -> int:
    try:
        return int(request.args.get(name, default) or default)
    except (TypeError, ValueError):
        return default


def _client_ip() -> str:
    """Client address (first X-Forwarded-For hop behind the Coolify proxy)."""
    return (request.headers.get("X-Forwarded-For", "") or request.remote_addr or "").split(",")[0].strip()[:64]


def _audit(event: str, detail: str = "", user: Optional[dict] = None, email: str = "") -> None:
    u = user if user is not None else getattr(g, "user", None)
    db.log_security_event(event, user_id=(int(u["id"]) if u else None),
                          email=email or (u or {}).get("email", ""), ip=_client_ip(), detail=detail)


# Brute-force limits (failed logins in a sliding window).
LOGIN_MAX_PER_EMAIL = int(os.environ.get("LOGIN_MAX_PER_EMAIL", "5") or 5)
LOGIN_MAX_PER_IP = int(os.environ.get("LOGIN_MAX_PER_IP", "20") or 20)
LOGIN_WINDOW_MIN = int(os.environ.get("LOGIN_WINDOW_MIN", "15") or 15)
REGISTER_MAX_PER_IP_HOUR = 5
MIN_PASSWORD_LEN = 10


def _login_blocked(email: str) -> bool:
    ip = _client_ip()
    return (db.count_failed_attempts("login", LOGIN_WINDOW_MIN, email=email) >= LOGIN_MAX_PER_EMAIL
            or db.count_failed_attempts("login", LOGIN_WINDOW_MIN, ip=ip) >= LOGIN_MAX_PER_IP)


def _check_credentials(email: str, password: str):
    """Shared by dashboard and extension login: (user, error, status).
    Every attempt is throttled and recorded; failures are audited."""
    ip = _client_ip()
    if _login_blocked(email):
        _audit("login_blocked", "too many failed attempts", user=None, email=email)
        return None, f"محاولات كثيرة فاشلة. حاول مجدداً بعد {LOGIN_WINDOW_MIN} دقيقة.", 429
    user = db.get_user_by_email(email) if email else None
    if not user or not password or not check_password_hash(user["password_hash"], password):
        db.record_auth_attempt("login", email, ip, False)
        _audit("login_failed", "", user=None, email=email)
        return None, "البريد الإلكتروني أو كلمة المرور غير صحيحة", 401
    if not user.get("is_active"):
        db.record_auth_attempt("login", email, ip, False)
        _audit("login_refused_disabled", "", user=user)
        return None, "هذا الحساب معطل. يرجى التواصل مع المدير لتفعيله", 403
    db.record_auth_attempt("login", email, ip, True)
    return user, None, 200


def _registration_open() -> bool:
    return db.get_settings().get("registration_open", "false").lower() in ("1", "true", "yes", "on")


def login_required(f):
    @functools.wraps(f)
    def decorated_function(*args, **kwargs):
        if not getattr(g, "user", None):
            return jsonify({"ok": False, "error": "يرجى تسجيل الدخول أولاً", "unauthorized": True}), 401
        return f(*args, **kwargs)
    return decorated_function


def admin_required(f):
    @functools.wraps(f)
    def decorated_function(*args, **kwargs):
        if not getattr(g, "user", None):
            return jsonify({"ok": False, "error": "يرجى تسجيل الدخول أولاً", "unauthorized": True}), 401
        if g.user.get("role") != "admin":
            return jsonify({"ok": False, "error": "هذا الإجراء يتطلب صلاحيات مدير النظام (Admin)", "forbidden": True}), 403
        return f(*args, **kwargs)
    return decorated_function


def _scope_user_id():
    """Whose data the current dashboard request may see.

    A regular user only ever sees their own saves and tracked pages. An admin
    sees everything, or one user's data when ``?user_id=`` is given.
    """
    # Identity and role come from the server-side session (g.user), never
    # from the request: only an admin may pick another user's view.
    if g.user.get("role") == "admin":
        raw = (request.args.get("user_id") or "").strip()
        return int(raw) if raw.isdigit() else None
    return int(g.user["id"])



def _token_matches(provided: str, expected: str) -> bool:
    import hmac
    provided, expected = (provided or "").strip(), (expected or "").strip()
    return bool(provided) and bool(expected) and hmac.compare_digest(provided, expected)
