# -*- coding: utf-8 -*-
"""AdsCloud MCP server (READ-ONLY).

Exposes the scraped ad bank to MCP agents (ChatGPT / Claude / opencode).

Local (stdio):
    python mcp_server.py

Remote (Streamable HTTP, bearer token required):
    MCP_TOKEN=<long-random-token> python mcp_server.py --http --port 5003
    endpoint: POST /mcp      health: GET /health
"""
from __future__ import annotations

import argparse
import hmac
import json
import os
import sys
from typing import Any, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from db import db  # noqa: E402
from mcp.server.mcpserver import MCPServer  # noqa: E402

MAX_LIMIT = 100
SORTS = {"collected_at", "longest_running", "newest_launch", "most_collated", "biggest_page"}

INSTRUCTIONS = """\
AdsCloud holds ads scraped from the Meta (Facebook/Instagram) Ad Library for the
Algerian market. Every row is one ad with:
  ad_id, page_name, page_id, store_domain, start_date, days_active, is_active,
  collation_count, publisher_platforms, cta_text, body, link_url, video_url,
  keywords (the scrape keyword(s) that found the ad), page_total_ads,
  domain_total_ads.

How to work:
- Prefer search_ads with filters (keyword / store / page_id / dates / active).
- Use grouped=True to get one card per product (same advertiser + same product).
- Use get_ad(ad_id) for the full text/media of one ad.
- Use list_competitors to compare stores on a keyword or date window.
- Use keyword_performance to see which keywords find winners.

Product image workflow (important):
- You cannot upload images to this server. When the user shares a product photo,
  inspect it yourself (vision), then describe the product in short Arabic and
  French keywords: type, material, use, distinctive features (e.g. "مكنسة
  بخار", "منظف بالبخار", "nettoyeur vapeur"). Call search_by_attributes or
  search_ads with those words to find competing stores/ads.
- Then call list_competitors with the best keyword to rank the stores.

This server is read-only: it cannot scrape, tag or modify data.
"""

mcp = MCPServer(
    "AdsCloud",
    title="AdsCloud — Meta Ads Intelligence (DZ)",
    instructions=INSTRUCTIONS,
    version="1.0.0",
)


def _j(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)


def _limit(n: Any, default: int = 20) -> int:
    try:
        return max(1, min(int(n), MAX_LIMIT))
    except Exception:
        return default


def _sort(s: Any) -> str:
    s = str(s or "collected_at").strip()
    return s if s in SORTS else "collected_at"


def _compact(ad: dict) -> dict:
    body = ad.get("body") or ""
    return {
        "ad_id": ad.get("ad_id"),
        "page_name": ad.get("page_name"),
        "page_id": ad.get("page_id"),
        "store_domain": ad.get("store_domain"),
        "start_date": ad.get("start_date"),
        "days_active": ad.get("days_active"),
        "is_active": bool(ad.get("is_active")),
        "collation_count": ad.get("collation_count"),
        "platforms": ad.get("publisher_platforms") or [],
        "cta": ad.get("cta_text"),
        "keywords": ad.get("keywords") or ([ad.get("query")] if ad.get("query") else []),
        "niches": [n.get("slug") for n in (ad.get("niches") or [])],
        "link_url": ad.get("link_url"),
        "video_url": ad.get("video_url"),
        "page_total_ads": ad.get("page_total_ads"),
        "domain_total_ads": ad.get("domain_total_ads"),
        "group": (
            {"key": ad["group"].get("key"), "count": ad["group"].get("count"),
             "active": ad["group"].get("active")}
            if ad.get("group") else None
        ),
        "body": body[:400] + ("…" if len(body) > 400 else ""),
    }


@mcp.tool()
def stats_overview() -> str:
    """High-level counters: total ads, stores, tracked pages, last collection,
    plus the top scrape keywords with their ad counts."""
    stats = db.get_stats()
    kws = db.get_keywords_overview(limit=15)
    return _j({"stats": stats, "top_keywords": kws})


@mcp.tool()
def search_ads(
    text: str = "",
    keyword: str = "",
    niche: str = "",
    store: str = "",
    page_id: str = "",
    date_from: str = "",
    date_to: str = "",
    active_only: bool = False,
    min_days: int = 0,
    sort: str = "collected_at",
    grouped: bool = False,
    limit: int = 20,
) -> str:
    """Search the ad bank. All filters are optional and combine with AND.

    Args:
        text: free text over the ad body, page name and store domain.
        keyword: exact scrape keyword tag (e.g. "خشبي"). See keyword_performance.
        niche: niche slug (e.g. "kitchen", "beauty", "supplements"). See list_niches.
        store: store domain (e.g. "novamartdz.store").
        page_id: Facebook page id.
        date_from: launch date >= YYYY-MM-DD.
        date_to: launch date <= YYYY-MM-DD.
        active_only: only ads that are still running.
        min_days: minimum days active (>= 1).
        sort: collected_at | longest_running | newest_launch | most_collated | biggest_page.
        grouped: True -> one row per product group (same advertiser + same product).
        limit: 1..100.
    """
    limit = _limit(limit)
    filters = dict(
        search=(text or "").strip(),
        keywords=[keyword.strip()] if (keyword or "").strip() else None,
        niches=[niche.strip()] if (niche or "").strip() else None,
        store_domain=(store or "").strip(),
        page_id=(page_id or "").strip(),
        start_date_min=(date_from or "").strip(),
        start_date_max=(date_to or "").strip(),
        active_only=bool(active_only),
        min_days_active=max(0, int(min_days or 0)),
    )
    if grouped:
        rows = db.get_ad_groups(limit=limit, sort_by=_sort(sort), **filters)
        total = db.count_ad_groups(**filters)
    else:
        rows = db.get_ads(limit=limit, sort_by=_sort(sort), **filters)
        total = db.count_ads(**filters)
    rows = db.attach_entity_stats(rows)
    if not grouped:
        rows = db.attach_keywords(rows)
    return _j({"total": total, "returned": len(rows), "grouped": grouped,
               "ads": [_compact(a) for a in rows]})


@mcp.tool()
def search_by_attributes(attributes: str, limit: int = 20) -> str:
    """Find competitors for a product described by attributes.

    Use this after looking at a product image: pass a short description with
    the product type and distinctive features in Arabic and/or French, e.g.
    "مكنسة بخار محمولة منظف بالبخار nettoyeur vapeur". Returns matching ads
    from the bank (full text search over ad bodies).
    """
    return search_ads(text=attributes, limit=limit, grouped=False)


@mcp.tool()
def get_ad(ad_id: str) -> str:
    """Full details of one ad: complete body, media URL, advertiser, keywords,
    page/store totals. Use after search_ads to inspect a specific ad."""
    p = db._ph()
    row = db._query_one(f"SELECT * FROM meta_ads WHERE ad_id = {p}", (str(ad_id).strip(),))
    if not row:
        return _j({"error": "ad not found", "ad_id": ad_id})
    try:
        row["publisher_platforms"] = json.loads(row.get("publisher_platforms") or "[]")
    except Exception:
        row["publisher_platforms"] = []
    row = db.attach_entity_stats(db.attach_keywords([row]))[0]
    out = _compact(row)
    out["body"] = row.get("body") or ""
    return _j(out)


@mcp.tool()
def list_competitors(
    keyword: str = "",
    store: str = "",
    page_id: str = "",
    date_from: str = "",
    date_to: str = "",
    limit: int = 15,
) -> str:
    """Rank the stores competing on a keyword (or inside a date window).

    Returns one row per store domain: number of ads, active ads, winning ads
    (>= 5 days active), distinct pages, last seen. Pass at least one of
    keyword / store / page_id / date range.
    """
    limit = _limit(limit, 15)
    clause, params = db._build_ads_filter(
        keywords=[keyword.strip()] if (keyword or "").strip() else None,
        store_domain=(store or "").strip(),
        page_id=(page_id or "").strip(),
        start_date_min=(date_from or "").strip(),
        start_date_max=(date_to or "").strip(),
    )
    extra = "store_domain != '' AND store_domain IS NOT NULL"
    where = (clause + " AND " + extra) if clause else ("WHERE " + extra)
    sql = (
        "SELECT store_domain, COUNT(*) AS ads, "
        "SUM(CASE WHEN days_active >= 5 AND is_active = 1 THEN 1 ELSE 0 END) AS winning, "
        "SUM(CASE WHEN is_active = 1 THEN 1 ELSE 0 END) AS active, "
        "COUNT(DISTINCT page_id) AS pages, MAX(collected_at) AS last_seen "
        f"FROM meta_ads {where} GROUP BY store_domain ORDER BY ads DESC LIMIT {limit}"
    )
    return _j({"filters": {"keyword": keyword, "store": store, "page_id": page_id,
                           "date_from": date_from, "date_to": date_to},
               "competitors": db._query(sql, tuple(params))})


@mcp.tool()
def store_insights(domain: str) -> str:
    """Totals and the longest-running ads for one store domain."""
    domain = (domain or "").strip()
    if not domain:
        return _j({"error": "domain required"})
    clause, params = db._build_ads_filter(store_domain=domain)
    totals = db._query_one(
        "SELECT COUNT(*) AS ads, "
        "SUM(CASE WHEN is_active = 1 THEN 1 ELSE 0 END) AS active, "
        "SUM(CASE WHEN days_active >= 5 AND is_active = 1 THEN 1 ELSE 0 END) AS winning, "
        "COUNT(DISTINCT page_id) AS pages, MIN(start_date) AS first_launch, "
        f"MAX(start_date) AS last_launch FROM meta_ads {clause}", tuple(params))
    rows = db.attach_keywords(db.get_ads(store_domain=domain, limit=10, sort_by="longest_running"))
    return _j({"domain": domain, "totals": totals, "top_ads": [_compact(a) for a in rows]})


@mcp.tool()
def page_insights(page_id: str) -> str:
    """Totals, store domains and top ads for one Facebook page id."""
    pid = str(page_id or "").strip()
    if not pid:
        return _j({"error": "page_id required"})
    clause, params = db._build_ads_filter(page_id=pid)
    totals = db._query_one(
        "SELECT COUNT(*) AS ads, "
        "SUM(CASE WHEN is_active = 1 THEN 1 ELSE 0 END) AS active, "
        "SUM(CASE WHEN days_active >= 5 AND is_active = 1 THEN 1 ELSE 0 END) AS winning, "
        f"MIN(start_date) AS first_launch, MAX(start_date) AS last_launch FROM meta_ads {clause}",
        tuple(params))
    joiner = " AND " if clause else "WHERE "
    stores = db._query(
        "SELECT store_domain, COUNT(*) AS ads FROM meta_ads "
        f"{clause}{joiner}store_domain != '' GROUP BY store_domain ORDER BY ads DESC LIMIT 10",
        tuple(params))
    rows = db.attach_keywords(db.get_ads(page_id=pid, limit=10, sort_by="longest_running"))
    return _j({"page_id": pid, "totals": totals, "store_domains": stores,
               "top_ads": [_compact(a) for a in rows]})


@mcp.tool()
def list_niches() -> str:
    """List the niche taxonomy with the number of classified ads in each
    (kitchen, beauty, supplements, fashion, kids, electronics, home-tools,
    cleaning, fitness, auto, pets, jewelry, perfumes, furniture, bags-shoes)."""
    return _j({"niches": db.niche_counts()})


@mcp.tool()
def keyword_performance(limit: int = 50) -> str:
    """Performance of every scrape keyword: total ads, store ads, winning ads
    (>= 5 days), fresh ads (<= 3 days) and last run."""
    return _j({"keywords": db.get_keywords_overview(limit=_limit(limit, 50))})


@mcp.tool()
def find_winning_ads(
    keyword: str = "",
    store: str = "",
    min_days: int = 5,
    limit: int = 20,
) -> str:
    """Active ads that have been running for at least `min_days` (winners)."""
    rows = db.attach_keywords(db.get_ads(
        keywords=[keyword.strip()] if (keyword or "").strip() else None,
        store_domain=(store or "").strip(),
        active_only=True,
        min_days_active=max(1, int(min_days or 5)),
        sort_by="longest_running",
        limit=_limit(limit),
    ))
    return _j({"returned": len(rows), "ads": [_compact(a) for a in rows]})


@mcp.tool()
def product_group(group_key: str, limit: int = 50) -> str:
    """Every ad inside one product group (the `group.key` returned by
    search_ads(..., grouped=True))."""
    key = str(group_key or "").strip()
    if not key:
        return _j({"error": "group_key required"})
    rows = db.attach_entity_stats(db.get_group_ads(key, limit=_limit(limit, 50)))
    return _j({"group_key": key, "returned": len(rows), "ads": [_compact(a) for a in rows]})


def main() -> None:
    ap = argparse.ArgumentParser(description="AdsCloud MCP server (read-only)")
    ap.add_argument("--http", action="store_true", help="serve Streamable HTTP instead of stdio")
    ap.add_argument("--host", default=os.environ.get("MCP_HOST", "0.0.0.0"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("MCP_PORT", "5003")))
    args = ap.parse_args()

    if not args.http:
        mcp.run()  # stdio
        return

    token = (os.environ.get("MCP_TOKEN") or "").strip()
    if len(token) < 16:
        raise SystemExit("MCP_TOKEN (>=16 chars) is required for --http")

    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.responses import JSONResponse
    from mcp.server.transport_security import TransportSecuritySettings
    import uvicorn

    class BearerAuth(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            if request.url.path.rstrip("/") == "/health":
                return JSONResponse({"ok": True, "service": "adscloud-mcp"})
            auth = request.headers.get("authorization", "")
            provided = auth.split(" ", 1)[1].strip() if auth.lower().startswith("bearer ") else ""
            if not provided:
                provided = (request.headers.get("x-mcp-token", "")
                            or request.query_params.get("token", "")
                            or request.query_params.get("access_token", "")).strip()
            if not provided or not hmac.compare_digest(provided, token):
                return JSONResponse({"error": "unauthorized"}, status_code=401)
            return await call_next(request)

    app = mcp.streamable_http_app(transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=False))
    app.add_middleware(BearerAuth)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
