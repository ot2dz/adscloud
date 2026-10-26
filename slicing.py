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


def _algeria_today() -> date:
    try:
        from db import algeria_today
        return algeria_today()
    except Exception:  # pragma: no cover - db not importable
        return date.today()


def resolve_date_range(preset: str = "all", min_date: str = "", max_date: str = "") -> tuple[str, str, str]:
    """Launch-date window for a scrape, in Algeria time -> (min, max, label).

    Presets: all, today, yesterday, last_7d, this_month, last_month, last_30d,
    day (one date = ``min_date``) and custom (``min_date``..``max_date``).
    Raises ValueError (Arabic message) on an invalid or future range.
    """
    today = _algeria_today()
    p = (preset or "all").lower().strip()

    def iso(v: str, what: str) -> date:
        try:
            return date.fromisoformat((v or "").strip()[:10])
        except ValueError:
            raise ValueError(f"تاريخ {what} غير صالح")

    if p == "today":
        return today.isoformat(), today.isoformat(), f"اليوم ({today.isoformat()})"
    if p == "yesterday":
        y = today - timedelta(days=1)
        return y.isoformat(), y.isoformat(), f"أمس ({y.isoformat()})"
    if p == "last_7d":
        start = today - timedelta(days=6)
        return start.isoformat(), today.isoformat(), f"آخر 7 أيام ({start.isoformat()} ← {today.isoformat()})"
    if p == "last_30d":
        start = today - timedelta(days=29)
        return start.isoformat(), today.isoformat(), f"آخر 30 يوماً ({start.isoformat()} ← {today.isoformat()})"
    if p == "this_month":
        first = today.replace(day=1)
        return first.isoformat(), today.isoformat(), f"هذا الشهر ({first.isoformat()} ← {today.isoformat()})"
    if p == "last_month":
        last_prev = today.replace(day=1) - timedelta(days=1)
        first_prev = last_prev.replace(day=1)
        return first_prev.isoformat(), last_prev.isoformat(), f"الشهر الماضي ({first_prev.isoformat()} ← {last_prev.isoformat()})"
    if p == "day":
        d = iso(min_date, "اليوم")
        if d > today:
            raise ValueError("لا يمكن اختيار يوم في المستقبل")
        return d.isoformat(), d.isoformat(), f"يوم {d.isoformat()}"
    if p == "custom":
        d1, d2 = iso(min_date, "البداية"), iso(max_date or min_date, "النهاية")
        if d1 > d2:
            d1, d2 = d2, d1
        if d1 > today:
            raise ValueError("لا يمكن اختيار فترة في المستقبل")
        d2 = min(d2, today)
        return d1.isoformat(), d2.isoformat(), f"من {d1.isoformat()} إلى {d2.isoformat()}"
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


def build_day_passes(start_date: str, end_date: str, languages: list[str]) -> list[dict]:
    """Whole-market scrape plan: one pass per launch day x language, newest
    day first (today's ads are the most valuable and change the most).

    Meta's start-date window on a single day returns the ads launched that
    day, so each pass is small and well under any pagination ceiling. The
    plan only depends on the job's stored dates, so it is identical when an
    interrupted job resumes days later.
    """
    first = date.fromisoformat(start_date[:10])
    last = date.fromisoformat(end_date[:10])
    langs = [l for l in (languages or []) if l] or [""]
    passes = []
    day = last
    while day >= first:
        for lang in langs:
            passes.append({"day": day.isoformat(), "lang": lang,
                           "label": f"{day.isoformat()}{' · ' + LANG_AR.get(lang, lang) if lang else ''}"})
        day -= timedelta(days=1)
    return passes


LANG_AR = {"ar": "عربي", "fr": "فرنسي", "en": "إنجليزي"}


def build_pass_urls(base_url: str, sorts: list[str] | None = None,
                    windows: list[tuple[str, str] | None] | None = None) -> list[dict]:
    """Compatibility helper for existing callers."""
    return build_deep_passes(url=base_url)