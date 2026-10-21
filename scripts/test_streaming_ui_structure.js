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
assert.ok(!js.includes("tidal: { kind: 'native'"), 'TIDAL has no tab transport: the footer is its player');

// --- quality facts live in the shared footer meta-tag, not in the card --------
// The now-playing card must no longer carry its own quality badge: the stream
// facts render once, in the global footer, through the same meta-tag renderer
// as library/radio (app.js formatStreamingMetaLine -> formatRadioStreamLine).
assert.ok(!js.includes('.streaming-quality'),
    'streaming card must not render a quality badge');
assert.ok(!js.includes('formatQuality'),
    'streaming module must not own a quality formatter');
const appJs = fs.readFileSync(path.join(__dirname, '..', 'static', 'app.js'), 'utf8');
const playbackUiJs = fs.readFileSync(path.join(__dirname, '..', 'static', 'playback_ui.js'), 'utf8');
const libraryJs = fs.readFileSync(path.join(__dirname, '..', 'static', 'library_ui.js'), 'utf8');
assert.ok(playbackUiJs.includes('function formatStreamingMetaLine('),
    'playback_ui.js must host the shared streaming footer meta renderer');
assert.ok(playbackUiJs.includes("formatStreamingMetaLine(data)"),
    'streaming footer must render through the shared meta-tag renderer');
assert.ok(!playbackUiJs.includes('renderStreamingFooterMeta'),
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

// --- served shell wires the Spotify and Qobuz tabs + the module -------------------
for (const provider of ['spotify', 'qobuz']) {
    assert.ok(html.includes(`data-provider="${provider}"`), `index.html must ship a ${provider} shell`);
    assert.ok(html.includes(`data-tab="${provider}"`), `index.html must ship a ${provider} tab button`);
    assert.ok(html.includes(`id="tab-${provider}"`), `index.html must ship a ${provider} tab panel`);
}
assert.ok(html.includes('/static/streaming.js?v='), 'index.html must include streaming.js');

// --- TIDAL lives in the Library: no tab, no shell, no tab surface --------------
assert.ok(!html.includes('data-provider="tidal"'), 'index.html must not ship a TIDAL shell');
assert.ok(!html.includes('data-tab="tidal"'), 'index.html must not ship a TIDAL tab button');
assert.ok(!html.includes('id="tab-tidal"') && !html.includes('id="tab-btn-tidal"'),
    'index.html must not ship a TIDAL tab panel');
assert.ok(html.includes('id="library-tidal-status"') && html.includes('id="library-tidal-detail"'),
    'the Library must carry the TIDAL status button and the detail host');
for (const gone of ['renderTidalBrowse', 'renderTidalContent', 'renderTidalLogin', 'renderTidalDevice',
    'bindTidalSearchBar', 'executeTidalSearch', 'tidalPlaylistSaveRowHtml', 'showTidalSearchResultsFor',
    'activateTidalHost', 'setTidalLayout']) {
    assert.ok(!js.includes(gone), `${gone} belonged to the removed TIDAL tab`);
}
for (const id of ['tidal-search-input', 'tidal-subbar', 'tidal-toolbar', 'tidal-playlist-save-row']) {
    assert.ok(!js.includes(id), `${id} belonged to the removed TIDAL tab`);
}

// --- TIDAL login: the browser (PKCE) login dialog only -----------------------------
assert.ok(js.includes('api.openTidalLogin'), 'TIDAL login opens the shared provider login dialog');
assert.ok(!js.includes('auth/device') && !js.toLowerCase().includes('device login') && !js.includes('AAC 320'),
    'the device login is gone');
assert.ok(!html.toLowerCase().includes('device login') && !html.includes('AAC 320'), 'the device login is gone from the page');

// --- shared compact view tabs (Library views + search result types) --------
// The Library view tabs and the search result types are one shared
// segmented-navigation component (.view-tab).
assert.ok(css.includes('.view-tab') && css.includes('.view-tabs'),
    'the shared compact view-tab component must ship in style.css');
assert.ok(!css.includes('.streaming-chip'),
    'the old TIDAL filter-chip class must be gone (one shared view-tab remains)');
assert.ok(!css.includes('.btn-icon-toggle'),
    'the old library view-toggle button class must be gone (one shared view-tab remains)');
assert.ok(!css.includes('.streaming-browse-tab'),
    'the old level-1 TIDAL browse tab class must be gone (one shared view-tab remains)');
assert.ok(!css.includes('.streaming-browse-tabs'),
    'the old level-1 TIDAL browse container class must be gone');
assert.ok(html.includes('class="view-tab active"'),
    'the library view toggle must use the shared view-tab class');
assert.ok(html.includes('id="library-search-types"') && html.includes('data-search-type="albums"'),
    'the Library search result types must use the shared view-tab class');
const viewTabCss = css.slice(css.indexOf('.view-tab {'), css.indexOf('.view-tab:hover'));
assert.ok(/-?\d+px/.test(viewTabCss.match(/min-height:\s*([^;]+);/)[1]),
    'the shared view-tab must define a concrete min-height (36-40px range)');
const tabMinHeight = parseInt(viewTabCss.match(/min-height:\s*(\d+)px/)[1], 10);
assert.ok(tabMinHeight >= 36 && tabMinHeight <= 40,
    `shared view-tab min-height must sit in the compact 36-40px range (got ${tabMinHeight}px)`);
assert.ok(!viewTabCss.includes('999px') && viewTabCss.includes('var(--radius-sm)'),
    'the shared view-tab must use the standard radius, not a pill shape');

// The row + builds a persistent playlist selection (saved by the Library save
// row as a TIDAL playlist, a local playlist or a mix). Playback and favorite
// actions never touch the selection.
assert.ok(js.includes('tidalTrackAddButtonHtml') && js.includes('data-streaming-add'),
    'every TIDAL track row must expose the persistent + playlist-selection button');
assert.ok(js.includes('selectedTrackIds') && js.includes('syncTidalTrackSelection'),
    'the + selection must keep one shared cross-view selection state');
assert.ok(js.includes("event.target && event.target.closest('.streaming-add, .streaming-fav, .track-fav')"),
    'row clicks must skip the + and heart buttons instead of playing');
assert.ok(js.includes('/api/streaming/tidal/playlists/create') &&
    js.includes("'/api/streaming/tidal/playlists/' + encodeURIComponent(playlistId) + '/tracks'"),
    'the save actions must write through the TIDAL playlist endpoints');
assert.ok(js.includes('detailRequestId'),
    'TIDAL detail loads must ignore responses from an obsolete detail view');
assert.ok(js.includes('onPlay') && js.includes('rememberTidalTrack'),
    'a host list can own playback of its TIDAL rows and the shown tracks stay available for saved playlists');

// Playlist/detail queue semantics stay intact: clicking any track passes the
// whole track list plus the chosen start id to /api/play.
assert.ok(js.includes('function renderDetailTracks'),
    'the shared detail track-list renderer must stay');
assert.ok(js.includes('playTidalTracks(queueIds, trackId)')
    && extractFunction(js, 'renderDetailTracks').includes('queueIds: ids'),
    'a track click must start the whole queue at that track');
assert.ok(!js.includes('playTidalTracks(ids, ids[0])'),
    'the Play playlist button must be gone (track click starts the queue)');
assert.ok(js.includes('queue_track_ids: trackIds'),
    'playback must hand the full queue to /api/play');
assert.ok(!js.includes('trackSelectionMode'),
    'the old opt-in selection mode must be gone (the + button is always present)');
assert.ok(js.includes('tidalTrackAddButtonHtml'),
    'TIDAL track rows must always expose the + playlist-selection button');

// The visible provider status must not surface raw backend identifiers; the
// status is integrated into the player card (top-right chip).
for (const name of ['renderStatusLine', 'buildStatusBits', 'buildStatusDetail']) {
    const fn = extractFunction(js, name);
    assert.ok(!fn.includes('qbzd'), `${name} must not surface the qbzd backend name`);
    assert.ok(!fn.includes('tidalapi'), `${name} must not surface the tidalapi backend name`);
}
const statusLineRender = extractFunction(js, 'renderStatusLine');
assert.ok(statusLineRender.includes('els.statusChip'),
    'player providers must render status into the in-card status pill');
assert.ok(!statusLineRender.includes('catalogProvider'),
    'there is no catalog provider tab anymore');
assert.ok(js.includes("'<span class=\"streaming-status\" hidden></span>'"),
    'the now-playing card must carry the shared in-card status pill');
assert.ok(js.includes("textContent = 'Connected'"),
    'the status pill must be the one shared Connected label');
assert.ok(js.includes('buildStatusDetail'),
    'backend/provider detail must stay available via the pill tooltip');
const bitsBuilder = extractFunction(js, 'buildStatusBits');
assert.ok(bitsBuilder.includes("['Connected']"),
    'all providers must share the identical Connected status language');
assert.ok(bitsBuilder.includes('data.connected === true'),
    'Spotify Connected status must require a live backend signal');

// The served navigation has no TIDAL entry at all.
assert.ok(!html.includes('<span>Tidal</span>') && !html.includes('<span>TIDAL</span>'),
    'served navigation must not list TIDAL');

// Footer clearance is part of the streaming content layout contract.
assert.ok(css.includes('.tab-content') && css.includes('calc(2rem + var(--playback-footer-space))'),
    'tab content must reserve the centralized footer safe area');

// --- TIDAL album view mirrors the library (no Play Album, facts + tracks) ------
assert.ok(!js.includes('>Play album<'),
    'Tidal album view must not offer a Play Album button');
assert.ok(!js.includes('>Play playlist<'),
    'Tidal playlist view must not offer a Play playlist button');
assert.ok(js.includes('streaming-detail-artist') && js.includes('streaming-detail-facts'),
    'Tidal album view must bundle artist + facts next to the cover');
assert.ok(js.includes("fetch('/api/streaming/tidal/albums/' + encodeURIComponent(state.tidal.detailId))"),
    'Tidal album view must fetch album metadata for title/artist/year/quality');
assert.ok(js.includes('tidalQualityLabel'),
    'Tidal album view must map the real audio_quality to a display label');
assert.ok(css.includes('.streaming-detail-facts') && css.includes('.streaming-fav'),
    'album facts and favorite heart styles must ship in style.css');

// Both detail headers (album + playlist) reuse the library's back button and
// heart favorite and scale the cover to library proportions.
assert.ok(js.includes('class="album-detail-back"'),
    'Tidal detail back button must reuse the library back style');
assert.ok(js.includes('favoriteDetailHtml') && js.includes('album-favorite-toggle'),
    'Tidal detail favorite must be a library-style heart');
assert.ok(js.includes('favoriteHeartSvg()'),
    'the detail heart must render the shared currentColor SVG heart');
assert.ok(js.includes("favoriteDetailHtml('albums')") && js.includes("favoriteDetailHtml('playlists')"),
    'album and playlist headers must both carry the heart favorite');
assert.ok(js.includes('tidal-detail'),
    'Tidal album and playlist detail must share the library-mirroring layout');
assert.ok(js.includes("favoriteDetailHtml('playlists')") && js.includes('tidal-playlist-facts'),
    'the playlist detail must show a heart and a track-count facts line');
assert.ok(css.includes('.tidal-detail-header .streaming-detail-cover') && /width: 160px/.test(css),
    'Tidal detail cover must match the library 160px proportion');

// --- TIDAL track/album favorites (real state, no shadow) ---------------------
assert.ok(js.includes("'/api/streaming/tidal/favorites/ids'"),
    'heart state must come from the authoritative favorites/ids endpoint');
assert.ok(js.includes('favoriteButtonHtml') && js.includes('data-fav-type') && js.includes('data-fav-id'),
    'tracks and albums must render a favorite heart button');
assert.ok(js.includes('set_track_favorite') === false && js.includes('/favorite'),
    'the heart must write back through the provider favorite endpoint');
assert.ok(js.includes("const TIDAL_SEARCH_TYPES = ['albums', 'tracks', 'artists', 'playlists'];"),
    'the search result view must keep the track/album/artist/playlist search types');
assert.ok(js.includes('/api/streaming/tidal/favorites?type=') && js.includes('/api/streaming/tidal/playlists'),
    'favorite albums and real playlists must be fetched from distinct endpoints');

// Playlists are favoritable like tracks/albums: heart on every playlist row,
// state tracked in the same authoritative favorites/ids payload, and write-back
// through the generic favorite endpoint.
assert.ok(libraryJs.includes('TIDAL_CARD_FAVORITE_KINDS'),
    'playlist cards must render a favorite heart');
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
// screens. Tablet keeps a two-column reading flow; phones stack Back, cover,
// and metadata so the artwork and long titles retain useful scale.
assert.ok(/grid-template-areas:\s*"cover meta back"/.test(css),
    'detail header must be a cover/meta/back grid row');
assert.ok(css.includes('"back back"') && css.includes('"cover meta"'),
    'tablet detail header must keep cover/meta side by side with back on its own line');
assert.ok(/"back"\s*"cover"\s*"meta"/.test(css),
    'phone detail header must stack back/cover/meta vertically');

// --- detail views --------------------------------------------------------------
assert.ok(js.includes('function isTidalDetailView'),
    'streaming must have an explicit detail-view guard for the Library host');
assert.ok(js.includes("state.tidal.view === 'album' || state.tidal.view === 'playlist' || state.tidal.view === 'artist'"),
    'album/playlist/artist must count as detail views');

// --- TIDAL artist detail: same path from search and favorites ----------------
// Artist rows open a shared artist detail (cover/name/follow star in the
// header, Top Tracks + Albums sections); search and Favorites artists render
// through the same renderSearchItem path, so one openTidalArtist serves both.
assert.ok(js.includes('function openTidalArtist'),
    'artist rows must open a dedicated artist detail');
assert.ok(js.includes("favoriteButtonHtml('artists', item.id)"),
    'artist rows must render a follow heart like tracks/albums/playlists');
assert.ok(js.includes("artists: new Set((data.artists || []).map(String))"),
    'favorites/ids must feed artist follow state');
const artistRender = extractFunction(js, 'renderTidalArtist');
assert.ok(artistRender.includes("favoriteDetailHtml('artists')"),
    'the artist detail header must carry the follow heart');
const artistLoad = extractFunction(js, 'loadTidalArtist');
const artistFetch = extractFunction(js, 'fetchTidalArtist');
assert.ok(artistFetch.includes("'/api/streaming/tidal/artists/' + encodeURIComponent(id)"),
    'the artist detail must fetch through the artist endpoint');
assert.ok(artistLoad.includes('fetchTidalArtist(state.tidal.detailId)'),
    'the artist detail must load through the shared artist fetch');
// Top Tracks + Albums render through one body shared with the Library artist page.
const artistBody = extractFunction(js, 'renderTidalArtistBody');
assert.ok(artistLoad.includes('renderTidalArtistBody(results, data)'),
    'the artist detail must render its sections through the shared body');
assert.ok(artistBody.includes("heading.textContent = 'Top Tracks'") && artistBody.includes("heading.textContent = 'Albums'"),
    'the artist detail must render Top Tracks and Albums sections');
assert.ok(artistBody.includes("renderSearchItem('albums', item, [])"),
    'album rows in the artist detail must reuse the existing album row/click');
// The body appends its sections to the results host, so the "Loading…"
// placeholder from renderTidalArtist must be cleared first; otherwise every
// artist with catalog data keeps a stale loading line above Top Tracks.
const clearIndex = artistLoad.indexOf("results.innerHTML = ''");
assert.ok(clearIndex > 0,
    'the artist detail must clear the loading placeholder before rendering');
assert.ok(clearIndex < artistLoad.indexOf('renderTidalArtistBody(results, data)'),
    'the loading placeholder must be cleared before the sections are rendered');
assert.ok(js.includes('viewStack'),
    'detail navigation must keep a back stack so nested details return through');
assert.ok(js.includes('function closeTidalDetail'),
    'detail back must pop the shared navigation stack');

// --- one shared detail track row for library and Tidal ----------------------
// The shared row builder lives in library_ui.js and is handed to streaming.js
// via the init api, so both render the same index/play/title/sub/fav/duration row.
assert.ok(libraryJs.includes('function detailTrackRowHtml('),
    'library_ui.js must host the shared detail track-row builder');
assert.ok(appJs.includes('trackRowHtml: LibraryUI.detailTrackRowHtml'),
    'app.js must pass the shared row builder to streaming.js');
assert.ok(js.includes('trackRowHtml({'), 'streaming.js must render detail rows via the shared builder');
assert.ok(extractFunction(js, 'renderDetailTracks').includes("favoriteClass: 'track-fav'")
    && extractFunction(js, 'buildTidalTrackRow').includes("favoriteButtonHtml('tracks', item.id, opts.favoriteClass)"),
    'Tidal detail rows must use the shared track-fav favorite class');
// Library album detail rows: track number, round play button, artist-only sub
// line in album context, shared favorite class.
const albumTracksRender = extractFunction(libraryJs, 'renderAlbumDetailTracks');
assert.ok(albumTracksRender.includes('detailTrackRowHtml({'),
    'library album rows must render via the shared row builder');
assert.ok(albumTracksRender.includes('index: index + 1'),
    'library album rows must show track numbers');
assert.ok(albumTracksRender.includes('track.artist'),
    'library album rows must keep an artist sub line');
assert.ok(!albumTracksRender.includes('[track.artist, track.album]'),
    'library album rows must not repeat the album name in album context');
assert.ok(albumTracksRender.includes('detailFavoriteButtonHtml'),
    'library album rows must use the shared detailFavoriteButtonHtml helper');
assert.ok(albumTracksRender.includes('.track-play'),
    'library album rows must bind the shared round play button');
// Result covers fall back to the shared neutral artwork asset instead of a
// broken-image icon or an empty box.
assert.ok(js.includes('artworkPlaceholderUrl'),
    'result covers must use the shared artwork placeholder on load failure');
assert.ok(js.includes('function tidalImageFallbackUrl(') && js.includes('data-fallback-src'),
    'TIDAL result covers must retry a reachable smaller image variant before removal');
assert.ok(css.includes('.streaming-result-cover:empty'),
    'empty result covers must render the neutral placeholder tile');

// The shared row CSS ships once (grouped with the Tidal equivalents).
for (const cls of ['.track-index', '.track-play', '.track-info', '.track-sub', '.track-fav']) {
    assert.ok(css.includes(cls), `shared track-row CSS must ship ${cls}`);
}
assert.ok(css.includes('.track-play'),
    'round play button must ship as .track-play');
assert.ok(css.includes('.track-duration'),
    'track duration CSS rule must be present');

console.log('PASS  scripts/test_streaming_ui_structure.js');
