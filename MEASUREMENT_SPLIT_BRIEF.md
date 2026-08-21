# Briefing: Measurement-Block-Split (Review-Punkt 7)

Handoff für eine neue Session. Stand: 2026-08-21, app.js hat 14.911 Zeilen.
Dieser Plan ist nicht committet – nach Abschluss löschen oder als Docs aufnehmen.

## Ziel

Den Measurement-Block aus `static/app.js` herauslösen und in wartbare,
testbare Module überführen – **streng verhaltenserhaltend**, mit demselben
Muster, das im Repo schon etabliert ist (`window.FXRoute*` + Dependency-
Injection via `init(api)`, siehe `streaming.js`, `radio.js`, und die reinen
Mathe-Module `measurement_dsp.js` / `hybrid_measurement.js` als UMD).

Nebenziele (fallen teilweise automatisch ab):
- `renderMeasurementPanel` baut aktuell bei **jeder** Eingabeänderung das
  gesamte Panel-DOM inkl. Listener neu auf → gezielte Updates statt Vollneubau.
- 700-Zeilen-Funktionen auflösen.
- Toter Code entfernen (siehe "Bekannte Leichen" unten).

## Umfang & aktuelle Anker (grep-Anker nutzen, Zeilen verschieben sich)

Der Block erstreckt sich grob von ~6700 bis ~13100 (**~6.400 Zeilen**):
Graph, PEQ, Convolver, Auto-Sub, Hybrid-Wizard, SPL-Kalibrierung-Glue.

| Funktion | Zeile (aktuell) | Größe |
|---|---|---|
| `startMeasurementWindowHeartbeat` | 9469 | Heartbeat 10 s, Backend-Session-Lock |
| `drawMeasurementGraph` | 9645 | Canvas-Rendering, rAF-koalesziert |
| `handleAutoSubResult` | 10230 | 187 Zeilen |
| `renderMeasurementPanelDefensively` | 10418 | Recovery-/Fehlpfad |
| `renderMeasurementPanel` | 11269 | **704 Zeilen**, Hot Path |
| `setupMeasurementActions` | 11974 | 296 Zeilen, Listener-Bindung |

Dazu: `state.measurement.*` (zentraler State), diverse loose Globals
(Timer/Guards, u. a. `measurementWindowHeartbeatTimer`), DOM-Cache
`elements` (einmalig beim Start befüllt).

## Bekannte Leichen (mit weg)

- `index.html` ~834–841: `measurement-channel-select`-Gruppe ist permanent
  `hidden`, wird aber noch sync'd/bound in app.js (Suche:
  `measurement-channel-select`). UI-Ersatz ist die Chip-Reihe
  (`#measurement-channel-chip-row`). Beim Split: Wiring löschen.

## Phasenplan (jede Phase = eigene Session, eigener Commit)

**Phase 0 — Bestandsaufnahme (schnelles Modell reicht)**
- Inventar: alle Funktionen im Block mit Zeilenbereich; für jede Funktion:
  welche `state.measurement.*`-Felder liest/schreibt sie, welche
  `elements.*`/getElementById nutzt sie, welche Listener bindet sie.
  Ergebnis als Tabelle im Commit/PR-Text ablegen.
- Baseline: `scripts/run_tests.sh` + `python3 scripts/check_ui_walkthrough.py`
  (200 Checks inkl. Screenshots in /tmp/fxroute-ui-shots) vor Phase 1
  sichern und Screenshots aufbewahren.

**Phase 1 — Blätter extrahieren (schnelles Modell OK)**
- Pure Helpers (Normalisierung, Formatierung, PEQ-State-Defaults, …) in ein
  UMD-Modul `static/measurement_ui.js` (Vorlage: Kopf von
  `measurement_dsp.js`), injiziert statt global. Kein Verhalten ändern.

**Phase 2 — Graph-Modul (gutes Modell empfohlen)**
- `drawMeasurementGraph` + Trace-Aufbau + rAF-Koaleszenz in eigenes Modul.
  Canvas-Element wird übergeben, keine globalen Zugriffe.

**Phase 3 — renderMeasurementPanel splitten (gutes Modell, Kernstück)**
- Zuerst: statisches Panel-Skelett einmalig rendern (HTML steht bereits fast
  vollständig in index.html!), dann **gezielte Updates** pro Feld/Gruppe.
- Listener: einmalig via Delegation oder beim Skelett binden — NIEMALS bei
  jedem Update neu binden (aktuelles Verhalten macht das; das ist der Bug).
- `renderMeasurementPanelDefensively` als Fallback-Vollneubau behalten.
- Regression: jede Panel-Eingabe darf nur ihre eigene Region neu zeichnen;
  UI-Walkthrough-Screenshots müssen vor/nach gleich bleiben.

**Phase 4 — Wizard/Auto-Sub-Flows (gutes Modell)**
- `handleAutoSubResult`, Hybrid-Wizard-Schritte in Module; Backend-Interaktion
  bleibt über injiziertes `api`-Objekt (Muster streaming.js:88–98).

**Phase 5 — Aufräumen**
- Tote channel-select-Wiring löschen, Konsolenreste entfernen,
  `app.js`-Version bumpen, Doku/CHANGELOG falls gepflegt.

## Verifikationsprotokoll (JEDE Phase)

1. `node --check static/*.js`
2. Fokus: `scripts/test_measurement_*` (Python) + Node-Mess-Tests
3. `python3 scripts/check_ui_walkthrough.py` (Screenshots vergleichen!)
4. `scripts/run_tests.sh` als Final Gate (lokal 10 native Skips = normal)
5. Commit (ein Phase = ein Commit); Deploy + `.104`-Smoke erst wenn Phase
   komplett

## Risiken / Stolpersteine

- **Listener-Doppelbindung** bei gezielten Updates (siehe Phase 3).
- **Globale Kopplung**: `state.dsp`/`state.measurement` werden verstreut
  mutiert (z. B. WS-Handler baut `state.dsp` wholesale um, ~1183). Nicht
  "verbessern" während des Moves — erst move, dann aufräumen.
- **Backend-Session-Locks**: Measurement läuft unter
  `measurement_sr_session`-Locks (Sample-Rate-Policy eingefroren,
  HTTP 423 auf andere Operationen). Heartbeat und Start-/Ende-Sequenz
  dürfen timingseitig NICHT verändert werden.
- **Element-Cache**: `elements` wird einmalig gebaut; dynamische Panel-
  Knoten werden in Funktionen teils direkt per getElementById gesucht —
  beim Move konsistent machen (Übergabe als Parameter).
- **UMD-Module sind Node-testbar** — dafür bauen, nicht dagegen.

## Modell-/Session-Empfehlung

- Phase 0 + 1: schnelles Modell genügt (mechanisch).
- Phase 2: mittel.
- Phase 3: **starkes Modell mit großem Kontext** empfohlen — 704-Zeilen-
  Funktion, Listener-Lebenszyklen und State-Kopplung sind der heikelste Teil.
- Phase 4: starkes Modell.
- Pro Phase eine Session; dieses Briefing + "Arbeite Phase N gemäß
  MEASUREMENT_SPLIT_BRIEF.md" reicht als Prompt.
