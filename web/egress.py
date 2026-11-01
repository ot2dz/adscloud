"""Egress IP badge: shows the public IP the scraper currently goes out from.

- with META_ADS_PROXY configured: the IP as seen through the first proxy
- without a proxy: the server's own public IP
- additionally reports the local rotator agent state (current home IP,
  last rotation) when IP_ROTATE_URL is configured.

The answer is cached for a few seconds so the header can poll cheaply.
"""
from __future__ import annotations

import logging
import os
import time

from flask import Blueprint, jsonify

from scrape_manager import _proxy_setting
from web.common import login_required

import ip_rotator

logger = logging.getLogger(__name__)

bp = Blueprint("egress", __name__)

_CACHE = {"ts": 0.0, "data": None}
_CACHE_TTL = 20.0
_IP_URL = "https://api.ipify.org?format=json"


def _first_proxy(spec: str) -> str:
    parts = [p.strip() for p in (spec or "").split(",") if p.strip()]
    return parts[0] if parts else ""


def _probe(proxy_url: str | None) -> str:
    from curl_cffi import requests as creq

    if proxy_url:
        r = creq.get(_IP_URL, proxies={"http": proxy_url, "https": proxy_url},
                     impersonate="chrome", timeout=12)
    else:
        r = creq.get(_IP_URL, impersonate="chrome", timeout=10)
    if r.status_code != 200:
        raise RuntimeError("HTTP %s" % r.status_code)
    try:
        return (r.json() or {}).get("ip", "")
    except Exception:
        return (r.text or "").strip()


@bp.route("/api/egress-ip", methods=["GET"])
@login_required
def api_egress_ip():
    now = time.time()
    if _CACHE["data"] and (now - _CACHE["ts"]) < _CACHE_TTL:
        return jsonify(_CACHE["data"])

    spec = (_proxy_setting() or os.environ.get("META_ADS_PROXY") or "").strip()
    raw = _first_proxy(spec)

    data = {"ok": True, "ip": None, "source": "direct", "proxy": None, "error": None}
    try:
        if raw:
            from meta_ads_collector.proxy_pool import parse_proxy, _redact_proxy
            purl = parse_proxy(raw)
            data["source"] = "proxy"
            data["proxy"] = _redact_proxy(purl)
            data["ip"] = _probe(purl)
        else:
            data["ip"] = _probe(None)
    except Exception as exc:  # noqa: BLE001
        data["ok"] = False
        data["error"] = str(exc)[:160]

    try:
        data["rotator"] = ip_rotator.status(timeout=6)
    except Exception:  # noqa: BLE001
        data["rotator"] = None

    _CACHE["ts"], _CACHE["data"] = now, data
    return jsonify(data)
