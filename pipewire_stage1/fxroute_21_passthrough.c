/*
 * FXRoute PipeWire-native 2.1 Stage-3 crossover helper.
 *
 * Scope of this corrected artifact:
 * - Local reviewable C/libpipewire helper source only.
 * - PipeWire filter/client-style node with explicit mono DSP ports.
 * - Stage-3 routing: Out 1/2 = optional LR24 highpassed L/R, Out 3/4 =
 *   LR24 lowpassed mono bass by default, or stereo bass with --bass-routing stereo.
 * - Sub level (dB gain), sub polarity (normal/invert), per-branch delay (ms).
 *
 * Timing model:
 * - One PipeWire filter process callback owns DSP timing.
 * - Explicit logical ports are:
 *     input_L, input_R, output_1, output_2, output_3, output_4.
 * - The callback reads/writes each explicit DSP port buffer for the current
 *   process quantum. There is no capture-driven output-buffer transport.
 * - Frame count comes from spa_io_position->clock.duration, not from an output
 *   chunk size.
 */

#define _GNU_SOURCE

#include <errno.h>
#include <getopt.h>
#include <inttypes.h>
#include <math.h>
#include <signal.h>
#include <stdatomic.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <unistd.h>

#include <pipewire/filter.h>
#include <pipewire/pipewire.h>
#include <pipewire/properties.h>
#include <spa/param/latency-utils.h>
#include <spa/pod/builder.h>

#define DEFAULT_NODE_NAME "fxroute_21_passthrough"
#define DEFAULT_RATE 48000u
#define DEFAULT_QUANTUM 1024u
#define DEFAULT_LOWPASS_HZ 0.0f
#define DEFAULT_HIGHPASS_HZ 0.0f
#define DEFAULT_SUB_LEVEL_DB 0.0f
#define DEFAULT_MAIN_DELAY_MS 0.0f
#define DEFAULT_SUB_DELAY_MS 0.0f
#define DEFAULT_SUB_POLARITY_INVERT false
#define DEFAULT_BASS_ROUTING FXROUTE_BASS_ROUTING_MONO
#define BUTTERWORTH_Q 0.7071067811865476f
#define PI_F 3.14159265358979323846f
#define PORT_COUNT 6u
#define PROCESS_LATENCY_MS 10u

enum fxroute_port_kind {
    FXROUTE_PORT_INPUT_L = 0,
    FXROUTE_PORT_INPUT_R,
    FXROUTE_PORT_OUTPUT_1,
    FXROUTE_PORT_OUTPUT_2,
    FXROUTE_PORT_OUTPUT_3,
    FXROUTE_PORT_OUTPUT_4,
};

enum fxroute_bass_routing {
    FXROUTE_BASS_ROUTING_MONO = 0,
    FXROUTE_BASS_ROUTING_STEREO,
};

struct fxroute_21_app;

struct fxroute_port {
    struct fxroute_21_app *app;
    const char *name;
    enum fxroute_port_kind kind;
};

struct biquad {
    float b0;
    float b1;
    float b2;
    float a1;
    float a2;
    float z1;
    float z2;
};

struct delay_line {
    float *buf;
    uint32_t size;
    uint32_t pos;
};

struct fxroute_21_app {
    struct pw_main_loop *loop;
    struct pw_filter *filter;
    struct fxroute_port *ports[PORT_COUNT];
    const char *node_name;
    uint32_t rate;
    uint32_t quantum;
    float lowpass_hz;
    float highpass_hz;
    bool lowpass_enabled;
    bool highpass_enabled;
    float sub_level_db;
    float sub2_level_db;
    float main_delay_ms;
    float sub_delay_ms;
    float sub2_delay_ms;
    bool sub_polarity_invert;
    bool sub2_polarity_invert;
    enum fxroute_bass_routing bass_routing;
    bool self_test_sub_gain;
    bool self_test_alignment;
    bool self_test_bass_routing;
    bool self_test_exact_sub_mute;
    atomic_bool exact_sub_mute;
    int exact_sub_mute_ack_socket;
    const char *exact_sub_mute_ack_path;
    int exact_sub_mute_last_ack;
    float sub_level_gain;
    float sub2_level_gain;
    struct biquad sub_lowpass_1;
    struct biquad sub_lowpass_2;
    struct biquad left_sub_lowpass_1;
    struct biquad left_sub_lowpass_2;
    struct biquad right_sub_lowpass_1;
    struct biquad right_sub_lowpass_2;
    struct biquad main_left_highpass_1;
    struct biquad main_left_highpass_2;
    struct biquad main_right_highpass_1;
    struct biquad main_right_highpass_2;
    struct delay_line main_delay_l;
    struct delay_line main_delay_r;
    struct delay_line sub_delay;
    struct delay_line sub2_delay;
};

static void on_signal(void *userdata, int signo)
{
    struct fxroute_21_app *app = userdata;

    (void)signo;
    if (app != NULL && app->loop != NULL) {
        pw_main_loop_quit(app->loop);
    }
}

static void on_exact_sub_mute_signal(void *userdata, int signo)
{
    struct fxroute_21_app *app = userdata;

    if (app == NULL) {
        return;
    }
    if (signo == SIGUSR1) {
        atomic_store_explicit(&app->exact_sub_mute, true, memory_order_release);
    } else if (signo == SIGUSR2) {
        atomic_store_explicit(&app->exact_sub_mute, false, memory_order_release);
    }
}

static void usage(const char *program)
{
    fprintf(stderr,
            "Usage: %s [options]\n"
            "  -n, --node-name <name>     PipeWire filter node name, default: %s\n"
            "  -r, --rate <hz>            Expected FXRoute effective output rate, default: %u\n"
            "  -q, --quantum <frames>     Requested node latency quantum, default: %u\n"
            "  -l, --lowpass-hz <hz>      Stage-2 sub LR24 lowpass frequency; 0 disables, default: %.1f\n"
            "  -H, --highpass-hz <hz>     Stage-3 main LR24 highpass frequency; 0 disables, default: %.1f\n"
            "  -L, --sub-level-db <db>    Sub level gain in dB, default: %.1f\n"
            "      --bass-routing <mode>  Bass routing: mono|stereo, default: mono\n"
            "      --sub2-level-db <db>   Sub 2 level gain in dB, default: %.1f\n"
            "  -M, --main-delay-ms <ms>   Main speaker delay in ms, default: %.1f\n"
            "  -S, --sub-delay-ms <ms>    Sub delay in ms, default: %.1f\n"
            "      --sub2-delay-ms <ms>   Sub 2 delay in ms, default: %.1f\n"
            "  -P, --sub-polarity <mode>  Sub polarity: normal|invert, default: normal\n"
            "      --sub2-polarity <mode> Sub 2 polarity: normal|invert, default: normal\n"
            "      --self-test-sub-gain    Run offline sub-gain DSP smoke test and exit\n"
            "      --self-test-alignment   Run offline branch-delay impulse smoke test and exit\n"
            "      --self-test-bass-routing Run offline L/R bass-routing smoke test and exit\n"
            "      --self-test-exact-sub-mute Run offline exact measurement-mute test and exit\n"
            "      --exact-sub-mute-ack <path> Runtime acknowledgement socket path\n"
            "  -h, --help                 Show this help\n"
            "\n"
            "Graph ports are explicit and must be linked by FXRoute later:\n"
            "  input_L, input_R, output_1, output_2, output_3, output_4\n",
            program,
            DEFAULT_NODE_NAME,
            DEFAULT_RATE,
            DEFAULT_QUANTUM,
            DEFAULT_LOWPASS_HZ,
            DEFAULT_HIGHPASS_HZ,
            DEFAULT_SUB_LEVEL_DB,
            DEFAULT_SUB_LEVEL_DB,
            DEFAULT_MAIN_DELAY_MS,
            DEFAULT_SUB_DELAY_MS,
            DEFAULT_SUB_DELAY_MS);
}

static int parse_u32(const char *text, uint32_t *out)
{
    char *end = NULL;
    unsigned long value;

    errno = 0;
    value = strtoul(text, &end, 10);
    if (errno != 0 || end == text || *end != '\0' || value == 0 || value > UINT32_MAX) {
        return -1;
    }
    *out = (uint32_t)value;
    return 0;
}

static int parse_float(const char *text, float *out)
{
    char *end = NULL;
    float value;

    errno = 0;
    value = strtof(text, &end);
    if (errno != 0 || end == text || *end != '\0' || !isfinite(value)) {
        return -1;
    }
    *out = value;
    return 0;
}

static int parse_float_nonnegative(const char *text, float *out)
{
    float value;
    if (parse_float(text, &value) != 0) {
        return -1;
    }
    if (value < 0.0f) {
        return -1;
    }
    *out = value;
    return 0;
}

static int parse_args(int argc, char **argv, struct fxroute_21_app *app)
{
    enum {
        OPT_SUB2_LEVEL_DB = 1000,
        OPT_SUB2_DELAY_MS,
        OPT_SUB2_POLARITY,
        OPT_SELF_TEST_EXACT_SUB_MUTE,
        OPT_EXACT_SUB_MUTE_ACK,
        OPT_BASS_ROUTING,
        OPT_SELF_TEST_BASS_ROUTING,
    };
    static const struct option long_options[] = {
        {"node-name", required_argument, NULL, 'n'},
        {"rate", required_argument, NULL, 'r'},
        {"quantum", required_argument, NULL, 'q'},
        {"lowpass-hz", required_argument, NULL, 'l'},
        {"highpass-hz", required_argument, NULL, 'H'},
        {"sub-level-db", required_argument, NULL, 'L'},
        {"bass-routing", required_argument, NULL, OPT_BASS_ROUTING},
        {"sub2-level-db", required_argument, NULL, OPT_SUB2_LEVEL_DB},
        {"main-delay-ms", required_argument, NULL, 'M'},
        {"sub-delay-ms", required_argument, NULL, 'S'},
        {"sub2-delay-ms", required_argument, NULL, OPT_SUB2_DELAY_MS},
        {"sub-polarity", required_argument, NULL, 'P'},
        {"sub2-polarity", required_argument, NULL, OPT_SUB2_POLARITY},
        {"self-test-exact-sub-mute", no_argument, NULL, OPT_SELF_TEST_EXACT_SUB_MUTE},
        {"exact-sub-mute-ack", required_argument, NULL, OPT_EXACT_SUB_MUTE_ACK},
        {"self-test-sub-gain", no_argument, NULL, 'T'},
        {"self-test-alignment", no_argument, NULL, 'A'},
        {"self-test-bass-routing", no_argument, NULL, OPT_SELF_TEST_BASS_ROUTING},
        {"help", no_argument, NULL, 'h'},
        {0, 0, 0, 0},
    };

    for (;;) {
        int option = getopt_long(argc, argv, "n:r:q:l:H:L:M:S:P:TAh", long_options, NULL);
        if (option == -1) {
            break;
        }

        switch (option) {
        case 'n':
            app->node_name = optarg;
            break;
        case 'r':
            if (parse_u32(optarg, &app->rate) != 0) {
                fprintf(stderr, "Invalid --rate: %s\n", optarg);
                return -1;
            }
            break;
        case 'q':
            if (parse_u32(optarg, &app->quantum) != 0) {
                fprintf(stderr, "Invalid --quantum: %s\n", optarg);
                return -1;
            }
            break;
        case 'l':
            if (parse_float(optarg, &app->lowpass_hz) != 0) {
                fprintf(stderr, "Invalid --lowpass-hz: %s\n", optarg);
                return -1;
            }
            break;
        case 'H':
            if (parse_float(optarg, &app->highpass_hz) != 0) {
                fprintf(stderr, "Invalid --highpass-hz: %s\n", optarg);
                return -1;
            }
            break;
        case 'L':
            if (parse_float(optarg, &app->sub_level_db) != 0) {
                fprintf(stderr, "Invalid --sub-level-db: %s\n", optarg);
                return -1;
            }
            break;
        case OPT_BASS_ROUTING:
            if (strcmp(optarg, "mono") == 0) {
                app->bass_routing = FXROUTE_BASS_ROUTING_MONO;
            } else if (strcmp(optarg, "stereo") == 0) {
                app->bass_routing = FXROUTE_BASS_ROUTING_STEREO;
            } else {
                fprintf(stderr, "Invalid --bass-routing: %s (expected mono|stereo)\n", optarg);
                return -1;
            }
            break;
        case OPT_SUB2_LEVEL_DB:
            if (parse_float(optarg, &app->sub2_level_db) != 0) {
                fprintf(stderr, "Invalid --sub2-level-db: %s\n", optarg);
                return -1;
            }
            break;
        case 'M':
            if (parse_float_nonnegative(optarg, &app->main_delay_ms) != 0) {
                fprintf(stderr, "Invalid --main-delay-ms: %s\n", optarg);
                return -1;
            }
            break;
        case 'S':
            if (parse_float_nonnegative(optarg, &app->sub_delay_ms) != 0) {
                fprintf(stderr, "Invalid --sub-delay-ms: %s\n", optarg);
                return -1;
            }
            break;
        case OPT_SUB2_DELAY_MS:
            if (parse_float_nonnegative(optarg, &app->sub2_delay_ms) != 0) {
                fprintf(stderr, "Invalid --sub2-delay-ms: %s\n", optarg);
                return -1;
            }
            break;
        case 'P':
            if (strcmp(optarg, "invert") == 0) {
                app->sub_polarity_invert = true;
            } else if (strcmp(optarg, "normal") == 0) {
                app->sub_polarity_invert = false;
            } else {
                fprintf(stderr, "Invalid --sub-polarity: %s (expected normal|invert)\n", optarg);
                return -1;
            }
            break;
        case OPT_SUB2_POLARITY:
            if (strcmp(optarg, "invert") == 0) {
                app->sub2_polarity_invert = true;
            } else if (strcmp(optarg, "normal") == 0) {
                app->sub2_polarity_invert = false;
            } else {
                fprintf(stderr, "Invalid --sub2-polarity: %s (expected normal|invert)\n", optarg);
                return -1;
            }
            break;
        case OPT_SELF_TEST_EXACT_SUB_MUTE:
            app->self_test_exact_sub_mute = true;
            break;
        case OPT_EXACT_SUB_MUTE_ACK:
            app->exact_sub_mute_ack_path = optarg;
            break;
        case 'T':
            app->self_test_sub_gain = true;
            break;
        case 'A':
            app->self_test_alignment = true;
            break;
        case OPT_SELF_TEST_BASS_ROUTING:
            app->self_test_bass_routing = true;
            break;
        case 'h':
            usage(argv[0]);
            exit(EXIT_SUCCESS);
        default:
            usage(argv[0]);
            return -1;
        }
    }

    if (optind < argc) {
        fprintf(stderr, "Unexpected argument: %s\n", argv[optind]);
        return -1;
    }

    return 0;
}

static void biquad_configure_lowpass(struct biquad *filter, float rate, float cutoff_hz)
{
    float w0 = 2.0f * PI_F * cutoff_hz / rate;
    float cos_w0 = cosf(w0);
    float sin_w0 = sinf(w0);
    float alpha = sin_w0 / (2.0f * BUTTERWORTH_Q);
    float a0 = 1.0f + alpha;
    float b0 = (1.0f - cos_w0) * 0.5f;
    float b1 = 1.0f - cos_w0;
    float b2 = (1.0f - cos_w0) * 0.5f;
    float a1 = -2.0f * cos_w0;
    float a2 = 1.0f - alpha;

    filter->b0 = b0 / a0;
    filter->b1 = b1 / a0;
    filter->b2 = b2 / a0;
    filter->a1 = a1 / a0;
    filter->a2 = a2 / a0;
    filter->z1 = 0.0f;
    filter->z2 = 0.0f;
}

static void biquad_configure_highpass(struct biquad *filter, float rate, float cutoff_hz)
{
    float w0 = 2.0f * PI_F * cutoff_hz / rate;
    float cos_w0 = cosf(w0);
    float sin_w0 = sinf(w0);
    float alpha = sin_w0 / (2.0f * BUTTERWORTH_Q);
    float a0 = 1.0f + alpha;
    float b0 = (1.0f + cos_w0) * 0.5f;
    float b1 = -(1.0f + cos_w0);
    float b2 = (1.0f + cos_w0) * 0.5f;
    float a1 = -2.0f * cos_w0;
    float a2 = 1.0f - alpha;

    filter->b0 = b0 / a0;
    filter->b1 = b1 / a0;
    filter->b2 = b2 / a0;
    filter->a1 = a1 / a0;
    filter->a2 = a2 / a0;
    filter->z1 = 0.0f;
    filter->z2 = 0.0f;
}

static float biquad_process(struct biquad *filter, float input)
{
    float output = filter->b0 * input + filter->z1;

    filter->z1 = filter->b1 * input - filter->a1 * output + filter->z2;
    filter->z2 = filter->b2 * input - filter->a2 * output;
    return output;
}

static void configure_sub_lowpass(struct fxroute_21_app *app)
{
    float nyquist = (float)app->rate * 0.5f;
    float max_cutoff = nyquist * 0.45f;

    app->lowpass_enabled = app->lowpass_hz > 0.0f && app->lowpass_hz < max_cutoff;
    if (!app->lowpass_enabled) {
        return;
    }
    biquad_configure_lowpass(&app->sub_lowpass_1, (float)app->rate, app->lowpass_hz);
    biquad_configure_lowpass(&app->sub_lowpass_2, (float)app->rate, app->lowpass_hz);
    biquad_configure_lowpass(&app->left_sub_lowpass_1, (float)app->rate, app->lowpass_hz);
    biquad_configure_lowpass(&app->left_sub_lowpass_2, (float)app->rate, app->lowpass_hz);
    biquad_configure_lowpass(&app->right_sub_lowpass_1, (float)app->rate, app->lowpass_hz);
    biquad_configure_lowpass(&app->right_sub_lowpass_2, (float)app->rate, app->lowpass_hz);
}

static void configure_main_highpass(struct fxroute_21_app *app)
{
    float nyquist = (float)app->rate * 0.5f;
    float max_cutoff = nyquist * 0.45f;

    app->highpass_enabled = app->highpass_hz > 0.0f && app->highpass_hz < max_cutoff;
    if (!app->highpass_enabled) {
        return;
    }
    biquad_configure_highpass(&app->main_left_highpass_1, (float)app->rate, app->highpass_hz);
    biquad_configure_highpass(&app->main_left_highpass_2, (float)app->rate, app->highpass_hz);
    biquad_configure_highpass(&app->main_right_highpass_1, (float)app->rate, app->highpass_hz);
    biquad_configure_highpass(&app->main_right_highpass_2, (float)app->rate, app->highpass_hz);
}

static uint32_t ms_to_samples(float delay_ms, uint32_t rate)
{
    return (uint32_t)(delay_ms * (float)rate / 1000.0f + 0.5f);
}

static int delay_line_init(struct delay_line *dl, uint32_t samples)
{
    dl->pos = 0;
    dl->size = samples;
    if (samples == 0) {
        dl->buf = NULL;
        return 0;
    }
    dl->buf = calloc(samples, sizeof(float));
    if (dl->buf == NULL) {
        return -1;
    }
    return 0;
}

static void delay_line_destroy(struct delay_line *dl)
{
    free(dl->buf);
    dl->buf = NULL;
    dl->size = 0;
    dl->pos = 0;
}

static inline float delay_line_process(struct delay_line *dl, float input)
{
    if (dl->size == 0) {
        return input;
    }
    float output = dl->buf[dl->pos];
    dl->buf[dl->pos] = input;
    dl->pos = (dl->pos + 1) % dl->size;
    return output;
}

static void configure_delays(struct fxroute_21_app *app)
{
    uint32_t main_samples = ms_to_samples(app->main_delay_ms, app->rate);
    uint32_t sub_samples = ms_to_samples(app->sub_delay_ms, app->rate);
    uint32_t sub2_samples = ms_to_samples(app->sub2_delay_ms, app->rate);

    delay_line_destroy(&app->main_delay_l);
    delay_line_destroy(&app->main_delay_r);
    delay_line_destroy(&app->sub_delay);
    delay_line_destroy(&app->sub2_delay);

    if (main_samples > 0) {
        if (delay_line_init(&app->main_delay_l, main_samples) != 0 ||
            delay_line_init(&app->main_delay_r, main_samples) != 0) {
            fprintf(stderr, "Failed to allocate main delay buffers (%u samples)\n", main_samples);
        }
    }
    if (sub_samples > 0) {
        if (delay_line_init(&app->sub_delay, sub_samples) != 0) {
            fprintf(stderr, "Failed to allocate sub delay buffer (%u samples)\n", sub_samples);
        }
    }
    if (sub2_samples > 0) {
        if (delay_line_init(&app->sub2_delay, sub2_samples) != 0) {
            fprintf(stderr, "Failed to allocate sub2 delay buffer (%u samples)\n", sub2_samples);
        }
    }
}

static void configure_sub_gain(struct fxroute_21_app *app)
{
    app->sub_level_gain = powf(10.0f, app->sub_level_db / 20.0f);
    app->sub2_level_gain = powf(10.0f, app->sub2_level_db / 20.0f);
}

static float process_sub_lowpass(struct fxroute_21_app *app, float input)
{
    float stage_1;

    if (!app->lowpass_enabled) {
        return input;
    }
    stage_1 = biquad_process(&app->sub_lowpass_1, input);
    return biquad_process(&app->sub_lowpass_2, stage_1);
}

static float process_stereo_sub_lowpass(struct fxroute_21_app *app, bool right_channel, float input)
{
    float stage_1;
    struct biquad *filter_1;
    struct biquad *filter_2;

    if (!app->lowpass_enabled) {
        return input;
    }

    filter_1 = right_channel ? &app->right_sub_lowpass_1 : &app->left_sub_lowpass_1;
    filter_2 = right_channel ? &app->right_sub_lowpass_2 : &app->left_sub_lowpass_2;
    stage_1 = biquad_process(filter_1, input);
    return biquad_process(filter_2, stage_1);
}

static float process_main_highpass(struct fxroute_21_app *app, bool right_channel, float input)
{
    float stage_1;
    struct biquad *filter_1;
    struct biquad *filter_2;

    if (!app->highpass_enabled) {
        return input;
    }

    filter_1 = right_channel ? &app->main_right_highpass_1 : &app->main_left_highpass_1;
    filter_2 = right_channel ? &app->main_right_highpass_2 : &app->main_left_highpass_2;
    stage_1 = biquad_process(filter_1, input);
    return biquad_process(filter_2, stage_1);
}

static float rms_for_buffer(const float *buffer, uint32_t frames);

static bool route_stage3_crossover(struct fxroute_21_app *app,
                                   const float *input_l, const float *input_r,
                                   float *output_1, float *output_2,
                                   float *output_3, float *output_4,
                                   uint32_t frames)
{
    const bool exact_sub_mute = atomic_load_explicit(&app->exact_sub_mute, memory_order_acquire);
    for (uint32_t frame = 0; frame < frames; ++frame) {
        float left = input_l[frame];
        float right = input_r[frame];

        /* Main branch: highpass → delay */
        float main_l = process_main_highpass(app, false, left);
        float main_r = process_main_highpass(app, true, right);
        output_1[frame] = delay_line_process(&app->main_delay_l, main_l);
        output_2[frame] = delay_line_process(&app->main_delay_r, main_r);

        /* Sub branches: mono or stereo lowpass, independent delay/polarity/level. */
        float sub1_low;
        float sub2_low;
        if (app->bass_routing == FXROUTE_BASS_ROUTING_STEREO) {
            sub1_low = process_stereo_sub_lowpass(app, false, left);
            sub2_low = process_stereo_sub_lowpass(app, true, right);
        } else {
            sub1_low = process_sub_lowpass(app, (left + right) * 0.5f);
            sub2_low = sub1_low;
        }

        float sub1 = delay_line_process(&app->sub_delay, sub1_low);
        if (app->sub_polarity_invert) {
            sub1 = -sub1;
        }
        sub1 *= app->sub_level_gain;
        output_3[frame] = exact_sub_mute ? 0.0f : sub1;

        float sub2 = delay_line_process(&app->sub2_delay, sub2_low);
        if (app->sub2_polarity_invert) {
            sub2 = -sub2;
        }
        sub2 *= app->sub2_level_gain;
        output_4[frame] = exact_sub_mute ? 0.0f : sub2;
    }
    return exact_sub_mute;
}

static void acknowledge_exact_sub_mute(struct fxroute_21_app *app, bool applied)
{
    struct sockaddr_un address;
    char value;

    if (app->exact_sub_mute_ack_socket < 0 || app->exact_sub_mute_ack_path == NULL ||
        app->exact_sub_mute_last_ack == (applied ? 1 : 0)) {
        return;
    }
    memset(&address, 0, sizeof(address));
    address.sun_family = AF_UNIX;
    if (strlen(app->exact_sub_mute_ack_path) >= sizeof(address.sun_path)) {
        return;
    }
    strcpy(address.sun_path, app->exact_sub_mute_ack_path);
    value = applied ? '1' : '0';
    if (sendto(app->exact_sub_mute_ack_socket, &value, 1, MSG_DONTWAIT,
               (const struct sockaddr *)&address, sizeof(address)) == 1) {
        app->exact_sub_mute_last_ack = applied ? 1 : 0;
    }
}

static int run_exact_sub_mute_self_test(struct fxroute_21_app *app)
{
    enum { frames = 4096 };
    float input_l[frames];
    float input_r[frames];
    float main_l_before[frames];
    float main_r_before[frames];
    float sub1_before[frames];
    float sub2_before[frames];
    float main_l_muted[frames];
    float main_r_muted[frames];
    float sub1_muted[frames];
    float sub2_muted[frames];

    for (uint32_t frame = 0; frame < frames; ++frame) {
        input_l[frame] = 0.2f * sinf((2.0f * PI_F * 31.0f * (float)frame) / (float)frames);
        input_r[frame] = 0.15f * cosf((2.0f * PI_F * 47.0f * (float)frame) / (float)frames);
    }
    atomic_store_explicit(&app->exact_sub_mute, false, memory_order_release);
    route_stage3_crossover(app, input_l, input_r, main_l_before, main_r_before, sub1_before, sub2_before, frames);
    configure_sub_lowpass(app);
    configure_main_highpass(app);
    configure_delays(app);
    atomic_store_explicit(&app->exact_sub_mute, true, memory_order_release);
    route_stage3_crossover(app, input_l, input_r, main_l_muted, main_r_muted, sub1_muted, sub2_muted, frames);

    const bool sub1_zero = rms_for_buffer(sub1_muted, frames) == 0.0f;
    const bool sub2_zero = rms_for_buffer(sub2_muted, frames) == 0.0f;
    const bool main_l_same = memcmp(main_l_before, main_l_muted, sizeof(main_l_before)) == 0;
    const bool main_r_same = memcmp(main_r_before, main_r_muted, sizeof(main_r_before)) == 0;
    printf("exact_sub_mute=true output_3_exact_zero=%s output_4_exact_zero=%s main_1_unchanged=%s main_2_unchanged=%s\n",
           sub1_zero ? "true" : "false", sub2_zero ? "true" : "false",
           main_l_same ? "true" : "false", main_r_same ? "true" : "false");
    return sub1_zero && sub2_zero && main_l_same && main_r_same ? EXIT_SUCCESS : EXIT_FAILURE;
}

static float rms_for_buffer(const float *buffer, uint32_t frames)
{
    double sum = 0.0;

    for (uint32_t frame = 0; frame < frames; ++frame) {
        sum += (double)buffer[frame] * (double)buffer[frame];
    }
    return sqrt(sum / (double)frames);
}

static int run_sub_gain_self_test(struct fxroute_21_app *app)
{
    enum { frames = 4096 };
    float input_l[frames];
    float input_r[frames];
    float output_1[frames];
    float output_2[frames];
    float output_3[frames];
    float output_4[frames];

    memset(output_1, 0, sizeof(output_1));
    memset(output_2, 0, sizeof(output_2));
    memset(output_3, 0, sizeof(output_3));
    memset(output_4, 0, sizeof(output_4));

    for (uint32_t frame = 0; frame < frames; ++frame) {
        float sample = 0.25f * sinf((2.0f * PI_F * 17.0f * (float)frame) / (float)frames);
        input_l[frame] = sample;
        input_r[frame] = sample;
    }

    route_stage3_crossover(app, input_l, input_r, output_1, output_2, output_3, output_4, frames);

    printf("sub_level_db=%.1f sub_level_gain=%.6f "
           "sub2_level_db=%.1f sub2_level_gain=%.6f "
           "output_3_rms=%.9f output_4_rms=%.9f\n",
           app->sub_level_db,
           app->sub_level_gain,
           app->sub2_level_db,
           app->sub2_level_gain,
           rms_for_buffer(output_3, frames),
           rms_for_buffer(output_4, frames));
    return EXIT_SUCCESS;
}

static int find_impulse_position(const float *buffer, uint32_t frames)
{
    for (uint32_t frame = 0; frame < frames; ++frame) {
        if (fabsf(buffer[frame]) > 0.5f) {
            return (int)frame;
        }
    }
    return -1;
}

static int run_alignment_self_test(struct fxroute_21_app *app)
{
    enum { frames = 4096 };
    float input_l[frames];
    float input_r[frames];
    float output_1[frames];
    float output_2[frames];
    float output_3[frames];
    float output_4[frames];
    int output_1_impulse;
    int output_2_impulse;
    int output_3_impulse;
    int output_4_impulse;

    memset(input_l, 0, sizeof(input_l));
    memset(input_r, 0, sizeof(input_r));
    memset(output_1, 0, sizeof(output_1));
    memset(output_2, 0, sizeof(output_2));
    memset(output_3, 0, sizeof(output_3));
    memset(output_4, 0, sizeof(output_4));

    input_l[0] = 1.0f;
    input_r[0] = 1.0f;

    route_stage3_crossover(app, input_l, input_r, output_1, output_2, output_3, output_4, frames);

    output_1_impulse = find_impulse_position(output_1, frames);
    output_2_impulse = find_impulse_position(output_2, frames);
    output_3_impulse = find_impulse_position(output_3, frames);
    output_4_impulse = find_impulse_position(output_4, frames);

    printf("rate=%u main_delay_ms=%.1f sub_delay_ms=%.1f sub2_delay_ms=%.1f "
           "main_delay_samples=%u sub_delay_samples=%u sub2_delay_samples=%u "
           "output_1_impulse=%d output_2_impulse=%d output_3_impulse=%d output_4_impulse=%d\n",
           app->rate,
           app->main_delay_ms,
           app->sub_delay_ms,
           app->sub2_delay_ms,
           ms_to_samples(app->main_delay_ms, app->rate),
           ms_to_samples(app->sub_delay_ms, app->rate),
           ms_to_samples(app->sub2_delay_ms, app->rate),
           output_1_impulse,
           output_2_impulse,
           output_3_impulse,
           output_4_impulse);
    return EXIT_SUCCESS;
}

static int run_bass_routing_self_test(struct fxroute_21_app *app)
{
    enum { frames = 4096 };
    float input_l[frames];
    float input_r[frames];
    float output_1[frames];
    float output_2[frames];
    float output_3[frames];
    float output_4[frames];

    memset(input_l, 0, sizeof(input_l));
    memset(input_r, 0, sizeof(input_r));
    memset(output_1, 0, sizeof(output_1));
    memset(output_2, 0, sizeof(output_2));
    memset(output_3, 0, sizeof(output_3));
    memset(output_4, 0, sizeof(output_4));

    for (uint32_t frame = 0; frame < frames; ++frame) {
        input_l[frame] = 0.25f * sinf((2.0f * PI_F * 17.0f * (float)frame) / (float)frames);
    }
    route_stage3_crossover(app, input_l, input_r, output_1, output_2, output_3, output_4, frames);
    printf("case=L-only bass_routing=%s output_3_rms=%.9f output_4_rms=%.9f\n",
           app->bass_routing == FXROUTE_BASS_ROUTING_STEREO ? "stereo" : "mono",
           rms_for_buffer(output_3, frames),
           rms_for_buffer(output_4, frames));

    memset(input_l, 0, sizeof(input_l));
    memset(input_r, 0, sizeof(input_r));
    memset(output_1, 0, sizeof(output_1));
    memset(output_2, 0, sizeof(output_2));
    memset(output_3, 0, sizeof(output_3));
    memset(output_4, 0, sizeof(output_4));

    for (uint32_t frame = 0; frame < frames; ++frame) {
        input_r[frame] = 0.25f * sinf((2.0f * PI_F * 17.0f * (float)frame) / (float)frames);
    }
    route_stage3_crossover(app, input_l, input_r, output_1, output_2, output_3, output_4, frames);
    printf("case=R-only bass_routing=%s output_3_rms=%.9f output_4_rms=%.9f\n",
           app->bass_routing == FXROUTE_BASS_ROUTING_STEREO ? "stereo" : "mono",
           rms_for_buffer(output_3, frames),
           rms_for_buffer(output_4, frames));

    return EXIT_SUCCESS;
}

static void clear_outputs(float *output_1, float *output_2,
                          float *output_3, float *output_4,
                          uint32_t frames)
{
    for (uint32_t frame = 0; frame < frames; ++frame) {
        output_1[frame] = 0.0f;
        output_2[frame] = 0.0f;
        output_3[frame] = 0.0f;
        output_4[frame] = 0.0f;
    }
}

static void on_process(void *userdata, struct spa_io_position *position)
{
    struct fxroute_21_app *app = userdata;
    uint32_t n_samples = position != NULL ? position->clock.duration : 0;
    const float *input_l;
    const float *input_r;
    float *output_1;
    float *output_2;
    float *output_3;
    float *output_4;

    if (n_samples == 0) {
        return;
    }

    input_l = pw_filter_get_dsp_buffer(app->ports[FXROUTE_PORT_INPUT_L], n_samples);
    input_r = pw_filter_get_dsp_buffer(app->ports[FXROUTE_PORT_INPUT_R], n_samples);
    output_1 = pw_filter_get_dsp_buffer(app->ports[FXROUTE_PORT_OUTPUT_1], n_samples);
    output_2 = pw_filter_get_dsp_buffer(app->ports[FXROUTE_PORT_OUTPUT_2], n_samples);
    output_3 = pw_filter_get_dsp_buffer(app->ports[FXROUTE_PORT_OUTPUT_3], n_samples);
    output_4 = pw_filter_get_dsp_buffer(app->ports[FXROUTE_PORT_OUTPUT_4], n_samples);

    if (output_1 == NULL || output_2 == NULL || output_3 == NULL || output_4 == NULL) {
        return;
    }

    if (input_l == NULL || input_r == NULL) {
        clear_outputs(output_1, output_2, output_3, output_4, n_samples);
        return;
    }

    bool applied_exact_sub_mute = route_stage3_crossover(
        app, input_l, input_r, output_1, output_2, output_3, output_4, n_samples);
    acknowledge_exact_sub_mute(app, applied_exact_sub_mute);
}

static const struct pw_filter_events filter_events = {
    PW_VERSION_FILTER_EVENTS,
    .process = on_process,
};

static struct fxroute_port *add_port(struct fxroute_21_app *app,
                                     enum pw_direction direction,
                                     enum fxroute_port_kind kind,
                                     const char *name,
                                     const char *channel)
{
    struct fxroute_port *port;
    struct pw_properties *port_props;

    port_props = pw_properties_new(
        PW_KEY_FORMAT_DSP, "32 bit float mono audio",
        PW_KEY_PORT_NAME, name,
        PW_KEY_AUDIO_CHANNEL, channel,
        NULL);
    if (port_props == NULL) {
        fprintf(stderr, "Failed to allocate PipeWire filter port properties for %s\n", name);
        return NULL;
    }

    port = pw_filter_add_port(app->filter,
                              direction,
                              PW_FILTER_PORT_FLAG_MAP_BUFFERS,
                              sizeof(struct fxroute_port),
                              port_props,
                              NULL, 0);
    if (port == NULL) {
        pw_properties_free(port_props);
        fprintf(stderr, "Failed to add PipeWire filter port %s\n", name);
        return NULL;
    }

    port->app = app;
    port->name = name;
    port->kind = kind;
    app->ports[kind] = port;

    return port;
}

static int create_filter(struct fxroute_21_app *app)
{
    uint8_t param_buffer[1024];
    struct spa_pod_builder builder = SPA_POD_BUILDER_INIT(param_buffer, sizeof(param_buffer));
    const struct spa_pod *params[1];
    uint32_t n_params = 0;
    struct pw_properties *properties;
    char latency_text[64];

    snprintf(latency_text, sizeof(latency_text), "%u/%u", app->quantum, app->rate);

    properties = pw_properties_new(
        PW_KEY_MEDIA_TYPE, "Audio",
        PW_KEY_MEDIA_CATEGORY, "Filter",
        PW_KEY_MEDIA_ROLE, "DSP",
        PW_KEY_NODE_NAME, app->node_name,
        PW_KEY_NODE_DESCRIPTION, "FXRoute 2.1 Stage-3 crossover",
        PW_KEY_NODE_PASSIVE, "follow",
        PW_KEY_NODE_LATENCY, latency_text,
        NULL);
    if (properties == NULL) {
        fprintf(stderr, "Failed to allocate PipeWire filter properties\n");
        return -1;
    }

    app->filter = pw_filter_new_simple(pw_main_loop_get_loop(app->loop),
                                       app->node_name,
                                       properties,
                                       &filter_events,
                                       app);
    if (app->filter == NULL) {
        pw_properties_free(properties);
        fprintf(stderr, "Failed to create PipeWire filter %s\n", app->node_name);
        return -1;
    }

    if (add_port(app, PW_DIRECTION_INPUT, FXROUTE_PORT_INPUT_L, "input_L", "FL") == NULL ||
        add_port(app, PW_DIRECTION_INPUT, FXROUTE_PORT_INPUT_R, "input_R", "FR") == NULL ||
        add_port(app, PW_DIRECTION_OUTPUT, FXROUTE_PORT_OUTPUT_1, "output_1", "FL") == NULL ||
        add_port(app, PW_DIRECTION_OUTPUT, FXROUTE_PORT_OUTPUT_2, "output_2", "FR") == NULL ||
        add_port(app, PW_DIRECTION_OUTPUT, FXROUTE_PORT_OUTPUT_3, "output_3", "RL") == NULL ||
        add_port(app, PW_DIRECTION_OUTPUT, FXROUTE_PORT_OUTPUT_4, "output_4", "RR") == NULL) {
        return -1;
    }

    params[n_params++] = spa_process_latency_build(&builder,
                                                   SPA_PARAM_ProcessLatency,
                                                   &SPA_PROCESS_LATENCY_INFO_INIT(
                                                       .ns = PROCESS_LATENCY_MS * SPA_NSEC_PER_MSEC));
    if (params[0] == NULL) {
        fprintf(stderr, "Failed to build PipeWire process latency parameter\n");
        return -1;
    }

    if (pw_filter_connect(app->filter, PW_FILTER_FLAG_RT_PROCESS, params, n_params) < 0) {
        fprintf(stderr, "Failed to connect PipeWire filter %s\n", app->node_name);
        return -1;
    }

    return 0;
}

static void destroy_filter(struct fxroute_21_app *app)
{
    if (app->filter != NULL) {
        pw_filter_destroy(app->filter);
        app->filter = NULL;
    }
}

int main(int argc, char **argv)
{
    struct fxroute_21_app app;
    int result;

    memset(&app, 0, sizeof(app));
    app.node_name = DEFAULT_NODE_NAME;
    app.rate = DEFAULT_RATE;
    app.quantum = DEFAULT_QUANTUM;
    app.lowpass_hz = DEFAULT_LOWPASS_HZ;
    app.highpass_hz = DEFAULT_HIGHPASS_HZ;
    app.sub_level_db = DEFAULT_SUB_LEVEL_DB;
    app.sub2_level_db = DEFAULT_SUB_LEVEL_DB;
    app.main_delay_ms = DEFAULT_MAIN_DELAY_MS;
    app.sub_delay_ms = DEFAULT_SUB_DELAY_MS;
    app.sub2_delay_ms = DEFAULT_SUB_DELAY_MS;
    app.sub_polarity_invert = DEFAULT_SUB_POLARITY_INVERT;
    app.sub2_polarity_invert = DEFAULT_SUB_POLARITY_INVERT;
    app.bass_routing = DEFAULT_BASS_ROUTING;
    app.exact_sub_mute_ack_socket = -1;
    app.exact_sub_mute_last_ack = 0;
    atomic_init(&app.exact_sub_mute, false);

    if (parse_args(argc, argv, &app) != 0) {
        return EXIT_FAILURE;
    }
    configure_sub_lowpass(&app);
    configure_main_highpass(&app);
    configure_sub_gain(&app);
    configure_delays(&app);

    if (app.self_test_sub_gain) {
        result = run_sub_gain_self_test(&app);
        delay_line_destroy(&app.main_delay_l);
        delay_line_destroy(&app.main_delay_r);
        delay_line_destroy(&app.sub_delay);
        delay_line_destroy(&app.sub2_delay);
        return result;
    }
    if (app.self_test_alignment) {
        result = run_alignment_self_test(&app);
        delay_line_destroy(&app.main_delay_l);
        delay_line_destroy(&app.main_delay_r);
        delay_line_destroy(&app.sub_delay);
        delay_line_destroy(&app.sub2_delay);
        return result;
    }
    if (app.self_test_bass_routing) {
        result = run_bass_routing_self_test(&app);
        delay_line_destroy(&app.main_delay_l);
        delay_line_destroy(&app.main_delay_r);
        delay_line_destroy(&app.sub_delay);
        delay_line_destroy(&app.sub2_delay);
        return result;
    }
    if (app.self_test_exact_sub_mute) {
        result = run_exact_sub_mute_self_test(&app);
        delay_line_destroy(&app.main_delay_l);
        delay_line_destroy(&app.main_delay_r);
        delay_line_destroy(&app.sub_delay);
        delay_line_destroy(&app.sub2_delay);
        return result;
    }

    pw_init(&argc, &argv);

    if (app.exact_sub_mute_ack_path != NULL) {
        app.exact_sub_mute_ack_socket = socket(AF_UNIX, SOCK_DGRAM | SOCK_NONBLOCK, 0);
        if (app.exact_sub_mute_ack_socket < 0) {
            fprintf(stderr, "Failed to create exact-sub-mute acknowledgement socket: %s\n", strerror(errno));
            pw_deinit();
            return EXIT_FAILURE;
        }
    }

    app.loop = pw_main_loop_new(NULL);
    if (app.loop == NULL) {
        fprintf(stderr, "Failed to create PipeWire main loop\n");
        pw_deinit();
        return EXIT_FAILURE;
    }

    if (pw_loop_add_signal(pw_main_loop_get_loop(app.loop), SIGINT, on_signal, &app) == NULL ||
        pw_loop_add_signal(pw_main_loop_get_loop(app.loop), SIGTERM, on_signal, &app) == NULL ||
        pw_loop_add_signal(pw_main_loop_get_loop(app.loop), SIGUSR1, on_exact_sub_mute_signal, &app) == NULL ||
        pw_loop_add_signal(pw_main_loop_get_loop(app.loop), SIGUSR2, on_exact_sub_mute_signal, &app) == NULL) {
        fprintf(stderr, "Failed to install PipeWire signal handlers\n");
        result = -1;
        goto out;
    }

    if (create_filter(&app) != 0) {
        result = -1;
        goto out;
    }

    fprintf(stderr,
            "FXRoute 2.1 helper started: node=%s expected_rate=%u requested_quantum=%u "
            "lowpass_hz=%.1f lowpass_enabled=%s highpass_hz=%.1f highpass_enabled=%s bass_routing=%s "
            "sub_level_db=%.1f sub_level_gain=%.4f main_delay_ms=%.1f sub_delay_ms=%.1f "
            "sub_polarity=%s sub2_level_db=%.1f sub2_level_gain=%.4f sub2_delay_ms=%.1f "
            "sub2_polarity=%s "
            "ports=input_L,input_R,output_1,output_2,output_3,output_4\n",
            app.node_name,
            app.rate,
            app.quantum,
            app.lowpass_hz,
            app.lowpass_enabled ? "true" : "false",
            app.highpass_hz,
            app.highpass_enabled ? "true" : "false",
            app.bass_routing == FXROUTE_BASS_ROUTING_STEREO ? "stereo" : "mono",
            app.sub_level_db,
            app.sub_level_gain,
            app.main_delay_ms,
            app.sub_delay_ms,
            app.sub_polarity_invert ? "invert" : "normal",
            app.sub2_level_db,
            app.sub2_level_gain,
            app.sub2_delay_ms,
            app.sub2_polarity_invert ? "invert" : "normal");

    pw_main_loop_run(app.loop);
    result = 0;

out:
    if (app.exact_sub_mute_ack_socket >= 0) {
        close(app.exact_sub_mute_ack_socket);
        app.exact_sub_mute_ack_socket = -1;
    }
    destroy_filter(&app);
    delay_line_destroy(&app.main_delay_l);
    delay_line_destroy(&app.main_delay_r);
    delay_line_destroy(&app.sub_delay);
    delay_line_destroy(&app.sub2_delay);
    if (app.loop != NULL) {
        pw_main_loop_destroy(app.loop);
        app.loop = NULL;
    }
    pw_deinit();
    return result == 0 ? EXIT_SUCCESS : EXIT_FAILURE;
}
