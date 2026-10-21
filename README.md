# sharist

Quick Share (früher Nearby Share) für Linux im Terminal-Look der Suite (GTK4): Android-Handys schicken dem Laptop
Dateien, Fotos, Text, Links und WLAN-Zugänge – und der Laptop schickt zurück. Alles direkt über das WLAN,
verschlüsselt, ohne Google-Konto und ohne Cloud.

## Installieren – Linux

```bash
gh auth login                                   # einmalig (Repo ist privat, Einladung nötig)
gh repo clone invincible-nix/sharist ~/sharist-src
cd ~/sharist-src && ./install.sh
```

Der Installer fragt nach den Systempaketen (GTK4, PyGObject, Bluetooth – einmal Passwort), kopiert sharist nach
`~/.local/share/sharist`, richtet dort eine kleine Python-Umgebung ein (zeroconf, protobuf, qrcode) und legt den
Befehl `sharist` und einen Menüeintrag an. Läuft eine Firewall (ufw/firewalld), bietet er an, **Port 37521/tcp**
zu öffnen – ohne den kommen Handys nicht durch. Zum Schluss: Autostart im Hintergrund, damit Handys den Laptop
auch bei geschlossenem Fenster sehen.

## Bedienung

- **¹ senden** – Dateien ins Fenster ziehen, Strg+O, Strg+V oder `sharist datei…`; Text/Link ins Feld tippen.
  Handys in der Nähe tauchen von selbst auf, ein Klick schickt los. Am Handy erscheint die Anfrage mit PIN.
  Findet sharist das Handy nicht: **qr-code zeigen** und mit der Handy-Kamera scannen – dann verbindet es sich
  selbst und das Handy fragt nicht mehr nach.
- **² empfangen** – am Handy „Teilen → Quick Share → *dein Gerätename*“. sharist zeigt Gerät, Inhalt und PIN,
  annehmen/ablehnen im Fenster oder direkt in der Benachrichtigung. Dateien landen in `~/Downloads`
  (änderbar), Text gleich in der Zwischenablage, Links lassen sich aus der Benachrichtigung öffnen.
- Schließen versteckt nur das Fenster; `sharist --toggle` eignet sich für ein Tastenkürzel,
  `sharist --daemon` für den Autostart, `sharist --quit` beendet.

## Damit das Handy den Laptop findet

- gleiches WLAN, am Handy Bluetooth an
- Quick Share am Handy: Sichtbarkeit **„Alle“** (oder „Alle für 10 Minuten“)
- Gastnetze/Uni-WLANs (eduroam) blocken Geräte untereinander oft – dann Handy-Hotspot nehmen

## Läuft auf

| System | Status |
|---|---|
| Arch (Hyprland) | getestet |
| Ubuntu 24.04+, Fedora 40+, openSUSE | sollte gehen |

Senden ans Handy braucht Bluetooth (BlueZ), damit Android seine Quick-Share-Werbung startet; ohne Bluetooth
bleibt der QR-Code. Das Protokoll folgt der Beschreibung von [NearDrop](https://github.com/grishka/NearDrop).

## Aktualisieren / Entfernen

```bash
./update.sh      # holt den neuen Stand und installiert ihn
./uninstall.sh   # entfernt sharist (empfangene Dateien und ~/.config/sharist bleiben)
```
