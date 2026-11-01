#!/usr/bin/env python3
# Copyright (C) 2026 roger181078
# SPDX-License-Identifier: GPL-3.0-only
"""One-off local migration of data directories. Never overwrites existing Dywali data."""
from pathlib import Path
import json

home = Path.home()
old_data = home/'.local/share/dynamic-wallpaper-studio'
new_data = home/'.local/share/dywali'
old_config = home/'.config/dynamic-wallpaper-studio'
new_config = home/'.config/dywali'
old_cache = home/'.cache/dynamic-wallpaper-studio'
new_cache = home/'.cache/dywali'

def migrate():
    # Stop before changing anything if destination folders already exist.
    for old,new in ((old_data,new_data),(old_config,new_config),(old_cache,new_cache)):
        if old.exists() and new.exists():
            raise RuntimeError(f'Conflicto: existen {old} y {new}. Haz copia y resuélvelo manualmente.')
    # Build updated config content before making changes.
    new_json = None
    original_config = None
    if (old_config/'config.json').exists():
        source = old_config/'config.json'
        original_config = source.read_bytes()
        cfg = json.loads(original_config.decode('utf-8'))
        for collection in cfg.get('collections',{}).values():
            for frame in collection.get('frames',[]):
                path = frame.get('path','')
                if isinstance(path,str):
                    frame['path'] = path.replace(str(old_data) + '/', str(new_data) + '/')
        new_json = json.dumps(cfg, ensure_ascii=False, indent=2)+'\n'
    moves=[]
    try:
        for old,new in ((old_data,new_data),(old_config,new_config),(old_cache,new_cache)):
            if old.exists():
                new.parent.mkdir(parents=True, exist_ok=True)
                old.rename(new)
                moves.append((old,new))
        if new_json is not None:
            target=new_config/'config.json'
            tmp=target.with_suffix('.migration-tmp')
            tmp.write_text(new_json,encoding='utf-8')
            tmp.replace(target)
    except Exception:
        # Paths are restored if the migration fails partway through.
        rollback({'moves': moves, 'original_config': original_config})
        (old_config/'config.json.migration-tmp').unlink(missing_ok=True)
        raise
    return {'moves': moves, 'original_config': original_config}


def rollback(receipt):
    """Undo a completed migration if the subsequent application deploy fails."""
    for old,new in reversed(receipt.get('moves', [])):
        if new.exists() and not old.exists():
            new.rename(old)
    original_config = receipt.get('original_config')
    if original_config is not None and (old_config/'config.json').is_file():
        tmp = old_config/'config.json.migration-rollback'
        tmp.write_bytes(original_config)
        tmp.replace(old_config/'config.json')

if __name__=='__main__':
    migrate()
    print('Rutas internas preparadas para Dywali.')
