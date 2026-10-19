# Common playback sample-rate conversion

## Qualification status

**Not ready for a product commit.** The earlier focused Library/TIDAL/Radio and
input matrix passed, and the additional Qobuz 96 kHz handoff now passes all three
modes after the reserve-accounting correction. One full matrix rerun reproduced
a High-mode underrun on 32-channel 96 → 48 kHz STDIN; a later full rerun passed,
so that intermittent failure is still being investigated. The test
host has been restored to Standard and its saved input/sample-rate settings.
No push or release has been made. The remaining failure must not be hidden by
the earlier passing matrix.

## Policy and signal path

**Technical settings → Audio Output → SRC Quality** stores one choice, separately
from **Sample Rate**:

| Mode | Nominal-rate engine |
| --- | --- |
| Low | libsoxr VHQ, one CPU worker |
| Standard (default) | Pinned CPU-only SoX 16.2.1, `rate -M -v -s` |
| High | The same binary, `rate -E -z -a -B 99.5 -A 99.5 -f <target>` |

The approved profiles are unchanged. There is no GPU dependency or additional
provider-specific resampler. Renderer-internal conversion, if any, happens before
FXRoute and cannot be undone by this setting.
The approved reference quality results remain Standard **81.8574 Balanced** and
High **99.6116 Balanced**; those are profile-reference results, not measurements
of every live clock handoff.

The existing path remains:

```text
native provider PCM → PipeWire adapter → graph-rate DSP ingress
                   → native DSP → selected hardware
```

The complete `fxroute.src.convert` SPA factory is selected by
`audio.adapt.converter` at native-client, Pulse and ALSA/BlueZ device adapters.
Its format conversion, channel mapping and adapter contracts come from pinned
PipeWire 1.6.8 SPA sources. A bounded asynchronous nominal worker feeds the
unchanged native PipeWire clock matcher; rate feedback, matcher options and
nondefault configuration are retained. Equal nominal rates skip the nominal
worker, not clock matching. No SRC is inserted after the graph-rate DSP ingress.

The worker uses nonblocking pipes and bounded SPSC queues. Engine construction,
allocation, child supervision and teardown are outside the process callback.
Merge buffers remain held until consumed. EOF drains the nominal engine and the
native matcher's independent padding/history before reporting completion.
An engine or queue failure is an error, never an implicit mode downgrade.

## Applying a mode

The central file is `$XDG_CONFIG_HOME/fxroute/src-policy.json`, or
`~/.config/fxroute/src-policy.json`. Its schema is `{"mode":"standard"}`.
A missing file selects Standard; an invalid policy is reported rather than
silently selecting a different engine.

- `GET /api/audio/src` reports the selected policy, binding readiness, observed
  adapter mode/rates/bypass/error, and provisional buffer limits.
- `POST /api/audio/src` with `{"mode":"low"}`, `{"mode":"standard"}` or
  `{"mode":"high"}` applies the selection automatically during App playback,
  including playing and paused tracks. Measurement and selected or active input
  sessions prevent a change.
- Application uses the existing Sample Rate playback transaction: quiet the
  source, rebuild the PipeWire/Pulse/WirePlumber adapters and owned player/DSP
  graph, then restore and verify playback before opening the output gate.
  Playing tracks resume automatically, paused tracks stay paused, and stopped
  playback stays stopped. The queue and selected track are retained; Local and
  TIDAL restore their position, while Radio reconnects to the live stream.
  The independent graph-rate pin is preserved. Existing
  unrelated audio clients can be disconnected by this restart.
- Failed application restores the previous policy and attempts to rebuild the
  previous graph. Failure to restore is reported separately. Request
  cancellation does not release graph ownership before application finishes.

The converter reads the file when constructed. Editing it manually does not
reconfigure existing adapters; use the API to avoid mixed live modes.

## Build and bindings

The installer builds the runtime and writes the common rules. For a development
build with the documented native build dependencies installed:

```sh
python3 native_src/build.py
sudo python3 scripts/install_src_plugin.py
python3 scripts/configure_src.py
```

The builder verifies SHA256 for both source archives and pins SoX with
`WITH_VULKAN=OFF`, `WITH_OPENMP=OFF`, `BUILD_SHARED_LIBS=OFF`. It records engine,
factory and source hashes in `native_src/build/manifest.json`. The runtime
library and SoX binary are generated files, not repository binaries.
Rebuilds publish immutable `runtime-<generation>` directories through an atomic
`current` symlink; a running child or mapped library is not overwritten. Old
generations remain available to their existing hosts until those hosts restart.
Do not delete them under running audio processes.

SPA library names are relative to the plugin search directories, not absolute
paths. The installer creates an owned symlink in the standard SPA directory so
ordinary native/ALSA clients can find the converter without an injected
environment. `environment.d/80-fxroute-src.conf` also adds the runtime to the user
service environment while retaining stock SPA locations. The configuration
helper activates this environment for new services; the owned MPV/STDIN clients
receive it explicitly. Common rules are installed under PipeWire client,
daemon and Pulse drop-ins and WirePlumber ALSA/BlueZ rules. A daemon node rule
also binds the product `fxroute_dsp_sink` ingress unconditionally, so its
constructor and relink behavior use the fixed converter even while idle.
Existing provider ingress-target routing is unchanged.

Readback reports unbound hardware or active stream adapters, missing parameters
on running adapters and an older runtime generation as adapter errors. Unbound
idle or suspended streams remain visible in diagnostics, but do not block SRC
selection for ongoing playback. Bound adapters still report mode, generation and
engine failures even when inactive. Idle, configured-but-not-started adapters can
remain `unconfigured`; passive graph-only split loopbacks and MIDI nodes are not
native audio boundaries. With no device or stream observed there is nothing to
bind. Selecting an SRC quality reconstructs the adapters with the installed
runtime, including when the same quality
is submitted through the API. Updating an older installation without the standard-path plugin or
the SRC build dependencies still completes: the update logs the skipped SRC
reconciliation, stock PipeWire conversion stays in use, and the configuration
helper refuses to publish client rules until the installer has been rerun.

SRC is serialized by the existing playback coordinator, with the same source
handoff and restore stages as Sample Rate. Adapter replacement refuses busy
DSP or measurement locks rather than waiting while owning the coordinator.
Application snapshots
the graph-rate pin and hardware identity once for both apply and rollback. The
original hardware, or its unambiguous stable-device successor, must return before
DSP rebuild. Child failure invalidates cached Props through a non-realtime event.
Inherited `SOX_OPTS` is excluded from the child environment so it cannot change
the approved profile or PCM gain. Uninstall removes the owned rules/discovery
and restarts affected audio services before allowing runtime deletion.

## Provisional limits

Unequal-rate adapters keep a **750 ms minimum** nominal startup reserve and use
**65,536-frame** input/output queues. Before the first real output only, the
adapter extends credited startup silence until a full native matcher input
quantum is buffered; downsampling ratios above 4:1 also retain 750 ms of
nominal PCM to cover engine burst spacing. It never re-primes after real output
begins. A preallocated startup FIFO (750 ms at the output rate plus 16,384
frames) drains the worker during reserve production without dropping PCM.
Unequal-rate adapters accept rates up to 768 kHz; allocation is outside the
realtime callback. A later
shortage with a healthy engine is concealed with counted silence and the
stream keeps running; only engine failure (a worker error) is terminal.
The 3,750 ms startup deadline starts with the first accepted input frame,
including callbacks with no output capacity and FIFO backpressure. Worker
failure still fails immediately and reset
clears the deadline. These are prototype-derived, provisional
values, not a promised latency target. Equal-rate playback has no nominal
reserve. These values have not been tuned from a quality preference and should
only change following measured product-path evidence.
Startup progress is counted in nominal-time units across native matcher rate
changes. Raw output padding can therefore vary with the graph-clock scale;
elapsed reserve is not reset or shortened when that scale changes. The 750 ms
figure is a minimum: under CPU contention the first usable burst may arrive
later within the 3,750 ms bound, and a startup that never becomes ready fails
with `ETIMEDOUT` instead of underrunning silently. That timeout never replaces an
earlier adapter error and is not counted in `underruns`, which count queue
starvation only. Worker channels are limited to 32; an unequal-rate adapter
with more channels is rejected at construction.

This qualification is not a guarantee for every PipeWire version, channel/rate
combination, scheduler, device, sustained load or child stall. ARM has not been
qualified. Bluetooth needs an actual connected audio sender for end-to-end
verification; a configured rule alone is not a playback pass.

## Diagnostics

Each adapter also publishes `fxroute.src.latency-ns` in its Props. This is the
fixed nominal-time offset retained by its startup padding: 750 ms initially
for unequal rates, updated to the actual padding if startup readiness extends
it, and zero for equal-rate bypass. It remains credited after real PCM begins;
only reset starts a new generation. The non-realtime event updates Props and
PipeWire latency propagation when the offset changes. Engine first-output wall
time and clock-servo backlog are separate quantities, not extra fixed delays.

AV Sync reads each linked adapter's own offset once, not the same propagated
latency at every port. Its existing input credit combines the common SRC offset
on the owned input/hardware paths with the digital helper's 2818/48 ms offset.
Different simultaneous SRC path offsets or failed/missing evidence make timing
unavailable. Measurement ignores preserved app ingress and keeps the hardware
credit. `/api/audio/src` exposes the adapter evidence as `latency_ns`.

Impulses at 0.5, 1.5 and 2.5 seconds through the built runtime on the audio host
all retained a 750 ms offset for Low, Standard and High at 44.1→96 and 96→48 kHz;
96→96 kHz retained zero. At these rates the impulse peak had no additional
sample offset after removal of startup padding. This does not turn the
minimum-phase Standard filter's frequency-dependent phase into a separate
constant group delay. The measured engine first-output times differed by mode
and ratio (about 0.1–612 ms), but were covered by the reserve.

Each converter writes one `FXSRC_WORKER_STATS` JSON line to its host's stderr
when it is destroyed, and one `FXSRC_ERROR_STATS` line from a non-realtime loop
event when it fails. The process callback only updates lock-free counters and
write-once timestamps; it never formats or writes diagnostics. Startup fields
describe the current generation, and a reset clears them:

- `lead`: silent output frames emitted before the first real output, i.e. the
  minimum startup reserve plus any readiness extension. It does not grow once
  real output begins.
- `reserve`: the minimum-reserve part of `lead`; `lead - reserve` is the
  readiness extension.
- `first_output_ns`: engine first-output latency, from the first frame the
  nominal worker accepted to its first produced frame; 0 while the engine has
  produced nothing.
- `first_real_ns`: from the first callback that offered input to the first real
  output frame; 0 before real output.

Within one generation each latency is published before the frame counter that
depends on it, and `reserve` never exceeds `lead`. A startup that never became
ready shows `first_output_ns` 0, `lead` above `reserve` and `error` 110
(`ETIMEDOUT`). Starvation after real output with a healthy engine is concealed:
both latencies stay set, `error` stays 0 and `underruns` counts each concealed
quantum over the converter's lifetime. A nonzero `error` (for example 32 for a
dead engine) still means terminal engine failure, never a survived stall.
`max_callback_ms` is the converter process callback's
maximum wall time. These values are telemetry, not tuned sizing targets.

## Verification (2026-10-03)

The corrected product sources passed **126 complete-SPA feedback/drain oracle
cases on both hosts**. These cover merge backpressure, variable/zero capacity,
short/empty/long EOF, both directions, all three engines, nondefault native
matcher configuration, fractional clock feedback and live drift. Worker output
also matched the pinned CLI byte-for-byte under output backpressure.

The earlier running test product passed **27 focused end-to-end cases**: three modes ×
Library, authenticated TIDAL, Radio, packed-S24 STDIN, 128-frame STDIN, selected
AUX28/AUX29 from 32-channel STDIN, plain native F32, Pulse F32 and ALSA-PipeWire
F32. All cases
captured nonzero finite post-DSP PCM, retained the hardware links without bypass,
and finite four-second streams retained the expected active duration. Existing
filters can extend a short stream's captured tail; those captures are not a
byte-exact nominal-conversion oracle. The independent SPA suite provides that
check. Effective mode and native/graph rates were checked during playback:
Library/TIDAL/Radio used 44.1 → 48 kHz and 32-channel STDIN used 96 → 48 kHz.
Plain native/ALSA checks omit `SPA_PLUGIN_DIR`.
After the reserve-accounting correction, a fresh full rerun passed both complete
Low/Standard matrices and High Library/TIDAL/Radio/STDIN/short STDIN before
failing at High 32-channel STDIN. The earlier 27 passes are not final acceptance
for the latest runtime. With the readiness wait active, the full **27/27 matrix
passes again on the final runtime**, including High 32-channel STDIN, and the
real queued Qobuz track captures post-DSP audio in all three modes. A subsequent full rerun, three narrowed High
STDIN/short/32-channel reruns and ten repeated High32 checks passed without
additional CPU work. Controlled startup contention later reproduced the failure
in all three attempts, as described below.

The first raw ALSA acceptance invocation omitted `PIPEWIRE_NODE=fxroute_dsp_sink`
and did not reach the post-DSP capture. Correcting the test to use the installed
Qobuz renderer's existing ingress target resolved it; no product routing change
or alternate SRC pipeline was introduced.

Playback is stopped after live acceptance. Source selection, automatic input
arming, sample-rate policy and SRC mode are restored; selecting provider media
can change the stopped player context. Cleanup attempts every restoration even
if an earlier restoration fails. The web demo simulates this API; it is
not audio evidence and is never deployed to the product test host.

The development host has no running product or usable DSP executable. Its
**36 private-graph product-library cases** cover all three modes, quanta 256/1024,
native F32/S16/S32 and 32-channel F32, plus Pulse F32/packed-S24, with an in-graph
stock sink tap. They passed with zero nominal underruns; observed queue maxima
were 5,174 input / 36,632 output frames and maximum callback wall time was
0.642 ms in the latest run. These are adapter/transport checks, not fictitious
provider playback.
`aplay` is absent there. Bluetooth has paired devices but no connected sender on
the product host. Spotify Desktop is installed/connected but stopped without a
selected track; it has not been exercised in this qualification.

The full local runnable suite passed **540 entries**, with **19 explicit skips**
(including the dependency-limited native suites and opt-in live tests). Latest
focused policy/API/native/runtime-publication regressions also pass, including
peak-node and 32-channel Props, child death, competing measurement/preset
requests, lock handoff, immutable rebuild and rollback rate-pin preservation.
Failure diagnostics use the non-RT error event and independently sampled
lock-free counters. A real-child-death EOF/drain regression reproduced a
diagnostic data race under ThreadSanitizer before those counters became atomic;
the corrected regression and normal/concurrent-SPA error delivery now pass. The
corrected 126-case oracle passes on both hosts with zero compiler warnings; the
10-test native suite covers clock handoffs, reserve conservation, nonzero PCM
parity, race-free error snapshots and the startup readiness wait.
Browser Settings were checked on the real product at desktop/mobile sizes;
the demo snapshot parity check passes.

### Qobuz handoff correction

The real queued Qobuz track is 96 kHz. Under fixed 48 kHz policy its handoff first
forces a 96 kHz graph, then the coordinator reapplies 48 kHz. Hardware adapter
nominal formats can remain 96 ↔ 48 kHz temporarily while native matching scales
span 1–2 and 0.5–1. The original fixed-output-frame reserve reproduced a High
underrun in the 18-channel 96 → 48 kHz adapter: it accepted 37,888 frames but
produced/delivered only 170 frames before the failure. Subtracting accumulated
raw padding at a newly changed rate was also incorrect; a falling-scale SPA test
reproduced that error.

Keeping the remaining reserve in nominal frames and consuming it by each
interval's matching rate fixes both cases without changing an engine/profile or
FIFO. Actual DSP-port SPA tests exercise rising/falling/multiple clock changes;
nonzero 18-channel PCM matches the approved High CLI and an independent native
matcher byte-for-byte, including padding and complete EOF. Drift, short/empty
EOF and reset tests preserve the reserve and do not re-prime after real output.
The real queued Qobuz track subsequently captured post-DSP audio in all three
modes, with observed `qbzd` 96 → 48 kHz conversion and stable hardware adapters
at equal 48 kHz rates, error zero. This is a scoped playback pass, not a claim of
seamless spectral continuity through every transient graph reconfiguration.

### Remaining High 32-channel failure

An actual STDIN run reported `fxroute.src.error = 32` on the 32-channel
96 → 48 kHz converter. The approved CPU-only CLI converts the exact four-second
fixture successfully in 2.007 s wall / 1.994 s child CPU time. Five isolated,
nominally paced worker trials also drain all 384,000 input / 192,000 output
frames. Their first full output burst arrives at 0.630–0.640 s, with maximum
input queue 29,184 and output queue 24,128 frames. These measurements show that
the engine can finish the fixture and establish its bursty startup behavior;
they do not explain away the product-path underrun or qualify sustained load.

Controlled contention uses three separately owned, approved High CPU-only CLI
jobs on that same fixture while the real STDIN path starts. On the four-core
N150 test host, all three product trials fail at startup, not after real PCM:
the converter accepted 73,728 frames, emitted its 36,000-frame minimum
reserve, and had produced/delivered only 0–192 nominal frames with `real = 0`.
Maximum input queues were 57,344–63,488 frames, near but below the 65,536-frame
bound. This
shows that 750 ms is not a sufficient startup margin under this controlled
contention. It does not justify an arbitrary replacement reserve or imply the
engine is incapable of sustained conversion. No provider media was replaced by
these transport/load fixtures, and all additional CPU jobs were cleaned up.

With the readiness wait active and the same three owned CLI companions, all
three product trials now start real PCM (lead 62,464 silent frames including
the reserve, real 23,552) but still
fail at about 27 s with `error = 32`: the worker input FIFO fills to
65,536 frames while the SoX child, starved of CPU, cannot sustain nominal
throughput. This is a sustained-load failure under 3× full-core contention on a
4-core host, distinct from the original startup failure. It does not invalidate
the unloaded passes, but sustained High32 under that contention remains
unqualified and the provisional sizing is unchanged.

Syscall-traced product diagnostics confirm that the initial 192-frame fragment
precedes the usable burst, but tracing perturbs the result: even the no-extra-
load trace fails where ten ordinary reruns passed. Those traced wall times are
not sizing targets. Trace-related cleanup requests also timed out; the
diagnostic drop-in and trace processes were removed, and Standard / Auto /
saved external-input arming were explicitly restored and verified afterward.

### Scoped CPU and queue observations

Six real native-PCM-to-DSP/hardware captures completed without nominal underruns
on the N150 host, without extra CPU work. Each used nine seconds of PCM: eight
seconds of signal, plus 0.5 s silence at each end so manually connected native
ports were ready before signal. Native and 32-channel cases used 44.1 → 48 and
96 → 48 kHz respectively and omitted `SPA_PLUGIN_DIR`.

| Mode | 2-channel SoX CPU, sampled | 32-channel SoX CPU, sampled | 32-channel launcher CPU, sampled | 32-channel maximum callback wall time |
| --- | --- | --- | --- | --- |
| Low | no child | no child | 1.32 s, including in-process worker | 1.682 ms |
| Standard | 0.17 s | 1.09 s | 0.95 s | 7.188 ms |
| High | 0.19 s | 4.89 s | 0.67 s | 5.499 ms |

These are 100 ms `/proc` CPU samples, not complete child accounting or total
PipeWire/DSP CPU. Wall durations were 9.83–10.10 s; High32's sampled child CPU
averages about 48.4% of one core over its run. Across these captures, observed
queue maxima were 14,336 input / 36,632 output frames. Callback wall maxima
time only the converter process callback, so they include scheduling delays
inside it; construction and teardown run outside it. They are not a deadline or
sustained-load guarantee. The controlled contention failures remain
disqualifying evidence.

### Reproduce the focused checks

```sh
python3 scripts/test_src_policy.py
python3 scripts/test_src_api.py
python3 scripts/test_src_native.py
node scripts/test_src_settings.js
FXROUTE_SRC_LIVE=1 python3 scripts/test_src_product_live.py
```

The live test needs the running product, selected hardware, accessible media and
permission to temporarily stop playback and change audio settings. Without its
opt-in environment variable it does not change live audio state.
