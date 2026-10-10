#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Build the common factory from checksum-pinned SPA and CPU-only SoX sources."""

import argparse
import difflib
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tarfile
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parent
PW_SHA = "8181172a1d95131f6af8bbc0b98f90b2a33349b042b84c3ce57dd5d11348cc58"
SOX_SHA = "40858f399e119b3c0213f8e6c026aa4862321e6a2c14cac705483bebaabd1157"
PINS = {
    "pipewire": ("https://codeload.github.com/PipeWire/pipewire/tar.gz/refs/tags/1.6.8", PW_SHA),
    "sox": ("https://codeload.github.com/simonefil/sox-vulkan/tar.gz/refs/tags/sox-16.2.1", SOX_SHA),
}


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def activate_runtime(generation, build):
    for name in ("sox", "libspa-fxroute-src.so", "manifest.json"):
        if not (generation / name).is_file():
            raise ValueError(f"Incomplete SRC generation: {name}")
    temporary = build / (".current-" + uuid.uuid4().hex)
    temporary.symlink_to(generation.name)
    os.replace(temporary, build / "current")
    for name in ("sox", "libspa-fxroute-src.so", "manifest.json"):
        link = build / (".link-" + uuid.uuid4().hex)
        link.symlink_to("current/" + name)
        os.replace(link, build / name)


def sources(kind, archive, build):
    url, checksum = PINS[kind]
    path = Path(archive) if archive else build / f"{kind}.tar.gz"
    if not path.is_file():
        with urllib.request.urlopen(url, timeout=60) as response:
            path.write_bytes(response.read())
    if sha(path) != checksum:
        raise ValueError(f"{kind} source archive checksum mismatch")
    destination = build / "sources" / kind
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(path) as archive_file:
        top = archive_file.getmembers()[0].name
        archive_file.extractall(destination, filter="data")
    return destination / top


def overlay(original):
    text = original

    def replace(old, new):
        nonlocal text
        if text.count(old) != 1:
            raise ValueError(f"Pinned SPA contract changed: {old[:70]}")
        text = text.replace(old, new)

    replace("resample_native_init(&this->resample)", "fx_resample_init(&this->resample)")
    replace("\tstruct spa_plugin_loader *loader;",
            "\tstruct spa_plugin_loader *loader;\n\tstruct spa_loop_utils *src_loop_utils;\n"
            "\tstruct spa_source *src_error_event;\n\t_Atomic int src_seen_error;")
    replace("static void emit_port_info(struct impl *this, struct port *port, bool full)",
            "static void src_error_event(void *data, uint64_t count)\n{\n"
            "\tstruct impl *this = data;\n"
            "\tif (!this->resample_peaks && this->resample.data && fx_resample_error(&this->resample)) {\n"
            "\t\tstruct fx_resample_stats s;\n\t\tfx_resample_stats(&this->resample, &s);\n"
            "\t\tfprintf(stderr, \"FXSRC_ERROR_STATS {\\\"error\\\":%d,\\\"channels\\\":%u,\\\"input_rate\\\":%u,\\\"output_rate\\\":%u,\"\n"
            "\t\t\t\"\\\"accepted\\\":%lu,\\\"produced\\\":%lu,\\\"delivered\\\":%lu,\\\"max_input\\\":%lu,\\\"max_output\\\":%lu,\"\n"
            "\t\t\t\"\\\"lead\\\":%lu,\\\"reserve\\\":%lu,\\\"real\\\":%lu,\\\"buffered\\\":%u,\"\n"
            "\t\t\t\"\\\"first_output_ns\\\":%lu,\\\"first_real_ns\\\":%lu}\\n\",\n"
            "\t\t\tfx_resample_error(&this->resample), this->resample.channels, this->resample.i_rate, this->resample.o_rate,\n"
            "\t\t\ts.worker.accepted, s.worker.produced, s.worker.delivered, s.worker.max_input, s.worker.max_output,\n"
            "\t\t\ts.lead_frames, s.reserve_frames, s.real_frames, s.buffered, s.worker.first_output_ns, s.first_real_ns);\n\t}\n"
            "\tthis->params[IDX_Props].user++;\n"
            "\tthis->info.change_mask |= SPA_NODE_CHANGE_MASK_PARAMS;\n\temit_node_info(this, false);\n}\n\n"
            "static void emit_port_info(struct impl *this, struct port *port, bool full)")
    replace("\tthis->data_loop = spa_support_find(support, n_support, SPA_TYPE_INTERFACE_DataLoop);",
            "\tthis->data_loop = spa_support_find(support, n_support, SPA_TYPE_INTERFACE_DataLoop);\n"
            "\tthis->src_loop_utils = spa_support_find(support, n_support, SPA_TYPE_INTERFACE_LoopUtils);\n"
            "\tatomic_init(&this->src_seen_error, 0);")
    replace("\tthis->props.filter_graph_disabled = filter_graph_disabled;\n\n\treturn 0;\n}",
            "\tthis->props.filter_graph_disabled = filter_graph_disabled;\n"
            "\tif (this->src_loop_utils) {\n"
            "\t\tthis->src_error_event = spa_loop_utils_add_event(this->src_loop_utils, src_error_event, this);\n"
            "\t\tif (!this->src_error_event) return -ENOMEM;\n\t}\n\n\treturn 0;\n}")
    replace("\tfree_dir(&this->dir[SPA_DIRECTION_INPUT]);",
            "\tif (this->src_error_event)\n\t\tspa_loop_utils_destroy_source(this->src_loop_utils, this->src_error_event);\n"
            "\tfree_dir(&this->dir[SPA_DIRECTION_INPUT]);")
    replace("\tuint8_t buffer[4096];\n\tstruct spa_result_node_params result;\n\tuint32_t count = 0;\n\tint res = 0;",
            "\tuint8_t buffer[8192];\n\tstruct spa_result_node_params result;\n\tuint32_t count = 0;\n\tint res = 0;")
    replace("fdelay = resample_delay(&this->resample) + resample_phase(&this->resample);",
            "fdelay = this->resample_peaks ? (double)resample_delay(&this->resample) + resample_phase(&this->resample) :\n"
            "\t\t\tfx_resample_match_delay(&this->resample);")
    replace("\t\tn_samples = SPA_MIN(n_samples, quant_samples);\n\t\tflush_in = true;",
            "\t\tn_samples = SPA_MIN(n_samples, quant_samples);\n\t\tflush_in = false;")
    replace("\tbool empty;\n};", "\tbool empty;\n\tbool end_of_input;\n};")
    replace("\tctx.empty = in_empty;", "\tctx.empty = in_empty;\n\tctx.end_of_input = draining;")
    replace("\t\tmax_out = quant_samples;\n\t\tif (!in_avail || this->drained) {",
            "\t\tmax_out = quant_samples;\n"
            "\t\tif (!in_avail && !this->drained && !this->resample_peaks && !resample_passthrough &&\n"
            "\t\t    resample_in_len(&this->resample, max_out - SPA_MIN(max_out, this->out_offset)) == 0) {\n"
            "\t\t\tin_avail = true;\n\t\t\tmax_in = 0;\n\t\t}\n"
            "\t\tif (!in_avail || this->drained) {")
    replace("\tuint32_t in_len = c->n_samples;", "\tuint32_t in_len = c->end_of_input ? 0 : c->n_samples;")
    replace("\tresample_process(&impl->resample, (const void**)c->datas[s->in_idx], &in_len,",
            "\tif (c->end_of_input && !impl->resample_peaks)\n\t\tfx_resample_finish(&impl->resample);\n"
            "\tresample_process(&impl->resample, (const void**)c->datas[s->in_idx], &in_len,")
    replace("\tc->in_samples = in_len;\n\tc->n_samples = out_len;",
            "\tc->in_samples = in_len;\n\tc->n_samples = out_len;\n"
            "\tif (out_len && !impl->resample_peaks)\n\t\tc->empty = false;")
    replace("\tthis->in_offset += ctx.in_samples;",
            "\tif (!this->resample_peaks && !resample_passthrough && fx_resample_error(&this->resample))\n"
            "\t\treturn -fx_resample_error(&this->resample);\n\tthis->in_offset += ctx.in_samples;")
    replace("(this->out_offset >= max_out || flush_out)", "(this->out_offset >= max_out || flush_out || draining)")
    replace("\t\tthis->drained = draining;",
            "\t\tthis->drained = draining && (this->resample_peaks || resample_passthrough || fx_resample_finished(&this->resample));")
    replace("\telse if (n_samples == 0 && this->resample_peaks) {",
            "\telse if (draining && !this->resample_peaks && !resample_passthrough && fx_resample_finished(&this->resample)) {\n"
            "\t\tthis->drained = true;\n\t\treturn SPA_STATUS_DRAINED;\n\t}\n"
            "\telse if (n_samples == 0 && this->resample_peaks) {")
    replace("\treturn res;\n}\n\nstatic int calc_width",
            "\tif (res >= 0 && !this->resample_peaks) {\n"
            "\t\tthis->latency.ns = this->resample.i_rate != this->resample.o_rate ? 750000000 : 0;\n"
            "\t\trecalc_latencies(this, SPA_DIRECTION_INPUT);\n\t\trecalc_latencies(this, SPA_DIRECTION_OUTPUT);\n"
            "\t}\n"
            "\tif (res >= 0) {\n\t\tatomic_store(&this->src_seen_error, 0);\n\t\tthis->params[IDX_Props].user++;\n"
            "\t\tthis->info.change_mask |= SPA_NODE_CHANGE_MASK_PARAMS;\n\t\temit_node_info(this, false);\n\t}\n"
            "\treturn res;\n}\n\nstatic int calc_width")
    replace("static int impl_node_process(void *object)", "static int impl_node_process_core(void *object)")
    replace("static const struct spa_node_methods impl_node = {",
            "static int impl_node_process(void *object)\n{\n\tstruct impl *this = object;\n"
            "\tuint64_t start = get_time_ns(this);\n\tint result = impl_node_process_core(object);\n"
             "\tif (!this->resample_peaks && this->resample.data)\n"
             "\t\tfx_resample_note_callback(&this->resample, get_time_ns(this) - start);\n"
             "\tif (result < 0 && !this->resample_peaks && this->resample.data && this->src_error_event) {\n"
             "\t\tint error = fx_resample_error(&this->resample);\n"
             "\t\tif (error && atomic_exchange(&this->src_seen_error, error) != error &&\n"
             "\t\t    spa_loop_utils_signal_event(this->src_loop_utils, this->src_error_event) < 0)\n"
             "\t\t\tatomic_store(&this->src_seen_error, 0);\n\t}\n"
             "\treturn result;\n}\n\nstatic const struct spa_node_methods impl_node = {")
    replace('\t\tspa_pod_builder_string(b, "resample.quality");',
            '\t\tspa_pod_builder_string(b, "fxroute.src.mode");\n'
            '\t\tspa_pod_builder_string(b, this->resample_peaks ? "peaks" : fx_resample_profile(&this->resample));\n'
            '\t\tspa_pod_builder_string(b, "fxroute.src.generation");\n'
            '\t\tspa_pod_builder_string(b, FXROUTE_SRC_BUILD_ID);\n'
            '\t\tspa_pod_builder_string(b, "fxroute.src.input-rate");\n'
            '\t\tspa_pod_builder_int(b, this->resample.i_rate);\n'
            '\t\tspa_pod_builder_string(b, "fxroute.src.output-rate");\n'
            '\t\tspa_pod_builder_int(b, this->resample.o_rate);\n'
            '\t\tspa_pod_builder_string(b, "fxroute.src.nominal-bypass");\n'
            '\t\tspa_pod_builder_bool(b, this->resample.i_rate == this->resample.o_rate);\n'
            '\t\tspa_pod_builder_string(b, "fxroute.src.error");\n'
            '\t\tspa_pod_builder_int(b, !this->resample_peaks && this->resample.data ? fx_resample_error(&this->resample) : 0);\n'
            '\t\tspa_pod_builder_string(b, "resample.quality");')
    return '#include <stdatomic.h>\n#include "resample-worker.h"\n#ifndef FXROUTE_SRC_BUILD_ID\n#define FXROUTE_SRC_BUILD_ID "unversioned"\n#endif\n' + text


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pipewire-archive")
    parser.add_argument("--sox-archive")
    parser.add_argument("--sox-build", type=Path, help="Reuse the already validated CPU-only CMake build")
    parser.add_argument("--soxr-include", type=Path)
    args = parser.parse_args()
    build = ROOT / "build"
    build.mkdir(exist_ok=True)
    generation_id = uuid.uuid4().hex
    generation = build / ("runtime-" + generation_id)
    generation.mkdir()
    pw = sources("pipewire", args.pipewire_archive, build)
    sox_source = sources("sox", args.sox_archive, build)
    sox_build = args.sox_build or build / "sox-build"
    if args.sox_build:
        cache = (sox_build / "CMakeCache.txt").read_text()
        for setting in ("WITH_VULKAN:BOOL=OFF", "WITH_OPENMP:BOOL=OFF", "BUILD_SHARED_LIBS:BOOL=OFF"):
            if setting not in cache:
                raise ValueError(f"Unapproved engine build: {setting} required")
    else:
        subprocess.run(["cmake", "-S", str(sox_source), "-B", str(sox_build), "-DCMAKE_BUILD_TYPE=Release",
                        "-DWITH_VULKAN=OFF", "-DWITH_OPENMP=OFF", "-DBUILD_SHARED_LIBS=OFF",
                        "-DWITH_ALSA=OFF", "-DWITH_AO=OFF", "-DWITH_OSS=OFF"], check=True)
        subprocess.run(["cmake", "--build", str(sox_build), "-j", "4"], check=True)
    binary = sox_build / "src/sox"
    version = subprocess.check_output([str(binary), "--version"], text=True)
    if "SoX v16.2.1" not in version:
        raise ValueError("Pinned SoX 16.2.1 required")
    shutil.copy2(binary, generation / "sox")
    upstream = pw / "spa/plugins/audioconvert"
    original = (upstream / "audioconvert.c").read_text()
    text = overlay(original)
    (build / "audioconvert.c").write_text(text)
    (build / "converter-overlay.diff").write_text("".join(difflib.unified_diff(
        original.splitlines(True), text.splitlines(True), fromfile="upstream/audioconvert.c", tofile="fxroute/audioconvert.c")))
    names = ["fmt-ops.c", "fmt-ops-c.c", "channelmix-ops.c", "channelmix-ops-c.c", "biquad.c", "crossover.c",
             "peaks-ops.c", "peaks-ops-c.c", "resample-native.c", "resample-native-c.c", "resample-peaks.c",
             "volume-ops.c", "volume-ops-c.c", "wavfile.c"]
    inputs = [build / "audioconvert.c", *[upstream / n for n in names],
              *[ROOT / n for n in ("worker.c", "resample-worker.c", "policy.c", "plugin.c")]]
    flags = ["-I", str(args.soxr_include)] if args.soxr_include else shlex.split(
        subprocess.check_output(["pkg-config", "--cflags", "soxr"], text=True))
    objects = []
    for source in inputs:
        obj = build / (source.stem + ".o")
        subprocess.run([os.environ.get("CC", "cc"), "-std=gnu11", "-O2", "-g", "-fPIC", "-D_GNU_SOURCE",
                         "-DRESAMPLE_DISABLE_PRECOMP",
                        f'-DFXROUTE_SRC_BUILD_ID="{generation_id}"',
                        "-I", str(ROOT), "-I", str(upstream), "-I", str(pw / "spa/include"), *flags,
                        "-c", str(source), "-o", str(obj)], check=True)
        objects.append(str(obj))
    library = generation / "libspa-fxroute-src.so"
    subprocess.run([os.environ.get("CC", "cc"), "-shared", "-Wl,-z,defs", *objects,
                    "-l:libsoxr.so.0", "-ldl", "-lpthread", "-lm", "-o", str(library)], check=True)
    manifest = {"generation": generation_id, "sox_version": "16.2.1", "sox_source_sha256": SOX_SHA, "pipewire_source_sha256": PW_SHA,
                "sox_binary_sha256": sha(generation / "sox"), "converter_sha256": sha(library),
                "vulkan": False, "openmp": False, "startup_reserve_ms": 750, "fifo_frames": 65536,
                "standard_args": ["-M", "-v", "-s"], "high_args": ["-E", "-z", "-a", "-B", "99.5", "-A", "99.5", "-f"]}
    (generation / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    activate_runtime(generation, build)
    print(build / "libspa-fxroute-src.so")


if __name__ == "__main__":
    main()
