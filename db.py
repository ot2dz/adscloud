# -*- coding: utf-8 -*-
"""
Unified storage for collected ads.

Uses PostgreSQL when DATABASE_URL is set (production), otherwise falls back to
a local SQLite file. Both share the same query surface so callers don't care.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger("meta_ads_db")

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()

_SQLITE_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "meta_ads.sqlite")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Database:
    def __init__(self, db_url: Optional[str] = None):
        self.db_url = (db_url or DATABASE_URL).strip()
        self.is_postgres = self.db_url.startswith(("postgres://", "postgresql://"))
        if self.db_url.startswith("postgres://"):
            self.db_url = self.db_url.replace("postgres://", "postgresql://", 1)
        self._init_schema()

    # ── connection helpers ────────────────────────────────────────────────
    def _ph(self) -> str:
        return "%s" if self.is_postgres else "?"

    def _table_exists(self, name: str) -> bool:
        if self.is_postgres:
            row = self._query_one("SELECT to_regclass(%s) AS t", (f"public.{name}",))
        else:
            row = self._query_one("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,))
        return bool(row)

    def _connect(self):
        if self.is_postgres:
            import psycopg2
            conn = psycopg2.connect(self.db_url)
            conn.autocommit = True
            return conn
        conn = sqlite3.connect(_SQLITE_PATH)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        try:
            if self.is_postgres:
                self._query_one("SELECT 1")
        except Exception as exc:  # pragma: no cover
            logger.error("PostgreSQL connection failed (%s); falling back to SQLite.", exc)
            self.is_postgres = False

        conn = self._connect()
        cur = conn.cursor()
        sql = """
        CREATE TABLE IF NOT EXISTS meta_ads (
            ad_id TEXT PRIMARY KEY,
            page_name TEXT,
            page_id TEXT,
            start_date TEXT,
            is_active INTEGER,
            link_url TEXT,
            store_domain TEXT,
            platform TEXT,
            body TEXT,
            cta_text TEXT,
            collation_count INTEGER,
            publisher_platforms TEXT,
            query TEXT,
            country TEXT,
            collected_at TEXT,
            first_seen TEXT
        )
        """
        cur.execute(sql)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_meta_ads_store ON meta_ads (store_domain)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_meta_ads_query ON meta_ads (query)")

        idcol = "SERIAL PRIMARY KEY" if self.is_postgres else "INTEGER PRIMARY KEY AUTOINCREMENT"
        cur.execute(f"""
        CREATE TABLE IF NOT EXISTS jobs (
            id {idcol},
            query TEXT,
            url TEXT,
            country TEXT,
            exact_phrase INTEGER DEFAULT 0,
            stores_only INTEGER DEFAULT 0,
            sweep INTEGER DEFAULT 0,
            sort_mode TEXT DEFAULT 'total_impressions',
            status TEXT DEFAULT 'pending',
            source TEXT DEFAULT 'manual',
            worker_id TEXT,
            created_at TEXT,
            started_at TEXT,
            finished_at TEXT,
            result_count INTEGER DEFAULT 0,
            error TEXT,
            date_scope TEXT DEFAULT 'impressions'
        )
        """)
        cur.execute(f"""
        CREATE TABLE IF NOT EXISTS targets (
            id {idcol},
            type TEXT DEFAULT 'keyword',
            target_value TEXT,
            label TEXT,
            country TEXT DEFAULT 'DZ',
            enabled INTEGER DEFAULT 1,
            interval_hours REAL DEFAULT 12.0,
            last_scraped_at TEXT,
            created_at TEXT
        )
        """)
        cur.execute(f"""
        CREATE TABLE IF NOT EXISTS keywords (
            id {idcol},
            query TEXT,
            enabled INTEGER DEFAULT 1,
            created_at TEXT
        )
        """)
        cur.execute(f"""
        CREATE TABLE IF NOT EXISTS users (
            id {idcol},
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT DEFAULT 'user',
            is_active INTEGER DEFAULT 1,
            created_at TEXT,
            last_login_at TEXT
        )
        """)
        cur.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
        """)

        # ── Page tracking (browser-extension integration) ──────────────────
        cur.execute("""
        CREATE TABLE IF NOT EXISTS tracked_pages (
            page_id TEXT PRIMARY KEY,
            page_name TEXT,
            page_url TEXT,
            page_likes INTEGER DEFAULT 0,
            platform TEXT DEFAULT '',
            country TEXT DEFAULT 'DZ',
            source TEXT DEFAULT 'extension',
            enabled INTEGER DEFAULT 1,
            first_tracked_at TEXT,
            last_snapshot_at TEXT,
            active_ads_count INTEGER DEFAULT 0,
            total_ads_count INTEGER DEFAULT 0,
            snapshots_count INTEGER DEFAULT 0
        )
        """)
        cur.execute(f"""
        CREATE TABLE IF NOT EXISTS page_snapshots (
            id {idcol},
            page_id TEXT,
            captured_at TEXT,
            active_ads_count INTEGER DEFAULT 0,
            total_ads_count INTEGER DEFAULT 0,
            ad_ids TEXT,
            source TEXT DEFAULT 'extension'
        )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_snapshots_page ON page_snapshots (page_id)")

        # ── Per-user ownership: who saved which ad / tracks which page ─────
        # Ads and pages stay global (one scrape serves everyone); these link
        # tables decide what each user sees.
        cur.execute("""
        CREATE TABLE IF NOT EXISTS user_ads (
            user_id INTEGER NOT NULL,
            ad_id TEXT NOT NULL,
            saved_at TEXT,
            PRIMARY KEY (user_id, ad_id)
        )
        """)
        cur.execute("""
        CREATE TABLE IF NOT EXISTS user_pages (
            user_id INTEGER NOT NULL,
            page_id TEXT NOT NULL,
            tracked_at TEXT,
            PRIMARY KEY (user_id, page_id)
        )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_user_pages_page ON user_pages (page_id)")
        conn.commit()

        # Idempotent migrations for the jobs table (progress + stop control + incremental + date filters).
        for col, ddl in [
            ("progress", "INTEGER DEFAULT 0"),
            ("progress_note", "TEXT DEFAULT ''"),
            ("stop_requested", "INTEGER DEFAULT 0"),
            ("url", "TEXT DEFAULT ''"),
            ("sweep", "INTEGER DEFAULT 0"),
            ("target_id", "INTEGER"),
            ("incremental", "INTEGER DEFAULT 0"),
            ("start_date_min", "TEXT"),
            ("start_date_max", "TEXT"),
            ("date_scope", "TEXT DEFAULT 'impressions'"),
            # Resume support: JSON {"pass": n, "cursor": "..."} of the next
            # unfetched page, retry bookkeeping and a liveness heartbeat.
            ("checkpoint", "TEXT"),
            ("attempts", "INTEGER DEFAULT 0"),
            ("retry_at", "TEXT"),
            ("heartbeat_at", "TEXT"),
        ]:
            try:
                if self.is_postgres:
                    cur.execute(f"ALTER TABLE jobs ADD COLUMN IF NOT EXISTS {col} {ddl}")
                else:
                    cols = [r[1] for r in cur.execute("PRAGMA table_info(jobs)").fetchall()]
                    if col not in cols:
                        cur.execute(f"ALTER TABLE jobs ADD COLUMN {col} {ddl}")
            except Exception:
                pass
        # Migrations for meta_ads (lifecycle & preview fields) and keywords (type).
        for table, col, ddl in [
            ("meta_ads", "first_seen", "TEXT"),
            ("meta_ads", "last_seen_active", "TEXT"),
            ("meta_ads", "days_active", "INTEGER DEFAULT 1"),
            ("meta_ads", "target_id", "INTEGER"),
            ("meta_ads", "preview_image_url", "TEXT"),
            ("keywords", "type", "TEXT DEFAULT 'keyword'"),
            ("keywords", "label", "TEXT DEFAULT ''"),
            ("targets", "country", "TEXT DEFAULT 'DZ'"),
            ("targets", "interval_hours", "REAL DEFAULT 12.0"),
            ("targets", "last_scraped_at", "TEXT"),
            ("users", "api_token", "TEXT"),
        ]:
            try:
                if self.is_postgres:
                    cur.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {col} {ddl}")
                else:
                    cols = [r[1] for r in cur.execute(f"PRAGMA table_info({table})").fetchall()]
                    if col not in cols:
                        cur.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
            except Exception:
                pass
        # Seed the admin user from environment variables only (never hardcode a
        # password in the repository). Without ADMIN_PASSWORD no admin is seeded.
        try:
            from werkzeug.security import generate_password_hash
            admin_email = (os.environ.get("ADMIN_EMAIL") or "must2dz@gmail.com").strip().lower()
            admin_password = (os.environ.get("ADMIN_PASSWORD") or "").strip()
            p = "%s" if self.is_postgres else "?"
            cur.execute(f"SELECT id FROM users WHERE email = {p}", (admin_email,))
            existing_admin = cur.fetchone()
            if not existing_admin and admin_password:
                hashed = generate_password_hash(admin_password)
                now_str = datetime.now(timezone.utc).isoformat()
                cur.execute(
                    f"INSERT INTO users (email, password_hash, role, is_active, created_at) VALUES ({p},{p},'admin',1,{p})",
                    (admin_email, hashed, now_str)
                )
            elif existing_admin:
                cur.execute(f"UPDATE users SET role='admin', is_active=1 WHERE email={p}", (admin_email,))
        except Exception as admin_err:
            logger.warning("Admin auto-seed notice: %s", admin_err)

        conn.commit()
        conn.close()

    # ── low-level helpers ─────────────────────────────────────────────────
    def _execute(self, sql: str, params: tuple = ()) -> None:
        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute(sql, params)
            conn.commit()
        finally:
            conn.close()

    def _query(self, sql: str, params: tuple = ()) -> list[dict]:
        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute(sql, params)
            rows = cur.fetchall()
            if self.is_postgres:
                cols = [c[0] for c in cur.description]
                return [dict(zip(cols, r)) for r in rows]
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def _query_one(self, sql: str, params: tuple = ()) -> Optional[dict]:
        rows = self._query(sql, params)
        return rows[0] if rows else None

    # ── per-user scoping ──────────────────────────────────────────────────
    def _user_scope_sql(self) -> str:
        """WHERE fragment (2 params: user_id, user_id) for the ads a user sees:
        the ads they saved plus every ad of the pages they track."""
        p = self._ph()
        return (f"(ad_id IN (SELECT ad_id FROM user_ads WHERE user_id = {p}) "
                f"OR page_id IN (SELECT page_id FROM user_pages WHERE user_id = {p}))")

    def link_user_ad(self, user_id: int, ad_id: str) -> None:
        aid = str(ad_id or "").strip()
        if not user_id or not aid:
            return
        p = self._ph()
        conflict = "ON CONFLICT (user_id, ad_id) DO NOTHING"
        self._execute(f"INSERT INTO user_ads (user_id, ad_id, saved_at) VALUES ({p},{p},{p}) {conflict}",
                      (int(user_id), aid, _now()))

    def link_user_page(self, user_id: int, page_id: str) -> None:
        pid = str(page_id or "").strip()
        if not user_id or not pid:
            return
        p = self._ph()
        conflict = "ON CONFLICT (user_id, page_id) DO NOTHING"
        self._execute(f"INSERT INTO user_pages (user_id, page_id, tracked_at) VALUES ({p},{p},{p}) {conflict}",
                      (int(user_id), pid, _now()))

    def unlink_user_page(self, user_id: int, page_id: str) -> int:
        """Remove one user's tracking of a page. Returns how many users still track it."""
        pid = str(page_id or "").strip()
        p = self._ph()
        self._execute(f"DELETE FROM user_pages WHERE user_id={p} AND page_id={p}", (int(user_id), pid))
        return self.count_page_trackers(pid)

    def count_page_trackers(self, page_id: str) -> int:
        row = self._query_one(f"SELECT COUNT(*) AS c FROM user_pages WHERE page_id={self._ph()}",
                              (str(page_id or "").strip(),))
        return int(row["c"]) if row and row.get("c") else 0

    def list_user_page_ids(self, user_id: int) -> list[str]:
        rows = self._query(f"SELECT page_id FROM user_pages WHERE user_id={self._ph()}", (int(user_id),))
        return [str(r["page_id"]) for r in rows]

    # ── public API ────────────────────────────────────────────────────────
    def _calc_days_active(self, start_date_str: str, first_seen_str: str) -> int:
        """Calculate how many days the ad has been running."""
        ref_date = None
        # Try parsing start_date
        if start_date_str:
            try:
                # check if numeric epoch timestamp
                if start_date_str.replace(".", "", 1).isdigit():
                    sec = float(start_date_str)
                    if sec > 1e11:  # milliseconds
                        sec /= 1000.0
                    ref_date = datetime.fromtimestamp(sec, tz=timezone.utc).date()
                else:
                    clean = start_date_str[:10]
                    ref_date = datetime.strptime(clean, "%Y-%m-%d").date()
            except Exception:
                pass
        if not ref_date and first_seen_str:
            try:
                ref_date = datetime.fromisoformat(first_seen_str).date()
            except Exception:
                pass
        if not ref_date:
            return 1
        today = datetime.now(timezone.utc).date()
        diff = (today - ref_date).days
        return max(1, diff + 1)

    def save_ad(self, ad: dict[str, Any], query: str = "", country: str = "DZ",
                target_id: Optional[int] = None) -> bool:
        """Upsert an ad with lifecycle tracking. Returns True if the ad is NEW."""
        p = self._ph()
        ad_id = str(ad.get("ad_id") or "")
        if not ad_id:
            return False

        existing = self._query_one(f"SELECT first_seen, start_date FROM meta_ads WHERE ad_id={p}", (ad_id,))
        is_new = existing is None
        now = _now()
        first_seen = existing["first_seen"] if existing and existing.get("first_seen") else now
        start_date = str(ad.get("start_date") or (existing.get("start_date") if existing else "") or "")
        days_active = self._calc_days_active(start_date, first_seen)

        preview_img = str(ad.get("preview_image_url") or "").strip()
        cols = ("ad_id", "page_name", "page_id", "start_date", "is_active", "link_url",
                "store_domain", "platform", "body", "cta_text", "collation_count",
                "publisher_platforms", "query", "country", "collected_at", "first_seen",
                "last_seen_active", "days_active", "target_id", "preview_image_url")
        values = (
            ad_id,
            ad.get("page_name") or "",
            str(ad.get("page_id") or ""),
            start_date,
            1 if ad.get("is_active", True) else 0,
            ad.get("link_url") or "",
            ad.get("store_domain") or "",
            ad.get("platform") or "",
            ad.get("body") or "",
            ad.get("cta_text") or "",
            int(ad.get("collation_count") or 1),
            json.dumps(ad.get("publisher_platforms") or [], ensure_ascii=False),
            query,
            country,
            now,
            first_seen,
            now,  # last_seen_active
            days_active,
            target_id,
            preview_img,
        )
        placeholders = ", ".join([p] * len(cols))
        col_list = ", ".join(cols)
        conn = self._connect()
        try:
            cur = conn.cursor()
            # first_seen is preserved on updates
            if self.is_postgres:
                updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in cols if c not in ("ad_id", "first_seen"))
                sql = (f"INSERT INTO meta_ads ({col_list}) VALUES ({placeholders}) "
                       f"ON CONFLICT (ad_id) DO UPDATE SET {updates}")
            else:
                updates = ", ".join(f"{c} = excluded.{c}" for c in cols if c not in ("ad_id", "first_seen"))
                sql = (f"INSERT INTO meta_ads ({col_list}) VALUES ({placeholders}) "
                       f"ON CONFLICT(ad_id) DO UPDATE SET {updates}")
            cur.execute(sql, values)
            conn.commit()
            return is_new
        except Exception as exc:
            logger.error("save_ad failed: %s", exc)
            return False
        finally:
            conn.close()

    def ad_exists(self, ad_id: str) -> bool:
        return self._query_one(f"SELECT 1 AS x FROM meta_ads WHERE ad_id={self._ph()}", (str(ad_id),)) is not None

    def _build_ads_filter(self, search: str = "", stores_only: bool = False,
                           query: str = "", store_domain: str = "",
                           page_id: str = "", preset: str = "", active_only: bool = False,
                           start_date_min: str = "", start_date_max: str = "",
                           min_days_active: int = 0, target_id: Optional[int] = None,
                           user_id: Optional[int] = None) -> tuple[str, list]:
        p = self._ph()
        where = []
        params: list = []

        if user_id is not None:
            where.append(self._user_scope_sql())
            params.extend([int(user_id), int(user_id)])

        if stores_only:
            where.append("store_domain != '' AND store_domain IS NOT NULL")
        if page_id:
            where.append(f"(page_id = {p} OR query = {p})")
            params.extend([page_id, page_id])
        elif store_domain:
            where.append(f"(store_domain = {p} OR query = {p})")
            params.extend([store_domain, store_domain])
        elif query:
            where.append(f"query = {p}")
            params.append(query)
        if target_id is not None and not (page_id or store_domain or query):
            where.append(f"target_id = {p}")
            params.append(target_id)
        if active_only or preset == "active":
            where.append("is_active = 1")
        elif preset == "stopped":
            where.append("is_active = 0")
        elif preset == "winning":
            where.append("(days_active >= 5 OR collation_count >= 3) AND is_active = 1")
        elif preset == "new_launches":
            where.append("days_active <= 3 AND is_active = 1")

        if min_days_active > 0:
            where.append(f"days_active >= {p}")
            params.append(min_days_active)

        if start_date_min:
            where.append(f"start_date >= {p}")
            params.append(start_date_min)
        if start_date_max:
            where.append(f"start_date <= {p}")
            params.append(start_date_max)

        if search:
            like = "%" + search + "%"
            where.append(f"(body LIKE {p} OR page_name LIKE {p} OR store_domain LIKE {p})")
            params.extend([like, like, like])

        clause = ("WHERE " + " AND ".join(where)) if where else ""
        return clause, params

    def get_ads(self, limit: int = 100, offset: int = 0, search: str = "",
                stores_only: bool = False, query: str = "", store_domain: str = "",
                page_id: str = "", preset: str = "", active_only: bool = False,
                start_date_min: str = "", start_date_max: str = "",
                min_days_active: int = 0, sort_by: str = "collected_at",
                target_id: Optional[int] = None, user_id: Optional[int] = None) -> list[dict]:
        p = self._ph()
        clause, params = self._build_ads_filter(
            search=search, stores_only=stores_only, query=query,
            store_domain=store_domain, page_id=page_id, preset=preset,
            active_only=active_only, start_date_min=start_date_min,
            start_date_max=start_date_max, min_days_active=min_days_active,
            target_id=target_id, user_id=user_id,
        )

        order_map = {
            "longest_running": "days_active DESC, is_active DESC",
            "newest_launch": "start_date DESC",
            "most_collated": "collation_count DESC",
            "collected_at": "collected_at DESC",
        }
        order_clause = order_map.get(sort_by, "collected_at DESC")

        sql = f"SELECT * FROM meta_ads {clause} ORDER BY {order_clause} LIMIT {p} OFFSET {p}"
        params.extend([limit, offset])
        rows = self._query(sql, tuple(params))
        for r in rows:
            try:
                r["publisher_platforms"] = json.loads(r.get("publisher_platforms") or "[]")
            except Exception:
                r["publisher_platforms"] = []
        return rows

    def count_ads(self, search: str = "", stores_only: bool = False,
                  query: str = "", store_domain: str = "",
                  page_id: str = "", preset: str = "", active_only: bool = False,
                  start_date_min: str = "", start_date_max: str = "",
                  min_days_active: int = 0, target_id: Optional[int] = None,
                  user_id: Optional[int] = None) -> int:
        clause, params = self._build_ads_filter(
            search=search, stores_only=stores_only, query=query,
            store_domain=store_domain, page_id=page_id, preset=preset,
            active_only=active_only, start_date_min=start_date_min,
            start_date_max=start_date_max, min_days_active=min_days_active,
            target_id=target_id, user_id=user_id,
        )
        row = self._query_one(f"SELECT COUNT(*) AS c FROM meta_ads {clause}", tuple(params))
        return int(row["c"]) if row else 0

    def get_queries(self) -> list[dict]:
        sql = ("SELECT query, COUNT(*) AS total_ads, "
               "SUM(CASE WHEN store_domain != '' AND store_domain IS NOT NULL THEN 1 ELSE 0 END) AS store_ads, "
               "MAX(collected_at) AS last_run FROM meta_ads "
               "WHERE query != '' GROUP BY query ORDER BY last_run DESC LIMIT 100")
        return self._query(sql)

    def get_stores(self, search: str = "", user_id: Optional[int] = None) -> list[dict]:
        p = self._ph()
        where = ["store_domain != '' AND store_domain IS NOT NULL"]
        params: list = []
        if search:
            where.append(f"store_domain LIKE {p}")
            params.append("%" + search + "%")
        if user_id is not None:
            where.append(self._user_scope_sql())
            params.extend([int(user_id), int(user_id)])
        sql = (f"SELECT store_domain, platform, COUNT(*) AS total_ads, "
               f"MAX(collected_at) AS last_seen FROM meta_ads "
               f"WHERE {' AND '.join(where)} "
               f"GROUP BY store_domain, platform ORDER BY total_ads DESC LIMIT 300")
        return self._query(sql, tuple(params))

    def get_stats(self, user_id: Optional[int] = None) -> dict:
        p = self._ph()
        total = self.count_ads(user_id=user_id)
        scope, scope_params = "", ()
        if user_id is not None:
            scope = " AND " + self._user_scope_sql()
            scope_params = (int(user_id), int(user_id))
        stores = self._query_one(
            "SELECT COUNT(DISTINCT store_domain) AS c FROM meta_ads "
            "WHERE store_domain != '' AND store_domain IS NOT NULL" + scope, scope_params)
        last = self._query_one("SELECT MAX(collected_at) AS t FROM meta_ads WHERE 1=1" + scope, scope_params)
        if user_id is not None:
            tracked = self._query_one(f"SELECT COUNT(*) AS c FROM user_pages WHERE user_id={p}", (int(user_id),))
        else:
            tracked = self._query_one("SELECT COUNT(*) AS c FROM tracked_pages")
        return {
            "backend": "PostgreSQL" if self.is_postgres else "SQLite",
            "total_ads": total,
            "total_stores": int(stores["c"]) if stores and stores.get("c") else 0,
            "total_tracked_pages": int(tracked["c"]) if tracked and tracked.get("c") else 0,
            "last_collection": last["t"] if last else None,
        }

    def clear_ads(self) -> None:
        self._execute("DELETE FROM meta_ads")

    # ── jobs (remote scrape queue) ────────────────────────────────────────
    def get_known_ad_ids(self, query: str = "", store_domain: str = "",
                         page_id: str = "", limit: int = 150) -> list[str]:
        """Fetch recently stored ad IDs for early stopping."""
        p = self._ph()
        where = []
        params = []
        if query:
            where.append(f"query = {p}")
            params.append(query)
        if store_domain:
            where.append(f"store_domain = {p}")
            params.append(store_domain)
        if page_id:
            where.append(f"page_id = {p}")
            params.append(page_id)
        clause = ("WHERE " + " AND ".join(where)) if where else ""
        sql = f"SELECT ad_id FROM meta_ads {clause} ORDER BY collected_at DESC LIMIT {p}"
        params.append(limit)
        rows = self._query(sql, tuple(params))
        return [r["ad_id"] for r in rows if r.get("ad_id")]

    def mark_page_inactive_ads(self, page_id: str, active_ad_ids: list[str]) -> int:
        """Mark ads that disappeared from a monitored page as inactive."""
        if not page_id or not active_ad_ids:
            return 0
        p = self._ph()
        placeholders = ", ".join([p] * len(active_ad_ids))
        sql = f"UPDATE meta_ads SET is_active = 0 WHERE page_id = {p} AND ad_id NOT IN ({placeholders})"
        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute(sql, (page_id, *active_ad_ids))
            count = cur.rowcount if hasattr(cur, "rowcount") else 0
            conn.commit()
            return max(0, count)
        except Exception:
            return 0
        finally:
            conn.close()

    def create_job(self, query: str, country: str = "DZ", exact_phrase: bool = False,
                   stores_only: bool = False, sort_mode: str = "total_impressions",
                   source: str = "manual", url: str = "", sweep: bool = False,
                   target_id: Optional[int] = None, incremental: bool = False,
                   start_date_min: str = "", start_date_max: str = "",
                   date_scope: str = "impressions") -> int:
        p = self._ph()
        conn = self._connect()
        try:
            cur = conn.cursor()
            cols = ("query", "url", "country", "exact_phrase", "stores_only", "sweep",
                    "sort_mode", "status", "source", "created_at", "target_id", "incremental",
                    "start_date_min", "start_date_max", "date_scope")
            vals = (query, url, country, int(exact_phrase), int(stores_only), int(sweep),
                    sort_mode, "pending", source, _now(), target_id, int(incremental),
                    start_date_min, start_date_max, date_scope or "impressions")
            placeholders = ", ".join([p] * len(cols))
            col_list = ", ".join(cols)
            if self.is_postgres:
                cur.execute(
                    f"INSERT INTO jobs ({col_list}) VALUES ({placeholders}) RETURNING id", vals)
                new_id = cur.fetchone()[0]
            else:
                cur.execute(
                    f"INSERT INTO jobs ({col_list}) VALUES ({placeholders})", vals)
                new_id = cur.lastrowid
            conn.commit()
            return int(new_id)
        finally:
            conn.close()

    def list_jobs(self, limit: int = 50) -> list[dict]:
        return self._query(f"SELECT * FROM jobs ORDER BY id DESC LIMIT {limit}")

    def claim_next_job(self, worker_id: str) -> Optional[dict]:
        """Atomically mark the oldest pending job as running and return it."""
        p = self._ph()
        conn = self._connect()
        job = None
        try:
            cur = conn.cursor()
            if self.is_postgres:
                now = _now()
                cur.execute(
                    "UPDATE jobs SET status='running', worker_id=%s, started_at=%s, heartbeat_at=%s "
                    "WHERE id = (SELECT id FROM jobs WHERE status='pending' "
                    "AND (retry_at IS NULL OR retry_at <= %s) "
                    "ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED) "
                    "RETURNING *", (worker_id, now, now, now))
                row = cur.fetchone()
                if row:
                    cols = [c[0] for c in cur.description]
                    job = dict(zip(cols, row))
            else:
                now = _now()
                cur.execute("SELECT id FROM jobs WHERE status='pending' "
                            "AND (retry_at IS NULL OR retry_at <= ?) ORDER BY id LIMIT 1", (now,))
                r = cur.fetchone()
                if r:
                    jid = r[0]
                    cur.execute("UPDATE jobs SET status='running', worker_id=?, started_at=?, heartbeat_at=? "
                                "WHERE id=?", (worker_id, now, now, jid))
                    conn.commit()
                    job = self._query_one("SELECT * FROM jobs WHERE id=?", (jid,))
        finally:
            conn.close()

        if job and job.get("incremental"):
            # Provide recently known ad_ids for smart early stopping
            q = job.get("query") or ""
            job["known_ad_ids"] = self.get_known_ad_ids(query=q, limit=150)
        return job

    def finish_job(self, job_id: int, status: str = "done", result_count: int = 0,
                   error: str = "", checkpoint: Optional[str] = None) -> None:
        """Close a job. ``checkpoint`` is kept on failure so it can be resumed."""
        p = self._ph()
        self._execute(
            f"UPDATE jobs SET status={p}, finished_at={p}, result_count={p}, error={p}, "
            f"checkpoint={p}, retry_at=NULL WHERE id={p}",
            (status, _now(), result_count, error, checkpoint, job_id))

    def save_checkpoint(self, job_id: int, checkpoint: Optional[str],
                        result_count: Optional[int] = None) -> None:
        """Persist the resume point and refresh the job heartbeat."""
        p = self._ph()
        if result_count is None:
            self._execute(f"UPDATE jobs SET checkpoint={p}, heartbeat_at={p} WHERE id={p}",
                          (checkpoint, _now(), job_id))
        else:
            self._execute(
                f"UPDATE jobs SET checkpoint={p}, heartbeat_at={p}, result_count={p} WHERE id={p}",
                (checkpoint, _now(), result_count, job_id))

    def schedule_retry(self, job_id: int, delay_minutes: float, checkpoint: Optional[str],
                       result_count: int = 0, error: str = "") -> None:
        """Put an interrupted job back in the queue, to resume after a delay."""
        from datetime import timedelta
        p = self._ph()
        retry_at = (datetime.now(timezone.utc) + timedelta(minutes=delay_minutes)).isoformat()
        self._execute(
            f"UPDATE jobs SET status='pending', worker_id=NULL, started_at=NULL, "
            f"attempts=COALESCE(attempts, 0) + 1, retry_at={p}, checkpoint={p}, "
            f"result_count={p}, error={p} WHERE id={p}",
            (retry_at, checkpoint, result_count, error, job_id))

    def release_job(self, job_id: int) -> None:
        """Hand a claimed-but-not-started job back to the queue untouched."""
        p = self._ph()
        self._execute(
            f"UPDATE jobs SET status='pending', worker_id=NULL, started_at=NULL WHERE id={p}",
            (job_id,))

    def update_job_progress(self, job_id: int, progress: int, note: str = "") -> None:
        p = self._ph()
        self._execute(
            f"UPDATE jobs SET progress={p}, progress_note={p} WHERE id={p}",
            (progress, note, job_id))

    def request_stop(self, job_id: int) -> None:
        self._execute(f"UPDATE jobs SET stop_requested={self._ph()} WHERE id={self._ph()}", (1, job_id))

    def get_job(self, job_id: int) -> Optional[dict]:
        return self._query_one(f"SELECT * FROM jobs WHERE id={self._ph()}", (job_id,))

    def requeue_stale_jobs(self, minutes: int = 20) -> int:
        """Return 'running' jobs with no heartbeat for too long back to 'pending'.

        Long healthy jobs keep refreshing ``heartbeat_at`` so they are never
        requeued (and re-run) while still working. Requeued jobs keep their
        checkpoint and resume where they stopped.
        """
        from datetime import timedelta
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()
        p = self._ph()
        stale = self._query(
            f"SELECT id FROM jobs WHERE status={p} AND started_at IS NOT NULL "
            f"AND COALESCE(heartbeat_at, started_at) < {p}",
            ("running", cutoff))
        for row in stale:
            self._execute(
                f"UPDATE jobs SET status={p}, worker_id=NULL, started_at=NULL WHERE id={p}",
                ("pending", row["id"]))
        return len(stale)

    def has_pending_jobs(self) -> bool:
        """True if a job is waiting to run now (not one scheduled for later)."""
        p = self._ph()
        return bool(self._query_one(
            f"SELECT id FROM jobs WHERE status='pending' AND (retry_at IS NULL OR retry_at <= {p}) LIMIT 1",
            (_now(),)))

    def cancel_pending_jobs(self, source: str) -> int:
        """Cancel queued jobs of a source (incl. ones waiting to resume)."""
        p = self._ph()
        rows = self._query(f"SELECT id FROM jobs WHERE status='pending' AND source={p}", (source,))
        for row in rows:
            self._execute(
                f"UPDATE jobs SET status='stopped', finished_at={p}, retry_at=NULL WHERE id={p}",
                (_now(), row["id"]))
        return len(rows)

    def has_open_job(self, target_id: Optional[int]) -> bool:
        """True if the target already has a pending or running job."""
        if target_id is None:
            return False
        p = self._ph()
        return bool(self._query_one(
            f"SELECT id FROM jobs WHERE target_id={p} AND status IN ('pending', 'running') LIMIT 1",
            (target_id,)))

    def requeue_orphaned_jobs(self, worker_prefix: str) -> int:
        """At startup: jobs left 'running' by a previous process of this
        worker (crash / redeploy) go straight back to the queue to resume."""
        p = self._ph()
        rows = self._query(
            f"SELECT id FROM jobs WHERE status='running' AND worker_id LIKE {p}",
            (worker_prefix + "%",))
        for row in rows:
            self.release_job(row["id"])
        return len(rows)

    # ── targets (Pages, Domains, Keywords, Custom URLs) ───────────────────
    def add_target(self, type: str, target_value: str, label: str = "",
                   country: str = "DZ", interval_hours: float = 12.0) -> bool:
        val = (target_value or "").strip()
        typ = (type or "keyword").strip().lower()
        if not val:
            return False
        p = self._ph()
        if self._query_one(f"SELECT id FROM targets WHERE type={p} AND target_value={p}", (typ, val)):
            return False
        self._execute(
            f"INSERT INTO targets (type, target_value, label, country, enabled, interval_hours, created_at) "
            f"VALUES ({p},{p},{p},{p},1,{p},{p})",
            (typ, val, label or val, country or "DZ", float(interval_hours or 12.0), _now()))
        return True

    def list_targets(self) -> list[dict]:
        # Migrate any old keywords to targets if targets is empty
        if not self._query_one("SELECT 1 FROM targets LIMIT 1"):
            old_kws = self._query("SELECT * FROM keywords")
            for k in old_kws:
                t_type = k.get("type") or "keyword"
                self.add_target(type=t_type, target_value=k.get("query", ""),
                                label=k.get("label", ""), country="DZ")
        targets = self._query("SELECT * FROM targets ORDER BY id DESC")
        p = self._ph()
        for t in targets:
            typ = t.get("type") or "keyword"
            val = t.get("target_value") or ""
            tid = t.get("id")
            if typ == "page":
                row = self._query_one(f"SELECT COUNT(*) AS c FROM meta_ads WHERE page_id = {p} OR target_id = {p}", (val, tid))
            elif typ == "domain":
                row = self._query_one(f"SELECT COUNT(*) AS c FROM meta_ads WHERE store_domain = {p} OR target_id = {p}", (val, tid))
            else:
                row = self._query_one(f"SELECT COUNT(*) AS c FROM meta_ads WHERE query = {p} OR target_id = {p}", (val, tid))
            t["ads_count"] = int(row["c"]) if row and row.get("c") else 0
        return targets

    def delete_target(self, target_id: int) -> None:
        self._execute(f"DELETE FROM targets WHERE id={self._ph()}", (target_id,))

    def toggle_target(self, target_id: int, enabled: bool) -> None:
        self._execute(f"UPDATE targets SET enabled={self._ph()} WHERE id={self._ph()}",
                      (1 if enabled else 0, target_id))

    def update_target_scraped(self, target_id: int) -> None:
        self._execute(f"UPDATE targets SET last_scraped_at={self._ph()} WHERE id={self._ph()}",
                      (_now(), target_id))

    # ── tracked pages (browser-extension integration) ─────────────────────
    def upsert_tracked_page(self, page_id, page_name: str = "", page_url: str = "",
                            page_likes: int = 0, platform: str = "", country: str = "DZ",
                            source: str = "extension") -> bool:
        """Insert or update a tracked page. Returns True when newly created."""
        pid = str(page_id or "").strip()
        if not pid:
            return False
        p = self._ph()
        existing = self._query_one(f"SELECT page_id FROM tracked_pages WHERE page_id={p}", (pid,))
        now = _now()
        if existing is None:
            self._execute(
                f"INSERT INTO tracked_pages (page_id, page_name, page_url, page_likes, platform, "
                f"country, source, enabled, first_tracked_at) "
                f"VALUES ({p},{p},{p},{p},{p},{p},{p},1,{p})",
                (pid, page_name or "", page_url or "", int(page_likes or 0),
                 platform or "", country or "DZ", source or "extension", now))
            is_new = True
        else:
            is_new = False
        updates, params = [], []
        if page_name:
            updates.append(f"page_name={p}"); params.append(page_name)
        if page_url:
            updates.append(f"page_url={p}"); params.append(page_url)
        if page_likes:
            updates.append(f"page_likes={p}"); params.append(int(page_likes))
        if platform:
            updates.append(f"platform={p}"); params.append(platform)
        if updates:
            params.append(pid)
            self._execute(f"UPDATE tracked_pages SET {', '.join(updates)} WHERE page_id={p}", tuple(params))
        return is_new

    def untrack_page(self, page_id) -> None:
        pid = str(page_id or "").strip()
        if not pid:
            return
        p = self._ph()
        self._execute(f"DELETE FROM tracked_pages WHERE page_id={p}", (pid,))
        self._execute(f"DELETE FROM page_snapshots WHERE page_id={p}", (pid,))
        self._execute(f"DELETE FROM user_pages WHERE page_id={p}", (pid,))

    def delete_all_tracked_pages(self) -> None:
        self._execute("DELETE FROM tracked_pages")
        self._execute("DELETE FROM page_snapshots")
        self._execute("DELETE FROM user_pages")

    def add_page_snapshot(self, page_id, active_ads_count: int = 0, total_ads_count: int = 0,
                          ad_ids=None, source: str = "extension") -> None:
        """Store one observation of a page and refresh its denormalized counters."""
        pid = str(page_id or "").strip()
        if not pid:
            return
        p = self._ph()
        now = _now()
        self._execute(
            f"INSERT INTO page_snapshots (page_id, captured_at, active_ads_count, total_ads_count, "
            f"ad_ids, source) VALUES ({p},{p},{p},{p},{p},{p})",
            (pid, now, int(active_ads_count or 0), int(total_ads_count or 0),
             json.dumps(ad_ids or [], ensure_ascii=False), source or "extension"))
        self._execute(
            f"UPDATE tracked_pages SET last_snapshot_at={p}, active_ads_count={p}, total_ads_count={p}, "
            f"snapshots_count=(SELECT COUNT(*) FROM page_snapshots WHERE page_id={p}) WHERE page_id={p}",
            (now, int(active_ads_count or 0), int(total_ads_count or 0), pid, pid))

    def list_tracked_pages(self, limit: int = 500, user_id: Optional[int] = None) -> list[dict]:
        """All tracked pages, or only those one user tracks. Each page lists the
        emails of the users tracking it (``tracked_by``)."""
        p = self._ph()
        if user_id is not None:
            pages = self._query(
                f"SELECT * FROM tracked_pages WHERE page_id IN "
                f"(SELECT page_id FROM user_pages WHERE user_id={p}) "
                f"ORDER BY COALESCE(last_snapshot_at, first_tracked_at) DESC LIMIT {p}",
                (int(user_id), limit))
        else:
            pages = self._query(
                f"SELECT * FROM tracked_pages ORDER BY COALESCE(last_snapshot_at, first_tracked_at) DESC LIMIT {p}",
                (limit,))
        for pg in pages:
            owners = self._query(
                f"SELECT u.email FROM user_pages up JOIN users u ON u.id = up.user_id "
                f"WHERE up.page_id={p} ORDER BY up.tracked_at", (pg["page_id"],))
            pg["tracked_by"] = [o["email"] for o in owners]
            snaps = self._query(
                f"SELECT captured_at, active_ads_count, total_ads_count FROM page_snapshots "
                f"WHERE page_id={p} ORDER BY id DESC LIMIT 20", (pg["page_id"],))
            pg["snapshots"] = list(reversed(snaps))
            row = self._query_one(f"SELECT COUNT(*) AS c FROM meta_ads WHERE page_id={p}", (pg["page_id"],))
            pg["ads_count"] = int(row["c"]) if row and row.get("c") else 0
        return pages

    def get_page_snapshots(self, page_id, limit: int = 100) -> list[dict]:
        p = self._ph()
        rows = self._query(
            f"SELECT * FROM page_snapshots WHERE page_id={p} ORDER BY id DESC LIMIT {p}",
            (str(page_id), limit))
        for r in rows:
            try:
                r["ad_ids"] = json.loads(r.get("ad_ids") or "[]")
            except Exception:
                r["ad_ids"] = []
        return rows


    # Compatibility wrappers for old keyword routes
    def add_keyword(self, query: str, type: str = "keyword", label: str = "") -> bool:
        return self.add_target(type=type, target_value=query, label=label)

    def list_keywords(self) -> list[dict]:
        targets = self.list_targets()
        return [{"id": t["id"], "query": t["target_value"], "type": t["type"],
                 "label": t["label"], "enabled": t["enabled"]} for t in targets]

    def delete_keyword(self, kw_id: int) -> None:
        self.delete_target(kw_id)

    def toggle_keyword(self, kw_id: int, enabled: bool) -> None:
        self.toggle_target(kw_id, enabled)

    # ── settings (schedule) ───────────────────────────────────────────────
    # ── users & authentication ───────────────────────────────────────────
    def create_user(self, email: str, password_hash: str, role: str = "user", is_active: int = 1) -> Optional[int]:
        p = self._ph()
        conn = self._connect()
        try:
            cur = conn.cursor()
            cols = ("email", "password_hash", "role", "is_active", "created_at")
            vals = (email.strip().lower(), password_hash, role, int(is_active), _now())
            placeholders = ", ".join([p] * len(cols))
            col_list = ", ".join(cols)
            if self.is_postgres:
                cur.execute(f"INSERT INTO users ({col_list}) VALUES ({placeholders}) RETURNING id", vals)
                new_id = cur.fetchone()[0]
            else:
                cur.execute(f"INSERT INTO users ({col_list}) VALUES ({placeholders})", vals)
                new_id = cur.lastrowid
            conn.commit()
            return int(new_id)
        except Exception as exc:
            logger.error("create_user failed: %s", exc)
            return None
        finally:
            conn.close()

    def get_user_by_email(self, email: str) -> Optional[dict]:
        return self._query_one(f"SELECT * FROM users WHERE email = {self._ph()}", (email.strip().lower(),))

    def get_user_by_id(self, user_id: int) -> Optional[dict]:
        return self._query_one(f"SELECT * FROM users WHERE id = {self._ph()}", (int(user_id),))

    def list_users(self) -> list[dict]:
        return self._query(
            "SELECT id, email, role, is_active, created_at, last_login_at, "
            "(SELECT COUNT(*) FROM user_ads ua WHERE ua.user_id = users.id) AS saved_ads_count, "
            "(SELECT COUNT(*) FROM user_pages up WHERE up.user_id = users.id) AS tracked_pages_count "
            "FROM users ORDER BY id ASC")

    def get_or_create_api_token(self, user_id: int) -> str:
        """Personal token the browser extension sends instead of the shared INGEST_TOKEN."""
        import secrets
        user = self.get_user_by_id(user_id)
        if user and user.get("api_token"):
            return user["api_token"]
        token = "adra_" + secrets.token_urlsafe(32)
        self._execute(f"UPDATE users SET api_token={self._ph()} WHERE id={self._ph()}", (token, int(user_id)))
        return token

    def get_user_by_token(self, token: str) -> Optional[dict]:
        tok = (token or "").strip()
        if not tok:
            return None
        return self._query_one(f"SELECT * FROM users WHERE api_token = {self._ph()}", (tok,))

    def update_user_status(self, user_id: int, is_active: bool) -> None:
        self._execute(f"UPDATE users SET is_active = {self._ph()} WHERE id = {self._ph()}", (1 if is_active else 0, int(user_id)))

    def update_user_role(self, user_id: int, role: str) -> None:
        self._execute(f"UPDATE users SET role = {self._ph()} WHERE id = {self._ph()}", (role, int(user_id)))

    def update_user_login(self, user_id: int) -> None:
        self._execute(f"UPDATE users SET last_login_at = {self._ph()} WHERE id = {self._ph()}", (_now(), int(user_id)))

    def delete_user(self, user_id: int) -> None:
        p = self._ph()
        self._execute(f"DELETE FROM user_ads WHERE user_id = {p}", (int(user_id),))
        self._execute(f"DELETE FROM user_pages WHERE user_id = {p}", (int(user_id),))
        self._execute(f"DELETE FROM users WHERE id = {p}", (int(user_id),))

    def get_settings(self) -> dict:
        rows = self._query("SELECT key, value FROM settings")
        return {r["key"]: r["value"] for r in rows}

    def set_setting(self, key: str, value: str) -> None:
        p = self._ph()
        conn = self._connect()
        try:
            cur = conn.cursor()
            if self.is_postgres:
                cur.execute(
                    f"INSERT INTO settings (key, value) VALUES ({p},{p}) "
                    f"ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", (key, value))
            else:
                cur.execute(
                    f"INSERT INTO settings (key, value) VALUES ({p},{p}) "
                    f"ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))
            conn.commit()
        finally:
            conn.close()


db = Database()
