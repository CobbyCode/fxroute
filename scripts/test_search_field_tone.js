#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only
// Structural checks for the shared search-field tone.
//
// Radio, TIDAL and Library each render a search input, but the background
// arrived through different rules: the radio file carries an unscoped
// translucent `.station-search-input, .url-input` override that also tints
// every `.url-input` (including the library search), while the TIDAL input
// keeps the opaque token. The three fields must render the identical gray,
// so one shared rule pins the same opaque background and border on all of
// them with selectors that beat the translucent override by specificity.

const assert = require('assert/strict');
const fs = require('fs');
const path = require('path');

const root = path.join(__dirname, '..');
const css = fs.readFileSync(path.join(root, 'static', 'style.css'), 'utf8');
const libraryCss = fs.readFileSync(path.join(root, 'static', 'css', '_library.css'), 'utf8');

const sharedRule = css.match(/#station-search,\s*#tidal-search-input,\s*\.library-search-field \.library-search-input\s*\{[^}]*\}/);
assert.ok(sharedRule, 'style.css must ship one shared rule for all three search inputs');
assert.ok(/background:\s*var\(--bg-input\)/.test(sharedRule[0]),
    'the shared search rule must use the same opaque background');
assert.ok(/border-color:\s*var\(--border\)/.test(sharedRule[0]),
    'the shared search rule must use the same border');

// The shared rule must live in a source partial (not hand-patched into the
// built artifact), so the css rebuild contract keeps holding.
assert.ok(libraryCss.includes('#tidal-search-input'),
    '_library.css must host the shared search-tone rule');

// No other background may compete for these inputs after the shared rule:
// the only remaining input backgrounds are the token base and the
// (weaker, earlier) translucent radio override it intentionally beats.
for (const m of css.matchAll(/([^{}]+)\{[^}]*background:[^}]*\}/g)) {
    const selector = m[1];
    if (/#station-search|#tidal-search-input/.test(selector) && !m[0].includes('var(--bg-input)')) {
        assert.fail(`competing background for the search inputs: ${selector.trim()}`);
    }
}

console.log('PASS scripts/test_search_field_tone.js');
