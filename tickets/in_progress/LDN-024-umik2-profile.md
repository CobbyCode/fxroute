# Ticket: LDN-024

## Project
FXRoute

## Goal
Auf Basis der bestätigten Mikrofonprofil-Struktur ausschließlich UMIK-2 für die
automatische SPL-Kalibrierung unterstützen, ohne UMIK-1-Verhalten zu ändern.

## Scope
- UMIK-2 anhand stabiler USB-/PipeWire-Metadaten erkennen.
- Aus der UMIK-2-Cal-Datei `Sens Factor`, `AGain` und `SERNO` lesen.
- `Sens Factor` nicht zusätzlich mit `AGain` verrechnen.
- Den dokumentierten Werksgain aus `AGain` voraussetzen und gegen den
  erforderlichen Capture-Zustand prüfen.
- Capture-Zustand einschließlich erforderlichem Capture-Level/Gain verifizieren;
  bei fehlenden oder widersprüchlichen Daten automatische SPL-Ermittlung
  fail-closed deaktivieren und manuelle SPL-Eingabe verfügbar lassen.
- UI bei gültigem UMIK-2 als „UMIK-2“ anzeigen.
- UMIK-1 unverändert weiter unterstützen.
- Fixtures für UMIK-2-Cal-Datei und PipeWire-Metadaten ergänzen.

## Restrictions
- Keine UMIK-1-Regression oder Änderung an SPL-Formel, Loudness, AutoSub oder
  Messablauf außerhalb des notwendigen Profil-/Capture-Gates.
- Kein UMM-6 oder sonstiges neues Mikrofonprofil.
- Separater lokaler Commit.
- Deploy auf `paul@192.168.178.104:/home/paul/fxroute` ohne Push oder Release.
- Mangels UMIK-2-Hardware als implementiert, aber praktisch nicht verifiziert
  dokumentieren.
- Bestehende ungetrackte Dateien und Backups nicht anfassen.

## Verification
- UMIK-2 gültig: USB-/PipeWire-Erkennung, Cal-Headerwerte, `AGain` als
  dokumentierter Werksgain, Capture-Referenzzustand und UI-Modell „UMIK-2“.
- UMIK-2 ungültig bei fehlendem/abweichendem `Sens Factor`, `AGain`, `SERNO`,
  Cal-Datei/Serienzuordnung, widersprüchlichem Capture-Gain oder nicht
  verfügbarem Capture.
- Prüfen, dass die SPL-Berechnung den Sens Factor nur einmal nutzt und AGain
  nicht als zusätzlicher SPL-Korrekturwert eingeht.
- Bestehende UMIK-1-Fixtures und fokussierte SPL-Regressionen ausführen.
- Syntax-/Diff-Prüfung und lokaler Commit.
- Deployment sowie Service-/API-Prüfung auf `.104`, soweit erreichbar.
- Keine praktische UMIK-2-Hardwareprüfung möglich; explizit vermerken.

## Status
in_progress

## Implementation results
- Added an isolated UMIK-2 profile using USB VID/PID `2752:002b` plus explicit
  UMIK-2 PipeWire/ALSA metadata; the UMIK-1 profile and its required checks remain
  unchanged.
- UMIK-2 calibration headers now require `Sens Factor`, `AGain = 18 dB`, and
  `SERNO`, including filename/serial matching.
- `AGain` is treated only as the documented factory/capture reference. The SPL
  calculation still consumes only `Sens Factor`; no additional AGain correction
  is applied.
- Automatic UMIK-2 SPL is fail-closed unless the selected capture is unique,
  available, and already reports the `100% / 0 dB` reference state. Manual SPL
  entry remains available through the existing path.
- The SPL UI displays the microphone model returned by the backend, including
  `UMIK-2`.

## Test results
- `python3 scripts/test_umik2_spl_calibration.py`: passed.
- `python3 scripts/test_umik1_spl_calibration.py`: passed.
- `python3 scripts/test_spl_calibration.py`: passed.
- `python3 -m py_compile main.py scripts/test_umik2_spl_calibration.py scripts/test_umik1_spl_calibration.py`: passed.
- `node --check static/app.js`: passed.
- `git diff --check`: passed.
- No practical UMIK-2 hardware verification was possible. The `18 dB` factory
  gain and `100% / 0 dB` capture-reference interpretation is implemented
  conservatively from the ticket/calibration-header semantics and remains the
  hardware-validation risk.
