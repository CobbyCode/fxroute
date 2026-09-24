#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only
// Focused frontend tests for the subwoofer tile module: normalizers keep
// their floors/fallbacks, the panel renders/hides per routed mode, the
// debounced save queue commits once with the latest values in order, and a
// measurement preflush waits for a pending commit. app.js keeps thin
// delegating wrappers.

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const repoRoot = path.resolve(__dirname, '..');
const appSource = fs.readFileSync(path.join(repoRoot, 'static', 'app.js'), 'utf8');
const indexSource = fs.readFileSync(path.join(repoRoot, 'static', 'index.html'), 'utf8');
require('../static/output_state.js');
require('../static/crossover_ui.js');
const SubwooferUI = require('../static/subwoofer_ui.js');

if (typeof globalThis.window === 'undefined') globalThis.window = {};
globalThis.window.setTimeout = globalThis.window.setTimeout || setTimeout;
globalThis.window.clearTimeout = globalThis.window.clearTimeout || clearTimeout;
globalThis.window.requestAnimationFrame = globalThis.window.requestAnimationFrame || ((fn) => { fn(); return 1; });
globalThis.window.cancelAnimationFrame = globalThis.window.cancelAnimationFrame || (() => {});
globalThis.window.devicePixelRatio = globalThis.window.devicePixelRatio || 1;

// Module boundary: canonical owner plus app.js delegation and script order.
for (const name of ['renderSubwooferPanel', 'saveSubwooferDebounced',
    'flushSubwooferSettingsBeforeMeasurement', 'normalizeSubwooferSettings',
    'collectSubwooferSettings', 'drawSubwooferPreview',
    'beginSubwooferSave', 'routedSubwooferView', 'isSubwooferModeName']) {
    assert.match(appSource, new RegExp(`function ${name}\\(`), `app.js must keep a ${name} wrapper`);
}
assert.equal(typeof SubwooferUI.wireSubwooferControls, 'function', 'subwoofer module owns the wiring');
assert.doesNotMatch(appSource, /\n(?:async function|function) wireSubwooferControls\(/, 'shim dropped from app.js');
assert.match(appSource, /FXRouteSubwooferUI/, 'app.js wrappers must delegate to the subwoofer module');
assert.match(indexSource, /subwoofer_ui\.js\?v=\d+\.\d+\.\d+/);
assert.ok(indexSource.indexOf('subwoofer_ui.js') < indexSource.indexOf('/static/app.js'),
    'subwoofer module must load before app.js');

function stubClassList(initial = []) {
    const set = new Set(initial);
    return { add: (c) => set.add(c), remove: (c) => set.delete(c),
        toggle: (c, force) => { if (force === undefined) { if (set.has(c)) set.delete(c); else set.add(c); } else if (force) set.add(c); else set.delete(c); },
        contains: (c) => set.has(c), has: (c) => set.has(c) };
}

function stubEl(overrides = {}) {
    return { value: '', textContent: '', innerHTML: '', disabled: false,
        checked: false, dataset: {}, classList: stubClassList(),
        setAttribute() {}, addEventListener() {}, focus() {}, select() {},
        closest: () => null, ...overrides };
}

function subCatalog() {
    return { active_mode: 'stereo-sub', revision: 4,
        capabilities: { filter_families: { 'linkwitz-riley': [12, 24] } },
        modes: { 'stereo-sub': {
            topology: { sub_mode: 'mono', sub_roles: ['sub1'] },
            bass_management: {},
            processing: { sub1: {} },
        } } };
}

function subElementsFixture() {
    const input = (value) => stubEl({ value });
    return {
        effectsSubwooferCard: stubEl(),
        effectsSubwooferSharedCrossover: stubEl(),
        effectsSubwooferLeftCrossover: stubEl(),
        effectsSubwooferRightCrossover: stubEl(),
        effectsSubwooferGlobalLabel: stubEl(),
        effectsSubwooferTabRow: stubEl(),
        effectsSubwooferSideTabs: stubEl(),
        effectsSubwooferTabLeft: stubEl(),
        effectsSubwooferTabRight: stubEl(),
        effectsSubwooferTabBoth: stubEl(),
        effectsSubwooferLinkWrap: stubEl(),
        effectsSubwooferFrequencyNumber: input('80'),
        effectsSubwooferFamily: input('linkwitz-riley'),
        effectsSubwooferSlope: input('24'),
        effectsSubwooferLeftFrequency: input('80'),
        effectsSubwooferLeftFamily: input('linkwitz-riley'),
        effectsSubwooferLeftSlope: input('24'),
        effectsSubwooferRightFrequency: input('80'),
        effectsSubwooferRightFamily: input('linkwitz-riley'),
        effectsSubwooferRightSlope: input('24'),
        effectsSubwooferLink: stubEl({ checked: true }),
        effectsSubwooferMainHighpass: input('on'),
        effectsSubwooferLeftMainHighpass: input('on'),
        effectsSubwooferRightMainHighpass: input('on'),
        effectsSubwooferRouting: stubEl(),
        effectsSubwooferModeBadge: stubEl(),
        effectsSubwooferLevelLabel: stubEl(),
        effectsSubwooferDelayLabel: stubEl(),
        effectsSubwooferPolarityLabel: stubEl(),
        effectsSubwooferSub1GroupLabel: stubEl(),
        effectsSubwooferSub2GroupLabel: stubEl(),
        effectsSubwooferSub2LevelLabel: stubEl(),
        effectsSubwooferSub2DelayLabel: stubEl(),
        effectsSubwooferSub2PolarityLabel: stubEl(),
        effectsSubwooferLevel: input('0'),
        effectsSubwooferDelay: input('0'),
        effectsSubwooferPolarity: input('normal'),
        effectsSubwooferSub2Level: input('0'),
        effectsSubwooferSub2Delay: input('0'),
        effectsSubwooferSub2Polarity: input('normal'),
        effectsSubwooferSub2Fields: [],
        effectsSubwooferDerivedDelays: stubEl(),
        effectsSubwooferDdMain: stubEl(),
        effectsSubwooferDdSub1: stubEl(),
        effectsSubwooferDdSub2: stubEl(),
        effectsSubwooferFeedback: stubEl(),
    };
}

function initSubUI(state, elements, extra = {}) {
    SubwooferUI.init({ getState: () => state, getElements: () => elements,
        getActiveEditing: () => new Set(), applyMutation: async () => null, ...extra });
}

const tick = () => new Promise((resolve) => setImmediate(resolve));
const delay = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

(async () => {
    // Normalizers: floors, fallbacks and legacy aliases are unchanged.
    {
        initSubUI({ outputSystem: {} }, {});
        assert.equal(SubwooferUI.normalizeSingleSubwooferSettings({ level_db: -80 }).level_db, -80);
        assert.equal(SubwooferUI.normalizeSubwooferSettings({ sub_level_db: -80 }).sub_level_db, -80);
        assert.equal(SubwooferUI.normalizeSubwooferSettings({}).crossover_frequency_hz, 80);
        assert.equal(SubwooferUI.normalizeSubwooferSettings({ family: 'nope' }).family, 'linkwitz-riley');
        assert.equal(SubwooferUI.isSubwooferModeName('subwoofer-2.1'), true);
        assert.equal(SubwooferUI.isSubwooferModeName('stereo'), false);
        assert.equal(SubwooferUI.isSubwoofer22Mode('subwoofer-2.2'), true);
        assert.equal(SubwooferUI.routedSubwooferView().mode, 'stereo');
    }

    // Panel visibility follows the routed mode, never the settings dialog.
    {
        const card = stubEl();
        initSubUI({ outputSystem: { catalog: null } }, { effectsSubwooferCard: card });
        SubwooferUI.renderSubwooferPanel();
        assert.equal(card.classList.has('hidden'), true, 'no catalog hides the tile');
    }
    {
        const state = { outputSystem: { catalog: subCatalog() } };
        const elements = subElementsFixture();
        initSubUI(state, elements);
        SubwooferUI.renderSubwooferPanel();
        assert.equal(elements.effectsSubwooferCard.classList.has('hidden'), false);
        assert.match(elements.effectsSubwooferRouting.textContent, /Crossover 80 Hz/);
        assert.equal(elements.effectsSubwooferFrequencyNumber.value, '80');
    }

    // Collect reads the DOM values into one committable shape.
    {
        const state = { outputSystem: { catalog: subCatalog() } };
        const elements = subElementsFixture();
        elements.effectsSubwooferLevel.value = '2.5';
        elements.effectsSubwooferPolarity.value = 'invert';
        initSubUI(state, elements);
        const collected = SubwooferUI.collectSubwooferSettings();
        assert.equal(collected.sub_level_db, 2.5);
        assert.equal(collected.sub_polarity, 'invert');
        assert.equal(collected.crossover_frequency_hz, 80);
    }

    // Debounce: rapid edits collapse into one commit with the latest values.
    {
        const applied = [];
        const state = { outputSystem: { catalog: subCatalog() } };
        const elements = subElementsFixture();
        initSubUI(state, elements, {
            applyMutation: async (kind, fields) => { applied.push(fields); return { ok: true }; } });
        elements.effectsSubwooferLevel.value = '1';
        SubwooferUI.saveSubwooferDebounced(30);
        elements.effectsSubwooferLevel.value = '2';
        SubwooferUI.saveSubwooferDebounced(30);
        elements.effectsSubwooferLevel.value = '3';
        await SubwooferUI.saveSubwooferDebounced(30);
        assert.equal(applied.length, 1, 'three rapid edits commit once');
        assert.equal(applied[0].processing.sub1.level_db, 3, 'the commit carries the latest value');
        assert.equal(applied[0].mode, 'stereo-sub');
    }

    // Sequential saves run in commit order with their own payloads.
    {
        const applied = [];
        const state = { outputSystem: { catalog: subCatalog() } };
        const elements = subElementsFixture();
        initSubUI(state, elements, {
            applyMutation: async (kind, fields) => { applied.push(fields.frequency_hz); return { ok: true }; } });
        elements.effectsSubwooferFrequencyNumber.value = '70';
        await SubwooferUI.saveSubwooferDebounced(5);
        elements.effectsSubwooferFrequencyNumber.value = '90';
        await SubwooferUI.saveSubwooferDebounced(5);
        assert.deepEqual(applied, [70, 90]);
    }

    // A mode change before the commit rejects into the tile feedback.
    {
        const state = { outputSystem: { catalog: subCatalog() } };
        const elements = subElementsFixture();
        const feedback = stubEl();
        elements.effectsSubwooferFeedback = feedback;
        initSubUI(state, elements, { applyMutation: async () => ({ ok: true }) });
        const run = SubwooferUI.saveSubwooferDebounced(5);
        state.outputSystem.catalog.active_mode = 'stereo';
        await assert.rejects(run, /Output mode changed/);
        await tick();
        assert.match(feedback.textContent, /Output mode changed/);
    }

    // Flush: idle is a no-op, pending commits gate the measurement start.
    {
        initSubUI({ outputSystem: { catalog: null } }, {});
        await SubwooferUI.flushSubwooferSettingsBeforeMeasurement();
    }
    {
        let release = null;
        const gate = new Promise((resolve) => { release = resolve; });
        const state = { outputSystem: { catalog: subCatalog() } };
        const elements = subElementsFixture();
        elements.effectsSubwooferLevel.value = '4';
        initSubUI(state, elements, { applyMutation: () => gate });
        SubwooferUI.saveSubwooferDebounced(5);
        await delay(25);
        let flushed = false;
        const flush = SubwooferUI.flushSubwooferSettingsBeforeMeasurement().then(() => { flushed = true; });
        await tick();
        await tick();
        assert.equal(flushed, false, 'flush waits for the pending commit');
        release({ saved: true });
        await flush;
        assert.equal(flushed, true);
        await tick();
    }

    console.log('subwoofer ui frontend tests: ok');
})().catch((error) => { console.error(error); process.exit(1); });
