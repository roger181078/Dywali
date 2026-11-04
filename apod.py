#!/usr/bin/env python3
# Copyright (C) 2026 roger181078
# SPDX-License-Identifier: GPL-3.0-only
"""NASA APOD provider for Dywali."""
import json
import email.utils
import fcntl
import os
import shutil
import socket
import tempfile
import time
import urllib.parse
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path
from PIL import Image

API = 'https://science.nasa.gov/wp-json/wp/v2/apod-basic/'
CACHE = Path.home() / '.cache/dywali/apod'
STATE = Path.home() / '.local/state/dywali/apod.json'
LOCK = STATE.with_suffix('.lock')
USER_AGENT = 'Dywali/1.0.1 (NASA APOD wallpaper source)'
FREQUENCIES = (720, 1440)
API_KEY = os.environ.get('DYWALI_NASA_API_KEY', 'DEMO_KEY')
LOOKBACK_DAYS = 7
MAX_IMAGE_PIXELS = 80_000_000
RATE_LIMIT_FALLBACK = 15 * 60
RETRY_BASE = 60
RETRY_MAX = 6 * 60 * 60
NASA_HOSTS = frozenset({
    'nasa.gov', 'www.nasa.gov', 'science.nasa.gov', 'apod.nasa.gov',
    'images.nasa.gov', 'images-assets.nasa.gov', 'assets.science.nasa.gov',
})


class ApodRetryScheduled(RuntimeError):
    """A transient failure or server limit has scheduled the next attempt."""
    def __init__(self, retry_at, message):
        self.retry_at = float(retry_at)
        remaining = max(1, int(self.retry_at - time.time() + 0.999))
        super().__init__(f'{message} Retry in {remaining} s.')


class ApodURLRejected(ValueError):
    pass


def _valid_nasa_url(url):
    if not isinstance(url, str):
        return False
    try:
        parsed = urllib.parse.urlsplit(url)
        host = (parsed.hostname or '').lower()
        return (parsed.scheme == 'https' and host in NASA_HOSTS and
                not parsed.username and not parsed.password and
                parsed.port in (None, 443))
    except ValueError:
        return False


def _looks_like_article(url, permalink=''):
    parsed = urllib.parse.urlsplit(url)
    path = parsed.path.lower()
    if path.endswith(('.html', '.htm')) or path.endswith('/'):
        return True
    if '/wp-json/' in path or ('/apod/ap' in path and path.endswith('.html')):
        return True
    if permalink and _valid_nasa_url(permalink):
        page = urllib.parse.urlsplit(permalink)
        return (parsed.scheme, parsed.netloc, parsed.path) == (page.scheme, page.netloc, page.path)
    return False


class _NasaRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Follow only HTTPS redirects within the explicit NASA host policy."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not _valid_nasa_url(newurl):
            raise ApodURLRejected('NASA APOD redirect target is not an approved HTTPS host')
        old = urllib.parse.urlsplit(req.full_url)
        new = urllib.parse.urlsplit(newurl)
        if ('api_key' in urllib.parse.parse_qs(old.query) and
                (old.scheme, old.netloc) != (new.scheme, new.netloc)):
            raise ApodURLRejected('NASA APOD API-key redirect must remain on the original host')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _open_request(request, timeout):
    return urllib.request.build_opener(_NasaRedirectHandler()).open(request, timeout=timeout)


def _retry_delay(value):
    if value:
        try:
            return max(1, min(86400, int(value.strip())))
        except (AttributeError, ValueError):
            try:
                when = email.utils.parsedate_to_datetime(value)
                if when.tzinfo is None:
                    when = when.replace(tzinfo=timezone.utc)
                return max(1, min(86400, int(when.timestamp() - time.time() + 1)))
            except (TypeError, ValueError, OverflowError):
                pass
    return RATE_LIMIT_FALLBACK


def _schedule_retry(*, retry_after=None):
    state = load_state()
    now = time.time()
    if retry_after is not None:
        retry_at = max(float(state.get('blocked_until') or 0),
                       float(state.get('retry_at') or 0), now + _retry_delay(retry_after))
        state['blocked_until'] = retry_at
        message = 'NASA APOD rate limit reached.'
    else:
        failures = min(10, int(state.get('transient_failures') or 0) + 1)
        delay = min(RETRY_MAX, RETRY_BASE * (2 ** (failures - 1)))
        retry_at = now + delay
        state['transient_failures'] = failures
        state['retry_at'] = retry_at
        message = 'Temporary NASA APOD service failure.'
    state['error'] = message
    state['error_at'] = now
    save_state(state)
    raise ApodRetryScheduled(retry_at, message)


class ApodVideoOnly(RuntimeError):
    pass


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


def _request(url, limit, accept='application/json'):
    if not _valid_nasa_url(url):
        raise ApodURLRejected('NASA APOD URL is not an approved HTTPS host')
    req = urllib.request.Request(url, headers={
        'User-Agent': USER_AGENT,
        'Accept': accept,
    })
    try:
        response = _open_request(req, timeout=25)
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            _schedule_retry(retry_after=exc.headers.get('Retry-After') if exc.headers else None)
        if 500 <= exc.code <= 599:
            _schedule_retry()
        raise RuntimeError(f'NASA APOD returned HTTP {exc.code}.') from None
    except (urllib.error.URLError, TimeoutError, socket.timeout, ConnectionError):
        _schedule_retry()
    with response:
        final_url = response.geturl()
        if not _valid_nasa_url(final_url):
            raise ApodURLRejected('NASA APOD final URL is not an approved HTTPS host')
        content_type = response.headers.get_content_type() if response.headers else ''
        chunks = []
        size = 0
        while True:
            chunk = response.read(min(65536, limit - size + 1))
            if not chunk:
                break
            size += len(chunk)
            if size > limit:
                raise ValueError('NASA APOD response is too large')
            chunks.append(chunk)
    return b''.join(chunks), final_url, content_type


def _request_json(url, limit):
    raw, _final_url, content_type = _request(url, limit)
    if content_type and content_type not in ('application/json', 'application/ld+json'):
        raise ValueError('NASA APOD returned a non-JSON response')
    payload = json.loads(raw.decode('utf-8'))
    if not isinstance(payload, (list, dict)):
        raise ValueError('NASA APOD returned an unexpected response')
    return payload


def _today():
    return datetime.now(timezone.utc).date()


def _parse_item(payload, expected_date):
    if not isinstance(payload, dict) or payload.get('date') != expected_date:
        raise ValueError('NASA APOD response date did not match the requested date')
    media_type = str(payload.get('media_type') or '')
    page = payload.get('permalink') or ''
    if page and not _valid_nasa_url(page):
        page = ''
    hdurl = str(payload.get('hdurl') or '')
    url = str(payload.get('url') or '')
    candidates = []
    if media_type == 'image':
        for candidate in (hdurl, url):
            if (candidate and _valid_nasa_url(candidate) and not _looks_like_article(candidate, page)
                    and candidate not in candidates):
                candidates.append(candidate)
    item = {
        'date': expected_date,
        'title': str(payload.get('title') or ''),
        'explanation': str(payload.get('explanation') or ''),
        'credit': str(payload.get('credit') or ''),
        'copyright': str(payload.get('copyright') or ''),
        'media_type': media_type,
        'page': page,
        'image_url': hdurl if hdurl and _valid_nasa_url(hdurl) else '',
        'url': '',
        'source_url': '',
        'download_url': '',
        'candidate_urls': candidates,
    }
    return item


def _fetch_date(day):
    params = {'date': day.isoformat(), 'api_key': API_KEY}
    payload = _request_json(API + '?' + urllib.parse.urlencode(params), 2 * 1024 * 1024)
    records = payload if isinstance(payload, list) else [payload]
    for record in records:
        if not isinstance(record, dict) or record.get('date') != day.isoformat():
            continue
        try:
            item = _parse_item(record, day.isoformat())
            if not item['media_type']:
                raise ValueError('NASA APOD did not provide media_type')
            return item
        except ValueError as exc:
            continue
    raise ValueError('NASA APOD returned no valid entry for the requested date')


def _download_image(urls, target):
    """Try approved high-resolution/normal URLs and return source and final URLs."""
    errors = []
    for url in dict.fromkeys(urls):
        if not _valid_nasa_url(url) or _looks_like_article(url):
            continue
        temp = None
        staged = None
        try:
            raw, final_url, content_type = _request(url, 45 * 1024 * 1024, accept='image/*,*/*')
            if content_type == 'text/html':
                raise ValueError('NASA APOD candidate returned an HTML page, not an image')
            CACHE.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=CACHE, suffix='.img', delete=False) as tmp:
                tmp.write(raw)
                temp = Path(tmp.name)
            with Image.open(temp) as image:
                if image.width < 320 or image.height < 180 or image.width * image.height < 120_000:
                    raise ValueError(f'image too small ({image.width}x{image.height})')
                if image.width * image.height > MAX_IMAGE_PIXELS:
                    raise ValueError('image dimensions exceed the APOD safety limit')
                image.verify()
            with Image.open(temp) as image:
                # Valid small photographs are preferable to a stale wallpaper.
                rgb = image.convert('RGB')
                with tempfile.NamedTemporaryFile(dir=CACHE, suffix='.jpg', delete=False) as out:
                    staged = Path(out.name)
                rgb.save(staged, format='JPEG', quality=93)
            os.replace(staged, target)
            return url, final_url
        except ApodRetryScheduled:
            raise
        except Exception as exc:
            errors.append(str(exc)[:200])
        finally:
            if temp is not None:
                temp.unlink(missing_ok=True)
            if staged is not None:
                staged.unlink(missing_ok=True)
    raise ValueError('NASA APOD: no usable image version: ' + '; '.join(errors[-3:]))


def _recent_image_candidates(today):
    for offset in range(LOOKBACK_DAYS + 1):
        day = today - timedelta(days=offset)
        try:
            item = _fetch_date(day)
        except ApodRetryScheduled:
            raise
        except Exception:
            if offset == 0:
                raise
            continue
        if item['media_type'] == 'image' and item['candidate_urls']:
            if offset:
                item['notice_code'] = 'recent_fallback'
                item['notice'] = f'The newest APOD entries did not have a usable image. Showing {item["date"]}.'
            yield item


def _persist_image_and_state(staged_image, target, state):
    """Commit image/state together; roll the image back if state persistence fails."""
    backup = None
    if target.is_file():
        with tempfile.NamedTemporaryFile(dir=CACHE, suffix='.backup', delete=False) as file:
            backup = Path(file.name)
        shutil.copy2(target, backup)
    try:
        os.replace(staged_image, target)
        save_state(state)
    except Exception:
        if backup is not None:
            os.replace(backup, target)
            backup = None
        else:
            target.unlink(missing_ok=True)
        raise
    finally:
        if backup is not None:
            backup.unlink(missing_ok=True)


def refresh(minutes=1440, force=False):
    minutes = int(minutes)
    if minutes not in FREQUENCIES:
        raise ValueError('Unsupported NASA APOD interval')
    STATE.parent.mkdir(parents=True, exist_ok=True)
    # Serialize GUI and systemd refreshes so both observe persisted cooldowns.
    with LOCK.open('a+b') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return _refresh_locked(minutes, force)


def _refresh_locked(minutes, force):
    data = load_state()
    path = CACHE / 'current.jpg'
    now = time.time()
    if path.is_file() and not force and now - float(data.get('checked_at', 0)) < minutes * 60:
        return path, data, False
    retry_at = max(float(data.get('blocked_until') or 0), float(data.get('retry_at') or 0))
    if retry_at > now:
        message = data.get('error') or 'NASA APOD request is temporarily delayed.'
        if path.is_file():
            return path, data, False
        raise RuntimeError(message)
    try:
        previous_source_url = str(data.get('source_url') or data.get('url') or '')
        failures = []
        for item in _recent_image_candidates(_today()):
            staged_image = None
            try:
                if (not path.is_file() or item.get('date') != data.get('date')
                        or not previous_source_url or previous_source_url not in item['candidate_urls']):
                    CACHE.mkdir(parents=True, exist_ok=True)
                    with tempfile.NamedTemporaryFile(dir=CACHE, suffix='.jpg', delete=False) as tmp:
                        staged_image = Path(tmp.name)
                    source_url, chosen_url = _download_image(item['candidate_urls'], staged_image)
                    changed = True
                else:
                    source_url = previous_source_url
                    chosen_url = str(data.get('download_url') or data.get('url') or '')
                    changed = False
                item.pop('candidate_urls', None)
                item['url'] = chosen_url
                item['source_url'] = source_url
                item['download_url'] = chosen_url
                item['candidate_url'] = source_url
                item['checked_at'] = now
                item['error'] = ''
                item['error_at'] = 0
                item.pop('blocked_until', None)
                item.pop('retry_at', None)
                item.pop('transient_failures', None)
                state = {**data, **item}
                for key in ('blocked_until', 'retry_at', 'transient_failures'):
                    state.pop(key, None)
                if changed:
                    _persist_image_and_state(staged_image, path, state)
                    staged_image = None
                else:
                    save_state(state)
                return path, state, changed
            except ApodRetryScheduled:
                raise
            except Exception as image_exc:
                failures.append(f'{item.get("date", "?")}: {str(image_exc)[:200]}')
            finally:
                if staged_image is not None:
                    staged_image.unlink(missing_ok=True)
        raise RuntimeError('No usable NASA APOD image in recent days. ' + ' | '.join(failures[:3]))
    except Exception as exc:
        if isinstance(exc, ApodRetryScheduled):
            data = load_state()
        else:
            try:
                _schedule_retry()
            except ApodRetryScheduled as scheduled:
                exc = scheduled
                data = load_state()
        data['error'] = str(exc)[:500]
        data['error_at'] = now
        save_state(data)
        if path.is_file():
            return path, data, False
        raise RuntimeError('NASA APOD unavailable and no cached image: ' + str(exc)[:500]) from None
