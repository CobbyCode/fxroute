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
// --- TIDAL browse: one shared navigation row, search is a permanent bar ----
// Tracks is the default browse category. The four categories live in one
// navigation row; search categories only exist in the result state and never
// compete with them.
assert.ok(js.includes("browseCategory: 'tracks'"),
    'TIDAL browse must default to Tracks');
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
assert.ok(tidalBrowseRender.includes('placeholder="Search"'),
    'the Tidal search placeholder must stay compact');
assert.ok(tidalBrowseRender.indexOf('tidal-search-input') > tidalBrowseRender.indexOf('data-browse=') &&
    tidalBrowseRender.indexOf('tidal-search-input') > tidalBrowseRender.indexOf('tidal-subbar'),
    'the search bar must share the second header row with the browse navigation');
assert.ok(tidalBrowseRender.includes('tidal-toolbar-actions') && tidalBrowseRender.includes('entry.els.statusLine'),
    'Tidal connected status must move into the first header row actions');
assert.ok(tidalBrowseRender.includes('id="tidal-refresh-btn"'),
    'the first header row must carry the manual refresh button');
assert.ok(!tidalBrowseRender.includes('tidal-search-types'),
    'search types must not be visible in the browse state');
assert.ok(!tidalBrowseRender.includes('data-browse="search"'),
    'Search must not be a browse tab anymore');
assert.ok(tidalBrowseRender.includes('data-browse="') && tidalBrowseRender.includes('TIDAL_BROWSE_LABELS[cat]'),
    'browse tabs must render through the shared category map');
assert.ok(js.includes("const TIDAL_BROWSE_CATEGORIES = ['tracks', 'albums', 'artists', 'playlists'];"),
    'browse navigation must be Tracks, Albums, Artists and Playlists only');
assert.ok(js.includes('renderTidalBrowseSection(state.tidal.browseCategory)'),
    'first authenticated render must show the current browse category (Tracks by default)');

// --- TIDAL toolbar/search state --------------------------------------------
// Row 1 is the page title with Connected + refresh on the right; the search
// controls share the second row with the Tracks/Albums/Artists/Playlists
// navigation. Search type controls belong to the executed-results view, never
// to the browse categories.
assert.ok(js.includes('entry.els.statusLine'),
    'TIDAL Connected status must be moved into the first header row actions');
assert.ok(js.includes('function bindTidalRefresh') && js.includes('function refreshTidalCatalog'),
    'the manual TIDAL refresh must have an explicit handler');
assert.ok(js.includes('loadTidalFavoriteIds(true)') && js.includes('refreshTidalStatus()'),
    'refresh must reuse the existing favorite-ids and status provider logic');
assert.ok(js.includes('refreshActiveTidalView'),
    'refresh must re-render the currently active TIDAL view');
assert.ok(js.includes('state.tidal.searchRequestId += 1'),
    'clearing or navigating away must invalidate pending search responses');
assert.ok(js.includes('if (input && state.tidal.searchExecuted) input.value = state.tidal.searchQuery'),
    'a content-key rebuild must restore the executed query in the search input');
assert.ok(js.includes('placeholder="Search"'),
    'TIDAL search must use the short Search placeholder');
assert.ok(!js.includes('id="tidal-search-types"'),
    'search types must not be rendered beside the browse search bar');
assert.ok(js.includes('function renderTidalSearchResults'),
    'executed searches must have a dedicated result view');
assert.ok(js.includes('Search results for &quot;'),
    'executed searches must show the query in a result heading');
assert.ok(js.includes("const TIDAL_SEARCH_TYPES = ['artists', 'tracks', 'albums', 'playlists'];"),
    'search result types must have the requested order');
assert.ok(js.includes('tidal-search-result-types'),
    'search result type controls must be distinct from the browse categories');
assert.ok(js.includes('if (!state.tidal.searchExecuted || !state.tidal.searchQuery) return;'),
    'search type changes must not request before a query was executed');
assert.ok(js.includes('void executeTidalSearch(type)'),
    'changing a search type must automatically execute the stored query');
assert.ok(js.includes("input.addEventListener('input'"),
    'clearing the search field must leave the result state immediately');

// --- shared compact view tabs (Library + TIDAL browse + search) ------------
// Library Tracks/Folders/Albums and TIDAL Tracks/Albums/Artists/Playlists
// (one navigation row) plus the search result types must be one shared
// segmented-navigation component (.view-tab). No separate Favorites level
// and no inner category chips remain.
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
assert.ok(js.includes('class="view-tab'),
    'TIDAL browse and search type tabs must use the shared view-tab class');
assert.ok(js.includes('.tidal-subbar .view-tab[data-browse]') && js.includes('#tidal-search-result-types .view-tab'),
    'TIDAL browse and search type click wiring must target the shared class');
const viewTabCss = css.slice(css.indexOf('.view-tab {'), css.indexOf('.view-tab:hover'));
assert.ok(/-?\d+px/.test(viewTabCss.match(/min-height:\s*([^;]+);/)[1]),
    'the shared view-tab must define a concrete min-height (36-40px range)');
const tabMinHeight = parseInt(viewTabCss.match(/min-height:\s*(\d+)px/)[1], 10);
assert.ok(tabMinHeight >= 36 && tabMinHeight <= 40,
    `shared view-tab min-height must sit in the compact 36-40px range (got ${tabMinHeight}px)`);
assert.ok(!viewTabCss.includes('999px') && viewTabCss.includes('var(--radius-sm)'),
    'the shared view-tab must use the standard radius, not a pill shape');

// Track search results are a temporary queue, while playlist/detail queues keep
// their existing ids path. Selection controls are opt-in and disappear from the
// normal result presentation.
assert.ok(js.includes("const queueIds = type === 'tracks' ? items.map((item) => String(item.id)).filter(Boolean) : [];"),
    'track search results must build a queue from all displayed tracks');
assert.ok(js.includes('playTidalTracks(queueIds, trackId)'),
    'a track search click must start the complete search-results queue');
assert.ok(js.includes('tidal-track-selection-toggle') && js.includes('tidal-select-all') &&
    js.includes('tidal-clear-selection') && js.includes('tidal-play-selected'),
    'track selection mode must expose Select all, Clear and Play selected');
assert.ok(js.includes('tidal-track-select'),
    'track checkboxes must be scoped to the opt-in selection mode');
assert.ok(js.includes('const selectedIds = items.map((item) => String(item.id)).filter((id) => state.tidal.selectedTrackIds.has(id))'),
    'Play selected must create a queue only from checked search tracks');
const tidalSearchRender = extractFunction(js, 'renderTidalSearchResults');
assert.ok(tidalSearchRender.includes('Search results for'),
    'search results must have a distinct heading');
assert.ok(tidalSearchRender.includes('tidal-search-result-types'),
    'search result categories must be rendered only after a search');
assert.ok(js.includes("artists: 'Artists'") && js.includes("tracks: 'Tracks'") && js.includes("albums: 'Albums'") && js.includes("playlists: 'Playlists'"),
    'search results must expose all result categories');
assert.ok(tidalSearchRender.includes('state.tidal.searchQuery'),
    'the executed query must be shown in the search result heading');
assert.ok(js.includes('detailRequestId'),
    'TIDAL detail loads must ignore responses from an obsolete detail view');

// Toolbar layout follows the library pattern: title row with Connected +
// refresh right-aligned, then a second row with the navigation left and the
// search right. Both right-aligned groups share one right edge so browse <->
// detail never shifts horizontally.
assert.ok(css.includes('.tidal-toolbar') && css.includes('.tidal-toolbar-actions'),
    'TIDAL toolbar must place the connected status + refresh in the title row');
assert.ok(css.includes('.tidal-subbar'),
    'TIDAL second header row (navigation + search) must have its own container');
assert.ok(/@media \(max-width: 760px\)[\s\S]*?\.tidal-subbar \.streaming-search\s*\{[\s\S]*?width: 100%;/.test(css),
    'TIDAL search must take the full second-row width on mobile');
assert.ok(!css.includes('.streaming-status-line'),
    'the old standalone status line must be gone; one shared .streaming-status pill remains');
assert.ok(css.includes('#tidal-fav-results') && css.includes('#tidal-search-items'),
    'Favorites and search results must both reserve a gap below their subtabs');
assert.ok(/#tidal-fav-results[\s\S]*?#tidal-search-items[\s\S]*?margin-top:\s*0\.35rem/.test(css),
    'the subtab-to-content gap must stay small and consistent');

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
assert.ok(js.includes('trackSelectionMode'),
    'Tidal search tracks must have an optional selection mode');
assert.ok(js.includes('Select all') && js.includes('Clear') && js.includes('Play selected'),
    'selection controls must be available after activating selection mode');

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
    'player providers must render status into the in-card status pill');
assert.ok(statusLineRender.includes('els.statusLine'),
    'catalog providers keep the toolbar status pill');
assert.ok(statusLineRender.includes('catalogProvider'),
    'status placement must branch on the catalog flag, not the provider id');
assert.ok(js.includes("'<span class=\"streaming-status\" hidden></span>'"),
    'the now-playing card must carry the shared in-card status pill');
assert.ok(js.includes("textContent = 'Connected'"),
    'the status pill must be the one shared Connected label');
assert.ok(js.includes('buildStatusDetail'),
    'backend/provider detail must stay available via the pill tooltip');
const bitsBuilder = extractFunction(js, 'buildStatusBits');
assert.ok(!bitsBuilder.includes("PROVIDER_META.tidal.name"),
    'the status label must not repeat the provider name (Tidal is the page title)');
assert.ok(bitsBuilder.includes("['Connected']"),
    'all providers must share the identical Connected status language');

// The served navigation changes visible copy only, not the provider id.
assert.ok(html.includes('data-tab="tidal"') && html.includes('<span>Tidal</span>'),
    'Tidal navigation must use title case while retaining its tidal tab id');
assert.ok(!html.includes('<span>TIDAL</span>'), 'served navigation must not use all-caps TIDAL');

// Footer clearance is part of the streaming content layout contract.
assert.ok(css.includes('.tab-content') && css.includes('calc(2rem + var(--playback-footer-space))'),
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
assert.ok(js.includes("const TIDAL_BROWSE_CATEGORIES = ['tracks', 'albums', 'artists', 'playlists'];"),
    'the four browse categories must be Tracks, Albums, Artists and Playlists in one row');
assert.ok(js.includes("TIDAL_BROWSE_LABELS = { tracks: 'Tracks', albums: 'Albums', artists: 'Artists', playlists: 'Playlists' }"),
    'Tracks, Albums, Artists and Playlists must be the four labelled browse tabs');
assert.ok(js.includes('data-browse="\' + cat + \'"'),
    'the browse tabs must carry their category in data-browse');
assert.ok(js.includes("const TIDAL_SEARCH_TYPES = ['artists', 'tracks', 'albums', 'playlists'];"),
    'the search result view must keep the track/album/artist/playlist search types');
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
assert.ok(js.includes("state.tidal.view === 'album' || state.tidal.view === 'playlist' || state.tidal.view === 'artist'"),
    'album/playlist/artist must count as detail views for the status line');

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
assert.ok(artistRender.includes("favoriteStarHtml('artists')"),
    'the artist detail header must carry the follow star');
const artistLoad = extractFunction(js, 'loadTidalArtist');
assert.ok(artistLoad.includes("'/api/streaming/tidal/artists/' + encodeURIComponent(state.tidal.detailId)"),
    'the artist detail must fetch through the artist endpoint');
assert.ok(artistLoad.includes("heading.textContent = 'Top Tracks'") && artistLoad.includes("heading.textContent = 'Albums'"),
    'the artist detail must render Top Tracks and Albums sections');
assert.ok(artistLoad.includes("renderSearchItem('albums', item, [])"),
    'album rows in the artist detail must reuse the existing album row/click');
assert.ok(js.includes('viewStack'),
    'detail navigation must keep a back stack so nested details return through');
assert.ok(js.includes('function closeTidalDetail'),
    'detail back must pop the shared navigation stack');
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
// Result covers fall back to a neutral placeholder: a failed image removes
// itself so the CSS :empty tile (e.g. TIDAL artists without a picture) shows
// instead of a broken-image icon or an empty box.
assert.ok(js.includes('onerror="this.remove()"'),
    'result covers must self-remove on image load failure');
assert.ok(css.includes('.streaming-result-cover:empty'),
    'empty result covers must render the neutral placeholder tile');

// The shared row CSS ships once (grouped with the Tidal equivalents).
for (const cls of ['.track-index', '.track-play', '.track-info', '.track-sub', '.track-fav']) {
    assert.ok(css.includes(cls), `shared track-row CSS must ship ${cls}`);
}
assert.ok(css.includes('.streaming-result-play,') && css.includes('.track-play'),
    'round play button must be one grouped CSS rule');
assert.ok(css.includes('.streaming-result-duration,') && css.includes('.track-duration'),
    'duration must be one grouped CSS rule');

console.log('PASS  scripts/test_streaming_ui_structure.js');
