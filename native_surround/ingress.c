/* SPDX-License-Identifier: AGPL-3.0-only */
#define _GNU_SOURCE
#include "surround.h"
#include <pipewire/pipewire.h>
#include <spa/param/audio/format-utils.h>
#include <errno.h>
#include <pthread.h>
#include <signal.h>
#include <stdatomic.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#define QUEUE 32768
struct input_frame { uint64_t seq; float pcm[2]; };
struct output_frame { uint64_t seq; float pcm[SURROUND_OUTPUTS]; };
struct engine {
    struct pw_main_loop *loop;
    struct pw_filter *filter;
    struct spa_hook listener;
    void *inputs[2], *outputs[SURROUND_OUTPUTS];
    struct input_frame input[QUEUE];
    struct output_frame output[QUEUE];
    _Atomic uint64_t input_read, input_write, output_read, output_write;
    _Atomic uint64_t faults;
    _Atomic int stopping;
    uint64_t seq, base;
    int has_input;
    pthread_t worker;
    surround *decoder;
    const char *status;
};

static void live_emit(void *data, uint64_t frame, const float pcm[SURROUND_OUTPUTS]) {
    struct engine *e = data;
    uint64_t w = atomic_load_explicit(&e->output_write, memory_order_relaxed);
    uint64_t r = atomic_load_explicit(&e->output_read, memory_order_acquire);
    if (w - r >= QUEUE) { atomic_fetch_add(&e->faults, 1); return; }
    e->output[w % QUEUE].seq = e->base + frame;
    memcpy(e->output[w % QUEUE].pcm, pcm, sizeof e->output[0].pcm);
    atomic_store_explicit(&e->output_write, w + 1, memory_order_release);
}

static void *decode_worker(void *data) {
    struct engine *e = data;
    uint64_t next = 0, last_fault = 0;
    unsigned status_ticks = 0;
    int started = 0;
    while (!atomic_load(&e->stopping)) {
        uint64_t r = atomic_load_explicit(&e->input_read, memory_order_relaxed);
        uint64_t w = atomic_load_explicit(&e->input_write, memory_order_acquire);
        uint64_t fault = atomic_load(&e->faults);
        if (last_fault != fault) {
            surround_reset(e->decoder);
            e->base = next;
            last_fault = fault;
        }
        /* Only this thread owns codec state and filesystem status updates. */
        while (r < w && !atomic_load(&e->stopping)) {
            struct input_frame f = e->input[r % QUEUE];
            if (!started) {
                e->base = f.seq;
                started = 1;
            } else if (f.seq != next) {
                surround_reset(e->decoder);
                e->base = f.seq;
            }
            surround_push(e->decoder, f.pcm, 1);
            next = f.seq + 1;
            atomic_store_explicit(&e->input_read, ++r, memory_order_release);
        }
        if (++status_ticks >= 100) {
            if (surround_write_status(e->decoder, e->status)) {
                fprintf(stderr, "cannot write Surround status: %s\n", strerror(errno));
                pw_main_loop_quit(e->loop);
                break;
            }
            status_ticks = 0;
        }
        struct timespec wait = {.tv_nsec = 1000000};
        nanosleep(&wait, NULL);
    }
    return NULL;
}

static void process(void *data, struct spa_io_position *position) {
    struct engine *e = data;
    if (!position || !position->clock.duration) return;
    uint32_t frames = position->clock.duration;
    const float *left = pw_filter_get_dsp_buffer(e->inputs[0], frames);
    const float *right = pw_filter_get_dsp_buffer(e->inputs[1], frames);
    float *output[SURROUND_OUTPUTS];
    for (unsigned c = 0; c < SURROUND_OUTPUTS; c++) {
        output[c] = pw_filter_get_dsp_buffer(e->outputs[c], frames);
        if (output[c]) memset(output[c], 0, (size_t)frames * sizeof(float));
    }
    /* This filter does not force a hardware/graph rate. Wrong clocks fail closed. */
    int clock_valid = position->clock.rate.num == 1 && position->clock.rate.denom == SURROUND_RATE;
    int valid = left && right && clock_valid;
    /* Unlinked startup is not a stream discontinuity: the first PCM source
     * must still receive recognition lookahead rather than an unknown latch. */
    if (!clock_valid || (!valid && e->has_input)) atomic_fetch_add(&e->faults, 1);
    uint64_t iw = atomic_load_explicit(&e->input_write, memory_order_relaxed);
    uint64_t ir = atomic_load_explicit(&e->input_read, memory_order_acquire);
    uint64_t or = atomic_load_explicit(&e->output_read, memory_order_relaxed);
    uint64_t ow = atomic_load_explicit(&e->output_write, memory_order_acquire);
    if (valid && frames > QUEUE - (iw - ir)) {
        valid = 0;
        atomic_fetch_add(&e->faults, 1);
    }
    if (valid) e->has_input = 1;
    for (uint32_t i = 0; i < frames; i++, e->seq++) {
        if (valid) {
            e->input[iw % QUEUE] = (struct input_frame){.seq = e->seq, .pcm = {left[i], right[i]}};
            iw++;
        }
        if (e->seq < SURROUND_LIVE_LATENCY) continue;
        uint64_t wanted = e->seq - SURROUND_LIVE_LATENCY;
        while (or < ow && e->output[or % QUEUE].seq < wanted) or++;
        if (or < ow && e->output[or % QUEUE].seq == wanted) {
            if (valid) for (unsigned c = 0; c < SURROUND_OUTPUTS; c++)
                if (output[c]) output[c][i] = e->output[or % QUEUE].pcm[c];
            or++;
        }
    }
    atomic_store_explicit(&e->input_write, iw, memory_order_release);
    atomic_store_explicit(&e->output_read, or, memory_order_release);
}

static struct engine *signal_engine;
static void stop_signal(void *data, int number) {
    (void)number;
    struct engine *e = data;
    pw_main_loop_quit(e->loop);
}

static void filter_state(void *data, enum pw_filter_state old, enum pw_filter_state state, const char *error) {
    (void)old;
    struct engine *e = data;
    if (state == PW_FILTER_STATE_ERROR || state == PW_FILTER_STATE_UNCONNECTED) {
        fprintf(stderr, "Surround filter disconnected: %s\n", error ? error : "connection closed");
        atomic_fetch_add(&e->faults, 1);
        pw_main_loop_quit(e->loop);
    }
}

static const struct pw_filter_events events = {
    PW_VERSION_FILTER_EVENTS, .state_changed = filter_state, .process = process,
};

struct offline_writer { FILE *file; int error, stereo; };
static void offline_emit(void *data, uint64_t frame, const float pcm[SURROUND_OUTPUTS]) {
    (void)frame;
    struct offline_writer *w = data;
    unsigned channels = w->stereo ? 2 : 8;
    if (fwrite(pcm + (w->stereo ? 8 : 0), sizeof(float), channels, w->file) != channels) w->error = 1;
}

static int offline(const char *input, const char *output, const char *status, unsigned chunk, int stereo_output) {
    if (!strcmp(input, output)) { fprintf(stderr, "input and output must differ\n"); return 1; }
    FILE *in = fopen(input, "rb");
    if (!in) { perror(input); return 1; }
    struct offline_writer w = {.file = fopen(output, "wb"), .stereo = stereo_output};
    if (!w.file) { perror(output); fclose(in); return 1; }
    surround *d = surround_create(offline_emit, &w);
    uint8_t *raw = malloc((size_t)chunk * 8);
    float *stereo = malloc((size_t)chunk * 2 * sizeof(float));
    if (!d || !raw || !stereo) w.error = 1;
    while (!w.error) {
        size_t bytes = fread(raw, 1, (size_t)chunk * 8, in);
        if (bytes % 8) { fprintf(stderr, "unaligned stereo S32 carrier\n"); w.error = 1; break; }
        if (!bytes) break;
        for (size_t i = 0; i < bytes / 4; i++) {
            uint32_t u = (uint32_t)raw[i * 4] | (uint32_t)raw[i * 4 + 1] << 8 |
                         (uint32_t)raw[i * 4 + 2] << 16 | (uint32_t)raw[i * 4 + 3] << 24;
            stereo[i] = (float)((double)(int32_t)u / 2147483648.0);
        }
        surround_push(d, stereo, bytes / 8);
    }
    if (ferror(in)) w.error = 1;
    if (d && !w.error) {
        surround_finish(d);
        if (surround_write_status(d, status)) { perror("Surround status"); w.error = 1; }
    }
    surround_destroy(d);
    free(raw); free(stereo);
    fclose(in);
    if (fclose(w.file)) w.error = 1;
    return w.error ? 1 : 0;
}

static int live(const char *status) {
    struct engine *e = calloc(1, sizeof *e);
    if (!e) return 1;
    e->status = status;
    e->decoder = surround_create(live_emit, e);
    pw_init(NULL, NULL);
    e->loop = pw_main_loop_new(NULL);
    if (!e->decoder || !e->loop) goto error;
    e->filter = pw_filter_new_simple(pw_main_loop_get_loop(e->loop), "fxroute-surround-input",
        pw_properties_new(PW_KEY_MEDIA_TYPE, "Audio", PW_KEY_MEDIA_CATEGORY, "Filter",
            PW_KEY_MEDIA_ROLE, "DSP", PW_KEY_NODE_NAME, "fxroute_surround_input",
            PW_KEY_NODE_AUTOCONNECT, "false", "node.dont-reconnect", "true",
            "node.want-driver", "true", "node.rate", "1/48000", "audio.rate", "48000", NULL),
        &events, e);
    if (!e->filter) goto error;
    const char *input_names[2] = {"carrier_L", "carrier_R"};
    for (unsigned i = 0; i < 2; i++) {
        e->inputs[i] = pw_filter_add_port(e->filter, PW_DIRECTION_INPUT,
            PW_FILTER_PORT_FLAG_MAP_BUFFERS, 0,
            pw_properties_new(PW_KEY_FORMAT_DSP, "32 bit float mono audio", PW_KEY_PORT_NAME, input_names[i], NULL), NULL, 0);
        if (!e->inputs[i]) goto error;
    }
    for (unsigned i = 0; i < SURROUND_OUTPUTS; i++) {
        char name[32];
        if (i < 8) snprintf(name, sizeof name, "output_%s", surround_positions[i]);
        else snprintf(name, sizeof name, "output_stereo_%s", surround_positions[i - 8]);
        e->outputs[i] = pw_filter_add_port(e->filter, PW_DIRECTION_OUTPUT,
            PW_FILTER_PORT_FLAG_MAP_BUFFERS, 0,
            pw_properties_new(PW_KEY_FORMAT_DSP, "32 bit float mono audio", PW_KEY_PORT_NAME, name, NULL), NULL, 0);
        if (!e->outputs[i]) goto error;
    }
    if (surround_write_status(e->decoder, status)) goto error;
    if (pw_filter_connect(e->filter, PW_FILTER_FLAG_RT_PROCESS, NULL, 0) < 0) goto error;
    if (pthread_create(&e->worker, NULL, decode_worker, e)) goto error;
    signal_engine = e;
    pw_loop_add_signal(pw_main_loop_get_loop(e->loop), SIGINT, stop_signal, e);
    pw_loop_add_signal(pw_main_loop_get_loop(e->loop), SIGTERM, stop_signal, e);
    pw_main_loop_run(e->loop);
    atomic_store(&e->stopping, 1);
    pw_filter_set_active(e->filter, false);
    pthread_join(e->worker, NULL);
    pw_filter_destroy(e->filter);
    pw_main_loop_destroy(e->loop);
    surround_destroy(e->decoder);
    signal_engine = NULL;
    free(e);
    return 0;
error:
    fprintf(stderr, "cannot start Surround ingress\n");
    if (e->filter) pw_filter_destroy(e->filter);
    if (e->loop) pw_main_loop_destroy(e->loop);
    surround_destroy(e->decoder);
    free(e);
    return 1;
}

int main(int argc, char **argv) {
    const char *input = NULL, *output = NULL, *status = NULL;
    unsigned chunk = 257;
    int stereo = 0;
    for (int i = 1; i < argc; i++) {
        if (!strcmp(argv[i], "--offline") && i + 2 < argc) { input = argv[++i]; output = argv[++i]; }
        else if (!strcmp(argv[i], "--status") && i + 1 < argc) status = argv[++i];
        else if (!strcmp(argv[i], "--stereo")) stereo = 1;
        else if (!strcmp(argv[i], "--chunk") && i + 1 < argc) {
            char *end; unsigned long n = strtoul(argv[++i], &end, 10);
            if (*end || n < 1 || n > 16384) goto usage;
            chunk = (unsigned)n;
        } else goto usage;
    }
    if (stereo && !input) goto usage;
    return input ? offline(input, output, status, chunk, stereo) : live(status);
usage:
    fprintf(stderr, "usage: %s [--offline INPUT.s32 OUTPUT.f32 [--stereo]] [--status PATH] [--chunk FRAMES]\n", argv[0]);
    return 2;
}
