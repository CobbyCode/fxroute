#ifndef FXROUTE_LV2_HOST_H
#define FXROUTE_LV2_HOST_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

typedef struct fx_lv2_host fx_lv2_host;

typedef enum {
    FX_LV2_PORT_UNKNOWN = 0,
    FX_LV2_PORT_AUDIO_INPUT,
    FX_LV2_PORT_AUDIO_OUTPUT,
    FX_LV2_PORT_CONTROL_INPUT,
    FX_LV2_PORT_CONTROL_OUTPUT
} fx_lv2_port_type;

fx_lv2_host *fx_lv2_host_new(const char *uri, double rate, uint32_t max_block,
                             char *error, size_t error_size);
void fx_lv2_host_free(fx_lv2_host *host);
unsigned fx_lv2_host_port_count(const fx_lv2_host *host);
const char *fx_lv2_host_port_symbol(const fx_lv2_host *host, unsigned index);
fx_lv2_port_type fx_lv2_host_port_type_at(const fx_lv2_host *host, unsigned index);
bool fx_lv2_host_set_control(fx_lv2_host *host, const char *symbol, float value);
void fx_lv2_host_activate(fx_lv2_host *host);
void fx_lv2_host_deactivate(fx_lv2_host *host);
bool fx_lv2_host_run(fx_lv2_host *host, const float *left_in, const float *right_in,
                     float *left_out, float *right_out, uint32_t frames,
                     char *error, size_t error_size);
float fx_lv2_host_latency(const fx_lv2_host *host);

#endif
