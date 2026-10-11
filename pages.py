# -*- coding: utf-8 -*-
"""Helpers for tracking targets: Pages, Store Domains, Keywords, and Custom URLs."""
from __future__ import annotations

import re
from typing import Optional
from urllib.parse import quote

ADS_LIBRARY_BASE = "https://www.facebook.com/ads/library/"


def extract_page_id(text: str) -> Optional[str]:
    """Extract a Meta page id from a URL, an Ad Library page URL, or a raw id."""
    s = (text or "").strip()
    if not s:
        return None
    if re.fullmatch(r"\d{6,}", s):
        return s
    m = re.search(r"(?:view_all_page_id|page_id)=(\d{6,})", s, re.IGNORECASE)
    if m:
        return m.group(1)
    m = re.search(r"-(\d{8,})/?(\?|$)", s)
    if m:
        return m.group(1)
    m = re.search(r"facebook\.com/(?:ads/library/\?[^ ]*)?(\d{9,})", s, re.IGNORECASE)
    if m:
        return m.group(1)
    m = re.search(r"(\d{9,})", s)
    return m.group(1) if m else None


def clean_domain(text: str) -> str:
    """Extract pure domain without protocol or paths (e.g. https://bxrrt.com/shop -> bxrrt.com)."""
    s = (text or "").strip().lower()
    s = re.sub(r"^https?://", "", s)
    s = re.sub(r"^www\.", "", s)
    s = s.split("/")[0].split("?")[0].split("#")[0].strip()
    return s


def page_url(page_id: str, country: str = "DZ", active_status: str = "active") -> str:
    return (
        f"{ADS_LIBRARY_BASE}?active_status={active_status}&ad_type=all"
        f"&country={country}&is_targeted_country=false&media_type=all"
        f"&view_all_page_id={page_id}&search_type=page"
    )


def domain_url(domain: str, country: str = "DZ", active_status: str = "active") -> str:
    dom = clean_domain(domain)
    return (
        f"{ADS_LIBRARY_BASE}?active_status={active_status}&ad_type=all"
        f"&country={country}&is_targeted_country=false&media_type=all"
        f"&q={quote(dom)}&search_type=keyword_unordered"
        f"&sort_data[direction]=desc&sort_data[mode]=total_impressions"
    )


def keyword_url(keyword: str, country: str = "DZ", exact_phrase: bool = False,
                sort_mode: str = "total_impressions") -> str:
    q = f'"{keyword}"' if exact_phrase else keyword
    search_type = "keyword_exact_phrase" if exact_phrase else "keyword_unordered"
    return (
        f"{ADS_LIBRARY_BASE}?active_status=active&ad_type=all"
        f"&country={country}&is_targeted_country=false&media_type=all"
        f"&q={quote(q)}&search_type={search_type}"
        f"&sort_data[direction]=desc&sort_data[mode]={sort_mode}"
    )


def resolve_target_url(target_type: str, target_value: str, country: str = "DZ") -> str:
    """Resolve any target into its Meta Ad Library URL."""
    val = (target_value or "").strip()
    typ = (target_type or "keyword").strip().lower()

    if typ == "custom_url" or val.startswith("http"):
        if "facebook.com/ads/library" in val:
            return val

    if typ == "page":
        pid = extract_page_id(val) or val
        return page_url(pid, country=country)

    if typ == "domain":
        dom = clean_domain(val)
        return domain_url(dom, country=country)

    # default keyword
    return keyword_url(val, country=country)