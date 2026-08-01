# Ticket: PEQ-001

## Project
FXRoute

## Goal
Den bestehenden PEQ Assistant gezielt auf zwölf temporäre Filter erweitern.

## Task
Das gerade eingeführte Assistant-Limit ausschließlich im bestehenden PEQ-Assistant-Frontendpfad von acht auf zwölf erhöhen: F1–F12, identische Bedienung/Bearbeitungszeile, F9–F12 initial ungesetzt, Add-Guard, Slot-Erzeugung, Zähler und beide Hilfetexte auf zwölf. Take L/R/Both und Create PEQ Preset unverändert lassen. Den vorhandenen Test so anpassen, dass zwölf Slots akzeptiert, ein dreizehnter abgewiesen und alle zwölf gesetzten Filter vollständig in bestehender Reihenfolge in den L/R-Payload übernommen werden.

## Input
- `static/app.js`
- `static/index.html`
- Backend-/Preset-Pfad unterstützt bestätigt bis zu 20 Bänder pro Kanal
- Nur das gerade eingeführte Assistant-Limit von acht auf zwölf ändern
- Keine Änderungen an anderen PEQ-, Target- oder Convolver-Pfaden
- Keine UI-Neugestaltung
- Keine Deployment-, Push- oder Release-Arbeit
- Fremde/uncommittete Worktree-Änderungen erhalten

## Expected Output
- Eng begrenzte Änderungen am PEQ Assistant
- Angepasster automatisierter Zwölf-Slot-/Preset-Reihenfolgetest
- Test- und Syntaxnachweis

## Target Path
`/home/pbclaw/ai/projects/fxroute`

## Notes
Nur ticketbezogene Dateien ändern. Vorhandenes Preset-Staging mappt aktuell bereits `peq.filters` vollständig; Verhalten nicht unnötig umbauen.

## Status
done
