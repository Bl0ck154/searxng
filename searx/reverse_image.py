# SPDX-License-Identifier: AGPL-3.0-or-later
"""Reverse-image search helpers for the Oscar SearXNG fork.

The public endpoint lives in :mod:`searx.webapp` and deliberately keeps
provider-specific behavior here. Reverse-image providers are implemented directly against public web endpoints.
Yandex CBIR is currently the production provider; no paid search API or API
key is required.
"""

from __future__ import annotations

import hashlib
import html as html_lib
import json
import os
import time
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx
from lxml import html

from searx.engines.yandex_proxy_pool import fetch_yandex_via_free_proxy


YANDEX_REVERSE_URL = "https://yandex.com/images/search"

ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp"}
MAX_UPLOAD_BYTES = int(os.getenv("SEARXNG_REVERSE_IMAGE_MAX_UPLOAD_BYTES") or 8 * 1024 * 1024)
DEFAULT_LIMIT = 30
MAX_LIMIT = 100
REQUEST_TIMEOUT = 18.0

_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9,ru;q=0.7",
}


class ReverseImageError(RuntimeError):
    """Expected user/provider error suitable for a 4xx/5xx JSON response."""


def _clean_public_url(value: str) -> str:
    value = (value or "").strip()
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ReverseImageError("image_url must be an absolute http(s) URL")
    return value


def _dedupe_url(value: str) -> str:
    """Strip tracking/query/fragment for stable cross-provider deduplication."""
    try:
        parsed = urlsplit(value)
    except ValueError:
        return value
    if parsed.scheme not in {"http", "https"}:
        return value
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path, "", ""))


def _as_https(value: str | None) -> str:
    value = str(value or "").strip()
    if value.startswith("//"):
        return "https:" + value
    return value


def _normalize_result(
    *,
    provider: str,
    match_type: str,
    title: Any,
    url: Any,
    source: Any = "",
    thumbnail: Any = "",
    image_url: Any = "",
    position: Any = None,
    content: Any = "",
) -> dict[str, Any] | None:
    link = str(url or "").strip()
    if not link.startswith(("http://", "https://")):
        return None
    item: dict[str, Any] = {
        "provider": provider,
        "match_type": match_type,
        "title": str(title or "").strip(),
        "url": link,
        "source": str(source or "").strip(),
        "thumbnail": _as_https(str(thumbnail or "")),
        "image_url": _as_https(str(image_url or "")),
    }
    if position is not None:
        item["position"] = position
    content = str(content or "").strip()
    if content:
        item["content"] = content
    return item


def _merge_unique(results: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for item in results:
        key = _dedupe_url(str(item.get("url") or ""))
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(item)
        if len(out) >= limit:
            break
    return out


def _merge_provider_results(provider_rows: list[list[dict[str, Any]]], limit: int) -> list[dict[str, Any]]:
    """Round-robin providers so one engine cannot consume the whole result budget."""
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    max_rows = max((len(rows) for rows in provider_rows), default=0)
    for index in range(max_rows):
        for rows in provider_rows:
            if index >= len(rows):
                continue
            item = rows[index]
            key = _dedupe_url(str(item.get("url") or ""))
            if not key or key in seen:
                continue
            seen.add(key)
            out.append(item)
            if len(out) >= limit:
                return out
    return out



def _extract_yandex_state(page: str) -> dict[str, Any]:
    try:
        dom = html.fromstring(page)
    except Exception as exc:
        raise ReverseImageError("invalid Yandex reverse-image response") from exc

    best: dict[str, Any] | None = None
    for node in dom.xpath('//*[@data-state]'):
        raw = node.get("data-state") or ""
        if "cbir" not in raw.lower():
            continue
        try:
            decoded = json.loads(html_lib.unescape(raw))
        except Exception:
            continue
        initial = decoded.get("initialState") if isinstance(decoded, dict) else None
        if not isinstance(initial, dict):
            continue
        if "cbirSites" in initial:
            return initial
        best = best or initial

    if best is None:
        lowered = page.lower()
        if "captcha" in lowered or "not a robot" in lowered:
            raise ReverseImageError("Yandex reverse-image search returned SmartCaptcha")
        raise ReverseImageError("Yandex reverse-image state missing")
    return best


def _yandex_reverse(
    *,
    image_url: str | None,
    image_bytes: bytes | None,
    mime_type: str,
    limit: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    params: dict[str, Any]
    with httpx.Client(timeout=REQUEST_TIMEOUT, follow_redirects=True, headers=_BROWSER_HEADERS) as client:
        if image_bytes is not None:
            upload_params = {
                "rpt": "imageview",
                "format": "json",
                "request": '{"blocks":[{"block":"b-page_type_search-by-image__link"}]}',
            }
            upload = client.post(
                YANDEX_REVERSE_URL,
                params=upload_params,
                files={"upfile": ("blob", image_bytes, mime_type)},
            )
            upload.raise_for_status()
            try:
                upload_payload = upload.json()
            except ValueError as exc:
                raise ReverseImageError("invalid Yandex reverse-image upload response") from exc
            cbir_id = ""
            for block in upload_payload.get("blocks") or []:
                if not isinstance(block, dict):
                    continue
                block_params = block.get("params")
                if isinstance(block_params, dict) and block_params.get("cbirId"):
                    cbir_id = str(block_params["cbirId"]).strip()
                    break
            if not cbir_id:
                raise ReverseImageError("Yandex reverse-image upload returned no cbirId")
            params = {"rpt": "imageview", "cbir_page": "sites", "cbir_id": cbir_id}
        else:
            params = {
                "rpt": "imageview",
                "cbir_page": "sites",
                "url": _clean_public_url(str(image_url or "")),
            }

        response = client.get(YANDEX_REVERSE_URL, params=params)

    page = response.text
    try:
        state = _extract_yandex_state(page)
    except ReverseImageError:
        # Reuse the server-wide validated Yandex proxy broker as a best-effort
        # fallback. Reverse-image CBIR is more aggressively protected than the
        # normal Yandex Images endpoint, so failure here is non-fatal.
        proxied = fetch_yandex_via_free_proxy(
            str(response.request.url),
            response.request.headers,
            ("cbir",),
        )
        if proxied is None:
            raise
        state = _extract_yandex_state(proxied.text)

    results: list[dict[str, Any]] = []
    sites = (state.get("cbirSites") or {}).get("sites") or []
    for index, raw in enumerate(sites, start=1):
        if not isinstance(raw, dict):
            continue
        original = raw.get("originalImage") or {}
        item = _normalize_result(
            provider="yandex",
            match_type="site",
            title=raw.get("title"),
            url=raw.get("url"),
            source=raw.get("domain"),
            thumbnail=(raw.get("thumb") or {}).get("url"),
            image_url=original.get("url"),
            position=index,
            content=raw.get("description"),
        )
        if item:
            results.append(item)

    metadata: dict[str, Any] = {}
    tags = (state.get("cbirTags") or {}).get("tags") or []
    if tags:
        metadata["tags"] = tags[:20]
    ocr = state.get("cbirOcr") or {}
    if isinstance(ocr, dict) and ocr.get("plainText"):
        metadata["ocr_text"] = str(ocr.get("plainText"))
    preview = state.get("cbirPreview") or {}
    if isinstance(preview, dict):
        metadata["image_expired"] = bool(preview.get("imageExpired"))
        metadata["image_width"] = preview.get("imageWidth")
        metadata["image_height"] = preview.get("imageHeight")

    return _merge_unique(results, limit), metadata


def reverse_image_search(
    *,
    image_bytes: bytes | None = None,
    image_url: str | None = None,
    filename: str = "image.jpg",
    mime_type: str = "image/jpeg",
    providers: tuple[str, ...] = ("yandex",),
    safe: bool = False,
    limit: int = DEFAULT_LIMIT,
) -> dict[str, Any]:
    """Run reverse-image providers and return a normalized aggregate response."""
    if (image_bytes is None) == (image_url is None):
        raise ReverseImageError("provide exactly one of image or image_url")
    if image_bytes is not None:
        if mime_type not in ALLOWED_IMAGE_TYPES:
            raise ReverseImageError("supported upload types: JPEG, PNG, WebP")
        if not image_bytes:
            raise ReverseImageError("empty image upload")
        fingerprint = hashlib.sha256(image_bytes).hexdigest()
        source = "upload"
    else:
        image_url = _clean_public_url(str(image_url or ""))
        fingerprint = hashlib.sha256(image_url.encode("utf-8")).hexdigest()
        source = "url"

    limit = max(1, min(MAX_LIMIT, int(limit or DEFAULT_LIMIT)))
    requested = tuple(dict.fromkeys(p.strip().lower() for p in providers if p.strip()))
    supported = {"yandex"}
    unknown = [p for p in requested if p not in supported]
    if unknown:
        raise ReverseImageError("unsupported provider(s): " + ", ".join(unknown))

    provider_results: list[list[dict[str, Any]]] = []
    provider_status: list[dict[str, Any]] = []
    metadata: dict[str, Any] = {}

    for provider in requested:
        started = time.monotonic()
        try:
            if provider == "yandex":
                rows, provider_meta = _yandex_reverse(
                    image_url=image_url,
                    image_bytes=image_bytes,
                    mime_type=mime_type,
                    limit=limit,
                )
            else:  # pragma: no cover
                continue
            elapsed = round((time.monotonic() - started) * 1000)
            provider_status.append(
                {"provider": provider, "ok": True, "count": len(rows), "latency_ms": elapsed}
            )
            if provider_meta:
                metadata[provider] = provider_meta
            provider_results.append(rows)
        except (ReverseImageError, httpx.HTTPError, ValueError, json.JSONDecodeError) as exc:
            elapsed = round((time.monotonic() - started) * 1000)
            provider_status.append(
                {
                    "provider": provider,
                    "ok": False,
                    "count": 0,
                    "latency_ms": elapsed,
                    "error": str(exc),
                }
            )

    return {
        "fingerprint": fingerprint,
        "source": source,
        "safe": bool(safe),
        "results": _merge_provider_results(provider_results, limit),
        "providers": provider_status,
        "metadata": metadata,
    }
