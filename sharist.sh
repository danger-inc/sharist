#!/usr/bin/env bash
# Starter für sharist (Quick Share). Läuft es schon im Hintergrund, geht der Aufruf direkt per D-Bus hin.
#   sharist  ·  sharist --toggle (Super+D)  ·  sharist datei…  (zum Senden)  ·  sharist --daemon  ·  sharist --quit
APP=de.invincible.Sharist
OBJ=/de/invincible/Sharist
D="$HOME/.local/share/sharist"
PY="$D/venv/bin/python"; [ -x "$PY" ] || PY=python3
LOG="${XDG_RUNTIME_DIR:-/tmp}/sharist.log"

running() { gdbus call --session --dest org.freedesktop.DBus --object-path /org/freedesktop/DBus \
    --method org.freedesktop.DBus.NameHasOwner "$APP" 2>/dev/null | grep -q true; }
action() { gdbus call --session --timeout 3 --dest "$APP" --object-path "$OBJ" \
    --method org.gtk.Actions.Activate "$1" "${2:-[]}" '{}' >/dev/null 2>&1; }
start() { setsid -f "$PY" "$D/sharist.py" "$@" >>"$LOG" 2>&1 </dev/null; }

case "${1:-}" in
    --daemon) running || start --daemon; exit 0 ;;
    --quit)   running && action quit; exit 0 ;;
    --toggle) running && action toggle && exit 0; start; exit 0 ;;
    "")       running && action show && exit 0; start; exit 0 ;;
    *)        args=(); for a in "$@"; do args+=("$(realpath -- "$a" 2>/dev/null || echo "$a")"); done
              start "${args[@]}"; exit 0 ;;
esac
