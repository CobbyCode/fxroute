#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only
// TIDAL branding contract: official, unmodified white artwork; full logo where
// there is room; 13 px minimum height; never over cover art; links back to
// TIDAL that stay out of the remote/keyboard focus order.

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const repoRoot = path.resolve(__dirname, '..');
const read = (...parts) => fs.readFileSync(path.join(repoRoot, ...parts), 'utf8');
const helpers = require('../static/ui_helpers.js');

let checks = 0;
function check(name, condition) {
    assert.ok(condition, name);
    checks += 1;
}

// --- artwork files ---------------------------------------------------------
// First path data of TIDAL's own embed-player symbols (tidalLongLogo, tidalLogo).
const OFFICIAL_LOGO_PATH_START = 'M9.13281 6.75L6.84961 9L4.56641 6.75L6.84961 4.5L9.13281 6.75Z';
const OFFICIAL_DIAMOND_PATH_START = 'M2.6667 2.40002L0 5.06646L2.6667 7.7329L5.33341 5.06646L2.6667 2.40002Z';
const logoSvg = read('static', 'tidal-logo-white.svg');
const diamondSvg = read('static', 'tidal-diamond-white.svg');
check('the logo file keeps the official path data', logoSvg.includes(`d="${OFFICIAL_LOGO_PATH_START}`));
check('the diamond file keeps the official path data', diamondSvg.includes(`d="${OFFICIAL_DIAMOND_PATH_START}`));
check('the logo is the white variant at the official 69:9 proportion',
    /viewBox="0 0 69 9"/.test(logoSvg) && /fill="#fff"/.test(logoSvg));
check('the diamond is the white variant', /fill="#fff"/.test(diamondSvg));
check('no recolouring or extra shapes in the artwork files',
    (logoSvg.match(/<path/g) || []).length === 1 && (diamondSvg.match(/<path/g) || []).length === 2
    && !/fill="#(?!fff)/i.test(logoSvg + diamondSvg) && !/<(circle|rect|text)/.test(logoSvg + diamondSvg));

// --- helper markup ---------------------------------------------------------
check('only TIDAL has a source mark', helpers.sourceGlyphHtml('spotify') === '');

const diamond = helpers.sourceGlyphHtml('tidal');
check('the default mark is the diamond file in a labelled span',
    diamond.startsWith('<span ') && diamond.includes('/static/tidal-diamond-white.svg')
    && diamond.includes('aria-label="TIDAL"') && !diamond.includes('<svg') && !diamond.includes('<a '));
check('the diamond marker keeps the legacy class', /class="[^"]*source-glyph--tidal/.test(diamond));

const card = helpers.sourceGlyphHtml('tidal', '', { variant: 'card', link: { kind: 'album', id: '123' } });
check('a card mark carries both files: logo in grids, diamond in lists',
    card.includes('/static/tidal-logo-white.svg') && card.includes('/static/tidal-diamond-white.svg'));
check('a card mark links to the item on TIDAL',
    card.startsWith('<a ') && card.includes('href="https://tidal.com/browse/album/123"')
    && card.includes('target="_blank"') && card.includes('rel="noopener noreferrer"'));
check('the brand link is out of the focus order and does not open the card',
    card.includes('tabindex="-1"') && card.includes('onclick="event.stopPropagation()"'));
check('a linked mark stays labelled', card.includes('aria-label="TIDAL"'));
check('the marks load at high priority so offscreen cards do not wait behind cover requests',
    (card.match(/fetchpriority="high"/g) || []).length === 2 && diamond.includes('fetchpriority="high"')
    && /library-tidal-status[\s\S]*?fetchpriority="high"[\s\S]*?<\/button>/.test(read('static', 'index.html')));

const heading = helpers.sourceGlyphHtml('tidal', '', { variant: 'heading', link: true, decorative: true });
check('a heading mark is the full logo only', heading.includes('tidal-logo-white.svg') && !heading.includes('tidal-diamond-white.svg'));
check('a generic link goes to the TIDAL home page', heading.includes('href="https://tidal.com"'));
check('a decorative mark is hidden from assistive tech', heading.includes('aria-hidden="true"') && !heading.includes('aria-label'));

check('tidalWebUrl builds item pages',
    helpers.tidalWebUrl('artist', 42) === 'https://tidal.com/browse/artist/42'
    && helpers.tidalWebUrl('playlist', 'a-b') === 'https://tidal.com/browse/playlist/a-b'
    && helpers.tidalWebUrl('track', '7') === 'https://tidal.com/browse/track/7');
check('tidalWebUrl escapes ids', helpers.tidalWebUrl('album', 'x/y?z') === 'https://tidal.com/browse/album/x%2Fy%3Fz');
check('tidalWebUrl falls back to the home page',
    helpers.tidalWebUrl('album', null) === 'https://tidal.com' && helpers.tidalWebUrl('unknown', 1) === 'https://tidal.com'
    && helpers.tidalWebUrl() === 'https://tidal.com');

// --- placement and size in the shipped sources -----------------------------
const librarySource = read('static', 'library_ui.js');
check('no TIDAL mark is rendered inside a cover wrapper',
    !/album-art-wrap">[^\n]*sourceGlyphHtml/.test(librarySource) && !librarySource.includes('source-glyph--tile'));

const tokens = read('static', 'css', '_tokens.css');
const markHeight = /--tidal-mark-h:\s*max\(\s*(\d+)px/.exec(tokens);
check('the mark height token never drops below TIDAL\'s 13 px minimum', markHeight && Number(markHeight[1]) >= 13);
const allCss = ['_base.css', '_library.css'].map(file => read('static', 'css', file)).join('\n');
check('no cover-overlay styling is left', !allCss.includes('source-glyph--tile'));
check('the shipped stylesheet carries the brand rules', read('static', 'style.css').includes('.source-glyph--card'));

const indexSource = read('static', 'index.html');
const statusButton = /<button id="library-tidal-status"[\s\S]*?<\/button>/.exec(indexSource);
check('the header button uses the official diamond file, not a redrawn glyph',
    statusButton && statusButton[0].includes('/static/tidal-diamond-white.svg') && !statusButton[0].includes('<svg'));

console.log(`PASS  scripts/test_tidal_branding.js (${checks} checks)`);
