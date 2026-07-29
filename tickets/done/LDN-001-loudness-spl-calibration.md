# Ticket: LDN-001

## Project
FXRoute

## Goal
LSP-Loudness als globalen Helper vor dem finalen Peak Limiter integrieren,
exklusiv zu Auto Gain betreiben und eine profilbezogene SPL-Kalibrierung
bereitstellen.

## Task
Die freigegebene Loudness-/SPL-Spezifikation eng begrenzt in Backend und
Frontend umsetzen, automatisiert testen und lokal committen.

## Input
- Autoritatives Projekt: `/home/pbclaw/ai/projects/fxroute`
- EasyEffects-Referenzpreset auf `.104`: `loudness.json`
- Kein Worktree, keine Kopie, kein Refactoring, kein Release
- Bestehende uncommittete AutoSub-Änderungen nicht überschreiben

## Expected Output
- LSP-Loudness unmittelbar vor dem finalen Peak Limiter
- Persistente UI und Backend-Exklusivität zu Auto Gain
- Eindeutiges Volume-Routing ohne doppelten Gain
- Manuell vollständig nutzbare SPL-Kalibrierung
- Automatische SPL-Ermittlung nur bei verifizierbarer absoluter Kette
- Zielgerichtete Tests und lokaler Commit

## Target Path
`/home/pbclaw/ai/projects/fxroute`

## Notes
Automatische USB-Mikrofon-SPL-Ermittlung muss fail-closed sein; unbekannte
Capture-Gains oder unzureichende analoge Kalibrierungen erzwingen manuellen
Fallback.

## Status
done
