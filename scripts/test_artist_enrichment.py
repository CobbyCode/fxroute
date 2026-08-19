#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only

"""Focused tests for the shared artist-enrichment owner and its TIDAL wiring.

Covers the shared SQLite caches (canonical artist, provider mapping, reverse
lookup), safe matching fallbacks, library-store delegation, the TIDAL provider
enrichment composition and the guarantee that enrichment failures/refreshes
never disturb TIDAL catalog data.
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from artist_enrichment import ArtistEnrichmentService  # noqa: E402
from library.metadata import LibraryMetadataStore  # noqa: E402
from streaming.tidal import catalog  # noqa: E402
from streaming.tidal.provider import TidalProvider  # noqa: E402


def _no_network(*args, **kwargs):
    raise RuntimeError("network disabled in tests")


class FakeBackend:
    """Scriptable request backend tracking every enrichment network call.

    Entries match on a URL fragment plus optional exact ``params`` (all given
    key/value pairs must match) and/or required present ``has`` param keys.
    """

    def __init__(self):
        self.calls: list[tuple[str, dict | None]] = []
        self._entries: list[tuple[str, dict | None, tuple, object]] = []

    def set(self, url_fragment: str, payload: object, *, params: dict | None = None, has: list[str] | None = None) -> None:
        self._entries.append((url_fragment, params, tuple(has or ()), payload))

    def _request_json(self, url: str, params: dict | None = None):
        self.calls.append((url, params))
        for fragment, want, has, payload in self._entries:
            if fragment not in url:
                continue
            if want is not None:
                if not params or not all(params.get(key) == value for key, value in want.items()):
                    continue
            if has and not all(key in (params or {}) for key in has):
                continue
            return payload
        raise AssertionError(f"unexpected url: {url} params={params}")


def _artist_search(name: str, *candidates: dict) -> dict:
    return {"artists": [dict(item, name=name) for item in candidates]}


def _mk_tmp_service(backend):
    return ArtistEnrichmentService(db_path=Path(tempfile.mkdtemp()) / "e.sqlite", request_backend=backend)


class ArtistEnrichmentCacheTests(unittest.TestCase):
    """Test #1: canonical-artist about + similar share one per-MBID cache."""

    def test_about_and_similar_cached_per_mbid(self):
        backend = FakeBackend()
        backend.set("musicbrainz.org/ws/2/artist", _artist_search("Daft Punk", {"id": "mb-1", "score": 100}), has=["query"])
        backend.set("/ws/2/artist/mb-1", {"relations": [{"url": {"resource": "https://www.wikidata.org/wiki/Q1"}}], "name": "Daft Punk"})
        backend.set("wikidata.org", {"entities": {"Q1": {"sitelinks": {"enwiki": {"title": "Daft Punk"}}}}})
        backend.set("en.wikipedia.org", {"extract": "Daft Punk were a French duo. Sentence two."})
        backend.set("lb-radio", {"tracks": [{"similar_artist_mbid": "mb-s1", "similar_artist_name": "Justice", "total_listen_count": 7}]})
        svc = _mk_tmp_service(backend)

        first = svc.enriched_artist("tidal", "tid-1", "Daft Punk")
        self.assertTrue(first["available"])
        self.assertEqual(first["mb_artist_id"], "mb-1")
        self.assertEqual(first["about"], "Daft Punk were a French duo. Sentence two.")
        self.assertEqual(len(first["similar"]), 1)
        self.assertEqual(first["similar"][0]["artist"], "Justice")
        calls_after_first = len(backend.calls)

        second = svc.enriched_artist("tidal", "tid-1", "Daft Punk")
        self.assertTrue(second["available"])
        self.assertTrue(second["cached"])
        # A second call for the same provider artist must not hit the network.
        self.assertEqual(len(backend.calls), calls_after_first)

    def test_about_cache_used_by_other_provider_null(self):
        # A second provider resolving the same canonical MBID also reuses the
        # cached about text: no second artist/wikidata/wikipedia request.
        backend = FakeBackend()
        backend.set("musicbrainz.org/ws/2/artist", _artist_search("Daft Punk", {"id": "mb-1", "score": 100}), has=["query"])
        backend.set("/ws/2/artist/mb-1", {"relations": [{"url": {"resource": "https://www.wikidata.org/wiki/Q1"}}], "name": "Daft Punk"})
        backend.set("wikidata.org", {"entities": {"Q1": {"sitelinks": {"enwiki": {"title": "Daft Punk"}}}}})
        backend.set("en.wikipedia.org", {"extract": "Daft Punk were a French duo. Sentence two."})
        backend.set("lb-radio", {"tracks": []})
        svc = _mk_tmp_service(backend)

        first = svc.enriched_artist("tidal", "tid-1", "Daft Punk")
        self.assertTrue(first["available"])
        backend.calls.clear()

        second = svc.enriched_artist("tidal", "tid-2", "Daft Punk")
        self.assertTrue(second["available"])
        self.assertEqual(second["about"], "Daft Punk were a French duo. Sentence two.")
        # Only the provider match search runs; about is served from the cache.
        self.assertFalse(any("wikidata" in url or "wikipedia" in url or url.endswith("/ws/2/artist/mb-1") for url, _ in backend.calls))


class ProviderMappingTests(unittest.TestCase):
    """Test #2: provider artist id -> canonical MBID mapping + reverse lookup."""

    def test_mapping_stored_and_reversed_by_mbid(self):
        backend = FakeBackend()
        backend.set("musicbrainz.org/ws/2/artist", _artist_search("Daft Punk", {"id": "mb-1", "score": 100}), has=["query"])
        backend.set("/ws/2/artist/mb-1", {"relations": [], "name": "Daft Punk"})
        backend.set("wikidata.org", {"entities": {}})
        backend.set("lb-radio", {"tracks": []})
        svc = _mk_tmp_service(backend)

        svc.enriched_artist("tidal", "tid-1", "Daft Punk", art_url="https://c/1.jpg")
        row = svc.store.get_provider_artist("tidal", "tid-1")
        self.assertEqual(row["mb_artist_id"], "mb-1")
        self.assertEqual(row["match_state"], "mapped")
        self.assertEqual(row["art_url"], "https://c/1.jpg")

        # Reverse lookup (MBID -> provider artist id) powers the jump from a
        # similar artist straight into the provider artist without a search.
        mapped = svc.store.reverse_provider_by_mbid("tidal", "mb-1")
        self.assertEqual(mapped["provider_artist_id"], "tid-1")
        self.assertEqual(mapped["art_url"], "https://c/1.jpg")

    def test_art_url_filled_on_later_visit(self):
        # An art URL learned on a later visit is persisted without re-matching
        # or new enrichment network requests.
        backend = FakeBackend()
        backend.set("musicbrainz.org/ws/2/artist", _artist_search("Daft Punk", {"id": "mb-1", "score": 100}), has=["query"])
        backend.set("/ws/2/artist/mb-1", {"relations": [], "name": "Daft Punk"})
        backend.set("wikidata.org", {"entities": {}})
        backend.set("lb-radio", {"tracks": []})
        svc = _mk_tmp_service(backend)

        svc.enriched_artist("tidal", "tid-1", "Daft Punk")
        calls_before = len(backend.calls)
        svc.enriched_artist("tidal", "tid-1", "Daft Punk", art_url="https://c/1.jpg")
        self.assertEqual(len(backend.calls), calls_before, "a mapped artist must not be re-matched")
        row = svc.store.get_provider_artist("tidal", "tid-1")
        self.assertEqual(row["art_url"], "https://c/1.jpg")

    def test_similar_item_gets_provider_mapping_without_search(self):
        # Test #9: once a similar artist's MBID is mapped to a provider artist,
        # the similar list carries the provider artist id so the UI can open it
        # directly instead of running a provider search.
        backend = FakeBackend()
        backend.set("musicbrainz.org/ws/2/artist", _artist_search("Daft Punk", {"id": "mb-1", "score": 100}), has=["query"])
        backend.set("/ws/2/artist/mb-1", {"relations": [], "name": "Daft Punk"})
        backend.set("wikidata.org", {"entities": {}})
        backend.set("lb-radio", {"tracks": [{"similar_artist_mbid": "mb-s1", "similar_artist_name": "Justice", "total_listen_count": 3}]})
        svc = _mk_tmp_service(backend)
        svc.enriched_artist("tidal", "tid-1", "Daft Punk")

        # Simulate a later visit of "Justice" that mapped its MBID -> tidal id.
        svc.store.set_provider_artist(
            "tidal", "tid-9", name="Justice", art_url="https://c/j.jpg",
            mb_artist_id="mb-s1", canonical_name="Justice", match_state="mapped",
            attempted_at="2026-01-01T00:00:00+00:00", matched_at="2026-01-01T00:00:00+00:00", error=None,
        )
        result = svc.enriched_artist("tidal", "tid-1", "Daft Punk")
        justice = next((i for i in result["similar"] if i.get("artist_mbid") == "mb-s1"), None)
        self.assertIsNotNone(justice)
        self.assertEqual(justice["provider_artist_id"], "tid-9")
        self.assertEqual(justice["art_url"], "https://c/j.jpg")


class MatchingFallbackTests(unittest.TestCase):
    """Test #3: safe fallback for no match and ambiguous same-name matches."""

    def test_unmatched_artist_gets_no_enrichment(self):
        backend = FakeBackend()
        backend.set("musicbrainz.org/ws/2/artist", {"artists": []}, has=["query"])
        svc = _mk_tmp_service(backend)
        result = svc.enriched_artist("tidal", "tid-x", "Totally Unknown Artist")
        self.assertFalse(result["available"])
        self.assertEqual(result["match_state"], "unmatched")
        self.assertIsNone(result["mb_artist_id"])
        self.assertIsNone(result["about"])

    def test_ambiguous_same_name_gets_no_enrichment(self):
        backend = FakeBackend()
        backend.set(
            "musicbrainz.org/ws/2/artist",
            _artist_search("Jungle", {"id": "m1", "score": 100, "disambiguation": "English"},
                           {"id": "m2", "score": 100, "disambiguation": "French"}),
            has=["query"],
        )
        svc = _mk_tmp_service(backend)
        result = svc.enriched_artist("tidal", "tid-y", "Jungle")
        self.assertFalse(result["available"])
        self.assertEqual(result["match_state"], "ambiguous")
        self.assertIsNone(result["about"])

    def test_ambiguous_resolved_by_album_titles(self):
        # Two distinct "Jungle" artists exist; only one has the provider's
        # album, so the release-group disambiguation maps it confidently.
        backend = FakeBackend()
        backend.set(
            "musicbrainz.org/ws/2/artist",
            _artist_search("Jungle", {"id": "m1", "score": 100, "disambiguation": "English"},
                           {"id": "m2", "score": 100, "disambiguation": "French"}),
            has=["query"],
        )
        backend.set("musicbrainz.org/ws/2/release-group", {"release-groups": [{"title": "Another Album"}]}, params={"artist": "m1"})
        backend.set("musicbrainz.org/ws/2/release-group", {"release-groups": [{"title": "Loving in Stereo"}]}, params={"artist": "m2"})
        backend.set("/ws/2/artist/m2", {"relations": [], "name": "Jungle"})
        backend.set("wikidata.org", {"entities": {}})
        backend.set("lb-radio", {"tracks": []})
        svc = _mk_tmp_service(backend)
        result = svc.enriched_artist("tidal", "tid-3", "Jungle", album_titles=["Loving in Stereo"])
        self.assertTrue(result["available"])
        self.assertEqual(result["mb_artist_id"], "m2")

    def test_ambiguous_without_album_data_stays_ambiguous(self):
        backend = FakeBackend()
        backend.set(
            "musicbrainz.org/ws/2/artist",
            _artist_search("Jungle", {"id": "m1", "score": 100}, {"id": "m2", "score": 100}),
            has=["query"],
        )
        svc = _mk_tmp_service(backend)
        result = svc.enriched_artist("tidal", "tid-z", "Jungle")
        self.assertFalse(result["available"])
        self.assertEqual(result["match_state"], "ambiguous")

    def test_unmatched_cooled_down(self):
        # An unmatched artist must not be re-searched on every request.
        backend = FakeBackend()
        backend.set("musicbrainz.org/ws/2/artist", {"artists": []}, has=["query"])
        svc = _mk_tmp_service(backend)
        svc.enriched_artist("tidal", "tid-x", "Nobody")
        backend.calls.clear()
        again = svc.enriched_artist("tidal", "tid-x", "Nobody")
        self.assertEqual(backend.calls, [], "unmatched mapping must be cooled down")
        self.assertFalse(again["available"])


class LibraryDelegationTests(unittest.TestCase):
    """Test #4: the existing library enrichment path stays intact and now
    delegates its network work to the shared owner through the store's own
    (patchable) request boundary."""

    def test_store_request_json_patch_blocks_shared_network(self):
        store = LibraryMetadataStore(db_path=Path(tempfile.mkdtemp()) / "m.sqlite", cover_dir=Path(tempfile.mkdtemp()) / "c")
        self.assertIsNotNone(store.artist_enrichment)
        with mock.patch.object(LibraryMetadataStore, "_request_json", _no_network):
            match = store._find_musicbrainz_release("Homework", "Daft Punk")
        self.assertIsNone(match, "delegated release match must respect the store request boundary")

    def test_album_discover_delegates_to_shared_similar_cache(self):
        backend = FakeBackend()
        store = LibraryMetadataStore(
            db_path=Path(tempfile.mkdtemp()) / "m.sqlite",
            cover_dir=Path(tempfile.mkdtemp()) / "c",
            artist_enrichment=ArtistEnrichmentService(
                db_path=Path(tempfile.mkdtemp()) / "am.sqlite", request_backend=backend
            ),
        )
        # Seed album with a mapped artist and let the shared discover serve it.
        store.sync_albums([{"id": "a1", "name": "Homework", "artist": "Daft Punk"}])
        with store._connect() as conn:
            conn.execute("UPDATE albums SET mb_artist_id = 'mb-1' WHERE album_key = 'a1'")
        backend.set("lb-radio", {"tracks": [{"similar_artist_mbid": "mb-s1", "similar_artist_name": "Justice", "total_listen_count": 4}]})
        result = store.get_album_discover("a1")
        # The library still returns its album-scoped discover contract.
        self.assertIsNotNone(result)
        items = result.get("items") or []
        self.assertEqual([x.get("artist") for x in items], ["Justice"])
        self.assertEqual(result.get("cached"), False)
        # A second call is served from the shared cache (no new requests).
        backend.calls.clear()
        store.get_album_discover("a1")
        self.assertEqual(backend.calls, [], "album discover must reuse the shared similar cache")

    def test_existing_album_artist_description_adopted_into_shared_cache(self):
        # Pre-existing per-album artist enrichment is adopted into the shared
        # cache on access (lazy, no network) so it is not re-fetched later.
        store = LibraryMetadataStore(
            db_path=Path(tempfile.mkdtemp()) / "m.sqlite",
            cover_dir=Path(tempfile.mkdtemp()) / "c",
            artist_enrichment=ArtistEnrichmentService(
                db_path=Path(tempfile.mkdtemp()) / "am.sqlite", request_backend=FakeBackend()
            ),
        )
        store.sync_albums([{"id": "a1", "name": "Homework", "artist": "Daft Punk"}])
        store.artist_enrichment._backend.calls.clear()  # drop the batch-enrich attempt
        with store._connect() as conn:
            conn.execute(
                "UPDATE albums SET mb_artist_id='mb-1', artist_description='Existing bio.' WHERE album_key='a1'"
            )
        store.get_album("a1")
        cached = store.artist_enrichment.store.get_artist("mb-1")
        self.assertEqual(cached["description"], "Existing bio.")
        self.assertEqual(store.artist_enrichment._backend.calls, [],
                         "adoption must not issue any network request")


class TidalProviderEnrichmentTests(unittest.IsolatedAsyncioTestCase):
    """TIDAL artist/album enrichment composition and resilience."""

    def _backend(self):
        backend = FakeBackend()
        backend.set("musicbrainz.org/ws/2/artist", _artist_search("Daft Punk", {"id": "mb-1", "score": 100}), has=["query"])
        backend.set("/ws/2/artist/mb-1", {"relations": [{"url": {"resource": "https://www.wikidata.org/wiki/Q1"}}], "name": "Daft Punk"})
        backend.set("wikidata.org", {"entities": {"Q1": {"sitelinks": {"enwiki": {"title": "Daft Punk"}}}}})
        backend.set("en.wikipedia.org", {"extract": "Daft Punk were a French duo. Two."})
        backend.set("lb-radio", {"tracks": [{"similar_artist_mbid": "mb-s1", "similar_artist_name": "Justice", "total_listen_count": 2}]})
        backend.set("musicbrainz.org/ws/2/release", {"releases": [{
            "id": "rel-1", "title": "Homework", "artist-credit": [{"name": "Daft Punk"}], "score": 100,
            "country": "FR", "date": "1997", "label-info": [{"label": {"name": "Virgin"}}],
        }]}, has=["query"])
        backend.set("/ws/2/release/rel-1", {"id": "rel-1", "country": "FR", "date": "1997",
            "release-group": {"id": "rg-1", "primary-type": "Album", "first-release-date": "1997-01-20"},
            "artist-credit": [{"artist": {"id": "mb-1"}}], "label-info": [{"label": {"name": "Virgin"}}],
            "tags": [{"name": "electronic", "count": 9}]})
        backend.set("/ws/2/release-group/rg-1", {"primary-type": "Album", "first-release-date": "1997-01-20", "tags": [{"name": "electronic", "count": 9}]})
        return backend

    async def test_artist_detail_includes_enrichment(self):
        enrichment = ArtistEnrichmentService(db_path=Path(tempfile.mkdtemp()) / "e.sqlite", request_backend=self._backend())
        provider = TidalProvider(artist_enrichment=enrichment)
        with mock.patch.object(catalog, "get_artist", return_value={
            "id": "tid-1", "name": "Daft Punk", "art_url": "https://c/1.jpg",
            "albums": [{"id": "a1", "title": "Homework"}],
            "top_tracks": [{"id": "t1", "title": "Around the World"}],
        }) as get_artist:
            data = await provider.get_artist("tid-1")
        get_artist.assert_called_once_with("tid-1")
        self.assertEqual(data["name"], "Daft Punk")
        enrichment_data = data["enrichment"]
        self.assertTrue(enrichment_data["available"])
        self.assertEqual(enrichment_data["mb_artist_id"], "mb-1")
        self.assertEqual(enrichment_data["about"], "Daft Punk were a French duo. Two.")
        self.assertEqual(enrichment_data["similar"][0]["artist"], "Justice")

    async def test_album_detail_enriches_but_keeps_tidal_primary(self):
        # Test #7: the TIDAL album payload is untouched at the top level; the
        # enrichment only carries additive fields the provider lacks.
        enrichment = ArtistEnrichmentService(db_path=Path(tempfile.mkdtemp()) / "e.sqlite", request_backend=self._backend())
        provider = TidalProvider(artist_enrichment=enrichment)
        with mock.patch.object(catalog, "get_album", return_value={
            "id": "al-1", "title": "Homework", "artist": "Daft Punk", "artist_id": "tid-1",
            "year": 1997, "audio_quality": "LOSSLESS", "num_tracks": 16, "art_url": "https://c/a.jpg",
        }):
            data = await provider.get_album("al-1")
        self.assertEqual(data["year"], 1997)
        self.assertEqual(data["audio_quality"], "LOSSLESS")
        self.assertEqual(data["num_tracks"], 16)
        enrich = data["enrichment"]
        self.assertTrue(enrich["available"])
        self.assertTrue(enrich["artist"]["mapped"])
        self.assertIn("release_type", enrich["supplement"])
        self.assertIn("label", enrich["supplement"])
        self.assertIn("genres", enrich["supplement"])
        # No duplicate: year/quality are TIDAL-only and never appear as supplement.
        self.assertNotIn("year", enrich["supplement"])
        self.assertNotIn("audio_quality", enrich["supplement"])

    async def test_artist_enrichment_failure_never_breaks_tidal_data(self):
        # Test #11: enrichment timeouts/errors leave the TIDAL artist data intact.
        broken = ArtistEnrichmentService(db_path=Path(tempfile.mkdtemp()) / "e.sqlite")
        with mock.patch.object(broken, "enriched_artist", side_effect=RuntimeError("enrichment timeout")):
            provider = TidalProvider(artist_enrichment=broken)
            with mock.patch.object(catalog, "get_artist", return_value={
                "id": "tid-1", "name": "Daft Punk", "albums": [], "top_tracks": [],
            }):
                data = await provider.get_artist("tid-1")
        self.assertEqual(data["name"], "Daft Punk")
        self.assertFalse(data["enrichment"]["available"])
        self.assertIn("timeout", data["enrichment"]["error"])

    async def test_album_enrichment_failure_never_breaks_tidal_data(self):
        broken = ArtistEnrichmentService(db_path=Path(tempfile.mkdtemp()) / "e.sqlite")
        with mock.patch.object(broken, "enriched_album", side_effect=RuntimeError("timeout")):
            provider = TidalProvider(artist_enrichment=broken)
            with mock.patch.object(catalog, "get_album", return_value={
                "id": "al-1", "title": "H", "artist": "A", "year": 1990,
            }):
                data = await provider.get_album("al-1")
        self.assertEqual(data["title"], "H")
        self.assertFalse(data["enrichment"]["available"])

    async def test_status_refresh_never_triggers_enrichment(self):
        # Test #10: provider status (polled by the UI every few seconds) must
        # not call into enrichment at all.
        enrichment = ArtistEnrichmentService(db_path=Path(tempfile.mkdtemp()) / "e.sqlite", request_backend=self._backend())
        provider = TidalProvider(artist_enrichment=enrichment)
        provider.configure(lambda: {})
        with mock.patch.object(enrichment, "enriched_artist", wraps=enrichment.enriched_artist) as wrapped:
            for _ in range(3):
                await provider.status()
        wrapped.assert_not_called()

    async def test_repeated_artist_views_stay_cached(self):
        # Opening the same artist twice must not re-trigger enrichment requests.
        backend = self._backend()
        enrichment = ArtistEnrichmentService(db_path=Path(tempfile.mkdtemp()) / "e.sqlite", request_backend=backend)
        provider = TidalProvider(artist_enrichment=enrichment)
        with mock.patch.object(catalog, "get_artist", return_value={
            "id": "tid-1", "name": "Daft Punk", "albums": [{"id": "a1", "title": "Homework"}],
            "top_tracks": [],
        }):
            first = await provider.get_artist("tid-1")
            calls_after_first = len(backend.calls)
            second = await provider.get_artist("tid-1")
        self.assertTrue(first["enrichment"]["available"])
        self.assertTrue(second["enrichment"]["cached"])
        self.assertEqual(len(backend.calls), calls_after_first, "a reopened artist must not re-fetch enrichment")


if __name__ == "__main__":
    unittest.main()
