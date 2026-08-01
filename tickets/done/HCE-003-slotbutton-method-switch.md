# Ticket: HCE-003

## Project
FXRoute

## Goal
Slotbutton-Geometrie und Zustände von F1–F12 und P1–P8 vollständig vereinheitlichen und den Wechsel aus dem Custom-House-Curve-Editor bei erneuter PEQ-/Convolver-Aktivierung robust machen.

## Scope
- `static/app.js`, `static/style.css` und gezielte JS-Tests.
- F1–F12/P1–P8 gemeinsame Button-Klasse bzw. gemeinsame CSS-Zustände: Größe, Mindestbreite, Rundung, Gap, Schrift, leer/gestrichelt, belegt/farbig, aktiv/Füllung/Hervorhebungsring.
- Während Custom-House-Curve-Editing PEQ und Convolver bedienbar.
- PEQ-/Convolver-Aktivierung schließt den Editor unabhängig davon, ob der zugrunde liegende Methodowert bereits PEQ ist.
- Vorher tatsächlich aktive Target Curve merken; nach Moduswechsel wiederherstellen, falls vorhanden, sonst Neutral.
- Custom-Draft beim Verlassen erhalten; erneutes Öffnen zeigt ihn wieder.
- Keine Änderungen an House-Curve-Speicherung, Filterberechnung oder Export.

## Verification
1. Neutral → Custom öffnen.
2. PEQ aktivieren: Editor zu, Target Neutral, PEQ Assistant sichtbar.
3. Custom erneut öffnen: Draft erhalten.
4. Convolver aktivieren: Editor zu, vorheriges Target sichtbar.
5. F/P aktive, belegte, unbelegte Slots statisch und per gezieltem Test gleichförmig.
6. `node --check static/app.js`, bestehende HCE-Tests, neue Method-/Style-Assertions, `git diff --check`.

## Constraints
Keine Änderungen an Kurvenspeicherung, Filterberechnung oder Export.

## Deployment

- Freigegebenes Deployment auf `paul@192.168.178.104:/home/paul/fxroute` durchgeführt.
- Übertragen: ausschließlich `static/app.js` und `static/style.css`.
- Remote-Backup: `/home/paul/fxroute/backups/hce003-deploy-20260801-0216/`.
- Bestehende Remote-Dateien wurden vor dem Transfer gesichert.
- Neuer Remote-Stand entspricht lokal exakt:
  - `static/app.js`: `301a394d10cfc71131d2401d3ca0f7f7e7500f53c7dddbad5305f4818ab5d1e2`
  - `static/style.css`: `904bb461616732d1baa1196997e65148e01feef5227ef46cb796fa6273721380`
- `systemctl --user restart fxroute.service` erfolgreich; Service `active`, MainPID `3021489`.
- API `/api/status`: HTTP 200, Version `0.8.0`, `error: null`.
- Remote-Marker für Slotrenderer, Assistenzwechsel und gemeinsame Slot-CSS vorhanden.
- Kein Commit, Push oder Release.
