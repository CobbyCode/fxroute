# Input auto-select internals

How FXRoute decides that an armed input carries a signal. The controls and the
user-facing rules are in the [manual](../MANUAL.md#48-input-auto-select).

## Detection across graph changes

Rate changes, channel-tier reprobes and DSP graph rebuilds fence local capture
activity. Monitoring restarts on the settled graph with a fresh attack; samples
and decisions from an earlier graph or probe cannot select an input.

- Capture gaps longer than 300 ms restart the attack.
- A new local claim also needs qualifying data no older than 750 ms; the longer
  signal-end hold does not qualify a new claim.
- Fresh external HTTP presence (`POST /api/audio/input-presence`) remains an
  independent trigger and is reconsidered once graph work settles.

## Digital lock

- Scarlett 16i16 4th Gen capture channels 9–10 are recognized as S/PDIF even
  when the source name is generic.
- An explicitly lost digital lock blocks local activation; an unknown lock still
  permits signal-based detection.
- A lock alone never counts as programme.

## Journal

Automatic-switch journal entries identify the input, the trigger (local audio,
HTTP presence or STDIN), peak and RMS level, lock, data age and graph/probe
generation. External presence value changes are logged without repeating
unchanged heartbeats.

```bash
journalctl --user -u fxroute -f
```
