# Ticket: PEQ-002

## Project
FXRoute

## Goal
Den lokal geprüften PEQ-001-Stand gesichert auf `paul@192.168.178.104:/home/paul/fxroute` deployen.

## Task
Produktionsstand und Service vorab read-only prüfen, Backup der beiden Ziel-Dateien anlegen, ausschließlich `static/app.js` und `static/index.html` aus PEQ-001 übertragen, Service neu starten und per Service-/HTTP-/Datei-Nachweis verifizieren. Gezielten Node-Test lokal erneut als Quellnachweis ausführen; keine breiten Tests.

## Input
- Lokale geprüfte Dateien: `static/app.js`, `static/index.html`
- Ziel: `paul@192.168.178.104:/home/paul/fxroute`
- Service: `fxroute.service`
- Test: `node scripts/test_measurement_peq_eight_filters.js`

## Expected Output
- Recoverable Backup auf `.104`
- Nur die zwei PEQ-001-Produktionsdateien deployed
- Service aktiv, `/api/status` erreichbar
- Ziel-Dateien enthalten Acht-Slot-Limit und aktualisierten Hilfetext

## Target Path
`paul@192.168.178.104:/home/paul/fxroute`

## Notes
Kein Push, Release oder Commit. Keine Konfigurations-/Laufzustandsänderung außer unvermeidlichem Service-Neustart.

## Status
done
