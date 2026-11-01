#!/usr/bin/env python3
# Copyright (C) 2026 roger181078
# SPDX-License-Identifier: GPL-3.0-only
"""Transactionally deploy only Dywali-owned application files."""

import argparse
import os
from pathlib import Path
import stat
import tempfile


COMMON_MODULES = (
    'studio.py', 'engine.py', 'displays.py', 'scheduler.py', 'default_config.py',
    'migration.py', 'i18n.py', 'slideshow.py', 'distribution.py', 'providers.py',
)
PROVIDER_MODULES = {
    'private': ('spotlight.py', 'wikimedia.py', 'apod.py', 'bing.py'),
    'public-1.0': ('wikimedia.py', 'apod.py'),
}
PRIVATE_MODULES = ('spotlight.py', 'bing.py')


class DeployError(RuntimeError):
    pass


def _reject_symlink(path):
    if path.is_symlink():
        raise DeployError(f'Rechazado enlace simbólico en una ruta de aplicación: {path}')


def _reject_symlink_parents(path):
    absolute = Path(os.path.abspath(path))
    for parent in absolute.parents:
        if parent == Path(parent.anchor):
            break
        _reject_symlink(parent)


def _ensure_directory(path):
    path = Path(path)
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        _reject_symlink(current)
        if current.exists() and not current.is_dir():
            raise DeployError(f'No es un directorio: {current}')
        current.mkdir(exist_ok=True)


def _read_regular(path):
    _reject_symlink(path)
    try:
        info = path.stat(follow_symlinks=False)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode):
        raise DeployError(f'No es un archivo regular de Dywali: {path}')
    return path.read_bytes(), stat.S_IMODE(info.st_mode)


def _is_owned(relative, contents, home, data):
    name = relative.name
    if relative.parts[0] == 'dywali':
        if name.endswith('.py'):
            return b'SPDX-License-Identifier: GPL-3.0-only' in contents[:2048]
        if name == 'LICENSE':
            return b'GNU GENERAL PUBLIC LICENSE' in contents[:256]
        if relative.parts[:2] == ('dywali', 'locale'):
            return True  # Locale files live in Dywali's private application directory.
        if relative.parts[:2] == ('dywali', 'icons'):
            return b'AI-generated for Dywali' in contents[:512]
    if relative == Path('bin/dywali'):
        return contents.startswith(b'#!/bin/sh\nexec /usr/bin/python3 ' + _shell_path(data / 'dywali/studio.py'))
    if relative == Path('bin/dywali-apply'):
        return contents.startswith(b'#!/bin/sh\nexec /usr/bin/python3 ' + _shell_path(data / 'dywali/engine.py'))
    if relative == Path('applications/dywali.desktop'):
        return b'Name=Dywali\n' in contents and os.fsencode(home / '.local/bin/dywali') in contents
    if relative == Path('icons/hicolor/scalable/apps/dywali.svg'):
        return b'AI-generated for Dywali' in contents[:512]
    return False


def _shell_path(path):
    value = os.fsencode(path)
    escaped = (value.replace(b'\\', b'\\\\').replace(b'"', b'\\"')
               .replace(b'$', b'\\$').replace(b'`', b'\\`'))
    return b'"' + escaped + b'"'


def _atomic_write(target, contents, mode):
    target.parent.mkdir(parents=True, exist_ok=True)
    _reject_symlink(target.parent)
    fd, temporary = tempfile.mkstemp(prefix='.dywali-write-', dir=target.parent)
    try:
        with os.fdopen(fd, 'wb') as output:
            output.write(contents)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, target)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _source_payload(source, profile):
    if profile not in PROVIDER_MODULES:
        raise DeployError(f'Perfil no admitido: {profile}')
    module_names = (*COMMON_MODULES, *PROVIDER_MODULES[profile])
    payload = {}
    for name in module_names:
        path = source / name
        _reject_symlink(path)
        if not path.is_file():
            raise DeployError(f'Falta un archivo requerido: {path}')
        payload[Path('dywali') / name] = (path.read_bytes(), 0o644)
    for relative in (Path('LICENSE'), Path('icons/dywali.svg'),
                     Path('locale/es/LC_MESSAGES/dywali.po'),
                     Path('locale/es/LC_MESSAGES/dywali.mo')):
        path = source / relative
        _reject_symlink(path)
        if not path.is_file():
            raise DeployError(f'Falta un archivo requerido: {path}')
        target_relative = Path('dywali') / relative
        payload[target_relative] = (path.read_bytes(), 0o644)
        if relative == Path('icons/dywali.svg'):
            payload[Path('icons/hicolor/scalable/apps/dywali.svg')] = (
                path.read_bytes(), 0o644)
    return payload


def deploy(source, destination, home, data, profile, check_only=False):
    source, destination, home, data = map(Path, (source, destination, home, data))
    payload = _source_payload(source, profile)
    if destination != data / 'dywali':
        raise DeployError('El destino de aplicación debe ser XDG_DATA_HOME/dywali.')
    _reject_symlink(destination)
    if destination.exists() and not destination.is_dir():
        raise DeployError(f'El destino no es un directorio: {destination}')

    # Stage generated launchers and desktop entry alongside application modules.
    bin_dir = home / '.local/bin'
    payload[Path('bin/dywali')] = (
        b'#!/bin/sh\nexec /usr/bin/python3 ' + _shell_path(destination / 'studio.py')
        + b' "$@"\n', 0o755)
    payload[Path('bin/dywali-apply')] = (
        b'#!/bin/sh\nexec /usr/bin/python3 ' + _shell_path(destination / 'engine.py')
        + b' "$@"\n', 0o755)
    desktop_exec = (str(bin_dir / 'dywali').replace('\\', '\\\\')
                    .replace('"', '\\"').replace('%', '%%'))
    payload[Path('applications/dywali.desktop')] = (
        '[Desktop Entry]\nType=Application\nVersion=1.0\nName=Dywali\n'
        'GenericName=Dynamic Wallpapers for Linux\n'
        'Comment=Dynamic wallpapers with smooth transitions for XFCE\n'
        f'Exec="{desktop_exec}"\nIcon=dywali\nTerminal=false\n'
        'Categories=Settings;DesktopSettings;\nStartupNotify=true\n'
    ).encode(), 0o644

    # Leafy is seeded only when absent; all existing images remain untouched.
    for name in ('Leafy-Day.jpg', 'Leafy-Night.jpg'):
        relative = Path('dywali/assets') / name
        source_path = source / 'assets' / name
        _reject_symlink(source_path)
        if not source_path.is_file():
            raise DeployError(f'Falta una imagen Leafy requerida: {source_path}')
        target = destination / 'assets' / name
        _reject_symlink(target)
        _reject_symlink_parents(target.parent)
        if not target.exists():
            payload[relative] = (source_path.read_bytes(), 0o644)

    files_to_remove = []
    if profile == 'public-1.0':
        files_to_remove.extend(destination / name for name in PRIVATE_MODULES)
        files_to_remove.extend(destination / (Path(name).stem + suffix)
                               for name in PRIVATE_MODULES for suffix in ('.pyc', '.pyo'))
        pycache = destination / '__pycache__'
        _reject_symlink(pycache)
        if pycache.exists():
            for name in PRIVATE_MODULES:
                files_to_remove.extend(pycache.glob(Path(name).stem + '.*.pyc'))

    # Preflight all target collisions before the first replacement.
    targets = []
    for relative, (contents, mode) in payload.items():
        target = (data / relative if relative.parts[0] != 'bin' else home / '.local' / relative)
        if relative.parts[0] == 'applications':
            target = data / relative
        elif relative.parts[0] == 'icons':
            target = data / relative
        _reject_symlink(target)
        _reject_symlink_parents(target.parent)
        old = _read_regular(target)
        if old is not None and not _is_owned(relative, old[0], home, data):
            # Leafy files were excluded above; every other occupied target must
            # carry a Dywali ownership signature before it may be replaced.
            raise DeployError(f'El destino ya existe y no se reconoce como archivo Dywali: {target}')
        targets.append((target, contents, mode, old))
    removals = []
    for target in files_to_remove:
        _reject_symlink(target)
        old = _read_regular(target)
        if old is not None:
            if (target.name in PRIVATE_MODULES
                    and b'SPDX-License-Identifier: GPL-3.0-only' not in old[0][:2048]):
                raise DeployError(f'No se elimina un módulo ajeno: {target}')
            removals.append((target, old))

    if check_only:
        return

    changed = []
    try:
        for target, contents, mode, old in targets:
            _ensure_directory(target.parent)
            changed.append((target, old))
            _atomic_write(target, contents, mode)
        for target, old in removals:
            changed.append((target, old))
            target.unlink()
    except Exception as exc:
        rollback_errors = []
        for target, old in reversed(changed):
            try:
                if old is None:
                    target.unlink(missing_ok=True)
                else:
                    _atomic_write(target, old[0], old[1])
            except Exception as rollback_exc:
                rollback_errors.append(f'{target}: {rollback_exc}')
        detail = ('; rollback failed: ' + '; '.join(rollback_errors)) if rollback_errors else ''
        raise DeployError(f'Deployment failed and was rolled back: {exc}{detail}') from exc


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true',
                        help='validate sources and destination collisions without writing')
    parser.add_argument('source', type=Path)
    parser.add_argument('destination', type=Path)
    parser.add_argument('home', type=Path)
    parser.add_argument('data', type=Path)
    parser.add_argument('profile', choices=tuple(PROVIDER_MODULES))
    args = parser.parse_args()
    try:
        deploy(args.source, args.destination, args.home, args.data, args.profile,
               check_only=args.check)
    except (DeployError, OSError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
