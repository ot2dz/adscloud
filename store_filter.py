# -*- coding: utf-8 -*-
"""Store-domain extraction and platform detection.

Only genuine e-commerce / website links count as a "store". Messaging apps,
social networks, app stores, link-in-bio pages and URL shorteners are rejected.
"""
from __future__ import annotations

from typing import Optional
from urllib.parse import urlparse

# Hosts (and their subdomains) that are NEVER a store.
_NON_STORE_HOSTS = {
    # messaging / chat
    "wa.me", "whatsapp.com", "whatsapp.net", "whatsapp.link", "wa.link",
    "chat.whatsapp.com", "m.me", "messenger.com", "fb.com", "facebook.com",
    "fb.me", "fb.watch", "facebook.net", "t.me", "telegram.me", "telegram.org",
    "telegram.dog", "discord.gg", "discord.com", "viber.com", "line.me",
    "snapchat.com", "wechat.com",
    # social
    "instagram.com", "threads.net", "tiktok.com", "twitter.com", "x.com",
    "linkedin.com", "pinterest.com", "reddit.com", "tumblr.com", "vk.com",
    # video / audio
    "youtube.com", "youtu.be", "vimeo.com", "spotify.com", "soundcloud.com",
    # app stores
    "play.google.com", "apps.apple.com", "itunes.apple.com",
    "appgallery.huawei.com", "aptoide.com", "apkpure.com", "apkcombo.com",
    "apkmirror.com", "uptodown.com",
    # link-in-bio / URL shorteners
    "linktr.ee", "linktree", "beacons.ai", "bio.link", "taplink.cc",
    "milkshake.app", "bit.ly", "tinyurl.com", "goo.gl", "t.co", "shorturl.at",
    "cutt.ly", "rb.gy", "is.gd", "rebrand.ly", "s.id", "ow.ly", "buff.ly",
    "adf.ly",
    # google / misc hosting
    "google.com", "googleusercontent.com", "gstatic.com", "g.co",
    "maps.app.goo.gl", "g.page", "forms.gle", "docs.google.com",
    "sites.google.com", "blogspot.com", "wordpress.com", "medium.com",
    "wixsite.com", "gmail.com",
}

# Non-web schemes that are never a store.
_BAD_SCHEMES = {"mailto", "tel", "sms", "javascript", "intent", "market", "fb", "whatsapp"}


def host_of(url: str) -> str:
    """Return the bare lowercase host of a URL (no scheme, no www, no port)."""
    if not url or not isinstance(url, str):
        return ""
    u = url.strip()
    if not u:
        return ""
    # Reject non-web schemes early.
    if ":" in u.split("//", 1)[0] and "://" not in u:
        scheme = u.split(":", 1)[0].lower()
        if scheme in _BAD_SCHEMES:
            return ""
    if "://" not in u:
        u = "https://" + u
    try:
        parsed = urlparse(u)
    except Exception:
        return ""
    if parsed.scheme.lower() not in ("http", "https"):
        return ""
    netloc = parsed.netloc.lower().split("@")[-1].split(":")[0]
    if netloc.startswith("www."):
        netloc = netloc[4:]
    return netloc


def is_store_domain(domain: str) -> bool:
    """True when *domain* is a real store/site host (not messaging/social/app-store)."""
    d = (domain or "").strip().lower().lstrip(".")
    if "." not in d or len(d) < 4:
        return False
    for bad in _NON_STORE_HOSTS:
        if d == bad or d.endswith("." + bad):
            return False
    return True


def extract_store_domain(url: str) -> tuple[Optional[str], Optional[str]]:
    """Return (clean_domain, platform) for a store URL, else (None, None)."""
    host = host_of(url)
    if not host or not is_store_domain(host):
        return None, None

    platform = "Custom Domain"
    low = (url or "").lower()
    if "youcan.shop" in host or "youcan.store" in host:
        platform = "YouCan Shop"
    elif "myshopify.com" in host or "shopify" in low:
        platform = "Shopify"
    elif "woocommerce" in low:
        platform = "WooCommerce"
    elif host.endswith(".dz"):
        platform = "Algerian (.dz)"
    return host, platform


# Query parameters that only track the click, never identify the product.
_TRACKING_PARAMS = ("utm_", "fbclid", "gclid", "ttclid", "ref", "source", "campaign",
                    "ad_id", "adset", "fbc", "fbp", "mc_", "_ga")


def product_key(page_id: str, link_url: str, body: str = "", store_domain: str = "") -> str:
    """Stable key for "the same product from the same advertiser".

    Ads of one advertiser pointing to the same product page (host + path,
    tracking parameters dropped) share a key. When the link is only the store
    homepage, the ad text tells products apart instead.
    """
    import hashlib
    from urllib.parse import parse_qsl, unquote, urlsplit

    owner = str(page_id or "").strip() or (store_domain or "").strip().lower() or "?"
    path = ""
    host = ""
    query = ""
    try:
        parts = urlsplit((link_url or "").strip())
        host = parts.netloc.lower().split("@")[-1].split(":")[0]
        if host.startswith("www."):
            host = host[4:]
        path = unquote(parts.path or "").rstrip("/").lower()
        kept = sorted((k.lower(), v) for k, v in parse_qsl(parts.query, keep_blank_values=False)
                      if not k.lower().startswith(_TRACKING_PARAMS))
        query = "&".join(f"{k}={v}" for k, v in kept)
    except Exception:
        pass
    if host and (path or query):
        return f"{owner}|u:{host}{path}{('?' + query) if query else ''}"
    # Homepage link (or no link): fall back to the ad text.
    text = " ".join((body or "").split())[:200].lower()
    digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]
    return f"{owner}|t:{host}:{digest}"
