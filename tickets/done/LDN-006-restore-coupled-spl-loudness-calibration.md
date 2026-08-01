# Ticket: LDN-006

## Project
FXRoute

## Goal
Die profilbezogene SPL-Kalibrierung als gekoppelten Loudness-Arbeitspunkt
wiederherstellen, ohne den globalen Kalibrierungsgain erneut zu aktivieren.

## Task
Den gespeicherten `requiredAdjustmentDb`-Wert ausschließlich bei aktiver
Loudness als `LSP volume = A - T` und gekoppelten Loudness-Output-Gain `+T`
anwenden. Bei deaktivierter Loudness müssen Compensation und Kalibrierungseinfluss
0 dB bleiben. Sichere Toggle-Reihenfolge, Persistenz und fünf fokussierte
Live-Nachweise auf `.104`.

## Expected Output
Separater Fix-Commit, gezielte Tests und Live-Verifikation ohne Push oder Release.

## Target Path
`/home/pbclaw/ai/projects/fxroute`

## Status
done

## Result

- `requiredAdjustmentDb` wirkt ausschließlich im aktivierten Loudness-Block:
  LSP-Volume `A - T`, Loudness-Output-Gain `+T`.
- Bei Loudness aus bleiben Loudness bypassed, Compensation und
  Limiter-Input-Gain bei 0 dB; System-Master bleibt auf dem Sliderwert.
- 60,1 dB / 46 % wurde live als `-43.13453 dB + 22.9 dB` verifiziert.
- Sichere Toggle-Reihenfolge zeigte beim Einschalten nur einen kurzen Abfall,
  niemals einen Aufwärtssprung.
- Normaler Playback-Peakcheck und Persistenz über Service-Neustart bestanden.
- Produktion restauriert: Loudness aus, 46 %, Wiedergabe gestoppt.
