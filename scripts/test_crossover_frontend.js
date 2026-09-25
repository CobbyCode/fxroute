#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const Crossover = require('../static/crossover.js');
const repoRoot = path.resolve(__dirname, '..');
const indexSource = fs.readFileSync(path.join(repoRoot, 'static', 'index.html'), 'utf8');

const CAPS = {
    filter_families: { 'linkwitz-riley': [12, 24, 36, 48, 60, 72], butterworth: [6, 12, 24], bessel: [6, 12] },
};

assert.deepEqual(
    Crossover.orderedWays({ right_high: 1, left_low: 1, right_low: 1, left_high: 1, sub1: 1 }),
    ['left_low', 'left_high', 'right_low', 'right_high']);
assert.deepEqual(
    Crossover.orderedWays({ left_mid: 1, left_low_mid: 1, left_low: 1, left_high: 1 }),
    ['left_low', 'left_low_mid', 'left_mid', 'left_high']);

assert.deepEqual(Crossover.applicableFilters('left_low'), ['lowpass']);
assert.deepEqual(Crossover.applicableFilters('right_high'), ['highpass']);
assert.deepEqual(Crossover.applicableFilters('left_mid'), ['highpass', 'lowpass']);
assert.deepEqual(Crossover.applicableFilters('left_low_mid'), ['highpass', 'lowpass']);

// Sub-owned Low high-pass: with routed subs and Main highpass on, the Low
// way additionally shows the sub crossover (read-only, owned by the Sub tile).
const bass80 = { frequency_hz: 80, main_highpass_enabled: true };
assert.deepEqual(Crossover.bassHighpass(bass80, ['sub1']), {
    family: 'linkwitz-riley', slope_db_oct: 24, frequency_hz: 80 });
assert.equal(Crossover.bassHighpass(bass80, []), null);
assert.equal(Crossover.bassHighpass({ frequency_hz: 80, main_highpass_enabled: false }, ['sub1']), null);
assert.equal(Crossover.bassHighpass({ frequency_hz: 80 }, ['sub1']), null);
assert.equal(Crossover.bassHighpass(bass80, ['sub1']).frequency_hz, 80);
assert.deepEqual(Crossover.derivedHighpassForRole('left_low', bass80, ['sub_l']), {
    family: 'linkwitz-riley', slope_db_oct: 24, frequency_hz: 80 });
assert.equal(Crossover.derivedHighpassForRole('right_low', bass80, []), null);
assert.equal(Crossover.derivedHighpassForRole('left_mid', bass80, ['sub_l']), null);
assert.equal(Crossover.derivedHighpassForRole('left_high', bass80, ['sub_l']), null);
assert.deepEqual(Crossover.applicableFilters('left_low', { bass: bass80, subRoles: ['sub1'] }),
    ['highpass', 'lowpass']);
assert.deepEqual(Crossover.applicableFilters('left_low', { bass: bass80, subRoles: [] }), ['lowpass']);
assert.deepEqual(Crossover.applicableFilters('left_mid', { bass: bass80, subRoles: ['sub1'] }),
    ['highpass', 'lowpass']);

// Type/slope travel with the sub crossover, and only a true Stereo pair
// resolves per side while it is unlinked; Dual-Mono stays on the shared one.
const shaped = { frequency_hz: 90, main_highpass_enabled: true, family: 'butterworth', slope_db_oct: 36 };
assert.equal(Crossover.filterLabel('butterworth', 36), 'BW36');
assert.deepEqual(Crossover.bassHighpass(shaped, ['sub1']),
    { family: 'butterworth', slope_db_oct: 36, frequency_hz: 90 });
const unlinked = { ...shaped, sub_link: false, sub_filters: {
    left: { family: 'bessel', slope_db_oct: 18, frequency_hz: 60 } } };
assert.deepEqual(Crossover.bassCrossoverForSide(unlinked, 'left'),
    { family: 'bessel', slope_db_oct: 18, frequency_hz: 60 });
// No right override: that side falls back to the shared crossover.
assert.deepEqual(Crossover.bassCrossoverForSide(unlinked, 'right'),
    { family: 'butterworth', slope_db_oct: 36, frequency_hz: 90 });
assert.deepEqual(Crossover.bassHighpass(unlinked, ['sub_l', 'sub_r'], 'left_low'),
    { family: 'bessel', slope_db_oct: 18, frequency_hz: 60 });
assert.deepEqual(Crossover.bassHighpass(unlinked, ['sub_l', 'sub_r'], 'right_high'),
    { family: 'butterworth', slope_db_oct: 36, frequency_hz: 90 });
assert.equal(Crossover.sideForRole('sub_l'), 'left');
assert.equal(Crossover.sideForRole('sub_r'), 'right');
// A coupled pair and a Dual-Mono pair both keep the shared crossover, even
// when the stored state still carries side overrides from an earlier unlink.
assert.deepEqual(Crossover.bassHighpass({ ...unlinked, sub_link: true }, ['sub_l', 'sub_r'], 'left_low'),
    { family: 'butterworth', slope_db_oct: 36, frequency_hz: 90 });
assert.deepEqual(Crossover.bassHighpass(unlinked, ['sub1', 'sub2'], 'left_low'),
    { family: 'butterworth', slope_db_oct: 36, frequency_hz: 90 });
assert.deepEqual(Crossover.sharedBassCrossover(unlinked),
    { family: 'butterworth', slope_db_oct: 36, frequency_hz: 90 });

// Linked way tabs: left/right pairs merge into one L/R tab per way;
// single-sided ways keep their own tab.
assert.equal(Crossover.wayKey('left_low_mid'), 'low_mid');
assert.equal(Crossover.wayLabel('low'), 'Low');
assert.equal(Crossover.wayLabel('low_mid'), 'Low Mid');
assert.deepEqual(
    Crossover.pairedWays(['left_low', 'left_high', 'right_low', 'right_high']),
    [{ way: 'low', left: 'left_low', right: 'right_low' },
     { way: 'high', left: 'left_high', right: 'right_high' }]);
assert.deepEqual(
    Crossover.pairedWays(['left_low', 'right_low', 'left_high']),
    [{ way: 'low', left: 'left_low', right: 'right_low' },
     { way: 'high', left: 'left_high', right: null }]);

function stubTabs() {
    return { innerHTML: '', dataset: {}, addEventListener() {} };
}
{
    // Linked: two tabs for a 2-way pair, canonical left data values, and
    // either counterpart counts as active.
    const tabs = stubTabs();
    Crossover.renderWayTabs(tabs,
        ['left_low', 'left_high', 'right_low', 'right_high'], 'right_low', () => {}, true);
    const buttons = tabs.innerHTML.match(/<button/g) || [];
    assert.equal(buttons.length, 2);
    assert.match(tabs.innerHTML, /L\/R · Low/);
    assert.match(tabs.innerHTML, /L\/R · High/);
    assert.match(tabs.innerHTML, /data-crossover-way="left_low"/);
    assert.doesNotMatch(tabs.innerHTML, /data-crossover-way="right_low"/);
    assert.match(tabs.innerHTML, /data-crossover-way="left_low" class="crossover-tab is-active"/);
}
{
    // Linked with a single-sided way: pair tab plus lone L tab.
    const tabs = stubTabs();
    Crossover.renderWayTabs(tabs, ['left_low', 'right_low', 'left_high'], 'left_high', () => {}, true);
    const buttons = tabs.innerHTML.match(/<button/g) || [];
    assert.equal(buttons.length, 2);
    assert.match(tabs.innerHTML, /L\/R · Low/);
    assert.match(tabs.innerHTML, /L · High/);
}
{
    // Unlinked render is unchanged: one tab per side.
    const tabs = stubTabs();
    Crossover.renderWayTabs(tabs,
        ['left_low', 'left_high', 'right_low', 'right_high'], 'right_low', () => {}, false);
    const buttons = tabs.innerHTML.match(/<button/g) || [];
    assert.equal(buttons.length, 4);
    assert.match(tabs.innerHTML, /R · Low/);
    assert.match(tabs.innerHTML, /data-crossover-way="right_low" class="crossover-tab is-active"/);
}

assert.deepEqual(Crossover.slopesForFamily('linkwitz-riley', CAPS), [12, 24, 36, 48, 60, 72]);
assert.deepEqual(Crossover.slopesForFamily('nope', CAPS), []);

const two = Crossover.starterValues(2);
assert.equal(two.left_low.lowpass.frequency_hz, 2000);
assert.equal(two.left_low.lowpass.family, 'linkwitz-riley');
assert.equal(two.left_low.lowpass.slope_db_oct, 24);
assert.equal(two.left_low.highpass, undefined);
assert.equal(two.right_high.highpass.frequency_hz, 2000);
assert.equal(Object.keys(two).length, 4);

const three = Crossover.starterValues(3);
assert.equal(three.left_low.lowpass.frequency_hz, 300);
assert.equal(three.left_mid.highpass.frequency_hz, 300);
assert.equal(three.left_mid.lowpass.frequency_hz, 2500);
assert.equal(three.left_high.highpass.frequency_hz, 2500);

const four = Crossover.starterValues(4);
assert.equal(four.left_low.lowpass.frequency_hz, 200);
assert.equal(four.left_low_mid.highpass.frequency_hz, 200);
assert.equal(four.left_low_mid.lowpass.frequency_hz, 800);
assert.equal(four.left_mid.highpass.frequency_hz, 800);
assert.equal(four.left_mid.lowpass.frequency_hz, 3000);
assert.equal(four.left_high.highpass.frequency_hz, 3000);

assert.throws(() => Crossover.starterValues(5), /2-way|3-way|4-way/);

const processing = {
    left_low: { highpass: null, lowpass: { family: 'linkwitz-riley', slope_db_oct: 24, frequency_hz: 310 } },
    left_mid: { highpass: null, lowpass: null },
    left_high: { highpass: { family: 'butterworth', slope_db_oct: 12, frequency_hz: 2500 }, lowpass: null },
    right_low: { highpass: null, lowpass: { family: 'linkwitz-riley', slope_db_oct: 24, frequency_hz: 300 } },
    right_mid: { highpass: { family: 'linkwitz-riley', slope_db_oct: 24, frequency_hz: 300 },
                 lowpass: { family: 'linkwitz-riley', slope_db_oct: 24, frequency_hz: 2500 } },
    right_high: { highpass: { family: 'linkwitz-riley', slope_db_oct: 24, frequency_hz: 2500 }, lowpass: null },
};
assert.deepEqual(Crossover.missingStarterRoles(processing, three), ['left_mid']);

assert.equal(Crossover.mirrorRole('left_low'), 'right_low');
assert.equal(Crossover.mirrorRole('right_high'), 'left_high');
assert.equal(Crossover.mirrorRole('left_low_mid'), 'right_low_mid');
assert.equal(Crossover.mirrorRole('sub1'), null);
assert.equal(Crossover.mirrorRole(null), null);

assert.equal(Crossover.starterFrequency(2, 'left_low', 'lowpass'), 2000);
assert.equal(Crossover.starterFrequency(2, 'right_high', 'highpass'), 2000);
assert.equal(Crossover.starterFrequency(3, 'left_mid', 'lowpass'), 2500);
assert.equal(Crossover.starterFrequency(4, 'right_mid', 'highpass'), 800);
assert.equal(Crossover.starterFrequency(2, 'left_low', 'highpass'), null);
assert.equal(Crossover.starterFrequency(5, 'left_low', 'lowpass'), null);

assert.equal(Crossover.clampLevelDb(99), 24);
assert.equal(Crossover.clampLevelDb(-99), -80);
assert.equal(Crossover.clampAlignmentMs(99), 40);
assert.equal(Crossover.clampAlignmentMs(-99), -40);
assert.equal(Crossover.clampFrequencyHz(5), 20);
assert.equal(Crossover.clampFrequencyHz(30000), 20000);

assert.match(indexSource, /crossover\.js\?v=\d+\.\d+\.\d+/);
assert.match(indexSource, /<canvas id="effects-crossover-graph"/);
const appSource = fs.readFileSync(path.join(repoRoot, 'static', 'app.js'), 'utf8');
const crossoverUiSource = fs.readFileSync(path.join(repoRoot, 'static', 'crossover_ui.js'), 'utf8');
// Tile fetch/render/mutation capture live in crossover_ui.js; app.js keeps
// thin wrappers plus the output-state ownership and settings wiring.
for (const name of ['fetchCrossoverResponse', 'renderCrossoverTile', 'collectCrossoverWayMutation',
    'saveCrossoverWay', 'maybeApplyCrossoverStarters', 'wireCrossoverTile']) {
    assert.match(crossoverUiSource, new RegExp(`function ${name}\\(`), `crossover_ui.js must own ${name}`);
    assert.match(appSource, new RegExp(`function ${name}\\(`), `app.js must keep a ${name} wrapper`);
}
assert.match(appSource, /FXRouteCrossoverUI/, 'app.js wrappers must delegate to the crossover UI module');
assert.match(indexSource, /crossover_ui\.js\?v=\d+\.\d+\.\d+/);
assert.ok(indexSource.indexOf('crossover_ui.js') < indexSource.indexOf('/static/app.js'),
    'crossover UI module must load before app.js');
// Starter values autofill the first valid 2/3/4-way config; no button, no status text.
assert.doesNotMatch(appSource, /effectsCrossoverStarter/);
assert.doesNotMatch(appSource, /Already initialized|All ways initialized|Starter values missing/);
assert.match(crossoverUiSource, /maybeApplyCrossoverStarters/);
// L/R link mirrors only filter values, never trim.
assert.match(crossoverUiSource, /mirrorRole/);
assert.match(indexSource, /Link L\/R/);
// Link L/R is off by default: unchecked box, false initial state, and
// strict true-checks so an unset state never links.
assert.doesNotMatch(indexSource, /id="effects-crossover-link" checked/);
assert.match(appSource, /linkLR:\s*false/);
assert.match(crossoverUiSource, /getState\(\)\.crossover\.linkLR === true/);
assert.doesNotMatch(crossoverUiSource, /getState\(\)\.crossover\.linkLR !== false/);
// Trim stays side-specific and is hidden while linked: unlinked shows all
// trim values, linked shows only the shared crossover parameters.
assert.match(indexSource, /id="effects-crossover-trim-group"/);
assert.match(crossoverUiSource, /effectsCrossoverTrimGroup/);
assert.match(crossoverUiSource, /Trim[\s\S]*hidden while linked|hidden while linked/);
// Filter type Off plus compact headers.
assert.match(crossoverUiSource, /off: 'Off'/);
assert.match(crossoverUiSource, /\['off', \.\.\.Object\.keys\(catalog\.capabilities/);
// Off disables the filter fully and independently per direction: Type Off
// writes null, hides its Frequency/Slope rows, keeps Type selectable.
// Both Off leave the way unfiltered (flat).
assert.match(crossoverUiSource, /if \(familyEl\?\.value === 'off'\) return null/);
assert.match(crossoverUiSource, /freqGroup.*kindOff \? 'none' : ''/s);
assert.match(crossoverUiSource, /slopeGroup.*kindOff \? 'none' : ''/s);
assert.match(crossoverUiSource, /-Way Stereo System/);
const subwooferUiSource = fs.readFileSync(path.join(repoRoot, 'static', 'subwoofer_ui.js'), 'utf8');
assert.match(subwooferUiSource, /Crossover \$/);
assert.match(subwooferUiSource, /Main HPF/);
for (const id of ['effects-crossover-card', 'effects-crossover-tabs', 'effects-crossover-graph',
    'effects-crossover-frequency-highpass', 'effects-crossover-frequency-lowpass',
    'effects-crossover-frequency-highpass-group', 'effects-crossover-slope-highpass-group',
    'effects-crossover-frequency-lowpass-group', 'effects-crossover-slope-lowpass-group',
    'effects-crossover-family-highpass', 'effects-crossover-slope-highpass',
    'effects-crossover-family-lowpass', 'effects-crossover-slope-lowpass',
    'effects-crossover-trim-group',
    'effects-crossover-level',
    'effects-crossover-delay', 'effects-crossover-polarity', 'effects-crossover-link',
    'effects-crossover-summary']) {
    assert.match(indexSource, new RegExp(`id="${id}"`), `missing #${id}`);
}
assert.doesNotMatch(indexSource, /id="effects-crossover-starter"/, 'starter button is gone: first valid configs autofill');
assert.match(indexSource, /Link L\/R/);
// Way tabs and the L/R link share one row above the graph; no separate
// actions row below Trim remains.
assert.match(indexSource, /class="crossover-tabrow"/);
assert.doesNotMatch(indexSource, /crossover-actions/);
// A linked save carries only the shared crossover filters; trim fields are
// added back only when unlinked.
assert.match(crossoverUiSource, /if\s*\(!linked\)\s*\{\s*\n.*mutation\.level_db/s);
assert.match(crossoverUiSource, /effectsCrossoverTrimGroup.*linkedCrossover \? 'none' : ''/);
{
    const tabrow = indexSource.indexOf('class="crossover-tabrow"');
    const tabs = indexSource.indexOf('id="effects-crossover-tabs"');
    const link = indexSource.indexOf('id="effects-crossover-link"');
    const graph = indexSource.indexOf('id="effects-crossover-graph"');
    assert.ok(tabrow >= 0 && tabrow < tabs && tabs < link && link < graph,
        'order must read tab row, way tabs, link, graph');
}
// The response graph painter lives in crossover_view.js; app.js keeps a
// thin delegating wrapper so renderCrossoverTile/repaintCrossoverGraph stay
// unchanged. No state, mutation or listener logic moved with it.
assert.match(indexSource, /crossover_view\.js\?v=\d+\.\d+\.\d+/);
const CrossoverView = require('../static/crossover_view.js');
assert.equal(typeof CrossoverView.drawCrossoverResponse, 'function');
assert.match(appSource, /function drawCrossoverResponse\(canvas, ways, activeRole\)/);
assert.match(appSource, /FXRouteCrossoverView/);

// Behavioral pin: the extracted painter handles edge cases and paints the
// same primitives (axes, dimmed/active ways, dashed Off-direction, cutoff
// markers) through a stub 2d context.
globalThis.FXRouteCrossover = Crossover;
function stubContext() {
    const calls = [];
    return { calls,
        setTransform() { calls.push(['setTransform']); },
        clearRect() { calls.push(['clearRect']); },
        fillRect() { calls.push(['fillRect']); },
        strokeRect() { calls.push(['strokeRect']); },
        beginPath() { calls.push(['beginPath']); },
        moveTo() { calls.push(['moveTo']); },
        lineTo() { calls.push(['lineTo']); },
        stroke() { calls.push(['stroke']); },
        fillText() { calls.push(['fillText']); },
        setLineDash(dash) { calls.push(['setLineDash', [...(dash || [])]]); },
        measureText() { return { width: 30 }; },
    };
}
function stubCanvas(clientWidth, ctx) {
    return { clientWidth, clientHeight: 136, width: 0, height: 0,
        getContext: (kind) => (kind === '2d' ? ctx : null) };
}
assert.equal(CrossoverView.drawCrossoverResponse(null, {}, 'left_low'), undefined);
assert.equal(CrossoverView.drawCrossoverResponse({ getContext: () => null }, {}, 'left_low'), undefined);
{
    const ctx = stubContext();
    CrossoverView.drawCrossoverResponse(stubCanvas(0, ctx), {}, 'left_low');
    assert.ok(!ctx.calls.some(([name]) => name === 'fillText'), 'hidden card paints nothing');
}
{
    const ctx = stubContext();
    const canvas = stubCanvas(600, ctx);
    const ways = {
        left_low: { points: [[20, -30], [100, -12], [1000, -6], [20000, -24]],
            complete: true, filters: { lowpass: { frequency_hz: 2000 } }, derived_highpass: null },
        left_high: { points: [[20, -24], [1000, -6], [2000, -6], [20000, -30]],
            complete: false, filters: { highpass: { frequency_hz: 2000 } } },
    };
    CrossoverView.drawCrossoverResponse(canvas, ways, 'left_low');
    assert.equal(canvas.width, 600);
    assert.equal(canvas.height, 136);
    assert.ok(ctx.calls.some(([name]) => name === 'fillText'), 'axes and markers are painted');
    assert.ok(ctx.calls.some(([name]) => name === 'stroke'), 'way curves are stroked');
    assert.ok(ctx.calls.some(([name, arg]) => name === 'setLineDash' && arg.length === 2),
        'cleared (Off) direction renders dashed');
}

// Behavioral pin: the tile mutation capture (Off clears, linked omits trim,
// sub-owned high-pass stays display-only), the starter autofill and the
// linked mirror save all run through crossover_ui.js with the app mutation
// path injected.
const CrossoverUI = require('../static/crossover_ui.js');
globalThis.FXRouteCrossover = Crossover;

function crossoverFixtureState() {
    return {
        crossover: { activeWay: 'left_low', response: null, busy: false, linkLR: false },
        outputSystem: { catalog: {
            active_mode: 'stereo-sub', revision: 7,
            capabilities: { filter_families: { 'linkwitz-riley': [12, 24] } },
            modes: { 'stereo-sub': {
                topology: { way_count: 2 },
                bass_management: {},
                crossover_enabled: true,
                processing: {
                    left_low: {}, left_high: {},
                    right_low: {}, right_high: {},
                },
            } },
        } },
    };
}

function crossoverInput(value) {
    const listeners = {};
    return { value, addEventListener(type, listener) { (listeners[type] ||= []).push(listener); },
        emit(type) { for (const listener of listeners[type] || []) listener({ target: this }); } };
}

function crossoverFixtureElements(overrides = {}) {
    return {
        effectsCrossoverCard: null,
        effectsCrossoverFrequencyHighpass: crossoverInput(''),
        effectsCrossoverFamilyHighpass: crossoverInput('linkwitz-riley'),
        effectsCrossoverSlopeHighpass: crossoverInput('24'),
        effectsCrossoverFrequencyLowpass: crossoverInput('2000'),
        effectsCrossoverFamilyLowpass: crossoverInput('linkwitz-riley'),
        effectsCrossoverSlopeLowpass: crossoverInput('24'),
        effectsCrossoverLevel: crossoverInput('1.5'),
        effectsCrossoverDelay: crossoverInput('0.25'),
        effectsCrossoverPolarity: crossoverInput('invert'),
        ...overrides,
    };
}

function initCrossoverUI(state, elements, extra = {}) {
    CrossoverUI.init({ getState: () => state, getElements: () => elements,
        showToast: () => {}, ensureOutputBoxes: () => {},
        applyMutation: async () => null, ...extra });
}

{
    // Off clears the filter: Type Off writes null even with a stale value.
    const state = crossoverFixtureState();
    state.crossover.activeWay = 'left_high';
    initCrossoverUI(state, crossoverFixtureElements({
        effectsCrossoverFamilyHighpass: crossoverInput('off') }));
    const mutation = CrossoverUI.collectCrossoverWayMutation();
    assert.equal(mutation.role, 'left_high');
    assert.equal(mutation.highpass, null);
    assert.equal(mutation.level_db, 1.5, 'unlinked saves carry trim');
}
{
    // Linked pairs share only the crossover filters; trim stays per-way.
    const state = crossoverFixtureState();
    state.crossover.linkLR = true;
    initCrossoverUI(state, crossoverFixtureElements());
    const mutation = CrossoverUI.collectCrossoverWayMutation();
    assert.equal(mutation.lowpass.frequency_hz, 2000);
    assert.ok(!('level_db' in mutation), 'linked mutation carries no trim');
    assert.ok(!('alignment_ms' in mutation), 'linked mutation carries no align');
    assert.ok(!('polarity' in mutation), 'linked mutation carries no polarity');
}
{
    // The sub-owned Low high-pass is display-only: never persisted, even
    // though the input shows its frequency.
    const state = crossoverFixtureState();
    const mode = state.outputSystem.catalog.modes['stereo-sub'];
    mode.topology.sub_roles = ['sub1'];
    mode.bass_management = { frequency_hz: 80, main_highpass_enabled: true };
    initCrossoverUI(state, crossoverFixtureElements({
        effectsCrossoverFrequencyHighpass: crossoverInput('80') }));
    const mutation = CrossoverUI.collectCrossoverWayMutation();
    assert.equal(mutation.highpass, null, 'derived high-pass is not persisted');
    assert.equal(mutation.lowpass.frequency_hz, 2000);
}
{
    // Every way shows compact trim, but an unrelated save retains the exact
    // catalog values instead of persisting their rounded display strings.
    const state = crossoverFixtureState();
    const mode = state.outputSystem.catalog.modes['stereo-sub'];
    mode.processing.left_low_mid = {};
    mode.processing.right_low_mid = {};
    for (const role of Object.keys(mode.processing)) {
        mode.processing[role] = { level_db: -4.47049, alignment_ms: 7.8125 };
    }
    state.crossover.response = { crossover_enabled: true,
        ways: Object.fromEntries(Object.keys(mode.processing).map((role) => [role, {}])) };
    const elements = crossoverFixtureElements({
        effectsCrossoverCard: { classList: { toggle() {} } },
        effectsCrossoverLevel: crossoverInput(''),
        effectsCrossoverDelay: crossoverInput(''),
    });
    initCrossoverUI(state, elements);
    for (const role of Object.keys(mode.processing)) {
        state.crossover.activeWay = role;
        CrossoverUI.renderCrossoverTile();
        assert.equal(elements.effectsCrossoverLevel.value, '-4.47', `${role} level display`);
        assert.equal(elements.effectsCrossoverDelay.value, '7.81', `${role} align display`);
        const mutation = CrossoverUI.collectCrossoverWayMutation();
        assert.equal(mutation.level_db, -4.47049, `${role} level precision`);
        assert.equal(mutation.alignment_ms, 7.8125, `${role} align precision`);
    }
}
(async () => {
{
    // Typed changes and stepper-dispatched changes show two places after
    // capture; the payload still carries the user's unrounded input.
    const state = crossoverFixtureState();
    const mode = state.outputSystem.catalog.modes['stereo-sub'];
    mode.processing.left_low = { level_db: -4.47049, alignment_ms: 7.8125 };
    state.crossover.response = { crossover_enabled: true, ways: { left_low: {} } };
    const elements = crossoverFixtureElements({
        effectsCrossoverCard: { classList: { toggle() {} } },
        effectsCrossoverLevel: crossoverInput(''),
        effectsCrossoverDelay: crossoverInput(''),
    });
    const applied = [];
    const realFetch = globalThis.fetch;
    const realDocument = globalThis.document;
    globalThis.document = { activeElement: elements.effectsCrossoverLevel };
    globalThis.fetch = async () => ({ ok: true, json: async () => state.crossover.response });
    initCrossoverUI(state, elements, {
        applyMutation: async (_kind, fields) => { applied.push(fields); return { ok: true }; },
    });
    CrossoverUI.renderCrossoverTile();
    CrossoverUI.wireCrossoverTile();
    elements.effectsCrossoverLevel.value = '-3.141592';
    elements.effectsCrossoverLevel.emit('change');
    assert.equal(applied[0].level_db, -3.141592);
    assert.equal(applied[0].alignment_ms, 7.8125);
    assert.equal(elements.effectsCrossoverLevel.value, '-3.14');
    await new Promise(setImmediate);
    globalThis.document.activeElement = elements.effectsCrossoverDelay;
    elements.effectsCrossoverDelay.value = '8.3136';
    elements.effectsCrossoverDelay.emit('change');
    assert.equal(applied[1].alignment_ms, 8.3136);
    assert.equal(elements.effectsCrossoverDelay.value, '8.31');
    globalThis.fetch = realFetch;
    globalThis.document = realDocument;
}
{
    // First valid 2-way config seeds every way with starters, then refetches.
    const realFetch = globalThis.fetch;
    const applied = [];
    const toasts = [];
    const state = crossoverFixtureState();
    const elements = crossoverFixtureElements();
    initCrossoverUI(state, elements, {
        showToast: (message) => toasts.push(message),
        applyMutation: async (kind, fields) => { applied.push([kind, fields]); return { ok: true }; },
    });
    globalThis.fetch = async () => ({ ok: true, json: async () => ({ ways: {} }) });
    await CrossoverUI.maybeApplyCrossoverStarters();
    globalThis.fetch = realFetch;
    assert.equal(applied.length, 4, 'all four 2-way roles are seeded');
    assert.equal(applied[0][1].role, 'left_low');
    assert.equal(applied[0][1].lowpass.frequency_hz, 2000);
    assert.ok(toasts.length > 0, 'seeding reports success');
    assert.deepEqual(state.crossover.response, { ways: {} });
}
{
    // A linked save mirrors the shared filters to the mirror way; the
    // mirror carries no trim.
    const realFetch = globalThis.fetch;
    const applied = [];
    const state = crossoverFixtureState();
    state.crossover.linkLR = true;
    initCrossoverUI(state, crossoverFixtureElements(), {
        applyMutation: async (kind, fields) => { applied.push(fields); return { ok: true }; },
    });
    globalThis.fetch = async () => ({ ok: true, json: async () => ({ ways: {} }) });
    await CrossoverUI.saveCrossoverWay();
    globalThis.fetch = realFetch;
    assert.equal(applied.length, 2);
    assert.equal(applied[0].role, 'left_low');
    assert.equal(applied[1].role, 'right_low');
    assert.ok(!('level_db' in applied[1]), 'mirror save carries no trim');
    assert.equal(applied[1].lowpass.frequency_hz, 2000);
}
console.log('crossover frontend tests: ok');
})().catch((error) => { console.error(error); process.exit(1); });
