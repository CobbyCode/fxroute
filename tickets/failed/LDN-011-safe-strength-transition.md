# Ticket: LDN-011

## Project
FXRoute

## Goal
Prevent positive transient gain during Loudness Strength changes.

## Task
For a pure Strength change, apply the coupled Loudness gains in a safe
direction-dependent order: lower output gain before raising LSP volume for a
larger offset, and lower LSP volume before raising output gain for a smaller
offset.

## Input
Confirmed LDN-010 production baseline at commit
`4fbdcc027c64757b86c4f0c6f72628473b6d25e2`, deployed on `.104`.

## Expected Output
Focused implementation and tests proving no positive intermediate gain,
unchanged bypass/master/UI behavior, one persistence operation, one UI save,
all adjacent live transitions in both directions, separate commit, and
deployment on `.104`.

## Target Path
`/home/pbclaw/ai/projects/fxroute`

## Notes
Do not change Strength levels, formula, SPL calibration, toggle logic, UI,
system master, push state, or release state.

## Status
failed

## Result
Pure Strength changes use a two-phase active-preset transition. Increasing
offsets lower `output-gain` first; decreasing offsets lower `volume` first.
The second phase restores the unchanged final sum. Both phases preserve the
existing Loudness bypass state. Persisted extras are written once, the system
master is untouched, and non-Strength changes retain the existing path.

Automated checks cover all adjacent transitions in both directions, exact
final gain balance, non-positive intermediate gain, bypass preservation,
single persistence, pure-change routing, Python syntax, existing Loudness
regressions, integration, and SPL coupling.

Live on `.104`, all adjacent transitions in both directions used the intended
order. Every final `volume + output-gain` sum was exactly
`-25.212984202991393 dB`, Loudness stayed active throughout each transition,
the system master stayed at 38, FFT stayed 8192, and the initial state
(Loudness off, Strength Min, playback stopped) was restored.

## Rollback
Rejected after user validation: no audible improvement was apparent and the
mains subsequently produced no sound. The `.104` runtime files and the exact
pre-experiment state were restored from the LDN-011 backup. The experiment is
not part of the confirmed production state.
