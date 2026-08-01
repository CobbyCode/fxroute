# Ticket: HCE-001

## Project
FXRoute

## Goal
Einen isolierten Custom-House-Curve-Editor ergänzen, der exakt den bestehenden Import-, Speicher- und Target-Dropdown-Pfad für normale House-Curve-Dateien verwendet.

## Task
Zuerst den aktuellen End-to-End-Pfad für importierte House-Curve-Dateien, Persistenz, Parsing/Interpolation und Target-Dropdown vollständig verifizieren. Anschließend im vorhandenen Measurement-Bereich einen Editor analog zum PEQ Assistant ergänzen: Target-Dropdown-Eintrag „Create Custom House Curve…“, acht umschaltbare Punkte P1–P8, je Punkt Frequency (Hz), Gain (dB) und Delete, dieselbe Umschalt-/Einzelzeilenbedienung, Sortierung nach Frequenz und logarithmische Interpolation über der Frequenz, editierbarer automatisch kollisionsfreier Namensvorschlag wie „Custom House Curve 1“ sowie Button „Create Target Curve“. Beim Speichern eine normale, zum bestehenden House-Curve-Import kompatible Datei am bereits verwendeten Speicherort erzeugen; danach bestehende Optionsliste aktualisieren, neue Kurve sofort auswählen und als Target nutzbar machen.

## Input
- `static/app.js`
- `static/index.html`
- bestehende relevante Styles nur falls für die vorhandene PEQ-Assistant-Bedienung erforderlich
- `main.py`, `measurement.py` und bestehende House-Curve-Tests/Parser
- vorhandener API-Pfad `/api/measurements/house-curves`
- bestehende importierte Dateien und Built-in-Targets bleiben kompatibel
- keine parallele Presetstruktur und kein neuer Speicherort
- kein Export-Button
- PEQ und Convolver unverändert
- fremde/uncommittete Worktree-Änderungen erhalten
- kein Commit, Deployment, Push oder Release

## Expected Output
- Eng begrenzter Custom-House-Curve-Editor mit P1–P8
- Persistenz als bestehend kompatible House-Curve-Datei im vorhandenen Speicherpfad
- Unmittelbare Anzeige, Auswahl und Target-Nutzung im bestehenden Dropdown
- Gezielte Tests: acht Punkte anlegen/bearbeiten/löschen; vollständig und korrekt frequenzsortiert gespeichert; kollisionsfreie Namensvorschläge; gespeicherte Kurve erscheint und funktioniert im vorhandenen Target-Pfad
- Syntax-/Diff-Prüfung nur soweit für diesen Schritt nötig

## Target Path
`/home/pbclaw/ai/projects/fxroute`

## Notes
Vor Implementierung die Diskrepanz prüfen, dass `main.py` und das Frontend House-Curve-Methoden referenzieren, diese im aktuellen `measurement.py` aber nicht per Textsuche auffindbar waren. Keine breite Wiederherstellung oder Architekturänderung; nur den bestätigten bestehenden Pfad verwenden und eine tatsächlich nötige Lücke eng dokumentieren/beheben.

Verifiziert: Commit `e577d66` entfernte `house_curves_dir`, Listing, Upload/Delete und Parser aus `measurement.py`, während `/api/measurements/house-curves`, der Frontend-Upload und das Target-Dropdown bestehen blieben. Deshalb wurden ausschließlich diese historisch vorhandenen Store-Funktionen am unveränderten Pfad `~/.local/state/fxroute/measurements/house_curves` wiederhergestellt; andere Änderungen des Release-Commits wurden nicht zurückgenommen.

Umgesetzt und gezielt geprüft:
- Target-Dropdown-Eintrag und Editor P1–P8 mit Frequency, Gain und Delete
- kollisionsfreier Namensvorschlag aus den vorhandenen House-Curve-Dateinamen
- frequenzsortierte normale Textdatei über den bestehenden Multipart-Endpunkt
- unmittelbare Übernahme der API-Liste und Auswahl als `house:<id>` im vorhandenen Target-Pfad
- bestehende logarithmische Zielkurveninterpolation unverändert weiterverwendet
- `python3 scripts/test_measurement_house_curve_store.py`
- `node scripts/test_custom_house_curve_editor.js`
- Python-/JavaScript-Syntaxchecks und `git diff --check`

## Status
done
