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
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

logger = logging.getLogger("meta_ads_db")

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()

_SQLITE_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "meta_ads.sqlite")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# Algeria is UTC+1 all year (no daylight saving), so a fixed offset is exact
# and needs no tz database in the container.
ALGERIA_TZ = timezone(timedelta(hours=1))


def algeria_today() -> date:
    return datetime.now(ALGERIA_TZ).date()


def _to_algeria_date(value: str) -> Optional[date]:
    """Parse 'YYYY-MM-DD', an ISO timestamp or an epoch (s/ms) into a date."""
    v = str(value or "").strip()
    if not v:
        return None
    try:
        if v.replace(".", "", 1).isdigit():
            sec = float(v)
            if sec > 1e11:  # milliseconds
                sec /= 1000.0
            return datetime.fromtimestamp(sec, tz=timezone.utc).date()
        if len(v) == 10:
            return datetime.strptime(v, "%Y-%m-%d").date()
        dt = datetime.fromisoformat(v)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(ALGERIA_TZ).date()
    except Exception:
        return None


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
        # Competitors each user tracks (any target type). The target row is
        # shared so a competitor followed by several users is scraped once;
        # label / enabled / interval are the user's own settings.
        cur.execute("""
        CREATE TABLE IF NOT EXISTS user_targets (
            user_id INTEGER NOT NULL,
            target_id INTEGER NOT NULL,
            label TEXT DEFAULT '',
            enabled INTEGER DEFAULT 1,
            interval_hours REAL DEFAULT 12.0,
            source TEXT DEFAULT 'dashboard',
            created_at TEXT,
            PRIMARY KEY (user_id, target_id)
        )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_user_targets_target ON user_targets (target_id)")
        # Server-side login sessions: the cookie only carries a random id whose
        # SHA-256 is stored here, so a session can be revoked (logout, logout
        # everywhere, user disabled) and stops working everywhere at once.
        cur.execute("""
        CREATE TABLE IF NOT EXISTS user_sessions (
            sid_hash TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            created_at TEXT,
            last_seen_at TEXT,
            expires_at TEXT,
            revoked_at TEXT,
            ip TEXT,
            user_agent TEXT
        )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_user_sessions_user ON user_sessions (user_id)")
        # Login / registration attempts, for brute-force throttling.
        cur.execute(f"""
        CREATE TABLE IF NOT EXISTS auth_attempts (
            id {idcol},
            kind TEXT,
            email TEXT,
            ip TEXT,
            success INTEGER DEFAULT 0,
            created_at TEXT
        )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_auth_attempts_time ON auth_attempts (created_at)")
        # Audit log of security-relevant events (who did what, from where).
        cur.execute(f"""
        CREATE TABLE IF NOT EXISTS security_events (
            id {idcol},
            created_at TEXT,
            event TEXT,
            user_id INTEGER,
            email TEXT,
            ip TEXT,
            detail TEXT
        )
        """)
        # Browser-extension tokens: only a SHA-256 is stored, one row per
        # extension login, individually revocable.
        cur.execute(f"""
        CREATE TABLE IF NOT EXISTS api_tokens (
            id {idcol},
            user_id INTEGER NOT NULL,
            token_hash TEXT UNIQUE NOT NULL,
            created_at TEXT,
            last_used_at TEXT,
            revoked_at TEXT
        )
        """)
        # How many ads a page / a store domain runs in total on Meta (not only
        # the ones our searches happened to collect). Column names are
        # prefixed so this table can be joined to meta_ads unambiguously.
        cur.execute("""
        CREATE TABLE IF NOT EXISTS entity_stats (
            ent_kind TEXT NOT NULL,
            ent_key TEXT NOT NULL,
            ent_ads INTEGER,
            ent_checked_at TEXT,
            ent_status TEXT DEFAULT 'ok',
            PRIMARY KEY (ent_kind, ent_key)
        )
        """)
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
            ("meta_ads", "product_key", "TEXT"),
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
        # Product grouping: one key per "same advertiser + same product".
        try:
            cur.execute("CREATE INDEX IF NOT EXISTS idx_meta_ads_product ON meta_ads (product_key)")
            conn.commit()
            self._backfill_product_keys(cur, conn)
        except Exception as exc:
            logger.warning("product_key backfill notice: %s", exc)
            if self.is_postgres:
                conn.rollback()

        # Ad <-> keyword tags: one ad can be discovered by many keywords. This
        # keeps every keyword that matched an ad, so re-scraping the same ad
        # with another keyword never overwrites the earlier tag.
        cur.execute("""
        CREATE TABLE IF NOT EXISTS ad_keywords (
            ad_id TEXT NOT NULL,
            keyword TEXT NOT NULL,
            target_id INTEGER,
            first_seen TEXT,
            last_seen TEXT,
            PRIMARY KEY (ad_id, keyword)
        )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_ad_keywords_keyword ON ad_keywords (keyword)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_ad_keywords_ad ON ad_keywords (ad_id)")
        try:
            if self.is_postgres:
                cur.execute("INSERT INTO ad_keywords (ad_id, keyword, first_seen, last_seen) "
                            "SELECT ad_id, query, first_seen, collected_at FROM meta_ads "
                            "WHERE query IS NOT NULL AND query <> '' "
                            "ON CONFLICT (ad_id, keyword) DO NOTHING")
            else:
                cur.execute("INSERT OR IGNORE INTO ad_keywords (ad_id, keyword, first_seen, last_seen) "
                            "SELECT ad_id, query, first_seen, collected_at FROM meta_ads "
                            "WHERE query IS NOT NULL AND query <> ''")
        except Exception as exc:  # noqa: BLE001
            logger.warning("ad_keywords backfill notice: %s", exc)

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

    def _backfill_product_keys(self, cur, conn, batch: int = 2000) -> None:
        """Compute product_key for ads stored before grouping existed."""
        from store_filter import product_key
        p = self._ph()
        total = 0
        while True:
            cur.execute(f"SELECT ad_id, page_id, link_url, body, store_domain FROM meta_ads "
                        f"WHERE product_key IS NULL OR product_key = '' LIMIT {int(batch)}")
            rows = cur.fetchall()
            if not rows:
                break
            updates = [(product_key(r[1], r[2], r[3], r[4]), r[0]) for r in rows]
            cur.executemany(f"UPDATE meta_ads SET product_key={p} WHERE ad_id={p}", updates)
            conn.commit()
            total += len(rows)
        if total:
            logger.info("product_key backfilled for %s ads", total)

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
    def _user_scope_sql(self, prefix: str = "") -> str:
        """WHERE fragment for the ads a user sees (params: _user_scope_params):
        ads they saved, every ad of the pages they track, and the ads
        collected for their store / keyword / custom-link competitors.

        ``prefix`` qualifies the ad columns (e.g. ``"m."``) when the fragment is
        used in a join that also exposes an ``ad_id`` column."""
        p = self._ph()
        mine = (f"SELECT t.target_value FROM targets t JOIN user_targets ut ON ut.target_id = t.id "
                f"WHERE ut.user_id = {p}")
        return (f"({prefix}ad_id IN (SELECT ad_id FROM user_ads WHERE user_id = {p}) "
                f"OR {prefix}page_id IN (SELECT page_id FROM user_pages WHERE user_id = {p}) "
                f"OR {prefix}store_domain IN ({mine} AND t.type = 'domain') "
                f"OR {prefix}query IN ({mine} AND t.type IN ('keyword', 'custom_url', 'domain')))")

    @staticmethod
    def _user_scope_params(user_id: int) -> list:
        return [int(user_id)] * 4

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
    def _calc_days_active(self, start_date_str: str, first_seen_str: str,
                          is_active: bool = True, last_seen_active: str = "") -> int:
        """Days the ad has been running, launch day = day 1, in Algeria time.

        A running ad counts up to today; a stopped one up to the last day it
        was seen active. Stored values are refreshed daily
        (:meth:`refresh_days_active`) so they never freeze at scrape time.
        """
        start = _to_algeria_date(start_date_str) or _to_algeria_date(first_seen_str)
        if not start:
            return 1
        end = algeria_today()
        if not is_active:
            end = _to_algeria_date(last_seen_active) or end
        return max(1, (end - start).days + 1)

    def refresh_days_active(self) -> int:
        """Recompute days_active for every ad (run once a day). Returns rows changed."""
        p = self._ph()
        rows = self._query("SELECT ad_id, start_date, first_seen, is_active, last_seen_active, "
                           "days_active FROM meta_ads")
        updates = []
        for r in rows:
            d = self._calc_days_active(r.get("start_date") or "", r.get("first_seen") or "",
                                       bool(r.get("is_active")), r.get("last_seen_active") or "")
            if d != r.get("days_active"):
                updates.append((d, r["ad_id"]))
        if updates:
            conn = self._connect()
            try:
                cur = conn.cursor()
                cur.executemany(f"UPDATE meta_ads SET days_active={p} WHERE ad_id={p}", updates)
                conn.commit()
            finally:
                conn.close()
        return len(updates)

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

        # Links are rendered in the dashboard: only plain web URLs are kept
        # (no javascript:/data: tricks coming from ads or the extension).
        def _web_url(v) -> str:
            v = str(v or "").strip()
            return v if v.lower().startswith(("http://", "https://")) else ""
        ad = dict(ad)
        ad["link_url"] = _web_url(ad.get("link_url"))
        preview_img = _web_url(ad.get("preview_image_url"))
        from store_filter import product_key
        pkey = product_key(str(ad.get("page_id") or ""), ad.get("link_url") or "",
                           ad.get("body") or "", ad.get("store_domain") or "")
        cols = ("ad_id", "page_name", "page_id", "start_date", "is_active", "link_url",
                "store_domain", "platform", "body", "cta_text", "collation_count",
                "publisher_platforms", "query", "country", "collected_at", "first_seen",
                "last_seen_active", "days_active", "target_id", "preview_image_url",
                "product_key")
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
            pkey,
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
            # Tag the ad with the keyword that found it (many per ad, kept).
            kw = (query or "").strip()
            if kw:
                if self.is_postgres:
                    cur.execute(
                        f"INSERT INTO ad_keywords (ad_id, keyword, target_id, first_seen, last_seen) "
                        f"VALUES ({p},{p},{p},{p},{p}) "
                        f"ON CONFLICT (ad_id, keyword) DO UPDATE SET "
                        f"last_seen = EXCLUDED.last_seen, "
                        f"target_id = COALESCE(EXCLUDED.target_id, ad_keywords.target_id)",
                        (ad_id, kw, target_id, now, now))
                else:
                    cur.execute(
                        f"INSERT OR IGNORE INTO ad_keywords (ad_id, keyword, target_id, first_seen, last_seen) "
                        f"VALUES ({p},{p},{p},{p},{p})", (ad_id, kw, target_id, now, now))
                    cur.execute(
                        f"UPDATE ad_keywords SET last_seen = {p}, "
                        f"target_id = COALESCE({p}, target_id) "
                        f"WHERE ad_id = {p} AND keyword = {p}",
                        (now, target_id, ad_id, kw))
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
                           user_id: Optional[int] = None,
                           min_product_ads: int = 0,
                           keywords: Optional[list[str]] = None) -> tuple[str, list]:
        p = self._ph()
        where = []
        params: list = []

        if user_id is not None:
            where.append(self._user_scope_sql())
            params.extend(self._user_scope_params(user_id))

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

        if keywords:
            ks = [str(k).strip() for k in keywords if str(k).strip()]
            if ks:
                in_list = ", ".join([p] * len(ks))
                where.append(f"ad_id IN (SELECT ad_id FROM ad_keywords WHERE keyword IN ({in_list}))")
                params.extend(ks)

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

        # Products (same advertiser + same product) with at least N ads that
        # match all the filters above.
        if min_product_ads and int(min_product_ads) > 1:
            base = ("WHERE " + " AND ".join(where)) if where else ""
            where.append(f"product_key IN (SELECT product_key FROM meta_ads {base} "
                         f"GROUP BY product_key HAVING COUNT(*) >= {p})")
            params = params + params + [int(min_product_ads)]

        clause = ("WHERE " + " AND ".join(where)) if where else ""
        return clause, params

    def get_ads(self, limit: int = 100, offset: int = 0, search: str = "",
                stores_only: bool = False, query: str = "", store_domain: str = "",
                page_id: str = "", preset: str = "", active_only: bool = False,
                start_date_min: str = "", start_date_max: str = "",
                min_days_active: int = 0, sort_by: str = "collected_at",
                target_id: Optional[int] = None, user_id: Optional[int] = None,
                min_product_ads: int = 0,
                keywords: Optional[list[str]] = None) -> list[dict]:
        p = self._ph()
        clause, params = self._build_ads_filter(
            search=search, stores_only=stores_only, query=query,
            store_domain=store_domain, page_id=page_id, preset=preset,
            active_only=active_only, start_date_min=start_date_min,
            start_date_max=start_date_max, min_days_active=min_days_active,
            target_id=target_id, user_id=user_id, min_product_ads=min_product_ads,
            keywords=keywords,
        )

        order_map = {
            "longest_running": "days_active DESC, is_active DESC",
            "newest_launch": "start_date DESC",
            "most_collated": "collation_count DESC",
            "collected_at": "collected_at DESC",
            "biggest_page": "COALESCE(es.ent_ads, -1) DESC, collected_at DESC",
        }
        order_clause = order_map.get(sort_by, "collected_at DESC")
        join = self._PAGE_STATS_JOIN if sort_by == "biggest_page" else ""

        sql = f"SELECT meta_ads.* FROM meta_ads {join} {clause} ORDER BY {order_clause} LIMIT {p} OFFSET {p}"
        params.extend([limit, offset])
        rows = self._query(sql, tuple(params))
        for r in rows:
            try:
                r["publisher_platforms"] = json.loads(r.get("publisher_platforms") or "[]")
            except Exception:
                r["publisher_platforms"] = []
        return self.attach_keywords(rows)

    def attach_keywords(self, rows: list[dict]) -> list[dict]:
        """Attach ``keywords`` (all scrape keywords that found each ad) to rows.

        Falls back to the legacy ``meta_ads.query`` column when an ad predates
        the ``ad_keywords`` table."""
        if not rows:
            return rows
        p = self._ph()
        ids = [str(r.get("ad_id") or "") for r in rows if r.get("ad_id")]
        mapping: dict[str, list[str]] = {}
        for i in range(0, len(ids), 400):
            chunk = ids[i:i + 400]
            in_list = ", ".join([p] * len(chunk))
            for r in self._query(
                    f"SELECT ad_id, keyword FROM ad_keywords WHERE ad_id IN ({in_list}) "
                    f"ORDER BY keyword", tuple(chunk)):
                mapping.setdefault(str(r["ad_id"]), []).append(str(r["keyword"]))
        for r in rows:
            ks = mapping.get(str(r.get("ad_id") or ""), [])
            if not ks and (r.get("query") or "").strip():
                ks = [str(r["query"]).strip()]
            r["keywords"] = ks
        return rows

    # ── page / domain totals on Meta ──────────────────────────────────────
    def save_entity_stat(self, kind: str, key: str, ads: Optional[int], status: str = "ok") -> None:
        p = self._ph()
        sql = (f"INSERT INTO entity_stats (ent_kind, ent_key, ent_ads, ent_checked_at, ent_status) "
               f"VALUES ({p},{p},{p},{p},{p}) ON CONFLICT (ent_kind, ent_key) DO UPDATE SET "
               f"ent_ads = excluded.ent_ads, ent_checked_at = excluded.ent_checked_at, "
               f"ent_status = excluded.ent_status")
        self._execute(sql, (kind, str(key), ads, _now(), status))

    def stale_entities(self, max_age_hours: float = 24.0, retry_hours: float = 6.0,
                       limit: int = 50) -> list[tuple[str, str]]:
        """Pages and store domains seen in the bank whose total is missing or
        older than ``max_age_hours`` (failed checks retried after ``retry_hours``).
        Never-checked entities of the most recently collected ads come first."""
        p = self._ph()
        ok_cut = (datetime.now(timezone.utc) - timedelta(hours=max_age_hours)).isoformat()
        err_cut = (datetime.now(timezone.utc) - timedelta(hours=retry_hours)).isoformat()
        out: list[tuple[str, str]] = []
        for kind, col in (("page", "page_id"), ("domain", "store_domain")):
            rows = self._query(
                f"SELECT m.{col} AS k, MAX(m.collected_at) AS last, MAX(es.ent_checked_at) AS checked "
                f"FROM meta_ads m LEFT JOIN entity_stats es "
                f"ON es.ent_kind = {p} AND es.ent_key = m.{col} "
                f"WHERE m.{col} IS NOT NULL AND m.{col} != '' "
                f"AND (es.ent_checked_at IS NULL "
                f"OR (es.ent_status = 'ok' AND es.ent_checked_at < {p}) "
                f"OR (es.ent_status != 'ok' AND es.ent_checked_at < {p})) "
                f"GROUP BY m.{col} ORDER BY MAX(es.ent_checked_at) IS NOT NULL, MAX(m.collected_at) DESC "
                f"LIMIT {int(limit)}", (kind, ok_cut, err_cut))
            out += [(kind, r["k"]) for r in rows]
        # interleave so pages and domains progress together
        pages = [e for e in out if e[0] == "page"]
        doms = [e for e in out if e[0] == "domain"]
        mixed = []
        for i in range(max(len(pages), len(doms))):
            mixed += pages[i:i + 1] + doms[i:i + 1]
        return mixed[:limit]

    def attach_entity_stats(self, ads: list[dict]) -> list[dict]:
        """Add page_total_ads / domain_total_ads (Meta totals) and
        domain_pages (pages in our bank using that domain) to each ad."""
        if not ads:
            return ads
        p = self._ph()
        pages = sorted({str(a.get("page_id") or "") for a in ads} - {""})
        doms = sorted({str(a.get("store_domain") or "") for a in ads} - {""})
        stats: dict = {}
        for kind, keys in (("page", pages), ("domain", doms)):
            if keys:
                rows = self._query(
                    f"SELECT ent_key, ent_ads, ent_checked_at FROM entity_stats WHERE ent_kind={p} "
                    f"AND ent_key IN ({', '.join([p] * len(keys))})", (kind, *keys))
                for r in rows:
                    stats[(kind, r["ent_key"])] = r
        dom_pages = {}
        if doms:
            rows = self._query(
                f"SELECT store_domain, COUNT(DISTINCT page_id) AS n FROM meta_ads "
                f"WHERE store_domain IN ({', '.join([p] * len(doms))}) GROUP BY store_domain", tuple(doms))
            dom_pages = {r["store_domain"]: int(r["n"] or 0) for r in rows}
        for a in ads:
            ps = stats.get(("page", str(a.get("page_id") or "")))
            ds = stats.get(("domain", str(a.get("store_domain") or "")))
            a["page_total_ads"] = ps["ent_ads"] if ps else None
            a["domain_total_ads"] = ds["ent_ads"] if ds else None
            a["domain_pages"] = dom_pages.get(a.get("store_domain") or "", 0)
        return ads

    # ── product groups (same advertiser + same product) ───────────────────
    _GROUP_ORDER = {
        "longest_running": "max_days DESC, active DESC",
        "newest_launch": "last_launch DESC",
        "most_collated": "ads_count DESC, active DESC",
        "collected_at": "last_collected DESC",
        "biggest_page": "page_ads DESC, ads_count DESC",
    }
    _PAGE_STATS_JOIN = ("LEFT JOIN entity_stats es ON es.ent_kind = 'page' "
                        "AND es.ent_key = meta_ads.page_id")

    def get_ad_groups(self, limit: int = 60, offset: int = 0, sort_by: str = "collected_at",
                      **filters) -> list[dict]:
        """One item per product: a representative ad plus a ``group`` summary.

        Filters are the same as :meth:`get_ads` and apply to the ads before
        grouping, so counts reflect only the matching ads.
        """
        p = self._ph()
        clause, params = self._build_ads_filter(**filters)
        order = self._GROUP_ORDER.get(sort_by, self._GROUP_ORDER["collected_at"])
        groups = self._query(
            f"SELECT product_key, COUNT(*) AS ads_count, SUM(is_active) AS active, "
            f"MIN(start_date) AS first_launch, MAX(start_date) AS last_launch, "
            f"COUNT(DISTINCT body) AS texts, MAX(days_active) AS max_days, "
            f"MAX(collected_at) AS last_collected, MAX(COALESCE(es.ent_ads, -1)) AS page_ads "
            f"FROM meta_ads {self._PAGE_STATS_JOIN} {clause} GROUP BY product_key "
            f"ORDER BY {order}, product_key LIMIT {p} OFFSET {p}",
            tuple(params) + (limit, offset))
        if not groups:
            return []
        keys = [g["product_key"] for g in groups]
        in_list = ", ".join([p] * len(keys))
        joiner = " AND " if clause else "WHERE "
        rows = self._query(
            f"SELECT * FROM meta_ads {clause}{joiner}product_key IN ({in_list})",
            tuple(params) + tuple(keys))
        # Representative: an active ad first, then the most recently launched.
        best: dict = {}
        for r in rows:
            k = r["product_key"]
            rank = (int(r.get("is_active") or 0), r.get("start_date") or "", r.get("collected_at") or "")
            if k not in best or rank > best[k][0]:
                best[k] = (rank, r)
        out = []
        for g in groups:
            ad = dict(best[g["product_key"]][1])
            try:
                ad["publisher_platforms"] = json.loads(ad.get("publisher_platforms") or "[]")
            except Exception:
                ad["publisher_platforms"] = []
            ad["group"] = {
                "key": g["product_key"],
                "count": int(g["ads_count"] or 0),
                "active": int(g["active"] or 0),
                "first_launch": g["first_launch"] or "",
                "last_launch": g["last_launch"] or "",
                "texts": int(g["texts"] or 0),
                "max_days": int(g["max_days"] or 0),
            }
            out.append(ad)
        # A product group can be found by several keywords: expose the union so
        # the grouped card shows every keyword behind it.
        if keys:
            in_list = ", ".join([p] * len(keys))
            kmap: dict = {}
            for r in self._query(
                    f"SELECT m.product_key AS pk, ak.keyword AS kw FROM ad_keywords ak "
                    f"JOIN meta_ads m ON m.ad_id = ak.ad_id "
                    f"WHERE m.product_key IN ({in_list}) "
                    f"GROUP BY m.product_key, ak.keyword ORDER BY ak.keyword",
                    tuple(keys)):
                kmap.setdefault(str(r["pk"]), []).append(str(r["kw"]))
            for ad in out:
                gk = kmap.get(str(ad["group"]["key"]))
                if not gk and (ad.get("query") or "").strip():
                    gk = [str(ad["query"]).strip()]
                ad["keywords"] = gk or []
                ad["group"]["keywords"] = ad["keywords"]
        return out

    def count_ad_groups(self, **filters) -> int:
        clause, params = self._build_ads_filter(**filters)
        row = self._query_one(
            f"SELECT COUNT(DISTINCT product_key) AS c FROM meta_ads {clause}", tuple(params))
        return int(row["c"]) if row else 0

    def get_group_ads(self, product_key: str, user_id: Optional[int] = None,
                      limit: int = 500) -> list[dict]:
        """All ads of one product group (newest launch first)."""
        p = self._ph()
        clause, params = self._build_ads_filter(user_id=user_id)
        joiner = " AND " if clause else "WHERE "
        rows = self._query(
            f"SELECT * FROM meta_ads {clause}{joiner}product_key = {p} "
            f"ORDER BY is_active DESC, start_date DESC LIMIT {int(limit)}",
            tuple(params) + (product_key,))
        for r in rows:
            try:
                r["publisher_platforms"] = json.loads(r.get("publisher_platforms") or "[]")
            except Exception:
                r["publisher_platforms"] = []
        return self.attach_keywords(rows)

    def count_ads(self, search: str = "", stores_only: bool = False,
                  query: str = "", store_domain: str = "",
                  page_id: str = "", preset: str = "", active_only: bool = False,
                  start_date_min: str = "", start_date_max: str = "",
                  min_days_active: int = 0, target_id: Optional[int] = None,
                  user_id: Optional[int] = None, min_product_ads: int = 0,
                  keywords: Optional[list[str]] = None) -> int:
        clause, params = self._build_ads_filter(
            search=search, stores_only=stores_only, query=query,
            store_domain=store_domain, page_id=page_id, preset=preset,
            active_only=active_only, start_date_min=start_date_min,
            start_date_max=start_date_max, min_days_active=min_days_active,
            target_id=target_id, user_id=user_id, min_product_ads=min_product_ads,
            keywords=keywords,
        )
        row = self._query_one(f"SELECT COUNT(*) AS c FROM meta_ads {clause}", tuple(params))
        return int(row["c"]) if row else 0

    def get_queries(self) -> list[dict]:
        sql = ("SELECT query, COUNT(*) AS total_ads, "
               "SUM(CASE WHEN store_domain != '' AND store_domain IS NOT NULL THEN 1 ELSE 0 END) AS store_ads, "
               "MAX(collected_at) AS last_run FROM meta_ads "
               "WHERE query != '' GROUP BY query ORDER BY last_run DESC LIMIT 100")
        return self._query(sql)

    def get_keywords_overview(self, user_id: Optional[int] = None,
                              limit: int = 300) -> list[dict]:
        """Per-keyword counters for the keyword filter bar (ads, store ads,
        winning ads, recent launches, last run)."""
        p = self._ph()
        where = ["ak.keyword <> ''"]
        params: list = []
        if user_id is not None:
            where.append(self._user_scope_sql(prefix="m."))
            params.extend(self._user_scope_params(user_id))
        clause = "WHERE " + " AND ".join(where)
        sql = (
            "SELECT ak.keyword AS keyword, COUNT(*) AS total_ads, "
            "SUM(CASE WHEN m.store_domain != '' AND m.store_domain IS NOT NULL THEN 1 ELSE 0 END) AS store_ads, "
            "SUM(CASE WHEN m.days_active >= 5 AND m.is_active = 1 THEN 1 ELSE 0 END) AS winning, "
            "SUM(CASE WHEN m.days_active <= 3 AND m.is_active = 1 THEN 1 ELSE 0 END) AS fresh, "
            "MAX(m.collected_at) AS last_run "
            f"FROM ad_keywords ak JOIN meta_ads m ON m.ad_id = ak.ad_id {clause} "
            f"GROUP BY ak.keyword ORDER BY total_ads DESC, ak.keyword LIMIT {int(limit)}"
        )
        return self._query(sql, tuple(params))

    def get_stores(self, search: str = "", user_id: Optional[int] = None) -> list[dict]:
        p = self._ph()
        where = ["store_domain != '' AND store_domain IS NOT NULL"]
        params: list = []
        if search:
            where.append(f"store_domain LIKE {p}")
            params.append("%" + search + "%")
        if user_id is not None:
            where.append(self._user_scope_sql())
            params.extend(self._user_scope_params(user_id))
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
            scope_params = tuple(self._user_scope_params(user_id))
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

    # ── competitors per user (shared targets + user_targets links) ────────
    def get_target(self, target_id: int) -> Optional[dict]:
        return self._query_one(f"SELECT * FROM targets WHERE id={self._ph()}", (int(target_id),))

    def find_target(self, type: str, target_value: str) -> Optional[dict]:
        p = self._ph()
        return self._query_one(f"SELECT * FROM targets WHERE type={p} AND target_value={p}",
                               ((type or "").strip().lower(), (target_value or "").strip()))

    def _sync_target_from_links(self, target_id: int) -> None:
        """Shared target settings follow its users: enabled if any user has it
        enabled, scraped as often as the most demanding user asks."""
        p = self._ph()
        row = self._query_one(
            f"SELECT MAX(enabled) AS en, MIN(interval_hours) AS iv FROM user_targets WHERE target_id={p}",
            (int(target_id),))
        if row and row.get("en") is not None:
            self._execute(f"UPDATE targets SET enabled={p}, interval_hours={p} WHERE id={p}",
                          (int(row["en"] or 0), float(row["iv"] or 12.0), int(target_id)))

    def add_user_target(self, user_id: int, type: str, target_value: str, label: str = "",
                        country: str = "DZ", interval_hours: float = 12.0,
                        source: str = "dashboard") -> tuple[int, bool]:
        """Track a competitor for one user. Returns (target_id, newly_linked)."""
        typ = (type or "keyword").strip().lower()
        val = (target_value or "").strip()
        self.add_target(type=typ, target_value=val, label=label or val, country=country,
                        interval_hours=interval_hours)
        t = self.find_target(typ, val)
        tid = int(t["id"])
        p = self._ph()
        existing = self._query_one(
            f"SELECT target_id FROM user_targets WHERE user_id={p} AND target_id={p}", (int(user_id), tid))
        if existing:
            if label:
                self._execute(f"UPDATE user_targets SET label={p} WHERE user_id={p} AND target_id={p}",
                              (label, int(user_id), tid))
            return tid, False
        self._execute(
            f"INSERT INTO user_targets (user_id, target_id, label, enabled, interval_hours, source, created_at) "
            f"VALUES ({p},{p},{p},1,{p},{p},{p})",
            (int(user_id), tid, label or val, float(interval_hours or 12.0), source, _now()))
        if typ == "page":
            self.link_user_page(user_id, val)
        self._sync_target_from_links(tid)
        return tid, True

    def user_has_target(self, user_id: int, target_id: int) -> bool:
        p = self._ph()
        return self._query_one(f"SELECT 1 AS x FROM user_targets WHERE user_id={p} AND target_id={p}",
                               (int(user_id), int(target_id))) is not None

    def delete_target_everywhere(self, target_id: int) -> None:
        """Remove a competitor for everyone (and its page history)."""
        t = self.get_target(target_id)
        p = self._ph()
        self._execute(f"DELETE FROM user_targets WHERE target_id={p}", (int(target_id),))
        self.delete_target(int(target_id))
        if t and t.get("type") == "page":
            self.untrack_page(t.get("target_value"))

    def remove_user_target(self, user_id: int, target_id: int) -> None:
        """One user stops tracking; the competitor is dropped when nobody follows it."""
        t = self.get_target(target_id)
        p = self._ph()
        self._execute(f"DELETE FROM user_targets WHERE user_id={p} AND target_id={p}",
                      (int(user_id), int(target_id)))
        if t and t.get("type") == "page":
            self._execute(f"DELETE FROM user_pages WHERE user_id={p} AND page_id={p}",
                          (int(user_id), str(t.get("target_value"))))
        left = self._query_one(f"SELECT COUNT(*) AS c FROM user_targets WHERE target_id={p}", (int(target_id),))
        if not left or not int(left.get("c") or 0):
            self.delete_target_everywhere(target_id)
        else:
            self._sync_target_from_links(target_id)

    def set_user_target_enabled(self, user_id: Optional[int], target_id: int, enabled: bool) -> None:
        """Per user, or for the shared target itself when user_id is None (admin)."""
        p = self._ph()
        if user_id is None:
            self._execute(f"UPDATE user_targets SET enabled={p} WHERE target_id={p}",
                          (1 if enabled else 0, int(target_id)))
            self.toggle_target(int(target_id), enabled)
            return
        self._execute(f"UPDATE user_targets SET enabled={p} WHERE user_id={p} AND target_id={p}",
                      (1 if enabled else 0, int(user_id), int(target_id)))
        self._sync_target_from_links(target_id)

    def list_competitors(self, user_id: Optional[int] = None) -> list[dict]:
        """Competitors of one user (their label/enabled), or all of them for
        an admin overview (with the emails of the users following each)."""
        self.list_targets()  # keeps the legacy keywords -> targets migration
        p = self._ph()
        if user_id is not None:
            rows = self._query(
                f"SELECT t.*, ut.label AS user_label, ut.enabled AS user_enabled, "
                f"ut.interval_hours AS user_interval, ut.source AS source, ut.created_at AS tracked_at "
                f"FROM targets t JOIN user_targets ut ON ut.target_id = t.id "
                f"WHERE ut.user_id={p} ORDER BY ut.created_at DESC, t.id DESC", (int(user_id),))
            for r in rows:
                r["label"] = r.pop("user_label") or r.get("label")
                r["enabled"] = r.pop("user_enabled")
                r["interval_hours"] = r.pop("user_interval") or r.get("interval_hours")
        else:
            rows = self._query("SELECT * FROM targets ORDER BY id DESC")
        for t in rows:
            typ, val, tid = t.get("type") or "keyword", t.get("target_value") or "", t.get("id")
            if typ == "page":
                row = self._query_one(f"SELECT COUNT(*) AS c FROM meta_ads WHERE page_id = {p}", (val,))
                pg = self._query_one(f"SELECT * FROM tracked_pages WHERE page_id={p}", (val,))
                snaps = self._query(
                    f"SELECT captured_at, active_ads_count, total_ads_count FROM page_snapshots "
                    f"WHERE page_id={p} ORDER BY id DESC LIMIT 20", (val,))
                t["activity"] = {
                    "active_ads_count": (pg or {}).get("active_ads_count"),
                    "last_snapshot_at": (pg or {}).get("last_snapshot_at"),
                    "snapshots_count": (pg or {}).get("snapshots_count") or 0,
                    "page_likes": (pg or {}).get("page_likes") or 0,
                    "snapshots": list(reversed(snaps)),
                }
            elif typ == "domain":
                row = self._query_one(f"SELECT COUNT(*) AS c FROM meta_ads WHERE store_domain = {p} OR query = {p}", (val, val))
            else:
                row = self._query_one(f"SELECT COUNT(*) AS c FROM meta_ads WHERE query = {p}", (val,))
            t["ads_count"] = int(row["c"]) if row and row.get("c") else 0
            owners = self._query(
                f"SELECT u.email FROM user_targets ut JOIN users u ON u.id = ut.user_id "
                f"WHERE ut.target_id={p} ORDER BY ut.created_at", (tid,))
            t["tracked_by"] = [o["email"] for o in owners]
        return rows

    def migrate_user_targets(self) -> int:
        """One-time move to per-user competitors: extension pages go to the
        users tracking them, the remaining (admin-made) targets to the admins."""
        if self._query_one("SELECT 1 AS x FROM user_targets LIMIT 1"):
            return 0
        p = self._ph()
        moved = 0
        for up in self._query("SELECT user_id, page_id, tracked_at FROM user_pages"):
            name = (self._query_one(f"SELECT page_name FROM tracked_pages WHERE page_id={p}",
                                    (up["page_id"],)) or {}).get("page_name") or ""
            self.add_user_target(up["user_id"], "page", up["page_id"], label=name, source="extension")
            moved += 1
        admins = [r["id"] for r in self._query("SELECT id FROM users WHERE role='admin' ORDER BY id")]
        for t in self._query("SELECT * FROM targets"):
            if self._query_one(f"SELECT 1 AS x FROM user_targets WHERE target_id={p} LIMIT 1", (t["id"],)):
                continue
            for aid in admins:
                self.add_user_target(aid, t["type"], t["target_value"], label=t.get("label") or "",
                                     country=t.get("country") or "DZ",
                                     interval_hours=t.get("interval_hours") or 12.0)
                moved += 1
            if admins and not t.get("enabled"):
                self.set_user_target_enabled(None, t["id"], False)
        return moved

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

    # ── server-side sessions ──────────────────────────────────────────────
    @staticmethod
    def _sid_hash(sid: str) -> str:
        import hashlib
        return hashlib.sha256((sid or "").encode("utf-8")).hexdigest()

    def create_session(self, user_id: int, days: float = 7.0, ip: str = "", user_agent: str = "") -> str:
        """New session for a user; returns the raw id to put in the cookie."""
        import secrets
        sid = secrets.token_urlsafe(32)
        now = datetime.now(timezone.utc)
        p = self._ph()
        self._execute(
            f"INSERT INTO user_sessions (sid_hash, user_id, created_at, last_seen_at, expires_at, ip, user_agent) "
            f"VALUES ({p},{p},{p},{p},{p},{p},{p})",
            (self._sid_hash(sid), int(user_id), now.isoformat(), now.isoformat(),
             (now + timedelta(days=days)).isoformat(), (ip or "")[:64], (user_agent or "")[:255]))
        return sid

    def get_session_user(self, sid: str) -> Optional[dict]:
        """The user of a live session (not revoked, not expired, user still
        existing and active), with the session row under ``_session``."""
        if not sid:
            return None
        p = self._ph()
        row = self._query_one(f"SELECT * FROM user_sessions WHERE sid_hash={p}", (self._sid_hash(sid),))
        if not row or row.get("revoked_at"):
            return None
        if (row.get("expires_at") or "") <= _now():
            return None
        user = self.get_user_by_id(int(row["user_id"]))
        if not user or not user.get("is_active"):
            return None
        user["_session"] = row
        return user

    def touch_session(self, sid: str) -> None:
        self._execute(f"UPDATE user_sessions SET last_seen_at={self._ph()} WHERE sid_hash={self._ph()}",
                      (_now(), self._sid_hash(sid)))

    def revoke_session(self, sid: str) -> None:
        self._execute(f"UPDATE user_sessions SET revoked_at={self._ph()} WHERE sid_hash={self._ph()} "
                      f"AND revoked_at IS NULL", (_now(), self._sid_hash(sid)))

    def revoke_user_sessions(self, user_id: int) -> int:
        """Log a user out everywhere. Returns how many sessions were live."""
        p = self._ph()
        row = self._query_one(f"SELECT COUNT(*) AS c FROM user_sessions WHERE user_id={p} AND revoked_at IS NULL",
                              (int(user_id),))
        self._execute(f"UPDATE user_sessions SET revoked_at={p} WHERE user_id={p} AND revoked_at IS NULL",
                      (_now(), int(user_id)))
        return int(row["c"]) if row else 0

    def purge_old_sessions(self, keep_days: int = 30) -> None:
        cut = (datetime.now(timezone.utc) - timedelta(days=keep_days)).isoformat()
        self._execute(f"DELETE FROM user_sessions WHERE expires_at < {self._ph()}", (cut,))

    # ── brute-force throttling ────────────────────────────────────────────
    def record_auth_attempt(self, kind: str, email: str, ip: str, success: bool) -> None:
        p = self._ph()
        self._execute(f"INSERT INTO auth_attempts (kind, email, ip, success, created_at) VALUES ({p},{p},{p},{p},{p})",
                      (kind, (email or "")[:254], (ip or "")[:64], 1 if success else 0, _now()))

    def count_failed_attempts(self, kind: str, minutes: int, email: str = "", ip: str = "") -> int:
        p = self._ph()
        cut = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()
        col, val = ("email", email) if email else ("ip", ip)
        row = self._query_one(
            f"SELECT COUNT(*) AS c FROM auth_attempts WHERE kind={p} AND success=0 AND {col}={p} AND created_at > {p}",
            (kind, val, cut))
        return int(row["c"]) if row else 0

    def count_attempts(self, kind: str, minutes: int, ip: str) -> int:
        p = self._ph()
        cut = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()
        row = self._query_one(f"SELECT COUNT(*) AS c FROM auth_attempts WHERE kind={p} AND ip={p} AND created_at > {p}",
                              (kind, ip, cut))
        return int(row["c"]) if row else 0

    def purge_auth_attempts(self, keep_days: int = 7) -> None:
        cut = (datetime.now(timezone.utc) - timedelta(days=keep_days)).isoformat()
        self._execute(f"DELETE FROM auth_attempts WHERE created_at < {self._ph()}", (cut,))

    # ── security audit log ────────────────────────────────────────────────
    def log_security_event(self, event: str, user_id: Optional[int] = None, email: str = "",
                           ip: str = "", detail: str = "") -> None:
        p = self._ph()
        try:
            self._execute(f"INSERT INTO security_events (created_at, event, user_id, email, ip, detail) "
                          f"VALUES ({p},{p},{p},{p},{p},{p})",
                          (_now(), event, user_id, (email or "")[:254], (ip or "")[:64], (detail or "")[:500]))
        except Exception as exc:  # auditing must never break the request
            logger.warning("security event not logged: %s", exc)

    def list_security_events(self, limit: int = 200, event: str = "") -> list[dict]:
        p = self._ph()
        if event:
            return self._query(f"SELECT * FROM security_events WHERE event={p} ORDER BY id DESC LIMIT {int(limit)}", (event,))
        return self._query(f"SELECT * FROM security_events ORDER BY id DESC LIMIT {int(limit)}")

    # ── extension tokens (hashed, revocable) ──────────────────────────────
    def migrate_plain_api_tokens(self) -> int:
        """Move tokens stored in clear (users.api_token) to hashed rows, so
        installed extensions keep working but the database holds no token."""
        p = self._ph()
        moved = 0
        for u in self._query("SELECT id, api_token FROM users WHERE api_token IS NOT NULL AND api_token != ''"):
            h = self._sid_hash(u["api_token"])
            if not self._query_one(f"SELECT id FROM api_tokens WHERE token_hash={p}", (h,)):
                self._execute(f"INSERT INTO api_tokens (user_id, token_hash, created_at) VALUES ({p},{p},{p})",
                              (int(u["id"]), h, _now()))
            self._execute(f"UPDATE users SET api_token=NULL WHERE id={p}", (int(u["id"]),))
            moved += 1
        return moved

    def create_api_token(self, user_id: int) -> str:
        """A new extension token (shown once; only its hash is stored)."""
        import secrets
        token = "adra_" + secrets.token_urlsafe(32)
        p = self._ph()
        self._execute(f"INSERT INTO api_tokens (user_id, token_hash, created_at) VALUES ({p},{p},{p})",
                      (int(user_id), self._sid_hash(token), _now()))
        return token

    def revoke_api_tokens(self, user_id: int) -> int:
        p = self._ph()
        row = self._query_one(f"SELECT COUNT(*) AS c FROM api_tokens WHERE user_id={p} AND revoked_at IS NULL",
                              (int(user_id),))
        self._execute(f"UPDATE api_tokens SET revoked_at={p} WHERE user_id={p} AND revoked_at IS NULL",
                      (_now(), int(user_id)))
        return int(row["c"]) if row else 0

    def count_api_tokens(self, user_id: int) -> int:
        row = self._query_one(f"SELECT COUNT(*) AS c FROM api_tokens WHERE user_id={self._ph()} AND revoked_at IS NULL",
                              (int(user_id),))
        return int(row["c"]) if row else 0

    def get_or_create_api_token(self, user_id: int) -> str:
        """Kept for callers: tokens are no longer readable once issued, so this
        always issues a new one (only its hash is stored)."""
        return self.create_api_token(user_id)

    def get_user_by_token(self, token: str) -> Optional[dict]:
        tok = (token or "").strip()
        if not tok:
            return None
        p = self._ph()
        row = self._query_one(f"SELECT id, user_id, last_used_at FROM api_tokens WHERE token_hash={p} "
                              f"AND revoked_at IS NULL", (self._sid_hash(tok),))
        if not row:
            return None
        last = row.get("last_used_at") or ""
        if not last or last < (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat():
            self._execute(f"UPDATE api_tokens SET last_used_at={p} WHERE id={p}", (_now(), row["id"]))
        return self.get_user_by_id(int(row["user_id"]))

    def update_user_status(self, user_id: int, is_active: bool) -> None:
        self._execute(f"UPDATE users SET is_active = {self._ph()} WHERE id = {self._ph()}", (1 if is_active else 0, int(user_id)))
        if not is_active:
            self.revoke_user_sessions(user_id)
            self.revoke_api_tokens(user_id)

    def update_user_role(self, user_id: int, role: str) -> None:
        self._execute(f"UPDATE users SET role = {self._ph()} WHERE id = {self._ph()}", (role, int(user_id)))

    def update_user_login(self, user_id: int) -> None:
        self._execute(f"UPDATE users SET last_login_at = {self._ph()} WHERE id = {self._ph()}", (_now(), int(user_id)))

    def delete_user(self, user_id: int) -> None:
        p = self._ph()
        self._execute(f"DELETE FROM user_ads WHERE user_id = {p}", (int(user_id),))
        for row in self._query(f"SELECT target_id FROM user_targets WHERE user_id = {p}", (int(user_id),)):
            self.remove_user_target(int(user_id), int(row["target_id"]))
        self._execute(f"DELETE FROM user_pages WHERE user_id = {p}", (int(user_id),))
        self._execute(f"DELETE FROM user_sessions WHERE user_id = {p}", (int(user_id),))
        self._execute(f"DELETE FROM api_tokens WHERE user_id = {p}", (int(user_id),))
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
