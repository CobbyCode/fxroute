# FX-CONVOLVER-PHASE-002 — Compare Linear, Minimum, and Mixed FIR generation

## Status
review

## Goal
Add an experimental, protected comparison path for Convolver FIR generation variants without replacing the current working Linear FIR baseline.

## Context
Paul likes the current Convolver result and wants to keep it. Current generator is a straightforward linear-phase frequency-sampled FIR and is already useful, including correction ranges up to ~3 kHz on Wharfedale Diamond 12.2. The next experiment is to compare phase strategies because higher correction ranges can make pre-ringing/group-delay behavior more relevant.

## Requirements

### Preserve baseline
- Do not remove or alter the current Linear FIR behavior except where needed for clean option plumbing.
- Existing presets and workflow must remain usable.
- Keep the current `Linear FIR 4096/8192/32768 taps` options available.

### Add comparison variants
Add a clearly experimental phase/type option set, ideally in the existing Convolver `Type` dropdown or a nearby advanced selector:

1. `Linear FIR` — current behavior.
2. `Minimum phase FIR` — generate a minimum-phase version of the correction IR.
3. `Mixed phase / Hybrid FIR` — conservative experiment:
   - Prefer less pre-ringing than full linear phase.
   - Preserve useful low-frequency correction behavior.
   - Avoid aggressive full-range inversion.
   - If a full mixed-phase implementation is too large, implement a defensible first hybrid approach and document it.

### Practical comparison UX
- Generated preset names should include enough info to identify the strategy, e.g. `Lin`, `Min`, `Mix` or similar.
- Keep sample rate and tap-count choices explicit.
- It should be easy to create A/B comparable presets for the same measurement/range/target.

### Safety / honesty
- If range end is above ~1 kHz, keep or strengthen the warning about tonality/reflection correction.
- Document that nice target tracking is not proof of good sound.
- Avoid claiming minimum/mixed phase is “better”; this is a listening comparison tool.

## Suggested implementation notes
- Investigate a browser-side minimum-phase transform path for the generated magnitude response/IR.
- A common approach is cepstrum/Hilbert-style minimum-phase reconstruction from log magnitude, then IFFT.
- If implementing true mixed phase is too much for this ticket, make a first hybrid mode such as:
  - bass/lower range keeps more linear behavior,
  - higher range uses minimum-phase reconstruction or shorter effective window,
  - with a clear label and code comments.

## Validation
- `node --check static/app.js`
- `git diff --check`
- If possible, create/inspect at least one generated WAV/metadata path for each mode.
- Deploy to `.104` only after local checks pass.

## Deliverable
- Code changes in `/home/pbclaw/ai/projects/fxroute-public`.
- Short review note summarizing actual DSP approach, limitations, and validation.

## Implementation notes
- Preserved the existing linear FIR path as the default and kept the existing `Linear FIR 4096/8192/32768 taps` options.
- Added experimental `Minimum phase FIR` and `Mixed/Hybrid FIR` options for 4096/8192/32768 taps in the existing Convolver `Type` dropdown.
- Minimum phase uses a browser-side real-cepstrum reconstruction from the generated log magnitude response, then IFFT to a causal IR.
- Mixed/Hybrid is intentionally conservative: it fades from linear phase below roughly 250 Hz to minimum phase above roughly 900 Hz, preserving low-frequency linear behavior while reducing upper-range pre-ringing risk.
- Generated preset names now include `Lin`, `Min`, or `Mix`; draft metadata freezes sample rate, type, phase mode, and tap count at take-time so creation does not silently change if the dropdown changes later.
- Strengthened wide-range warning to mention tonality/reflection correction and that target tracking is not proof of good sound; non-linear modes also show an experimental listening-comparison warning.

## Validation results
- Passed: `node --check static/app.js`
- Passed: `git diff --check`
- Ran a local Node validation against the actual convolver generator slice in `static/app.js` for `linear`, `minimum`, and `mixed` at 48 kHz / 4096 taps; each produced a 4096-sample impulse and 8236-byte mono WAV blob.
- Not deployed and not committed.

## Live review update
- Paul tested the first Mixed/Hybrid implementation and found it unusable: it sounded like some parts of the music/instruments had separate delay.
- Removed Mixed/Hybrid from the UI and generator path for now.
- Linear and Minimum phase both remain available; Paul reports both are usable, with Minimum sounding a bit cleaner/tighter in bass and Linear a little more lively.
- Redeployed to `.104` with cache token `0.5.2-phase-v4`.

## Polish update
- Removed `experimental` wording from Minimum phase labels after live testing showed it is a useful mode.
- Milder warning behavior:
  - wide-range warning now starts above 5 kHz and uses shorter wording;
  - headroom warning now only appears for +9 dB Max Boost cases with high required attenuation;
  - removed the misleading “narrow correction range” recommendation from the headroom warning.
- Redeployed to `.104` with cache token `0.5.2-phase-v5`.
- Simplified +9 dB headroom warning to neutral wording: `High boost settings need extra headroom.` Redeployed to `.104` with cache token `0.5.2-phase-v6`.
- Removed the +9 dB/headroom warning entirely because auto-gain already applies the needed attenuation and the warning was more confusing than helpful. Redeployed to `.104` with cache token `0.5.2-phase-v7`.
