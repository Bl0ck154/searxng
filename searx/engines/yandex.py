# SPDX-License-Identifier: AGPL-3.0-or-later
"""Yandex (Web, images)"""

from json import loads
from urllib.parse import urlencode
from html import unescape
from lxml import html
from searx.exceptions import SearxEngineCaptchaException
from searx.utils import humanize_bytes, eval_xpath, eval_xpath_list, extract_text, extr
from searx.engines.yandex_proxy_pool import (
    apply_yandex_route,
    fetch_yandex_via_free_proxy,
    report_yandex_route_result,
)


# Engine metadata
about = {
    "website": 'https://yandex.com/',
    "wikidata_id": 'Q5281',
    "official_api_documentation": "?",
    "use_official_api": False,
    "require_api_key": False,
    "results": 'HTML',
}

# Engine configuration
categories = []
paging = True
search_type = ""

# Search URL
base_url_web = 'https://yandex.com/search/site/'
base_url_images = 'https://yandex.ru/images/search'

# Supported languages
yandex_supported_langs = [
    "ru",  # Russian
    "en",  # English
    "be",  # Belarusian
    "fr",  # French
    "de",  # German
    "id",  # Indonesian
    "kk",  # Kazakh
    "tt",  # Tatar
    "tr",  # Turkish
    "uk",  # Ukrainian
]

results_xpath = '//li[contains(@class, "serp-item")]'
url_xpath = './/a[@class="b-serp-item__title-link"]/@href'
title_xpath = './/h3[@class="b-serp-item__title"]/a[@class="b-serp-item__title-link"]/span'
content_xpath = './/div[@class="b-serp-item__content"]//div[@class="b-serp-item__text"]'


def catch_bad_response(resp):
    if resp.headers.get('x-yandex-captcha') == 'captcha':
        raise SearxEngineCaptchaException()


def request(query, params):
    query_params_web = {
        "tmpl_version": "releases",
        "text": query,
        "web": "1",
        "frame": "1",
        "searchid": "3131712",
    }

    lang = params["language"].split("-")[0]
    if lang in yandex_supported_langs:
        query_params_web["lang"] = lang

    query_params_images = {
        "text": query,
        "uinfo": "sw-1920-sh-1080-ww-1125-wh-999",
    }

    if params['pageno'] > 1:
        query_params_web.update({"p": params["pageno"] - 1})
        query_params_images.update({"p": params["pageno"] - 1})

    params["cookies"] = {'cookie': "yp=1716337604.sp.family%3A0#1685406411.szm.1:1920x1080:1920x999"}

    if search_type == 'web':
        params['url'] = f"{base_url_web}?{urlencode(query_params_web)}"
    elif search_type == 'images':
        # Keep the frontend variant deterministic. Yandex occasionally serves
        # a different/A-B image state to rotating scraper UAs, which makes the
        # embedded JSON shape less stable than a normal desktop browser request.
        params['headers']['User-Agent'] = (
            'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
            '(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36'
        )
        params['headers']['Accept-Language'] = 'ru-RU,ru;q=0.9,en;q=0.7'
        # Let response() see 403/429/CAPTCHA pages so it can try the bounded
        # free-proxy fallback before the bot spends a SerpApi request.
        params['raise_for_httperror'] = False
        params['url'] = f"{base_url_images}?{urlencode(query_params_images)}"
        apply_yandex_route(params)

    return params


def _image_parse(resp):
    # Yandex already embeds the complete search state as JSON in a data-state
    # attribute. Parsing that state is much more stable than cutting a JSON
    # substring out of serialized HTML.
    dom = html.fromstring(resp.text)
    json_resp = None
    for node in dom.xpath('//*[@data-state]'):
        state = node.get('data-state') or ''
        if '\"initialState\"' not in state or '\"serpList\"' not in state:
            continue
        try:
            candidate = loads(state)
        except Exception:
            continue
        entities = (
            candidate.get('initialState', {})
            .get('serpList', {})
            .get('items', {})
            .get('entities', {})
        )
        if isinstance(entities, dict) and entities:
            json_resp = candidate
            break

    if not json_resp:
        raise ValueError('Yandex image state missing')

    results = []
    entities = json_resp['initialState']['serpList']['items']['entities']
    for _, item_data in entities.items():
        snippet = item_data.get('snippet') or {}
        viewer = item_data.get('viewerData') or {}
        title = snippet.get('title') or item_data.get('alt') or ''
        source = snippet.get('url') or item_data.get('url') or ''

        candidates = []
        thumb = viewer.get('thumb')
        if isinstance(thumb, dict):
            candidates.append(thumb)
        candidates.extend(i for i in (viewer.get('dups') or []) if isinstance(i, dict))
        candidates.extend(i for i in (viewer.get('preview') or []) if isinstance(i, dict))
        candidates = [i for i in candidates if i.get('url') and i.get('w') and i.get('h')]
        if not candidates:
            continue

        image_source = max(candidates, key=lambda i: int(i.get('w') or 0) * int(i.get('h') or 0))
        width = int(image_source.get('w') or 0)
        height = int(image_source.get('h') or 0)

        humanized_filesize = None
        if image_source.get('fileSizeInBytes'):
            humanized_filesize = humanize_bytes(image_source['fileSizeInBytes'])

        results.append(
            {
                'title': title,
                'url': source,
                'img_src': image_source['url'],
                'filesize': humanized_filesize,
                'thumbnail_src': item_data.get('image') or thumb.get('url', '') if isinstance(thumb, dict) else '',
                'template': 'images.html',
                'resolution': f'{width} x {height}',
                'width': width,
                'height': height,
                'source': snippet.get('domain') or '',
            }
        )
    return results


def _image_response(resp, allow_proxy=True):
    status = int(getattr(resp, 'status_code', 0) or 0)
    captcha = resp.headers.get('x-yandex-captcha') == 'captcha'
    if status != 200 or captcha:
        if allow_proxy:
            report_yandex_route_result(resp, False)
            proxied = fetch_yandex_via_free_proxy(
                str(resp.request.url),
                resp.request.headers,
                ('initialState', 'serpList'),
            )
            if proxied is not None:
                return _image_response(proxied, allow_proxy=False)
        if captcha:
            raise SearxEngineCaptchaException()
        return []

    try:
        results = _image_parse(resp)
        if allow_proxy:
            report_yandex_route_result(resp, True)
        return results
    except Exception:
        if allow_proxy:
            report_yandex_route_result(resp, False)
            proxied = fetch_yandex_via_free_proxy(
                str(resp.request.url),
                resp.request.headers,
                ('initialState', 'serpList'),
            )
            if proxied is not None:
                try:
                    return _image_parse(proxied)
                except Exception:
                    pass
        return []


def response(resp):
    if search_type == 'web':
        catch_bad_response(resp)

        dom = html.fromstring(resp.text)

        results = []

        for result in eval_xpath_list(dom, results_xpath):
            results.append(
                {
                    'url': extract_text(eval_xpath(result, url_xpath)),
                    'title': extract_text(eval_xpath(result, title_xpath)),
                    'content': extract_text(eval_xpath(result, content_xpath)),
                }
            )

        return results

    if search_type == 'images':
        return _image_response(resp)

    return []
