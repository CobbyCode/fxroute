#!/usr/bin/env node
// Regression checks for the Radio direct-search interaction.

const assert = require('assert/strict');
const fs = require('fs');
const path = require('path');

const root = path.join(__dirname, '..');
const html = fs.readFileSync(path.join(root, 'static', 'index.html'), 'utf8');
const radioJs = fs.readFileSync(path.join(root, 'static', 'radio.js'), 'utf8');
const css = fs.readFileSync(path.join(root, 'static', 'style.css'), 'utf8');

function extractFunction(source, name) {
    const match = new RegExp(`function\\s+${name}\\s*\\(`).exec(source);
    assert.ok(match, `missing ${name}`);
    const brace = source.indexOf('{', match.index);
    assert.notEqual(brace, -1, `missing body ${name}`);
    let depth = 0;
    let quote = '';
    let escaped = false;
    for (let index = brace; index < source.length; index += 1) {
        const character = source[index];
        if (quote) {
            if (escaped) escaped = false;
            else if (character === '\\') escaped = true;
            else if (character === quote) quote = '';
            continue;
        }
        if (`'\"\``.includes(character)) quote = character;
        else if (character === '{') depth += 1;
        else if (character === '}' && --depth === 0) return source.slice(match.index, index + 1);
    }
    throw new Error(`unterminated ${name}`);
}

const setupActions = extractFunction(radioJs, 'setupStationActions');
const clearOnlineResults = extractFunction(radioJs, 'clearOnlineResults');
const searchOnlineStations = extractFunction(radioJs, 'searchOnlineStations');
const renderSearchResults = extractFunction(radioJs, 'renderSearchResults');

assert.equal(html.includes('station-search-online'), false,
    'Radio direct search must not render a separate Web Search button');
assert.match(html, /id="station-search-grid" class="radio-search-groups"/,
    'grouped direct-search results must not reuse the station card grid on the outer container');
assert.match(html, /id="station-search-status"[^>]*role="status"[^>]*aria-live="polite"/,
    'direct-search status must be announced to assistive technology');
assert.match(setupActions, /scheduleOnlineStationSearch\(\)/,
    'typing in the station field must schedule the web search');
assert.doesNotMatch(setupActions, /searchOnlineStations\(\)/,
    'typing must not issue the web search synchronously');
assert.match(radioJs, /function\s+scheduleOnlineStationSearch\s*\(/,
    'Radio direct search must have a debounce scheduler');
assert.match(radioJs, /setTimeout\(\(\)\s*=>\s*\{[\s\S]*?searchOnlineStations\([^)]*\)[\s\S]*?\},\s*(?:\d+|ONLINE_SEARCH_DEBOUNCE_MS)\)/,
    'Radio web search must be debounced');
assert.match(clearOnlineResults, /clearTimeout\(/,
    'clearing or replacing a query must cancel its pending web search');
assert.match(searchOnlineStations, /requestId\s*!==\s*state\.onlineRequestId/,
    'late Radio Browser responses must be ignored');
assert.match(renderSearchResults, /station-result-group/,
    'search results must group local and web sources visibly');
assert.match(renderSearchResults, /My Stations/,
    'saved local stations must be labelled in direct search results');
assert.match(renderSearchResults, /Web Results/,
    'Radio Browser stations must be labelled in direct search results');

assert.match(css, /\.radio-panel-header\s*\{[\s\S]*?grid-template-columns:\s*minmax\(0,\s*1fr\)\s+minmax\(420px,\s*820px\)\s+minmax\(0,\s*1fr\)/,
    'Radio search must use equal side tracks to center the header field');
assert.match(css, /@media\s*\(min-width:\s*1200px\)[\s\S]*?\.radio-search-input-wrap\s*\{[\s\S]*?margin-inline:\s*6\.5rem;/,
    'desktop Radio search must preserve its width with centered margins');
assert.match(css, /@media\s*\(min-width:\s*900px\)[\s\S]*?\.radio-search-input-wrap\s*\{[\s\S]*?margin-inline:\s*5\.125rem;/,
    'tablet Radio search must preserve its width with centered margins');
assert.match(css, /@media\s*\(min-width:\s*701px\)\s*and\s*\(max-width:\s*1199px\)[\s\S]*?\.radio-panel-header\s*\{[\s\S]*?minmax\(420px,\s*calc\(100%\s*-\s*13\.8rem\)\)/,
    'tablet Radio header must preserve the previous search track width');
assert.match(css, /\.catalog-station-card\s+\.station-name\s*\{[\s\S]*?min-height:\s*calc\(1\.3em\s*\*\s*2\);/,
    'catalog cards must reserve two title lines');
assert.match(css, /\.catalog-station-card\s+\.catalog-station-action\s*\{[\s\S]*?margin-top:\s*auto;/,
    'catalog actions must share the same bottom line');
assert.match(css, /\.radio-search-results[\s\S]*?\.station-name[\s\S]*?min-height:\s*(?:calc\([^;]+\)|[^;]+);/,
    'direct-search station names must reserve two lines');
assert.match(css, /\.radio-search-results[\s\S]*?\.catalog-station-card\s+\.catalog-station-action[\s\S]*?margin-top:\s*auto;/,
    'direct-search actions must align at the bottom of every result card');

console.log('PASS scripts/test_radio_direct_search.js');
