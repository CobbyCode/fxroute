# FXRoute Ubuntu 26.04 — Phase-2-Hardware-Testplan

Ziel: den in QEMU voll abgenommenen Port (Commit-Stand `013d8e7`, Produkt-ISO
`dist/fxroute-ubuntu-26.04-x86_64.iso`) auf echter Hardware zu verifizieren.
Die vier Blöcke bauen aufeinander auf; Abbruch bei einem Block bedeutet Stopp und
Log-Mitnahme. Alle softwareseitigen Checks übernimmt der Agent per SSH nach
Absprache; hier stehen die manuellen Schritte + Erwartungen.

Vorbereitung einmalig:

* Ziel-PC: UEFI-Modus (kein CSM), Secure Boot im Setup **aktiviert** lassen.
* USB-Stick mit dem Produkt-ISO beschrieben (z. B.
  `sudo dd if=fxroute-ubuntu-26.04-x86_64.iso of=/dev/sdX bs=4M oflag=direct status=progress`; mindestens so groß wie das ISO).
* Ethernet optional verbunden (nicht erforderlich — WLAN wird im Installer
  konfiguriert), Scarlett per USB angeschlossen, Lautsprecher/Monitor am
  Hauptausgang.
* Nach dem ersten Boot berichtet der Test den Gerätenamen `fxroute-XXXXXX`
  (maschine-id-Suffix); SSH-Passwort = Installer-Passwort des Benutzers.

## Block A — Secure Boot (nur signierte Ubuntu-Kette)

1. ISO/Stick im UEFI-Boot-Menü wählen; es muss der Shim lädt (kein MOK-
   Enroll-Screen, keine "Security Violation").
2. GRUB → `Try or Install Ubuntu` startet normal (Secure Boot an).
3. GRUB → `Install FXRoute` → Installation durchlaufen lassen.
4. Installiertes System bootet mit Secure Boot an (kein MOK, kein
   "Verification failed").

Checks (Host nach Erstboot, softwareseitig):

```bash
mokutil --sb-state                 # erwartet: SecureBoot enabled
sudo efibootmgr -v                 # ubuntu + fxroute-Einträge vorhanden
bootctl status 2>/dev/null | head  # Secure Boot: yes (falls systemd-efi)
```

Bestanden wenn: alle drei Schritte ohne Secure-Boot-Fehler; `mokutil`
meldet "SecureBoot enabled"; kein eigener Kernel/MOK nötig war.

## Block B — WLAN: einmal im Installer, danach automatisch

1. Im Installer (`Install FXRoute`, Subiquity-Netzwerkseite) das Ziel-WLAN
   auswählen, Passwort eingeben, **weiter** (kein anderer Netzwerkschritt).
2. Installation abschließen, Stick entfernen, Neustart lassen.

Software-Checks nach Erstboot (softwareseitig, kein Zweitsetup nötig):

```bash
nmcli -f GENERAL,IP4 dev show wlan0   # state: connected (activated)
nmcli -g GENERAL.STATE dev wlan0      # "connected"
nmcli -g 802-11-wireless.ssid connection show "$(nmcli -t -f NAME,TYPE connection show --active | awk -F: '$2==\"802-11-wireless\"{print \$1}')"
ip -4 addr show wlan0 | grep inet
```

Bestanden wenn: nach dem Erstboot ohne jede Nutzeraktion eine
aktive WLAN-Verbindung mit IPv4-Adresse besteht (Netplan/Curtin hat die im
Installer gesetzte Verbindung übernommen).

## Block C — First-Boot / Autologin / Kiosk

1. Nach dem Installer-Reboot: kein Login-Screen; GDM meldet den
   Installer-Benutzer automatisch an (GNOME-Desktop erscheint).
2. Kurz danach startet Firefox **vollbild (Kiosk)** mit FXRoute.

Software-Checks (softwareseitig):

```bash
test -f /var/lib/fxroute-iso/install-complete && echo marker-ok
systemctl --user is-active fxroute.service
loginctl list-sessions --no-legend        # seat0-Session des Benutzers
pgrep -af "firefox --kiosk"
curl -fsS http://127.0.0.1:8000/api/status | head -c 200
```

Zusätzlich manuell: Desktop-Link „FXRoute" existiert, Firefox zeigt die
FXRoute-Oberfläche; nach 5 Minuten Leerlauf **kein** Sperren/Dimmen.

## Block D — Scarlett (18→14→10→18) inkl. Playback/VU/AUX-Links

Vorbereitung: Scarlett (18i20 o. ä.) via USB verbunden; `pactl list cards
short` bzw. `pactl list cards | grep -A2 scarlett` zeigt das Gerät mit
Profilen. FXRoute wurde mit `--providers none` installiert → DSP-Kette über
PipeWire.

Kurzscript (softwareseitig, ein Befehl pro Schritt, alle Ausgaben fürs Log):

```bash
pactl list cards short                        # Scarlett-Karte sichtbar
pactl set-card-profile <scarlett-card> output:analogue-output-surround-71 ...
```

Szenario (jeder Schritt manuell am Gerät + softwareseitiger Log-Zweig):

1. **18 Ein/Ausgänge aktiv** (Profil analog, alle Kanäle sichtbar).
2. **→ 14 Kanäle:** Kanalzahl im Scarlett-Hardware-Panel oder FXRoute-UI
   reduzieren; `pactl list sinks` zeigt weniger Kanäle; laufendes Playback
   überlebt den Wechsel oder startet sauber neu.
3. **→ 10 Kanäle:** dito; prüfen, dass AUX/Routing-Links in der FXRoute-UI
   nicht hängen bleiben (AUX-Link-Status pro Kanal prüfen).
4. **→ 18 zurück:** alle Kanäle wieder da; Playback/VU zeigt wieder Werte.

Während 2–4 fortlaufend prüfen:

* **Playback:** Testton/Stream läuft ohne Dropouts; Samplingrate laut
  `pactl list sinks | grep -E "Sample|Channel"` wie erwartet.
* **VU/Peak:** FXRoute `/api/status` → `output_peak_warning.detected`
  bei lautem Signal `true`; `vu_db_l/r` bewegen sich.
* **AUX-Links:** AUX-Ausgänge folgen dem Main-Routing (Links in der UI
  aktiv, keine verwaisten Links nach Kanalzahlwechsel).
* **USB-Log:** `sudo dmesg -w | grep -iE "usb|scarlett"` zeigt sauberes
  Enumerate/Re-Enumerate ohne `cannot set freq`-Fehler;
  `journalctl -b -u fxroute` ohne DSP-Fehler.

Bestanden wenn: alle Kanalzahlwechsel ohne Neustart überlebt werden,
Playback durchgehend hörbar/lauffähig, VU-Werte live, AUX-Links intakt,
USB-Log ohne Fehler.

## Abbruchkriterien / Nacharbeit

* A schlägt fehl → Secure Boot mit Shim-Fehler: Boot-Log sichern, Issue.
* B schlägt fehl: `networkctl status wlan0`, `/etc/netplan/*.yaml` sichern.
* C schlägt fehl: `journalctl -u fxroute-first-boot -b` + Desktop-Datei
  `/home/<user>/Desktop/FXRoute-NOT-STARTED.txt` prüfen.
* D schlägt fehl: `dmesg`-Auszug + `pactl list cards` sichern; Scarlett-
  Profil-Wechsel in Phase 2 ggf. über `alsa-ucm-conf` nachziehen.
