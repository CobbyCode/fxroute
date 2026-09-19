# FXRoute Ubuntu 26.04 x86 Port (Phase 1: QEMU-Prototyp)

Paralleler Port des FXRoute-x86-Installers auf offizielles Ubuntu 26.04 LTS.
Der Leap-/Agama-Pfad (`iso/`) bleibt unverändert; `.104` wird nicht angefasst;
kein Push/Release aus diesem Worktree ohne explizite Anforderung.

## Gewählter Build-/Installer-Pfad (Ubuntu-Standardweg)

* **Basis:** offizielles `ubuntu-26.04.1-desktop-amd64.iso` (aktuellster
  26.04-Point-Release, SHA256-geprüft gegen `releases.ubuntu.com`).
* **ISO-Customization:** `livefs-editor` (`livefs-edit`, Canonical-Upstream,
  per `pipx` aus dem Git-Repo installiert) — das von Ubuntu dokumentierte
  Werkzeug für diesen Zweck. Kein eigener ISO-Bau, kein mkmedia, kein
  Fremd-Tool wie Cubic. Begründung: offiziell vorgesehen, arbeitet direkt auf
  dem signierten Original-ISO (Secure-Boot-Kette aus Shim/signed Kernel bleibt
  unangetastet), Aktionen (`--python`, `--cp`, eigene No-Recommends-
  Paketinstallation) decken genau unseren Bedarf ab.
* **Installer:** Subiquity/Autoinstall mit `subiquity.autoinstallpath`
  (absolut: `/cdrom/fxroute-seed/<profil>.yaml` — Subiquity resolving den
  Pfad relativ zu seinem CWD, dem Live-User-Home, daher absolut) auf zwei
  eigenen GRUB-Einträgen `Install FXRoute Desktop` / `Install FXRoute
  Headless`, jeweils mit `autoinstall`. Der Eintrag `Try FXRoute Live`
  (gleiche Live-Session wie Stock-Ubuntu plus FXRoute-Autostart) bleibt
  Default und ohne Seed-Zugang (kein `autoinstall.yaml` im ISO-Root, keine
  cloud-init-Datasource): normales Ubuntu-Live-/Installer-Modell.
* **Interaktiv bleiben:** Keyboard, Netzwerk (WLAN einmalig im Installer)
  und Identity (Benutzer/Hostname/Passwort) via `interactive-sections` in
  **beiden** Profilen. Ubuntu-typische Abfrage, keine eigenen Dialoge.
  Netplan aus dem Installer wird von Curtin standardmäßig aufs Ziel
  übernommen → WLAN nach Erstboot automatisch verbunden (Realtest Phase 2).
* **Erstboot:** `fxroute-first-boot.service` (oneshot, aus Autoinstall
  `late-commands` installiert) entpackt `source.tar`, ruft `install.sh` mit
  denselben Flags wie der Leap-Pfad und richtet den Desktop-Stack (GDM-
  Autologin, Firefox-Kiosk, GNOME-Energiesparren aus) ein.
* **Live (`Try FXRoute Live`):** Casper-Live-Session; FXRoute-Abhängigkeiten
  sind ohne Recommends bereits im Squashfs, ein Autostart läuft `install.sh`
  in der RAM-Session (erst nach Netz und apt-Lock, `apt-get update` mit
  Retry) und öffnet den Firefox-Kiosk. **Der Start bis zur FXRoute-Oberfläche
  kann deutlich länger dauern (DSP-Engine wird in der RAM-Session
  kompiliert); das ist bekannt und kein Fehler, solange das System danach
  normal startet.** Scheitert der Start, liegt ein Hinweis auf dem Desktop
  und es öffnet sich kein toter Kiosk; ein `FXRoute`-Icon auf dem Desktop
  startet den Kiosk neu (nötig z. B. nach Alt+F4). Live- und Install-Root
  sind bewusst nicht identisch.
* **Headless-Ziel (Install FXRoute Headless):** dasselbe
  `ubuntu-desktop-minimal`-Setup, aber der Curtin-Late-Command
  `prepare-headless-target.sh` entfernt Desktop/GDM/Firefox-deb explizit aus
  dem Zielsystem (Purge-Liste ohne autoremove, Fail-closed bei
  Kollateralschaden; Firefox-Snap-Seed-Eintrag offline entprunt) und setzt
  `multi-user.target` vor dem Erstboot. Der Firefox-Snap selbst wird beim
  First-Boot mit laufendem snapd entfernt (nach `snap wait system
  seed.loaded`). Bedienung per Browser im LAN (FXRoute-HTTP) und SSH;
  Linger/User-Manager übernimmt `install.sh`.

## Übernahme vs. Neubau

Unverändert übernommen (kein Ubuntu-Bedarf zur Änderung):

* `install.sh` — erkennt `apt` per Capability-Probe, alle Paketlisten haben
  einen `apt`-Zweig. Verifiziert auf Ubuntu 26.04 (resolute, QEMU-VM und
  echte Hardware): sämtliche `apt`-Paketnamen lösen auf, PipeWire ist 1.6.2
  (≥ 1.4.9, kein Backport nötig), `caddy` 2.6.2 liegt in den Standard-Repos,
  Firefox ist der stock Snap. Provider kommen per Evergreen-Logik (aktuelle
  stabile Upstream-Releases: Spotifyd, TIDAL-`tidalapi` per PyPI, Qobuz per
  offiziellem Upstream mit kompatiblem Fork als Fallback).
* Gesamte FXRoute-Anwendung: DSP, Routing, Provider, Measurement, Library.
* `fxroute.service` (User-Unit), `source.tar`/`build-commit`-Muster,
  Geräte-Namensableitung (`fxroute-<machine-id>`), SSH-Defaults-Idee.

Ubuntu-spezifisch neu (Leap-/Agama-Teile, keine Wiederverwendung möglich):

| Leap (`iso/`) | Ubuntu (`ubuntu/`) |
|---|---|
| `build-leap-16-iso.sh` (mkmedia) | `build-ubuntu-iso.sh` (livefs-edit) |
| `profiles/*.jsonnet` (Agama) | `autoinstall/user-data` (Subiquity) |
| `build-live-from-installed.sh` (dmsquash-LiveFX) | Deps per eigener No-Recommends-Action im Casper-Squashfs |
| `live-boot-options.txt` (`rd.live.*`) | kein Ersatz nötig (Casper-Default) |
| `first-boot-install.sh` (rpm/SDDM/Plasma) | `scripts/first-boot-install-ubuntu.sh` (apt/GDM/GNOME, Firefox-Snap) |
| `fxroute-live-init.sh` (dracut-Umgebung) | `scripts/fxroute-live-autostart.sh` (Casper-Autostart) |
| SDDM/Plasma/KWallet/PowerDevil-Setup | GDM-Autologin + dconf-Overrides (Sperre/Suspend aus) |
| `test-leap-16-iso.sh` (Agama-SSH-API) | `test-ubuntu-iso.sh` (GRUB-Autoinstall + Curtin-Wartepfad) |

## Layout

```
ubuntu/
  README.md                  diese Datei
  build-ubuntu-iso.sh        ISO-Build (lädt/prueft Basis-ISO, livefs-edit)
  test-ubuntu-iso.sh         QEMU-Abnahme (Live → Install → Appliance)
  test_iso_harness.py        Unit-Tests für den Harness (gemockte SSH/QEMU)
  test_desktop_defaults.py   Unit-Test: dconf-Profil/Keyfile aus first-boot
  autoinstall/
    user-data                Subiquity-Autoinstall (en_US/us, Rest Standard)
    user-data.test           Test-Seed-Overrides (QEMU only: autologin test/test)
    first-boot.service       Oneshot-Unit fürs installierte System
    fxroute-live.desktop     Autostart-Eintrag der Live-Session
    fxroute-test-ssh.service QEMU-SSH-Hook als Unit (nur Test-Seed)
  generate-seeds.py          Rendert fxroute-seed/{desktop,headless}.yaml
  livefs-actions/            livefs-edit --python-Aktionen
  scripts/
    first-boot-install-ubuntu.sh
    prepare-headless-target.sh
    fxroute-live-autostart.sh
    fxroute-ubuntu-launcher.sh
    fxroute-test-ssh.sh      QEMU-SSH-Hook (nur Test-Seed)
```

## Phase-1-Abnahme (QEMU, UEFI/OVMF, Secure Boot vorerst aus)

Stand 17.09.2026, Test-ISO `dist/fxroute-ubuntu-26.04-test.iso`:

* **Live-Phase bestanden:** Default-Eintrag `Try FXRoute Live` bootet
  die GNOME-Live-Session; das Autostart-Skript richtet FXRoute ein und der
  Kiosk öffnet. `/api/status` über QEMU-Portforward erreichbar (voller
  Status-JSON, `system.version 1.0-beta8`).
* **Install-Phase bestanden:** GRUB-Eintrag `Install FXRoute` (mit
  `autoinstall` auf der Cmdline, per SSH verifiziert) fährt Subiquity
  vollständig unbeaufsichtigt durch: LVM-Layout, Testbenutzer, late-commands
  (Payload nach `/opt/fxroute-iso`, first-boot-Unit enabled). Der Reboot
  landet direkt im installierten System (Subiquity stellt die
  EFI-Bootreihenfolge um; es erscheint kein zweites GRUB-Menü).
* **Appliance-Phase bestanden:** Die installierte Platte bootet ohne ISO.
  `fxroute-first-boot.service` läuft einmalig erfolgreich und setzt
  `install-complete`. `fxroute.service` (User-Unit) ist aktiv, HTTP
  `/api/status` antwortet auf 127.0.0.1:8000, GDM-Autologin als
  Installationsbenutzer aktiv, Firefox läuft im `--kiosk`-Modus.

Gelernt/behoben während der QEMU-Läufe (Details siehe Git-Historie):

* Stock-Desktop-Cmdline enthält kein `boot=casper` → Live-Guard prüft den
  Casper-Mount (`/cdrom/casper`) statt des Kernel-Flags.
* Casper stapelt Squashfs-Layer mit `.live` oben; die Platzhalter
  `/var/lib/cloud/seed/nocloud/*` in der `.live`-Schicht sind leer und
  überdecken Seed-Dateien in tieferen Schichten. Die Seeds liegen deshalb als
  Dateien auf dem ISO (`/fxroute-seed/<profil>.yaml`) und werden pro
  GRUB-Eintrag per absolutem `subiquity.autoinstallpath=/cdrom/...`
  ausgewählt (Subiquity löst relativ zu seinem CWD auf, dem Live-User-Home;
  kein `autoinstall.yaml` im ISO-Root, keine cloud-init-Datasource, damit der
  Stock-`Try`-Eintrag unbeeinflusst bleibt).
* `md5sum.txt` muss nach dem Squashfs-Rebuild neu geschrieben werden
  (Pre-Repack-Hook `final_checksums.py`); xorriso erzeugt `/boot.catalog`
  erst danach — Katalog daher exkludiert wie im Ubuntu-Original, Payload
  bleibt vollständig abgedeckt.
* Der Autoinstall-Reboot zeigt kein zweites GRUB (EFI-Reihenfolge wird von
  Subiquity umgestellt); der Harness erkennt das installierte System per
  SSH (root-Fs, Cmdline, Marker) statt per GRUB-Banner.
* dconf-System-Overrides brauchen zwingend `/etc/dconf/profile/user` mit
  `system-db:local`; Ubuntu legt es nicht an. uint32-Keys brauchen die
  `idle-delay=uint32 0`-Notation (bare `0` = int32 wird ignoriert). Beides
  unit-getestet (`test_desktop_defaults.py`).

## Offene Hardware-Gates (Phase 2, echte Hardware)

* WLAN-Realtest (Intel 8265 / 8086:24fd / iwlwifi): im Installer
  konfigurieren → nach Erstboot ohne Zweiteinrichtung verbunden.
* Secure Boot auf echter Hardware (nur signierter Ubuntu-Pfad, kein MOK).
* Scarlett-Audiointerface am Zielsystem.
