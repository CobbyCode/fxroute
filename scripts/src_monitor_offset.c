/* SPDX-License-Identifier: AGPL-3.0-only */
/* Sink-side converter (DSP inputs + monitors -> device port) across idle runs
 * and relinks. Emulates PipeWire's port mixer: unlinked inputs get a cleared
 * EMPTY quantum-limit buffer that is kept until the converter returns it; a
 * linked peer is passed through. "renegotiate" relinks hand the converter the
 * peer buffer at once (DSP engine rebuild), "stream" relinks wait for the
 * returned buffer (stream start). Every sample encodes channel and frame, so
 * lost, repeated or foreign samples are detected on the device and monitor
 * ports. Tagged memory sits behind and beyond all inputs; "exact" allocates
 * inputs without slack for AddressSanitizer builds.
 * Usage: src_monitor_offset <plugin.so> <channels> <s32|f32> <idle-quantum>
 *        <resume-quantum> <empty-cycles> <renegotiate|stream> <rounds>
 *        <none|rate> [exact] [split] [retained] */
#ifndef _GNU_SOURCE
#define _GNU_SOURCE
#endif
#include <dlfcn.h>
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <spa/node/node.h>
#include <spa/node/io.h>
#include <spa/node/command.h>
#include <spa/param/audio/format-utils.h>
#include <spa/param/audio/dsp-utils.h>
#include <spa/param/port-config.h>
#include <spa/support/plugin.h>
#include <spa/utils/result.h>

#define MAX_CH 8
#define LIMIT 8192
#define BEHIND 16384
#define TAG_TAIL 0x7fc0aaaau
#define TAG_BEHIND 0x7fc0bbbbu
#define TAG_MIXER 0x7fc0ccccu
#define CODE_SCALE 1048576.0

static int failures;

#define FAIL(...) do { if (failures++ < 20) { fprintf(stderr, __VA_ARGS__); fputc('\n', stderr); } } while (0)

static void check(int res, const char *what)
{
    if (res < 0) { fprintf(stderr, "%s: %s\n", what, spa_strerror(res)); exit(2); }
}

/* Sample value carrying (channel, frame); exact in F32, S32 and S24. */
static float code_value(uint32_t channel, uint64_t frame)
{
    return (float)((channel * 65536u + (uint32_t)(frame % 65535u) + 1u) / CODE_SCALE);
}

struct track {
    uint64_t next[MAX_CH], seen[MAX_CH];
    const char *name;
};

/* Accept silence or the next frame of this channel; anything else is lost,
 * repeated or foreign data. */
static void expect(struct track *t, uint32_t channel, double value, int cycle)
{
    if (value == 0.0)
        return;
    if (!isfinite(value)) {
        FAIL("%s ch%u cycle %d: nonfinite sample %.9g", t->name, channel, cycle, value);
        return;
    }
    double scaled = value * CODE_SCALE;
    long code = lround(scaled);
    uint32_t want = channel * 65536u + (uint32_t)(t->next[channel] % 65535u) + 1u;
    if (fabs(scaled - (double)code) > 1e-3 || code != (long)want) {
        FAIL("%s ch%u cycle %d: got %.9g (code %ld) expected frame %llu (code %u)", t->name, channel, cycle,
             value, code, (unsigned long long)t->next[channel], want);
        return;
    }
    t->next[channel]++;
    t->seen[channel]++;
}

struct buf { struct spa_buffer buffer; struct spa_data data; struct spa_chunk chunk; };

static void init_buf(struct buf *b, void *memory, uint32_t maxsize, uint32_t flags)
{
    memset(b, 0, sizeof(*b));
    b->buffer.n_datas = 1;
    b->buffer.datas = &b->data;
    b->data = (struct spa_data){ .type = SPA_DATA_MemPtr, .flags = flags, .fd = -1, .maxsize = maxsize,
                                 .data = memory, .chunk = &b->chunk };
}

static void *alloc_tagged(uint32_t samples, uint32_t valid, uint32_t tail, int exact)
{
    if (exact)
        return calloc(samples, sizeof(float));
    uint32_t *words = aligned_alloc(64, (size_t)(samples + BEHIND) * sizeof(float));
    for (uint32_t i = 0; i < samples + BEHIND; i++)
        words[i] = i < valid ? 0 : i < samples ? tail : TAG_BEHIND;
    return words;
}

static struct spa_audio_info_raw layout(enum spa_audio_format format, uint32_t rate, uint32_t channels)
{
    static const uint32_t positions[][MAX_CH] = {
        { SPA_AUDIO_CHANNEL_FL, SPA_AUDIO_CHANNEL_FR },
        { SPA_AUDIO_CHANNEL_FL, SPA_AUDIO_CHANNEL_FR, SPA_AUDIO_CHANNEL_RL, SPA_AUDIO_CHANNEL_RR,
          SPA_AUDIO_CHANNEL_FC, SPA_AUDIO_CHANNEL_LFE },
        { SPA_AUDIO_CHANNEL_FL, SPA_AUDIO_CHANNEL_FR, SPA_AUDIO_CHANNEL_RL, SPA_AUDIO_CHANNEL_RR,
          SPA_AUDIO_CHANNEL_FC, SPA_AUDIO_CHANNEL_LFE, SPA_AUDIO_CHANNEL_SL, SPA_AUDIO_CHANNEL_SR },
    };
    struct spa_audio_info_raw raw = { .format = format, .rate = rate, .channels = channels };
    memcpy(raw.position, positions[channels == 2 ? 0 : channels == 6 ? 1 : 2], channels * sizeof(uint32_t));
    return raw;
}

/* A device format change drops the port's buffers, as in PipeWire. */
static void set_device(struct spa_node *node, enum spa_audio_format format, uint32_t rate, uint32_t channels,
                       struct spa_buffer *buffer, struct spa_io_buffers *io)
{
    uint8_t storage[2048];
    struct spa_pod_builder builder = SPA_POD_BUILDER_INIT(storage, sizeof(storage));
    struct spa_audio_info_raw raw = layout(format, rate, channels);
    struct spa_pod *pod = spa_format_audio_raw_build(&builder, SPA_PARAM_Format, &raw);
    check(spa_node_port_set_param(node, SPA_DIRECTION_OUTPUT, 0, SPA_PARAM_Format, 0, pod), "device format");
    if (!buffer)
        return;
    check(spa_node_port_use_buffers(node, SPA_DIRECTION_OUTPUT, 0, 0, &buffer, 1), "device buffers");
    *io = (struct spa_io_buffers)SPA_IO_BUFFERS_INIT;
    check(spa_node_port_set_io(node, SPA_DIRECTION_OUTPUT, 0, SPA_IO_Buffers, io, sizeof(*io)), "device io");
}

int main(int argc, char **argv)
{
    if (argc < 10) {
        fprintf(stderr, "usage: src_monitor_offset <plugin.so> <channels> <s32|f32> <idle-quantum> <resume-quantum> "
                        "<empty-cycles> <renegotiate|stream> <rounds> <none|rate> [exact] [split] [retained]\n");
        return 2;
    }
    uint32_t channels = (uint32_t)atoi(argv[2]);
    int s32 = !strcmp(argv[3], "s32");
    uint32_t idle_q = (uint32_t)atoi(argv[4]), resume_q = (uint32_t)atoi(argv[5]);
    int empty_cycles = atoi(argv[6]), renegotiate = !strcmp(argv[7], "renegotiate"), rounds = atoi(argv[8]);
    int rate_change = !strcmp(argv[9], "rate"), exact = 0, split = 0, retained = 0;
    for (int i = 10; i < argc; i++) {
        exact |= !strcmp(argv[i], "exact");
        split |= !strcmp(argv[i], "split");
        retained |= !strcmp(argv[i], "retained");
    }
    if ((channels != 2 && channels != 6 && channels != 8) || !idle_q || !resume_q ||
        idle_q > LIMIT || resume_q > LIMIT || rounds < 1 || empty_cycles < 0 ||
        (retained && (idle_q != resume_q || idle_q > LIMIT / 2 || empty_cycles != 0))) {
        fprintf(stderr, "invalid arguments\n");
        return 2;
    }

    void *library = dlopen(argv[1], RTLD_NOW | RTLD_LOCAL);
    if (!library) { fprintf(stderr, "%s\n", dlerror()); return 2; }
    spa_handle_factory_enum_func_t enumerate = dlsym(library, SPA_HANDLE_FACTORY_ENUM_FUNC_NAME);
    const struct spa_handle_factory *factory = NULL, *candidate;
    for (uint32_t index = 0; enumerate(&candidate, &index) == 1;)
        if (!strcmp(candidate->name, "fxroute.src.convert") || !strcmp(candidate->name, "audio.convert"))
            factory = factory ? factory : candidate;
    if (!factory) { fprintf(stderr, "no converter factory\n"); return 2; }
    struct spa_dict_item items[] = { { "clock.quantum-limit", "8192" },
                                     { "convert.direction", split ? "input" : "output" } };
    struct spa_dict properties = SPA_DICT_INIT(items, 2);
    struct spa_handle *handle = calloc(1, spa_handle_factory_get_size(factory, &properties));
    check(spa_handle_factory_init(factory, handle, &properties, NULL, 0), "init");
    struct spa_node *node;
    check(spa_handle_get_interface(handle, SPA_TYPE_INTERFACE_Node, (void **)&node), "node");

    uint32_t rate = rate_change ? 44100 : 48000;
    enum spa_audio_format device_format = s32 ? SPA_AUDIO_FORMAT_S32_LE : SPA_AUDIO_FORMAT_F32;
    uint8_t storage[2048];
    struct spa_pod_builder builder = SPA_POD_BUILDER_INIT(storage, sizeof(storage));
    struct spa_pod *config = spa_pod_builder_add_object(&builder, SPA_TYPE_OBJECT_ParamPortConfig, SPA_PARAM_PortConfig,
        SPA_PARAM_PORT_CONFIG_direction, SPA_POD_Id(SPA_DIRECTION_OUTPUT),
        SPA_PARAM_PORT_CONFIG_mode, SPA_POD_Id(SPA_PARAM_PORT_CONFIG_MODE_convert));
    check(spa_node_set_param(node, SPA_PARAM_PortConfig, 0, config), "device port config");
    set_device(node, device_format, rate, channels, NULL, NULL);
    spa_pod_builder_init(&builder, storage, sizeof(storage));
    struct spa_audio_info_raw planar = layout(SPA_AUDIO_FORMAT_F32P, rate, channels);
    struct spa_pod *planar_format = spa_format_audio_raw_build(&builder, SPA_PARAM_Format, &planar);
    config = spa_pod_builder_add_object(&builder, SPA_TYPE_OBJECT_ParamPortConfig, SPA_PARAM_PortConfig,
        SPA_PARAM_PORT_CONFIG_direction, SPA_POD_Id(SPA_DIRECTION_INPUT),
        SPA_PARAM_PORT_CONFIG_mode, SPA_POD_Id(SPA_PARAM_PORT_CONFIG_MODE_dsp),
        SPA_PARAM_PORT_CONFIG_monitor, SPA_POD_Bool(true),
        SPA_PARAM_PORT_CONFIG_format, SPA_POD_Pod(planar_format));
    check(spa_node_set_param(node, SPA_PARAM_PortConfig, 0, config), "dsp port config");
    spa_pod_builder_init(&builder, storage, sizeof(storage));
    struct spa_audio_info_dsp dsp = { .format = SPA_AUDIO_FORMAT_DSP_F32 };
    struct spa_pod *dsp_format = spa_format_audio_dsp_build(&builder, SPA_PARAM_Format, &dsp);
    for (uint32_t c = 0; c < channels; c++) {
        check(spa_node_port_set_param(node, SPA_DIRECTION_INPUT, c, SPA_PARAM_Format, 0, dsp_format), "input format");
        check(spa_node_port_set_param(node, SPA_DIRECTION_OUTPUT, c + 1, SPA_PARAM_Format, 0, dsp_format), "monitor format");
    }
    struct spa_io_position position = { .clock = { .rate = { 1, rate }, .target_rate = { 1, rate }, .rate_diff = 1.0,
                                                   .duration = idle_q, .target_duration = idle_q } };
    check(spa_node_set_io(node, SPA_IO_Position, &position, sizeof(position)), "position");

    /* Per input: two mixer-owned buffers registered with the converter and
     * two peer buffers of exactly one resume quantum (plus tagged tail). */
    static struct buf registered[MAX_CH][2], mixer[MAX_CH][2], peer[MAX_CH][2], monitor[MAX_CH], device;
    static struct spa_io_buffers in_io[MAX_CH], mon_io[MAX_CH], out_io;
    uint32_t peer_samples = exact ? idle_q * (retained ? 2 : 1) : LIMIT;
    int mixer_next[MAX_CH] = { 0 }, peer_next[MAX_CH] = { 0 };
    for (uint32_t c = 0; c < channels; c++) {
        struct spa_buffer *list[2];
        for (int b = 0; b < 2; b++) {
            void *mix_memory = exact ? calloc(LIMIT, sizeof(float)) : alloc_tagged(LIMIT, LIMIT, 0, 0);
            if (!exact)
                for (uint32_t i = LIMIT; i < LIMIT + BEHIND; i++) ((uint32_t *)mix_memory)[i] = TAG_MIXER;
            init_buf(&mixer[c][b], mix_memory, LIMIT * sizeof(float), SPA_DATA_FLAG_READWRITE | SPA_DATA_FLAG_DYNAMIC);
            init_buf(&peer[c][b], alloc_tagged(peer_samples, resume_q, TAG_TAIL, exact), peer_samples * sizeof(float),
                     SPA_DATA_FLAG_READWRITE | SPA_DATA_FLAG_DYNAMIC);
            init_buf(&registered[c][b], mix_memory, LIMIT * sizeof(float), SPA_DATA_FLAG_READWRITE | SPA_DATA_FLAG_DYNAMIC);
            list[b] = &registered[c][b].buffer;
        }
        check(spa_node_port_use_buffers(node, SPA_DIRECTION_INPUT, c, 0, list, 2), "input buffers");
        in_io[c] = (struct spa_io_buffers)SPA_IO_BUFFERS_INIT;
        check(spa_node_port_set_io(node, SPA_DIRECTION_INPUT, c, SPA_IO_Buffers, &in_io[c], sizeof(in_io[c])), "input io");
        init_buf(&monitor[c], aligned_alloc(64, LIMIT * sizeof(float)), LIMIT * sizeof(float), SPA_DATA_FLAG_READWRITE);
        struct spa_buffer *mon = &monitor[c].buffer;
        check(spa_node_port_use_buffers(node, SPA_DIRECTION_OUTPUT, c + 1, 0, &mon, 1), "monitor buffers");
        mon_io[c] = (struct spa_io_buffers)SPA_IO_BUFFERS_INIT;
        check(spa_node_port_set_io(node, SPA_DIRECTION_OUTPUT, c + 1, SPA_IO_Buffers, &mon_io[c], sizeof(mon_io[c])), "monitor io");
    }
    init_buf(&device, aligned_alloc(64, LIMIT * MAX_CH * 4), LIMIT * MAX_CH * 4, SPA_DATA_FLAG_READWRITE);
    check(spa_node_port_use_buffers(node, SPA_DIRECTION_OUTPUT, 0, 0, (struct spa_buffer *[]){ &device.buffer }, 1),
          "device buffers");
    out_io = (struct spa_io_buffers)SPA_IO_BUFFERS_INIT;
    check(spa_node_port_set_io(node, SPA_DIRECTION_OUTPUT, 0, SPA_IO_Buffers, &out_io, sizeof(out_io)), "device io");
    struct spa_command start = SPA_NODE_COMMAND_INIT(SPA_NODE_COMMAND_Start);
    check(spa_node_send_command(node, &start), "start");

    /* Schedule: per round a linked phase, then an idle run; a final linked
     * phase. The rate variant starts at 44.1 kHz and moves the device to
     * 48 kHz when the first idle run begins, the way PipeWire reconfigures a
     * running sink: suspend, new format, start. */
    struct track device_track = { .name = "device" }, monitor_track = { .name = "monitor" };
    uint64_t delivered[MAX_CH] = { 0 };
    int linked_cycles = 4, cycle = 0, resume_cycle = -1, first_output_delay = -1, short_cycles = 0;
    uint32_t max_monitor = 0;
    for (int round = 0; round <= rounds; round++) {
        for (int phase = 0; phase < 2; phase++) {
            int linked = phase == 0, steps = linked ? linked_cycles : empty_cycles;
            if (round == rounds && !linked)
                break;
            uint32_t quantum = linked && round > 0 ? resume_q : idle_q;
            if (!linked && rate_change && rate != 48000) {
                struct spa_command suspend = SPA_NODE_COMMAND_INIT(SPA_NODE_COMMAND_Suspend);
                check(spa_node_send_command(node, &suspend), "suspend");
                rate = 48000;
                position.clock.rate = position.clock.target_rate = SPA_FRACTION(1, rate);
                set_device(node, device_format, rate, channels, &device.buffer, &out_io);
                check(spa_node_send_command(node, &start), "restart");
            }
            for (int step = 0; step < steps; step++, cycle++) {
                int relink = linked && step == 0 && round > 0;
                if (relink && resume_cycle < 0)
                    resume_cycle = cycle;
                position.clock.rate = position.clock.target_rate = SPA_FRACTION(1, rate);
                position.clock.duration = position.clock.target_duration = quantum;
                /* Port mixer: refill only returned inputs, except a renegotiated relink. */
                for (uint32_t c = 0; c < channels; c++) {
                    if (in_io[c].status == SPA_STATUS_HAVE_DATA && !(relink && renegotiate))
                        continue;
                    int id = mixer_next[c];
                    mixer_next[c] ^= 1;
                    if (linked) {
                        struct buf *p = &peer[c][peer_next[c]];
                        peer_next[c] ^= 1;
                        uint32_t offered = quantum * (retained ? 2 : 1);
                        if (exact && p->data.maxsize != offered * sizeof(float)) {
                            free(p->data.data);
                            p->data.data = calloc(offered, sizeof(float));
                            p->data.maxsize = offered * sizeof(float);
                        }
                        float *samples = p->data.data;
                        for (uint32_t i = 0; i < offered; i++)
                            samples[i] = code_value(c, delivered[c] + i);
                        delivered[c] += offered;
                        p->chunk = (struct spa_chunk){ .offset = 0, .size = offered * sizeof(float), .stride = sizeof(float) };
                        registered[c][id].buffer = p->buffer;
                    } else {
                        struct buf *m = &mixer[c][id];
                        memset(m->data.data, 0, LIMIT * sizeof(float));
                        m->chunk = (struct spa_chunk){ .offset = 0, .size = LIMIT * sizeof(float), .stride = sizeof(float),
                                                       .flags = SPA_CHUNK_FLAG_EMPTY };
                        registered[c][id].buffer = m->buffer;
                    }
                    in_io[c] = (struct spa_io_buffers){ .status = SPA_STATUS_HAVE_DATA, .buffer_id = (uint32_t)id };
                }
                memset(device.data.data, 0x55, LIMIT * MAX_CH * 4);
                int res = spa_node_process(node);
                if (res < 0) { FAIL("cycle %d: process %s", cycle, spa_strerror(res)); break; }

                uint32_t frames = out_io.status == SPA_STATUS_HAVE_DATA ? device.chunk.size / (channels * 4) : 0;
                if (linked && frames != quantum) {
                    short_cycles++;
                    FAIL("cycle %d: device got %u of %u frames while linked", cycle, frames, quantum);
                }
                uint64_t before = device_track.seen[0];
                for (uint32_t i = 0; i < frames * channels; i++) {
                    double value = s32 ? ((int32_t *)device.data.data)[i] / 2147483648.0 : ((float *)device.data.data)[i];
                    expect(&device_track, i % channels, value, cycle);
                }
                if (linked && device_track.seen[0] - before != quantum)
                    FAIL("cycle %d: first/current device block has %llu of %u valid frames", cycle,
                         (unsigned long long)(device_track.seen[0] - before), quantum);
                if (first_output_delay < 0 && resume_cycle >= 0 && device_track.seen[0] > before)
                    first_output_delay = cycle - resume_cycle;
                if (out_io.status == SPA_STATUS_HAVE_DATA)
                    out_io.status = SPA_STATUS_NEED_DATA;
                for (uint32_t c = 0; c < channels; c++) {
                    if (mon_io[c].status != SPA_STATUS_HAVE_DATA)
                        continue;
                    uint32_t samples = monitor[c].chunk.size / sizeof(float);
                    /* A retained input's monitor exposes its unread suffix,
                     * not just the samples consumed by the device this cycle. */
                    if (retained) {
                        uint32_t remaining = (uint32_t)(delivered[c] - device_track.seen[c] + quantum);
                        if (samples != remaining)
                            FAIL("monitor ch%u cycle %d: got %u of %u remaining frames", c, cycle, samples, remaining);
                        monitor_track.next[c] = device_track.next[c] - quantum;
                    }
                    max_monitor = SPA_MAX(max_monitor, samples);
                    const uint32_t *words = monitor[c].data.data;
                    for (uint32_t i = 0; i < samples; i++) {
                        if (words[i] == TAG_TAIL || words[i] == TAG_BEHIND || words[i] == TAG_MIXER)
                            FAIL("monitor ch%u cycle %d: sample %u is foreign memory 0x%08x", c, cycle, i, words[i]);
                        else
                            expect(&monitor_track, c, ((float *)monitor[c].data.data)[i], cycle);
                    }
                    mon_io[c].status = SPA_STATUS_NEED_DATA;
                }
            }
        }
    }
    for (uint32_t c = 0; c < channels; c++) {
        if (device_track.seen[c] != delivered[c])
            FAIL("device ch%u: played %llu of %llu delivered frames", c,
                 (unsigned long long)device_track.seen[c], (unsigned long long)delivered[c]);
        uint64_t expected_monitor = retained ? delivered[c] / 2 * 3 : delivered[c];
        if (monitor_track.seen[c] != expected_monitor)
            FAIL("monitor ch%u: mirrored %llu of %llu delivered frames", c,
                 (unsigned long long)monitor_track.seen[c], (unsigned long long)expected_monitor);
    }
    printf("MONITOR_OFFSET {\"factory\":\"%s\",\"channels\":%u,\"cycles\":%d,\"delivered\":%llu,\"played\":%llu,"
           "\"mirrored\":%llu,\"short_cycles\":%d,\"first_output_delay\":%d,\"max_monitor\":%u,\"failures\":%d}\n",
           factory->name, channels, cycle, (unsigned long long)delivered[0], (unsigned long long)device_track.seen[0],
           (unsigned long long)monitor_track.seen[0], short_cycles, first_output_delay, max_monitor, failures);
    spa_handle_clear(handle);
    free(handle);
    for (uint32_t c = 0; c < channels; c++) {
        for (int b = 0; b < 2; b++) {
            free(mixer[c][b].data.data);
            free(peer[c][b].data.data);
        }
        free(monitor[c].data.data);
    }
    free(device.data.data);
    dlclose(library);
    return failures ? 1 : 0;
}
