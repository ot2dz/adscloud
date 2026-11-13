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
import threading
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

import niches as niche_defs
import scoring
from store_filter import BIG_MARKETPLACES, JUNK_LIKE_PATTERNS, is_big_marketplace, is_non_arabic, junk_reason

logger = logging.getLogger("meta_ads_db")

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()

# Path override keeps tests (and side-by-side runs) off the production file.
_SQLITE_PATH = (os.environ.get("META_ADS_SQLITE")
                or os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "meta_ads.sqlite"))


SLOW_QUERY_MS = float(os.environ.get("SLOW_QUERY_MS", "300"))
_CACHE_LOCK = threading.Lock()


def _log_if_slow(sql: str, t0: float) -> None:
    """Log queries slower than SLOW_QUERY_MS (0 = off), to find what is heavy."""
    ms = (time.perf_counter() - t0) * 1000
    if SLOW_QUERY_MS and ms >= SLOW_QUERY_MS:
        logger.warning("slow query %.0f ms: %s", ms, " ".join(sql.split())[:400])


def _invalidates_cache(fn):
    """Database method that changes what the cached counters count: drop the
    cache before (so nothing stale is served) and after (so a read that raced
    with the write is not kept)."""
    import functools

    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        self.invalidate_cache()
        try:
            return fn(self, *args, **kwargs)
        finally:
            self.invalidate_cache()
    return wrapper


def _changes_ads(fn):
    """Database method that rewrites many ads at once (status check, purge,
    backfill…): drops the counter cache and marks the whole product summary
    for a rebuild."""
    import functools

    inner = _invalidates_cache(fn)

    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        try:
            return inner(self, *args, **kwargs)
        finally:
            self.mark_groups_stale()
    return wrapper


class _PgPool:
    """Reuses PostgreSQL connections across queries and threads.

    Never blocks and never refuses: when every pooled connection is busy a new
    one is opened (as before), and at most ``max_idle`` are kept for reuse.
    A connection idle for a while is pinged first, so one the server dropped
    is replaced instead of failing the next query."""

    PING_AFTER_S = 30.0

    def __init__(self, url: str, max_idle: int = int(os.environ.get("DB_POOL_SIZE", "10"))):
        self.url = url
        self.max_idle = max(0, max_idle)
        self._idle: list = []          # [(raw_conn, last_used_monotonic)]
        self._lock = threading.Lock()

    def _new(self):
        import psycopg2
        conn = psycopg2.connect(self.url)
        conn.autocommit = True
        return conn

    def get(self) -> "_PooledConn":
        while True:
            with self._lock:
                item = self._idle.pop() if self._idle else None
            if item is None:
                return _PooledConn(self, self._new())
            raw, last = item
            if raw.closed:
                continue
            if time.monotonic() - last > self.PING_AFTER_S:
                try:
                    with raw.cursor() as c:
                        c.execute("SELECT 1")
                except Exception:  # noqa: BLE001 — dropped by the server
                    try:
                        raw.close()
                    except Exception:
                        pass
                    continue
            return _PooledConn(self, raw)

    def put(self, raw) -> None:
        import psycopg2.extensions as ext
        reusable = (not raw.closed and raw.autocommit
                    and raw.info.transaction_status == ext.TRANSACTION_STATUS_IDLE)
        if reusable:
            with self._lock:
                if len(self._idle) < self.max_idle:
                    self._idle.append((raw, time.monotonic()))
                    return
        try:
            raw.close()
        except Exception:
            pass


class _PooledConn:
    """Wraps a pooled psycopg2 connection: ``close()`` returns it to the pool."""

    __slots__ = ("_pool", "_raw")

    def __init__(self, pool: _PgPool, raw):
        self._pool = pool
        self._raw = raw

    def close(self) -> None:
        raw, self._raw = self._raw, None
        if raw is not None:
            self._pool.put(raw)

    def __getattr__(self, name):
        if self._raw is None:
            raise AttributeError(f"connection already returned to the pool ({name})")
        return getattr(self._raw, name)

    def __setattr__(self, name, value):
        if name in _PooledConn.__slots__:
            object.__setattr__(self, name, value)
        else:
            setattr(self._raw, name, value)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_or_empty(raw) -> dict:
    """ad_events.detail as a dict ({} when empty or not JSON)."""
    if isinstance(raw, dict):
        return raw
    try:
        val = json.loads(raw or "{}")
        return val if isinstance(val, dict) else {}
    except (TypeError, ValueError):
        return {}


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
        self._niches_cache: list[dict] = []
        self._blocked_domains: list[str] = []
        self._result_cache: dict = {}
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
        """A database connection; callers always ``close()`` it when done.

        PostgreSQL connections come from a small pool: ``close()`` hands them
        back instead of tearing them down, so a page load no longer pays a new
        TCP + auth handshake for each of its queries."""
        if self.is_postgres:
            return self._pg_pool().get()
        conn = sqlite3.connect(_SQLITE_PATH)
        conn.row_factory = sqlite3.Row
        return conn

    def _pg_pool(self) -> "_PgPool":
        pool = getattr(self, "_pool", None)
        if pool is None or pool.url != self.db_url:
            pool = self._pool = _PgPool(self.db_url)
        return pool

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
        # Ad lifecycle timeline: launched / stopped / resumed events. Kept apart
        # from meta_ads so history survives repeated status flips.
        cur.execute(f"""
        CREATE TABLE IF NOT EXISTS ad_events (
            id {idcol},
            ad_id TEXT,
            market TEXT DEFAULT 'dz',
            page_id TEXT,
            store_domain TEXT,
            product_key TEXT,
            query TEXT,
            page_name TEXT,
            body TEXT,
            link_url TEXT,
            event TEXT,
            event_at TEXT,
            detail TEXT,
            created_at TEXT
        )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_ad_events_ad ON ad_events (ad_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_ad_events_time ON ad_events (event_at)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_ad_events_market ON ad_events (market, event_at)")
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
            # 'month_days' = whole-market scrape split by launch day x language
            ("mode", "TEXT DEFAULT ''"),
            ("languages", "TEXT DEFAULT ''"),
            # Resume support: JSON {"pass": n, "cursor": "..."} of the next
            # unfetched page, retry bookkeeping and a liveness heartbeat.
            ("checkpoint", "TEXT"),
            ("attempts", "INTEGER DEFAULT 0"),
            ("retry_at", "TEXT"),
            ("heartbeat_at", "TEXT"),
            # 'dz' or 'global' (foreign market, manual scans only).
            ("market", "TEXT DEFAULT 'dz'"),
            # Completeness check: Meta's own estimate at the start of the scan,
            # ads Meta actually returned (before our filters, cumulative over
            # resumed runs) and whether Meta said "no more pages" (NULL = n/a).
            ("reported_count", "INTEGER"),
            ("scanned_count", "INTEGER DEFAULT 0"),
            ("completed", "INTEGER"),
            # Whole-market scans: slices planned and slices that reached
            # Meta's end (NULL for single searches).
            ("slices_total", "INTEGER"),
            ("slices_ok", "INTEGER"),
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
            ("meta_ads", "video_url", "TEXT"),
            ("meta_ads", "video_sd_url", "TEXT"),
            ("meta_ads", "collation_id", "TEXT"),
            ("meta_ads", "stopped_at", "TEXT"),
            ("meta_ads", "status_checked_at", "TEXT"),
            ("meta_ads", "product_key", "TEXT"),
            ("meta_ads", "primary_niche", "TEXT"),
            ("meta_ads", "niche_checked_at", "TEXT"),
            # Persisted product strength (see scoring.py). NULL until computed.
            ("meta_ads", "hunter_score", "INTEGER"),
            # 'dz' (default Arabic bank) or 'global' (foreign market side tab).
            ("meta_ads", "market", "TEXT DEFAULT 'dz'"),
            ("keywords", "type", "TEXT DEFAULT 'keyword'"),
            ("keywords", "label", "TEXT DEFAULT ''"),
            ("targets", "country", "TEXT DEFAULT 'DZ'"),
            ("targets", "interval_hours", "REAL DEFAULT 12.0"),
            ("targets", "last_scraped_at", "TEXT"),
            ("targets", "market", "TEXT DEFAULT 'dz'"),
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
            cur.execute("CREATE INDEX IF NOT EXISTS idx_meta_ads_market ON meta_ads (market, store_domain)")
            conn.commit()
            self._backfill_product_keys(cur, conn)
        except Exception as exc:
            logger.warning("product_key backfill notice: %s", exc)
            if self.is_postgres:
                conn.rollback()

        # Precomputed product summary for the default grouped view (see
        # get_ad_groups). Derived data only: it can be dropped at any time.
        cur.execute("""
        CREATE TABLE IF NOT EXISTS product_groups (
            product_key TEXT PRIMARY KEY,
            ads_count INTEGER,
            active INTEGER,
            first_launch TEXT,
            last_launch TEXT,
            texts INTEGER,
            max_days INTEGER,
            last_collected TEXT,
            page_id TEXT,
            hunter INTEGER
        )
        """)
        for col in ("last_collected", "last_launch", "max_days", "ads_count", "hunter"):
            cur.execute(f"CREATE INDEX IF NOT EXISTS idx_product_groups_{col} ON product_groups ({col})")
        conn.commit()

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

        # ── Niche taxonomy + per-ad classification (multi-label) ──────────
        cur.execute(f"""
        CREATE TABLE IF NOT EXISTS niches (
            id {idcol},
            slug TEXT UNIQUE,
            name_ar TEXT,
            name_fr TEXT,
            description TEXT,
            keywords TEXT,
            negatives TEXT,
            enabled INTEGER DEFAULT 1,
            taxonomy_version INTEGER DEFAULT 1,
            created_at TEXT,
            updated_at TEXT
        )
        """)
        cur.execute("""
        CREATE TABLE IF NOT EXISTS ad_niches (
            ad_id TEXT NOT NULL,
            niche_id INTEGER NOT NULL,
            score REAL DEFAULT 0,
            confidence INTEGER DEFAULT 0,
            method TEXT DEFAULT 'rules',
            updated_at TEXT,
            PRIMARY KEY (ad_id, niche_id, method)
        )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_ad_niches_niche ON ad_niches (niche_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_ad_niches_ad ON ad_niches (ad_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_meta_ads_niche ON meta_ads (primary_niche)")
        # Winning products: a product (same advertiser + same product page =
        # product_key) a user saved to follow, with personal notes. Its ads,
        # status and launch/stop history come from meta_ads / ad_events.
        cur.execute(f"""
        CREATE TABLE IF NOT EXISTS winning_products (
            id {idcol},
            user_id INTEGER NOT NULL,
            product_key TEXT NOT NULL,
            ad_id TEXT,
            page_id TEXT,
            page_name TEXT,
            store_domain TEXT,
            link_url TEXT,
            body TEXT,
            notes TEXT DEFAULT '',
            stage TEXT DEFAULT 'watching',
            created_at TEXT,
            updated_at TEXT,
            UNIQUE (user_id, product_key)
        )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_winning_pkey ON winning_products (product_key)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_ad_events_pkey ON ad_events (product_key)")
        # AI competitor reports: one store's ads summarised into a dossier and
        # analysed by the configured LLM. Kept with their date for later reading.
        cur.execute(f"""
        CREATE TABLE IF NOT EXISTS ai_reports (
            id {idcol},
            user_id INTEGER,
            store_domain TEXT,
            platform TEXT,
            status TEXT DEFAULT 'running',
            provider TEXT,
            model TEXT,
            lang TEXT,
            note TEXT DEFAULT '',
            ads_count INTEGER DEFAULT 0,
            unique_texts INTEGER DEFAULT 0,
            tokens_est INTEGER DEFAULT 0,
            prompt_tokens INTEGER,
            completion_tokens INTEGER,
            cost REAL,
            dossier TEXT,
            report TEXT,
            error TEXT,
            created_at TEXT,
            finished_at TEXT
        )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_ai_reports_user ON ai_reports (user_id, created_at)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_ai_reports_domain ON ai_reports (store_domain)")
        # kind: 'report' (strategy report) or 'audit' (per-product decision table);
        # audit: the audit rows as JSON.
        for col, ddl in (("kind", "TEXT DEFAULT 'report'"), ("audit", "TEXT")):
            try:
                if self.is_postgres:
                    cur.execute(f"ALTER TABLE ai_reports ADD COLUMN IF NOT EXISTS {col} {ddl}")
                else:
                    cols = [r[1] for r in cur.execute("PRAGMA table_info(ai_reports)").fetchall()]
                    if col not in cols:
                        cur.execute(f"ALTER TABLE ai_reports ADD COLUMN {col} {ddl}")
            except Exception:
                pass
        # Seed/refresh the taxonomy from code (idempotent by slug).
        try:
            p = "%s" if self.is_postgres else "?"
            now_seed = _now()
            for n in niche_defs.NICHES:
                kw_json = json.dumps([{"k": k, "w": w} for k, w in n["keywords"]], ensure_ascii=False)
                neg_json = json.dumps(n.get("negatives", []), ensure_ascii=False)
                cur.execute(f"SELECT id FROM niches WHERE slug = {p}", (n["slug"],))
                if cur.fetchone():
                    cur.execute(
                        f"UPDATE niches SET name_ar={p}, name_fr={p}, keywords={p}, negatives={p}, "
                        f"enabled=1, taxonomy_version=1, updated_at={p} WHERE slug={p}",
                        (n["name_ar"], n["name_fr"], kw_json, neg_json, now_seed, n["slug"]))
                else:
                    cur.execute(
                        f"INSERT INTO niches (slug, name_ar, name_fr, keywords, negatives, "
                        f"enabled, taxonomy_version, created_at, updated_at) "
                        f"VALUES ({p},{p},{p},{p},{p},1,1,{p},{p})",
                        (n["slug"], n["name_ar"], n["name_fr"], kw_json, neg_json,
                         now_seed, now_seed))
        except Exception as exc:  # noqa: BLE001
            logger.warning("niche seed notice: %s", exc)

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

        try:
            self._backfill_ad_events(cur, conn)
        except Exception as ev_err:  # noqa: BLE001
            logger.warning("ad_events backfill notice: %s", ev_err)

        conn.commit()
        conn.close()
        try:
            self._load_niches()
        except Exception as exc:  # noqa: BLE001
            logger.warning("niche cache notice: %s", exc)
        try:
            self._load_blocked_domains()
            purged = self.purge_blocked_ads()
            if purged:
                logger.info("purged %s big-marketplace ads", purged)
            purged = self.purge_junk_ads()
            if purged:
                logger.info("purged %s drama-app / foreign-site ads", purged)
            purged = self.purge_non_arabic_ads()
            if purged:
                logger.info("purged %s non-Arabic ads", purged)
        except Exception as exc:  # noqa: BLE001
            logger.warning("blocked-domain purge notice: %s", exc)
        if os.environ.get("DB_SPEED_INDEXES", "1").strip().lower() not in ("0", "false", "no"):
            if self.is_postgres:
                threading.Thread(target=self.ensure_speed_indexes, name="db-indexes", daemon=True).start()
            else:  # SQLite (local/tests): instant, and no concurrent writer
                self.ensure_speed_indexes()

    # Indexes behind the ads listing: sort columns (so "newest / longest /
    # hunter … LIMIT 60" reads 60 index entries instead of sorting 100k rows)
    # and the selective filters (page, target, launch date).
    _SPEED_INDEXES = (
        ("idx_meta_ads_collected", "meta_ads (collected_at)"),
        ("idx_meta_ads_start", "meta_ads (start_date)"),
        ("idx_meta_ads_days", "meta_ads (days_active)"),
        ("idx_meta_ads_hunter", "meta_ads (hunter_score)"),
        ("idx_meta_ads_page", "meta_ads (page_id)"),
        ("idx_meta_ads_target", "meta_ads (target_id)"),
    )
    # PostgreSQL only: trigram indexes make the text search (LIKE '%…%')
    # use an index instead of reading every ad body.
    _TRGM_INDEXES = (
        ("idx_meta_ads_body_trgm", "meta_ads USING gin (body gin_trgm_ops)"),
        ("idx_meta_ads_pname_trgm", "meta_ads USING gin (page_name gin_trgm_ops)"),
        ("idx_meta_ads_domain_trgm", "meta_ads USING gin (store_domain gin_trgm_ops)"),
    )

    def ensure_speed_indexes(self) -> None:
        """Create the listing indexes if missing. Runs in a background thread
        at startup; on PostgreSQL the build is CONCURRENTLY, so scraping and
        the dashboard keep working while a big table is indexed."""
        t0 = time.perf_counter()
        made = []
        if not self.is_postgres:
            for name, target in self._SPEED_INDEXES:
                try:
                    self._execute(f"CREATE INDEX IF NOT EXISTS {name} ON {target}")
                except Exception as exc:  # noqa: BLE001
                    logger.warning("index %s notice: %s", name, exc)
            return
        wanted = list(self._SPEED_INDEXES)
        try:
            self._execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
            wanted += list(self._TRGM_INDEXES)
        except Exception as exc:  # noqa: BLE001
            logger.warning("pg_trgm unavailable, text search stays unindexed: %s", exc)
        for name, target in wanted:
            try:
                row = self._query_one(
                    "SELECT i.indisvalid AS ok FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid "
                    "WHERE c.relname = %s", (name,))
                if row and row.get("ok"):
                    continue
                if row:  # left invalid by an interrupted concurrent build
                    self._execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
                self._execute(f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {name} ON {target}")
                made.append(name)
            except Exception as exc:  # noqa: BLE001
                logger.warning("index %s notice: %s", name, exc)
        if made:
            try:
                self._execute("ANALYZE meta_ads")
            except Exception:  # noqa: BLE001
                pass
            logger.info("speed indexes built in %.1fs: %s", time.perf_counter() - t0, ", ".join(made))

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

    # ── ad lifecycle events (launched / stopped / resumed) ────────────────
    _EVENT_COLS = ("ad_id", "market", "page_id", "store_domain", "product_key", "query",
                   "page_name", "body", "link_url", "event", "event_at", "detail", "created_at")

    def _event_values(self, ad_id: str, event: str, event_at: str, detail: dict,
                      row: dict) -> tuple:
        market = "global" if str((row or {}).get("market") or "dz").lower() == "global" else "dz"
        body = str((row or {}).get("body") or "")
        return (
            str(ad_id), market,
            str((row or {}).get("page_id") or ""),
            str((row or {}).get("store_domain") or ""),
            str((row or {}).get("product_key") or ""),
            str((row or {}).get("query") or ""),
            str((row or {}).get("page_name") or ""),
            body[:2000],
            str((row or {}).get("link_url") or ""),
            event, event_at or _now(),
            json.dumps(detail or {}, ensure_ascii=False), _now(),
        )

    def _log_ad_events(self, events: list[tuple]) -> None:
        if not events:
            return
        p = self._ph()
        cols = ", ".join(self._EVENT_COLS)
        ph = ", ".join([p] * len(self._EVENT_COLS))
        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.executemany(f"INSERT INTO ad_events ({cols}) VALUES ({ph})", events)
            conn.commit()
        except Exception as exc:  # noqa: BLE001
            logger.warning("ad_events insert failed: %s", exc)
        finally:
            conn.close()

    def _backfill_ad_events(self, cur, conn) -> None:
        """One-time history seed from the current meta_ads columns."""
        p = self._ph()
        detail = json.dumps({"backfilled": True}, ensure_ascii=False)
        now = _now()
        cols = ", ".join(self._EVENT_COLS)
        select_common = (
            "m.ad_id, COALESCE(m.market, 'dz'), m.page_id, m.store_domain, m.product_key, "
            "m.query, m.page_name, m.body, m.link_url")
        cur.execute(
            f"INSERT INTO ad_events ({cols}) "
            f"SELECT {select_common}, 'launched', "
            f"COALESCE(m.first_seen, m.start_date, m.collected_at), {p}, {p} "
            f"FROM meta_ads m WHERE NOT EXISTS ("
            f"SELECT 1 FROM ad_events e WHERE e.ad_id=m.ad_id AND e.event='launched')",
            (detail, now))
        launched = cur.rowcount or 0
        cur.execute(
            f"INSERT INTO ad_events ({cols}) "
            f"SELECT {select_common}, 'stopped', "
            f"COALESCE(m.stopped_at, m.last_seen_active), {p}, {p} "
            f"FROM meta_ads m WHERE m.is_active = 0 "
            f"AND (m.stopped_at IS NOT NULL OR m.last_seen_active IS NOT NULL) "
            f"AND NOT EXISTS (SELECT 1 FROM ad_events e "
            f"WHERE e.ad_id=m.ad_id AND e.event='stopped')",
            (detail, now))
        stopped = cur.rowcount or 0
        conn.commit()
        if launched or stopped:
            logger.info("ad_events backfilled: %s launched, %s stopped", launched, stopped)

    def _target_event_scope(self, target: dict) -> tuple[str, list]:
        p = self._ph()
        typ = str(target.get("type") or "keyword").strip().lower()
        val = str(target.get("target_value") or "").strip()
        mkt = str(target.get("market") or "dz").lower()
        mcond = "e.market = 'global'" if mkt == "global" else "(e.market IS NULL OR e.market = 'dz')"
        if typ == "domain":
            dom = val.lower()
            if dom.startswith("www."):
                dom = dom[4:]
            return f"({mcond}) AND (LOWER(e.store_domain) = {p} OR e.query = {p})", [dom, val]
        if typ == "page":
            return f"({mcond}) AND e.page_id = {p}", [val]
        # keyword / custom_url events carry the scrape query.
        return f"({mcond}) AND e.query = {p}", [val]

    @staticmethod
    def _period_days(start, end) -> Optional[int]:
        s = _to_algeria_date(str(start or ""))
        e = _to_algeria_date(str(end or ""))
        if not s or not e:
            return None
        return max(1, (e - s).days + 1)

    def _build_ad_activity(self, events: list[dict]) -> list[dict]:
        """Group the event stream into one summary per ad, with running periods.

        Each period is a closed/open run: ``start`` -> ``end`` + ``days``. A
        stopped ad can have several periods (stop -> resume -> stop...)."""
        grouped: dict[str, list] = {}
        for e in events:
            grouped.setdefault(str(e.get("ad_id") or ""), []).append(e)
        out: list[dict] = []
        for ad_id, evs in grouped.items():
            if not ad_id:
                continue
            evs.sort(key=lambda x: (x.get("event_at") or "", x.get("id") or 0))
            last = evs[-1]
            current_active = last.get("current_active")
            current_active = bool(current_active) if current_active is not None else None
            periods: list[dict] = []
            open_p = None
            relaunch_of = ""
            for e in evs:
                ev = e.get("event")
                d = e.get("detail") or {}
                if ev == "launched":
                    open_p = {
                        "start": d.get("start_date") or str(e.get("event_at") or "")[:10],
                        "detected_at": e.get("event_at") or "",
                        "kind": "launch",
                        "relaunch_of": d.get("relaunch_of") or "",
                        "previous_stopped_at": d.get("previous_stopped_at") or "",
                        "downtime_days": d.get("downtime_days"),
                    }
                    if d.get("relaunch_of"):
                        relaunch_of = str(d["relaunch_of"])
                elif ev == "resumed":
                    open_p = {
                        "start": e.get("event_at") or "",
                        "detected_at": e.get("event_at") or "",
                        "kind": "resume",
                        "previous_stopped_at": d.get("previous_stopped_at") or "",
                        "downtime_days": d.get("downtime_days"),
                    }
                elif ev == "stopped":
                    start = open_p.get("start") if open_p else ""
                    days = self._period_days(start, e.get("event_at"))
                    if days is None:
                        days = d.get("days_active")
                    period = {
                        "start": start, "end": e.get("event_at") or "",
                        "days": days, "open": False,
                        "stop_detected_at": e.get("created_at") or "",
                        "kind": open_p.get("kind", "unknown") if open_p else "unknown",
                        "relaunch_of": open_p.get("relaunch_of", "") if open_p else "",
                    }
                    periods.append(period)
                    e["period_start"] = period["start"]
                    e["period_end"] = period["end"]
                    e["period_days"] = days
                    e["stop_detected_at"] = period["stop_detected_at"]
                    open_p = None
            if open_p:
                if current_active:
                    end = algeria_today().isoformat()
                    is_open = True
                else:
                    end = last.get("meta_stopped_at") or last.get("meta_last_seen_active") \
                        or open_p.get("detected_at")
                    is_open = False
                periods.append({
                    "start": open_p.get("start") or "", "end": end,
                    "days": self._period_days(open_p.get("start"), end),
                    "open": is_open, "stop_detected_at": "",
                    "kind": open_p.get("kind", "unknown"),
                    "relaunch_of": open_p.get("relaunch_of", ""),
                })
            closed = [x for x in periods if not x.get("open")]
            out.append({
                "ad_id": ad_id,
                "page_name": last.get("page_name") or "",
                "body": last.get("body") or "",
                "link_url": last.get("link_url") or "",
                "store_domain": last.get("store_domain") or "",
                "product_key": last.get("product_key") or "",
                "current_active": current_active,
                "meta_start_date": last.get("meta_start_date") or "",
                "first_event_at": evs[0].get("event_at") or "",
                "last_event_at": last.get("event_at") or "",
                "periods": periods,
                "cycles": len(periods),
                "total_running_days": sum(int(x.get("days") or 0) for x in periods),
                "last_stop_at": closed[-1]["end"] if closed else "",
                "last_stop_detected_at": closed[-1].get("stop_detected_at") if closed else "",
                "relaunch_of": relaunch_of,
                "relaunched_by": [],
                "events_count": len(evs),
            })
        by_old: dict[str, list] = {}
        for s in out:
            if s.get("relaunch_of"):
                by_old.setdefault(str(s["relaunch_of"]), []).append(s["ad_id"])
        for s in out:
            s["relaunched_by"] = by_old.get(s["ad_id"], [])
        out.sort(key=lambda s: s.get("last_event_at") or "", reverse=True)
        return out

    def target_activity(self, target: dict, limit: int = 200) -> dict:
        """Rich timeline for one competitor: launches, stops, resumes + scans."""
        p = self._ph()
        ev_scope, ev_params = self._target_event_scope(target)
        fetch = max(int(limit), 3000)
        events = self._query(
            f"SELECT e.*, m.is_active AS current_active, m.start_date AS meta_start_date, "
            f"m.days_active AS current_days, m.stopped_at AS meta_stopped_at, "
            f"m.last_seen_active AS meta_last_seen_active, m.preview_image_url "
            f"FROM ad_events e LEFT JOIN meta_ads m ON m.ad_id = e.ad_id "
            f"WHERE {ev_scope} ORDER BY e.event_at DESC, e.id DESC LIMIT {int(fetch)}",
            tuple(ev_params))
        for r in events:
            try:
                r["detail"] = json.loads(r.get("detail") or "{}")
            except Exception:
                r["detail"] = {}
        ads = self._build_ad_activity(events)
        timeline = events[:int(limit)]
        ad_scope, ad_params = self._target_ad_scope(target)
        srow = self._query_one(
            f"SELECT COUNT(*) AS total, "
            f"SUM(CASE WHEN is_active = 1 THEN 1 ELSE 0 END) AS active "
            f"FROM meta_ads WHERE {ad_scope}", tuple(ad_params)) or {}
        cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        counts = {r["event"]: int(r["c"] or 0) for r in self._query(
            f"SELECT e.event, COUNT(*) AS c FROM ad_events e WHERE {ev_scope} "
            f"AND e.event_at >= {p} GROUP BY e.event", tuple(ev_params) + (cutoff,))}
        scans = []
        if str(target.get("type") or "") == "page":
            scans = self._query(
                f"SELECT captured_at, active_ads_count, total_ads_count FROM page_snapshots "
                f"WHERE page_id={p} ORDER BY id DESC LIMIT 100",
                (str(target.get("target_value") or ""),))
        total = int(srow.get("total") or 0)
        active = int(srow.get("active") or 0)
        return {
            "target": target,
            "summary": {
                "total_ads": total,
                "active_ads": active,
                "stopped_ads": max(0, total - active),
                "launched_7d": counts.get("launched", 0),
                "stopped_7d": counts.get("stopped", 0),
                "resumed_7d": counts.get("resumed", 0),
            },
            "ads": ads[:500],
            "events": timeline,
            "scans": scans,
        }

    # ── low-level helpers ─────────────────────────────────────────────────
    def _execute(self, sql: str, params: tuple = ()) -> None:
        t0 = time.perf_counter()
        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute(sql, params)
            conn.commit()
        finally:
            conn.close()
            _log_if_slow(sql, t0)

    def _query(self, sql: str, params: tuple = ()) -> list[dict]:
        t0 = time.perf_counter()
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
            _log_if_slow(sql, t0)

    # ── short-lived result cache (counters only) ──────────────────────────
    # Totals and filter-bar counters scan the whole ads table. They are asked
    # for on every page change and by several timers, but nobody needs them to
    # the second: they are reused for CACHE_TTL seconds and dropped at once
    # when ads are deleted or a user's scope / a filter setting changes.
    CACHE_TTL = float(os.environ.get("DB_CACHE_TTL", "30"))

    def _cached(self, key: tuple, compute):
        if self.CACHE_TTL <= 0:
            return compute()
        now = time.monotonic()
        with _CACHE_LOCK:
            hit = self._result_cache.get(key)
            if hit and now - hit[0] < self.CACHE_TTL:
                return hit[1]
        val = compute()
        with _CACHE_LOCK:
            if len(self._result_cache) > 2000:
                self._result_cache.clear()
            self._result_cache[key] = (now, val)
        return val

    def invalidate_cache(self) -> None:
        with _CACHE_LOCK:
            self._result_cache.clear()

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

    @_invalidates_cache
    def link_user_ad(self, user_id: int, ad_id: str) -> None:
        aid = str(ad_id or "").strip()
        if not user_id or not aid:
            return
        p = self._ph()
        conflict = "ON CONFLICT (user_id, ad_id) DO NOTHING"
        self._execute(f"INSERT INTO user_ads (user_id, ad_id, saved_at) VALUES ({p},{p},{p}) {conflict}",
                      (int(user_id), aid, _now()))

    @_invalidates_cache
    def link_user_page(self, user_id: int, page_id: str) -> None:
        pid = str(page_id or "").strip()
        if not user_id or not pid:
            return
        p = self._ph()
        conflict = "ON CONFLICT (user_id, page_id) DO NOTHING"
        self._execute(f"INSERT INTO user_pages (user_id, page_id, tracked_at) VALUES ({p},{p},{p}) {conflict}",
                      (int(user_id), pid, _now()))

    @_invalidates_cache
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

    # ── daily status check (is the ad still running?) ─────────────────────
    def status_check_pages(self) -> list[str]:
        """Pages that still have ads marked active, in a stable order."""
        rows = self._query("SELECT DISTINCT page_id FROM meta_ads "
                           "WHERE is_active = 1 AND page_id IS NOT NULL AND page_id != '' "
                           "AND (market IS NULL OR market = 'dz') ORDER BY page_id")
        return [str(r["page_id"]) for r in rows]

    def top_active_pages(self, n: int = 2) -> list[str]:
        rows = self._query("SELECT page_id, COUNT(*) AS c FROM meta_ads WHERE is_active = 1 "
                           "AND page_id IS NOT NULL AND (market IS NULL OR market = 'dz') "
                           f"AND page_id != '' GROUP BY page_id ORDER BY c DESC LIMIT {int(n)}")
        return [str(r["page_id"]) for r in rows]

    def active_ads_of_pages(self, page_ids: list[str]) -> list[dict]:
        if not page_ids:
            return []
        p = self._ph()
        return self._query(
            f"SELECT ad_id, page_id, collation_id, product_key, last_seen_active FROM meta_ads "
            f"WHERE is_active = 1 AND (market IS NULL OR market = 'dz') "
            f"AND page_id IN ({', '.join([p] * len(page_ids))})", tuple(page_ids))

    @_changes_ads
    def apply_status_check(self, confirmed: dict[str, str], stopped: list[str]) -> None:
        """confirmed = {ad_id: collation_id or ''} seen running now; stopped =
        ad_ids gone from Meta. A stopped ad keeps last_seen_active (its last
        confirmed day), so days_active stops counting there."""
        p = self._ph()
        now = _now()
        conn = self._connect()
        try:
            cur = conn.cursor()
            if confirmed:
                cur.executemany(
                    f"UPDATE meta_ads SET is_active = 1, stopped_at = NULL, last_seen_active = {p}, "
                    f"status_checked_at = {p}, collation_id = COALESCE(NULLIF({p}, ''), collation_id) "
                    f"WHERE ad_id = {p}",
                    [(now, now, cid or "", aid) for aid, cid in confirmed.items()])
            if stopped:
                cur.executemany(
                    f"UPDATE meta_ads SET is_active = 0, status_checked_at = {p}, "
                    f"stopped_at = COALESCE(last_seen_active, {p}) WHERE ad_id = {p}",
                    [(now, now, aid) for aid in stopped])
            conn.commit()
        finally:
            conn.close()
        events: list[tuple] = []
        for aid in stopped:
            row = self._query_one(
                f"SELECT ad_id, page_name, page_id, store_domain, product_key, query, body, "
                f"link_url, market, start_date, first_seen, last_seen_active, stopped_at "
                f"FROM meta_ads WHERE ad_id = {p}", (aid,))
            if row:
                d = self._calc_days_active(row.get("start_date") or "", row.get("first_seen") or "",
                                           False, row.get("last_seen_active") or "")
                self._execute(f"UPDATE meta_ads SET days_active = {p} WHERE ad_id = {p}", (d, aid))
                events.append(self._event_values(
                    aid, "stopped", row.get("stopped_at") or now,
                    {"days_active": d,
                     "last_seen_active": row.get("last_seen_active") or "",
                     "start_date": row.get("start_date") or ""},
                    dict(row)))
        self._log_ad_events(events)

    # ── on-demand target re-scan reconciliation ───────────────────────────
    def _target_ad_scope(self, target: dict) -> tuple[str, list]:
        """SQL condition matching the ads a competitor target owns."""
        p = self._ph()
        typ = str(target.get("type") or "keyword").strip().lower()
        val = str(target.get("target_value") or "").strip()
        mkt = str(target.get("market") or "dz").lower()
        mcond = "market = 'global'" if mkt == "global" else "(market IS NULL OR market = 'dz')"
        if typ == "domain":
            dom = val.lower()
            if dom.startswith("www."):
                dom = dom[4:]
            return f"({mcond}) AND (LOWER(store_domain) = {p} OR query = {p})", [dom, val]
        if typ == "page":
            return f"({mcond}) AND (page_id = {p} OR query = {p})", [val, val]
        if typ == "custom_url":
            return f"({mcond}) AND query = {p}", [val]
        return (f"({mcond}) AND (query = {p} OR ad_id IN "
                f"(SELECT ad_id FROM ad_keywords WHERE keyword = {p}))", [val, val])

    @_changes_ads
    def reconcile_target_ads(self, target: dict, seen_ids: list[str],
                             seen_collations: Optional[list[str]] = None,
                             seen_product_keys: Optional[list[str]] = None) -> dict:
        """Mark this target's active ads that the fresh scan did not return as
        stopped, exactly like the daily status check does.

        A previously active ad is kept alive when the scan returned its id, a
        copy of its collation group, or another ad of the same product."""
        clause, params = self._target_ad_scope(target)
        rows = self._query(
            f"SELECT ad_id, collation_id, product_key FROM meta_ads "
            f"WHERE is_active = 1 AND ({clause})", tuple(params))
        ids = {str(x) for x in (seen_ids or [])}
        colls = {str(x) for x in (seen_collations or []) if x}
        pkeys = {str(x) for x in (seen_product_keys or []) if x}
        confirmed: dict[str, str] = {}
        stopped: list[str] = []
        for r in rows:
            aid = str(r["ad_id"])
            cid = str(r.get("collation_id") or "")
            pk = str(r.get("product_key") or "")
            if aid in ids or (cid and cid in colls) or (pk and pk in pkeys):
                confirmed[aid] = ""
            else:
                stopped.append(aid)
        if confirmed or stopped:
            self.apply_status_check(confirmed, stopped)
        return {"checked": len(rows), "confirmed": len(confirmed), "stopped": len(stopped)}

    @_changes_ads
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
                target_id: Optional[int] = None, market: str = "dz") -> bool:
        """Upsert an ad with lifecycle tracking. Returns True if the ad is NEW."""
        p = self._ph()
        ad_id = str(ad.get("ad_id") or "")
        if not ad_id:
            return False
        market = "global" if str(market or "").strip().lower() == "global" else "dz"

        existing = self._query_one(
            f"SELECT first_seen, start_date, product_key, is_active, stopped_at, last_seen_active, "
            f"days_active FROM meta_ads WHERE ad_id={p}", (ad_id,))
        is_new = existing is None
        old_pkey = str((existing or {}).get("product_key") or "")
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
        ad["market"] = market
        preview_img = _web_url(ad.get("preview_image_url"))
        # Big marketplaces are never stored (they are also purged on startup).
        if self.is_blocked_domain(ad.get("store_domain") or "") or self.is_unwanted_ad(ad):
            return False
        from store_filter import product_key
        pkey = product_key(str(ad.get("page_id") or ""), ad.get("link_url") or "",
                           ad.get("body") or "", ad.get("store_domain") or "")
        cols = ("ad_id", "page_name", "page_id", "start_date", "is_active", "link_url",
                "store_domain", "platform", "body", "cta_text", "collation_count",
                "publisher_platforms", "query", "country", "collected_at", "first_seen",
                "last_seen_active", "days_active", "target_id", "preview_image_url",
                "video_url", "video_sd_url", "product_key", "collation_id", "market")
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
            _web_url(ad.get("video_url")),
            _web_url(ad.get("video_sd_url")),
            pkey,
            str(ad.get("collation_id") or ""),
            market,
        )
        placeholders = ", ".join([p] * len(cols))
        col_list = ", ".join(cols)
        conn = self._connect()
        try:
            cur = conn.cursor()
            # first_seen is preserved on updates
            if self.is_postgres:
                updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in cols if c not in ("ad_id", "first_seen", "video_sd_url"))
                updates = updates.replace("collation_id = EXCLUDED.collation_id",
                                          "collation_id = COALESCE(NULLIF(EXCLUDED.collation_id, ''), meta_ads.collation_id)")
                updates += ", video_sd_url = COALESCE(NULLIF(EXCLUDED.video_sd_url, ''), meta_ads.video_sd_url)"
                updates += ", stopped_at = CASE WHEN EXCLUDED.is_active = 1 THEN NULL ELSE meta_ads.stopped_at END"
                sql = (f"INSERT INTO meta_ads ({col_list}) VALUES ({placeholders}) "
                       f"ON CONFLICT (ad_id) DO UPDATE SET {updates}")
            else:
                updates = ", ".join(f"{c} = excluded.{c}" for c in cols if c not in ("ad_id", "first_seen", "video_sd_url"))
                updates = updates.replace("collation_id = excluded.collation_id",
                                          "collation_id = COALESCE(NULLIF(excluded.collation_id, ''), meta_ads.collation_id)")
                updates += ", video_sd_url = COALESCE(NULLIF(excluded.video_sd_url, ''), meta_ads.video_sd_url)"
                updates += ", stopped_at = CASE WHEN excluded.is_active = 1 THEN NULL ELSE meta_ads.stopped_at END"
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
            # Niche classification (multi-label, rules-based) for new ads.
            try:
                self._save_niche_rows(cur, ad_id, self._classify_results(ad), now)
            except Exception as niche_err:  # noqa: BLE001
                logger.debug("niche classify failed: %s", niche_err)
            # Lifecycle timeline: log launches, stops and resumes only when
            # the state actually changes (repeated scans stay quiet).
            try:
                ev_row = dict(ad)
                ev_row["query"] = query
                ev_row["product_key"] = pkey
                ev_row["market"] = market
                event, detail = None, {}
                incoming_active = bool(ad.get("is_active", True))
                was_active = bool(existing.get("is_active")) if existing else None

                def _days_ago(value):
                    d = _to_algeria_date(value or "")
                    return max(0, (algeria_today() - d).days) if d else None

                if is_new:
                    event = "launched"
                    detail = {"start_date": start_date, "detected_at": now}
                    # A new ad for a product whose older ads are all stopped is
                    # a relaunch, not just another creative.
                    if pkey:
                        prev = cur.execute(
                            f"SELECT ad_id, COALESCE(stopped_at, last_seen_active) AS stopped "
                            f"FROM meta_ads WHERE product_key={p} AND ad_id<>{p} AND is_active=0 "
                            f"ORDER BY COALESCE(stopped_at, last_seen_active) DESC LIMIT 1",
                            (pkey, ad_id)).fetchone()
                        act = cur.execute(
                            f"SELECT COUNT(*) AS c FROM meta_ads WHERE product_key={p} "
                            f"AND ad_id<>{p} AND is_active=1", (pkey, ad_id)).fetchone()
                        if prev and int(act[0] or 0) == 0:
                            detail.update({
                                "relaunch_of": str(prev[0]),
                                "previous_stopped_at": prev[1] or "",
                                "downtime_days": _days_ago(prev[1]),
                            })
                elif was_active is not None and was_active != incoming_active:
                    if incoming_active:
                        event = "resumed"
                        detail = {
                            "previous_stopped_at": existing.get("stopped_at") or "",
                            "downtime_days": _days_ago(
                                existing.get("stopped_at") or existing.get("last_seen_active")),
                            "days_active_before": int(existing.get("days_active") or 0),
                        }
                    else:
                        event = "stopped"
                        detail = {
                            "days_active": int(existing.get("days_active") or days_active),
                            "last_seen_active": existing.get("last_seen_active") or "",
                        }
                if event:
                    cur.execute(
                        f"INSERT INTO ad_events ({', '.join(self._EVENT_COLS)}) "
                        f"VALUES ({', '.join([p] * len(self._EVENT_COLS))})",
                        self._event_values(ad_id, event, now, detail, ev_row))
            except Exception as ev_err:  # noqa: BLE001
                logger.debug("ad event log failed: %s", ev_err)
            # Product strength for every ad of this product (and the old group
            # if the ad moved). Persisted so the winning preset can filter SQL.
            try:
                score, _ = self._group_hunter_score(cur, pkey)
                cur.execute(f"UPDATE meta_ads SET hunter_score={p} WHERE product_key={p}",
                            (score, pkey))
                if old_pkey and old_pkey != pkey:
                    old_score, _ = self._group_hunter_score(cur, old_pkey)
                    cur.execute(f"UPDATE meta_ads SET hunter_score={p} WHERE product_key={p}",
                                (old_score, old_pkey))
            except Exception as score_err:  # noqa: BLE001
                logger.debug("hunter score failed: %s", score_err)
            conn.commit()
            self.mark_groups_dirty(pkey, old_pkey)
            return is_new
        except Exception as exc:
            logger.error("save_ad failed: %s", exc)
            return False
        finally:
            conn.close()

    def ad_exists(self, ad_id: str) -> bool:
        return self._query_one(f"SELECT 1 AS x FROM meta_ads WHERE ad_id={self._ph()}", (str(ad_id),)) is not None

    # ── persisted Hunter Score (product strength) ─────────────────────────
    def _group_hunter_score(self, cur, product_key: str) -> tuple[int, int]:
        """Score one product group using the same rules as ``scoring.hunter``.

        Returns ``(score, ads_count)``; ``(0, 0)`` when the group is empty."""
        if not product_key:
            return 0, 0
        p = self._ph()
        cur.execute(
            f"SELECT COUNT(*) AS c, SUM(is_active) AS active, COUNT(DISTINCT body) AS texts, "
            f"MIN(start_date) AS first_launch, MAX(collected_at) AS last_collected "
            f"FROM meta_ads WHERE product_key={p}", (product_key,))
        row = cur.fetchone()
        if not row:
            return 0, 0
        count = int(row[0] or 0)
        if count <= 0:
            return 0, 0
        cur.execute(
            f"SELECT DISTINCT ak.keyword FROM ad_keywords ak "
            f"JOIN meta_ads m ON m.ad_id = ak.ad_id WHERE m.product_key={p}", (product_key,))
        keywords = [str(r[0]) for r in cur.fetchall() if r and r[0]]
        result = scoring.hunter({
            "group": {
                "count": count,
                "active": int(row[1] or 0),
                "texts": int(row[2] or 0),
                "first_launch": row[3] or "",
                "last_collected": row[4] or "",
            },
            "keywords": keywords,
        })
        return int(result["score"]), count

    @_changes_ads
    def backfill_hunter_scores(self, force: bool = False, batch: int = 500) -> int:
        """(Re)compute ``meta_ads.hunter_score`` for every product group.

        Ads scored before the column existed are picked up automatically; with
        ``force=True`` every score is recomputed. Returns the number of ads
        written."""
        if force:
            self._execute("UPDATE meta_ads SET hunter_score = NULL")
        p = self._ph()
        total = 0
        while True:
            rows = self._query(
                f"SELECT DISTINCT product_key FROM meta_ads "
                f"WHERE hunter_score IS NULL AND product_key IS NOT NULL AND product_key <> '' "
                f"LIMIT {int(batch)}")
            if not rows:
                break
            keys = [str(r["product_key"]) for r in rows]
            conn = self._connect()
            try:
                cur = conn.cursor()
                for key in keys:
                    score, count = self._group_hunter_score(cur, key)
                    cur.execute(f"UPDATE meta_ads SET hunter_score={p} WHERE product_key={p}",
                                (score, key))
                    total += count
                conn.commit()
            finally:
                conn.close()
            if len(keys) < batch:
                break
        # Ads with no product key cannot be grouped; score them on their own.
        while True:
            rows = self._query(
                "SELECT * FROM meta_ads WHERE hunter_score IS NULL "
                "AND (product_key IS NULL OR product_key = '') LIMIT 500")
            if not rows:
                break
            rows = self.attach_keywords(rows)
            updates = []
            for r in rows:
                try:
                    updates.append((int(scoring.hunter(r).get("score", 0)), r["ad_id"]))
                except Exception:  # noqa: BLE001
                    updates.append((0, r["ad_id"]))
            conn = self._connect()
            try:
                cur = conn.cursor()
                cur.executemany(f"UPDATE meta_ads SET hunter_score={p} WHERE ad_id={p}", updates)
                conn.commit()
            finally:
                conn.close()
            total += len(updates)
        return total

    # ── CTA allow-list (which call-to-action buttons are shown) ───────────
    DEFAULT_CTA_ALLOWLIST = ("SHOP_NOW", "BUY_NOW", "ORDER_NOW", "GET_OFFER", "MESSAGE_PAGE",
                             "WHATSAPP_MESSAGE", "GET_DETAILS", "LEARN_MORE")
    _cta_cache: tuple = (0.0, None)

    def cta_allowlist(self) -> list[str]:
        """CTAs an ad must have to be shown ([] = filter off). Cached briefly:
        it is read on every ads query."""
        import time as _t
        ts, val = self._cta_cache
        if val is not None and _t.monotonic() - ts < 10:
            return val
        st = {r["key"]: r["value"] for r in self._query(
            "SELECT key, value FROM settings WHERE key IN ('cta_filter_enabled', 'cta_allowlist')")}
        if st.get("cta_filter_enabled", "true").lower() not in ("1", "true", "yes", "on"):
            val = []
        else:
            raw = st.get("cta_allowlist")
            items = raw.split(",") if raw is not None else list(self.DEFAULT_CTA_ALLOWLIST)
            val = sorted({x.strip().upper() for x in items if x.strip()})
        self._cta_cache = (_t.monotonic(), val)
        return val

    def _cta_clause(self) -> tuple[str, list]:
        allow = self.cta_allowlist()
        if not allow:
            return "", []
        return f"UPPER(COALESCE(cta_text, '')) IN ({', '.join([self._ph()] * len(allow))})", list(allow)

    def _build_ads_filter(self, search: str = "", stores_only: bool = False,
                           query: str = "", store_domain: str = "",
                           page_id: str = "", preset: str = "", active_only: bool = False,
                           start_date_min: str = "", start_date_max: str = "",
                           min_days_active: int = 0, target_id: Optional[int] = None,
                           user_id: Optional[int] = None,
                           min_product_ads: int = 0,
                           keywords: Optional[list[str]] = None,
                           niches: Optional[list[str]] = None,
                           market: str = "dz") -> tuple[str, list]:
        p = self._ph()
        where = []
        params: list = []

        # 'dz' is the default Arabic bank; 'global' is the foreign side tab.
        market = str(market or "dz").strip().lower()
        if market == "dz":
            where.append("(market IS NULL OR market = 'dz')")
        elif market != "all":
            where.append(f"market = {p}")
            params.append(market)

        # Only ads whose CTA is on the allow-list are ever listed/counted.
        # The foreign market keeps every CTA and language.
        cta_sql, cta_params = ("", []) if market == "global" else self._cta_clause()
        if cta_sql:
            where.append(cta_sql)
            params.extend(cta_params)

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

        if niches:
            ns = [str(n).strip() for n in niches if str(n).strip()]
            if ns:
                in_list = ", ".join([p] * len(ns))
                where.append(
                    f"ad_id IN (SELECT an.ad_id FROM ad_niches an "
                    f"JOIN niches n ON n.id = an.niche_id "
                    f"WHERE n.slug IN ({in_list}) AND an.confidence >= 40)")
                params.extend(ns)

        if target_id is not None and not (page_id or store_domain or query):
            where.append(f"target_id = {p}")
            params.append(target_id)
        if active_only or preset == "active":
            where.append("is_active = 1")
        elif preset == "stopped":
            where.append("is_active = 0")
        elif preset == "winning":
            # Persisted product strength, computed on save and backfilled.
            where.append(f"COALESCE(hunter_score, 0) >= {scoring.WINNING_THRESHOLD}")
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
                keywords: Optional[list[str]] = None,
                niches: Optional[list[str]] = None,
                market: str = "dz") -> list[dict]:
        p = self._ph()
        clause, params = self._build_ads_filter(
            search=search, stores_only=stores_only, query=query,
            store_domain=store_domain, page_id=page_id, preset=preset,
            active_only=active_only, start_date_min=start_date_min,
            start_date_max=start_date_max, min_days_active=min_days_active,
            target_id=target_id, user_id=user_id, min_product_ads=min_product_ads,
            keywords=keywords, niches=niches, market=market,
        )

        order_map = {
            "longest_running": "days_active DESC, is_active DESC",
            "newest_launch": "start_date DESC",
            "most_collated": "collation_count DESC",
            "collected_at": "collected_at DESC",
            "biggest_page": "COALESCE(es.ent_ads, -1) DESC, collected_at DESC",
            "hunter": "COALESCE(hunter_score, 0) DESC, collected_at DESC",
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
        return self.attach_niches(self.attach_keywords(rows))

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

    # ── big-marketplace blocklist (purged permanently) ────────────────────
    def _load_blocked_domains(self) -> None:
        """Extra blocked domains from Settings (comma/newline separated)."""
        extra = ""
        try:
            extra = (self.get_settings().get("blocked_domains") or "")
        except Exception:
            extra = ""
        text = str(extra).replace(";", ",").replace("\n", ",")
        self._blocked_domains = [x.strip().lower() for x in text.split(",") if x.strip()]

    def is_blocked_domain(self, domain: str) -> bool:
        return is_big_marketplace(domain, self._blocked_domains)

    _arabic_cache: tuple = (0.0, None)

    def arabic_only(self) -> bool:
        """Keep only Arabic ads (setting arabic_only, on by default). Cached briefly."""
        import time as _t
        ts, val = self._arabic_cache
        if val is not None and _t.monotonic() - ts < 10:
            return val
        row = self._query_one("SELECT value FROM settings WHERE key = 'arabic_only'")
        val = str((row or {}).get("value") or "true").lower() in ("1", "true", "yes", "on")
        self._arabic_cache = (_t.monotonic(), val)
        return val

    def is_unwanted_ad(self, ad: dict) -> bool:
        """Drama apps, foreign sites, non-Arabic ads and ads whose CTA would
        be hidden anyway are never stored.

        The foreign-market side tab keeps every language/CTA/foreign TLD; only
        the big-marketplace blocklist still applies (checked in save_ad)."""
        if str(ad.get("market") or "dz").lower() == "global":
            return False
        cta = (ad.get("cta_text") or "").strip().upper()
        if junk_reason(ad.get("store_domain") or "", cta, ad.get("body") or ""):
            return True
        if self.arabic_only() and is_non_arabic(ad.get("body") or ""):
            return True
        allow = self.cta_allowlist()
        return bool(allow) and cta not in allow

    @_changes_ads
    def purge_blocked_ads(self) -> int:
        """Permanently delete ads of big marketplaces (and their tags/links)."""
        domains = [d for d in (list(BIG_MARKETPLACES) + list(self._blocked_domains))
                   if str(d).strip()]
        p = self._ph()
        conds, params = [], []
        for raw in domains:
            b = str(raw).strip().lower().lstrip(".")
            if not b:
                continue
            if b.endswith("."):
                base = b[:-1]
                conds.append(f"(store_domain = {p} OR store_domain LIKE {p} OR store_domain LIKE {p})")
                params.extend([base, b + "%", "%." + b])
            else:
                conds.append(f"(store_domain = {p} OR store_domain LIKE {p})")
                params.extend([b, "%." + b])
        if not conds:
            return 0
        rows = self._query(f"SELECT ad_id FROM meta_ads WHERE {' OR '.join(conds)}", tuple(params))
        return self._delete_ads([str(r["ad_id"]) for r in rows])

    @_changes_ads
    def purge_junk_ads(self) -> int:
        """Permanently delete drama-app and foreign-site ads (see junk_reason)."""
        p = self._ph()
        conds = " OR ".join([f"LOWER(store_domain) LIKE {p}"] * len(JUNK_LIKE_PATTERNS))
        rows = self._query(
            f"SELECT ad_id, store_domain, cta_text, body FROM meta_ads "
            f"WHERE (market IS NULL OR market = 'dz') AND ({conds})",
            tuple(JUNK_LIKE_PATTERNS))
        return self._delete_ads([str(r["ad_id"]) for r in rows
                                 if junk_reason(r["store_domain"], r.get("cta_text") or "", r.get("body") or "")])

    def purge_non_arabic_ads(self) -> int:
        """Permanently delete ads written in Latin script (see is_non_arabic)."""
        if not self.arabic_only():
            return 0
        rows = self._query("SELECT ad_id, body FROM meta_ads WHERE (market IS NULL OR market = 'dz')")
        return self._delete_ads([str(r["ad_id"]) for r in rows if is_non_arabic(r.get("body") or "")])

    @_changes_ads
    def _delete_ads(self, ids: list[str]) -> int:
        p = self._ph()
        if not ids:
            return 0
        for i in range(0, len(ids), 400):
            chunk = ids[i:i + 400]
            in_list = ", ".join([p] * len(chunk))
            for table in ("ad_keywords", "ad_niches", "ad_events", "user_ads"):
                try:
                    self._execute(f"DELETE FROM {table} WHERE ad_id IN ({in_list})", tuple(chunk))
                except Exception:
                    pass
            self._execute(f"DELETE FROM meta_ads WHERE ad_id IN ({in_list})", tuple(chunk))
        return len(ids)

    # ── niche classification (multi-label, rules-based) ───────────────────
    def _load_niches(self) -> None:
        rows = self._query(
            "SELECT id, slug, name_ar, name_fr, keywords, negatives FROM niches WHERE enabled = 1")
        cache = []
        for r in rows:
            try:
                kws = [(x.get("k"), int(x.get("w") or 1)) for x in json.loads(r.get("keywords") or "[]")]
            except Exception:
                kws = []
            try:
                negs = json.loads(r.get("negatives") or "[]")
            except Exception:
                negs = []
            cache.append({
                "id": r["id"], "slug": r["slug"], "name_ar": r.get("name_ar") or "",
                "name_fr": r.get("name_fr") or "", "keywords": kws, "negatives": negs,
            })
        self._niches_cache = cache

    def _classify_results(self, ad: dict) -> list[dict]:
        if not self._niches_cache:
            return []
        return niche_defs.classify(
            ad.get("body") or "", ad.get("page_name") or "",
            ad.get("store_domain") or "", niches=self._niches_cache)

    def _save_niche_rows(self, cur, ad_id: str, results: list[dict], now: str) -> None:
        p = self._ph()
        by_slug = {n["slug"]: n["id"] for n in self._niches_cache}
        cur.execute(f"DELETE FROM ad_niches WHERE ad_id = {p} AND method = 'rules'", (ad_id,))
        for r in results:
            nid = by_slug.get(r["slug"])
            if not nid:
                continue
            if self.is_postgres:
                cur.execute(
                    f"INSERT INTO ad_niches (ad_id, niche_id, score, confidence, method, updated_at) "
                    f"VALUES ({p},{p},{p},{p},'rules',{p}) "
                    f"ON CONFLICT (ad_id, niche_id, method) DO UPDATE SET "
                    f"score = EXCLUDED.score, confidence = EXCLUDED.confidence, "
                    f"updated_at = EXCLUDED.updated_at",
                    (ad_id, nid, r["score"], r["confidence"], now))
            else:
                cur.execute(
                    f"INSERT OR REPLACE INTO ad_niches "
                    f"(ad_id, niche_id, score, confidence, method, updated_at) "
                    f"VALUES ({p},{p},{p},{p},'rules',{p})",
                    (ad_id, nid, r["score"], r["confidence"], now))
        primary = results[0]["slug"] if results else ""
        cur.execute(f"UPDATE meta_ads SET primary_niche = {p}, niche_checked_at = {p} "
                    f"WHERE ad_id = {p}", (primary, now, ad_id))

    def attach_niches(self, rows: list[dict]) -> list[dict]:
        """Attach ``niches`` (slug, name, confidence, method) to ad rows."""
        if not rows:
            return rows
        p = self._ph()
        ids = [str(r.get("ad_id") or "") for r in rows if r.get("ad_id")]
        mapping: dict[str, list[dict]] = {}
        for i in range(0, len(ids), 400):
            chunk = ids[i:i + 400]
            in_list = ", ".join([p] * len(chunk))
            for r in self._query(
                    f"SELECT an.ad_id, n.slug, n.name_ar, an.confidence, an.method "
                    f"FROM ad_niches an JOIN niches n ON n.id = an.niche_id "
                    f"WHERE an.ad_id IN ({in_list}) AND an.confidence >= 40 "
                    f"ORDER BY an.confidence DESC, n.slug", tuple(chunk)):
                mapping.setdefault(str(r["ad_id"]), []).append({
                    "slug": r["slug"], "name_ar": r.get("name_ar") or "",
                    "confidence": int(r["confidence"] or 0), "method": r["method"],
                })
        for r in rows:
            items = mapping.get(str(r.get("ad_id") or ""), [])
            r["niches"] = items
            r["primary_niche"] = items[0]["slug"] if items else (r.get("primary_niche") or "")
        return rows

    def niche_counts(self, user_id: Optional[int] = None) -> list[dict]:
        p = self._ph()
        clause, params = self._build_ads_filter(user_id=user_id)
        sub = f"SELECT ad_id FROM meta_ads {clause}"
        return self._cached(("niche_counts", user_id), lambda: self._query(
            f"SELECT n.slug, n.name_ar, COUNT(DISTINCT an.ad_id) AS total_ads "
            f"FROM ad_niches an JOIN niches n ON n.id = an.niche_id "
            f"WHERE an.confidence >= 40 AND an.ad_id IN ({sub}) "
            f"GROUP BY n.slug, n.name_ar ORDER BY total_ads DESC", tuple(params)))

    def classify_pending(self, limit: int = 500) -> int:
        """Classify ads that were never classified (backfill + new scrapes)."""
        if not self._niches_cache:
            self._load_niches()
        if not self._niches_cache:
            return 0
        rows = self._query(
            "SELECT ad_id, body, page_name, store_domain FROM meta_ads "
            "WHERE niche_checked_at IS NULL AND (market IS NULL OR market = 'dz') "
            f"LIMIT {int(limit)}")
        if not rows:
            return 0
        conn = self._connect()
        try:
            cur = conn.cursor()
            now = _now()
            for r in rows:
                self._save_niche_rows(cur, str(r["ad_id"]), self._classify_results(r), now)
            conn.commit()
        finally:
            conn.close()
        return len(rows)

    @_invalidates_cache
    def set_manual_niche(self, ad_id: str, slug: str, add: bool = True) -> bool:
        p = self._ph()
        row = self._query_one(f"SELECT id FROM niches WHERE slug = {p}", (slug,))
        if not row:
            return False
        nid = int(row["id"])
        if add:
            if self.is_postgres:
                self._execute(
                    f"INSERT INTO ad_niches (ad_id, niche_id, score, confidence, method, updated_at) "
                    f"VALUES ({p},{p},99,100,'manual',{p}) "
                    f"ON CONFLICT (ad_id, niche_id, method) DO UPDATE SET "
                    f"confidence = 100, updated_at = EXCLUDED.updated_at",
                    (ad_id, nid, _now()))
            else:
                self._execute(
                    f"INSERT OR REPLACE INTO ad_niches "
                    f"(ad_id, niche_id, score, confidence, method, updated_at) "
                    f"VALUES ({p},{p},99,100,'manual',{p})", (ad_id, nid, _now()))
        else:
            self._execute(
                f"DELETE FROM ad_niches WHERE ad_id = {p} AND niche_id = {p} AND method = 'manual'",
                (ad_id, nid))
        best = self._query_one(
            f"SELECT n.slug FROM ad_niches an JOIN niches n ON n.id = an.niche_id "
            f"WHERE an.ad_id = {p} AND an.confidence >= 40 "
            f"ORDER BY (CASE WHEN an.method = 'manual' THEN 1 ELSE 0 END) DESC, "
            f"an.confidence DESC LIMIT 1", (ad_id,))
        self._execute(f"UPDATE meta_ads SET primary_niche = {p} WHERE ad_id = {p}",
                      ((best or {}).get("slug") or "", ad_id))
        return True

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
        "hunter": "hunter DESC, active DESC",
    }
    _PAGE_STATS_JOIN = ("LEFT JOIN entity_stats es ON es.ent_kind = 'page' "
                        "AND es.ent_key = meta_ads.page_id")

    # ── precomputed product summary (the default grouped view) ────────────
    # One row per product with the same numbers get_ad_groups computes, over
    # the ads the default view shows (Algerian bank, allowed CTA, has a store).
    # Saving an ad refreshes its product row on the next read; bulk changes
    # (status check, purge, CTA setting…) rebuild the table in the background,
    # and until that finishes the view falls back to the live GROUP BY, so the
    # numbers shown are never older than the ads table.
    _SUMMARY_ORDER = {
        "longest_running": "pg.max_days DESC, pg.active DESC",
        "newest_launch": "pg.last_launch DESC",
        "most_collated": "pg.ads_count DESC, pg.active DESC",
        "collected_at": "pg.last_collected DESC",
        "biggest_page": "page_ads DESC, pg.ads_count DESC",
        "hunter": "pg.hunter DESC, pg.active DESC",
    }
    _SUMMARY_COLS = ("product_key, ads_count, active, first_launch, last_launch, texts, "
                     "max_days, last_collected, page_id, hunter")
    _SUMMARY_SELECT = (
        "SELECT product_key, COUNT(*), SUM(is_active), MIN(start_date), MAX(start_date), "
        "COUNT(DISTINCT body), MAX(days_active), MAX(collected_at), MAX(page_id), "
        "MAX(COALESCE(hunter_score, 0)) FROM meta_ads ")
    SUMMARY_MAX_AGE_S = float(os.environ.get("GROUP_SUMMARY_MAX_AGE", "900"))
    _groups_lock = threading.Lock()

    def _summary_state(self) -> dict:
        st = getattr(self, "_gs", None)
        if st is None:
            # Fresh process: whatever the table holds may predate this run.
            st = self._gs = {"stale": True, "gen": 0, "dirty": set(), "built": 0.0,
                             "building": False}
        return st

    def mark_groups_dirty(self, *keys) -> None:
        """These products changed (an ad was saved): refresh them on next read."""
        st = self._summary_state()
        with self._groups_lock:
            st["dirty"].update(k for k in keys if k)

    def mark_groups_stale(self) -> None:
        """Many ads changed: the whole summary needs a rebuild."""
        st = self._summary_state()
        with self._groups_lock:
            st["stale"] = True
            st["gen"] += 1

    @staticmethod
    def _is_default_view(filters: dict) -> bool:
        """Only the default listing (stores only, no other filter) is summarised."""
        if not filters.get("stores_only"):
            return False
        if str(filters.get("market") or "dz").lower() != "dz":
            return False
        return not any(v for k, v in filters.items() if k not in ("stores_only", "market"))

    def can_use_group_summary(self, filters: dict) -> bool:
        return self._is_default_view(filters) and not self._summary_state()["stale"]

    def _summary_ready(self, filters: dict) -> bool:
        """True when the summary can answer this request (refreshing the
        products saved since the last read first). Kicks off a background
        rebuild when the summary is stale or old."""
        if not self._is_default_view(filters):
            return False
        st = self._summary_state()
        if st["stale"] or time.monotonic() - st["built"] > self.SUMMARY_MAX_AGE_S:
            self._start_group_rebuild()
        if st["stale"]:
            return False
        with self._groups_lock:
            keys, st["dirty"] = st["dirty"], set()
        if len(keys) > 5000:          # a huge burst: cheaper to rebuild everything
            self.mark_groups_stale()
            self._start_group_rebuild()
            return False
        if keys:
            try:
                self._refresh_group_keys(sorted(keys))
            except Exception as exc:  # noqa: BLE001
                logger.warning("product summary refresh failed: %s", exc)
                with self._groups_lock:
                    st["dirty"].update(keys)
                return False
        return True

    def _summary_write(self, delete_sql: str, insert_sql: str, params: tuple) -> None:
        """Replace summary rows in one transaction (readers see old or new, never half)."""
        conn = self._connect()
        try:
            cur = conn.cursor()
            if self.is_postgres:
                cur.execute("BEGIN")
            try:
                cur.execute(delete_sql, params[:delete_sql.count(self._ph())])
                cur.execute(insert_sql, params[delete_sql.count(self._ph()):])
                cur.execute("COMMIT") if self.is_postgres else conn.commit()
            except Exception:
                cur.execute("ROLLBACK") if self.is_postgres else conn.rollback()
                raise
        finally:
            conn.close()

    def _refresh_group_keys(self, keys: list[str]) -> None:
        p = self._ph()
        base, base_params = self._build_ads_filter(stores_only=True)
        for i in range(0, len(keys), 400):
            chunk = keys[i:i + 400]
            in_list = ", ".join([p] * len(chunk))
            with self._groups_lock_rw():
                self._summary_write(
                    f"DELETE FROM product_groups WHERE product_key IN ({in_list})",
                    f"INSERT INTO product_groups ({self._SUMMARY_COLS}) {self._SUMMARY_SELECT}"
                    f"{base} AND product_key IN ({in_list}) GROUP BY product_key",
                    tuple(chunk) + tuple(base_params) + tuple(chunk))

    def rebuild_group_summary(self) -> bool:
        """Recompute the whole summary. Returns True when it is now current."""
        st = self._summary_state()
        with self._groups_lock:
            gen = st["gen"]
            st["dirty"] = set()       # a full rebuild covers everything saved so far
        t0 = time.perf_counter()
        base, base_params = self._build_ads_filter(stores_only=True)
        with self._groups_lock_rw():
            self._summary_write(
                "DELETE FROM product_groups",
                f"INSERT INTO product_groups ({self._SUMMARY_COLS}) {self._SUMMARY_SELECT}"
                f"{base} AND product_key IS NOT NULL GROUP BY product_key",
                tuple(base_params))
        with self._groups_lock:
            st["built"] = time.monotonic()
            if st["gen"] == gen:      # nothing went stale while we were building
                st["stale"] = False
                current = True
            else:
                current = False
        logger.info("product summary rebuilt in %.0f ms (current=%s)",
                    (time.perf_counter() - t0) * 1000, current)
        return current

    def _groups_lock_rw(self):
        """Serialises summary writers in this process (full rebuild vs per-key refresh)."""
        lock = getattr(self, "_gw_lock", None)
        if lock is None:
            lock = self._gw_lock = threading.Lock()
        return lock

    def _start_group_rebuild(self) -> None:
        st = self._summary_state()
        with self._groups_lock:
            if st["building"]:
                return
            st["building"] = True

        def run():
            try:
                for _ in range(3):    # retry if ads went stale mid-build
                    if self.rebuild_group_summary():
                        break
            except Exception as exc:  # noqa: BLE001
                logger.warning("product summary rebuild failed: %s", exc)
            finally:
                with self._groups_lock:
                    st["building"] = False
        threading.Thread(target=run, name="group-summary", daemon=True).start()

    def get_ad_groups(self, limit: int = 60, offset: int = 0, sort_by: str = "collected_at",
                      **filters) -> list[dict]:
        """One item per product: a representative ad plus a ``group`` summary.

        Filters are the same as :meth:`get_ads` and apply to the ads before
        grouping, so counts reflect only the matching ads.
        """
        p = self._ph()
        clause, params = self._build_ads_filter(**filters)
        if self._summary_ready(filters):
            # Default view: read the precomputed product summary (an indexed
            # ORDER BY … LIMIT) instead of grouping the whole ads table.
            order = self._SUMMARY_ORDER.get(sort_by, self._SUMMARY_ORDER["collected_at"])
            groups = self._query(
                f"SELECT pg.*, COALESCE(es.ent_ads, -1) AS page_ads FROM product_groups pg "
                f"LEFT JOIN entity_stats es ON es.ent_kind = 'page' AND es.ent_key = pg.page_id "
                f"ORDER BY {order}, pg.product_key LIMIT {p} OFFSET {p}", (limit, offset))
        else:
            order = self._GROUP_ORDER.get(sort_by, self._GROUP_ORDER["collected_at"])
            groups = self._query(
                f"SELECT product_key, COUNT(*) AS ads_count, SUM(is_active) AS active, "
                f"MIN(start_date) AS first_launch, MAX(start_date) AS last_launch, "
                f"COUNT(DISTINCT body) AS texts, MAX(days_active) AS max_days, "
                f"MAX(collected_at) AS last_collected, MAX(COALESCE(es.ent_ads, -1)) AS page_ads, "
                f"MAX(COALESCE(hunter_score, 0)) AS hunter "
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
        return self.attach_niches(out)

    def count_ad_groups(self, **filters) -> int:
        if self._summary_ready(filters):
            sql, params = "SELECT COUNT(*) AS c FROM product_groups", []
        else:
            clause, params = self._build_ads_filter(**filters)
            sql = f"SELECT COUNT(DISTINCT product_key) AS c FROM meta_ads {clause}"

        def run() -> int:
            row = self._query_one(sql, tuple(params))
            return int(row["c"]) if row else 0
        return self._cached(("count", sql, tuple(params)), run)

    def get_group_ads(self, product_key: str, user_id: Optional[int] = None,
                      limit: int = 500, market: str = "dz") -> list[dict]:
        """All ads of one product group (newest launch first)."""
        p = self._ph()
        clause, params = self._build_ads_filter(user_id=user_id, market=market)
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
        return self.attach_niches(self.attach_keywords(rows))

    def count_ads(self, search: str = "", stores_only: bool = False,
                  query: str = "", store_domain: str = "",
                  page_id: str = "", preset: str = "", active_only: bool = False,
                  start_date_min: str = "", start_date_max: str = "",
                  min_days_active: int = 0, target_id: Optional[int] = None,
                  user_id: Optional[int] = None, min_product_ads: int = 0,
                  keywords: Optional[list[str]] = None,
                  niches: Optional[list[str]] = None, market: str = "dz") -> int:
        clause, params = self._build_ads_filter(
            search=search, stores_only=stores_only, query=query,
            store_domain=store_domain, page_id=page_id, preset=preset,
            active_only=active_only, start_date_min=start_date_min,
            start_date_max=start_date_max, min_days_active=min_days_active,
            target_id=target_id, user_id=user_id, min_product_ads=min_product_ads,
            keywords=keywords, niches=niches, market=market,
        )
        filters = dict(search=search, stores_only=stores_only, query=query,
                       store_domain=store_domain, page_id=page_id, preset=preset,
                       active_only=active_only, start_date_min=start_date_min,
                       start_date_max=start_date_max, min_days_active=min_days_active,
                       target_id=target_id, user_id=user_id, min_product_ads=min_product_ads,
                       keywords=keywords, niches=niches, market=market)
        if self._summary_ready(filters):
            # Default view: the summary already counts every grouped ad; only
            # ads without a product key (none normally) are counted live.
            sql = (f"SELECT (SELECT COALESCE(SUM(ads_count), 0) FROM product_groups) + "
                   f"(SELECT COUNT(*) FROM meta_ads {clause} AND product_key IS NULL) AS c")
        else:
            sql = f"SELECT COUNT(*) AS c FROM meta_ads {clause}"

        def run() -> int:
            row = self._query_one(sql, tuple(params))
            return int(row["c"]) if row else 0
        return self._cached(("count", sql, tuple(params)), run)

    def get_queries(self) -> list[dict]:
        sql = ("SELECT query, COUNT(*) AS total_ads, "
               "SUM(CASE WHEN store_domain != '' AND store_domain IS NOT NULL THEN 1 ELSE 0 END) AS store_ads, "
               "MAX(collected_at) AS last_run FROM meta_ads "
               "WHERE query != '' AND (market IS NULL OR market = 'dz') "
               "GROUP BY query ORDER BY last_run DESC LIMIT 100")
        return self._query(sql)

    def get_keywords_overview(self, user_id: Optional[int] = None,
                              limit: int = 300) -> list[dict]:
        """Per-keyword counters for the keyword filter bar (ads, store ads,
        winning ads, recent launches, last run)."""
        p = self._ph()
        where = ["ak.keyword <> ''", "(m.market IS NULL OR m.market = 'dz')"]
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
        return self._cached(("keywords", sql, tuple(params)),
                            lambda: self._query(sql, tuple(params)))

    def get_stores(self, search: str = "", user_id: Optional[int] = None) -> list[dict]:
        p = self._ph()
        where = ["store_domain != '' AND store_domain IS NOT NULL",
                 "(market IS NULL OR market = 'dz')"]
        params: list = []
        if search:
            where.append(f"store_domain LIKE {p}")
            params.append("%" + search + "%")
        cta_sql, cta_params = self._cta_clause()
        if cta_sql:
            where.append(cta_sql)
            params.extend(cta_params)
        if user_id is not None:
            where.append(self._user_scope_sql())
            params.extend(self._user_scope_params(user_id))
        sql = (f"SELECT store_domain, platform, COUNT(*) AS total_ads, "
               f"MAX(collected_at) AS last_seen FROM meta_ads "
               f"WHERE {' AND '.join(where)} "
               f"GROUP BY store_domain, platform ORDER BY total_ads DESC LIMIT 300")
        return self._query(sql, tuple(params))

    # ── foreign market side tab ───────────────────────────────────────────
    def global_stores(self, search: str = "", limit: int = 200) -> list[dict]:
        """Store domains collected in the foreign market, with ad counters."""
        p = self._ph()
        where = ["market = 'global' AND store_domain != '' AND store_domain IS NOT NULL"]
        params: list = []
        if search:
            where.append(f"store_domain LIKE {p}")
            params.append("%" + search + "%")
        return self._query(
            f"SELECT store_domain, COUNT(*) AS total_ads, SUM(is_active) AS active_ads, "
            f"MAX(collected_at) AS last_seen FROM meta_ads WHERE {' AND '.join(where)} "
            f"GROUP BY store_domain ORDER BY total_ads DESC LIMIT {int(limit)}", tuple(params))

    def get_stats(self, user_id: Optional[int] = None) -> dict:
        return self._cached(("stats", user_id), lambda: self._get_stats(user_id))

    def _get_stats(self, user_id: Optional[int] = None) -> dict:
        p = self._ph()
        total = self.count_ads(user_id=user_id)
        scope, scope_params = "", ()
        if user_id is not None:
            scope = " AND " + self._user_scope_sql()
            scope_params = tuple(self._user_scope_params(user_id))
        scope = " AND (market IS NULL OR market = 'dz')" + scope
        cta_sql, cta_params = self._cta_clause()
        if cta_sql:
            scope += " AND " + cta_sql
            scope_params = tuple(scope_params) + tuple(cta_params)
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

    # ── selective reset ("مسح القاعدة") ───────────────────────────────────
    # Sections an ad can belong to. An ad is deleted only when EVERY section it
    # belongs to was chosen; ads in no section are the plain bank ("bank").
    RESET_SECTIONS = ("bank", "competitors", "keywords", "winning", "global")
    _DZ = "(market IS NULL OR market = 'dz')"

    def _reset_conditions(self) -> dict:
        dz_t = "(t.market IS NULL OR t.market = 'dz')"
        comp = (
            f"(page_id IN (SELECT t.target_value FROM targets t WHERE t.type = 'page' AND {dz_t}) "
            f"OR page_id IN (SELECT page_id FROM tracked_pages) "
            f"OR store_domain IN (SELECT t.target_value FROM targets t WHERE t.type = 'domain' AND {dz_t}) "
            f"OR target_id IN (SELECT t.id FROM targets t WHERE t.type IN ('page', 'domain') AND {dz_t}))")
        kw = (
            f"(query IN (SELECT t.target_value FROM targets t WHERE t.type = 'keyword' AND {dz_t}) "
            f"OR ad_id IN (SELECT ak.ad_id FROM ad_keywords ak WHERE ak.keyword IN "
            f"(SELECT t.target_value FROM targets t WHERE t.type = 'keyword' AND {dz_t})) "
            f"OR target_id IN (SELECT t.id FROM targets t WHERE t.type = 'keyword' AND {dz_t}))")
        win = ("(product_key IS NOT NULL AND product_key <> '' "
               "AND product_key IN (SELECT product_key FROM winning_products))")
        # "IS TRUE": an ad with an empty page/store/key makes these NULL, and
        # NOT NULL is NULL too, which would neither protect nor delete it.
        return {k: f"({v} IS TRUE)" for k, v in
                (("competitors", comp), ("keywords", kw), ("winning", win))}

    def _reset_where(self, sel: dict) -> list[str]:
        """WHERE clauses (OR-ed) of the ads a selection deletes."""
        c = self._reset_conditions()
        out = []
        if any(sel.get(k) for k in ("bank", "competitors", "keywords", "winning")):
            parts = [self._DZ]
            for k in ("competitors", "keywords", "winning"):
                if not sel.get(k):
                    parts.append(f"NOT {c[k]}")  # protected section
            if not sel.get("bank"):
                parts.append("(" + " OR ".join(c[k] for k in ("competitors", "keywords", "winning")
                                               if sel.get(k)) + ")")
            out.append("(" + " AND ".join(parts) + ")")
        if sel.get("global"):
            out.append("(market = 'global')")
        return out

    def reset_counts(self, sel: Optional[dict] = None) -> dict:
        """Ads per section, and how many a selection would delete."""
        c = self._reset_conditions()
        n = lambda where: int((self._query_one(f"SELECT COUNT(*) AS n FROM meta_ads WHERE {where}") or {}).get("n") or 0)
        counts = {
            "bank": n(f"{self._DZ} AND NOT {c['competitors']} AND NOT {c['keywords']} AND NOT {c['winning']}"),
            "competitors": n(f"{self._DZ} AND {c['competitors']}"),
            "keywords": n(f"{self._DZ} AND {c['keywords']}"),
            "winning": n(f"{self._DZ} AND {c['winning']}"),
            "global": n("market = 'global'"),
            "total": n("1=1"),
        }
        where = self._reset_where(sel or {})
        counts["to_delete"] = n(" OR ".join(where)) if where else 0
        return counts

    @_changes_ads
    def reset_ads(self, sel: dict) -> int:
        """Delete the ads of the chosen sections (and their tags, niches,
        events and per-user links). Lists (competitors, winning products),
        users and settings are never touched. Returns the ads deleted."""
        where = self._reset_where(sel)
        if not where:
            return 0
        cond = " OR ".join(where)
        doomed = f"SELECT ad_id FROM meta_ads WHERE {cond}"
        conn = self._connect()
        try:
            cur = conn.cursor()
            # Postgres can't reuse a subquery on a table being deleted from in
            # one statement safely across the sequence, so snapshot the ids.
            cur.execute("CREATE TEMP TABLE IF NOT EXISTS reset_ids (ad_id TEXT PRIMARY KEY)")
            cur.execute("DELETE FROM reset_ids")
            cur.execute(f"INSERT INTO reset_ids (ad_id) {doomed}")
            for t in ("ad_keywords", "ad_niches", "ad_events", "user_ads"):
                cur.execute(f"DELETE FROM {t} WHERE ad_id IN (SELECT ad_id FROM reset_ids)")
            cur.execute("DELETE FROM meta_ads WHERE ad_id IN (SELECT ad_id FROM reset_ids)")
            deleted = cur.rowcount if cur.rowcount is not None else 0
            cur.execute("DELETE FROM reset_ids")
            conn.commit()
            return int(deleted)
        finally:
            conn.close()

    @_changes_ads
    def clear_ads(self) -> None:
        """Reset the Algerian bank only; the foreign side tab is separate."""
        self._execute("DELETE FROM ad_events WHERE (market IS NULL OR market = 'dz')")
        self._execute("DELETE FROM meta_ads WHERE (market IS NULL OR market = 'dz')")

    # ── jobs (remote scrape queue) ────────────────────────────────────────
    def get_known_ad_ids(self, query: str = "", store_domain: str = "",
                         page_id: str = "", limit: int = 150) -> list[str]:
        """Fetch recently stored ad IDs for early stopping."""
        p = self._ph()
        where = ["(market IS NULL OR market = 'dz')"]
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

    @_changes_ads
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
                   source: str = "manual", url: str = "",
                   target_id: Optional[int] = None, incremental: bool = False,
                   start_date_min: str = "", start_date_max: str = "",
                   date_scope: str = "impressions", mode: str = "", languages: str = "",
                   market: str = "dz") -> int:
        p = self._ph()
        conn = self._connect()
        try:
            cur = conn.cursor()
            cols = ("query", "url", "country", "exact_phrase", "stores_only",
                    "sort_mode", "status", "source", "created_at", "target_id", "incremental",
                    "start_date_min", "start_date_max", "date_scope", "mode", "languages", "market")
            vals = (query, url, country, int(exact_phrase), int(stores_only),
                    sort_mode, "pending", source, _now(), target_id, int(incremental),
                    start_date_min, start_date_max, date_scope or "impressions", mode or "", languages or "",
                    "global" if str(market).lower() == "global" else "dz")
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

    @_invalidates_cache
    def finish_job(self, job_id: int, status: str = "done", result_count: int = 0,
                   error: str = "", checkpoint: Optional[str] = None,
                   scanned_count: Optional[int] = None, completed: Optional[bool] = None,
                   slices_total: Optional[int] = None, slices_ok: Optional[int] = None) -> None:
        """Close a job. ``checkpoint`` is kept on failure so it can be resumed.

        The coverage fields (scanned / completed / slices) are only written
        when given, so callers that do not track them leave them untouched.
        """
        p = self._ph()
        sets = [f"status={p}", f"finished_at={p}", f"result_count={p}", f"error={p}",
                f"checkpoint={p}", "retry_at=NULL"]
        vals: list = [status, _now(), result_count, error, checkpoint]
        if scanned_count is not None:
            sets.append(f"scanned_count={p}")
            vals.append(int(scanned_count))
        if completed is not None:
            sets.append(f"completed={p}")
            vals.append(1 if completed else 0)
        for col, val in (("slices_total", slices_total), ("slices_ok", slices_ok)):
            if val is not None:
                sets.append(f"{col}={p}")
                vals.append(int(val))
        vals.append(job_id)
        self._execute(f"UPDATE jobs SET {', '.join(sets)} WHERE id={p}", tuple(vals))

    def set_job_reported(self, job_id: int, reported_count: Optional[int]) -> None:
        """Store Meta's estimated result count, as soon as it is known."""
        if reported_count is None:
            return
        p = self._ph()
        self._execute(f"UPDATE jobs SET reported_count={p} WHERE id={p}",
                      (int(reported_count), job_id))

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
                       result_count: int = 0, error: str = "",
                       scanned_count: Optional[int] = None) -> None:
        """Put an interrupted job back in the queue, to resume after a delay."""
        from datetime import timedelta
        p = self._ph()
        retry_at = (datetime.now(timezone.utc) + timedelta(minutes=delay_minutes)).isoformat()
        extra, vals = "", [retry_at, checkpoint, result_count, error]
        if scanned_count is not None:
            extra = f", scanned_count={p}"
            vals.append(int(scanned_count))
        vals.append(job_id)
        self._execute(
            f"UPDATE jobs SET status='pending', worker_id=NULL, started_at=NULL, "
            f"attempts=COALESCE(attempts, 0) + 1, retry_at={p}, checkpoint={p}, "
            f"result_count={p}, error={p}{extra} WHERE id={p}",
            tuple(vals))

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

    def has_open_mode_job(self, mode: str) -> bool:
        """True if a job of this mode is queued, waiting to resume or running."""
        p = self._ph()
        return bool(self._query_one(
            f"SELECT id FROM jobs WHERE mode={p} AND status IN ('pending', 'running') LIMIT 1", (mode,)))

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
                   country: str = "DZ", interval_hours: float = 12.0,
                   market: str = "dz") -> bool:
        val = (target_value or "").strip()
        typ = (type or "keyword").strip().lower()
        if not val:
            return False
        p = self._ph()
        mkt = "global" if str(market).lower() == "global" else "dz"
        if mkt == "global":
            dup = self._query_one(
                f"SELECT id FROM targets WHERE type={p} AND target_value={p} AND market={p}",
                (typ, val, mkt))
        else:
            dup = self._query_one(
                f"SELECT id FROM targets WHERE type={p} AND target_value={p} "
                f"AND (market IS NULL OR market = 'dz')", (typ, val))
        if dup:
            return False
        self._execute(
            f"INSERT INTO targets (type, target_value, label, country, enabled, interval_hours, created_at, market) "
            f"VALUES ({p},{p},{p},{p},1,{p},{p},{p})",
            (typ, val, label or val, country or "DZ", float(interval_hours or 12.0), _now(), mkt))
        return True

    def list_targets(self, market: Optional[str] = None) -> list[dict]:
        # Migrate any old keywords to targets if targets is empty
        if not self._query_one("SELECT 1 FROM targets LIMIT 1"):
            old_kws = self._query("SELECT * FROM keywords")
            for k in old_kws:
                t_type = k.get("type") or "keyword"
                self.add_target(type=t_type, target_value=k.get("query", ""),
                                label=k.get("label", ""), country="DZ")
        p = self._ph()
        if market:
            if str(market).lower() == "dz":
                targets = self._query(
                    "SELECT * FROM targets WHERE (market IS NULL OR market = 'dz') ORDER BY id DESC")
            else:
                targets = self._query(
                    f"SELECT * FROM targets WHERE market={p} ORDER BY id DESC", (market,))
        else:
            targets = self._query("SELECT * FROM targets ORDER BY id DESC")
        for t in targets:
            typ = t.get("type") or "keyword"
            val = t.get("target_value") or ""
            tid = t.get("id")
            mkt = t.get("market") or "dz"
            mcond = "market = 'global'" if mkt == "global" else "(market IS NULL OR market = 'dz')"
            if typ == "page":
                row = self._query_one(
                    f"SELECT COUNT(*) AS c FROM meta_ads WHERE ({mcond}) "
                    f"AND (page_id = {p} OR target_id = {p})", (val, tid))
            elif typ == "domain":
                row = self._query_one(
                    f"SELECT COUNT(*) AS c FROM meta_ads WHERE ({mcond}) "
                    f"AND (store_domain = {p} OR target_id = {p})", (val, tid))
            else:
                row = self._query_one(
                    f"SELECT COUNT(*) AS c FROM meta_ads WHERE ({mcond}) "
                    f"AND (query = {p} OR target_id = {p})", (val, tid))
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

    def find_target(self, type: str, target_value: str,
                    market: Optional[str] = None) -> Optional[dict]:
        p = self._ph()
        typ = (type or "").strip().lower()
        val = (target_value or "").strip()
        if market:
            mkt = str(market).lower()
            if mkt == "global":
                return self._query_one(
                    f"SELECT * FROM targets WHERE type={p} AND target_value={p} AND market={p}",
                    (typ, val, mkt))
            return self._query_one(
                f"SELECT * FROM targets WHERE type={p} AND target_value={p} "
                f"AND (market IS NULL OR market = 'dz')", (typ, val))
        return self._query_one(f"SELECT * FROM targets WHERE type={p} AND target_value={p}",
                               (typ, val))

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

    @_invalidates_cache
    def add_user_target(self, user_id: int, type: str, target_value: str, label: str = "",
                        country: str = "DZ", interval_hours: float = 12.0,
                        source: str = "dashboard", market: str = "dz") -> tuple[int, bool]:
        """Track a competitor for one user. Returns (target_id, newly_linked)."""
        typ = (type or "keyword").strip().lower()
        val = (target_value or "").strip()
        self.add_target(type=typ, target_value=val, label=label or val, country=country,
                        interval_hours=interval_hours, market=market)
        t = self.find_target(typ, val, market=market)
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

    # ── winning products (saved by a user, followed every 12 h) ───────────
    WINNING_STAGES = ("watching", "testing", "ordered", "dropped")

    def add_winning_product(self, user_id: int, ad_id: str) -> Optional[dict]:
        """Save the product of ``ad_id`` for ``user_id`` (idempotent).

        A product is every ad of the same advertiser for the same product
        page (product_key). Returns the saved row, or None if the ad is unknown.
        """
        p = self._ph()
        ad = self._query_one(
            f"SELECT ad_id, product_key, page_id, page_name, store_domain, link_url, body "
            f"FROM meta_ads WHERE ad_id={p}", (str(ad_id),))
        if not ad:
            return None
        pkey = str(ad.get("product_key") or "")
        if not pkey:
            from store_filter import product_key
            pkey = product_key(str(ad.get("page_id") or ""), ad.get("link_url") or "",
                               ad.get("body") or "", ad.get("store_domain") or "")
        now = _now()
        self._execute(
            f"INSERT INTO winning_products (user_id, product_key, ad_id, page_id, page_name, "
            f"store_domain, link_url, body, notes, stage, created_at, updated_at) "
            f"VALUES ({', '.join([p] * 12)}) ON CONFLICT (user_id, product_key) DO NOTHING",
            (int(user_id), pkey, str(ad["ad_id"]), str(ad.get("page_id") or ""),
             str(ad.get("page_name") or ""), str(ad.get("store_domain") or ""),
             str(ad.get("link_url") or ""), str(ad.get("body") or "")[:2000], "", "watching", now, now))
        return self._query_one(f"SELECT * FROM winning_products WHERE user_id={p} AND product_key={p}",
                               (int(user_id), pkey))

    def get_winning_product(self, wid: int) -> Optional[dict]:
        return self._query_one(f"SELECT * FROM winning_products WHERE id={self._ph()}", (int(wid),))

    def update_winning_product(self, wid: int, notes: Optional[str] = None,
                               stage: Optional[str] = None) -> None:
        p = self._ph()
        sets, vals = [f"updated_at={p}"], [_now()]
        if notes is not None:
            sets.append(f"notes={p}")
            vals.append(str(notes)[:4000])
        if stage is not None and stage in self.WINNING_STAGES:
            sets.append(f"stage={p}")
            vals.append(stage)
        vals.append(int(wid))
        self._execute(f"UPDATE winning_products SET {', '.join(sets)} WHERE id={p}", tuple(vals))

    def remove_winning_product(self, wid: int) -> None:
        self._execute(f"DELETE FROM winning_products WHERE id={self._ph()}", (int(wid),))

    def winning_product_keys(self, user_id: int) -> dict[str, int]:
        """{product_key: saved id} of one user's winning products."""
        rows = self._query(f"SELECT id, product_key FROM winning_products WHERE user_id={self._ph()}",
                           (int(user_id),))
        return {r["product_key"]: int(r["id"]) for r in rows}

    def winning_pages(self) -> list[str]:
        """Pages of every saved product, active or not: a stopped product's
        page must keep being checked so a relaunch is noticed."""
        rows = self._query("SELECT DISTINCT page_id FROM winning_products "
                           "WHERE page_id IS NOT NULL AND page_id <> '' ORDER BY page_id")
        return [str(r["page_id"]) for r in rows]

    def list_winning_products(self, user_id: Optional[int] = None) -> list[dict]:
        """Saved products with their live numbers (None = every user's)."""
        p = self._ph()
        where, params = ("WHERE w.user_id = " + p, (int(user_id),)) if user_id is not None else ("", ())
        rows = self._query(
            f"SELECT w.*, u.email AS owner_email FROM winning_products w "
            f"LEFT JOIN users u ON u.id = w.user_id {where} ORDER BY w.created_at DESC", params)
        keys = sorted({r["product_key"] for r in rows})
        if not keys:
            return rows
        in_list = ", ".join([p] * len(keys))
        stats = {r["product_key"]: r for r in self._query(
            f"SELECT product_key, COUNT(*) AS total_ads, "
            f"SUM(CASE WHEN is_active = 1 THEN 1 ELSE 0 END) AS active_ads, "
            f"MIN(start_date) AS first_launch, MAX(start_date) AS last_launch, "
            f"MAX(days_active) AS max_days, MAX(COALESCE(stopped_at, last_seen_active)) AS last_seen, "
            f"MAX(status_checked_at) AS checked_at "
            f"FROM meta_ads WHERE product_key IN ({in_list}) GROUP BY product_key", tuple(keys))}
        # Picture: the newest running ad's image, else the newest ad's.
        images: dict = {}
        for r in self._query(
                f"SELECT product_key, preview_image_url FROM meta_ads WHERE product_key IN ({in_list}) "
                f"AND preview_image_url IS NOT NULL AND preview_image_url <> '' "
                f"ORDER BY is_active DESC, start_date DESC", tuple(keys)):
            images.setdefault(r["product_key"], r["preview_image_url"])
        week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        recent: dict = {}
        last_event: dict = {}
        for e in self._query(
                f"SELECT product_key, event, event_at, detail FROM ad_events "
                f"WHERE product_key IN ({in_list}) ORDER BY event_at DESC, id DESC", tuple(keys)):
            k = e["product_key"]
            if k not in last_event:
                last_event[k] = {"event": e["event"], "event_at": e["event_at"],
                                 "detail": _json_or_empty(e.get("detail"))}
            if str(e.get("event_at") or "") >= week_ago:
                c = recent.setdefault(k, {"launched": 0, "stopped": 0, "resumed": 0})
                if e["event"] in c:
                    c[e["event"]] += 1
        out = []
        for r in rows:
            k = r["product_key"]
            s = stats.get(k) or {}
            item = dict(r)
            item.update({
                "total_ads": int(s.get("total_ads") or 0),
                "active_ads": int(s.get("active_ads") or 0),
                "first_launch": s.get("first_launch") or "",
                "last_launch": s.get("last_launch") or "",
                "max_days": int(s.get("max_days") or 0),
                "last_seen": s.get("last_seen") or "",
                "checked_at": s.get("checked_at") or "",
                "image": images.get(k, ""),
                "last_event": last_event.get(k),
                "week": recent.get(k, {"launched": 0, "stopped": 0, "resumed": 0}),
            })
            out.append(item)
        return out

    def winning_product_events(self, pkey: str, limit: int = 60) -> list[dict]:
        p = self._ph()
        rows = self._query(
            f"SELECT id, ad_id, event, event_at, detail FROM ad_events WHERE product_key={p} "
            f"ORDER BY event_at DESC, id DESC LIMIT {int(limit)}", (pkey,))
        for r in rows:
            r["detail"] = _json_or_empty(r.get("detail"))
        return rows

    def max_ad_event_id(self) -> int:
        row = self._query_one("SELECT MAX(id) AS m FROM ad_events")
        return int((row or {}).get("m") or 0)

    def winning_events_after(self, last_id: int, limit: int = 1000) -> list[dict]:
        """Events of saved products newer than ``last_id`` (oldest first)."""
        p = self._ph()
        rows = self._query(
            f"SELECT id, product_key, ad_id, event, event_at, detail, page_name, store_domain, "
            f"link_url, body FROM ad_events WHERE id > {p} AND product_key IN "
            f"(SELECT product_key FROM winning_products) ORDER BY id LIMIT {int(limit)}",
            (int(last_id),))
        for r in rows:
            r["detail"] = _json_or_empty(r.get("detail"))
        return rows

    def product_active_count(self, pkey: str) -> int:
        row = self._query_one(
            f"SELECT COUNT(*) AS c FROM meta_ads WHERE product_key={self._ph()} AND is_active = 1", (pkey,))
        return int((row or {}).get("c") or 0)

    @_invalidates_cache
    def delete_target_everywhere(self, target_id: int) -> None:
        """Remove a competitor for everyone (and its page history)."""
        t = self.get_target(target_id)
        p = self._ph()
        self._execute(f"DELETE FROM user_targets WHERE target_id={p}", (int(target_id),))
        self.delete_target(int(target_id))
        if t and t.get("type") == "page":
            self.untrack_page(t.get("target_value"))

    @_invalidates_cache
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

    @_invalidates_cache
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

    def list_competitors(self, user_id: Optional[int] = None,
                        market: str = "dz") -> list[dict]:
        """Competitors of one user (their label/enabled), or all of them for
        an admin overview (with the emails of the users following each)."""
        self.list_targets()  # keeps the legacy keywords -> targets migration
        p = self._ph()
        mkt = "global" if str(market).lower() == "global" else "dz"
        mfilter = f"t.market={p}" if mkt == "global" else "(t.market IS NULL OR t.market = 'dz')"
        if user_id is not None:
            params = [int(user_id)] + ([mkt] if mkt == "global" else [])
            rows = self._query(
                f"SELECT t.*, ut.label AS user_label, ut.enabled AS user_enabled, "
                f"ut.interval_hours AS user_interval, ut.source AS source, ut.created_at AS tracked_at "
                f"FROM targets t JOIN user_targets ut ON ut.target_id = t.id "
                f"WHERE ut.user_id={p} AND {mfilter} "
                f"ORDER BY ut.created_at DESC, t.id DESC", tuple(params))
            for r in rows:
                r["label"] = r.pop("user_label") or r.get("label")
                r["enabled"] = r.pop("user_enabled")
                r["interval_hours"] = r.pop("user_interval") or r.get("interval_hours")
        else:
            if mkt == "global":
                rows = self._query(
                    f"SELECT * FROM targets WHERE market={p} ORDER BY id DESC", (mkt,))
            else:
                rows = self._query(
                    "SELECT * FROM targets WHERE (market IS NULL OR market = 'dz') ORDER BY id DESC")
        for t in rows:
            typ, val, tid = t.get("type") or "keyword", t.get("target_value") or "", t.get("id")
            mkt = t.get("market") or "dz"
            mcond = "market = 'global'" if mkt == "global" else "(market IS NULL OR market = 'dz')"
            if typ == "page":
                row = self._query_one(
                    f"SELECT COUNT(*) AS c FROM meta_ads WHERE ({mcond}) AND page_id = {p}", (val,))
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
                row = self._query_one(
                    f"SELECT COUNT(*) AS c FROM meta_ads WHERE ({mcond}) "
                    f"AND (store_domain = {p} OR query = {p})", (val, val))
            else:
                row = self._query_one(
                    f"SELECT COUNT(*) AS c FROM meta_ads WHERE ({mcond}) AND query = {p}", (val,))
            t["ads_count"] = int(row["c"]) if row and row.get("c") else 0
            owners = self._query(
                f"SELECT u.email FROM user_targets ut JOIN users u ON u.id = ut.user_id "
                f"WHERE ut.target_id={p} ORDER BY ut.created_at", (tid,))
            t["tracked_by"] = [o["email"] for o in owners]
        return rows

    @_invalidates_cache
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
            "(SELECT COUNT(*) FROM user_pages up WHERE up.user_id = users.id) AS tracked_pages_count, "
            "(SELECT COUNT(*) FROM security_events se WHERE se.event = 'api_register' "
            " AND se.user_id = users.id) AS extension_signup_count "
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

    @_invalidates_cache
    def delete_user(self, user_id: int) -> None:
        p = self._ph()
        self._execute(f"DELETE FROM user_ads WHERE user_id = {p}", (int(user_id),))
        for row in self._query(f"SELECT target_id FROM user_targets WHERE user_id = {p}", (int(user_id),)):
            self.remove_user_target(int(user_id), int(row["target_id"]))
        self._execute(f"DELETE FROM user_pages WHERE user_id = {p}", (int(user_id),))
        self._execute(f"DELETE FROM winning_products WHERE user_id = {p}", (int(user_id),))
        self._execute(f"DELETE FROM user_sessions WHERE user_id = {p}", (int(user_id),))
        self._execute(f"DELETE FROM api_tokens WHERE user_id = {p}", (int(user_id),))
        self._execute(f"DELETE FROM users WHERE id = {p}", (int(user_id),))

    def get_settings(self) -> dict:
        rows = self._query("SELECT key, value FROM settings")
        return {r["key"]: r["value"] for r in rows}

    @_invalidates_cache
    def set_setting(self, key: str, value: str) -> None:
        if key in ("cta_filter_enabled", "cta_allowlist"):
            self._cta_cache = (0.0, None)
            self.mark_groups_stale()  # the summary only counts allowed CTAs
        if key == "arabic_only":
            self._arabic_cache = (0.0, None)
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
        if key in ("cta_filter_enabled", "cta_allowlist"):
            self._cta_cache = (0.0, None)
            self.mark_groups_stale()  # again: a rebuild may have raced the write

    # ── AI competitor reports ─────────────────────────────────────────────
    # A report still "running" after this long died with its process.
    AI_REPORT_STALE_MIN = 20
    AI_AUDIT_STALE_MIN = 60  # an audit makes one model call per product

    def store_ads_for_analysis(self, domains: list[str], user_id: Optional[int] = None,
                               limit: int = 5000) -> list[dict]:
        """Every ad whose store_domain is one of ``domains`` (exact match),
        newest launch first. ``user_id`` limits it to what that user may see."""
        doms = [d for d in dict.fromkeys(str(x or "").strip().lower() for x in domains) if d]
        if not doms:
            return []
        p = self._ph()
        where = [f"store_domain IN ({', '.join([p] * len(doms))})"]
        params: list = list(doms)
        if user_id is not None:
            where.append(self._user_scope_sql())
            params.extend(self._user_scope_params(user_id))
        return self._query(
            "SELECT ad_id, page_id, page_name, store_domain, platform, link_url, body, cta_text, "
            "is_active, start_date, stopped_at, days_active, collation_count, publisher_platforms, "
            "product_key, primary_niche, first_seen "
            f"FROM meta_ads WHERE {' AND '.join(where)} "
            f"ORDER BY start_date DESC LIMIT {int(limit)}", tuple(params))

    def store_domains_under(self, host: str, limit: int = 50) -> list[dict]:
        """Distinct store domains equal to ``host`` or one of its subdomains,
        with their ad counts."""
        h = str(host or "").strip().lower()
        if not h:
            return []
        p = self._ph()
        return self._query(
            f"SELECT store_domain, COUNT(*) AS ads FROM meta_ads "
            f"WHERE store_domain = {p} OR store_domain LIKE {p} "
            f"GROUP BY store_domain ORDER BY ads DESC LIMIT {int(limit)}",
            (h, "%." + h.replace("%", "")))

    def similar_store_domains(self, label: str, limit: int = 8) -> list[dict]:
        """Store domains containing ``label`` (suggestions when nothing matched)."""
        lab = str(label or "").strip().lower().replace("%", "").replace("_", "")
        if len(lab) < 3:
            return []
        p = self._ph()
        return self._query(
            f"SELECT store_domain, COUNT(*) AS ads FROM meta_ads WHERE store_domain LIKE {p} "
            f"GROUP BY store_domain ORDER BY ads DESC LIMIT {int(limit)}", ("%" + lab + "%",))

    def create_ai_report(self, **fields) -> int:
        cols = ("user_id", "store_domain", "platform", "status", "provider", "model", "lang",
                "note", "ads_count", "unique_texts", "tokens_est", "dossier", "created_at", "kind")
        row = dict(fields, status="running", created_at=_now())
        row.setdefault("kind", "report")
        vals = tuple(row.get(c) for c in cols)
        p = self._ph()
        conn = self._connect()
        try:
            cur = conn.cursor()
            sql = f"INSERT INTO ai_reports ({', '.join(cols)}) VALUES ({', '.join([p] * len(cols))})"
            if self.is_postgres:
                cur.execute(sql + " RETURNING id", vals)
                new_id = cur.fetchone()[0]
            else:
                cur.execute(sql, vals)
                new_id = cur.lastrowid
            conn.commit()
            return int(new_id)
        finally:
            conn.close()

    def finish_ai_report(self, rid: int, report: str = "", error: str = "",
                         prompt_tokens: Optional[int] = None,
                         completion_tokens: Optional[int] = None,
                         cost: Optional[float] = None, audit: Optional[str] = None) -> None:
        p = self._ph()
        self._execute(
            f"UPDATE ai_reports SET status={p}, report={p}, error={p}, prompt_tokens={p}, "
            f"completion_tokens={p}, cost={p}, finished_at={p}, audit=COALESCE({p}, audit) WHERE id={p}",
            ("error" if error else "done", report or "", (error or "")[:2000],
             prompt_tokens, completion_tokens, cost, _now(), audit, int(rid)))

    def progress_ai_report(self, rid: int, report: str, audit: Optional[str] = None) -> None:
        """Partial result of a running report (status stays 'running')."""
        p = self._ph()
        self._execute(
            f"UPDATE ai_reports SET report={p}, audit=COALESCE({p}, audit) "
            f"WHERE id={p} AND status='running'", (report or "", audit, int(rid)))

    def _ai_report_fresh(self, row: Optional[dict]) -> Optional[dict]:
        """Mark a long-dead "running" report as failed (on read)."""
        if row and row.get("status") == "running":
            try:
                started = datetime.fromisoformat(str(row.get("created_at")))
                age_min = (datetime.now(timezone.utc) - started).total_seconds() / 60
            except Exception:
                age_min = 0
            limit = self.AI_AUDIT_STALE_MIN if row.get("kind") == "audit" else self.AI_REPORT_STALE_MIN
            if age_min > limit:
                self.finish_ai_report(int(row["id"]), error="interrupted (server restarted?)")
                row = dict(row, status="error", error="interrupted (server restarted?)")
        return row

    def get_ai_report(self, rid: int) -> Optional[dict]:
        row = self._query_one(f"SELECT * FROM ai_reports WHERE id={self._ph()}", (int(rid),))
        return self._ai_report_fresh(row)

    def list_ai_reports(self, user_id: Optional[int] = None, domain: str = "",
                        limit: int = 100) -> list[dict]:
        p = self._ph()
        where, params = [], []
        if user_id is not None:
            where.append(f"user_id={p}")
            params.append(int(user_id))
        if domain:
            where.append(f"store_domain={p}")
            params.append(domain)
        clause = ("WHERE " + " AND ".join(where)) if where else ""
        rows = self._query(
            "SELECT id, user_id, store_domain, platform, status, kind, provider, model, lang, note, "
            "ads_count, unique_texts, tokens_est, prompt_tokens, completion_tokens, cost, error, "
            f"created_at, finished_at FROM ai_reports {clause} ORDER BY id DESC LIMIT {int(limit)}",
            tuple(params))
        return [self._ai_report_fresh(r) for r in rows]

    def count_ai_reports_since(self, user_id: int, since_iso: str) -> int:
        p = self._ph()
        row = self._query_one(
            f"SELECT COUNT(*) AS n FROM ai_reports WHERE user_id={p} AND created_at >= {p} "
            "AND COALESCE(provider, '') <> 'manual'",  # pasted AI Studio answers are free
            (int(user_id), since_iso))
        return int((row or {}).get("n") or 0)

    def delete_ai_report(self, rid: int) -> None:
        self._execute(f"DELETE FROM ai_reports WHERE id={self._ph()}", (int(rid),))


db = Database()

