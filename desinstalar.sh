#!/bin/sh
# Copyright (C) 2026 roger181078
# SPDX-License-Identifier: GPL-3.0-only
set -eu
DATA="${XDG_DATA_HOME:-$HOME/.local/share}"
DEST="$DATA/dywali"
UNITDIR="$HOME/.config/systemd/user"
case "$DATA" in /*) ;; *) echo 'XDG_DATA_HOME debe ser una ruta absoluta.' >&2; exit 1 ;; esac

check_path_no_symlink() {
 candidate=$1
 while [ "$candidate" != / ]; do
  [ ! -L "$candidate" ] || { echo "Ruta con enlace simbólico no admitido: $candidate" >&2; exit 1; }
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
   && grep -Fq "$marker" "$path" || { echo "Se conserva archivo no reconocido: $path" >&2; exit 1; }
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
  [ ! -L "$path" ] && [ -f "$path" ] || { echo "Se conserva archivo no reconocido: $path" >&2; exit 1; }
 fi
done
for name in studio engine displays scheduler default_config migration i18n slideshow spotlight \
 wikimedia apod bing distribution providers; do
 for path in "$DEST/$name.pyc" "$DEST/$name.pyo" "$DEST/__pycache__/$name".*.pyc; do
  check_path_no_symlink "$path"
  [ -e "$path" ] || continue
  [ ! -L "$path" ] && [ -f "$path" ] || { echo "Se conserva bytecode no regular: $path" >&2; exit 1; }
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
  [ ! -L "$UNITDIR/$unit" ] || { echo "No se elimina enlace de unidad no reconocido: $UNITDIR/$unit" >&2; exit 1; }
  is_dywali_unit "$unit" || { echo "No se elimina unidad no reconocida: $UNITDIR/$unit" >&2; exit 1; }
  HAS_DYWALI_UNIT=1
 fi
done
if [ "$HAS_DYWALI_UNIT" -eq 1 ]; then
 if ! command -v systemctl >/dev/null 2>&1 || ! systemctl --user show-environment >/dev/null 2>&1; then
  echo 'No se pudo comprobar/detener el temporizador de usuario; no se eliminaron archivos de aplicación.' >&2
  exit 1
 fi
 systemctl --user disable --now dywali.timer || {
  echo 'No se pudo detener dywali.timer; no se eliminaron archivos de aplicación.' >&2
  exit 1
 }
 rm -f "$UNITDIR/dywali.timer" "$UNITDIR/dywali.service"
 systemctl --user daemon-reload
elif command -v systemctl >/dev/null 2>&1 && systemctl --user show-environment >/dev/null 2>&1; then
 if systemctl --user is-enabled --quiet dywali.timer 2>/dev/null \
    || systemctl --user is-active --quiet dywali.timer 2>/dev/null; then
  echo 'dywali.timer está cargado pero no hay una unidad local reconocible; se conservó la aplicación.' >&2
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
  [ ! -L "$path" ] || { echo "Se conserva archivo no regular: $path" >&2; exit 1; }
  grep -Fq 'SPDX-License-Identifier: GPL-3.0-only' "$path" || {
   echo "Se conserva archivo no reconocido: $path" >&2; exit 1;
  }
  rm -f -- "$path"
 fi
done
remove_if_owned "$DEST/LICENSE" || exit 1
remove_if_owned "$DEST/icons/dywali.svg" || exit 1
for path in "$DEST/locale/es/LC_MESSAGES/dywali.po" "$DEST/locale/es/LC_MESSAGES/dywali.mo"; do
 if [ -e "$path" ]; then
  [ ! -L "$path" ] || { echo "Se conserva enlace simbólico no reconocido: $path" >&2; exit 1; }
  rm -f -- "$path"
 fi
done
for name in studio engine displays scheduler default_config migration i18n slideshow spotlight \
 wikimedia apod bing distribution providers; do
 for path in "$DEST/$name.pyc" "$DEST/$name.pyo"; do
  [ -e "$path" ] || continue
  [ ! -L "$path" ] || { echo "Se conserva bytecode enlazado externamente: $path" >&2; exit 1; }
  rm -f -- "$path"
 done
 for path in "$DEST/__pycache__/$name".*.pyc; do
  [ -e "$path" ] || continue
  [ ! -L "$path" ] || { echo "Se conserva bytecode enlazado externamente: $path" >&2; exit 1; }
  rm -f -- "$path"
 done
done
printf 'Dywali desinstalado. Se conservaron colecciones, configuración, imágenes, cachés y estado.\n'
printf 'También se conservaron archivos desconocidos y cualquier directorio de datos no vacío.\n'
