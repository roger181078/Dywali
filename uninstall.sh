#!/bin/sh
# Copyright (C) 2026 roger181078
# SPDX-License-Identifier: GPL-3.0-only
set -eu
LOCALE=${LC_ALL:-${LC_MESSAGES:-${LANG:-}}}
LOCALE=${LOCALE%%@*}; LOCALE=${LOCALE%%.*}; LOCALE=${LOCALE%%_*}
case "$LOCALE" in es) LANG_ID=es ;; *) LANG_ID=en ;; esac
msg() {
 key=$1; shift
 case "$LANG_ID:$key" in
  en:relative_data) printf '%s\n' 'XDG_DATA_HOME must be an absolute path.' >&2 ;;
  es:relative_data) printf '%s\n' 'XDG_DATA_HOME debe ser una ruta absoluta.' >&2 ;;
  en:symlink_path) printf 'Unsupported symbolic link in path: %s\n' "$1" >&2 ;;
  es:symlink_path) printf 'Ruta con enlace simbólico no admitido: %s\n' "$1" >&2 ;;
  en:foreign_file) printf 'Leaving unrecognized file untouched: %s\n' "$1" >&2 ;;
  es:foreign_file) printf 'Se conserva archivo no reconocido: %s\n' "$1" >&2 ;;
  en:foreign_regular) printf 'Leaving non-regular unrecognized file untouched: %s\n' "$1" >&2 ;;
  es:foreign_regular) printf 'Se conserva archivo no regular: %s\n' "$1" >&2 ;;
  en:foreign_bytecode) printf 'Leaving non-regular bytecode untouched: %s\n' "$1" >&2 ;;
  es:foreign_bytecode) printf 'Se conserva bytecode no regular: %s\n' "$1" >&2 ;;
  en:foreign_unit_link) printf 'Leaving unrecognized unit symlink untouched: %s\n' "$1" >&2 ;;
  es:foreign_unit_link) printf 'No se elimina enlace de unidad no reconocido: %s\n' "$1" >&2 ;;
  en:foreign_unit) printf 'Leaving unrecognized unit untouched: %s\n' "$1" >&2 ;;
  es:foreign_unit) printf 'No se elimina unidad no reconocida: %s\n' "$1" >&2 ;;
  en:timer_check) printf '%s\n' 'Could not check/stop the user timer; application files were not removed.' >&2 ;;
  es:timer_check) printf '%s\n' 'No se pudo comprobar/detener el temporizador de usuario; no se eliminaron archivos de aplicación.' >&2 ;;
  en:timer_stop) printf '%s\n' 'Could not stop dywali.timer; application files were not removed.' >&2 ;;
  es:timer_stop) printf '%s\n' 'No se pudo detener dywali.timer; no se eliminaron archivos de aplicación.' >&2 ;;
  en:loaded_unit) printf '%s\n' 'dywali.timer is loaded but no recognizable local unit exists; the application was preserved.' >&2 ;;
  es:loaded_unit) printf '%s\n' 'dywali.timer está cargado pero no hay una unidad local reconocible; se conservó la aplicación.' >&2 ;;
  en:foreign_symlink) printf 'Leaving unrecognized symbolic link untouched: %s\n' "$1" >&2 ;;
  es:foreign_symlink) printf 'Se conserva enlace simbólico no reconocido: %s\n' "$1" >&2 ;;
  en:foreign_bytecode_link) printf 'Leaving externally linked bytecode untouched: %s\n' "$1" >&2 ;;
  es:foreign_bytecode_link) printf 'Se conserva bytecode enlazado externamente: %s\n' "$1" >&2 ;;
  en:done) printf '%s\n' 'Dywali uninstalled. Collections, configuration, images, caches, and state were preserved.' ;;
  es:done) printf '%s\n' 'Dywali desinstalado. Se conservaron colecciones, configuración, imágenes, cachés y estado.' ;;
  en:done_unknown) printf '%s\n' 'Unknown files and any non-empty data directories were also preserved.' ;;
  es:done_unknown) printf '%s\n' 'También se conservaron archivos desconocidos y cualquier directorio de datos no vacío.' ;;
 esac
}
DATA="${XDG_DATA_HOME:-$HOME/.local/share}"
DEST="$DATA/dywali"
UNITDIR="$HOME/.config/systemd/user"
case "$DATA" in /*) ;; *) msg relative_data; exit 1 ;; esac

check_path_no_symlink() {
 candidate=$1
 while [ "$candidate" != / ]; do
  [ ! -L "$candidate" ] || { msg symlink_path "$candidate"; exit 1; }
  candidate=${candidate%/*}
  [ -n "$candidate" ] || candidate=/
 done
}

check_owned() {
 path=$1
 marker=$2
 check_path_no_symlink "$path"
 if [ -e "$path" ]; then
  [ ! -L "$path" ] && [ -f "$path" ] \
    && grep -Fq "$marker" "$path" || { msg foreign_file "$path"; exit 1; }
 fi
}

# Validate every candidate first so an unrelated collision cannot lead to a
# half-uninstallation after some owned files have already been removed.
check_owned "$HOME/.local/bin/dywali" '/studio.py'
check_owned "$HOME/.local/bin/dywali-apply" '/engine.py'
check_owned "$DATA/applications/dywali.desktop" 'Name=Dywali'
check_owned "$DATA/icons/hicolor/scalable/apps/dywali.svg" 'AI-generated for Dywali'
for name in studio.py engine.py displays.py scheduler.py default_config.py migration.py \
 i18n.py slideshow.py spotlight.py wikimedia.py apod.py bing.py distribution.py providers.py; do
 check_owned "$DEST/$name" 'SPDX-License-Identifier: GPL-3.0-only'
done
check_owned "$DEST/LICENSE" 'GNU GENERAL PUBLIC LICENSE'
check_owned "$DEST/icons/dywali.svg" 'AI-generated for Dywali'
for path in "$DEST/locale/es/LC_MESSAGES/dywali.po" "$DEST/locale/es/LC_MESSAGES/dywali.mo"; do
 check_path_no_symlink "$path"
 if [ -e "$path" ]; then
   [ ! -L "$path" ] && [ -f "$path" ] || { msg foreign_file "$path"; exit 1; }
 fi
done
for name in studio engine displays scheduler default_config migration i18n slideshow spotlight \
 wikimedia apod bing distribution providers; do
 for path in "$DEST/$name.pyc" "$DEST/$name.pyo" "$DEST/__pycache__/$name".*.pyc; do
  check_path_no_symlink "$path"
  [ -e "$path" ] || continue
   [ ! -L "$path" ] && [ -f "$path" ] || { msg foreign_bytecode "$path"; exit 1; }
 done
done

is_dywali_unit() {
 case "$1" in
  dywali.timer) [ -f "$UNITDIR/$1" ] && grep -Fq 'Description=Actualización periódica Dywali' "$UNITDIR/$1" ;;
  dywali.service) [ -f "$UNITDIR/$1" ] && grep -Fq 'Description=Actualizar fondo de Dywali' "$UNITDIR/$1" ;;
 esac
}

HAS_DYWALI_UNIT=0
for unit in dywali.timer dywali.service; do
 check_path_no_symlink "$UNITDIR/$unit"
 if [ -e "$UNITDIR/$unit" ]; then
   [ ! -L "$UNITDIR/$unit" ] || { msg foreign_unit_link "$UNITDIR/$unit"; exit 1; }
   is_dywali_unit "$unit" || { msg foreign_unit "$UNITDIR/$unit"; exit 1; }
  HAS_DYWALI_UNIT=1
 fi
done
if [ "$HAS_DYWALI_UNIT" -eq 1 ]; then
 if ! command -v systemctl >/dev/null 2>&1 || ! systemctl --user show-environment >/dev/null 2>&1; then
   msg timer_check
  exit 1
 fi
 systemctl --user disable --now dywali.timer || {
   msg timer_stop
  exit 1
 }
 rm -f "$UNITDIR/dywali.timer" "$UNITDIR/dywali.service"
 systemctl --user daemon-reload
elif command -v systemctl >/dev/null 2>&1 && systemctl --user show-environment >/dev/null 2>&1; then
 if systemctl --user is-enabled --quiet dywali.timer 2>/dev/null \
    || systemctl --user is-active --quiet dywali.timer 2>/dev/null; then
   msg loaded_unit
  exit 1
 fi
fi

remove_if_owned() {
 path=$1
 if [ -e "$path" ]; then
  rm -f -- "$path"
 fi
}

# Launchers/icons are removed only when their contents identify Dywali.
remove_if_owned "$HOME/.local/bin/dywali" || exit 1
remove_if_owned "$HOME/.local/bin/dywali-apply" || exit 1
remove_if_owned "$DATA/applications/dywali.desktop" || exit 1
remove_if_owned "$DATA/icons/hicolor/scalable/apps/dywali.svg" || exit 1

# Remove known application modules from either profile. Never recurse through
# the installation tree: collections, downloaded images and unknown files stay.
for name in studio.py engine.py displays.py scheduler.py default_config.py migration.py \
 i18n.py slideshow.py spotlight.py wikimedia.py apod.py bing.py distribution.py providers.py; do
 path="$DEST/$name"
 if [ -e "$path" ]; then
   [ ! -L "$path" ] || { msg foreign_regular "$path"; exit 1; }
  grep -Fq 'SPDX-License-Identifier: GPL-3.0-only' "$path" || {
    msg foreign_file "$path"; exit 1;
  }
  rm -f -- "$path"
 fi
done
remove_if_owned "$DEST/LICENSE" || exit 1
remove_if_owned "$DEST/icons/dywali.svg" || exit 1
for path in "$DEST/locale/es/LC_MESSAGES/dywali.po" "$DEST/locale/es/LC_MESSAGES/dywali.mo"; do
 if [ -e "$path" ]; then
   [ ! -L "$path" ] || { msg foreign_symlink "$path"; exit 1; }
  rm -f -- "$path"
 fi
done
for name in studio engine displays scheduler default_config migration i18n slideshow spotlight \
 wikimedia apod bing distribution providers; do
 for path in "$DEST/$name.pyc" "$DEST/$name.pyo"; do
  [ -e "$path" ] || continue
   [ ! -L "$path" ] || { msg foreign_bytecode_link "$path"; exit 1; }
  rm -f -- "$path"
 done
 for path in "$DEST/__pycache__/$name".*.pyc; do
  [ -e "$path" ] || continue
   [ ! -L "$path" ] || { msg foreign_bytecode_link "$path"; exit 1; }
  rm -f -- "$path"
 done
done
msg done
msg done_unknown
