# Copyright (C) 2026 roger181078
# SPDX-License-Identifier: GPL-3.0-only
"""Discover safe XFCE/X11 wallpaper targets without creating properties."""
import re
import subprocess
from pathlib import Path

from i18n import _

_XRANDR_ACTIVE = re.compile(
    r'^([^\s]+)\s+connected(?:\s+primary)?\s+(\d+x\d+)[+-]\d+[+-]\d+(?:\s|$)')
_SCREEN = re.compile(r'^/backdrop/screen(\d+)/')
_LAST_IMAGE = re.compile(
    r'^/backdrop/screen(\d+)/monitor([^/]+)/workspace0/last-image$')


def parse_xrandr(text):
    """Return output names that are connected and have an active mode."""
    outputs = []
    for line in text.splitlines():
        match = _XRANDR_ACTIVE.match(line.strip())
        if match and match.group(1) not in outputs:
            outputs.append(match.group(1))
    return outputs


def _parse_xrandr_displays(text, screen):
    displays = []
    for line in text.splitlines():
        match = _XRANDR_ACTIVE.match(line.strip())
        if match and not any(d['name'] == match.group(1) for d in displays):
            displays.append({
                'screen': screen,
                'name': match.group(1),
                'resolution': match.group(2),
                'property': None,
            })
    return displays


def parse_xfconf(text):
    """Parse `xfconf-query -lv` output into (property, value) pairs."""
    properties = []
    for line in text.splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2 and parts[0].startswith('/'):
            properties.append((parts[0], parts[1].strip()))
    return properties


def _query_xfconf(runner):
    try:
        result = runner(['xfconf-query', '-c', 'xfce4-desktop', '-lv'],
                        capture_output=True, text=True)
    except OSError as exc:
        raise RuntimeError(_('Could not query XFCE wallpaper properties: {error}').format(error=exc)) from exc
    if result.returncode:
        error = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(_('Could not query XFCE wallpaper properties: {error}').format(error=error))
    return parse_xfconf(result.stdout)


def selection_from_config(config):
    """Resolve new, intermediate, and legacy display-selection settings."""
    if 'display_selection' in config:
        selection = config['display_selection']
        if not isinstance(selection, dict):
            raise ValueError(_('Display selection configuration is invalid.'))
        mode = selection.get('mode')
        outputs = selection.get('outputs', [])
        if mode not in ('auto', 'custom') or not isinstance(outputs, list):
            raise ValueError(_('Display selection configuration is invalid.'))
        identifiers = []
        for output in outputs:
            if (not isinstance(output, dict) or type(output.get('screen')) is not int
                    or output['screen'] < 0 or not isinstance(output.get('name'), str)
                    or not output['name'].strip() or '/' in output['name']):
                raise ValueError(_('Display selection configuration is invalid.'))
            identifier = (output['screen'], output['name'])
            if identifier in identifiers:
                raise ValueError(_('Display selection contains duplicate outputs.'))
            identifiers.append(identifier)
        if mode == 'custom' and not identifiers:
            raise ValueError(_('Custom display selection must include at least one output.'))
        return mode, set(identifiers), None

    monitor_mode = config.get('monitor_mode')
    if monitor_mode not in (None, 'auto', 'selected'):
        raise ValueError(_('Unsupported monitor selection mode: {mode}').format(mode=monitor_mode))
    if monitor_mode == 'auto' or (monitor_mode is None and 'monitors' not in config):
        return 'auto', set(), None
    names = config.get('monitors')
    if not isinstance(names, list) or not all(isinstance(name, str) and name for name in names):
        raise ValueError(_('The configured monitor list is invalid.'))
    return 'legacy', set(), set(names)


def make_display_selection(mode, outputs):
    """Build and validate the persisted, versioned display-selection object."""
    if mode not in ('auto', 'custom'):
        raise ValueError(_('Unsupported display selection mode: {mode}').format(mode=mode))
    selection = {
        'mode': mode,
        'outputs': [
            {'screen': screen, 'name': name}
            for screen, name in sorted(set(outputs))
        ],
    }
    selection_from_config({'display_selection': selection})
    return selection


def config_after_display_dialog(config, accepted, mode, outputs):
    """Return a new config on acceptance; cancellation leaves the original untouched."""
    if not accepted:
        return None
    updated = dict(config)
    updated['display_selection'] = make_display_selection(mode, outputs)
    return updated


def discover_outputs(runner=subprocess.run, extra_screens=()):
    """Return active XRandR outputs and their exact existing XFCE workspace0 property, if any."""
    properties = _query_xfconf(runner)
    last_images = {}
    screens = set()
    for prop, _value in properties:
        screen_match = _SCREEN.match(prop)
        if screen_match:
            screens.add(int(screen_match.group(1)))
        match = _LAST_IMAGE.match(prop)
        if match and not match.group(2).isdecimal():
            # XFCE's generic monitor0/monitor1 properties are numeric indices,
            # not verified XRandR connector names.
            last_images[(int(match.group(1)), match.group(2))] = prop
    screens.update(extra_screens)
    if not screens:
        # Query screen zero only to diagnose a fresh/unconfigured XFCE profile;
        # this does not synthesize a property or make it a target.
        screens.add(0)

    failures = []
    displays = []
    for screen in sorted(screens):
        try:
            result = runner(['xrandr', '--screen', str(screen), '--query'],
                            capture_output=True, text=True)
        except OSError as exc:
            failures.append(_('Could not query active outputs on screen {screen}: {error}').format(
                screen=screen, error=exc))
            continue
        if result.returncode:
            error = result.stderr.strip() or result.stdout.strip()
            failures.append(_('Could not query active outputs on screen {screen}: {error}').format(
                screen=screen, error=error))
            continue

        for display in _parse_xrandr_displays(result.stdout, screen):
            display['property'] = last_images.get((screen, display['name']))
            displays.append(display)
    return displays, failures


def _missing_property(display):
    return _('No existing XFCE wallpaper property matches output {monitor} on screen {screen} workspace0. Open XFCE Desktop Settings > Background, select this display, and apply a wallpaper once; then retry Dywali.').format(
        monitor=display['name'], screen=display['screen'])


def discover_targets(config, runner=subprocess.run):
    """Return only selected active outputs with existing workspace0 properties."""
    mode, selected_ids, legacy_names = selection_from_config(config)
    screens = {screen for screen, _name in selected_ids} if mode == 'custom' else ()
    displays, failures = discover_outputs(runner, screens)
    targets = []
    active_ids = {(d['screen'], d['name']) for d in displays}
    active_names = {d['name'] for d in displays}
    for display in displays:
        identifier = (display['screen'], display['name'])
        if mode == 'custom' and identifier not in selected_ids:
            continue
        if mode == 'legacy' and display['name'] not in legacy_names:
            continue
        if display['property'] is None:
            failures.append(_missing_property(display))
            continue
        targets.append({**display, 'monitor': display['name']})

    if mode == 'custom':
        for screen, name in sorted(selected_ids - active_ids):
            failures.append(_('Selected output {monitor} on screen {screen} is not active; reconnect it or change the display selection.').format(
                monitor=name, screen=screen))
    elif mode == 'legacy':
        for name in sorted(legacy_names - active_names):
            failures.append(_('Configured output {monitor} is not active or has no matching XFCE property.').format(
                monitor=name))
    if not targets and not failures:
        failures.append(_('No active XFCE workspace0 wallpaper targets were found.'))
    return targets, failures


def apply_to_targets(targets, filename, runner=subprocess.run):
    """Apply to discovered targets and return successful labels and errors."""
    successes = []
    failures = []
    for target in targets:
        try:
            result = runner([
                'xfconf-query', '-c', 'xfce4-desktop', '-p', target['property'],
                '-s', str(filename)], capture_output=True, text=True)
        except OSError as exc:
            failures.append(f"{target['screen']}/{target['monitor']}: {exc}")
            continue
        if result.returncode:
            error = result.stderr.strip() or result.stdout.strip()
            failures.append(f"{target['screen']}/{target['monitor']}: {error}")
        else:
            successes.append(f"{target['screen']}/{target['monitor']}/workspace0")
    return successes, failures


def referenced_wallpapers(xfconf_text, output_dir):
    """Find generated wallpaper files referenced by any XFCE backdrop value."""
    output_dir = Path(output_dir).resolve()
    references = set()
    for prop, value in parse_xfconf(xfconf_text):
        if not prop.startswith('/backdrop/'):
            continue
        candidate = Path(value)
        if (candidate.name.startswith('wallpaper-') and candidate.suffix == '.jpg'
                and candidate.parent.resolve() == output_dir):
            references.add(candidate.resolve())
    return references


def cleanup_unused_wallpapers(output_dir, current, runner=subprocess.run):
    """Prune only unreferenced generated files; skip cleanup if XFCE cannot be queried."""
    output_dir = Path(output_dir)
    current = Path(current).resolve()
    try:
        properties = _query_xfconf(runner)
    except RuntimeError:
        return []
    text = '\n'.join(f'{prop} {value}' for prop, value in properties)
    references = referenced_wallpapers(text, output_dir)
    removed = []
    for candidate in output_dir.glob('wallpaper-*.jpg'):
        if candidate.is_symlink() or not candidate.is_file():
            continue
        if candidate.resolve() == current or candidate.resolve() in references:
            continue
        try:
            candidate.unlink()
            removed.append(candidate)
        except OSError:
            # Cleanup is best-effort and must not invalidate an applied wallpaper.
            pass
    return removed
