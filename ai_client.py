# -*- coding: utf-8 -*-
"""OpenAI-compatible LLM client (OpenRouter, OpenCode Zen or any custom base URL).

The API key is stored encrypted in the settings table (Fernet, key derived
from SECRET_KEY). Changing SECRET_KEY makes the stored key unreadable: the
admin then has to enter it again.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import time
import urllib.error
import urllib.request
from typing import Optional

logger = logging.getLogger("ai_client")

PROVIDERS = {
    "openrouter": {"label": "OpenRouter", "base_url": "https://openrouter.ai/api/v1"},
    "opencode": {"label": "OpenCode Zen", "base_url": "https://opencode.ai/zen/v1"},
    "custom": {"label": "Custom (OpenAI-compatible)", "base_url": ""},
}

DEFAULTS = {
    "ai_provider": "openrouter",
    "ai_base_url": "",
    "ai_model": "",
    "ai_max_output_tokens": "4000",
    "ai_max_texts": "120",
    "ai_temperature": "0.3",
    "ai_daily_limit": "20",
    "ai_web_search": "1",
    "ai_audit_max_products": "8",
}

_ENC_PREFIX = "enc:v1:"


class AIError(Exception):
    pass


# ── encryption ───────────────────────────────────────────────────────────
def _fernet():
    from cryptography.fernet import Fernet
    secret = (os.environ.get("SECRET_KEY") or os.environ.get("FLASK_SECRET_KEY") or "").strip()
    if not secret:
        # Same fallback as the app in dev mode: a key that only lives this run.
        secret = os.environ.setdefault("_AI_EPHEMERAL_SECRET", base64.b64encode(os.urandom(32)).decode())
    key = base64.urlsafe_b64encode(hashlib.sha256(("ai-key|" + secret).encode()).digest())
    return Fernet(key)


def encrypt_secret(plain: str) -> str:
    plain = (plain or "").strip()
    if not plain:
        return ""
    return _ENC_PREFIX + _fernet().encrypt(plain.encode()).decode()


def decrypt_secret(stored: str) -> str:
    """Plain key, or "" when it is missing / cannot be decrypted."""
    s = (stored or "").strip()
    if not s:
        return ""
    if not s.startswith(_ENC_PREFIX):
        return s  # legacy plain value
    try:
        return _fernet().decrypt(s[len(_ENC_PREFIX):].encode()).decode()
    except Exception:
        logger.warning("AI API key cannot be decrypted (SECRET_KEY changed?)")
        return ""


def mask(key: str) -> str:
    if not key:
        return ""
    return (key[:6] + "…" + key[-4:]) if len(key) > 12 else "••••"


# ── config ───────────────────────────────────────────────────────────────
def load_config(settings: dict) -> dict:
    """Effective AI settings (strings from the settings table → typed dict)."""
    def _get(k):
        return str(settings.get(k) or DEFAULTS.get(k, "")).strip()

    provider = _get("ai_provider")
    if provider not in PROVIDERS:
        provider = "custom"
    base_url = (_get("ai_base_url") or PROVIDERS[provider]["base_url"]).rstrip("/")
    raw_key = str(settings.get("ai_api_key") or "")
    key = decrypt_secret(raw_key)

    def _num(k, cast, lo, hi):
        try:
            return max(lo, min(hi, cast(_get(k))))
        except (TypeError, ValueError):
            return cast(DEFAULTS[k])

    return {
        "provider": provider,
        "base_url": base_url,
        "api_key": key,
        "key_set": bool(raw_key),
        "key_unreadable": bool(raw_key) and not key,
        "model": _get("ai_model"),
        "max_output_tokens": _num("ai_max_output_tokens", int, 500, 32000),
        "max_texts": _num("ai_max_texts", int, 10, 600),
        "temperature": _num("ai_temperature", float, 0.0, 1.5),
        "daily_limit": _num("ai_daily_limit", int, 0, 10000),
        "web_search": _get("ai_web_search") not in ("0", "false", "off", ""),
        "audit_max_products": _num("ai_audit_max_products", int, 1, 25),
    }


def ready(cfg: dict) -> Optional[str]:
    """None when the client can be used, else why not."""
    if not cfg.get("base_url"):
        return "AI base URL is not set"
    if cfg.get("key_unreadable"):
        return "The saved AI key cannot be decrypted (SECRET_KEY changed) — enter it again"
    if not cfg.get("api_key"):
        return "AI API key is not set"
    if not cfg.get("model"):
        return "AI model is not set"
    return None


# ── HTTP ─────────────────────────────────────────────────────────────────
def _request(cfg: dict, path: str, payload: Optional[dict] = None, timeout: float = 60) -> dict:
    url = cfg["base_url"].rstrip("/") + path
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    if cfg.get("api_key"):
        req.add_header("Authorization", "Bearer " + cfg["api_key"])
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            raw = e.read().decode("utf-8", "replace")
            j = json.loads(raw)
            err = j.get("error")
            detail = (err.get("message") if isinstance(err, dict) else err) or raw
        except Exception:
            pass
        raise AIError(f"HTTP {e.code} from provider: {str(detail)[:400]}") from None
    except urllib.error.URLError as e:
        raise AIError(f"Cannot reach provider: {e.reason}") from None
    except TimeoutError:
        raise AIError("Provider timed out") from None
    try:
        return json.loads(body)
    except ValueError:
        raise AIError("Provider returned a non-JSON answer: " + body[:200]) from None


def chat(cfg: dict, messages: list[dict], max_tokens: Optional[int] = None,
         timeout: float = 300, web_search: int = 0) -> dict:
    """One chat completion. Returns {text, prompt_tokens, completion_tokens,
    cost, finish_reason, model, citations}.

    ``web_search`` > 0 asks OpenRouter's "web" plugin for that many search
    results (ignored by other providers: see supports_web_search)."""
    payload = {
        "model": cfg["model"],
        "messages": messages,
        "max_tokens": int(max_tokens or cfg["max_output_tokens"]),
        "temperature": cfg.get("temperature", 0.3),
    }
    if cfg.get("provider") == "openrouter":
        payload["usage"] = {"include": True}  # cost in the answer
        if web_search > 0:
            payload["plugins"] = [{"id": "web", "max_results": int(web_search)}]
    t0 = time.time()
    j = _request(cfg, "/chat/completions", payload, timeout=timeout)
    if j.get("error"):
        err = j["error"]
        raise AIError(str(err.get("message") if isinstance(err, dict) else err)[:400])
    choices = j.get("choices") or []
    if not choices:
        raise AIError("Provider returned no answer")
    msg = choices[0].get("message") or {}
    text = msg.get("content") or ""
    if isinstance(text, list):  # some providers return content parts
        text = "".join(p.get("text", "") for p in text if isinstance(p, dict))
    usage = j.get("usage") or {}
    citations = []
    for a in msg.get("annotations") or []:
        u = (a.get("url_citation") or {}).get("url") if isinstance(a, dict) else None
        if u and u not in citations:
            citations.append(u)
    logger.info("AI chat model=%s in=%s out=%s %.1fs", cfg.get("model"),
                usage.get("prompt_tokens"), usage.get("completion_tokens"), time.time() - t0)
    return {
        "text": str(text).strip(),
        "prompt_tokens": usage.get("prompt_tokens"),
        "completion_tokens": usage.get("completion_tokens"),
        "cost": usage.get("cost"),
        "finish_reason": choices[0].get("finish_reason"),
        "model": j.get("model") or cfg.get("model"),
        "citations": citations,
    }


def supports_web_search(cfg: dict) -> bool:
    return cfg.get("provider") == "openrouter" and bool(cfg.get("web_search"))


_MODELS_CACHE: dict = {}


def list_models(cfg: dict, ttl: float = 3600) -> list[dict]:
    """[{id, name, context, price_in, price_out}] (prices per 1M tokens, USD, when known)."""
    ck = (cfg.get("base_url"), bool(cfg.get("api_key")))
    hit = _MODELS_CACHE.get(ck)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    j = _request(cfg, "/models", None, timeout=30)
    out = []
    for m in j.get("data") or []:
        if not isinstance(m, dict) or not m.get("id"):
            continue
        pr = m.get("pricing") or {}

        def _per_m(v):
            try:
                return round(float(v) * 1_000_000, 4)
            except (TypeError, ValueError):
                return None
        out.append({
            "id": m["id"],
            "name": m.get("name") or m["id"],
            "context": m.get("context_length"),
            "price_in": _per_m(pr.get("prompt")),
            "price_out": _per_m(pr.get("completion")),
        })
    out.sort(key=lambda x: x["id"])
    _MODELS_CACHE[ck] = (time.time(), out)
    return out


def model_info(cfg: dict) -> Optional[dict]:
    """Cached /models entry of the configured model (None if unknown)."""
    try:
        for m in list_models(cfg):
            if m["id"] == cfg.get("model"):
                return m
    except AIError:
        pass
    return None
