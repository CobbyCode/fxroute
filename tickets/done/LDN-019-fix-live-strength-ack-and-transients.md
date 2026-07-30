# Ticket: LDN-019

## Project
FXRoute

## Goal
Den real weiterhin auftretenden Strength-Save-Fehler und hörbaren positiven
Strength-Transienten auf `.104` nachweislich beseitigen.

## Task
Den unmittelbar erfassten realen UI-Request auswerten, den Control-Server-
Acknowledgement-Vergleich robust gegen die tatsächliche Wertdarstellung machen
und den kompletten Fehler-/Rollback-/Audiopfad des Strength-Wechsels absichern.
Anschließend echte UI-nahe Requests und akustisch relevante Runtime-
Hüllkurven auf `.104` prüfen.

## Input
- LDN-018 Fix `7c0a1289c27d67ad555ec8cd746ce850fa78fc75`
- Reale `.104`-Exception am 2026-07-30 20:02:15:
  `EasyEffects did not acknowledge loudness#0 volume=7.03094544423532
  (last response='7')`
- Der Property-Wert wurde vom Server offenbar numerisch angenommen, aber die
  textuelle Exaktheitsprüfung wertete die gerundete Antwort als Fehler.
- Um 20:02:12 wurden parallel zu einem Loudness/Strength-Request direkte
  EasyEffects→Hardware-Links beobachtet und vom Link-Watcher entfernt; Ursache
  und Relevanz für den Transienten müssen abgegrenzt werden.
- Bestehende AutoSub-Hunks und bestätigte LDN-/SPL-Fixes unangetastet lassen.

## Expected Output
- Nachgewiesene Ursache des Save-Fehlers und des weiterhin hörbaren Sprungs
- Eng begrenzter Fix mit Tests für gerundete numerische Acknowledgements,
  Fehler-Rollback und positive-Peak-Vermeidung
- Separater Commit, Deployment und reale `.104`-Prüfung
- Kein Preset-/Graph-/Peak-Reconfigure, kein Doppel-Save, kein Push/Release

## Target Path
- `/home/pbclaw/ai/projects/fxroute`
- `paul@192.168.178.104:/home/paul/fxroute`

## Notes
Aktuellen Nutzerzustand vor Änderungen vollständig erfassen und danach exakt
wiederherstellen. Main-/Sub-Links dürfen nicht beeinflusst werden.

## Diagnosis
- Der reale Save-Fehler war ein falsches negatives Acknowledgement:
  `set_property` hatte `volume=7.03094544423532` angewendet, der
  EasyEffects-Local-Server stellte denselben Pluginwert beim Lesen aber mit
  ganzzahliger Auflösung als `7` dar. Der bisher feste Vergleich mit
  `abs_tol=1e-6` wartete deshalb bis zum Timeout, startete einen unnötigen
  Rollback und ließ den UI-Request als HTTP 500 enden.
- Der UI-Pfad sendet pro debounced Strength-Änderung genau einen vollständigen
  `/api/easyeffects/extras`-POST. Das Backend erkannte die Änderung korrekt als
  Runtime-Übergang; kein zweiter Save-, Preset- oder Reload-Pfad war beteiligt.
- Der positive Sprung um 20:02:12 war vom Property-Acknowledgement getrennt:
  Der damalige Guard-Übergang endete bereits erfolgreich mit HTTP 200.
  Gleichzeitig existierten zwei direkte
  `ee_soe_output_level`→Hardware-Links (IDs 97/163), die den Stage1-Pfad
  parallel umgingen. Der Link-Watcher brauchte rund 1,1 s zum Entfernen.
  Ein solcher paralleler Main-Pfad kann den hörbaren positiven Sprung
  erklären. Beim späteren HTTP-500 um 20:02:15 waren die direkten Links
  bereits entfernt.
- Die Guard- und Rollback-Reihenfolge selbst blieb pegelmonoton: zuerst
  Output-Gain absenken, dann Arbeitsparameter/Volume ändern, anschließend
  Output-Gain hochrampen. Auch wenn eine Mutation angewendet wurde und erst
  deren Acknowledgement fehlschlägt, überschreitet die Summenhüllkurve den
  Ausgangspegel nicht.

## Implementation
- `easyeffects.py`: Numerische Property-Bestätigungen verwenden jetzt die aus
  der tatsächlichen Antwortdarstellung abgeleitete halbe Auflösung als
  Toleranz. Eine Antwort `7` bestätigt damit einen auf die nächste ganze
  Einheit dargestellten Wert wie `7.03094544423532`; fein aufgelöste Antworten
  behalten entsprechend enge Toleranzen. Boolesche Bestätigungen bleiben
  unverändert.
- `scripts/test_loudness_strength_runtime.py`: Test für den realen gerundeten
  Acknowledgement-Fall sowie einen angewendeten Volume-Write mit anschließend
  simuliertem Ack-Fehler, vollständigem Rollback und nachgewiesen ausbleibender
  positiver Summenhüllkurve ergänzt. Bestehende Reihenfolge-/Guard-Tests decken
  alle benachbarten Strength-Übergänge ab.
- Keine Änderung an Formel, Zielen, Strength-Stufen, UI, SPL Calibration,
  No-op-Pfad, Routing oder bestehenden AutoSub-Hunks.

## Validation
- Lokal bestanden:
  - `python3 scripts/test_loudness_strength_runtime.py`
  - `python3 scripts/test_loudness_live_regressions.py`
  - `python3 scripts/test_loudness_integration.py`
  - `python3 scripts/test_spl_calibration.py`
  - `python3 -m py_compile easyeffects.py main.py`
  - `git diff --check`
- Separater Code-/Test-Commit:
  `1f6b729fea62b02a890d603c742395e21686c562`
  (`Accept rounded EasyEffects acknowledgements`).
- Deployment ausschließlich von `easyeffects.py` nach `.104`; lokaler und
  deployter SHA-256:
  `3327f6b5ed33404e604ce7e1d66714105fd29d0b26fd21a481ac3a8b2bf3503f`.
  Kein Push oder Release.
- Reale UI-äquivalente Requests auf `.104`, jeweils HTTP 200:
  Loudness an/Min, Min→Full, Full→Med→Light→Min,
  Min→Light→Med→Full und Full→Min. Der vormals fehlerhafte Min-Wert wurde
  dabei real als `volume=7.030945...` angewendet und als `7` zurückgelesen.
  Alle neun Übergänge nutzten den Guard; das kanonische
  `volumeDb=-9.669054555764681` blieb unverändert.
- Der aktive Min-Zustand blieb über Service-Neustart exakt persistent.
  Ein identischer nachlaufender Restore-Request war ein No-op
  (`updated_presets=0`).
- Während der Übergänge: 0 HTTP-4xx/5xx, 0 Preset-Load/-Reload,
  0 Graph-/Peak-Reconfigure, 0 direkte Link-Watcher-Ereignisse. 240 schnelle
  `pw-link`-Samples enthielten keinen direkten EasyEffects→Hardware-Link.
  Main-/Sub-Routing blieb logisch unverändert.
- Live-Evidence:
  `/home/paul/fxroute/backups/ldn019-live-20260730-200751/`.
- Exakter Nutzerzustand wiederhergestellt:
  Groove Salad geladen und pausiert, Volume/System-Master 69, Auto Gain aus,
  Ziel −18, Loudness aus, Strength Min, FFT 8192,
  `volumeDb=-9.669054555764681`, unveränderte SPL Calibration. Extras,
  System-Master und `pw-link -l` sind gegenüber dem Vorher-Snapshot identisch;
  Runtime `bypass=true`, `volume=-9.66905455576468`, `outputGain=0`.
  FXRoute ist aktiv.

## Review
- Main-Review akzeptiert: Der reale gerundete Ack-Fall ist reproduziert und
  die Antwortauflösung wird nun korrekt berücksichtigt.
- Guard- und Rollback-Hüllkurven bleiben in den fokussierten Tests ohne
  positiven Peak; die übrigen Loudness-, Integrations- und SPL-Tests bestehen.
- Die deployte Prüfsumme stimmt; FXRoute auf `.104` ist aktiv.
- Der frühere hörbare Sprung korrelierte mit einem temporären parallelen
  EasyEffects→Hardware-Pfad. Dieser trat während der neun neuen Übergänge und
  240 Link-Samples nicht auf. Die abschließende akustische Bestätigung bleibt
  bewusst beim Nutzer.

## Status
done
