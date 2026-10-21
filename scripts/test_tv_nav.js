#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only
// Focused checks for the device-independent remote/keyboard navigation:
// key intents, spatial focus scoring and the page/CSS wiring. The rendered
// behaviour (focus moves, dialogs, tab strip) is covered by test_tv_nav.py.

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const TvNav = require('../static/tv_nav.js');
const repoRoot = path.resolve(__dirname, '..');
const indexSource = fs.readFileSync(path.join(repoRoot, 'static', 'index.html'), 'utf8');
const navSource = fs.readFileSync(path.join(repoRoot, 'static', 'tv_nav.js'), 'utf8');
const modalSource = fs.readFileSync(path.join(repoRoot, 'static', 'modal.js'), 'utf8');
const baseCss = fs.readFileSync(path.join(repoRoot, 'static', 'css', '_base.css'), 'utf8');
const overlaysCss = fs.readFileSync(path.join(repoRoot, 'static', 'css', '_overlays.css'), 'utf8');

// ---------------------------------------------------------------------------
// Key intents
// ---------------------------------------------------------------------------
const intents = {
    ArrowUp: 'nav-up',
    ArrowDown: 'nav-down',
    ArrowLeft: 'nav-left',
    ArrowRight: 'nav-right',
    // Page Up goes right to the next page, Page Down left to the previous.
    PageUp: 'tabs-next',
    PageDown: 'tabs-previous',
    Backspace: 'back',
    BrowserBack: 'browser-back',
    ContextMenu: 'source-picker',
    Compose: 'source-picker',
    Enter: 'activate',
    ' ': 'activate',
};
for (const [key, action] of Object.entries(intents)) {
    assert.equal(TvNav.keyAction({ key }), action, `key ${key}`);
}
assert.equal(TvNav.keyAction({ key: 'ArrowLeft', altKey: true }), 'browser-back',
    'remotes may send the desktop back shortcut');
assert.equal(TvNav.keyAction({ key: 'ArrowRight', altKey: true }), 'nav-right');
assert.equal(TvNav.keyAction({ key: 'a' }), null);
assert.equal(TvNav.keyAction({ key: 'Tab' }), null, 'Tab keeps native focus traversal');
assert.equal(TvNav.keyAction({ key: 'Escape' }), null, 'Escape stays with the modal manager');
// Legacy keyCode fallbacks: Firefox reports BrowserBack with keyCode 0, but
// remotes/older engines may only provide the legacy code.
assert.equal(TvNav.keyAction({ key: 'Unidentified', keyCode: 166 }), 'browser-back');
assert.equal(TvNav.keyAction({ key: 'Unidentified', keyCode: 93 }), 'source-picker');
assert.equal(TvNav.keyAction({ key: 'Home' }), null,
    'the keyboard Home key keeps scrolling, caret and slider handling');

// ---------------------------------------------------------------------------
// Native key owners
// ---------------------------------------------------------------------------
function fakeInput(type, tagName = 'INPUT') {
    return { nodeType: 1, tagName, isContentEditable: false,
        getAttribute: (name) => (name === 'type' ? type : null) };
}
assert.equal(TvNav.isTextEntry(fakeInput('text')), true);
assert.equal(TvNav.isTextEntry(fakeInput('search')), true);
assert.equal(TvNav.isTextEntry(fakeInput(null, 'TEXTAREA')), true);
assert.equal(TvNav.isTextEntry({ nodeType: 1, tagName: 'DIV', isContentEditable: true,
    getAttribute: () => null }), true);
assert.equal(TvNav.isTextEntry(fakeInput('range')), false);
assert.equal(TvNav.isTextEntry(fakeInput(null, 'SELECT')), false);

assert.equal(TvNav.isNativeArrowOwner(fakeInput('range'), 'left'), true,
    'slider left/right stay native');
assert.equal(TvNav.isNativeArrowOwner(fakeInput('range'), 'right'), true);
assert.equal(TvNav.isNativeArrowOwner(fakeInput('range'), 'up'), false,
    'up/down leave a slider instead of trapping the focus');
assert.equal(TvNav.isNativeArrowOwner(fakeInput('range'), 'down'), false);
assert.equal(TvNav.isNativeArrowOwner(fakeInput(null, 'SELECT'), 'down'), false,
    'a closed dropdown must not trap the remote arrows');
assert.equal(TvNav.isNativeArrowOwner(fakeInput(null, 'SELECT'), 'up'), false);
assert.equal(TvNav.isNativeArrowOwner(fakeInput('text'), 'left'), true,
    'typing owns all keys');
assert.equal(TvNav.isNativeArrowOwner(fakeInput('checkbox'), 'down'), false,
    'checkboxes are still reachable with arrows');

// ---------------------------------------------------------------------------
// Spatial picking
// ---------------------------------------------------------------------------
function rect(left, top, width = 100, height = 40) {
    return { left, top, width, height, right: left + width, bottom: top + height };
}
const from = rect(0, 0);
const right = { element: 'right', rect: rect(110, 0) };
const down = { element: 'down', rect: rect(0, 60) };
const left = { element: 'left', rect: rect(-110, 0) };
const up = { element: 'up', rect: rect(0, -60) };
const diagonal = { element: 'diagonal', rect: rect(110, 60) };
const candidates = [right, down, left, up, diagonal];

assert.equal(TvNav.pickSpatial(from, candidates, 'right'), 'right');
assert.equal(TvNav.pickSpatial(from, candidates, 'down'), 'down');
assert.equal(TvNav.pickSpatial(from, candidates, 'left'), 'left');
assert.equal(TvNav.pickSpatial(from, candidates, 'up'), 'up');
assert.equal(TvNav.pickSpatial(from, [right], 'left'), null, 'nothing behind the focus');
assert.equal(TvNav.pickSpatial(from, [left], 'right'), null);
assert.equal(TvNav.pickSpatial(null, candidates, 'right'), null);

// Same-row travel wins over a diagonal element at a comparable distance.
{
    const rowMate = { element: 'row', rect: rect(160, 2) };
    const nearDiagonal = { element: 'diag', rect: rect(160, 60) };
    assert.equal(TvNav.pickSpatial(from, [nearDiagonal, rowMate], 'right'), 'row');
}
// Left/Right stay in the row: a far same-row element beats a much closer
// control of another row (Library tabs -> search field, not the header).
{
    const farRowMate = { element: 'far-row', rect: rect(420, 2) };
    const nearDiagonal = { element: 'diag', rect: rect(160, 60) };
    assert.equal(TvNav.pickSpatial(from, [farRowMate, nearDiagonal], 'right'), 'far-row');
    assert.equal(TvNav.pickSpatial(from, [nearDiagonal], 'right'), 'diag',
        'an empty row falls back to the nearest control of another row');
    const behind = { element: 'behind', rect: rect(-420, 2) };
    assert.equal(TvNav.pickSpatial(from, [behind, nearDiagonal], 'right'), 'diag',
        'a row mate behind the focus is not a candidate');
    const aboveRow = { element: 'above', rect: rect(260, -50) };
    assert.equal(TvNav.pickSpatial(from, [aboveRow, farRowMate], 'right'), 'far-row');
}
// The nearest element in the direction wins over a far one.
{
    const near = { element: 'near', rect: rect(0, 60) };
    const far = { element: 'far', rect: rect(0, 600) };
    assert.equal(TvNav.pickSpatial(from, [far, near], 'down'), 'near');
}
// Overlapping horizontally is preferred to a same-distance off-row target.
{
    const aligned = { element: 'aligned', rect: rect(10, 50) };
    const offset = { element: 'offset', rect: rect(300, 50) };
    assert.equal(TvNav.pickSpatial(from, [offset, aligned], 'down'), 'aligned');
}
assert.equal(typeof TvNav.spatialScore(from, rect(110, 0), 'right'), 'number');
assert.equal(TvNav.spatialScore(from, rect(-110, 0), 'right'), null);

// ---------------------------------------------------------------------------
// Tab stepping
// ---------------------------------------------------------------------------
assert.equal(TvNav.stepTabIndex(0, 4, 1), 1);
assert.equal(TvNav.stepTabIndex(3, 4, 1), 0, 'PageDown wraps');
assert.equal(TvNav.stepTabIndex(0, 4, -1), 3, 'PageUp wraps');
assert.equal(TvNav.stepTabIndex(-1, 4, 1), 1, 'no active tab starts at the first');
assert.equal(TvNav.stepTabIndex(0, 0, 1), -1);

// ---------------------------------------------------------------------------
// Wiring
// ---------------------------------------------------------------------------
assert.match(indexSource, /tv_nav\.js\?v=\d+\.\d+\.\d+/);
assert.ok(indexSource.indexOf('tv_nav.js') < indexSource.indexOf('/static/app.js'),
    'the navigation module must load before app.js');
assert.ok(indexSource.indexOf('modal.js') < indexSource.indexOf('tv_nav.js'),
    'the navigation module uses the shared modal manager');
assert.match(indexSource, /id="source-picker"[^>]*class="manage-overlay hidden"/);
assert.match(indexSource, /id="source-picker-list"[^>]*role="listbox"/);
assert.match(indexSource, /id="source-picker-close"/);
assert.match(indexSource, /id="source-select"[^>]*aria-expanded="false"/);

assert.match(modalSource, /function topRoot\(\)/);
assert.match(modalSource, /function closeTop\(\)/);
assert.match(modalSource, /topRoot,\s*\n\s*closeTop/);
// Rendered contrast and brand-clearance contracts live in test_tv_nav_cards.py.
assert.match(baseCss, /\.tv-nav-focus\.tv-nav-focus:focus\s*\{[^}]*outline:\s*2px solid var\(--[\w-]+\)/);
assert.match(overlaysCss, /\.source-picker-option\[aria-selected="true"\]/);

// The module must stay device-agnostic: no vendor/product IDs and no
// system-level key remapping.
assert.doesNotMatch(navSource, /1915|1025|XING|evdev|uinput|xdotool/i);
assert.match(navSource, /BrowserBack/);
assert.doesNotMatch(navSource, /toggleSettings/, 'BrowserBack runs homeAction');
assert.match(navSource, /ContextMenu/);
assert.match(navSource, /addEventListener\('contextmenu'/,
    'a keyboard context menu must be suppressed, not just its keydown');
assert.match(navSource, /activateFocused/,
    'Enter/Space must click focusable rows, not only ARIA roles');
assert.match(navSource, /releaseNativeFocus/,
    'text fields and dropdowns need an Escape exit');
assert.doesNotMatch(navSource, /addEventListener\('keyup'/);

console.log('tv nav tests: ok');
