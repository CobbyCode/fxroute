#!/usr/bin/env python3
import subprocess
import tempfile
import shlex
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
NATIVE = ROOT / "native_dsp"


def test_atomic_mutes_and_peak_snapshots_work_offline(tmp_path):
    harness = tmp_path / "control_test.c"
    binary = tmp_path / "control_test"
    config = tmp_path / "dsp.conf"
    config.write_text(
        "rate 48000\n"
        "inputs 1\n"
        "outputs 3\n"
        "matrix 0 0 1\n"
        "matrix 1 0 0.5\n"
        "matrix 2 0 0.25\n"
    )
    harness.write_text(
        r'''
#include "dsp.h"
#include <math.h>
#include <stdint.h>

int main(int argc, char **argv) {
    char error[256];
    fxdsp *d = fxdsp_load(argv[1], error, sizeof error);
    float source[] = {1.0f, 0.5f};
    float a[2], b[2], c[2], tap_l[2], tap_r[2];
    const float *input[] = {source};
    float *output[] = {a, b, c};
    float peaks[FXDSP_MAX_CHANNELS] = {0};
    if (!d) return 1;
    fxdsp_set_mute(d, UINT32_C(0x5), 1);
    fxdsp_process(d, input, output, 1);
    fxdsp_set_mute(d, UINT32_C(0x1), 0);
    source[0] = source[1];
    output[0] = a + 1;
    output[1] = b + 1;
    output[2] = c + 1;
    fxdsp_process(d, input, output, 1);
    if (a[0] != 0 || b[0] != 0.5f || c[0] != 0) return 2;
    if (a[1] != 0.5f || b[1] != 0.25f || c[1] != 0) return 3;
    if (fxdsp_peaks(d, peaks, FXDSP_MAX_CHANNELS) != 3) return 4;
    if (fabsf(peaks[0] - 0.5f) > 1e-6f ||
        fabsf(peaks[1] - 0.5f) > 1e-6f || peaks[2] != 0) return 5;
    fxdsp_reset_peaks(d);
    if (fxdsp_peaks(d, peaks, FXDSP_MAX_CHANNELS) != 3) return 6;
    if (peaks[0] != 0 || peaks[1] != 0 || peaks[2] != 0) return 7;
    fxdsp_set_effect_bypass(d, 1);
    if (!fxdsp_effect_bypass(d)) return 8;
    fxdsp_set_output_gain_db(d, -6.020599913f);
    if (fabsf(fxdsp_output_gain_db(d) + 6.020599913f) > 1e-4f) return 9;
    source[0] = 1.0f;
    output[0] = a; output[1] = b; output[2] = c;
    float *tap[] = {tap_l, tap_r};
    fxdsp_process_tapped(d, input, output, tap, 1);
    if (fabsf(tap_l[0] - 1.0f) > 1e-6f || fabsf(a[0] - 0.5f) > 1e-5f) return 10;
    fxdsp_free(d);
    (void)argc;
    return 0;
}
'''
    )
    flags = shlex.split(subprocess.check_output(
        ["pkg-config", "--cflags", "--libs", "libebur128", "lilv-0", "samplerate",
         "speexdsp"], text=True))
    subprocess.run([
        "cc", "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror", "-pedantic",
        "-I", str(NATIVE), str(NATIVE / "dsp.c"), str(NATIVE / "autogain.c"),
        str(NATIVE / "crystalizer.c"), str(NATIVE / "lv2_host.c"), str(harness),
        *flags, "-lm", "-o", str(binary),
    ], check=True)
    subprocess.run([str(binary), str(config)], check=True)


def test_pipewire_engine_exposes_non_rt_datagram_control_protocol():
    source = (NATIVE / "pipewire_engine.c").read_text()
    assert "usage: %s CONFIG [CONTROL_SOCKET]" in source
    assert "SOCK_DGRAM" in source and "AF_UNIX" in source
    assert '"mute"' in source and '"peaks reset"' in source and '"peaks get"' in source
    assert '"effects bypass"' in source and '"gain db"' in source
    assert '"{\\"peaks\\":["' in source
    assert '"swap config' in source and "fxdsp_swap_config" in source
    assert '"live begin"' in source and '"live commit"' in source
    assert "pthread_create" in source and "pthread_join" in source


def test_native_config_swap_is_atomic_and_keeps_realtime_callback_free_of_loading():
    dsp = (NATIVE / "dsp.c").read_text()
    engine = (NATIVE / "pipewire_engine.c").read_text()
    assert "fxdsp_compatible" in dsp
    assert "apply_live_updates" in dsp
    live = dsp[dsp.index("static void apply_live_updates"):dsp.index("int fxdsp_live_commit")]
    assert "design(" not in live and "powf(" not in live and "llround(" not in live
    assert "atomic_exchange_explicit(&engine->dsp" in engine
    process = engine[engine.index("static void on_process"):engine.index("\n}", engine.index("static void on_process"))]
    assert "fxdsp_load" not in process
    assert "fxdsp_free" not in process


def test_pipewire_process_callback_has_no_non_rt_operations():
    source = (NATIVE / "pipewire_engine.c").read_text()
    start = source.index("static void on_process")
    body = source[start:source.index("\n}", start)]
    forbidden = (
        "malloc(", "calloc(", "realloc(", "free(", "fopen(", "read(", "write(",
        "recv", "send", "socket(", "printf(", "snprintf(", "pthread_", "mutex", "lock(",
    )
    assert all(call not in body for call in forbidden)


def test_pipewire_process_callback_silences_partial_port_cycles():
    source = (NATIVE / "pipewire_engine.c").read_text()
    start = source.index("static void on_process")
    body = source[start:source.index("\n}", start)]
    assert "if (!input[i]) complete = 0;" in body
    assert "if (!output[i]) complete = 0;" in body
    assert "memset(output[i], 0, frames * sizeof *output[i])" in body


def test_pipewire_engine_exposes_post_effect_pre_matrix_taps():
    source = (NATIVE / "pipewire_engine.c").read_text()
    assert '"post_effect_FL"' in source and '"post_effect_FR"' in source
    assert "fxdsp_process_tapped" in source


def test_post_effect_tap_stays_full_band_before_subwoofer_crossovers(tmp_path):
    harness = tmp_path / "tap_test.c"
    binary = tmp_path / "tap_test"
    harness.write_text(r'''
#include "dsp.h"
#include <math.h>
#include <stdio.h>

int main(int argc, char **argv) {
    char error[256];
    fxdsp *d = fxdsp_load(argv[1], error, sizeof error);
    float left[4096], right[4096], outputs[4][4096], taps[2][4096];
    const float *input[] = {left, right};
    float *output[] = {outputs[0], outputs[1], outputs[2], outputs[3]};
    float *tap[] = {taps[0], taps[1]};
    (void)argc;
    if (!d) { fprintf(stderr, "%s\n", error); return 1; }
    for (unsigned i=0;i<4096;i++) {
        left[i]=0.4f*sinf(2.0f*3.14159265358979323846f*40.0f*(float)i/48000.0f);
        right[i]=0.2f*sinf(2.0f*3.14159265358979323846f*40.0f*(float)i/48000.0f);
    }
    fxdsp_process_tapped(d,input,output,tap,4096);
    double tap_l=0,tap_r=0,main_l=0,main_r=0,sub_l=0,sub_r=0;
    for (unsigned i=1024;i<4096;i++) {
        tap_l+=taps[0][i]*taps[0][i]; tap_r+=taps[1][i]*taps[1][i];
        main_l+=outputs[0][i]*outputs[0][i]; main_r+=outputs[1][i]*outputs[1][i];
        if(fxdsp_outputs(d)>2){sub_l+=outputs[2][i]*outputs[2][i];sub_r+=outputs[3][i]*outputs[3][i];}
    }
    tap_l=sqrt(tap_l/3072);tap_r=sqrt(tap_r/3072);main_l=sqrt(main_l/3072);main_r=sqrt(main_r/3072);
    sub_l=sqrt(sub_l/3072);sub_r=sqrt(sub_r/3072);
    printf("tap_l=%g tap_r=%g main_l=%g main_r=%g sub_l=%g sub_r=%g\n",tap_l,tap_r,main_l,main_r,sub_l,sub_r);
    if(fabs(tap_l/tap_r-2.0)>0.01) return 2;
    if(fxdsp_outputs(d)==2) { if(fabs(main_l/tap_l-1.0)>0.01||fabs(main_r/tap_r-1.0)>0.01)return 3; }
    else { if(main_l>=tap_l*0.5||main_r>=tap_r*0.5||sub_l<=main_l||sub_r<=main_r)return 4; }
    fxdsp_free(d); return 0;
}
''')
    flags = shlex.split(subprocess.check_output(
        ["pkg-config", "--cflags", "--libs", "libebur128", "lilv-0", "samplerate",
         "speexdsp"], text=True))
    subprocess.run([
        "cc", "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror", "-pedantic",
        "-I", str(NATIVE), str(NATIVE / "dsp.c"), str(NATIVE / "autogain.c"),
        str(NATIVE / "crystalizer.c"), str(NATIVE / "lv2_host.c"), str(harness),
        *flags, "-lm", "-o", str(binary),
    ], check=True)
    configs = {
        "stereo": "rate 48000\ninputs 2\noutputs 2\nmatrix 0 0 1\nmatrix 1 1 1\n",
        "21": "rate 48000\ninputs 2\noutputs 4\nmatrix 0 0 1\nmatrix 1 1 1\nmatrix 2 0 .5\nmatrix 2 1 .5\nmatrix 3 0 .5\nmatrix 3 1 .5\npeq 0 highpass 80 .70710678 0\npeq 0 highpass 80 .70710678 0\npeq 1 highpass 80 .70710678 0\npeq 1 highpass 80 .70710678 0\npeq 2 lowpass 80 .70710678 0\npeq 2 lowpass 80 .70710678 0\npeq 3 lowpass 80 .70710678 0\npeq 3 lowpass 80 .70710678 0\n",
        "22": "rate 48000\ninputs 2\noutputs 4\nmatrix 0 0 1\nmatrix 1 1 1\nmatrix 2 0 1\nmatrix 3 1 1\npeq 0 highpass 80 .70710678 0\npeq 0 highpass 80 .70710678 0\npeq 1 highpass 80 .70710678 0\npeq 1 highpass 80 .70710678 0\npeq 2 lowpass 80 .70710678 0\npeq 2 lowpass 80 .70710678 0\npeq 3 lowpass 80 .70710678 0\npeq 3 lowpass 80 .70710678 0\n",
    }
    for name, text in configs.items():
        config = tmp_path / f"{name}.conf"
        config.write_text(text)
        subprocess.run([str(binary), str(config)], check=True)


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as directory:
        test_atomic_mutes_and_peak_snapshots_work_offline(Path(directory))
    test_pipewire_engine_exposes_non_rt_datagram_control_protocol()
    test_pipewire_process_callback_has_no_non_rt_operations()
    test_pipewire_process_callback_silences_partial_port_cycles()
    test_pipewire_engine_exposes_post_effect_pre_matrix_taps()
    with tempfile.TemporaryDirectory() as directory:
        test_post_effect_tap_stays_full_band_before_subwoofer_crossovers(Path(directory))
    print("native DSP control tests passed")
