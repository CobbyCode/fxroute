#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only
// Structural checks for the shared artwork-led detail heroes.

const assert = require('assert/strict');
const fs = require('fs');
const path = require('path');

const root = path.join(__dirname, '..');
const appJs = fs.readFileSync(path.join(root, 'static', 'app.js'), 'utf8');
const streamingJs = fs.readFileSync(path.join(root, 'static', 'streaming.js'), 'utf8');
const html = fs.readFileSync(path.join(root, 'static', 'index.html'), 'utf8');
const css = fs.readFileSync(path.join(root, 'static', 'style.css'), 'utf8');

function extractFunction(source, name) {
    const match = new RegExp(`function\\s+${name}\\s*\\(`).exec(source);
    assert.ok(match, `missing ${name}`);
    const brace = source.indexOf('{', match.index);
    let depth = 0;
    let quote = '';
    let escaped = false;
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

// The local album and playlist details both opt into the new artwork-led hero
// layout and carry the shared backdrop image anchor.
assert.ok(html.includes('class="album-detail album-detail--hero hidden"'),
    'local album detail must opt into the new hero layout');
assert.ok(html.includes('id="album-detail-backdrop"'),
    'local album detail must carry a backdrop image anchor');
assert.ok(html.includes('id="playlist-detail" class="album-detail album-detail--hero hidden"'),
    'local playlist detail must opt into the same hero layout');
assert.ok(html.includes('id="playlist-detail-backdrop"'),
    'local playlist detail must carry a backdrop image anchor');

const openAlbum = extractFunction(appJs, 'openAlbumDetail');
const openTopTracks = extractFunction(appJs, 'openSmartTopTracks');
assert.ok(openAlbum.includes('setAlbumDetailBackdrop('),
    'local album open must synchronize the artwork backdrop');
assert.ok(openTopTracks.includes('setAlbumDetailBackdrop('),
    'Top 40 album detail must synchronize the artwork backdrop');
// The library tab shares the window scroll (no inner scroll container), so
// opening an album must reset it after showing the detail — otherwise the
// detail inherits the grid position and starts mid-page at the tracks.
const scrollReset = extractFunction(appJs, 'scrollAlbumDetailToTop');
assert.ok(scrollReset.includes('window.scrollTo(0, 0)'),
    'the album scroll reset must return the window scroll to exactly the top');
for (const [fn, label] of [[openAlbum, 'openAlbumDetail'], [openTopTracks, 'openSmartTopTracks']]) {
    assert.ok(fn.includes('scrollAlbumDetailToTop()'),
        `${label} must reset the scroll when opening the album detail`);
    assert.ok(fn.indexOf('scrollAlbumDetailToTop()') > fn.indexOf("albumDetail.classList.remove('hidden')"),
        `${label} must reset the scroll after showing the detail, not during loading`);
}
assert.ok(appJs.includes('albumDetailBackdrop:'),
    'app.js must retain a dedicated local album backdrop element');
assert.ok(appJs.includes('playlistDetailBackdrop:'),
    'app.js must carry a dedicated local playlist backdrop element');
assert.ok(appJs.includes('renderPlaylistDetailInfo'),
    'local playlist detail must render a header info line');

const detailBackdrop = extractFunction(streamingJs, 'detailBackdropHtml');
const syncBackdrop = extractFunction(streamingJs, 'syncDetailBackdrop');
const detailCover = extractFunction(streamingJs, 'detailCoverHtml');
assert.ok(detailBackdrop.includes('detail-header-backdrop'),
    'TIDAL detail markup must include the shared backdrop');
assert.ok(syncBackdrop.includes('detail-header-backdrop') && syncBackdrop.includes('coverImg('),
    'TIDAL detail artwork updates must patch the backdrop image');
assert.ok(detailCover.includes('coverImg(art, \'eager\')'),
    'TIDAL detail covers must use the shared artwork fallback when unavailable');

for (const name of ['renderTidalAlbum', 'renderTidalArtist', 'renderTidalPlaylist']) {
    const render = extractFunction(streamingJs, name);
    assert.ok(render.includes('streaming-detail--hero'), `${name} must opt into the hero layout`);
    assert.ok(render.includes('detail-hero-header'), `${name} must use the shared hero header`);
    assert.ok(render.includes('detailBackdropHtml()'), `${name} must render the backdrop`);
}

const albumMeta = extractFunction(streamingJs, 'renderTidalAlbumMeta');
const artistLoad = extractFunction(streamingJs, 'loadTidalArtist');
assert.ok(albumMeta.includes('syncDetailBackdrop(content, meta.art_url)'),
    'TIDAL album metadata art must update the backdrop');
assert.ok(albumMeta.includes('state.tidal.detailArt = meta.art_url'),
    'TIDAL album metadata art must update the detail state');
assert.ok(artistLoad.includes('syncDetailBackdrop(content, data.art_url)'),
    'TIDAL artist metadata art must update the backdrop');
assert.ok(artistLoad.includes('state.tidal.detailArt = data.art_url'),
    'TIDAL artist metadata art must update the detail state');

for (const selector of [
    '.detail-hero-header',
    '.detail-header-backdrop',
    '.detail-hero-cover',
    '.detail-hero-meta',
    '.detail-hero-cover:empty',
    '.album-detail--hero',
    '.streaming-detail--hero',
]) {
    assert.ok(css.includes(selector), `style.css must ship ${selector}`);
}
assert.ok(css.includes('@media (max-width: 600px)'),
    'detail heroes must define a phone composition');
assert.ok(css.includes('@media (min-width: 601px) and (max-width: 1099px)'),
    'detail heroes must define a tablet composition');
assert.ok(css.includes('grid-template-columns: clamp(200px, 22vw, 280px)'),
    'desktop detail heroes must use a restrained cover column');
assert.ok(css.includes('font-size: clamp(1.4rem, 2.4vw, 2.6rem)'),
    'larger detail titles must use one restrained shared scale');
assert.ok(css.includes('grid-template-columns: minmax(170px, 24vw) minmax(0, 1fr) auto'),
    'tablet detail heroes must preserve the shared three-column composition');
assert.ok(css.includes('grid-template-areas: "cover meta back"'),
    'tablet detail heroes must keep metadata and back action on one grid row');
assert.ok(css.includes('width: min(62vw, 220px)'),
    'phone detail covers must stay prominent without dominating the hero');
assert.ok(css.includes('font-size: clamp(1.3rem, 6.2vw, 1.8rem)'),
    'phone detail titles must use a smaller scale than tablets');
assert.ok(!css.includes('clamp(1rem, 2.4vw, 1.6rem)'),
    'tablet detail metadata must not rely on a special expansion offset');
assert.ok(css.includes('margin-block-start: clamp(0.5rem, calc(12vw - 5.1rem), 3.2rem);'),
    'larger detail metadata must use a stable closed-state anchor');
assert.ok(!css.includes('transform: translateY(calc(0px - clamp(0.5rem, 1.2vw, 0.9rem)))'),
    'larger detail metadata must not move with content-height-dependent transforms');
assert.ok(css.includes('gap: 0.7rem;') && css.includes('max-width: 100%;'),
    'detail title rows must use one shared title-to-favorite gap');
assert.ok(css.includes('.album-detail--hero .detail-hero-title-row > .album-favorite-toggle'),
    'library detail favorites must use the shared title-row alignment');
assert.ok(css.includes('.streaming-detail--hero .detail-hero-title-row > .album-favorite-toggle'),
    'TIDAL detail favorites must use the shared title-row alignment');
assert.ok(css.includes('align-self: flex-start;\n    margin: 0;'),
    'detail favorites must align to the first title line without viewport margins');

console.log('PASS test_detail_hero_ui.js');
