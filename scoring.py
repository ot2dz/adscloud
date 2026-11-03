# -*- coding: utf-8 -*-
"""Product "Hunter Score" (0-100) computed only from data we already store.

It mirrors the playbook thresholds of the e-commerce toolkit, but uses what the
Meta ad bank actually knows:

  +25  the product was found by 2+ different scrape keywords  ("two lists")
  +20  healthy number of active ads (5-20)
  +15  enough ads behind the product (>= 10)
  +15  recent activity (collected/launched within 7 days)
  +15  proven age (7-90 days; older evergreen scores slightly lower)
  +10  creative variety (>= 3 distinct texts)

Fields that only PPSPY has (monthly sales, revenue, spend, price) are NOT
guessed here. The label is a language-neutral code; the UI translates it.
"""
from __future__ import annotations

from datetime import datetime, timezone

# A product is "winning" when its persisted Hunter Score reaches this bar.
# Stored in ``meta_ads.hunter_score`` so SQL presets can filter on it.
WINNING_THRESHOLD = 70


def _parse_date(value) -> "datetime | None":
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    # common shapes: 2026-10-30, 2026-10-30T12:00:00, 2026-10-30 12:00:00+00:00
    head = text[:19].replace(" ", "T")
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(head[:len(fmt) + 2] if "%S" in fmt else head, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except Exception:  # noqa: BLE001
        return None


def _days_since(value, now: datetime) -> "int | None":
    dt = _parse_date(value)
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return max(0, int((now - dt).total_seconds() // 86400))


def _label(score: int) -> str:
    if score >= WINNING_THRESHOLD:
        return "strong"
    if score >= 40:
        return "medium"
    return "weak"


def hunter(ad: dict, now: "datetime | None" = None) -> dict:
    """Return {score, label, confirmed, parts} for one ad / product group."""
    now = now or datetime.now(timezone.utc)
    g = ad.get("group") or {}

    active = int(g.get("active") or 0)
    if not g and ad.get("is_active") is not None:
        active = 1 if ad.get("is_active") else 0
    total = int(g.get("count") or 0) or 1
    texts = int(g.get("texts") or 0) or 1

    keywords = [k for k in (g.get("keywords") or ad.get("keywords") or []) if k]
    kw_count = len({str(k).strip().lower() for k in keywords})

    first = g.get("first_launch") or ad.get("start_date") or ""
    last = g.get("last_collected") or ad.get("collected_at") or ""
    age = _days_since(first, now)
    recency = _days_since(last, now)

    score = 0
    parts = []

    if kw_count >= 2:
        score += 25
        parts.append({"k": "lists", "pts": 25, "v": kw_count})
    elif kw_count == 1:
        score += 10
        parts.append({"k": "lists", "pts": 10, "v": 1})

    if 5 <= active <= 20:
        score += 20
        parts.append({"k": "active", "pts": 20, "v": active})
    elif 3 <= active < 5:
        score += 12
        parts.append({"k": "active", "pts": 12, "v": active})
    elif active > 20:
        score += 14
        parts.append({"k": "active", "pts": 14, "v": active})
    elif active >= 1:
        score += 5
        parts.append({"k": "active", "pts": 5, "v": active})

    if total >= 10:
        score += 15
        parts.append({"k": "depth", "pts": 15, "v": total})
    elif total >= 5:
        score += 8
        parts.append({"k": "depth", "pts": 8, "v": total})

    if recency is not None:
        if recency <= 7:
            score += 15
            parts.append({"k": "recent", "pts": 15, "v": recency})
        elif recency <= 14:
            score += 10
            parts.append({"k": "recent", "pts": 10, "v": recency})
        elif recency <= 30:
            score += 5
            parts.append({"k": "recent", "pts": 5, "v": recency})

    if age is not None:
        if 7 <= age <= 90:
            score += 15
            parts.append({"k": "age", "pts": 15, "v": age})
        elif age > 90:
            score += 12
            parts.append({"k": "age", "pts": 12, "v": age})
        else:
            score += 4
            parts.append({"k": "age", "pts": 4, "v": age})

    if texts >= 3:
        score += 10
        parts.append({"k": "creative", "pts": 10, "v": texts})
    elif texts == 2:
        score += 5
        parts.append({"k": "creative", "pts": 5, "v": texts})

    return {
        "score": int(score),
        "label": _label(int(score)),
        "confirmed": kw_count >= 2,
        "parts": parts,
    }


def attach(items: "list[dict]", now: "datetime | None" = None) -> "list[dict]":
    """Attach ``item['hunter']`` to every ad / group row.

    Rows persisted with a ``hunter_score`` keep that value (it is what the
    winning preset filters on, so the badge and the preset always agree)."""
    now = now or datetime.now(timezone.utc)
    for item in items:
        try:
            result = hunter(item, now)
            stored = item.get("hunter_score")
            if stored is not None:
                score = max(0, min(100, int(stored)))
                result["score"] = score
                result["label"] = _label(score)
            item["hunter"] = result
        except Exception:  # noqa: BLE001
            item["hunter"] = {"score": 0, "label": "weak", "confirmed": False, "parts": []}
    return items
