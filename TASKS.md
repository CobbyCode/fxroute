# TASKS — audio-mini-pc-webapp

## Goal
Add a first technical settings surface to FXRoute without disrupting the current stable DSP/import baseline.

## Tasks

- [x] FX-SETTINGS-001 — Define and implement a first settings entry path via the FX header logo
- [x] FX-SETTINGS-002 — Add v1 Audio Output selection with real-device switching and safe fallback behavior
- [x] FX-SETTINGS-003 — Add v1 Source Mode selection with direct external-input monitoring
- [x] BT-001 — Define Bluetooth architecture and backend inventory/API for sink-first integration
- [x] BT-002 — Deliver a first visible Bluetooth input MVP with conservative receiver activation/routing
- [ ] FX-SETTINGS-004 — Re-evaluate DAC keep-awake only if a clear reproducible DAC sleep/click problem is confirmed
- [x] FX-MEASURE-001 — Add measurement modal/view shell, separate measurement persistence, and saved-trace comparison graph
- [x] FX-MEASURE-002 — Add real mic measurement flow with input selection, calibration upload, sweep/capture, and saved current measurement
- [x] FX-MEASURE-SWEEP-001 — Add a real host-local sweep playback/record/deconvolution path for the optional `.104` measurement mode
- [ ] FX-MEASURE-SWEEP-002 — Upgrade the host-local sweep analysis with real inverse-sweep deconvolution, timing/clock handling, and IR/windowed response extraction
- [ ] FX-MEASURE-SWEEP-003 — Add raw full-band review output alongside the conservative trusted trace for host-local sweep evaluation
- [x] FX-MEASURE-CLIENT-001 — Add browser-client microphone capture as the initial measurement path while host-local remained available; this path is now archived after failing to become trustworthy for room measurement
- [ ] FX-MEASURE-CLIENT-002 — [parked] Stabilize browser-client sweep measurement repeatability and harden the browser analysis path before trusting it beyond rough/convenience use
- [ ] FX-MEASURE-CLIENT-003 — [parked] Harden browser measurement robustness for comparison work, especially upper-band reliability, outlier handling, and graph/QC behavior
- [ ] FX-MEASURE-CLIENT-004 — [parked] Replace the too-coarse browser upper-band cap with targeted 18–20 kHz edge handling and honest graph/QC behavior
- [ ] FX-MEASURE-CLIENT-005 — [parked] Test whether extending the internal browser sweep to ~23 kHz improves the visible 18–20 kHz edge while keeping the browser display capped at 20 kHz
- [ ] FX-MEASURE-CLIENT-006 — [parked] Investigate final-edge analysis behavior around 18–20 kHz, including whether clock/timing/drift effects plausibly contribute
- [ ] FX-MEASURE-REF-001 — Define a REW-näher host measurement reference architecture (loopback/dual-channel first, acoustic fallback second) and map it onto `.104` PipeWire realities
- [ ] FX-MEASURE-REF-002 — Implement host-local dual-channel/reference capture and timing/drift alignment ahead of ESS deconvolution
- [ ] FX-MEASURE-REF-003 — Compare the new host reference path against REW at the listening position and document remaining gaps
- [ ] FX-MEASURE-RESTORE-001 — Consolidate the separate host-local host-reference path as the stable measurement baseline without reopening playback/samplerate/Spotify regressions
- [ ] FX-MEASURE-REF-004 — Design a browser-compatible acoustic timing-reference path with Marker A/B, marker-based offset/drift estimation, and full ESS deconvolution
- [x] FX-MEASURE-REF-005 — Implemented the browser acoustic-reference path and found it not practically useful in-room
- [x] FX-MEASURE-REF-006 — Designed a browser V2 hybrid/server-first reference path with a more acoustically robust marker/reference program after the first browser marker path proved too fragile live
- [x] FX-MEASURE-REF-007 — Implemented and evaluated the browser V2 hybrid/server-first reference path; archived after it still failed to become trustworthy for room measurement
- [ ] FX-MEASURE-003 — Add one manual peak-filter preview with estimated corrected response and Copy to PEQ
- [ ] FX-MEASURE-004 — Extend the assistant to 3-4 manual filters with enable/disable and Copy All to PEQ
- [ ] FX-MEASURE-005 — Evaluate optional conservative Auto-PEQ later without turning the feature into a full REW replacement

## Notes
- Current accepted baseline already includes compare/combine, PEQ gain, IR cleanup, and no autoload on import.
- 2026-04-30 measurement direction update: browser-vs-host appears to have been a misleading framing; both paths can still fail similarly at the listening position, so the next serious measurement work should prioritize true timing reference architecture (host loopback/dual-channel first, browser acoustic reference second) over more anchor-threshold tuning.
- 2026-04-30 restore update: the proven working baseline to protect is the separate host-local host-reference path plus the recovered everyday playback baseline. Do not mix DSP-in-path design into that restore; treat it as a later dedicated step.
- Measurement planning note added on 2026-04-27: `outputs/fxroute-measurement-assistant-plan-2026-04-27.md`.
- Measurement assistant rule: measurements are stored separately from DSP presets and active PEQ state; the measurement view is only an assistant layer and Copy to PEQ is the bridge into the existing PEQ workflow.
- DAC keep-awake prototype was tried and then removed again on 2026-04-25 because it did not show clear value and briefly destabilized the UI during rollback; do not revive it without a clearer problem statement or an explicitly optional design.
- FX-SETTINGS-004 is intentionally parked behind Bluetooth work until DAC behavior is explicitly re-checked and the value of any keep-awake logic is proven.
- Bluetooth planning note added on 2026-04-26: `outputs/fxroute-bluetooth-audio-device-model-2026-04-26.md`.
- Bluetooth BT-001 inventory/API spec added on 2026-04-26: `outputs/fxroute-bluetooth-inventory-api-spec-2026-04-26.md`.
- BT-001 now also has a first implementation note: `outputs/fxroute-bluetooth-readonly-inventory-implementation-note-2026-04-26.md`.
- BT-002 was completed on 2026-04-26: Bluetooth input is visibly usable in settings, conservatively activatable/routable on `.104`, and now also shows codec + samplerate when active.
- Recommended Bluetooth order from those notes: BT sink as external source first, BT source as normal output second, remote control only as an optional capability layer.
- DAC keep-awake remains intentionally deferred unless a clear reproducible DAC sleep/click problem returns.
- Installer-managed Spotify cache cleanup and optional system package update helpers were implemented on 2026-04-26 as opt-in env-driven timers, intentionally kept out of the live settings UI and defaulted to `off`.
- Emerging follow-up ideas for later evaluation: Bluetooth source, UPnP/DLNA renderer, max samplerate setting, distro-aware maintenance helpers.
