#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only

"""Focused tests for the shared artist-enrichment owner and its TIDAL wiring.

Covers the shared SQLite caches (canonical artist, provider mapping, reverse
lookup), safe matching fallbacks, library-store delegation, the TIDAL provider
enrichment composition and the guarantee that enrichment failures/refreshes
never disturb TIDAL catalog data.
"""

import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import artist_enrichment as ae  # noqa: E402
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


def _release_search(*candidates: dict) -> dict:
    return {"releases": [dict(candidate) for candidate in candidates]}


def _release_detail(release_id: str, *, year: str, country: str, track_count: int, rg: str) -> dict:
    return {
        "id": release_id,
        "date": year,
        "country": country,
        "track-count": track_count,
        "release-group": {"id": rg, "primary-type": "Album", "first-release-date": year},
        "artist-credit": [{"artist": {"id": "mb-1"}}],
        "label-info": [{"label": {"name": "A Label"}}],
        "tags": [{"name": "electronic", "count": 9}],
    }


def _artist_backend() -> FakeBackend:
    """Backend answering bare artist resolution (about/similar degrade quietly)."""
    backend = FakeBackend()
    backend.set("musicbrainz.org/ws/2/artist", _artist_search("Daft Punk", {"id": "mb-1", "score": 100}), has=["query"])
    backend.set("musicbrainz.org/ws/2/artist/mb-1", {"relations": [], "name": "Daft Punk"})
    backend.set("wikidata.org", {"entities": {}})
    backend.set("lb-radio", {"tracks": []})
    return backend


class ReleaseIdentityTests(unittest.TestCase):
    """Releases are cached per provider album id, never per normalized title.

    Tests #1-#3: distinct editions keep distinct mappings and never receive the
    other edition's cached supplement, because identity is (provider,
    provider_release_id) -> mb_release_id -> canonical release row.
    """

    def _daft_backend(self) -> FakeBackend:
        backend = _artist_backend()
        backend.set(
            "musicbrainz.org/ws/2/release",
            _release_search(
                {"id": "rel-1997", "title": "Homework", "artist-credit": [{"name": "Daft Punk"}], "score": 100, "date": "1997-01-20"},
                {"id": "rel-2012", "title": "Homework", "artist-credit": [{"name": "Daft Punk"}], "score": 100, "date": "2012-01-01"},
            ),
            has=["query"],
        )
        backend.set("/ws/2/release/rel-1997", _release_detail("rel-1997", year="1997-01-20", country="FR", track_count=16, rg="rg-1997"))
        backend.set("/ws/2/release/rel-2012", _release_detail("rel-2012", year="2012-01-01", country="US", track_count=16, rg="rg-2012"))
        backend.set("/ws/2/release-group/rg-1997", {"primary-type": "Album", "first-release-date": "1997-01-20", "tags": [{"name": "electronic", "count": 9}]})
        backend.set("/ws/2/release-group/rg-2012", {"primary-type": "Album", "first-release-date": "2012-01-01", "tags": [{"name": "electronic", "count": 9}]})
        return backend

    def test_distinct_editions_get_distinct_release_mappings(self):
        # Two TIDAL albums sharing one normalized (artist, album) title but with
        # different stable TIDAL album ids and different years must resolve to
        # distinct canonical releases and never share a cache entry.
        backend = self._daft_backend()
        svc = _mk_tmp_service(backend)

        original = svc.enriched_album("tidal", "tid-album-1", "Homework", "Daft Punk",
                                      provider_artist_id="tid-art-1", year=1997, num_tracks=16)
        remastered = svc.enriched_album("tidal", "tid-album-2", "Homework (Remastered)", "Daft Punk",
                                        provider_artist_id="tid-art-1", year=2012, num_tracks=16)

        row1 = svc.store.get_provider_release("tidal", "tid-album-1")
        row2 = svc.store.get_provider_release("tidal", "tid-album-2")
        self.assertEqual(row1["mb_release_id"], "rel-1997")
        self.assertEqual(row2["mb_release_id"], "rel-2012")
        self.assertNotEqual(row1["mb_release_id"], row2["mb_release_id"],
                            "editions must not collapse onto one cache key")
        # Each official edition gets its own edition's supplement, never the
        # other one's.
        self.assertEqual(original["supplement"].get("country"), "FR")
        self.assertEqual(remastered["supplement"].get("country"), "US")

    def test_same_provider_album_is_a_cache_hit_without_new_requests(self):
        # Test #4: the same provider album id reuses its mapping + canonical
        # release and issues no new MusicBrainz request.
        backend = self._daft_backend()
        svc = _mk_tmp_service(backend)
        first = svc.enriched_album("tidal", "tid-album-1", "Homework", "Daft Punk",
                                   provider_artist_id="tid-art-1", year=1997)
        calls_after_first = len(backend.calls)
        second = svc.enriched_album("tidal", "tid-album-1", "Homework", "Daft Punk",
                                    provider_artist_id="tid-art-1", year=1997)
        self.assertEqual(len(backend.calls), calls_after_first,
                         "a mapped provider album must be a pure cache hit")
        self.assertEqual(first["supplement"], second["supplement"])

    def test_track_count_disambiguates_when_year_is_unhelpful(self):
        # Test #5: with no usable year, a bounded track-count tiebreak picks the
        # candidate whose track count equals the provider value.
        backend = _artist_backend()
        backend.set(
            "musicbrainz.org/ws/2/release",
            _release_search(
                {"id": "rel-a", "title": "Homework", "artist-credit": [{"name": "Daft Punk"}], "score": 100, "date": ""},
                {"id": "rel-b", "title": "Homework", "artist-credit": [{"name": "Daft Punk"}], "score": 100, "date": ""},
            ),
            has=["query"],
        )
        backend.set("/ws/2/release/rel-a", _release_detail("rel-a", year="", country="DE", track_count=10, rg="rg-a"))
        backend.set("/ws/2/release/rel-b", _release_detail("rel-b", year="", country="UK", track_count=12, rg="rg-b"))
        backend.set("/ws/2/release-group/rg-b", {"primary-type": "Album", "tags": [{"name": "electronic", "count": 9}]})
        svc = _mk_tmp_service(backend)

        result = svc.enriched_album("tidal", "tid-album-1", "Homework", "Daft Punk",
                                    provider_artist_id="tid-art-1", num_tracks=12)
        row = svc.store.get_provider_release("tidal", "tid-album-1")
        self.assertEqual(row["mb_release_id"], "rel-b")
        self.assertEqual(result["supplement"].get("country"), "UK")

    def test_unsure_release_match_yields_no_supplement(self):
        # Test #6: when equal signals cannot separate two editions, no guessed
        # supplement is delivered.
        backend = _artist_backend()
        backend.set(
            "musicbrainz.org/ws/2/release",
            _release_search(
                {"id": "rel-a", "title": "Homework", "artist-credit": [{"name": "Daft Punk"}], "score": 100, "date": ""},
                {"id": "rel-b", "title": "Homework", "artist-credit": [{"name": "Daft Punk"}], "score": 100, "date": ""},
            ),
            has=["query"],
        )
        backend.set("/ws/2/release/rel-a", _release_detail("rel-a", year="", country="DE", track_count=16, rg="rg-a"))
        backend.set("/ws/2/release/rel-b", _release_detail("rel-b", year="", country="UK", track_count=16, rg="rg-b"))
        svc = _mk_tmp_service(backend)

        result = svc.enriched_album("tidal", "tid-album-1", "Homework", "Daft Punk",
                                    provider_artist_id="tid-art-1", num_tracks=16)
        self.assertEqual(result["supplement"], {})
        row = svc.store.get_provider_release("tidal", "tid-album-1")
        self.assertEqual(row["match_state"], "ambiguous")
        self.assertIsNone(row["mb_release_id"])


class MappingVersionTests(unittest.TestCase):
    """provider_artists matcher versioning + force_rematch semantics."""

    def _seed_mapped(self, svc, *, provider_artist_id="tid-1", mb_artist_id="mb-old", version):
        now = ae._utc_now()
        svc.store.set_provider_artist(
            "tidal", provider_artist_id, name="Daft Punk", art_url="https://c/1.jpg",
            mb_artist_id=mb_artist_id, canonical_name="Daft Punk",
            match_state="mapped", attempted_at=now, matched_at=now, error=None,
            mapping_version=version,
        )
        # Seed the about/similar caches so the mapped path stays request-free.
        svc.store.upsert_artist_description(mb_artist_id, "Daft Punk", "cached bio", attempted_at=now, error=None)
        svc.store.upsert_artist_similar(mb_artist_id, [], attempted_at=now, error=None)

    def _rematch_backend(self, new_mbid="mb-new") -> FakeBackend:
        backend = FakeBackend()
        backend.set("musicbrainz.org/ws/2/artist", _artist_search("Daft Punk", {"id": new_mbid, "score": 100}), has=["query"])
        backend.set(f"musicbrainz.org/ws/2/artist/{new_mbid}", {"relations": [], "name": "Daft Punk"})
        backend.set("wikidata.org", {"entities": {}})
        backend.set("lb-radio", {"tracks": []})
        return backend

    def test_mapped_current_version_is_a_pure_cache_hit(self):
        # Test #1: mapped + current mapping_version -> no network request at all.
        backend = FakeBackend()  # any network call raises
        svc = _mk_tmp_service(backend)
        self._seed_mapped(svc, version=ae.MATCHER_VERSION)
        result = svc.enriched_artist("tidal", "tid-1", "Daft Punk")
        self.assertTrue(result["available"])
        self.assertTrue(result["cached"])
        self.assertEqual(result["mb_artist_id"], "mb-old")
        self.assertEqual(backend.calls, [], "current-version mapping must not hit the network")

    def test_older_version_triggers_exactly_one_rematch(self):
        # Test #2 + #3: mapped + older version -> exactly one matcher run; the
        # successful rematch stores the current version.
        backend = self._rematch_backend()
        svc = _mk_tmp_service(backend)
        self._seed_mapped(svc, version=0)
        result = svc.enriched_artist("tidal", "tid-1", "Daft Punk")
        search_calls = [c for c in backend.calls if "query" in (c[1] or {})]
        self.assertEqual(len(search_calls), 1, "an older mapping must be re-matched exactly once")
        self.assertEqual(result["mb_artist_id"], "mb-new")
        row = svc.store.get_provider_artist("tidal", "tid-1")
        self.assertEqual(row["mb_artist_id"], "mb-new")
        self.assertEqual(row["mapping_version"], ae.MATCHER_VERSION)

    def test_force_rematch_reruns_even_at_current_version(self):
        # Test #4: force_rematch re-resolves despite a current mapping version.
        backend = self._rematch_backend()
        svc = _mk_tmp_service(backend)
        self._seed_mapped(svc, version=ae.MATCHER_VERSION)
        result = svc.enriched_artist("tidal", "tid-1", "Daft Punk", force_rematch=True)
        search_calls = [c for c in backend.calls if "query" in (c[1] or {})]
        self.assertEqual(len(search_calls), 1)
        self.assertEqual(result["mb_artist_id"], "mb-new")

    def test_failed_version_rematch_preserves_mapping_and_old_version(self):
        # Test #1: transient failure during a version-rematch must keep the old
        # mapping AND the old mapping_version (a failed network call must not be
        # recorded as a completed matcher run).
        backend = FakeBackend()  # the matcher request raises -> transient failure
        svc = _mk_tmp_service(backend)
        self._seed_mapped(svc, version=0)
        result = svc.enriched_artist("tidal", "tid-1", "Daft Punk")
        self.assertTrue(result["available"], "the existing mapping must keep rendering")
        self.assertEqual(result["mb_artist_id"], "mb-old")
        row = svc.store.get_provider_artist("tidal", "tid-1")
        self.assertEqual(row["mb_artist_id"], "mb-old")
        self.assertEqual(row["match_state"], "mapped")
        self.assertEqual(row["mapping_version"], 0, "a transient failure must keep the old version")
        self.assertTrue(row["error"], "the transient failure must be recorded")
        self.assertTrue(row["attempted_at"], "the attempt time must be recorded")

    def test_transient_rematch_within_cooldown_suppresses_retry(self):
        # Test #2: the direct follow-up inside the transient cooldown makes no
        # new request and still serves the old mapping.
        backend = FakeBackend()
        svc = _mk_tmp_service(backend)
        self._seed_mapped(svc, version=0)
        svc.enriched_artist("tidal", "tid-1", "Daft Punk")  # first attempt -> transient failure
        backend.calls.clear()
        again = svc.enriched_artist("tidal", "tid-1", "Daft Punk")
        self.assertTrue(again["available"])
        self.assertEqual(again["mb_artist_id"], "mb-old")
        self.assertEqual(backend.calls, [], "the transient cooldown must suppress an immediate retry")

    def test_transient_rematch_retried_after_cooldown_expiry(self):
        # Test #3: once the transient cooldown has elapsed the version-rematch
        # is attempted again (exactly once) and still preserves the mapping.
        backend = FakeBackend()
        svc = _mk_tmp_service(backend)
        self._seed_mapped(svc, version=0)
        # Simulate the earlier transient failure > the 1h transient cooldown ago.
        past = (datetime.now(timezone.utc) - timedelta(hours=2)).replace(microsecond=0).isoformat()
        svc.store.set_provider_artist(
            "tidal", "tid-1", name="Daft Punk", art_url="https://c/1.jpg",
            mb_artist_id="mb-old", canonical_name="Daft Punk",
            match_state="mapped", attempted_at=past, matched_at=past,
            error="503 old", mapping_version=0,
        )
        result = svc.enriched_artist("tidal", "tid-1", "Daft Punk")
        self.assertEqual(len(backend.calls), 1, "an expired cooldown must re-attempt the version-rematch")
        self.assertEqual(result["mb_artist_id"], "mb-old")
        row = svc.store.get_provider_artist("tidal", "tid-1")
        self.assertEqual(row["match_state"], "mapped")
        self.assertEqual(row["mapping_version"], 0, "the transient failure keeps the old version")

    def test_deterministic_decline_keeps_mapping_and_stamps_version(self):
        # Documented semantics: a *completed* matcher run that finds no safer
        # replacement (here: unmatched) preserves the mapping but stamps it at
        # the current version — the verdict of a finished run, not a network
        # failure — so it is not re-rematched on every open.
        backend = FakeBackend()
        backend.set("musicbrainz.org/ws/2/artist", {"artists": []}, has=["query"])
        svc = _mk_tmp_service(backend)
        self._seed_mapped(svc, version=0)
        result = svc.enriched_artist("tidal", "tid-1", "Daft Punk")
        self.assertTrue(result["available"])
        self.assertEqual(result["mb_artist_id"], "mb-old")
        row = svc.store.get_provider_artist("tidal", "tid-1")
        self.assertEqual(row["match_state"], "mapped")
        self.assertEqual(row["mb_artist_id"], "mb-old")
        self.assertEqual(row["mapping_version"], ae.MATCHER_VERSION)
        self.assertEqual(row["error"], "no safe MusicBrainz match")
        # No further rematch on a direct follow-up (version is now current).
        backend.calls.clear()
        svc.enriched_artist("tidal", "tid-1", "Daft Punk")
        self.assertEqual(backend.calls, [])

    def test_ambiguous_cooldown_still_applies(self):
        # Test #6: non-mapped cooldowns stay intact for ambiguous rows.
        backend = FakeBackend()
        svc = _mk_tmp_service(backend)
        now = ae._utc_now()
        svc.store.set_provider_artist(
            "tidal", "tid-1", name="Jungle", art_url="", mb_artist_id=None,
            canonical_name=None, match_state="ambiguous", attempted_at=now,
            matched_at=None, error="ambiguous MusicBrainz match", mapping_version=0,
        )
        result = svc.enriched_artist("tidal", "tid-1", "Jungle")
        self.assertFalse(result["available"])
        self.assertEqual(result["match_state"], "ambiguous")
        self.assertEqual(backend.calls, [], "ambiguous rows must be cooled down")


class SchemaMigrationTests(unittest.TestCase):
    """Test #7: an existing DB without the new columns/tables migrates
    additively and the legacy release rows are preserved, not destroyed."""

    def _old_db(self, db_path: Path) -> None:
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute(
                """
                CREATE TABLE provider_artists (
                    provider TEXT NOT NULL,
                    provider_artist_id TEXT NOT NULL,
                    name TEXT,
                    art_url TEXT,
                    mb_artist_id TEXT,
                    canonical_name TEXT,
                    match_state TEXT,
                    attempted_at TEXT,
                    matched_at TEXT,
                    error TEXT,
                    PRIMARY KEY (provider, provider_artist_id)
                )
                """
            )
            conn.execute(
                "INSERT INTO provider_artists VALUES ('tidal','tid-1','Daft Punk','https://c/1.jpg',"
                "'mb-O','Daft Punk','mapped','2026-01-01T00:00:00+00:00','2026-01-01T00:00:00+00:00',NULL)"
            )
            conn.execute(
                """
                CREATE TABLE releases (
                    release_key TEXT PRIMARY KEY,
                    album TEXT,
                    artist TEXT,
                    mb_artist_id TEXT,
                    mb_release_id TEXT,
                    mb_release_group_id TEXT,
                    release_type TEXT,
                    year INTEGER,
                    country TEXT,
                    label TEXT,
                    genres_json TEXT DEFAULT '[]',
                    artist_description TEXT,
                    album_description TEXT,
                    attempted_at TEXT,
                    error TEXT
                )
                """
            )
            conn.execute("INSERT INTO releases (release_key, label) VALUES ('homework::daft punk', 'Old Label')")
            conn.execute(
                """
                CREATE TABLE artists (
                    mb_artist_id TEXT PRIMARY KEY,
                    name TEXT,
                    description TEXT,
                    description_attempted_at TEXT,
                    description_error TEXT,
                    similar_json TEXT DEFAULT '[]',
                    similar_attempted_at TEXT,
                    similar_error TEXT,
                    updated_at TEXT
                )
                """
            )
            conn.commit()
        finally:
            conn.close()

    def test_old_db_migrates_additively_and_preserves_legacy_rows(self):
        db_path = Path(tempfile.mkdtemp()) / "old.sqlite"
        self._old_db(db_path)

        store = ArtistEnrichmentService(db_path=db_path).store
        # Additive columns/tables exist.
        with store._connect() as conn:
            pa_cols = {r["name"] for r in conn.execute("PRAGMA table_info(provider_artists)").fetchall()}
            self.assertIn("mapping_version", pa_cols)
            legacy = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='releases_legacy'").fetchone()
            self.assertIsNotNone(legacy, "legacy releases table must be preserved (renamed)")
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM releases_legacy").fetchone()[0], 1)
            rel_cols = {r["name"] for r in conn.execute("PRAGMA table_info(releases)").fetchall()}
            self.assertIn("mb_release_id", rel_cols)
            self.assertNotIn("release_key", rel_cols)
            pr_exists = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='provider_releases'").fetchone()
            self.assertIsNotNone(pr_exists)

        # A pre-existing mapped row (mapping_version 0) is usable and re-resolves
        # once with the current matcher (older version -> rematch).
        backend = FakeBackend()
        backend.set("musicbrainz.org/ws/2/artist", _artist_search("Daft Punk", {"id": "mb-NEW", "score": 100}), has=["query"])
        backend.set("musicbrainz.org/ws/2/artist/mb-NEW", {"relations": [], "name": "Daft Punk"})
        backend.set("wikidata.org", {"entities": {}})
        backend.set("lb-radio", {"tracks": []})
        svc = ArtistEnrichmentService(db_path=db_path, request_backend=backend)
        row_before = svc.store.get_provider_artist("tidal", "tid-1")
        self.assertEqual(row_before["mapping_version"], 0)
        result = svc.enriched_artist("tidal", "tid-1", "Daft Punk")
        self.assertEqual(result["mb_artist_id"], "mb-NEW")
        self.assertEqual(svc.store.get_provider_artist("tidal", "tid-1")["mapping_version"], ae.MATCHER_VERSION)


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
