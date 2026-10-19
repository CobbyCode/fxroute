/* SPDX-License-Identifier: AGPL-3.0-only */
/* Raw F32 -> DSP: preserve all 1024 EMPTY frames before the next block. */
#include <dlfcn.h>
#include <stdio.h>
#include <stdlib.h>
#include <spa/node/node.h>
#include <spa/node/io.h>
#include <spa/node/command.h>
#include <spa/param/audio/format-utils.h>
#include <spa/param/audio/dsp-utils.h>
#include <spa/param/port-config.h>
#include <spa/support/plugin.h>
#include <spa/utils/result.h>

struct buf {
    struct spa_buffer buffer;
    struct spa_data data;
    struct spa_chunk chunk;
};

static void check(int result, const char *what)
{
    if (result < 0) {
        fprintf(stderr, "%s: %s\n", what, spa_strerror(result));
        exit(2);
    }
}

static void init_buf(struct buf *b, void *memory, uint32_t size)
{
    *b = (struct buf){0};
    b->buffer.n_datas = 1;
    b->buffer.datas = &b->data;
    b->data = (struct spa_data){.type = SPA_DATA_MemPtr, .flags = SPA_DATA_FLAG_READWRITE,
        .fd = -1, .maxsize = size, .data = memory, .chunk = &b->chunk};
}

int main(int argc, char **argv)
{
    if (argc != 2) {
        fprintf(stderr, "usage: src_empty_duration <plugin.so>\n");
        return 2;
    }
    void *library = dlopen(argv[1], RTLD_NOW | RTLD_LOCAL);
    if (!library) {
        fprintf(stderr, "%s\n", dlerror());
        return 2;
    }
    spa_handle_factory_enum_func_t enumerate = dlsym(library, SPA_HANDLE_FACTORY_ENUM_FUNC_NAME);
    const struct spa_handle_factory *factory;
    uint32_t index = 0;
    if (!enumerate || enumerate(&factory, &index) != 1)
        return 2;
    struct spa_dict_item props[] = {{"clock.quantum-limit", "8192"}, {"convert.direction", "input"}};
    struct spa_dict properties = SPA_DICT_INIT(props, 2);
    struct spa_handle *handle = calloc(1, spa_handle_factory_get_size(factory, &properties));
    check(spa_handle_factory_init(factory, handle, &properties, NULL, 0), "init");
    struct spa_node *node;
    check(spa_handle_get_interface(handle, SPA_TYPE_INTERFACE_Node, (void **)&node), "node");

    uint8_t storage[2048];
    struct spa_pod_builder b = SPA_POD_BUILDER_INIT(storage, sizeof(storage));
    struct spa_pod *config = spa_pod_builder_add_object(&b, SPA_TYPE_OBJECT_ParamPortConfig, SPA_PARAM_PortConfig,
        SPA_PARAM_PORT_CONFIG_direction, SPA_POD_Id(SPA_DIRECTION_INPUT),
        SPA_PARAM_PORT_CONFIG_mode, SPA_POD_Id(SPA_PARAM_PORT_CONFIG_MODE_convert));
    check(spa_node_set_param(node, SPA_PARAM_PortConfig, 0, config), "input config");
    spa_pod_builder_init(&b, storage, sizeof(storage));
    struct spa_audio_info_raw raw = {.format = SPA_AUDIO_FORMAT_F32, .rate = 48000, .channels = 2,
        .position = {SPA_AUDIO_CHANNEL_FL, SPA_AUDIO_CHANNEL_FR}};
    struct spa_pod *format = spa_format_audio_raw_build(&b, SPA_PARAM_Format, &raw);
    check(spa_node_port_set_param(node, SPA_DIRECTION_INPUT, 0, SPA_PARAM_Format, 0, format), "input format");
    spa_pod_builder_init(&b, storage, sizeof(storage));
    raw.format = SPA_AUDIO_FORMAT_F32P;
    format = spa_format_audio_raw_build(&b, SPA_PARAM_Format, &raw);
    config = spa_pod_builder_add_object(&b, SPA_TYPE_OBJECT_ParamPortConfig, SPA_PARAM_PortConfig,
        SPA_PARAM_PORT_CONFIG_direction, SPA_POD_Id(SPA_DIRECTION_OUTPUT),
        SPA_PARAM_PORT_CONFIG_mode, SPA_POD_Id(SPA_PARAM_PORT_CONFIG_MODE_dsp),
        SPA_PARAM_PORT_CONFIG_format, SPA_POD_Pod(format));
    check(spa_node_set_param(node, SPA_PARAM_PortConfig, 0, config), "output config");
    spa_pod_builder_init(&b, storage, sizeof(storage));
    struct spa_audio_info_dsp dsp = {.format = SPA_AUDIO_FORMAT_DSP_F32};
    format = spa_format_audio_dsp_build(&b, SPA_PARAM_Format, &dsp);
    for (int c = 0; c < 2; c++)
        check(spa_node_port_set_param(node, SPA_DIRECTION_OUTPUT, c, SPA_PARAM_Format, 0, format), "output format");
    struct spa_io_position position = {.clock = {.rate = {1, 48000}, .target_rate = {1, 48000},
        .rate_diff = 1.0, .duration = 256, .target_duration = 256}};
    check(spa_node_set_io(node, SPA_IO_Position, &position, sizeof(position)), "position");

    float pcm[2048] = {0}, output[2][256];
    struct buf in, out[2];
    struct spa_io_buffers in_io = SPA_IO_BUFFERS_INIT, out_io[2];
    init_buf(&in, pcm, sizeof(pcm));
    in.chunk = (struct spa_chunk){.size = sizeof(pcm), .stride = 8, .flags = SPA_CHUNK_FLAG_EMPTY};
    struct spa_buffer *buffer = &in.buffer;
    check(spa_node_port_use_buffers(node, SPA_DIRECTION_INPUT, 0, 0, &buffer, 1), "input buffers");
    check(spa_node_port_set_io(node, SPA_DIRECTION_INPUT, 0, SPA_IO_Buffers, &in_io, sizeof(in_io)), "input io");
    for (int c = 0; c < 2; c++) {
        init_buf(&out[c], output[c], sizeof(output[c]));
        buffer = &out[c].buffer;
        check(spa_node_port_use_buffers(node, SPA_DIRECTION_OUTPUT, c, 0, &buffer, 1), "output buffers");
        out_io[c] = (struct spa_io_buffers)SPA_IO_BUFFERS_INIT;
        check(spa_node_port_set_io(node, SPA_DIRECTION_OUTPUT, c, SPA_IO_Buffers, &out_io[c], sizeof(out_io[c])), "output io");
    }
    in_io.status = SPA_STATUS_HAVE_DATA;
    in_io.buffer_id = 0;
    struct spa_command start = SPA_NODE_COMMAND_INIT(SPA_NODE_COMMAND_Start);
    check(spa_node_send_command(node, &start), "start");

    int first_nonzero = -1;
    for (int cycle = 0; cycle < 5; cycle++) {
        if (in_io.status != SPA_STATUS_HAVE_DATA) {
            for (int i = 0; i < 512; i++)
                pcm[i] = 0.25f;
            in.chunk = (struct spa_chunk){.size = 512 * sizeof(float), .stride = 8};
            in_io.status = SPA_STATUS_HAVE_DATA;
            in_io.buffer_id = 0;
        }
        for (int c = 0; c < 2; c++)
            out_io[c].status = SPA_STATUS_NEED_DATA;
        check(spa_node_process(node), "process");
        if (out_io[0].status != SPA_STATUS_HAVE_DATA || out[0].chunk.size != 256 * sizeof(float) ||
            out_io[1].status != SPA_STATUS_HAVE_DATA || out[1].chunk.size != 256 * sizeof(float)) {
            fprintf(stderr, "cycle %d: expected complete stereo quantum\n", cycle);
            return 2;
        }
        printf("cycle=%d input_status=%d output_frames=%u first_sample=%g\n",
            cycle, in_io.status, out[0].chunk.size / 4, output[0][0]);
        for (int c = 0; c < 2; c++)
            for (int i = 0; i < 256; i++)
                if (output[c][i] != (cycle < 4 ? 0.0f : 0.25f)) {
                    fprintf(stderr, "cycle %d channel %d sample %d: silence duration changed\n", cycle, c, i);
                    return 1;
                }
        if (output[0][0] != 0 && first_nonzero < 0)
            first_nonzero = cycle;
    }
    printf("FIRST_NONZERO_CYCLE %d\n", first_nonzero);
    spa_handle_clear(handle);
    free(handle);
    dlclose(library);
    return first_nonzero == 4 ? 0 : 1;
}
