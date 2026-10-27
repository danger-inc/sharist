# sharist – Änderungen

## 1.3.0 (2026-10-27)
- **Wechsel-Animation**: Der Wechsel zwischen Hell und Dunkel (und zwischen Designs) läuft als Scanbalken über den ganzen Bildschirm. Alle Fenster, die Leiste und das Terminal wechseln darunter im selben Moment, hinter dem Balken steht das neue Design. An/aus und Dauer (Standard 1,5 s) stehen in der Designauswahl der Einstellungen.

## 1.2.0 (2026-10-26)
- **Design wählbar**: In den Einstellungen gibt es die Designauswahl wie in notist (Automatisch + 9 Designs). Die Wahl gilt für alle -ist-Apps zugleich und wirkt sofort. Bei „Automatisch“ (Desktop-Farben) kannst du zwischen **Dunkel** und **Hell** umschalten.

## 1.1.1 (2026-10-25)
- **Update-Knopf**: Neben der Versionsnummer steht „󰑐“ – Klick sucht sofort nach einem Update. Gibt es eine neuere
  Version, wird daraus „󰚰 update“; ein Klick lädt und installiert sie (Linux und Windows), danach heißt er „󰑓 neu starten“.
- **Rückmeldung bei Umschalt-Knöpfen**: „An“ (z. B. „Ton behalten“) ist jetzt auch unter dem Mauszeiger zu sehen,
  Drücken färbt sofort – man muss die Maus nicht mehr wegbewegen, um zu sehen, ob der Klick angekommen ist.
- **Windows: echte Titelleiste** – das Fenster lässt sich verschieben, in der Größe ändern, minimieren und
  maximieren.

## 1.1.0 (2026-10-21)
- **Neuere Pixels finden den Laptop**: Sie suchen beim Teilen nur noch per Bluetooth. sharist kündigt sich jetzt
  zusätzlich per Bluetooth an und nimmt dort Verbindungen an; die Datei selbst wechselt danach aufs WLAN
  (ohne WLAN läuft sie langsam über Bluetooth weiter).
- **qr-code repariert**: Der Schlüssel im Code war bei etwa jedem zweiten Code falsch. Wird das Handy erkannt, bevor
  etwas gewählt ist, merkt sich sharist es und schickt los, sobald eine Datei gewählt wird.

## 1.0.0 (2026-10-21)
- **Quick Share für den Laptop**: Android-Handys sehen den Laptop unter „Teilen → Quick Share“ und schicken Dateien,
  Fotos, Text, Links und WLAN-Zugänge. Eine Anfrage zeigt Gerät, Inhalt und **PIN** – annehmen oder ablehnen im
  Fenster oder direkt in der Benachrichtigung.
- **Senden ans Handy**: Dateien ablegen, `strg+o`, `strg+v` oder `sharist datei…`; Text/Link ins Feld. Geräte in der
  Nähe erscheinen von selbst (sharist weckt Android per Bluetooth auf). Findet das Handy nichts: **qr-code** zeigen
  und mit der Kamera scannen.
- Läuft unsichtbar im Hintergrund, **Super+D** öffnet das Fenster. Empfangener Text landet gleich in der
  Zwischenablage, Links lassen sich aus der Benachrichtigung öffnen.
