# Handoff — Multichannel / Task 8

## Arbeitsstand
- Worktree: `/home/pbclaw/ai/projects/fxroute-multichannel`
- Branch: `feature/multichannel-crossover`
- Implementierungsstand vor diesem Handoff-Commit: `b7defd6bee9f96e36c3e4c01d0c008c81fd7a552` (`b7defd6`).
- Tasks 1–7 abgeschlossen einschließlich Task-7-Review und Nacharbeiten. Task 8 ist **teilweise implementiert, nicht produktiv vollständig verdrahtet**.
- Dieser Commit aktualisiert ausschließlich den Handoff.

## Maßgebliche Pläne
Die bestehenden Pläne sind maßgeblich. **Keine Neuplanung oder erneute Implementierung bereits abgeschlossener Arbeiten.**
- `docs/superpowers/plans/2026-09-16-multichannel-spec.md`
- `docs/superpowers/plans/2026-09-16-multichannel-crossover.md` — Gesamtplan, Task 8 und Ausführungsprotokoll.
- `docs/superpowers/plans/2026-09-17-autosub-service-integration.md` — verbleibende AutoSub-Integration, Slices A–G.

Das Planverzeichnis ist absichtlich lokal Git-ignoriert; Dateien liegen im genannten Worktree. Nicht die Exclusion ändern. Eine neue Session soll denselben Worktree verwenden und die Pläne dort lesen.

## Erledigt bis Task 8 / b7defd6
Tasks 1–7 liefern Routing-/Rollenmodell, pro Modus isolierten atomaren Output-State, A/B-Bänke, Processing-Plan, Crossover-SOS und native Output-Bank-Ausführung, transaktionale Anwendung/API, UI und bereichsgebundene Messungen samt PEQ/FIR-Commit-Gate. Task-7-Nacharbeiten umfassen Summed-Sub-Klassifikation, Repeat-Sperre für einseitige Bereiche, gespeicherte Bereichs-Badges und Entfernung ungenutzter Auswahlzustände.

### Fünf implementierte Task-8-Bausteine
1. **`414d888` — Routing-Adapter** (`measurement/autosub/roles.py`): Topologie → Optimizer-Pfad; none/mono/dual-mono/stereo → unavailable/single-sub/dual-sub/stereo-subs. Mixed Sub 1/R ist dual-mono; einzelnes `sub_l` ist mono. Rollenbasierte Indizes/Masken und Seitenlisten; Crossover-Main umfasst alle Wege der Seite. Engine-Reihenfolge ist kanonisch, nicht Hardware-Zuweisungsreihenfolge.
2. **`9e0eb27` — Rollenadressiertes Exact-Sub-Mute**: `DSPRuntime.set_exact_sub_mute(enabled, *, mask=None)`; ohne Maske bleibt Legacy 3/4. Der Candidate-Funnel reicht die explizite Maske durch Remeasurement und stellt den vorherigen Zustand wieder her. Peak-Zeroing kopiert Daten und lehnt nicht modellierte Outputs ab. Folgefix in `24b7de2`: gemeinsamer Lock serialisiert Maskenentscheidung, Control-ACK und Zustand; überlappende Output-/Exact-Masken bleiben erhalten.
3. **`24b7de2` — In-Memory Candidate-Stager** (`measurement/autosub/candidate_state.py`): `sub_candidate_state`, `compile_candidate`, `CandidateStager.stage()/restore()`. Eingefrorener Startzustand/Revision, expliziter Geräte-/Kanalkontext, nur geroutete Sub-Trims und Bass-Felder; Bänke und relative Speaker-Way-Delays bleiben erhalten. Guarded Stage, Readback, revisionsgeschützte und cancellation-sichere Wiederherstellung. Keine Persistenz im Stager. Runtime-Ramp-Ziele sind explizit; Recovery überschießt nicht auf 0 dB.
4. **`cd228f5` — Job-scoped Candidate-Session (Slice A)** (`measurement/autosub/candidate_session.py`, `deps.py`): `AutoSubProposal` verlangt vollständige absolute Sub-Maps plus beide Bass-Felder. Session-Konstruktion inert; `activate(rate)` synchron; `stage(proposal)`, `ensure_ready(rate)`, `commit_winner(proposal)`, `restore()` asynchron. Commit prüft erneut Runtime-Aktivität, Fingerprint, Rate, endlichen Gain und Revision, dann synchroner `service.commit`. Nach Commit ist der alte Stager retired. Sweep-Layout stammt vom tatsächlich gerenderten Target, keine erneute IR-Auflösung nach Stage. Owner-Registry liegt außerhalb serialisierter Job-Dicts. Neue Dependency-Hooks sind noch nicht in `main.py` verdrahtet.
5. **`b7defd6` — Staged-Layout im realen Capture-Pfad (Teil von Slice D)**: interne Parameter `expected_native_layout`, `expected_native_output_mode`, `expected_plan_fingerprint` an `MeasurementStore.start_measurement`; all-or-none, nur `raw_helper`, vor erstem Await tief kopiert. Weitergabe über Child-Job und `host_capture.py` an `routing.py`. Staged-Kontext ersetzt dort Legacy-Overview-Ableitung. Rate/Mode/Layout/Bypass/Aktivität/Fingerprint werden auch bei übersprungener Diagnose geprüft; Mismatch blockiert vor Audio-Prozessstart. Keine HTTP-Override-Schnittstelle; Legacy-Aufrufe unverändert. **Owner-Prearm-Teil von D bleibt offen.**

## Architektur — bei Weiterarbeit beachten
- Routing ist Topologie-Autorität; Legacy-Modi dürfen höchstens Algorithmuslabels bleiben. Scoring-, Scan-, Polarity-, Gain- und Confirmation-Mathematik unverändert lassen.
- Ein gefrorener Startzustand pro Job; alle Vorschläge vollständig und startrelativ. Kein Rebase auf eine neuere Revision, kein Persistieren einzelner Scan-Kandidaten.
- Nur der final akustisch akzeptierte, erneut verifizierte Vorschlag darf einmalig committed werden. Die Session besitzt den Commit-Mechanismus; die Runner-/Lifecycle-Anbindung fehlt noch.
- `CandidateStager` nutzt `guarded_rebuild_rendered`, nicht unguarded `sync_rendered`. Readback muss echte Links (`runtime.verify()`), Prozess-/Planidentität und Gain prüfen. Externe Graph-Ownership muss die Integration serialisieren; Revisionschecks allein machen Store und Engine nicht atomar.
- Nach Winner-Commit niemals über den alten Stager den Startzustand wiederherstellen. Capture drainen und Masken/Scopes aufräumen, bevor Messsession/Ownership freigegeben werden.

## Offen — empfohlene Reihenfolge
**B → C → restliche Runner-/Prearm-/Winner-Commit-/Release-Anbindung → Speaker Align.** Nicht erneut mit Grundlagen beginnen.

- **B — Authoritative Start-Route:** `runners/start.py` und `main.py`-Composition auf Service/Owner verdrahten; Topologie-Gate und logische Sub-Slot→Rollen-Zuordnung; Owner nach Measurement-Entry am festen Messrate aktivieren. Keine unvollständige neue Route für reale Sweeps freischalten.
- **C — Compiled-Layout Peak-Predictor:** tatsächliche Output-Anzahl, Routen, SOS/PEQ, FIR, Delay/Trim/Polarity, Runtime-/Sink-Gain berücksichtigen; Cache passend zum Processing/IR-Inhalt; Native-PCM-Parität prüfen.
- **D Rest — Owner-Prearm:** staged Graph nur auf Rate, Gerät, Links und Fingerprint prüfen; nicht aus Legacy-Persistenz neu bauen. Capture-Kontext-Transport ist bereits erledigt (`b7defd6`).
- **E — Alle Runner-IO-Grenzen:** Candidate-Apply/Readback/Restore und direkte Setter in `optimize.py`, `optimize_22.py`, `optimize_22_stereo.py`, `candidates.py`, `measurement.py` auf Owner umstellen. Vollständige Vorschläge, rollenbasierte Main-only-Masken, fataler Abbruch bei Ownership-/Revisionsverlust.
- **F — Winner-Commit und Lifecycle:** final behaltenen Vorschlag nach bestehenden akustischen Gates erneut stage/verifizieren, einmalig committen; Cancellation und Fehler drainen/restoren vor Unregister/Lock-Release.
- **G — Session-Release:** bei Rate-/Playback-Restore den aktuellen committed Output-Plan laden, nicht den Legacy-Overview-Graph. Auch verzögerten Window-Close und Coordinator-Resume abdecken.
- **Speaker Align (Task 8):** fehlt vollständig. Feste Mikrofonposition, gemeinsame upstream Referenz, unverschobenes IR-Timing, relative Delays `max(t)-t`, Overlap-Band-/Phasenprüfung, Qualitäts-/Stale-Gates; synthetische und reale Verifikation laut Gesamtplan.

### Bekannte Restprobleme / Grenzen
- AutoSub-Start und Runner verwenden noch Legacy-Mode-Strings und `set_audio_output_mode`-Persistenz.
- Pre-Arm (`measurement/session.py`) rekonstruiert aus persisted state und würde staged Kandidaten überschreiben.
- Peak-Predictor modelliert weiterhin genau vier Outputs; neue Mute-Helfer erweitern dieses Modell nicht.
- Winner-Commit-Wiring in den Runnern und vollständige Cleanup-/Release-Anbindung fehlen.
- Speaker Align fehlt. Keine Behauptung, Task 8 oder Crossover-AutoSub sei end-to-end fertig.
- Bekannte Altgrenzen: Store↔Engine-TOCTOU ohne gemeinsame Ownership, Legacy-Checks in `silent_active` bei exotischen v2-Routings.

## Gezielte Verifikation
Direkt ausführbare unittest-Skripte; Exitcodes prüfen, nicht durch `| tail` verdecken.

```bash
python3 scripts/test_autosub_role_mapping.py
python3 scripts/test_auto_sub_role_mute.py
python3 scripts/test_autosub_candidate_state.py
python3 scripts/test_autosub_candidate_session.py
python3 scripts/test_autosub_dependency_injection.py
python3 scripts/test_measurement_staged_layout.py
python3 scripts/test_measurement_playback_target.py
python3 scripts/test_measurement_capture_policy.py
python3 scripts/test_measurement_job_setup.py
python3 scripts/test_dsp_runtime.py
python3 scripts/test_output_state_lifecycle.py
git diff --check
```

Zuletzt fokussiert grün: Rollen 23, Mute 30, Stager 31, Session 38, Dependencies 11, Staged-Layout 15, Playback-Target 14, Capture-Policy 4, Job-Setup 6, Runtime 62, Output-Lifecycle 8. Läufe erfolgten je Baustein, nicht als neuer kompletter Sweep nach b7defd6. Erwartete Fehler-Fixtures loggen Tracebacks; einige ältere Suiten Warnungen.

Für Runner-Änderungen zusätzlich die bestehenden `scripts/test_auto_sub_*`-Suiten (insbesondere alle drei Final-Path-, Confirmation-, Gain-, Polarity- und Cancellation-Suiten) ausführen, numerische Assertions nicht abschwächen. `scripts/run_tests.sh` entdeckt neue `test_*.py` automatisch und nutzt einen XDG-Sandbox; letzter vollständiger historischer Stand vor Task 8: 381 bestanden / 0 fehlgeschlagen / 13 übersprungen — **kein aktueller Vollsweep-Nachweis**.

### Native / Hardware
- Testhost: `paul@192.168.178.104`; Scratch: `/home/paul/fxroute-mc-build`.
- Interpreter: `/home/paul/fxroute/.venv/bin/python3` (nur verwenden, Produkt-App nicht verändern).
- Quellen in Scratch synchronisieren; `.git`, `__pycache__`, `node_modules` ausschließen, kein `--delete`.
- Lokal fehlen Native-Build-Abhängigkeiten; Native-Suiten auf `.104` prüfen. Neue Native-Suite in `NATIVE_HELPER_TESTS` in `scripts/run_tests.sh` registrieren.
- Keine zweite Live-Engine neben der Produktinstanz starten (Node-Namenskollision). Reale akustische Verifikation erfordert abgestimmtes Wartungsfenster/isolierten Aufbau.
