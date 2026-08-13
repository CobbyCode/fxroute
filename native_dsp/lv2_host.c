#include "lv2_host.h"

#include <lilv/lilv.h>
#include <lv2/atom/atom.h>
#include <lv2/buf-size/buf-size.h>
#include <lv2/core/lv2.h>
#include <lv2/options/options.h>
#include <lv2/parameters/parameters.h>
#include <lv2/resize-port/resize-port.h>
#include <lv2/urid/urid.h>

#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define MAX_URIDS 256
#define MAX_URI 255

struct fx_lv2_host {
    LilvWorld *world;
    LilvInstance *instance;
    unsigned port_count;
    char **symbols;
    fx_lv2_port_type *types;
    float *controls;
    void **atom_buffers;
    bool *atom_inputs;
    float **audio_buffers;
    float *zero_buffer;
    uint32_t audio_in[2], audio_out[2], sidechain_in[2], latency_port, max_block;
    bool active;
    char urids[MAX_URIDS][MAX_URI + 1];
    uint32_t urid_count;
    LV2_URID_Map map;
    LV2_Options_Option options[6];
    int32_t min_frames, max_frames, nominal_frames, sequence_size;
    float rate;
    LV2_URID atom_chunk, atom_sequence;
    LV2_Feature map_feature, bounded_feature, options_feature;
    const LV2_Feature *features[4];
};

static void errorf(char *error, size_t size, const char *format, ...) {
    va_list args;
    if (!error || !size) return;
    va_start(args, format);
    (void)vsnprintf(error, size, format, args);
    va_end(args);
}

/* This fixed table also makes map() safe if a plugin calls it from run(). */
static LV2_URID map_uri(void *handle, const char *uri) {
    fx_lv2_host *host = handle;
    size_t length;
    if (!uri) return 0;
    for (uint32_t i = 0; i < host->urid_count; ++i)
        if (!strcmp(host->urids[i], uri)) return i + 1;
    length = strlen(uri);
    if (host->urid_count == MAX_URIDS || length > MAX_URI) return 0;
    memcpy(host->urids[host->urid_count], uri, length + 1);
    return ++host->urid_count;
}

static void setup_features(fx_lv2_host *h) {
    LV2_URID integer, real;
    h->map.handle = h;
    h->map.map = map_uri;
    integer = map_uri(h, LV2_ATOM__Int);
    real = map_uri(h, LV2_ATOM__Float);
    h->atom_chunk = map_uri(h, LV2_ATOM__Chunk);
    h->atom_sequence = map_uri(h, LV2_ATOM__Sequence);
    h->min_frames = 1;
    h->max_frames = h->nominal_frames = (int32_t)h->max_block;
    h->options[0] = (LV2_Options_Option){LV2_OPTIONS_INSTANCE, 0, map_uri(h, LV2_BUF_SIZE__minBlockLength), sizeof(int32_t), integer, &h->min_frames};
    h->options[1] = (LV2_Options_Option){LV2_OPTIONS_INSTANCE, 0, map_uri(h, LV2_BUF_SIZE__maxBlockLength), sizeof(int32_t), integer, &h->max_frames};
    h->options[2] = (LV2_Options_Option){LV2_OPTIONS_INSTANCE, 0, map_uri(h, LV2_BUF_SIZE__nominalBlockLength), sizeof(int32_t), integer, &h->nominal_frames};
    h->options[3] = (LV2_Options_Option){LV2_OPTIONS_INSTANCE, 0, map_uri(h, LV2_BUF_SIZE__sequenceSize), sizeof(int32_t), integer, &h->sequence_size};
    h->options[4] = (LV2_Options_Option){LV2_OPTIONS_INSTANCE, 0, map_uri(h, LV2_PARAMETERS__sampleRate), sizeof(float), real, &h->rate};
    h->options[5] = (LV2_Options_Option){0, 0, 0, 0, 0, NULL};
    h->map_feature = (LV2_Feature){LV2_URID__map, &h->map};
    h->bounded_feature = (LV2_Feature){LV2_BUF_SIZE__boundedBlockLength, NULL};
    h->options_feature = (LV2_Feature){LV2_OPTIONS__options, h->options};
    h->features[0] = &h->map_feature;
    h->features[1] = &h->bounded_feature;
    h->features[2] = &h->options_feature;
    h->features[3] = NULL;
}

static bool supported_features(fx_lv2_host *h, const LilvPlugin *plugin,
                               char *error, size_t size) {
    LilvNodes *nodes = lilv_plugin_get_required_features(plugin);
    LILV_FOREACH(nodes, i, nodes) {
        const char *uri = lilv_node_as_uri(lilv_nodes_get(nodes, i));
        if (strcmp(uri, LV2_URID__map) && strcmp(uri, LV2_BUF_SIZE__boundedBlockLength) &&
            strcmp(uri, LV2_OPTIONS__options)) {
            errorf(error, size, "unsupported required LV2 feature: %s", uri);
            lilv_nodes_free(nodes);
            return false;
        }
    }
    lilv_nodes_free(nodes);
    (void)h;
    return true;
}

static bool determine_sequence_size(fx_lv2_host *h, const LilvPlugin *plugin,
                                    char *error, size_t size) {
    LilvNode *minimum_size = lilv_new_uri(h->world, LV2_RESIZE_PORT__minimumSize);
    int32_t result = 32768;
    if (!minimum_size) {
        errorf(error, size, "failed to allocate LV2 metadata node");
        return false;
    }
    for (uint32_t i = 0; i < lilv_plugin_get_num_ports(plugin); ++i) {
        LilvNodes *values = lilv_port_get_value(
            plugin, lilv_plugin_get_port_by_index(plugin, i), minimum_size);
        LILV_FOREACH(nodes, j, values) {
            const LilvNode *value = lilv_nodes_get(values, j);
            int64_t requested = lilv_node_is_int(value) ? lilv_node_as_int(value) : 0;
            if (requested > result && requested <= INT32_MAX) result = (int32_t)requested;
            if (requested > INT32_MAX) {
                errorf(error, size, "LV2 Atom port requires an unsupported buffer size");
                lilv_nodes_free(values);
                lilv_node_free(minimum_size);
                return false;
            }
        }
        lilv_nodes_free(values);
    }
    lilv_node_free(minimum_size);
    h->sequence_size = result;
    return true;
}

static int audio_channel(const char *symbol, bool input) {
    static const char *const inputs[2][4] = {
        {"in_l", "input_l", "lv2_audio_in_1", "left_in"},
        {"in_r", "input_r", "lv2_audio_in_2", "right_in"},
    };
    static const char *const outputs[2][4] = {
        {"out_l", "output_l", "lv2_audio_out_1", "left_out"},
        {"out_r", "output_r", "lv2_audio_out_2", "right_out"},
    };
    const char *const (*names)[4] = input ? inputs : outputs;
    for (int channel = 0; channel < 2; ++channel)
        for (size_t i = 0; i < 4; ++i)
            if (!strcmp(symbol, names[channel][i])) return channel;
    return -1;
}

static bool inspect_ports(fx_lv2_host *h, const LilvPlugin *plugin,
                          char *error, size_t size) {
    LilvNode *audio = lilv_new_uri(h->world, LV2_CORE__AudioPort);
    LilvNode *control = lilv_new_uri(h->world, LV2_CORE__ControlPort);
    LilvNode *atom = lilv_new_uri(h->world, LV2_ATOM__AtomPort);
    LilvNode *input = lilv_new_uri(h->world, LV2_CORE__InputPort);
    LilvNode *output = lilv_new_uri(h->world, LV2_CORE__OutputPort);
    LilvNode *designation = lilv_new_uri(h->world, LV2_CORE__designation);
    bool ok = audio && control && atom && input && output && designation;
    if (!ok) errorf(error, size, "failed to allocate LV2 metadata nodes");
    for (uint32_t i = 0; ok && i < h->port_count; ++i) {
        const LilvPort *port = lilv_plugin_get_port_by_index(plugin, i);
        const LilvNode *symbol = lilv_port_get_symbol(plugin, port);
        bool in = lilv_port_is_a(plugin, port, input);
        bool out = lilv_port_is_a(plugin, port, output);
        if (!symbol || in == out) {
            errorf(error, size, "LV2 port %u has invalid symbol or direction", i); ok = false; break;
        }
        {
            const char *text = lilv_node_as_string(symbol);
            size_t length = strlen(text) + 1;
            h->symbols[i] = malloc(length);
            if (h->symbols[i]) memcpy(h->symbols[i], text, length);
        }
        if (!h->symbols[i]) { errorf(error, size, "out of memory copying LV2 ports"); ok = false; break; }
        if (lilv_port_is_a(plugin, port, audio)) {
            int channel = audio_channel(h->symbols[i], in);
            h->types[i] = in ? FX_LV2_PORT_AUDIO_INPUT : FX_LV2_PORT_AUDIO_OUTPUT;
            if (channel >= 0) {
                uint32_t *ports = in ? h->audio_in : h->audio_out;
                if (ports[channel] != UINT32_MAX) {
                    errorf(error, size, "duplicate primary LV2 audio port: %s", h->symbols[i]);
                    ok = false;
                } else {
                    ports[channel] = i;
                }
            } else if (in && !strcmp(h->symbols[i], "sc_l")) {
                h->sidechain_in[0] = i;
                lilv_instance_connect_port(h->instance, i, h->zero_buffer);
            } else if (in && !strcmp(h->symbols[i], "sc_r")) {
                h->sidechain_in[1] = i;
                lilv_instance_connect_port(h->instance, i, h->zero_buffer);
            } else if (out) {
                h->audio_buffers[i] = calloc(h->max_block, sizeof(float));
                if (!h->audio_buffers[i]) {
                    errorf(error, size, "out of memory allocating LV2 audio port");
                    ok = false;
                } else {
                    lilv_instance_connect_port(h->instance, i, h->audio_buffers[i]);
                }
            } else {
                lilv_instance_connect_port(h->instance, i, h->zero_buffer);
            }
        } else if (lilv_port_is_a(plugin, port, control)) {
            LilvNode *def = NULL;
            h->types[i] = in ? FX_LV2_PORT_CONTROL_INPUT : FX_LV2_PORT_CONTROL_OUTPUT;
            lilv_port_get_range(plugin, port, &def, NULL, NULL);
            if (in && def) {
                if (lilv_node_is_float(def)) h->controls[i] = lilv_node_as_float(def);
                else if (lilv_node_is_int(def)) h->controls[i] = (float)lilv_node_as_int(def);
                else if (lilv_node_is_bool(def)) h->controls[i] = lilv_node_as_bool(def) ? 1.0f : 0.0f;
            }
            lilv_node_free(def);
            if (out) {
                LilvNodes *values = lilv_port_get_value(plugin, port, designation);
                LILV_FOREACH(nodes, j, values)
                    if (!strcmp(lilv_node_as_uri(lilv_nodes_get(values, j)), LV2_CORE__latency)) h->latency_port = i;
                lilv_nodes_free(values);
            }
            lilv_instance_connect_port(h->instance, i, &h->controls[i]);
        } else if (lilv_port_is_a(plugin, port, atom)) {
            LV2_Atom_Sequence *sequence = calloc(1, (size_t)h->sequence_size);
            if (!sequence) { errorf(error, size, "out of memory allocating LV2 Atom port"); ok = false; }
            else {
                sequence->atom.type = in ? h->atom_sequence : h->atom_chunk;
                sequence->atom.size = in ? sizeof(sequence->body) : (uint32_t)h->sequence_size - sizeof(LV2_Atom);
                h->atom_buffers[i] = sequence;
                h->atom_inputs[i] = in;
                lilv_instance_connect_port(h->instance, i, sequence);
            }
        } else { errorf(error, size, "unsupported LV2 port type: %s", h->symbols[i]); ok = false; }
    }
    if (ok && (h->audio_in[0] == UINT32_MAX || h->audio_in[1] == UINT32_MAX ||
               h->audio_out[0] == UINT32_MAX || h->audio_out[1] == UINT32_MAX)) {
        errorf(error, size, "LV2 plugin has no recognized primary stereo audio ports"); ok = false;
    }
    lilv_node_free(audio); lilv_node_free(control); lilv_node_free(atom); lilv_node_free(input);
    lilv_node_free(output); lilv_node_free(designation);
    return ok;
}

fx_lv2_host *fx_lv2_host_new(const char *plugin_uri, double rate, uint32_t max_block,
                             char *error, size_t size) {
    fx_lv2_host *h = NULL;
    LilvNode *uri = NULL;
    const LilvPlugin *plugin;
    if (!plugin_uri || rate <= 0.0 || !max_block || max_block > INT32_MAX) {
        errorf(error, size, "invalid LV2 host arguments"); return NULL;
    }
    h = calloc(1, sizeof(*h));
    if (!h) { errorf(error, size, "out of memory creating LV2 host"); return NULL; }
    h->latency_port = UINT32_MAX; h->max_block = max_block; h->rate = (float)rate;
    h->audio_in[0] = h->audio_in[1] = UINT32_MAX;
    h->audio_out[0] = h->audio_out[1] = UINT32_MAX;
    h->sidechain_in[0] = h->sidechain_in[1] = UINT32_MAX;
    h->world = lilv_world_new();
    if (!h->world) { errorf(error, size, "failed to create Lilv world"); goto fail; }
    lilv_world_load_all(h->world);
    uri = lilv_new_uri(h->world, plugin_uri);
    plugin = uri ? lilv_plugins_get_by_uri(lilv_world_get_all_plugins(h->world), uri) : NULL;
    if (!plugin) { errorf(error, size, "LV2 plugin not found: %s", plugin_uri); goto fail; }
    if (!supported_features(h, plugin, error, size)) goto fail;
    if (!determine_sequence_size(h, plugin, error, size)) goto fail;
    setup_features(h);
    h->instance = lilv_plugin_instantiate(plugin, rate, h->features);
    if (!h->instance) { errorf(error, size, "failed to instantiate LV2 plugin: %s", plugin_uri); goto fail; }
    h->port_count = lilv_plugin_get_num_ports(plugin);
    h->symbols = calloc(h->port_count, sizeof(*h->symbols));
    h->types = calloc(h->port_count, sizeof(*h->types));
    h->controls = calloc(h->port_count, sizeof(*h->controls));
    h->atom_buffers = calloc(h->port_count, sizeof(*h->atom_buffers));
    h->atom_inputs = calloc(h->port_count, sizeof(*h->atom_inputs));
    h->audio_buffers = calloc(h->port_count, sizeof(*h->audio_buffers));
    h->zero_buffer = calloc(h->max_block, sizeof(*h->zero_buffer));
    if (!h->symbols || !h->types || !h->controls || !h->atom_buffers || !h->atom_inputs ||
        !h->audio_buffers || !h->zero_buffer) { errorf(error, size, "out of memory allocating LV2 ports"); goto fail; }
    if (!inspect_ports(h, plugin, error, size)) goto fail;
    lilv_node_free(uri);
    if (error && size) error[0] = '\0';
    return h;
fail:
    lilv_node_free(uri); fx_lv2_host_free(h); return NULL;
}

void fx_lv2_host_free(fx_lv2_host *h) {
    if (!h) return;
    fx_lv2_host_deactivate(h); lilv_instance_free(h->instance);
    if (h->symbols) for (unsigned i = 0; i < h->port_count; ++i) free(h->symbols[i]);
    if (h->atom_buffers) for (unsigned i = 0; i < h->port_count; ++i) free(h->atom_buffers[i]);
    if (h->audio_buffers) for (unsigned i = 0; i < h->port_count; ++i) free(h->audio_buffers[i]);
    free(h->symbols); free(h->types); free(h->controls); free(h->atom_buffers); free(h->atom_inputs);
    free(h->audio_buffers); free(h->zero_buffer); lilv_world_free(h->world); free(h);
}
unsigned fx_lv2_host_port_count(const fx_lv2_host *h) { return h ? h->port_count : 0; }
const char *fx_lv2_host_port_symbol(const fx_lv2_host *h, unsigned i) { return h && i < h->port_count ? h->symbols[i] : NULL; }
fx_lv2_port_type fx_lv2_host_port_type_at(const fx_lv2_host *h, unsigned i) { return h && i < h->port_count ? h->types[i] : FX_LV2_PORT_UNKNOWN; }
bool fx_lv2_host_set_control(fx_lv2_host *h, const char *symbol, float value) {
    if (!h || !symbol) return false;
    for (unsigned i = 0; i < h->port_count; ++i) if (h->types[i] == FX_LV2_PORT_CONTROL_INPUT && !strcmp(h->symbols[i], symbol)) { h->controls[i] = value; return true; }
    return false;
}
void fx_lv2_host_activate(fx_lv2_host *h) { if (h && !h->active) { lilv_instance_activate(h->instance); h->active = true; } }
void fx_lv2_host_deactivate(fx_lv2_host *h) { if (h && h->active) { lilv_instance_deactivate(h->instance); h->active = false; } }

static void run_error(char *error, size_t size, const char *message) {
    size_t length;
    if (!error || !size) return;
    length = strlen(message);
    if (length >= size) length = size - 1;
    memcpy(error, message, length); error[length] = '\0';
}

bool fx_lv2_host_run(fx_lv2_host *h, const float *li, const float *ri,
                     float *lo, float *ro, uint32_t frames, char *error, size_t size) {
    if (!h || !li || !ri || !lo || !ro) { run_error(error, size, "invalid LV2 processing buffers"); return false; }
    if (!h->active) { run_error(error, size, "LV2 plugin is not active"); return false; }
    if (frames > h->max_block) { run_error(error, size, "LV2 block exceeds configured maximum"); return false; }
    for (unsigned i = 0; i < h->port_count; ++i) if (h->atom_buffers[i]) {
        LV2_Atom_Sequence *sequence = h->atom_buffers[i];
        sequence->atom.type = h->atom_inputs[i] ? h->atom_sequence : h->atom_chunk;
        sequence->atom.size = h->atom_inputs[i] ? sizeof(sequence->body) : (uint32_t)h->sequence_size - sizeof(LV2_Atom);
        sequence->body.unit = 0;
        sequence->body.pad = 0;
    }
    lilv_instance_connect_port(h->instance, h->audio_in[0], (void *)li);
    lilv_instance_connect_port(h->instance, h->audio_in[1], (void *)ri);
    if (h->sidechain_in[0] != UINT32_MAX)
        lilv_instance_connect_port(h->instance, h->sidechain_in[0], (void *)li);
    if (h->sidechain_in[1] != UINT32_MAX)
        lilv_instance_connect_port(h->instance, h->sidechain_in[1], (void *)ri);
    lilv_instance_connect_port(h->instance, h->audio_out[0], lo);
    lilv_instance_connect_port(h->instance, h->audio_out[1], ro);
    lilv_instance_run(h->instance, frames);
    if (error && size) error[0] = '\0';
    return true;
}
float fx_lv2_host_latency(const fx_lv2_host *h) { return h && h->latency_port != UINT32_MAX ? h->controls[h->latency_port] : 0.0f; }
