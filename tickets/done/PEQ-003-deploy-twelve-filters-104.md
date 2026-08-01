# Ticket: PEQ-003

## Project
FXRoute

## Goal
Den lokal geprüften PEQ-001-Zwölf-Slot-Stand gesichert auf `paul@192.168.178.104:/home/paul/fxroute` deployen.

## Task
Produktionsstand read-only prüfen, Backup ausschließlich von `static/app.js` und `static/index.html` anlegen, genau diese beiden geprüften Dateien übertragen, `fxroute.service` neu starten und Service, `/api/status`, Remote-Hashes sowie Zwölf-Slot-Marker verifizieren.

## Input
- Lokale Hashes: `static/app.js` `99353a4f…`, `static/index.html` `e57917fd…`
- Ziel: `paul@192.168.178.104:/home/paul/fxroute`
- Gezielter Test: `node scripts/test_measurement_peq_eight_filters.js`

## Expected Output
- Recoverable Backup auf `.104`
- Nur beide PEQ-001-Produktionsdateien deployed
- Service/API gesund
- F1–F12 und Zwölf-Filter-Limit remote belegt

## Target Path
`paul@192.168.178.104:/home/paul/fxroute`

## Notes
Kein Commit, Push oder Release. Keine anderen Dateien oder Laufzeitkonfigurationen ändern.

## Status
done
