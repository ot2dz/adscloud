# -*- coding: utf-8 -*-
"""Best-effort public-IP rotation via a local rotator agent.

The agent (E:\\opencode\\ip-rotator) re-dials the ONT PPPoE session
(``pppoe client detach/attach interface wan1``) and waits for a new
public IP before answering.

This module is a thin, failure-proof client:

- no-op when ``IP_ROTATE_URL`` is unset (deployments stay unchanged)
- never raises: the scrape continues exactly as before on any error
- debounced: a burst of blocks triggers a single rotation

The agent answers ``202 {"accepted": true}`` immediately and re-dials a
moment later, because the re-dial tears down the SSH tunnel that carries
the request itself.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_last_ok = 0.0


def _url() -> str:
    return (os.environ.get("IP_ROTATE_URL") or "").strip()


def enabled() -> bool:
    """True when a rotator endpoint is configured."""
    return bool(_url())


def rotate(timeout: float = 30.0, min_interval: float = 120.0) -> "dict | None":
    """Ask the rotator to change the public IP. Returns the agent's JSON or None.

    ``min_interval=0`` forces a manual rotation (still bounded by the agent).
    """
    url = _url()
    if not url:
        return None

    global _last_ok
    with _lock:
        if min_interval and _last_ok and (time.time() - _last_ok) < min_interval:
            logger.info("IP rotate skipped (debounce %ss)", min_interval)
            return None
        try:
            req = urllib.request.Request(
                url,
                method="POST",
                data=b"{}",
                headers={"Content-Type": "application/json", "Accept": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read().decode("utf-8", "replace")
            data = json.loads(body or "{}")
            if data.get("accepted"):
                _last_ok = time.time()
                logger.warning("Public IP rotation started (agent working in background)")
            elif data.get("ok"):
                _last_ok = time.time()
                logger.warning(
                    "Public IP rotated: %s -> %s (%.1fs)",
                    data.get("old_ip"), data.get("new_ip"), float(data.get("seconds") or 0),
                )
            else:
                logger.warning("IP rotate did not change the IP: %s", data)
            return data
        except urllib.error.HTTPError as exc:
            # 429 (too soon) / 409 (busy) carry a JSON body worth returning.
            try:
                data = json.loads(exc.read().decode("utf-8", "replace") or "{}")
            except Exception:  # noqa: BLE001
                data = {"ok": False, "error": "HTTP %s" % exc.code}
            logger.warning("IP rotate HTTP %s: %s", exc.code, data)
            return data
        except Exception as exc:  # noqa: BLE001
            logger.warning("IP rotate failed: %s", exc)
            return None


def status(timeout: float = 10.0) -> "dict | None":
    """Agent status (current IP, last rotation). None when unavailable."""
    url = _url()
    if not url:
        return None
    try:
        base = url.rsplit("/", 1)[0]
        req = urllib.request.Request(base + "/status", headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", "replace") or "{}")
    except Exception:
        return None
