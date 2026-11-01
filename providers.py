# Copyright (C) 2026 roger181078
# SPDX-License-Identifier: GPL-3.0-only
"""Lazy provider access governed by the build-time distribution profile."""
import importlib
from pathlib import Path

from distribution import provider_available
from i18n import _

_MODULES = {
    'spotlight': 'spotlight',
    'wikimedia': 'wikimedia',
    'apod': 'apod',
    'bing': 'bing',
}


class ProviderUnavailable(RuntimeError):
    """Raised when a saved collection needs a provider absent from this build."""


def module(name):
    if name not in _MODULES:
        raise ProviderUnavailable(_('Unknown online provider: {name}').format(name=name))
    if not provider_available(name):
        raise ProviderUnavailable(
            _('The {name} provider is not included in this Dywali edition.').format(name=name))
    return importlib.import_module(_MODULES[name])


def refresh(name, *args, **kwargs):
    return module(name).refresh(*args, **kwargs)


def load_state(name):
    return module(name).load_state()


def cache_dir(name):
    if name not in _MODULES:
        raise ProviderUnavailable(_('Unknown online provider: {name}').format(name=name))
    return Path.home() / '.cache' / 'dywali' / name


def next_image(name, *args, **kwargs):
    return module(name).next_image(*args, **kwargs)
