# HCE-002 — Custom-House-Curve-Editor gezielt nachpolieren

**Status:** done

## Ziel

Den bestehenden Custom-House-Curve-Editor gezielt um Slotfarben, neutralen Ausgangszustand und Reset-Verhalten ergänzen, ohne Speicher-, Import-, Export-, PEQ- oder andere Measurement-Pfade zu verändern.

## Anforderungen

1. Slots P1–P8 verwenden dieselbe Slotfarbe wie der zugehörige Diagrammpunkt; belegte Slots farbig, unbelegte grau/gestrichelt, aktiver Slot zusätzlich eindeutig hervorgehoben.
2. Ein neuer leerer Entwurf startet automatisch mit genau P1 = 20 Hz / 0 dB; P2–P8 bleiben unbelegt. Die konstante Fortführung ergibt sofort eine flache grüne 0-dB-Linie.
3. Bestehende Entwürfe bleiben beim bloßen Editor-/Moduswechsel erhalten.
4. Der vorhandene Reset-Button setzt im aktiven Custom-Modus genau auf P1 = 20 Hz / 0 dB und P2–P8 unbelegt zurück; der Editor bleibt geöffnet. Außerhalb des Custom-Modus bleibt Reset unverändert.

## Grenzen

- Nur bestehender Custom-House-Curve-Editor und direkt zugehörige UI-/Testpfade.
- Keine Änderungen an Speicherung, Import, Export, PEQ-Verhalten oder anderen Measurement-Pfaden.
- Kein Deployment, kein Remote-Neustart, kein Commit oder Push ohne separate Freigabe.

## Prüfung

- [x] Neuer Custom-Entwurf startet mit genau `P1 = 20 Hz / 0 dB`; die bestehende konstante Fortführung liefert bei 20 Hz, 1 kHz und 20 kHz jeweils 0 dB.
- [x] Slots verwenden feste `slot`-IDs: belegte P1–P8-Buttons und Diagrammpunkte greifen auf dieselbe Palette und denselben Slot zu; unbelegte Buttons bleiben durch die bestehende `is-empty`-Darstellung grau/gestrichelt.
- [x] Auswahl und Dragging behalten die Slot-ID; der aktive Slot wird zusätzlich über Hintergrund/Outline und Editor-Slotlabel hervorgehoben.
- [x] Der vorhandene Reset-Handler setzt im Custom-Modus auf genau P1 zurück und lässt `activeEditor = houseCurve` unverändert.
- [x] Bestehende Entwürfe bleiben beim erneuten Öffnen nach einem Moduswechsel erhalten.
- [x] `node --check static/app.js` bestanden.
- [x] `node scripts/test_custom_house_curve_editor.js` bestanden.
- [x] `node scripts/test_custom_house_curve_interaction.js` bestanden.
- [x] `node scripts/test_hce001_editor_state.js` bestanden.
- [x] `git diff --check` für die gezielten HCE-Dateien bestanden.

Deployment auf `.104` erfolgreich durchgeführt.

- Übertragen: ausschließlich `static/app.js` und `static/index.html`.
- Remote-Backup: `/home/paul/fxroute/backups/hce002-deploy-20260731-224238/`.
- Lokale/remote SHA-256 für `static/app.js`: `80b5083d641767be00a4d66f8397905bef1853c5d8e9da59bce19820bc7194aa`.
- Lokale/remote SHA-256 für `static/index.html`: `d1d218460c6b6de553b1d75f5de36f3d2055465e3836a5ab07238add42e64030`.
- User-Service nach `systemctl --user daemon-reload` und `systemctl --user restart fxroute.service`: `active/running`, MainPID `2737755`.
- API `http://127.0.0.1:8000/api/status`: HTTP 200, Version `0.8.0`, `error: null`.
- Remote-Marker für Resetfunktion, Slotbuttons und Custom-Panel vorhanden.
- Kein Commit und kein Push.

Der optionale Remote-`node --check` war nicht ausführbar, weil `.104` kein `node` installiert hat; der lokale `node --check static/app.js` ist erfolgreich.

## Lokaler Layout-Follow-up

- Namensfeld und `Create Target Curve` werden im Custom-Panel auf Desktop und Tablet über ein zweispaltiges Grid in einer Zeile gehalten.
- Das Namensfeld ist flexibel; die Aktion bleibt rechts mit sinnvoller fester Breite und vertikaler Ausrichtung.
- Erst unter dem bestehenden Breakpoint `max-width: 599px` wird auf eine Spalte gewechselt; Feld und Button sind dort jeweils voll breit.
- Ausschließlich `static/style.css` geändert; keine Funktions-, Text- oder JavaScript-Änderung.
- `node --check static/app.js`, `git diff --check -- static/style.css` und statische Responsive-CSS-Assertions bestanden.
- Interaktiver Browser-Check lokal wegen Browser-Gateway-Timeout nicht ausführbar.
Deployment des Layout-Follow-ups auf `.104` erfolgreich durchgeführt.

- Übertragen: ausschließlich `static/style.css`.
- Remote-Backup: `/home/paul/fxroute/backups/hce002-layout-deploy-20260731-225819/style.css`.
- Lokale/remote SHA-256 für `static/style.css`: `276ff0de0a8d6734cd9674aa4a1307595fc92b11989eb876d3553f5c3c516ffc`.
- User-Service nach `systemctl --user daemon-reload` und `systemctl --user restart fxroute.service`: `active/running`, MainPID `2798409`.
- API nach dem kurzen Startfenster: HTTP 200, Version `0.8.0`, `error: null`.
- Remote-Layoutmarker für Desktop-/Tablet-Grid und Mobile-Einspaltenlayout vorhanden.
- Kein Commit und kein Push.

Der interaktive Browser-Check war lokal wegen eines Browser-Gateway-Timeouts nicht ausführbar; statische Responsive-CSS-Assertions sowie Diff-Prüfung waren erfolgreich.

## Lokaler PEQ-Slot-Style-Follow-up

- PEQ-F1–F12 verwenden weiterhin dieselbe gemeinsame Chip-Komponente wie P1–P8: gleiche Höhe, Rundung, Grundumrandung und Abstände.
- Belegte PEQ-Slots behalten ihre bestehende Filterpalette und zeigen die jeweilige Filterfarbe sichtbar als Umrandung und getönte Füllung.
- Aktive PEQ-Slots erhalten analog zu House Curve die stärkere Füllung und den zusätzlichen äußeren Hervhebungsring.
- Unbelegte Slots bleiben dunkel/grau und gestrichelt.
- PEQ-Slots erhalten eine einheitliche Mindestbreite von `3.25rem`, damit F10–F12 nicht abweichend dimensioniert werden.
- Responsive Umbruch- und Abstandsregeln bleiben unverändert.
- Ausschließlich die Slotdarstellung angepasst; keine Änderung an Palette, Filterlogik oder House-Curve-Stil.
- `node --check static/app.js`, die drei HCE-Tests, statische PEQ-Visual-Assertions und `git diff --check` bestanden.
- Der bestehende `test_measurement_peq_eight_filters.js` bleibt wegen der bereits vorhandenen Vier-Filter-Begrenzung rot; diese Logik wurde nicht verändert.
Deployment des PEQ-Slot-Style-Follow-ups auf `.104` erfolgreich durchgeführt.

- Übertragen: ausschließlich `static/app.js` und `static/style.css`.
- Remote-Backup: `/home/paul/fxroute/backups/hce002-peq-slot-style-deploy-20260801-0105/`.
- Lokale/remote SHA-256 für `static/app.js`: `9bf7261ad5b1497e082654b0ca585194cbca2a064c515a6f2e84a49721fbdfb2`.
- Lokale/remote SHA-256 für `static/style.css`: `f53a7d560c848ab7e383e816e084d48eba60515ef609aa81ea36b4a9519b1f3a`.
- User-Service nach `systemctl --user restart fxroute.service`: `active/running`, MainPID `2814176`.
- API `http://127.0.0.1:8000/api/status`: HTTP 200, Systemversion `0.8.0`, `error: null`.
- Nach dem Neustart war der API-Port kurzzeitig noch nicht bereit; die abschließende Prüfung war erfolgreich.
- Kein Commit und kein Push.
