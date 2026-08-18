#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only
// Structural checks for the shared streaming UI.
//
// These verify the new UI is capability-driven rather than branching on
// provider identity for general controls, and that the served page shell
// actually wires up the three provider tabs and the streaming module.

const assert = require('assert/strict');
const fs = require('fs');
const path = require('path');

const js = fs.readFileSync(path.join(__dirname, '..', 'static', 'streaming.js'), 'utf8');
const html = fs.readFileSync(path.join(__dirname, '..', 'static', 'index.html'), 'utf8');
const css = fs.readFileSync(path.join(__dirname, '..', 'static', 'style.css'), 'utf8');

// --- capability-gated rendering -------------------------------------------------
// The shared now-playing card must read `caps.<name>` for every control
// cluster, so a provider with a different capability surface renders the same
// way without any `if (provider === ...)` branching in the render path.
const requiredCapabilityGates = [
    'caps.transport',
    'caps.seek',
    'caps.shuffle',
    'caps.loop',
    'caps.progress',
    'caps.cover',
];
for (const gate of requiredCapabilityGates) {
    assert.ok(js.includes(gate), `streaming.js must gate rendering on ${gate}`);
}

// --- no provider-identity branching in the renderer -----------------------------
// The render path (renderNowPlaying) must not hard-code provider ids. Provider
// ids may only appear in the transport adapter map and in provider-specific
// content dispatch, so extract renderNowPlaying and check it stays identity-free.
function extractFunction(source, name) {
    const match = new RegExp(`function\\s+${name}\\s*\\(`).exec(source);
    assert.ok(match, `missing ${name}`);
    const brace = source.indexOf('{', match.index);
    let depth = 0, quote = '', escaped = false;
    for (let i = brace; i < source.length; i += 1) {
        const c = source[i];
        if (quote) {
            if (escaped) escaped = false;
            else if (c === '\\') escaped = true;
            else if (c === quote) quote = '';
            continue;
        }
        if ('\'"`'.includes(c)) quote = c;
        else if (c === '{') depth += 1;
        else if (c === '}' && --depth === 0) return source.slice(match.index, i + 1);
    }
    throw new Error(`unterminated ${name}`);
}

const renderNowPlaying = extractFunction(js, 'renderNowPlaying');
assert.ok(!/providerId\s*===/.test(renderNowPlaying), 'renderNowPlaying must not branch on provider id');
assert.ok(!/\bproviderId\s*==/.test(renderNowPlaying), 'renderNowPlaying must not branch on provider id');

// The provider ids are confined to the transport adapter map.
assert.ok(/const TRANSPORT\s*=/.test(js), 'transport adapter map must exist');
assert.ok(js.includes("spotify: { kind: 'app'"), 'spotify transport adapter');
assert.ok(js.includes("qobuz: { kind: 'remote'"), 'qobuz transport adapter');
assert.ok(js.includes("tidal: { kind: 'native'"), 'tidal transport adapter');

// --- quality facts live in the shared footer meta-tag, not in the card --------
// The now-playing card must no longer carry its own quality badge: the stream
// facts render once, in the global footer, through the same meta-tag renderer
// as library/radio (app.js formatStreamingMetaLine -> formatRadioStreamLine).
assert.ok(!js.includes('.streaming-quality'),
    'streaming card must not render a quality badge');
assert.ok(!js.includes('formatQuality'),
    'streaming module must not own a quality formatter');
const appJs = fs.readFileSync(path.join(__dirname, '..', 'static', 'app.js'), 'utf8');
assert.ok(appJs.includes('function formatStreamingMetaLine('),
    'app.js must host the shared streaming footer meta renderer');
assert.ok(appJs.includes("formatStreamingMetaLine(data)"),
    'streaming footer must render through the shared meta-tag renderer');
assert.ok(!appJs.includes('renderStreamingFooterMeta'),
    'no UI-side remember-last-string caching of the footer meta tag');

// --- queue continuation line is data-driven ----------------------------------
// The queue line (count + next up) must render from normalized provider data
// (queue_len/queue_index/next_track) without any provider-identity branch.
const nowPlayingBody = js.slice(js.indexOf('function renderNowPlaying'), js.indexOf('function showEmpty'));
assert.ok(nowPlayingBody.includes('formatQueueInfo(data)'),
    'renderNowPlaying must render the queue info line');
assert.ok(nowPlayingBody.includes('els.queueInfo'),
    'renderNowPlaying must write the queue info element');
assert.ok(js.includes("'<div class=\"streaming-queue\" hidden></div>'"),
    'now-playing card must carry a queue info element');

const formatQueueInfo = new Function('return ' + extractFunction(js, 'formatQueueInfo'))();
assert.equal(formatQueueInfo({ queue_len: 7, queue_index: 3, next_track: { title: 'Next One' } }),
    '3/7 · Next: Next One');
assert.equal(formatQueueInfo({ queue_len: 7, next_track: {} }), '1/7');
assert.equal(formatQueueInfo({ queue_len: 7, queue_index: 2, next_track: {} }), '2/7');
assert.equal(formatQueueInfo({ queue_len: 1 }), '');
assert.equal(formatQueueInfo({}), '');
assert.ok(!/providerId/.test(extractFunction(js, 'formatQueueInfo')),
    'queue info formatting must not branch on provider id');

// --- served shell wires the three provider tabs + the module ----------------------
for (const provider of ['spotify', 'qobuz', 'tidal']) {
    assert.ok(html.includes(`data-provider="${provider}"`), `index.html must ship a ${provider} shell`);
    assert.ok(html.includes(`data-tab="${provider}"`), `index.html must ship a ${provider} tab button`);
    assert.ok(html.includes(`id="tab-${provider}"`), `index.html must ship a ${provider} tab panel`);
}
assert.ok(html.includes('/static/streaming.js?v='), 'index.html must include streaming.js');

// --- TIDAL login flow is present (PKCE default + device alternative) --------------
assert.ok(js.includes('auth/pkce'), 'PKCE login endpoint used');
assert.ok(js.includes('auth/device'), 'device login endpoint used');
assert.ok(js.includes('Device login (limited to AAC 320 kbps)'), 'device login is labelled as limited');

// --- streaming UI hierarchy --------------------------------------------------
assert.ok(js.includes("tidal: { name: 'Tidal', canConnect: true, catalog: true }"),
    'Tidal visible provider name and catalog role must be explicit');
assert.ok(js.includes('catalogProvider'),
    'Tidal must be identified as a catalog provider whose player is the footer');
// --- TIDAL browse: Favorites default, search is a permanent bar ------------
// Favorites is the default browse section; the search bar lives above the
// navigation and only ever overlays the browse body with a temporary results
// state, so there is no empty "search first" start screen anymore.
assert.ok(js.includes("browseSection: 'favorites'"),
    'TIDAL browse must default to Favorites');
assert.ok(js.includes("state.tidal.searchQuery = query"),
    'executed Tidal searches must record the query');
assert.ok(js.includes("state.tidal.searchQuery = ''"),
    'clearing Tidal search must reset the executed query');
assert.ok(!js.includes('Search Tidal for music.'),
    'no empty search start screen: cleared search returns to the browse section');
assert.ok(js.includes('function bindTidalSearchBar'),
    'the browse surface must wire the permanent search bar');
assert.ok(js.includes('function resetTidalSearch') && js.includes('function clearTidalSearch'),
    'search reset must be explicit state, not DOM-only toggling');
const tidalBrowseRender = extractFunction(js, 'renderTidalBrowse');
assert.ok(tidalBrowseRender.includes('id="tidal-search-input"') && tidalBrowseRender.includes('id="tidal-search-btn"'),
    'the search bar must be part of the browse surface');
assert.ok(tidalBrowseRender.indexOf('tidal-search-input') < tidalBrowseRender.indexOf('streaming-browse-tabs'),
    'the search bar must sit above the browse navigation');
assert.ok(!tidalBrowseRender.includes('data-browse="search"'),
    'Search must not be a browse tab anymore');
assert.ok(tidalBrowseRender.includes('data-browse="favorites"') && tidalBrowseRender.includes('data-browse="playlists"'),
    'browse navigation must be Favorites and Playlists only');
assert.ok(js.includes('renderTidalBrowseSection(state.tidal.browseSection)'),
    'first authenticated render must show the current browse section (Favorites by default)');

// Playlist/detail queue semantics stay intact: clicking any track passes the
// whole track list plus the chosen start id to /api/play.
assert.ok(js.includes('function renderDetailTracks'),
    'the shared detail track-list renderer must stay');
assert.ok(js.includes('playTidalTracks(ids, trackId)'),
    'a track click must start the whole queue at that track');
assert.ok(js.includes('playTidalTracks(ids, ids[0])'),
    'Play playlist must start the whole queue at track 1');
assert.ok(js.includes('queue_track_ids: trackIds'),
    'playback must hand the full queue to /api/play');

// Catalog providers must not render a second in-tab now-playing card: the
// global footer is the authoritative player, so the card stays hidden.
const nowPlayingRender = js.slice(js.indexOf('function renderNowPlaying'), js.indexOf('function showEmpty'));
assert.ok(nowPlayingRender.includes('meta.catalog'),
    'catalog providers must be routed through the catalog flag');
assert.ok(nowPlayingRender.includes('els.nowPlaying.hidden = true'),
    'catalog providers must keep the in-tab now-playing card hidden');

// The visible provider status must not surface raw backend identifiers, and
// player providers integrate the status into the card (top-right chip) while
// catalog providers keep the standalone line.
for (const name of ['renderStatusLine', 'buildStatusBits', 'buildStatusDetail']) {
    const fn = extractFunction(js, name);
    assert.ok(!fn.includes('qbzd'), `${name} must not surface the qbzd backend name`);
    assert.ok(!fn.includes('tidalapi'), `${name} must not surface the tidalapi backend name`);
}
const statusLineRender = extractFunction(js, 'renderStatusLine');
assert.ok(statusLineRender.includes('els.statusChip'),
    'player providers must render status into the in-card chip');
assert.ok(statusLineRender.includes('els.statusLine'),
    'catalog providers keep the standalone status line');
assert.ok(statusLineRender.includes('catalogProvider'),
    'status placement must branch on the catalog flag, not the provider id');
assert.ok(js.includes("'<div class=\"streaming-status-chip\" hidden></div>'"),
    'now-playing card must carry the in-card status chip');
assert.ok(js.includes("'● ' + bits.join(' · ')"),
    'in-card status must be a compact dot-led label');
assert.ok(js.includes('buildStatusDetail'),
    'backend/provider detail must stay available via the chip tooltip');
const bitsBuilder = extractFunction(js, 'buildStatusBits');
assert.ok(bitsBuilder.includes("PROVIDER_META.tidal.name"),
    'Tidal status must use the visible provider name');

// The served navigation changes visible copy only, not the provider id.
assert.ok(html.includes('data-tab="tidal"') && html.includes('<span>Tidal</span>'),
    'Tidal navigation must use title case while retaining its tidal tab id');
assert.ok(!html.includes('<span>TIDAL</span>'), 'served navigation must not use all-caps TIDAL');

// Footer clearance is part of the streaming content layout contract.
assert.ok(css.includes('.tab-content') && css.includes('padding: 1rem 1.25rem calc(1.5rem + var(--playback-footer-space))'),
    'tab content must reserve the centralized footer safe area');

// --- TIDAL album view mirrors the library (no Play Album, facts + tracks) ------
assert.ok(!js.includes('>Play album<'),
    'Tidal album view must not offer a Play Album button');
assert.ok(js.includes('>Play playlist<'),
    'Tidal playlist view keeps its Play playlist button');
assert.ok(js.includes('streaming-detail-artist') && js.includes('streaming-detail-facts'),
    'Tidal album view must bundle artist + facts next to the cover');
assert.ok(js.includes("fetch('/api/streaming/tidal/albums/' + encodeURIComponent(state.tidal.detailId))"),
    'Tidal album view must fetch album metadata for title/artist/year/quality');
assert.ok(js.includes('tidalQualityLabel'),
    'Tidal album view must map the real audio_quality to a display label');
assert.ok(css.includes('.streaming-detail-facts') && css.includes('.streaming-fav'),
    'album facts and favorite heart styles must ship in style.css');

// Both detail headers (album + playlist) reuse the library's back button and
// star favorite and scale the cover to library proportions.
assert.ok(js.includes('class="album-detail-back"'),
    'Tidal detail back button must reuse the library back style');
assert.ok(js.includes('favoriteStarHtml') && js.includes('album-favorite-toggle'),
    'Tidal detail favorite must be a library-style star, not a heart');
assert.ok(js.includes("'★' : '☆'"),
    'the detail star must render filled/outline like the library');
assert.ok(js.includes("favoriteStarHtml('albums')") && js.includes("favoriteStarHtml('playlists')"),
    'album and playlist headers must both carry the star favorite');
assert.ok(js.includes('tidal-detail'),
    'Tidal album and playlist detail must share the library-mirroring layout');
assert.ok(js.includes("favoriteStarHtml('playlists')") && js.includes('tidal-playlist-facts'),
    'the playlist detail must show a star and a track-count facts line');
assert.ok(css.includes('.tidal-detail-header .streaming-detail-cover') && /width: 160px/.test(css),
    'Tidal detail cover must match the library 160px proportion');

// --- TIDAL track/album favorites (real state, no shadow) ---------------------
assert.ok(js.includes("'/api/streaming/tidal/favorites/ids'"),
    'heart state must come from the authoritative favorites/ids endpoint');
assert.ok(js.includes('favoriteButtonHtml') && js.includes('data-fav-type') && js.includes('data-fav-id'),
    'tracks and albums must render a favorite heart button');
assert.ok(js.includes('set_track_favorite') === false && js.includes('/favorite'),
    'the heart must write back through the provider favorite endpoint');
assert.ok(js.includes("chip('tracks', 'Tracks', true) + chip('albums', 'Albums', false) + chip('artists', 'Artists', false)"),
    'Favorites must offer tracks/albums/artists categories');
assert.ok(js.includes("data-browse=\"favorites\"") && js.includes("data-browse=\"playlists\""),
    'Favorites and Playlists must be separate browse tabs');
assert.ok(js.includes("chip('tracks', 'Tracks', true) + chip('albums', 'Albums', false)"),
    'the search bar must keep the track/album/artist/playlist search types');
assert.ok(js.includes('/api/streaming/tidal/favorites?type=') && js.includes('/api/streaming/tidal/playlists'),
    'favorite albums and real playlists must be fetched from distinct endpoints');

// Playlists are favoritable like tracks/albums: heart on every playlist row,
// state tracked in the same authoritative favorites/ids payload, and write-back
// through the generic favorite endpoint.
assert.ok(js.includes("favoriteButtonHtml('playlists', item.id)"),
    'playlist rows (search + playlists view) must render a favorite heart');
assert.ok(js.includes("playlists: new Set((data.playlists || []).map(String))"),
    'favorites/ids must feed playlist hearts alongside tracks/albums');
assert.ok(js.includes("playlists: new Set()"),
    'favorite state must track playlists from the start');
assert.ok(js.includes("'/api/streaming/tidal/' + type + '/' + encodeURIComponent(idStr) + '/favorite'"),
    'playlist hearts must write back through the generic favorite endpoint');

// Quality label mapping is honest: only the real Tidal tier, no invented
// codec/bit-depth/sample-rate at album level.
const tidalQualityLabel = new Function('return ' + extractFunction(js, 'tidalQualityLabel'))();
assert.equal(tidalQualityLabel('LOSSLESS'), 'Lossless');
assert.equal(tidalQualityLabel('HI_RES_LOSSLESS'), 'Hi-Res Lossless');
assert.equal(tidalQualityLabel('HIGH'), 'High');
assert.equal(tidalQualityLabel('LOW'), 'Low');
assert.equal(tidalQualityLabel(''), '');
assert.equal(tidalQualityLabel(null), '');

// --- unified detail views: back button + shared header grid -----------------
// Both the library album detail and the Tidal album/playlist details use the
// same '← Back' label, and the Tidal back button lives inside the shared
// detail header (no separate toolbar row above it).
assert.ok(html.includes('← Back') && html.includes('id="album-detail-back"'),
    'library album detail must use the shared ← Back label');
assert.ok(js.includes('← Back'), 'Tidal detail views must use the shared ← Back label');
assert.ok(!js.includes('tidal-detail-toolbar'),
    'Tidal detail must not keep a separate toolbar row above the header');
const albumRender = extractFunction(js, 'renderTidalAlbum');
const playlistRender = extractFunction(js, 'renderTidalPlaylist');
for (const fn of [albumRender, playlistRender]) {
    assert.ok(fn.includes('class="album-detail-back"'),
        'Tidal detail back button must reuse the library back style');
    assert.ok(fn.indexOf('album-detail-back') > fn.indexOf('streaming-detail-header'),
        'Tidal back button must be inside the detail header');
}
// The shared header grid places cover / meta / back in one row on wide
// screens. On narrow ones the hero stays compact side by side (cover left,
// meta right) and only the back button moves to its own line; the hero is
// never stacked vertically or centered.
assert.ok(/grid-template-areas:\s*"cover meta back"/.test(css),
    'detail header must be a cover/meta/back grid row');
assert.ok(css.includes('"back back"') && css.includes('"cover meta"'),
    'narrow detail header must keep cover/meta side by side with back on its own line');
assert.ok(!/"back"\s*"cover"\s*"meta"/.test(css),
    'narrow detail header must not stack back/cover/meta vertically');

// --- status line is a main-surface-only detail ------------------------------
// The permanent Connected status stays on the main catalog surface; detail
// views hide it (real errors still render via the login surface and line).
assert.ok(js.includes('function isTidalDetailView'),
    'streaming must have an explicit detail-view guard for the status line');
assert.ok(js.includes("state.tidal.view === 'album' || state.tidal.view === 'playlist'"),
    'album/playlist must count as detail views for the status line');
const statusRender = extractFunction(js, 'renderStatusLine');
assert.ok(statusRender.includes('applyCatalogStatusLine'),
    'catalog status must render through the shared status-line helper');
const browseRender = extractFunction(js, 'renderTidalBrowse');
assert.ok(browseRender.includes('applyCatalogStatusLine'),
    'navigating into/out of detail views must sync the status line immediately');

// --- one shared detail track row for library and Tidal ----------------------
// The shared row builder lives in app.js and is handed to streaming.js via
// the init api, so both render the same index/play/title/sub/fav/duration row.
assert.ok(appJs.includes('function detailTrackRowHtml('),
    'app.js must host the shared detail track-row builder');
assert.ok(appJs.includes('trackRowHtml: detailTrackRowHtml'),
    'app.js must pass the shared row builder to streaming.js');
assert.ok(js.includes('trackRowHtml({'), 'streaming.js must render detail rows via the shared builder');
assert.ok(js.includes("favoriteButtonHtml('tracks', item.id, 'track-fav')"),
    'Tidal detail rows must use the shared track-fav favorite class');
// Library album detail rows: track number, round play button, artist-only sub
// line in album context, shared favorite class.
const albumTracksRender = extractFunction(appJs, 'renderAlbumDetailTracks');
assert.ok(albumTracksRender.includes('detailTrackRowHtml({'),
    'library album rows must render via the shared row builder');
assert.ok(albumTracksRender.includes('index: index + 1'),
    'library album rows must show track numbers');
assert.ok(albumTracksRender.includes('track.artist'),
    'library album rows must keep an artist sub line');
assert.ok(!albumTracksRender.includes('[track.artist, track.album]'),
    'library album rows must not repeat the album name in album context');
assert.ok(albumTracksRender.includes('class="track-fav'),
    'library album rows must use the shared track-fav favorite class');
assert.ok(albumTracksRender.includes('.track-play'),
    'library album rows must bind the shared round play button');
// The shared row CSS ships once (grouped with the Tidal equivalents).
for (const cls of ['.track-index', '.track-play', '.track-info', '.track-sub', '.track-fav']) {
    assert.ok(css.includes(cls), `shared track-row CSS must ship ${cls}`);
}
assert.ok(css.includes('.streaming-result-play,') && css.includes('.track-play'),
    'round play button must be one grouped CSS rule');
assert.ok(css.includes('.streaming-result-duration,') && css.includes('.track-duration'),
    'duration must be one grouped CSS rule');

console.log('PASS  scripts/test_streaming_ui_structure.js');
