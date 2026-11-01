# Copyright (C) 2026 roger181078
# SPDX-License-Identifier: GPL-3.0-only
"""Build-time feature profile for this Dywali package.

Select the profile when producing a distribution, not from user preferences.
"""

PROFILE = 'public-1.0'
PROFILES = {
    'private': frozenset({'spotlight', 'wikimedia', 'apod', 'bing'}),
    'public-1.0': frozenset({'wikimedia', 'apod'}),
}

if PROFILE not in PROFILES:
    raise RuntimeError(f'Unknown Dywali distribution profile: {PROFILE}')


def available_providers():
    """Return provider types enabled by the package profile."""
    return PROFILES[PROFILE]


def provider_available(name):
    return name in available_providers()
