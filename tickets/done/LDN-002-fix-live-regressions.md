# Ticket: LDN-002

## Project
FXRoute

## Goal
Die drei im Loudness-Live-Test gefundenen Fehler eng begrenzt beheben:
korrektes PipeWire-Volume-Mapping mit sicherer Umschaltreihenfolge,
Erhalt profilbezogener Kalibrierungsdaten bei partiellen Updates und
HTTP 400 für den Auto-Gain/Loudness-Mutex.

## Task
Nur die betroffenen Loudness-/SPL-Pfade und gezielten Regressionstests ändern,
die vorhandenen AutoSub-Hunks unangetastet lassen, alle freigegebenen Tests
ausführen und einen isolierten Follow-up-Commit auf `364690d` erzeugen.

## Input
- Autoritatives Projekt: `/home/pbclaw/ai/projects/fxroute`
- Basiscommit: `364690dae0ef94cc72a44091fa5f3c9a7f11cc2e`
- Live-Befunde auf `.104`: 46 % wurden als −6,745 statt ca. −20,23 dB
  übernommen; UI-Toggle löschte Trim/Profil; Mutex lieferte HTTP 500.
- Keine AutoSub-, USB-Mikrofon-, Release-, Push- oder Refactoring-Arbeit.

## Expected Output
- Autoritative/kubische Volume-Umrechnung und sichere Toggle-Reihenfolge
- Partielle Update-Semantik ohne Verlust von Trim oder Profilzuordnung
- Strukturierter HTTP-400-Mutexfehler
- Gezielte Regressionstests und isolierter Follow-up-Commit

## Target Path
`/home/pbclaw/ai/projects/fxroute`

## Notes
Deployment und Live-Test erfolgen erst nach Main-Review des lokalen Commits.

## Status
done

## Result
- Autoritative kubische FXRoute/PipeWire-Umrechnung zentral in
  `system_volume.py`; 46 % ergeben −20,23453 dB.
- Sichere Umschaltreihenfolge: beim Einschalten erst Loudness-Preset laden,
  dann System-Master auf 100 %; beim Ausschalten erst System-Master
  restaurieren, dann Loudness bypassen.
- JSON-Updates besitzen Merge-Semantik und erhalten Kalibrierungstrim,
  Profilzuordnung und Profilhistorie bei Toggle/FFT-Wechsel.
- Auto-Gain/Loudness-Mutex liefert strukturierten HTTP 400.

## Verification
- Loudness-Integration, SPL-Kalibrierung und gezielte LDN-002-Regressionen:
  bestanden.
- AutoSub Gain Apply/Revert (24), Gain Calculation (7), Polarity (5),
  Candidate Ledger (6), Main Target Anchor (9), Target Curve Snapshot (6):
  bestanden.
- Python-Compile, JavaScript-Syntax und `git diff --check`: bestanden.
- `test_auto_sub_main_reference_capture.py`: bestehender unabhängiger Fehler;
  Test erwartet die in `main.py` nicht vorhandene Funktion
  `_prepare_subwoofer_runtime_for_measurement_start`.
