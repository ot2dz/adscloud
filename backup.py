# -*- coding: utf-8 -*-
"""Full backup (download) and restore of the platform's data.

The backup is a ZIP streamed while it is written, so a bank of 100k+ ads never
sits in memory:
  manifest.json          format, version, date, backend, rows per table
  tables/<name>.jsonl    one JSON object per row, every column
  ads.csv                the ads, readable in Excel (UTF-8 with BOM)

Never included: user accounts, sessions, login attempts, security events,
API tokens, and any setting whose name looks like a secret (API keys, tokens,
passwords). A restore never touches those either, so the current logins and
the current OpenRouter key keep working.

Restore = replace: every table found in the backup is emptied and refilled,
in ONE transaction (all or nothing). Tables missing from the backup are left
as they are.
"""
from __future__ import annotations

import csv
import io
import json
import logging
import re
import sqlite3
import threading
import time
import zipfile
from datetime import datetime, timezone
from typing import Iterator, Optional

logger = logging.getLogger("backup")

FORMAT = "adscloud-backup"
VERSION = 1
EXCLUDED_TABLES = {"users", "user_sessions", "auth_attempts", "security_events",
                   "api_tokens", "reset_ids"}
SECRET_SETTING = re.compile(r"(api_key|key$|token|secret|password|passwd|cookie)", re.I)
ADS_CSV_COLUMNS = ("ad_id", "page_name", "page_id", "store_domain", "link_url", "start_date",
                   "days_active", "is_active", "stopped_at", "cta_text", "primary_niche",
                   "collation_count", "query", "market", "first_seen", "collected_at", "body")
CHUNK = 2000
_FLUSH_AT = 256 * 1024


def is_secret_setting(key: str) -> bool:
    return bool(SECRET_SETTING.search(str(key or "")))


# ── raw connections (outside the pool: long reads / one big transaction) ──
def _raw_connect(db):
    if db.is_postgres:
        import psycopg2
        conn = psycopg2.connect(db.db_url)
        conn.autocommit = False
        return conn
    from db.core import _SQLITE_PATH
    return sqlite3.connect(_SQLITE_PATH)


def list_tables(db) -> list[str]:
    if db.is_postgres:
        rows = db._query("SELECT table_name AS t FROM information_schema.tables "
                         "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'")
    else:
        rows = db._query("SELECT name AS t FROM sqlite_master WHERE type = 'table' "
                         "AND name NOT LIKE 'sqlite_%'")
    return sorted(r["t"] for r in rows if r["t"] not in EXCLUDED_TABLES)


def table_columns(db, table: str) -> list[str]:
    if db.is_postgres:
        rows = db._query("SELECT column_name AS c FROM information_schema.columns "
                         "WHERE table_schema = 'public' AND table_name = %s ORDER BY ordinal_position",
                         (table,))
        return [r["c"] for r in rows]
    return [r["name"] for r in db._query(f"PRAGMA table_info({_ident(table)})")]


def _ident(name: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name or ""):
        raise ValueError(f"bad table name: {name!r}")
    return name


def iter_rows(db, table: str) -> Iterator[dict]:
    """Every row of a table as a dict, read in chunks."""
    conn = _raw_connect(db)
    try:
        if db.is_postgres:
            cur = conn.cursor(name=f"bk_{_ident(table)}_{int(time.time() * 1000)}")
            cur.itersize = CHUNK
            cur.execute(f"SELECT * FROM {_ident(table)}")
            cols = None
            for row in cur:
                if cols is None:
                    cols = [d[0] for d in cur.description]
                yield dict(zip(cols, row))
            cur.close()
        else:
            cur = conn.cursor()
            cur.execute(f"SELECT * FROM {_ident(table)}")
            cols = [d[0] for d in cur.description]
            while True:
                rows = cur.fetchmany(CHUNK)
                if not rows:
                    break
                for row in rows:
                    yield dict(zip(cols, row))
    finally:
        conn.close()


class _Sink:
    """Write-only, non-seekable stream: zipfile then writes data descriptors,
    and the bytes are handed to the HTTP response as they come."""

    def __init__(self):
        self._parts: list[bytes] = []
        self.size = 0

    def write(self, b) -> int:
        b = bytes(b)
        self._parts.append(b)
        self.size += len(b)
        return len(b)

    def flush(self) -> None:
        pass

    def take(self) -> bytes:
        out = b"".join(self._parts)
        self._parts, self.size = [], 0
        return out


def _json_line(row: dict) -> bytes:
    return (json.dumps(row, ensure_ascii=False, default=str) + "\n").encode("utf-8")


def stream_backup(db) -> Iterator[bytes]:
    """The backup ZIP, chunk by chunk."""
    sink = _Sink()
    zf = zipfile.ZipFile(sink, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True)
    counts: dict = {}
    for table in list_tables(db):
        n = 0
        with zf.open(f"tables/{table}.jsonl", "w", force_zip64=True) as f:
            for row in iter_rows(db, table):
                if table == "settings" and is_secret_setting(row.get("key")):
                    continue
                f.write(_json_line(row))
                n += 1
                if sink.size >= _FLUSH_AT:
                    yield sink.take()
        counts[table] = n
        if sink.size:
            yield sink.take()
    # Excel-friendly copy of the ads.
    with zf.open("ads.csv", "w", force_zip64=True) as f:
        f.write("﻿".encode("utf-8"))
        text = io.TextIOWrapper(f, encoding="utf-8", newline="")
        w = csv.writer(text)
        w.writerow(ADS_CSV_COLUMNS)
        if "meta_ads" in counts:
            for row in iter_rows(db, "meta_ads"):
                w.writerow([row.get(c, "") for c in ADS_CSV_COLUMNS])
                if sink.size >= _FLUSH_AT:
                    text.flush()
                    yield sink.take()
        text.flush()
        text.detach()
    manifest = {"format": FORMAT, "version": VERSION,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "backend": "postgresql" if db.is_postgres else "sqlite",
                "excluded": sorted(EXCLUDED_TABLES) + ["secret settings"],
                "tables": counts}
    zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
    zf.close()
    yield sink.take()


# ── restore ────────────────────────────────────────────────────────────────
class RestoreError(Exception):
    pass


def inspect_backup(path: str, db) -> dict:
    """Manifest + what a restore would do, without changing anything."""
    try:
        zf = zipfile.ZipFile(path)
    except zipfile.BadZipFile:
        raise RestoreError("الملف ليس نسخة احتياطية صالحة (ZIP تالف)") from None
    with zf:
        try:
            manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
        except KeyError:
            raise RestoreError("لا يوجد manifest.json: هذا ليس ملف نسخة احتياطية من المنصة") from None
        if manifest.get("format") != FORMAT or int(manifest.get("version") or 0) > VERSION:
            raise RestoreError("صيغة نسخة احتياطية غير معروفة")
        present = {n[len("tables/"):-len(".jsonl")] for n in zf.namelist()
                   if n.startswith("tables/") and n.endswith(".jsonl")}
    current = set(list_tables(db))
    tables = manifest.get("tables") or {}
    return {
        "created_at": manifest.get("created_at"),
        "backend": manifest.get("backend"),
        "tables": {t: tables.get(t, 0) for t in sorted(present)},
        "will_restore": sorted(t for t in present if t in current and t not in EXCLUDED_TABLES),
        "skipped": sorted(t for t in present if t not in current or t in EXCLUDED_TABLES),
        "untouched": sorted(current - present),
    }


_state_lock = threading.Lock()
_state: dict = {"running": False}


def restore_state() -> dict:
    with _state_lock:
        return dict(_state)


def _set_state(**kw) -> None:
    with _state_lock:
        _state.update(kw)


def restore(path: str, db) -> dict:
    """Replace the tables found in the backup, in one transaction."""
    info = inspect_backup(path, db)
    ph = "%s" if db.is_postgres else "?"
    conn = _raw_connect(db)
    done: dict = {}
    try:
        cur = conn.cursor()
        with zipfile.ZipFile(path) as zf:
            for table in info["will_restore"]:
                _set_state(table=table, rows=0)
                cols_now = set(table_columns(db, table))
                if table == "settings":
                    keep = {r[0] for r in _fetch(cur, "SELECT key FROM settings")
                            if is_secret_setting(r[0])}
                    cur.execute("DELETE FROM settings WHERE " +
                                (" AND ".join(f"key <> {ph}" for _ in keep) if keep else "1=1"),
                                tuple(keep))
                else:
                    cur.execute(f"DELETE FROM {_ident(table)}")
                n = 0
                batch: list = []
                cols: Optional[list] = None
                with zf.open(f"tables/{table}.jsonl") as f:
                    for raw in io.TextIOWrapper(f, encoding="utf-8"):
                        if not raw.strip():
                            continue
                        row = json.loads(raw)
                        if table == "settings" and is_secret_setting(row.get("key")):
                            continue
                        if cols is None:
                            cols = [c for c in row if c in cols_now]
                            sql = (f"INSERT INTO {_ident(table)} ({', '.join(_ident(c) for c in cols)}) "
                                   f"VALUES ({', '.join([ph] * len(cols))})")
                        batch.append(tuple(row.get(c) for c in cols))
                        if len(batch) >= 500:
                            cur.executemany(sql, batch)
                            n += len(batch)
                            batch = []
                            _set_state(rows=n)
                    if batch:
                        cur.executemany(sql, batch)
                        n += len(batch)
                done[table] = n
                _set_state(rows=n)
        if db.is_postgres:  # serial ids continue after the restored ones
            for table in done:
                if "id" in set(table_columns(db, table)):
                    cur.execute(f"SELECT setval(pg_get_serial_sequence('{_ident(table)}', 'id'), "
                                f"GREATEST(COALESCE((SELECT MAX(id) FROM {_ident(table)}), 0), 1))")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    # Caches and summaries built from the old data.
    for fn in ("invalidate_cache", "mark_groups_stale", "_load_niches"):
        try:
            getattr(db, fn)()
        except Exception:
            logger.exception("after restore: %s failed", fn)
    return done


def _fetch(cur, sql: str) -> list:
    cur.execute(sql)
    return cur.fetchall()


def start_restore(path: str, db) -> bool:
    """Run the restore in a background thread; False if one is running."""
    with _state_lock:
        if _state.get("running"):
            return False
        _state.clear()
        _state.update(running=True, started_at=datetime.now(timezone.utc).isoformat(),
                      table="", rows=0, error="", done=None)

    def _run():
        try:
            done = restore(path, db)
            _set_state(running=False, done=done, finished_at=datetime.now(timezone.utc).isoformat())
        except Exception as e:  # noqa: BLE001 — reported to the admin
            logger.exception("restore failed")
            _set_state(running=False, error=str(e)[:500],
                       finished_at=datetime.now(timezone.utc).isoformat())
        finally:
            try:
                import os
                os.remove(path)
            except OSError:
                pass

    threading.Thread(target=_run, name="restore", daemon=True).start()
    return True
