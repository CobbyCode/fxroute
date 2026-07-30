# Ticket: LDN-022

## Project
FXRoute

## Goal
Run SPL Calibration pink noise through neutral Auto Gain and Loudness states,
independent of the previously active Auto-Gain target and Loudness correction.

## Task
Extend only the existing SPL Calibration runtime snapshot/restore lifecycle:
remember Auto Gain bypass and target, bypass Auto Gain without changing its
target, and restore Auto Gain plus Loudness exactly on every exit path.

## Input
Confirmed neutral-Loudness SPL path from LDN-015 and current production code
through LDN-021.

## Expected Output
- SPL pink noise is not raised from −23 LUFS by Auto Gain.
- Auto Gain target is not changed during calibration.
- Auto Gain and Loudness runtime state is restored on stop, save, cancel,
  automatic completion, and failure.
- Normal sweeps, AutoSub, all other DSP stages, targets, formulas, and gain
  structure remain unchanged.
- No preset reload or graph reconfigure.
- Focused tests, separate commit, deployment to `.104`; no push or release.

## Target Path
`/home/pbclaw/ai/projects/fxroute`

## Notes
Preserve unrelated dirty AutoSub changes and project artifacts.

## Status
in_progress
