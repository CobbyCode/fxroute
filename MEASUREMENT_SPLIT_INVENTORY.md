# Measurement-Block Inventar (Phase 0)

Handoff-Artefakt zu `MEASUREMENT_SPLIT_BRIEF.md` (Review-Punkt 7).
Stand: 2026-08-21, `static/app.js` @ 14.911 Zeilen (HEAD f25ed7b).

## Kernaussagen

- **Blockgrenzen (korrigiert gegenüber dem Briefing):** Der Block ist nicht
  durchgehend. Er besteht aus der SPL-Kalibrierung (6434–6637) und dem
  Hauptblock (6737–12270); dazwischen liegen Download-Polling-Helper
  (6638–6736, nicht Measurement). Ab 12271 beginnt der Effects-PEQ-Bereich
  (`fetchEffects` … `importRewPeqPreset`) — gehört **nicht** zum Split.
  Insgesamt ~5.740 Zeilen, 275 Funktionen.
- **Hot Spots:** `renderMeasurementPanel` (11269–11973, 705 Zeilen) baut das
  Panel-DOM bei jedem Aufruf neu und bindet dabei ~18 Listener neu
  (der im Briefing beschriebene Bug). `setupMeasurementActions`
  (11974–12270, 297 Zeilen) bindet ~60 Listener — einmalig beim Boot, dort
  ist das Muster korrekt. `handleAutoSubResult` 188 Zeilen,
  `drawMeasurementGraph` 120 Zeilen (rAF-koalesziert über
  `scheduleMeasurementGraphRender`, 9550–9563).
- **Listener-Bindung gesamt:** 82 `addEventListener`-Stellen im Block, nur
  3 Funktionen binden direkt: `setupMeasurementActions`,
  `renderMeasurementPanel`, `setupHybridMeasurementWizard`.
- **Tote Wiring bestätigt:** `#measurement-channel-select` liegt in
  `index.html` (839–846) in einer permanent `hidden`-Gruppe; app.js sync'd
  es in `renderMeasurementPanel` (11357–11359), bindet einen change-Listener
  in `setupMeasurementActions` (12011–12013) und resettet es bei Clear
  (12039). Element-Cache-Eintrag: Zeile 438. Phase-5-Entfernung.
- **Externe Kopplungen (beim Move beachten):**
  - `flushSubwooferSettingsBeforeMeasurement` (1643) ruft in den
    Measurement-Flow hinein (Subwoofer-Settings vor Sweep).
  - WS-Handler baut `state.dsp` wholesale um (~1181–1190) und triggert
    `renderEffects()`, nicht das Measurement-Panel.
  - Element-Cache `elements`: Measurement-Einträge ~427–470 und
    SPL ~624–631; dynamische Panel-Knoten werden teils direkt per
    getElementById gesucht (siehe Spalte in Tabelle B).
  - Loose Globals (Timer/Guards) stehen im Dateikopf, Zeile 317–326
    (u. a. `measurementWindowHeartbeatTimer`).
- **Heartbeat/Session-Lock:** `sendMeasurementWindowHeartbeat`/
  `startMeasurementWindowHeartbeat`/`stopMeasurementWindowHeartbeat`
  (9457–9488) — timingseitig NICHT ändern (`measurement_sr_session`-Locks).

## Phasen-Fortschritt

**Phase 2** (2026-08-21): `static/measurement_graph.js` (IIFE, DI via
`init()`, Muster streaming.js) angelegt. Verschoben: `drawMeasurementGraph`,
`scheduleMeasurementGraphRender(+ForResize)` (rAF-Koaleszenz, Flag ist jetzt
modulprivat), `buildMeasurementGraphEntry`, `getGraphMeasurementEntries`.
Canvas/Panel kommen über injizierte Getter (`getCanvas`/`getPanel`),
State-Leser und Overlay-Painter werden als hoisted Funktionsreferenzen
injiziert; Modul nutzt `FXRouteMeasurementUI`/`-Dsp` direkt. app.js hält
gleichnamige Wrapper; `measurementResizeScheduled`-Global entfallen;
ResizeObserver-Wiring bleibt in app.js. index.html: Script-Tag +
app.js v=0.9.56. Verifikation wie Phase 1: Fokus-Tests grün, Walkthrough
200 Checks grün ohne strukturelle Screenshot-Diffs (>2000 px), Final Gate
200/0/10.

**Phase 0** (2026-08-21, Commit 1401fc0): dieses Inventar + Baseline.

**Phase 1** (2026-08-21): `static/measurement_ui.js` (UMD) angelegt. 66 reine
Leaf-Funktionen + 15 Konstanten aus app.js dorthin verschoben; app.js hält
gleichnamige Delegierungs-Wrapper (`return MeasurementUI.x(…)`), Konstanten-
Aliase nur wo bleibender Code sie nutzt (13). Nicht verschoben (bewusst):
`formatMeasurementIrCompactRange` (destrukturierter Param) und
`drawMeasurementIrGraph` (Canvas → Phase 2); extraction-getestete Helfer
(`splCalibrationModeLabel`, `describeMeasurementScope`,
`measurementModeNoteText`, `serializeCustomHouseCurvePoints`) bleiben bis auf
Weiteres in app.js. 8 Node-Tests bekamen `MeasurementUI` in den VM-Kontext;
`check_fir_regression.js` prüft `measurementConvolverAlignedPhaseModes`
jetzt gegen das Modul. Verifikation: node --check ok, Fokus-Tests grün,
Walkthrough 200 Checks grün — Screenshot-Pixel-Diffs ≤688 px ausschließlich
in der Ecke Artwork/Avatar und innerhalb der Laufzeit-Streuung (2
unveränderte Läufe weichen selbst in 84/169 Shots ab; strukturelle Diffs =
0). Final Gate: 200 passed / 0 failed / 10 native Skips.
Lektion: Walkthrough-"PASS" allein fängt Boot-Crashes nicht — der erste
Modulstand exportierte die Konstanten nicht (Boot-TDZ-Crash im
Measurement-Panel, nur per Screenshot-Diff sichtbar).

## Baseline (vor Phase 1, 2026-08-21)


- `node --check static/*.js`: alle ok.
- `scripts/run_tests.sh`: **200 passed, 0 failed, 10 skipped**
  (10 native C/LV2-Skips = erwartungsgemäß, fehlende Build-Abhängigkeiten
  lokal; laut AGENTS.md normal). Darin enthalten:
  `check_ui_walkthrough.py` (PASS) sowie die Measurement-Fokus-Tests
  (`test_measurement_*`, `test_spl_calibration_frontend.js`,
  `test_hybrid_measurement.js`, `check_measurement_merge.py`,
  `check_measurement_repeat.py`).
- UI-Walkthrough-Screenshots: 169 Stück in `/tmp/fxroute-ui-shots`
  (Stand 08:47), archiviert als Baseline in
  `/tmp/opencode/fxroute-ui-shots-phase0-baseline/` (14 MB) — Vergleichsbasis
  für Phase 3 („vor/nach gleich").


## A) Teilbereiche

| Teilbereich | Zeilen |
|---|---|
| SPL-Kalibrierung-Glue | 6434–6637 |
| State-Normalisierung & Helpers | 6737–6884 |
| PEQ-State & Editing | 6885–6930 |
| Convolver-State/-Defaults/-Curves | 6931–7157 |
| PEQ-Editing fortgesetzt | 7158–7458 |
| Convolver-Quellen/Analyse/Build | 7459–8206 |
| Graph-Geometrie/Hover/Handles | 8207–8620 |
| IR-Graph | 8621–8724 |
| Graph-Pointer & Entries | 8725–8923 |
| Mikrofon-Kalibrierung | 8924–9038 |
| House-Curve | 9039–9265 |
| Setup-Settings/Inputs/Fetch | 9266–9452 |
| Panel open/Heartbeat/Input-Auswahl | 9453–9558 |
| Graph-Render (Scales/Overlays/draw) | 9559–9764 |
| Summary/Timing/Quality | 9765–9939 |
| Job-Status-Helpers | 9940–10025 |
| Auto-Sub | 10026–10417 |
| Defensive Render & Start-Flows | 10418–10520 |
| Hybrid-Wizard | 10521–10882 |
| Measurement-Jobs (start/poll/save/delete/merge) | 10883–11268 |
| renderMeasurementPanel | 11269–11973 |
| setupMeasurementActions | 11974–12270 |

## A) `state.measurement.*` Feld-Inventar

| Feld | gelesen | geschrieben |
|---|---|---|
| `activeEditor` | x | x |
| `activeJobId` | x | x |
| `activeMeasurementKind` |  | x |
| `assistMode` | x | x |
| `autoSubInFlight` | x |  |
| `autoSubJobId` | x |  |
| `autoSubMeasurements` | x | x |
| `calibrationDeleting` |  | x |
| `calibrationExporting` |  | x |
| `calibrationFilename` | x | x |
| `calibrationOptions` | x | x |
| `calibrationUpdating` |  | x |
| `captureAvailable` |  | x |
| `convolverAssistant` | x | x |
| `currentMeasurement` | x | x |
| `currentMeasurementName` | x | x |
| `currentMeasurementSaved` |  | x |
| `displaySmoothing` | x | x |
| `hostCaptureAvailable` | x | x |
| `houseCurveDeleting` |  | x |
| `houseCurveExporting` |  | x |
| `houseCurveFilename` |  | x |
| `houseCurveOptions` | x | x |
| `houseCurveUpdating` |  | x |
| `hybridWizard` |  | x |
| `inputs` |  | x |
| `inputsLoading` |  | x |
| `loading` |  | x |
| `measurementSampleRate` | x | x |
| `measurementView` | x | x |
| `measurements` | x | x |
| `modeNote` |  | x |
| `open` |  | x |
| `pendingRepeatMeasurements` | x | x |
| `peqAssistant` |  | x |
| `repeatJobActive` | x | x |
| `reviewVisibilityById` | x | x |
| `saveInFlight` | x | x |
| `savedGroupOpen` |  | x |
| `selectedCalibrationRef` | x | x |
| `selectedChannel` | x | x |
| `selectedInputConfigured` |  | x |
| `selectedInputId` | x | x |
| `selectedInputKey` | x | x |
| `selectedInputLegacyId` |  | x |
| `selectedInputUnavailable` |  | x |
| `selectedMicInputChannel` | x | x |
| `selectedReferenceInputChannel` | x | x |
| `setupOpen` |  | x |
| `startInFlight` | x | x |
| `statusText` |  | x |
| `storage` |  | x |
| `visibilityById` | x | x |

## B) Funktionen

| Funktion | Zeilen | LOC | Teilbereich | liest state.measurement | schreibt state.measurement | elements.* | getElementById | addEventListener | removeEventListener |
|---|---|---|---|---|---|---|---|---|---|
| `splCalibrationModeLabel` | 6434–6439 | 6 | SPL-Kalibrierung-Glue | — | — | — | — | — | — |
| `resetSplCalibrationNoiseButton` | 6440–6445 | 6 | SPL-Kalibrierung-Glue | — | — | splCalibrationNoise | — | — | — |
| `runSplCalibrationNoiseCountdown` | 6446–6454 | 9 | SPL-Kalibrierung-Glue | — | — | splCalibrationNoise | — | — | — |
| `stopSplCalibrationOperation` | 6455–6475 | 21 | SPL-Kalibrierung-Glue | — | — | splCalibrationNoise, splCalibrationStatus | — | — | — |
| `openSplCalibration` | 6476–6496 | 21 | SPL-Kalibrierung-Glue | — | — | splCalibrationAutoStatus, splCalibrationNoise, splCalibrationPanel, splCalibrationStatus | — | — | — |
| `closeSplCalibration` | 6497–6502 | 6 | SPL-Kalibrierung-Glue | — | — | splCalibrationPanel | — | — | — |
| `toggleSplCalibrationNoise` | 6503–6569 | 67 | SPL-Kalibrierung-Glue | — | — | splCalibrationMeasured, splCalibrationNoise, splCalibrationStatus | — | — | — |
| `saveSplCalibration` | 6570–6597 | 28 | SPL-Kalibrierung-Glue | — | — | splCalibrationMeasured, splCalibrationNoise, splCalibrationStatus | — | — | — |
| `startDownload` | 6598–6637 | 40 | SPL-Kalibrierung-Glue | — | — | downloadUrl, downloadUrlHint | — | — | — |
| `normalizeMeasurementTrace` | 6737–6749 | 13 | State-Normalisierung & Helpers | — | — | — | — | — | — |
| `normalizeMeasurementEntry` | 6750–6772 | 23 | State-Normalisierung & Helpers | — | — | — | — | — | — |
| `normalizeMeasurementVisibility` | 6773–6784 | 12 | State-Normalisierung & Helpers | — | — | — | — | — | — |
| `formatMeasurementDate` | 6785–6791 | 7 | State-Normalisierung & Helpers | — | — | — | — | — | — |
| `normalizeMeasurementReviewVisibility` | 6792–6799 | 8 | State-Normalisierung & Helpers | — | — | — | — | — | — |
| `getVisibleMeasurementEntries` | 6800–6804 | 5 | State-Normalisierung & Helpers | currentMeasurement, measurements, visibilityById | — | — | — | — | — |
| `getCurrentMeasurementEntry` | 6805–6808 | 4 | State-Normalisierung & Helpers | currentMeasurement | — | — | — | — | — |
| `getCurrentMeasurementEntries` | 6809–6819 | 11 | State-Normalisierung & Helpers | autoSubMeasurements, pendingRepeatMeasurements | — | — | — | — | — |
| `measurementReviewVisible` | 6820–6823 | 4 | State-Normalisierung & Helpers | reviewVisibilityById | — | — | — | — | — |
| `getMeasurementDisplayTraces` | 6824–6830 | 7 | State-Normalisierung & Helpers | — | — | — | — | — | — |
| `measurementSmoothingHalfWindowOctaves` | 6831–6834 | 4 | State-Normalisierung & Helpers | — | — | — | — | — | — |
| `smoothMeasurementTracePoints` | 6835–6838 | 4 | State-Normalisierung & Helpers | — | — | — | — | — | — |
| `trackFileUrl` | 6839–6842 | 4 | State-Normalisierung & Helpers | — | — | — | — | — | — |
| `measurementFileUrl` | 6843–6846 | 4 | State-Normalisierung & Helpers | — | — | — | — | — | — |
| `presetFileUrl` | 6847–6873 | 27 | State-Normalisierung & Helpers | — | — | — | — | — | — |
| `getVisibleMeasurementColorById` | 6874–6884 | 11 | State-Normalisierung & Helpers | — | — | — | — | — | — |
| `getDefaultMeasurementPeqFilter` | 6885–6895 | 11 | PEQ-State & Editing | — | — | — | — | — | — |
| `getDefaultMeasurementPeqState` | 6896–6905 | 10 | PEQ-State & Editing | — | — | — | — | — | — |
| `ensureMeasurementPeqState` | 6906–6921 | 16 | PEQ-State & Editing | — | peqAssistant | — | — | — | — |
| `getMeasurementPeqFilters` | 6922–6925 | 4 | PEQ-State & Editing | — | — | — | — | — | — |
| `getMeasurementPeqActiveFilter` | 6926–6930 | 5 | PEQ-State & Editing | — | — | — | — | — | — |
| `clampMeasurementConvolverFrequency` | 6931–6934 | 4 | Convolver-State/-Defaults/-Curves | — | — | — | — | — | — |
| `getDefaultMeasurementConvolverState` | 6935–6953 | 19 | Convolver-State/-Defaults/-Curves | — | — | — | — | — | — |
| `ensureMeasurementConvolverState` | 6954–6992 | 39 | Convolver-State/-Defaults/-Curves | — | convolverAssistant | — | — | — | — |
| `getMeasurementActiveEditor` | 6993–6997 | 5 | Convolver-State/-Defaults/-Curves | activeEditor | — | — | — | — | — |
| `getMeasurementRestorableTargetCurve` | 6998–7005 | 8 | Convolver-State/-Defaults/-Curves | — | — | — | — | — | — |
| `setMeasurementActiveEditor` | 7006–7022 | 17 | Convolver-State/-Defaults/-Curves | convolverAssistant | activeEditor | — | — | — | — |
| `setMeasurementAssistMode` | 7023–7036 | 14 | Convolver-State/-Defaults/-Curves | — | assistMode | — | — | — | — |
| `getMeasurementConvolverCurveOptions` | 7037–7046 | 10 | Convolver-State/-Defaults/-Curves | houseCurveOptions | — | — | — | — | — |
| `getMeasurementConvolverCurve` | 7047–7050 | 4 | Convolver-State/-Defaults/-Curves | — | — | — | — | — | — |
| `getMeasurementSlotChipStyle` | 7051–7055 | 5 | Convolver-State/-Defaults/-Curves | — | — | — | — | — | — |
| `renderMeasurementSlotChip` | 7056–7060 | 5 | Convolver-State/-Defaults/-Curves | — | — | — | — | — | — |
| `getAutoSubTargetCurveSnapshot` | 7061–7073 | 13 | Convolver-State/-Defaults/-Curves | — | — | — | — | — | — |
| `getMeasurementConvolverCurveDb` | 7074–7079 | 6 | Convolver-State/-Defaults/-Curves | — | — | — | — | — | — |
| `getMeasurementHouseCurvePreviewPoints` | 7080–7086 | 7 | Convolver-State/-Defaults/-Curves | — | — | — | — | — | — |
| `getMeasurementTargetCurvePreview` | 7087–7093 | 7 | Convolver-State/-Defaults/-Curves | — | — | — | — | — | — |
| `getMeasurementConvolverDraftPhaseMode` | 7094–7098 | 5 | Convolver-State/-Defaults/-Curves | — | — | — | — | — | — |
| `getMeasurementConvolverDrafts` | 7099–7102 | 4 | Convolver-State/-Defaults/-Curves | — | — | — | — | — | — |
| `getMeasurementConvolverDraftPhaseMismatch` | 7103–7111 | 9 | Convolver-State/-Defaults/-Curves | — | — | — | — | — | — |
| `clearMeasurementConvolverDraftForPhaseChange` | 7112–7127 | 16 | Convolver-State/-Defaults/-Curves | — | — | — | — | — | — |
| `updateMeasurementConvolverField` | 7128–7157 | 30 | Convolver-State/-Defaults/-Curves | — | measurementSampleRate | — | — | — | — |
| `focusMeasurementPeqPanelContext` | 7158–7162 | 5 | PEQ-Editing fortgesetzt | — | — | measurementPeqPanel | — | — | — |
| `isEditableMeasurementPeqTarget` | 7163–7167 | 5 | PEQ-Editing fortgesetzt | — | — | — | — | — | — |
| `handleMeasurementPeqNumberInputArrowKey` | 7168–7181 | 14 | PEQ-Editing fortgesetzt | — | — | — | — | — | — |
| `syncMeasurementPeqQInput` | 7182–7186 | 5 | PEQ-Editing fortgesetzt | — | — | measurementPeqEditor | — | — | — |
| `stepActiveMeasurementPeqQ` | 7187–7196 | 10 | PEQ-Editing fortgesetzt | — | — | — | — | — | — |
| `handleMeasurementPeqGraphWheel` | 7197–7209 | 13 | PEQ-Editing fortgesetzt | — | — | measurementPanel, measurementPeqPanel | — | — | — |
| `selectMeasurementPeqFilter` | 7210–7214 | 5 | PEQ-Editing fortgesetzt | — | — | — | — | — | — |
| `clampMeasurementPeqFrequency` | 7215–7218 | 4 | PEQ-Editing fortgesetzt | — | — | — | — | — | — |
| `clampMeasurementPeqGain` | 7219–7222 | 4 | PEQ-Editing fortgesetzt | — | — | — | — | — | — |
| `clampMeasurementPeqQ` | 7223–7226 | 4 | PEQ-Editing fortgesetzt | — | — | — | — | — | — |
| `measurementXToFrequency` | 7227–7230 | 4 | PEQ-Editing fortgesetzt | — | — | — | — | — | — |
| `measurementYToDb` | 7231–7234 | 4 | PEQ-Editing fortgesetzt | — | — | — | — | — | — |
| `addMeasurementPeqFilter` | 7235–7255 | 21 | PEQ-Editing fortgesetzt | — | — | — | — | — | — |
| `createMeasurementPeqFilterFromPoint` | 7256–7262 | 7 | PEQ-Editing fortgesetzt | — | — | — | — | — | — |
| `updateMeasurementPeqFilter` | 7263–7271 | 9 | PEQ-Editing fortgesetzt | — | — | — | — | — | — |
| `stepMeasurementPeqQ` | 7272–7279 | 8 | PEQ-Editing fortgesetzt | — | — | — | — | — | — |
| `stepMeasurementPeqGain` | 7280–7287 | 8 | PEQ-Editing fortgesetzt | — | — | — | — | — | — |
| `stepMeasurementPeqFrequency` | 7288–7295 | 8 | PEQ-Editing fortgesetzt | — | — | — | — | — | — |
| `deleteMeasurementPeqFilter` | 7296–7303 | 8 | PEQ-Editing fortgesetzt | — | — | — | — | — | — |
| `resetMeasurementGraph` | 7304–7327 | 24 | PEQ-Editing fortgesetzt | — | autoSubMeasurements, currentMeasurement, currentMeasurementName, currentMeasurementSaved, pendingRepeatMeasurements | — | — | — | — |
| `measurementPeqFilterToBand` | 7328–7337 | 10 | PEQ-Editing fortgesetzt | — | — | — | — | — | — |
| `showMeasurementPeqTakeFeedback` | 7338–7348 | 11 | PEQ-Editing fortgesetzt | — | — | measurementPeqTakeFeedback | — | — | — |
| `getMeasurementPeqNameSuffix` | 7349–7353 | 5 | PEQ-Editing fortgesetzt | — | — | — | — | — | — |
| `getMeasurementPeqDraftMode` | 7354–7362 | 9 | PEQ-Editing fortgesetzt | — | — | — | — | — | — |
| `getMeasurementPeqPresetName` | 7363–7369 | 7 | PEQ-Editing fortgesetzt | — | — | — | — | — | — |
| `takeMeasurementPeqToPreset` | 7370–7397 | 28 | PEQ-Editing fortgesetzt | — | — | — | — | — | — |
| `createMeasurementPeqPresetFromDraft` | 7398–7458 | 61 | PEQ-Editing fortgesetzt | — | — | effectsPeqModeSelect, measurementPeqCreateBtn | — | — | — |
| `getMeasurementConvolverSelectedSourceEntries` | 7459–7463 | 5 | Convolver-Quellen/Analyse/Build | — | — | — | — | — | — |
| `getMeasurementConvolverSourceEntries` | 7464–7467 | 4 | Convolver-Quellen/Analyse/Build | — | — | — | — | — | — |
| `getMeasurementConvolverSourceSelectionState` | 7468–7509 | 42 | Convolver-Quellen/Analyse/Build | — | — | — | — | — | — |
| `getMeasurementConvolverMeasurementForSide` | 7510–7517 | 8 | Convolver-Quellen/Analyse/Build | — | — | — | — | — | — |
| `getMeasurementConvolverTracePoints` | 7518–7523 | 6 | Convolver-Quellen/Analyse/Build | — | — | — | — | — | — |
| `getMeasurementConvolverAdaptiveDipGuardStrength` | 7524–7527 | 4 | Convolver-Quellen/Analyse/Build | — | — | — | — | — | — |
| `applyMeasurementConvolverDipGuard` | 7528–7531 | 4 | Convolver-Quellen/Analyse/Build | — | — | — | — | — | — |
| `analyzeMeasurementConvolverSide` | 7532–7548 | 17 | Convolver-Quellen/Analyse/Build | — | — | — | — | — | — |
| `getMeasurementConvolverSelectedSourceCount` | 7549–7552 | 4 | Convolver-Quellen/Analyse/Build | — | — | — | — | — | — |
| `getMeasurementConvolverMultiSourceWarning` | 7553–7556 | 4 | Convolver-Quellen/Analyse/Build | — | — | — | — | — | — |
| `buildMeasurementConvolverWarnings` | 7557–7571 | 15 | Convolver-Quellen/Analyse/Build | — | — | — | — | — | — |
| `formatMeasurementConvolverGain` | 7572–7576 | 5 | Convolver-Quellen/Analyse/Build | — | — | — | — | — | — |
| `getMeasurementConvolverNameSuffix` | 7577–7581 | 5 | Convolver-Quellen/Analyse/Build | — | — | — | — | — | — |
| `getMeasurementConvolverItemName` | 7582–7591 | 10 | Convolver-Quellen/Analyse/Build | — | — | — | — | — | — |
| `getMeasurementConvolverPreviewMode` | 7592–7601 | 10 | Convolver-Quellen/Analyse/Build | — | — | — | — | — | — |
| `getMeasurementConvolverPreviewGain` | 7602–7611 | 10 | Convolver-Quellen/Analyse/Build | — | — | — | — | — | — |
| `showMeasurementConvolverFeedback` | 7612–7618 | 7 | Convolver-Quellen/Analyse/Build | — | — | measurementConvolverFeedback | — | — | — |
| `waitForNextAnimationFrame` | 7619–7622 | 4 | Convolver-Quellen/Analyse/Build | — | — | — | — | — | — |
| `getMeasurementConvolverSampleRate` | 7623–7635 | 13 | Convolver-Quellen/Analyse/Build | measurementSampleRate | — | — | — | — | — |
| `getMeasurementConvolverTypeOption` | 7636–7639 | 4 | Convolver-Quellen/Analyse/Build | — | — | — | — | — | — |
| `getMeasurementConvolverTypeKeys` | 7640–7643 | 4 | Convolver-Quellen/Analyse/Build | — | — | — | — | — | — |
| `getMeasurementConvolverPhaseModeForType` | 7644–7647 | 4 | Convolver-Quellen/Analyse/Build | — | — | — | — | — | — |
| `getMeasurementConvolverPhaseLabel` | 7648–7654 | 7 | Convolver-Quellen/Analyse/Build | — | — | — | — | — | — |
| `getMeasurementConvolverPhaseTag` | 7655–7661 | 7 | Convolver-Quellen/Analyse/Build | — | — | — | — | — | — |
| `getMeasurementConvolverFirLengthForType` | 7662–7665 | 4 | Convolver-Quellen/Analyse/Build | — | — | — | — | — | — |
| `getMeasurementConvolverFirLength` | 7666–7670 | 5 | Convolver-Quellen/Analyse/Build | — | — | — | — | — | — |
| `getMeasurementConvolverTypeLabel` | 7671–7674 | 4 | Convolver-Quellen/Analyse/Build | — | — | — | — | — | — |
| `interpolateMeasurementConvolverCorrection` | 7675–7678 | 4 | Convolver-Quellen/Analyse/Build | — | — | — | — | — | — |
| `buildMeasurementConvolverMagnitudeBins` | 7679–7682 | 4 | Convolver-Quellen/Analyse/Build | — | — | — | — | — | — |
| `buildMeasurementConvolverLinearImpulseFromMagnitudes` | 7683–7686 | 4 | Convolver-Quellen/Analyse/Build | — | — | — | — | — | — |
| `fftMeasurementConvolverComplex` | 7687–7690 | 4 | Convolver-Quellen/Analyse/Build | — | — | — | — | — | — |
| `buildMeasurementConvolverMinimumSpectrum` | 7691–7694 | 4 | Convolver-Quellen/Analyse/Build | — | — | — | — | — | — |
| `buildMeasurementConvolverImpulseFromSpectrum` | 7695–7698 | 4 | Convolver-Quellen/Analyse/Build | — | — | — | — | — | — |
| `buildMeasurementConvolverImpulse` | 7699–7702 | 4 | Convolver-Quellen/Analyse/Build | — | — | — | — | — | — |
| `getMeasurementConvolverTimingMs` | 7703–7707 | 5 | Convolver-Quellen/Analyse/Build | — | — | — | — | — | — |
| `getMeasurementConvolverTimingDelta` | 7708–7774 | 67 | Convolver-Quellen/Analyse/Build | — | — | — | — | — | — |
| `getMeasurementConvolverTimingPairDebug` | 7775–7812 | 38 | Convolver-Quellen/Analyse/Build | pendingRepeatMeasurements | — | — | — | — | — |
| `formatMeasurementConvolverTimingRelation` | 7813–7823 | 11 | Convolver-Quellen/Analyse/Build | — | — | — | — | — | — |
| `getMeasurementConvolverTimingSafetyMessage` | 7824–7828 | 5 | Convolver-Quellen/Analyse/Build | — | — | — | — | — | — |
| `alignStereoImpulsesForMinimumAligned` | 7829–7853 | 25 | Convolver-Quellen/Analyse/Build | — | — | — | — | — | — |
| `getMeasurementAnalysisSampleRate` | 7854–7868 | 15 | Convolver-Quellen/Analyse/Build | — | — | — | — | — | — |
| `getMeasurementDirectArrivalTiming` | 7869–7948 | 80 | Convolver-Quellen/Analyse/Build | — | — | — | — | — | — |
| `writeMeasurementConvolverWav` | 7949–7952 | 4 | Convolver-Quellen/Analyse/Build | — | — | — | — | — | — |
| `appendMeasurementConvolverExtras` | 7953–7969 | 17 | Convolver-Quellen/Analyse/Build | — | — | — | — | — | — |
| `createMeasurementConvolverPreset` | 7970–8019 | 50 | Convolver-Quellen/Analyse/Build | — | — | — | — | — | — |
| `takeMeasurementConvolverToDraft` | 8020–8091 | 72 | Convolver-Quellen/Analyse/Build | — | — | — | — | — | — |
| `createMeasurementConvolverPresetFromDraft` | 8092–8206 | 115 | Convolver-Quellen/Analyse/Build | — | — | — | — | — | — |
| `getMeasurementGraphBounds` | 8207–8215 | 9 | Graph-Geometrie/Hover/Handles | — | — | — | — | — | — |
| `getMeasurementGraphDisplaySize` | 8216–8224 | 9 | Graph-Geometrie/Hover/Handles | — | — | — | — | — | — |
| `getMeasurementGraphRenderContext` | 8225–8236 | 12 | Graph-Geometrie/Hover/Handles | — | — | measurementGraph | — | — | — |
| `getMeasurementGraphView` | 8237–8240 | 4 | Graph-Geometrie/Hover/Handles | measurementView | — | — | — | — | — |
| `getMeasurementIrPreviewPoints` | 8241–8253 | 13 | Graph-Geometrie/Hover/Handles | — | — | — | — | — | — |
| `buildMeasurementIrGraphEntry` | 8254–8273 | 20 | Graph-Geometrie/Hover/Handles | — | — | — | — | — | — |
| `getMeasurementIrPeakAbs` | 8274–8280 | 7 | Graph-Geometrie/Hover/Handles | — | — | — | — | — | — |
| `getMeasurementIrWindowRms` | 8281–8289 | 9 | Graph-Geometrie/Hover/Handles | — | — | — | — | — | — |
| `getMeasurementIrStrongestAbs` | 8290–8300 | 11 | Graph-Geometrie/Hover/Handles | — | — | — | — | — | — |
| `formatMeasurementIrDb` | 8301–8306 | 6 | Graph-Geometrie/Hover/Handles | — | — | — | — | — | — |
| `formatMeasurementIrMs` | 8307–8311 | 5 | Graph-Geometrie/Hover/Handles | — | — | — | — | — | — |
| `formatMeasurementIrAmplitude` | 8312–8317 | 6 | Graph-Geometrie/Hover/Handles | — | — | — | — | — | — |
| `getMeasurementIrDiagnostics` | 8318–8341 | 24 | Graph-Geometrie/Hover/Handles | — | — | — | — | — | — |
| `buildMeasurementIrDiagnostics` | 8342–8347 | 6 | Graph-Geometrie/Hover/Handles | — | — | — | — | — | — |
| `formatMeasurementIrCompactRange` | 8348–8357 | 10 | Graph-Geometrie/Hover/Handles | — | — | — | — | — | — |
| `buildMeasurementIrSummary` | 8358–8365 | 8 | Graph-Geometrie/Hover/Handles | — | — | — | — | — | — |
| `buildMeasurementIrDiagnosticsTooltip` | 8366–8376 | 11 | Graph-Geometrie/Hover/Handles | — | — | — | — | — | — |
| `renderMeasurementIrDiagnostics` | 8377–8385 | 9 | Graph-Geometrie/Hover/Handles | — | — | measurementIrDiagnostics | — | — | — |
| `getMeasurementGraphPointerPosition` | 8386–8396 | 11 | Graph-Geometrie/Hover/Handles | — | — | — | — | — | — |
| `formatMeasurementHoverFrequency` | 8397–8407 | 11 | Graph-Geometrie/Hover/Handles | — | — | — | — | — | — |
| `formatMeasurementHoverDb` | 8408–8413 | 6 | Graph-Geometrie/Hover/Handles | — | — | — | — | — | — |
| `getMeasurementTraceDisplayedDbAtFrequency` | 8414–8435 | 22 | Graph-Geometrie/Hover/Handles | — | — | — | — | — | — |
| `getMeasurementFrequencyHoverTooltip` | 8436–8457 | 22 | Graph-Geometrie/Hover/Handles | — | — | — | — | — | — |
| `getMeasurementPeqHandlePosition` | 8458–8464 | 7 | Graph-Geometrie/Hover/Handles | — | — | — | — | — | — |
| `getMeasurementPeqHandleHitRadius` | 8465–8470 | 6 | Graph-Geometrie/Hover/Handles | — | — | — | — | — | — |
| `getCustomHouseCurvePointSlot` | 8471–8478 | 8 | Graph-Geometrie/Hover/Handles | — | — | — | — | — | — |
| `getCustomHouseCurvePointColor` | 8479–8486 | 8 | Graph-Geometrie/Hover/Handles | — | — | — | — | — | — |
| `getCustomHouseCurveHandlePosition` | 8487–8493 | 7 | Graph-Geometrie/Hover/Handles | — | — | — | — | — | — |
| `getCustomHouseCurveHandleHitRadius` | 8494–8499 | 6 | Graph-Geometrie/Hover/Handles | — | — | — | — | — | — |
| `findCustomHouseCurveHandleAtPosition` | 8500–8511 | 12 | Graph-Geometrie/Hover/Handles | — | — | — | — | — | — |
| `findMeasurementPeqFilterHandleAtPosition` | 8512–8523 | 12 | Graph-Geometrie/Hover/Handles | — | — | — | — | — | — |
| `measurementPeqWorkingLineHit` | 8524–8528 | 5 | Graph-Geometrie/Hover/Handles | — | — | — | — | — | — |
| `measurementPeqTouchCreateCoolingDown` | 8529–8532 | 4 | Graph-Geometrie/Hover/Handles | — | — | — | — | — | — |
| `markMeasurementPeqTouchCreate` | 8533–8536 | 4 | Graph-Geometrie/Hover/Handles | — | — | — | — | — | — |
| `getMeasurementConvolverRangeHandleAtPosition` | 8537–8547 | 11 | Graph-Geometrie/Hover/Handles | — | — | — | — | — | — |
| `drawMeasurementTargetCurve` | 8548–8574 | 27 | Graph-Geometrie/Hover/Handles | — | — | — | — | — | — |
| `drawMeasurementConvolverRangeOverlay` | 8575–8600 | 26 | Graph-Geometrie/Hover/Handles | assistMode | — | — | — | — | — |
| `drawCustomHouseCurveHandles` | 8601–8620 | 20 | Graph-Geometrie/Hover/Handles | — | — | — | — | — | — |
| `measurementIrTimeToX` | 8621–8627 | 7 | IR-Graph | — | — | — | — | — | — |
| `measurementXToIrTime` | 8628–8634 | 7 | IR-Graph | — | — | — | — | — | — |
| `measurementIrAmplitudeToY` | 8635–8639 | 5 | IR-Graph | — | — | — | — | — | — |
| `getNearestMeasurementIrPoint` | 8640–8650 | 11 | IR-Graph | — | — | — | — | — | — |
| `getMeasurementIrHoverTooltip` | 8651–8674 | 24 | IR-Graph | — | — | — | — | — | — |
| `drawMeasurementIrGraph` | 8675–8724 | 50 | IR-Graph | — | — | — | — | — | — |
| `handleMeasurementGraphPointerDown` | 8725–8786 | 62 | Graph-Pointer & Entries | assistMode | — | measurementGraph | — | — | — |
| `handleMeasurementGraphPointerMove` | 8787–8853 | 67 | Graph-Pointer & Entries | — | — | measurementGraph | — | — | — |
| `handleMeasurementGraphPointerLeave` | 8854–8857 | 4 | Graph-Pointer & Entries | — | — | measurementGraph | — | — | — |
| `handleMeasurementGraphPointerUp` | 8858–8877 | 20 | Graph-Pointer & Entries | — | — | measurementGraph | — | — | — |
| `buildMeasurementGraphEntry` | 8878–8892 | 15 | Graph-Pointer & Entries | displaySmoothing | — | — | — | — | — |
| `getGraphMeasurementEntries` | 8893–8910 | 18 | Graph-Pointer & Entries | — | — | — | — | — | — |
| `measurementModeReady` | 8911–8914 | 4 | Graph-Pointer & Entries | hostCaptureAvailable, selectedInputId | — | — | — | — | — |
| `describeMeasurementScope` | 8915–8919 | 5 | Graph-Pointer & Entries | — | — | — | — | — | — |
| `measurementModeNoteText` | 8920–8923 | 4 | Graph-Pointer & Entries | — | — | — | — | — | — |
| `measurementHasCalibrationSelected` | 8924–8927 | 4 | Mikrofon-Kalibrierung | calibrationFilename, selectedCalibrationRef | — | — | — | — | — |
| `applyMeasurementCalibrationState` | 8928–8938 | 11 | Mikrofon-Kalibrierung | — | calibrationFilename, calibrationOptions, selectedCalibrationRef | measurementCalibrationFile | — | — | — |
| `setActiveMeasurementCalibration` | 8939–8962 | 24 | Mikrofon-Kalibrierung | — | calibrationUpdating, statusText | — | — | — | — |
| `uploadMeasurementCalibration` | 8963–8985 | 23 | Mikrofon-Kalibrierung | — | calibrationFilename, calibrationUpdating, statusText | — | — | — | — |
| `downloadSelectedMeasurementCalibration` | 8986–9008 | 23 | Mikrofon-Kalibrierung | calibrationOptions, selectedCalibrationRef | calibrationExporting, statusText | — | — | — | — |
| `deleteSelectedMeasurementCalibration` | 9009–9038 | 30 | Mikrofon-Kalibrierung | activeJobId, calibrationOptions, selectedCalibrationRef, startInFlight | calibrationDeleting, statusText | — | — | — | — |
| `applyMeasurementHouseCurveState` | 9039–9045 | 7 | House-Curve | — | houseCurveFilename, houseCurveOptions | measurementHouseCurveFile | — | — | — |
| `uploadMeasurementHouseCurve` | 9046–9070 | 25 | House-Curve | — | houseCurveFilename, houseCurveUpdating, statusText | — | — | — | — |
| `ensureCustomHouseCurveState` | 9071–9093 | 23 | House-Curve | — | — | — | — | — | — |
| `getCustomHouseCurveNameSuggestion` | 9094–9101 | 8 | House-Curve | houseCurveOptions | — | — | — | — | — |
| `openCustomHouseCurveEditor` | 9102–9110 | 9 | House-Curve | — | — | — | — | — | — |
| `handleMeasurementTargetCurveSelection` | 9111–9121 | 11 | House-Curve | — | — | — | — | — | — |
| `addCustomHouseCurvePoint` | 9122–9141 | 20 | House-Curve | — | — | — | — | — | — |
| `resetCustomHouseCurveDraft` | 9142–9149 | 8 | House-Curve | — | — | — | — | — | — |
| `updateCustomHouseCurvePoint` | 9150–9156 | 7 | House-Curve | — | — | — | — | — | — |
| `addCustomHouseCurvePointAtPosition` | 9157–9162 | 6 | House-Curve | — | — | — | — | — | — |
| `deleteCustomHouseCurvePoint` | 9163–9168 | 6 | House-Curve | — | — | — | — | — | — |
| `serializeCustomHouseCurvePoints` | 9169–9176 | 8 | House-Curve | — | — | — | — | — | — |
| `createCustomHouseCurve` | 9177–9214 | 38 | House-Curve | — | statusText | — | — | — | — |
| `downloadSelectedMeasurementHouseCurve` | 9215–9237 | 23 | House-Curve | houseCurveOptions | houseCurveExporting, statusText | measurementHouseCurveSelect | — | — | — |
| `deleteSelectedMeasurementHouseCurve` | 9238–9265 | 28 | House-Curve | houseCurveOptions | houseCurveDeleting, statusText | measurementHouseCurveSelect | — | — | — |
| `applyMeasurementSetupSettings` | 9266–9289 | 24 | Setup-Settings/Inputs/Fetch | — | measurementSampleRate, selectedInputConfigured, selectedInputKey, selectedInputLegacyId, selectedMicInputChannel, selectedReferenceInputChannel | — | — | — | — |
| `saveMeasurementSetupSettings` | 9290–9308 | 19 | Setup-Settings/Inputs/Fetch | — | — | — | — | — | — |
| `fetchMeasurements` | 9309–9343 | 35 | Setup-Settings/Inputs/Fetch | activeJobId, currentMeasurement, saveInFlight, startInFlight | calibrationOptions, houseCurveOptions, loading, measurements, reviewVisibilityById, selectedCalibrationRef, statusText, storage, visibilityById | — | — | — | — |
| `measurementInputAvailabilityMessage` | 9344–9357 | 14 | Setup-Settings/Inputs/Fetch | — | — | — | — | — | — |
| `measurementSetupStatusText` | 9358–9362 | 5 | Setup-Settings/Inputs/Fetch | — | — | — | — | — | — |
| `applyMeasurementInputSelection` | 9363–9378 | 16 | Setup-Settings/Inputs/Fetch | selectedMicInputChannel, selectedReferenceInputChannel | selectedInputConfigured, selectedInputId, selectedInputKey, selectedInputUnavailable | — | — | — | — |
| `fetchMeasurementInputs` | 9379–9452 | 74 | Setup-Settings/Inputs/Fetch | activeJobId, startInFlight | captureAvailable, hostCaptureAvailable, inputs, inputsLoading, measurementSampleRate, modeNote, selectedInputConfigured, selectedInputId, selectedInputKey, selectedInputUnavailable, statusText | — | — | — | — |
| `isMeasurementPanelOpen` | 9453–9456 | 4 | Panel open/Heartbeat/Input-Auswahl | — | — | measurementPanel | — | — | — |
| `sendMeasurementWindowHeartbeat` | 9457–9468 | 12 | Panel open/Heartbeat/Input-Auswahl | — | — | — | — | — | — |
| `startMeasurementWindowHeartbeat` | 9469–9480 | 12 | Panel open/Heartbeat/Input-Auswahl | — | — | — | — | — | — |
| `stopMeasurementWindowHeartbeat` | 9481–9488 | 8 | Panel open/Heartbeat/Input-Auswahl | — | — | — | — | — | — |
| `toggleMeasurementPanel` | 9489–9513 | 25 | Panel open/Heartbeat/Input-Auswahl | — | modeNote, open | effectsMeasureOpenBtn, measurementCloseBtn, measurementPanel | — | — | — |
| `getSelectedMeasurementInput` | 9514–9518 | 5 | Panel open/Heartbeat/Input-Auswahl | — | — | — | — | — | — |
| `getSelectedMeasurementInputChannelCount` | 9519–9522 | 4 | Panel open/Heartbeat/Input-Auswahl | — | — | — | — | — | — |
| `normalizeMeasurementInputChannelSelections` | 9523–9540 | 18 | Panel open/Heartbeat/Input-Auswahl | — | — | — | — | — | — |
| `getMeasurementReferenceWarning` | 9541–9549 | 9 | Panel open/Heartbeat/Input-Auswahl | — | — | — | — | — | — |
| `scheduleMeasurementGraphRender` | 9550–9558 | 9 | Panel open/Heartbeat/Input-Auswahl | — | — | — | — | — | — |
| `scheduleMeasurementGraphRenderForResize` | 9559–9563 | 5 | Graph-Render (Scales/Overlays/draw) | — | — | measurementPanel | — | — | — |
| `getSortedNumericValues` | 9564–9567 | 4 | Graph-Render (Scales/Overlays/draw) | — | — | — | — | — | — |
| `getValueQuantile` | 9568–9571 | 4 | Graph-Render (Scales/Overlays/draw) | — | — | — | — | — | — |
| `getMeasurementGraphRange` | 9572–9575 | 4 | Graph-Render (Scales/Overlays/draw) | — | — | — | — | — | — |
| `measurementFrequencyToX` | 9576–9579 | 4 | Graph-Render (Scales/Overlays/draw) | — | — | — | — | — | — |
| `measurementDbToY` | 9580–9583 | 4 | Graph-Render (Scales/Overlays/draw) | — | — | — | — | — | — |
| `getMeasurementPeqFilterMagnitude` | 9584–9587 | 4 | Graph-Render (Scales/Overlays/draw) | — | — | — | — | — | — |
| `drawMeasurementPeqOverlay` | 9588–9644 | 57 | Graph-Render (Scales/Overlays/draw) | — | — | — | — | — | — |
| `drawMeasurementGraph` | 9645–9764 | 120 | Graph-Render (Scales/Overlays/draw) | — | — | measurementGraph, measurementPanel | — | — | — |
| `summarizeMeasurementBand` | 9765–9768 | 4 | Summary/Timing/Quality | — | — | — | — | — | — |
| `formatMeasurementUpperLimit` | 9769–9775 | 7 | Summary/Timing/Quality | — | — | — | — | — | — |
| `summarizeMeasurementEntry` | 9776–9780 | 5 | Summary/Timing/Quality | — | — | — | — | — | — |
| `formatMeasurementQualityReason` | 9781–9803 | 23 | Summary/Timing/Quality | — | — | — | — | — | — |
| `getMeasurementQualitySummary` | 9804–9810 | 7 | Summary/Timing/Quality | — | — | — | — | — | — |
| `getMeasurementQualityTitle` | 9811–9815 | 5 | Summary/Timing/Quality | — | — | — | — | — | — |
| `formatSignedMeasurementMs` | 9816–9823 | 8 | Summary/Timing/Quality | — | — | — | — | — | — |
| `getMeasurementLrRepeatGlobalDeltaMs` | 9824–9836 | 13 | Summary/Timing/Quality | — | — | — | — | — | — |
| `formatMeasurementLrRepeatDelta` | 9837–9845 | 9 | Summary/Timing/Quality | — | — | — | — | — | — |
| `getMeasurementTimingInfo` | 9846–9939 | 94 | Summary/Timing/Quality | — | — | — | — | — | — |
| `sleep` | 9940–9947 | 8 | Job-Status-Helpers | — | — | — | — | — | — |
| `getMeasurementJobStatus` | 9948–9951 | 4 | Job-Status-Helpers | — | — | — | — | — | — |
| `normalizeMeasurementKind` | 9952–9958 | 7 | Job-Status-Helpers | — | — | — | — | — | — |
| `getActiveMeasurementKind` | 9959–9968 | 10 | Job-Status-Helpers | — | — | — | — | — | — |
| `hasActiveMeasurementJob` | 9969–9973 | 5 | Job-Status-Helpers | — | — | — | — | — | — |
| `getMeasurementJobResultMeasurement` | 9974–9980 | 7 | Job-Status-Helpers | — | — | — | — | — | — |
| `formatMeasurementInputLevelText` | 9981–9989 | 9 | Job-Status-Helpers | — | — | — | — | — | — |
| `formatMeasurementJobStatusText` | 9990–9997 | 8 | Job-Status-Helpers | repeatJobActive | — | — | — | — | — |
| `syncMeasurementStartButtonFallback` | 9998–10025 | 28 | Job-Status-Helpers | — | — | measurementRepeatStartBtn, measurementStartBtn | — | — | — |
| `syncSubwooferControlsDuringAutoSub` | 10026–10037 | 12 | Auto-Sub | autoSubInFlight | — | effectsSubwooferDelay, effectsSubwooferFrequencyNumber, effectsSubwooferLevel, effectsSubwooferMainHighpass, effectsSubwooferPolarity, effectsSubwooferSub2Delay, effectsSubwooferSub2Level, effectsSubwooferSub2Polarity | — | — | — |
| `syncAutoSubButton` | 10038–10064 | 27 | Auto-Sub | — | — | measurementAutoSubGroup, measurementAutoSubStartBtn, splCalibrationOpen | — | — | — |
| `startAutoSubOptimize` | 10065–10135 | 71 | Auto-Sub | — | — | measurementCalibrationFile | — | — | — |
| `cancelAutoSubOptimize` | 10136–10154 | 19 | Auto-Sub | autoSubJobId | statusText | — | — | — | — |
| `pollAutoSubJob` | 10155–10229 | 75 | Auto-Sub | — | — | measurementAutoSubStatus | — | — | — |
| `handleAutoSubResult` | 10230–10417 | 188 | Auto-Sub | — | — | measurementAutoSubStatus | — | — | — |
| `renderMeasurementPanelDefensively` | 10418–10436 | 19 | Defensive Render & Start-Flows | — | activeJobId, startInFlight, statusText | — | — | — | — |
| `startHostMeasurement` | 10437–10480 | 44 | Defensive Render & Start-Flows | hostCaptureAvailable, selectedCalibrationRef, selectedChannel, selectedInputId, selectedInputKey, selectedMicInputChannel, selectedReferenceInputChannel | activeJobId, activeMeasurementKind, currentMeasurementSaved, pendingRepeatMeasurements, statusText | measurementCalibrationFile | — | — | — |
| `startLrRepeatMeasurement` | 10481–10520 | 40 | Defensive Render & Start-Flows | currentMeasurementName, hostCaptureAvailable, selectedCalibrationRef, selectedInputId, selectedInputKey, selectedMicInputChannel, selectedReferenceInputChannel | activeJobId, activeMeasurementKind, pendingRepeatMeasurements, repeatJobActive, statusText | measurementCalibrationFile | — | — | — |
| `getHybridWizardState` | 10521–10530 | 10 | Hybrid-Wizard | — | hybridWizard | — | — | — | — |
| `getCurrentOutputModeName` | 10531–10534 | 4 | Hybrid-Wizard | — | — | — | — | — | — |
| `openHybridMeasurementWizard` | 10535–10569 | 35 | Hybrid-Wizard | — | — | measurementHybridPanel, measurementHybridPrimaryBtn | — | — | — |
| `closeHybridMeasurementWizard` | 10570–10585 | 16 | Hybrid-Wizard | — | activeJobId, activeMeasurementKind | measurementHybridPanel | — | — | — |
| `renderHybridRoomDiagram` | 10586–10606 | 21 | Hybrid-Wizard | — | — | measurementHybridPanel | — | — | — |
| `renderHybridMeasurementWizard` | 10607–10660 | 54 | Hybrid-Wizard | — | — | measurementHybridActive, measurementHybridBackBtn, measurementHybridInstruction, measurementHybridMode, measurementHybridPanel, measurementHybridPrimaryBtn, measurementHybridProgress, measurementHybridStatus, measurementHybridSummary, measurementHybridTitle | — | — | — |
| `buildHybridMeasurementForm` | 10661–10675 | 15 | Hybrid-Wizard | selectedCalibrationRef, selectedInputId, selectedInputKey, selectedMicInputChannel, selectedReferenceInputChannel | — | measurementCalibrationFile | — | — | — |
| `hybridSpeakerName` | 10676–10681 | 6 | Hybrid-Wizard | — | — | — | — | — | — |
| `runHybridWizardStep` | 10682–10745 | 64 | Hybrid-Wizard | — | activeJobId, activeMeasurementKind | — | — | — | — |
| `cancelHybridWizardMeasurement` | 10746–10757 | 12 | Hybrid-Wizard | — | — | — | — | — | — |
| `runHybridWizardSweep` | 10758–10821 | 64 | Hybrid-Wizard | — | activeJobId, activeMeasurementKind | — | — | — | — |
| `openHybridProfileInConvolver` | 10822–10859 | 38 | Hybrid-Wizard | — | currentMeasurement, currentMeasurementName, currentMeasurementSaved, pendingRepeatMeasurements | measurementConvolverPanel | — | — | — |
| `setupHybridMeasurementWizard` | 10860–10882 | 23 | Hybrid-Wizard | — | — | measurementHybridBackBtn, measurementHybridCloseBtn, measurementHybridOpenBtn, measurementHybridPanel, measurementHybridPrimaryBtn | — | elements.measurementHybridOpenBtn:click; elements.measurementHybridCloseBtn?:click; elements.measurementHybridPrimaryBtn?:click; elements.measurementHybridBackBtn?:click | — |
| `startMeasurement` | 10883–10914 | 32 | Measurement-Jobs (start/poll/save/delete/merge) | — | activeJobId, activeMeasurementKind, currentMeasurementSaved, repeatJobActive, startInFlight, statusText | — | — | — | — |
| `startLrRepeat` | 10915–10942 | 28 | Measurement-Jobs (start/poll/save/delete/merge) | activeJobId | activeMeasurementKind, repeatJobActive, startInFlight, statusText | — | — | — | — |
| `cancelMeasurement` | 10943–10968 | 26 | Measurement-Jobs (start/poll/save/delete/merge) | — | activeJobId, activeMeasurementKind, repeatJobActive, startInFlight, statusText | — | — | — | — |
| `pollMeasurementJob` | 10969–11069 | 101 | Measurement-Jobs (start/poll/save/delete/merge) | reviewVisibilityById | activeJobId, activeMeasurementKind, currentMeasurement, currentMeasurementName, currentMeasurementSaved, pendingRepeatMeasurements, repeatJobActive, startInFlight, statusText | — | — | — | — |
| `saveCurrentMeasurement` | 11070–11159 | 90 | Measurement-Jobs (start/poll/save/delete/merge) | reviewVisibilityById, visibilityById | autoSubMeasurements, currentMeasurement, currentMeasurementName, currentMeasurementSaved, pendingRepeatMeasurements, saveInFlight, statusText | — | — | — | — |
| `deleteMeasurement` | 11160–11183 | 24 | Measurement-Jobs (start/poll/save/delete/merge) | reviewVisibilityById, startInFlight, visibilityById | saveInFlight, statusText | — | — | — | — |
| `deleteSelectedMeasurements` | 11184–11217 | 34 | Measurement-Jobs (start/poll/save/delete/merge) | reviewVisibilityById, startInFlight, visibilityById | saveInFlight, statusText | — | — | — | — |
| `mergeSelectedMeasurements` | 11218–11268 | 51 | Measurement-Jobs (start/poll/save/delete/merge) | reviewVisibilityById, startInFlight, visibilityById | saveInFlight, statusText | — | — | — | — |
| `renderMeasurementPanel` | 11269–11973 | 705 | renderMeasurementPanel | visibilityById | savedGroupOpen | measurementAssistMode, measurementCalibrationDeleteBtn, measurementCalibrationExportBtn, measurementCalibrationName, measurementCalibrationSelect, measurementCalibrationUploadName, measurementChannelSelect, measurementClearBtn, measurementConvolverCreateBtn, measurementConvolverDipGuard, measurementConvolverIrLength, measurementConvolverMaxBoost, measurementConvolverMaxCut, measurementConvolverPanel, measurementConvolverPhaseMode, measurementConvolverPresetName, measurementConvolverRangeEnd, measurementConvolverRangeStart, measurementConvolverSampleRate, measurementConvolverSummary, measurementConvolverTakeBothBtn, measurementConvolverTakeLeftBtn, measurementConvolverTakeRightBtn, measurementConvolverTarget, measurementConvolverWarnings, measurementCustomHouseCurveChips, measurementCustomHouseCurveCreateBtn, measurementCustomHouseCurveEditor, measurementCustomHouseCurveName, measurementCustomHouseCurvePanel, measurementEmpty, measurementGraph, measurementGraphControls, measurementGraphSubtitle, measurementHouseCurveDeleteBtn, measurementHouseCurveExportBtn, measurementHouseCurveName, measurementHouseCurveSelect, measurementHouseCurveUploadName, measurementInputGroup, measurementInputRefreshBtn, measurementInputSelect, measurementList, measurementMicInputChannelSelect, measurementModeNote, measurementNameInput, measurementPeqChips, measurementPeqCreateBtn, measurementPeqDraftSummary, measurementPeqEditor, measurementPeqPanel, measurementPeqPresetName, measurementPeqTakeBothBtn, measurementPeqTakeLeftBtn, measurementPeqTakeRightBtn, measurementReferenceInputChannelSelect, measurementReferenceWarning, measurementRepeatStartBtn, measurementSaveBtn, measurementSetupCard, measurementSetupStatus, measurementSetupToggleBtn, measurementStartBtn, measurementSummary, measurementTargetCurve | — | button:click; button:click; input:input; input:change; button:click; input:keydown; input:input; input:change; button:click; button:click; button:click; button:click; details:toggle; input:change; input:change; button:click; button:click; button:click | — |
| `setupMeasurementActions` | 11974–12270 | 297 | setupMeasurementActions | — | calibrationFilename, currentMeasurementName, displaySmoothing, houseCurveFilename, measurementView, selectedCalibrationRef, selectedChannel, selectedMicInputChannel, selectedReferenceInputChannel, setupOpen | effectsMeasureOpenBtn, measurementAssistMode, measurementAutoSubStartBtn, measurementCalibrationDeleteBtn, measurementCalibrationExportBtn, measurementCalibrationFile, measurementCalibrationSelect, measurementChannelSelect, measurementClearBtn, measurementCloseBtn, measurementConvolverCreateBtn, measurementConvolverDipGuard, measurementConvolverIrLength, measurementConvolverMaxBoost, measurementConvolverMaxCut, measurementConvolverPhaseMode, measurementConvolverPresetName, measurementConvolverRangeEnd, measurementConvolverRangeStart, measurementConvolverSampleRate, measurementConvolverTakeBothBtn, measurementConvolverTakeLeftBtn, measurementConvolverTakeRightBtn, measurementConvolverTarget, measurementCustomHouseCurveCreateBtn, measurementCustomHouseCurveName, measurementGraph, measurementHouseCurveDeleteBtn, measurementHouseCurveExportBtn, measurementHouseCurveFile, measurementHouseCurveSelect, measurementInputRefreshBtn, measurementInputSelect, measurementMicInputChannelSelect, measurementNameInput, measurementPanel, measurementPeqCreateBtn, measurementPeqPresetName, measurementPeqTakeBothBtn, measurementPeqTakeLeftBtn, measurementPeqTakeRightBtn, measurementReferenceInputChannelSelect, measurementRepeatStartBtn, measurementSaveBtn, measurementSetupToggleBtn, measurementStartBtn, measurementTargetCurve | — | elements.effectsMeasureOpenBtn:click; elements.measurementCloseBtn:click; backdrop:click; document:keydown; window:pagehide; elements.measurementSetupToggleBtn:click; elements.measurementInputSelect:pointerdown; elements.measurementInputSelect:focus; elements.measurementInputSelect:change; elements.measurementInputRefreshBtn:click; elements.measurementChannelSelect:change; elements.measurementMicInputChannelSelect:change; elements.measurementReferenceInputChannelSelect:change; button:click; button:click; button:click; elements.measurementCalibrationSelect:change; elements.measurementCalibrationFile:change; elements.measurementCalibrationDeleteBtn:click; elements.measurementCalibrationExportBtn:click; elements.measurementHouseCurveSelect:change; elements.measurementHouseCurveFile:change; elements.measurementHouseCurveDeleteBtn:click; elements.measurementHouseCurveExportBtn:click; elements.measurementNameInput:input; elements.measurementStartBtn:click; elements.measurementRepeatStartBtn:click; elements.measurementAutoSubStartBtn:click; elements.measurementSaveBtn:click; elements.measurementClearBtn:click; elements.measurementAssistMode:change; elements.measurementAssistMode:focus; elements.measurementAssistMode:blur; elements.measurementAssistMode:click; elements.measurementTargetCurve:change; elements.measurementCustomHouseCurveName:input; elements.measurementCustomHouseCurveCreateBtn:click; input:change; input:input; elements.measurementConvolverPresetName:input; elements.measurementConvolverTakeLeftBtn:click; elements.measurementConvolverTakeRightBtn:click; elements.measurementConvolverTakeBothBtn:click; elements.measurementConvolverCreateBtn:click; elements.measurementPeqPresetName:input; elements.measurementPeqTakeLeftBtn:click; elements.measurementPeqTakeRightBtn:click; elements.measurementPeqTakeBothBtn:click; elements.measurementPeqCreateBtn:click; elements.measurementGraph:pointerdown; elements.measurementGraph:pointermove; elements.measurementGraph:pointerup; elements.measurementGraph:pointercancel; elements.measurementGraph:pointerleave; elements.measurementGraph:wheel; window:resize; window:orientationchange; document:fullscreenchange; window.visualViewport?:resize | — |

## C) Loose Globals (Dateikopf) und Block-Konstanten

| Zeile | Deklaration |
|---|---|
| 317 | `let measurementInputScanOnFocusDone = false;` |
| 318 | `let measurementSettingsRevision = 0;` |
| 319 | `let measurementResizeScheduled = false;` |
| 320 | `let measurementGraphResizeObserver = null;` |
| 323 | `let measurementGraphPointerId = null;` |
| 324 | `let measurementPeqTakeFeedbackTimer = null;` |
| 325 | `let measurementPeqLastTouchCreateAt = 0;` |
| 326 | `let measurementWindowHeartbeatTimer = null;` |
| 6851 | `const measurementComparePalette = ['#60a5fa', '#f59e0b', '#f472b6', '#a78bfa', '#f87171', '#facc15'];` |
| 6852 | `const measurementCurrentColor = '#22c55e';` |
| 6853 | `const measurementPeqPalette = ['#60a5fa', '#f59e0b', '#f472b6', '#a78bfa'];` |
| 6854 | `const measurementPeqTypes = ['bell', 'low_shelf', 'high_shelf', 'low_pass', 'high_pass', 'notch', 'gain'];` |
| 6855 | `const MEASUREMENT_CONVOLVER_TIMING_SAFETY_LIMIT_MS = 6.0;` |
| 6856 | `const measurementConvolverPhaseModes = ['linear', 'minimum', 'minimum_aligned', 'hybrid_aligned'];` |
| 6857 | `const measurementConvolverAlignedPhaseModes = ['minimum_aligned', 'hybrid_aligned'];` |
| 6858 | `const measurementConvolverCurves = {` |
| 6864 | `const measurementPeqTypeLabels = {` |
| 7628 | `const measurementConvolverTapOptions = [2048, 4096, 8192, 16384, 32768];` |
| 7629 | `const measurementConvolverTypeOptions = measurementConvolverPhaseModes.flatMap((phaseMode) => measurementConvolverTapOpt` |
| 9822 | `const MEASUREMENT_LR_REPEAT_DELTA_TOOLTIP = 'L/R delta is calculated as Right - Left. Positive means the right channel a` |
| 9944 | `const MEASUREMENT_JOB_SUCCESS_STATES = new Set(['completed', 'complete', 'finished', 'success', 'ready', 'done', 'ok']);` |
| 9945 | `const MEASUREMENT_JOB_FAILED_STATES = new Set(['failed', 'failure', 'error']);` |
| 9946 | `const MEASUREMENT_JOB_CANCELLED_STATES = new Set(['cancelled', 'canceled']);` |
