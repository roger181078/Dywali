# Copyright (C) 2026 roger181078
# SPDX-License-Identifier: GPL-3.0-only
"""Persistent, non-repeating shuffle schedule for Dywali."""
import json
import random
import time
from pathlib import Path

STATE = Path.home() / '.local/state/dywali/slideshow.json'


def next_image(collection, name, now=None, state_file=None, advance=False):
    """Return current file and next due timestamp; advance only when due.

    State is atomic and isolated per collection. `advance` forces next photo.
    """
    now = time.time() if now is None else float(now)
    state_file = Path(state_file or STATE)
    images = list(collection.get('images', []))
    if not images:
        raise ValueError('Slideshow collection contains no images.')
    for path in images:
        if not Path(path).is_file():
            raise ValueError(f'Image not found: {path}')
    interval = max(60, int(collection.get('slide_minutes', 10)) * 60)
    try:
        state = json.loads(state_file.read_text(encoding='utf-8'))
    except (FileNotFoundError, ValueError, OSError):
        state = {}
    item = state.get(name, {})
    if (item.get('images') != images or item.get('interval') != interval
            or item.get('current') not in images):
        item = {'images': images, 'interval': interval, 'current': None,
                'queue': [], 'due': 0}
    if advance or now >= item['due'] or item['current'] is None:
        if not item['queue']:
            queue = images[:]
            random.shuffle(queue)
            if len(queue) > 1 and item['current'] == queue[0]:
                queue.append(queue.pop(0))
            item['queue'] = queue
        item['current'] = item['queue'].pop(0)
        # Advance due only once, without cycling through missed intervals.
        item['due'] = now + interval
    state[name] = item
    state_file.parent.mkdir(parents=True, exist_ok=True)
    temp = state_file.with_suffix('.tmp')
    temp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding='utf-8')
    temp.replace(state_file)
    return item['current'], item['due']
