# Ticket: LDN-010

## Project
FXRoute

## Goal
Adjust only the Loudness Strength offsets.

## Task
Use Full/Med/Light/Min offsets 0/10/20/30 dB while preserving the existing
pegel-neutral formula, UI, persistence, IDs, and gain structure.

## Input
Confirmed LDN-009 implementation at commit
`9b1c0d6f85b2803d2f608d7e0dd7a15c9ae19bf7`.

## Expected Output
Focused offset change, targeted neutrality and persistence checks, separate
commit, and deployment on `.104`.

## Target Path
`/home/pbclaw/ai/projects/fxroute`

## Notes
No push, release, UI change, or unrelated refactor.

## Status
done
