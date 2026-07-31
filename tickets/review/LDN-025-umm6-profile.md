# LDN-025 — Dayton Audio UMM-6 SPL calibration profile

## Project
FXRoute

## Goal
Ausschließlich Dayton Audio UMM-6 für die automatische SPL-Kalibrierung
unterstützen, ohne UMIK-1 oder UMIK-2 zu verändern.

## Requirements

- UMM-6 anhand stabiler USB-/PipeWire-Metadaten und Modellname erkennen.
- Individuelle Cal-Datei lesen: Sens Factor und SERNO; Serienzuordnung
  fail-closed prüfen.
- Bekannte UMM-6-Referenz verwenden: −19 dBFS/Pa bei +30 dB IPGA.
- Passenden Capture-Referenzzustand prüfen; bei unbekannter oder
  widersprüchlicher Zuordnung automatische Messung deaktivieren.
- Dreispalten-Cal-Dateien mit Frequenz, Korrektur und Phase unterstützen.
  Die vorhandene Zwei-Spalten-Unterstützung für UMIK-1/UMIK-2 darf sich nicht
  ändern. Phase ist bei der frequenzabhängigen Korrektur korrekt zu behandeln.
- Bei Unsicherheit den bestehenden manuellen C/Slow-Fallback verfügbar lassen.
- UI soll für das erkannte Profil „Dayton UMM-6“ anzeigen.
- UMIK-1 und UMIK-2 nicht verändern; bestehende Tests müssen grün bleiben.

## Fixture / verification

- Eine realistische dreispaltige UMM-6-Cal-Datei als versionierte Fixture
  verwenden, nicht nur eine synthetisch erzeugte Inline-Zeichenkette.
- Profil-, Header-, Serien-, Metadaten-, Capture-Gate- und Korrekturtests
  ergänzen.
- Python-/JavaScript-Syntax, fokussierte SPL-/UMIK-Regressionen und
  `git diff --check` ausführen.
- Mangels UMM-6-Hardware als fixture-basiert implementiert, aber praktisch
  nicht verifiziert dokumentieren.

## Deployment / git

- Separater lokaler Commit ausschließlich für dieses Ticket.
- Danach auf `paul@192.168.178.104:/home/paul/fxroute` deployen und Service/API
  prüfen, ohne fremde Remote-Änderungen zu überschreiben.
- Kein Push und kein Release.
- Bestehende uncommittete Dateien und Backups nicht anfassen oder aufnehmen.

## Scope guard

Keine Änderungen an UMIK-1-/UMIK-2-Profilen, SPL-Grundformel außerhalb des
notwendigen UMM-6-Pfads, Loudness, AutoSub oder allgemeinem Messablauf.

## Status
implemented locally; fixture-based validation complete, hardware verification pending

## Implementation

- Added a separate `Dayton UMM-6` SPL profile without changing the UMIK-1 or
  UMIK-2 profile implementations.
- Detection requires USB VID/PID `0d8c:0147` and a UMM-6 model token in
  PipeWire/ALSA device metadata.
- The individual `Sens Factor` and `SERNO` are parsed and the serial must match
  the calibration filename.
- Automatic SPL is enabled only for the documented maximum UMM-6 input state:
  +30 dB IPGA, represented by the verified 100% / 0 dB PipeWire capture state.
- The UMM-6 calculation uses the individual Sens Factor as the dBFS reading at
  94 dB SPL. The nominal manufacturer reference is −19 dBFS/Pa at +30 dB IPGA.
- A UMM-6-only three-column parser applies inverse magnitude and phase as a
  complex frequency-domain correction. The existing two-column UMIK parser and
  both UMIK SPL paths remain unchanged.
- Any missing, malformed, duplicate, mismatched, or unverifiable UMM-6 input,
  calibration, serial, curve, or capture state disables automatic measurement;
  the existing manual C/Slow workflow remains available.
- Added the versioned realistic calibration fixture
  `scripts/fixtures/1880171.txt`.

## Validation

- `python3 scripts/test_umm6_spl_calibration.py` — passed
- `python3 scripts/test_umik1_spl_calibration.py` — passed
- `python3 scripts/test_umik2_spl_calibration.py` — passed
- `python3 scripts/test_spl_calibration.py` — passed
- `python3 -m py_compile main.py scripts/test_umm6_spl_calibration.py scripts/test_umik1_spl_calibration.py scripts/test_umik2_spl_calibration.py` — passed
- `node --check app.js` — passed
- `node --check static/app.js` — passed
- `git diff --check -- main.py scripts/test_umm6_spl_calibration.py scripts/fixtures/1880171.txt tickets/in_progress/LDN-025-umm6-profile.md` — passed

## Remaining verification / assumptions

- No Dayton UMM-6 hardware was available, so USB/PipeWire discovery, the
  100% / 0 dB to +30 dB IPGA mapping, and absolute SPL accuracy could only be
  validated with fixtures and simulated metadata.
- The conservative Sens Factor interpretation follows the REW USB microphone
  convention (individual dBFS reading at 94 dB SPL at maximum input gain).
- Phase is interpreted as degrees and inverted together with the magnitude
  calibration. A hardware comparison against REW remains required.
- Per assignment, no commit, push, release, deployment, or remote service check
  was performed.
