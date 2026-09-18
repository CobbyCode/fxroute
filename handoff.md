# Handoff — Multichannel / Task 8 + Unified Output + Backend-v2-Migration

## Arbeitsstand (2026-09-18, paired filter banks)
- HEAD: `a73fdcf` (darunter `6d644a1` Paired Filter Banks, deployed auf `.104`, State rev 100→102 intakt, Auswahl zurück auf Global).
- Paired Filter Banks: Stereo-Paare (Main/Low/Low-Mid/Mid/High/Sub L/R) teilen je eine Bank über verlustfrei erhaltenen Pro-Rolle-Bindings (`audio/filter_banks.py`); Mono-Rollen einzeln; Pure-Stereo ohne Auswahl (Global); All Banks schaltet konfigurierte Bereichsbänke atomar (keine eigene Kette); Import/Measurement an konkrete Bänke gebunden; A/B-Kachel mit kompakter Auswahl, Statuszeile entfernt. Plan: `docs/superpowers/plans/2026-09-18-paired-filter-banks.md` (Git-ignoriert).
- Verifikation: lokal **407/0/14**; `.104`-Scratch: 14 Python-Suiten + nativer Bank-Chain-Test grün (JS nur lokal); Browser-Check `check_output_system_ui.py` ok; Produkt aktiv, HTTP 200, Assets (`app.js?v=0.9.181`, `output_state.js?v=0.9.8`, `style.css?v=0.9.236`), gruppierter Katalog live verifiziert, Select-Roundtrip main→global ohne Fingerprint-Änderung. Backup `~/deploy-backup/fxroute-a73fdcf-pre-banks.tar`. Kein Push/Release.
- Nebenbefund (pre-existing, gefixt): `test_native_dsp_bank_chain.py` referenzierte den entfernten `crossover`-Modus (auf Base identisch rot); auf Stereo-Sub + Crossover-Flag portiert.
- Bewusst fail-closed: vor dem Paarmodell eingefrorene Single-Role-Messungen lassen sich nicht in Paar-Bänke committen (409); Re-Messung nötig.

## Arbeitsstand
- Worktree: `/home/pbclaw/ai/projects/fxroute-multichannel`
- Branch: `feature/multichannel-crossover`
- HEAD: `8937e62` (Cleanup nach Output-Modell-Vereinheitlichung; darunter `88d2ef6` Polarity N/I, `3b70ab7` Sub-Sprach-Angleichung, `c28a6ab` Crossover-Designfix, `8be89a0` DSP-Layout, `d4ceff8` Crossover-Band, `22a3328` Routing-UI-Cleanup, `3c8f246` Settings-Cleanup, `9bfb099` kanalabhängiger Mode-Selektor, `8988501` Modell-Vereinheitlichung).
- Unified Output Model ist **implementiert, lokal 405/0/14 verifiziert und auf `.104` deployed** (siehe unten). Nächster Schritt ist die **Backend-v2-Migration**, geplant in `docs/superpowers/plans/2026-09-18-backend-v2-migration.md` (6 Tasks, noch nicht begonnen — Einstieg dort, Task 0).
- `.104`-Deploy-Praxis nach `../fxroute/AGENTS.md` (im Worktree als `./AGENTS.md` kopiert, Git-ignoriert): Backup nach `~/deploy-backup/fxroute-<sha>-*.tar`, Transfer ohne `demo/`, Restart, Asset-Verifikation. Demo-Server läuft nie auf `.104`; Produkt läuft unter `/home/paul/fxroute` (Port 8000).
- Tasks 1–7 abgeschlossen einschließlich Task-7-Review und Nacharbeiten. Task 8 ist **implementiert, das AutoSub-Gate geöffnet; reale akustische 2-/3-Wege-Verifikation bleibt offen, ist aber mangels passenden Aufbaus aktuell kein Blocker**.
- AutoSub-Slices A–G sind implementiert und committet (B zusätzlich reviewed; F durch zwei externe Review-Runden, G durch eine mit SHIP-Verdict). Speaker Align: Analyse-/Proposal-Slice (`583d3be`), interner Full-Resolution-Capture-Evidenztransport (`0d4855f`) und serielle Way-Acquisition mit Common-Input-Attestation (`17cdfb6`) implementiert und committet, alle drei mit Review SHIP für ihren begrenzten Slice. Guarded Apply, Commit/Restore/Release, API, Frontend und Release-Registrierung sind implementiert (Details unten); reale 2-/3-Wege-Verifikation bleibt separat.
- Der AutoSub-HTTP-Start ist seit `00b596b` geöffnet (`_AUTO_SUB_SERVICE_INTEGRATION_READY = True` in `measurement/autosub/runners/start.py:69`; gestützte Topologie registriert statt 503; ungültige Topologie weiter 400, fehlende Session weiter 503). Kein HTTP-/Environment-Override. Der Verhaltens-Gate-Test ist invertiert (`test_supported_topology_is_open_in_production`): ein versehentlich wieder geschlossenes Gate wird rot.

## Kurz-Handoff (2026-09-18)

- Fertig und live auf `.104` (`http://192.168.178.104:8000`): Unified Output Model (Stereo / Stereo + Sub, Crossover als On/Off-Schalter, ein Hardware-Routing), kanalabhängiger Mode-Selektor, Settings-Reihenfolge, Crossover-Kachel im Sub-Stil mit Pro-Filter-Gruppen — **plus Backend-v2-Migration** (`1425b6d`): Legacy-Routen weg (404), `output_state.js?v=0.9.7`, State rev 98 intakt.
- Verifikation: lokal **405/0/14**; `.104`-Scratch: native Plan-Peak-Parität PASS + 8/8 Migrations-Suiten grün (JS nur lokal, kein Node dort); Produkt neu gestartet, aktiv, HTTP 200, Backup `~/deploy-backup/fxroute-1425b6d-pre-v2.tar`; Demo nie auf `.104`.
- Backend-v2-Migration: Tasks 0–6 **fertig und deployed** (Details unten); danach Post-Migration-Cleanup `32635df` ebenfalls deployed (Details unten). Kein Push/Release erfolgt.
- Regeln: `./AGENTS.md` (Git-ignoriert), kein Push/Release, Deploy nur Backup → Transfer (ohne `demo/`) → Restart → Verify.

## Post-Migration-Cleanup (2026-09-18)

`32635df` — vestigiale Legacy-Parameter aus der Service-Welt entfernt:

- `_auto_sub_apply_candidate` liest nur noch `global_config`/`subwoofers_config`/`job`;
  `output_mode`/`verify`/`load_overview` gelöscht. Fünf Runner-Call-Sites ohne
  `verify`-Lambdas; toter `_verify_final_config` in `optimize.py` entfernt;
  `_auto_sub_22_verify_alignment` bleibt für `measurement.py`,
  `_auto_sub_22_verify_subwoofers` für `test_auto_sub_candidate_lifecycle.py`.
- 11 Test-Doubles auf Stage-and-Return umgestellt; die beiden Failure-Injection-Doubles
  geben explizit `False`; das 2.1-Confirmation-Double modelliert die Mode-Mismatch-
  Verweigerung am Recommit statt über einen Real-Readback-`verify` (Testvertrag erhalten:
  `test_final_recommit_verifies_complete_mode_state` further grün).
- `overview.py`: vier veraltete Kommentare („falls back to the mode file") korrigiert —
  ohne nutzbaren v2-Head degradiert das Overview zu Stereo.
- Verifikation: 16 Fokus-Suiten lokal grün, `py_compile` + `git diff --check` clean,
  keine leftover `verify=`/`output_mode=`/`load_overview=`-Kwargs.
- Deploy `.104`: Backup `~/deploy-backup/fxroute-32635df-pre-cleanup.tar` (19 Dateien),
  Transfer ohne `demo/`/`media/cache`, 14 Fokus-Suiten im App-Venv grün, Service
  neu gestartet, `active`, HTTP 200 (`/api/status`, `/`). Keine Asset-Änderung, kein
  Version-Bump nötig.
- Produkt-Sanity auf `.104`: Preset-Katalog + aktives Preset aufgelöst
  (`/api/dsp/presets`); `set_crossover` on/off-Roundtrip über den v2-Apply
  (`/api/audio/output-state/apply`) 200/200 mit Revisions-Guard; State-Inhalt
  unverändert (rev 98→100 nur Revisionen), `crossover_enabled` wieder `False`,
  Bank `global`/Preset A `Neutral` unberührt, Topologie `stereo-sub` ohne Issues.
  Kein Preset-Reload gegen die Live-Instanz (Restart hat den DSP-/Preset-Pfad
  bereits ausgeübt).

## Backend-v2-Migration — Stand (fertig)

Plan: `docs/superpowers/plans/2026-09-18-backend-v2-migration.md`. Tasks 1–3 waren
per Plan-RED nicht reproduzierbar (v2-Pfad längst aktiv) und sind als
Kontrakt-Tests + Sensitivitäts-Mutationen committet; Tasks 4+5 als ein
kombinierter Slice (Plan-Gate ließ keine Löschung vor Task deserves zu —
Befund: einziger produzierbarer Job trägt immer `output_state_context`,
v2-Commit/Rollback/Link-Build längst aktiv). Commits auf
`feature/multichannel-crossover` (aufbauend auf `680f16f`):

- `19627e8` Task 0: Read-only-Audit aller Legacy-Call-Sites (Voll-Liste hier
  im Handoff, Abschnitt „Backend-v2-Migration Task 0 Audit“).
- `38f82d4` Task 1: Service-Pfad berührt keine Legacy-Persistenz
  (Kontrakt-Test; Plan-Sketch adaptiert — `start` hat kein
  `set_audio_output_mode`, Funnel-Modul nimmt den Slot).
- `dca5280` Task 2: Commit/Rollback über `OutputService`, Legacy-Files per
  Canary unberührt (Commit- + Rollback-Test).
- `b2c6853` Task 3: Link-Build aus Plan-Routen, Mode-Label-unabhängigkeit
  (`from_plan` + Diagnose-Tests).
- `fa4f9dd` Task 5 vorgezogen (Phase A1): beide POST-Routen
  (`/api/audio/output-mode`, `/api/audio/output-routing`) + `set_bass`-Kind
  gelöscht; 423-Lock jetzt auf dem v2-Apply; Crossover/Mute-Garantien auf
  `set_crossover`/`set_routing` umgeschrieben; lokale `AGENTS.md`-Payloads
  auf v2-Apply umgestellt (Datei untracked, nie committen).
- `a6af720` Phase A3a (Lesepfad, additiv): Overview-Mode-Payload wird aus
  dem v2-Head abgeleitet (`configure_output_state_head`, total failure
  semantics, inkl. `-80`-dB-Parks); Device-Switch ist selection-only
  (kein Fallback, kein Persist, stales Device-Gedächtnis ignoriert);
  `_current_output_mode` aus Runtime-Config, dann v2-Rollen, dann Stereo.
- `f33300e` Phase A2 (Coordinator): Commit/Rollback verlangen v2-Kandidat
  (Legacy-Branches, `_verify_output_mode_rollback`, Finalize-Sub-Gate,
  `persist`-Deps-Feld, `output_mode/routing_config`-Felder entfernt).
- `d0ae4ac` Task 4 (Phase B): Legacy-Persistenz + numerische
  Routing-Writes gelöscht (Samplerate-Persist/Overview-Defs,
  `save/saved/restore/all_saved`, Einmal-Migrations-Loader inkl.
  `legacy_snapshot_loader`/Migrationsfenster — `.104` läuft längst
  `output-state.json`); AutoSub (Candidates/Funnel/3 Runner) service-only
  (Legacy-Branches verlangen jetzt Service-Job, Sync/Restore/21-Verify-
  Helper gelöscht); ~28 Testdateien migriert (eine gelöscht:
  `test_auto_sub_sync_runtime.py`), Voll-Sweep **405/0/14**.

Verhaltens-Notizen (bei Weiterarbeit beachten):

- Single-Sub-Kontextmaske ist 4 (nur `output_3`); Maske 12 war das
  Legacy-Default für beide Sub-Outputs.
- `output_route_pairs`/`routing_payload`/`BassManagementConfig.from_overview`
  bleiben als reine Lesefunktionen (Overview-Link-Build, Messkontext);
  keine Schreiber mehr vorhanden (Prod-Grep clean).
- `audio-output-mode.json`/`output-routing.json` werden nie mehr
  geschrieben; alte Dateien auf Hosts sind totes Gewicht (Canary-Test
  beweist Unberührtheit).
- Demo (`demo/`, `demo/dist`) behält simulierte Legacy-Routen (Constraint);
  `demo/dist` nach `output_state.js`-Änderung neu gebaut, Parität grün.

## Maßgebliche Pläne
Die bestehenden Pläne sind maßgeblich. **Keine Neuplanung oder erneute Implementierung bereits abgeschlossener Arbeiten.**
- `docs/superpowers/plans/2026-09-16-multichannel-spec.md`
- `docs/superpowers/plans/2026-09-16-multichannel-crossover.md` — Gesamtplan, Task 8 und Ausführungsprotokoll.
- `docs/superpowers/plans/2026-09-17-autosub-service-integration.md` — AutoSub-Integration, Slices A–G, mit Ausführungslog je Slice.

Das Planverzeichnis ist absichtlich lokal Git-ignoriert; Dateien liegen im genannten Worktree. Nicht die Exclusion ändern. Eine neue Session soll denselben Worktree verwenden und die Pläne dort lesen.

## Erledigt
Tasks 1–7 liefern Routing-/Rollenmodell, pro Modus isolierten atomaren Output-State, A/B-Bänke, Processing-Plan, Crossover-SOS und native Output-Bank-Ausführung, transaktionale Anwendung/API, UI und bereichsgebundene Messungen samt PEQ/FIR-Commit-Gate. Task-7-Nacharbeiten umfassen Summed-Sub-Klassifikation, Repeat-Sperre für einseitige Bereiche, gespeicherte Bereichs-Badges und Entfernung ungenutzter Auswahlzustände.

### Implementierte Task-8-Bausteine
1. **`414d888` — Routing-Adapter** (`measurement/autosub/roles.py`): Topologie → Optimizer-Pfad; none/mono/dual-mono/stereo → unavailable/single-sub/dual-sub/stereo-subs. Mixed Sub 1/R ist dual-mono; einzelnes `sub_l` ist mono. Rollenbasierte Indizes/Masken und Seitenlisten; Crossover-Main umfasst alle Wege der Seite. Engine-Reihenfolge ist kanonisch, nicht Hardware-Zuweisungsreihenfolge.
2. **`9e0eb27` — Rollenadressiertes Exact-Sub-Mute**: `DSPRuntime.set_exact_sub_mute(enabled, *, mask=None)`; ohne Maske bleibt Legacy 3/4. Der Candidate-Funnel reicht die explizite Maske durch Remeasurement und stellt den vorherigen Zustand wieder her. Peak-Zeroing kopiert Daten und lehnt nicht modellierte Outputs ab. Folgefix in `24b7de2`: gemeinsamer Lock serialisiert Maskenentscheidung, Control-ACK und Zustand; überlappende Output-/Exact-Masken bleiben erhalten.
3. **`24b7de2` — In-Memory Candidate-Stager** (`measurement/autosub/candidate_state.py`): `sub_candidate_state`, `compile_candidate`, `CandidateStager.stage()/restore()`. Eingefrorener Startzustand/Revision, expliziter Geräte-/Kanalkontext, nur geroutete Sub-Trims und Bass-Felder; Bänke und relative Speaker-Way-Delays bleiben erhalten. Guarded Stage, Readback, revisionsgeschützte und cancellation-sichere Wiederherstellung. Keine Persistenz im Stager. Runtime-Ramp-Ziele sind explizit; Recovery überschießt nicht auf 0 dB.
4. **`cd228f5` — Job-scoped Candidate-Session (Slice A)** (`measurement/autosub/candidate_session.py`, `deps.py`): `AutoSubProposal` verlangt vollständige absolute Sub-Maps plus beide Bass-Felder. Session-Konstruktion inert; `activate(rate)` synchron; `stage(proposal)`, `ensure_ready(rate)`, `commit_winner(proposal)`, `restore()` asynchron. Commit prüft erneut Runtime-Aktivität, Fingerprint, Rate, endlichen Gain und Revision, dann synchroner `service.commit`. Nach Commit ist der alte Stager retired. Sweep-Layout stammt vom tatsächlich gerenderten Target, keine erneute IR-Auflösung nach Stage. Owner-Registry liegt außerhalb serialisierter Job-Dicts.
5. **`b7defd6` — Staged-Layout im realen Capture-Pfad (Teil von Slice D)**: interne Parameter `expected_native_layout`, `expected_native_output_mode`, `expected_plan_fingerprint` an `MeasurementStore.start_measurement`; all-or-none, nur `raw_helper`, vor erstem Await tief kopiert. Weitergabe über Child-Job und `host_capture.py` an `routing.py`. Staged-Kontext ersetzt dort Legacy-Overview-Ableitung. Rate/Mode/Layout/Bypass/Aktivität/Fingerprint werden auch bei übersprungener Diagnose geprüft; Mismatch blockiert vor Audio-Prozessstart. Keine HTTP-Override-Schnittstelle; Legacy-Aufrufe unverändert.
6. **`1b71a6c` — Slice B, autoritativer Start + Composition**: `runners/start.py` friert genau einen `service.load()` ein; Routing bestimmt Optimizer, kanonische Sub-Slot→Rollen-Map und Exact-Mute-Maske (suspendierte Senken nutzen den Channel-Map-Fallback, unbekannte Kapazität → 400). Runner-Snapshot aus `processing`/`bass_management`; Legacy-Modi sind nur Algorithmuslabels. `output_state_context` im Job, inerter Owner separat in der Registry. `main.py` injiziert Service + Factory (`_build_plan_target`, guarded Rebuild, Readback von Links/Gerät/Prozess-/Planidentität). Alle drei Runner aktivieren nach `register_auto_sub` mit fester `session.measurement_rate`. Fehler vor Worker-Start entfernen Job/Owner und geben den Lock frei. Entry-Cleanup (aus E/F vorgezogen): Owner-Restore, committete Owner nie restaurieren, abgeschirmte Finalizer-Drainage vor Unregister/Drop/Release.
7. **`a20ecac` — Slice C, Compiled-Layout Peak-Predictor**: `jobs.py`-Predictor mit exakt einem von `config` xor `layout`; Layout-Pfad pro Output in nativer Reihenfolge (Routen, SOS/PEQ mit aus `native_dsp/dsp.c` gespiegelten Koeffizienten, Mono-Convolver per FFT, Delay/Trim/Invert, Runtime-Output-Gain, Sink-Gain), Peaks als `output_1..N`. Striktes Fail-closed, Single-Slot-Cache (Model-Tag, Fingerprint, Layout-Signatur, IR-Identität, Rate, Sweep, Gains); Comparison wirft bei Output-Set-Mismatch. Native-Parität auf `.104` ≤ 0.05 dB bewiesen (`scripts/test_native_dsp_plan_peak_parity.py` in `NATIVE_HELPER_TESTS`).
8. **`123047c` — Slice D, Owner-Prearm + Funnel-Staging**: Service-Sweeps stagen den Funnel-Kandidaten über den Owner (`roles.autosub_scan_knobs`), pre-armen mit `owner.ensure_ready` (kein Orchestration-Sync), predicten aus dem Staged-Layout mit Live-Runtime-Gain, übergeben Staged-Layout/Modus/Fingerprint an den Child-Sweep, muten Main-Only mit der Kontext-Rollenmaske (auch beim Restore). Legacy-Jobs unverändert.
9. **`f50c881` — Slice E, Runner-IO-Grenzen**: reine Adapter (`autosub_explicit_knobs`-Kern, `autosub_apply_knobs`-Translator; Scan-Adapter mit Legacy-Wertquellen: Single aus Funnel-Args, Dual aus Snapshot, inaktiv −80 dB); `_auto_sub_apply_candidate` mit `job`-Param stagt für Service (False nur recoverable, Drift/Restore raisen); `_stage_auto_sub_service_state` für Fire-and-forget-Sites; alle direkten Runner-Sites (Winner/Polarity/Gain/Correction/Revert/Deep-Bass/Recommit), Bare-Resyncs für Service geskipt, Derived-Delays explizit gebaut; Legacy-Zeilen byte-identisch. Kritischer Test: volle Runner aller drei Topologien mit raisenden Legacy-Settern. Zwölf bestehende Test-Doubles akzeptieren die neue `job`-Kwarg (ignoriert, Assertions unverändert).
10. **`b5ac9ed` — Slice F, Winner-Commit + Cancellation-sichere Finalisierung**: `AutoSubCandidateSession.commit_staged(cancel_requested)` committet den verifizierten Staging-Stand unter Owner-Lock (Re-Readback, gefrorene Revision, synchroner `service.commit`); Equal-to-start/never-staged ist verifizierter No-op ohne Revisionsbump; kooperative Cancellation (Probe, nach jedem Await geprüft) vetoed die Persistenz und lässt den Owner uncommitted. `candidates._commit_auto_sub_service_winner` (run-fatal, Cancel-Refusal, `committed_revision` im Job-Kontext) rufen alle drei Runner genau einmal vor Completion auf, nach allen akustischen Gates. Cleanup restauriert committete Owner nie → die committete Revision überlebt; Cancel-Refusal läuft über den Cancel-Arm (Job wird cancelled, nicht failed). `MeasurementStore.drain_job` wartet den echten Child-Task-Abschluss inkl. Raw-Scope-/Masken-Cleanup (abschirmt wiederholte Cancellation, deferred Cancel-Persist-Fehler erst nach dem Drain); Service-Funnel draint vor Peak-Read und in Early-Returns, `finish_capture` restauriert Exact-Mute immer nach dem Drain, auch bei Drain-Fehler. Legacy-Pfad byte-identisch. Suites: Winner-Commit 11, Worker-Lifecycle 5, Job-Drain 4, Owner-Prearm 13, Runner-IO 28 (neuer Vertrag).
11. **`ac52b02` — Slice G, Session-Release baut committete Pläne**: `measurement/autosub/release.py` (`create_release_adapter`: eager Geräte-Validierung; Adapter rendert aktuelles `OutputService.load()` zur Restore-Rate, appliziert via `sync_rendered`, liefert Revision/Fingerprint/Rate; immer live, nie rebased). `MeasurementServices.build_autosub_release_adapter` (Default None; in `main.py` late-bound verdrahtet). Session-Slot + `register_autosub_release_adapter`; Release ruft den Adapter an der Measurement-only-Grenze statt `sync_runtime_at_rate` sowie nach committetem Coordinator-Restore mit der Playback-Rate; Slot wird bei Release-Ende gelöscht. Finalizer registriert vor Unregister nur für committete Service-Jobs; fehlende Services/Factory/Session → Warnung + Legacy-Pfad. Legacy-Pfad byte-identisch. Suite: Session-Release-Plan 13 (Review-Verdict SHIP).
12. **`583d3be` — Speaker-Align-Analyse/Proposal**: reine `SpeakerAlignment`-Grenze, eingefrorene rollenbasierte Requests, unverschobene IRs mit gemeinsamer Referenz und fester Mikrofonposition, additive startrelative Delays sowie Overlap-/Phasen-/Summen-Gates. Opt-in-DTFT/Referenzursprung/Direct-Retention in `build_complex_response`; Legacy-Default exakt erhalten. 27 Tests, Zweit-Review SHIP.
13. **`0d4855f` — Full-Resolution-Capture-Evidenztransport**: `measurement/capture_evidence.py`, interner Opt-in in `start_measurement`, Analyzer-/Host-Callback ohne Job-/HTTP-JSON. Ein Owner pro Job, ein Buffer pro Versuch, finale Policy-Analyse per Objektidentität gepaart, einmaliges `take()` nach Task-Ende. Fertige Tasks werden bei `take()` synchron abgeglichen, damit ein noch wartender Done-Callback kein falsches „not ready“ erzeugt; späte Callbacks können konsumierte Evidenz nicht erneut freigeben. Fehler/Cancel verwerfen Buffer. Beobachtete Input-/Link-Metadaten werden transportiert, physische Upstream-/Mikrofon-Attestation bleibt Aufgabe des nächsten Adapters. 21 Tests, Zweit-Review SHIP.
14. **`17cdfb6` — Serielle Speaker-Align-Way-Acquisition**: `measurement/speaker_acquisition.py` (reines Add-on, keine bestehenden Dateien geändert), ein `CaptureEvidence`-Owner pro gefrorenem `SpeakerAlignment`-Request, strikt seriell. Target-Gleichheit und Input-Samkeit werden zur Registrierungszeit geprüft (ValueError stale, gerade gestarteter Job wird vorher gedraint: null Sweeps, kein Worker-Leak); stabile elektrische Referenz pro Weg (Fallback/toleriert/akustisch fail-fast ohne Laundering); gleiche beobachtete Mic-Node/-Serial, Mic-/ER-Kanäle, Referenznode und Rate über alle Wege; Caller-IDs müssen den gefrorenen Request-IDs gleichen. Cancellation zwischen Wegen bricht vor dem nächsten Capture ab, In-Flight-Cancel/Fehler propagiert ohne Teilergebnis. Happy Path läuft echtes `propose` Ende-zu-Ende (injizierte 240 Samples ≈ 5 ms wiedergefunden). 10 Tests, Review SHIP mit explizit deferred Follow-ups — alle sechs umgesetzt und committet als `f237e49` (17 Tests): fail-closed `None`-Provenance, toleriert-/akustisch-Tests, Provenance-Mismatch-Test, Session-IDs in Provenance, Drain-Garantie für alle Pre-Await-Fehler, äußerer `task.cancel()`-Test. Zwei Korrekturen dabei: fail-closed-Key prüft nur die immer erforderlichen Identitätsteile (Acoustic-only scheitert am Referenz-Gate mit korrektem Status); absorbiertes Outer-Cancel stellt den `CancelledError`-Vertrag via `cancelling()` wieder her.
15. **`ddca782` — Guarded Speaker-Align-Trial-Apply + akustische Bestätigung**: `measurement/speaker_apply.py` (reines Add-on, keine bestehenden Dateien geändert; AutoSub-`CandidateStager` bewusst unberührt). `verify_confirmation` über zwei echten `propose`-Outputs: gleiche Paare/Revision/Fingerprint, ~null Residual-Delays (`MAX_CONFIRMED_RESIDUAL_MS = 0.25`, hardware-pending) und keine Summen-Regression (wiederverwendet proposes `MIN_SUM_DB`/`MAX_SUM_REGRESSION_DB`); unüberzeugend → `confirmed: False`, rebased/fehlgeformt → ValueError. `apply_and_confirm` mit injizierten stage/restore/acquire-Grenzen: stale-Check vor Stage, detached Candidate-Kopie, acquire+propose+verify, Restore auf jedem Pfad (shielded bei Cancel), Non-Confirmation ist Ergebnis, Fehler re-raise nach Restore, Stage-Fehler ohne Restore (atomarer Stage-Vertrag). 17 Tests, Review SHIP ohne Criticals (17/17 reviewer-seitig lokal). Vorgeschriebene Follow-ups für den Commit/Wiring-Slice: fail-closed Revisions-/Fingerprint-Präsenz, finiter Residual, geshieldetes Restore auf allen Post-Stage-Pfaden, 3-Wege-Multi-Pair-Coverage, Threshold-Passthrough, Readback/Lock/Timeout im Produktions-Wiring.
16. **`d2f11ee` — Speaker-Align-Commit-Session + Produktions-Wiring**: `measurement/speaker_commit.py` (neu; AutoSub-`CandidateStager`/`AutoSubCandidateSession` bewusst unberührt). `require_speaker_candidate` (nur `alignment_ms` darf abweichen, geänderte Ways müssen aktiv geroutet sein, Revision muss stimmen, equal-to-start ok), `compile_speaker_candidate` über den autoritativen Service, `SpeakerAlignSession` (gefrorener Start + Revision, Guarded-Transitions mit Revisions-Hooks/Readback-Verify/Gain-Erhalt/cancellation-sicherer Recovery wie die bewährten AutoSub-Formen, ein Lock über Trial→Commit, Acquire-Timeout, Retire-nach-Commit, equal-to-start als verifizierter No-op, Cancel-Veto), `confirm_and_commit` (staged zeigen → acquire+propose+verify → noch gestagten Stand committen oder geshieldet restaurieren), `create_speaker_release_adapter` in AutoSub-Release-Form. Alle sieben Trial-Apply-Follow-ups plus alle acht Commit-Review-Items vor dem Commit umgesetzt (u. a. Pre-Stage-Envelope-Check, `ValueError`-Koerzion, Numpy-`_finite`, Lock-Doc-Präzision). 30 Apply- + 26 Commit-Tests, Review SHIP ohne Criticals.
17. **`86b3ffa` — Speaker-Align-Application-Service**: `measurement/speaker_service.py` (neu, keine bestehenden Dateien geändert). Ein-Job-Besitz mit `threading.Lock`, Sync-Validierung vor Job-Existenz, Worker mit Re-Freeze + Pre-Acquire-Freshness-Gate, Dry-run (`apply_and_confirm` über Session-Stage/Restore, nie Commit) vs Vollfluss (`confirm_and_commit`), terminal committed/trial-done/unconfirmed/failed/cancelled, dual Cancel (Flag + Task), JSON-sichere Summaries ohne IRs/numpy. 14 Tests, Review SHIP ohne Criticals; Follow-ups in den HTTP/Composition-Slice deferred (Frozen-Start-Reuse, Cancel in erstes `propose`, HTTP-Fehler-Taxonomie, echtes `describe`/`create_session`-Wiring inkl. Release-Adapter, Loop-Vertrag, gelockte Reads, strikte JSON-Numerik, `wait_for`-Pacing, Ownership-Release/Retention).
18. **`0f556a0` — Speaker-Align-HTTP-API + Composition**: `measurement/speaker_api.py` (neu): `POST /api/speaker-align/start` (200/400/409), `GET /jobs` + `/jobs/{id}` (200/404), `POST /jobs/{id}/cancel` (200/404) über injizierten Singleton-Accessor; `build_speaker_align_service` mit Runtime-Ownership-Pinning, striktem Readback, Ports-Check, Rate pro Job. `main.py` minimal (+Import, Singleton, Describe, Configure, `include_router`). Service-Follow-ups alle umgesetzt (Taxonomie, Frozen-Reuse, Cancel-in-Propose, Retention, `wait_for`). 25 Service- + 13 API-Tests. Erst-Review NOT READY mit einer kritischen Regression (`NameError` beim `main`-Import — Configure lief vor Factory-Def; keine Suite importiert `main`); per Lambda + `import-main`-Verifikation (alle 4 Routen registriert) gefixt, Delta-Re-Review SHIP. Release-Adapter-Registrierung weiter deferred (dokumentiert).
19. **`f506df1` — Speaker-Align-Frontend + Demo-Parität**: `static/speaker_align.js` (neu, UMD): JSON-Payload-Validierung (Side/Input/Reference/Position/Dry-run), Crossover-Katalog-Visibility (`active_mode === crossover` + `way_count >= 2`), terminale Status-Texte ohne Platzhalter/`[object Object]`. Mess-Panel-Sektion (`measurement-speaker-align-group/start/status/side/dry-run/reference/position`, default hidden) + Asset-Versionen (`speaker_align.js 0.9.1`, `measurement_flows.js 0.9.24`, `app.js 0.9.172`). `app.js`: State/Elemente/JSON-API (`/api/speaker-align/start|jobs|cancel`), ein Job (`speakerAlignInFlight/JobId/CancelRequested/Result`), `getActiveMeasurementKind`/`hasActiveMeasurementJob`/`requestMeasurementCancellation`/Render-/Fallback-/Katalog-Sync. `measurement_flows.js`: `sync/start/cancel/poll/handle` mit Generation-Guard, strukturierter Fehlerformatierung und Overview-Refresh, nur über injiziertes `api`. Demo: staged `queued/acquiring/confirming/committed`-Routen. Suite: `scripts/test_speaker_align_frontend.js`. Responsive-Strukturtest auf viertes Workflow-Label aktualisiert (weiter 166 Checks).
20. **`5b319d8` — Speaker-Align-Release-Registrierung**: Session-Slot `register_speaker_align_release_adapter` + Invoke an beiden Release-Stellen (Measurement-only statt Legacy-Sync; nach committetem Coordinator-Restore mit Playback-Rate) + Slot-Clear; beide Adapter (AutoSub + Speaker) werden aufgerufen, beide rendern live Head. `SpeakerAlignService.on_committed`-Hook (optional, nur committet mit Output-Key/Channels/Revision/Job-ID, log-and-continue, nie job-fatal; trial/unconfirmed/failed/cancelled nie). `build_speaker_align_service` paart `get_measurement_session` + `build_release_adapter` (einzeln → ValueError; beide None → Legacy-Pfad). `main.py`: `_create_speaker_align_release_adapter` + Singleton-Wiring. Suite: `scripts/test_measurement_speaker_release.py` 12 (Session 6, Service-Hook 4, Composition 2).

## Architektur — bei Weiterarbeit beachten
- Routing ist Topologie-Autorität; Legacy-Modi dürfen höchstens Algorithmuslabels bleiben. Scoring-, Scan-, Polarity-, Gain- und Confirmation-Mathematik unverändert lassen.
- Ein gefrorener Startzustand pro Job; alle Vorschläge vollständig und startrelativ. Kein Rebase auf eine neuere Revision, kein Persistieren einzelner Scan-Kandidaten.
- Service- und Legacy-Pfade laufen nebeneinander: Service-Jobs erkennt man an `"output_state_context" in job`. Legacy-Zeilen in Runnern/Funnel nur anfassen, wenn bestehende Suiten den Unterschied beweisen; `*_audio_output_mode`-Bindings (Runner-Modul, `candidates`, `measurement`, `samplerate`-Quelle für funktionslokale Imports) sind je eigene Patch-Ziele.
- Nur der final akustisch akzeptierte, erneut verifizierte Vorschlag darf einmalig committed werden — Slice F ist implementiert: `commit_staged` (Owner) + `_commit_auto_sub_service_winner` (Runner-Helper) + Aufruf vor Completion. `commit_winner(proposal)` bleibt für explizite Proposal-Commits; Runner nutzen `commit_staged` (kein Triplet-Resend).
- `CandidateStager` nutzt `guarded_rebuild_rendered`, nicht unguarded `sync_rendered`. Readback muss echte Links (`runtime.verify()`), Prozess-/Planidentität und Gain prüfen. Externe Graph-Ownership muss die Integration serialisieren; Revisionschecks allein machen Store und Engine nicht atomar.
- Nach Winner-Commit niemals über den alten Stager den Startzustand wiederherstellen (Cleanup skippt committete Owner). Captures vor jedem Restore über `MeasurementStore.drain_job` drainen; Exact-Mute-Restore nach dem Drain, Drain-Fehler danach propagieren.
- Session-Release baut committete Pläne (Slice G): Finalizer registriert den Release-Adapter vor Unregister (nur committete Service-Jobs); Release ruft ihn statt Legacy-Sync und löscht den Slot bei Ende. Ein verwaister Adapter (Release erst später) rendert immer den aktuellen Head — inhaltlich nie stale; Geräte-Kontext (Key/Channels/Ports) bleibt gepinnt (enger Rest-Edge bei Idle-Fenster-Gerätewechsel, dokumentiertes Follow-up: Invoke-time-Validierung).
- Der Predictor modelliert nur die Per-Output-Kette; Aufrufer brauchen einen neutralen Global-Pfad (dokumentiert in `jobs.py`).

## Offen — empfohlene Reihenfolge
**Speaker Align — Capture-/Apply-Integration nach dem Analyse-Slice.** Nicht erneut mit Grundlagen beginnen.

- **Rollout-Gate (erledigt, `00b596b`):** `_AUTO_SUB_SERVICE_INTEGRATION_READY = True` in `runners/start.py`; freigegeben nach TDD-Nachweis (RED 503 auf Altbaum via `/tmp/opencode/gate_open_red.py`, GREEN Registrierung bei offenem Gate via `/tmp/opencode/gate_open_green.py`), Full-Sweep 403/0/14 lokal und `.104`-Staffel auf dem Flip-Baum (siehe unten). Zurückflippen auf `False` verriegelt die Startroute wieder (503); der invertierte Gate-Test fängt das.
- **Speaker Align (Task 8), nächster Integrationsrand:** Planung, Transport, Acquisition, Trial-Apply, Commit-Session, Application-Service, HTTP-API mit Composition, Frontend, Release-Adapter-Registrierung und Invoke-time-Device-Validierung sind implementiert (Details oben). Offen: reale 2-/3-Wege-Verifikation am isolierten Aufbau (mangels passenden Aufbaus aktuell kein Blocker).

### Bekannte Restprobleme / Grenzen
- Erledigt (`6280015`): verwaiste Release-Adapter validieren die live Selektion zur Invoke-Zeit und verweigern fail-closed bei Gerätewechsel (Key/Channels/Ports exakt), statt den gepinnten Graphen über die neue Selektion zu bauen. Slot-Clear bei uncommittedtem Finalize bleibt geprüft und verworfen (aktiv schädlich: Orphan rendert immer aktuelle Wahrheit).
- Erledigt (`c76d3f7`): der `.104`-Canary-Schreibbefund (`test_auto_sub_fine_winner_apply.py` persistierte `audio-output-mode.json`) war ein Test-Isolationsleck, kein Produktionsfehler — der Legacy-Restore läuft über candidates' eigene Modul-Bindings, die die Runner-/Samplerate-Patches nie erreichten. TDD: Sentinel-Patches bewiesen das Leck lokal (4 Treffer, geschluckt in `False`), danach beide Bindings auf die Test-Fakes geroutet. Tracer-Sweep der 4 verdächtigsten Runner-Suiten ohne weiteren Befund. `.104`: Suite 3/3 grün plus Canary-Nachweis (kein Mode-File geschrieben); breite Staffel 54/55 Dateien grün (einziger Ausfall `test_measurement_speaker_api.py` mit 12× vorbestehendem `httpx`-Importfehler, lokal grün), Canary über alle 55 Dateien ohne Mode-File (nur `pulse/cookie`). Lokal Full-Sweep 404/0/14, XDG-Audit 32 Dateien sauber. Produktions-Service-Pfad war nie betroffen (Runner-IO-Suite patcht alle Legacy-Setter auf Raise, 28/28).
- Speaker Align ist noch nicht live integriert. Keine Behauptung, Task 8 oder Crossover-AutoSub sei end-to-end fertig.
- Bekannte Altgrenzen: Store↔Engine-TOCTOU ohne gemeinsame Ownership, Legacy-Checks in `silent_active` bei exotischen v2-Routings.

## Gezielte Verifikation
Direkt ausführbare unittest-Skripte; Exitcodes prüfen, nicht durch `| tail` verdecken.

```bash
python3 scripts/test_autosub_role_mapping.py
python3 scripts/test_auto_sub_role_mute.py
python3 scripts/test_autosub_candidate_state.py
python3 scripts/test_autosub_candidate_session.py
python3 scripts/test_autosub_session_release_plan.py
python3 scripts/test_autosub_winner_commit.py
python3 scripts/test_autosub_worker_lifecycle.py
python3 scripts/test_autosub_dependency_injection.py
python3 scripts/test_autosub_service_start.py
python3 scripts/test_auto_sub_start_job_leak.py
python3 scripts/test_autosub_owner_prearm.py
python3 scripts/test_autosub_runner_service_io.py
python3 scripts/test_auto_sub_plan_peak_prediction.py
python3 scripts/test_auto_sub_peak_prediction.py
python3 scripts/test_auto_sub_gain_apply_revert.py
python3 scripts/test_measurement_staged_layout.py
python3 scripts/test_measurement_playback_target.py
python3 scripts/test_measurement_capture_policy.py
python3 scripts/test_measurement_job_setup.py
python3 scripts/test_measurement_job_drain.py
python3 scripts/test_dsp_runtime.py
python3 scripts/test_output_state_lifecycle.py
python3 scripts/test_speaker_align.py
python3 scripts/test_measurement_capture_evidence.py
python3 scripts/test_measurement_speaker_acquisition.py
python3 scripts/test_measurement_speaker_apply.py
python3 scripts/test_measurement_speaker_commit.py
python3 scripts/test_measurement_speaker_service.py
python3 scripts/test_measurement_speaker_api.py
python3 scripts/test_measurement_speaker_release.py
node scripts/test_speaker_align_frontend.js
python3 scripts/test_hybrid_measurement.py
git diff --check
```

Zuletzt fokussiert grün: Service-Start 23, Start-Leak 3, Dependency-Injection 11, Candidate-Session 38, Session-Release-Plan 13, Winner-Commit 11, Worker-Lifecycle 5, Job-Drain 4, Owner-Prearm 13, Runner-Service-IO 28, Plan-Peak 17, Peak-Prediction 5, Gain-Apply-Revert 23, Staged-Layout 15, Playback-Target 14, Capture-Policy 4, Job-Setup 6, Runtime 62, Output-Lifecycle 8, alle 47 `test_auto_sub_*`-/`test_autosub_*`-/Drain-Suiten. Vollständiger `run_tests.sh`-Sweep nach Slice G: lokal 394 bestanden / 0 fehlgeschlagen / 14 übersprungen (native Helper-Suiten lokal geskipt). `.104`-Verifikation für Slice G gefahren (rsync ohne `--delete`, App-Venv, Produkt-App unberührt): neue Suite 13/13, betroffenes Set 56/56 (alle Auto-/Autosub-/Drain- plus SR-Session-/Release-/Ownership-/Entry-/DI-/Watcher-/Setup-/Target-/Policy-Suiten), nativ 11/11 Helper + 3/3 C inkl. Plan-Peak-Parität. Staged-Layout-`httpx`-Gap unverändert vorbestehend (von G unberührt).
- Speaker-Align-Analyse-Slice (`583d3be`, Zweit-Review SHIP für den begrenzten Slice): neue Suite `test_speaker_align.py` 27/27 lokal und auf `.104`; 9 Suiten (128 Tests: Hybrid 13, Analyzer-IR 1, Bank-Target 23, Output-Mask 8, Repeat-Way-Sweeps 8, Output-State 10, Processing-Plan 7, Candidate-State 31, Speaker-Align 27) lokal und auf `.104` grün; Legacy-Hybrid-Default 16/16 exakt gegen HEAD; voller `run_tests.sh`-Sweep lokal 395/0/14. Erst-Review (not-ready) mit zwei False-Accepts reproduziert und per Regressionstest gefixt (Direct-Event-Retention, Residual-Phasenvieto); LR12/LR24 mit echter Ankunftserkennung bei 44,1/48/96 kHz akzeptiert, LR48/LR72 fail-closed abgelehnt.
- Speaker-Align-Capture-Evidenz-Slice (`0d4855f`, Zweit-Review SHIP): neue Suite 21/21 lokal und `.104`; dort 12 betroffene Suiten mit 134 Tests grün (Capture-Evidence, Speaker-Align, Analyzer-IR, Hybrid, Bank-Target, Output-Mask, Repeat-Way-Sweeps, Capture-Policy, Job-Setup, Job-Drain, Playback-Target, Runtime-Output-Mask), zusätzlich Staged-Layout 14 Tests grün (nur dessen HTTP-Form-Test wegen vorbestehend fehlendem `httpx` ausgelassen). Erst-Review NOT READY: Done-Callback kann später als Await/Drain laufen. Mit Regressionstest rot reproduziert, durch direkte Completion-Reconciliation in `take()` und idempotenten Callback gefixt. Zwei zusätzliche Cancellation-Tests für bereits empfangene bzw. ausgewählte IRs. `py_compile` und `git diff --check` grün. Früherer Vollsweep 396/0/14 in `/tmp/opencode/speaker-capture-full-sweep.log` ist überholt (Start vor Review-Fix, lief über die Code-Änderungen). Sauberer Final-Sweep auf `0d4855f`: 396 bestanden / 0 fehlgeschlagen / 14 übersprungen (`/tmp/opencode/speaker-capture-full-sweep-final.log`, Exit 0); finale 21er-Suite danach separat lokal und `.104` verifiziert.
- Speaker-Align-Trial-Apply-Slice (`ddca782`, Review SHIP für den begrenzten Slice): neue Suite `test_measurement_speaker_apply.py` 17/17 lokal und auf `.104`; dort 14 Suiten / 168 Tests grün (Apply 17, Acquisition 17, Capture-Evidence 21, Speaker-Align 27, Capture-Policy 4, Job-Setup 6, Job-Drain 4, Bank-Target 23, Output-Mask 8, Repeat-Way-Sweeps 8, Playback-Target 14, Analyzer-IR 1, Hybrid 13, Runtime-Output-Mask 5). Keine bestehenden Dateien geändert (AutoSub-`CandidateStager` bewusst unberührt). Review ohne Criticals; sieben Follow-ups in den Commit/Wiring-Slice deferred (Revisions-/Fingerprint-Präsenz, finiter Residual, geshieldetes Restore überall, 3-Wege-Coverage, Threshold-Passthrough, Readback/Lock/Timeout, Doc-Präzision).
- Speaker-Align-Commit-Slice (`d2f11ee`, Review SHIP ohne Criticals): 30 Apply- + 26 Commit-Tests lokal und `.104`; dort 18 Suiten / 289 Tests grün (zusätzlich Candidate-State 31, Candidate-Session 38, Release-Plan 13). Alle 15 Review-Follow-ups (7 Trial-Apply + 8 Commit-Review) vor dem Commit umgesetzt und getestet. `git diff --check` sauber.
- Speaker-Align-Service-Slice (`86b3ffa`, Review SHIP ohne Criticals): neue Suite `test_measurement_speaker_service.py` 14/14 lokal und `.104`; dort 19 Suiten / 303 Tests grün. Service-Review-Follow-ups in den HTTP/Composition-Slice deferred (Frozen-Start-Reuse, Cancel ins erste `propose`, HTTP-Fehler-Taxonomie, echtes `describe`/`create_session`-Wiring inkl. Release-Adapter, Loop-Vertrag, gelockte Reads, strikte JSON-Numerik, `wait_for`-Pacing, Ownership-Release/Retention).
- Speaker-Align-HTTP-Slice (`0f556a0`, Delta-Re-Review SHIP): 25 Service- + 13 API-Tests lokal; `.104` Service 25, Komposition 1, 19 Suiten / 314 grün (Router-Tests dort wegen vorbestehend fehlendem `httpx` ausgelassen). Erst-Review NOT READY mit kritischer `main`-Import-Regression (`NameError` — keine Suite importiert `main`); per Lambda + `import-main`-Verifikation (alle 4 Routen registriert) gefixt. Release-Adapter-Registrierung weiter deferred (dokumentiert).
- Speaker-Align-Frontend-Slice (`f506df1`): neue Suite `test_speaker_align_frontend.js` grün; angrenzende JS-Suiten (`output-mode`, `ui-structure`, `output-state`, `crossover`, `demo-auto-sub-flow`, `poll-interleave`) + `check_router_structure.py` + `git diff --check` grün; Demo neu gebaut, Parität grün; `test_measurement_ui_responsive.py` auf viertes Label aktualisiert (166 Checks grün). Finaler Full-Sweep nach Test-Fix + Release-Slice: lokal 403/0/14 (`/tmp/opencode/final-sweep.log`, Exit 0). JS-Frontend auf `.104` nicht fahrbar (vorbestehend kein `node`/`playwright` dort).
- Speaker-Align-Release-Slice (`5b319d8`): neue Suite `test_measurement_speaker_release.py` 12/12 lokal; angrenzend Service 25, API 13, AutoSub-Release-Plan 13, SR-Session 25 grün; `import main` mit allen 4 Routen grün; `git diff --check` sauber. `.104` (rsync ohne `--delete`, App-Venv, Produkt-App unberührt): Release 12, Service 25, Commit 26, Apply 30, Align 27, Evidence 21, Acquisition 17, AutoSub-Release 13, SR-Session 25, Drain 4, Bank-Target 23, Output-Mask 8, Repeat-Ways 8, Playback-Target 14, Analyzer-IR 1, Hybrid 13, Runtime-Mask 5, Candidate-State 31, Candidate-Session 38, Policy 4, Job-Setup 6, Komposition 1/1, Structure-Check und `import main` (alle 4 Routen) grün — 21 Suiten / 351 Tests + Komposition. Router-Tests dort wegen vorbestehend fehlendem `httpx` ausgelassen (wie im HTTP-Slice).
- Gate-Flip (`00b596b`, freigegeben): TDD-Nachweis mit Wegwerf-Skripten außerhalb des Repos (Repo unberührt) — RED `/tmp/opencode/gate_open_red.py` (gestützte Topologie → 503 auf Altbaum), GREEN `/tmp/opencode/gate_open_green.py` (offenes Gate registriert Service-Job mit `output_state_context` + genau 1 Worker; erste GREEN-Iteration scheiterte real an `main.runtime.dsp_runtime`-Ownership, Skript spiegelt jetzt das Suite-Harness). Reviewfertiger Patch `/tmp/opencode/gate_flip_only.patch` bestand `git apply --check`; in isolierter Kopie angewendet grün (Service-Start 23 inkl. neuem Open-Gate-Test, Leak 3, Runner-IO 28, Winner 11). Nach Commit: lokal fokussiert Service-Start 23, Leak 3, Runner-IO 28, Winner 11, Release-Plan 13, Speaker-Service 25, Speaker-API 13, Speaker-Release 12, Candidate-Session 38, Owner-Prearm 13, Plan-Peak 17, `import main` (4 Speaker- + 3 AutoSub-Routen), Structure-Check grün; Full-Sweep lokal 403/0/14 auf dem Flip-Baum. `.104` (rsync ohne `--delete`, App-Venv, Produkt-App unberührt): Service-Start 23, Leak 3, Runner-IO 28, Winner 11, Release-Plan 13, Speaker-Release 12, Speaker-Service 25, Native-Plan-Peak-Parität PASS, `import main` (Routen-ok), Structure-Check grün. Fehlerjagd ohne Befund (alter Testname ersetzt, 503 nur noch im berechtigten Session-None-Zweig, kein Frontend-/Demo-Bezug aufs Gate). Produktinstanz unversehrt und durchgehend live (dieselben PIDs 3790560/3790866, Produkt-`main.py` mtime 16.09.). Live-Akustik (2-/3-Wege-Sweeps mit Ergebnis + Rollback) unter der Vorgabe — kein zweiter Engine-Prozess parallel zur Produktinstanz — nicht fahrbar: kein isolierter Aufbau, kein Wartungsfenster; keine Sweeps gegen die Produkt-Engine gefahren, keine Staging-/Commit-Eingriffe live. Protokoll-Entwurf: `/tmp/opencode/real_verification_protocol.md`.
- Release-Adapter-Device-Validierung (`6280015`, review-verordnetes Follow-up zum Orphan-Edge): neuer reiner Baustein `measurement/release_device.py` (`check_release_device` — exakter Key-/Channels-/Ports-Abgleich, unauflösbar zählt als gewechselt), optionales `resolve_live_device` in beiden Fabriken (AutoSub + Speaker; ohne Resolver gepinntes Verhalten wie bisher), Produktions-Verdrahtung `_live_release_device_context` in `main.py` für beide `_create_*_release_adapter`. Neue Suite `test_release_device.py` 10/10 (Helper 6, Live-Kontext 2, Fabrik-Verdrahtung beider Adapter 2); Release-Plan 19 (+6: Match/Refusal Key+unauflösbar/Resolver-Typ/Resolver-los/Refusal-ohne-Verkeilung), Speaker-Release 14 (+2: Match/Ports-Refusal). TDD: RED als Modul-Importfehler bzw. 4×/2× `unexpected keyword argument`; Mutationskontrolle ohne Check lässt exakt die 5 Refusal-Tests rot werden; Slot-Clear-Mutation fängt exakt die 2 Clear-Tests (vorbestehender + neuer). Ein Testfehler während GREEN war testseitig (`dict(None)` in der Lambda, Produktion unberührt). Lokal Full-Sweep 404/0/14 auf dem Slice-Baum; `.104` (rsync ohne `--delete`, App-Venv, Produkt-App unberührt und durchgehend live, PIDs 3790560/3790866): Release-Device 10, Release-Plan 19, Speaker-Release 14, Speaker-Service 25, Speaker-Commit 26, Service-Start 23, Winner 11, Acquisition 17, Evidence 21, Align 27, `import main` (Routen-ok), Structure-Check grün.

Für Runner-Änderungen zusätzlich die bestehenden `scripts/test_auto_sub_*`-Suiten (insbesondere alle drei Final-Path-, Confirmation-, Gain-, Polarity- und Cancellation-Suiten) ausführen, numerische Assertions nicht abschwächen. `scripts/run_tests.sh` entdeckt neue `test_*.py` automatisch und nutzt einen XDG-Sandbox. Test-Doubles von `_auto_sub_apply_candidate` müssen die `job`-Kwarg akzeptieren.

### Native / Hardware
- Testhost: `paul@192.168.178.104`; Scratch: `/home/paul/fxroute-mc-build`.
- Interpreter: `/home/paul/fxroute/.venv/bin/python3` (nur verwenden, Produkt-App nicht verändern).
- Quellen in Scratch synchronisieren (`rsync -a --exclude=.git --exclude=__pycache__ --exclude=node_modules`, kein `--delete`).
- Lokal fehlen Native-Build-Abhängigkeiten; Native-Suiten auf `.104` prüfen. Neue Native-Suite in `NATIVE_HELPER_TESTS` in `scripts/run_tests.sh` registrieren.
- Bekannte `.104`-Umgebungslücken (vorbestehend, nicht regressionsverdächtig): `httpx`, `PIL`, `node`, `playwright` fehlen; kein Git-Verlauf im Scratch; ein qbzd-Shell-Env-Problem. Vollsweep dort: 291/13/98 mit genau diesen Ursachen.
- Keine zweite Live-Engine neben der Produktinstanz starten (Node-Namenskollision). Reale akustische Verifikation erfordert abgestimmtes Wartungsfenster/isolierten Aufbau.

## Backend-v2-Migration Task 0 Audit (2026-09-18)

Read-only-Audit der Legacy-Mode-/Routing-Call-Sites. Jede Site genau ein Verdikt:
`migrate Task N` oder `keep (Grund)`. Grep-Basis: Step-1-/Step-2-Befehle aus dem
Plan (Stand `680f16f`), verfeinert um `saved_routing_state`/`restore_audio_output_mode_raw`/
`output_route_pairs`/`set_bass`/beide POST-Routen. `__pycache__`-Treffer ignoriert.

### Reader

- `audio/samplerate/constants.py:13-19` (Modus-Definitionen): keep (Algorithmuslabels + Validierungsmenge; nur Persistenz geht).
- `audio/samplerate/__init__.py:32-37,171-176` (Konstanten-Re-Exporte): keep (Labels; Persistenz-Re-Exporte entfallen mit Task 4).
- `audio/samplerate/overview.py:27-31` (Konstanten-Imports): keep bis Task 4 (vom zu loeschenden Payload-Code benoetigt).
- `audio/samplerate/overview.py:392-405` (Overview-Notes + `routing_status` aus Mode-Label): migrate Task 3 (Anzeige/Topologie aus v2-Plan-Rollen; Port-Discovery keep).
- `audio/samplerate/overview.py:633-640` (`_output_mode_label`): migrate Task 4 (Display-Helper stirbt mit der Persistenz).
- `audio/samplerate/overview.py:666-707` (`set_audio_output_selection` Device-Mode-Fallback + Remember): migrate Task 4 (Device-Wechsel appliziert via OutputService; Selektion selbst keep).
- `audio/samplerate/overview.py:709-743` (`prepare_audio_output_mode` Validierung + Overlay): migrate Task 5 (Routen-Body entfällt; Validierung wandert in den v2-Apply).
- `audio/samplerate/persistence.py:200-245,322ff` (`_load/_build` Normalisierung): migrate Task 4 (delete).
- `audio/samplerate/persistence.py:267-291` (`read/restore_audio_output_mode_raw`): migrate Task 4 (delete; Verbraucher Task 2/5 zuerst auf Revisionen).
- `audio/output_routing.py:37-51` (`routing_payload` assignments/customized/inactive): migrate Task 4 (Overview braucht es nicht mehr; reine Port-Ableitung via `output_ports` keep).
- `audio/output_routing.py:71-84` (`saved_routing_state`, `all_saved_routes`): `saved_routing_state` migrate Task 2 (Snapshot -> v2-Revision), danach delete Task 4; `all_saved_routes` keep bis Task 4 (Einmal-Loader `main._legacy_output_snapshot`).
- `audio/output_routing.py:104-109` (`output_route_pairs`): migrate Task 3 (Plan-`physical_routes`).
- `audio/output_ports.py:221-256` (`hardware_playback_ports*_from_mode`): keep (Hardware-Discovery, planseitig explizit unberuehrt).
- `playback/orchestration.py:471,502,508-523` (Diagnose `mode_minimum`, `output_routing.available`, `route_pairs`-Fallback, `planned_routes`): migrate Task 3 (Counts/Paare aus Plan-Topologie; Port-Discovery keep).
- `playback/orchestration.py:730` (Repairable-Gate auf Mode-Membership): migrate Task 3 (Gate auf Diagnose-Routen/Topologie).
- `playback/orchestration.py:863` (Effects-Stage Mode-Read): migrate Task 3 (ungenutztes Label; `v2_pending` gatet bereits).
- `playback/runtime/deps.py:51` (`persist_audio_output_mode`-Feld): migrate Task 2 (entfällt nach Coordinator-Migration).
- `playback/runtime/output_mode.py:145-172` (Rollback Alt-Mode-Rekonstruktion inkl. Sub-Blöcke): migrate Task 2.
- `playback/runtime/output_mode.py:296-299` (Finalize-Gate `mode in SUBWOOFER_MODES and v2.target None`): migrate Task 2 (Gate auf v2-Target/Topologie).
- `playback/runtime/snapshot.py:56-62` (Session-Graph-Reconcile-Verzweigung): migrate Task 3 (Repair-Pfad aus Topologie/Diagnose).
- `playback/runtime/verification.py:220-227` (Stabilisierungs-Repair-Verzweigung): migrate Task 3.
- `playback/runtime/verification.py:417-430` (Commit-Helper-Readback, gated auf Mode-Label): migrate Task 3 (Helper-Pflicht aus Plan-Rollen).
- `playback/silent_active.py:72-101` (Mode-Check + `output_route_pairs` mit `signal <= 2`): migrate Task 3 (Rollen + Plan-Routen; Port-Discovery keep).
- `playback/silent_active.py:139-144` (Snapshot Mode-Echo): migrate Task 3 (Snapshot trägt Topologie).
- `playback/transition/models.py:100-102` (`output_mode_config/routing_config/output_mode_target`): migrate Task 2 (write-never-Felder loeschen, sobald kein Reader mehr).
- `dsp/orchestration.py:115-125` (`with_subwoofer_derived_delays` Mode-Gate): migrate Task 3 (Delays aus Plan).
- `dsp/orchestration.py:855-883` (Link-Watcher `cheap_mode`/Overview-Gates): migrate Task 3 (Gate aus Runtime-Config/v2-Topologie).
- `dsp/runtime.py:170-241` (`BassManagementConfig.from_overview`): migrate Task 3 (Verbraucher auf Plan; Struct-Nutzung Messkontext bis dahin keep).
- `dsp/runtime.py:310-399` (`DSPRuntimeConfig.from_overview`, `_resolve_ports`-Verzweigung, `output_route_pairs`-Nutzen): migrate Task 3 (`from_plan` only; Port-Discovery keep).
- `dsp/runtime.py:821-850` (`_resolve_hardware_ports` + `_sync` via `from_overview`): `_sync`-Pfad migrate Task 3 (`sync_rendered`); `_resolve_hardware_ports` keep (Discovery).
- `measurement/autosub/runners/start.py:75-88,98,166,209-227,267-308` (`algorithm_mode`-Map, Snapshot-`mode`, `output_state_context`, Stereo/22-Dispatch): keep (reine Dispatch/Display-Labels aus gefrorener Topologie).
- `measurement/autosub/runners/optimize.py`, `optimize_22.py`, `optimize_22_stereo.py` (alle `OUTPUT_MODE_*`-Konstanten, `output_mode=`/`mode=`-Kwargs, Gain/Scoring-Mode-Args, `_auto_sub_22_verify_*`-Args, Service-Branch `BassManagementConfig(...)`-Display-Konstruktion): keep (Algorithmuslabels; Service-Branch-Konstruktion display-only ohne Persistenz).
- `measurement/autosub/candidates.py:85,209,454-456` (Legacy-Payload-Shape-Verifikation): keep bis Task 4 (Legacy-Payload-Validierung; entfällt mit der Persistenz).
- `measurement/autosub/measurement.py:92,143,404,441,450,459` u. Fingerprint/Verify-Helper (Mode-Verzweigungen, Defaults `261/1899`, Gain `1280/1289/1317/1716/1785/1948/2051`): keep (Algorithmus-Dispatch + Fingerprint/Scoring-Mathematik, unverändert lassen).
- `measurement/autosub/scoring.py:280,288,299`: keep (Scoring-Mathematik, unverändert lassen).
- `measurement/session.py:996-1011,1014-1066` (Sweep-Log-Gate, Save-Kontext, Sweep-Sync-Gate auf Mode-Label): migrate Task 3 (aus Plan-Topologie/Rollen ableiten).
- `measurement/routing.py:354-364` (Mode-String + `from_overview`-Layout): migrate Task 3 (Route aus gestagtem Plan-Layout; Mode-Echo aus Topologie).
- `measurement/store.py:1287` (Electrical-Reference-Keep-Gate): migrate Task 3 (Gate auf Plan-Rollen).
- `main.py:430-434` (Konstanten-Imports): keep (Labels).
- `main.py:1460` (`api_mode` im Debug-Dump): migrate Task 5 (Debug-Payload auf v2-Topologie mit Routen-Entfernung).
- `main.py:2679-2692` (`_current_output_mode` mit Persistenz-Fallback): migrate Task 3 (Gate aus Runtime-Config; Fallback entfällt Task 4).
- `main.py:2731` (`get_output_mode`-Verdrahtung): migrate Task 3 (mit Watcher-Gate).
- `main.py:3930-3943` (`_legacy_output_snapshot` via Raw-Mode + `all_saved_routes`): keep (Einmal-Migrations-Loader; delete Task 4 nach Fenster).
- `static/measurement_flows.js`: keep (keine Legacy-Routen-Calls).
- `static/output_state.js`, `static/app.js` (output-state-Endpunkte): keep (v2-Pfad).
- `scripts/check_test_xdg_isolation.py:38-43` (Writer/Routen-Auditliste): migrate Task 4/5 (mit den Delete-Slices aktualisieren).

### Writer (Produktion, ohne Tests)

- `audio/samplerate/overview.py:681` (Device-Switch-Persist): migrate Task 4.
- `audio/samplerate/overview.py:745` (`persist_audio_output_mode`-Def): migrate Task 4 (delete).
- `audio/samplerate/overview.py:765` (`set_audio_output_mode`-Def): migrate Task 4 (delete; Task-1-Caller zuerst umgezogen).
- `audio/samplerate/persistence.py:279` (`restore_audio_output_mode_raw`): migrate Task 4 (delete; Same-Mode-Restore wandert Task 5 nach `revert`).
- `audio/output_routing.py:61` (`save_assignments`): migrate Task 2 (Commit -> `OutputService.commit`), delete Task 4.
- `audio/output_routing.py:87` (`restore_routing_state`): migrate Task 2 (Rollback -> `revert`), delete Task 4.
- `playback/runtime/output_mode.py:61,65` (Commit-Persist + Save): migrate Task 2.
- `playback/runtime/output_mode.py:139,159` (Rollback-Restore + Alt-Config-Re-Persist): migrate Task 2.
- `playback/runtime/snapshot.py:92` (Routing-Snapshot): migrate Task 2 (v2-Vorgänger-Revision).
- `measurement/autosub/candidates.py:338` (Legacy-Branch `set_audio_output_mode`): migrate Task 1.
- `measurement/autosub/measurement.py:417-430` (Kandidaten-Config `set_audio_output_mode` + `_load`-Verify): migrate Task 1.
- `measurement/autosub/runners/optimize.py:640,694,738,770,814,981`, `optimize_22.py:599,631,678,821`, `optimize_22_stereo.py:905,1057,1130,1153,1208,1306,1338` (Legacy-`else`-Branches `set_audio_output_mode` + DSP-Sync + `_load_audio_output_mode` als `load_overview`): migrate Task 1.
- `main.py:893` (Deps-`persist`-Lambda): migrate Task 2.
- `main.py:4756,4770` (Same-Mode-Routen-Body Persist/Restore): migrate Task 5 (entfällt mit der Route).
- `main.py:4679,4718` (POST `/api/audio/output-routing`, `/api/audio/output-mode`): migrate Task 5 (delete).
- `main.py:4124-4129` (`set_bass`-Mutation): migrate Task 5 (delete).
