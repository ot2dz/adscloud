# -*- coding: utf-8 -*-
"""
Scan planning: split a search into date slices.

Note: the old belief that Meta stops paginating after ~3,500-4,000 ads is
NOT true today. Tested 2026-11-08: a plain keyword search for "التوصيل" (DZ)
returned 29,929 of Meta's estimated 30,412 ads (98%) and Meta itself
reported the end of the results. Slicing is therefore not needed to get past
a page limit; check a job's coverage line (Meta estimate vs. scanned, and
"completed") before assuming one exists. The old monthly "Deep Sweep" was
removed for that reason; ``build_day_passes`` plans the whole-market scan.
"""
from __future__ import annotations

import re
from datetime import date, timedelta

# Whole-market jobs carry the window inside their label:
#   "🔍 كل إعلانات السوق (متاجر فقط) 2026-10-01 ← 2026-10-20"
# The same window already lives on the job and on each ad, so tags keep only
# the clean label (one tag instead of one per scanned window).
_DATE_SUFFIX_RE = re.compile(r"\s*\d{4}-\d{2}-\d{2}\s*[^\w\s]*\s*\d{4}-\d{2}-\d{2}\s*$", re.UNICODE)


def clean_scan_label(label: str) -> str:
    """Drop a trailing ``YYYY-MM-DD ← YYYY-MM-DD`` window from a scan label."""
    return _DATE_SUFFIX_RE.sub("", str(label or "")).strip()


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


def build_day_passes(start_date: str, end_date: str, languages: list[str]) -> list[dict]:
    """Whole-market scrape plan: one pass per launch day x language, newest
    day first (today's ads are the most valuable and change the most).

    Meta's start-date window on a single day returns the ads launched that
    day, so each pass is small and quick to resume or retry. The
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