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
    en:no_sudo) printf '%s\n' 'Run the installer without sudo.' >&2 ;;
    es:no_sudo) printf '%s\n' 'Ejecuta el instalador sin sudo.' >&2 ;;
    en:missing_distribution) printf '%s\n' 'Missing distribution.py' >&2 ;;
    es:missing_distribution) printf '%s\n' 'Falta distribution.py' >&2 ;;
    en:bad_profile) printf '%s\n' 'PROFILE must contain exactly one literal constant.' >&2 ;;
    es:bad_profile) printf '%s\n' 'PROFILE debe contener una constante literal única.' >&2 ;;
    en:unknown_profile) printf '%s\n' 'Unknown Dywali profile.' >&2 ;;
    es:unknown_profile) printf '%s\n' 'Perfil Dywali desconocido.' >&2 ;;
    en:missing_file) printf 'Missing required safe file: %s\n' "$1" >&2 ;;
    es:missing_file) printf 'Falta un archivo seguro requerido: %s\n' "$1" >&2 ;;
    en:dependencies) printf '%s\n' 'Missing dependencies: sudo apt install python3-gi gir1.2-gtk-3.0 python3-pil' >&2 ;;
    es:dependencies) printf '%s\n' 'Faltan dependencias: sudo apt install python3-gi gir1.2-gtk-3.0 python3-pil' >&2 ;;
    en:systemd_dir_link) printf 'Externally linked systemd directory: %s\n' "$1" >&2 ;;
    es:systemd_dir_link) printf 'Directorio systemd enlazado externamente: %s\n' "$1" >&2 ;;
    en:unit_link) printf 'Unsupported symbolic link for unit: %s\n' "$1" >&2 ;;
    es:unit_link) printf 'Enlace simbólico de unidad no admitido: %s\n' "$1" >&2 ;;
    en:foreign_unit) printf 'Foreign or unrecognized unit: %s\n' "$1" >&2 ;;
    es:foreign_unit) printf 'Unidad ajena o no reconocida: %s\n' "$1" >&2 ;;
    en:legacy_unit) printf 'Unrecognized legacy unit; leaving unchanged: %s\n' "$1" >&2 ;;
    es:legacy_unit) printf 'Unidad heredada no reconocida; no se modifica: %s\n' "$1" >&2 ;;
    en:preserve_unsupported) printf "Active collection '%s' preserved; timer remains stopped in public-1.0.\n" "$1" >&2 ;;
    es:preserve_unsupported) printf "Colección activa '%s' conservada; temporizador queda detenido en public-1.0.\n" "$1" >&2 ;;
    en:preserve_unknown) printf "Active collection '%s' not verified; timer remains stopped.\n" "$1" >&2 ;;
    es:preserve_unknown) printf "Colección activa '%s' no verificada; temporizador queda detenido.\n" "$1" >&2 ;;
    en:legacy_service) printf 'Preserving unrecognized service unit: %s\n' "$1" >&2 ;;
    es:legacy_service) printf 'Se conserva unidad de servicio no reconocida: %s\n' "$1" >&2 ;;
    en:installed) printf '\nDywali 1.0.1 (%s) installed. Launch with: %s/.local/bin/dywali\n' "$1" "$2" ;;
    es:installed) printf '\nDywali 1.0.1 (%s) instalado. Abrir con: %s/.local/bin/dywali\n' "$1" "$2" ;;
    en:preserved) printf '%s\n' 'Existing collections, images, configuration, and cache were preserved.' ;;
    es:preserved) printf '%s\n' 'Colecciones, imágenes, configuración y caché existentes se han conservado.' ;;
  esac
}
SRC=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
DATA="${XDG_DATA_HOME:-$HOME/.local/share}"
DEST="$DATA/dywali"
UNITDIR="$HOME/.config/systemd/user"

case "$DATA" in /*) ;; *) msg relative_data; exit 1 ;; esac

[ "$(id -u)" -ne 0 ] || { msg no_sudo; exit 1; }
[ -f "$SRC/distribution.py" ] || { msg missing_distribution; exit 1; }

# Read a literal build-time profile from the file; do not execute source code
# to decide which files are safe to require or copy.
PROFILE=$(DYWALI_SCRIPT_LANG="$LANG_ID" /usr/bin/python3 - "$SRC/distribution.py" <<'PY'
import ast
import sys
tree = ast.parse(open(sys.argv[1], encoding='utf-8').read())
values = [node.value for node in tree.body
          if isinstance(node, ast.Assign)
          and any(isinstance(target, ast.Name) and target.id == 'PROFILE'
                  for target in node.targets)]
if len(values) != 1 or not isinstance(values[0], ast.Constant):
    import os
    raise SystemExit('PROFILE debe contener una constante literal única.' if os.environ.get('DYWALI_SCRIPT_LANG') == 'es' else 'PROFILE must contain exactly one literal constant.')
profile = values[0].value
if profile not in ('private', 'public-1.0'):
    import os
    raise SystemExit('Perfil Dywali desconocido.' if os.environ.get('DYWALI_SCRIPT_LANG') == 'es' else 'Unknown Dywali profile.')
print(profile)
PY
)

COMMON='studio.py engine.py displays.py scheduler.py default_config.py migration.py i18n.py slideshow.py distribution.py providers.py'
if [ "$PROFILE" = private ]; then
  PROVIDERS='spotlight.py wikimedia.py apod.py bing.py'
else
  PROVIDERS='wikimedia.py apod.py'
fi
for f in $COMMON $PROVIDERS install_payload.py LICENSE icons/dywali.svg \
         assets/Leafy-Day.jpg assets/Leafy-Night.jpg \
         locale/es/LC_MESSAGES/dywali.po locale/es/LC_MESSAGES/dywali.mo; do
  [ -f "$SRC/$f" ] && [ ! -L "$SRC/$f" ] || { msg missing_file "$f"; exit 1; }
done
if ! /usr/bin/python3 -c 'import gi;gi.require_version("Gtk","3.0");from gi.repository import Gtk,Gdk,GdkPixbuf,GLib,Pango;from PIL import Image' >/dev/null 2>&1; then
  msg dependencies; exit 1
fi

# Compile/import a disposable, profile-exact payload before migration,
# service changes, or writes to installed application paths.
CHECK_DIR=$(mktemp -d "${TMPDIR:-/tmp}/dywali-preflight.XXXXXX")
RESTORE_TIMERS=0
OLD_ENABLED=0
OLD_ACTIVE=0
CURRENT_ENABLED=0
CURRENT_ACTIVE=0
SYSTEMD_USER=0
restore_timer() {
  unit=$1; enabled=$2; active=$3
  if [ "$enabled" -eq 1 ]; then systemctl --user enable "$unit" >/dev/null 2>&1 || true; fi
  if [ "$active" -eq 1 ]; then systemctl --user start "$unit" >/dev/null 2>&1 || true; fi
}
cleanup() {
  result=$?
  if [ "$RESTORE_TIMERS" -eq 1 ] && [ "$SYSTEMD_USER" -eq 1 ]; then
    restore_timer dynamic-wallpaper-studio.timer "$OLD_ENABLED" "$OLD_ACTIVE"
    restore_timer dywali.timer "$CURRENT_ENABLED" "$CURRENT_ACTIVE"
  fi
  rm -rf -- "$CHECK_DIR"
  exit "$result"
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

for f in $COMMON $PROVIDERS LICENSE install_payload.py; do
  mkdir -p "$CHECK_DIR/$(dirname -- "$f")"
  cp "$SRC/$f" "$CHECK_DIR/$f"
done
for directory in assets icons locale; do
  mkdir -p "$CHECK_DIR/$directory"
done
cp "$SRC/icons/dywali.svg" "$CHECK_DIR/icons/dywali.svg"
cp "$SRC/assets/Leafy-Day.jpg" "$SRC/assets/Leafy-Night.jpg" "$CHECK_DIR/assets/"
mkdir -p "$CHECK_DIR/locale/es/LC_MESSAGES"
cp "$SRC/locale/es/LC_MESSAGES/dywali.po" "$SRC/locale/es/LC_MESSAGES/dywali.mo" \
   "$CHECK_DIR/locale/es/LC_MESSAGES/"

for f in $COMMON $PROVIDERS install_payload.py; do
  PYTHONPYCACHEPREFIX="$CHECK_DIR/pycache" /usr/bin/python3 -m py_compile "$CHECK_DIR/$f"
done
(
  cd "$CHECK_DIR"
  PYTHONPYCACHEPREFIX="$CHECK_DIR/pycache" /usr/bin/python3 -c \
    'import sys;sys.path.insert(0,sys.argv[1]);import distribution,providers,engine,studio;assert distribution.PROFILE == sys.argv[2]' \
    "$CHECK_DIR" "$PROFILE"
)
# Validate all installed target collisions before stopping timers or migrating.
/usr/bin/python3 "$CHECK_DIR/install_payload.py" --check \
  "$CHECK_DIR" "$DEST" "$HOME" "$DATA" "$PROFILE"

# Do not overwrite unit files that are not recognizably Dywali-owned.
for directory in "$HOME/.config" "$HOME/.config/systemd" "$UNITDIR"; do
  [ ! -L "$directory" ] || { msg systemd_dir_link "$directory"; exit 1; }
done
for unit in dywali.service dywali.timer; do
  if [ -e "$UNITDIR/$unit" ]; then
    [ ! -L "$UNITDIR/$unit" ] || { msg unit_link "$UNITDIR/$unit"; exit 1; }
    case "$unit" in
      dywali.service) marker='Description=Actualizar fondo de Dywali' ;;
      dywali.timer) marker='Description=Actualización periódica Dywali' ;;
    esac
    grep -Fq "$marker" "$UNITDIR/$unit" || {
      msg foreign_unit "$UNITDIR/$unit"; exit 1;
    }
  fi
done
for unit in dynamic-wallpaper-studio.timer dynamic-wallpaper-studio.service; do
  if [ -e "$UNITDIR/$unit" ] && { [ -L "$UNITDIR/$unit" ] \
       || ! grep -Fq 'dynamic-wallpaper-studio' "$UNITDIR/$unit"; }; then
    msg legacy_unit "$UNITDIR/$unit"
    exit 1
  fi
done

if command -v systemctl >/dev/null 2>&1 && systemctl --user show-environment >/dev/null 2>&1; then
  SYSTEMD_USER=1
  systemctl --user is-enabled --quiet dynamic-wallpaper-studio.timer 2>/dev/null && OLD_ENABLED=1 || :
  systemctl --user is-active --quiet dynamic-wallpaper-studio.timer 2>/dev/null && OLD_ACTIVE=1 || :
  systemctl --user is-enabled --quiet dywali.timer 2>/dev/null && CURRENT_ENABLED=1 || :
  systemctl --user is-active --quiet dywali.timer 2>/dev/null && CURRENT_ACTIVE=1 || :
  if [ "$OLD_ENABLED" -eq 1 ] || [ "$OLD_ACTIVE" -eq 1 ] \
     || [ "$CURRENT_ENABLED" -eq 1 ] || [ "$CURRENT_ACTIVE" -eq 1 ]; then
    RESTORE_TIMERS=1
    if [ "$OLD_ENABLED" -eq 1 ] || [ "$OLD_ACTIVE" -eq 1 ]; then
      systemctl --user disable --now dynamic-wallpaper-studio.timer >/dev/null
    fi
    if [ "$CURRENT_ENABLED" -eq 1 ] || [ "$CURRENT_ACTIVE" -eq 1 ]; then
      systemctl --user disable --now dywali.timer >/dev/null
    fi
  fi
fi

# Migration preserves the old cache. Keep its undo receipt until the app-file
# transaction commits so a deployment failure restores both data paths and code.
/usr/bin/python3 - "$CHECK_DIR" "$DEST" "$HOME" "$DATA" "$PROFILE" <<'PY'
import sys
source, destination, home, data, profile = sys.argv[1:]
sys.path.insert(0, source)
import install_payload, migration
receipt = migration.migrate()
try:
    install_payload.deploy(source, destination, home, data, profile)
except BaseException:
    migration.rollback(receipt)
    raise
PY
/usr/bin/python3 "$DEST/default_config.py"

# Preserve a previously configured schedule only when the active collection is
# supported by this edition. A public install keeps a Bing/Spotlight collection
# intact but leaves its timer disabled rather than failing every interval.
ACTIVE_MODE=$(/usr/bin/python3 - "$HOME/.config/dywali/config.json" <<'PY'
import json, sys
try:
    config = json.load(open(sys.argv[1], encoding='utf-8'))
    print(config.get('collections', {}).get(config.get('active'), {}).get('type', 'dynamic'))
except (OSError, ValueError, TypeError):
    print('unknown')
PY
)
TIMER_SAFE=1
case "$ACTIVE_MODE" in
  dynamic|slideshow|apod|wikimedia) ;;
  spotlight|bing)
    if [ "$PROFILE" = private ]; then :; else TIMER_SAFE=0; fi
    ;;
  *) TIMER_SAFE=0 ;;
esac
if [ "$TIMER_SAFE" -eq 0 ] && { [ "$OLD_ENABLED" -eq 1 ] || [ "$OLD_ACTIVE" -eq 1 ] \
     || [ "$CURRENT_ENABLED" -eq 1 ] || [ "$CURRENT_ACTIVE" -eq 1 ]; }; then
  if [ "$PROFILE" = public-1.0 ] && { [ "$ACTIVE_MODE" = spotlight ] || [ "$ACTIVE_MODE" = bing ]; }; then
    msg preserve_unsupported "$ACTIVE_MODE"
  else
    msg preserve_unknown "$ACTIVE_MODE"
  fi
fi
if [ "$SYSTEMD_USER" -eq 1 ] && [ "$TIMER_SAFE" -eq 1 ] \
   && { [ "$OLD_ENABLED" -eq 1 ] || [ "$OLD_ACTIVE" -eq 1 ] \
        || [ "$CURRENT_ENABLED" -eq 1 ] || [ "$CURRENT_ACTIVE" -eq 1 ]; }; then
  INTERVAL=$(/usr/bin/python3 - "$HOME/.config/dywali/config.json" <<'PY'
import json, sys
try:
    print(int(json.load(open(sys.argv[1], encoding='utf-8')).get('interval_minutes', 5)))
except (OSError, ValueError, TypeError):
    print(5)
PY
)
  /usr/bin/python3 - "$DEST" "$INTERVAL" <<'PY'
import sys
sys.path.insert(0, sys.argv[1])
import scheduler
scheduler.install(int(sys.argv[2]))
PY
  if [ "$CURRENT_ENABLED" -eq 0 ] && [ "$OLD_ENABLED" -eq 0 ]; then
    systemctl --user disable dywali.timer
  fi
  if [ "$CURRENT_ACTIVE" -eq 0 ] && [ "$OLD_ACTIVE" -eq 0 ]; then
    systemctl --user stop dywali.timer
  fi
fi
RESTORE_TIMERS=0
# Remove only recognizable legacy launchers/units after the new payload and
# timer policy are sound; leave unrelated files untouched.
for path in "$HOME/.local/bin/dynamic-wallpaper-studio" "$HOME/.local/bin/dynamic-wallpaper-apply"; do
  if [ -e "$path" ]; then
    [ ! -L "$path" ] && grep -Fq '.local/share/dynamic-wallpaper-studio/' "$path" \
      && rm -f -- "$path"
  fi
done
if [ -f "$DATA/applications/dynamic-wallpaper-studio.desktop" ] \
   && grep -Fq 'Name=Dynamic Wallpaper Studio' "$DATA/applications/dynamic-wallpaper-studio.desktop"; then
  rm -f -- "$DATA/applications/dynamic-wallpaper-studio.desktop"
fi
if [ -f "$DATA/icons/hicolor/scalable/apps/dynamic-wallpaper-studio.svg" ] \
   && grep -Fq 'Dynamic Wallpaper Studio' "$DATA/icons/hicolor/scalable/apps/dynamic-wallpaper-studio.svg"; then
  rm -f -- "$DATA/icons/hicolor/scalable/apps/dynamic-wallpaper-studio.svg"
fi
if [ -e "$UNITDIR/dynamic-wallpaper-studio.timer" ] \
   && [ ! -L "$UNITDIR/dynamic-wallpaper-studio.timer" ] \
   && grep -Fq 'dynamic-wallpaper-studio' "$UNITDIR/dynamic-wallpaper-studio.timer"; then
  if [ -e "$UNITDIR/dynamic-wallpaper-studio.service" ] \
     && { [ -L "$UNITDIR/dynamic-wallpaper-studio.service" ] \
       || ! grep -Fq 'dynamic-wallpaper-studio' "$UNITDIR/dynamic-wallpaper-studio.service"; }; then
     msg legacy_service "$UNITDIR/dynamic-wallpaper-studio.service"
  else
    rm -f -- "$UNITDIR/dynamic-wallpaper-studio.timer" "$UNITDIR/dynamic-wallpaper-studio.service"
  fi
fi
if [ "$SYSTEMD_USER" -eq 1 ]; then systemctl --user daemon-reload || true; fi
if command -v gtk-update-icon-cache >/dev/null 2>&1; then
  gtk-update-icon-cache -f "$DATA/icons/hicolor" >/dev/null 2>&1 || true
fi
msg installed "$PROFILE" "$HOME"
msg preserved
