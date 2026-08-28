#!/usr/bin/env node
// Focused regression coverage for the footer's existing peak state.

const assert = require('assert/strict');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const root = path.join(__dirname, '..');
const appSource = fs.readFileSync(path.join(root, 'static', 'app.js'), 'utf8');
const playbackCss = fs.readFileSync(path.join(root, 'static', 'css', '_playback.css'), 'utf8');

function extractFunction(name) {
    const match = new RegExp(`function\\s+${name}\\s*\\(`).exec(appSource);
    assert.ok(match, `missing ${name}`);
    const brace = appSource.indexOf('{', match.index);
    assert.notEqual(brace, -1, `missing body ${name}`);
    let depth = 0;
    let quote = '';
    let escaped = false;
    for (let index = brace; index < appSource.length; index += 1) {
        const character = appSource[index];
        if (quote) {
            if (escaped) escaped = false;
            else if (character === '\\') escaped = true;
            else if (character === quote) quote = '';
            continue;
        }
        if (`'"\``.includes(character)) quote = character;
        else if (character === '{') depth += 1;
        else if (character === '}' && --depth === 0) return appSource.slice(match.index, index + 1);
    }
    throw new Error(`unterminated ${name}`);
}

function makeClassList() {
    return {
        values: new Set(),
        toggle(name, force) {
            const enabled = force === undefined ? !this.values.has(name) : !!force;
            if (enabled) this.values.add(name);
            else this.values.delete(name);
            return enabled;
        },
        contains(name) {
            return this.values.has(name);
        },
        remove(...names) {
            names.forEach(name => this.values.delete(name));
        },
    };
}

function makeMeter() {
    return {
        querySelectorAll() {
            return Array.from({ length: 6 }, () => ({ classList: makeClassList() }));
        },
    };
}

function makeBadge() {
    return { classList: makeClassList(), style: {}, textContent: '', title: '' };
}

const badge = makeBadge();
const playbackMeter = { classList: makeClassList() };
const sandbox = {
    state: {
        playback: {
            playing: true,
            paused: false,
            output_peak_warning: {},
        },
    },
    elements: {
        outputLevelBadge: badge,
        playbackMeter,
        meterLeft: makeMeter(),
        meterRight: makeMeter(),
    },
    window: { __footerSource: 'local' },
    isStreamingFooterSource: () => false,
    streamingFooterData: () => null,
    responsiveMeterSegmentCount: () => 6,
};

vm.createContext(sandbox);
vm.runInContext([
    extractFunction('meterLitCount'),
    extractFunction('renderMeterChannel'),
    extractFunction('renderStereoMeter'),
    extractFunction('formatOutputLevelBadgeDb'),
    extractFunction('renderPeakWarningBadge'),
].join('\n'), sandbox);

function renderWarning(warning) {
    sandbox.state.playback.output_peak_warning = warning;
    sandbox.renderPeakWarningBadge();
}

renderWarning({
    available: true,
    detected: false,
    vu_db: -6,
    vu_fresh: true,
    detected_l: false,
    detected_r: false,
});
assert.equal(badge.textContent, '-06 dB', 'normal VU value must remain unchanged');
assert.equal(badge.classList.contains('hidden'), false, 'normal VU value must be visible');
assert.equal(badge.classList.contains('is-peak'), false, 'normal VU value must not be peak-styled');

renderWarning({
    available: true,
    detected: true,
    vu_db: null,
    vu_fresh: false,
    detected_l: false,
    detected_r: false,
});
assert.equal(badge.textContent, '00 dB', 'peak fallback must reuse the VU number format');
assert.equal(badge.classList.contains('hidden'), false, 'peak fallback must not be hidden');
assert.equal(badge.classList.contains('is-peak'), true, 'peak fallback must use peak styling');
assert.match(badge.title, /peak detected/i, 'peak fallback must explain the state');

/* Regression: the peak state must only recolor the badge. Switching to peak
   with a fresh VU value must keep the exact same text (format and width) as
   the preceding normal render. */
renderWarning({
    available: true,
    detected: false,
    vu_db: -3,
    vu_fresh: true,
    detected_l: false,
    detected_r: false,
});
assert.equal(badge.textContent, '-03 dB', 'pre-peak VU value must remain unchanged');
const prePeakText = badge.textContent;

renderWarning({
    available: true,
    detected: true,
    vu_db: -3,
    vu_fresh: true,
    detected_l: true,
    detected_r: false,
});
assert.equal(badge.textContent, prePeakText, 'peak must keep the VU text so the display never jumps');
assert.equal(badge.textContent, sandbox.formatOutputLevelBadgeDb(-3), 'peak text must come from the shared VU formatter');
assert.equal(badge.classList.contains('is-peak'), true, 'fresh peak must use peak styling');
assert.match(badge.title, /peak detected/i, 'fresh peak must explain the state');

assert.match(playbackCss, /\.output-level-badge\.is-peak\s*\{[\s\S]*?color:\s*#ff5a5f/,
    'peak badge must use the existing meter red');

console.log('PASS  scripts/test_footer_peak_display.js (normal and peak states)');
