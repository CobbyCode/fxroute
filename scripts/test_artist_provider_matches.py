#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only

"""Local artist -> streaming provider mapping used by the Library Artists view.

Covers the stored-mapping resolver (MBID first, unique name second, never an
ambiguous or MBID-contradicting guess), the album discover endpoint attaching
provider mappings on request, and the batch endpoint answering by position.
"""

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import library.api as library_api  # noqa: E402
from artist_enrichment import ArtistEnrichmentService  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from library.api import LibraryApiRuntime, configure_runtime  # noqa: E402

NOW = "2026-01-01T00:00:00+00:00"


def _service() -> ArtistEnrichmentService:
    return ArtistEnrichmentService(db_path=Path(tempfile.mkdtemp()) / "e.sqlite", request_backend=None)


def _map(svc, provider_id, name, *, mbid=None, art="", provider="tidal", state="mapped", matched_at=NOW):
    svc.store.set_provider_artist(
        provider,
        provider_id,
        name=name,
        art_url=art,
        mb_artist_id=mbid,
        canonical_name=name,
        match_state=state,
        attempted_at=NOW,
        matched_at=matched_at,
        error=None,
    )


class ProviderMatchTests(unittest.TestCase):
    def test_mbid_hit_wins_over_name(self):
        svc = _service()
        _map(svc, "t-1", "Björk", mbid="mb-bjork", art="https://img/1")
        _map(svc, "t-2", "Bjork", mbid="mb-other")
        got = svc.provider_matches("tidal", [{"name": "Bjork", "mbid": "mb-bjork"}])
        self.assertEqual(got[0]["provider_artist_id"], "t-1")
        self.assertEqual(got[0]["art_url"], "https://img/1")

    def test_unique_name_matches_ignoring_accents_and_case(self):
        svc = _service()
        _map(svc, "t-1", "Beyoncé", art="https://img/b")
        got = svc.provider_matches("tidal", [{"name": "BEYONCE"}])
        self.assertEqual(got[0]["provider_artist_id"], "t-1")

    def test_ambiguous_name_is_not_guessed(self):
        svc = _service()
        _map(svc, "t-1", "Nirvana", mbid="mb-1")
        _map(svc, "t-2", "Nirvana", mbid="mb-2")
        self.assertEqual(svc.provider_matches("tidal", [{"name": "Nirvana"}]), [None])

    def test_name_hit_with_contradicting_mbid_is_dropped(self):
        svc = _service()
        _map(svc, "t-1", "Genesis", mbid="mb-uk-band")
        self.assertEqual(svc.provider_matches("tidal", [{"name": "Genesis", "mbid": "mb-us-band"}]), [None])
        # An unmapped MBID on either side cannot contradict.
        got = svc.provider_matches("tidal", [{"name": "Genesis"}])
        self.assertEqual(got[0]["provider_artist_id"], "t-1")

    def test_only_confident_mappings_of_the_provider_count(self):
        svc = _service()
        _map(svc, "t-1", "Portishead", state="unmatched")
        _map(svc, "q-1", "Portishead", provider="qobuz")
        self.assertEqual(svc.provider_matches("tidal", [{"name": "Portishead"}]), [None])

    def test_answers_by_position_and_survives_odd_input(self):
        svc = _service()
        _map(svc, "t-1", "Air")
        got = svc.provider_matches("tidal", [{"name": "Air"}, {}, {"name": ""}, {"name": "Unknown"}])
        self.assertEqual([bool(item) for item in got], [True, False, False, False])

    def test_non_latin_names_match_by_their_own_key(self):
        svc = _service()
        _map(svc, "t-1", "Мумий Тролль")
        _map(svc, "t-2", "Сплин")
        got = svc.provider_matches("tidal", [{"name": "сплин"}, {"name": "Другой"}])
        self.assertEqual(got[0]["provider_artist_id"], "t-2")
        self.assertIsNone(got[1])

    def test_attach_provider_mappings_marks_mapped_items_only(self):
        svc = _service()
        _map(svc, "t-1", "Justice", mbid="mb-j", art="https://img/j")
        items = svc.attach_provider_mappings("tidal", [
            {"type": "artist", "artist": "Justice", "artist_mbid": "mb-j"},
            {"type": "artist", "artist": "Nobody", "artist_mbid": "mb-n"},
        ])
        self.assertEqual(items[0]["provider_artist_id"], "t-1")
        self.assertEqual(items[0]["art_url"], "https://img/j")
        self.assertNotIn("provider_artist_id", items[1])


class _FakeStore:
    def __init__(self, svc, items):
        self.artist_enrichment = svc
        self._items = items

    def get_album_discover(self, album_id, force=False):
        return {"items": [dict(item) for item in self._items], "source": "artist", "cached": True}


class _FakeScanner:
    def __init__(self, svc, items):
        self.metadata_store = _FakeStore(svc, items)

    def get_album_tracks(self, album_id, authoritative=False):
        return [SimpleNamespace(id="t")] if album_id == "known" else []

    def get_album_discover(self, album_id, force=False):
        return self.metadata_store.get_album_discover(album_id, force)


class _FakeRequest:
    def __init__(self, body):
        self._body = body

    async def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class RouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.svc = _service()
        _map(self.svc, "t-1", "Justice", mbid="mb-j", art="https://img/j")
        self.scanner = _FakeScanner(self.svc, [
            {"type": "artist", "artist": "Justice", "artist_mbid": "mb-j"},
            {"type": "artist", "artist": "Nobody", "artist_mbid": "mb-n"},
        ])
        self.original_runtime = library_api._runtime
        configure_runtime(LibraryApiRuntime(get_scanner=lambda: self.scanner, get_settings=lambda: None))

    async def asyncTearDown(self):
        library_api._runtime = self.original_runtime

    async def test_discover_without_provider_stays_plain(self):
        resp = await library_api.get_album_discover("known")
        self.assertNotIn("provider_artist_id", resp["items"][0])

    async def test_discover_with_provider_attaches_mappings(self):
        resp = await library_api.get_album_discover("known", provider="TIDAL")
        self.assertEqual(resp["items"][0]["provider_artist_id"], "t-1")
        self.assertNotIn("provider_artist_id", resp["items"][1])

    async def test_discover_unknown_album_is_404(self):
        with self.assertRaises(HTTPException) as ctx:
            await library_api.get_album_discover("missing", provider="tidal")
        self.assertEqual(ctx.exception.status_code, 404)

    async def test_batch_endpoint_answers_by_position(self):
        resp = await library_api.get_artist_provider_matches(_FakeRequest({
            "provider": "tidal",
            "artists": [{"name": "Justice"}, {"name": "Nobody"}],
        }))
        self.assertEqual(resp["provider"], "tidal")
        self.assertEqual(resp["matches"][0]["provider_artist_id"], "t-1")
        self.assertIsNone(resp["matches"][1])

    async def test_batch_endpoint_validates_the_body(self):
        for body in ({}, {"provider": "tidal"}, {"provider": "tidal", "artists": "x"}, {"artists": []}):
            with self.assertRaises(HTTPException) as ctx:
                await library_api.get_artist_provider_matches(_FakeRequest(body))
            self.assertEqual(ctx.exception.status_code, 400, body)
        too_many = {"provider": "tidal", "artists": [{}] * (library_api.ARTIST_MATCH_MAX_ARTISTS + 1)}
        with self.assertRaises(HTTPException) as ctx:
            await library_api.get_artist_provider_matches(_FakeRequest(too_many))
        self.assertEqual(ctx.exception.status_code, 400)


if __name__ == "__main__":
    unittest.main()
