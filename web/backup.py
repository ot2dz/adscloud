"""Settings → backup: download a full backup, restore one (admin only)."""
import os
import secrets
import tempfile
from datetime import datetime, timezone

from flask import Blueprint, Response, jsonify, request, stream_with_context

import backup
from db import db
from web.common import _audit, admin_required

bp = Blueprint("backup", __name__)

_UPLOAD_DIR = os.path.join(tempfile.gettempdir(), "adscloud-restore")
_TOKEN_RE = "0123456789abcdef"


def _upload_path(token: str) -> str:
    if not token or len(token) != 32 or any(c not in _TOKEN_RE for c in token):
        raise ValueError("bad token")
    return os.path.join(_UPLOAD_DIR, token + ".zip")


@bp.route("/api/backup/download", methods=["GET"])
@admin_required
def api_backup_download():
    name = f"adscloud-backup-{datetime.now(timezone.utc).strftime('%Y-%m-%d-%H%M')}.zip"
    _audit("backup_download", name)
    return Response(stream_with_context(backup.stream_backup(db)), mimetype="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{name}"',
                             "X-Accel-Buffering": "no", "Cache-Control": "no-store"})


@bp.route("/api/backup/restore/upload", methods=["POST"])
@admin_required
def api_backup_restore_upload():
    """Save the uploaded backup and say what a restore would do (nothing changes yet)."""
    f = request.files.get("file")
    if not f:
        return jsonify({"error": "اختر ملف النسخة الاحتياطية"}), 400
    os.makedirs(_UPLOAD_DIR, exist_ok=True)
    token = secrets.token_hex(16)
    path = _upload_path(token)
    f.save(path)
    try:
        info = backup.inspect_backup(path, db)
    except backup.RestoreError as e:
        os.remove(path)
        return jsonify({"error": str(e)}), 400
    return jsonify(dict(info, token=token))


@bp.route("/api/backup/restore/start", methods=["POST"])
@admin_required
def api_backup_restore_start():
    c = request.get_json(force=True, silent=True) or {}
    if str(c.get("confirm") or "").strip() != "استعادة":
        return jsonify({"error": "اكتب كلمة «استعادة» للتأكيد"}), 400
    try:
        path = _upload_path(str(c.get("token") or ""))
    except ValueError:
        return jsonify({"error": "رمز غير صالح"}), 400
    if not os.path.exists(path):
        return jsonify({"error": "الملف المرفوع لم يعد موجوداً — ارفعه من جديد"}), 404
    if not backup.start_restore(path, db):
        return jsonify({"error": "استعادة أخرى قيد التنفيذ"}), 409
    _audit("backup_restore_started", c.get("token"))
    return jsonify({"ok": True})


@bp.route("/api/backup/restore/status", methods=["GET"])
@admin_required
def api_backup_restore_status():
    return jsonify(backup.restore_state())
