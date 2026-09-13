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
    const segments = Array.from({ length: 6 }, () => ({ classList: makeClassList() }));
    return {
        querySelectorAll() {
            return segments;
        },
        litCount() {
            return segments.filter(segment => segment.classList.contains('is-lit')).length;
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

function extractDeclaration(pattern) {
    const match = appSource.match(pattern);
    assert.ok(match, `missing declaration ${pattern}`);
    return match[0];
}

vm.createContext(sandbox);
vm.runInContext([
    extractDeclaration(/let lastValidVuSnapshot = null;/),
    extractDeclaration(/const VU_HOLDOVER_MS = \d+;/),
    extractFunction('isFiniteVuDb'),
    extractFunction('rememberValidVu'),
    extractFunction('heldVuSnapshot'),
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

/* Regression: a missing sample is silence on the meter, never full-scale.
   Number(null) is 0 (0 dB), so the guard must catch nullish values before
   the numeric conversion. */
assert.equal(sandbox.meterLitCount(null, 6), 0, 'null level must light no segments');
assert.equal(sandbox.meterLitCount(undefined, 6), 0, 'undefined level must light no segments');
assert.equal(sandbox.meterLitCount('', 6), 0, 'empty level must light no segments');
assert.equal(sandbox.meterLitCount(-60, 6), 0, 'silence floor must light no segments');
assert.ok(sandbox.meterLitCount(-6, 6) > 0, 'normal level must light segments');

/* Regression: a single invalid sample must not blank the meter. While
   playback stays active the last valid VU level bridges the gap. */
renderWarning({
    available: true,
    detected: false,
    vu_db: -14,
    vu_db_l: -14,
    vu_db_r: -15,
    vu_fresh: true,
    detected_l: false,
    detected_r: false,
});
assert.equal(badge.textContent, '-14 dB', 'valid VU must render before the dropout');
const heldLit = sandbox.elements.meterLeft.litCount();
assert.ok(heldLit > 0, 'valid VU must light meter segments');

renderWarning({
    available: true,
    detected: false,
    vu_db: -14,
    vu_db_l: -14,
    vu_db_r: -15,
    vu_fresh: false,
    detected_l: false,
    detected_r: false,
});
assert.equal(badge.textContent, '-14 dB', 'single stale sample must hold the dB text');
assert.equal(badge.classList.contains('hidden'), false, 'single stale sample must not hide the badge');
assert.equal(sandbox.elements.meterLeft.litCount(), heldLit, 'single stale sample must hold the bars');

renderWarning({
    available: false,
    detected: false,
    vu_db: null,
    vu_db_l: null,
    vu_db_r: null,
    vu_fresh: false,
    detected_l: false,
    detected_r: false,
});
assert.equal(badge.textContent, '-14 dB', 'single unavailable snapshot must hold the dB text');
assert.equal(sandbox.elements.meterLeft.litCount(), heldLit, 'single unavailable snapshot must hold the bars');

/* The holdover is bounded: a sustained outage must hide the meter again. */
vm.runInContext('lastValidVuSnapshot.at -= 5000;', sandbox);
renderWarning({
    available: true,
    detected: false,
    vu_db: -14,
    vu_db_l: -14,
    vu_db_r: -15,
    vu_fresh: false,
    detected_l: false,
    detected_r: false,
});
assert.equal(badge.classList.contains('hidden'), true, 'sustained stale state must hide the badge again');
assert.equal(sandbox.elements.meterLeft.litCount(), 0, 'sustained stale state must clear the bars');

/* Silence is a valid sample: the -60 dB floor stays visible, not hidden. */
renderWarning({
    available: true,
    detected: false,
    vu_db: -60,
    vu_db_l: -60,
    vu_db_r: -60,
    vu_fresh: true,
    detected_l: false,
    detected_r: false,
});
assert.equal(badge.textContent, '-60 dB', 'silence floor must keep its dB text');
assert.equal(badge.classList.contains('hidden'), false, 'silence floor must stay visible');
assert.equal(sandbox.elements.meterLeft.litCount(), 0, 'silence floor lights no segments');

console.log('PASS  scripts/test_footer_peak_display.js (normal and peak states)');
