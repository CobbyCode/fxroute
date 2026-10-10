/* SPDX-License-Identifier: AGPL-3.0-only */
#define _GNU_SOURCE
#include <assert.h>
#include <dlfcn.h>
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <spa/node/node.h>
#include <spa/node/io.h>
#include <spa/node/command.h>
#include <spa/param/audio/format-utils.h>
#include <spa/param/audio/dsp-utils.h>
#include <spa/param/port-config.h>
#include <spa/support/plugin.h>
#include "decode-buffer.h"

/* Model an AAC source's real RateMatch boundary, not a replacement clock servo. */
int main(int argc, char **argv)
{
    assert(argc == 3);
    unsigned input_rate = 48000, output_rate = atoi(argv[2]);
    void *library = dlopen(argv[1], RTLD_NOW | RTLD_LOCAL); assert(library);
    spa_handle_factory_enum_func_t enumerate = dlsym(library, SPA_HANDLE_FACTORY_ENUM_FUNC_NAME);
    const struct spa_handle_factory *factory; uint32_t index = 0;
    assert(enumerate(&factory, &index) > 0);
    struct spa_dict_item items[] = {{"convert.direction", "input"}, {"clock.quantum-limit", "8192"}};
    struct spa_dict properties = SPA_DICT_INIT(items, 2);
    struct spa_handle *handle = calloc(1, spa_handle_factory_get_size(factory, &properties));
    assert(spa_handle_factory_init(factory, handle, &properties, NULL, 0) == 0);
    struct spa_node *node;
    assert(spa_handle_get_interface(handle, SPA_TYPE_INTERFACE_Node, (void **)&node) == 0);
    char storage[2048]; struct spa_pod_builder builder = SPA_POD_BUILDER_INIT(storage, sizeof(storage));
    struct spa_audio_info_raw raw = {.format = SPA_AUDIO_FORMAT_F32, .rate = input_rate,
        .channels = 2, .position = {SPA_AUDIO_CHANNEL_FL, SPA_AUDIO_CHANNEL_FR}};
    const struct spa_pod *format = spa_format_audio_raw_build(&builder, SPA_PARAM_Format, &raw);
    assert(spa_node_port_set_param(node, SPA_DIRECTION_INPUT, 0, SPA_PARAM_Format, 0, format) == 0);
    const struct spa_pod *config = spa_pod_builder_add_object(&builder,
        SPA_TYPE_OBJECT_ParamPortConfig, SPA_PARAM_PortConfig,
        SPA_PARAM_PORT_CONFIG_direction, SPA_POD_Id(SPA_DIRECTION_OUTPUT),
        SPA_PARAM_PORT_CONFIG_mode, SPA_POD_Id(SPA_PARAM_PORT_CONFIG_MODE_dsp),
        SPA_PARAM_PORT_CONFIG_format, SPA_POD_Pod(format));
    assert(spa_node_set_param(node, SPA_PARAM_PortConfig, 0, config) == 0);
    struct spa_audio_info_dsp dsp = {.format = SPA_AUDIO_FORMAT_DSP_F32};
    spa_pod_builder_init(&builder, storage, sizeof(storage));
    format = spa_format_audio_dsp_build(&builder, SPA_PARAM_Format, &dsp);
    for (unsigned c = 0; c < 2; ++c)
        assert(spa_node_port_set_param(node, SPA_DIRECTION_OUTPUT, c, SPA_PARAM_Format, 0, format) == 0);
    struct spa_io_position position = {.clock = {.rate = {1, output_rate}, .duration = 1024,
        .target_rate = {1, output_rate}, .target_duration = 1024}};
    assert(spa_node_set_io(node, SPA_IO_Position, &position, sizeof(position)) == 0);
    struct spa_io_rate_match match = {.flags = SPA_IO_RATE_MATCH_FLAG_ACTIVE, .rate = 1.0};
    assert(spa_node_port_set_io(node, SPA_DIRECTION_INPUT, 0, SPA_IO_RateMatch, &match, sizeof(match)) == 0);
    static float pcm[3][8192 * 2] __attribute__((aligned(64)));
    struct spa_chunk chunks[3] = {0}; struct spa_data data[3];
    struct spa_buffer buffers[3]; struct spa_io_buffers io[3];
    for (unsigned p = 0; p < 3; ++p) {
        unsigned direction = p ? SPA_DIRECTION_OUTPUT : SPA_DIRECTION_INPUT, port = p ? p - 1 : 0;
        data[p] = (struct spa_data){.type = SPA_DATA_MemPtr, .flags = SPA_DATA_FLAG_READWRITE,
            .fd = -1, .maxsize = sizeof(pcm[p]), .data = pcm[p], .chunk = &chunks[p]};
        buffers[p] = (struct spa_buffer){.n_datas = 1, .datas = &data[p]};
        io[p] = (struct spa_io_buffers)SPA_IO_BUFFERS_INIT;
        struct spa_buffer *buffer = &buffers[p];
        assert(spa_node_port_use_buffers(node, direction, port, 0, &buffer, 1) == 0);
        assert(spa_node_port_set_io(node, direction, port, SPA_IO_Buffers, &io[p], sizeof(io[p])) == 0);
    }
    struct spa_command command = SPA_NODE_COMMAND_INIT(SPA_NODE_COMMAND_Start);
    assert(spa_node_send_command(node, &command) == 0);
    struct spa_bt_decode_buffer decode;
    assert(spa_bt_decode_buffer_init(&decode, NULL, 8, input_rate, 8192, 8192) == 0);
    uint64_t base = 1000000000, packet = 0, dropped = 0;
    unsigned underruns = 0, silent = 0, active = 0;
    double correction = 0;
    unsigned ticks = (output_rate * 6 + 1023) / 1024;
    for (unsigned tick = 0; tick < ticks; ++tick) {
        uint64_t now = base + (uint64_t)tick * 1024 * SPA_NSEC_PER_SEC / output_rate;
        position.clock.nsec = now;
        position.clock.next_nsec = base + (uint64_t)(tick + 1) * 1024 * SPA_NSEC_PER_SEC / output_rate;
        while (base + packet * 1024 * SPA_NSEC_PER_SEC / input_rate <= now) {
            size_t before = spa_bt_decode_buffer_get_size(&decode);
            uint32_t room; float *samples = spa_bt_decode_buffer_get_write(&decode, &room);
            if ((uint64_t)tick * 1024 > output_rate)
                dropped += (before - spa_bt_decode_buffer_get_size(&decode)) / 8;
            assert(room >= 1024 * 8);
            for (unsigned i = 0; i < 2048; ++i) samples[i] = .125f;
            spa_bt_decode_buffer_write_packet(&decode, 1024 * 8,
                base + packet * 1024 * SPA_NSEC_PER_SEC / input_rate);
            ++packet;
        }
        io[1].status = io[2].status = SPA_STATUS_NEED_DATA;
        for (unsigned attempt = 0; attempt < 8 && io[1].status != SPA_STATUS_HAVE_DATA; ++attempt) {
            if (io[0].status != SPA_STATUS_HAVE_DATA) {
                unsigned requested = match.size; assert(requested <= 8192);
                unsigned before = decode.read_index;
                spa_bt_decode_buffer_process(&decode, requested, 1024 * SPA_NSEC_PER_SEC / output_rate);
                if ((uint64_t)tick * 1024 > output_rate) {
                    dropped += (decode.read_index - before) / 8;
                    underruns += decode.buffering;
                }
                if ((uint64_t)tick * 1024 > output_rate * 5)
                    correction = fmax(correction, fabs(decode.corr - 1.0));
                uint32_t available;
                const void *samples = spa_bt_decode_buffer_get_read(&decode, &available);
                unsigned bytes = SPA_MIN(available, requested * 8);
                memcpy(pcm[0], samples, bytes);
                memset((char *)pcm[0] + bytes, 0, requested * 8 - bytes);
                spa_bt_decode_buffer_read(&decode, bytes);
                match.rate = 1.0 / decode.corr;
                chunks[0] = (struct spa_chunk){.size = requested * 8, .stride = 8};
                io[0].status = SPA_STATUS_HAVE_DATA; io[0].buffer_id = 0;
                spa_bt_decode_buffer_set_next(&decode, position.clock.next_nsec,
                    match.delay, match.delay_frac, match.rate, true);
            }
            assert(spa_node_process(node) >= 0);
        }
        assert(io[1].status == SPA_STATUS_HAVE_DATA && chunks[1].size == 1024 * 4);
        if ((uint64_t)tick * 1024 > output_rate * 2) for (unsigned i = 0; i < 1024; ++i) {
            ++active; silent += fabsf(pcm[1][i]) < .01f;
        }
        usleep(1024 * 1000000 / output_rate);
    }
    fprintf(stderr, "Bluetooth feedback: output_rate=%u dropped=%lu underruns=%u silent=%u/%u max_correction=%g delay=%u\n",
        output_rate, dropped, underruns, silent, active, correction, match.delay);
    spa_bt_decode_buffer_clear(&decode);
    spa_handle_clear(handle); free(handle); dlclose(library);
    assert(dropped == 0 && "Fixed SRC latency must not make Bluetooth drop receive PCM");
    assert(underruns == 0 && "Clock matching must not starve a healthy Bluetooth source");
    assert(silent == 0 && "Steady Bluetooth PCM must not contain injected silence");
    assert(correction < .004999 && "Fixed SRC latency must not saturate the clock servo");
    return 0;
}
