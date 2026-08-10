#!/usr/bin/env python3
"""Host tests for the measurement session job release watcher.

Validates the P1-1 fix: a registered running measurement job must never be
removed from the measurement session merely because the watcher poll window
(formerly a fixed 300 x 0.5 s = 150 s) elapsed.  Unregister is coupled to job
terminality instead of wall-clock time:

- a job still running after the former 150 s window stays registered;
- exactly one unregister happens once the job reaches a terminal status
  (completed / failed / cancelled);
- a stale/interrupted job without a live worker is terminalized by the
  existing store normalization (observed through get_job) and then
  unregistered exactly once;
- watcher cancellation/shutdown leaves no pending task and no unregister;
- the session generation guard and the lookup-failure exit keep their
  existing semantics.
"""

import asyncio
import sys
import time
import traceback
from pathlib import Path
from unittest.mock import AsyncMock, patch

# Ensure the project root is on sys.path so 'import measurement_session' works.
_project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_project_root))
import measurement_session

# The watcher polls through measurement_session.asyncio.sleep, which is the
# global asyncio module.  The tests patch that attribute; everything that
# needs a real yield while the patch is active must use this original.
_real_sleep = asyncio.sleep

TERMINAL = {"completed", "failed", "cancelled"}


# ── Fakes ────────────────────────────────────────────────────────────────────

class _FakeStore:
    """Minimal store double mirroring the real get_job semantics."""

    def __init__(self, status: str = "running", *, promote_on_lookup: bool = False):
        self.job = {"id": "job-1", "status": status}
        self.promote_on_lookup = promote_on_lookup
        self.get_job_calls = 0
        self.lookup_error: BaseException | None = None

    def get_job(self, job_id: str):
        self.get_job_calls += 1
        if self.lookup_error is not None:
            raise self.lookup_error
        # Mirror the real store: a stale non-terminal record without a live
        # worker is promoted to a terminal state during the lookup itself
        # (measurement.py _promote_stale_job_to_terminal via get_job).
        if self.promote_on_lookup and self.job["status"] not in TERMINAL:
            self.job["status"] = "cancelled"
        return dict(self.job)


class _FakeSession:
    def __init__(self, generation: int = 1):
        self.generation = generation
        self.unregister_manual_job = AsyncMock()


def _services(store, session):
    return measurement_session.MeasurementServices(
        get_store=lambda: store,
        get_session=lambda: session,
        auto_sub_active=lambda: False,
    )


async def _watch_task(store, session, *, generation: int = 1, ownership_job_id=None):
    with patch.object(
        measurement_session, "_measurement_services", return_value=_services(store, session)
    ):
        await measurement_session._unregister_measurement_job_after_completion(
            "job-1", generation, ownership_job_id=ownership_job_id
        )


def _fake_sleep(on_poll=None):
    """Patch the watcher's asyncio.sleep: count polls, never wait 0.5 s real."""

    async def fake_sleep(_seconds):
        if on_poll is not None:
            on_poll()
        await _real_sleep(0)

    return patch("measurement_session.asyncio.sleep", side_effect=fake_sleep)


async def _wait_until(predicate, *, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition was not reached within the timeout")
        await _real_sleep(0.01)


def _run(coro):
    return asyncio.run(coro)


# ── Test cases ───────────────────────────────────────────────────────────────

class TestJobStaysRegisteredBeyondOldWindow:
    """A running job must never be unregistered because time elapsed."""

    def test_running_job_beyond_150s_stays_registered(self) -> None:
        store = _FakeStore(status="running")
        session = _FakeSession()
        polls = {"n": 0}

        def on_poll():
            polls["n"] += 1

        async def scenario():
            with _fake_sleep(on_poll=on_poll):
                task = asyncio.create_task(_watch_task(store, session))
                try:
                    # Well beyond the former 300-poll / 150 s window.
                    await _wait_until(lambda: polls["n"] > 320)
                    session.unregister_manual_job.assert_not_awaited()
                    assert store.job["status"] == "running"
                finally:
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
                assert task.done()
            session.unregister_manual_job.assert_not_awaited()

        _run(scenario())

    def test_cancelling_is_not_terminal_and_stays_registered(self) -> None:
        store = _FakeStore(status="cancelling")
        session = _FakeSession()
        polls = {"n": 0}

        def on_poll():
            polls["n"] += 1

        async def scenario():
            with _fake_sleep(on_poll=on_poll):
                task = asyncio.create_task(_watch_task(store, session))
                try:
                    await _wait_until(lambda: polls["n"] > 320)
                    session.unregister_manual_job.assert_not_awaited()
                finally:
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
                assert task.done()
            session.unregister_manual_job.assert_not_awaited()

        _run(scenario())


class TestUnregisterOnTerminalStatus:
    """Exactly one unregister once the job is terminal."""

    def test_completed_after_long_running_unregisters_exactly_once(self) -> None:
        store = _FakeStore(status="running")
        session = _FakeSession()
        polls = {"n": 0}

        def on_poll():
            polls["n"] += 1
            if polls["n"] == 350:
                store.job["status"] = "completed"

        async def scenario():
            with _fake_sleep(on_poll=on_poll):
                await asyncio.wait_for(
                    asyncio.create_task(_watch_task(store, session)), timeout=5.0
                )
            session.unregister_manual_job.assert_awaited_once_with("job-1")

        _run(scenario())

    def test_failed_job_unregisters_exactly_once(self) -> None:
        store = _FakeStore(status="failed")
        session = _FakeSession()

        async def scenario():
            with _fake_sleep():
                await asyncio.wait_for(
                    asyncio.create_task(_watch_task(store, session)), timeout=5.0
                )
            session.unregister_manual_job.assert_awaited_once_with("job-1")

        _run(scenario())

    def test_cancelled_job_unregisters_exactly_once(self) -> None:
        store = _FakeStore(status="cancelled")
        session = _FakeSession()

        async def scenario():
            with _fake_sleep():
                await asyncio.wait_for(
                    asyncio.create_task(_watch_task(store, session)), timeout=5.0
                )
            session.unregister_manual_job.assert_awaited_once_with("job-1")

        _run(scenario())

    def test_cancelling_then_cancelled_unregisters_exactly_once(self) -> None:
        store = _FakeStore(status="cancelling")
        session = _FakeSession()
        polls = {"n": 0}

        def on_poll():
            polls["n"] += 1
            if polls["n"] == 350:
                store.job["status"] = "cancelled"

        async def scenario():
            with _fake_sleep(on_poll=on_poll):
                await asyncio.wait_for(
                    asyncio.create_task(_watch_task(store, session)), timeout=5.0
                )
            session.unregister_manual_job.assert_awaited_once_with("job-1")

        _run(scenario())

    def test_short_normal_job_unregisters_exactly_once(self) -> None:
        store = _FakeStore(status="running")
        session = _FakeSession()
        polls = {"n": 0}

        def on_poll():
            polls["n"] += 1
            if polls["n"] == 2:
                store.job["status"] = "completed"

        async def scenario():
            with _fake_sleep(on_poll=on_poll):
                await asyncio.wait_for(
                    asyncio.create_task(_watch_task(store, session)), timeout=5.0
                )
            session.unregister_manual_job.assert_awaited_once_with("job-1")
            assert polls["n"] >= 2

        _run(scenario())

    def test_ownership_job_id_is_forwarded_to_unregister(self) -> None:
        store = _FakeStore(status="completed")
        session = _FakeSession()

        async def scenario():
            with _fake_sleep():
                await asyncio.wait_for(
                    asyncio.create_task(
                        _watch_task(store, session, ownership_job_id="pending:abc")
                    ),
                    timeout=5.0,
                )
            session.unregister_manual_job.assert_awaited_once_with("pending:abc")

        _run(scenario())


class TestStaleJobPromotion:
    """A stale/interrupted job without a live worker is terminalized and released."""

    def test_stale_job_promoted_by_store_lookup_unregisters_exactly_once(self) -> None:
        store = _FakeStore(status="running", promote_on_lookup=True)
        session = _FakeSession()

        async def scenario():
            with _fake_sleep():
                await asyncio.wait_for(
                    asyncio.create_task(_watch_task(store, session)), timeout=5.0
                )
            # The store normalization promoted the stale record on first lookup.
            assert store.job["status"] == "cancelled"
            session.unregister_manual_job.assert_awaited_once_with("job-1")

        _run(scenario())


class TestWatcherExitConditions:
    """Cancellation, shutdown and session-generation semantics."""

    def test_cancelled_watcher_leaves_no_task_leak_and_no_unregister(self) -> None:
        store = _FakeStore(status="running")
        session = _FakeSession()
        polls = {"n": 0}

        def on_poll():
            polls["n"] += 1

        async def scenario():
            with _fake_sleep(on_poll=on_poll):
                task = asyncio.create_task(_watch_task(store, session))
                await _wait_until(lambda: polls["n"] > 10)
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                assert task.done()
                polls_before = polls["n"]
                await _real_sleep(0.02)
                assert polls["n"] == polls_before
            session.unregister_manual_job.assert_not_awaited()

        _run(scenario())

    def test_generation_mismatch_skips_unregister(self) -> None:
        store = _FakeStore(status="completed")
        session = _FakeSession(generation=7)

        async def scenario():
            with _fake_sleep():
                await asyncio.wait_for(
                    asyncio.create_task(_watch_task(store, session, generation=1)),
                    timeout=5.0,
                )
            session.unregister_manual_job.assert_not_awaited()

        _run(scenario())

    def test_generation_change_during_running_job_stops_watcher_promptly(self) -> None:
        store = _FakeStore(status="running")
        session = _FakeSession(generation=1)
        polls = {"n": 0}

        def on_poll():
            polls["n"] += 1
            if polls["n"] == 10:
                # The session is released/restarted while the job still runs.
                session.generation = 2

        async def scenario():
            with _fake_sleep(on_poll=on_poll):
                task = asyncio.create_task(_watch_task(store, session, generation=1))
                await asyncio.wait_for(task, timeout=5.0)
                # Polls 1-9 completed a full store lookup each; poll 10 flips
                # the generation, so the watcher stops on its next iteration
                # before a 10th lookup, without unregistering.
                assert store.get_job_calls == 9
                assert task.done()
            session.unregister_manual_job.assert_not_awaited()

        _run(scenario())

    def test_lookup_failure_stops_watcher_and_unregisters_once(self) -> None:
        store = _FakeStore(status="running")
        store.lookup_error = KeyError("job-1")
        session = _FakeSession()

        async def scenario():
            with _fake_sleep():
                await asyncio.wait_for(
                    asyncio.create_task(_watch_task(store, session)), timeout=5.0
                )
            session.unregister_manual_job.assert_awaited_once_with("job-1")

        _run(scenario())

    def test_store_unavailable_stops_watcher_and_unregisters_once(self) -> None:
        # Defensive exit: with no store there is nothing left to poll, so the
        # watcher ends and releases the ownership (existing semantics).
        session = _FakeSession()

        async def scenario():
            with patch.object(
                measurement_session,
                "_measurement_services",
                return_value=measurement_session.MeasurementServices(
                    get_store=lambda: None,
                    get_session=lambda: session,
                    auto_sub_active=lambda: False,
                ),
            ):
                with _fake_sleep():
                    await asyncio.wait_for(
                        asyncio.create_task(
                            measurement_session._unregister_measurement_job_after_completion(
                                "job-1", 1
                            )
                        ),
                        timeout=5.0,
                    )
            session.unregister_manual_job.assert_awaited_once_with("job-1")

        _run(scenario())


# ── Runner ───────────────────────────────────────────────────────────────────

def run_tests() -> int:
    test_classes = [
        TestJobStaysRegisteredBeyondOldWindow,
        TestUnregisterOnTerminalStatus,
        TestStaleJobPromotion,
        TestWatcherExitConditions,
    ]
    passed = 0
    failed = 0
    errors: list[str] = []

    for cls in test_classes:
        instance = cls()
        print(f"\n{'='*60}")
        print(f"  {cls.__name__}")
        print(f"{'='*60}")
        for name in sorted(dir(instance)):
            if not name.startswith("test_"):
                continue
            method = getattr(instance, name)
            if not callable(method):
                continue
            try:
                method()
                passed += 1
                print(f"  ✓ {name}")
            except Exception:
                failed += 1
                err = traceback.format_exc()
                errors.append(f"  ✗ {name}\n{err}")
                print(f"  ✗ {name}")
                traceback.print_exc()

    print(f"\n{'='*60}")
    print(f"  Results: {passed} passed, {failed} failed")
    print(f"{'='*60}")

    if errors:
        print("\nFailures:")
        for err in errors:
            print(err)

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(run_tests())
