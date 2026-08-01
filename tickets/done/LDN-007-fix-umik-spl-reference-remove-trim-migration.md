# Ticket: LDN-007

## Project
FXRoute

## Goal
Die UMIK-1-dBFS→SPL-Referenz korrigieren und den unveröffentlichten
`calibrationTrimDb`-Migrations-/Rewritepfad entfernen.

## Task
Nur die nachgewiesene UMIK-SPL-Referenz von 120 auf die Sens-Factor-konforme
124-dB-Basis korrigieren. Legacy-`trimDb`-/`calibrationTrimDb`-Konvertierung
und automatisches Umschreiben beim Laden entfernen. Bestehende
`requiredAdjustmentDb`-Persistenz und Loudness-Gainstruktur unverändert lassen.

## Expected Output
Fokussierter Commit und Live-Nachweis auf `.104`, anschließend Wiedergabe und
Pink Noise gestoppt.

## Target Path
`/home/pbclaw/ai/projects/fxroute`

## Status
done

## Result

- Reale Diagnose: Rohpegel −54,664 dBFS, C-/Cal-korrigiert −57,243 dBFS,
  Capture 100 % / 0 dB, Sens Factor 0,371 dB.
- UMIK/REW-Sens-Factor-Referenz von 120 auf 124 dB korrigiert.
- Automatische Live-Messung: 66,21 dB SPL gegenüber 65,7 dB REW.
- Legacy-Trim-Konvertierung, `calibrationTrimDb`-Kompatibilitätsfeld und
  automatisches Config-Rewrite beim Laden entfernt.
- Manueller Wert 65,7 dB akzeptiert; `requiredAdjustmentDb=17,3` blieb über
  Neustart erhalten.
- Loudness-Gainstruktur unverändert verifiziert; Produktion mit Loudness aus,
  Wiedergabe und Pink Noise gestoppt.
