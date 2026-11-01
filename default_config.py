#!/usr/bin/env python3
# Copyright (C) 2026 roger181078
# SPDX-License-Identifier: GPL-3.0-only
"""Colección de bienvenida: se crea solo en instalaciones sin configuración."""
import json
from pathlib import Path

CONFIG = Path.home() / '.config/dywali/config.json'

def make_default(root):
    frames = [
        {'path': str((root / 'assets' / 'Leafy-Night.jpg').resolve()), 'time': '05:30', 'mode': 'blend'},
        {'path': str((root / 'assets' / 'Leafy-Day.jpg').resolve()), 'time': '08:00', 'mode': 'hold'},
        {'path': str((root / 'assets' / 'Leafy-Day.jpg').resolve()), 'time': '19:30', 'mode': 'blend'},
        {'path': str((root / 'assets' / 'Leafy-Night.jpg').resolve()), 'time': '22:00', 'mode': 'hold'},
    ]
    return {'active': 'Leafy', 'display_selection': {'mode': 'auto', 'outputs': []},
            'interval_minutes': 5, 'collections': {'Leafy': {'frames': frames}}}

def seed_if_missing(root, config=CONFIG):
    config.parent.mkdir(parents=True, exist_ok=True)
    try:
        with config.open('x', encoding='utf-8') as file:
            json.dump(make_default(root), file, ensure_ascii=False, indent=2)
            file.write('\n')
    except FileExistsError:
        return False
    return True

if __name__ == '__main__':
    root = Path(__file__).resolve().parent
    print('Colección Leafy inicializada.' if seed_if_missing(root) else 'Configuración existente conservada.')
