# FXRoute Armbian-Image-Builds

Build-Protokoll der offiziellen Armbian-Images (`armbian/build-image.sh`).
Ablage der Images: `dist/` (git-ignoriert). Build-Logs `armbian-vim1s-build-*.log`
sind ebenfalls git-ignoriert und liegen nur lokal im Checkout.

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
