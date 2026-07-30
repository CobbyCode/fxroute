# Ticket: REL-080

## Project
FXRoute

## Goal
Publish FXRoute 0.8.0 from the complete confirmed production state.

## Task
Reconcile authoritative and `.104` code, include only confirmed changes,
update README, manual, changelog, version and asset versions, run existing
relevant tests, publish main/tag/GitHub Release, and put `.104` on the exact
release commit.

## Input
- Authoritative repository: `/home/pbclaw/ai/projects/fxroute`
- Production: `paul@192.168.178.104:/home/paul/fxroute`
- Confirmed Loudness/SPL series through LDN-022
- Confirmed safe AutoSub AutoGain work in ASG-001

## Expected Output
- Release commit and pushed `v0.8.0` tag
- Published GitHub Release
- `.104` byte- and commit-aligned with the release
- Updated concise public documentation and release notes
- Relevant existing tests passing

## Target Path
`/home/pbclaw/ai/projects/fxroute`

## Notes
Do not include failed experiments, backups, internal intermediate results, new
features, refactors, or migration paths.

## Status
review

## Reconciliation

- `origin/main` is the merge base of the authoritative project.
- Confirmed Loudness/SPL code through LDN-022 is committed and matches the
  effective `.104` runtime files.
- Confirmed ASG-001 source consists of `main.py`,
  `subwoofer_runtime.py`, `pipewire_stage1/fxroute_21_passthrough.c`, and its
  focused AutoGain test. The release includes this validated implementation.
- Failed Loudness and Predictive AutoSub experiments are not effective in the
  release tree and are not mentioned in public release notes.
- Local backup, output, and scratch artifacts are excluded from the release.

## Verification

- Core Python compilation passed.
- Loudness, Strength, combined Auto Gain, SPL Calibration, UMIK, samplerate,
  peak-monitor, AutoSub AutoGain, polarity, candidate-ledger, target-anchor,
  subwoofer-runtime, and native helper tests passed.
- Frontend JavaScript syntax checks passed.
- `git diff --check` passed.

## Release preparation

- README and Manual describe the actual 0.8.0 Loudness, SPL Calibration, and
  confirmed AutoSub behavior.
- Changelog contains concise 0.8.0 release notes.
- `VERSION` and frontend asset cache versions are 0.8.0.
