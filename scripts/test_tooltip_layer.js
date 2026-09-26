#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only
// App tooltip layer: placement stays inside the viewport and flips above
// hosts on the bottom edge; the text follows the host's current state.

const assert = require('node:assert/strict');
const { placeTooltip, tooltipText } = require('../static/tooltip.js');

const viewport = { width: 390, height: 844 };
const rect = (left, top, width, height) => ({ left, top, right: left + width, bottom: top + height, width, height });

// Below the host, start-aligned.
assert.deepEqual(placeTooltip(rect(40, 100, 44, 44), { width: 120, height: 24 }, viewport, null),
    { left: 40, top: 151, side: 'below' });

// End anchor aligns the bubble's right edge with the host's.
assert.deepEqual(placeTooltip(rect(300, 100, 44, 44), { width: 120, height: 24 }, viewport, 'end'),
    { left: 224, top: 151, side: 'below' });

// A start-aligned bubble near the right edge is clamped into the viewport.
assert.equal(placeTooltip(rect(360, 100, 20, 20), { width: 200, height: 24 }, viewport, null).left, 390 - 8 - 200);

// An end-aligned bubble near the left edge is clamped too.
assert.equal(placeTooltip(rect(4, 100, 20, 20), { width: 200, height: 24 }, viewport, 'end').left, 8);

// Playback footer: no room below, so the bubble opens above the host.
const footerButton = rect(180, 800, 36, 36);
const above = placeTooltip(footerButton, { width: 90, height: 24 }, viewport, null);
assert.equal(above.side, 'above');
assert.equal(above.top, 800 - 7 - 24);

// Neither side fits: take the side with more room.
const tall = { width: 200, height: 500 };
assert.equal(placeTooltip(rect(10, 200, 40, 40), tall, viewport, null).side, 'below');
assert.equal(placeTooltip(rect(10, 450, 40, 40), tall, viewport, null).side, 'above');
assert.equal(placeTooltip(rect(10, 450, 40, 40), tall, viewport, null).top, 8);

function host(attrs, extra = {}) {
    return {
        classList: { contains: (name) => (attrs.class || '').split(' ').includes(name) },
        getAttribute: (name) => (name in attrs ? attrs[name] : null),
        ...extra,
    };
}

function option(value, text, ariaLabel) {
    return { value, textContent: text, getAttribute: (name) => (name === 'aria-label' ? ariaLabel ?? null : null) };
}

function selectWrap(attrs, options, selectedIndex) {
    const select = { options, selectedIndex };
    return host({ class: 'select-tooltip-wrap', ...attrs }, { querySelector: () => select, select });
}

assert.equal(tooltipText(host({ 'data-tooltip': '  Add to favorites ' })), 'Add to favorites');
assert.equal(tooltipText(host({})), '');
// An expanded host (open power menu) shows no bubble over its own surface.
assert.equal(tooltipText(host({ 'data-tooltip': 'System power', 'aria-expanded': 'true' })), '');
assert.equal(tooltipText(host({ 'data-tooltip': 'System power', 'aria-expanded': 'false' })), 'System power');

// Station select: compacted option text, full name from the aria-label.
const stations = selectWrap({}, [
    option('', 'Select a station…'),
    option('s1', 'Radio Paradise - Main Mix (EU)…', 'Radio Paradise - Main Mix (EU) 320k AAC'),
], 1);
assert.equal(tooltipText(stations), 'Radio Paradise - Main Mix (EU) 320k AAC');
// A reset to the placeholder (programmatic value = '') shows nothing.
stations.select.selectedIndex = 0;
assert.equal(tooltipText(stations), '');

// Compare selects: prefix, option text, placeholder ignored.
const presets = selectWrap({ 'data-tooltip-prefix': 'Preset B' }, [
    option('', 'Select preset…'),
    option('Conv LR HybAlign BK 30-3000Hz -7dB', 'Conv LR HybAlign BK 30-3000Hz -7dB'),
], 1);
assert.equal(tooltipText(presets), 'Preset B: Conv LR HybAlign BK 30-3000Hz -7dB');
presets.select.selectedIndex = 0;
assert.equal(tooltipText(presets), '');
// All Banks: an option without a value attribute reports its text as value.
const aggregate = selectWrap({ 'data-tooltip-prefix': 'Preset A' }, [option('All Banks · A', 'All Banks · A')], 0);
assert.equal(tooltipText(aggregate), 'Preset A: All Banks · A');
// Nothing selected at all.
assert.equal(tooltipText(selectWrap({}, [], -1)), '');

console.log('tooltip layer: ok');
