#include "timing.h"

static unsigned bucket(uint64_t ns) {
    if (ns < 64) return (unsigned)ns;
    unsigned exponent = 6;
    uint64_t v = ns >> 6;
    while (v >>= 1) exponent++;
    unsigned index = 64 + (exponent - 6) * 32 + (unsigned)((ns - (UINT64_C(1) << exponent)) >> (exponent - 5));
    return index < FX_TIMING_BINS ? index : FX_TIMING_BINS - 1;
}
static uint64_t upper_bound(unsigned index) {
    if (index < 64) return index;
    unsigned exponent = 6 + (index - 64) / 32;
    return (UINT64_C(1) << exponent) + (uint64_t)((index - 64) % 32 + 1) * (UINT64_C(1) << (exponent - 5)) - 1;
}
int fx_timing_available(const fx_timing *t) {
    return atomic_is_lock_free(&t->bins[0]) && atomic_is_lock_free(&t->maximum) && atomic_is_lock_free(&t->reset_epoch);
}
void fx_timing_record(fx_timing *t, uint64_t ns) {
    uint64_t current = atomic_load_explicit(&t->maximum, memory_order_relaxed);
    while (current < ns && !atomic_compare_exchange_weak_explicit(&t->maximum, &current, ns,
                                                                 memory_order_relaxed, memory_order_relaxed)) {}
    atomic_fetch_add_explicit(&t->bins[bucket(ns)], 1, memory_order_release);
}
fx_timing_summary fx_timing_read(const fx_timing *t) {
    fx_timing_summary result = {0};
    uint32_t bins[FX_TIMING_BINS];
    for (;;) {
        uint32_t epoch = atomic_load_explicit(&t->reset_epoch, memory_order_acquire);
        if (epoch & 1U) continue;
        result.samples = 0;
        for (unsigned i = 0; i < FX_TIMING_BINS; i++) {
            bins[i] = atomic_load_explicit(&t->bins[i], memory_order_acquire);
            result.samples += bins[i];
        }
        result.max_ns = atomic_load_explicit(&t->maximum, memory_order_acquire);
        if (epoch == atomic_load_explicit(&t->reset_epoch, memory_order_acquire)) break;
    }
    if (!result.samples) return result;
    uint64_t rank50 = (result.samples + 1) / 2, rank99 = (result.samples * 99 + 99) / 100, count = 0;
    int have50 = 0;
    for (unsigned i = 0; i < FX_TIMING_BINS; i++) {
        count += bins[i];
        uint64_t upper = i == FX_TIMING_BINS - 1 ? result.max_ns : upper_bound(i);
        if (upper > result.max_ns) upper = result.max_ns;
        if (!have50 && count >= rank50) { result.p50_ns = upper; have50 = 1; }
        if (count >= rank99) { result.p99_ns = upper; break; }
    }
    return result;
}
void fx_timing_reset(fx_timing *t) {
    atomic_fetch_add_explicit(&t->reset_epoch, 1, memory_order_acq_rel);
    for (unsigned i = 0; i < FX_TIMING_BINS; i++) atomic_store_explicit(&t->bins[i], 0, memory_order_release);
    atomic_store_explicit(&t->maximum, 0, memory_order_release);
    atomic_fetch_add_explicit(&t->reset_epoch, 1, memory_order_release);
}
