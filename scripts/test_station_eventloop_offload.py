#!/usr/bin/env python3
"""P1-4 regression tests: station I/O must not block the asyncio event loop.

Covers:
  * event-loop liveness while a patched (blocking) stations.safe_get holds
    create / edit / import / GET-lazy-enrichment / browser-selection workers,
  * store serialization: parallel creates, create vs delete, create vs
    lazy enrichment all share one mutation ownership (no lost updates),
  * pure reads are side-effect-free (no network, no persist), including the
    radio artwork playback path,
  * lazy enrichment runs exactly once and persists under ownership,
  * cancellation: the mutation lock stays held until the real worker has
    finished and is released cleanly afterwards.

No real internet access: stations.safe_get is patched to block on
threading.Events and return canned responses.
"""

import asyncio
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main  # noqa: E402
import radio_api  # noqa: E402
import stations  # noqa: E402

TICK_INTERVAL = 0.01
LIVE_TICK_THRESHOLD = 15


class FakeResponse:
    def __init__(self, text="", content=b"", content_type="text/plain", status=200):
        self.status_code = status
        self.ok = 200 <= status < 400
        self._text = text
        self._content = content
        self.headers = {"content-type": content_type}

    @property
    def text(self):
        return self._text

    @property
    def content(self):
        return self._content


class BlockingSafeGet:
    """Drop-in for stations.safe_get.  Each call blocks until its gate opens."""

    def __init__(self, responses):
        self.responses = responses
        self.calls = 0
        self.gates = [threading.Event() for _ in responses]
        self.enters = [threading.Event() for _ in responses]

    def install(self):
        self.original = stations.safe_get
        stations.safe_get = self.__call__

    def __call__(self, url, *, params=None, headers=None, timeout=None, max_redirects=5, max_bytes=None):
        idx = self.calls
        self.calls += 1
        if idx < len(self.enters):
            self.enters[idx].set()
        if idx < len(self.gates):
            if not self.gates[idx].wait(10.0):
                raise AssertionError(f"safe_get call {idx} gate never released")
        if idx < len(self.responses):
            return self.responses[idx]
        raise AssertionError(f"unexpected safe_get call {idx} for {url}")


class FailNetwork:
    """safe_get replacement that fails loudly: pure reads must never reach it."""

    def __init__(self):
        self.calls = 0

    def install(self):
        self.original = stations.safe_get
        stations.safe_get = self.__call__

    def __call__(self, url, *args, **kwargs):
        self.calls += 1
        raise AssertionError(f"unexpected network access during pure read: {url}")


class BrowserFakeResponse:
    def __init__(self, data):
        self.data = data
        self.status_code = 200

    def json(self):
        return self.data


def browser_item(index=1):
    return {
        "stationuuid": f"uuid-{index}",
        "name": f"Station {index}",
        "url": f"https://example.test/input-{index}.pls",
        "url_resolved": f"https://example.test/stream-{index}",
        "favicon": f"https://example.test/icon-{index}.png",
        "country": "Germany",
        "countrycode": "DE",
        "language": "german",
        "tags": "ambient",
        "codec": "MP3",
        "bitrate": 192,
        "lastcheckok": 1,
        "clickcount": 10,
    }


class StationEventLoopOffloadTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.previous_config_home = os.environ.get("XDG_CONFIG_HOME")
        os.environ["XDG_CONFIG_HOME"] = self.temp_dir.name
        self.static_dir = Path(tempfile.mkdtemp(prefix="fxroute-test-static-"))
        self.art_dir = self.static_dir / "station-art"
        self.art_dir.mkdir()
        self.previous_art_dir = stations.STATION_ART_DIR
        self.previous_static_dir = radio_api.STATIC_DIR
        stations.STATION_ART_DIR = self.art_dir
        radio_api.STATIC_DIR = self.static_dir
        stations._cached_stations = None
        radio_api._station_mutation_lock = None
        self._original_safe_get = stations.safe_get

    def tearDown(self):
        stations.safe_get = self._original_safe_get
        stations.STATION_ART_DIR = self.previous_art_dir
        radio_api.STATIC_DIR = self.previous_static_dir
        stations._cached_stations = None
        radio_api._station_mutation_lock = None
        if self.previous_config_home is None:
            os.environ.pop("XDG_CONFIG_HOME", None)
        else:
            os.environ["XDG_CONFIG_HOME"] = self.previous_config_home
        self.temp_dir.cleanup()
        shutil.rmtree(self.static_dir, ignore_errors=True)

    def station_file(self) -> Path:
        return Path(self.temp_dir.name) / "fxroute" / "stations.json"

    def write_stations(self, items):
        self.station_file().parent.mkdir(parents=True, exist_ok=True)
        self.station_file().write_text(json.dumps(items, indent=2) + "\n", encoding="utf-8")
        stations._cached_stations = None

    def install_network(self, helper):
        helper.install()
        return helper

    async def _tick(self, ticks):
        while True:
            ticks.append(time.monotonic())
            await asyncio.sleep(TICK_INTERVAL)

    async def _measure_liveness(self, ticks, seconds=0.3):
        before = len(ticks)
        await asyncio.sleep(seconds)
        return len(ticks) - before

    def _scenario(self, scenario_factory):
        """Run an async scenario on a fresh loop with a ticker; ticks are passed in."""
        ticks = []

        async def run():
            ticker = asyncio.ensure_future(self._tick(ticks))
            try:
                return await scenario_factory(ticks)
            finally:
                ticker.cancel()
                try:
                    await ticker
                except asyncio.CancelledError:
                    pass

        return asyncio.run(run())

    # ------------------------------------------------------------------
    # Event-loop liveness while blocking network calls run in workers
    # ------------------------------------------------------------------

    def test_create_keeps_loop_live_and_persists_resolved_stream(self):
        self.write_stations([])
        helper = self.install_network(BlockingSafeGet([
            FakeResponse(text="[playlist]\nFile1=https://example.test/stream"),
        ]))

        async def scenario(ticks):
            task = asyncio.ensure_future(radio_api.create_station(
                radio_api.StationUpsertRequest(name="Diag", stream_url="https://example.test/radio.pls")
            ))
            await asyncio.to_thread(helper.enters[0].wait)
            live_ticks = await self._measure_liveness(ticks)
            helper.gates[0].set()
            return await task, live_ticks

        result, live_ticks = self._scenario(scenario)
        self.assertGreaterEqual(live_ticks, LIVE_TICK_THRESHOLD, "ticker must keep running while create resolves network")
        self.assertEqual(helper.calls, 1)
        self.assertEqual(result["station"]["stream_url"], "https://example.test/stream")
        saved = stations.get_stations()
        self.assertEqual([station.id for station in saved], ["diag"])
        self.assertEqual(saved[0].stream_url, "https://example.test/stream")

    def test_edit_keeps_loop_live_and_persists_resolved_stream(self):
        self.write_stations([{
            "id": "diag",
            "name": "Diag",
            "input_url": "https://example.test/radio.pls",
            "stream_url": "https://example.test/stream",
        }])
        helper = self.install_network(BlockingSafeGet([
            FakeResponse(text="[playlist]\nFile1=https://example.test/edited"),
        ]))

        async def scenario(ticks):
            task = asyncio.ensure_future(radio_api.edit_station(
                "diag",
                radio_api.StationUpsertRequest(name="Diag2", stream_url="https://example.test/edited.pls"),
            ))
            await asyncio.to_thread(helper.enters[0].wait)
            live_ticks = await self._measure_liveness(ticks)
            helper.gates[0].set()
            return await task, live_ticks

        result, live_ticks = self._scenario(scenario)
        self.assertGreaterEqual(live_ticks, LIVE_TICK_THRESHOLD, "ticker must keep running while edit resolves network")
        self.assertEqual(helper.calls, 1)
        self.assertEqual(result["station"]["stream_url"], "https://example.test/edited")
        saved = stations.get_stations()
        self.assertEqual([station.id for station in saved], ["diag"])
        self.assertEqual(saved[0].stream_url, "https://example.test/edited")

    def test_import_keeps_loop_live_and_persists_all_items(self):
        self.write_stations([])
        helper = self.install_network(BlockingSafeGet([
            FakeResponse(text="[playlist]\nFile1=https://example.test/a"),
            FakeResponse(text="[playlist]\nFile1=https://example.test/b"),
        ]))

        async def scenario(ticks):
            task = asyncio.ensure_future(radio_api.import_stations([
                radio_api.StationImportItem(name="A", url="https://example.test/a.pls"),
                radio_api.StationImportItem(name="B", url="https://example.test/b.pls"),
            ]))
            await asyncio.to_thread(helper.enters[0].wait)
            live_ticks = await self._measure_liveness(ticks)
            helper.gates[0].set()
            await asyncio.to_thread(helper.enters[1].wait)
            live_ticks += await self._measure_liveness(ticks)
            helper.gates[1].set()
            return await task, live_ticks

        result, live_ticks = self._scenario(scenario)
        self.assertGreaterEqual(live_ticks, LIVE_TICK_THRESHOLD, "ticker must keep running during import")
        self.assertEqual(helper.calls, 2)
        self.assertEqual([item["status"] for item in result["results"]], ["ok", "ok"])
        saved = stations.get_stations()
        self.assertEqual([station.id for station in saved], ["a", "b"])
        self.assertEqual(saved[0].stream_url, "https://example.test/a")
        self.assertEqual(saved[1].stream_url, "https://example.test/b")

    def test_get_list_enriches_lazily_keeps_loop_live_and_persists(self):
        self.write_stations([{
            "id": "diag-soma",
            "name": "Diag Soma",
            "input_url": "https://somafm.com/diag130.pls",
            "stream_url": "https://ice4.somafm.com/diag-128-mp3",
        }])
        helper = self.install_network(BlockingSafeGet([
            FakeResponse(text='<meta property="og:image" content="https://somafm.com/diag.jpg">'),
            FakeResponse(content=b"\x89PNG-fake", content_type="image/png"),
        ]))

        async def scenario(ticks):
            task = asyncio.ensure_future(radio_api.list_stations())
            await asyncio.to_thread(helper.enters[0].wait)
            live_ticks = await self._measure_liveness(ticks)
            helper.gates[0].set()
            await asyncio.to_thread(helper.enters[1].wait)
            live_ticks += await self._measure_liveness(ticks)
            helper.gates[1].set()
            first = await task
            second = await asyncio.ensure_future(radio_api.list_stations())
            return first, second, live_ticks

        first, second, live_ticks = self._scenario(scenario)
        self.assertGreaterEqual(live_ticks, LIVE_TICK_THRESHOLD, "ticker must keep running during GET enrichment")
        self.assertEqual(helper.calls, 2, "enrichment must happen exactly once, cache/persist covers the rest")
        raw = json.loads(self.station_file().read_text(encoding="utf-8"))
        self.assertEqual(raw[0]["image_url"], "/static/station-art/diag.png")
        self.assertTrue((self.art_dir / "diag.png").is_file())
        self.assertEqual(first[0]["image_url"], "/static/station-art/diag.png")
        self.assertEqual(second[0]["image_url"], "/static/station-art/diag.png")

    def test_browser_selection_uses_locked_worker_and_keeps_loop_live(self):
        self.write_stations([])
        helper = self.install_network(BlockingSafeGet([
            FakeResponse(text="[playlist]\nFile1=https://example.test/stream-1"),
        ]))

        async def scenario(ticks):
            with patch("radio_api.requests.get", return_value=BrowserFakeResponse([browser_item()])):
                task = asyncio.ensure_future(radio_api.add_station_browser_selection("uuid-1"))
                await asyncio.to_thread(helper.enters[0].wait)
                live_ticks = await self._measure_liveness(ticks)
                helper.gates[0].set()
                return await task, live_ticks

        result, live_ticks = self._scenario(scenario)
        self.assertGreaterEqual(live_ticks, LIVE_TICK_THRESHOLD, "ticker must keep running during browser-selection add")
        self.assertEqual(helper.calls, 1)
        self.assertEqual(result["station"]["id"], "station-1")
        self.assertEqual([station.id for station in stations.get_stations()], ["station-1"])

    # ------------------------------------------------------------------
    # Store serialization: one ownership connects all writers
    # ------------------------------------------------------------------

    def test_parallel_creates_are_serialized_without_lost_update(self):
        self.write_stations([])
        helper = self.install_network(BlockingSafeGet([
            FakeResponse(text="[playlist]\nFile1=https://example.test/a"),
            FakeResponse(text="[playlist]\nFile1=https://example.test/b"),
        ]))

        async def scenario(ticks):
            task_a = asyncio.ensure_future(radio_api.create_station(
                radio_api.StationUpsertRequest(name="A", stream_url="https://example.test/a.pls")
            ))
            await asyncio.to_thread(helper.enters[0].wait)
            self.assertEqual(helper.calls, 1, "first create is inside its worker")
            task_b = asyncio.ensure_future(radio_api.create_station(
                radio_api.StationUpsertRequest(name="B", stream_url="https://example.test/b.pls")
            ))
            await asyncio.sleep(0.3)
            self.assertEqual(helper.calls, 1, "second create must wait at the mutation lock, not start its own network work")
            helper.gates[0].set()
            await task_a
            await asyncio.to_thread(helper.enters[1].wait)
            self.assertEqual(helper.calls, 2, "second create only runs after ownership was released")
            helper.gates[1].set()
            return await task_b

        self._scenario(scenario)
        saved = stations.get_stations()
        self.assertEqual([station.id for station in saved], ["a", "b"], "both creates must persist, no lost update")

    def test_parallel_create_and_delete_do_not_clobber_each_other(self):
        self.write_stations([{
            "id": "victim",
            "name": "Victim",
            "input_url": "https://example.test/victim",
            "stream_url": "https://example.test/victim",
        }])
        helper = self.install_network(BlockingSafeGet([
            FakeResponse(text="[playlist]\nFile1=https://example.test/a"),
        ]))

        async def scenario(ticks):
            task_create = asyncio.ensure_future(radio_api.create_station(
                radio_api.StationUpsertRequest(name="A", stream_url="https://example.test/a.pls")
            ))
            await asyncio.to_thread(helper.enters[0].wait)
            task_delete = asyncio.ensure_future(radio_api.remove_station("victim"))
            await asyncio.sleep(0.3)
            self.assertEqual(helper.calls, 1, "delete must wait at the mutation lock while create is mid-flight")
            helper.gates[0].set()
            await task_create
            await task_delete

        self._scenario(scenario)
        saved = stations.get_stations()
        self.assertEqual(
            [station.id for station in saved], ["a"],
            "delete of the victim and the new create must both be reflected, no lost update",
        )

    def test_lazy_enrichment_and_create_share_the_same_ownership(self):
        self.write_stations([{
            "id": "diag-soma",
            "name": "Diag Soma",
            "input_url": "https://somafm.com/diag130.pls",
            "stream_url": "https://ice4.somafm.com/diag-128-mp3",
        }])
        helper = self.install_network(BlockingSafeGet([
            FakeResponse(text='<meta property="og:image" content="https://somafm.com/diag.jpg">'),
            FakeResponse(content=b"\x89PNG-fake", content_type="image/png"),
        ]))

        async def scenario(ticks):
            task_enrich = asyncio.ensure_future(radio_api.list_stations())
            await asyncio.to_thread(helper.enters[0].wait)
            self.assertEqual(helper.calls, 1)
            task_create = asyncio.ensure_future(radio_api.create_station(
                radio_api.StationUpsertRequest(name="New", stream_url="https://example.test/direct-stream")
            ))
            await asyncio.sleep(0.3)
            self.assertEqual(helper.calls, 1, "create must wait for the enrichment's persist")
            helper.gates[0].set()
            await asyncio.to_thread(helper.enters[1].wait)
            self.assertEqual(helper.calls, 2)
            helper.gates[1].set()
            await task_enrich
            await task_create

        self._scenario(scenario)
        saved = stations.get_stations()
        self.assertEqual([station.id for station in saved], ["diag-soma", "new"])
        self.assertEqual(saved[0].image_url, "/static/station-art/diag.png")
        self.assertEqual(saved[1].stream_url, "https://example.test/direct-stream")

    # ------------------------------------------------------------------
    # Cancellation / worker lifecycle
    # ------------------------------------------------------------------

    def test_cancelled_caller_holds_ownership_until_worker_finishes(self):
        self.write_stations([])
        helper = self.install_network(BlockingSafeGet([
            FakeResponse(text="[playlist]\nFile1=https://example.test/a"),
            FakeResponse(text="[playlist]\nFile1=https://example.test/b"),
        ]))

        async def scenario(ticks):
            task_a = asyncio.ensure_future(radio_api.create_station(
                radio_api.StationUpsertRequest(name="A", stream_url="https://example.test/a.pls")
            ))
            await asyncio.to_thread(helper.enters[0].wait)
            task_a.cancel()
            await asyncio.sleep(0.1)
            task_b = asyncio.ensure_future(radio_api.create_station(
                radio_api.StationUpsertRequest(name="B", stream_url="https://example.test/b.pls")
            ))
            await asyncio.sleep(0.3)
            self.assertEqual(helper.calls, 1, "ownership must stay held while the cancelled caller's worker still runs")
            helper.gates[0].set()
            with self.assertRaises(asyncio.CancelledError):
                await task_a
            await asyncio.to_thread(helper.enters[1].wait)
            helper.gates[1].set()
            result_b = await task_b
            self.assertEqual(result_b["station"]["id"], "b")
            saved = stations.get_stations()
            self.assertEqual(
                [station.id for station in saved], ["a", "b"],
                "both workers run to completion and persist; the cancelled caller must not lose its write",
            )

            async def lock_probe():
                async with radio_api._get_station_mutation_lock():
                    return True

            await asyncio.wait_for(lock_probe(), 1.0)

        self._scenario(scenario)

    # ------------------------------------------------------------------
    # Atomic writes: reader vs writer
    # ------------------------------------------------------------------

    def test_atomic_write_reader_never_sees_partial_file(self):
        self.write_stations([{
            "id": "a",
            "name": "A",
            "input_url": "https://example.test/a",
            "stream_url": "https://example.test/a",
        }])
        entered_replace = threading.Event()
        release_replace = threading.Event()
        real_replace = stations.os.replace

        def blocked_replace(src, dst):
            entered_replace.set()
            if not release_replace.wait(10.0):
                raise AssertionError("atomic replace gate never released")
            return real_replace(src, dst)

        stations.os.replace = blocked_replace
        try:
            writer_errors = []
            def writer():
                try:
                    stations._save_raw_stations([
                        {
                            "id": "a",
                            "name": "A",
                            "input_url": "https://example.test/a",
                            "stream_url": "https://example.test/a",
                        },
                        {
                            "id": "b",
                            "name": "B",
                            "input_url": "https://example.test/b",
                            "stream_url": "https://example.test/b",
                        },
                    ])
                except Exception as exc:  # pragma: no cover - failure reporting
                    writer_errors.append(exc)

            thread = threading.Thread(target=writer)
            thread.start()
            self.assertTrue(entered_replace.wait(10.0), "writer must reach the atomic replace")

            stations._cached_stations = None
            mid_write = stations.get_stations()
            self.assertEqual(
                [station.id for station in mid_write], ["a"],
                "reader mid-write must see the complete OLD state",
            )
            raw_mid_write = json.loads(self.station_file().read_text(encoding="utf-8"))
            self.assertEqual([item["id"] for item in raw_mid_write], ["a"], "file must still be complete JSON mid-write")

            release_replace.set()
            thread.join(timeout=10.0)
            self.assertFalse(thread.is_alive(), "writer thread must finish")
            self.assertEqual(writer_errors, [])

            stations._cached_stations = None
            after = stations.get_stations()
            self.assertEqual(
                [station.id for station in after], ["a", "b"],
                "reader after the commit must see the complete NEW state",
            )
        finally:
            stations.os.replace = real_replace

    def test_stale_cache_publication_race_reader_cannot_publish_old_snapshot(self):
        self.write_stations([])
        real_load = stations._load_raw_stations
        entered_read = threading.Event()
        release_read = threading.Event()
        load_calls = {"count": 0}

        def blocking_load():
            raw = real_load()
            load_calls["count"] += 1
            if load_calls["count"] == 1:
                entered_read.set()
                if not release_read.wait(10.0):
                    raise AssertionError("reader load gate never released")
            return raw

        stations._load_raw_stations = blocking_load
        try:
            reader_result = {}

            def reader():
                reader_result["stations"] = stations.get_stations()

            reader_thread = threading.Thread(target=reader)
            reader_thread.start()
            self.assertTrue(entered_read.wait(10.0), "reader must have loaded the old raw state")

            stations.add_station("a", "https://example.test/direct-a")

            release_read.set()
            reader_thread.join(timeout=10.0)
            self.assertFalse(reader_thread.is_alive(), "reader thread must finish")
            self.assertNotIn("error", reader_result)

            loaded = reader_result["stations"]
            self.assertEqual(
                [station.id for station in loaded], ["a"],
                "the overlapping reader must not publish its stale empty snapshot",
            )
            next_read = stations.get_stations()
            self.assertEqual(
                [station.id for station in next_read], ["a"],
                "the cache must not be permanently stale after the race",
            )
        finally:
            stations._load_raw_stations = real_load

    # ------------------------------------------------------------------
    # Pure reads are side-effect-free
    # ------------------------------------------------------------------

    def test_pure_read_is_side_effect_free_and_playback_paths_do_not_network(self):
        seed = [{
            "id": "diag-soma",
            "name": "Diag Soma",
            "input_url": "https://somafm.com/diag130.pls",
            "stream_url": "https://ice4.somafm.com/diag-128-mp3",
        }]
        self.write_stations(seed)
        network = FailNetwork()
        network.install()
        before = self.station_file().read_bytes()

        loaded = stations.get_stations()
        self.assertEqual(network.calls, 0, "pure read must not hit the network")
        self.assertEqual(loaded[0].image_url, None, "pure read must not enrich")
        self.assertEqual(self.station_file().read_bytes(), before, "pure read must not persist")

        artwork = main._radio_artwork_url_for_track({"id": "radio_diag-soma", "station_id": "diag-soma"})
        self.assertEqual(artwork, "")
        track = main._playback_track_with_artwork_fields(
            {"id": "radio_diag-soma", "station_id": "diag-soma", "source": "radio"}
        )
        self.assertEqual(track["artwork_url"], None)
        self.assertEqual(track["artwork_available"], False)
        self.assertEqual(network.calls, 0, "playback radio artwork path must not trigger hidden network access")


    # ------------------------------------------------------------------
    # Browser selection: find-or-create under one ownership
    # ------------------------------------------------------------------

    def test_parallel_identical_browser_selections_create_single_station(self):
        self.write_stations([])
        helper = self.install_network(BlockingSafeGet([
            FakeResponse(text="[playlist]\nFile1=https://example.test/stream-1"),
        ]))

        async def scenario(ticks):
            with patch("radio_api.requests.get", return_value=BrowserFakeResponse([browser_item()])):
                task1 = asyncio.ensure_future(radio_api.add_station_browser_selection("uuid-1"))
                await asyncio.to_thread(helper.enters[0].wait)
                task2 = asyncio.ensure_future(radio_api.add_station_browser_selection("uuid-1"))
                await asyncio.sleep(0.3)
                self.assertEqual(
                    helper.calls, 1,
                    "second selection must wait at the mutation ownership, not start its own create",
                )
                helper.gates[0].set()
                result1 = await task1
                result2 = await task2
                return result1, result2

        result1, result2 = self._scenario(scenario)
        self.assertEqual(helper.calls, 1, "the second selection must reuse the already saved station")
        self.assertEqual(
            result1["station"]["id"], result2["station"]["id"],
            "both selections must reference the same saved station",
        )
        saved = stations.get_stations()
        self.assertEqual(
            [station.id for station in saved], [result1["station"]["id"]],
            "the store must contain exactly one entry for the shared input/resolved URL",
        )
        self.assertEqual(saved[0].input_url, "https://example.test/input-1.pls")
        self.assertEqual(saved[0].stream_url, "https://example.test/stream-1")


if __name__ == "__main__":
    unittest.main()
