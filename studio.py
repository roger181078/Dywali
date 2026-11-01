#!/usr/bin/env python3
# Copyright (C) 2026 roger181078
# SPDX-License-Identifier: GPL-3.0-only
"""Interfaz GTK3 para editar secuencias horarias de fondos XFCE."""
import sys
from i18n import _, get_language, save_language, current_locale, LANGUAGES
import subprocess
import shutil
import copy
import re
import json
import time
import zipfile
import tempfile
import threading
import html
import os
import unicodedata
import urllib.parse
from html.parser import HTMLParser
from pathlib import Path
try:
    import gi
    gi.require_version('Gtk', '3.0')
    from gi.repository import Gtk, Gdk, GdkPixbuf, GLib, Pango, GObject
except (ImportError, ValueError) as err:
    sys.exit('Falta GTK 3 para Python: sudo apt install python3-gi gir1.2-gtk-3.0\n'+str(err))
from PIL import Image, ImageOps
from engine import CONFIG, load_config, save_config, validate_frames, render, apply
import scheduler
import providers
from distribution import provider_available
from displays import config_after_display_dialog, discover_outputs, selection_from_config
from default_config import make_default, seed_if_missing

ROOT = Path(__file__).resolve().parent
APOD_PUBLICATION_HOME = 'https://science.nasa.gov/'
WIKIMEDIA_PUBLICATION_HOME = 'https://commons.wikimedia.org/'

class _APODTextParser(HTMLParser):
    """Extract readable text from NASA's explanation HTML (without rendering markup)."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)

    def handle_starttag(self, tag, attrs):
        if tag in ('p', 'br', 'div', 'li'):
            self.parts.append('\n')

    def handle_endtag(self, tag):
        if tag in ('p', 'div', 'li'):
            self.parts.append('\n')


def apod_plain_text(value):
    parser = _APODTextParser()
    parser.feed(str(value or ''))
    parser.close()
    text = html.unescape(''.join(parser.parts))
    text = ''.join(char for char in text
                   if char in '\n\t' or not unicodedata.category(char).startswith('C'))
    return '\n'.join(' '.join(line.split()) for line in text.splitlines() if line.strip())


def valid_wikimedia_page_url(value):
    """Accept only canonical HTTPS Commons file pages from provider state."""
    if not isinstance(value, str):
        return False
    try:
        parsed = urllib.parse.urlsplit(value)
        return (parsed.scheme == 'https' and parsed.hostname == 'commons.wikimedia.org' and
                not parsed.username and not parsed.password and parsed.port in (None, 443) and
                parsed.path.startswith('/wiki/File:'))
    except ValueError:
        return False


def valid_apod_permalink(value):
    """Accept only a canonical HTTPS publication URL on NASA's science site."""
    if not isinstance(value, str):
        return False
    try:
        parsed = urllib.parse.urlsplit(value)
        sensitive_parts = ('api', 'key', 'token', 'auth', 'secret', 'credential', 'signature', 'sig')
        sensitive_query = any(any(part in key.lower() for part in sensitive_parts)
                              for key, _ in urllib.parse.parse_qsl(parsed.query))
        return (parsed.scheme == 'https' and parsed.hostname == 'science.nasa.gov' and
                not parsed.username and not parsed.password and parsed.port in (None, 443) and
                parsed.path.startswith('/') and not parsed.fragment and
                not sensitive_query)
    except ValueError:
        return False


def _safe_apod_provenance_url(value):
    """Keep URL origin/path but omit query and fragment, where secrets may occur."""
    if not isinstance(value, str):
        return ''
    try:
        parsed = urllib.parse.urlsplit(value)
        if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or
                parsed.password or parsed.port not in (None, 443)):
            return ''
        return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, '', ''))
    except ValueError:
        return ''


def apod_provenance(state):
    """Build the stable, credential-free sidecar schema for a saved APOD JPEG."""
    state = state or {}
    return {
        'schema_version': 1,
        'provider': 'NASA APOD',
        'date': str(state.get('date') or ''),
        'title': apod_plain_text(state.get('title')),
        'description': apod_plain_text(state.get('explanation')),
        'credit': apod_plain_text(state.get('credit')),
        'copyright': apod_plain_text(state.get('copyright')),
        'permalink': (_safe_apod_provenance_url(state.get('page') or state.get('permalink'))
                      if valid_apod_permalink(state.get('page') or state.get('permalink')) else ''),
        'image_url': _safe_apod_provenance_url(state.get('image_url')),
        'candidate_url': _safe_apod_provenance_url(
            state.get('candidate_url') or state.get('source_url')),
        'download_url': _safe_apod_provenance_url(
            state.get('download_url') or state.get('url')),
    }


def apod_attribution_text(state):
    state = state or {}
    date = apod_plain_text(state.get('date')) or _('Not available')
    title = apod_plain_text(state.get('title')) or _('Not available')
    lines = [_('NASA APOD — {date}: {title}').format(date=date, title=title)]
    lines.append(_('Credit: {value}').format(
        value=apod_plain_text(state.get('credit')) or _('Not available')))
    lines.append(_('Copyright holder: {value}').format(
        value=apod_plain_text(state.get('copyright')) or _('Not available')))
    description = apod_plain_text(state.get('explanation'))
    excerpt = ' '.join(description.split())
    if len(excerpt) > 240:
        excerpt = excerpt[:237].rsplit(' ', 1)[0] + '…'
    lines.append(_('Description: {value}').format(
        value=excerpt or _('Not available')))
    if state.get('error'):
        lines.append(_('NASA APOD check failed; showing last saved image: ') +
                     apod_plain_text(state.get('error')))
    return '\n'.join(lines)


def save_apod_with_provenance(source, destination, state):
    """Save JPEG and exclusive JSON sidecar; roll back JPEG if sidecar commit fails."""
    source, destination = Path(source), Path(destination)
    sidecar = destination.with_suffix('.json')
    if sidecar.exists():
        raise FileExistsError(f'APOD provenance file already exists: {sidecar}')
    destination.parent.mkdir(parents=True, exist_ok=True)
    image_tmp = metadata_tmp = backup = None
    image_committed = False
    sidecar_committed = False
    try:
        with tempfile.NamedTemporaryFile(dir=destination.parent, suffix='.jpg', delete=False) as tmp:
            image_tmp = Path(tmp.name)
        shutil.copy2(source, image_tmp)
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=destination.parent,
                                         suffix='.json', delete=False) as tmp:
            metadata_tmp = Path(tmp.name)
            json.dump(apod_provenance(state), tmp, ensure_ascii=False, indent=2)
            tmp.write('\n')
            tmp.flush()
            os.fsync(tmp.fileno())
        if destination.exists():
            with tempfile.NamedTemporaryFile(dir=destination.parent, suffix='.backup',
                                             delete=False) as tmp:
                backup = Path(tmp.name)
            shutil.copy2(destination, backup)
        os.replace(image_tmp, destination)
        image_tmp = None
        image_committed = True
        # link() is atomic and refuses to replace a sidecar created concurrently.
        os.link(metadata_tmp, sidecar)
        sidecar_committed = True
        return sidecar
    except Exception:
        if sidecar_committed:
            sidecar.unlink(missing_ok=True)
        if image_committed:
            if backup is not None:
                os.replace(backup, destination)
                backup = None
            else:
                destination.unlink(missing_ok=True)
        raise
    finally:
        for temp in (image_tmp, metadata_tmp, backup):
            if temp is not None:
                temp.unlink(missing_ok=True)


def wikimedia_attribution_text(state, behavior):
    prefix = (_('Current Wikimedia picture of the day: ') if behavior == 'potd'
              else _('Current Wikimedia featured image: '))
    title = state.get('title') or _('Not available')
    author = state.get('author') or ''
    credit = state.get('credit') or ''
    attribution = author or credit or _('Not available')
    lines = [prefix + title, _('Author / credit: {value}').format(value=attribution)]
    if author and credit and author != credit:
        lines.append(_('Credit: {value}').format(value=credit))
    if state.get('description'):
        lines.append(_('Description: {value}').format(value=state['description']))
    lines.append(_('Declared license: {value}').format(
        value=state.get('license') or _('Not available')))
    return '\n'.join(lines)


class PreviewSlider(Gtk.DrawingArea):
    """Control horario propio: su grosor no depende de estados CSS del tema."""
    __gsignals__ = {'value-changed': (GObject.SignalFlags.RUN_FIRST, None, ())}

    def __init__(self):
        super().__init__()
        self._value = 720
        self._dragging = False
        self.set_size_request(120, 24)
        self.set_hexpand(True)
        self.set_vexpand(False)
        self.set_can_focus(True)
        self.add_events(Gdk.EventMask.BUTTON_PRESS_MASK |
                        Gdk.EventMask.BUTTON_RELEASE_MASK |
                        Gdk.EventMask.POINTER_MOTION_MASK |
                        Gdk.EventMask.KEY_PRESS_MASK)
        self.connect('draw', self._draw)
        self.connect('button-press-event', self._press)
        self.connect('motion-notify-event', self._motion)
        self.connect('button-release-event', self._release)
        self.connect('key-press-event', self._key)

    def get_value(self):
        return self._value

    def set_value(self, value):
        value = max(0, min(1439, int(round(value))))
        if value != self._value:
            self._value = value
            self.queue_draw()
            self.emit('value-changed')

    def _geometry(self):
        width = self.get_allocated_width()
        height = self.get_allocated_height()
        margin = 9
        return margin, max(margin + 1, width - margin), height / 2

    def _draw(self, _widget, cr):
        x0, x1, cy = self._geometry()
        progress = (x1 - x0) * (self._value / 1439)
        # Track de 4 px fijo, sea cual sea el estado del ratón.
        cr.set_source_rgb(0.73, 0.74, 0.76)
        cr.rectangle(x0, cy - 2, x1 - x0, 4)
        cr.fill()
        cr.set_source_rgb(0.33, 0.46, 0.72)
        cr.rectangle(x0, cy - 2, progress, 4)
        cr.fill()
        # Asa circular, idéntica durante clic y arrastre.
        cr.arc(x0 + progress, cy, 7, 0, 6.283185307179586)
        cr.set_source_rgb(1, 1, 1)
        cr.fill_preserve()
        cr.set_line_width(1)
        cr.set_source_rgb(0.73, 0.74, 0.76)
        cr.stroke()
        return False

    def _set_from_x(self, x):
        x0, x1, _ = self._geometry()
        ratio = max(0, min(1, (x - x0) / (x1 - x0)))
        self.set_value(round(ratio * 1439))

    def _press(self, _widget, event):
        if event.button != 1:
            return False
        self.grab_focus()
        self._dragging = True
        self._set_from_x(event.x)
        return True

    def _motion(self, _widget, event):
        if self._dragging:
            self._set_from_x(event.x)
            return True
        return False

    def _release(self, _widget, event):
        if event.button == 1 and self._dragging:
            self._set_from_x(event.x)
            self._dragging = False
            return True
        return False

    def _key(self, _widget, event):
        if event.keyval in (Gdk.KEY_Left, Gdk.KEY_Down):
            self.set_value(self._value - 15)
        elif event.keyval in (Gdk.KEY_Right, Gdk.KEY_Up):
            self.set_value(self._value + 15)
        elif event.keyval == Gdk.KEY_Home:
            self.set_value(0)
        elif event.keyval == Gdk.KEY_End:
            self.set_value(1439)
        else:
            return False
        return True

class Studio(Gtk.Window):
    def __init__(self):
        super().__init__(title='Dywali')
        self.set_default_size(1050, 720)
        icon_path = ROOT / 'icons' / 'dywali.svg'
        if icon_path.is_file():
            try:
                self.set_icon_from_file(str(icon_path))
            except GLib.Error:
                pass
        self._loading = False
        self._save_source = None
        self._drag_time = None
        self._dragging = False
        self._loaded_name = None
        self._time_slots = []  # (hora, modo) asociados a cada posición, no a la imagen
        self._drag_source_index = None  # solo movemos las imágenes, nunca filas completas
        self._ignore_selection = False
        self.set_border_width(14)
        self.connect('destroy', Gtk.main_quit)
        # Instalaciones nuevas: misma colección de bienvenida que el instalador.
        # Una configuración previa inválida NO se sobrescribe automáticamente.
        seed_if_missing(ROOT)
        self.data = load_config()
        base = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.add(base)
        heading = Gtk.Label()
        heading.set_markup('<span size="x-large" weight="bold">Dywali - Dynamic Wallpapers for Linux</span>')
        heading.set_xalign(0)
        base.pack_start(heading, False, False, 0)
        top = Gtk.Box(spacing=8)
        base.pack_start(top, False, False, 0)
        top.pack_start(Gtk.Label(label=_("Collection:" )), False, False, 0)
        self.combo = Gtk.ComboBoxText()
        self.combo.connect('changed', self.changed_collection)
        top.pack_start(self.combo, True, True, 0)
        self.button(top, _("New collection" ), self.new_collection)
        self.button(top, _("Duplicate" ), self.duplicate_collection)
        self.button(top, _("Rename" ), self.rename_collection)
        self.button(top, _("Delete collection" ), self.delete_collection)
        modebar = Gtk.Box(spacing=8)
        base.pack_start(modebar, False, False, 0)
        modebar.pack_start(Gtk.Label(label=_('Collection mode:')), False, False, 0)
        self.mode_combo = Gtk.ComboBoxText()
        self.mode_combo.append('dynamic', _('Dynamic (24-hour timeline)'))
        self.mode_combo.append('slideshow', _('Random slideshow'))
        provider_labels = {
            'spotlight': _('Microsoft Spotlight (experimental)'),
            'wikimedia': _('Wikimedia Commons (experimental)'),
            'apod': _('NASA APOD (experimental)'),
            'bing': _('Bing Wallpaper (experimental)'),
        }
        for provider in ('spotlight', 'wikimedia', 'apod', 'bing'):
            if provider_available(provider):
                self.mode_combo.append(provider, provider_labels[provider])
        # Keep a legacy unsupported mode visible in the selector so users can
        # explicitly choose a supported mode without rewriting their config at startup.
        saved_provider_types = {
            collection.get('type') for collection in self.data.get('collections', {}).values()
            if collection.get('type') in provider_labels and not provider_available(collection.get('type'))
        }
        for provider in ('spotlight', 'bing'):
            if provider in saved_provider_types:
                self.mode_combo.append(provider, _('{provider} (unavailable in this edition)').format(
                    provider=provider_labels[provider].split(' (')[0]))
        self.mode_combo.connect('changed', self.change_collection_mode)
        modebar.pack_start(self.mode_combo, False, False, 0)
        self.slideshow_controls = Gtk.Box(spacing=8)
        modebar.pack_start(self.slideshow_controls, False, False, 0)
        self.slideshow_controls.pack_start(Gtk.Label(label=_('Change every')), False, False, 0)
        self.slide_interval = Gtk.ComboBoxText()
        for n in (1, 2, 5, 10, 15, 30, 60, 120, 240):
            self.slide_interval.append(str(n), f'{n} min')
        self.slide_interval.connect('changed', self.on_slide_interval)
        self.slideshow_controls.pack_start(self.slide_interval, False, False, 0)
        self.slide_next = Gtk.Button(label=_('Next image'))
        self.slide_next.connect('clicked', self.next_slide)
        self.slideshow_controls.pack_start(self.slide_next, False, False, 0)
        self.slide_countdown = Gtk.Label(xalign=0)
        self.slide_countdown.set_valign(Gtk.Align.CENTER)
        self.slideshow_controls.pack_start(self.slide_countdown, False, False, 0)
        self.spotlight_controls = Gtk.Box(spacing=8)
        modebar.pack_start(self.spotlight_controls, False, False, 0)
        self.spotlight_controls.pack_start(Gtk.Label(label=_('Check every')), False, False, 0)
        self.spotlight_minutes = Gtk.ComboBoxText()
        for n in (5, 15, 30, 60, 360, 720, 1440):
            self.spotlight_minutes.append(str(n), f'{n} min' if n < 60 else f'{n//60} h')
        self.spotlight_minutes.connect('changed', self.on_spotlight_interval)
        self.spotlight_controls.pack_start(self.spotlight_minutes, False, False, 0)
        self.spotlight_refresh = Gtk.Button(label=_('Check now'))
        self.spotlight_refresh.connect('clicked', self.refresh_spotlight_ui)
        self.spotlight_controls.pack_start(self.spotlight_refresh, False, False, 0)
        self.spotlight_save = Gtk.Button(label=_('Save image'))
        self.spotlight_save.connect('clicked', self.save_spotlight_image)
        self.spotlight_controls.pack_start(self.spotlight_save, False, False, 0)
        self.spotlight_countdown = Gtk.Label(xalign=0)
        self.spotlight_countdown.set_no_show_all(True)
        self.spotlight_countdown.set_tooltip_text(_('Spotlight checks for new images; a check does not always change the wallpaper.'))
        self.bing_controls = Gtk.Box(spacing=8)
        base.pack_start(self.bing_controls, False, False, 0)
        self.bing_controls.pack_start(Gtk.Label(label=_('Source')), False, False, 0)
        self.bing_behavior = Gtk.ComboBoxText()
        self.bing_behavior.append('daily', _('Image of the day'))
        self.bing_behavior.append('recent', _('Recent images (last 8 days)'))
        self.bing_behavior.connect('changed', self.on_bing_behavior)
        self.bing_controls.pack_start(self.bing_behavior, False, False, 0)
        self.bing_check_label = Gtk.Label(label=_('Check every'))
        self.bing_controls.pack_start(self.bing_check_label, False, False, 0)
        self.bing_minutes = Gtk.ComboBoxText()
        for n in (60, 360, 720, 1440):
            self.bing_minutes.append(str(n), f'{n//60} h')
        self.bing_minutes.connect('changed', self.on_bing_interval)
        self.bing_controls.pack_start(self.bing_minutes, False, False, 0)
        self.bing_rotate_label = Gtk.Label(label=_("Change every"))
        self.bing_controls.pack_start(self.bing_rotate_label, False, False, 0)
        self.bing_rotate = Gtk.ComboBoxText()
        for n in (15, 30, 60, 120, 240, 360):
            self.bing_rotate.append(str(n), f'{n} min' if n < 60 else f'{n//60} h')
        self.bing_rotate.connect('changed', self.on_bing_rotation)
        self.bing_controls.pack_start(self.bing_rotate, False, False, 0)
        self.bing_order_label = Gtk.Label(label=_("Order"))
        self.bing_controls.pack_start(self.bing_order_label, False, False, 0)
        self.bing_order = Gtk.ComboBoxText()
        self.bing_order.append('sequential', _('Sequential'))
        self.bing_order.append('random', _('Random (no repeats)'))
        self.bing_order.connect('changed', self.on_bing_rotation)
        self.bing_controls.pack_start(self.bing_order, False, False, 0)
        self.bing_auto = Gtk.CheckButton(label=_('Automatic'))
        self.bing_auto.connect('toggled', self.on_bing_rotation)
        self.bing_controls.pack_start(self.bing_auto, False, False, 0)
        self.bing_refresh = Gtk.Button(label=_('Check now'))
        self.bing_refresh.connect('clicked', self.refresh_bing_ui)
        self.bing_controls.pack_start(self.bing_refresh, False, False, 0)
        self.bing_next = Gtk.Button(label=_('Next image'))
        self.bing_next.connect('clicked', self.next_bing_ui)
        self.bing_controls.pack_start(self.bing_next, False, False, 0)
        self.bing_save = Gtk.Button(label=_('Save image'))
        self.bing_save.connect('clicked', self.save_bing_image)
        self.bing_controls.pack_start(self.bing_save, False, False, 0)
        self.wikimedia_controls = Gtk.Box(spacing=8)
        modebar.pack_start(self.wikimedia_controls, False, False, 0)
        self.wikimedia_controls.pack_start(Gtk.Label(label=_('Source')), False, False, 0)
        self.wikimedia_behavior = Gtk.ComboBoxText()
        self.wikimedia_behavior.append('potd', _('Picture of the day'))
        self.wikimedia_behavior.append('featured', _('Random featured image'))
        self.wikimedia_behavior.connect('changed', self.on_wikimedia_behavior)
        self.wikimedia_controls.pack_start(self.wikimedia_behavior, False, False, 0)
        self.wikimedia_controls.pack_start(Gtk.Label(label=_('Check every')), False, False, 0)
        self.wikimedia_minutes = Gtk.ComboBoxText()
        for n in (60, 360, 720, 1440):
            self.wikimedia_minutes.append(str(n), f'{n} min' if n < 60 else f'{n//60} h')
        self.wikimedia_minutes.connect('changed', self.on_wikimedia_interval)
        self.wikimedia_controls.pack_start(self.wikimedia_minutes, False, False, 0)
        self.wikimedia_refresh = Gtk.Button(label=_('Check now'))
        self.wikimedia_refresh.connect('clicked', self.refresh_wikimedia_ui)
        self.wikimedia_controls.pack_start(self.wikimedia_refresh, False, False, 0)
        self.wikimedia_save = Gtk.Button(label=_('Save image'))
        self.wikimedia_save.connect('clicked', self.save_wikimedia_image)
        self.wikimedia_controls.pack_start(self.wikimedia_save, False, False, 0)
        self.apod_controls = Gtk.Box(spacing=8)
        modebar.pack_start(self.apod_controls, False, False, 0)
        self.apod_controls.pack_start(Gtk.Label(label=_('Check every')), False, False, 0)
        self.apod_minutes = Gtk.ComboBoxText()
        for n in (720, 1440):
            self.apod_minutes.append(str(n), f'{n//60} h')
        self.apod_minutes.connect('changed', self.on_apod_interval)
        self.apod_controls.pack_start(self.apod_minutes, False, False, 0)
        self.apod_refresh = Gtk.Button(label=_('Check now'))
        self.apod_refresh.connect('clicked', self.refresh_apod_ui)
        self.apod_controls.pack_start(self.apod_refresh, False, False, 0)
        self.apod_save = Gtk.Button(label=_('Save image'))
        self.apod_save.connect('clicked', self.save_apod_image)
        self.apod_controls.pack_start(self.apod_save, False, False, 0)
        self.apod_details = Gtk.Button(label=_('Read explanation'))
        self.apod_details.connect('clicked', self.show_apod_explanation)
        self.apod_controls.pack_start(self.apod_details, False, False, 0)
        self.spotlight_info = Gtk.Label(xalign=0)
        self.spotlight_info.set_line_wrap(True)
        base.pack_start(self.spotlight_info, False, False, 0)
        base.pack_start(self.spotlight_countdown, False, False, 0)
        self.wikimedia_info = Gtk.Label(xalign=0)
        self.wikimedia_info.set_line_wrap(True)
        base.pack_start(self.wikimedia_info, False, False, 0)
        self.wikimedia_page_link = Gtk.LinkButton.new_with_label(
            WIKIMEDIA_PUBLICATION_HOME, _('Open original file page on Wikimedia Commons'))
        self.wikimedia_page_link.set_sensitive(False)
        self.wikimedia_page_link.hide()
        base.pack_start(self.wikimedia_page_link, False, False, 0)
        self.apod_info = Gtk.Label(xalign=0)
        self.apod_info.set_line_wrap(True)
        base.pack_start(self.apod_info, False, False, 0)
        self.apod_page_link = Gtk.LinkButton.new_with_label(
            APOD_PUBLICATION_HOME, _('Open original publication on NASA APOD'))
        self.apod_page_link.set_sensitive(False)
        self.apod_page_link.hide()
        base.pack_start(self.apod_page_link, False, False, 0)
        self.bing_info = Gtk.Label(xalign=0)
        self.bing_info.set_line_wrap(True)
        base.pack_start(self.bing_info, False, False, 0)
        self.bing_countdown = Gtk.Label(xalign=0)
        base.pack_start(self.bing_countdown, False, False, 0)
        if provider_available('bing'):
            GLib.timeout_add_seconds(1, self._bing_countdown_tick)
        # DrawingArea no impone el tamaño natural de la imagen a la ventana.
        self.preview_pixbuf = None
        self.preview = Gtk.DrawingArea()
        self.preview.set_size_request(320, 180)
        self.preview.set_hexpand(True)
        self.preview.set_vexpand(True)
        self.preview.connect('draw', self.draw_preview)
        base.pack_start(self.preview, True, True, 0)
        hours = Gtk.Box(spacing=8)
        base.pack_start(hours, False, False, 0)
        hours.pack_start(Gtk.Label(label=_("Preview:" )), False, False, 0)
        self.scale = PreviewSlider()
        # GTK debe mostrar tambien los hijos del control de vista previa.
        self.scale.connect('value-changed', self.update_preview)
        hours.pack_start(self.scale, True, True, 0)
        self.time_label = Gtk.Label(label='12:00')
        hours.pack_start(self.time_label, False, False, 0)
        timeline_header = Gtk.Box(spacing=8)
        self.timeline_header = timeline_header
        self.timeline_header.set_no_show_all(True)
        base.pack_start(timeline_header, False, False, 0)
        timeline_header.pack_start(Gtk.Label(label=_("Timeline · 24 hours" ), xalign=0), True, True, 0)
        fixed_legend = Gtk.Label()
        fixed_legend.set_markup('<span foreground="#579e7a">■</span> ' + _('Fixed'))
        timeline_header.pack_start(fixed_legend, False, False, 0)
        blend_legend = Gtk.Label()
        blend_legend.set_markup('<span foreground="#5c7ac4">■</span> ' + _('Transition'))
        timeline_header.pack_start(blend_legend, False, False, 0)
        self.timeline = Gtk.DrawingArea()
        self.timeline.set_no_show_all(True)
        self.timeline.set_size_request(250, 66)
        self.timeline.set_hexpand(True)
        self.timeline.add_events(Gdk.EventMask.BUTTON_PRESS_MASK | Gdk.EventMask.BUTTON_RELEASE_MASK | Gdk.EventMask.POINTER_MOTION_MASK)
        self.timeline.connect('draw', self.draw_timeline)
        self.timeline.connect('button-press-event', self.click_timeline)
        self.timeline.connect('motion-notify-event', self.move_timeline)
        self.timeline.connect('button-release-event', self.release_timeline)
        self.timeline.set_tooltip_text(_("Click a segment to select its image; drag boundaries to adjust times." ))
        base.pack_start(self.timeline, False, False, 0)
        self.frames_header = Gtk.Label(label=_('Frames · each mode applies from its time until the next:'), xalign=0)
        base.pack_start(self.frames_header, False, False, 0)
        self.store = Gtk.ListStore(str,str,GdkPixbuf.Pixbuf,str)
        self.store.connect('row-changed', self.on_store_changed)
        self.store.connect('row-inserted', self.on_store_changed)
        self.store.connect('row-deleted', self.on_store_changed)
        self.tree = Gtk.TreeView(model=self.store)
        self.tree.get_selection().connect('changed', self.on_frame_selected)
        # No utilizar set_reorderable(True): GTK mueve toda la fila (hora incluida),
        # a menudo mediante señales row-inserted/row-deleted en vez de rows-reordered.
        self.tree.set_reorderable(False)
        targets = [Gtk.TargetEntry.new('application/x-dws-image-index',
                                        Gtk.TargetFlags.SAME_WIDGET, 0)]
        self.tree.enable_model_drag_source(Gdk.ModifierType.BUTTON1_MASK,
                                           targets, Gdk.DragAction.COPY)
        self.tree.enable_model_drag_dest(targets, Gdk.DragAction.COPY)
        self.tree.connect('drag-begin', self.on_image_drag_begin)
        self.tree.connect('drag-data-get', self.on_image_drag_data_get)
        self.tree.connect('drag-data-received', self.on_image_drag_data_received)
        self.tree.connect('drag-end', self.on_image_drag_end)
        self.tree.set_tooltip_text(_("Drag a row to reorder images. Times and modes stay assigned to their slots." ))
        self.tree.append_column(Gtk.TreeViewColumn(_("Preview" ), Gtk.CellRendererPixbuf(), pixbuf=2))
        col_time = Gtk.TreeViewColumn(_("Time" ), Gtk.CellRendererText(), text=0)
        self.col_time = col_time
        self.tree.append_column(col_time)
        path_renderer = Gtk.CellRendererText()
        path_renderer.set_property('ellipsize', Pango.EllipsizeMode.MIDDLE)
        path_column = Gtk.TreeViewColumn(_("File" ), path_renderer)
        path_column.pack_start(path_renderer, True)
        path_column.set_cell_data_func(path_renderer, self.render_filename)
        path_column.set_expand(True)
        path_column.set_resizable(True)
        self.tree.append_column(path_column)
        mode_renderer = Gtk.CellRendererText()
        mode_column = Gtk.TreeViewColumn(_('Until next'), mode_renderer)
        mode_column.set_cell_data_func(mode_renderer, self.render_mode)
        self.tree.append_column(mode_column)
        self.col_mode = mode_column
        duration_renderer = Gtk.CellRendererText()
        duration_column = Gtk.TreeViewColumn(_("Duration" ), duration_renderer)
        duration_column.pack_start(duration_renderer, True)
        duration_column.set_cell_data_func(duration_renderer, self.render_duration)
        self.tree.append_column(duration_column)
        self.col_duration = duration_column
        self.tree.set_has_tooltip(True)
        self.tree.connect('query-tooltip', self.filename_tooltip)
        scroll = Gtk.ScrolledWindow()
        self.frame_scroll = scroll
        scroll.set_min_content_height(165)
        scroll.add(self.tree)
        base.pack_start(scroll, True, True, 0)
        controls = Gtk.Box(spacing=8)
        self.edit_controls = controls
        base.pack_start(controls, False, False, 0)
        self.button(controls,_("Import HEIC" ),self.import_heic)
        self.button(controls,_("Add images" ),self.add_frame)
        self.btn_replace = self.button(controls,_("Replace image" ),self.replace_frame)
        self.btn_remove = self.button(controls,_("Remove image" ),self.remove_frame)
        self.btn_time = self.button(controls,_("Change time" ),self.edit_time)
        self.btn_time.set_no_show_all(True)
        self.btn_mode = self.button(controls,_("Change mode" ),self.edit_mode)
        self.btn_mode.set_no_show_all(True)
        # Guardado automático activo; el botón manual ya no es necesario.
        self.btn_apply = self.button(controls,_("Apply now" ),self.apply_now)
        sharing = Gtk.Box(spacing=8)
        base.pack_start(sharing, False, False, 0)
        self.button(sharing,_("Export collection" ),self.export_collection)
        self.button(sharing,_("Import collection" ),self.import_collection)
        self.button(sharing,_("About" ),self.show_about)
        self.button(sharing, _('Language'), self.language_dialog)
        self.button(sharing, _('Screens…'), self.display_selection_dialog)
        auto = Gtk.Box(spacing=10)
        auto.set_halign(Gtk.Align.START)
        base.pack_start(auto, False, False, 0)
        auto.pack_start(Gtk.Label(label=_("Automatic updates:" )),False,False,0)
        self.auto_switch=Gtk.Switch()
        self.auto_switch.set_valign(Gtk.Align.CENTER)
        self.auto_switch.set_vexpand(False)
        self.auto_switch.set_halign(Gtk.Align.START)
        self.auto_switch.set_hexpand(False)
        self.auto_switch.set_active(scheduler.enabled() if scheduler.available() else False)
        self.auto_switch.connect('notify::active',self.toggle_auto)
        auto.pack_start(self.auto_switch,False,False,0)
        self.auto_every_label = Gtk.Label(label=_("Every" ))
        self.auto_every_label.set_no_show_all(True)
        auto.pack_start(self.auto_every_label,False,False,0)
        self.interval=Gtk.ComboBoxText()
        self.interval.set_no_show_all(True)
        for n in (1,2,5,10,15,30,60): self.interval.append(str(n),str(n)+' min')
        self.interval.set_active_id(str(self.data.get('interval_minutes',5)))
        if self.interval.get_active()<0:self.interval.set_active_id('5')
        self.interval.connect('changed',self.interval_changed)
        auto.pack_start(self.interval,False,False,0)
        self.status = Gtk.Label(xalign=0)
        base.pack_start(self.status, False, False, 0)
        self.populate()
        # Solo lee el reloj persistente; no provoca cambios de imagen.
        GLib.timeout_add_seconds(1, self.refresh_slide_countdown)
        if provider_available('spotlight'):
            GLib.timeout_add_seconds(1, self.refresh_spotlight_countdown)
        self.refresh_slide_countdown()

    def refresh_slide_countdown(self):
        """Show the remaining time from the playback engine's persisted deadline."""
        if not self.is_slideshow():
            self.slide_countdown.set_text('')
            return True
        if not self.auto_switch.get_active():
            self.slide_countdown.set_text(_('Paused'))
            return True
        name = self.name()
        collection = self.data['collections'].get(name, {})
        try:
            from slideshow import STATE
            state = json.loads(STATE.read_text(encoding='utf-8'))
            item = state.get(name, {})
            if (item.get('current') not in collection.get('images', []) or
                    item.get('images') != collection.get('images', []) or
                    item.get('interval') != int(collection.get('slide_minutes', 10)) * 60):
                raise ValueError('No matching playback state')
            remaining = max(0, int(item['due'] - time.time() + 0.999))
            if remaining == 0:
                display = _('Next change pending')
            else:
                hours, rem = divmod(remaining, 3600)
                minutes, seconds = divmod(rem, 60)
                clock = f'{hours:02d}:{minutes:02d}:{seconds:02d}' if hours else f'{minutes:02d}:{seconds:02d}'
                display = _('Next change in {time}').format(time=clock)
        except (FileNotFoundError, ValueError, OSError, KeyError, TypeError):
            display = _('Waiting for first update')
        self.slide_countdown.set_text(display)
        return True

    def refresh_spotlight_countdown(self):
        """Read Spotlight's persisted last successful check; never request network."""
        if not self.is_spotlight():
            self.spotlight_countdown.hide()
            return True
        self.spotlight_countdown.show()
        if not self.auto_switch.get_active():
            label = _('Automatic updates paused')
        else:
            state = providers.load_state('spotlight')
            checked = float(state.get('checked_at') or 0)
            if not checked or not (providers.cache_dir('spotlight') / 'current.jpg').is_file():
                label = _('Waiting for first Spotlight check')
            else:
                remain = max(0, int(checked + self.spotlight_interval_minutes() * 60 - time.time() + 0.999))
                if remain:
                    hours, rem = divmod(remain, 3600)
                    minutes, seconds = divmod(rem, 60)
                    clock = f'{hours:02d}:{minutes:02d}:{seconds:02d}' if hours else f'{minutes:02d}:{seconds:02d}'
                    label = _('Next Spotlight check in {time}').format(time=clock)
                else:
                    label = _('Spotlight check pending')
        self.spotlight_countdown.set_text(label)
        return True

    def is_spotlight(self):
        return bool(provider_available('spotlight') and self.name() and self.data['collections'][self.name()].get('type') == 'spotlight')

    def is_wikimedia(self):
        return bool(self.name() and self.data['collections'][self.name()].get('type') == 'wikimedia')

    def is_apod(self):
        return bool(self.name() and self.data['collections'][self.name()].get('type') == 'apod')

    def is_slideshow(self):
        return bool(self.name() and self.data['collections'][self.name()].get('type') == 'slideshow')

    def is_bing(self):
        return bool(provider_available('bing') and self.name() and self.data['collections'][self.name()].get('type') == 'bing')

    def _hide_apod_page_link(self):
        self.apod_page_link.set_sensitive(False)
        self.apod_page_link.set_uri(APOD_PUBLICATION_HOME)
        self.apod_page_link.hide()

    def _hide_wikimedia_page_link(self):
        self.wikimedia_page_link.set_sensitive(False)
        self.wikimedia_page_link.set_uri(WIKIMEDIA_PUBLICATION_HOME)
        self.wikimedia_page_link.hide()

    def change_collection_mode(self, widget):
        if self._loading or not self.name():
            return
        new_type = widget.get_active_id()
        if new_type not in ('dynamic', 'slideshow', 'spotlight', 'wikimedia', 'apod', 'bing'):
            return
        if new_type in ('spotlight', 'wikimedia', 'apod', 'bing') and not provider_available(new_type):
            return
        self._autosave_pending()
        collection = self.data['collections'][self.name()]
        if new_type == 'slideshow' and collection.get('type') != 'slideshow':
            collection['images'] = [frame['path'] for frame in collection.get('frames', [])]
            collection['slide_minutes'] = 10
        if new_type == 'spotlight':
            collection.setdefault('spotlight_minutes', 1440)
        if new_type == 'wikimedia':
            collection.setdefault('wikimedia_minutes', 1440)
            collection.setdefault('wikimedia_behavior', 'potd')
        if new_type == 'apod':
            collection.setdefault('apod_minutes', 1440)
        if new_type == 'bing':
            collection.setdefault('bing_minutes', 1440)
            collection.setdefault('bing_behavior', 'daily')
            collection.setdefault('bing_market', 'es-ES')
            collection.setdefault('bing_rotate_minutes', 30)
            collection.setdefault('bing_order', 'sequential')
            collection.setdefault('bing_auto', True)
        collection['type'] = new_type
        save_config(self.data)
        self.load_frames()
        self.sync_scheduler()

    def sync_scheduler(self):
        collection = self.data['collections'].get(self.name(), {}) if self.name() else {}
        mode = collection.get('type')
        if mode in ('spotlight', 'bing', 'wikimedia', 'apod') and not provider_available(mode):
            # Preserve the user's existing timer configuration; engine.py will
            # refuse this collection without network access or applying a fallback.
            return
        if scheduler.enabled() if scheduler.available() else False:
            try:
                scheduler.install(1 if (self.is_spotlight() or self.is_wikimedia() or self.is_apod() or self.is_bing() or self.is_slideshow()) else self.data.get('interval_minutes', 5))
            except Exception as exc:
                self.message(str(exc), True)

    def spotlight_interval_minutes(self):
        collection = self.data['collections'].get(self.name(), {})
        return int(collection.get('spotlight_minutes', int(collection.get('spotlight_hours', 24))*60))

    @staticmethod
    def spotlight_interval_label(minutes):
        if minutes < 60:
            return _('{minutes} minutes').format(minutes=minutes)
        return _('{hours} hours').format(hours=minutes//60)

    def wikimedia_interval_minutes(self):
        collection = self.data['collections'].get(self.name(), {})
        return int(collection.get('wikimedia_minutes', 1440))

    def wikimedia_behavior_mode(self):
        collection = self.data['collections'].get(self.name(), {})
        behavior = collection.get('wikimedia_behavior', 'potd')
        return behavior if behavior in ('potd', 'featured') else 'potd'

    def apod_interval_minutes(self):
        collection = self.data['collections'].get(self.name(), {})
        return int(collection.get('apod_minutes', 1440))

    def bing_interval_minutes(self):
        collection = self.data['collections'].get(self.name(), {})
        return int(collection.get('bing_minutes', 1440))

    def bing_behavior_mode(self):
        collection = self.data['collections'].get(self.name(), {})
        behavior = collection.get('bing_behavior', 'daily')
        return behavior if behavior in ('daily', 'recent') else 'daily'

    def on_bing_rotation(self, *args):
        if self._loading or not self.is_bing(): return
        collection = self.data['collections'][self.name()]
        value = self.bing_rotate.get_active_id()
        if value: collection['bing_rotate_minutes'] = int(value)
        order = self.bing_order.get_active_id()
        if order: collection['bing_order'] = order
        collection['bing_auto'] = self.bing_auto.get_active()
        save_config(self.data)
        self.sync_scheduler()
        self._bing_countdown_tick()

    def _bing_countdown_tick(self):
        if not self.is_bing() or self.bing_behavior_mode() != 'recent':
            self.bing_countdown.hide()
            return True
        self.bing_countdown.show()
        state = providers.load_state('bing')
        collection = self.data['collections'][self.name()]
        catalog = state.get('catalog') or []
        index = int(state.get('current_index',0)) + 1
        text = _('Image {index} of {total}').format(index=index if catalog else 0, total=len(catalog))
        if collection.get('bing_auto',True) and state.get('mode') == 'recent':
            interval = int(collection.get('bing_rotate_minutes',30)) * 60
            left = max(0, int(interval - (time.time() - float(state.get('applied_at',0)))))
            text += ' · ' + _('Next change in {time}').format(time=f'{left//60:02d}:{left%60:02d}')
        else:
            text += ' · ' + _('Manual change')
        self.bing_countdown.set_text(text)
        return True

    def save_bing_image(self, *args):
        source = providers.cache_dir('bing') / 'current.jpg'
        if not self.is_bing() or not source.is_file():
            self.message(_('No Bing image available to save.'), True)
            return
        self._save_online_image(source, _('Bing image'))

    def on_bing_interval(self, widget):
        if self._loading or not self.is_bing(): return
        value = widget.get_active_id()
        if value:
            self.data['collections'][self.name()]['bing_minutes'] = int(value)
            save_config(self.data)
            self.sync_scheduler()

    def on_bing_behavior(self, widget):
        if self._loading or not self.is_bing(): return
        value = widget.get_active_id()
        if value in ('daily', 'recent'):
            self.data['collections'][self.name()]['bing_behavior'] = value
            save_config(self.data)
            self.sync_scheduler()
            self.load_frames()

    def refresh_bing_ui(self, *args):
        if not self.is_bing(): return
        self.bing_refresh.set_sensitive(False)
        self.bing_info.set_text(_('Checking Bing Wallpaper…'))
        self.preview_pixbuf = None
        self.preview.queue_draw()
        minutes = self.bing_interval_minutes()
        behavior = self.bing_behavior_mode()
        market = self.data['collections'][self.name()].get('bing_market', 'es-ES')
        def worker():
            try:
                collection = self.data['collections'][self.name()]
                path, state, changed = providers.refresh('bing', collection.get('bing_rotate_minutes', 30) if behavior == 'recent' else minutes,
                    force=True, behavior=behavior, market=market, order=collection.get('bing_order','sequential'),
                    automatic=collection.get('bing_auto',True))
                GLib.idle_add(self._bing_refresh_done, str(path), state, None)
            except Exception as exc:
                GLib.idle_add(self._bing_refresh_done, None, None, str(exc))
        threading.Thread(target=worker, daemon=True).start()

    def _bing_refresh_done(self, path, state, error):
        self.bing_refresh.set_sensitive(True)
        if error:
            self.bing_info.set_text(error)
            self.message(error, False)
            return False
        if self.is_bing():
            self._display_slide(path)
            self._bing_update_info()
            try:
                apply(self.data)
                self.message(_('Bing wallpaper applied.'))
            except Exception as exc:
                self.message(str(exc), True)
        return False

    def next_bing_ui(self, *args):
        if not self.is_bing() or self.bing_behavior_mode() != 'recent':
            return
        self.bing_next.set_sensitive(False)
        self.bing_info.set_text(_('Loading next Bing image…'))
        market = self.data['collections'][self.name()].get('bing_market', 'es-ES')
        def worker():
            try:
                collection = self.data['collections'][self.name()]
                path, state, changed = providers.next_image('bing', market=market, order=collection.get('bing_order','sequential'),
                    minutes=collection.get('bing_rotate_minutes',30))
                GLib.idle_add(self._bing_next_done, str(path), state, None)
            except Exception as exc:
                GLib.idle_add(self._bing_next_done, None, None, str(exc))
        threading.Thread(target=worker, daemon=True).start()

    def _bing_next_done(self, path, state, error):
        self.bing_next.set_sensitive(True)
        if error:
            self.bing_info.set_text(error)
            self.message(error, False)
            return False
        if self.is_bing():
            self._display_slide(path)
            self._bing_update_info()
            try:
                apply(self.data)
                self.message(_('Bing wallpaper applied.'))
            except Exception as exc:
                self.message(str(exc), True)
        return False

    def _bing_update_info(self):
        state = providers.load_state('bing')
        image = providers.cache_dir('bing') / 'current.jpg'
        behavior = self.bing_behavior_mode() if self.is_bing() else 'daily'
        self.bing_next.set_sensitive(behavior == 'recent')
        self._bing_countdown_tick()
        if image.is_file():
            title = state.get('title', '') or _('Untitled')
            date = state.get('date', '') or '?'
            if behavior == 'recent':
                queue = list(state.get('catalog') or [])
                total = len(queue) or 1
                current = int(state.get('current_index', 0) or 0) + 1
                head = _('Current Bing recent image {current}/{total}: {date} — {title}').format(current=current, total=total, date=date, title=title)
            else:
                head = _('Current Bing image of the day: {date} — {title}').format(date=date, title=title)
            lines = [head]
            if state.get('copyright'):
                lines.append(state.get('copyright'))
            self.bing_info.set_text('\n'.join(line for line in lines if line).strip())
            self._display_slide(str(image))
        else:
            self.bing_info.set_text(_('No Bing image cached. Click Check now.'))

    def save_spotlight_image(self, *args):
        source = providers.cache_dir('spotlight') / 'current.jpg'
        if not self.is_spotlight() or not source.is_file():
            self.message(_('No Spotlight image available to save.'), True)
            return
        self._save_online_image(source, _('Spotlight image'))

    def _save_online_image(self, source, title_prefix):
        dialog = Gtk.FileChooserDialog(
            title=title_prefix, transient_for=self,
            action=Gtk.FileChooserAction.SAVE)
        dialog.add_buttons(_('Cancel'), Gtk.ResponseType.CANCEL,
                           _('Save'), Gtk.ResponseType.ACCEPT)
        dialog.set_do_overwrite_confirmation(True)
        pictures = Path.home() / 'Imágenes'
        if pictures.is_dir():
            dialog.set_current_folder(str(pictures))
        dialog.set_current_name(title_prefix.replace(' ', '-') + '-' + time.strftime('%Y%m%d-%H%M%S') + '.jpg')
        result = dialog.run()
        filename = dialog.get_filename() if result == Gtk.ResponseType.ACCEPT else None
        dialog.destroy()
        if filename:
            try:
                shutil.copy2(source, filename)
                self.message(_('{name} saved to {path}').format(name=title_prefix, path=filename))
            except OSError as exc:
                self.message(str(exc), True)

    def save_wikimedia_image(self, *args):
        source = providers.cache_dir('wikimedia') / 'current.jpg'
        if not self.is_wikimedia() or not source.is_file():
            self.message(_('No Wikimedia image available to save.'), True)
            return
        self._save_online_image(source, _('Wikimedia image'))

    def save_apod_image(self, *args):
        source = providers.cache_dir('apod') / 'current.jpg'
        if not self.is_apod() or not source.is_file():
            self.message(_('No NASA APOD image available to save.'), True)
            return
        dialog = Gtk.FileChooserDialog(
            title=_('NASA APOD image'), transient_for=self,
            action=Gtk.FileChooserAction.SAVE)
        dialog.add_buttons(_('Cancel'), Gtk.ResponseType.CANCEL,
                           _('Save'), Gtk.ResponseType.ACCEPT)
        dialog.set_do_overwrite_confirmation(True)
        pictures = Path.home() / 'Imágenes'
        if pictures.is_dir():
            dialog.set_current_folder(str(pictures))
        dialog.set_current_name(_('NASA APOD image').replace(' ', '-') +
                                '-' + time.strftime('%Y%m%d-%H%M%S') + '.jpg')
        result = dialog.run()
        filename = dialog.get_filename() if result == Gtk.ResponseType.ACCEPT else None
        dialog.destroy()
        if not filename:
            return
        try:
            state = providers.load_state('apod')
            sidecar = save_apod_with_provenance(source, filename, state)
            self.message(_('{name} and provenance saved to {path}').format(
                name=_('NASA APOD image'), path=filename))
        except FileExistsError:
            self.message(_('Cannot save NASA APOD provenance: a JSON sidecar already exists.'), True)
        except OSError as exc:
            self.message(_('Could not save NASA APOD image and provenance: {error}').format(
                error=str(exc)), True)

    def on_spotlight_interval(self, widget):
        if self._loading or not self.is_spotlight(): return
        value=widget.get_active_id()
        if value:
            self.data['collections'][self.name()]['spotlight_minutes']=int(value)
            save_config(self.data)
            self.sync_scheduler()

    def refresh_spotlight_ui(self, *args):
        if not self.is_spotlight(): return
        self.spotlight_refresh.set_sensitive(False)
        self.spotlight_info.set_text(_('Checking Spotlight…'))
        minutes=self.spotlight_interval_minutes()
        def worker():
            try:
                path,state,changed=providers.refresh('spotlight', minutes,force=True)
                GLib.idle_add(self._spotlight_refresh_done,str(path),state,None)
            except Exception as exc:
                GLib.idle_add(self._spotlight_refresh_done,None,None,str(exc))
        threading.Thread(target=worker,daemon=True).start()

    def _spotlight_refresh_done(self,path,state,error):
        self.spotlight_refresh.set_sensitive(True)
        if error:
            self.spotlight_info.set_text(error)
            self.message(error,True)
            return False
        if self.is_spotlight():
            self._display_slide(path)
            self._spotlight_update_info()
            try:
                apply(self.data)
                self.message(_('Spotlight wallpaper applied.'))
            except Exception as exc:self.message(str(exc),True)
        return False

    def _spotlight_update_info(self):
        state=providers.load_state('spotlight')
        image=providers.cache_dir('spotlight')/'current.jpg'
        if image.is_file():
            self.spotlight_info.set_text((_('Current Spotlight image: ') + state.get('title','') + '\n' +
                state.get('credit','')).strip())
            self._display_slide(str(image))
        else:
            self.spotlight_info.set_text(_('No image cached. Click Check now.'))

    def on_wikimedia_interval(self, widget):
        if self._loading or not self.is_wikimedia(): return
        value=widget.get_active_id()
        if value:
            self.data['collections'][self.name()]['wikimedia_minutes']=int(value)
            save_config(self.data)
            self.sync_scheduler()

    def on_wikimedia_behavior(self, widget):
        if self._loading or not self.is_wikimedia(): return
        value = widget.get_active_id()
        if value in ('potd', 'featured'):
            self.data['collections'][self.name()]['wikimedia_behavior'] = value
            save_config(self.data)
            self.sync_scheduler()
            self.preview_pixbuf = None
            self.preview.queue_draw()
            self._wikimedia_update_info()

    def refresh_wikimedia_ui(self, *args):
        if not self.is_wikimedia(): return
        self.wikimedia_refresh.set_sensitive(False)
        self.wikimedia_info.set_text(_('Checking Wikimedia Commons…'))
        self.preview_pixbuf = None
        self.preview.queue_draw()
        minutes=self.wikimedia_interval_minutes()
        def worker():
            try:
                path,state,changed=providers.refresh('wikimedia', minutes,force=True,behavior=self.wikimedia_behavior_mode())
                GLib.idle_add(self._wikimedia_refresh_done,str(path),state,None)
            except Exception as exc:
                GLib.idle_add(self._wikimedia_refresh_done,None,None,str(exc))
        threading.Thread(target=worker,daemon=True).start()

    def _wikimedia_refresh_done(self,path,state,error):
        self.wikimedia_refresh.set_sensitive(True)
        if error:
            if self.is_wikimedia():
                self._wikimedia_update_info()
            else:
                self.wikimedia_info.set_text(error)
            self.message(error,False)
            return False
        if self.is_wikimedia():
            self._display_slide(path)
            self._wikimedia_update_info()
            try:
                apply(self.data)
                self.message(_('Wikimedia wallpaper applied.'))
            except Exception as exc:self.message(str(exc),True)
        return False

    def _wikimedia_update_info(self):
        if not self.is_wikimedia():
            self._hide_wikimedia_page_link()
            return
        state=providers.load_state('wikimedia')
        image=providers.cache_dir('wikimedia')/'current.jpg'
        behavior = self.wikimedia_behavior_mode()
        if image.is_file() and state.get('mode', 'potd') == behavior:
            self.wikimedia_info.set_text(wikimedia_attribution_text(state, behavior))
            page_url = state.get('page_url')
            if valid_wikimedia_page_url(page_url):
                self.wikimedia_page_link.set_uri(page_url)
                self.wikimedia_page_link.set_sensitive(True)
                self.wikimedia_page_link.show()
            else:
                self._hide_wikimedia_page_link()
            self._display_slide(str(image))
        else:
            self._hide_wikimedia_page_link()
            if behavior == 'potd':
                self.wikimedia_info.set_text(_('No picture cached for this Wikimedia source. Click Check now.'))
            else:
                self.wikimedia_info.set_text(_('No featured image cached for this Wikimedia source. Click Check now.'))

    def on_apod_interval(self, widget):
        if self._loading or not self.is_apod(): return
        value=widget.get_active_id()
        if value:
            self.data['collections'][self.name()]['apod_minutes']=int(value)
            save_config(self.data)
            self.sync_scheduler()

    def refresh_apod_ui(self, *args):
        if not self.is_apod(): return
        self.apod_refresh.set_sensitive(False)
        self.apod_info.set_text(_('Checking NASA APOD…'))
        self.preview_pixbuf = None
        self.preview.queue_draw()
        minutes=self.apod_interval_minutes()
        def worker():
            try:
                path,state,changed=providers.refresh('apod', minutes,force=True)
                GLib.idle_add(self._apod_refresh_done,str(path),state,None)
            except Exception as exc:
                GLib.idle_add(self._apod_refresh_done,None,None,str(exc))
        threading.Thread(target=worker,daemon=True).start()

    def _apod_refresh_done(self,path,state,error):
        self.apod_refresh.set_sensitive(True)
        if error:
            self.apod_info.set_text(error)
            self.message(error,False)
            return False
        if self.is_apod():
            self._display_slide(path)
            self._apod_update_info()
            try:
                apply(self.data)
                if state.get('error'):
                    self.message(_('NASA APOD check failed; using cached image: ') + str(state['error']), True)
                else:
                    self.message(_('NASA APOD wallpaper applied.'))
            except Exception as exc:self.message(str(exc),True)
        return False

    def show_apod_explanation(self, *args):
        state = providers.load_state('apod')
        body = apod_plain_text(state.get('explanation'))
        if not body:
            self.message(_('No APOD explanation available.'), False)
            return
        dialog = Gtk.Dialog(_('NASA APOD explanation'), self, Gtk.DialogFlags.MODAL,
                            (_('Close'), Gtk.ResponseType.CLOSE))
        dialog.set_default_size(650, 420)
        area = dialog.get_content_area()
        area.set_spacing(10)
        header = Gtk.Label(label=apod_plain_text(state.get('title')) or _('Untitled'), xalign=0)
        header.set_line_wrap(True)
        area.pack_start(header, False, False, 8)
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        display = Gtk.TextView()
        display.set_editable(False)
        display.set_cursor_visible(False)
        display.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        display.set_left_margin(12)
        display.set_right_margin(12)
        display.set_top_margin(8)
        display.get_buffer().set_text(body)
        scroll.add(display)
        area.pack_start(scroll, True, True, 0)
        dialog.show_all()
        dialog.run()
        dialog.destroy()

    def _apod_update_info(self):
        if not self.is_apod():
            self._hide_apod_page_link()
            return
        state=providers.load_state('apod')
        image=providers.cache_dir('apod')/'current.jpg'
        if image.is_file():
            self.apod_info.set_text(apod_attribution_text(state))
            if state.get('notice_code') == 'video_fallback':
                self.apod_info.set_text(self.apod_info.get_text() + '\n' +
                    _("Today's APOD is a video. Showing the latest recent image instead."))
            elif state.get('notice'):
                self.apod_info.set_text(self.apod_info.get_text() + '\n' +
                                        apod_plain_text(state.get('notice')))
            body = apod_plain_text(state.get('explanation'))
            self.apod_details.set_sensitive(bool(body))
            page = state.get('page') or state.get('permalink')
            if valid_apod_permalink(page):
                self.apod_page_link.set_uri(page)
                self.apod_page_link.set_sensitive(True)
                self.apod_page_link.show()
            else:
                self._hide_apod_page_link()
            self._display_slide(str(image))
        else:
            self.apod_details.set_sensitive(False)
            self._hide_apod_page_link()
            self.apod_info.set_text(_('No APOD image cached. Click Check now.'))

    def on_slide_interval(self, widget):
        if self._loading or not self.is_slideshow():
            return
        val = widget.get_active_id()
        if val:
            self.data['collections'][self.name()]['slide_minutes'] = int(val)
            save_config(self.data)
            self.sync_scheduler()

    def next_slide(self, *args):
        if not self.is_slideshow():
            return
        self._autosave_pending()
        from slideshow import next_image
        try:
            path, _ = next_image(self.data['collections'][self.name()], self.name(), advance=True)
            self._display_slide(path)
            # This is a real user action: apply immediately to the desktop.
            apply(self.data)
        except Exception as exc:
            self.message(str(exc), True)

    def _display_slide(self, path):
        try:
            with Image.open(path) as src:
                pic = ImageOps.exif_transpose(src).convert('RGB')
                pic.thumbnail((920, 540), Image.Resampling.LANCZOS)
            self.preview_pixbuf = GdkPixbuf.Pixbuf.new_from_bytes(
                GLib.Bytes.new(pic.tobytes()), GdkPixbuf.Colorspace.RGB,
                False, 8, pic.width, pic.height, pic.width * 3)
            self.preview.queue_draw()
        except Exception as exc:
            self.message(str(exc), True)

    def language_dialog(self, *args):
        dialog = Gtk.Dialog(_('Language'), self, Gtk.DialogFlags.MODAL,
                            (Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
                             Gtk.STOCK_OK, Gtk.ResponseType.OK))
        area = dialog.get_content_area()
        area.set_spacing(10)
        label = Gtk.Label(label=_('Choose the interface language. Restart Dywali to apply changes.'), xalign=0)
        area.pack_start(label, False, False, 8)
        combo = Gtk.ComboBoxText()
        for code, name in LANGUAGES:
            combo.append(code, name)
        combo.set_active_id(get_language())
        area.pack_start(combo, False, False, 8)
        dialog.show_all()
        if dialog.run() == Gtk.ResponseType.OK:
            chosen = combo.get_active_id()
            if chosen:
                save_language(chosen)
                self.message(_('Language preference saved. Restart Dywali to apply it.'))
        dialog.destroy()

    def display_selection_dialog(self, *args):
        try:
            mode, selected_ids, legacy_names = selection_from_config(self.data)
            outputs, discovery_errors = discover_outputs(
                extra_screens={screen for screen, _name in selected_ids})
        except (RuntimeError, ValueError) as exc:
            self.message(str(exc), True)
            return

        active_ids = {(item['screen'], item['name']) for item in outputs}
        if mode == 'legacy':
            selected_ids = {(item['screen'], item['name']) for item in outputs
                            if item['name'] in legacy_names}
            mode = 'custom'

        dialog = Gtk.Dialog(_('Display selection'), self, Gtk.DialogFlags.MODAL,
                            (Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
                             Gtk.STOCK_OK, Gtk.ResponseType.OK))
        dialog.set_default_size(470, 390)
        dialog.set_resizable(False)
        area = dialog.get_content_area()
        area.set_spacing(8)
        intro = Gtk.Label(label=_('Choose which active displays Dywali should update.'), xalign=0)
        intro.set_line_wrap(True)
        area.pack_start(intro, False, False, 8)

        all_radio = Gtk.RadioButton.new_with_label_from_widget(None, _('All displays (automatic)'))
        custom_radio = Gtk.RadioButton.new_with_label_from_widget(all_radio, _('Only selected displays'))
        all_radio.set_active(mode == 'auto')
        custom_radio.set_active(mode == 'custom')
        area.pack_start(all_radio, False, False, 0)
        area.pack_start(custom_radio, False, False, 0)

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_min_content_height(175)
        listbox = Gtk.ListBox()
        listbox.set_selection_mode(Gtk.SelectionMode.NONE)
        scroll.add(listbox)
        area.pack_start(scroll, True, True, 0)

        checkboxes = {}

        def add_output_row(identifier, title, available, status, active):
            row = Gtk.ListBoxRow()
            content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            checkbox = Gtk.CheckButton(label=title)
            checkbox.set_active(active)
            checkbox.get_child().set_line_wrap(True)
            content.pack_start(checkbox, False, False, 0)
            if status:
                detail = Gtk.Label(label=status, xalign=0)
                detail.set_line_wrap(True)
                detail.get_style_context().add_class('dim-label')
                content.pack_start(detail, False, False, 0)
            content.set_margin_start(6)
            content.set_margin_end(6)
            content.set_margin_top(4)
            content.set_margin_bottom(4)
            row.add(content)
            listbox.add(row)
            if identifier is not None:
                checkboxes[identifier] = (checkbox, available)
            return checkbox

        for item in outputs:
            identifier = (item['screen'], item['name'])
            ready = bool(item['property'])
            title = _('Screen {screen} · {name} · {resolution}').format(
                screen=item['screen'], name=item['name'], resolution=item['resolution'])
            status = '' if ready else _('Unavailable: initialize this display in XFCE Desktop Settings > Background.')
            add_output_row(identifier, title, ready, status, identifier in selected_ids)

        disconnected = selected_ids - active_ids
        for screen, name in sorted(disconnected):
            title = _('Screen {screen} · {name}').format(screen=screen, name=name)
            add_output_row((screen, name), title, False, _('Not currently connected. Saved selection retained.'), True)

        legacy_missing = sorted(legacy_names - {item['name'] for item in outputs}) if legacy_names else []
        if legacy_missing:
            area.pack_start(Gtk.Label(
                label=_('Legacy selected outputs not currently detected: {names}').format(
                    names=', '.join(legacy_missing)), xalign=0), False, False, 0)
        if discovery_errors:
            for error in discovery_errors:
                warning = Gtk.Label(label=error, xalign=0)
                warning.set_line_wrap(True)
                area.pack_start(warning, False, False, 0)
        if not outputs and not disconnected:
            empty = Gtk.Label(label=_('No active displays were detected.'), xalign=0)
            area.pack_start(empty, False, False, 0)

        hint = Gtk.Label(label=_('Displays without an XFCE wallpaper property must first be initialized from Desktop Settings > Background.'), xalign=0)
        hint.set_line_wrap(True)
        area.pack_start(hint, False, False, 4)

        def update_checkbox_sensitivity(*_args):
            custom = custom_radio.get_active()
            for checkbox, _available in checkboxes.values():
                checkbox.set_sensitive(custom)

        for checkbox, _available in checkboxes.values():
            checkbox.connect('toggled', update_checkbox_sensitivity)
        custom_radio.connect('toggled', update_checkbox_sensitivity)
        update_checkbox_sensitivity()

        while True:
            dialog.show_all()
            update_checkbox_sensitivity()
            response = dialog.run()
            if response != Gtk.ResponseType.OK:
                config_after_display_dialog(self.data, False, 'auto', [])
                break
            new_mode = 'auto' if all_radio.get_active() else 'custom'
            chosen = [identifier for identifier, (checkbox, _available) in checkboxes.items()
                      if checkbox.get_active()]
            try:
                # Validate before touching pending editor state or persistent config.
                config_after_display_dialog(self.data, True, new_mode, chosen)
                self._autosave_pending()
                updated = config_after_display_dialog(self.data, True, new_mode, chosen)
                save_config(updated)
            except (OSError, ValueError, TypeError) as exc:
                self.message(str(exc), True)
                continue
            self.data = updated
            break
        dialog.destroy()
        if response == Gtk.ResponseType.OK:
            self.message(_('Display selection saved. It will be used on the next wallpaper update.'))

    def show_about(self, *args):
        dialog = Gtk.AboutDialog(transient_for=self, modal=True)
        dialog.set_program_name('Dywali')
        dialog.set_version('1.0.0')
        dialog.set_comments(_('Dynamic Wallpapers for Linux'))
        dialog.set_copyright('Copyright (C) 2026 roger181078')
        # GTK only exposes its secondary license page for CUSTOM licenses;
        # predefined license types show a disclaimer/link instead.
        dialog.set_license((ROOT / 'LICENSE').read_text(encoding='utf-8'))
        project_url = 'https://github.com/roger181078/Dywali'
        dialog.set_website(project_url)
        dialog.set_website_label(_('Project website'))

        def open_project_page(_dialog, uri):
            if uri != project_url:
                return False
            try:
                Gtk.show_uri_on_window(dialog, uri, Gdk.CURRENT_TIME)
            except GLib.Error as exc:
                self.message(_('Could not open project page: {error}').format(error=exc), True)
            return True

        dialog.connect('activate-link', open_project_page)
        dialog.set_authors(['Dywali project'])
        icon = ROOT / 'icons' / 'dywali.svg'
        if icon.is_file():
            try:
                dialog.set_logo(GdkPixbuf.Pixbuf.new_from_file_at_scale(str(icon), 96, 96, True))
            except GLib.Error:
                pass
        # GTK traduce los botones internos del AboutDialog según el locale del
        # proceso, que puede diferir del idioma seleccionado dentro de Dywali.
        # Ajustamos solo esos botones, sin modificar el locale global del usuario.
        chosen = 'es' if current_locale() == 'es' else 'en'
        labels = {
            'credits': {'es': 'Créditos', 'en': 'Credits'},
            'créditos': {'es': 'Créditos', 'en': 'Credits'},
            'license': {'es': 'Licencia', 'en': 'License'},
            'licencia': {'es': 'Licencia', 'en': 'License'},
            'close': {'es': 'Cerrar', 'en': 'Close'},
            'cerrar': {'es': 'Cerrar', 'en': 'Close'},
        }

        def localize_buttons(widget):
            if isinstance(widget, Gtk.Button):
                label = widget.get_label()
                if label:
                    key = label.replace('_', '').strip().casefold()
                    if key in labels:
                        widget.set_use_underline(False)
                        widget.set_label(labels[key][chosen])
            if isinstance(widget, Gtk.Container):
                for child in widget.get_children():
                    localize_buttons(child)

        dialog.show_all()
        localize_buttons(dialog)

        coffee_button = Gtk.Button(label=_('☕ Buy Me a Coffee'))
        coffee_button.set_use_underline(False)

        def open_donation_page(_button):
            try:
                Gtk.show_uri_on_window(
                    dialog, 'https://buymeacoffee.com/reminderclock',
                    Gdk.CURRENT_TIME)
            except GLib.Error as exc:
                self.message(_('Could not open donation page: {error}').format(error=exc), True)

        coffee_button.connect('clicked', open_donation_page)
        action_area = dialog.get_action_area()
        action_area.pack_start(coffee_button, False, False, 0)
        action_area.set_child_secondary(coffee_button, True)
        action_children = action_area.get_children()
        credits_index = next((index for index, child in enumerate(action_children)
                              if isinstance(child, Gtk.Button) and
                              (child.get_label() or '').casefold() in ('créditos', 'credits')),
                             len(action_children) - 1)
        action_area.reorder_child(coffee_button, credits_index + 1)
        coffee_button.show()
        dialog.run()
        dialog.destroy()

    def render_mode(self, column, cell, model, itr, data=None):
        cell.set_property('text', _('Fixed') if model[itr][3] == 'Fija' else _('Transition'))

    def render_filename(self, column, cell, model, itr, data=None):
        cell.set_property('text', Path(model[itr][1]).name)

    def render_duration(self, column, cell, model, itr, data=None):
        """Duración entre la hora de esta fila y la siguiente cronológica."""
        try:
            current = self._minute_of(model[itr][0])
            times = sorted(self._minute_of(row[0]) for row in model)
            later = next((t for t in times if t > current), times[0] + 1440)
            minutes = later - current
            cell.set_property('text', f'{minutes // 60:02d}:{minutes % 60:02d}')
        except (ValueError, IndexError):
            cell.set_property('text', '—')

    @staticmethod
    def _minute_of(hhmm):
        hour, minute = map(int, hhmm.split(':'))
        if not (0 <= hour < 24 and 0 <= minute < 60):
            raise ValueError(_('Invalid time'))
        return hour * 60 + minute

    def filename_tooltip(self, tree, x, y, keyboard_mode, tooltip):
        if keyboard_mode:
            idx = self._selected_frame_index()
        else:
            hit = tree.get_path_at_pos(x, y)
            idx = hit[0].get_indices()[0] if hit else None
        if idx is None or idx >= len(self.store):
            return False
        tooltip.set_text(self.store[idx][1])
        return True

    def export_collection(self, *args):
        """ZIP transportable: configuración + fotos, sin dependencia de rutas externas."""
        if not self.name():
            return
        dialog = Gtk.FileChooserDialog(_("Export collection" ), self, Gtk.FileChooserAction.SAVE,
            (Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL, Gtk.STOCK_SAVE, Gtk.ResponseType.OK))
        dialog.set_do_overwrite_confirmation(True)
        dialog.set_current_name(re.sub(r'[^\w .-]', '_', self.name()) + '.zip')
        destination = dialog.get_filename() if dialog.run() == Gtk.ResponseType.OK else None
        dialog.destroy()
        if destination is None:
            return
        try:
            random_mode = self.is_slideshow()
            if random_mode:
                paths = [row[1] for row in self.store]
                if not paths: raise ValueError(_('No images in this collection.'))
                frames = [{'path':path} for path in paths]
            else:
                frames = self.frames()
                validate_frames(frames)
            manifest = {'format': 'dywali-collection', 'version': 1,
                        'name': self.name(), 'frames': []}
            if random_mode:
                manifest['type'] = 'slideshow'
                manifest['slide_minutes'] = self.data['collections'][self.name()].get('slide_minutes', 10)
            dest = Path(destination)
            with tempfile.NamedTemporaryFile(prefix='dywali-export-', suffix='.zip',
                    dir=dest.parent, delete=False) as temporary:
                staging = Path(temporary.name)
            try:
                with zipfile.ZipFile(staging, 'w', compression=zipfile.ZIP_DEFLATED,
                                     compresslevel=5) as archive:
                    for index, frame in enumerate(frames):
                        path = Path(frame['path'])
                        suffix = path.suffix.lower()
                        if suffix not in ('.jpg', '.jpeg', '.png'):
                            raise ValueError(f'Unsupported format: {path.name}')
                        internal = f'images/{index:04d}{suffix}'
                        archive.write(path, internal)
                        if random_mode:
                            manifest['frames'].append({'image': internal})
                        else:
                            manifest['frames'].append({'time':frame['time'],
                                'mode':frame.get('mode','blend'), 'image': internal})
                    archive.writestr('collection.json', json.dumps(manifest,ensure_ascii=False,indent=2))
                staging.replace(dest)
            finally:
                staging.unlink(missing_ok=True)
            self.message(_('Collection exported: {path}').format(path=dest))
        except (OSError, ValueError, zipfile.BadZipFile) as exc:
            self.message(_('Could not export: {error}').format(error=exc), True)

    def import_collection(self, *args):
        dialog = Gtk.FileChooserDialog(_("Import collection ZIP" ), self, Gtk.FileChooserAction.OPEN,
            (Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL, Gtk.STOCK_OPEN, Gtk.ResponseType.OK))
        filter_zip = Gtk.FileFilter()
        filter_zip.set_name(_('ZIP collections (*.zip)'))
        filter_zip.add_pattern('*.zip')
        dialog.add_filter(filter_zip)
        filename = dialog.get_filename() if dialog.run() == Gtk.ResponseType.OK else None
        dialog.destroy()
        if not filename:
            return
        try:
            with zipfile.ZipFile(filename) as archive:
                members = {info.filename: info for info in archive.infolist()}
                if 'collection.json' not in members:
                    raise ValueError(_('Missing collection.json.'))
                if members['collection.json'].file_size > 1_000_000:
                    raise ValueError(_("Configuration is too large." ))
                manifest = json.loads(archive.read('collection.json'))
                if manifest.get('format') not in ('dywali-collection', 'dynamic-wallpaper-studio-collection') or manifest.get('version') != 1:
                    raise ValueError(_("Incompatible collection format." ))
                random_mode = manifest.get('type') == 'slideshow'
                frames = manifest.get('frames')
                if not isinstance(frames,list) or not (1 if random_mode else 2) <= len(frames) <= 500:
                    raise ValueError(_("Collection must have between 2 and 500 images." ))
                original_name = manifest.get('name')
                if not isinstance(original_name,str) or not original_name.strip():
                    raise ValueError(_("Invalid collection name." ))
                # Comprobar completamente el archivo antes de copiar nada.
                verified = []
                used = set()
                total_size = 0
                for frame in frames:
                    if not isinstance(frame,dict):
                        raise ValueError(_('Invalid frame.'))
                    internal = frame.get('image')
                    if not isinstance(internal,str) or not re.fullmatch(r'images/[0-9]{4}\.(jpg|jpeg|png)',internal):
                        raise ValueError(_("Invalid internal image path." ))
                    if internal not in members:
                        raise ValueError(_('An image is missing from the ZIP.'))
                    # Una imagen puede aparecer en varios fotogramas con horarios
                    # y modos diferentes. Solo contabilizamos su tamaño una vez.
                    if internal not in used:
                        used.add(internal)
                        size = members[internal].file_size
                        total_size += size
                        if size > 100_000_000 or total_size > 1_000_000_000:
                            raise ValueError(_("ZIP contains oversized images." ))
                    time = frame.get('time') if not random_mode else f'{len(verified)//60:02d}:{len(verified)%60:02d}'
                    if not isinstance(time,str) or not re.fullmatch(r'[0-2][0-9]:[0-5][0-9]',time):
                        raise ValueError(_("Invalid time." ))
                    self._minute_of(time)
                    mode = frame.get('mode','blend')
                    if mode not in ('blend','hold'):
                        raise ValueError(_('Unknown mode.'))
                    verified.append((time, mode, internal))
                if not random_mode and len({t for t,_,_ in verified}) != len(verified):
                    raise ValueError(_('Duplicate times.'))
                root = Path.home()/'.local/share/dywali/importados-colecciones'
                root.mkdir(parents=True, exist_ok=True)
                with tempfile.TemporaryDirectory(prefix='dws-import-',dir=root) as temp:
                    folder = Path(temp)
                    for internal in used:
                        path = folder / Path(internal).name
                        with archive.open(internal) as source, path.open('wb') as output:
                            shutil.copyfileobj(source, output)
                        with Image.open(path) as img:
                            img.verify()
                    name = original_name.strip()
                    if name in self.data['collections']:
                        number = 2
                        while f'{name} ({number})' in self.data['collections']:
                            number += 1
                        name = f'{name} ({number})'
                    import uuid
                    final_folder = root / uuid.uuid4().hex
                    folder.rename(final_folder)
                    imported = [{'time':time,'mode':mode,
                                 'path':str(final_folder / Path(internal).name)}
                                for time,mode,internal in verified]
                    # El motor debe poder abrir todas las imágenes al finalizar.
                    if not random_mode: validate_frames(imported)
                    self._autosave_pending() if self.name() else None
                    if random_mode:
                        self.data['collections'][name] = {
                            'type': 'slideshow', 'images': [f['path'] for f in imported],
                            'slide_minutes': int(manifest.get('slide_minutes', 10))}
                    else:
                        self.data['collections'][name] = {'frames': imported}
                    self.data['active'] = name
                    save_config(self.data)
            self.populate(name)
            self.message(_('Collection imported: {name} ({count} frames).').format(name=name, count=len(frames)))
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError,
                zipfile.BadZipFile) as exc:
            self.message(_('Could not import: {error}').format(error=exc), True)

    def button(self, parent, label, callback):
        b = Gtk.Button(label=label)
        b.connect('clicked',callback)
        parent.pack_start(b,False,False,0)
        return b

    def update_action_buttons(self):
        selected = self._selected_frame_index() is not None
        for button in (self.btn_replace, self.btn_time, self.btn_mode, self.btn_remove):
            button.set_sensitive(selected)
        active_collection = self.data['collections'].get(self.name(), {}) if self.name() else {}
        unavailable = (active_collection.get('type') in ('spotlight', 'bing', 'wikimedia', 'apod')
                       and not provider_available(active_collection.get('type')))
        self.btn_apply.set_sensitive(len(self.store) > 0 and not unavailable)
        self.slide_next.set_sensitive(len(self.store) > 0)

    def name(self):
        return self.combo.get_active_text()

    def populate(self, name=None):
        self.combo.handler_block_by_func(self.changed_collection)
        self.combo.remove_all()
        names = list(self.data['collections'])
        for n in names:
            self.combo.append_text(n)
        self.combo.set_active(names.index(name or self.data['active']) if names else -1)
        self.combo.handler_unblock_by_func(self.changed_collection)
        self.load_frames()

    def load_frames(self):
        self._loading = True
        # These links are siblings of the provider info labels. Clear them before
        # rendering a new collection so a previous provider URL cannot persist.
        self._hide_apod_page_link()
        self._hide_wikimedia_page_link()
        self.store.clear()
        name = self.name()
        collection = self.data['collections'].get(name, {}) if name else {}
        random_mode = collection.get('type') == 'slideshow'
        spotlight_mode = collection.get('type') == 'spotlight' and provider_available('spotlight')
        wikimedia_mode = collection.get('type') == 'wikimedia' and provider_available('wikimedia')
        apod_mode = collection.get('type') == 'apod' and provider_available('apod')
        bing_mode = collection.get('type') == 'bing' and provider_available('bing')
        raw_mode = collection.get('type', 'dynamic')
        unsupported_mode = (raw_mode in ('spotlight', 'bing', 'wikimedia', 'apod')
                            and not provider_available(raw_mode))
        online_mode = spotlight_mode or wikimedia_mode or apod_mode or bing_mode or unsupported_mode
        simple_mode = random_mode or online_mode
        self.mode_combo.set_active_id(raw_mode if raw_mode in ('spotlight', 'wikimedia', 'apod', 'bing')
                                      else ('slideshow' if random_mode else 'dynamic'))
        if spotlight_mode:
            self.spotlight_controls.show_all()
            self.spotlight_minutes.set_active_id(str(collection.get('spotlight_minutes',int(collection.get('spotlight_hours',24))*60)))
            self.spotlight_info.show()
            self.spotlight_countdown.show()
            self._spotlight_update_info()
            self.refresh_spotlight_countdown()
        else:
            self.spotlight_controls.hide()
            self.spotlight_info.hide()
            self.spotlight_countdown.hide()
        if wikimedia_mode:
            self.wikimedia_controls.show_all()
            self.wikimedia_behavior.set_active_id(collection.get('wikimedia_behavior', 'potd'))
            self.wikimedia_minutes.set_active_id(str(collection.get('wikimedia_minutes', 1440)))
            self.wikimedia_info.show()
            self._wikimedia_update_info()
        else:
            self.wikimedia_controls.hide()
            self.wikimedia_info.hide()
        if apod_mode:
            self.apod_controls.show_all()
            self.apod_minutes.set_active_id(str(collection.get('apod_minutes', 1440)))
            self.apod_info.show()
            self._apod_update_info()
        else:
            self.apod_controls.hide()
            self.apod_info.hide()
        if bing_mode:
            self.bing_controls.show_all()
            self.bing_behavior.set_active_id(collection.get('bing_behavior', 'daily'))
            self.bing_minutes.set_active_id(str(collection.get('bing_minutes', 1440)))
            self.bing_rotate.set_active_id(str(collection.get('bing_rotate_minutes', 30)))
            self.bing_order.set_active_id(collection.get('bing_order', 'sequential'))
            self.bing_auto.set_active(bool(collection.get('bing_auto', True)))
            recent = collection.get('bing_behavior', 'daily') == 'recent'
            self.bing_check_label.set_visible(not recent)
            self.bing_minutes.set_visible(not recent)
            self.bing_rotate_label.set_visible(recent)
            self.bing_order_label.set_visible(recent)
            self.bing_rotate.set_visible(recent)
            self.bing_order.set_visible(recent)
            self.bing_auto.set_visible(recent)
            self.bing_info.show()
            self._bing_update_info()
        else:
            self.bing_controls.hide()
            self.bing_info.hide()
            self.bing_countdown.hide()
        # Controlar explícitamente la visibilidad del grupo, sin no-show-all.
        # show_all() también hace visibles la etiqueta, el selector y el botón.
        if random_mode:
            self.slideshow_controls.show_all()
        else:
            self.slideshow_controls.hide()
        self.auto_every_label.set_visible(not simple_mode)
        self.interval.set_visible(not simple_mode)
        self.timeline_header.set_visible(not simple_mode)
        self.timeline.set_visible(not simple_mode)
        self.scale.get_parent().set_visible(not simple_mode)
        self.frames_header.set_visible(not online_mode)
        self.frame_scroll.set_visible(not online_mode)
        self.edit_controls.set_visible(not online_mode)
        self.frames_header.set_text(_('Images in slideshow:') if random_mode else
                                   _('Frames · each mode applies from its time until the next:'))
        for col in (self.col_time, self.col_mode, self.col_duration):
            col.set_visible(not simple_mode)
        self.btn_time.set_visible(not simple_mode)
        self.btn_mode.set_visible(not simple_mode)
        if random_mode:
            self.slide_interval.set_active_id(str(collection.get('slide_minutes', 10)))
            for index, path in enumerate(collection.get('images', [])):
                minute = index % 1440
                self.store.append([f'{minute//60:02d}:{minute%60:02d}', path,
                                   self.thumbnail(path), 'Fija'])
        elif name and not online_mode:
            for f in sorted(collection.get('frames', []), key=lambda f:f['time']):
                self.store.append([f['time'], f['path'], self.thumbnail(f['path']),
                                   'Fija' if f.get('mode') == 'hold' else 'Transición'])
        self._loading = False
        self._remember_slots()
        self._drag_source_index = None
        self._loaded_name = name
        self.update_preview()
        self.update_action_buttons()
        if unsupported_mode:
            label = _('Spotlight') if raw_mode == 'spotlight' else _('Bing')
            self.status.set_text(_('{provider} is not included in this Dywali edition. '
                                   'Your collection and active selection were preserved; no image was applied.')
                                 .format(provider=label))

    def thumbnail(self, filename):
        try:
            return GdkPixbuf.Pixbuf.new_from_file_at_scale(filename, 100, 55, True)
        except Exception:
            return None

    def frames(self):
        return [{'time':row[0], 'path':row[1], 'mode':'hold' if row[3]=='Fija' else 'blend'} for row in self.store]

    def changed_collection(self,*args):
        if self._save_source:
            GLib.source_remove(self._save_source)
            self._save_source = None
            try:
                old_frames = self.frames()
                if self._loaded_name in self.data['collections']:
                    previous = self.data['collections'][self._loaded_name]
                    if previous.get('type') in ('spotlight', 'wikimedia'):
                        pass
                    elif previous.get('type') == 'slideshow':
                        previous['images'] = [row[1] for row in self.store]
                    else:
                        validate_frames(old_frames)
                        previous['frames'] = old_frames
                    save_config(self.data)
            except ValueError:
                pass
        self.load_frames()
        if self.name():
            self.data['active'] = self.name()
            save_config(self.data)
            self.sync_scheduler()

    def message(self, text, error=False):
        self.status.set_text(text)
        if error:
            d=Gtk.MessageDialog(transient_for=self,flags=0,message_type=Gtk.MessageType.ERROR,buttons=Gtk.ButtonsType.OK,text=text)
            d.run(); d.destroy()

    def _remember_slots(self):
        """Actualizar la referencia de horas/modos tras una edición ordinaria."""
        self._time_slots = [(row[0], row[3]) for row in self.store]

    def on_store_changed(self, *args):
        if self._loading:
            return
        self._remember_slots()
        self.tree.queue_draw()
        self.schedule_save()
        self.update_action_buttons()

    def on_image_drag_begin(self, widget, context):
        model, itr = self.tree.get_selection().get_selected()
        self._drag_source_index = model.get_path(itr).get_indices()[0] if itr else None

    def on_image_drag_data_get(self, widget, context, selection, info, timestamp):
        if self._drag_source_index is not None:
            selection.set(selection.get_target(), 8,
                          str(self._drag_source_index).encode('ascii'))

    def on_image_drag_data_received(self, widget, context, x, y, selection, info, timestamp):
        success = False
        try:
            source = int(bytes(selection.get_data()).decode('ascii'))
            n = len(self.store)
            dest = self.tree.get_dest_row_at_pos(x, y)
            if not (0 <= source < n) or dest is None:
                raise ValueError(_("Invalid drop position" ))
            path, position = dest
            target = path.get_indices()[0]
            if position in (Gtk.TreeViewDropPosition.AFTER,
                            Gtk.TreeViewDropPosition.INTO_OR_AFTER):
                target += 1
            # Transformamos la posición de inserción tras retirar la imagen origen.
            if source < target:
                target -= 1
            target = max(0, min(n - 1, target))
            if source != target:
                # Capturar antes de modificar nada. Los horarios y modos
                # permanecen asociados a su POSICIÓN, no a la imagen.
                slots = [(row[0], row[3]) for row in self.store]
                images = [(row[1], row[2]) for row in self.store]
                moved = images.pop(source)
                images.insert(target, moved)
                self._loading = True
                try:
                    for idx, (path_name, pixbuf) in enumerate(images):
                        self.store[idx][1] = path_name
                        self.store[idx][2] = pixbuf
                        assert (self.store[idx][0], self.store[idx][3]) == slots[idx]
                finally:
                    self._loading = False
                self.tree.get_selection().select_path(Gtk.TreePath.new_from_indices([target]))
                self.schedule_save()
                self.update_preview()
                self.message(_("Images reordered; times and modes unchanged." ))
            success = True
        except (ValueError, TypeError, AttributeError) as error:
            self.message(_('Could not move image: {error}').format(error=error))
        finally:
            Gtk.drag_finish(context, success, False, timestamp)

    def on_image_drag_end(self, widget, context):
        self._drag_source_index = None

    def schedule_save(self):
        if self._loading or not self.name():
            return
        if self._save_source is not None:
            GLib.source_remove(self._save_source)
        self._save_source = GLib.timeout_add(500, self._autosave)

    def _autosave(self):
        self._save_source = None
        if not self.name():
            return False
        try:
            if self.is_spotlight() or self.is_wikimedia() or self.is_apod() or self.is_bing():
                pass
            elif self.is_slideshow():
                self.data['collections'][self.name()]['images'] = [row[1] for row in self.store]
            else:
                frames = self.frames()
                validate_frames(frames)
                self.data['collections'][self.name()]['frames'] = frames
            self.data['active'] = self.name()
            save_config(self.data)
            self.status.set_text(_("Changes saved automatically." ))
        except (ValueError, KeyError) as err:
            self.status.set_text(_("Unsaved: " ) + str(err))
        return False

    def ask_name(self, title, default=''):
        dialog = Gtk.Dialog(title, self, 0, (Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL, Gtk.STOCK_OK, Gtk.ResponseType.OK))
        entry = Gtk.Entry(text=default)
        entry.set_activates_default(True)
        dialog.set_default_response(Gtk.ResponseType.OK)
        dialog.get_content_area().pack_start(entry, True, True, 10)
        dialog.show_all()
        result = entry.get_text().strip() if dialog.run() == Gtk.ResponseType.OK else None
        dialog.destroy()
        return result

    def duplicate_collection(self, *args):
        original = self.name()
        if not original:
            return
        name = self.ask_name(_("Duplicate collection" ), original + ' - copia')
        if name is None:
            return
        if not name or name in self.data['collections']:
            self.message(_("The name must be new and nonempty." ), True)
            return
        self._autosave_pending()
        self.data['collections'][name] = copy.deepcopy(self.data['collections'][original])
        self.data['active'] = name
        save_config(self.data)
        self.populate(name)
        self.message(_('Collection duplicated: {name}').format(name=name))

    def rename_collection(self, *args):
        original = self.name()
        if not original:
            return
        name = self.ask_name(_("Rename collection" ), original)
        if name is None or name == original:
            return
        if not name or name in self.data['collections']:
            self.message(_("The name must be new and nonempty." ), True)
            return
        self._autosave_pending()
        # Solo cambia el nombre; conserva frames y rutas existentes.
        self.data['collections'][name] = self.data['collections'].pop(original)
        self.data['active'] = name
        save_config(self.data)
        self.populate(name)
        self.message(_('Collection renamed: {name}').format(name=name))

    def _autosave_pending(self):
        if self._save_source is not None:
            GLib.source_remove(self._save_source)
            self._save_source = None
        self._autosave()

    def new_collection(self,*args):
        dialog = Gtk.Dialog(_("New collection"), self, Gtk.DialogFlags.MODAL,
                            (Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
                             Gtk.STOCK_OK, Gtk.ResponseType.OK))
        entry = Gtk.Entry()
        entry.set_placeholder_text(_("Collection name"))
        entry.set_activates_default(True)
        dialog.set_default_response(Gtk.ResponseType.OK)
        dialog.get_content_area().add(entry)
        dialog.show_all()
        response = dialog.run()
        name = entry.get_text().strip() if response == Gtk.ResponseType.OK else None
        # Cerrarlo ANTES de actualizar la ventana principal evita que quede visible.
        dialog.hide()
        dialog.destroy()
        if name is None:
            return
        if not name or name in self.data['collections']:
            self.message(_("Enter a new, nonempty name."), True)
            return
        self.data['collections'][name] = {'frames': []}
        self.data['active'] = name
        save_config(self.data)
        self.populate(name)

    def delete_collection(self,*args):
        name=self.name()
        if len(self.data['collections'])<=1:
            self.message(_("At least one collection must remain." ),True); return
        del self.data['collections'][name]
        self.data['active']=next(iter(self.data['collections']))
        save_config(self.data); self.populate()

    def _ordered_frame_intervals(self):
        """(fila, inicio, fin) en minutos; el último intervalo cruza medianoche."""
        ordered = []
        for i, row in enumerate(self.store):
            try:
                h, m = map(int, row[0].split(':'))
                if 0 <= h < 24 and 0 <= m < 60:
                    ordered.append((h * 60 + m, i))
            except (ValueError, TypeError):
                continue
        ordered.sort()
        if not ordered:
            return []
        if len(ordered) == 1:
            return [(ordered[0][1], 0, 1440)]
        return [(idx, start, ordered[(n + 1) % len(ordered)][0] + (1440 if n == len(ordered)-1 else 0))
                for n, (start, idx) in enumerate(ordered)]

    def _selected_frame_index(self):
        model, itr = self.tree.get_selection().get_selected()
        return model.get_path(itr).get_indices()[0] if itr is not None else None

    def _select_frame(self, index, seek=True):
        if index is None or not 0 <= index < len(self.store):
            return
        self._ignore_selection = True
        try:
            path = Gtk.TreePath.new_from_indices([index])
            self.tree.get_selection().select_path(path)
            self.tree.scroll_to_cell(path, None, False, 0, 0)
        finally:
            self._ignore_selection = False
        if seek:
            for idx, start, end in self._ordered_frame_intervals():
                if idx == index:
                    middle = int((start + end) / 2) % 1440
                    self.scale.set_value(middle)
                    break
        self.timeline.queue_draw()
        self.update_action_buttons()

    def on_frame_selected(self, selection):
        if self._loading or self._ignore_selection:
            return
        self.update_action_buttons()
        index = self._selected_frame_index()
        if index is not None:
            self._select_frame(index, seek=True)
        else:
            self.timeline.queue_draw()

    def _frame_at_minute(self, minute):
        for index, start, end in self._ordered_frame_intervals():
            if start <= minute < end or start <= minute + 1440 < end:
                return index
        return None

    def selected(self):
        model, itr=self.tree.get_selection().get_selected()
        if itr is None:
            self.message(_("Select an image from the list first." )); return None
        return itr

    @staticmethod
    def _natural_image_key(filename):
        """Orden natural: imagen-2 antes de imagen-10."""
        return [int(part) if part.isdigit() else part.casefold()
                for part in re.split(r'(\d+)', Path(filename).name)]

    @staticmethod
    def _initial_times(existing, count):
        """Asignar horas únicas sin alterar los horarios existentes."""
        occupied = set(existing)
        if len(occupied) + count > 1440:
            raise ValueError(_("No more unique frame times are available." ))
        # Distribución en 24 horas para colecciones nuevas.
        # En colecciones existentes se buscan horas libres cercanas al reparto ideal.
        result = []
        for i in range(count):
            ideal = round(i * 1440 / count) % 1440
            if ideal not in occupied:
                chosen = ideal
            else:
                # Favorecer pasos de cinco minutos para facilitar la edición.
                candidates = [(ideal + step * 5) % 1440 for step in range(1, 289)]
                candidates += [(ideal + step) % 1440 for step in range(1, 1440)]
                chosen = next((minute for minute in candidates if minute not in occupied), None)
                if chosen is None:
                    raise ValueError('No quedan horarios libres.')
            result.append(chosen)
            occupied.add(chosen)
        return result

    def add_frame(self,*args):
        dialog = Gtk.FileChooserDialog(
            _("Add images" ), self, Gtk.FileChooserAction.OPEN,
            (Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
             Gtk.STOCK_OPEN, Gtk.ResponseType.OK))
        dialog.set_select_multiple(True)
        filt = Gtk.FileFilter()
        filt.set_name(_("JPEG and PNG images" ))
        for pat in ('*.jpg', '*.jpeg', '*.png', '*.JPG', '*.JPEG', '*.PNG'):
            filt.add_pattern(pat)
        dialog.add_filter(filt)
        filenames = dialog.get_filenames() if dialog.run() == Gtk.ResponseType.OK else []
        dialog.destroy()
        if not filenames:
            return
        filenames = sorted(filenames, key=self._natural_image_key)
        valid = []
        invalid = []
        for filename in filenames:
            try:
                with Image.open(filename) as img:
                    img.verify()
                thumb = self.thumbnail(filename)
                if thumb is None:
                    raise ValueError('No se pudo crear la miniatura')
                valid.append((filename, thumb))
            except (OSError, ValueError) as exc:
                invalid.append(f'{Path(filename).name}: {exc}')
        if not valid:
            self.message(_("Could not open any selected images." ), True)
            return
        if self.is_slideshow():
            self._loading = True
            try:
                for filename, thumb in valid:
                    index = len(self.store) % 1440
                    self.store.append([f'{index//60:02d}:{index%60:02d}', filename, thumb, 'Fija'])
            finally:
                self._loading = False
            self.schedule_save()
            self.update_preview()
            return
        existing = []
        for row in self.store:
            h, m = map(int, row[0].split(':'))
            existing.append(h * 60 + m)
        try:
            times = self._initial_times(existing, len(valid))
        except ValueError as exc:
            self.message(str(exc), True)
            return
        # Inserción atómica a nivel de GUI para impedir guardados intermedios.
        self._loading = True
        try:
            for (filename, thumb), minute in zip(valid, times):
                self.store.append([f'{minute//60:02d}:{minute%60:02d}',
                                   filename, thumb, 'Transición'])
        finally:
            self._loading = False
        self._remember_slots()
        self.tree.queue_draw()
        self.schedule_save()
        self.update_preview()
        if invalid:
            self.message(_('Added {count} images; skipped {invalid} invalid files: {files}').format(count=len(valid), invalid=len(invalid), files='; '.join(invalid)))
        else:
            self.message(_('Added {count} images in natural order. You can adjust their times; automatic save is enabled.').format(count=len(valid)))

    def replace_frame(self, *args):
        """Sustituir solo el archivo de la fila seleccionada, manteniendo su horario y modo."""
        itr = self.selected()
        if itr is None:
            return
        dialog = Gtk.FileChooserDialog(
            _("Replace image" ), self, Gtk.FileChooserAction.OPEN,
            (Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
             Gtk.STOCK_OPEN, Gtk.ResponseType.OK))
        filt = Gtk.FileFilter()
        filt.set_name(_("JPEG and PNG images" ))
        for pat in ('*.jpg', '*.jpeg', '*.png', '*.JPG', '*.JPEG', '*.PNG'):
            filt.add_pattern(pat)
        dialog.add_filter(filt)
        # Partimos de la carpeta de la imagen seleccionada cuando exista.
        current = Path(self.store[itr][1])
        if current.parent.is_dir():
            dialog.set_current_folder(str(current.parent))
        filename = dialog.get_filename() if dialog.run() == Gtk.ResponseType.OK else None
        dialog.destroy()
        if not filename:
            return
        try:
            # Verificamos que Pillow puede leerla antes de alterar la colección.
            with Image.open(filename) as img:
                img.verify()
            pixbuf = self.thumbnail(filename)
            if pixbuf is None:
                raise ValueError(_('Could not create thumbnail.'))
        except Exception as exc:
            self.message(_('Could not open selected image: {error}').format(error=exc), True)
            return
        # Modificamos solo ruta y miniatura. Hora y modo no cambian.
        self.store[itr][1] = filename
        self.store[itr][2] = pixbuf
        # Las señales row-changed activan el guardado automático de v0.6.
        self.update_preview()
        self.message(_("Image replaced. Time and mode preserved; automatic save pending." ))

    def import_heic(self,*args):
        if not shutil.which('heif-convert'):
            self.message(_('Install libheif-examples: sudo apt install libheif-examples'),True)
            return
        dialog=Gtk.FileChooserDialog(_("Import HEIC" ),self,Gtk.FileChooserAction.OPEN,(Gtk.STOCK_CANCEL,Gtk.ResponseType.CANCEL,Gtk.STOCK_OPEN,Gtk.ResponseType.OK))
        filt=Gtk.FileFilter();filt.set_name(_('HEIC files'));filt.add_pattern('*.heic');filt.add_pattern('*.HEIC');dialog.add_filter(filt)
        filename=dialog.get_filename() if dialog.run()==Gtk.ResponseType.OK else None
        dialog.destroy()
        if not filename:return
        target=Path.home()/'.local/share/dywali/importados'/Path(filename).stem
        target.mkdir(parents=True,exist_ok=True)
        before=set(target.glob('*.jpg'))
        p=subprocess.run(['heif-convert','-q','95',filename,str(target/'imagen.jpg')],capture_output=True,text=True)
        if p.returncode:
            self.message(_('HEIC extraction error: {error}').format(error=p.stderr.strip()),True);return
        images=sorted(set(target.glob('*.jpg'))-before)
        if not images:
            # Algunas versiones sobreescriben el destino en vez de numerar imágenes.
            images=sorted(target.glob('*.jpg'))
        if not images:
            self.message(_("No images were extracted from the file." ),True);return
        if self.is_slideshow():
            self._loading = True
            try:
                for frame in images:
                    minute = len(self.store) % 1440
                    self.store.append([f'{minute//60:02d}:{minute%60:02d}',
                                       str(frame), self.thumbnail(str(frame)), 'Fija'])
            finally:
                self._loading = False
            self.schedule_save()
        else:
            # Propuesta de horas equidistantes, revisable antes de guardar.
            for i,frame in enumerate(images):
                t=round(i*1440/len(images))%1440
                self.store.append([f'{t//60:02d}:{t%60:02d}',str(frame),self.thumbnail(str(frame)),'Transición'])
        self.message(_('Imported {count} images. Check their times; changes are saved automatically.').format(count=len(images)))
        self.update_preview()

    def toggle_auto(self, switch, _param):
        if getattr(self,'_changing_switch',False):return
        try:
            if switch.get_active():
                if not self.save(): raise RuntimeError(_("Fix the collection before enabling updates." ))
                interval=int(self.interval.get_active_id())
                slideshow = self.is_slideshow()
                scheduler.install(1 if (self.is_spotlight() or self.is_wikimedia() or self.is_apod() or self.is_bing() or slideshow) else interval)
                if self.is_spotlight():
                    minutes=self.spotlight_interval_minutes()
                    self.message(_('Spotlight enabled. Checking for new images every {interval}.').format(interval=self.spotlight_interval_label(minutes)))
                elif self.is_wikimedia():
                    minutes=self.wikimedia_interval_minutes()
                    self.message(_('Wikimedia enabled. Checking for a new image every {interval}.').format(interval=self.spotlight_interval_label(minutes)))
                elif self.is_apod():
                    minutes=self.apod_interval_minutes()
                    self.message(_('NASA APOD enabled. Checking for a new image every {interval}.').format(interval=self.spotlight_interval_label(minutes)))
                elif self.is_bing():
                    minutes=self.bing_interval_minutes()
                    self.message(_('Bing Wallpaper enabled. Checking for a new image every {interval}.').format(interval=self.spotlight_interval_label(minutes)))
                elif slideshow:
                    slide_minutes = int(self.data['collections'][self.name()].get('slide_minutes', 10))
                    self.message(_('Automatic updates enabled. Slideshow changes every {interval} minutes.').format(interval=slide_minutes))
                else:
                    self.message(_('Automatic updates enabled every {interval} minutes.').format(interval=interval))
            else:
                scheduler.disable()
                self.message(_("Automatic updates disabled." ))
        except Exception as e:
            self._changing_switch=True
            switch.set_active(scheduler.enabled() if scheduler.available() else False)
            self._changing_switch=False
            self.message(str(e),True)

    def interval_changed(self,*args):
        if not self.interval.get_active_id():return
        self.data['interval_minutes']=int(self.interval.get_active_id())
        if self.auto_switch.get_active():
            try:
                if self.save():scheduler.install(1 if (self.is_spotlight() or self.is_wikimedia() or self.is_apod() or self.is_bing() or self.is_slideshow()) else self.data['interval_minutes'])
            except Exception as e:self.message(str(e),True)
        else:
            save_config(self.data)

    def edit_time(self,*args):
        itr=self.selected()
        if itr is None:return
        dialog=Gtk.Dialog(_("Change time" ),self,0,(Gtk.STOCK_CANCEL,Gtk.ResponseType.CANCEL,Gtk.STOCK_OK,Gtk.ResponseType.OK))
        entry=Gtk.Entry(text=self.store[itr][0]);entry.set_placeholder_text('HH:MM')
        dialog.get_content_area().add(entry);dialog.show_all()
        if dialog.run()==Gtk.ResponseType.OK:
            t=entry.get_text().strip()
            try:
                h,m=map(int,t.split(':'))
                if not (0<=h<24 and 0<=m<60):raise ValueError()
                self.store[itr][0]=f'{h:02d}:{m:02d}'
            except ValueError:self.message(_("Invalid time; use HH:MM." ),True)
        dialog.destroy();self.update_preview()

    def edit_mode(self,*args):
        itr=self.selected()
        if itr is None:return
        dialog=Gtk.Dialog(_("Behavior until next image" ),self,0,
                          (Gtk.STOCK_CANCEL,Gtk.ResponseType.CANCEL,Gtk.STOCK_OK,Gtk.ResponseType.OK))
        area=dialog.get_content_area()
        area.set_spacing(8)
        label=Gtk.Label(label=_('The selected mode applies from this image’s time until the next.\nTransition: gradually blends. Fixed: keeps this image unchanged.'), xalign=0)
        area.pack_start(label,False,False,8)
        combo=Gtk.ComboBoxText()
        combo.append('blend', _('Smooth transition'))
        combo.append('hold',_("Keep image fixed" ))
        combo.set_active_id('hold' if self.store[itr][3]=='Fija' else 'blend')
        area.pack_start(combo,False,False,8)
        dialog.show_all()
        if dialog.run()==Gtk.ResponseType.OK:
            self.store[itr][3]='Fija' if combo.get_active_id()=='hold' else 'Transición'
        dialog.destroy()
        self.update_preview()

    def remove_frame(self,*args):
        itr=self.selected()
        if itr is not None:self.store.remove(itr);self.update_preview()

    def save(self,*args):
        try:
            if not (self.is_slideshow() or self.is_spotlight() or self.is_wikimedia() or self.is_apod() or self.is_bing()): validate_frames(self.frames())
        except ValueError as e:self.message(str(e),True);return False
        name=self.name()
        if self.is_spotlight() or self.is_wikimedia() or self.is_apod() or self.is_bing():
            pass
        elif self.is_slideshow():
            self.data['collections'][name]['images'] = [row[1] for row in self.store]
        else:
            self.data['collections'][name]['frames'] = self.frames()
        self.data['active']=name
        self.data['interval_minutes']=int(self.interval.get_active_id())
        save_config(self.data)
        self.message(_('Saved: {name}. The current schedule was not modified.').format(name=name))
        return True

    def timeline_segments(self):
        """Devuelve intervalos [inicio, fin) para las 24 horas, incluido el cambio de día."""
        frames = []
        for row in self.store:
            try:
                hour, minute = map(int, row[0].split(':'))
                time = hour * 60 + minute
                if 0 <= time < 1440:
                    frames.append((time, row[3] == 'Fija'))
            except (ValueError, TypeError):
                pass
        frames.sort(key=lambda x: x[0])
        if not frames:
            return []
        if len(frames) == 1:
            return [(0, 1440, frames[0][1])]
        segments = []
        for idx, (start, hold) in enumerate(frames):
            end = frames[(idx + 1) % len(frames)][0]
            if idx == len(frames) - 1:
                end += 1440
            if end > 1440:
                segments.append((start, 1440, hold))
                segments.append((0, end - 1440, hold))
            else:
                segments.append((start, end, hold))
        return segments

    def draw_timeline(self, widget, cr):
        width = widget.get_allocated_width()
        height = widget.get_allocated_height()
        if width < 30 or height < 30:
            return False
        left, right = 12.0, float(width - 12)
        usable = max(1., right - left)
        top, bar_h = 10., 25.
        # GTK theme-friendly backgrounds; reserved colors represent the two modes.
        cr.set_source_rgb(0.87, 0.88, 0.90)
        cr.rectangle(left, top, usable, bar_h)
        cr.fill()
        for start, end, hold in self.timeline_segments():
            x = left + usable * start / 1440.
            w = usable * (end - start) / 1440.
            if hold:
                cr.set_source_rgb(0.34, 0.62, 0.48)  # Fija: verde tenue
            else:
                cr.set_source_rgb(0.36, 0.48, 0.77)  # Transición: azul
            cr.rectangle(x, top, w, bar_h)
            cr.fill()
        # Marco visible alrededor de la franja de la fila seleccionada.
        # Los intervalos que cruzan medianoche se pintan en dos partes.
        selected = self._selected_frame_index()
        if selected is not None:
            for idx, start, end in self._ordered_frame_intervals():
                if idx != selected:
                    continue
                pieces = [(start, min(end, 1440))]
                if end > 1440:
                    pieces.append((0, end - 1440))
                cr.set_source_rgba(0.12, 0.15, 0.20, 0.95)
                cr.set_line_width(3.)
                for a, b in pieces:
                    if b > a:
                        x = left + usable * a / 1440.
                        w = usable * (b - a) / 1440.
                        cr.rectangle(x + 1, top + 1.5, max(0, w - 2), bar_h - 3)
                        cr.stroke()
                break
        cr.set_line_width(1.)
        cr.set_source_rgba(1, 1, 1, .65)
        for hour in range(0, 25, 3):
            x = left + usable * hour / 24.
            cr.move_to(x, top)
            cr.line_to(x, top + bar_h)
        cr.stroke()
        # Marcadores de límites editables.
        cr.set_source_rgba(1, 1, 1, .95)
        for frame in self.frames():
            try:
                h, m = map(int, frame['time'].split(':'))
                x = left + usable * (h * 60 + m) / 1440.
                cr.rectangle(x - 2, top + 1, 4, bar_h - 2)
            except ValueError:
                pass
        cr.fill()
        # Time tick labels stay within window limits.
        layout = widget.create_pango_layout('')
        cr.set_source_rgb(.35, .35, .38)
        for hour in (0, 6, 12, 18, 24):
            label = f'{hour:02d}:00'
            layout.set_text(label, -1)
            tw, th = layout.get_pixel_size()
            x = left + usable * hour / 24. - tw / 2
            x = min(max(0., x), width - tw)
            cr.move_to(x, top + bar_h + 6)
            from gi.repository import PangoCairo
            PangoCairo.show_layout(cr, layout)
        minute = self.scale.get_value()
        x = left + usable * minute / 1440.
        cr.set_source_rgb(.13, .13, .16)
        cr.set_line_width(2.)
        cr.move_to(x, top - 6)
        cr.line_to(x, top + bar_h + 2)
        cr.stroke()
        return False

    def timeline_minute(self, event_x):
        width = self.timeline.get_allocated_width()
        usable = max(1., width - 24.)
        return max(0, min(1439, round((event_x - 12.) / usable * 1439)))

    def closest_marker(self, x):
        width = self.timeline.get_allocated_width()
        usable = max(1., width - 24.)
        best = None
        for row in self.store:
            hour, minute = map(int, row[0].split(':'))
            at = hour * 60 + minute
            pos = 12. + usable * at / 1440.
            distance = abs(x - pos)
            if distance <= 9 and (best is None or distance < best[0]):
                best = (distance, row[0])
        return best[1] if best else None

    def click_timeline(self, widget, event):
        if event.button != 1:
            return False
        self._drag_time = self.closest_marker(event.x)
        self._dragging = self._drag_time is not None
        if self._dragging:
            for i, row in enumerate(self.store):
                if row[0] == self._drag_time:
                    self._select_frame(i, seek=False)
                    break
        minute = self.timeline_minute(event.x)
        if not self._dragging:
            self._select_frame(self._frame_at_minute(minute), seek=False)
        self.scale.set_value(minute)
        return True

    def move_timeline(self, widget, event):
        if self._dragging:
            self.scale.set_value(self.timeline_minute(event.x))
            return True
        return False

    def release_timeline(self, widget, event):
        if event.button != 1 or not self._dragging:
            return False
        previous = self._drag_time
        self._drag_time = None
        self._dragging = False
        new_minute = self.timeline_minute(event.x)
        new_minute = round(new_minute / 5) * 5
        new_minute = min(new_minute, 1435)
        new_time = f'{new_minute//60:02d}:{new_minute%60:02d}'
        if new_time != previous:
            if any(row[0] == new_time for row in self.store):
                self.message(_('A frame already exists at that time. No change was made.'), True)
            else:
                for row in self.store:
                    if row[0] == previous:
                        row[0] = new_time
                        self.message(_('Time changed to {time}. Automatically saved.').format(time=new_time))
                        break
        self.update_preview()
        return True

    def draw_preview(self, widget, cr):
        pix = self.preview_pixbuf
        if pix is None:
            return False
        width = widget.get_allocated_width()
        height = widget.get_allocated_height()
        if width <= 0 or height <= 0:
            return False
        factor = min(width / pix.get_width(), height / pix.get_height())
        draw_w = pix.get_width() * factor
        draw_h = pix.get_height() * factor
        cr.save()
        cr.translate((width - draw_w) / 2, (height - draw_h) / 2)
        cr.scale(factor, factor)
        Gdk.cairo_set_source_pixbuf(cr, pix, 0, 0)
        cr.paint()
        cr.restore()
        return False

    def update_preview(self,*args):
        collection = self.data['collections'].get(self.name(), {}) if self.name() else {}
        kind = collection.get('type')
        if kind in ('spotlight', 'bing', 'wikimedia', 'apod') and not provider_available(kind):
            self.preview_pixbuf = None
            self.preview.queue_draw()
            return
        if self.is_spotlight():
            self._spotlight_update_info()
            return
        if self.is_wikimedia():
            self._wikimedia_update_info()
            return
        if self.is_apod():
            self._apod_update_info()
            return
        if self.is_bing():
            self._bing_update_info()
            return
        if self.is_slideshow():
            paths = [row[1] for row in self.store]
            if paths:
                from slideshow import next_image
                try:
                    path, _ = next_image({'images': paths,
                        'slide_minutes': self.data['collections'][self.name()].get('slide_minutes', 10)},
                        self.name())
                    self._display_slide(path)
                except Exception as exc:
                    self.message(str(exc), True)
            else:
                self.preview_pixbuf = None
                self.preview.queue_draw()
            return
        minute=int(self.scale.get_value())
        self.time_label.set_text(f'{minute//60:02d}:{minute%60:02d}')
        self.timeline.queue_draw()
        try:
            pic,_=render(self.frames(),minute,preview=True)
            raw=pic.tobytes()
            pix=GdkPixbuf.Pixbuf.new_from_bytes(GLib.Bytes.new(raw),GdkPixbuf.Colorspace.RGB,False,8,pic.width,pic.height,pic.width*3)
            self.preview_pixbuf = pix
            self.preview.queue_draw()
            self.status.set_text('')
        except Exception as e:
            self.preview_pixbuf = None
            self.preview.queue_draw()
            self.status.set_text(_('Preview unavailable: {error}').format(error=e))

    def apply_now(self,*args):
        if not self.save():return
        try:
            filename=apply(self.data)
            self.message(_('Wallpaper applied: {filename}').format(filename=filename))
        except (RuntimeError,ValueError,KeyError) as e:self.message(str(e),True)

if __name__=='__main__':
    win = Studio()
    # GTK initially measures all mode-specific controls during show_all().
    # Resolve the active mode first, then restore the intended initial size
    # after GTK has processed the first layout cycle. Only runs on startup:
    # user-driven resizing and subsequent mode switches stay untouched.
    win.show_all()
    win.load_frames()
    def restore_initial_size():
        win.resize(1050, 720)
        return False
    GLib.idle_add(restore_initial_size, priority=GLib.PRIORITY_LOW)
    Gtk.main()
