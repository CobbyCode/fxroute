#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only
// Structural checks for the shared missing-artwork placeholder.

const assert = require('assert/strict');
const fs = require('fs');
const path = require('path');

const root = path.join(__dirname, '..');
const appJs = fs.readFileSync(path.join(root, 'static', 'app.js'), 'utf8');
const streamingJs = fs.readFileSync(path.join(root, 'static', 'streaming.js'), 'utf8');
const indexHtml = fs.readFileSync(path.join(root, 'static', 'index.html'), 'utf8');
const css = fs.readFileSync(path.join(root, 'static', 'style.css'), 'utf8');
const libraryCss = fs.readFileSync(path.join(root, 'static', 'css', '_library.css'), 'utf8');
const streamingCss = fs.readFileSync(path.join(root, 'static', 'css', '_streaming.css'), 'utf8');
const svg = fs.readFileSync(path.join(root, 'static', 'artwork-placeholder.svg'), 'utf8');

assert.match(svg, /<svg\b[^>]*viewBox="0 0 200 200"/,
    'the artwork placeholder must be a square SVG');
assert.match(svg, /<radialGradient\b/, 'the artwork placeholder must use tonal depth');
assert.ok((svg.match(/<circle\b/g) || []).length >= 8,
    'the artwork placeholder must show multiple record grooves and a center label');
assert.match(svg, /stroke-width="[23]"/, 'the artwork placeholder must define a clear outer edge');
assert.match(svg, /<path\b[^>]*opacity=/, 'the artwork placeholder must contain a restrained highlight');
assert.doesNotMatch(svg, /<text\b|[♫♪♬]/,
    'the artwork placeholder must not contain text or music-note glyphs');
assert.doesNotMatch(svg, /#(?:6ee7b7|4ade80|6366f1|8b5cf6)/i,
    'the artwork placeholder must stay neutral rather than use an accent color');

assert.match(appJs, /function artworkPlaceholderUrl\(\)/,
    'app.js must own the shared artwork placeholder URL');
assert.match(appJs, /artwork-placeholder\.svg\?v=\d+/,
    'the shared artwork placeholder URL must be cache-busted');
assert.match(appJs, /function trackThumbHtml[\s\S]*artworkPlaceholderUrl\(\)/,
    'library track thumbnails must use the shared artwork placeholder');
assert.match(appJs, /function albumArtFallbackSvg[\s\S]*artworkPlaceholderUrl\(\)/,
    'library album artwork fallbacks must use the shared artwork placeholder');

assert.match(streamingJs, /artworkPlaceholderUrl/,
    'streaming artwork rendering must receive the shared artwork placeholder');
assert.doesNotMatch(streamingJs, /<span class="detail-cover-placeholder"[^>]*>\s*[♫♪♬]/,
    'streaming detail artwork must not render a music-note fallback');
assert.match(indexHtml, /artwork-placeholder\.svg\?v=\d+/,
    'the playback artwork fallback must use the shared artwork asset');

assert.doesNotMatch(libraryCss, /\.track-thumb:empty::after\s*\{[^}]*content:\s*['"][♫♪♬]/,
    'shared track thumbnails must not render a music-note fallback');
assert.doesNotMatch(streamingCss, /\.streaming-result-cover:empty::after\s*\{[^}]*content:\s*['"][♫♪♬]/,
    'streaming result covers must not render a music-note fallback');
assert.doesNotMatch(css, /\.streaming-detail--hero \.detail-hero-cover:empty::before\s*\{[^}]*content:\s*['"][♫♪♬]/,
    'detail hero covers must not render a music-note fallback');
assert.match(libraryCss, /\.track-thumb:empty[\s\S]*artwork-placeholder\.svg/,
    'shared track thumbnails must point at the shared artwork asset');
assert.match(streamingCss, /\.streaming-result-cover:empty[\s\S]*artwork-placeholder\.svg/,
    'streaming result covers must point at the shared artwork asset');

console.log('PASS  scripts/test_artwork_placeholder.js');
