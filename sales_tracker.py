# -*- coding: utf-8 -*-
"""LightFunnels sales tracker — read a store's order counter without an admin
account.

Every LightFunnels storefront numbers its orders sequentially per account
(``#10185``, ``#10186`` …). Placing one Cash-on-Delivery order on any product
page of the store therefore tells the next order number, and the difference
between two probes minus our own probe orders is the number of real orders the
store received in between.

Pipeline (everything is public, exactly what the storefront JS does):

1. ``GET <product url>`` → the page embeds ``var data = {...}`` (account,
   funnel, steps, cart, payment gateways, the GraphQL ``code``), ``var env``
   (captcha URL + checkout token) and ``var tree`` (the buy button id + iref).
2. ``POST /api`` ``CREATE_CHECKOUT`` → a checkout slug.
3. Solve the PBKDF2 proof-of-work captcha (``api.lightfunnels.com/storefront``).
4. ``POST /api`` ``EXECUTE_CHECKOUT`` → the COD order is created.
5. ``POST /api`` ``order(slug)`` → ``order.name`` = the sequential number.

Limits that shape the design (verified on a live store):

- LightFunnels refuses a second COD order from the same IP/device for a while
  (GraphQL key ``errors.cod_order_too_soon``) even with another phone or
  session, so each probe needs a fresh egress IP.
- Every probe creates one real order at the competitor: probe rarely and treat
  the count as ``next_number - previous_number - 1``.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import random
import threading
import time
from datetime import datetime, timezone
from typing import Callable, Optional
from urllib.parse import quote, urlparse

logger = logging.getLogger("sales_tracker")

IMPERSONATE = "chrome"
DEFAULT_TIMEOUT = 30.0
CAPTCHA_URL = "https://api.lightfunnels.com/storefront/captcha"
CAPTCHA_MAX_ATTEMPTS = int(os.environ.get("SALES_CAPTCHA_MAX_ATTEMPTS", "200000") or 200000)
CAPTCHA_THREADS = max(1, int(os.environ.get("SALES_CAPTCHA_THREADS", "4") or 4))
DEFAULT_COUNTRY = "DZ"

_CREATE_Q = """
mutation CREATE_CHECKOUT($payload: CheckoutPayloadInput!, $token: String!){
  checkout: checkoutCreate(payload: $payload, token: $token){ slug }
}
"""

_EXECUTE_Q = """
mutation EXECUTE_CHECKOUT(
  $form: CheckoutPayloadInput!, $checkoutSlug: String!, $button: String,
  $destination: String!, $is_express: Boolean! = false, $source: String,
  $captcha_token: String, $honeypot: String, $otp: OtpInput
) {
  checkout: checkoutExecute(
    form: $form, checkoutSlug: $checkoutSlug, button: $button,
    destination: $destination, is_express: $is_express, source: $source,
    captcha_token: $captcha_token, honeypot: $honeypot, otp: $otp
  ){ redirect slug lf_payment payment_method payment_method_uid destination }
}
"""

_ORDER_Q = "query fetchOrder($slug: String){ order(slug: $slug) }"

_SHIPPING_Q = """
query shipping_regions($countryCode: String!){
  shipping_regions(countryCode: $countryCode){
    id region cost rate_or_extra
    custom_shipping_rates { id label price }
    cities { city id cost custom_shipping_rates { id label price } }
  }
}
"""


class SalesTrackError(Exception):
    """A probe failed. ``code`` is a stable key for the UI/logs."""
    code = "error"


class NotLightfunnels(SalesTrackError):
    code = "not_lightfunnels"


class CooldownError(SalesTrackError):
    """Same IP already placed a COD order too recently."""
    code = "cooldown"


class CaptchaError(SalesTrackError):
    code = "captcha"


class OtpRequired(SalesTrackError):
    """The storefront asks for a WhatsApp/SMS code: tracing by order is impossible."""
    code = "otp"


class ProbeError(SalesTrackError):
    code = "probe"


# ── HTML parsing ───────────────────────────────────────────────────────
def _json_after(html: str, marker: str) -> Optional[dict]:
    """Parse ``marker ... {json};`` (bracket matching inside strings aware)."""
    start = html.find(marker)
    if start < 0:
        return None
    start = html.find("{", start + len(marker))
    if start < 0:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(html)):
        ch = html[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        else:
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(html[start:i + 1])
                    except ValueError:
                        return None
    return None


def _walk(node, out: list) -> None:
    if isinstance(node, dict):
        out.append(node)
        for v in node.values():
            _walk(v, out)
    elif isinstance(node, list):
        for v in node:
            _walk(v, out)


def _find_buy_button(tree: dict) -> dict:
    """The node that runs the checkout: {id, iref}. Its ``p.action`` is
    ``process-payment``."""
    nodes: list = []
    _walk(tree, nodes)
    for n in nodes:
        p = n.get("p") if isinstance(n.get("p"), dict) else {}
        if p.get("action") == "process-payment":
            return {"id": str(n.get("id") or ""), "iref": str(n.get("iref") or "")}
    for n in nodes:  # fallback: first button node
        if str(n.get("t") or "").lower() == "button" and n.get("id"):
            return {"id": str(n.get("id") or ""), "iref": str(n.get("iref") or "")}
    return {"id": "", "iref": ""}


def normalize_url(value: str) -> str:
    v = (value or "").strip()
    if not v:
        raise NotLightfunnels("أدخل رابط متجر")
    if not v.startswith(("http://", "https://")):
        v = "https://" + v
    return v


def parse_store(html: str, url: str) -> dict:
    """Detect a LightFunnels storefront and extract what a probe needs."""
    data = _json_after(html, "var data =")
    if not data or not isinstance(data.get("account"), dict) or not data["account"].get("id"):
        raise NotLightfunnels("هذا المتجر ليس LightFunnels (لا يوجد account.id في الصفحة)")
    env = _json_after(html, "var env =") or {}
    tree = _json_after(html, "var tree =") or {}
    lf = _json_after(html, "var Lightfunnels =") or {}
    lf_funnel = lf.get("funnel") if isinstance(lf.get("funnel"), dict) else {}

    steps = [s for s in (data.get("steps") or []) if isinstance(s, dict)]
    product_step = next((s for s in steps if s.get("type") == "product_page"), steps[0] if steps else None)
    thank_step = next((s for s in steps if s.get("type") == "thank_you_page"), steps[-1] if steps else None)
    if not product_step or not thank_step:
        raise NotLightfunnels("لم أجد خطوات الفانل (product_page / thank_you_page)")

    checkout = (data.get("state") or {}).get("checkout") or {}
    body = checkout.get("body") or []
    variant_id = None
    if body and isinstance(body[0], dict) and body[0].get("variants"):
        variant_id = body[0]["variants"][0].get("id")

    gateways = [g for g in (data.get("payment_methods") or []) if isinstance(g, dict) and g.get("uid")]
    gateway = next((g for g in gateways if str(g.get("value")) == "cod"), gateways[0] if gateways else None)
    if not gateway or not body:
        raise NotLightfunnels("لا يوجد نموذج طلب (منتجات/بوابة دفع) في هذه الصفحة")

    host = urlparse(url).netloc
    path_parts = [p for p in urlparse(url).path.split("/") if p]
    funnel_slug = path_parts[0] if path_parts else str(product_step.get("slug") or "")
    button = _find_buy_button(tree)
    currency = str(checkout.get("currency") or data.get("currency") or "")
    country = ((data.get("visitor") or {}).get("country") or DEFAULT_COUNTRY)
    otp_required = bool((data.get("channel") or {}).get("enable_otp"))

    return {
        "host": host,
        "origin": f"{urlparse(url).scheme}://{host}",
        "url": url,
        "account_id": str(data["account"]["id"]),
        "account_uid": str(data["account"].get("uid") or ""),
        "account_name": str(data["account"].get("name") or "").strip(),
        "funnel_id": str(data.get("funnel", {}).get("id") or lf_funnel.get("id") or ""),
        "funnel_uid": str(data.get("funnel", {}).get("uid") or lf_funnel.get("uid") or ""),
        "product_step_id": product_step.get("id"),
        "funnel_slug": funnel_slug,
        "thank_slug": str(thank_step.get("slug") or ""),
        "steps": steps,
        "variant_id": variant_id,
        "body": body,
        "gateway_uid": str(gateway.get("uid")),
        "gateway_value": str(gateway.get("value") or ""),
        "currency": currency,
        "country": country,
        "lang": str((data.get("visitor") or {}).get("lang") or "ar"),
        "code": str(data.get("events") or ""),
        "token": str((env or {}).get("token") or ""),
        "captcha_url": str((env or {}).get("CAPTCHA_URL") or CAPTCHA_URL),
        "button": button,
        "otp_required": otp_required,
        "product_name": "",
        "product_image": "",
    }


# ── captcha (PBKDF2 proof of work) ─────────────────────────────────────
def _captcha_key(password: bytes, salt: bytes, cost: int, dklen: int) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password, salt, cost, dklen=dklen)


def solve_captcha(challenge: dict, *, max_attempts: int = CAPTCHA_MAX_ATTEMPTS,
                  threads: int = CAPTCHA_THREADS, deadline: float = 45.0,
                  version: str = "") -> dict:
    """Find the counter whose derived key starts with ``keyPrefix``.

    The server picks a small counter (a few thousand at most), so searching
    upwards from 0 finds it in a second or two; several threads share the
    range because ``pbkdf2_hmac`` releases the GIL.
    """
    params = (challenge or {}).get("parameters") or {}
    prefix = str(params.get("keyPrefix") or "").lower()
    if not prefix:
        raise CaptchaError("challenge بدون keyPrefix")
    try:
        nonce = bytes.fromhex(str(params.get("nonce") or ""))
        salt = bytes.fromhex(str(params.get("salt") or ""))
        cost = int(params.get("cost") or 0)
        dklen = int(params.get("keyLength") or 32)
    except (TypeError, ValueError) as exc:
        raise CaptchaError(f"challenge غير صالح: {exc}") from exc
    if not nonce or not salt or not cost:
        raise CaptchaError("challenge غير صالح (nonce/salt/cost)")

    found: dict = {}
    stop = threading.Event()
    deadline_at = time.monotonic() + max(1.0, deadline)

    def worker(start: int, step: int) -> None:
        c = start
        while c < max_attempts and not stop.is_set():
            if (c - start) % 512 == 0 and time.monotonic() > deadline_at:
                return
            dk = _captcha_key(nonce + c.to_bytes(4, "big"), salt, cost, dklen)
            if dk.hex().startswith(prefix):
                if not stop.is_set():
                    found["counter"] = c
                    found["derivedKey"] = dk.hex()
                    stop.set()
                return
            c += step

    started = time.perf_counter()
    workers = [threading.Thread(target=worker, args=(i, threads), daemon=True)
               for i in range(min(threads, max_attempts))]
    for w in workers:
        w.start()
    for w in workers:
        w.join(timeout=max(0.5, deadline_at - time.monotonic()))
    stop.set()
    if "counter" not in found:
        raise CaptchaError("تعذّر حل كابتشا الدفع (انتهت المحاولات)")

    solution = {
        "counter": found["counter"],
        "derivedKey": found["derivedKey"],
        "time": round((time.perf_counter() - started) * 1000, 1),
    }
    token = {"challenge": challenge, "solution": solution}
    if version:
        token["version"] = version
    return {
        "token": base64.b64encode(json.dumps(token, separators=(",", ":")).encode()).decode(),
        "counter": solution["counter"],
        "ms": solution["time"],
    }


# ── GraphQL helpers ────────────────────────────────────────────────────
def _gql(session, host: str, code: str, query: str, variables: dict,
         referer: str = "", timeout: float = DEFAULT_TIMEOUT) -> dict:
    headers = {"Content-Type": "application/json", "Accept": "*/*",
               "Origin": f"https://{host}"}
    if referer:
        headers["Referer"] = referer
    r = session.post(f"https://{host}/api", json={"query": query, "variables": variables, "code": code},
                     headers=headers, timeout=timeout)
    if getattr(r, "status_code", 0) != 200:
        raise ProbeError(f"HTTP {getattr(r, 'status_code', '?')} من {host}")
    try:
        payload = r.json()
    except Exception as exc:  # noqa: BLE001
        raise ProbeError(f"رد غير صالح من {host}: {exc}") from exc
    errors = payload.get("errors") or []
    if errors:
        keys = " ".join(str(e.get("key") or "") for e in errors)
        msg = "؛ ".join(str(e.get("message") or "") for e in errors) or "خطأ من المتجر"
        if "cod_order_too_soon" in keys:
            raise CooldownError(msg)
        if "otp" in keys:
            raise OtpRequired(msg)
        if "captcha" in keys.lower() or "captcha" in msg.lower() or "verif" in msg.lower():
            raise CaptchaError(msg)
        raise ProbeError(msg)
    return payload.get("data") or {}


def _proxy_url(proxy: Optional[str]) -> Optional[str]:
    """Accept ``host:port:user:pass`` or a URL, return a curl-compatible URL."""
    raw = (proxy or "").strip()
    if not raw:
        return None
    try:
        from meta_ads_collector.proxy_pool import parse_proxy
        return parse_proxy(raw)
    except Exception:  # noqa: BLE001 — fall back to the raw value
        return raw if "://" in raw else f"http://{raw}"


def _new_session(proxy: Optional[str] = None):
    from curl_cffi import requests as creq
    kwargs = {"impersonate": IMPERSONATE}
    purl = _proxy_url(proxy)
    if purl:
        kwargs["proxies"] = {"http": purl, "https": purl}
    return creq.Session(**kwargs)


# ── shipping / profile ─────────────────────────────────────────────────
def _pick_shipping(session, cfg: dict, want_state: str = "", timeout: float = DEFAULT_TIMEOUT) -> dict:
    try:
        data = _gql(session, cfg["host"], cfg["code"], _SHIPPING_Q,
                    {"countryCode": cfg["country"]}, referer=cfg["url"], timeout=timeout)
    except SalesTrackError:
        return {"state": want_state or "Alger", "city": "", "rate_id": None}
    regions = [r for r in (data.get("shipping_regions") or []) if isinstance(r, dict)]
    if not regions:
        return {"state": want_state or "Alger", "city": "", "rate_id": None}
    region = None
    if want_state:
        region = next((r for r in regions if str(r.get("region") or "").lower() == want_state.lower()), None)
    region = region or next((r for r in regions if str(r.get("region") or "").lower() == "alger"), None) or regions[0]
    cities = [c for c in (region.get("cities") or []) if isinstance(c, dict)]
    rate_id = None
    rates = [x for x in (region.get("custom_shipping_rates") or []) if isinstance(x, dict)]
    if rates:
        rate_id = rates[0].get("id")
    return {
        "state": str(region.get("region") or ""),
        "city": str((cities[0] or {}).get("city") if cities else ""),
        "rate_id": rate_id,
    }


_NAMES = ("Ahmed", "Sara", "Yacine", "Nadia", "Karim", "Imene", "Sofiane", "Lina")


def fake_profile(country: str = DEFAULT_COUNTRY) -> dict:
    """An obviously test order: random plausible phone, generic name."""
    if country == "DZ":
        phone = "0" + random.choice("57") + "".join(random.choice("0123456789") for _ in range(8))
    else:
        phone = "0" + "".join(random.choice("0123456789") for _ in range(9))
    return {
        "first_name": random.choice(_NAMES),
        "phone": phone,
        "email": f"test{random.randint(1000, 9999)}@example.com",
    }


def _checkout_form(cfg: dict, profile: dict, shipping: dict) -> dict:
    country = cfg["country"]
    return {
        "body": cfg["body"],
        "order_bumps_body": [],
        "files": [],
        "shipping_address": {
            "last_name": "", "first_name": profile["first_name"], "line1": "", "line2": "",
            "country": country, "city": shipping.get("city") or "", "zip": "",
            "state": shipping.get("state") or "", "area": "", "email": "", "phone": profile["phone"],
        },
        "billing_address": {
            "last_name": "", "first_name": profile["first_name"], "line1": "", "line2": "",
            "country": country, "city": "", "zip": "", "state": "", "area": "",
            "email": "", "phone": profile["phone"],
        },
        "first_name": profile["first_name"], "last_name": "", "email": profile.get("email", ""),
        "billing_address_as_shipping_address": False, "accepts_marketing": False,
        "accept_order_bump": False, "accept_terms_agreement": False, "discount_code": "",
        "phone": profile["phone"], "payment_method_uid": cfg["gateway_uid"],
        "payment_method_data": {}, "custom": {}, "shipping_rate_id": shipping.get("rate_id"),
        "phone_country_code": country,
    }


def _client_version(session, cfg: dict, timeout: float = DEFAULT_TIMEOUT) -> str:
    """The storefront caches the ETag of a locale asset in sessionStorage
    (``lf_rt``) and echoes it in the captcha token as ``version``."""
    lang = (str(cfg.get("lang") or "ar")[:2] or "ar")
    url = f"{cfg['origin']}/assets/locale/{lang}.{os.urandom(4).hex()}.json"
    try:
        r1 = session.get(url, timeout=timeout)
        try:
            r1.json()
        except Exception:  # noqa: BLE001
            pass
        etag = r1.headers.get("ETag") or r1.headers.get("etag") or ""
        r2 = session.get(url, headers={"If-None-Match": etag}, timeout=timeout)
        try:
            r2.json()
        except Exception:  # noqa: BLE001
            pass
        val = r2.headers.get("ETag") or r2.headers.get("etag") or etag
        return val.replace("W/", "").replace('"', "").strip()
    except Exception:  # noqa: BLE001
        return ""


def _seed_cookies(session, cfg: dict) -> None:
    """The storefront JS keeps the cart in two cookies. Presenting them makes the
    order look like a real checkout (without them LightFunnels asks for an OTP)."""
    try:
        fid = str(cfg.get("funnel_id") or "")
        if not fid:
            return
        prefix = f"lf_{fid}_"
        cart = json.dumps({"body": cfg["body"]}, separators=(",", ":"))
        bumps = json.dumps({"body": []}, separators=(",", ":"))
        for name, val in ((prefix + "cart", cart), (prefix + "orderBumps", bumps)):
            session.cookies.set(name, quote(val, safe=""), domain=cfg["host"], path="/")
    except Exception:  # noqa: BLE001 — cookies are best-effort
        logger.debug("could not seed storefront cookies", exc_info=True)


# ── public API ─────────────────────────────────────────────────────────
def detect(store_url: str, *, proxy: Optional[str] = None, session=None,
           timeout: float = DEFAULT_TIMEOUT) -> dict:
    """Read-only: is this a LightFunnels store and what is its config?"""
    url = normalize_url(store_url)
    sess = session or _new_session(proxy)
    r = sess.get(url, timeout=timeout)
    if getattr(r, "status_code", 0) != 200:
        raise ProbeError(f"تعذّر تحميل الصفحة (HTTP {getattr(r, 'status_code', '?')})")
    cfg = parse_store(r.text, url)
    cfg.pop("code", None)  # never send the tracking blob to the browser
    cfg.pop("token", None)
    cfg.pop("body", None)
    return cfg


def probe(store_url: str, *, proxy: Optional[str] = None, session=None,
          profile: Optional[dict] = None, state: str = "", timeout: float = DEFAULT_TIMEOUT,
          ip: str = "", solver: Callable[[dict], dict] = solve_captcha) -> dict:
    """Place one COD order and return ``order.name`` (the live counter)."""
    url = normalize_url(store_url)
    host = urlparse(url).netloc
    sess = session or _new_session(proxy)

    r = sess.get(url, timeout=timeout)
    if getattr(r, "status_code", 0) != 200:
        raise ProbeError(f"تعذّر تحميل الصفحة (HTTP {getattr(r, 'status_code', '?')})")
    cfg = parse_store(r.text, url)

    profile = profile or fake_profile(cfg["country"])
    _seed_cookies(sess, cfg)
    shipping = _pick_shipping(sess, cfg, want_state=state, timeout=timeout)
    form = _checkout_form(cfg, profile, shipping)

    created = _gql(sess, host, cfg["code"], _CREATE_Q,
                   {"payload": form, "token": cfg["token"]}, referer=url, timeout=timeout)
    slug = str(((created.get("checkout") or {}).get("slug")) or "")
    if not slug:
        raise ProbeError("لم يرجع المتجر slug للطلب")

    # captcha (proof of work served by LightFunnels)
    challenge = sess.get(cfg["captcha_url"], timeout=timeout).json()
    version = _client_version(sess, cfg, timeout=timeout)
    try:
        solved = solver(challenge, version=version)
    except TypeError:
        solved = solver(challenge)
    if isinstance(solved, dict):
        captcha_token = solved.get("token")
    else:  # allow a solver returning the base64 token directly
        captcha_token = solved
    if not captcha_token:
        raise CaptchaError("لم أستطع توليد كابتشا")

    iref = quote(cfg["button"].get("iref") or "", safe="")
    destination = (f"{cfg['origin']}/{cfg['funnel_slug']}/{cfg['thank_slug']}"
                   f"?checkout={slug}" + (f"&iref={iref}" if iref else ""))

    executed = _gql(sess, host, cfg["code"], _EXECUTE_Q, {
        "form": form, "checkoutSlug": slug, "button": cfg["button"].get("id") or None,
        "destination": destination, "is_express": False, "source": url,
        "captcha_token": captcha_token, "honeypot": "",
    }, referer=url, timeout=timeout)
    checkout = executed.get("checkout") or {}
    if not checkout:
        raise ProbeError("لم يتم إنشاء الطلب")

    order = _gql(sess, host, cfg["code"], _ORDER_Q, {"slug": slug}, referer=url, timeout=timeout).get("order") or {}
    if not order:
        raise ProbeError("تم الطلب لكن لم أستطع قراءة رقمه")

    name = str(order.get("name") or "")
    digits = "".join(ch for ch in name if ch.isdigit())
    if not digits:
        raise ProbeError(f"رقم طلب غير متوقع: {name!r}")

    return {
        "store_host": host,
        "product_url": url,
        "account_id": cfg["account_id"],
        "account_name": cfg["account_name"],
        "funnel_id": cfg["funnel_id"],
        "funnel_slug": cfg["funnel_slug"],
        "product_id": None,
        "variant_id": cfg["variant_id"],
        "order_name": name,
        "order_number": int(digits),
        "order_id": str(order.get("id") or ""),
        "currency": str(order.get("currency") or cfg["currency"]),
        "total": order.get("total"),
        "created_at": str(order.get("created_at") or ""),
        "probed_at": datetime.now(timezone.utc).isoformat(),
        "phone": profile["phone"],
        "state": shipping.get("state") or "",
        "city": shipping.get("city") or "",
        "ip": ip or "",
        "slug": slug,
    }


def orders_between(prev_number: Optional[int], number: int, probes_between: int = 1) -> int:
    """Real customer orders between two probes.

    Each probe creates one order itself, so the raw jump is one bigger.
    """
    if prev_number is None:
        return 0
    return max(0, int(number) - int(prev_number) - max(1, int(probes_between)))
