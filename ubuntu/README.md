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
  unangetastet), Aktionen (`--install-packages`, `--cp`,
  `--add-autoinstall-config`) decken genau unseren Bedarf ab.
* **Installer:** Subiquity/Autoinstall (`autoinstall:`-Sektion per
  `--add-autoinstall-config`) + eigener GRUB-Eintrag `Install FXRoute`, der
  per Kernel-Cmdline `autoinstall` startet. Der normale
  `Try or Install Ubuntu`-Eintrag bleibt Default und unverändert:
  normales Ubuntu-Live-/Installer-Modell, keine eigene
  Persistenz-/Nicht-Persistenz-Logik.
* **Interaktiv bleibt:** Identity (Benutzer) und Netzwerk (WLAN einmalig im
  Installer) via `interactive-sections`. Ubuntu-typische Abfrage, keine
  Leap-Oberflächen-Nachbildung. Netplan aus dem Installer wird von Curtin
  standardmäßig aufs Ziel übernommen → WLAN nach Erstboot automatisch
  verbunden (Realtest Phase 2).
* **Erstboot:** `fxroute-first-boot.service` (oneshot, aus Autoinstall
  `late-commands` installiert) entpackt `source.tar`, ruft `install.sh` mit
  denselben Flags wie der Leap-Pfad und richtet den Desktop-Stack (GDM-
  Autologin, Firefox-Kiosk, GNOME-Energiesparren aus) ein.
* **Live (`Try`):** Casper-Live-Session; FXRoute-Abhängigkeiten sind per
  `--install-packages` bereits im Squashfs, ein Autostart läuft `install.sh`
  in der RAM-Session und öffnet den Firefox-Kiosk. Live- und Install-Root
  sind bewusst nicht identisch.

## Übernahme vs. Neubau

Unverändert übernommen (kein Ubuntu-Bedarf zur Änderung):

* `install.sh` — erkennt `apt` per Capability-Probe, alle Paketlisten haben
  einen `apt`-Zweig. Verifiziert auf Ubuntu 26.04 (resolute, QEMU-VM):
  sämtliche `apt`-Paketnamen lösen auf, PipeWire ist 1.6.2 (≥ 1.4.9,
  kein Backport nötig), `caddy` 2.6.2 liegt in den Standard-Repos,
  Firefox ist der stock Snap. **Keine `install.sh`-Änderung nötig.**
* Gesamte FXRoute-Anwendung: DSP, Routing, Provider, Measurement, Library.
* `fxroute.service` (User-Unit), `source.tar`/`build-commit`-Muster,
  Geräte-Namensableitung (`fxroute-<machine-id>`), SSH-Defaults-Idee.

Ubuntu-spezifisch neu (Leap-/Agama-Teile, keine Wiederverwendung möglich):

| Leap (`iso/`) | Ubuntu (`ubuntu/`) |
|---|---|
| `build-leap-16-iso.sh` (mkmedia) | `build-ubuntu-iso.sh` (livefs-edit) |
| `profiles/*.jsonnet` (Agama) | `autoinstall/user-data` (Subiquity) |
| `build-live-from-installed.sh` (dmsquash-LiveFX) | Deps per `--install-packages` im Casper-Squashfs |
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
  autoinstall/user-data      Subiquity-Autoinstall (en_US/us, Rest Standard)
  scripts/
    first-boot-install-ubuntu.sh
    fxroute-live-autostart.sh
    fxroute-ubuntu-launcher.sh
```

## Phase-1-Abnahme (QEMU, UEFI, Secure Boot vorerst aus)

1. ISO bootet, `Try Ubuntu` → Live-Session mit FXRoute (`/api/status`).
2. `Install FXRoute` → WLAN/Ethernet + Benutzer interaktiv, Rest automatisch.
3. Installiertes System bootet selbstständig → GDM-Autologin → Firefox-Kiosk
   mit FXRoute, `fxroute.service` aktiv, `/api/status` OK.

## Offene Hardware-Gates (Phase 2, echte Hardware)

* WLAN-Realtest (Intel 8265 / 8086:24fd / iwlwifi): im Installer
  konfigurieren → nach Erstboot ohne Zweiteinrichtung verbunden.
* Secure Boot auf echter Hardware (nur signierter Ubuntu-Pfad, kein MOK).
* Scarlett-Audiointerface am Zielsystem.
