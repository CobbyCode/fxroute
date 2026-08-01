# Ticket: SR-001

## Project
FXRoute

## Goal
Restore the configured PipeWire default sample rate after startup and after a
measurement session without changing Loudness, AutoSub, or unrelated behavior.

## Task
Trace the saved default rate, service startup, measurement-session lifecycle,
hardcoded 48000/force-rate/request_open paths, and the live PipeWire graph.
Fix only the demonstrated regression and verify 44.1 kHz normal playback,
48 kHz measurement operation, and restoration to 44.1 kHz.

## Input
- Authoritative project: `/home/pbclaw/ai/projects/fxroute`
- Live host: `paul@192.168.178.104:/home/paul/fxroute`
- No refactors, backups, Loudness/AutoSub changes, push, or release

## Expected Output
- Focused code fix and regression test
- Separate local commit
- Live deployment and 44.1/48/44.1 kHz verification on `.104`

## Target Path
`/home/pbclaw/ai/projects/fxroute`

## Notes
Preserve the fixed 48 kHz measurement rate; only stale/startup ownership of
that rate may be corrected.

## Status
done
