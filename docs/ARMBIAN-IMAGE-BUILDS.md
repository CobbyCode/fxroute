# FXRoute Armbian-Image-Builds

Build-Protokoll der offiziellen Armbian-Images (`armbian/build-image.sh`).
Ablage der Images: `dist/` (git-ignoriert). Build-Logs `armbian-vim1s-build-*.log`
sind ebenfalls git-ignoriert und liegen nur lokal im Checkout.

## 2026-09-06 — khadas-vim1s (trixie/legacy), HEAD `6edc851`

- FXRoute HEAD: `6edc8517c63080109cb323e7f46f48ea83dc5de6`
  (`fix(installer): wait for PipeWire/DSP readiness instead of failing
  cold boot`), sauberer kanonischer `main`, Working Tree clean. VERSION im
  Repo und damit im Build: `0.9.17` (unveraendert).
- Enthalten seit dem Vorgaenger-Build (`d40e801`, ebenfalls 0.9.17):
  `e3b1d76` (Appliance-First-Boot: X11-Autologin-Session,
  lokalisierte Desktop-Ordner, Bookmark-/Wallpaper-Robustheit),
  `3a82d09` (Hardware-Power-Button faehrt sauber herunter) und `6edc851`
  (PipeWire/DSP-Validierung mit begrenztem Wait/Retry statt Cold-Boot-Fehlalarm);
  dazu zwei Docs-Commits. Keine Versionsaenderung.
- Altes Artefakt vorher geloescht:
  `dist/fxroute-armbian-khadas-vim1s-trixie-legacy.oowow.img.xz` + `.sha256`
  (Stand HEAD `d40e801`, 394338304 Bytes) sowie alle fuenf alten
  `armbian-vim1s-build-*.log`-Dateien (Platzersparnis).
- Armbian-Pin: `4a50e16e09222e00d3f57884b4dfbf8fdb4ce5dc` (im Wrapper
  fest verdrahtet, unveraendert).
- Kommando: `./armbian/build-image.sh --board khadas-vim1s`
  (BOARD=khadas-vim1s, RELEASE=trixie, BRANCH=legacy, EXT=image-output-oowow),
  als Hintergrund-Job. Keine weiteren Codeaenderungen, keine Full-Suite
  und keine zusaetzliche Checksum-/Entpack-Verifikation (wie beauftragt);
  die Builder-Marker sind das Fertigstellungskriterium.
- Build-Log: `armbian-vim1s-build-2026-09-06-6edc851.log` (5094 Zeilen,
  lokal, git-ignoriert). Erfolgreich: `[armbian] wrote` + `[armbian] sha256`
  im Log, null `[armbian][error]`-Zeilen, Exit-Status 0. Einzige Warnung:
  bekannte kosmetische `Permission denied`-Meldungen beim Aufraeumen
  Docker-root-owned Cache-Restdateien (`[armbian][warn]` zum Work-Dir),
  wie in frueheren Laeufen; beeinflusst den Erfolg nicht.
- Erzeugtes Image: `dist/fxroute-armbian-khadas-vim1s-trixie-legacy.oowow.img.xz`
  (395694080 Bytes), plus `.sha256`-Sidecar:
  `e05a6b9366b2dca98d2f62a720410bb355a551222d76ba8551e9b12f198d813f`.
- Bewusst keine frische Image-Abnahme in diesem Schritt: nicht geflasht,
  kein Onboarding/First-Boot-Test; die Endverifikation laeuft wie ueblich
  auf dem frisch geflashten Geraet.
- Kein Push, kein Release.

## 2026-09-06 — khadas-vim1s (trixie/legacy), HEAD `d40e801`

- FXRoute HEAD: `d40e801f7e3351bbfff53a55430e53671b407068`
  (`docs(release): 0.9.17 with provider-setup and DSP hygiene fixes`),
  sauberer kanonischer `main`, Working Tree clean. VERSION im Repo und
  damit im Build: `0.9.17` (Patch-Bump von 0.9.16, kein 1.x).
- Enthalten seit dem Vorgaenger-Build (`b04d35b`): nur `57722bd`/`4f95e74`
  (README/MANUAL-Dokumentation) und `d40e801` (Version-Bump 0.9.17);
  `armbian/` seit `5dfda4e` unveraendert — keine produktrelevanten
  Codeaenderungen, der Build traegt den abgenommenen 0.9.16-Stand plus
  Release-Dokumentation.
- Vorab-Checks bestanden: 298 GiB frei auf /home (NVMe), 30 GiB RAM
  (16 GiB verfuegbar), Docker 29.4.0 mit
  `ghcr.io/armbian/docker-armbian-build:armbian-debian-trixie-latest`,
  Armbian-Cache-Checkout exakt auf Pin `4a50e16e…`, kein anderer Build
  aktiv (nur die beiden `armbian-web-config.py --preview`-Prozesse),
  `scripts/test_armbian_images.py` (46/46) und
  `scripts/test_armbian_web_config.py` (66/66) vor dem Build OK.
- Altes Artefakt vorher geloescht:
  `dist/fxroute-armbian-khadas-vim1s-trixie-legacy.oowow.img.xz` + `.sha256`
  (Stand HEAD `b04d35b`, 394137600 Bytes, sha256 `378d7ed5…`).
- Armbian-Pin: `4a50e16e09222e00d3f57884b4dfbf8fdb4ce5dc` (im Wrapper
  fest verdrahtet; Cache-Checkout exakt auf diesem Pin, im Build-Log
  bestaetigt).
- Kommando: `./armbian/build-image.sh --board khadas-vim1s`
  (BOARD=khadas-vim1s, RELEASE=trixie, BRANCH=legacy, EXT=image-output-oowow).
- Build: Start 2026-09-06T14:25:33Z, detached via `setsid nohup` (PID
  2505872, eigene Session). Work-Dir dieses Laufs: `image.yuIggN`.
  Kernel-Artifact `linux-image-legacy-meson-s4t7 5.15.137` und U-Boot
  `uboot-khadas-vim1s-legacy 2019.01` aus dem Armbian-Remote-Cache
  (ORAS/ghcr).
- Build-Log: `armbian-vim1s-build-2026-09-06-d40e801.log` (5094 Zeilen,
  lokal, git-ignoriert, persistent).
- Build: Ende 2026-09-06T14:33:54Z (Dauer ca. 8,5 Minuten). Erfolgreich:
  `[armbian] wrote` + `[armbian] sha256` im Log, null
  `[armbian][error]`-Zeilen, keine interaktiven Prompts. Einzige Warnung:
  bekannte kosmetische `Permission denied`-Meldungen (4780 Zeilen) beim
  Aufraeumen Docker-root-owned Cache-Restdateien; das Work-Dir
  `image.yuIggN` schliesst sich damit den Alt-Resten an. Diesmal keine
  `[wrapper] exit status`-Zeile im Log: die Zeile stammte in frueheren
  Laeufen aus der ueberwachenden Shell, nicht aus dem Wrapper; der
  Wrapper endete hier unmittelbar nach `copy_image_output` (letzte
  Anweisung, `set -Eeuo pipefail`) mit Exit 0.
- Erzeugtes Image: `dist/fxroute-armbian-khadas-vim1s-trixie-legacy.oowow.img.xz`
  (394338304 Bytes), plus `.sha256`-Sidecar:
  `32081adb62757e19d087d198c808c55e3f0e62bbe27c91feedabacfe951d63f3`.
- Verifikation (grundlegende Build-Checks): `sha256sum -c` des neuen
  Images: OK. `xz -t` (XZ-Stream-Integritaet): OK. Dateityp: XZ mit
  CRC64-Checksumme.
- Provenienz (HEAD im Image nachgewiesen):
  - Root-Partition (MBR, Start Sektor 8192) aus dem dekomprimierten Image
    per dd extrahiert (im `--privileged` Armbian-Container; unprivilegierte
    Container-Zugriffe scheitern am Daemon-seitigen Userns-Remapping) und
    `/opt/fxroute-armbian/source.tar` per `debugfs` ausgelesen:
    byte-identisch mit dem deterministischen Archiv aus HEAD `d40e801`
    (gleicher sha256 `a911c7b4e3baf4745cb1804de1cac5300ef0cc0a859fe287f9e9ad04f5a821a3`,
    39331840 Bytes, 719 Eintraege, `git ls-files`-Tar mit mtime 0).
  - Damit enthaelt das Image nachweislich den 0.9.17-Stand (`VERSION`-
    Bump `d40e801`) inklusive aller zuvor abgenommenen Fixes.
- Bewusst noch keine frische Image-Abnahme in diesem Schritt: nicht
  geflasht, kein Onboarding/First-Boot-Test; die bereits auf .126
  erledigten End-to-End-Abnahmen wurden nicht wiederholt, die
  Endverifikation (Onboarding, First-Boot, fxroute.service, HTTP :8000)
  laeuft wie ueblich auf dem frisch geflashten Geraet.
- Kein Push, kein Release.

## 2026-09-06 — khadas-vim1s (trixie/legacy), HEAD `b04d35b`

- FXRoute HEAD: `b04d35bbc6ddc6998709774af199a62ac051331e`
  (`fix(bluetooth): skip source-overview build while bluetooth input is idle`),
  sauberer kanonischer `main`, Working Tree clean.
- Enthalten seit dem Vorgaenger-Build (`3ab4fbc`): SMB-Discovery-Ueberarbeitung
  (`f1adc62`/`e54dc1d`/`a991030`/`3b47066`/`e115c59`), Fresh-Install-Bluetooth-Fixes
  samt Release `0.9.16` (`4b690c6`/`4c05de9`/`016f352`/`0536b67`) und die drei
  Logging-Hygiene-Gates (`6a75d3d` Samplerate-Change-Gate, `9623680`
  Subwoofer-Link-Watch-Gate, `b04d35b` Bluetooth-Source-Overview-Gate).
  `armbian/` seit `5dfda4e` unveraendert.
- Altes Artefakt vorher geloescht:
  `dist/fxroute-armbian-khadas-vim1s-trixie-legacy.oowow.img.xz` + `.sha256`
  (Stand HEAD `3ab4fbc`, 394600448 Bytes, sha256 `a67c79dc…`).
- Armbian-Pin: `4a50e16e09222e00d3f57884b4dfbf8fdb4ce5dc` (im Wrapper
  fest verdrahtet; Cache-Checkout exakt auf diesem Pin).
- Kommando: `./armbian/build-image.sh --board khadas-vim1s`
  (BOARD=khadas-vim1s, RELEASE=trixie, BRANCH=legacy, EXT=image-output-oowow).
- Build: Start 2026-09-06T10:38:43Z, detached via `setsid nohup` (PID
  2479597, eigene Session). Work-Dir dieses Laufs: `image.OKnKDt`.
  Kernel-Artifact `kernel-meson-s4t7-legacy 5.15.137` aus dem
  Armbian-Remote-Cache.
- Build-Log: `armbian-vim1s-build-2026-09-06-b04d35b.log` (5095 Zeilen,
  lokal, git-ignoriert, persistent).
- Build: Ende 2026-09-06T10:47:02Z (Dauer ca. 8,5 Minuten). Erfolgreich:
  `[armbian] wrote` + `[armbian] sha256` im Log, `[wrapper] exit status: 0`
  am Logende, keine `[armbian][error]`-Zeilen, keine interaktiven Prompts.
  Einzige Warnung: bekannte kosmetische `Permission denied`-Meldungen (4780
  Zeilen) beim Aufraeumen Docker-root-owned Cache-Restdateien; das Work-Dir
  `image.OKnKDt` schliesst sich damit den Alt-Resten an.
- Erzeugtes Image: `dist/fxroute-armbian-khadas-vim1s-trixie-legacy.oowow.img.xz`
  (394137600 Bytes), plus `.sha256`-Sidecar:
  `378d7ed55ad3085397e3096f646c0b8a177d799360f8587bec20de644077a5a4`.
- Verifikation (grundlegende Build-Checks): `sha256sum -c` des neuen
  Images: OK. `xz -t` (XZ-Stream-Integritaet): OK. Dateityp: XZ mit
  CRC64-Checksumme.
- Bewusst noch keine frische Image-Abnahme in diesem Schritt: nicht
  geflasht, kein Onboarding/First-Boot-Test; die Endverifikation
  (Onboarding, First-Boot, fxroute.service, HTTP :8000) laeuft auf dem
  frisch geflashten Geraet.
- Kein Push, kein Release.

## 2026-09-06 — khadas-vim1s (trixie/legacy), HEAD `3ab4fbc`

- FXRoute HEAD: `3ab4fbc76c6b264bc3a14011e8b10849752cee2e`
  (`fix(spotify): keep claim watch alive across late provider installs`),
  sauberer kanonischer `main`, Working Tree clean.
- Seit dem Vorgaenger-Build (`7ed93fa`) nur `3f26695` (docs) und `3ab4fbc`
  (spotify claim-watch); `armbian/` zuletzt geaendert in `5dfda4e` — keine
  offenen produktrelevanten Aenderungen, kein Build aktiv (nur die beiden
  `armbian-web-config.py --preview`-Prozesse).
- Altes Artefakt vorher geloescht:
  `dist/fxroute-armbian-khadas-vim1s-trixie-legacy.oowow.img.xz` + `.sha256`
  (Stand HEAD `7ed93fa`, 393326592 Bytes, sha256 `c97c1bb7…`).
- Armbian-Pin: `4a50e16e09222e00d3f57884b4dfbf8fdb4ce5dc` (im Wrapper
  fest verdrahtet).
- Kommando: `./armbian/build-image.sh --board khadas-vim1s`
  (BOARD=khadas-vim1s, RELEASE=trixie, BRANCH=legacy, EXT=image-output-oowow).
- Build: Start 2026-09-06T04:30:24Z, detached via `setsid nohup` (PID
  2444241, eigene Session). Work-Dir dieses Laufs: `image.tq98UG`.
- Build-Log: `armbian-vim1s-build-2026-09-06-3ab4fbc.log` (5094 Zeilen,
  lokal, git-ignoriert, persistent).
- Build: Ende ~2026-09-06T04:39Z (Dauer ca. 9 Minuten). Erfolgreich:
  `[armbian] wrote` + `[armbian] sha256` im Log (deterministische Kopie via
  `copy_image_output`), keine `[armbian][error]`-Zeilen, keine
  interaktiven Prompts. Einzige Warnung: bekannte kosmetische
  `Permission denied`-Meldungen (4780 Zeilen) beim Aufraeumen
  Docker-root-owned Cache-Restdateien; das Work-Dir `image.tq98UG`
  schliesst sich damit den Alt-Resten an.
- Erzeugtes Image: `dist/fxroute-armbian-khadas-vim1s-trixie-legacy.oowow.img.xz`
  (394600448 Bytes), plus `.sha256`-Sidecar:
  `a67c79dc4d9bd6431f9b983daca2819c4aa20e62aa5583f6d91ce8ee1aab46e6`.
- Verifikation (grundlegende Build-Checks): `sha256sum -c` des neuen
  Images: OK. `xz -t` (XZ-Stream-Integritaet): OK. Dateityp: XZ mit
  CRC64-Checksumme.
- Bewusst noch keine frische Image-Abnahme in diesem Schritt: nicht
  geflasht, kein Onboarding/First-Boot-Test; die Endverifikation
  (Onboarding, First-Boot, fxroute.service, HTTP :8000) laeuft auf dem
  frisch geflashten Geraet.
- Kein Push, kein Release.

## 2026-09-06 — khadas-vim1s (trixie/legacy), HEAD `7ed93fa`

- FXRoute HEAD: `7ed93fac909e1c4ff69504e1036346de5959a376`
  (`fix(deps): pin requests==2.34.2 to satisfy tidalapi floor`),
  sauberer kanonischer `main`, Working Tree clean.
- Vorab-Checks bestanden: 303 GiB frei auf /home (NVMe), 30 GiB RAM
  (16 GiB verfuegbar; Swap 2/2 GiB belegt = Bestandszustand, identisch zu
  den erfolgreichen Vorgaenger-Builds), Docker 29.4.0 mit
  `ghcr.io/armbian/docker-armbian-build:armbian-debian-trixie-latest`,
  Armbian-Cache-Checkout exakt auf Pin `4a50e16e…`, kein anderer Build
  aktiv, `scripts/test_armbian_images.py` (46/46) und
  `scripts/test_armbian_web_config.py` (66/66) vor dem Build OK.
- Armbian-Pin: `4a50e16e09222e00d3f57884b4dfbf8fdb4ce5dc`
  (Cache-Verzeichnis bereits auf diesem Pin, Docker-Basis vorhanden).
- Kommando: `./armbian/build-image.sh --board khadas-vim1s`
  (BOARD=khadas-vim1s, RELEASE=trixie, BRANCH=legacy, EXT=image-output-oowow).
- Build: Start 2026-09-06T02:50:57Z, detached via `setsid nohup` (PID
  2421688, eigene Session; laeuft unabhaengig von der Agent-Session
  weiter). Work-Dir dieses Laufs: `image.ij2TBk`. Kernel-Artifact
  `kernel-meson-s4t7-legacy 5.15.137` und U-Boot
  `uboot-khadas-vim1s-legacy 2019.01` kommen aus dem
  Armbian-Remote-Cache.
- Build-Log: `armbian-vim1s-build-2026-09-06-7ed93fa.log` (lokal,
  git-ignoriert, persistent).
- Bemerkung: bekannte kosmetische `Permission denied`-Meldungen (4780
  Zeilen) beim Aufraeumen Docker-root-owned Cache-Restdateien
  (Daemon-seitiges Userns-Remapping; beeinflusst Exit-Status nicht); das
  Work-Dir `image.ij2TBk` schliesst sich damit den Alt-Resten an.
- Build: Ende 2026-09-06T02:59Z (Dauer ca. 8,5 Minuten), Exit-Status 0.
  Der Wrapper lief vollstaendig bis zum erfolgreichen `copy_image_output`
  (`[armbian] wrote` + `[armbian] sha256` im Log); einzige Warnung die
  bekannte kosmetische Aufraeum-Meldung.
- Erzeugtes Image: `dist/fxroute-armbian-khadas-vim1s-trixie-legacy.oowow.img.xz`
  (393326592 Bytes), plus `.sha256`-Sidecar:
  `c97c1bb7b0892464c6f1af7683ce6751cfebca353c6c9639cf4da0d17bb7d347`.
  Es ersetzt das alte Image (Stand HEAD `a69d6b2`, 406597632 Bytes) auf
  demselben Ausgabepfad.
- Verifikation (grundlegende Build-Checks): `sha256sum -c` des neuen
  Images: OK. `xz -t` (XZ-Stream-Integritaet): OK. Dateityp: XZ mit
  CRC64-Checksumme.
- Bewusst noch keine frische Image-Abnahme in diesem Schritt: nicht
  geflasht, kein Onboarding/First-Boot-Test; die Endverifikation
  (Onboarding, First-Boot, fxroute.service, HTTP :8000) laeuft auf dem
  frisch geflashten Geraet.
- Kein Push, kein Release.

## 2026-09-06 — khadas-vim1s (trixie/legacy), HEAD `a69d6b2`

- FXRoute HEAD: `a69d6b2b8545c345d9ce5281f722aeaad1481730`
  (`fix(providers): machine-readable 503 contract replaces prose-matching`),
  sauberer kanonischer `main`, Working Tree clean.
- Neubau nach Loeschen aller bisherigen VIM1S-Artefakte: Image
  `dist/fxroute-armbian-khadas-vim1s-trixie-legacy.oowow.img.xz` + `.sha256`
  (Stand `5dfda4e`, 405950464 Bytes) sowie die drei alten Build-Logs
  `armbian-vim1s-build-2026-09-05*.log` entfernt.
- Vorab-Checks bestanden: 304 GiB frei auf /home (NVMe), 30 GiB RAM
  (16 GiB verfuegbar; Swap 2/2 GiB belegt = Bestandszustand, identisch zu
  den erfolgreichen Vorgaenger-Builds), Docker 29.4.0 mit
  `ghcr.io/armbian/docker-armbian-build:armbian-debian-trixie-latest`,
  Armbian-Cache-Checkout exakt auf Pin `4a50e16e…`, kein anderer Build
  aktiv, `scripts/test_armbian_images.py` und `test_armbian_web_config.py`
  vor dem Build OK.
- Armbian-Pin: `4a50e16e09222e00d3f57884b4dfbf8fdb4ce5dc`
  (Cache-Verzeichnis bereits auf diesem Pin, Docker-Basis vorhanden).
- Kommando: `./armbian/build-image.sh --board khadas-vim1s`
  (BOARD=khadas-vim1s, RELEASE=trixie, BRANCH=legacy, EXT=image-output-oowow).
- Build: Start 2026-09-05T22:21:06Z, detached via `setsid nohup` (PID
  2364666, eigene Session; laeuft unabhaengig von der Agent-Session
  weiter). Work-Dir dieses Laufs: `image.R2x7zv`. Kernel-Artifact
  `kernel-meson-s4t7-legacy 5.15.137` kommt aus dem Armbian-Remote-Cache.
- Build-Log: `armbian-vim1s-build-2026-09-06-a69d6b2.log` (lokal,
  git-ignoriert, persistent; der Wrapper haengt seine Exit-Status-Zeile
  `[wrapper] exit status: <rc>` ans Logende).
- Bemerkung: Drei alte Docker-root-owned Work-Dir-Reste
  (`~/.cache/fxroute/armbian/tmp/image.{j75Asc,Pc2nmN,Ttv4aJ}`, je ~296 MB)
  waren mit `rm` und Container-Root (auch `--userns=host`) nicht
  entfernbar (Daemon-seitiges Userns-Remapping; Host-Sudo braucht
  Passwort). Kosmetisch bei 304 GiB frei; der Build nutzt ein frisches
  mktemp-Work-Dir.
- Build: Ende 2026-09-05T22:29Z, Exit-Status 0 (8 Minuten 40 Sekunden).
  Einzige Warnung: bekannte kosmetische `Permission denied`-Meldungen beim
  Aufraeumen Docker-root-owned Cache-Restdateien; das Work-Dir `image.R2x7zv`
  schliesst sich damit den drei Alt-Resten an (siehe oben).
- Erzeugtes Image: `dist/fxroute-armbian-khadas-vim1s-trixie-legacy.oowow.img.xz`
  (406597632 Bytes), plus `.sha256`-Sidecar:
  `a573f24aa7aa945cd8df9b2a720e3c797433d1d32b5af5821d3ee676025811e8`.
- Verifikation (HEAD `a69d6b2` im Image enthalten):
  - `sha256sum -c` des neuen Images: OK.
  - `/opt/fxroute-armbian/source.tar` per dd + `debugfs` (aus dem
    `--privileged` Armbian-Container; unprivilegierte Container-Zugriffe
    scheitern am Daemon-seitigen Userns-Remapping) extrahiert: Inhalt
    byte-identisch mit dem deterministischen Archiv aus `a69d6b2`
    (`diff -rq` Exit 0, 713 Eintraege). Einziger Unterschied auf Tar-Ebene:
    Modus-Bits der sechs `media/screenshots/*.png` (666 im Build-Archiv
    wegen Disk-Modus im Haupt-Checkout vs. 644 in der Git-Referenz).
  - Image-`installer_contract.py` enthaelt `PROVIDER_CONTRACT=helper-missing`,
    Image-`install.sh` 7 Contract-Referenzen, Image-`armbian-web-config.py`
    `disableWifiForEthernet` (2 Treffer).
- Kein Push, kein Release. Flashen per OOWOW erfolgt manuell; die
  Endverifikation (Onboarding, First-Boot, fxroute.service, HTTP :8000)
  laeuft auf dem frisch geflashten Geraet.

## 2026-09-05 — khadas-vim1s (trixie/legacy), HEAD `5dfda4e`

- FXRoute HEAD: `5dfda4e5fcbfc4c19b6006a903b934cdb41a386b`
  (`fix(armbian): disable Wi-Fi input while Ethernet is active`)
- Enthaltene First-Boot/Onboarding-Fixes seit `12bbd06`: `5dfda4e`
  (WLAN-Eingabe bei aktivem Ethernet deaktiviert, Stray-WLAN im POST wird
  ignoriert), `84f43b9` (root-staged Installer-Override ohne Reflash),
  `7c7dc0a` (PipeWire `-dev`-Pakete aus trixie-backports bei aktivem
  1.4.9-Backports-Laufzeitsystem; behebt den auf .125 reproduzierten
  `libpipewire-0.3-dev`-Abhängigkeitsabbruch im First-Boot).
- Ausgangszustand: sauberer kanonischer `main`, keine weiteren funktionalen
  Aenderungen in diesem Durchlauf.
- Armbian-Pin: `4a50e16e09222e00d3f57884b4dfbf8fdb4ce5dc`
  (Cache-Verzeichnis bereits auf diesem Pin, Docker-Basis vorhanden).
- Kommando: `./armbian/build-image.sh --board khadas-vim1s`
  (BOARD=khadas-vim1s, RELEASE=trixie, BRANCH=legacy, EXT=image-output-oowow).
- Build: Start 2026-09-05T14:54Z, Ende 2026-09-05T15:02Z, Exit-Status 0.
  Einzige Warnung: bekannte kosmetische `Permission denied`-Meldungen beim
  Aufraeumen Docker-erzeugter root-owned Cache-Dateien (beeinflusst Exit-Status nicht).
- Erzeugtes Image: `dist/fxroute-armbian-khadas-vim1s-trixie-legacy.oowow.img.xz`
  (405950464 Bytes), plus `.sha256`-Sidecar:
  `1f566fea905efd580b2518c02179a8934ac21780a899e690b8bb98e7298c77bc`.
- Das neue Image ersetzt das alte (Stand 2026-09-05T09:27Z, HEAD `12bbd06`)
  auf demselben Ausgabepfad; `sha256sum -c` des neuen Images: OK.
- Verifikation (HEAD im Image enthalten):
  - `sha256sum -c` des neuen Images: OK.
  - `/opt/fxroute-armbian/source.tar` aus dem Image extrahiert und byte-identisch
    mit dem deterministischen Archiv aus HEAD (gleicher md5).
  - `install.sh` aus dem Image-Archiv identisch mit Checkout (`0c75dcb8…`),
    enthaelt `debian_trixie_backports_active` (2 Treffer).
  - `armbian-web-config.py` aus dem Image-Archiv enthaelt `disableWifiForEthernet`
    (2 Treffer); `first-boot-install.sh` enthaelt `INSTALLER_OVERRIDE_FILE`
    (3 Treffer).
  - Wrapper-Regression `scripts/test_armbian_images.py`: 46/46 OK,
    `scripts/test_armbian_web_config.py`: 66/66 OK (vor dem Build).
- Build-Log: `armbian-vim1s-build-2026-09-05-5dfda4e.log` (lokal, git-ignoriert).
- Kein Push, kein Release. Flashen per OOWOW erfolgt manuell; die
  Endverifikation (Onboarding, First-Boot, fxroute.service, HTTP :8000)
  laeuft auf dem frisch geflashten Geraet.

## 2026-09-05 — khadas-vim1s (trixie/legacy), HEAD `12bbd06`

- FXRoute HEAD: `12bbd06a369a0558fedd5cfd35ff919eab68470a`
  (`feat(measurement): scale direct blend by gate decisiveness confidence`)
- Enthaltene Gate-/Hybrid-Fixes: `12bbd06` (Gate-Confidence-Skalierung),
  `9415846` (Direct-Gate per Repeat-Sweep validiert), `eabd023`
  (weicherer Gated-Direct-Hybrid-Blend).
- Ausgangszustand: sauberer kanonischer `main`, keine Codeaenderungen im Build-Schritt.
- Armbian-Pin: `4a50e16e09222e00d3f57884b4dfbf8fdb4ce5dc`
  (Cache-Verzeichnis bereits auf diesem Pin, Docker-Basis
  `ghcr.io/armbian/docker-armbian-build:armbian-debian-trixie-latest` vorhanden).
- Kommando: `./armbian/build-image.sh --board khadas-vim1s`
  (BOARD=khadas-vim1s, RELEASE=trixie, BRANCH=legacy, EXT=image-output-oowow).
- Build: Start 2026-09-05T09:16:00Z, Ende 2026-09-05T09:27:07Z, Exit-Status 0.
  Einzige Warnung: bekannte kosmetische `Permission denied`-Meldungen beim
  Aufraeumen Docker-erzeugter root-owned Cache-Dateien (beeinflusst Exit-Status nicht).
- Erzeugtes Image: `dist/fxroute-armbian-khadas-vim1s-trixie-legacy.oowow.img.xz`
  (406908928 Bytes), plus `.sha256`-Sidecar:
  `ca61c2aed5541cee2a95408149dbbd181529b880b42f1db4cf10c5f887fecc5d`.
- Altes Image geloescht (nach erfolgreichem `sha256sum -c`):
  `dist/fxroute-armbian-khadas-vim1s-trixie-legacy.oowow.img.xz` mit
  `424a2178fdb8cc984d4f7e393aa3de636b28c0cfcd7ba39d1692141fa5401c42`
  (Stand 2026-09-05T04:21Z, HEAD `9b88b81`).
- Verifikation (HEAD im Image enthalten):
  - `sha256sum -c` des neuen Images: OK.
  - `/opt/fxroute-armbian/source.tar` per `sfdisk` + `debugfs` aus dem Image
    extrahiert und byte-identisch mit dem deterministischen Archiv aus HEAD
    (`fab06577212cbfdce9fd5fd929ecd9fd0cf3593bd0d0efc1fda999d8e7d4237a`).
  - `measurement/hybrid.py` aus dem Image-Archiv identisch mit `git show HEAD`.
  - Gate-Marker (`_direct_gate_confidence`, `DIRECT_GATE_CONTRAST_SCALE`,
    `DIRECT_GATE_RUNNER_LIMIT`) in der Image-`hybrid.py` vorhanden (6 Treffer).
  - Wrapper-Regression `scripts/test_armbian_images.py` vor dem Build: 45/45 OK.
- Build-Log: `armbian-vim1s-build-2026-09-05-12bbd06.log` (lokal, git-ignoriert).
- Kein Push, kein Release, keine weiteren Images in diesem Schritt.
