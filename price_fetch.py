# -*- coding: utf-8 -*-
"""Read a product's selling price from its landing page (competitor price).

Tries, in order: Shopify's ``<product>.js`` JSON, JSON-LD offers, price meta
tags / itemprop, then the most common DZD price written in the page.

Plain HTTP requests with an honest User-Agent. Sites protected by Cloudflare or
another bot check are NOT bypassed: they are reported as ``blocked`` and the
user types the price by hand.
"""
from __future__ import annotations

import json
import re
import threading
import time
import urllib.error
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from typing import Optional
from urllib.parse import urlsplit

_CACHE: dict[str, tuple[float, dict]] = {}
_CACHE_TTL = 6 * 3600
_LOCK = threading.Lock()

_LD_RE = re.compile(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', re.S | re.I)
_META_RE = re.compile(
    r'<meta[^>]+(?:property|name|itemprop)=["\'](?:product:price:amount|og:price:amount|price)["\'][^>]*'
    r'content=["\']([\d.,\s]+)["\']', re.I)
_META_RE2 = re.compile(
    r'<meta[^>]+content=["\']([\d.,\s]+)["\'][^>]*(?:property|name|itemprop)=["\']'
    r'(?:product:price:amount|og:price:amount|price)["\']', re.I)
_ITEMPROP_RE = re.compile(r'itemprop=["\']price["\'][^>]*content=["\']([\d.,\s]+)["\']', re.I)
_TAG_RE = re.compile(r"<script.*?</script>|<style.*?</style>|<[^>]+>", re.S | re.I)
_DZD_RE = re.compile(
    r"(?<![\d.,])(\d{1,3}(?:[ .,  ]\d{3})+|\d{3,6})(?:[.,]\d{2})?\s*"
    r"(?:دج|د\.ج|دينار|da\b|dzd\b|dz\b|dinars?)", re.I)
# Currency written BEFORE the number: "DZ 5,200", "DA 5200", "دج 5200" (Lightfunnels...)
_DZD_PREFIX_RE = re.compile(
    r"(?:\bDZD?\b|\bDA\b|دج|د\.ج)\s*(\d{1,3}(?:[ .,\u00a0\u202f]\d{3})+|\d{3,6})(?:[.,]\d{2})?(?![\d])",
    re.I)
# Store platforms embed their product JSON in the page: "price": 5200
_JSON_PRICE_RE = re.compile(r'"(?:price|sale_price|final_price)"\s*:\s*"?(\d{3,6}(?:\.\d{1,2})?)"?', re.I)
_CHALLENGE = ("just a moment", "cf-chl", "challenge-platform", "attention required",
              "enable javascript and cookies", "cf_chl_opt")
LO, HI = 200, 500_000  # plausible product price range in DZD


def _num(v) -> Optional[int]:
    if v is None:
        return None
    s = re.sub(r"[^\d.,]", "", str(v))
    if not s:
        return None
    # "2,900.00" / "2.900,00" / "2900" / "2 900"
    if re.search(r"[.,]\d{2}$", s):
        s = s[:-3]
    s = re.sub(r"[.,]", "", s)
    try:
        x = int(s)
    except ValueError:
        return None
    return x if LO <= x <= HI else None


def _from_ld(html: str) -> Optional[int]:
    def walk(o):
        if isinstance(o, dict):
            for k in ("price", "lowPrice"):
                if k in o:
                    v = _num(o[k])
                    if v:
                        return v
            for v in o.values():
                r = walk(v)
                if r:
                    return r
        elif isinstance(o, list):
            for v in o:
                r = walk(v)
                if r:
                    return r
        return None

    for block in _LD_RE.findall(html):
        try:
            r = walk(json.loads(block.strip()))
        except ValueError:
            continue
        if r:
            return r
    return None


def _from_text(html: str) -> Optional[int]:
    """Most common DZD price in the visible text (currency after or before)."""
    text = _TAG_RE.sub(" ", html).replace("&nbsp;", " ")
    found = [v for rx in (_DZD_RE, _DZD_PREFIX_RE)
             for v in (_num(m.group(1)) for m in rx.finditer(text)) if v]
    if not found:
        return None
    return Counter(found).most_common(1)[0][0]


def _from_embedded_json(html: str) -> Optional[int]:
    """Most common non-zero "price" in the platform's embedded product JSON."""
    found = [v for v in (_num(m.group(1).split(".")[0]) for m in _JSON_PRICE_RE.finditer(html)) if v]
    if not found:
        return None
    return Counter(found).most_common(1)[0][0]


def extract_price(html: str) -> tuple[Optional[int], str]:
    """(price, how) from a product page's HTML."""
    if not html:
        return None, ""
    v = _from_ld(html)
    if v:
        return v, "json-ld"
    for rx in (_META_RE, _META_RE2, _ITEMPROP_RE):
        m = rx.search(html)
        if m and _num(m.group(1)):
            return _num(m.group(1)), "meta"
    v = _from_text(html)
    if v:
        return v, "text"
    v = _from_embedded_json(html)
    return (v, "page-json") if v else (None, "")


USER_AGENT = "AdsCloud-PriceCheck/1.0 (+product price lookup)"
MAX_BYTES = 3_000_000


class _Resp:
    def __init__(self, status: int, text: str):
        self.status_code, self.text = status, text

    def json(self):
        return json.loads(self.text)


def _get(url: str, timeout: float) -> _Resp:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                               "Accept": "text/html,application/json;q=0.9"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read(MAX_BYTES)
            charset = r.headers.get_content_charset() or "utf-8"
            return _Resp(r.status, raw.decode(charset, "replace"))
    except urllib.error.HTTPError as e:
        try:
            body = e.read(200_000).decode("utf-8", "replace")
        except Exception:
            body = ""
        return _Resp(e.code, body)


def fetch_price(url: str, timeout: float = 12) -> dict:
    """{url, price, how, status: ok|not_found|blocked|error, detail}."""
    with _LOCK:
        hit = _CACHE.get(url)
    if hit and time.time() - hit[0] < _CACHE_TTL:
        return hit[1]
    out = {"url": url, "price": None, "how": "", "status": "error", "detail": ""}
    try:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            out["detail"] = "bad url"
            return out
        if "/products/" in parts.path:  # Shopify: cheap JSON first
            try:
                r = _get(f"{parts.scheme}://{parts.netloc}{parts.path.rstrip('/')}.js", timeout)
                if r.status_code == 200 and r.text.lstrip().startswith("{"):
                    j = r.json()
                    cents = j.get("price") or (j.get("variants") or [{}])[0].get("price")
                    v = _num(int(cents) // 100) if cents else None
                    if v:
                        out.update(price=v, how="shopify", status="ok")
            except Exception:
                pass
        if out["status"] != "ok":
            r = _get(url, timeout)
            body = r.text or ""
            low = body[:20000].lower()
            if r.status_code in (403, 429, 503) or any(c in low for c in _CHALLENGE):
                out.update(status="blocked", detail=f"HTTP {r.status_code}")
            elif r.status_code >= 400:
                out.update(status="error", detail=f"HTTP {r.status_code}")
            else:
                v, how = extract_price(body)
                out.update(price=v, how=how, status="ok" if v else "not_found")
    except Exception as e:  # network errors, timeouts
        out.update(status="error", detail=str(e)[:160])
    if out["status"] in ("ok", "not_found"):
        with _LOCK:
            _CACHE[url] = (time.time(), out)
    return out


def fetch_prices(urls: list[str], workers: int = 6) -> list[dict]:
    urls = list(dict.fromkeys(u for u in urls if u))[:30]
    if not urls:
        return []
    with ThreadPoolExecutor(max_workers=min(workers, len(urls))) as ex:
        return list(ex.map(fetch_price, urls))
