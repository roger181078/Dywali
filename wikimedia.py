#!/usr/bin/env python3
# Copyright (C) 2026 roger181078
# SPDX-License-Identifier: GPL-3.0-only
"""Experimental Wikimedia Commons source.

Supports two behaviors:
- picture of the day
- random featured image
"""
import json
import os
import random
import re
import time
import tempfile
import html as html_lib
from html.parser import HTMLParser
import urllib.parse
import urllib.request
import urllib.error
import fcntl
import email.utils
import math
from datetime import datetime, timezone
from pathlib import Path
from PIL import Image

FEED = 'https://api.wikimedia.org/feed/v1/wikipedia/commons/featured'
COMMONS_API = 'https://commons.wikimedia.org/w/api.php'
CACHE = Path.home() / '.cache/dywali/wikimedia'
STATE = Path.home() / '.local/state/dywali/wikimedia.json'
FREQUENCIES = (60, 360, 720, 1440)
# Configure a genuine project contact before public distribution.
CONTACT = os.environ.get('DYWALI_CONTACT', '').strip()
USER_AGENT = 'Dywali/1.0.1 (personal XFCE wallpaper client' + ('; ' + CONTACT if CONTACT else '') + ')'
MANUAL_COOLDOWN = 30  # seconds; local UX throttle, not a Wikimedia quota
RATE_LIMIT_FALLBACK = 15 * 60
LOCK = STATE.with_suffix('.lock')
CATALOG = CACHE / 'featured-catalog.json'
CATALOG_MAX_AGE = 30 * 24 * 3600
FEATURED_CATEGORY = 'Category:Featured pictures on Wikimedia Commons'
PNG_SIGNATURE = b'\x89PNG\r\n\x1a\n'
BEHAVIORS = ('potd', 'featured')
RECENT_LIMIT = 40
RASTER_EXTS = ('.jpg', '.jpeg', '.png', '.tif', '.tiff', '.webp', '.bmp')
def load_state():
    try:
        return json.loads(STATE.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def save_state(data):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix('.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(tmp, STATE)


class WikimediaCooldown(RuntimeError):
    """Do not issue further requests until Wikimedia's limit expires."""
    def __init__(self, retry_at):
        self.retry_at = float(retry_at)
        remaining = max(1, math.ceil(self.retry_at - time.time()))
        super().__init__(f'Consulta pausada. Vuelve a intentarlo dentro de {remaining} s.')


def _retry_delay(value):
    if value:
        try:
            return max(1, min(86400, int(value)))
        except ValueError:
            try:
                when = email.utils.parsedate_to_datetime(value)
                return max(1, min(86400, int(when.timestamp() - time.time() + 1)))
            except (TypeError, ValueError):
                pass
    return RATE_LIMIT_FALLBACK


def _cooldown_deadline(data, force):
    """Return the latest deadline applicable to this kind of refresh."""
    deadlines = [float(data.get('blocked_until') or 0)]
    if force:
        deadlines.append(float(data.get('manual_until') or 0))
    return max(deadlines)


def _check_throttle(data, force, now):
    until = _cooldown_deadline(data, force)
    if until > now:
        raise WikimediaCooldown(until)


def _request(url, limit, accept='application/json'):
    req = urllib.request.Request(url, headers={
        'User-Agent': USER_AGENT,
        'Accept': accept,
    })
    try:
        response = urllib.request.urlopen(req, timeout=25)
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            retry_after = exc.headers.get('Retry-After') if exc.headers else None
            delay = _retry_delay(retry_after)
            state = load_state()
            state['blocked_until'] = max(float(state.get('blocked_until') or 0), time.time() + delay)
            state['error'] = f'HTTP 429. Wikimedia solicita esperar {delay} s.'
            save_state(state)
            raise WikimediaCooldown(state['blocked_until']) from exc
        raise
    with response:
        chunks = []
        size = 0
        while True:
            chunk = response.read(min(65536, limit - size + 1))
            if not chunk:
                break
            size += len(chunk)
            if size > limit:
                raise ValueError('Wikimedia response is too large')
            chunks.append(chunk)
    return b''.join(chunks)


def _request_json(url, limit):
    return json.loads(_request(url, limit).decode('utf-8'))


def _clean(text):
    if isinstance(text, dict):
        text = text.get('text') or text.get('value') or text.get('en') or ''
        if isinstance(text, dict):
            text = next((value for value in text.values() if isinstance(value, str)), '')
    if text is None:
        return ''
    text = html_lib.unescape(str(text))
    text = re.sub(r'<[^>]+>', ' ', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text


class _LinkParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.hrefs = []

    def handle_starttag(self, tag, attrs):
        if tag.casefold() == 'a':
            href = dict(attrs).get('href')
            if href:
                self.hrefs.append(href)


def _metadata_value(value):
    if isinstance(value, dict):
        return value.get('value') or value.get('text') or ''
    return value or ''


def _validated_https_url(value, hosts=None):
    value = _clean(_metadata_value(value))
    try:
        parsed = urllib.parse.urlsplit(value)
        host = (parsed.hostname or '').lower()
        if (parsed.scheme != 'https' or not host or parsed.username or parsed.password or
                parsed.port not in (None, 443)):
            return ''
        if hosts and not any(host == allowed or host.endswith('.' + allowed) for allowed in hosts):
            return ''
        return urllib.parse.urlunsplit(parsed)
    except ValueError:
        return ''


def _valid_url(url):
    return bool(_validated_https_url(url, ('wikimedia.org',)))


def _is_png_text_limit_error(raw, exc):
    return (raw.startswith(PNG_SIGNATURE) and isinstance(exc, ValueError) and
            'PngImagePlugin.MAX_TEXT_CHUNK' in str(exc))


def _store_valid_image(raw, path):
    """Validate and convert into a sibling temp file before replacing current.jpg."""
    CACHE.mkdir(parents=True, exist_ok=True)
    source_suffix = '.png' if raw.startswith(PNG_SIGNATURE) else '.img'
    with tempfile.NamedTemporaryFile(dir=CACHE, suffix=source_suffix, delete=False) as source_file:
        source_file.write(raw)
        source_path = Path(source_file.name)
    output_path = None
    try:
        with Image.open(source_path) as image:
            image.verify()
        with Image.open(source_path) as image:
            if image.width < 800 or image.height < 450:
                raise ValueError('Image resolution too small')
            converted = image.convert('RGB')
        with tempfile.NamedTemporaryFile(dir=CACHE, suffix='.jpg', delete=False) as output_file:
            output_path = Path(output_file.name)
        converted.save(output_path, quality=93)
        os.replace(output_path, path)
    finally:
        source_path.unlink(missing_ok=True)
        if output_path is not None:
            output_path.unlink(missing_ok=True)


def _file_name(value):
    value = _clean(value)
    return value[5:].strip() if value.casefold().startswith('file:') else value


def _commons_page(value, filename=''):
    page = _validated_https_url(value, ('commons.wikimedia.org',))
    if page and urllib.parse.urlsplit(page).hostname.lower() == 'commons.wikimedia.org':
        return page
    title = _clean(value)
    if not title.casefold().startswith('file:') and filename:
        title = 'File:' + filename
    if title.casefold().startswith('file:'):
        return 'https://commons.wikimedia.org/wiki/' + urllib.parse.quote(title.replace(' ', '_'), safe='/:()')
    return ''


def _normalize_metadata(*sources):
    """Return a stable, plain-text metadata record; absent fields stay unknown."""
    result = {
        'title': '', 'original_filename': '', 'author': '', 'credit': '',
        'description': '', 'license': '', 'license_url': '', 'page_url': '',
        'image_url': '', 'date': '',
    }
    for source in sources:
        if not isinstance(source, dict):
            continue
        meta = source.get('extmetadata') or {}
        filename = _file_name(source.get('original_filename') or source.get('file_name') or
                              source.get('file_title'))
        values = {
            'title': source.get('title') or _metadata_value(meta.get('ObjectName')),
            'original_filename': filename,
            'author': source.get('author') or _metadata_value(meta.get('Artist')),
            'credit': source.get('credit') or _metadata_value(meta.get('Credit')),
            'description': source.get('description') or _metadata_value(meta.get('ImageDescription')),
            'license': source.get('license') or _metadata_value(meta.get('LicenseShortName')) or
                       _metadata_value(meta.get('UsageTerms')),
            'license_url': source.get('license_url') or _metadata_value(meta.get('LicenseUrl')),
            'page_url': (source.get('page_url') or source.get('page') or
                         source.get('descriptionurl') or ('File:' + filename if filename else '')),
            'image_url': source.get('image_url') or source.get('url') or source.get('thumburl'),
            'date': source.get('date'),
        }
        for key, value in values.items():
            if result[key] or not value:
                continue
            if key == 'original_filename':
                result[key] = _clean(value)
            elif key in ('page_url', 'image_url', 'license_url'):
                if key == 'page_url':
                    result[key] = _commons_page(value, filename)
                elif key == 'image_url':
                    result[key] = _validated_https_url(value, ('wikimedia.org',))
                else:
                    result[key] = _validated_https_url(value)
            elif key == 'date':
                result[key] = _clean(value)
            else:
                result[key] = _clean(value)
        # Some Commons responses include the license URL as an anchor in the
        # display value. Preserve only its separately validated URL, never HTML.
        if not result['license_url']:
            raw_license = _metadata_value(meta.get('LicenseShortName')) or _metadata_value(meta.get('UsageTerms'))
            if isinstance(raw_license, str) and '<' in raw_license:
                parser = _LinkParser()
                try:
                    parser.feed(raw_license)
                except Exception:
                    pass
                for href in parser.hrefs:
                    result['license_url'] = _validated_https_url(href)
                    if result['license_url']:
                        break
    result['url'] = result['image_url']  # Compatibility with older state readers.
    return result


def _legacy_credit(item):
    """Keep the existing UI's credit line useful when Commons only has Artist."""
    if not item.get('credit') and item.get('author'):
        item['credit'] = item['author']
    return item


def _today():
    return datetime.now(timezone.utc).date()


def _from_feed(day):
    url = f'{FEED}/{day:%Y/%m/%d}'
    payload = _request_json(url, 2 * 1024 * 1024)
    image = payload.get('image') or {}
    candidates = []
    for key in ('originalimage', 'image', 'thumbnail'):
        value = image.get(key)
        if isinstance(value, str):
            candidates.append(value)
        elif isinstance(value, dict):
            candidates.extend([value.get('source'), value.get('url')])
    picture_url = next((u for u in candidates if _valid_url(u)), '')
    if not picture_url:
        raise ValueError('Wikimedia feed did not provide a usable image URL')
    if isinstance(image.get('file_page'), dict):
        file_page = image['file_page']
        page = file_page.get('url') or ''
        filename = _file_name(file_page.get('title') or '')
    else:
        page = image.get('file_page') or ''
        filename = ''
    feed_item = _normalize_metadata({
        'url': picture_url,
        'title': _clean(image.get('title')),
        'description': _clean(image.get('description')),
        'author': _clean(image.get('artist')),
        'credit': _clean(image.get('credit')),
        'license': _clean(image.get('license')),
        'page': page,
        'original_filename': filename,
        'date': day.isoformat(),
        'source': 'feed',
        'mode': 'potd',
    })
    thumbnail = image.get('thumbnail')
    if isinstance(thumbnail, str):
        fallback_urls = [thumbnail]
    elif isinstance(thumbnail, dict):
        fallback_urls = [thumbnail.get('source'), thumbnail.get('url')]
    else:
        fallback_urls = []
    # Keep the validated historical request sequence: the feed already has
    # enough metadata and its thumbnail is available as a PNG-safe fallback.
    fallback_urls = list(dict.fromkeys(
        url for url in fallback_urls if url and url != picture_url and _valid_url(url)))
    return _legacy_credit({**feed_item, 'source': 'feed', 'mode': 'potd',
                           'date': day.isoformat(), 'fallback_image_urls': fallback_urls})


def _imageinfo(filename):
    params = {
        'action': 'query', 'format': 'json', 'formatversion': '2',
        'prop': 'imageinfo', 'titles': filename,
        'iiprop': 'url|extmetadata', 'iiurlwidth': '2560',
    }
    payload = _request_json(COMMONS_API + '?' + urllib.parse.urlencode(params), 2 * 1024 * 1024)
    page = (payload.get('query', {}).get('pages') or [{}])[0]
    info = (page.get('imageinfo') or [{}])[0]
    meta = info.get('extmetadata') or {}
    original_url = info.get('url') or ''
    thumbnail_url = info.get('thumburl') or ''
    if not _valid_url(original_url):
        raise ValueError('Wikimedia imageinfo did not provide a valid image URL')
    title = _clean(_metadata_value(meta.get('ObjectName'))) or _file_name(page.get('title', ''))
    result = _normalize_metadata({
        'url': original_url,
        'title': title,
        'original_filename': page.get('title', ''),
        'extmetadata': meta,
        'page': info.get('descriptionurl') or '',
    })
    if not result['page_url']:
        result['page_url'] = _commons_page('', result['original_filename'])
    result['thumbnail_url'] = (thumbnail_url if _valid_url(thumbnail_url) and
                               thumbnail_url != original_url else '')
    return result


def _from_action(day):
    date_iso = day.isoformat()
    candidates = [
        f'Template:Potd/{date_iso} (en)',
        f'Template:Potd/{date_iso}',
        f'Commons:Picture of the day/{date_iso}',
        f'Commons:Picture of the day/{date_iso}/en',
    ]
    filename = ''
    for title in candidates:
        params = {
            'action': 'query', 'format': 'json', 'formatversion': '2',
            'prop': 'images', 'titles': title,
        }
        payload = _request_json(COMMONS_API + '?' + urllib.parse.urlencode(params), 2 * 1024 * 1024)
        page = (payload.get('query', {}).get('pages') or [{}])[0]
        images = page.get('images') or []
        if images:
            filename = images[0].get('title') or ''
            if filename:
                break
    if not filename:
        raise ValueError('Could not resolve Wikimedia Commons Picture of the Day')
    item = _imageinfo(filename)
    item.update({'date': date_iso, 'source': 'action', 'mode': 'potd'})
    thumbnail_url = item.get('thumbnail_url')
    # imageinfo historically selected thumburl as the image to download.
    # Preserve the original URL for metadata, but do not retry it after the
    # thumbnail: the API-sized thumbnail is the primary representation here.
    item['download_url'] = thumbnail_url or item.get('image_url')
    item['fallback_image_urls'] = []
    return _legacy_credit(_normalize_metadata(item) | {
        'date': item['date'], 'source': item['source'], 'mode': item['mode'],
        'thumbnail_url': item.get('thumbnail_url', ''),
        'download_url': item['download_url'],
        'fallback_image_urls': item['fallback_image_urls'],
    })


def _fetch_potd(day):
    errors = []
    for getter in (_from_feed, _from_action):
        try:
            return getter(day)
        except WikimediaCooldown:
            raise
        except Exception as exc:
            errors.append(str(exc))
    raise RuntimeError(' / '.join(errors) if errors else 'Wikimedia unavailable')


def _load_catalog():
    try:
        return json.loads(CATALOG.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def _save_catalog(data):
    CATALOG.parent.mkdir(parents=True, exist_ok=True)
    tmp = CATALOG.with_suffix('.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(tmp, CATALOG)


def _choose_featured_title(previous_title=''):
    """Choose from one random slice of Commons featured category.

    Unlike a full traversal of ~22k files this takes a single API request.
    Sampling by sortkey prefix isn't perfectly uniform but is fast and useful
    for a desktop wallpaper shuffle.
    """
    state = load_state()
    recent = [t for t in state.get('recent_titles', []) if isinstance(t, str)]
    excluded = set(recent[-RECENT_LIMIT:])
    if previous_title:
        excluded.add(previous_title)
    rng = random.SystemRandom()
    starts = '0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ'
    for attempt in range(5):
        start = rng.choice(starts)
        params = {
            'action': 'query', 'format': 'json', 'formatversion': '2',
            'list': 'categorymembers', 'cmtitle': FEATURED_CATEGORY,
            'cmtype': 'file', 'cmlimit': '300',
            'cmstartsortkeyprefix': start,
        }
        payload = _request_json(COMMONS_API + '?' + urllib.parse.urlencode(params), 2 * 1024 * 1024)
        if payload.get('error'):
            raise RuntimeError('Wikimedia API: ' + str(payload['error']))
        members = payload.get('query', {}).get('categorymembers') or []
        candidates = [m.get('title') for m in members
                      if isinstance(m.get('title'), str) and
                         m['title'].startswith('File:') and
                         m['title'].lower().endswith(RASTER_EXTS)]
        eligible = [t for t in candidates if t not in excluded]
        if eligible:
            return rng.choice(eligible), recent
    raise RuntimeError('Wikimedia featured category returned no suitable new image')


def _fetch_featured_random(previous_title=''):
    title, recent = _choose_featured_title(previous_title)
    item = _imageinfo(title)
    # Featured images historically download the API-sized thumbnail first;
    # keep that behavior while retaining image_url as the original file URL.
    item['download_url'] = item.get('thumbnail_url') or item.get('image_url')
    recent.append(title)
    item.update({
        'date': _today().isoformat(),
        'source': 'featured-catalog',
        'mode': 'featured',
        'file_title': title,
        'recent_titles': recent[-RECENT_LIMIT:],
    })
    return _legacy_credit(_normalize_metadata(item) | {
        'date': item['date'], 'source': item['source'], 'mode': item['mode'],
        'file_title': title, 'recent_titles': item['recent_titles'],
        'download_url': item['download_url'],
    })


def refresh(minutes=1440, force=False, behavior='potd'):
    minutes = int(minutes)
    if minutes not in FREQUENCIES:
        raise ValueError('Unsupported Wikimedia interval')
    if behavior not in BEHAVIORS:
        raise ValueError('Unsupported Wikimedia behavior')
    STATE.parent.mkdir(parents=True, exist_ok=True)
    # Same lock for the GUI and the systemd user timer: no parallel queries.
    with LOCK.open('a+b') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return _refresh_locked(minutes, force, behavior)


def _refresh_locked(minutes, force, behavior):
    data = load_state()
    now = time.time()
    path = CACHE / 'current.jpg'
    _check_throttle(data, force, now)
    same_mode = data.get('mode', 'potd') == behavior
    if path.is_file() and not force and same_mode and now - float(data.get('checked_at', 0)) < minutes * 60:
        return path, data, False
    if force:
        # Reserve a slot before the request to block repeated manual clicks,
        # even when network errors occur.
        data['manual_until'] = now + MANUAL_COOLDOWN
        save_state(data)
    try:
        if behavior == 'featured':
            item = _fetch_featured_random(data.get('file_title', '') if same_mode else '')
        else:
            item = _fetch_potd(_today())
        previous = (data.get('image_url') or data.get('url')) if same_mode else ''
        image_url = item.get('image_url') or item.get('url')
        if not image_url or not _valid_url(image_url):
            raise ValueError('Wikimedia metadata did not contain a valid image URL')
        stored_download_url = (data.get('download_url')
                               if same_mode and image_url == previous else '')
        if not _valid_url(stored_download_url):
            stored_download_url = ''
        preferred_download_url = item.get('download_url')
        if not _valid_url(preferred_download_url):
            preferred_download_url = ''
        download_url = preferred_download_url or stored_download_url or image_url
        if image_url != previous or not path.is_file() or not same_mode:
            alternatives = item.get('fallback_image_urls') or []
            if not isinstance(alternatives, (list, tuple)):
                alternatives = []
            urls = list(dict.fromkeys(
                url for url in [download_url, *alternatives]
                if url and url != download_url and _valid_url(url)))
            urls.insert(0, download_url)
            original_png_rejected = False
            for index, candidate_url in enumerate(urls):
                raw = _request(candidate_url, 35 * 1024 * 1024, accept='image/*,*/*')
                try:
                    _store_valid_image(raw, path)
                except Exception as exc:
                    has_fallback = index + 1 < len(urls)
                    is_png_text_limit = _is_png_text_limit_error(raw, exc)
                    if index == 0 and has_fallback and is_png_text_limit:
                        original_png_rejected = True
                        continue
                    if behavior == 'potd' and index == 0 and is_png_text_limit:
                        raise ValueError(
                            'Wikimedia POTD PNG metadata is too large and no valid thumbnail alternative is available.'
                        ) from exc
                    if behavior == 'potd' and original_png_rejected:
                        raise ValueError(
                            'Wikimedia POTD original PNG was rejected and its thumbnail also failed validation.'
                        ) from exc
                    raise
                download_url = candidate_url
                break
            else:
                raise ValueError('Wikimedia did not provide a compatible image representation')
        recent_titles = item.get('recent_titles')
        item = _normalize_metadata(item)
        item.update({
            'mode': behavior,
            'source': item.get('source') or ('featured-catalog' if behavior == 'featured' else 'feed'),
            'download_url': download_url,
            'checked_at': now,
            'checked_at_iso': datetime.fromtimestamp(now, timezone.utc).isoformat(),
            'error': '',
        })
        if recent_titles:
            item['recent_titles'] = recent_titles[-RECENT_LIMIT:]
        # Keep throttle values and featured-image history, not fields belonging
        # to whichever previous image or mode happened to occupy the state file.
        persisted = {key: data[key] for key in ('blocked_until', 'manual_until') if key in data}
        history = data.get('recent_titles')
        if isinstance(history, list):
            persisted['recent_titles'] = [title for title in history if isinstance(title, str)][-RECENT_LIMIT:]
        if item.get('recent_titles'):
            persisted['recent_titles'] = item['recent_titles']
        state = {**persisted, **item}
        save_state(state)
        return path, state, image_url != previous or not same_mode
    except Exception as exc:
        data = load_state()  # Preserve blocked_until written by _request on HTTP 429.
        if isinstance(exc, WikimediaCooldown):
            # A manual request must wait for every active gate; scheduled
            # refreshes deliberately ignore the manual-click cooldown.
            until = _cooldown_deadline(data, force)
            if until > time.time():
                exc = WikimediaCooldown(until)
        data['error'] = str(exc)
        data['error_at'] = now
        save_state(data)
        if path.is_file() and data.get('mode', 'potd') == behavior and not force:
            return path, data, False
        if isinstance(exc, WikimediaCooldown):
            raise exc
        raise RuntimeError('Wikimedia: ' + str(exc)) from exc
