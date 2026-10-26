#!/usr/bin/env bash
# sharist installieren (ins eigene Benutzerverzeichnis, root nur für Systempakete und die Firewall-Regel).
#   ./install.sh            normal (fragt nach Systempaketen, Firewall und Autostart)
#   ./install.sh --update   ohne Rückfragen (von update.sh)
set -euo pipefail
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$SRC/pkg.sh"
DIR="$HOME/.local/share/sharist"
RICE="$HOME/.local/share/rice-apps"
BIN="$HOME/.local/bin"
APPS="$HOME/.local/share/applications"
ICONS="$HOME/.local/share/icons/hicolor/scalable/apps"
PORT=37521
UPDATE=0; [[ "${1:-}" == "--update" ]] && UPDATE=1

say "== sharist installieren =="
basics_missing() {
    python3 -c 'import gi, venv, ensurepip; gi.require_version("Gtk", "4.0")' 2>/dev/null && return 1
    return 0
}
if { (( UPDATE )) && basics_missing && say "Es fehlen Systempakete – werden nachinstalliert …"; } \
   || ask "Systempakete installieren (GTK4, PyGObject, Bluetooth, Benachrichtigungen)?"; then
    pkg_install \
        "gtk4 python python-gobject python-cairo python-cryptography adwaita-icon-theme xdg-utils libnotify bluez bluez-utils" \
        "python3 python3-venv python3-gi python3-gi-cairo gir1.2-gtk-4.0 python3-cryptography adwaita-icon-theme xdg-utils libglib2.0-bin libnotify-bin bluez" \
        "python3 python3-gobject python3-cairo gtk4 python3-cryptography adwaita-icon-theme xdg-utils glib2 libnotify bluez" \
        "python3 python3-gobject python3-gobject-cairo python3-gobject-Gdk typelib-1_0-Gtk-4_0 python3-cryptography adwaita-icon-theme xdg-utils glib2-tools libnotify-tools bluez" \
        || warn "Paketinstallation nicht vollständig – weiter."
fi
echo

say "Kopiere sharist nach $DIR …"
mkdir -p "$DIR/pb" "$DIR/proto" "$RICE/fonts" "$BIN" "$APPS" "$ICONS"
cp -f "$SRC"/app/sharist/*.py "$SRC"/app/sharist/VERSION "$SRC"/app/sharist/CHANGELOG.md \
      "$SRC"/app/sharist/requirements.txt "$DIR/"
cp -f "$SRC"/app/sharist/pb/*.py "$DIR/pb/"
cp -f "$SRC"/app/sharist/proto/*.proto "$DIR/proto/"
install -m 755 "$SRC/sharist.sh" "$DIR/sharist.sh"
cp -f "$SRC/app/tui.py" "$SRC/app/glass.py" "$SRC/app/istheme.py" "$SRC/app/changelog.py" "$SRC/app/swipe.py" "$RICE/"   # gemeinsame Bausteine
cp -f "$SRC"/app/fonts/* "$RICE/fonts/"                           # Symbolschrift (Icons ohne Nerd Font)
ln -sf "$DIR/sharist.sh" "$BIN/sharist"
sed "s|@EXEC@|$BIN/sharist|g" "$SRC/sharist.desktop.in" > "$APPS/sharist.desktop"
cp -f "$SRC/sharist.svg" "$ICONS/sharist.svg"
refresh_desktop

# eigene Python-Umgebung für zeroconf/protobuf/qrcode (GTK kommt vom System)
if [ ! -x "$DIR/venv/bin/python" ] || ! "$DIR/venv/bin/python" -c 'import pip' 2>/dev/null; then
    say "Richte Python-Umgebung ein …"
    rm -rf "$DIR/venv"
    python3 -m venv --system-site-packages "$DIR/venv"
fi
"$DIR/venv/bin/python" -m pip install -q --disable-pip-version-check -r "$DIR/requirements.txt" \
    || warn "pip-Installation fehlgeschlagen – ohne Internet? Später: ./update.sh"

if (( ! UPDATE )); then
    # Handys verbinden sich zum Laptop → eingehender TCP-Port muss offen sein
    if command -v ufw >/dev/null && systemctl is-active -q ufw 2>/dev/null \
       && ask "Firewall (ufw): Port $PORT/tcp für Quick Share öffnen? (braucht sudo)"; then
        sudo ufw allow "$PORT/tcp" comment sharist && echo "  ufw: $PORT/tcp offen"
    fi
    if command -v firewall-cmd >/dev/null && systemctl is-active -q firewalld 2>/dev/null \
       && ask "Firewall (firewalld): Port $PORT/tcp für Quick Share öffnen? (braucht sudo)"; then
        sudo firewall-cmd -q --permanent --add-port="$PORT/tcp" && sudo firewall-cmd -q --reload && echo "  firewalld: $PORT/tcp offen"
    fi
    echo
    if ask "sharist beim Anmelden unsichtbar starten? (sonst sehen Handys den Laptop nur bei offenem Fenster)"; then
        mkdir -p "$HOME/.config/autostart"
        sed -e "s|@EXEC@ %F|$BIN/sharist --daemon|" -e "s|^Name=sharist|Name=sharist (Hintergrund)|" \
            "$SRC/sharist.desktop.in" > "$HOME/.config/autostart/sharist.desktop"
        echo "  Autostart: ~/.config/autostart/sharist.desktop"
        echo "  (Hyprland/Sway lesen den Ordner nicht – dort selbst eintragen:  exec-once = sharist --daemon)"
    fi
fi
# läuft sharist schon im Hintergrund? → neu starten, damit der neue Code gilt
if gdbus call --session --dest org.freedesktop.DBus --object-path /org/freedesktop/DBus \
     --method org.freedesktop.DBus.NameHasOwner de.invincible.Sharist 2>/dev/null | grep -q true; then
    "$BIN/sharist" --quit; sleep 1.5; "$BIN/sharist" --daemon
fi

echo
say "Prüfe …"
check_gtk || warn "Ohne passendes GTK startet sharist nicht – siehe README."
if "$DIR/venv/bin/python" -c 'import zeroconf, google.protobuf, qrcode, cryptography' 2>/dev/null; then
    echo "  Python-Module: ok"
else
    warn "  Python-Module fehlen (zeroconf/protobuf/qrcode/cryptography) – ./update.sh erneut versuchen"
fi
command -v bluetoothctl >/dev/null && echo "  Bluetooth: vorhanden (weckt Android-Handys auf)" \
    || echo "  Bluetooth: fehlt – Senden klappt dann nur per QR-Code"
echo
path_hint
say "Fertig!  Starten mit:  sharist   (oder über das Anwendungsmenü)"
echo "Tipp: Tastenkürzel auf  sharist --toggle  legen.   Aktualisieren: ./update.sh   Entfernen: ./uninstall.sh"
