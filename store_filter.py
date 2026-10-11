# -*- coding: utf-8 -*-
"""Store-domain extraction and platform detection."""
from __future__ import annotations

import re
from typing import Optional
from urllib.parse import urlparse

# URLs that are NOT e-commerce stores and must be skipped.
_IGNORED = [
    r"wa\.me", r"whatsapp\.com", r"api\.whatsapp", r"m\.me", r"messenger\.com",
    r"facebook\.com", r"fb\.com", r"fb\.watch", r"instagram\.com",
    r"t\.me", r"telegram\.me", r"tiktok\.com", r"youtube\.com", r"youtu\.be",
    r"bit\.ly", r"linktr\.ee", r"linktree",
]


def extract_store_domain(url: str) -> tuple[Optional[str], Optional[str]]:
    """Return (clean_domain, platform) for a store URL, else (None, None)."""
    if not url or not isinstance(url, str):
        return None, None

    url_clean = url.strip()
    for pat in _IGNORED:
        if re.search(pat, url_clean, re.IGNORECASE):
            return None, None

    try:
        if not url_clean.startswith(("http://", "https://")):
            url_clean = "https://" + url_clean
        netloc = urlparse(url_clean).netloc.lower()
        if netloc.startswith("www."):
            netloc = netloc[4:]

        if "." not in netloc or len(netloc) < 4:
            return None, None

        platform = "Custom Domain"
        if "youcan.shop" in netloc or "youcan.store" in netloc:
            platform = "YouCan Shop"
        elif "myshopify.com" in netloc or "shopify" in url_clean:
            platform = "Shopify"
        elif "woocommerce" in url_clean:
            platform = "WooCommerce"
        elif netloc.endswith(".dz"):
            platform = "Algerian (.dz)"

        return netloc, platform
    except Exception:
        return None, None
