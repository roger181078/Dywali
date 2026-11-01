#!/usr/bin/env python3
# Copyright (C) 2026 roger181078
# SPDX-License-Identifier: GPL-3.0-only
"""Motor de Dywali: no toca los scripts Monterey/Dome existentes."""
import json
import sys
from datetime import datetime
from pathlib import Path
from i18n import _
from PIL import Image, ImageOps
from slideshow import next_image
from displays import apply_to_targets, cleanup_unused_wallpapers, discover_targets
from distribution import provider_available
import providers

CONFIG = Path.home()/'.config/dywali/config.json'
OUTPUT = Path.home()/'.cache/dywali'

def load_config():
    if not CONFIG.is_file():
        raise RuntimeError(_('No configuration found. Open the graphical application first.'))
    return json.loads(CONFIG.read_text(encoding='utf-8'))

def save_config(data):
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG.with_suffix('.tmp')
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding='utf-8')
    tmp.replace(CONFIG)

def validate_frames(frames):
    if len(frames) < 2:
        raise ValueError(_('At least two images are required.'))
    times = []
    for frame in frames:
        try:
            h, m = map(int, frame['time'].split(':'))
            assert 0 <= h < 24 and 0 <= m < 60
        except (ValueError, AssertionError, KeyError) as exc:
            raise ValueError(_('Invalid time (HH:MM format).')) from exc
        path = Path(frame['path']).expanduser()
        if not path.is_file():
            raise ValueError(_('Image not found: {path}').format(path=path))
        if frame.get('mode', 'blend') not in ('blend', 'hold'):
            raise ValueError(_('Invalid interval mode.'))
        times.append(h*60+m)
    if len(set(times)) != len(times):
        raise ValueError(_('Two images cannot have the same time.'))

def render(frames, minute, preview=False):
    """Interpolación circular: último fotograma -> primero del día siguiente."""
    validate_frames(frames)
    ordered = sorted(frames, key=lambda f: f['time'])
    minutes = [int(f['time'][:2])*60+int(f['time'][3:]) for f in ordered]
    idx = len(minutes)-1
    for i, t in enumerate(minutes):
        if minute >= t:
            idx = i
    nextidx = (idx+1) % len(minutes)
    start = minutes[idx]
    end = minutes[nextidx] + (1440 if nextidx == 0 else 0)
    curr = minute + (1440 if minute < start else 0)
    ratio = (curr-start)/(end-start) if ordered[idx].get('mode', 'blend') == 'blend' else 0.0
    def open_image(path):
        with Image.open(path) as src:
            rgb = ImageOps.exif_transpose(src).convert('RGB')
            if preview:
                rgb.thumbnail((920, 540), Image.Resampling.LANCZOS)
            return rgb
    a = open_image(ordered[idx]['path'])
    if ratio == 0.0:
        return a, (idx, nextidx, ratio)
    b = open_image(ordered[nextidx]['path'])
    if a.size != b.size:
        if preview:
            b = b.resize(a.size, Image.Resampling.LANCZOS)
        else:
            raise ValueError(_('All images must have the same resolution.'))
    return Image.blend(a, b, ratio), (idx, nextidx, ratio)

def apply(config):
    collection = config['collections'][config['active']]
    mode = collection.get('type', 'dynamic')
    if mode in ('spotlight', 'wikimedia', 'apod', 'bing') and not provider_available(mode):
        # Fail before provider import, network access, output creation or XFCE changes.
        raise providers.ProviderUnavailable(
            _('The {name} provider is not included in this Dywali edition.').format(name=mode))
    now = datetime.now()
    minute = now.hour*60+now.minute+now.second/60
    if collection.get('type') == 'spotlight':
        image_path, _state, _changed = providers.refresh('spotlight', collection.get('spotlight_minutes', int(collection.get('spotlight_hours', 24))*60))
        with Image.open(image_path) as src:
            image = ImageOps.exif_transpose(src).convert('RGB')
    elif collection.get('type') == 'wikimedia':
        image_path, _state, _changed = providers.refresh('wikimedia', collection.get('wikimedia_minutes', 1440), behavior=collection.get('wikimedia_behavior', 'potd'))
        with Image.open(image_path) as src:
            image = ImageOps.exif_transpose(src).convert('RGB')
    elif collection.get('type') == 'apod':
        image_path, _state, _changed = providers.refresh('apod', collection.get('apod_minutes', 1440))
        with Image.open(image_path) as src:
            image = ImageOps.exif_transpose(src).convert('RGB')
    elif collection.get('type') == 'bing':
        image_path, _state, _changed = providers.refresh('bing', collection.get('bing_rotate_minutes', 30) if collection.get('bing_behavior') == 'recent' else collection.get('bing_minutes', 1440),
                                        behavior=collection.get('bing_behavior', 'daily'),
                                        market=collection.get('bing_market', 'es-ES'),
                                        order=collection.get('bing_order', 'sequential'),
                                        automatic=collection.get('bing_auto', True))
        with Image.open(image_path) as src:
            image = ImageOps.exif_transpose(src).convert('RGB')
    elif collection.get('type') == 'slideshow':
        image_path, _due = next_image(collection, config['active'])
        with Image.open(image_path) as src:
            image = ImageOps.exif_transpose(src).convert('RGB')
    else:
        image, _render_info = render(collection['frames'], minute)
    OUTPUT.mkdir(parents=True, exist_ok=True)
    filename = OUTPUT/f'wallpaper-{now:%Y%m%d-%H%M%S-%f}.jpg'
    image.save(filename, quality=93)
    try:
        targets, failures = discover_targets(config)
    except (RuntimeError, ValueError):
        filename.unlink(missing_ok=True)
        raise
    successes, apply_failures = apply_to_targets(targets, filename)
    failures.extend(apply_failures)
    if not successes:
        filename.unlink(missing_ok=True)
        raise RuntimeError(_('Wallpaper could not be applied: {errors}').format(
            errors='; '.join(failures) or _('No active XFCE workspace0 wallpaper targets were found.')))

    # Re-read every XFCE backdrop property before pruning; old images may still
    # be referenced by monitors/workspaces that were not part of this update.
    cleanup_unused_wallpapers(OUTPUT, filename)
    if failures:
        raise RuntimeError(_('Wallpaper applied to {successes}; failed on {failures}.').format(
            successes=', '.join(successes), failures='; '.join(failures)))
    return filename

def main():
    try:
        print(apply(load_config()))
    except (ValueError, RuntimeError, KeyError) as exc:
        sys.exit(str(exc))


if __name__ == '__main__':
    main()
