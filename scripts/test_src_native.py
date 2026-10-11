#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Product worker PCM parity and policy reader (uses the built product library)."""
import array
import ctypes as C
import json
import math
import os
import re
import shutil
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
LIBRARY = ROOT / "native_src/build/libspa-fxroute-src.so"
F = C.POINTER(C.c_float)


@unittest.skipUnless(LIBRARY.is_file(), "build the common SRC runtime with native_src/build.py")
class NativeSrcTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.lib = C.CDLL(str(LIBRARY))
        cls.lib.fx_worker_create.argtypes = [C.c_int, C.c_uint, C.c_uint, C.c_uint, C.c_uint]
        cls.lib.fx_worker_create.restype = C.c_void_p
        for name in ("fx_worker_push", "fx_worker_pull"):
            getattr(cls.lib, name).argtypes = [C.c_void_p, C.POINTER(F), C.c_uint]
            getattr(cls.lib, name).restype = C.c_uint
        for name in ("fx_worker_finish", "fx_worker_finished", "fx_worker_error", "fx_worker_destroy"):
            getattr(cls.lib, name).argtypes = [C.c_void_p]

    def test_native_policy_reader_matches_central_modes_and_rejects_corruption(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"XDG_CONFIG_HOME": directory}):
            value = C.c_int(-1)
            self.assertEqual(self.lib.fx_src_load_profile(C.byref(value)), 0)
            self.assertEqual(value.value, 1)
            path = Path(directory) / "fxroute/src-policy.json"
            path.parent.mkdir()
            for mode, expected in (("low", 0), ("standard", 1), ("high", 2)):
                path.write_text(json.dumps({"mode": mode}))
                self.assertEqual(self.lib.fx_src_load_profile(C.byref(value)), 0)
                self.assertEqual(value.value, expected)
            for text in ('{"mode":"unknown"}', '[]', 'null', '{"mode":"high"}garbage', '{"mode":"high"', '{"mode":"high", "mode":"low"}', '{mode=high}', '{"mode":"high", "extra":"value"}'):
                path.write_text(text)
                self.assertLess(self.lib.fx_src_load_profile(C.byref(value)), 0, text)

    def test_pinned_sox_worker_retains_all_pcm_under_output_backpressure(self):
        samples = array.array("f", (0.02 * math.sin(2 * math.pi * (911 + channel * 271) * frame / 44100)
                                    for frame in range(44100) for channel in range(2)))
        source = [(C.c_float * 44100)(*samples[channel::2]) for channel in range(2)]
        pointers = (F * 2)(*[C.cast(p, F) for p in source])
        for profile, options in ((1, ["-M", "-v", "-s"]), (2, ["-E", "-z", "-a", "-B", "99.5", "-A", "99.5", "-f", "48000"])):
            reference = subprocess.run([str(ROOT / "native_src/build/sox"), "--buffer", "512", "-D", "-L", "-t", "raw",
                "-r", "44100", "-e", "floating-point", "-b", "32", "-c", "2", "-", "-t", "raw", "-r", "48000",
                "-e", "floating-point", "-b", "32", "-", "rate", *options], input=samples.tobytes(),
                env={k:v for k,v in os.environ.items() if k != "SOX_OPTS"}, capture_output=True, check=True).stdout
            worker = self.lib.fx_worker_create(profile, 2, 44100, 48000, 4096)
            self.assertTrue(worker)
            rendered = array.array("f")
            offset = 0
            output = [(C.c_float * 257)() for _ in range(2)]
            outputs = (F * 2)(*[C.cast(p, F) for p in output])
            deadline = time.monotonic() + 15
            eof = False
            try:
                while not self.lib.fx_worker_finished(worker):
                    self.assertLess(time.monotonic(), deadline, "Worker failed to drain")
                    self.assertEqual(self.lib.fx_worker_error(worker), 0)
                    if offset < 44100:
                        offered = (F * 2)(*[C.cast(C.byref(p, offset * 4), F) for p in source])
                        offset += self.lib.fx_worker_push(worker, offered, min(513, 44100 - offset))
                    elif not eof:
                        self.lib.fx_worker_finish(worker)
                        eof = True
                    made = self.lib.fx_worker_pull(worker, outputs, 257)
                    rendered.extend(output[c][i] for i in range(made) for c in range(2))
                    time.sleep(0.001)
                self.assertEqual(offset, 44100)
                self.assertEqual(rendered.tobytes(), reference)
            finally:
                self.lib.fx_worker_destroy(worker)

    def test_sox_opts_cannot_change_the_pinned_worker_profile(self):
        with patch.dict(os.environ, {"SOX_OPTS": "-v 0.5"}):
            self.test_pinned_sox_worker_retains_all_pcm_under_output_backpressure()

    def run_reset_lifecycle(self, scenario):
        build = ROOT / "native_src/build"
        includes = build / "sources/pipewire/pipewire-1.6.8/spa/include"
        upstream = includes.parent / "plugins/audioconvert"
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"XDG_CONFIG_HOME": directory}):
            path = Path(directory)
            policy = path / "fxroute/src-policy.json"
            policy.parent.mkdir()
            policy.write_text('{"mode":"standard"}')
            executable = path / "reset-lifecycle"
            subprocess.run(["cc", "-std=gnu11", "-I", str(includes), "-I", str(upstream),
                            "-I", str(ROOT / "native_src"), str(ROOT / "scripts/src_reset_lifecycle.c"),
                            "-ldl", "-o", str(executable)], check=True)
            library = LIBRARY
            environment = dict(os.environ)
            if scenario in {"relocated", "missing-engine", "activation-race"}:
                generation = path / "generation"
                generation.mkdir()
                if scenario != "missing-engine":
                    shutil.copy2(build / "sox", generation / "sox")
                shutil.copy2(LIBRARY, generation / LIBRARY.name)
                library = path / LIBRARY.name
                library.symlink_to(generation / LIBRARY.name)
                if scenario == "activation-race":
                    other = path / "next-generation"
                    other.mkdir()
                    shutil.copy2(build / "sox", other / "sox")
                    shutil.copy2(LIBRARY, other / LIBRARY.name)
                    shim = path / "activate.so"
                    subprocess.run(["cc", "-shared", "-fPIC", str(ROOT / "scripts/src_load_activation.c"),
                                    "-ldl", "-o", str(shim)], check=True)
                    environment.update(LD_PRELOAD=str(shim), FX_SRC_TEST_ALIAS=str(library),
                                       FX_SRC_TEST_NEXT=str(other / LIBRARY.name))
            result = subprocess.run([str(executable), str(library), scenario],
                                    text=True, capture_output=True, timeout=10, env=environment)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_reset_after_free_or_failed_init_is_safe_and_reinitializable(self):
        self.run_reset_lifecycle("reset")

    def test_relocated_plugin_uses_its_own_immutable_engine_generation(self):
        self.run_reset_lifecycle("relocated")

    def test_missing_engine_fails_initialization_without_crashing_on_flush(self):
        self.run_reset_lifecycle("missing-engine")

    def test_activation_between_mapping_and_constructor_keeps_the_loaded_engine(self):
        self.run_reset_lifecycle("activation-race")

    def test_peak_node_props_do_not_read_worker_composition_memory(self):
        includes = ROOT / "native_src/build/sources/pipewire/pipewire-1.6.8/spa/include"
        with tempfile.TemporaryDirectory() as directory:
            executable = str(Path(directory) / "peak-props")
            subprocess.run(["cc", "-std=gnu11", "-I", str(includes), str(ROOT / "scripts/src_peak_props.c"),
                            "-ldl", "-o", executable], check=True)
            subprocess.run([executable, str(LIBRARY)], check=True)
            observed=subprocess.run([executable, str(LIBRARY), "32"], check=True,capture_output=True,text=True)
            import re
            metrics=json.loads(re.search(r"FXSRC_WORKER_STATS (\{.*\})",observed.stderr)[1])
            self.assertEqual(metrics["accepted"],0)
            self.assertEqual(metrics["produced"],0)
            self.assertEqual(metrics["error"],0)
            self.assertEqual(metrics["rate_min"],1)
            self.assertEqual(metrics["rate_max"],1)

    def test_stalled_engine_survives_with_counted_silence(self):
        includes = ROOT / "native_src/build/sources/pipewire/pipewire-1.6.8/spa/include"
        upstream = includes.parent / "plugins/audioconvert"
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"XDG_CONFIG_HOME": directory}):
            path = Path(directory)
            policy = path / "fxroute/src-policy.json"
            policy.parent.mkdir()
            policy.write_text('{"mode":"high"}')
            executable = str(path / "stall-survival")
            subprocess.run(["cc", "-std=gnu11", "-I", str(includes), "-I", str(upstream),
                            "-I", str(ROOT / "native_src"), str(ROOT / "scripts/src_stall_survival.c"),
                            "-ldl", "-o", executable], check=True)
            result = subprocess.run([executable, str(LIBRARY)], text=True, capture_output=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("STALL-SURVIVAL-OK", result.stdout)

    def test_child_death_publishes_error_outside_the_realtime_callback(self):
        includes=ROOT / "native_src/build/sources/pipewire/pipewire-1.6.8/spa/include"
        with tempfile.TemporaryDirectory() as directory,patch.dict(os.environ,{"XDG_CONFIG_HOME":directory}):
            executable=str(Path(directory) / "failure-props")
            subprocess.run(["cc","-std=gnu11","-I",str(includes),str(ROOT / "scripts/src_failure_props.c"),
                            "-ldl","-lpthread","-o",executable],check=True)
            subprocess.run([executable,str(LIBRARY)],check=True,timeout=5)
            subprocess.run([executable,str(LIBRARY),"drain"],check=True,timeout=5)

    def run_stats_race(self,*scenario,timeout=10):
        build=ROOT / "native_src/build"
        includes=build / "sources/pipewire/pipewire-1.6.8/spa/include"
        upstream=includes.parent / "plugins/audioconvert"
        with tempfile.TemporaryDirectory() as directory,patch.dict(os.environ,{"XDG_CONFIG_HOME":directory}):
            path=Path(directory)
            probe=subprocess.run(["cc","-fsanitize=thread","-x","c","-","-o",str(path / "tsan-probe")],
                input="int main(void){return 0;}\n",text=True,capture_output=True)
            if probe.returncode:
                self.skipTest("Compiler ThreadSanitizer runtime is unavailable: "+probe.stderr.strip())
            flags=["cc","-std=gnu11","-O1","-g","-fsanitize=thread","-fPIC","-D_GNU_SOURCE","-DRESAMPLE_DISABLE_PRECOMP",
                   "-I",str(includes),"-I",str(upstream),"-I",str(ROOT / "native_src")]
            for name,source in (("composition",ROOT / "native_src/resample-worker.c"),("audioconvert",build / "audioconvert.c")):
                subprocess.run([*flags,"-c",str(source),"-o",str(path / (name+".o"))],check=True)
            objects=[str(p) for p in build.glob("*.o") if p.name not in ("resample-worker.o","audioconvert.o")]
            library=path / "race-src.so"
            shutil.copy2(build / "sox", path / "sox")
            subprocess.run(["cc","-fsanitize=thread","-shared",*objects,str(path / "composition.o"),str(path / "audioconvert.o"),
                            "-l:libsoxr.so.0","-lpthread","-lm","-o",str(library)],check=True)
            executable=path / "stats-race"
            subprocess.run([*flags,str(ROOT / "scripts/src_stats_race.c"),"-ldl","-lpthread","-o",str(executable)],check=True)
            result=subprocess.run([str(executable),str(library),*scenario],text=True,capture_output=True,timeout=timeout,
                env={**os.environ,"TSAN_OPTIONS":"halt_on_error=0:log_path="+str(path / "tsan")})
            reports="\n".join(p.read_text() for p in path.glob("tsan.*"))
            self.assertEqual(result.returncode,0,result.stdout+result.stderr+reports)
            self.assertNotIn("WARNING: ThreadSanitizer",reports)

    def test_error_snapshot_is_race_free_while_a_dead_worker_continues_eof_drain(self):
        self.run_stats_race()

    def test_startup_telemetry_snapshots_are_race_free_and_reset_per_generation(self):
        self.run_stats_race("startup",timeout=30)

    def test_channel_tables_stay_in_bounds_up_to_the_spa_channel_maximum(self):
        build=ROOT / "native_src/build"
        includes=build / "sources/pipewire/pipewire-1.6.8/spa/include"
        upstream=includes.parent / "plugins/audioconvert"
        with tempfile.TemporaryDirectory() as directory,patch.dict(os.environ,{"XDG_CONFIG_HOME":directory}):
            path=Path(directory)
            probe=subprocess.run(["cc","-fsanitize=address","-x","c","-","-o",str(path / "asan-probe")],
                input="int main(void){return 0;}\n",text=True,capture_output=True)
            if probe.returncode:
                self.skipTest("Compiler AddressSanitizer runtime is unavailable: "+probe.stderr.strip())
            flags=["cc","-std=gnu11","-O1","-g","-fsanitize=address","-fno-omit-frame-pointer","-fPIC","-D_GNU_SOURCE",
                   "-DRESAMPLE_DISABLE_PRECOMP","-I",str(includes),"-I",str(upstream),"-I",str(ROOT / "native_src")]
            subprocess.run([*flags,"-c",str(ROOT / "native_src/resample-worker.c"),"-o",str(path / "composition.o")],check=True)
            objects=[str(p) for p in build.glob("*.o") if p.name!="resample-worker.o"]
            library=path / "bounds-src.so"
            shutil.copy2(build / "sox", path / "sox")
            subprocess.run(["cc","-fsanitize=address","-shared",*objects,str(path / "composition.o"),
                            "-l:libsoxr.so.0","-lpthread","-lm","-o",str(library)],check=True)
            executable=path / "channel-bounds"
            subprocess.run([*flags,str(ROOT / "scripts/src_channel_bounds.c"),"-ldl","-lm","-o",str(executable)],check=True)
            result=subprocess.run([str(executable),str(library)],text=True,capture_output=True,timeout=20,
                env={**os.environ,"ASAN_OPTIONS":"detect_leaks=0"})
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            self.assertNotIn("AddressSanitizer",result.stderr)

    def test_high_18_channel_merge_retains_reserve_during_graph_clock_changes(self):
        includes=ROOT / "native_src/build/sources/pipewire/pipewire-1.6.8/spa/include"
        with tempfile.TemporaryDirectory() as directory,patch.dict(os.environ,{"XDG_CONFIG_HOME":directory}):
            policy=Path(directory) / "fxroute/src-policy.json";policy.parent.mkdir();policy.write_text('{"mode":"high"}')
            executable=str(Path(directory) / "clock-handoff")
            subprocess.run(["cc","-std=gnu11","-I",str(includes),str(ROOT / "scripts/src_clock_handoff.c"),
                            "-ldl","-o",executable],check=True)
            for scenario in ("2","2-1","1-2","2-1-2"):
                subprocess.run([executable,str(LIBRARY),scenario],check=True,timeout=12)

    def test_bluetooth_clock_feedback_excludes_fixed_src_startup_latency(self):
        spa = ROOT / "native_src/build/sources/pipewire/pipewire-1.6.8/spa"
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"XDG_CONFIG_HOME": directory}):
            path = Path(directory)
            policy = path / "fxroute/src-policy.json"
            policy.parent.mkdir()
            executable = path / "bluetooth-feedback"
            # Only the receive-socket helpers need BlueZ development headers.
            decode = (spa / "plugins/bluez5/decode-buffer.h").read_text()
            buffer_code = decode.split("struct spa_bt_recvmsg_data {", 1)[0]
            (path / "decode-buffer.h").write_text(
                buffer_code.replace("#include <bluetooth/bluetooth.h>\n", "") + "\n#endif\n")
            subprocess.run(["cc", "-std=gnu11", "-I", str(spa / "include"),
                            "-I", str(path),
                            "-I", str(spa / "plugins/bluez5"),
                            str(ROOT / "scripts/src_bluetooth_feedback.c"),
                            "-ldl", "-lm", "-o", str(executable)], check=True)
            for mode in ("low", "standard", "high"):
                policy.write_text(json.dumps({"mode": mode}))
                for rate in (44100, 48000, 96000):
                    with self.subTest(mode=mode, rate=rate):
                        result = subprocess.run([str(executable), str(LIBRARY), str(rate)],
                                                text=True, capture_output=True, timeout=12)
                        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_startup_waits_for_pcm_times_out_through_backpressure_and_never_reprimes(self):
        includes=ROOT / "native_src/build/sources/pipewire/pipewire-1.6.8/spa/include"
        upstream=includes.parent / "plugins/audioconvert"
        with tempfile.TemporaryDirectory() as directory,patch.dict(os.environ,{"XDG_CONFIG_HOME":directory}):
            path=Path(directory);source=path / "input.f32";reference=path / "reference.f32"
            source.write_bytes(array.array("f",(0.03*math.sin(2*math.pi*(697+300*c)*i/44100)
                                               for i in range(176400) for c in range(2))).tobytes())
            executable=path / "startup-ready"
            subprocess.run(["cc","-std=gnu11","-I",str(includes),"-I",str(upstream),"-I",str(ROOT / "native_src"),
                            str(ROOT / "scripts/src_startup_ready.c"),"-ldl","-lm","-o",str(executable)],check=True)
            subprocess.run([str(ROOT / "native_src/build/sox"),"--buffer","512","-D","-L","-t","raw","-r","44100",
                            "-e","floating-point","-b","32","-c","2",str(source),"-t","raw","-r","48000","-e","floating-point",
                            "-b","32",str(reference),"rate","-M","-v","-s"],check=True,capture_output=True,
                           env={k:v for k,v in os.environ.items() if k!="SOX_OPTS"})
            for scenario in ("resume","timeout-reset","post-start"):
                actual=path / (scenario+".f32")
                result=subprocess.run([str(executable),str(LIBRARY),scenario,str(source),str(actual)],
                                      check=True,text=True,capture_output=True,timeout=16)
                metrics=json.loads(re.search(r"STARTUP_READY (\{.*\})",result.stdout)[1])
                rendered=actual.read_bytes();prefix=metrics["lead"]*8
                self.assertFalse(any(rendered[:prefix]),scenario)
                if scenario!="post-start":self.assertEqual(rendered[prefix:],reference.read_bytes(),scenario)
                # Lifetime underruns count queue starvation only, never the startup timeout.
                # A post-start stall is concealed with counted silence, never terminal.
                lifetime=json.loads(re.search(r"FXSRC_WORKER_STATS (\{.*\})",result.stderr)[1])
                if scenario == "post-start":
                    self.assertGreaterEqual(lifetime["underruns"], 1, scenario)
                else:
                    self.assertEqual(lifetime["underruns"], 0, scenario)
                # The teardown diagnostics carry the same write-once startup telemetry.
                for key in ("lead","reserve","first_output_ns","first_real_ns"):
                    self.assertEqual(lifetime[key],metrics[key],(scenario,key))

    def test_reserve_conserves_nominal_input_progress_through_drift_eof_and_reset(self):
        includes=ROOT / "native_src/build/sources/pipewire/pipewire-1.6.8/spa/include"
        upstream=includes.parent / "plugins/audioconvert"
        with tempfile.TemporaryDirectory() as directory,patch.dict(os.environ,{"XDG_CONFIG_HOME":directory}):
            policy=Path(directory) / "fxroute/src-policy.json";policy.parent.mkdir();policy.write_text('{"mode":"high"}')
            executable=str(Path(directory) / "reserve")
            subprocess.run(["cc","-std=gnu11","-I",str(includes),"-I",str(upstream),"-I",str(ROOT / "native_src"),
                            str(ROOT / "scripts/src_reserve.c"),"-ldl","-lm","-o",executable],check=True)
            for scenario in ("2-1","1-2","2-1-2","drift"):
                subprocess.run([executable,str(LIBRARY),scenario],check=True,timeout=15)

    def test_nonzero_pcm_matches_the_approved_engine_and_native_matcher_across_clock_handoffs(self):
        build=ROOT / "native_src/build"
        includes=build / "sources/pipewire/pipewire-1.6.8/spa/include"
        upstream=includes.parent / "plugins/audioconvert"
        with tempfile.TemporaryDirectory() as directory,patch.dict(os.environ,{"XDG_CONFIG_HOME":directory}):
            path=Path(directory)
            policy=path / "fxroute/src-policy.json";policy.parent.mkdir();policy.write_text('{"mode":"high"}')
            flags=["cc","-std=gnu11","-O2","-fPIC","-I",str(includes),"-I",str(upstream),"-I",str(ROOT / "native_src"),
                   "-I",str(ROOT / "scripts/src_clock_oracle")]
            for name,source in (("oracle",ROOT / "scripts/src_clock_oracle.c"),("composition",ROOT / "native_src/resample-worker.c")):
                subprocess.run([*flags,"-DFX_SRC_VERIFY","-c",str(source),"-o",str(path / (name+".o"))],check=True)
            library=path / "clock-src.so"
            shutil.copy2(build / "sox", path / "sox")
            objects=[str(p) for p in build.glob("*.o") if p.name!="resample-worker.o"]
            subprocess.run(["cc","-shared","-Wl,-z,defs",*objects,str(path / "oracle.o"),str(path / "composition.o"),
                            "-l:libsoxr.so.0","-lpthread","-lm","-o",str(library)],check=True)
            executable=path / "clock-handoff"
            subprocess.run([*flags,str(ROOT / "scripts/src_clock_handoff.c"),"-ldl","-o",str(executable)],check=True)
            samples=array.array("f",(0.05*math.sin(2*math.pi*(997+37*c)*i/96000)
                                      for i in range(96000) for c in range(18)))
            source=path / "input.f32";source.write_bytes(samples.tobytes())
            reference=path / "nominal.f32"
            subprocess.run([str(build / "sox"),"--buffer","512","-D","-L","-t","raw","-r","96000","-e","floating-point",
                            "-b","32","-c","18",str(source),"-t","raw","-r","48000","-e","floating-point","-b","32",str(reference),
                            "rate","-E","-z","-a","-B","99.5","-A","99.5","-f","48000"],
                           env={k:v for k,v in os.environ.items() if k!="SOX_OPTS"},check=True,capture_output=True)
            for scenario in ("2","2-1","1-2","2-1-2"):
                actual=path / "actual.f32";expected=path / "matched.f32"
                result=subprocess.run([str(executable),str(library),scenario,str(source),str(actual)],
                    env={**os.environ,"FXROUTE_VERIFY_NOMINAL":str(reference),"FXROUTE_VERIFY_OUTPUT":str(expected)},
                    check=True,text=True,capture_output=True,timeout=12)
                metrics=json.loads(re.search(r"CLOCK_ORACLE (\{.*\})",result.stdout)[1])
                padding=metrics["lead_frames"]*18*4;rendered=actual.read_bytes()
                self.assertTrue(padding)
                self.assertFalse(any(rendered[:padding]))
                self.assertEqual(rendered[padding:],expected.read_bytes(),scenario)


if __name__ == "__main__":
    unittest.main()
