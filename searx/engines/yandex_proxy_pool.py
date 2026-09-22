# SPDX-License-Identifier: AGPL-3.0-or-later
"""Yandex routing/fallback through the canonical server-wide proxy broker.

Normal mode keeps the hot path direct and sends only a small sample through the
broker's full Yandex pool. The broker ranks static Webshare ahead of public
proxies, so sampled proxy traffic uses the stable account-backed lane first and
public proxies only as fallback. Direct failures can still use the same broker
pool as an emergency retry.
"""

from __future__ import annotations

import logging
import os
import subprocess
import threading
import time
from types import SimpleNamespace

LOG = logging.getLogger("searx.engines.yandex_proxy_pool")
BROKER_PROXY = os.getenv("YANDEX_PROXY_BROKER_URL", "http://172.17.0.1:20140")
BROKER_NETWORK = os.getenv("YANDEX_BROKER_NETWORK", "yandex_broker_proxy")
WGET_TIMEOUT = 4
PROCESS_TIMEOUT = 5.5

NORMAL_BROKER_EVERY = max(2, int(os.getenv("YANDEX_NORMAL_BROKER_EVERY", "10")))  # 10% broker sample
DEGRADED_DIRECT_EVERY = max(2, int(os.getenv("YANDEX_DEGRADED_DIRECT_EVERY", "5")))  # 20% direct probes
DEGRADED_SECONDS = max(60, int(os.getenv("YANDEX_DEGRADED_SECONDS", "900")))
RECOVERY_DIRECT_SUCCESSES = max(1, int(os.getenv("YANDEX_RECOVERY_DIRECT_SUCCESSES", "3")))

_ROUTE_LOCK = threading.Lock()
_ROUTE_SEQ = 0
_DEGRADED_UNTIL = 0.0
_DIRECT_RECOVERY_STREAK = 0


class ProxyResponse:
    def __init__(self, url: str, request_headers, body: bytes):
        self.status_code = 200
        self.headers = {}
        self.content = body
        self.text = body.decode("utf-8", "replace")
        self.request = SimpleNamespace(url=url, headers=request_headers)


def _safe_headers(headers) -> dict[str, str]:
    src = {str(k).lower(): str(v) for k, v in dict(headers or {}).items()}
    out: dict[str, str] = {}
    for name in ("user-agent", "accept-language", "accept", "cookie"):
        if src.get(name):
            out[name] = src[name]
    return out


def choose_yandex_route() -> str:
    """Keep the hot path direct and sample broker-any occasionally."""
    global _ROUTE_SEQ
    with _ROUTE_LOCK:
        _ROUTE_SEQ += 1
        return "broker" if _ROUTE_SEQ % NORMAL_BROKER_EVERY == 0 else "direct"


def apply_yandex_route(params) -> str:
    """Choose a route and switch the current SearXNG request network if needed."""
    route = choose_yandex_route()
    params["_yandex_route"] = route
    if route == "broker":
        # Import lazily to avoid engine/network initialization cycles.
        from searx import network as searx_network

        searx_network.set_context_network_name(BROKER_NETWORK)
    return route


def report_yandex_route_result(resp, ok: bool) -> None:
    """Update degraded/recovery state from the original SearXNG response."""
    global _DEGRADED_UNTIL, _DIRECT_RECOVERY_STREAK
    params = getattr(resp, "search_params", None) or {}
    route = str(params.get("_yandex_route") or "direct")
    if route != "direct":
        return

    now = time.monotonic()
    with _ROUTE_LOCK:
        degraded = now < _DEGRADED_UNTIL
        if not ok:
            _DIRECT_RECOVERY_STREAK = 0
            _DEGRADED_UNTIL = max(_DEGRADED_UNTIL, now + DEGRADED_SECONDS)
            if not degraded:
                LOG.warning(
                    "Yandex direct route degraded; broker fallback armed for %ds while direct remains primary",
                    DEGRADED_SECONDS,
                )
            return

        if degraded:
            _DIRECT_RECOVERY_STREAK += 1
            if _DIRECT_RECOVERY_STREAK >= RECOVERY_DIRECT_SUCCESSES:
                _DEGRADED_UNTIL = 0.0
                _DIRECT_RECOVERY_STREAK = 0
                LOG.info("Yandex direct route recovered; returning to normal direct/broker mix")


def fetch_yandex_via_free_proxy(url: str, headers, expected_markers: tuple[str, ...]):
    """Emergency fetch through the broker's full validated Yandex pool."""
    safe_headers = _safe_headers(headers)
    env = os.environ.copy()
    env["http_proxy"] = BROKER_PROXY
    env["https_proxy"] = BROKER_PROXY
    env["HTTP_PROXY"] = BROKER_PROXY
    env["HTTPS_PROXY"] = BROKER_PROXY

    command = [
        "/usr/sbin/wget",
        "-qO-",
        "--tries=1",
        f"--timeout={WGET_TIMEOUT}",
    ]
    if safe_headers.get("user-agent"):
        command.append("--user-agent=" + safe_headers["user-agent"])
    for name in ("accept-language", "accept", "cookie"):
        if safe_headers.get(name):
            command.append(f"--header={name}: {safe_headers[name]}")
    command.append(url)

    try:
        result = subprocess.run(
            command,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=PROCESS_TIMEOUT,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        LOG.warning("Yandex broker emergency fallback failed: %s", exc)
        return None

    if result.returncode != 0 or not result.stdout:
        LOG.warning("Yandex broker emergency fallback failed rc=%s", result.returncode)
        return None

    text = result.stdout.decode("utf-8", "replace")
    if not all(marker in text for marker in expected_markers):
        LOG.warning("Yandex broker emergency fallback returned unexpected document")
        return None

    LOG.info("Yandex broker emergency fallback succeeded via %s", BROKER_PROXY)
    return ProxyResponse(url, safe_headers, result.stdout)
