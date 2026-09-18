# SPDX-License-Identifier: AGPL-3.0-or-later
"""Yandex Videos scraper for the local SearXNG instance.

Parses Yandex Video's server-rendered preloaded JSON and exposes the direct
Yandex MP4 preview as ``iframe_src``. The original publisher URL remains in
``url``. No API key is required.
"""

from json import loads
from urllib.parse import urlencode

from lxml import html

from searx.exceptions import SearxEngineCaptchaException
from searx.engines.yandex_proxy_pool import (
    apply_yandex_route,
    fetch_yandex_via_free_proxy,
    report_yandex_route_result,
)

about = {
    "website": "https://yandex.ru/video/",
    "wikidata_id": "Q5281",
    "official_api_documentation": None,
    "use_official_api": False,
    "require_api_key": False,
    "results": "HTML",
}

categories = ["videos"]
paging = True
safesearch = True
base_url = "https://yandex.ru/video/search"


def _https(url):
    if not url:
        return ""
    if url.startswith("//"):
        return "https:" + url
    return url


def request(query, params):
    args = {"text": query}
    if params["pageno"] > 1:
        args["p"] = params["pageno"] - 1

    # Yandex uses the sp.family cookie for adult filtering. 0 = off,
    # 1 = moderate, 2 = strict. Keep the search mode aligned with SearXNG.
    family = max(0, min(2, int(params.get("safesearch", 0) or 0)))
    params["cookies"] = {
        "cookie": f"yp=1810541790.sp.family%3A{family}#1685406411.szm.1:1920x1080:1920x999"
    }
    params["headers"]["User-Agent"] = (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
    )
    params["headers"]["Accept-Language"] = "ru-RU,ru;q=0.9,en;q=0.7"
    params["raise_for_httperror"] = False
    params["url"] = f"{base_url}?{urlencode(args)}"
    apply_yandex_route(params)
    return params


def _video_parse(resp):
    dom = html.fromstring(resp.text)
    states = dom.xpath('//noframes[@id="UniAppVideo-PreloadedState"]')
    if not states:
        raise ValueError("Yandex video state missing")

    raw = states[0].text or states[0].text_content() or ""
    if not raw.strip():
        raise ValueError("Yandex video state empty")

    data = loads(raw)
    search = data.get("pages", {}).get("search", {})
    clips = data.get("clips", {}).get("items", {})
    results = []

    for serp_item in search.get("serpItems", []):
        if not isinstance(serp_item, dict) or serp_item.get("type") != "videoSnippet":
            continue
        props = serp_item.get("props") or {}
        video_id = str(props.get("videoId") or "")
        clip = clips.get(video_id)
        if not isinstance(clip, dict):
            outer_id = str(serp_item.get("id") or "").split("-0-", 1)[0]
            clip = clips.get(outer_id)
        if not isinstance(clip, dict):
            continue

        preview = clip.get("preview") or {}
        if not isinstance(preview, dict):
            continue
        video_src = _https(str(preview.get("videoSrc") or ""))
        video_type = str(preview.get("videoType") or "").lower()
        if not video_src.startswith(("http://", "https://")):
            continue
        if video_type and not video_type.startswith("video/"):
            continue

        related = clip.get("relatedParams") or {}
        if not isinstance(related, dict):
            related = {}
        title = str(related.get("text") or clip.get("description") or "Yandex video").strip()
        description = str(clip.get("description") or "").strip()
        source = str(clip.get("greenHost") or "Yandex Video").strip()
        original = _https(str(clip.get("url") or "").strip())
        poster = _https(str(preview.get("posterSrc") or "").strip())

        results.append(
            {
                "template": "videos.html",
                "url": original or video_src,
                "title": title,
                "content": description,
                "thumbnail": poster,
                "iframe_src": video_src,
                "source": source,
            }
        )
    return results


def response(resp):
    status = int(getattr(resp, "status_code", 0) or 0)
    captcha = resp.headers.get("x-yandex-captcha") == "captcha"
    if status != 200 or captcha:
        report_yandex_route_result(resp, False)
        proxied = fetch_yandex_via_free_proxy(
            str(resp.request.url),
            resp.request.headers,
            ("UniAppVideo-PreloadedState",),
        )
        if proxied is not None:
            try:
                return _video_parse(proxied)
            except Exception:
                pass
        if captcha:
            raise SearxEngineCaptchaException()
        return []

    try:
        results = _video_parse(resp)
        report_yandex_route_result(resp, True)
        return results
    except Exception:
        report_yandex_route_result(resp, False)
        proxied = fetch_yandex_via_free_proxy(
            str(resp.request.url),
            resp.request.headers,
            ("UniAppVideo-PreloadedState",),
        )
        if proxied is not None:
            try:
                return _video_parse(proxied)
            except Exception:
                pass
        return []
