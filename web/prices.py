# -*- coding: utf-8 -*-
"""Product prices: the list of pages still to read, and their results.

The server never opens a product page here. It only hands the admin's browser
(Chrome extension) the links to read and stores what comes back. A store that
answers with a bot check is left alone for a day; its price can be typed.
"""
from __future__ import annotations

import ipaddress
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from flask import Blueprint, jsonify, request

from db import db
from store_filter import _TRACKING_PARAMS
from web.common import _audit, _int_arg, admin_required

bp = Blueprint("prices", __name__)

LO, HI = 200, 500_000      # plausible product price (DZD), as in price_fetch
FRESH_DAYS = 14            # re-read a known price after this
RETRY_DAYS = 3             # try again a page that gave no price
BLOCK_HOURS = 24           # leave a store alone after a bot check
PER_HOST = 2               # pages of one store per batch
STATUSES = ("ok", "not_found", "blocked", "error")


def _ago(**kw) -> str:
    return (datetime.now(timezone.utc) - timedelta(**kw)).isoformat()


def public_url(url: str) -> str:
    """The product link without tracking parameters, "" unless a public site."""
    try:
        u = urlsplit((url or "").strip())
        host = (u.hostname or "").lower()
        port = u.port
    except ValueError:
        return ""
    if u.scheme not in ("http", "https") or u.username or port not in (None, 80, 443):
        return ""
    if "." not in host or host.endswith((".local", ".localhost", ".internal", ".lan", ".home")):
        return ""
    try:
        ipaddress.ip_address(host)
        return ""
    except ValueError:
        pass
    if host.rsplit(".", 1)[-1].isdigit():  # 127.1 and friends
        return ""
    query = urlencode([(k, v) for k, v in parse_qsl(u.query)
                       if not k.lower().startswith(_TRACKING_PARAMS)])
    return urlunsplit((u.scheme, u.netloc, u.path, query, ""))


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def _pending_clause() -> tuple[str, list]:
    p = db._ph()
    clause, params = db._build_ads_filter(stores_only=True, active_only=True)
    clause += (f" AND product_key LIKE {p} AND link_url <> '' AND NOT EXISTS ("
               f"SELECT 1 FROM product_prices pp WHERE pp.product_key = meta_ads.product_key AND "
               f"(pp.source = 'manual' OR pp.checked_at >= "
               f"CASE WHEN pp.status = 'ok' THEN {p} ELSE {p} END))")
    return clause, params + ["%|u:%", _ago(days=FRESH_DAYS), _ago(days=RETRY_DAYS)]


@bp.route("/api/prices/queue")
@admin_required
def api_prices_queue():
    """Next product pages to read, newest scraped first, spread over stores."""
    p = db._ph()
    limit = max(1, min(_int_arg("limit", 10), 30))
    clause, params = _pending_clause()
    blocked = {r["host"] for r in db._query(
        f"SELECT DISTINCT host FROM product_prices WHERE status = 'blocked' AND checked_at >= {p}",
        (_ago(hours=BLOCK_HOURS),))}
    rows = db._query(
        f"SELECT product_key, MAX(link_url) AS url, MAX(collected_at) AS seen FROM meta_ads {clause} "
        f"GROUP BY product_key ORDER BY seen DESC, product_key LIMIT 3000", tuple(params))
    items, per_host, waiting = [], {}, 0
    for r in rows:
        url = public_url(r["url"])
        host = _host(url) if url else ""
        if not url or host in blocked:
            continue
        waiting += 1
        if len(items) < limit and per_host.get(host, 0) < PER_HOST:
            per_host[host] = per_host.get(host, 0) + 1
            items.append({"key": r["product_key"], "url": url})
    return jsonify({"items": items, "waiting": waiting, "more": len(rows) >= 3000,
                    "blocked_hosts": len(blocked)})


def _price(v):
    try:
        n = int(v)
    except (TypeError, ValueError, OverflowError):
        return None
    return n if LO <= n <= HI else None


def _known(key: str):
    return db._query_one(f"SELECT link_url FROM meta_ads WHERE product_key = {db._ph()} LIMIT 1", (key,))


def _save(key: str, host: str, price, source: str, how: str, status: str) -> None:
    ph = ", ".join([db._ph()] * 7)
    # A failed re-read keeps the last known price; a typed price is never replaced.
    keep = "" if source == "manual" else " WHERE product_prices.source <> 'manual'"
    db._execute(
        f"INSERT INTO product_prices (product_key, host, price, source, how, status, checked_at) "
        f"VALUES ({ph}) ON CONFLICT (product_key) DO UPDATE SET host = excluded.host, "
        f"price = COALESCE(excluded.price, product_prices.price), source = excluded.source, "
        f"how = excluded.how, status = excluded.status, checked_at = excluded.checked_at{keep}",
        (key, host, price, source, how, status, datetime.now(timezone.utc).isoformat()))


@bp.route("/api/prices/results", methods=["POST"])
@admin_required
def api_prices_results():
    data = request.get_json(force=True, silent=True)
    results = data.get("results") if isinstance(data, dict) else None
    saved = 0
    for r in (results if isinstance(results, list) else [])[:30]:
        if not isinstance(r, dict) or r.get("status") not in STATUSES:
            continue
        key = str(r.get("key") or "")
        row = _known(key) if key else None
        if not row:
            continue
        price = _price(r.get("price")) if r["status"] == "ok" else None
        status = "not_found" if r["status"] == "ok" and not price else r["status"]
        _save(key, _host(row["link_url"] or ""), price, "browser", str(r.get("how") or "")[:20], status)
        saved += 1
    if saved:
        db.invalidate_cache()
    return jsonify({"ok": True, "saved": saved})


@bp.route("/api/prices/manual", methods=["POST"])
@admin_required
def api_prices_manual():
    """Type a product's price (kept over automatic reads); empty removes it."""
    data = request.get_json(force=True, silent=True)
    data = data if isinstance(data, dict) else {}
    key = str(data.get("key") or "")
    row = _known(key) if key else None
    if not row:
        return jsonify({"error": "المنتج غير موجود"}), 404
    if data.get("price") in (None, ""):
        db._execute(f"DELETE FROM product_prices WHERE product_key = {db._ph()}", (key,))
        price = None
    else:
        price = _price(data.get("price"))
        if not price:
            return jsonify({"error": f"السعر يجب أن يكون بين {LO} و {HI} دج"}), 400
        _save(key, _host(row["link_url"] or ""), price, "manual", "", "ok")
    db.invalidate_cache()
    _audit("price_manual", f"{key[:120]} = {price}")
    return jsonify({"ok": True, "price": price})
