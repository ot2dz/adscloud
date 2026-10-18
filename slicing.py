# -*- coding: utf-8 -*-
"""
Deep Search Sweeper: partition queries across calendar month slices
to completely bypass Facebook's ~3,500-4,000 infinite scroll ceiling.
"""
from __future__ import annotations

from datetime import date, timedelta
from urllib.parse import urlsplit, parse_qsl, urlencode, urlunsplit

SORT_MODES = ["total_impressions", "relevancy_monthly_grouped"]


def _strip_param(pairs: list[tuple[str, str]], name: str) -> list[tuple[str, str]]:
    return [(k, v) for (k, v) in pairs if k != name]


def with_params(base_url: str, **params) -> str:
    """Return base_url with the given query params set (overwriting existing)."""
    parts = urlsplit(base_url)
    pairs = parse_qsl(parts.query, keep_blank_values=True)
    for key, value in params.items():
        if value is None:
            pairs = _strip_param(pairs, key)
        else:
            pairs = _strip_param(pairs, key)
            pairs.append((key, value))
    return urlunsplit(parts._replace(query=urlencode(pairs)))


def resolve_date_range(preset: str = "all", min_date: str = "", max_date: str = "") -> tuple[str, str, str]:
    """Resolve preset (this_month, last_month, last_7d, etc.) into ISO date strings and label."""
    today = date.today()
    p = (preset or "all").lower().strip()

    if p == "this_month":
        first_of_month = date(today.year, today.month, 1)
        label = f"هذا الشهر (من {first_of_month.isoformat()} فصاعداً)"
        return first_of_month.isoformat(), "", label

    elif p == "last_month":
        first_this = date(today.year, today.month, 1)
        last_prev = first_this - timedelta(days=1)
        first_prev = date(last_prev.year, last_prev.month, 1)
        label = f"الشهر الماضي ({first_prev.isoformat()} إلى {last_prev.isoformat()})"
        return first_prev.isoformat(), last_prev.isoformat(), label

    elif p == "last_7d":
        start_7d = today - timedelta(days=7)
        label = f"آخر 7 أيام (من {start_7d.isoformat()} فصاعداً)"
        return start_7d.isoformat(), "", label

    elif p == "last_30d":
        start_30d = today - timedelta(days=30)
        label = f"آخر 30 يوماً (من {start_30d.isoformat()} فصاعداً)"
        return start_30d.isoformat(), "", label

    elif p == "custom" and (min_date or max_date):
        label = f"فترة مخصصة (من {min_date or 'البداية'}{' إلى ' + max_date if max_date else ' فصاعداً'})"
        return min_date.strip(), max_date.strip(), label

    return "", "", "كل الفترات (بدون تقييد تاريخ)"


def clean_month_windows(months_back: int = 12) -> list[dict]:
    """Generate exact non-overlapping monthly partitions backwards from today."""
    windows = []
    today = date.today()
    y, m = today.year, today.month

    for _ in range(months_back):
        first = date(y, m, 1)
        if m == 12:
            nxt = date(y + 1, 1, 1)
        else:
            nxt = date(y, m + 1, 1)
        last = nxt - timedelta(days=1)
        max_date = today if (y == today.year and m == today.month) else last
        label = f"{y}-{m:02d}"
        windows.append({
            "min_date": first.isoformat(),
            "max_date": max_date.isoformat(),
            "label": label,
        })
        m -= 1
        if m == 0:
            m = 12
            y -= 1
    return windows


def build_deep_passes(query: str = "", url: str = "", country: str = "DZ",
                      exact: bool = False, sort_mode: str = "total_impressions",
                      months_back: int = 12) -> list[dict]:
    """Build sliced pass URLs for deep comprehensive scraping."""
    from pages import keyword_url

    base = url.strip() if url.strip() else keyword_url(query, country=country, exact_phrase=exact, sort_mode=sort_mode)
    windows = clean_month_windows(months_back)
    passes = []

    for idx, win in enumerate(windows, 1):
        max_exclusive = (date.fromisoformat(win["max_date"][:10]) + timedelta(days=1)).isoformat()
        params = {
            "start_date[min]": win["min_date"],
            "start_date[max]": max_exclusive,
            "sort_data[mode]": sort_mode,
            "sort_data[direction]": "desc",
        }
        pass_url = with_params(base, **params)
        passes.append({
            "pass_index": idx,
            "total_passes": len(windows),
            "label": win["label"],
            "min_date": win["min_date"],
            "max_date": win["max_date"],
            "url": pass_url,
        })
    return passes


def build_pass_urls(base_url: str, sorts: list[str] | None = None,
                    windows: list[tuple[str, str] | None] | None = None) -> list[dict]:
    """Compatibility helper for existing callers."""
    return build_deep_passes(url=base_url)