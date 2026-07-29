# Ticket: LDN-005

## Project
FXRoute

## Goal
SPL Calibration vollständig als Abgleich-Assistent betreiben und jeden
Kalibrierungstrim aus dem digitalen Wiedergabe-Gainpfad entfernen.

## Task
Aktiven Live-Trim sofort neutralisieren, gespeicherte Altwerte fail-safe auf
0 migrieren, nur Messwert/Abweichung/Metadaten speichern, UI-Text korrigieren,
gezielt testen und auf `.104` live verifizieren.

## Input
Autoritatives Projekt `/home/pbclaw/ai/projects/fxroute`; beobachteter
`+27.9 dB`-Trim führte zu massivem Limiting. Loudness-Reihenfolge,
Volume-Routing und AutoSub bleiben unverändert.

## Expected Output
Separater Fix-Commit, gezielte Regressionstests und sicherer Live-Nachweis
mit 0 dB Limiter-Input-Gain bei Loudness aus und an.

## Target Path
`/home/pbclaw/ai/projects/fxroute`

## Status
done

## Result

- Live-Sofortmaßnahme neutralisierte aktiven und gespeicherten Trim auf `.104`.
- Limiter-Input-Gain bleibt unabhängig vom Loudness-Zustand fest bei 0 dB.
- SPL Save speichert nur Messwert, erforderliche Hardwareanpassung,
  Profil-/Mikrofon-/Cal-Metadaten, Datum und ±1-dB-Kalibrierstatus.
- Alte `calibrationTrimDb`-/`trimDb`-Daten werden beim Laden sicher migriert.
- Fokussierte Loudness-, SPL- und UMIK-Regressionstests bestanden.
