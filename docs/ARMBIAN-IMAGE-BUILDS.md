# FXRoute Armbian-Image-Builds

Build-Protokoll der offiziellen Armbian-Images (`armbian/build-image.sh`).
Ablage der Images: `dist/` (git-ignoriert). Build-Logs `armbian-vim1s-build-*.log`
sind ebenfalls git-ignoriert und liegen nur lokal im Checkout.

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
