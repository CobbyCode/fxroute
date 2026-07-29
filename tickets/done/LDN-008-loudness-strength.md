# Ticket: LDN-008

## Project
FXRoute

## Goal
Add a persistent Loudness Strength selector while preserving the existing total gain.

## Task
Add Full/Med/Light/Min strength offsets and apply them only as the coupled
Loudness volume/output-gain split.

## Input
Existing Loudness implementation and confirmed Tone UI placement.

## Expected Output
Persistent strength selection, matching Tone UI control, focused regression tests.

## Target Path
`/home/pbclaw/ai/projects/fxroute`

## Notes
Do not change SPL calibration, Auto Gain, or unrelated DSP behavior.

## Status
done
