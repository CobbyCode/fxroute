#include "lv2_host.h"

#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static const char *const candidates[] = {
    "http://lsp-plug.in/plugins/lv2/loud_comp_stereo",
    "http://lsp-plug.in/plugins/lv2/para_equalizer_x32_lr",
    "http://lsp-plug.in/plugins/lv2/sc_limiter_stereo",
    "http://zamaudio.com/lv2/ZaMaximX2",
    "urn:zamaudio:ZaMaximX2",
    "http://calf.sourceforge.net/plugins/BassEnhancer",
};

static int test_plugin(const char *uri, bool allow_missing) {
    char error[512];
    fx_lv2_host *host;
    float in_l[256] = {0.0f};
    float in_r[256] = {0.0f};
    float out_l[256] = {0.0f};
    float out_r[256] = {0.0f};

    host = fx_lv2_host_new(uri, 48000.0, 256, error, sizeof(error));
    if (!host) {
        if (allow_missing && strstr(error, "not found")) return 0;
        fprintf(stderr, "LV2 host creation failed: %s\n", error);
        return 1;
    }

    if (fx_lv2_host_port_count(host) < 5 || fx_lv2_host_set_control(host, "no_such_control", 1.0f)) {
        fprintf(stderr, "LV2 port inspection/control contract failed\n");
        fx_lv2_host_free(host);
        return 1;
    }
    in_l[0] = 0.25f;
    in_r[0] = -0.25f;
    fx_lv2_host_activate(host);
    if (!fx_lv2_host_run(host, in_l, in_r, out_l, out_r, 256, error, sizeof(error))) {
        fprintf(stderr, "LV2 run failed: %s\n", error);
        fx_lv2_host_free(host);
        return 1;
    }
    for (size_t i = 0; i < 256; ++i) {
        if (!isfinite(out_l[i]) || !isfinite(out_r[i])) {
            fprintf(stderr, "LV2 plugin produced non-finite audio\n");
            fx_lv2_host_free(host);
            return 1;
        }
    }
    printf("LV2 host test passed: %s (%u ports, latency %.0f frames)\n", uri,
           fx_lv2_host_port_count(host), fx_lv2_host_latency(host));
    fx_lv2_host_free(host);
    return 0;
}

int main(void) {
    const char *uri = getenv("FXROUTE_TEST_LV2_URI");
    int failed = 0;

    if (uri) return test_plugin(uri, false);
    for (size_t i = 0; i < sizeof(candidates) / sizeof(candidates[0]); ++i)
        failed |= test_plugin(candidates[i], true);
    return failed;
}
