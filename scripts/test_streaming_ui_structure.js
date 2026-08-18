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
    'caps.audio_format',
    'caps.bit_depth',
    'caps.sample_rate',
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

// --- quality formatting is capability-gated -------------------------------------
const formatQuality = extractFunction(js, 'formatQuality');
assert.ok(formatQuality.includes('caps.audio_format'));
assert.ok(formatQuality.includes('caps.bit_depth'));
assert.ok(formatQuality.includes('caps.sample_rate'));

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
assert.ok(js.includes("state.tidal.searchQuery = query"),
    'executed Tidal searches must record the query');
assert.ok(js.includes("state.tidal.searchQuery = ''"),
    'clearing Tidal search must reset the executed query');
assert.ok(js.includes("Search Tidal for music."),
    'Tidal search must have a neutral initial/cleared state');
assert.ok(js.includes("results.innerHTML = '<p class=\"streaming-note\">Search Tidal for music.</p>'"),
    'empty Tidal search must remove old results and show the neutral state');

// Catalog providers must not render a second in-tab now-playing card: the
// global footer is the authoritative player, so the card stays hidden.
const nowPlayingRender = js.slice(js.indexOf('function renderNowPlaying'), js.indexOf('function showEmpty'));
assert.ok(nowPlayingRender.includes('meta.catalog'),
    'catalog providers must be routed through the catalog flag');
assert.ok(nowPlayingRender.includes('els.nowPlaying.hidden = true'),
    'catalog providers must keep the in-tab now-playing card hidden');

// The visible provider status must not surface raw backend identifiers.
const statusLineRender = extractFunction(js, 'renderStatusLine');
assert.ok(!statusLineRender.includes('qbzd'), 'status line must not surface the qbzd backend name');
assert.ok(!statusLineRender.includes('tidalapi'), 'status line must not surface the tidalapi backend name');
assert.ok(statusLineRender.includes("PROVIDER_META.tidal.name"),
    'Tidal status must use the visible provider name');

// The served navigation changes visible copy only, not the provider id.
assert.ok(html.includes('data-tab="tidal"') && html.includes('<span>Tidal</span>'),
    'Tidal navigation must use title case while retaining its tidal tab id');
assert.ok(!html.includes('<span>TIDAL</span>'), 'served navigation must not use all-caps TIDAL');

// Footer clearance is part of the streaming content layout contract.
assert.ok(css.includes('.tab-content') && css.includes('padding: 1rem 1.25rem calc(1.5rem + var(--playback-footer-space))'),
    'tab content must reserve the centralized footer safe area');

console.log('PASS  scripts/test_streaming_ui_structure.js');
