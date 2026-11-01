# Copyright (C) 2026 roger181078
# SPDX-License-Identifier: GPL-3.0-only
"""Dywali interface localization (GNU gettext). English is the source language."""
import gettext
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PREF = Path.home() / '.config' / 'dywali' / 'ui.json'
LANGUAGES = [('system', 'System default'), ('en', 'English'), ('es', 'Español')]

def get_language():
    try:
        value = json.loads(PREF.read_text(encoding='utf-8')).get('language', 'system')
        return value if value in ('system', 'en', 'es') else 'system'
    except (FileNotFoundError, ValueError, OSError):
        return 'system'

def save_language(language):
    if language not in ('system', 'en', 'es'):
        raise ValueError('Unsupported language')
    PREF.parent.mkdir(parents=True, exist_ok=True)
    tmp = PREF.with_suffix('.tmp')
    tmp.write_text(json.dumps({'language': language}, indent=2) + '\n', encoding='utf-8')
    tmp.replace(PREF)

def current_locale():
    lang = get_language()
    if lang != 'system':
        return lang
    raw = os.environ.get('LC_ALL') or os.environ.get('LC_MESSAGES') or os.environ.get('LANG') or 'en'
    return 'es' if raw.lower().split('_')[0].split('.')[0] == 'es' else 'en'

_translation = gettext.translation('dywali', localedir=str(ROOT / 'locale'), languages=[current_locale()], fallback=True)
_ = _translation.gettext
