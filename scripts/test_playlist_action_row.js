#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only
// Cross-tab checks for the unified playlist action row.
//
// Acceptance: the row appears in every affected tab at the same logical
// position and with the same width as in the working Albums reference,
// and with a symmetric vertical standoff above/below in every host.
//
//   TIDAL: Albums (album detail) is the reference. Tracks (browse list),
//     Artists (detail) and Playlists (detail) must match it: the browse row
//     sits before #tidal-browse-body (never under the entire result list)
//     and every TIDAL row uses the full-width --album variant.
//   Local Library: Albums (album detail) is the reference (covered in
//     test_library_save_row.js). Tracks/Folders/Favorites share the top home
//     before #tracks-list (likewise never under the result list).
//   Radio: has no playlist-build selection row on the same shared UI path,
//     so no change is expected; this test pins that absence.

const assert = require('assert/strict');
const fs = require('fs');
const path = require('path');

const root = path.join(__dirname, '..');
const streamingJs = fs.readFileSync(path.join(root, 'static', 'streaming.js'), 'utf8');
const libraryJs = fs.readFileSync(path.join(root, 'static', 'library_ui.js'), 'utf8');
const appJs = fs.readFileSync(path.join(root, 'static', 'app.js'), 'utf8');
const radioJs = fs.readFileSync(path.join(root, 'static', 'radio.js'), 'utf8');
const html = fs.readFileSync(path.join(root, 'static', 'index.html'), 'utf8');
const css = fs.readFileSync(path.join(root, 'static', 'style.css'), 'utf8');
const responsiveCss = fs.readFileSync(path.join(root, 'static', 'css', '_responsive.css'), 'utf8');

function extractFunction(source, name) {
    const match = new RegExp(`function\\s+${name}\\s*\\(`).exec(source);
    assert.ok(match, `missing ${name}`);
    let depth = 0, i = source.indexOf('{', match.index);
    for (; i < source.length; i += 1) {
        const c = source[i];
        if (c === '{') depth += 1;
        else if (c === '}' && --depth === 0) return source.slice(match.index, i + 1);
    }
    throw new Error(`unterminated ${name}`);
}

// --- TIDAL browse: row before body, full-width variant ---------------------
{
    const browseSrc = extractFunction(streamingJs, 'renderTidalBrowse');
    const rowPos = browseSrc.indexOf('tidalPlaylistSaveRowHtml');
    const bodyPos = browseSrc.indexOf('tidal-browse-body');
    assert.ok(rowPos >= 0 && bodyPos >= 0, 'browse must render both the save row and the body');
    assert.ok(rowPos < bodyPos,
        'TIDAL browse must render the playlist action row BEFORE #tidal-browse-body (never under the Tracks result list)');
    assert.ok(browseSrc.includes("tidalPlaylistSaveRowHtml('album')"),
        'TIDAL browse must use the full-width album variant like the Albums reference');
}

// --- TIDAL details: artist + playlist match the album width ----------------
{
    // Lookbehind excludes the function definition itself, so this counts
    // exactly the four render call sites (browse/album/artist/playlist).
    const calls = [...streamingJs.matchAll(/(?<!function )tidalPlaylistSaveRowHtml\(([^)]*)\)/g)].map((m) => m[1].trim());
    assert.equal(calls.length, 4, `expected the 4 save-row call sites (browse/album/artist/playlist), got ${calls.length}`);
    const plain = calls.filter((args) => args === '');
    assert.deepEqual(plain, [],
        'every TIDAL playlist action row (browse, album, artist, playlist) must use the album variant for the same width');
    for (const fn of ['renderTidalAlbum', 'renderTidalArtist', 'renderTidalPlaylist']) {
        const src = extractFunction(streamingJs, fn);
        assert.ok(src.includes("tidalPlaylistSaveRowHtml('album')"),
            `${fn} must use the album-variant row width`);
    }
}

// --- TIDAL CSS: the album variant is the full-width reference --------------
{
    const albumRule = css.match(/\.playlist-save-row\.tidal-playlist-save-row--album\s*\{[^}]*\}/);
    assert.ok(albumRule && /width:\s*100%/.test(albumRule[0]),
        'the TIDAL album row variant must use the full content width');
}

// --- Local library: top home shared by Tracks/Folders/Favorites ------------
{
    const rowPos = html.indexOf('id="playlist-save-row"');
    const tracksPos = html.indexOf('id="tracks-list"');
    assert.ok(rowPos >= 0 && tracksPos >= 0, 'library must contain the save row and the tracks list');
    assert.ok(rowPos < tracksPos,
        'Local Library must keep the save row BEFORE #tracks-list (Tracks/Folders/Favorites share the Albums top position)');
    const dockSrc = extractFunction(libraryJs, 'dockPlaylistSaveRow');
    assert.ok(dockSrc.includes('playlistDetail') && dockSrc.includes('playlistDetailTracks'),
        'dockPlaylistSaveRow must also dock inside the playlist detail (header/tracks) like the album detail');
    assert.ok(dockSrc.includes('tracksList'),
        'dockPlaylistSaveRow must home the row before the tracks list for Tracks/Folders/Favorites');
}

// --- Playlist detail: no special logic, selection survives navigation ----
// The detail follows exactly the same selection state as every other view:
// visibility is selection only, and opening (or switching) a playlist must
// not touch the existing track selection — a + in the local playlist shows
// the row normally, like everywhere else.
{
    const visSrc = extractFunction(libraryJs, 'updatePlaylistSaveRowVisibility');
    assert.ok(/const showRow = hasPlaylistSelection;/.test(visSrc),
        'the save row must be visible exactly when tracks are selected (no edit-state override)');
    assert.ok(!visSrc.includes('|| isEditingPlaylist'),
        'no stale edit-state clause may keep the row visible without a selection');
    const openSrc = extractFunction(libraryJs, 'openPlaylistDetail');
    assert.ok(!openSrc.includes('selectedTrackIds'),
        'opening (or switching) a playlist must preserve the existing track selection');
}

// --- No Delete in the save row: Save/Cancel only, separate delete path ----
// Playlist deletion lives outside the row (grid heart + tracks-list button);
// the row and its visibility carry no delete special logic anymore.
{
    assert.ok(!html.includes('id="delete-playlist"'),
        'the save row markup must not contain a Delete button');
    const visSrc = extractFunction(libraryJs, 'updatePlaylistSaveRowVisibility');
    assert.ok(!visSrc.includes('deletePlaylistBtn'),
        'the row visibility must not toggle any delete button');
    assert.ok(!libraryJs.includes('deletePlaylistBtn'),
        'no deletePlaylistBtn element reference may remain');
    assert.ok(!appJs.includes('deletePlaylistBtn') && !appJs.includes('delete-playlist'),
        'no deletePlaylistBtn wiring may remain in app.js either');
    const delSrc = extractFunction(libraryJs, 'deletePlaylistById');
    assert.ok(delSrc.includes("method: 'DELETE'"),
        'the separate playlist delete path must remain (grid heart + tracks-list button)');
}

// --- Radio: no shared playlist-build row, nothing to unify -----------------
{
    assert.ok(!radioJs.includes('playlist-save-row') && !radioJs.includes('tidal-playlist-save-row'),
        'Radio must not carry a playlist action row on the shared UI path');
    assert.ok(!radioJs.includes('data-streaming-add') && !radioJs.includes('selectedTrackIds'),
        'Radio must not share the TIDAL/library + playlist-build selection path');
    const radioPos = html.indexOf('id="tab-radio"');
    const libraryPos = html.indexOf('id="tab-library"');
    assert.ok(radioPos > 0 && libraryPos > radioPos,
        'the Radio tab must precede the Library tab in index.html (slice guard)');
    const radioTab = html.slice(radioPos, libraryPos);
    assert.ok(!radioTab.includes('playlist-save-row'),
        'the Radio tab must not contain a playlist action row');
}

// --- Vertical spacing: the TIDAL artist-detail reference everywhere -------
// Reference: the TIDAL artist detail shows 0.9rem of standoff above and
// below the row (14.4px at the 16px root size; verified out of band with a
// headless-browser sweep of all twelve views at desktop and phone widths).
// This file pins the CSS declarations that produce those values; it
// performs no browser measurement itself.
{
    // Home row: owns the reference standoff on both sides; top takes the
    // larger share against the actions-row/folder-path margin, bottom
    // against the grid margin.
    const baseRule = css.match(/\.playlist-save-row\s*\{[^}]*\}/);
    assert.ok(baseRule && /margin:\s*0\.9rem\s+0\s*;/.test(baseRule[0]),
        'the home row must own the 0.9rem reference standoff on both sides');
    // Neighbours converge on the same rhythm so max() lands on 0.9rem.
    const actionsRule = css.match(/\.library-actions-row\s*\{[^}]*\}/);
    assert.ok(actionsRule && /margin:\s*0\.15rem\s+0\s+0\.9rem\s*;/.test(actionsRule[0]),
        'the actions row must keep a 0.9rem bottom margin (top of the reference rhythm)');
    const gridRule = css.match(/\.albums-grid\s*\{[^}]*\}/);
    assert.ok(gridRule && /margin-top:\s*0\.9rem\s*;/.test(gridRule[0]),
        'the albums grid must keep a 0.9rem top margin (bottom of the reference rhythm)');
    // Docked library row: its margins pull against the same custom property
    // that sets the hero gap, so header/row and row/tracks both land on the
    // reference standoff at any viewport without the two declarations ever
    // drifting apart. The container gap itself is never touched, so the
    // track-list to discover spacing stays identical whether the row is
    // visible or not.
    const heroRule = css.match(/\.album-detail--hero\s*\{[^}]*\}/);
    assert.ok(heroRule && /--detail-hero-gap:\s*clamp\(1rem,\s*1\.8vw,\s*1\.5rem\)\s*;/.test(heroRule[0]) &&
        /gap:\s*var\(--detail-hero-gap\)\s*;/.test(heroRule[0]),
        'the local hero rule must set its gap from a single --detail-hero-gap property');
    const dockRule = css.match(/\.album-detail--hero\s*>\s*\.playlist-save-row\s*\{[^}]*\}/);
    assert.ok(dockRule && /margin-top:\s*calc\(0\.9rem\s*-\s*var\(--detail-hero-gap\)\)\s*;/.test(dockRule[0]) &&
        /margin-bottom:\s*calc\(0\.9rem\s*-\s*var\(--detail-hero-gap\)\s*-\s*0\.2rem\)\s*;/.test(dockRule[0]),
        'the docked library row must pull against the same property (never a duplicated literal)');
    assert.ok(dockRule && !/clamp\(/.test(dockRule[0]),
        'the docked row must not duplicate the gap expression');
    // No .album-detail:has(...) rule may set a gap: a future docked-header
    // style must not silently reintroduce the tracks-to-discover squeeze.
    assert.ok(!/\.album-detail:has\([^)]*\)\s*\{[^}]*gap:/.test(css),
        'no .album-detail:has(...) rule may set a gap');
    const discoverRule = css.match(/\.album-discover\s*\{[^}]*\}/);
    assert.ok(discoverRule && /margin-top:\s*0\.55rem\s*;/.test(discoverRule[0]),
        'the discover panel must keep its own top margin (constant in both row states)');
    // TIDAL browse keeps its own gap; the row adds the small difference to
    // reach the reference standoff on both sides.
    assert.ok(/\.streaming-browse\s*>\s*\.playlist-save-row\s*\{[^}]*margin:\s*0\.15rem\s+0\s*;[^}]*\}/.test(css),
        'the TIDAL browse row must add 0.15rem on both sides of the browse gap');
    // TIDAL detail: the container gap and both flanking margins are derived
    // from one token, so nothing here can be decided by source order and
    // the 0.9rem standoff survives any gap tuning. Both coupled values are
    // pinned: the token (with its current 0.7rem) and every consumer.
    const tidalDetailRule = css.match(/\.streaming-detail\s*\{[^}]*\}/);
    assert.ok(tidalDetailRule && /--tidal-detail-gap:\s*0\.7rem\s*;/.test(tidalDetailRule[0]) &&
        /gap:\s*var\(--tidal-detail-gap\)\s*;/.test(tidalDetailRule[0]),
        'the TIDAL detail must set its gap from a single --tidal-detail-gap property');
    const tidalRowRule = css.match(/\.streaming-detail\s*>\s*\.playlist-save-row\s*\{[^}]*\}/);
    assert.ok(tidalRowRule && /margin-top:\s*calc\(0\.9rem\s*-\s*var\(--tidal-detail-gap\)\)\s*;/.test(tidalRowRule[0]) &&
        /margin-bottom:\s*0\s*;/.test(tidalRowRule[0]),
        'the TIDAL detail row must pull against --tidal-detail-gap, not a duplicated literal');
    const tidalResultsRule = css.match(/\.streaming-detail--hero\s*>\s*\.streaming-results\s*\{[^}]*\}/);
    assert.ok(tidalResultsRule && /margin-top:\s*calc\(0\.9rem\s*-\s*var\(--tidal-detail-gap\)\)\s*;/.test(tidalResultsRule[0]),
        'the TIDAL results margin must pull against the same token (row-to-results stays the reference)');
    // The library and TIDAL results margins are the only remaining literal
    // complements of their gap; the docked library row mirrors its own, and
    // the TIDAL one is tokenised above.
    assert.ok(/\.album-detail--hero\s*>\s*\.album-detail-tracks\s*\{[^}]*margin-top:\s*0\.2rem\s*;[^}]*\}/.test(css),
        'the library tracks margin must keep its literal (mirrored by the docked row margin-bottom)');
    // Nothing may set a gap on the TIDAL hero root: that declaration would
    // tie with .streaming-detail at equal specificity and be decided purely
    // by source order, which is how the row margin drifted before.
    assert.ok(!/\.streaming-detail--hero\s*\{[^}]*gap:/.test(css) &&
        !/\.album-detail--hero,\s*\.streaming-detail--hero\s*\{[^}]*gap:/.test(css),
        'no hero rule may set a gap for the TIDAL root (it is owned by .streaming-detail)');
    // Overview content-start: the results containers keep their 0.35rem top
    // margin (subtab-to-content gap while the row is hidden), but while the
    // row is visible a container opening the body drops it — first tile/row
    // then starts at the reference standoff like in the Library overviews
    // (measured 14.4px everywhere) instead of jumping on tab switches.
    assert.ok(/#tidal-fav-results,\s*#tidal-playlists-results,\s*#tidal-search-items\s*\{[^}]*margin-top:\s*0\.35rem\s*;[^}]*\}/.test(css),
        'the results containers must keep their 0.35rem top margin for the hidden-row state');
    assert.ok(/\.streaming-browse:has\(>\s*#tidal-playlist-save-row:not\(\.hidden\)\)\s*>\s*#tidal-browse-body\s*>\s*#tidal-fav-results:first-child[\s\S]*?margin-top:\s*0\s*;/.test(css),
        'the favorites container must drop its top margin while the row is visible');
    assert.ok(/\.streaming-browse:has\(>\s*#tidal-playlist-save-row:not\(\.hidden\)\)\s*>\s*#tidal-browse-body\s*>\s*#tidal-playlists-results:first-child[\s\S]*?margin-top:\s*0\s*;/.test(css),
        'the playlists container must drop its top margin while the row is visible');
    // Parent gaps that complete the rhythm.
    assert.ok(/\.streaming-browse\s*\{[^}]*gap:\s*0\.75rem\s*;[^}]*\}/.test(css),
        'TIDAL browse must keep its 0.75rem gap (plus the row margins = reference)');
    // The TIDAL detail gap is asserted through its token above; here we only
    // pin that the value behind the token is still the 0.7rem the reference
    // rhythm was measured with, without matching the token's own name.
    assert.ok(/--tidal-detail-gap:\s*0\.7rem\s*;/.test(css),
        'the TIDAL detail gap token must still resolve to 0.7rem (row margins are derived from it)');
    // Responsive rules must not override the row's own margins: the 0.9rem
    // standoff holds below desktop widths too.
    for (const m of responsiveCss.matchAll(/\.playlist-save-row\s*\{[^}]*\}/g)) {
        assert.ok(!/margin-(top|bottom)\s*:/.test(m[0]),
            'no responsive rule may override the row vertical margins (uniform at every width)');
    }
}

console.log('PASS scripts/test_playlist_action_row.js');
