#!/usr/bin/env python3
"""Positive P1-3 regression: EasyEffects persistence concurrency.

Every production operation that mutates agent-output-extras.json or an
EasyEffects output preset must run fully serialized under the central
_asyeffects_mutation_lock, with the mutation-relevant read happening after
ownership was acquired.  JSON writes are atomic (temp file + os.replace).

These tests exercise the real production entrypoints (main._set_canonical_
output_volume, main.save_easyeffects_extras, main._load_easyeffects_preset
and the coordinator runtime-reapply pattern) against a real
EasyEffectsManager on temp directories.  No EasyEffects/Flatpak/gsettings/
socket calls are made: set_active_plugin_property and get_active_preset are
stubbed, load_preset's socket fallback is stubbed where it is reached.

Invariants asserted:

  1. Two concurrent writers changing different fields (AutoGain vs Loudness
     volume) both land in the final canonical state; the second writer reads
     its base state only after acquiring the mutation ownership.
  2. While writer A holds the mutation ownership, writer B waits: the
     persistence RMW never runs concurrently.
  3. Cancelling a worker caller releases the mutation ownership only after
     the worker actually finished.
  4. Readers during an atomic extras/preset write observe only the old or
     the new complete JSON, never defaults from parse errors / RuntimeError
     from partial presets.
  5. The event loop stays responsive while a slow manager worker holds the
     mutation ownership; no threading.Lock is waited on synchronously.
  6. Canonical volume write, extras save, preset load and coordinator
     runtime reapply run in parallel without deadlock and leave the global
     extras and the active preset mutually consistent.
"""

import asyncio
import copy
import json
import pathlib
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import easyeffects_persistence
import main
from easyeffects import EasyEffectsManager, EasyEffectsRuntime
from easyeffects_persistence import EasyEffectsPresetStore
from pathlib import Path

ACTIVE_PRESET = "Active Preset"


class WriteBarrier:
    """Block the first write_preset call; count concurrent entries."""

    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls = 0
        self.concurrent = 0

    def wrap(self, store):
        real = EasyEffectsPresetStore.write_preset.__get__(store, type(store))

        def wrapped(preset_name, payload):
            self.calls += 1
            self.concurrent += 1
            if not self.entered.is_set():
                self.entered.set()
                if not self.release.wait(timeout=10):
                    raise RuntimeError("write barrier timed out")
            try:
                return real(preset_name, payload)
            finally:
                self.concurrent -= 1

        store.write_preset = wrapped


class WriteCounter:
    """Count write_preset calls without blocking (no barrier)."""

    def __init__(self):
        self.calls = 0

    def wrap(self, store):
        real = EasyEffectsPresetStore.write_preset.__get__(store, type(store))

        def wrapped(preset_name, payload):
            self.calls += 1
            return real(preset_name, payload)

        store.write_preset = wrapped


class FakeExtrasRequest:
    def __init__(self, body):
        self._body = body

    async def json(self):
        return self._body


class EasyEffectsPersistenceConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = pathlib.Path(self._tmp.name)
        self.manager = self._make_manager(self.base)
        self.seed_extras = copy.deepcopy(self.manager.load_global_extras())
        self._barriers = []
        main.easyeffects_manager = self.manager
        main.easyeffects_mutation_lock = None
        main.canonical_volume_write_lock = None
        main.easyeeffects_preset_load_lock = None

    async def asyncTearDown(self):
        for barrier in self._barriers:
            barrier.release.set()
        await asyncio.sleep(0.3)
        main.easyeffects_manager = None
        main.easyeffects_mutation_lock = None
        main.canonical_volume_write_lock = None
        self._tmp.cleanup()

    def _make_manager(self, base_dir):
        runtime = EasyEffectsRuntime(
            mode="native",
            socket_path=base_dir / "sock",
            socket_candidates=[base_dir / "sock"],
            output_dir=base_dir / "config/easyeffects/output",
            irs_dir=base_dir / "config/easyeffects/irs",
            db_file=base_dir / "config/easyeffects/db/easyeffectsrc",
            global_extras_file=base_dir / "config/easyeffects/agent-output-extras.json",
            compare_state_file=base_dir / "config/easyeffects/agent-compare-state.json",
            cli_command=["easyeffects"],
            native_available=False,
            flatpak_available=False,
        )
        with mock.patch.object(EasyEffectsManager, "_detect_runtime", return_value=runtime):
            manager = EasyEffectsManager(home=base_dir / "home")
        manager.set_active_plugin_property = lambda *args, **kwargs: None
        manager.get_active_preset = lambda: ACTIVE_PRESET
        manager.preset_store.write_preset(
            ACTIVE_PRESET,
            {
                "output": {
                    "plugins_order": ["loudness", "autogain"],
                    "loudness": {"bypass": False, "volume": -20.0, "output-gain": 0.0},
                    "autogain": {"bypass": False, "target": -12.0},
                }
            },
        )
        seed = manager.normalize_effects_extras(None)
        seed["loudness"]["enabled"] = True
        seed["loudness"]["params"]["volumeDb"] = -20.0
        seed["autogain"]["enabled"] = True
        seed["autogain"]["params"]["targetDb"] = -12.0
        manager.save_global_extras(seed)
        return manager

    def _seed_extras(self):
        return self.manager.normalize_effects_extras(self.seed_extras)

    def _preset_payload(self):
        return json.loads(
            (self.manager.output_dir / f"{ACTIVE_PRESET}.json").read_text()
        )

    def _extras_payload(self):
        return json.loads(self.manager.global_extras_file.read_text())

    def _loudness_volume_for(self, extras):
        normalized = self.manager.normalize_effects_extras(extras)
        return float(
            self.manager._loudness_plugin_payload(
                normalized["loudness"], normalized["autogain"]
            )["volume"]
        )

    def _autogain_target_for(self, extras):
        normalized = self.manager.normalize_effects_extras(extras)
        return float(
            self.manager._autogain_plugin_payload(normalized["autogain"])["target"]
        )

    async def _coordinator_reapply(self):
        """Exact production pattern of stabilize_effects_after_rate_change /
        the commit drift recovery (main.py): acquire the mutation ownership,
        re-read the canonical extras under it, run the blocking manager call
        through the cancellation-safe worker."""
        async with main._easyeffects_mutation_lock():
            extras = self.manager.load_global_extras()
            await main._drain_worker(
                self.manager.apply_autogain_loudness_runtime,
                extras,
                extras,
                persist_all_presets=False,
            )

    async def _coordinator_reapply_with_validation(self, validate_hook=None, *, settle=True):
        """Production-shaped stabilize/commit-recovery sequence: the mutation
        ownership is held from the canonical read through the apply, the
        settle and the runtime readback/validation, exactly like
        stabilize_effects_after_rate_change() and the _verify_transition()
        drift recovery.  ``validate_hook`` models the readback phase."""
        async with main._easyeffects_mutation_lock():
            extras = self.manager.load_global_extras()
            await main._drain_worker(
                self.manager.apply_autogain_loudness_runtime,
                extras,
                extras,
                persist_all_presets=False,
            )
            if settle:
                settle_seconds = float(
                    getattr(self.manager, "LOUDNESS_STRENGTH_VOLUME_SETTLE_SECONDS", 0.0)
                )
                if settle_seconds > 0:
                    await asyncio.sleep(settle_seconds)
            if validate_hook is not None:
                await validate_hook()
            validated_extras = self.manager.load_global_extras()
        return validated_extras

    async def _assert_volume_writer_waits_during_coordinator_validation(self, coordinator_factory):
        counter = WriteCounter()
        counter.wrap(self.manager.preset_store)
        validate_entered = threading.Event()
        release_validate = threading.Event()

        async def validation_phase():
            validate_entered.set()
            if not await asyncio.to_thread(release_validate.wait, 10):
                raise RuntimeError("validation barrier timed out")

        coordinator_task = asyncio.create_task(coordinator_factory(validation_phase))
        self.assertTrue(await asyncio.to_thread(validate_entered.wait, 10))

        # The coordinator is inside its runtime readback/validation: the
        # ownership is still held, only the coordinator's own write happened,
        # and the persisted state is still the applied snapshot (no parallel
        # mutation interleaved between apply and verify, so no false mismatch
        # can be produced).
        self.assertTrue(main._easyeffects_mutation_lock().locked())
        self.assertEqual(counter.calls, 1)
        self.assertAlmostEqual(
            float(self._extras_payload()["loudness"]["params"]["volumeDb"]),
            -20.0,
            places=6,
        )

        # A parallel canonical volume writer must wait at the mutation
        # ownership until the coordinator's validation completed.
        volume_task = asyncio.create_task(main._set_canonical_output_volume(60))
        try:
            await asyncio.wait_for(self._until_write_calls(counter, 2), timeout=2.0)
        except asyncio.TimeoutError:
            pass
        else:
            self.fail(
                "volume writer reached the persistence RMW during the "
                "coordinator runtime validation"
            )
        self.assertFalse(volume_task.done())

        release_validate.set()
        validated = await asyncio.gather(coordinator_task, volume_task)
        # The coordinator validated against the extras it applied, never
        # against a state mixed with the volume writer's later change.
        self.assertAlmostEqual(
            float(validated[0]["loudness"]["params"]["volumeDb"]), -20.0, places=6
        )

    async def _assert_final_state_has_both_changes(self):
        extras = self._extras_payload()
        preset = self._preset_payload()
        self.assertAlmostEqual(
            float(extras["autogain"]["params"]["targetDb"]), -15.0, places=6
        )
        self.assertAlmostEqual(
            float(extras["loudness"]["params"]["volumeDb"]),
            float(self.manager.loudness_db_from_percent(60)),
            places=6,
        )
        self.assertAlmostEqual(
            float(preset["output"]["autogain#0"]["target"]),
            self._autogain_target_for(extras),
            places=6,
        )
        self.assertAlmostEqual(
            float(preset["output"]["loudness#0"]["volume"]),
            self._loudness_volume_for(extras),
            places=6,
        )

    async def test_coordinator_holds_ownership_through_settle_and_validation(self):
        with mock.patch.object(main, "set_output_volume", return_value=None), mock.patch.object(
            main.manager, "broadcast", mock.AsyncMock()
        ), mock.patch.object(main, "schedule_peak_monitor_refresh_after_effects_change"):
            await main.save_easyeffects_extras(
                FakeExtrasRequest({"autogainTargetDb": -15.0})
            )
            await self._assert_volume_writer_waits_during_coordinator_validation(
                lambda hook: self._coordinator_reapply_with_validation(
                    hook, settle=True
                )
            )
        await self._assert_final_state_has_both_changes()

    async def test_commit_drift_recovery_holds_ownership_through_validation(self):
        with mock.patch.object(main, "set_output_volume", return_value=None), mock.patch.object(
            main.manager, "broadcast", mock.AsyncMock()
        ), mock.patch.object(main, "schedule_peak_monitor_refresh_after_effects_change"):
            await main.save_easyeffects_extras(
                FakeExtrasRequest({"autogainTargetDb": -15.0})
            )
            await self._assert_volume_writer_waits_during_coordinator_validation(
                lambda hook: self._coordinator_reapply_with_validation(
                    hook, settle=False
                )
            )
        await self._assert_final_state_has_both_changes()

    async def _until_write_calls(self, barrier, count):
        while barrier.calls < count:
            await asyncio.sleep(0.01)

    async def test_concurrent_autogain_and_volume_changes_both_survive(self):
        with mock.patch.object(main, "set_output_volume", return_value=None), mock.patch.object(
            main.manager, "broadcast", mock.AsyncMock()
        ), mock.patch.object(main, "schedule_peak_monitor_refresh_after_effects_change"):
            # The AutoGain change is persisted before the transition (user
            # updated the canonical extras), exactly the production scenario
            # in which the coordinator re-apply runs.
            await main.save_easyeffects_extras(
                FakeExtrasRequest({"autogainTargetDb": -15.0})
            )

            barrier = WriteBarrier()
            self._barriers.append(barrier)
            barrier.wrap(self.manager.preset_store)

            # Writer A: coordinator runtime re-apply (the P1 thread writer).
            # Writer B: canonical volume write (Loudness volumeDb change).
            # While A holds the mutation ownership, B must wait: its RMW
            # read happens only after acquiring ownership, so both changes
            # survive and the persisted pair stays mutually consistent.
            reapply_task = asyncio.create_task(self._coordinator_reapply())
            self.assertTrue(await asyncio.to_thread(barrier.entered.wait, 10))

            volume_task = asyncio.create_task(main._set_canonical_output_volume(60))
            self.assertTrue(main._easyeffects_mutation_lock().locked())
            try:
                # B's volume change takes ~0.5s of guarded ramp before its
                # preset write; the full 2s window must show NO second write
                # while A holds the ownership.
                await asyncio.wait_for(self._until_write_calls(barrier, 2), timeout=2.0)
            except asyncio.TimeoutError:
                pass
            else:
                self.fail(
                    "volume writer reached the persistence RMW while the "
                    "coordinator writer held the mutation ownership"
                )
            self.assertEqual(barrier.calls, 1)
            self.assertFalse(volume_task.done())

            barrier.release.set()
            await asyncio.gather(reapply_task, volume_task)

        extras = self._extras_payload()
        preset = self._preset_payload()

        expected_extras = self.manager.normalize_effects_extras(
            {
                **self._seed_extras(),
                "autogain": {
                    **self._seed_extras()["autogain"],
                    "params": {**self._seed_extras()["autogain"]["params"], "targetDb": -15.0},
                },
                "loudness": {
                    **self._seed_extras()["loudness"],
                    "params": {
                        **self._seed_extras()["loudness"]["params"],
                        "volumeDb": self.manager.loudness_db_from_percent(60),
                    },
                },
            }
        )
        self.assertEqual(extras, expected_extras)
        self.assertAlmostEqual(
            float(extras["autogain"]["params"]["targetDb"]), -15.0, places=6
        )
        self.assertAlmostEqual(
            float(extras["loudness"]["params"]["volumeDb"]),
            float(self.manager.loudness_db_from_percent(60)),
            places=6,
        )
        # Global extras and the active preset agree after the concurrency.
        self.assertAlmostEqual(
            float(preset["output"]["autogain#0"]["target"]),
            self._autogain_target_for(expected_extras),
            places=6,
        )
        self.assertAlmostEqual(
            float(preset["output"]["loudness#0"]["volume"]),
            self._loudness_volume_for(expected_extras),
            places=6,
        )

    async def test_coordinator_writer_waits_for_mutation_ownership(self):
        entered = []
        real_apply = self.manager.apply_autogain_loudness_runtime

        def wrapped(*args, **kwargs):
            entered.append(True)
            return real_apply(*args, **kwargs)

        self.manager.apply_autogain_loudness_runtime = wrapped

        async with main._easyeffects_mutation_lock():
            task = asyncio.create_task(self._coordinator_reapply())
            await asyncio.sleep(0.05)
            self.assertTrue(main._easyeffects_mutation_lock().locked())
            # The threaded writer must not start its manager work while the
            # loop holds the mutation ownership.
            self.assertEqual(entered, [])
        await task
        self.assertEqual(entered, [True])

    async def test_cancelled_caller_releases_ownership_after_worker_finishes(self):
        barrier = WriteBarrier()
        self._barriers.append(barrier)
        barrier.wrap(self.manager.preset_store)

        caller = asyncio.create_task(self._coordinator_reapply())
        self.assertTrue(await asyncio.to_thread(barrier.entered.wait, 10))

        caller.cancel()
        await asyncio.sleep(0.05)

        lock = main._easyeffects_mutation_lock()
        self.assertTrue(lock.locked())
        self.assertTrue(barrier.concurrent >= 1)

        acquirer_done = asyncio.Event()

        async def acquire():
            async with main._easyeffects_mutation_lock():
                acquirer_done.set()

        acquirer = asyncio.create_task(acquire())
        await asyncio.sleep(0.05)
        self.assertFalse(acquirer_done.is_set())

        barrier.release.set()
        await asyncio.gather(caller, acquirer, return_exceptions=True)
        self.assertTrue(caller.cancelled())
        self.assertTrue(acquirer_done.is_set())
        self.assertFalse(lock.locked())

    async def test_atomic_extras_write_reader_never_sees_partial_json(self):
        big_extras = self.manager.normalize_effects_extras(None)
        big_extras["loudness"]["enabled"] = True
        big_extras["loudness"]["params"]["calibrationProfiles"] = {
            f"profile-{index}": {
                "id": f"profile-{index}",
                "targetSplDb": 83.0,
                "measuredSplDb": 82.0 + (index % 100) / 100.0,
                "label": "x" * 400,
            }
            for index in range(300)
        }
        seed = self._seed_extras()

        entered = threading.Event()
        release = threading.Event()
        real_replace = easyeffects_persistence.os.replace

        def pausing_replace(source, target):
            entered.set()
            if not release.wait(timeout=10):
                raise RuntimeError("replace barrier timed out")
            return real_replace(source, target)

        with mock.patch.object(
            easyeffects_persistence.os, "replace", side_effect=pausing_replace
        ):
            writer = asyncio.create_task(
                asyncio.to_thread(self.manager.save_global_extras, big_extras)
            )
            self.assertTrue(await asyncio.to_thread(entered.wait, 10))

            # The temp file is complete but not yet renamed: readers still
            # observe the old complete JSON, never a parse-failure default.
            during = await asyncio.to_thread(self.manager.load_global_extras)
            self.assertEqual(during, seed)

            release.set()
            await writer

        after = await asyncio.to_thread(self.manager.load_global_extras)
        self.assertEqual(after, self.manager.normalize_effects_extras(big_extras))

    async def test_atomic_preset_write_reader_never_sees_partial_preset(self):
        old_payload = json.loads(
            (self.manager.output_dir / f"{ACTIVE_PRESET}.json").read_text()
        )
        new_payload = copy.deepcopy(old_payload)
        new_payload["output"]["loudness"] = {
            **new_payload["output"]["loudness"],
            "volume": -28.0,
        }

        entered = threading.Event()
        release = threading.Event()
        real_replace = easyeffects_persistence.os.replace

        def pausing_replace(source, target):
            entered.set()
            if not release.wait(timeout=10):
                raise RuntimeError("replace barrier timed out")
            return real_replace(source, target)

        with mock.patch.object(
            easyeffects_persistence.os, "replace", side_effect=pausing_replace
        ):
            writer = asyncio.create_task(
                asyncio.to_thread(
                    self.manager.preset_store.write_preset, ACTIVE_PRESET, new_payload
                )
            )
            self.assertTrue(await asyncio.to_thread(entered.wait, 10))

            during = await asyncio.to_thread(
                self.manager.preset_store.read_preset, ACTIVE_PRESET
            )
            self.assertEqual(during, old_payload)

            release.set()
            await writer

        after = await asyncio.to_thread(
            self.manager.preset_store.read_preset, ACTIVE_PRESET
        )
        self.assertEqual(after, new_payload)

    async def test_event_loop_stays_responsive_while_worker_holds_ownership(self):
        ticks = []

        async def ticker():
            while True:
                ticks.append(1)
                await asyncio.sleep(0.005)

        tick = asyncio.create_task(ticker())
        try:
            task = asyncio.create_task(self._coordinator_reapply())
            await asyncio.sleep(0.05)
            self.assertTrue(main._easyeffects_mutation_lock().locked())
            await task
        finally:
            tick.cancel()
        # The worker ran ~0.5s of real waits off the loop: the loop ticked.
        self.assertGreater(len(ticks), 0)

    async def test_create_with_ir_resolves_extras_under_ownership(self):
        self.manager.irs_dir.mkdir(parents=True, exist_ok=True)
        (self.manager.irs_dir / "New.irs").write_bytes(b"x")
        self.manager.upload_ir = lambda source_path, filename, stored_name=None: {
            "name": stored_name or "x.irs",
            "basename": Path(stored_name or "x.irs").stem,
            "path": str(self.manager.irs_dir / (stored_name or "x.irs")),
            "size": 1,
            "format": "irs",
        }

        staged = threading.Event()
        release_stage = threading.Event()

        async def blocking_stage(upload, tmp_file, max_bytes):
            staged.set()
            if not await asyncio.to_thread(release_stage.wait, 10):
                raise RuntimeError("stage barrier timed out")

        class FakeUpload:
            filename = "test.wav"

        with mock.patch.object(main, "save_upload_to_file", side_effect=blocking_stage), mock.patch.object(
            main, "set_output_volume", return_value=None
        ), mock.patch.object(main.manager, "broadcast", mock.AsyncMock()), mock.patch.object(
            main, "schedule_peak_monitor_refresh_after_effects_change"
        ):
            create_task = asyncio.create_task(
                main.create_convolver_preset_with_ir(
                    preset_name="New",
                    load_after_create=False,
                    limiter_enabled=False,
                    headroom_enabled=False,
                    headroom_gain_db=-3.0,
                    autogain_enabled=False,
                    autogain_target_db=-12.0,
                    delay_enabled=False,
                    delay_left_ms=0.0,
                    delay_right_ms=0.0,
                    bass_enabled=False,
                    bass_amount=0.0,
                    tone_effect_enabled=False,
                    tone_effect_mode="crystalizer",
                    file=FakeUpload(),
                )
            )
            # The upload is staged outside the mutation ownership; canonical
            # loudness is still in state A here.
            self.assertTrue(await asyncio.to_thread(staged.wait, 10))

            # Before the create path acquires the mutation ownership, a
            # parallel canonical volume writer moves Loudness to state B.
            await main._set_canonical_output_volume(60)

            release_stage.set()
            result = await asyncio.wait_for(create_task, timeout=15)

        self.assertEqual(result["preset"]["name"], "New")
        # The extras the endpoint resolved under the ownership carry state B.
        expected_extras = main._effects_extras_from_form(
            limiter_enabled=False,
            headroom_enabled=False,
            headroom_gain_db=-3.0,
            autogain_enabled=False,
            autogain_target_db=-12.0,
            delay_enabled=False,
            delay_left_ms=0.0,
            delay_right_ms=0.0,
            bass_enabled=False,
            bass_amount=0.0,
            tone_effect_enabled=False,
            tone_effect_mode="crystalizer",
        )
        self.assertAlmostEqual(
            float(expected_extras["loudness"]["params"]["volumeDb"]),
            float(self.manager.loudness_db_from_percent(60)),
            places=6,
        )
        # The created preset contains state B, never the pre-upload state A.
        preset = json.loads((self.manager.output_dir / "New.json").read_text())
        self.assertAlmostEqual(
            float(preset["output"]["loudness#0"]["volume"]),
            self._loudness_volume_for(expected_extras),
            places=6,
        )

    def test_effects_extras_from_form_only_called_under_mutation_ownership(self):
        import ast

        tree = ast.parse(pathlib.Path(main.__file__).read_text())
        findings = []

        def visit(node, parents=()):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "_effects_extras_from_form"
            ):
                under_lock = any(
                    isinstance(parent, ast.AsyncWith)
                    and any(
                        isinstance(item.context_expr, ast.Call)
                        and isinstance(item.context_expr.func, ast.Name)
                        and item.context_expr.func.id == "_easyeffects_mutation_lock"
                        for item in parent.items
                    )
                    for parent in parents
                )
                if not under_lock:
                    findings.append(
                        f"_effects_extras_from_form outside mutation ownership: "
                        f"main.py:{node.lineno}"
                    )
            for child in ast.iter_child_nodes(node):
                visit(child, parents + (node,))

        visit(tree)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "_run_locked_worker"
                and any(keyword.arg == "extras" for keyword in node.keywords)
            ):
                findings.append(
                    f"_run_locked_worker must not carry extras (double-acquire "
                    f"risk): main.py:{node.lineno}"
                )
        self.assertEqual(findings, [])

    async def test_parallel_mutations_no_deadlock_consistent_final_state(self):
        with mock.patch.object(main, "set_output_volume", return_value=None), mock.patch.object(
            main.manager, "broadcast", mock.AsyncMock()
        ), mock.patch.object(main, "schedule_peak_monitor_refresh_after_effects_change"), mock.patch.object(
            self.manager, "_send_socket_command", return_value=""
        ):
            tasks = [
                main._set_canonical_output_volume(55),
                main.save_easyeffects_extras(
                    FakeExtrasRequest({"autogainTargetDb": -15.0})
                ),
                main._load_easyeffects_preset(ACTIVE_PRESET),
                self._coordinator_reapply(),
            ]
            await asyncio.wait_for(asyncio.gather(*tasks), timeout=15)
        self.assertFalse(main._easyeffects_mutation_lock().locked())

        # One trailing reapply makes the persisted pair authoritative and
        # comparable: extras file and active preset must agree.
        await self._coordinator_reapply()
        extras = self._extras_payload()
        preset = self._preset_payload()
        self.assertAlmostEqual(
            float(extras["autogain"]["params"]["targetDb"]), -15.0, places=6
        )
        self.assertAlmostEqual(
            float(extras["loudness"]["params"]["volumeDb"]),
            float(self.manager.loudness_db_from_percent(55)),
            places=6,
        )
        self.assertAlmostEqual(
            float(preset["output"]["autogain#0"]["target"]),
            self._autogain_target_for(extras),
            places=6,
        )
        self.assertAlmostEqual(
            float(preset["output"]["loudness#0"]["volume"]),
            self._loudness_volume_for(extras),
            places=6,
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
