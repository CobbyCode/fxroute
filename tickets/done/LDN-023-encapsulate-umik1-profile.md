# Ticket: LDN-023

## Project
FXRoute

## Goal
Die bestehende UMIK-1-Erkennung als kleine interne Mikrofonprofil-Logik kapseln,
ohne das aktuelle Verhalten zu ändern.

## Scope
- Bestehende UMIK-1-Erkennung, Sens-Factor-Prüfung und Referenzdaten in einem
  internen UMIK-1-Profil bündeln.
- Noch kein UMIK-2- oder UMM-6-Support.
- C/Slow-Messung, SPL-Formel, Capture-/Messablauf, UI/API-Vertrag und übrige
  Audio-/Loudness-/AutoSub-Logik nicht umbauen.
- Verhalten bei gültigem UMIK-1 und bei fail-closed Ablehnungen unverändert.

## Verification
- Bestehenden UMIK-1-Regressionstest erweitern/umschreiben, falls nötig.
- Fokussierte lokale Tests und Syntaxprüfung ausführen.
- Auf `paul@192.168.178.104:/home/paul/fxroute` mit vorhandenem UMIK-1 bauen/deployen.
- Live prüfen: Erkennung ist verfügbar und ein SPL-Wert wird mit unveränderter
  Formel/Messung geliefert.
- Vorher/nachher relevante API-/Live-Werte dokumentieren; Nutzerzustand nach
  der Prüfung restaurieren.

## Restrictions
- Separater lokaler Commit.
- Kein Push, kein Release.
- Keine UI-Änderung.
- Bestehende ungetrackte Dateien und Backups nicht anfassen.

## Status
review

## Implementation

- `_Umik1Profile` bündelt ausschließlich die bestehenden UMIK-1-Konstanten,
  USB-/Metadaten-Erkennung, Sens-Factor-/SERNO-Headerprüfung und die
  Referenzwerte für internen Gain und Capture.
- Die vorhandenen internen Helfer delegieren an das Profil; der bestehende
  Capability-Aufbau und sein API-Dictionary bleiben unverändert.
- Kein UMIK-2-/UMM-6-Pfad wurde ergänzt.
- C/Slow-Berechnung, SPL-Formel, Capture-/Messablauf sowie UI-, Loudness- und
  AutoSub-Code wurden nicht geändert.

## Regression Coverage

- Gültiges UMIK-1 mit passendem Sens Factor/SERNO bleibt verfügbar.
- Falsche Vendor-/Product-ID und UMIK-2-Metadaten werden weiterhin abgelehnt.
- Falsche Kalibrierungsseriennummer, fehlender Sens Factor, unbekannter
  Capture-Gain und fehlende 18-dB-Referenz bleiben fail-closed.
- Die Referenzwerte 0 dB / 100 % und die bestehende SPL-Formel sind abgesichert.

## Local Verification

- `python3 -m py_compile main.py scripts/test_umik1_spl_calibration.py scripts/test_spl_calibration.py`
- `python3 scripts/test_umik1_spl_calibration.py`
- `python3 scripts/test_spl_calibration.py`
- `git diff --check`

Alle Prüfungen bestanden.

## Review Status

Der lokale Commit ist erstellt. Deployment und Live-Hardware-Prüfung auf `.104`
sind noch offen: Der Host ist aus der aktuellen Sitzung über SSH/Ping nicht
erreichbar (`No route to host` / `Destination Host Unreachable`). Es wurde
keine dauerhafte Netzwerkkonfiguration verändert.
