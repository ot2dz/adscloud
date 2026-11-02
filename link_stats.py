# -*- coding: utf-8 -*-
"""Snapshots of public link metrics and the bot's followed pages."""

from __future__ import annotations

import json
from typing import Optional

from db import db, _now


def ensure_tables() -> None:
    p = db._ph()
    id_col = "SERIAL PRIMARY KEY" if db.is_postgres else "INTEGER PRIMARY KEY AUTOINCREMENT"
    conn = db._connect()
    try:
        cur = conn.cursor()
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS link_stats (
                id {id_col},
                source_url TEXT,
                page_id TEXT,
                page_name TEXT,
                post_id TEXT,
                media_type TEXT,
                views BIGINT,
                reactions BIGINT,
                comments BIGINT,
                shares BIGINT,
                ad_id TEXT,
                match_type TEXT,
                raw_json TEXT,
                collected_at TEXT
            )""")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS followed_pages (
                chat_id TEXT NOT NULL,
                page_id TEXT NOT NULL,
                page_name TEXT,
                created_at TEXT,
                active INTEGER DEFAULT 1,
                last_notified_at TEXT,
                last_seen_ad_id TEXT,
                PRIMARY KEY (chat_id, page_id)
            )""")
        conn.commit()
    finally:
        conn.close()


def save_snapshot(source_url: str, page_id: str = "", page_name: str = "",
                  post_id: str = "", media_type: str = "", views: Optional[int] = None,
                  reactions: Optional[int] = None, comments: Optional[int] = None,
                  shares: Optional[int] = None, ad_id: str = "", match_type: str = "none",
                  raw: dict | None = None) -> int:
    ensure_tables()
    p = db._ph()
    conn = db._connect()
    try:
        cur = conn.cursor()
        cur.execute(
            f"INSERT INTO link_stats (source_url, page_id, page_name, post_id, media_type, views,"
            f" reactions, comments, shares, ad_id, match_type, raw_json, collected_at)"
            f" VALUES ({p},{p},{p},{p},{p},{p},{p},{p},{p},{p},{p},{p},{p})",
            (source_url, page_id, page_name, post_id, media_type, views, reactions,
             comments, shares, ad_id, match_type,
             json.dumps(raw or {}, ensure_ascii=False), _now()))
        conn.commit()
        try:
            return int(cur.lastrowid) if not db.is_postgres else 0
        except Exception:
            return 0
    finally:
        conn.close()


def latest(source_url: str) -> Optional[dict]:
    ensure_tables()
    rows = db._query(
        f"SELECT * FROM link_stats WHERE source_url={db._ph()} ORDER BY collected_at DESC LIMIT 1",
        (source_url,))
    return dict(rows[0]) if rows else None


def history(source_url: str, limit: int = 20) -> list[dict]:
    ensure_tables()
    p = db._ph()
    rows = db._query(
        f"SELECT * FROM link_stats WHERE source_url={p} ORDER BY collected_at DESC LIMIT {p}",
        (source_url, max(1, int(limit))))
    return [dict(r) for r in rows]


def for_page(page_id: str, limit: int = 20) -> list[dict]:
    ensure_tables()
    p = db._ph()
    rows = db._query(
        f"SELECT * FROM link_stats WHERE page_id={p} ORDER BY collected_at DESC LIMIT {p}",
        (str(page_id), max(1, int(limit))))
    return [dict(r) for r in rows]


def follow(chat_id: str, page_id: str, page_name: str = "") -> None:
    ensure_tables()
    p = db._ph()
    conn = db._connect()
    try:
        cur = conn.cursor()
        if db.is_postgres:
            cur.execute(
                f"INSERT INTO followed_pages (chat_id, page_id, page_name, created_at, active)"
                f" VALUES ({p},{p},{p},{p},1) ON CONFLICT (chat_id, page_id) DO UPDATE"
                f" SET active=1, page_name=EXCLUDED.page_name",
                (str(chat_id), str(page_id), page_name, _now()))
        else:
            cur.execute(
                f"INSERT OR REPLACE INTO followed_pages (chat_id, page_id, page_name, created_at, active)"
                f" VALUES ({p},{p},{p},{p},1)",
                (str(chat_id), str(page_id), page_name, _now()))
        conn.commit()
    finally:
        conn.close()


def unfollow(chat_id: str, page_id: str) -> None:
    ensure_tables()
    p = db._ph()
    conn = db._connect()
    try:
        cur = conn.cursor()
        cur.execute(f"DELETE FROM followed_pages WHERE chat_id={p} AND page_id={p}",
                    (str(chat_id), str(page_id)))
        conn.commit()
    finally:
        conn.close()


def is_followed(chat_id: str, page_id: str) -> bool:
    ensure_tables()
    p = db._ph()
    row = db._query_one(
        f"SELECT 1 AS x FROM followed_pages WHERE chat_id={p} AND page_id={p} AND active=1",
        (str(chat_id), str(page_id)))
    return bool(row)


def followed(chat_id: Optional[str] = None) -> list[dict]:
    ensure_tables()
    if chat_id:
        rows = db._query(f"SELECT * FROM followed_pages WHERE chat_id={db._ph()} AND active=1",
                         (str(chat_id),))
    else:
        rows = db._query("SELECT * FROM followed_pages WHERE active=1")
    return [dict(r) for r in rows]


def mark_notified(chat_id: str, page_id: str, last_seen_ad_id: str = "") -> None:
    ensure_tables()
    p = db._ph()
    conn = db._connect()
    try:
        cur = conn.cursor()
        cur.execute(
            f"UPDATE followed_pages SET last_notified_at={p}, last_seen_ad_id={p}"
            f" WHERE chat_id={p} AND page_id={p}",
            (_now(), last_seen_ad_id, str(chat_id), str(page_id)))
        conn.commit()
    finally:
        conn.close()

