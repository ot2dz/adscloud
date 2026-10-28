import os
import csv
import functools
import io
import json
import logging
import secrets
import time
from datetime import datetime, timezone
from typing import Callable, Optional

from flask import Blueprint, Response, g, jsonify, render_template, request, send_file, session
from werkzeug.security import check_password_hash, generate_password_hash

from db import db
from pages import clean_domain, extract_page_id, resolve_target_url
from store_filter import extract_store_domain
from scrape_manager import (
    manager, job_wakeup, DEFAULT_COUNTRY, FIXED_COUNTRY, FIXED_LANGS,
    MONTH_MODE, MONTH_LANGS, MARKET_WILDCARD, STATUS_MODE, SCRAPE_ENABLED, _proxy_setting,
)
from background import _queue_month_scrape, _queue_status_check
from web.common import (
    SESSION_DAYS, _start_session, _int_arg, _client_ip, _audit,
    _login_blocked, _check_credentials, _registration_open,
    login_required, admin_required, _scope_user_id,
    LOGIN_MAX_PER_EMAIL, LOGIN_MAX_PER_IP, LOGIN_WINDOW_MIN,
    REGISTER_MAX_PER_IP_HOUR, MIN_PASSWORD_LEN,
)

logger = logging.getLogger("web_app")

bp = Blueprint("ads", __name__)


bp = Blueprint("ads", __name__)


@bp.route("/api/ads")
@login_required
def api_ads():
    limit = max(1, min(_int_arg("limit", 60), 500))
    offset = max(0, _int_arg("offset", 0))
    search = request.args.get("search", "")
    query = request.args.get("query", "")
    store_domain = request.args.get("store_domain", "")
    page_id = request.args.get("page_id", "")
    stores_only = True
    preset = request.args.get("preset", "")
    active_only = request.args.get("active_only", "0") == "1"
    start_date_min = request.args.get("start_date_min", "")
    start_date_max = request.args.get("start_date_max", "")
    sort_by = request.args.get("sort_by", "collected_at")
    min_days_active = max(0, _int_arg("min_days_active", 0))
    min_product_ads = max(0, _int_arg("min_product_ads", 0))
    target_id = request.args.get("target_id")
    target_id_val = int(target_id) if target_id and target_id.isdigit() else None
    keywords = [k.strip() for k in (request.args.get("keywords") or "").split(",") if k.strip()]
    niches = [x.strip() for x in (request.args.get("niche") or "").split(",") if x.strip()]

    filters = dict(
        search=search, stores_only=stores_only, query=query,
        store_domain=store_domain, page_id=page_id, preset=preset,
        active_only=active_only, start_date_min=start_date_min,
        start_date_max=start_date_max, min_days_active=min_days_active,
        target_id=target_id_val, user_id=_scope_user_id(),
        min_product_ads=min_product_ads, keywords=keywords, niches=niches,
    )
    total_ads = db.count_ads(**filters)
    if request.args.get("group", "0") == "1":
        # One card per product (same advertiser + same product link); paging
        # and ``total`` count products, ``total_ads`` the ads behind them.
        items = db.attach_entity_stats(
            db.get_ad_groups(limit=limit, offset=offset, sort_by=sort_by, **filters))
        return jsonify({"ads": items, "total": db.count_ad_groups(**filters),
                        "total_ads": total_ads, "grouped": True})
    ads = db.attach_entity_stats(db.get_ads(limit=limit, offset=offset, sort_by=sort_by, **filters))
    return jsonify({"ads": ads, "total": total_ads, "total_ads": total_ads, "grouped": False})


@bp.route("/api/ads/group")
@login_required
def api_ads_group():
    """Every ad of one product group, for the "show all N ads" view."""
    key = (request.args.get("key") or "").strip()
    if not key:
        return jsonify({"error": "key required"}), 400
    return jsonify({"ads": db.attach_entity_stats(db.get_group_ads(key, user_id=_scope_user_id()))})


@bp.route("/api/video")
def api_video():
    """Stream a Facebook video through our own origin (signed fbcdn URLs expire
    / are blocked cross-origin by some browsers). Only Meta CDN hosts are
    allowed, so this cannot be used as an open proxy.

    No login is required (media elements / plain fetch may omit cookies); the
    fbcdn allow-list plus a same-origin Referer check keep it from being abused
    by other sites."""
    from urllib.parse import urlsplit
    ref = request.headers.get("Referer") or request.headers.get("Origin") or ""
    if ref:
        ref_host = (urlsplit(ref).hostname or "").lower()
        own_host = (request.host or "").split(":")[0].lower()
        if ref_host and ref_host not in (own_host, "ads.botdz.com", "127.0.0.1", "localhost"):
            return jsonify({"error": "forbidden"}), 403

    raw = (request.args.get("u") or "").strip()
    if not raw:
        return jsonify({"error": "u required"}), 400
    parts = urlsplit(raw)
    host = (parts.hostname or "").lower()
    allowed = host.endswith(".fbcdn.net") or host == "fbcdn.net" or host.endswith(".facebook.com")
    if parts.scheme not in ("http", "https") or not allowed:
        return jsonify({"error": "host not allowed"}), 400

    from curl_cffi import requests as creq
    up_headers = {}
    rng = request.headers.get("Range")
    if rng:
        up_headers["Range"] = rng
    try:
        upstream = creq.get(raw, impersonate="chrome", timeout=90, stream=True,
                            headers=up_headers, allow_redirects=True)
    except Exception as exc:  # noqa: BLE001
        logger.warning("video proxy failed: %s", exc)
        return jsonify({"error": "upstream unavailable"}), 502
    if upstream.status_code not in (200, 206):
        upstream.close()
        return jsonify({"error": f"upstream {upstream.status_code}"}), 502

    def gen():
        try:
            for chunk in upstream.iter_content(65536):
                if chunk:
                    yield chunk
        finally:
            upstream.close()

    resp = Response(gen(), status=upstream.status_code, direct_passthrough=True)
    resp.headers["Content-Type"] = upstream.headers.get("Content-Type", "video/mp4")
    for h in ("Content-Length", "Accept-Ranges", "Content-Range"):
        if upstream.headers.get(h):
            resp.headers[h] = upstream.headers[h]
    resp.headers["Cache-Control"] = "private, max-age=1800"
    return resp


@bp.route("/api/diagnose")
@admin_required
def api_diagnose():
    """One-shot diagnosis: egress IP + a live Meta Ad Library probe.

    Explains why a scrape returns nothing (blocked IP/proxy vs empty results)."""
    from curl_cffi import requests as creq
    from meta_ads_collector.proxy_pool import parse_proxy, _redact_proxy

    saved = _proxy_setting()
    env_spec = (os.environ.get("META_ADS_PROXY") or "").strip()
    spec = saved or env_spec
    use_proxy = request.args.get("proxy", "1") != "0"
    if not use_proxy:
        spec = ""
    out = {"proxy_configured": bool(spec),
           "source": ("settings" if saved else ("env" if env_spec else "none")) if use_proxy else "disabled",
           "using_proxy": bool(spec),
           "proxy": "", "egress_ip": None, "direct_ip": None,
           "proxy_changes_ip": None, "meta_count": None,
           "meta_error": "", "verdict": ""}

    proxies = None
    proxy_arg = None
    if spec:
        parts = [p.strip() for p in spec.split(",") if p.strip()]
        try:
            purl = parse_proxy(parts[0])
            out["proxy"] = _redact_proxy(purl)
        except Exception as exc:  # noqa: BLE001
            out["verdict"] = f"ط·آµط¸ظ¹ط·ط›ط·آ© ط·آ§ط¸â€‍ط·آ¨ط·آ±ط¸ث†ط¸ئ’ط·آ³ط¸ظ¹ ط·ط›ط¸ظ¹ط·آ± ط·آµط·آ­ط¸ظ¹ط·آ­ط·آ©: {exc}"
            return jsonify(out)
        proxies = {"http": purl, "https": purl}
        try:
            r = creq.get("https://api.ipify.org?format=json", proxies=proxies,
                         impersonate="chrome", timeout=25)
            out["egress_ip"] = (r.json() or {}).get("ip")
        except Exception as exc:  # noqa: BLE001
            out["verdict"] = f"ط·ع¾ط·آ¹ط·آ°ط¸â€کط·آ± ط·آ§ط¸â€‍ط·آ§ط·ع¾ط·آµط·آ§ط¸â€‍ ط·آ¨ط·آ§ط¸â€‍ط·آ¨ط·آ±ط¸ث†ط¸ئ’ط·آ³ط¸ظ¹: {str(exc)[:180]}"
            return jsonify(out)
        proxy_arg = parts if len(parts) > 1 else parts[0]

    # Direct server IP (never through the proxy), to compare.
    try:
        r0 = creq.get("https://api.ipify.org?format=json", impersonate="chrome", timeout=15)
        out["direct_ip"] = (r0.json() or {}).get("ip")
    except Exception:  # noqa: BLE001
        out["direct_ip"] = None
    out["proxy_changes_ip"] = bool(
        proxies and out["direct_ip"] and out["egress_ip"]
        and out["direct_ip"] != out["egress_ip"])

    # Live Meta Ad Library probe (same path the scraper uses). Any query can be
    # tested: /api/diagnose?proxy=0&q=&country=DZ&min=2026-10-01&max=2026-10-10
    probe_q = request.args.get("q", "livraison")
    probe_country = (request.args.get("country") or "DZ").upper()
    probe_min = request.args.get("min") or None
    probe_max = request.args.get("max") or None
    out["probe"] = {"q": probe_q, "country": probe_country,
                    "min": probe_min, "max": probe_max}
    try:
        from meta_ads_collector import MetaAdsCollector
        c = MetaAdsCollector(proxy=proxy_arg)
        try:
            out["meta_count"] = c.count(
                query=probe_q, country=probe_country, status="ACTIVE",
                sort_by="SORT_BY_RELEVANCY_MONTHLY_GROUPED",
                start_date=probe_min, end_date=probe_max)
        finally:
            try:
                c.close()
            except Exception:
                pass
    except Exception as exc:  # noqa: BLE001
        out["meta_error"] = str(exc)[:200]

    if out.get("meta_count") and int(out["meta_count"]) > 0:
        where = "ط·آ¹ط·آ¨ط·آ± ط·آ§ط¸â€‍ط·آ¨ط·آ±ط¸ث†ط¸ئ’ط·آ³ط¸ظ¹" if proxies else "ط¸â€¦ط¸â€  IP ط·آ§ط¸â€‍ط·آ³ط¸ظ¹ط·آ±ط¸ظ¾ط·آ± ط·آ§ط¸â€‍ط¸â€¦ط·آ¨ط·آ§ط·آ´ط·آ±"
        out["verdict"] = f"أ¢إ“â€¦ ط¸ظ¹ط·آ¹ط¸â€¦ط¸â€‍ {where}: Meta ط·آ£ط·آ¹ط·آ§ط·آ¯ط·ع¾ ط¸â€ ط·ع¾ط·آ§ط·آ¦ط·آ¬ ط¸ظ¾ط·آ¹ط¸â€‍ط¸ظ¹ط·آ©."
    elif proxies and out.get("direct_ip") and out.get("egress_ip") == out.get("direct_ip"):
        out["verdict"] = ("أ¢â€؛â€‌ ط·آ§ط¸â€‍ط·آ¨ط·آ±ط¸ث†ط¸ئ’ط·آ³ط¸ظ¹ ط¸â€‍ط·آ§ ط¸ظ¹ط·ط›ط¸ظ¹ط¸â€کط·آ± ط·آ¹ط¸â€ ط¸ث†ط·آ§ط¸â€  ط·آ§ط¸â€‍ط·آ®ط·آ±ط¸ث†ط·آ¬ (egress = IP ط·آ§ط¸â€‍ط·آ³ط¸ظ¹ط·آ±ط¸ظ¾ط·آ± ط·آ§ط¸â€‍ط¸â€¦ط·آ¨ط·آ§ط·آ´ط·آ±). "
                          "ط¸â€،ط·آ°ط·آ§ ط·آ¨ط·آ±ط¸ث†ط¸ئ’ط·آ³ط¸ظ¹ ط¸â€¦ط·آ­ط¸â€‍ط¸ظ¹/ط·آ´ط¸ظ¾ط·آ§ط¸ظ¾ ط¸ث†ط¸â€‍ط·آ§ ط¸ظ¹ط¸ظ¾ط¸ظ¹ط·آ¯ ط·آ¶ط·آ¯ ط·آ­ط·آ¬ط·آ¨ Meta.")
    elif not proxies:
        out["verdict"] = ("أ¢â€؛â€‌ Meta ط·آ£ط·آ¹ط·آ§ط·آ¯ط·ع¾ 0 ط¸â€ ط·ع¾ط¸ظ¹ط·آ¬ط·آ© ط¸â€¦ط¸â€  IP ط·آ§ط¸â€‍ط·آ³ط¸ظ¹ط·آ±ط¸ظ¾ط·آ± ط·آ§ط¸â€‍ط¸â€¦ط·آ¨ط·آ§ط·آ´ط·آ± أ¢â‚¬â€‌ ط·آ§ط¸â€‍ط¸â‚¬IP ط¸â€¦ط·آ­ط·آ¬ط¸ث†ط·آ¨ ط¸â€¦ط·آ¤ط¸â€ڑط·ع¾ط¸â€¹ط·آ§ ط¸â€¦ط¸â€  "
                          "ط¸â€¦ط¸ئ’ط·ع¾ط·آ¨ط·آ© ط·آ§ط¸â€‍ط·آ¥ط·آ¹ط¸â€‍ط·آ§ط¸â€ ط·آ§ط·ع¾ (ط·آ³ط¸ظ¹ط¸عˆط·آ±ط¸ظ¾ط·آ¹ ط·آ§ط¸â€‍ط·آ­ط·آ¬ط·آ¨ ط·ع¾ط¸â€‍ط¸â€ڑط·آ§ط·آ¦ط¸ظ¹ط¸â€¹ط·آ§ ط·آ¨ط·آ¹ط·آ¯ ط¸ظ¾ط·ع¾ط·آ±ط·آ©).")
    else:
        out["verdict"] = ("أ¢â€؛â€‌ Meta ط·آ£ط·آ¹ط·آ§ط·آ¯ط·ع¾ 0 ط¸â€ ط·ع¾ط¸ظ¹ط·آ¬ط·آ© ط·آ¹ط·آ¨ط·آ± ط·آ§ط¸â€‍ط·آ¨ط·آ±ط¸ث†ط¸ئ’ط·آ³ط¸ظ¹ أ¢â‚¬â€‌ ط·آ¹ط¸â€ ط¸ث†ط·آ§ط¸â€  ط·آ®ط·آ±ط¸ث†ط·آ¬ ط·آ§ط¸â€‍ط·آ¨ط·آ±ط¸ث†ط¸ئ’ط·آ³ط¸ظ¹ ط¸â€¦ط·آ­ط·آ¬ط¸ث†ط·آ¨ ط¸â€¦ط·آ¤ط¸â€ڑط·ع¾ط¸â€¹ط·آ§. "
                          "ط·آ¬ط·آ±ط¸â€کط·آ¨ ط·آ¨ط·آ±ط¸ث†ط¸ئ’ط·آ³ط¸ظ¹ residential/mobile ط·آ¢ط·آ®ط·آ± ط·آ«ط¸â€¦ ط·آ£ط·آ¹ط·آ¯ ط·آ§ط¸â€‍ط·ع¾ط·آ´ط·آ®ط¸ظ¹ط·آµ.")
    return jsonify(out)


@bp.route("/api/queries")
@admin_required
def api_queries():
    return jsonify({"queries": db.get_queries()})


@bp.route("/api/ad-keywords")
@login_required
def api_ad_keywords():
    """Per-keyword counters for the keyword filter bar."""
    return jsonify({"keywords": db.get_keywords_overview(user_id=_scope_user_id())})


@bp.route("/api/niches")
@login_required
def api_niches():
    """Niche taxonomy with per-niche ad counts (for the filter bar)."""
    return jsonify({"niches": db.niche_counts(user_id=_scope_user_id())})


@bp.route("/api/ads/<ad_id>/niche", methods=["POST"])
@admin_required
def api_set_ad_niche(ad_id):
    """Manual niche override for one ad (method='manual' beats the rules)."""
    cfg = request.get_json(force=True, silent=True) or {}
    slug = (cfg.get("slug") or "").strip()
    if not slug:
        return jsonify({"error": "slug required"}), 400
    ok = db.set_manual_niche(ad_id, slug, add=bool(cfg.get("add", True)))
    return jsonify({"ok": ok})


@bp.route("/api/stores")
@login_required
def api_stores():
    return jsonify({"stores": db.get_stores(search=request.args.get("search", ""), user_id=_scope_user_id())})


@bp.route("/api/stats")
@login_required
def api_stats():
    return jsonify(db.get_stats(user_id=_scope_user_id()))


@bp.route("/api/reset", methods=["POST"])
@admin_required
def api_reset():
    db.clear_ads()
    return jsonify({"ok": True})


@bp.route("/api/export/<fmt>")
@login_required
def api_export(fmt: str):
    ads = db.get_ads(limit=100000, stores_only=False, user_id=_scope_user_id())
    if fmt == "csv":
        buf = io.StringIO()
        cols = ["ad_id", "page_name", "page_id", "start_date", "store_domain",
                "platform", "cta_text", "link_url", "keywords", "niches",
                "video_url", "body"]
        w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for ad in ads:
            row = dict(ad)
            row["keywords"] = " | ".join(row.get("keywords") or [])
            row["niches"] = " | ".join(x.get("slug", "") for x in (row.get("niches") or []))
            w.writerow(row)
        data = io.BytesIO(buf.getvalue().encode("utf-8-sig"))
        return send_file(data, mimetype="text/csv", as_attachment=True,
                         download_name=f"meta_ads_{int(datetime.now().timestamp())}.csv")
    if fmt == "json":
        data = io.BytesIO(json.dumps(ads, ensure_ascii=False, indent=2).encode("utf-8"))
        return send_file(data, mimetype="application/json", as_attachment=True,
                         download_name=f"meta_ads_{int(datetime.now().timestamp())}.json")
    return jsonify({"error": "unsupported format"}), 400


@bp.route("/api/maintenance/reclassify-stores", methods=["POST"])
@admin_required
def api_reclassify_stores():
    rows = db._query("SELECT ad_id, link_url, store_domain FROM meta_ads")
    changed = cleared = 0
    for r in rows:
        dom, plat = extract_store_domain(r.get("link_url") or "")
        new = dom or ""
        old = r.get("store_domain") or ""
        if new != old:
            db._execute(
                f"UPDATE meta_ads SET store_domain={db._ph()}, platform={db._ph()} WHERE ad_id={db._ph()}",
                (new, (plat or ""), r["ad_id"]))
            changed += 1
            if old and not new:
                cleared += 1
    logger.info("reclassify-stores: checked=%s changed=%s cleared=%s", len(rows), changed, cleared)
    return jsonify({"ok": True, "checked": len(rows), "changed": changed, "cleared_non_store": cleared})





