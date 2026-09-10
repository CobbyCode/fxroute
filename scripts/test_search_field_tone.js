#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only
// Structural checks for the shared search-field tone.
//
// Radio, TIDAL and Library each render a search input, but the background
// arrived through different rules: the radio file carries an unscoped
// translucent `.station-search-input, .url-input` override that also tints
// every `.url-input` (including the library search), while the TIDAL input
// keeps the opaque token. The three fields must render the identical gray,
// so one shared rule pins the same opaque background on all of them, and a
// second rule pins the same border — but only while the field is not
// focused, so `:focus` keeps its accent border instead of being
// re-overridden by ID specificity.

const assert = require('assert/strict');
const fs = require('fs');
const path = require('path');

const root = path.join(__dirname, '..');
const css = fs.readFileSync(path.join(root, 'static', 'style.css'), 'utf8');
const libraryCss = fs.readFileSync(path.join(root, 'static', 'css', '_library.css'), 'utf8');

// Background: pinned in every state (no :focus rule touches the background),
// so the three fields never fall back to the translucent override.
const bgRule = css.match(/#station-search,\s*#tidal-search-input,\s*\.library-search-field \.library-search-input\s*\{[^}]*\}/);
assert.ok(bgRule, 'style.css must ship one shared background rule for all three search inputs');
assert.ok(/background:\s*var\(--bg-input\)/.test(bgRule[0]),
    'the shared background rule must use the same opaque background');

// Border: pinned only while unfocused, or the ID specificity would beat
// `.station-search-input:focus` / `.streaming-search-input:focus` and kill
// the accent border on focus.
const borderRule = css.match(/#station-search:not\(:focus\),\s*#tidal-search-input:not\(:focus\),\s*\.library-search-field \.library-search-input:not\(:focus\)\s*\{[^}]*\}/);
assert.ok(borderRule, 'style.css must ship one shared border rule gated to :not(:focus)');
assert.ok(/border-color:\s*var\(--border\)/.test(borderRule[0]),
    'the shared border rule must use the same border');

// The shared rules must live in a source partial (not hand-patched into the
// built artifact), so the css rebuild contract keeps holding.
assert.ok(libraryCss.includes('#tidal-search-input'),
    '_library.css must host the shared search-tone rules');

// Radio and TIDAL keep their accent focus border: their :focus rules must
// set it, and no border-color rule may target the search IDs without a
// :not(:focus) gate (which would beat the :focus rule by specificity).
for (const name of ['station-search-input', 'streaming-search-input']) {
    const focusRule = css.match(new RegExp(`\\.${name}:focus\\s*\\{[^}]*\\}`));
    assert.ok(focusRule, `style.css must ship a :focus rule for .${name}`);
    assert.ok(/border-color:\s*var\(--accent\)/.test(focusRule[0]),
        `.${name}:focus must set the accent border`);
}
for (const m of css.matchAll(/([^{}]+)\{[^}]*border-color:[^}]*\}/g)) {
    const selector = m[1];
    if (/#station-search|#tidal-search-input/.test(selector) && !selector.includes(':not(:focus)')) {
        assert.fail(`un-gated border-color competes with the focus accent: ${selector.trim()}`);
    }
}

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