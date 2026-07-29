# Ticket: LDN-003

## Project
FXRoute

## Goal
Das aktuell ausgewählte UMIK-1 mit passender serieller Cal-Datei sicher erkennen
und automatische SPL Calibration nur bei verifiziertem Capture-Gain ermöglichen.

## Task
PipeWire-/USB-Metadaten des ausgewählten Capture-Eingangs auswerten, Sens Factor
und SERNO parsen, Zuordnung fail-closed prüfen, automatische C-gewichtete
3-Sekunden-Mittelung ergänzen, gezielt testen und auf `.104` live verifizieren.

## Input
Autoritatives Projekt `/home/pbclaw/ai/projects/fxroute`; UMIK-1 USB 2752:0007,
Cal SERNO 7148364 / Sens Factor 0.371 dB. Keine Änderungen an Loudness-DSP,
Volume-Routing oder AutoSub. Kein Push, Release oder UI-Cleanup.

## Expected Output
Gezielte Code- und Regressionsteständerungen, Live-Nachweis auf `.104`,
separater lokaler Commit.

## Target Path
`/home/pbclaw/ai/projects/fxroute`

## Status
done

## Result

- Live-Metadaten des ausgewählten UMIK-1 über PipeWire/ALSA/USB ausgewertet.
- UMIK-1 USB 2752:0007, Cal SERNO 7148364 und Sens Factor 0.371 dB erkannt.
- Capture-Gain 100 % / 0 dB und interner 18-dB-Zustand verifiziert.
- Automatische C-gewichtete 3-s-Mittelung mit −23-LUFS-Pink-Noise live
  ausgeführt; Ergebnis wurde in das vorhandene SPL-Feld übernommen.
- UMC-Capture und falsche MM1-Cal-Datei wurden fail-closed abgelehnt.
- Produktionszustand nach der Messung restauriert.
