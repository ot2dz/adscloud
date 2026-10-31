# -*- coding: utf-8 -*-
"""Push locally scraped ads to the production AdsCloud site.

Reads the local SQLite bank and POSTs the ads to /api/ingest in batches.
The server upserts by ad_id, so re-sending the same ads is safe.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

from db import _SQLITE_PATH

COLS = [
    "ad_id", "page_name", "page_id", "start_date", "is_active", "link_url",
    "store_domain", "platform", "body", "cta_text", "collation_count",
    "publisher_platforms", "query", "country", "preview_image_url",
    "video_url", "video_sd_url", "collation_id",
]


def load_ads(since: str | None, limit: int | None) -> list[dict]:
    if not os.path.exists(_SQLITE_PATH):
        sys.exit(f"local database not found: {_SQLITE_PATH}")
    conn = sqlite3.connect(_SQLITE_PATH)
    conn.row_factory = sqlite3.Row
    where, params = "", []
    if since:
        where = "WHERE collected_at >= ?"
        params.append(since)
    sql = f"SELECT {', '.join(COLS)} FROM meta_ads {where} ORDER BY collected_at"
    if limit:
        sql += f" LIMIT {int(limit)}"
    rows = [dict(r) for r in conn.execute(sql, params)]
    conn.close()
    for r in rows:
        r["is_active"] = bool(r["is_active"])
        r["collation_count"] = int(r["collation_count"] or 1)
        try:
            r["publisher_platforms"] = json.loads(r["publisher_platforms"] or "[]")
        except (TypeError, ValueError):
            r["publisher_platforms"] = []
        r["query"] = (r.get("query") or "").strip()
        r["country"] = (r.get("country") or "DZ").strip().upper() or "DZ"
    return rows


def post_batch(base_url: str, token: str, ads: list[dict], query: str, country: str) -> int:
    body = json.dumps({"ads": ads, "query": query, "country": country},
                      ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        base_url.rstrip("/") + "/api/ingest", data=body, method="POST",
        headers={"Content-Type": "application/json", "X-Ingest-Token": token})
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        sys.exit(f"server refused the batch: HTTP {exc.code} {detail}")
    except urllib.error.URLError as exc:
        sys.exit(f"cannot reach {base_url}: {exc.reason}")
    return int(data.get("stored") or 0)


def main() -> int:
    today = (datetime.now(timezone.utc) + timedelta(hours=1)).date().isoformat()
    ap = argparse.ArgumentParser(description="Push local AdsCloud ads to production.")
    ap.add_argument("--url", default=os.environ.get("ADSCLOUD_URL", "https://ads.botdz.com"))
    ap.add_argument("--token", default=os.environ.get("INGEST_TOKEN", ""))
    ap.add_argument("--since", default=None, help="only ads collected on/after YYYY-MM-DD")
    ap.add_argument("--all", action="store_true", help="push the whole local bank")
    ap.add_argument("--limit", type=int, default=None, help="stop after N ads")
    ap.add_argument("--batch", type=int, default=100)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    since = None if args.all else (args.since or today)
    ads = load_ads(since, args.limit)
    if not ads:
        print(f"no local ads to push (since={since or 'all'})")
        return 0

    groups: dict[tuple[str, str], list[dict]] = {}
    for ad in ads:
        groups.setdefault((ad["query"], ad["country"]), []).append(ad)

    print(f"local bank: {_SQLITE_PATH}")
    print(f"selected: {len(ads)} ads in {len(groups)} group(s) (since={since or 'all'})")
    for (query, country), items in groups.items():
        print(f"  query={query or '(none)'} country={country} ads={len(items)}")
    if args.dry_run:
        print("dry run - nothing sent")
        print(json.dumps(ads[0], ensure_ascii=False, indent=2))
        return 0

    if not args.token:
        sys.exit("missing token: pass --token or set INGEST_TOKEN (Coolify env)")
    if args.batch < 1:
        sys.exit("--batch must be >= 1")

    sent = stored = 0
    for (query, country), items in groups.items():
        print(f"pushing query={query or '(none)'} country={country} ads={len(items)}")
        for i in range(0, len(items), args.batch):
            chunk = items[i:i + args.batch]
            got = post_batch(args.url, args.token, chunk, query, country)
            sent += len(chunk)
            stored += got
            print(f"  batch {i // args.batch + 1}: sent={len(chunk)} new={got}")

    print(f"done: sent={sent} stored_new={stored} -> {args.url}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
