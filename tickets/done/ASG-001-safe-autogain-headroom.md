# Ticket: ASG-001

## Project
FXRoute AutoSub Safe Auto-Gain

## Goal
Den bestehenden sweepbasierten AutoSub-Auto-Gain-Bereich von ±2 dB auf
−6 bis +6 dB erweitern, ohne einen Kandidaten auszuführen, der an einem der
vier finalen Stage-Ausgänge mehr als −1 dBFS erzeugt.

## Task

- Ausschließlich den bestehenden sweepbasierten AutoSub-Pfad verwenden.
- Auto-Gain-Grenzen von −2/+2 dB auf −6/+6 dB erweitern.
- Vor jedem Gain-Sweep den bekannten Messsweep durch dieselbe
  Signalverarbeitung wie `fxroute_21_stage1` rechnen: Crossover,
  Kanalzuordnung/Mono-Summe, Gain, Delay und Polarität.
- Kandidaten verhindern oder sicher begrenzen, wenn einer der vier
  vorausberechneten Ausgangspeaks über −1 dBFS läge.
- In `fxroute_21_stage1` rücksetz- und auslesbare Peak-Erfassung für Output
  1–4 ergänzen.
- Vorausberechnete und tatsächlich gemessene Peaks im AutoSub-Ergebnis
  persistieren; relevante Abweichungen dürfen nicht unbemerkt bleiben.
- Den zweikanaligen EasyEffects-Peak-Monitor unverändert lassen.
- Keine Predictive-AutoSub-Arbeit, keine breite Architekturänderung und keine
  umfangreiche Testsuite.

## Input

- `/home/pbclaw/ai/projects/fxroute/main.py`
- `/home/pbclaw/ai/projects/fxroute/subwoofer_runtime.py`
- `/home/pbclaw/ai/projects/fxroute/pipewire_stage1/`
- vorhandene fokussierte AutoSub- und Native-Helper-Tests
- Nutzeranforderungen im aktuellen Auftrag

## Expected Output

- fokussierte Implementierung der sicheren ±6-dB-Auto-Gain-Suche
- vierkanalige Stage-Peak-Erfassung mit Reset/Read
- Ergebnisdiagnostik mit berechneten und gemessenen Peaks
- gezielte Tests für sicheren Kandidaten, blockierten/begrenzten +6-dB-
  Kandidaten, plausible vierkanalige Peakübereinstimmung sowie unveränderten
  Delay-/Polaritätsablauf
- dokumentierter Test- und Reviewstand

## Target Path
`/home/pbclaw/ai/projects/fxroute/`

## Notes

- Bestehende uncommittete Dateien nicht verwerfen oder überschreiben.
- Kein Deployment, Push oder Release.

## Status
done

## Result

- Sweepbasierter AutoGain-Bereich und Feedback-Gesamtbereich auf −6 bis
  +6 dB erweitert.
- Vor jedem AutoSub-Sweep wird das bekannte PCM mit den Stage1-LR24-Filtern,
  L/R- beziehungsweise `(L+R)*0.5`-Routing, Gain, abgeleiteten Delays und
  Polarität vorausberechnet. Kandidaten über −1 dBFS brechen den AutoSub-Lauf
  vor `start_measurement` sichtbar ab.
- `fxroute_21_stage1` erfasst Output-1–4-Peaks. Der Runtime-Control-Socket
  bestätigt Reset (`R`) und liefert Readback (`G`, JSON).
- Berechnete, gemessene und abweichende Peaks werden am Sweep und im final
  persistierten `auto_gain`-Ergebnis abgelegt. Abweichungen über 1 dB oder
  gemessene Überschreitung der −1-dBFS-Grenze brechen den gesamten Lauf ab.
- EasyEffects `peak_monitor.py` blieb unverändert.

## Focused verification

- `python3 scripts/test_auto_sub_gain_apply_revert.py`: 24 Tests, OK
  (±6 dB, sicherer Kandidat, blockierter +6-dB-Kandidat,
  vierkanaliger Peakvergleich, Gain-Feedback).
- `python3 scripts/test_auto_sub_polarity_check.py`: 5 Tests, OK.
- `python3 scripts/test_native_helper_alignment.py`: Delay-Impulse 0/3/6/30 ms,
  OK.
- `python3 scripts/test_subwoofer_runtime.py`: 10 Tests, OK.
- `python3 scripts/test_auto_sub_gain_calculation.py`: 7 Tests, OK.
- `python3 scripts/test_auto_sub_candidate_ledger.py`: 6 Tests, OK.
- `python3 scripts/test_auto_sub_main_target_anchor.py`: 9 Tests, OK.
- `python3 -m py_compile main.py subwoofer_runtime.py`: OK.
- `git diff --check`: OK.
- Nativer Neubau auf diesem Arbeits-Host nicht möglich: `libpipewire-0.3`-
  und `libspa-0.2`-pkg-config-Developmentmodule fehlen. Der vorhandene Build
  wurde nicht überschrieben.
- Kein Live-Hardware-Sweep und kein Deployment wurden in diesem Ticket
  ausgeführt; die Plausibilitätsprüfung erfolgte mit der identischen
  PCM-/DSP-Vorausberechnung und den fokussierten Helper-/Runtime-Tests.
- `scripts/test_auto_sub_main_reference_capture.py` erreicht einen bereits
  bestehenden/stalen Testfehler vor Ausführung der Candidate-Checks:
  Mock-Patch erwartet die in `main.py` nicht vorhandene Funktion
  `_prepare_subwoofer_runtime_for_measurement_start`.

## Live verification on `.104`

- Nativer Helper gegen PipeWire 1.6.7 / SPA 0.2 gebaut, Self-Test bestanden
  und zusammen mit `main.py`/`subwoofer_runtime.py` deployt.
- Der erste Live-Start deckte zwei eng begrenzte Integrationsfehler auf:
  Peak-Reset wartete vor Playback auf den noch inaktiven Audio-Callback und
  uvloop unterstützt `sock_sendto` hier nicht. Reset wird nun vorab queued,
  im ersten Callback vor der Peak-Erfassung angewandt und der Datagram-Pfad
  verwendet synchrones `sendto` plus threadbasiertes `select`/`recv`.
- Sicherer realer AutoSub-Lauf abgeschlossen. Normaler Delay-Scan und
  Polaritätscheck liefen; der Polaritätscheck behielt `invert` bei.
- Finaler Gain-Sweep: +4,472 dB, Endpegel +6,472 dB. Vorausberechnet/real:
  Main aktiv jeweils −1,940/−1,940 dBFS; Output 3/4 jeweils
  −1,502/−1,500 dBFS. Maximale Abweichung 0,002 dB, kein relevanter Mismatch,
  alle vier Ausgänge unter −1 dBFS. Werte sind im AutoSub-Ergebnis
  `auto_gain.stage_output_peaks.left/right` gespeichert.
- Ablehnungsfall: Testzustand +8,0 dB (= +6 dB gegenüber +2,0 dB) ergab für
  den ersten kombinierten Kandidaten vorausberechnet −0,00 dBFS. Der Lauf
  stoppte vor `start_measurement`; ausgeführt wurden nur die beiden
  sub-stummgeschalteten Main-Referenzen. Kein Clipping-Sweep.
- Fokusprüfungen nach den Live-Fixes: 24 AutoGain-Tests und 10 Runtime-Tests
  bestanden.
- Produktionszustand abschließend einmalig restauriert und verifiziert:
  2.2 LR24/80 Hz, Sub 1 +2,0 dB / 0,38 ms / normal, Sub 2 +2,0 dB /
  3,32 ms / invert, Helper aktiv und vier Links konfiguriert, Wiedergabe
  gestoppt.
- Nachweise: `outputs/ASG-001/raw/` und `outputs/ASG-001/validation/`.
