"""AI competitor analysis: store link → dossier → LLM report (saved with its date).

The model call runs in a background thread so a long answer never holds a
request (or a gunicorn thread) open: the browser polls the saved report.
"""
import json
import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from flask import Blueprint, g, jsonify, request

import ai_client
import competitor
import product_audit
from db import db
from web.common import _audit, _scope_user_id, admin_required, login_required

logger = logging.getLogger("web_app")

bp = Blueprint("analysis", __name__)

# At most this many reports are generated at the same time (whole app).
_SLOTS = threading.BoundedSemaphore(3)
LANGS = ("auto", "ar", "dz", "fr", "en")
STORE_ERRORS = {
    "invalid_link": "رابط غير صالح — الصق رابط المتجر أو الدومين.",
    "not_a_store": "هذا الرابط ليس متجراً (فيسبوك / واتساب / إنستغرام …).",
    "platform_root": "هذا دومين المنصة نفسها — الصق رابط المتجر كاملاً مثل name.youcan.shop",
}


def _is_admin() -> bool:
    return (g.user or {}).get("role") == "admin"


def _cfg() -> dict:
    return ai_client.load_config(db.get_settings())


# ── AI settings (admin) ──────────────────────────────────────────────────
@bp.route("/api/ai/config", methods=["GET"])
@admin_required
def api_ai_config():
    cfg = _cfg()
    s = db.get_settings()
    return jsonify({
        "providers": {k: v for k, v in ai_client.PROVIDERS.items()},
        "provider": cfg["provider"],
        "base_url": str(s.get("ai_base_url") or ""),
        "effective_base_url": cfg["base_url"],
        "model": cfg["model"],
        "key_set": cfg["key_set"],
        "key_unreadable": cfg["key_unreadable"],
        "key_hint": ai_client.mask(cfg["api_key"]),
        "max_output_tokens": cfg["max_output_tokens"],
        "max_texts": cfg["max_texts"],
        "temperature": cfg["temperature"],
        "daily_limit": cfg["daily_limit"],
        "web_search": cfg["web_search"],
        "audit_max_products": cfg["audit_max_products"],
    })


@bp.route("/api/ai/config", methods=["POST"])
@admin_required
def api_ai_config_save():
    c = request.get_json(force=True, silent=True) or {}
    if "provider" in c:
        prov = str(c["provider"]).strip()
        db.set_setting("ai_provider", prov if prov in ai_client.PROVIDERS else "custom")
    if "base_url" in c:
        url = str(c["base_url"] or "").strip().rstrip("/")
        if url and not url.startswith(("https://", "http://")):
            return jsonify({"error": "Base URL must start with https://"}), 400
        db.set_setting("ai_base_url", url)
    if "model" in c:
        db.set_setting("ai_model", str(c["model"] or "").strip()[:200])
    if "web_search" in c:
        db.set_setting("ai_web_search", "1" if c["web_search"] in (True, 1, "1", "true", "on") else "0")
    for k in ("max_output_tokens", "max_texts", "temperature", "daily_limit", "audit_max_products"):
        if k in c and str(c[k]).strip() != "":
            db.set_setting("ai_" + k, str(c[k]).strip()[:20])
    changed_key = False
    if c.get("clear_key"):
        db.set_setting("ai_api_key", "")
        changed_key = True
    elif str(c.get("api_key") or "").strip():
        db.set_setting("ai_api_key", ai_client.encrypt_secret(str(c["api_key"])))
        changed_key = True
    _audit("ai_settings_changed", ",".join(sorted(k for k in c if k != "api_key"))
           + (",api_key" if changed_key else ""))
    return jsonify({"ok": True})


def _cfg_with_overrides(c: dict) -> dict:
    """Saved config, with unsaved form values (provider/url/key/model) on top."""
    cfg = _cfg()
    prov = str(c.get("provider") or "").strip()
    if prov in ai_client.PROVIDERS:
        cfg["provider"] = prov
        cfg["base_url"] = (str(c.get("base_url") or "").strip().rstrip("/")
                           or ai_client.PROVIDERS[prov]["base_url"])
    elif str(c.get("base_url") or "").strip():
        cfg["base_url"] = str(c["base_url"]).strip().rstrip("/")
    if str(c.get("api_key") or "").strip():
        cfg["api_key"] = str(c["api_key"]).strip()
        cfg["key_unreadable"] = False
    if str(c.get("model") or "").strip():
        cfg["model"] = str(c["model"]).strip()
    return cfg


@bp.route("/api/ai/models", methods=["POST"])
@admin_required
def api_ai_models():
    cfg = _cfg_with_overrides(request.get_json(force=True, silent=True) or {})
    if not cfg["base_url"]:
        return jsonify({"error": "Base URL is not set"}), 400
    try:
        return jsonify({"models": ai_client.list_models(cfg, ttl=0)})
    except ai_client.AIError as e:
        return jsonify({"error": str(e)}), 502


@bp.route("/api/ai/test", methods=["POST"])
@admin_required
def api_ai_test():
    cfg = _cfg_with_overrides(request.get_json(force=True, silent=True) or {})
    why = ai_client.ready(cfg)
    if why:
        return jsonify({"ok": False, "error": why})
    try:
        r = ai_client.chat(cfg, [{"role": "user", "content": "Reply with the single word: OK"}],
                           max_tokens=20, timeout=60)
        return jsonify({"ok": True, "reply": r["text"][:100], "model": r["model"]})
    except ai_client.AIError as e:
        return jsonify({"ok": False, "error": str(e)})


# ── analysis ─────────────────────────────────────────────────────────────
def _prepare(payload: dict) -> tuple[dict, dict | None, list | None, dict]:
    """Resolve the store, load its ads, build dossier + prompt.

    Returns (store, dossier, messages, cfg); dossier is None when nothing to analyse."""
    cfg = _cfg()
    store = competitor.resolve_store(str(payload.get("store") or ""), db)
    if not _is_admin():
        store["suggestions"] = []  # do not reveal other stores of the bank
    if store["error"] or not store["domains"]:
        return store, None, None, cfg
    ads = db.store_ads_for_analysis(store["domains"], user_id=_scope_user_id())
    if not ads:
        return store, None, None, cfg
    dossier = competitor.build_dossier(ads, store, max_texts=cfg["max_texts"])
    lang = payload.get("lang") if payload.get("lang") in LANGS else "auto"
    messages = competitor.build_messages(dossier, lang=lang, note=str(payload.get("note") or "")[:2000])
    return store, dossier, messages, cfg


def _store_json(store: dict) -> dict:
    out = {k: store[k] for k in ("host", "platform", "domains", "notes", "error", "suggestions")}
    if store["error"]:
        out["error_text"] = STORE_ERRORS.get(store["error"], store["error"])
    return out


def _quota_left(cfg: dict):
    if _is_admin() or not cfg["daily_limit"]:
        return None
    since = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    return max(0, cfg["daily_limit"] - db.count_ai_reports_since(int(g.user["id"]), since))


@bp.route("/api/analysis/preview", methods=["POST"])
@login_required
def api_analysis_preview():
    payload = request.get_json(force=True, silent=True) or {}
    store, dossier, messages, cfg = _prepare(payload)
    out = {"store": _store_json(store), "ready_error": ai_client.ready(cfg),
           "model": cfg["model"], "provider": cfg["provider"], "quota_left": _quota_left(cfg),
           "web_search": ai_client.supports_web_search(cfg),
           "audit_max_products": cfg["audit_max_products"]}
    if dossier is None:
        out["ads_found"] = 0
        return jsonify(out)
    tok_in = competitor.estimate_messages(messages)
    info = ai_client.model_info(cfg) if not out["ready_error"] else None
    cost = None
    if info and info.get("price_in") is not None and info.get("price_out") is not None:
        cost = {"input": round(tok_in * info["price_in"] / 1e6, 4),
                "max": round((tok_in * info["price_in"] + cfg["max_output_tokens"] * info["price_out"]) / 1e6, 4)}
    t = dossier["totals"]
    out.update({
        "ads_found": t["ads"], "active": t["active"], "unique_texts": t["unique_texts"],
        "duplicate_ads": t["duplicate_ads"], "texts_sent": len(dossier["texts"]),
        "texts_omitted": dossier["omitted_texts"]["count"],
        "first_launch": t["first_launch"], "last_launch": t["last_launch"],
        "tokens_in": tok_in, "max_output_tokens": cfg["max_output_tokens"],
        "context": (info or {}).get("context"), "cost_usd": cost,
        "top_texts": [{"id": x["id"], "ads": x["ads"], "active": x["active"],
                       "max_days": x["max_days"], "text": x["text"][:160]}
                      for x in dossier["texts"][:8]],
    })
    return jsonify(out)


def _run_report(rid: int, cfg: dict, messages: list) -> None:
    try:
        r = ai_client.chat(cfg, messages, timeout=600)
        text = r["text"]
        if not text:
            raise ai_client.AIError("The model returned an empty answer")
        if r.get("finish_reason") == "length":
            text += "\n\n> ⚠️ The report was cut at the output-token limit (raise it in Settings → AI)."
        db.finish_ai_report(rid, report=text, prompt_tokens=r.get("prompt_tokens"),
                            completion_tokens=r.get("completion_tokens"), cost=r.get("cost"))
    except ai_client.AIError as e:
        db.finish_ai_report(rid, error=str(e))
    except Exception as e:  # never leave a report "running"
        logger.exception("AI report %s failed", rid)
        db.finish_ai_report(rid, error=f"internal error: {e}")
    finally:
        _SLOTS.release()


AUDIT_WORKERS = 3      # product audits running at the same time inside one audit
AUDIT_WEB_RESULTS = 6  # web search results per product (OpenRouter web plugin)


def _run_audit(rid: int, cfg: dict, dossier: dict, host: str) -> None:
    """identify products → audit each one (parallel, web search) → Python math → table."""
    usage = {"prompt_tokens": 0, "completion_tokens": 0, "cost": 0.0, "cost_known": True}
    lock = threading.Lock()

    def _add(r: dict) -> None:
        with lock:
            usage["prompt_tokens"] += int(r.get("prompt_tokens") or 0)
            usage["completion_tokens"] += int(r.get("completion_tokens") or 0)
            if r.get("cost") is None:
                usage["cost_known"] = False
            else:
                usage["cost"] += float(r["cost"])

    def _finish(**kw):
        db.finish_ai_report(rid, prompt_tokens=usage["prompt_tokens"] or None,
                            completion_tokens=usage["completion_tokens"] or None,
                            cost=round(usage["cost"], 6) if usage["cost_known"] and usage["cost"] else None,
                            **kw)

    try:
        web = ai_client.supports_web_search(cfg)
        r = ai_client.chat(cfg, product_audit.identify_messages(
            competitor.dossier_text(dossier), cfg["audit_max_products"]),
            max_tokens=min(4000, max(1500, cfg["max_output_tokens"])), timeout=300)
        _add(r)
        products = [p for p in (product_audit.parse_json(r["text"]).get("products") or [])
                    if isinstance(p, dict) and (p.get("name_ar") or p.get("name_en"))]
        products = products[:cfg["audit_max_products"]]
        if not products:
            raise ai_client.AIError("The model found no product in this store's ads")

        rows = [{"product": p} for p in products]

        def _save_progress():
            with lock:
                pending = sum(1 for x in rows if not x.get("calc") and not x.get("error"))
                md = product_audit.render(host, rows, web, pending=pending)
                js = json.dumps(rows, ensure_ascii=False, default=str)
            db.progress_ai_report(rid, md, js)

        _save_progress()

        def _one(row: dict) -> None:
            try:
                a = ai_client.chat(cfg, product_audit.audit_messages(row["product"], dossier, web),
                                   max_tokens=min(4000, max(2000, cfg["max_output_tokens"])),
                                   timeout=420, web_search=AUDIT_WEB_RESULTS if web else 0)
                _add(a)
                facts = product_audit.parse_json(a["text"])
                if not facts.get("competitor_price_dzd"):
                    facts["competitor_price_dzd"] = row["product"].get("competitor_price_dzd")
                calc = product_audit.evaluate(facts)
                with lock:
                    row.update(facts=facts, calc=calc, citations=a.get("citations") or [])
            except (ai_client.AIError, ValueError) as e:
                with lock:
                    row["error"] = str(e)[:300]
            except Exception as e:  # one product must not kill the audit
                logger.exception("audit %s product failed", rid)
                with lock:
                    row["error"] = f"internal error: {e}"[:300]
            _save_progress()

        with ThreadPoolExecutor(max_workers=AUDIT_WORKERS) as ex:
            list(ex.map(_one, rows))

        md = product_audit.render(host, rows, web)
        js = json.dumps(rows, ensure_ascii=False, default=str)
        if not any(x.get("calc") for x in rows):
            _finish(error="Every product audit failed: " + "; ".join(
                x.get("error", "") for x in rows)[:1500], report=md, audit=js)
        else:
            _finish(report=md, audit=js)
    except (ai_client.AIError, ValueError) as e:
        _finish(error=str(e))
    except Exception as e:  # never leave a report "running"
        logger.exception("AI audit %s failed", rid)
        _finish(error=f"internal error: {e}")
    finally:
        _SLOTS.release()


@bp.route("/api/analysis/run", methods=["POST"])
@login_required
def api_analysis_run():
    payload = request.get_json(force=True, silent=True) or {}
    store, dossier, messages, cfg = _prepare(payload)
    if store["error"]:
        return jsonify({"error": STORE_ERRORS.get(store["error"], store["error"])}), 400
    if dossier is None:
        return jsonify({"error": "لا توجد إعلانات لهذا المتجر في القاعدة."}), 404
    why = ai_client.ready(cfg)
    if why:
        return jsonify({"error": why}), 400
    left = _quota_left(cfg)
    if left is not None and left <= 0:
        return jsonify({"error": "وصلت للحد اليومي من التحليلات."}), 429
    if not _SLOTS.acquire(blocking=False):
        return jsonify({"error": "تحليلات أخرى قيد التشغيل — أعد المحاولة بعد قليل."}), 429
    kind = "audit" if payload.get("kind") == "audit" else "report"
    try:
        t = dossier["totals"]
        rid = db.create_ai_report(
            user_id=int(g.user["id"]), store_domain=store["host"], platform=store["platform"],
            provider=cfg["provider"], model=cfg["model"],
            lang=payload.get("lang") if payload.get("lang") in LANGS else "auto",
            note=str(payload.get("note") or "")[:2000], ads_count=t["ads"],
            unique_texts=t["unique_texts"], tokens_est=competitor.estimate_messages(messages),
            dossier=json.dumps(dossier, ensure_ascii=False), kind=kind)
        target = _run_audit if kind == "audit" else _run_report
        args = (rid, cfg, dossier, store["host"]) if kind == "audit" else (rid, cfg, messages)
        threading.Thread(target=target, args=args, name=f"ai-{kind}-{rid}", daemon=True).start()
    except Exception:
        _SLOTS.release()
        raise
    _audit(f"ai_{kind}_started", f"{store['host']} #{rid}")
    return jsonify({"ok": True, "id": rid, "kind": kind})


GEMINI_SYSTEM_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                  "prompts", "gemini_audit_system.md")
EXPORT_MAX_TEXTS = 100000  # AI Studio has a huge context: send every unique text


def _gemini_system() -> str:
    with open(GEMINI_SYSTEM_PATH, encoding="utf-8") as f:
        return f.read()


@bp.route("/api/analysis/export", methods=["POST"])
@login_required
def api_analysis_export():
    """System instructions + the store's full data, to paste in Google AI Studio."""
    payload = request.get_json(force=True, silent=True) or {}
    store = competitor.resolve_store(str(payload.get("store") or ""), db)
    if store["error"]:
        return jsonify({"error": STORE_ERRORS.get(store["error"], store["error"])}), 400
    ads = db.store_ads_for_analysis(store["domains"], user_id=_scope_user_id()) if store["domains"] else []
    if not ads:
        return jsonify({"error": "لا توجد إعلانات لهذا المتجر في القاعدة."}), 404
    dossier = competitor.build_dossier(ads, store, max_texts=EXPORT_MAX_TEXTS)
    data = competitor.export_text(dossier, ads, note=str(payload.get("note") or "")[:2000])
    system = _gemini_system()
    _audit("ai_export", store["host"])
    return jsonify({
        "store": store["host"], "system": system, "data": data,
        "filename": f"{store['host']}-{datetime.now(timezone.utc).date().isoformat()}.txt",
        "ads": dossier["totals"]["ads"], "unique_texts": dossier["totals"]["unique_texts"],
        "tokens_est": competitor.estimate_tokens(system) + competitor.estimate_tokens(data),
    })


@bp.route("/api/analysis/import", methods=["POST"])
@login_required
def api_analysis_import():
    """Save an answer pasted back from AI Studio as a report of this store."""
    payload = request.get_json(force=True, silent=True) or {}
    text = str(payload.get("report") or "").strip()
    if len(text) < 20:
        return jsonify({"error": "الصق نتيجة Gemini أولاً."}), 400
    store = competitor.resolve_store(str(payload.get("store") or ""), db)
    if store["error"]:
        return jsonify({"error": STORE_ERRORS.get(store["error"], store["error"])}), 400
    rid = db.create_ai_report(
        user_id=int(g.user["id"]), store_domain=store["host"], platform=store["platform"],
        provider="manual", model="Google AI Studio (Gemini)", lang="auto",
        note=str(payload.get("note") or "")[:2000], ads_count=int(payload.get("ads") or 0),
        unique_texts=int(payload.get("unique_texts") or 0), tokens_est=0, dossier="{}", kind="audit")
    db.finish_ai_report(rid, report=text[:400000])
    _audit("ai_import", f"{store['host']} #{rid}")
    return jsonify({"ok": True, "id": rid})


def _own_report(rid: int):
    row = db.get_ai_report(rid)
    if not row or (not _is_admin() and int(row.get("user_id") or 0) != int(g.user["id"])):
        return None
    return row


@bp.route("/api/analysis/reports")
@login_required
def api_analysis_reports():
    uid = None if _is_admin() else int(g.user["id"])
    domain = competitor.clean_store_host(request.args.get("domain") or "")
    return jsonify({"reports": db.list_ai_reports(user_id=uid, domain=domain)})


@bp.route("/api/analysis/reports/<int:rid>")
@login_required
def api_analysis_report(rid: int):
    row = _own_report(rid)
    if not row:
        return jsonify({"error": "not found"}), 404
    row = dict(row)
    row.pop("audit", None)  # the Markdown report already holds the table
    try:
        d = json.loads(row.pop("dossier") or "{}")
    except ValueError:
        d = {}
    # The full dossier is big; the UI only needs its summary parts.
    row["dossier"] = {k: d.get(k) for k in ("store", "today", "totals", "ctas", "products",
                                             "days_active_buckets", "signals_in_texts", "prices_dzd")}
    return jsonify(row)


@bp.route("/api/analysis/reports/<int:rid>", methods=["DELETE"])
@login_required
def api_analysis_report_delete(rid: int):
    row = _own_report(rid)
    if not row:
        return jsonify({"error": "not found"}), 404
    if row.get("status") == "running":
        return jsonify({"error": "still running"}), 409
    db.delete_ai_report(rid)
    return jsonify({"ok": True})
