# -*- coding: utf-8 -*-
"""Niche taxonomy + rule-based classifier.

Pure module (no DB access): the taxonomy lives here, ``db.py`` seeds it and
persists the per-ad scores.
"""
from __future__ import annotations

import math
import re
from typing import Iterable, Optional

# ── Taxonomy ───────────────────────────────────────────────────────────
# weights: 3 = strong keyword, 2 = medium, 1 = weak/ambiguous
NICHES: list[dict] = [
    {
        "slug": "kitchen",
        "name_ar": "الطبخ وأدوات المطبخ",
        "name_fr": "Cuisine",
        "keywords": [
            ("طنجرة", 3), ("قدر ضغط", 3), ("مقلاة", 3), ("أواني", 3), ("صحون", 3),
            ("خلاط", 3), ("محضر طعام", 3), ("cuisine", 3), ("cuisson", 3),
            ("cookware", 3), ("casserole", 3), ("poele", 3), ("poêle", 3),
            ("مطبخ", 1), ("طبخ", 2), ("وصفات", 2), ("ملاعق", 2), ("سكين", 2),
            ("لوح تقطيع", 2), ("ميزان مطبخ", 2), ("robot cuisine", 3),
        ],
        "negatives": ["herbalife", "مكمل غذائي", "رجيم", "شقة", "عقار"],
    },
    {
        "slug": "beauty",
        "name_ar": "تجميل وعناية بالبشرة",
        "name_fr": "Beauté",
        "keywords": [
            ("كريم", 3), ("سيروم", 3), ("ماسك", 3), ("واقي شمس", 3),
            ("كريم أساس", 3), ("أحمر شفاه", 3), ("مكياج", 3), ("skincare", 3),
            ("serum", 3), ("sérum", 3), ("creme", 3), ("crème", 3), ("maquillage", 3),
            ("بشرة", 2), ("ترطيب", 2), ("رموش", 2), ("حواجب", 2), ("جمال", 1),
        ],
        "negatives": [],
    },
    {
        "slug": "supplements",
        "name_ar": "صحة ومكملات غذائية",
        "name_fr": "Compléments alimentaires",
        "keywords": [
            ("مكمل غذائي", 3), ("حبوب", 2), ("كبسولات", 3), ("فيتامين", 3),
            ("بروتين", 3), ("كولاجين", 3), ("herbalife", 3), ("omega", 3),
            ("collagen", 3), ("complement alimentaire", 3),
            ("زيادة الوزن", 2), ("انقاص الوزن", 2), ("مناعة", 2), ("رجيم", 2),
            ("صحة", 1), ("طبيعي", 1),
        ],
        "negatives": [],
    },
    {
        "slug": "fashion",
        "name_ar": "أزياء وملابس",
        "name_fr": "Mode",
        "keywords": [
            ("فستان", 3), ("قميص", 3), ("بنطلون", 3), ("جاكيت", 3), ("حجاب", 3),
            ("عباية", 3), ("ملابس", 3), ("robe", 3), ("pantalon", 3),
            ("chemise", 3), ("ensemble", 3), ("أزياء", 2), ("موضة", 2),
            ("مقاسات", 2), ("قطن", 1), ("ستايل", 1),
        ],
        "negatives": [],
    },
    {
        "slug": "kids",
        "name_ar": "أطفال ومستلزمات الأمومة",
        "name_fr": "Bébé & enfants",
        "keywords": [
            ("حفاضات", 3), ("رضاعة", 3), ("عربة أطفال", 3), ("سرير طفل", 3),
            ("العاب اطفال", 3), ("couches bebe", 3), ("biberon", 3),
            ("poussette", 3), ("طفل", 2), ("اطفال", 2), ("مولود", 2),
            ("امومة", 2), ("لعبة", 2), ("تعليمي", 1),
        ],
        "negatives": [],
    },
    {
        "slug": "electronics",
        "name_ar": "إلكترونيات",
        "name_fr": "Électronique",
        "keywords": [
            ("سماعات", 3), ("ساعة ذكية", 3), ("شاحن", 3), ("باور بانك", 3),
            ("كاميرا", 3), ("تابلت", 3), ("ecouteurs", 3), ("montre connectee", 3),
            ("chargeur", 3), ("بلوتوث", 2), ("لاسلكي", 2), ("smart", 2),
            ("usb", 2), ("led", 1), ("تقنية", 1),
        ],
        "negatives": [],
    },
    {
        "slug": "home-tools",
        "name_ar": "أدوات منزلية وورشة",
        "name_fr": "Outillage",
        "keywords": [
            ("مفك", 3), ("دريل", 3), ("عدة", 2), ("منشار", 3), ("مطرقة", 3),
            ("tournevis", 3), ("perceuse", 3), ("outillage", 3), ("ادوات", 2),
            ("ورشة", 2), ("تصليح", 2), ("ستانلس", 1),
        ],
        "negatives": [],
    },
    {
        "slug": "cleaning",
        "name_ar": "تنظيف وعناية بالمنزل",
        "name_fr": "Nettoyage",
        "keywords": [
            ("منظف", 3), ("ممسحة", 3), ("مكنسة", 3), ("بخار", 2),
            ("مسحوق غسيل", 3), ("nettoyant", 3), ("aspirateur", 3), ("vapeur", 2),
            ("تنظيف", 2), ("تعقيم", 2), ("ازالة الدهون", 2), ("نظافة", 1),
        ],
        "negatives": [],
    },
    {
        "slug": "fitness",
        "name_ar": "رياضة ولياقة",
        "name_fr": "Fitness",
        "keywords": [
            ("دمبل", 3), ("حبل مقاومة", 3), ("سجادة يوغا", 3), ("جهاز رياضي", 3),
            ("musculation", 3), ("haltere", 3), ("haltère", 3), ("yoga", 3),
            ("رياضة", 2), ("لياقة", 2), ("تمرين", 2), ("عضلات", 2), ("كارديو", 2),
        ],
        "negatives": [],
    },
    {
        "slug": "auto",
        "name_ar": "سيارات ومستلزماتها",
        "name_fr": "Auto",
        "keywords": [
            ("اطار", 3), ("زيت محرك", 3), ("كارشور", 3), ("ملحقات سيارة", 3),
            ("voiture", 3), ("pneu", 3), ("nettoyeur voiture", 3), ("سيارة", 2),
            ("محرك", 2), ("غسيل سيارة", 2), ("طلاء", 1),
        ],
        "negatives": [],
    },
    {
        "slug": "pets",
        "name_ar": "حيوانات أليفة",
        "name_fr": "Animaux",
        "keywords": [
            ("طعام كلاب", 3), ("طعام قطط", 3), ("قفص", 2), ("رمل قطط", 3),
            ("chien", 3), ("chat", 2), ("litiere", 3), ("litière", 3),
            ("حيوان", 2), ("اليف", 2), ("طوق", 1),
        ],
        "negatives": [],
    },
    {
        "slug": "jewelry",
        "name_ar": "مجوهرات وساعات",
        "name_fr": "Bijoux & montres",
        "keywords": [
            ("سلسلة", 3), ("خاتم", 3), ("اقراط", 3), ("سوار", 3), ("ذهب", 2),
            ("فضة", 2), ("montre", 2), ("bijoux", 3), ("bague", 3),
            ("مجوهرات", 3), ("ساعة", 1), ("اكسسوار", 1),
        ],
        "negatives": [],
    },
    {
        "slug": "perfumes",
        "name_ar": "عطور",
        "name_fr": "Parfums",
        "keywords": [
            ("عطر", 3), ("برفان", 3), ("مسك", 2), ("بخور", 2), ("parfum", 3),
            ("eau de toilette", 3), ("musk", 2), ("رائحة", 1), ("فواح", 2),
        ],
        "negatives": [],
    },
    {
        "slug": "furniture",
        "name_ar": "أثاث وديكور",
        "name_fr": "Meubles & déco",
        "keywords": [
            ("كنبة", 3), ("طاولة", 3), ("كرسي", 3), ("سرير", 3), ("خزانة", 3),
            ("meuble", 3), ("canape", 3), ("canapé", 3), ("table", 2), ("lit", 2),
            ("اثاث", 3), ("ديكور", 2), ("صالون", 2), ("غرفة نوم", 2),
        ],
        "negatives": [],
    },
    {
        "slug": "bags-shoes",
        "name_ar": "حقائب وأحذية",
        "name_fr": "Sacs & chaussures",
        "keywords": [
            ("حقيبة", 3), ("حذاء", 3), ("صندل", 3), ("محفظة", 3), ("sac", 3),
            ("chaussure", 3), ("sandale", 3), ("جلد", 1), ("مقاس", 1),
            ("حقيبة ظهر", 2), ("اكسسوار", 1),
        ],
        "negatives": [],
    },
]

NICHE_BY_SLUG = {n["slug"]: n for n in NICHES}

# ── Text normalisation / matching ──────────────────────────────────────
_TASHKEEL = re.compile(r"[\u0610-\u061A\u064B-\u065F\u0670\u06D6-\u06ED]")
_URL = re.compile(r"https?://\S+", re.IGNORECASE)
_NONWORD = re.compile(r"[^\w\s\u0600-\u06FF]+", re.UNICODE)
_SPACES = re.compile(r"\s+")


def normalize(text) -> str:
    t = str(text or "").lower()
    t = _URL.sub(" ", t)
    t = t.replace("\u0640", "")
    t = _TASHKEEL.sub("", t)
    t = re.sub(r"[\u0623\u0625\u0622\u0671]", "\u0627", t)  # أ إ آ ٱ -> ا
    t = t.replace("\u0649", "\u064A").replace("\u0629", "\u0647")  # ى->ي  ة->ه
    t = _NONWORD.sub(" ", t)
    return _SPACES.sub(" ", t).strip()


def _match(norm_text: str, kw: str) -> bool:
    if not kw or not norm_text:
        return False
    if len(kw) <= 3:
        # short words: require a whole word (with optional Arabic article)
        return re.search(r"(?:^|\s)(?:ال)?" + re.escape(kw) + r"(?:\s|$)", norm_text) is not None
    return kw in norm_text


def score_niche(body: str, page_name: str, store_domain: str, niche: dict) -> tuple[float, int]:
    nb, np_, nd = normalize(body), normalize(page_name), normalize(str(store_domain or "").replace(".", " "))
    score = 0.0
    for kw, weight in niche["keywords"]:
        k = normalize(kw)
        if _match(nb, k):
            score += weight
        elif _match(np_, k):
            score += weight * 0.5
        elif _match(nd, k):
            score += weight * 0.5
    penalty = sum(2 for neg in niche.get("negatives", []) if _match(nb, normalize(neg)))
    score = max(0.0, score - penalty)
    conf = int(round(100 * (1 - math.exp(-score / 6)))) if score > 0 else 0
    return score, conf


def classify(body: str, page_name: str, store_domain: str,
             niches: Optional[Iterable[dict]] = None,
             min_confidence: int = 40) -> list[dict]:
    """Return [{'slug','score','confidence'}] sorted by confidence (multi-label)."""
    out = []
    for n in (niches or NICHES):
        score, conf = score_niche(body, page_name, store_domain, n)
        if conf >= min_confidence:
            out.append({"slug": n["slug"], "score": round(score, 2), "confidence": conf})
    out.sort(key=lambda x: (-x["confidence"], -x["score"]))
    return out
