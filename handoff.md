# Handoff — Multichannel / Task 8

## Arbeitsstand
- Worktree: `/home/pbclaw/ai/projects/fxroute-multichannel`
- Branch: `feature/multichannel-crossover`
- HEAD: `b5ac9ed` (Slice-F-Code; darüber nur Handoff-Commits), Arbeitsbaum sauber.
- Tasks 1–7 abgeschlossen einschließlich Task-7-Review und Nacharbeiten. Task 8 ist **teilweise implementiert, nicht produktiv vollständig verdrahtet**.
- AutoSub-Slices A–F sind implementiert und committet (B zusätzlich reviewed; F durch zwei externe Review-Runden mit reproduzierten und behobenen Defekten). Offen: **G (Session-Release), Speaker Align**.
- Der AutoSub-HTTP-Start ist ausdrücklich gesperrt (`_AUTO_SUB_SERVICE_INTEGRATION_READY = False` in `measurement/autosub/runners/start.py:68`, HTTP 503 bei unterstützter Topologie), bis Slice G integriert und verifiziert ist. Kein HTTP-/Environment-Override; Tests öffnen das Gate nur per Patching.

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

## Architektur — bei Weiterarbeit beachten
- Routing ist Topologie-Autorität; Legacy-Modi dürfen höchstens Algorithmuslabels bleiben. Scoring-, Scan-, Polarity-, Gain- und Confirmation-Mathematik unverändert lassen.
- Ein gefrorener Startzustand pro Job; alle Vorschläge vollständig und startrelativ. Kein Rebase auf eine neuere Revision, kein Persistieren einzelner Scan-Kandidaten.
- Service- und Legacy-Pfade laufen nebeneinander: Service-Jobs erkennt man an `"output_state_context" in job`. Legacy-Zeilen in Runnern/Funnel nur anfassen, wenn bestehende Suiten den Unterschied beweisen; `*_audio_output_mode`-Bindings (Runner-Modul, `candidates`, `measurement`, `samplerate`-Quelle für funktionslokale Imports) sind je eigene Patch-Ziele.
- Nur der final akustisch akzeptierte, erneut verifizierte Vorschlag darf einmalig committed werden — Slice F ist implementiert: `commit_staged` (Owner) + `_commit_auto_sub_service_winner` (Runner-Helper) + Aufruf vor Completion. `commit_winner(proposal)` bleibt für explizite Proposal-Commits; Runner nutzen `commit_staged` (kein Triplet-Resend).
- `CandidateStager` nutzt `guarded_rebuild_rendered`, nicht unguarded `sync_rendered`. Readback muss echte Links (`runtime.verify()`), Prozess-/Planidentität und Gain prüfen. Externe Graph-Ownership muss die Integration serialisieren; Revisionschecks allein machen Store und Engine nicht atomar.
- Nach Winner-Commit niemals über den alten Stager den Startzustand wiederherstellen (Cleanup skippt committete Owner). Captures vor jedem Restore über `MeasurementStore.drain_job` drainen; Exact-Mute-Restore nach dem Drain, Drain-Fehler danach propagieren.
- Der Predictor modelliert nur die Per-Output-Kette; Aufrufer brauchen einen neutralen Global-Pfad (dokumentiert in `jobs.py`).

## Offen — empfohlene Reihenfolge
**G → Speaker Align.** Nicht erneut mit Grundlagen beginnen.

- **Rollout-Gate:** `_AUTO_SUB_SERVICE_INTEGRATION_READY = False` in `runners/start.py` erst nach G und dessen Integrations-Gate entfernen. Kein HTTP-/Environment-Override. Die Tests öffnen das Gate ausschließlich lokal über Patching.
- **G — Session-Release:** bei Rate-/Playback-Restore den aktuellen committed Output-Plan laden, nicht den Legacy-Overview-Graph (`session.py`-Release-Boundary, Playback-Coordinator, verzögerter Window-Close/Resume). Tests: `test_autosub_session_release_plan.py`. Hinweis: F liefert `committed_revision` im `output_state_context` und `service.load()` enthält den committeten Plan.
- **Speaker Align (Task 8):** fehlt vollständig. Feste Mikrofonposition, gemeinsame upstream Referenz, unverschobenes IR-Timing, relative Delays `max(t)-t`, Overlap-Band-/Phasenprüfung, Qualitäts-/Stale-Gates; synthetische und reale Verifikation laut Gesamtplan.

### Bekannte Restprobleme / Grenzen
- Die G-Release-Anbindung fehlt (letzter AutoSub-Block vor Speaker Align).
- Vorbestehend, nur auf `.104` sichtbar: `test_auto_sub_fine_winner_apply.py` schreibt `config/fxroute/audio-output-mode.json` in den XDG-Canary (echte Hardware lässt den ungepatchten Legacy-Setter persistieren; lokal wirft er vorher). Harmlos unter Sandbox; der richtige Fix gehört zu Slice E-Nacharbeiten (keine Legacy-Setter mehr im Runner-Pfad; der Funnel/Commit-Pfad ist sie inzwischen). Details im Integrationsplan.
- Speaker Align fehlt. Keine Behauptung, Task 8 oder Crossover-AutoSub sei end-to-end fertig.
- Bekannte Altgrenzen: Store↔Engine-TOCTOU ohne gemeinsame Ownership, Legacy-Checks in `silent_active` bei exotischen v2-Routings.

## Gezielte Verifikation
Direkt ausführbare unittest-Skripte; Exitcodes prüfen, nicht durch `| tail` verdecken.

```bash
python3 scripts/test_autosub_role_mapping.py
python3 scripts/test_auto_sub_role_mute.py
python3 scripts/test_autosub_candidate_state.py
python3 scripts/test_autosub_candidate_session.py
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
git diff --check
```

Zuletzt fokussiert grün: Service-Start 23, Start-Leak 3, Dependency-Injection 11, Candidate-Session 38, Winner-Commit 11, Worker-Lifecycle 5, Job-Drain 4, Owner-Prearm 13, Runner-Service-IO 28, Plan-Peak 17, Peak-Prediction 5, Gain-Apply-Revert 23, Staged-Layout 15, Playback-Target 14, Capture-Policy 4, Job-Setup 6, Runtime 62, Output-Lifecycle 8, alle 46 `test_auto_sub_*`-/`test_autosub_*`-/Drain-Suiten. Vollständiger `run_tests.sh`-Sweep nach Slice F: lokal 393 bestanden / 0 fehlgeschlagen / 14 übersprungen (Baseline ohne F-Änderungen: 390/0/14; native Helper-Suiten lokal geskipt). `.104`-Verifikation für Slice F gefahren (rsync ohne `--delete` nach `/home/paul/fxroute-mc-build`, App-Venv, Produkt-App unberührt): fokussiert Winner-Commit 11, Worker-Lifecycle 5, Job-Drain 4, Owner-Prearm 13, Runner-Service-IO 28, Candidate-Session 38 (99/99 grün); alle 46 `test_auto_sub_*`-/`test_autosub_*`-Unittest-Suiten grün plus Mutation-Audit PASS (Exit 0); Mess-Spotchecks Playback-Target, Capture-Policy, Job-Setup, Fine-Winner-Apply grün, einziger Fehler Staged-Layout am bekannten `httpx`-Import-Gap (identisch zu D/E, Datei von F unberührt); nativ 11/11 Helper + 3/3 C grün inkl. Plan-Peak-Parität.

Für Runner-Änderungen zusätzlich die bestehenden `scripts/test_auto_sub_*`-Suiten (insbesondere alle drei Final-Path-, Confirmation-, Gain-, Polarity- und Cancellation-Suiten) ausführen, numerische Assertions nicht abschwächen. `scripts/run_tests.sh` entdeckt neue `test_*.py` automatisch und nutzt einen XDG-Sandbox. Test-Doubles von `_auto_sub_apply_candidate` müssen die `job`-Kwarg akzeptieren.

### Native / Hardware
- Testhost: `paul@192.168.178.104`; Scratch: `/home/paul/fxroute-mc-build`.
- Interpreter: `/home/paul/fxroute/.venv/bin/python3` (nur verwenden, Produkt-App nicht verändern).
- Quellen in Scratch synchronisieren (`rsync -a --exclude=.git --exclude=__pycache__ --exclude=node_modules`, kein `--delete`).
- Lokal fehlen Native-Build-Abhängigkeiten; Native-Suiten auf `.104` prüfen. Neue Native-Suite in `NATIVE_HELPER_TESTS` in `scripts/run_tests.sh` registrieren.
- Bekannte `.104`-Umgebungslücken (vorbestehend, nicht regressionsverdächtig): `httpx`, `PIL`, `node`, `playwright` fehlen; kein Git-Verlauf im Scratch; ein qbzd-Shell-Env-Problem. Vollsweep dort: 291/13/98 mit genau diesen Ursachen.
- Keine zweite Live-Engine neben der Produktinstanz starten (Node-Namenskollision). Reale akustische Verifikation erfordert abgestimmtes Wartungsfenster/isolierten Aufbau.
