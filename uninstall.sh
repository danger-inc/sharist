#!/usr/bin/env bash
# sharist entfernen. Empfangene Dateien bleiben, wo sie sind (Standard ~/Downloads).
set -euo pipefail
RICE="$HOME/.local/share/rice-apps"
"$HOME/.local/bin/sharist" --quit 2>/dev/null || true
pkill -u "$(id -u)" -f "share/sharist/sharist.py" 2>/dev/null || true
rm -rf "$HOME/.local/share/sharist"
rm -f "$HOME/.local/bin/sharist" "$HOME/.local/share/applications/sharist.desktop" \
      "$HOME/.local/share/icons/hicolor/scalable/apps/sharist.svg" "$HOME/.config/autostart/sharist.desktop"
# gemeinsame Bausteine nur entfernen, wenn keine andere Suite-App sie noch braucht
if [ ! -d "$RICE/calendar" ] && [ ! -d "$RICE/music" ] && [ ! -d "$HOME/.local/share/mailist" ] \
   && [ ! -d "$HOME/.local/share/wallist" ] && [ ! -d "$HOME/.local/share/linguist" ]; then
    rm -rf "$RICE/fonts" "$RICE/tui.py" "$RICE/glass.py" "$RICE/changelog.py" "$RICE/swipe.py" "$RICE/__pycache__"
    rmdir "$RICE" 2>/dev/null || true
fi
echo "sharist entfernt (Einstellungen: ~/.config/sharist)."
echo "Firewall-Regel ggf. selbst schließen:  sudo ufw delete allow 37521/tcp"
