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
window averages, **not individual callback p99 or worst-case measurements**.
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

- Prepare a **bounded, graph-owned worker pool outside the RT callback**,
  with explicit lifetime, scheduling/priority policy, and failure fallback.
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
