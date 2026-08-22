#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only
// Structural checks for the shared artist-enrichment UI on the TIDAL side.
//
// Verifies:
//  * TIDAL artist shows the About text directly visible (no accordion),
//  * TIDAL album keeps About collapsible through the shared library component,
//  * TIDAL album facts stay provider-primary and never duplicate,
//  * Discover Similar uses mapped artist ids or exactly one artist search,
//  * missing artist art is hydrated asynchronously and patches the existing
//    card when the search result arrives,
//  * a cached provider mapping avoids a re-search.

const assert = require('assert/strict');
const fs = require('fs');
const path = require('path');

const js = fs.readFileSync(path.join(__dirname, '..', 'static', 'streaming.js'), 'utf8');
const appJs = fs.readFileSync(path.join(__dirname, '..', 'static', 'app.js'), 'utf8');
const css = fs.readFileSync(path.join(__dirname, '..', 'static', 'style.css'), 'utf8');

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

// --- shared facts/about helpers are supplied by app.js ----------------------
assert.ok(appJs.includes('function detailFactsHtml('),
    'app.js must own the shared metadata-rows builder');
assert.ok(appJs.includes('function detailAboutHtml('),
    'app.js must own the shared collapsible About builder');
assert.ok(appJs.includes('factsHtml: detailFactsHtml') && appJs.includes('aboutHtml: detailAboutHtml'),
    'app.js must hand both shared builders to streaming.js');
assert.ok(js.includes("if (typeof api.factsHtml === 'function') factsHtml = api.factsHtml"),
    'streaming.js must receive the shared facts builder');
assert.ok(js.includes("if (typeof api.aboutHtml === 'function') aboutHtml = api.aboutHtml"),
    'streaming.js must receive the shared About builder');

// --- TIDAL artist: About directly visible ------------------------------------
// Test #5: the artist detail renders the About text in the hero (below the
// facts line), not behind an expandable accordion.
const artistRender = extractFunction(js, 'renderTidalArtist');
assert.ok(artistRender.includes('id="tidal-artist-about"'),
    'artist hero must carry an About container');
assert.ok(artistRender.includes('tidal-artist-about') && artistRender.includes('streaming-detail-main'),
    'artist About must live inside the hero/meta area');
assert.ok(!artistRender.includes('<details') && !artistRender.includes('<summary'),
    'artist About must not be an accordion on the artist page');

const artistLoad = extractFunction(js, 'loadTidalArtist');
assert.ok(artistLoad.includes("renderArtistAbout(content.querySelector('#tidal-artist-about'), data.enrichment && data.enrichment.about)"),
    'artist load must render the enrichment About text');
const aboutRender = extractFunction(js, 'renderArtistAbout');
assert.ok(aboutRender.includes('.streaming-about-text'),
    'artist About must render plain text markup');
assert.ok(aboutRender.includes('text.length > 200'),
    'long artist bios must be bounded');
assert.ok(aboutRender.includes('is-clamped') && aboutRender.includes("'More'") && aboutRender.includes("'Less'"),
    'long bios must clamp with a subtle More/Less toggle');
assert.ok(!aboutRender.includes('<details'),
    'artist About must never wrap in a details element');

// CSS for the direct about + clamp + toggle.
assert.ok(css.includes('.streaming-about-text') && css.includes('.streaming-about-text.is-clamped'),
    'clamped about text styles must ship');
assert.ok(css.includes('.about-more-toggle'),
    'the subtle about toggle style must ship');
assert.ok(css.includes('.tidal-artist-about'),
    'artist about container style must ship');

// --- TIDAL album: About collapsible + TIDAL-primary facts -------------------
// Test #6: the album keeps About collapsible through the shared library
// component (`<details class="album-detail-about">` produced by aboutHtml).
const albumRender = extractFunction(js, 'renderTidalAlbum');
assert.ok(albumRender.includes('id="tidal-album-about"'),
    'album detail must carry an About anchor');
const albumMetaRender = extractFunction(js, 'renderTidalAlbumMeta');
assert.ok(albumMetaRender.includes("aboutHtml('About this artist', about)"),
    'album About must render through the shared collapsible component');
assert.ok(albumMetaRender.includes('enrichment.artist.about'),
    'album About must come from the enrichment artist payload');

// Test #7: album facts stay TIDAL-primary and are never duplicated.
const factsRender = extractFunction(js, 'tidalAlbumFactsHtml');
assert.ok(factsRender.includes('meta.year') && factsRender.includes('tidalQualityLabel(meta.audio_quality)') && factsRender.includes('meta.num_tracks'),
    'album facts must lead with the operator year/quality/track count');
assert.ok(factsRender.includes('factsHtml(lines)'),
    'album facts must render through the shared rows builder');
assert.ok(factsRender.includes('supp.release_type') && factsRender.includes('supp.country') &&
    factsRender.includes('supp.label') && factsRender.includes('supp.genres'),
    'MusicBrainz supplement must only add release type/country/label/genres');
assert.ok(!factsRender.includes('supp.year') && !factsRender.includes('supp.audio_quality') && !factsRender.includes('supp.num_tracks'),
    'supplement must never re-present TIDAL year/quality/track count (no duplication, TIDAL wins)');

// --- Discover Similar -------------------------------------------------------
// Test #8 + #9: mapped id opens the artist directly; otherwise exactly one
// normal TIDAL artist search, falling back to the existing search-result flow.
const similarRender = extractFunction(js, 'loadTidalArtist');
assert.ok(similarRender.includes('renderSimilarArtists(results, similar)'),
    'artist detail must use the shared similar-artist renderer');
const similarGrid = extractFunction(js, 'renderSimilarArtists');
assert.ok(similarGrid.includes("'Discover Similar'") && similarGrid.includes('streaming-similar-grid') && similarGrid.includes('renderSimilarArtistItem(item)'),
    'similar artists must render as a card grid through one shared helper');
assert.ok(artistLoad.includes("hydrateSimilarArtistImages(results, similar, requestId, 'artist')"),
    'artist detail must hydrate missing similar-artist art after the first render');
const similarItem = extractFunction(js, 'renderSimilarArtistItem');
assert.ok(similarItem.includes("coverImg(item.art_url, 'eager')"),
    'similar cards must request available covers during the first render');
assert.ok(similarItem.includes("openTidalSimilarArtist(item)"),
    'similar card click must route through the similar-artist opener');
const cover = extractFunction(js, 'coverImg');
assert.ok(cover.includes('loading'),
    'shared cover markup must support an explicit loading priority');

const albumLoad = extractFunction(js, 'loadTidalAlbum');
assert.ok(albumLoad.includes('renderSimilarArtists(results, similar)') && albumLoad.includes('enrichment.similar'),
    'album detail must reuse the same Discover Similar card renderer');
assert.ok(albumLoad.includes("hydrateSimilarArtistImages(results, similar, requestId, 'album')"),
    'album detail must hydrate missing similar-artist art after the first render');

const hydrateSimilar = extractFunction(js, 'hydrateSimilarArtistImages');
assert.ok(hydrateSimilar.includes('resolveTidalArtistMatch(item.artist)'),
    'similar-art hydration must resolve each missing artist through the shared lookup');
assert.ok(hydrateSimilar.includes('.streaming-similar-item') && hydrateSimilar.includes('.streaming-result-cover'),
    'similar-art hydration must target the existing card instead of replacing the grid');
assert.ok(hydrateSimilar.includes("cover.innerHTML = coverImg(match.art_url, 'eager')"),
    'similar-art hydration must patch the card cover when the result arrives');
assert.ok(hydrateSimilar.includes('state.tidal.detailRequestId') && hydrateSimilar.includes('state.tidal.view'),
    'late similar-art responses must be ignored after leaving the detail');

const openSimilar = extractFunction(js, 'openTidalSimilarArtist');
assert.ok(openSimilar.includes('item.provider_artist_id'),
    'a cached provider mapping must open the artist directly');
assert.ok(openSimilar.includes("openTidalArtist(item.provider_artist_id"),
    'mapped similar artists jump straight into the artist detail');
assert.ok(openSimilar.includes('resolveTidalArtistByName(name)'),
    'unmapped similar artists trigger exactly one name resolver');

const resolveByName = extractFunction(js, 'resolveTidalArtistByName');
assert.ok(resolveByName.includes('resolveTidalArtistMatch(name)'),
    'similar-artist navigation must reuse the shared artist lookup');
assert.ok(resolveByName.includes("openTidalArtist(match.id"),
    'a single unique match must open the matching TIDAL artist');
assert.ok(resolveByName.includes('showTidalSearchResultsFor(name)'),
    'an ambiguous fallback must reuse the existing search-result flow');

const resolveMatch = extractFunction(js, 'resolveTidalArtistMatch');
assert.ok(resolveMatch.includes('artistLookupCache') && resolveMatch.includes('artistLookupPromises'),
    'similar-art lookup must cache resolved artists and deduplicate concurrent searches');
assert.ok(resolveMatch.includes("'/api/streaming/tidal/search?q='"),
    'similar-art lookup must use the normal TIDAL artist search endpoint');

const searchFallback = extractFunction(js, 'showTidalSearchResultsFor');
assert.ok(searchFallback.includes('searchQuery = name') && searchFallback.includes("searchResultType = 'artists'"),
    'the fallback must seed the permanent search bar with the artist name');
assert.ok(searchFallback.includes("executeTidalSearch('artists')"),
    'the fallback must render results through the existing executed-search flow');

// --- no live per-keystroke similar search -----------------------------------
assert.ok(!js.includes("openTidalSimilarArtist((input.value || '').trim())"),
    'similar-artist navigation must never run a live per-keystroke search');

// --- style for the similar grid ---------------------------------------------
const similarCssStart = css.indexOf('.streaming-similar-grid');
const similarCssEnd = css.indexOf('}', similarCssStart);
const similarCss = css.slice(similarCssStart, similarCssEnd + 1);
assert.ok(similarCss.includes('grid-template-columns: repeat(auto-fit, minmax(var(--tile-min), var(--tile-min)))') &&
    similarCss.includes('justify-content: center') && similarCss.includes('margin: 0.5rem auto 0'),
    'similar cards must use the same centered tile alignment as album cards');
const similarCoverStart = css.indexOf('.streaming-similar-item .streaming-result-cover');
const similarCoverEnd = css.indexOf('}', similarCoverStart);
const similarCoverCss = css.slice(similarCoverStart, similarCoverEnd + 1);
assert.ok(similarCoverCss.includes('height: auto'),
    'similar covers must override the row cover height to stay square');
assert.ok(css.includes('.streaming-similar-item'),
    'similar item styles must ship');

console.log('PASS  scripts/test_tidal_enrichment_ui.js');
