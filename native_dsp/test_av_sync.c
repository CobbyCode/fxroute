#define _POSIX_C_SOURCE 200809L
#include "dsp.h"
#include <assert.h>
#include <math.h>
#include <pthread.h>
#include <stdatomic.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

static fxdsp *load(const char *text) {
    char path[] = "/tmp/fxroute-av-sync-XXXXXX", error[256];
    int fd = mkstemp(path);
    assert(fd >= 0);
    FILE *file = fdopen(fd, "w");
    assert(file);
    fputs(text, file); fclose(file);
    fxdsp *d = fxdsp_load(path, error, sizeof error);
    unlink(path);
    if (!d) fprintf(stderr, "%s\n", error);
    assert(d);
    return d;
}

static fxdsp_latency tick(fxdsp *d) {
    float zero[1024] = {0}, output[8][1024];
    const float *in[8]; float *out[8];
    for (unsigned c = 0; c < 8; c++) { in[c] = zero; out[c] = output[c]; }
    fxdsp_process(d, in, out, 1024);
    fxdsp_latency latency;
    assert(fxdsp_read_latency(d, &latency));
    return latency;
}

typedef struct { fxdsp *d; atomic_int done; int result; } live_commit_job;

static void *commit_thread(void *context) {
    live_commit_job *job = context;
    job->result = fxdsp_live_commit(job->d);
    atomic_store(&job->done, 1);
    return NULL;
}

static void commit_while_processing(fxdsp *d) {
    live_commit_job job = {.d = d, .done = ATOMIC_VAR_INIT(0)};
    pthread_t thread;
    assert(!pthread_create(&thread, NULL, commit_thread, &job));
    while (!atomic_load(&job.done)) tick(d);
    assert(!pthread_join(thread, NULL) && job.result);
}

static fxdsp_latency assert_impulse_matches_readback(fxdsp *d) {
    for (unsigned n = 0; n < 64; n++) tick(d);
    fxdsp_latency s = tick(d);
    float input[8][509] = {{0}}, output[8][509];
    const float *in[8]; float *out[8];
    float peak[8] = {0}; size_t position[8] = {0};
    for (unsigned c = 0; c < 8; c++) { in[c] = input[c]; out[c] = output[c]; input[c][100] = 0.5f; }
    for (size_t offset = 0; offset < 50000; offset += 509) {
        fxdsp_process(d, in, out, 509);
        for (unsigned c = 0; c < fxdsp_outputs(d); c++) for (size_t n = 0; n < 509; n++) {
            if (fabsf(output[c][n]) > peak[c]) { peak[c] = fabsf(output[c][n]); position[c] = offset + n; }
        }
        memset(input, 0, sizeof input);
    }
    for (unsigned c = 0; c < fxdsp_outputs(d); c++) {
        assert(peak[c] > 0.01f);
        assert(position[c] == 100 + s.output[c].total - s.input);
    }
    return s;
}

static void test_impulses_follow_live_controls_in_stereo_and_sub_modes(void) {
    const unsigned rates[] = {44100, 48000, 96000};
    for (unsigned r = 0; r < 3; r++) for (unsigned sub = 0; sub < 2; sub++) {
        char text[2048];
        snprintf(text, sizeof text, "rate %u\ninputs 2\noutputs %u\nmatrix 0 0 1\nmatrix 1 1 1\n%s"
            "stage_begin 0 loud lv2 http://lsp-plug.in/plugins/lv2/loud_comp_stereo\n"
            "control fft 2\ncontrol volume 0\nstage_end\n"
            "output 0 0 7 normal\noutput 1 0 3 normal\n%savsync 300 0\n"
            "latency_basis 0 0\nlatency_basis 1 0\n%s", rates[r], sub ? 3 : 2,
            sub ? "matrix 2 0 0.5\nmatrix 2 1 0.5\n" : "",
            sub ? "output 2 0 11 normal\n" : "", sub ? "latency_basis 2 0\n" : "");
        fxdsp *d = load(text);
        fxdsp_latency initial = assert_impulse_matches_readback(d);
        assert(initial.actual == (uint64_t)llround(rates[r] * 0.3));
        assert(fxdsp_live_begin(d));
        assert(fxdsp_live_control(d, "loud", "fft", 5));
        commit_while_processing(d);
        fxdsp_latency changed = assert_impulse_matches_readback(d);
        assert(changed.output[0].dsp == 8192 && changed.actual == initial.actual);
        assert(changed.output[1].total - changed.output[0].total == initial.output[1].total - initial.output[0].total);
        fxdsp_set_effect_bypass(d, 1);
        changed = assert_impulse_matches_readback(d);
        assert(changed.bypassed && changed.output[0].dsp == 0 && changed.actual == initial.actual);
        assert(fxdsp_set_input_latency(d, 8192.0f * 1000.0f / rates[r]));
        changed = assert_impulse_matches_readback(d);
        assert(changed.input == 8192 && changed.actual == initial.actual);
        fxdsp_free(d);
    }
}

static void test_partial_calibration_recompile_keeps_native_impulse_alignment(void) {
    char text[2048];
    uint64_t original[3] = {0};
    for (unsigned captured = 0; captured < 2; captured++) {
        snprintf(text, sizeof text, "rate 48000\ninputs 8\nsurround 1\noutputs 3\n"
            "matrix 0 0 1\nmatrix 1 2 1\nmatrix 2 0 0.5\nmatrix 2 2 0.5\n"
            "stage_begin 0 front-eq lv2 http://lsp-plug.in/plugins/lv2/para_equalizer_x32_lr\n"
            "pairs 1\ncontrol mode 1\nstage_end\n"
            "output 0 0 0 normal\noutput 1 0 9 normal\noutput 2 0 4 normal\navsync 300 0\n"
            "source_basis 0 %u\nsource_basis 2 %u\nlatency_basis 0 %u\nlatency_basis 1 %u\nlatency_basis 2 %u\n",
            captured * 128, captured * 128, captured * 128, captured * 128, captured * 128);
        fxdsp *d = load(text);
        fxdsp_latency s = assert_impulse_matches_readback(d);
        assert(s.output[0].balance == 0 && s.output[1].balance == 6144 && !s.output[2].mixed);
        for (unsigned c = 0; c < 3; c++) {
            if (!captured) original[c] = s.output[c].total;
            else assert(s.output[c].total == original[c]);
        }
        fxdsp_set_effect_bypass(d, 1);
        s = assert_impulse_matches_readback(d);
        for (unsigned c = 0; c < 3; c++) assert(s.output[c].total == original[c]);
        fxdsp_free(d);
    }
}

static void test_alignment_and_total_credit(void) {
    fxdsp *d = load("rate 48000\ninputs 2\noutputs 2\nmatrix 0 0 1\nmatrix 1 1 1\n"
                    "output 0 0 7 normal\noutput 1 0 3 invert\navsync 100 0\n"
                    "latency_basis 0 0\nlatency_basis 1 0\n");
    fxdsp_latency s = tick(d);
    assert(s.minimum == 336 && s.common == 4464 && s.actual == 4800);
    assert(s.output[0].align == 336 && s.output[1].align == 144);
    assert(s.output[0].sync == 4464 && s.output[1].sync == 4464);
    assert(s.output[0].total - s.output[1].total == 192);
    fxdsp_free(d);
}

static void test_only_change_since_calibration_is_balanced(void) {
    /* The old EQ latency was already measured into alignment: no second compensation. */
    fxdsp *d = load("rate 48000\ninputs 8\nsurround 1\noutputs 3\n"
                    "matrix 0 0 1\nmatrix 1 2 1\nmatrix 2 4 1\n"
                    "stage_begin 0 front-eq lv2 http://lsp-plug.in/plugins/lv2/para_equalizer_x32_lr\n"
                    "pairs 1\ncontrol mode 1\nstage_end\n"
                    "output 0 0 0 normal\noutput 1 0 9 normal\noutput 2 0 4 normal\n"
                    "avsync 300 0\nlatency_basis 0 128\nlatency_basis 1 0\nlatency_basis 2 0\n");
    fxdsp_latency s = tick(d);
    assert(s.output[0].dsp == 6144 && s.output[1].dsp == 0);
    assert(s.output[0].balance == 0 && s.output[1].balance == 0);
    assert(s.minimum == 6144 && s.actual == 14400);
    fxdsp_set_effect_bypass(d, 1);
    s = tick(d);
    assert(s.bypassed && s.output[0].dsp == 0);
    assert(s.output[0].balance == 6144 && s.output[1].balance == 0);
    assert(s.output[1].align == 432 && s.actual == 14400);
    fxdsp_free(d);
}

static void test_unknown_legacy_basis_never_guesses_spread(void) {
    fxdsp *d = load("rate 48000\ninputs 8\nsurround 1\noutputs 2\n"
                    "matrix 0 0 1\nmatrix 1 2 1\n"
                    "stage_begin 0 front-eq lv2 http://lsp-plug.in/plugins/lv2/para_equalizer_x32_lr\n"
                    "pairs 1\ncontrol mode 1\nstage_end\n"
                    "output 0 0 0 normal\noutput 1 0 6 normal\navsync 200 0\n");
    fxdsp_latency s = tick(d);
    assert(!s.output[0].basis_known && !s.output[1].basis_known);
    assert(s.output[0].balance == 0 && s.output[1].balance == 0);
    assert(s.output[1].align == 288);
    fxdsp_free(d);
}

static void test_input_latency_and_capacity(void) {
    fxdsp *d = load("rate 48000\ninputs 2\noutputs 2\nmatrix 0 0 1\nmatrix 1 1 1\n"
                    "output 0 0 0 normal\noutput 1 0 40 normal\navsync 300 0\n"
                    "latency_basis 0 0\nlatency_basis 1 0\n");
    assert(fxdsp_set_input_latency(d, 8192.0f * 1000.0f / 48000.0f));
    fxdsp_latency s = tick(d);
    assert(s.input == 8192 && s.minimum == 8192 && s.common == 6208 && s.actual == 14400);
    assert(s.output[0].balance == 0 && s.output[1].balance == 0);
    assert(!fxdsp_set_input_latency(d, NAN));
    assert(!fxdsp_set_input_latency(d, -1.0f));
    fxdsp_free(d);
    d = load("rate 48000\ninputs 2\noutputs 2\nmatrix 0 0 1\nmatrix 1 1 1\n"
             "output 0 0 0 normal\noutput 1 0 40 normal\navsync 600 0\n");
    s = tick(d);
    assert(s.maximum == 22080 && s.actual == 22080 && s.limited);
    assert(s.output[1].align + s.output[1].sync == 24000);
    fxdsp_free(d);
}

static void test_loudness_uses_fft_not_stale_port_and_delays_are_intentional(void) {
    fxdsp *d = load("rate 48000\ninputs 2\noutputs 2\nmatrix 0 0 1\nmatrix 1 1 1\n"
                    "stage_begin 0 loud lv2 http://lsp-plug.in/plugins/lv2/loud_comp_stereo\n"
                    "control fft 5\ncontrol volume 0\nstage_end\n"
                    "stage_begin 1 manual native delay\nparam left_ms 10\nparam right_ms 0\nstage_end\n"
                    "output 0 0 0 normal\noutput 1 0 0 normal\navsync 50 0\n"
                    "latency_basis 0 0\nlatency_basis 1 0\n");
    fxdsp_latency s = tick(d);
    assert(s.output[0].dsp == 8192 && s.output[1].dsp == 8192);
    assert(s.output[0].intentional == 480 && s.output[1].intentional == 0);
    assert(s.output[0].balance == 0 && s.output[1].balance == 0);
    assert(s.minimum == 8672 && s.actual == 8672 && s.limited);
    fxdsp_free(d);
}

static void test_group_changes_are_balanced_before_bass_bus_mixing(void) {
    fxdsp *d = load("rate 48000\ninputs 8\nsurround 1\noutputs 2\n"
                    "matrix 0 0 1\nmatrix 1 0 0.5\nmatrix 1 2 0.5\n"
                    "stage_begin 0 front-eq lv2 http://lsp-plug.in/plugins/lv2/para_equalizer_x32_lr\n"
                    "pairs 1\ncontrol mode 1\nstage_end\n"
                    "output 0 0 0 normal\noutput 1 0 5 normal\navsync 200 0\n"
                    "source_basis 0 0\nsource_basis 2 0\nlatency_basis 0 0\nlatency_basis 1 0\n");
    fxdsp_latency s = tick(d);
    assert(s.source[0].dsp == 6144 && s.source[0].balance == 0);
    assert(s.source[2].dsp == 0 && s.source[2].balance == 6144);
    assert(!s.output[1].mixed && s.output[1].align == 240);
    assert(s.actual == 9600 && s.output[1].total == 9840);
    fxdsp_free(d);
}

static void test_source_balance_shares_capacity_with_output_alignment(void) {
    fxdsp *d = load("rate 48000\ninputs 2\noutputs 2\nmatrix 0 0 1\nmatrix 1 1 1\n"
                    "output 0 0 40 normal\noutput 1 0 0 normal\navsync 1000 0\n"
                    "source_basis 0 500\nsource_basis 1 0\nlatency_basis 0 500\nlatency_basis 1 0\n");
    fxdsp_latency s = tick(d);
    assert(s.balance_limited && s.limited);
    assert(s.source[0].balance == 22080);
    assert(s.output[0].align + s.output[0].sync <= 24000);
    assert(s.maximum == s.minimum);
    fxdsp_free(d);
}

static void test_unknown_convolver_does_not_derive_a_drop_to_zero(void) {
    char wav_path[]="/tmp/fxroute-av-sync-ir-XXXXXX";
    int fd=mkstemp(wav_path); assert(fd>=0);
    const unsigned char wav[] = {
        'R','I','F','F',40,0,0,0,'W','A','V','E','f','m','t',' ',16,0,0,0,
        3,0,1,0,128,187,0,0,0,238,2,0,4,0,32,0,'d','a','t','a',4,0,0,0,0,0,128,63};
    assert(write(fd,wav,sizeof wav)==sizeof wav); close(fd);
    char text[2048];
    for (unsigned stage=0;stage<2;stage++) {
        snprintf(text,sizeof text,
            "rate 48000\ninputs 2\noutputs 2\nmatrix 0 0 1\nmatrix 1 1 1\n%s%s%s"
            "output 0 0 7 normal\noutput 1 0 0 normal\navsync 300 0\n"
            "latency_basis 0 128\nlatency_basis 1 0\nsource_basis 0 128\nsource_basis 1 0\n",
            stage ? "stage_begin 0 fir native convolver\nparam path " : "oconv 0 0 0 -100 0 0 ",
            wav_path, stage ? "\nlatency 0 0 0 0 0\nstage_end\n" : "\n");
        fxdsp *d=load(text);
        fxdsp_latency s=tick(d);
        assert(s.output[0].unknown);
        assert(s.source[0].balance==0 && s.output[0].balance==0);
        assert(s.output[0].align==336);
        fxdsp_free(d);
    }
    unlink(wav_path);
}

static void test_oversized_finite_stage_metadata_is_rejected_before_loading_ir(void) {
    char path[]="/tmp/fxroute-av-sync-invalid-XXXXXX", error[256];
    int fd=mkstemp(path); assert(fd>=0);
    FILE *file=fdopen(fd,"w"); assert(file);
    fputs("rate 48000\ninputs 2\noutputs 2\n"
          "stage_begin 0 fir native convolver\nparam path /missing.wav\n"
          "latency 1e30 1e30 0 0 1\nstage_end\noutput 0 0 0 normal\n",file);
    fclose(file);
    fxdsp *d=fxdsp_load(path,error,sizeof error); unlink(path);
    assert(!d && strstr(error,"invalid config line"));
}

int main(void) {
    test_impulses_follow_live_controls_in_stereo_and_sub_modes();
    test_partial_calibration_recompile_keeps_native_impulse_alignment();
    test_alignment_and_total_credit();
    test_only_change_since_calibration_is_balanced();
    test_unknown_legacy_basis_never_guesses_spread();
    test_input_latency_and_capacity();
    test_loudness_uses_fft_not_stale_port_and_delays_are_intentional();
    test_group_changes_are_balanced_before_bass_bus_mixing();
    test_source_balance_shares_capacity_with_output_alignment();
    test_oversized_finite_stage_metadata_is_rejected_before_loading_ir();
    test_unknown_convolver_does_not_derive_a_drop_to_zero();
    puts("AV Sync native tests passed");
    return 0;
}
