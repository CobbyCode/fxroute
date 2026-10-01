# Native DSP performance

## Scope and reference

The reference is main at `48a58fd6`, built with the project's C11/gnu11
`-O2` flags, without `-march=native` or fast-math. Measurements use an Intel
N150 (four physical cores), GCC 16.2, and PipeWire 1.6.9. The test installation's
native source files were verified byte-for-byte against main before profiling;
its deployment checkout's Git HEAD is not the running source revision.

The reported high-rate playback limits remain the starting evidence, not a
claim that all sample rates become reliable after these changes.

## Profile findings

- Live music with convolution: approximately 65% of sampled DSP cycles in
  `convolve`, 17% in its FFT, and 14% in the remaining processing loop.
- Controlled 176.4 kHz convolution with a 32,825-frame stereo IR originating
  at 48 kHz: 91% in `convolve`. Instruction-level profiling identifies the
  **scalar complex spectrum multiply-accumulate**, not the already-vectorized
  four-accumulator direct FIR head, as the high-rate bottleneck.
- Crystalizer alone at 176.4 kHz: 62% in `filter_block`, 6% in the forward FFT,
  7% in kurtosis, 3% in crest measurements, and 6% in the adaptive/resampling
  block. The inverse FFT butterflies inside `filter_block` remain scalar on
  x86 even though spectral accumulation is already auto-vectorized.
- Pause after playback exposes a separate **subnormal-number slowdown**:
  roughly 94% of cycles are in the output biquads/processing loop, callback
  busy time rises from around 0.6 ms to 10–11 ms at 44.1 kHz/1024 frames,
  and hardware counters record 16.8 million floating-point assists in 5 s.
  These changes deliberately do not enable FTZ/DAZ, inject noise, or truncate
  filter tails. That issue is not fixed by the convolution/FFT optimization.

### Why high rates and more channels are expensive

The convolver preserves a direct 256-tap head and uses 256-frame uniform
tail partitions with 512-bin complex FFTs. Resampling a fixed-duration IR
increases its partition count with sample rate, while the number of processed
blocks per second also increases. Tail MAC work therefore scales approximately
with **channels × sample rate squared**, not just sample rate. The tested
IR produces about 512 tail partitions per channel at 192 kHz, with stereo
IR/history spectra alone occupying approximately 8 MiB, beyond this CPU's
6 MiB L3 cache.

Crystalizer operates at 2× oversampling, with 13 bands paired into seven
inverse FFTs. Its FIR length/partition count also grows with sample rate.
Neither oversampling quality nor band/filter design is changed here.

## Implemented changes

1. Expose the convolver's disjoint input/kernel/accumulator buffers through a
   small `restrict`-qualified helper. This lets the compiler vectorize complex
   MACs without architecture-specific code, reordered partitions, or changed
   arithmetic. Both stage and output-bank convolution use it.
2. Add guarded SSE2 Crystalizer DIF/DIT butterflies over four independent
   bins, using unaligned loads/stores and separate multiply/add operations.
   Existing NEON and portable scalar paths remain; no AVX/FMA or host-specific
   compiler flag is required. `FXROUTE_FORCE_SCALAR` disables explicit SIMD.

## Reproducing the processing benchmark

With native build dependencies installed, supply an absolute path to an
ordinary engine config and keep its referenced IR unchanged:

```sh
sh native_dsp/benchmark_pipeline.sh /absolute/path/pipeline.conf 30 1024
native_dsp/build/benchmark-pipeline /absolute/path/pipeline.conf 30 128
python3 scripts/test_native_dsp_kernel_parity.py
FXROUTE_DSP_REFERENCE=/absolute/path/reference-offline python3 scripts/test_native_dsp_kernel_parity.py
```

Build/save the benchmark at each revision with the same compiler and libraries,
then alternate reference/optimized runs. Save the config, IR hash, compiler
version, CPU governor, and complete JSON output. Do not benchmark alongside
another DSP benchmark, a build, or the native regression suite.

The benchmark feeds a deterministic channel-distinct broadband stream that
does not change with quantum. Graph loading, signal creation, sample hashing,
and optional output-file I/O are
outside callback timing. JSON reports accumulated callback wall time, main
thread CPU, total process CPU including the Crystalizer worker, callback
percentiles, and exceedances of `quantum / rate`. The optional fourth argument
writes all interleaved float32 output samples for byte comparison to the
original binary. Run on the same architecture/toolchain for byte comparisons.
The parity suite can use a saved original offline engine via
`FXROUTE_DSP_REFERENCE`. By default, x86 tests the new SSE2 path against
explicit SIMD disabled; other architectures retain their existing explicit
SIMD arithmetic (including NEON's fused operations) in both builds.

**Offline deadline exceedances are not PipeWire XRuns.** Live acceptance must
also record `pw-top` busy time and the change in driver/filter/output error
counters after graph/source warm-up, using identical hardware, rate, quantum,
graph, and source. Counter totals from previous playback or rate transitions
must not be counted as errors introduced during a steady-state comparison.

## Measured processing results

The following are medians of **three interleaved original/optimized runs**
using the revised, quantum-independent stimulus: 10 s audio, quantum 1024,
the same compiler/libraries/config/IR, no simultaneous live measurement or
native test suite. CPU times exclude graph preparation and file I/O.

| Workload | Rate | Native inputs → outputs | Main-thread CPU s, before → after | Total CPU s, before → after | Main reduction | Offline deadline exceedances, before → after |
|---|---:|---:|---:|---:|---:|---:|
| Convolver | 176.4 kHz | 2 → 4 | 6.750 → 4.220 | 6.752 → 4.222 | 37.5% | 1 → 0 |
| Convolver | 192 kHz | 2 → 4 | 8.064 → 5.021 | 8.067 → 5.024 | 37.7% | 4 → 0 |
| Crystalizer | 176.4 kHz | 2 → 4 | 1.780 → 1.332 | 3.401 → 2.543 | 25.2% | 0 → 0 |
| Both | 176.4 kHz | 2 → 4 | 8.514 → 5.562 | 10.276 → 6.964 | 34.7% | 94 → 3 |
| Both | 192 kHz | 2 → 4 | 9.950 → 6.567 | 11.934 → 8.174 | 34.0% | 905 → 14 |
| Both | 48 kHz | 6 → 6 | 3.550 → 2.449 | 3.938 → 2.729 | 31.0% | 0 → 0 |
| Both | 48 kHz | 8 → 8 | 4.872 → 3.320 | 5.260 → 3.599 | 31.9% | 1 → 1 |

All paired sample hashes match. A separate comparison of every output byte
for 10 s of the combined 176.4 kHz graph also matches the original binary.
The 6/8-input results are **synthetic native graphs**, not a claim that the
product already accepts live surround sources. Offline benchmark threads are
not PipeWire RT threads; their deadline exceedances include scheduler noise.

### Live comparison

The same Scarlett Pro Audio output, 2-input/4-output graph, unchanged IR,
periodic float32 source, paused radio owner, and active post-effect meter taps
were used before and after. Each case has 25 one-second `pw-top` snapshots
following 5 s of source warm-up. Hardware quanta were verified equal, rather
than inferred from the source client's 1024-frame request. The CPU governor
remained `powersave`; no affinity, clock, priority, or quantum tuning was used.

The final after-run restores the original meter consumer topology. An earlier
after-run immediately after service restart lacked that consumer; it is not
the source of the table below. Busy values are medians of `pw-top`'s sampled
latest-cycle durations, **not individual callback p99 or worst-case measurements**.
Unavailable BUSY readings are omitted from the medians. Three high-rate
before cases have fewer valid samples: 176.4 kHz Both has 23/25, 192 kHz
Convolver has 22/25, and 192 kHz Both has 16/25. Counter deltas still use the
first and last snapshots.
ERR columns are last-minus-first counter deltas, not historic totals and not
independent counts of audible glitches (one late graph cycle can affect
several counters).

| Rate | Workload | Hardware quantum | DSP busy ms, before → after | DSP ERR | Driver ERR | Hardware output ERR | Meter ERR |
|---|---|---:|---:|---:|---:|---:|---:|
| 48 kHz | Convolver | 1024 | 4.7 → 3.2 | 0 → 0 | 0 → 0 | 0 → 0 | 0 → 0 |
| 48 kHz | Both | 1024 | 6.1 → 4.1 | 0 → 0 | 0 → 0 | 0 → 0 | 0 → 0 |
| 96 kHz | Convolver | 4096 | 14.0 → 12.0 | 0 → 0 | 0 → 0 | 0 → 0 | 0 → 0 |
| 96 kHz | Both | 4096 | 19.0 → 16.2 | 0 → 0 | 0 → 0 | 0 → 0 | 0 → 0 |
| 176.4 kHz | Convolver | 8192 | 34.8 → 21.9 | 0 → 0 | 0 → 0 | 0 → 0 | 0 → 0 |
| 176.4 kHz | Both | 8192 | 43.6 → 25.8 | 118 → 2 | 240 → 3 | 0 → 0 | 0 → 0 |
| 192 kHz | Convolver | 4096 | 19.6 → 10.4 | 39 → 0 | 105 → 1 | 10 → 0 | 5 → 1 |
| 192 kHz | Both | 4096 | 23.5 → 13.3 | 363 → 3 | 725 → 7 | 0 → 0 | 0 → 2 |

The previously stable measured cases remain error-free, and critical CPU/
busy time is lower. However, **strict zero-XRun acceptance is not established
for the boundary loads**: 176.4 kHz with Crystalizer and 192 kHz still produce
occasional driver/filter/meter errors. These are not universally stable rates,
and no complete surround RT certification is claimed. Paused-tail denormals
also remain. Further scheduler/deadline tracing is needed before attributing
the remaining outliers specifically to worker wake-ups or other system load.

### Verification

- Local full suite: **530 passed, 0 failed, 17 explicit skips** (native/hardware
  prerequisites unavailable locally).
- On the native dependency-equipped host: all 15 `test_native_dsp_*.py`
  modules and the LV2, master-gain, and loudness-transition C wrappers pass;
  `build.sh` also runs the autogain and Crystalizer self-tests.
- Original-binary stream parity passes at 48/96/176.4 kHz, short/long IRs,
  stage/output-bank convolution, 2/6 channels, quanta 17/128/511/1024,
  silence tails and restart. Additional independent MAC tests cover
  unaligned buffers, scalar tails, signed zero, subnormals, and cancellation.
- No changes to sample-rate conversion quality, floating-point mode,
  filter design, delays, wet/dry gains, routing, or worker topology.

## Parallelization assessment

The complete stage chain and output banks are processed by one PipeWire RT
callback. Crystalizer has one global helper, only for inputs 0/1; inputs 2+
remain sequential. The helper is created on the first pair-processing call,
uses mutex/condition synchronization, and does not explicitly request RT
priority. On the profiled live process, the callback is `SCHED_RR/20` but
the helper is `SCHED_OTHER`; synchronous waiting can therefore also expose
scheduler jitter. This is not a general surround worker pool. The existing
mechanism is unchanged by these arithmetic optimizations.

Useful next steps, in order:

- Prepare a **bounded worker pool outside the RT callback**, with explicit
  lifetime, scheduling/priority policy, and failure fallback. The live engine
  owns one pool for the whole process and lends it to every graph; offline
  tools own theirs.
  Parallelize independent convolver/Crystalizer channels, then independent
  output banks after the matrix. Keep per-channel state on one worker and
  stage barriers; autogain/LV2 stereo pairs remain indivisible jobs.
  Measure wake-up/barrier p99 and worst-case time, not just total CPU usage.
  Adding an asynchronous quantum would change latency and is not equivalent.
- Consider **non-uniform delayed tail partitions** for long IRs, preserving
  the direct head and scheduling deadlines. This addresses the high-rate
  quadratic work/cache traffic rather than spreading it over more cores.
- Real/half-spectrum transforms and shared input spectra can further reduce
  work, but require independent numerical/latency validation; filters driven
  by different matrix/PEQ signals cannot blindly share histories.
- Handle pause-tail denormals as a separate, explicitly specified numerical
  change with decay/silence and resume regression coverage. Global fast-math
  or process-wide floating-point mode changes are not an acceptable shortcut.

Arithmetic vectorization reduces work without adding worker wake-ups, memory
allocation, synchronization, buffering, or audio latency to the RT path.

## Graph-owned worker scaling

The worker-pool comparison uses **current main at `69286835`** as its reference,
including the preceding SIMD improvements. The assessment above describes
that reference's global lazy Crystalizer helper, not the new pool.

The measurements below describe `4fde713e`'s per-graph pool, not a remeasurement
of the engine-owned pool. The live engine now sizes one pool per process and
lends it to compatible graphs. Arithmetic and stage barriers are unchanged;
pool capacity can differ from the old graph-specific allocation.

### Execution and scheduling policy

- **The live engine owns one pool for the whole process.** It is prepared after
  graph loading and before the filter activates, then lent to every graph,
  including later compatible replacements. Live graphs borrow helpers; offline
  graphs retain their existing owned pools and free them in `fxdsp_free()`.
  Live swaps no longer create, admit, or join pool helpers. Graph loading,
  reclamation and other stage cleanup still contribute to the 1 s reply budget;
  this is not a hard deadline guarantee. The live pool is joined after
  `pw_filter_destroy()` has synchronized filter deactivation with the data
  loop and before the context is destroyed.
- Use at most three helpers, also limited by the caller's available processors
  minus one and by the widest possible dispatch of the fixed channel layout.
  Capacity is fixed for the engine layout: a serial graph leaves helpers idle
  rather than churning them, and the helpers a graph engages are reported
  separately (`worker_used` vs. `workers`). `worker_used` is the maximum planned
  helper use, not an instantaneous count or a bypass-aware callback sample.
  The callback contributes one lane.
  Job `channel % (helpers + 1)` has stable ownership within that graph.
- Complete each stage synchronously before the next stage or matrix reads its
  scratch buffers. Output-bank jobs preserve route summation order, then run
  that output's PEQ/convolver, delay, gain, mute, and meters. Unlinked outputs
  still advance bank histories but retain the original skipped delay/meters.
- Helpers use release/acquire sequence publication and predicate-checked Linux
  private futex waits. The callback sleeps at unfinished barriers rather than
  spinning at RT priority; it creates no threads, allocates no memory, and owns
  no synchronization mutex. Non-Linux pool preparation falls back to serial.
- Autogain and LV2 stereo instances remain indivisible **serial** jobs on the
  callback. Their existing asynchronous estimator and shared compensation
  timing are not moved to helpers in this step. Each parallel phase propagates
  the processing caller's FP environment, including rounding and denormal mode,
  rather than inheriting the graph-preparation thread's potentially different
  environment. This does not enable FTZ/DAZ or change resampler/filter quality.
- The live engine requests PipeWire's configured RT priority via its thread
  **context-owned** thread utilities for creation, RT acquisition, and joining
  (including the existing permission broker and its thread registry). It
  verifies each helper's kernel TID scheduler/priority, allowing up to 500 ms
  per helper at startup for asynchronous broker grants and masking reset-on-fork
  policy flags. Any failed
  acquisition, inconsistent scheduling, or partial thread creation destroys
  all prepared helpers and selects serial processing. Offline tools explicitly
  use normal-priority helpers. No service privilege or RT-limit changes are
  required, and no affinity/governor tuning is applied. The control socket is
  started after this preparation; its 1 s reply timeout does not cover startup
  admission. Engine startup/readiness still includes those costs.
- `FXROUTE_DSP_WORKERS=0` forces serial execution; values `1` through `3` cap
  the pool, and invalid values select serial execution. The live engine reads
  it **once at startup**, not per graph load, so changing it later requires a
  restart.
- A stateful job already running cannot safely be cancelled, duplicated, or
  abandoned at a timeout. The barrier must drain even after a deadline miss;
  the serial fallback handles **preparation failures**, not a hung running
  kernel. Graph reclamation also waits for every synchronous helper job.

### Timing definitions

`FXROUTE_DSP_TIMING=1` enables fixed-size lock-free atomic histograms in the
live engine. The non-RT datagram control socket accepts `timing reset` and
`timing get`. Timing is disabled for ordinary product playback. Histograms
stay **graph-owned**: a swapped-in graph starts with its own empty histograms,
and `timing get` reports these worker fields:

- `workers`: successfully prepared helper capacity.
- `worker_limit`: intended startup capacity after environment/layout/CPU caps,
  retained even when pool preparation fails.
- `worker_jobs` / `worker_used`: graph demand and maximum planned helper use.
- `worker_mode`: `disabled` for zero intended capacity; `degraded` for failed
  preparation despite positive capacity; `serial` for a serial graph with a
  prepared pool; `pool` for a graph eligible to use that pool. This is not a
  bypass-aware instantaneous callback measurement. `degraded` does not identify
  a specific RTKit error: missing thread utilities or other preparation failures
  can produce it too.

Failed startup preparation keeps that engine serial; graph swaps do not retry
admission. Startup stderr reports the same capacity/limit/mode distinction.
Metric labels come from one enum-indexed table, with a compile-time count guard.
The native benchmark enables timing by default and includes these objects in
its JSON; the ordinary product engine remains disabled by default:

- `callback`: full live processing callback (buffer acquisition and incomplete
  buffer handling included); offline, the measured DSP invocation.
- `barrier`: residual wait after the callback finishes its own lane, per
  parallel phase. This includes helper work remaining, not just futex cost.
- `wake`: dispatch publication to helper entry, per awakened helper. This
  includes publication/wake syscall ordering and scheduler delay; it is not
  an isolated kernel wakeup-latency measurement.

Each object reports sample count, `p99_upper_ms`, and exact observed `max_ms`.
Histogram p99 is a nearest-rank bucket upper bound (32 subdivisions per power
of two, at most about 3.125% relative bucket width), capped by the observed
maximum. Reset is requested by control and acknowledged at the next drained
callback boundary; a 250 ms control timeout leaves the request pending rather
than clearing metrics mid-job. Snapshot readers retry reset generations so
retained samples cannot lose their maximum. Live snapshots are not atomic cuts
across all running jobs. Maxima are **observed**, not a
hard-real-time WCET guarantee. `pw-top` sampled latest-cycle durations remain separate from
individual callback timing.

### Matched offline worker results

These are medians of three interleaved reference/pool runs on the same N150,
using 10 s of deterministic input, quantum 1024, the unchanged 32,825-frame
48 kHz stereo IR, and the same `-O2` compiler/library setup. Both binaries
include the same timing histogram implementation; the instrumented reference
keeps `69286835`'s DSP and global-helper synchronization unchanged. Native
tests and live measurements did not overlap these runs.

| Workload | Rate kHz | Inputs → outputs | Critical wall s, reference → pool | Callback CPU s, reference → pool | Total CPU s, reference → pool | Wall reduction | Offline misses, total across three runs |
|---|---:|---:|---:|---:|---:|---:|---:|
| Convolver | 176.4 | 2 → 4 | 3.815 → 2.460 | 3.800 → 2.256 | 3.803 → 4.672 | 35.5% | 0 → 0 |
| Convolver | 192 | 2 → 4 | 4.609 → 2.957 | 4.517 → 2.726 | 4.520 → 5.631 | 35.8% | 0 → 0 |
| Both | 176.4 | 2 → 4 | 5.290 → 3.919 | 5.216 → 3.623 | 6.490 → 7.466 | 25.9% | 1 → 2 |
| Both | 192 | 2 → 4 | 6.270 → 4.660 | 6.181 → 4.301 | 7.651 → 8.836 | 25.7% | 14 → 0 |
| Stage Convolver + Crystalizer | 48 | 6 → 6 | 2.317 → 1.174 | 2.286 → 1.097 | 2.551 → 3.418 | 49.3% | 0 → 0 |
| Output-bank Convolver + Crystalizer | 48 | 6 → 6 | 2.268 → 1.124 | 2.238 → 1.074 | 2.491 → 3.357 | 50.4% | 0 → 0 |
| Stage Convolver + Crystalizer | 48 | 8 → 8 | 3.133 → 1.459 | 3.099 → 1.225 | 3.363 → 4.845 | 53.4% | 1 → 0 |
| Output-bank Convolver + Crystalizer | 48 | 8 → 8 | 3.101 → 1.396 | 3.074 → 1.185 | 3.334 → 4.728 | 55.0% | 0 → 0 |

All paired output hashes match, including the small-quantum probes. The
6/8-channel graphs are synthetic native graphs, not newly supported product
surround sources. Stage cases have channel-local output PEQ; bank cases put
the Convolver after that PEQ. Crystalizer remains before the matrix in both.

The tradeoff is explicit: callback CPU and critical wall time decrease, but
total CPU increases by roughly 15–44%. This is synchronous scaling, not a
reduction in total DSP work. A single 8-channel stage-graph pair at quantum
128 reduces offline deadline misses from 468 to 75; at quantum 256, from 20
to 1. Neither probe establishes live stability. The combined 176.4 kHz pool
also has an 8.048 ms observed offline outlier, versus 5.934 ms in the reference.

### Matched live worker results

Thirteen cases × two interleaved repetitions × reference/pool (52 runs) on
the same N150/Scarlett hardware, with identical configs, stereo IR,
deterministic periodic source, `-30 dB` gain, 5 s warm-up, and active
post-effect meter taps. Higher rates use the existing Scarlett tier
mechanism (`14ch` for 96 kHz, `10ch` for 176.4/192 kHz) with capture/rollback
to the original `18ch` tier; the physical graph rate/quantum is verified per
run. Pool helpers and the callback are kernel-verified `SCHED_RR/20` in the
systemd user-manager context. Timing reset is drained at a callback boundary;
control waits at most 250 ms.

Validation: every run has ≥20 common running `pw-top` snapshots covering the
DSP, source, meter, capture-driver, and hardware-output nodes, with stable
node IDs, monotonic ERR counters, and exact delta agreement
(`validate_live.py`). Callback CPU counts only the verified `RR/20`
`data-loop.0` thread, so the reference Crystalizer helper is not
misattributed. Reported callback/barrier/wake p99 is the worse per-run
histogram bucket upper bound (not pooled p99); observed maxima are exact.
Zero-sample barrier/wake entries mean N/A, not zero latency. `pw-top` BUSY is
the median of sampled latest-cycle durations, separate from individual
callback timing. ERR columns sum both repetitions per label (DSP / capture
driver / hardware output / meter / source).

| Case | Rate/quantum | Callback p99 ms, ref → pool | Callback max ms, ref → pool | Barrier p99/max pool ms | Wake p99/max pool ms | BUSY median ms, ref → pool | Callback CPU %, ref → pool | Total CPU %, ref → pool | ERR DSP/driver/output/meter/source, ref → pool |
|---|---|---:|---:|---:|---:|---:|---:|---:|---|
| conv-48000 | 48 kHz/1024 | 3.736 → 2.425 | 3.758 → 2.493 | 0.319/0.367 | 0.113/0.178 | 3.35 → 2.15 | 15.9 → 9.2 | 15.9 → 19.5 | 0/0/0/0/0 → 0/0/0/0/0 |
| both-48000 | 48 kHz/1024 | 5.636 → 4.981 | 6.009 → 4.989 | 0.483/0.514 | 0.109/0.230 | 5.35 → 4.38 | 23.7 → 18.7 | 32.2 → 38.3 | 0/0/0/0/0 → 0/0/0/0/0 |
| stage-6ch | 48 kHz/1024 | 11.010 → 8.258 | 12.045 → 8.368 | 0.369/0.480 | 0.106/0.214 | 9.45 → 7.42 | 44.1 → 33.9 | 48.7 → 106.5 | 0/0/0/0/0 → 0/0/0/0/0 |
| bank-6ch | 48 kHz/1024 | 11.272 → 8.389 | 12.618 → 8.479 | 0.418/0.488 | 0.117/0.274 | 8.75 → 7.75 | 42.4 → 34.2 | 49.7 → 107.6 | 0/0/0/0/0 → 0/0/0/0/0 |
| stage-8ch | 48 kHz/1024 | 9.699 → 8.913 | 12.527 → 9.033 | 0.557/0.662 | 0.113/0.205 | 8.75 → 8.50 | 41.3 → 37.5 | 44.4 → 147.7 | 0/0/0/0/0 → 0/0/0/0/0 |
| bank-8ch | 48 kHz/1024 | 11.534 → 8.913 | 14.173 → 9.166 | 0.508/0.789 | 0.104/0.193 | 9.45 → 8.45 | 44.5 → 37.5 | 50.5 → 146.7 | 0/0/0/0/0 → 0/0/0/0/0 |
| stage-8ch-q256 | 48 kHz/256 | 7.995 → 5.636 | 9.169 → 5.758 | 0.385/0.600 | 0.139/0.301 | 1.98 → 1.75 | 45.5 → 38.3 | 49.5 → 149.2 | 761/1523/0/0/0 → 729/1466/2/2/72 |
| conv-96000 | 96 kHz/4096 | 13.894 → 10.748 | 14.975 → 11.800 | 0.393/0.472 | 0.152/0.200 | 13.03 → 9.95 | 30.6 → 20.8 | 30.7 → 43.8 | 0/0/0/0/0 → 0/0/0/0/0 |
| both-96000 | 96 kHz/4096 | 18.350 → 15.729 | 21.386 → 16.872 | 0.328/0.474 | 0.117/0.179 | 16.85 → 14.93 | 38.4 → 32.5 | 49.2 → 65.9 | 0/0/0/0/0 → 0/0/0/0/0 |
| conv-176400 | 176.4 kHz/8192 | 24.642 → 25.166 | 27.938 → 34.082 | 0.385/0.530 | 0.117/0.205 | 21.42 → 20.50 | 46.3 → 42.0 | 46.3 → 85.8 | 0/0/0/0/0 → 0/0/0/0/0 |
| both-176400 | 176.4 kHz/8192 | 29.360 → 27.787 | 32.655 → 37.694 | 0.246/0.561 | 0.080/0.168 | 25.35 → 22.82 | 54.0 → 48.1 | 66.2 → 97.0 | 0/0/0/0/0 → 0/0/0/0/0 |
| conv-192000 | 192 kHz/4096 | 11.010 → 13.894 | 17.661 → 17.231 | 0.270/0.493 | 0.092/0.177 | 9.90 → 10.30 | 46.6 → 46.4 | 46.6 → 94.4 | 0/0/0/0/0 → 0/0/0/0/0 |
| both-192000 | 192 kHz/4096 | 15.729 → 14.156 | 18.494 → 22.896 | 0.217/0.482 | 0.076/0.170 | 12.28 → 11.00 | 57.6 → 48.4 | 70.4 → 98.3 | 0/0/0/0/0 → 1/1/0/0/0 |

At 48 kHz/1024 the 6/8-channel graphs stay ERR-free while callback p99
falls roughly 8–26% and callback CPU falls, but total process CPU rises
sharply (three helpers + callback). The 256-frame 8-channel case is unstable
in both builds (hundreds of DSP/driver ERR deltas); the pool does not make it
stable. High-rate behavior is mixed: 96 kHz improves, while 176.4/192 kHz
show equal or worse p99/maxima in several pool runs and one residual
DSP/driver ERR pair in `both-192000`. The established boundaries therefore
stand: 176.4 kHz with Crystalizer and 192 kHz are not universally XRun-free,
and no complete surround RT certification is claimed. Synthetic 6/8-channel
graphs are not newly supported product surround sources.

The historical 24-swap smoke used the per-graph pool and a 30 s socket timeout;
it did not establish compliance with the production 1 s reply deadline.

### Live-pool reuse verification

The ownership repair was checked separately on the same N150, at 44.1 kHz
with a requested 1024-frame quantum. An 8-input/8-output synthetic graph cycles
through serial, stage-Convolver + Crystalizer, and output-bank-Convolver +
Crystalizer configurations using a 32,825-frame mono IR. All control requests
use the production **1.0 s** socket timeout. These are short lifecycle tests,
not new performance or XRun-certification measurements.

| Engine | Mode | Successful swaps | Maximum observed reply ms |
|---|---|---:|---:|
| Product binary, no test instrumentation | Pool | 30 | 68.529 |
| Product binary, no test instrumentation | Forced serial | 30 | 61.104 |
| Test probe | Pool | 30 | 66.266 |
| Test probe | Forced serial | 30 | 69.038 |

- Pool helper kernel TIDs, start times, and `SCHED_RR/20` remain identical
  throughout each engine's swaps. Capacity remains three even across serial
  graphs; planned use changes between zero and three. Nonzero barrier/wake
  samples establish helper execution in every pooled stage/bank check.
- The test probe observes real SPA calls: exactly three creates/admissions at
  startup, none during swaps, and three joins at shutdown. Forced serial has
  zero creates, admissions, and joins. Invalid replacement configs leave the
  existing pool intact.
- A real Crystalizer helper is blocked after processing starts, then the engine
  receives SIGTERM. Filter destruction enters but cannot finish during the
  150 ms observation interval; no helper joins occur. Releasing the job allows
  filter destruction to complete, then all helpers join and the engine exits
  cleanly. No production shutdown gate or test hook is added.
- Native regressions compare PCM, post-effect taps, and meters byte-for-byte
  against serial runs at quanta 17/128/511/1024/8192, across 2/6/8-channel graphs
  and graph A followed by a narrower graph B on the same pool. Independently
  owned offline pools are also processed concurrently. Counted lifecycle tests
  verify that borrowed graph destruction never joins the pool, while offline
  graph destruction still does; graph timing remains independent.

The opt-in verifier is `scripts/verify_native_worker_swaps.py`; it requires an
unused product DSP node and a separately built `native_dsp/test_engine_workers.c`
probe, linked with `-Wl,--wrap=fx_crystalizer_process` instead of compiling
`pipewire_engine.c` separately. It creates only synthetic source/meter links and
does not change hardware links, services, or global clock metadata.

Reuse removes per-swap **pool** RT-admission requests, not all user-wide RTKit
activity: other processes or existing stage helpers can still consume that
budget. Failed startup admission retains serial fallback until engine restart.
Graph loading/drain/free still has no universal subsecond bound. The high-rate
and small-quantum limitations above remain unchanged.
