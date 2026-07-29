# Ticket: LDN-004

## Project
FXRoute

## Goal
Loudness-, Measurement- und SPL-Calibration-Oberfläche konsistent und responsiv
aufräumen, ohne Funktionsänderungen.

## Task
Gemeinsame Control-Geometrie für FFT/Headroom/Bass Amount, vier eindeutige
Messaktionszeilen und ein inhaltsnahes SPL-Modal umsetzen. Desktop, Tablet und
Mobil prüfen, lokal committen und auf `.104` deployen.

## Input
Autoritatives Projekt `/home/pbclaw/ai/projects/fxroute`; bestehende IDs,
Aktionen, FFT-Werte und Funktionslogik bleiben unverändert.

## Expected Output
Gezielte Änderungen an HTML/CSS, drei Live-Screenshots, separater lokaler
Commit und Deployment auf `.104`.

## Target Path
`/home/pbclaw/ai/projects/fxroute`

## Status
done

## Result

- FFT-, Headroom- und Bass-Amount-Control auf gemeinsame Höhe, Abstände und
  vertikale Ausrichtung vereinheitlicht.
- Measurement-Menü in vier eindeutige Aktionszeilen gegliedert.
- SPL-Modal auf automatische Inhaltshöhe und responsive Maximalbreite begrenzt.
- Desktop 1440 px, Tablet 800 px und Mobil 390 px ohne horizontalen Overflow,
  abgeschnittene Hilfetexte oder abweichende Buttongeometrie geprüft.
- Bestehende IDs, FFT-Werte und JavaScript-Aktionspfade unverändert.
