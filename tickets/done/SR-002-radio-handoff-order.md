# Ticket: SR-002

## Project
FXRoute

## Goal
Prevent stale asynchronous MPV/player-state callbacks from undoing a completed Radio ↔ Library sample-rate handoff, while preserving the required full Peak-Monitor restart on genuine playback-context changes.

## Scope
- Local source only: `/home/pbclaw/ai/projects/fxroute`
- Do not deploy, restart, or modify `.104`.
- Do not remove or bypass the Peak-Monitor restart. It may be required to restore PipeWire links after a source/context change.
- Serialize/version playback transitions so stale callbacks cannot apply samplerate, EasyEffects, subwoofer, or peak-monitor decisions for an older context.
- Bind deferred post-playback synchronization to the requested/current transition context.
- Harden `peak_monitor.stop()` only as a narrowly related race guard if needed (`ProcessLookupError`/`TimeoutError` around process shutdown).

## Acceptance criteria
1. Radio → local 48-kHz playback cannot be followed by a stale Radio callback that forces 44.1 kHz.
2. Local → Radio remains correctly synchronized at 44.1 kHz.
3. A genuine source/context change still performs the full Peak-Monitor restart and restores its target/links.
4. Same-source pause/resume retains the existing relink optimization.
5. Existing sample-rate and playback tests remain passing; add focused regression coverage for stale callback invalidation.
6. No live deployment in this ticket.

## Evidence
Live `.104` journal at 2026-08-01 08:14:44–08:14:49 showed:
- local handoff completed at 48 kHz before `/api/play` returned 200;
- afterward a stale `player:radio:/home/paul/Music/incoming/DAFT PUNK - Around the TESLA COILS.webm` callback forced 44.1 kHz;
- status repair then restored 48 kHz.
Peak-monitor restart must remain intact.

## Status
done

## Changes
- Added a monotonic playback-transition generation in `main.py`. Odd values
  identify an in-flight explicit play handoff; even values identify a committed
  playback context.
- Player-state callbacks capture their generation and serialize peak/context
  application with `source_transition_lock`. A callback queued before or during
  Radio ↔ Library handoff is discarded after the handoff instead of applying or
  broadcasting its old track, samplerate, EasyEffects, or peak-monitor context.
- Deferred peak-monitor synchronization now carries the committed generation,
  revalidates it after settling, and serializes final application with the same
  handoff lock.
- Deferred samplerate-recovery and subwoofer-runtime synchronization now carry
  that same committed generation and revalidate it after their asynchronous
  settling/rate steps. Force-rate changes and final runtime mutations are
  serialized with the source transition lock, without holding the lock during
  unrelated settling waits, so an older task cannot mutate a newer playback
  context.
- `sync_peak_monitor_for_playback_state` revalidates the generation around its
  asynchronous samplerate/preset/graph steps. Genuine context changes retain
  the full Peak-Monitor restart; same-context pause/resume retains relink-only
  behavior with restart fallback.
- Added `scripts/test_radio_handoff_generation.py` covering stale Radio callback
  invalidation, full restart and correct source-rate resolution in both handoff
  directions, the same-source relink optimization, stale deferred
  samplerate/subwoofer task invalidation, and AST coverage ensuring deferred
  context synchronizers receive an explicit generation.
- `peak_monitor.stop()` was left unchanged: the generation/serialization fix
  addresses the demonstrated race without broadening shutdown behavior.

## Validation
- `python3 scripts/test_radio_handoff_generation.py` — 7 tests passed.
- `python3 scripts/test_peak_monitor_samplerate.py` — passed.
- `python3 scripts/test_subwoofer_runtime.py` — 11 tests passed; expected
  mock/missing-helper diagnostics only.
- `python3 scripts/test_measurement_sr_session.py` — 16 tests passed.
- `python3 -m py_compile main.py peak_monitor.py scripts/test_radio_handoff_generation.py` — passed.
- `python3 scripts/test_native_helper_alignment.py` — blocked because the local
  built helper `pipewire_stage1/build/fxroute_21_passthrough` is absent; no
  deployment or remote host was used.
- `git diff --check` — passed.

## Deployment und Live-Abnahme

- Lokaler Fix-Commit: `393244cfe7e820b4606d58201c804a7bf61caaaf`
- Deployment nach `paul@192.168.178.104:/home/paul/fxroute` durchgeführt.
- Remote-Datei-Hash stimmt mit dem lokalen Commit überein:
  `5d2b3cef0a657b082a35438178b5fdb22be6432d9c90b130ae0ec0138f09dfa3`
- Vorhandene Remote-Datei gesichert als `main.py.bak-sr002-predeploy-20260801-0852`.
- User-Service `fxroute.service` nach Neustart `active/running`, MainPID `916549`.
- API `/api/status` und `/api/audio/samplerate` erfolgreich.
- Kontrollierte Live-Abnahme erfolgreich: Local 48 kHz → Radio 44,1 kHz → Local 48 kHz.
- POST `/api/stop` erfolgreich; Wiedergabe anschließend gestoppt.
- `.104` blieb auf seinem bestehenden Git-HEAD; nur die geprüfte `main.py` wurde deployt.

## Nachbeobachtung 2026-08-01 10:01

- Im pausierten Zustand fällt der Hardware-Sink erwartungsgemäß auf die konfigurierte Default-Rate von 44,1 kHz zurück; dieser Zustand ist kein Beleg für einen fehlgeschlagenen 48-kHz-Handoff während aktiver Wiedergabe.
- Der aktuelle Live-Zustand auf `.104` wirkt im praktischen Betrieb wieder unauffällig. Für die Bewertung des Local-Handoffs ist der aktive Übergang während laufender Wiedergabe maßgeblich, nicht der pausierte Sink-Zustand.
- Lokal wurde keine weitere Runtime-Änderung vorgenommen; der Stand bleibt auf dem Stabilitäts-Gate aus Commit `0131c57`.

## Nachbeobachtung 2026-08-01 10:25

- Der zusätzliche Fix aus Commit `752b704` wurde auf `.104` deployed und von Paul praktisch verifiziert.
- Mehrere laufende Output-Mode-Wechsel zwischen `subwoofer-2.2`, `stereo`, `subwoofer-2.1`, `subwoofer-2.2-stereo` und zurück wurden ohne hörbaren Lautstärkesprung bestätigt.
- Das Journal zeigt beim Stereo-Wechsel den geordneten `SUB-STOP` mit Wiederherstellung der direkten EasyEffects→Hardware-Links; anschließend war der Stereo-Graph warm verfügbar (`EE-GRAPH stereo ok`, kein EasyEffects-Service-Neustart im verifizierten Lauf).
- Der Dienst blieb `active/running`; die laufende Radio-Wiedergabe war nach dem Test weiterhin aktiv.
- SR-002 ist damit einschließlich des nachgelagerten Output-Mode-/Loudness-Transienten praktisch verifiziert.
