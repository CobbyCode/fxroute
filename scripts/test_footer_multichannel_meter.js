#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only
// Footer meter with three or more output positions: one vertical bar per
// position, stereo untouched, holdover per layout, responsive segment budget.

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const root = path.resolve(__dirname, '..');
const playbackCss = fs.readFileSync(path.join(root, 'static', 'css', '_playback.css'), 'utf8');
const responsiveCss = fs.readFileSync(path.join(root, 'static', 'css', '_responsive.css'), 'utf8');
const indexHtml = fs.readFileSync(path.join(root, 'static', 'index.html'), 'utf8');

function makeClassList() {
    const values = new Set();
    return {
        values,
        toggle(name, force) {
            const enabled = force === undefined ? !values.has(name) : !!force;
            if (enabled) values.add(name); else values.delete(name);
            return enabled;
        },
        contains: (name) => values.has(name),
        add: (...names) => names.forEach((name) => values.add(name)),
        remove: (...names) => names.forEach((name) => values.delete(name)),
    };
}

class FakeElement {
    constructor(tag) {
        this.tag = tag;
        this.className = '';
        this.dataset = {};
        this.title = '';
        this.textContent = '';
        this.children = [];
        this.attributes = {};
        this.classList = makeClassList();
    }
    appendChild(child) { this.children.push(child); return child; }
    append(...children) { children.forEach((child) => this.appendChild(child)); }
    replaceChildren(...children) { this.children = []; this.append(...children); }
    setAttribute(name, value) { this.attributes[name] = value; }
    querySelectorAll(selector) {
        const out = [];
        const walk = (node) => {
            for (const child of node.children) {
                const matches = selector === 'i' ? child.tag === 'i'
                    : child.className.split(' ').includes(selector.slice(1));
                if (matches) out.push(child);
                walk(child);
            }
        };
        walk(this);
        return out;
    }
}

let viewport = 'desktop';
global.document = { createElement: (tag) => new FakeElement(tag) };
global.window = {
    matchMedia: (query) => ({
        matches: (query === '(max-width: 700px)' && viewport === 'phone')
            || (query === '(max-width: 1180px)' && viewport !== 'desktop'),
    }),
};
const UI = require('../static/playback_ui.js');

function stereoRow() {
    const segments = Array.from({ length: 12 }, () => new FakeElement('i'));
    const row = new FakeElement('span');
    segments.forEach((segment) => row.appendChild(segment));
    return row;
}
const meter = new FakeElement('div');
const elements = {
    playbackMeter: meter,
    meterLeft: stereoRow(),
    meterRight: stereoRow(),
    meterMulti: new FakeElement('div'),
};
const warning = { playback: { output_peak_warning: {} } };
UI.init({
    getState: () => ({ playback: warning.playback }),
    getElements: () => elements,
});

const lit = (bar) => bar.querySelectorAll('i').filter((i) => i.classList.contains('is-lit')).length;
const shown = (bar) => bar.querySelectorAll('i').filter((i) => !i.classList.contains('meter-segment-hidden')).length;
const bars = () => elements.meterMulti.querySelectorAll('.meter-bar');
const labels = () => elements.meterMulti.querySelectorAll('.meter-col-label').map((el) => el.textContent);

function snapshot(channels, extra = {}) {
    return {
        available: true, vu_fresh: true, detected: false,
        vu_db: -20, vu_db_l: -20, vu_db_r: -20, detected_l: false, detected_r: false,
        channels, ...extra,
    };
}
const SURROUND_5 = (levels, detected = [false, false, false, false, false]) =>
    ({ keys: ['fl', 'fr', 'lfe', 'rl', 'rr'], vu_db: levels, detected });

// Layout contract: the bars only appear for 3..8 consistent positions.
assert.equal(UI.meterChannelLayout(snapshot(null)), null);
assert.equal(UI.meterChannelLayout(snapshot({ keys: ['l', 'r'], vu_db: [-3, -3], detected: [false, false] })), null);
assert.equal(UI.meterChannelLayout(snapshot({ keys: ['fl', 'fr', 'c'], vu_db: [-3, -3] })), null, 'mismatched lengths');
assert.equal(UI.meterChannelLayout(snapshot({ keys: Array(9).fill('fl'), vu_db: Array(9).fill(-3) })), null);
assert.ok(UI.meterChannelLayout(snapshot(SURROUND_5([-3, -3, -3, -3, -3]))));

// Live render: columns, labels, tooltips and lit segments follow the levels.
UI.renderOutputMeter(snapshot(SURROUND_5([-6, -20, -45, -60, -12])), true);
assert.ok(meter.classList.contains('is-multichannel'));
assert.ok(meter.classList.contains('is-active'));
assert.deepEqual(labels(), ['FL', 'FR', 'LFE', 'RL', 'RR']);
assert.deepEqual(elements.meterMulti.querySelectorAll('.meter-col').map((c) => c.title),
    ['Front L', 'Front R', 'LFE', 'Rear L', 'Rear R']);
assert.equal(bars().length, 5);
assert.equal(bars()[0].querySelectorAll('i').length, 8, 'each bar owns eight segments');
const litCounts = bars().map(lit);
assert.ok(litCounts[0] > litCounts[1] && litCounts[1] > litCounts[2], `louder position lights more: ${litCounts}`);
assert.equal(litCounts[3], 0, 'silence floor lights nothing');
assert.match(meter.attributes['aria-label'], /Front L, Front R, LFE, Rear L, Rear R/);

// The stereo rows keep their own contract while the bars take over.
UI.renderOutputMeter(snapshot(null, { vu_db_l: -6, vu_db_r: -30 }), true);
assert.equal(meter.classList.contains('is-multichannel'), false);
assert.equal(meter.attributes['aria-label'], 'Stereo output level');
assert.ok(lit(elements.meterLeft) > lit(elements.meterRight));

// A stable layout keeps its bar elements instead of rebuilding them.
UI.renderOutputMeter(snapshot(SURROUND_5([-6, -20, -45, -60, -12])), true);
const firstBar = bars()[0];
UI.renderOutputMeter(snapshot(SURROUND_5([-9, -9, -9, -9, -9])), true);
assert.equal(bars()[0], firstBar, 'same layout updates in place');
UI.renderOutputMeter(snapshot({ keys: ['fl', 'fr', 'c', 'lfe'], vu_db: [-9, -9, -9, -9], detected: [false, false, false, false] }), true);
assert.deepEqual(labels(), ['FL', 'FR', 'C', 'LFE']);

// Peak: only the clipping position shows its red top segment.
UI.renderOutputMeter(snapshot(SURROUND_5([-1, -20, -20, -20, -20], [true, false, false, false, false])), true);
const peaks = bars().map((bar) => bar.querySelectorAll('i').filter((i) => i.classList.contains('is-peak')).length);
assert.deepEqual(peaks, [1, 0, 0, 0, 0]);

// Holdover: one stale snapshot with the same layout keeps the bars, a
// different layout or a sustained outage clears them.
UI.renderOutputMeter(snapshot(SURROUND_5([-6, -20, -45, -60, -12])), true);
const heldCounts = bars().map(lit);
UI.renderOutputMeter(snapshot(SURROUND_5([null, null, null, null, null]), { vu_fresh: false }), true);
assert.deepEqual(bars().map(lit), heldCounts, 'single stale snapshot holds the bars');
UI.renderOutputMeter(snapshot({ keys: ['fl', 'fr', 'c', 'lfe', 'sl'], vu_db: [null, null, null, null, null], detected: [] }, { vu_fresh: false }), true);
assert.deepEqual(bars().map(lit), [0, 0, 0, 0, 0], 'a new layout never shows the old levels');
UI.renderOutputMeter(snapshot(SURROUND_5([-6, -20, -45, -60, -12])), true);
UI.renderOutputMeter(snapshot(SURROUND_5([null, null, null, null, null]), { vu_fresh: false }), false);
assert.deepEqual(bars().map(lit), [0, 0, 0, 0, 0], 'inactive playback clears the bars');
assert.equal(meter.classList.contains('is-active'), false);

// Responsive budget: eight segments on desktop, fewer on tablet and phone,
// with the amber zone staying the top quarter.
assert.equal(UI.responsiveMultiMeterSegmentCount(), 8);
viewport = 'tablet';
assert.equal(UI.responsiveMultiMeterSegmentCount(), 6);
viewport = 'phone';
assert.equal(UI.responsiveMultiMeterSegmentCount(), 5);
UI.renderOutputMeter(snapshot(SURROUND_5([-1, -1, -1, -1, -1])), true);
const phoneBar = bars()[0];
assert.equal(shown(phoneBar), 5);
const warn = phoneBar.querySelectorAll('i').filter((i) => i.classList.contains('is-warn')).length;
assert.equal(warn, 1, 'five segments keep a single amber step');
viewport = 'desktop';
UI.renderOutputMeter(snapshot(SURROUND_5([-1, -1, -1, -1, -1])), true);
assert.equal(shown(bars()[0]), 8);
assert.equal(bars()[0].querySelectorAll('i').filter((i) => i.classList.contains('is-warn')).length, 2);

// Stylesheet contract: no dB text next to the bars, labels dropped on phones,
// meter slot unchanged for stereo.
assert.match(playbackCss, /\.playback-meter\.is-multichannel \.output-level-badge\s*\{\s*display:\s*none/);
assert.match(playbackCss, /\.playback-meter\.is-multichannel \.meter-row,/);
assert.match(responsiveCss, /\.meter-col-label\s*\{\s*display:\s*none;/);
assert.match(indexHtml, /<div id="meter-multi" class="meter-multi" aria-hidden="true"><\/div>/);

console.log('PASS  scripts/test_footer_multichannel_meter.js (multichannel footer meter)');
