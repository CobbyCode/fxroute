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
    const listeners = {};
    return { value: '', textContent: '', innerHTML: '', disabled: false,
        checked: false, dataset: {}, classList: stubClassList(),
        setAttribute() {}, addEventListener(type, listener) { (listeners[type] ||= []).push(listener); },
        emit(type) { for (const listener of listeners[type] || []) listener({ target: this }); },
        focus() {}, select() {},
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

    // Preview curves: LR24 / 80 Hz low-pass and high-pass cross at the 80 Hz
    // marker at -6 dB; a sub trim must not shift the drawn low-pass.
    {
        require('../static/crossover.js');
        const strokes = [];
        let path = [];
        const ctx = {
            setTransform() {}, clearRect() {}, fillRect() {}, strokeRect() {}, fillText() {},
            measureText: () => ({ width: 30 }),
            beginPath() { path = []; }, moveTo(x, y) { path.push([x, y]); }, lineTo(x, y) { path.push([x, y]); },
            stroke() { strokes.push({ color: this.strokeStyle, points: path.slice() }); },
        };
        const canvas = { clientWidth: 560, clientHeight: 132, width: 0, height: 0, getContext: () => ctx };
        initSubUI({ outputSystem: {} }, { effectsSubwooferPreview: canvas });
        SubwooferUI.drawSubwooferPreview({ crossover_frequency_hz: 80, family: 'linkwitz-riley',
            slope_db_oct: 24, main_highpass_enabled: true, sub_level_db: 2.3 });
        const { pad, plotW, plotH } = SubwooferUI.getSubwooferPreviewLayout(560, 132);
        const xFor = (hz) => pad.left + (Math.log10(hz / 20) / Math.log10(300 / 20)) * plotW;
        const dbAt = (points, hz) => {
            const x = xFor(hz);
            const index = points.findIndex(([px]) => px >= x);
            const [x0, y0] = points[index - 1];
            const [x1, y1] = points[index];
            const y = y0 + (y1 - y0) * (x - x0) / (x1 - x0);
            return -((y - pad.top) / plotH) * 18;
        };
        const lowpass = strokes.find((stroke) => stroke.color === '#6ee7b7');
        const highpass = strokes.find((stroke) => stroke.color === '#93c5fd');
        assert.ok(lowpass && highpass, 'preview draws the sub low-pass and the main high-pass');
        assert.ok(Math.abs(dbAt(lowpass.points, 80) + 6.02) < 0.05, 'low-pass is -6 dB at the marker');
        assert.ok(Math.abs(dbAt(highpass.points, 80) + 6.02) < 0.05, 'high-pass is -6 dB at the marker');
        assert.ok(Math.abs(dbAt(lowpass.points, 40) + 0.53) < 0.05, 'sub trim does not lift the low-pass');
    }

    // Panel visibility follows the routed mode, never the settings dialog.
    for (const catalog of [{}, { revision: 1 }, { active_mode: 'stereo-sub', revision: 1 },
        { active_mode: 'stereo-sub', revision: 1, modes: {} }]) {
        const card = stubEl();
        initSubUI({ outputSystem: { catalog } }, { effectsSubwooferCard: card });
        assert.equal(SubwooferUI.routedSubwooferView().mode, 'stereo', 'incomplete catalogs have no routed subs');
        SubwooferUI.renderSubwooferPanel();
        assert.equal(card.classList.has('hidden'), true, 'incomplete catalogs hide the tile');
    }
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
        assert.equal(elements.effectsSubwooferLevel.value, '0.00');
        assert.equal(elements.effectsSubwooferDelay.value, '0.00');
    }

    // Sub 1 and Sub 2 trims share the crossover's compact display; the
    // collection still sees the original numeric values on unrelated saves.
    {
        const catalog = subCatalog();
        catalog.modes['stereo-sub'].topology.sub_roles = ['sub1', 'sub2'];
        catalog.modes['stereo-sub'].processing = {
            sub1: { level_db: -4.5, alignment_ms: 7.81 },
            sub2: { level_db: -3.2, alignment_ms: -39.99 },
        };
        const state = { outputSystem: { catalog } };
        const elements = subElementsFixture();
        initSubUI(state, elements, { applyMutation: async () => ({ ok: true }) });
        SubwooferUI.renderSubwooferPanel();
        assert.equal(elements.effectsSubwooferLevel.value, '-4.50');
        assert.equal(elements.effectsSubwooferDelay.value, '7.81');
        assert.equal(elements.effectsSubwooferSub2Level.value, '-3.20');
        assert.equal(elements.effectsSubwooferSub2Delay.value, '-39.99');
        const collected = SubwooferUI.collectSubwoofer22Settings();
        assert.equal(collected.subwoofers.sub1.level_db, -4.5);
        assert.equal(collected.subwoofers.sub1.alignment_ms, 7.81);
        assert.equal(collected.subwoofers.sub2.level_db, -3.2);
        assert.equal(collected.subwoofers.sub2.alignment_ms, -39.99);

        SubwooferUI.wireSubwooferControls();
        elements.effectsSubwooferLevel.value = '-2.567';
        elements.effectsSubwooferLevel.emit('change');
        assert.equal(elements.effectsSubwooferLevel.value, '-2.60');
        elements.effectsSubwooferSub2Delay.value = '8.316';
        elements.effectsSubwooferSub2Delay.emit('change');
        assert.equal(elements.effectsSubwooferSub2Delay.value, '8.32');
        await SubwooferUI.flushSubwooferSettingsBeforeMeasurement();
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

    // Navigation alone must not send a full, possibly stale sub snapshot.
    {
        let writes = 0;
        const state = { outputSystem: { catalog: subCatalog() } };
        const elements = subElementsFixture();
        initSubUI(state, elements, { applyMutation: async () => { writes++; return { ok: true }; } });
        SubwooferUI.renderSubwooferPanel();
        SubwooferUI.wireSubwooferControls();
        elements.effectsSubwooferLevel.emit('focus');
        elements.effectsSubwooferLevel.emit('blur');
        await SubwooferUI.flushSubwooferSettingsBeforeMeasurement();
        assert.equal(writes, 0, 'leaving an unchanged field must not re-save stale neighbor values');
    }

    // An old save echo must not replace a newer, unfocused local sub draft.
    {
        const state = { outputSystem: { catalog: subCatalog() } };
        state.outputSystem.catalog.modes['stereo-sub'].processing.sub1.level_db = 6;
        const elements = subElementsFixture();
        let release;
        const gate = new Promise(resolve => { release = resolve; });
        initSubUI(state, elements, { applyMutation: async () => { await gate; return { ok: true }; } });
        SubwooferUI.renderSubwooferPanel();
        elements.effectsSubwooferLevel.value = '-24';
        const save = SubwooferUI.saveSubwooferDebounced(0);
        await delay(10);
        SubwooferUI.renderSubwooferPanel();
        const displayed = elements.effectsSubwooferLevel.value;
        state.outputSystem.catalog.modes['stereo-sub'].processing.sub1.level_db = -24;
        release();
        await save;
        await tick();
        assert.equal(Number(displayed), -24, 'old catalog must not roll an unfocused pending trim back to +6 dB');
        assert.equal(Number(elements.effectsSubwooferLevel.value), -24);
    }

    // A held save keeps only the newest pending remote step, not a long
    // backlog that replays obsolete levels after the user stops pressing.
    {
        const state = { outputSystem: { catalog: subCatalog() } };
        const elements = subElementsFixture();
        const applied = [];
        let release;
        const gate = new Promise(resolve => { release = resolve; });
        initSubUI(state, elements, { applyMutation: async (kind, fields) => {
            applied.push(fields.processing.sub1.level_db);
            if (applied.length === 1) await gate;
            state.outputSystem.catalog.modes['stereo-sub'].processing.sub1 = fields.processing.sub1;
            return { ok: true };
        } });
        elements.effectsSubwooferLevel.value = '-0.1';
        const first = SubwooferUI.saveSubwooferDebounced(0);
        await delay(10);
        for (const value of ['-1', '-2', '-3']) {
            elements.effectsSubwooferLevel.value = value;
            SubwooferUI.saveSubwooferDebounced(0);
            await delay(10);
        }
        release();
        await first;
        await SubwooferUI.flushSubwooferSettingsBeforeMeasurement();
        assert.deepEqual(applied, [-0.1, -3], 'remote repeats must coalesce behind the active save');
    }

    // Returning to the in-flight value cancels the newer pending request,
    // including its draft, before another sub can collect that neighbor.
    {
        const state = { outputSystem: { catalog: subCatalog() } };
        const elements = subElementsFixture();
        let release;
        const gate = new Promise(resolve => { release = resolve; });
        initSubUI(state, elements, { applyMutation: async (kind, fields) => {
            await gate;
            state.outputSystem.catalog.modes['stereo-sub'].processing.sub1 = fields.processing.sub1;
            return { ok: true };
        } });
        elements.effectsSubwooferLevel.value = '-11';
        const first = SubwooferUI.saveSubwooferDebounced(0);
        await delay(10);
        elements.effectsSubwooferLevel.value = '-12';
        SubwooferUI.saveSubwooferDebounced(0);
        elements.effectsSubwooferLevel.value = '-11';
        SubwooferUI.saveSubwooferDebounced(0);
        SubwooferUI.renderSubwooferPanel();
        const displayed = Number(elements.effectsSubwooferLevel.value);
        release();
        await first;
        await SubwooferUI.flushSubwooferSettingsBeforeMeasurement();
        assert.equal(displayed, -11, 'reverse step must discard the canceled local draft');
    }

    // Switching Surround groups while a save is held must retain all
    // outstanding per-group patches when the pending request is coalesced.
    {
        const catalog = subCatalog();
        catalog.active_mode = 'surround';
        catalog.modes.surround = catalog.modes['stereo-sub'];
        const config = catalog.modes.surround;
        config.topology.roles = ['main_l', 'main_r', 'center', 'sub1'];
        config.bass_management = { sub_link: false, sub_filters: {
            front: { frequency_hz: 80 }, center: { frequency_hz: 110 } } };
        const state = { outputSystem: { catalog } };
        const elements = subElementsFixture();
        let release;
        const gate = new Promise(resolve => { release = resolve; });
        let writes = 0;
        initSubUI(state, elements, { applyMutation: async (kind, fields) => {
            if (++writes === 1) await gate;
            config.bass_management.sub_filters = { ...config.bass_management.sub_filters, ...fields.sub_filters };
            return { ok: true };
        } });
        SubwooferUI.setSubwooferSelectedGroup('front');
        SubwooferUI.renderSubwooferPanel();
        elements.effectsSubwooferLeftFrequency.value = '90';
        const first = SubwooferUI.saveSubwooferDebounced(0);
        await delay(10);
        elements.effectsSubwooferLeftFrequency.value = '100';
        SubwooferUI.saveSubwooferDebounced(0);
        SubwooferUI.setSubwooferSelectedGroup('center');
        SubwooferUI.renderSubwooferPanel();
        elements.effectsSubwooferLeftFrequency.value = '120';
        SubwooferUI.saveSubwooferDebounced(0);
        release();
        await first;
        await SubwooferUI.flushSubwooferSettingsBeforeMeasurement();
        assert.equal(config.bass_management.sub_filters.front.frequency_hz, 100, 'coalescing must keep the earlier pending group edit');
        assert.equal(config.bass_management.sub_filters.center.frequency_hz, 120);
    }

    // A successful POST may be followed by a failed refresh. Its acknowledged
    // trim must remain visible and survive the next neighbor save.
    {
        const catalog = subCatalog();
        const config = catalog.modes['stereo-sub'];
        config.topology.sub_mode = 'dual-mono';
        config.topology.sub_roles = ['sub1', 'sub2'];
        config.processing = { sub1: { level_db: 0 }, sub2: { level_db: 0 } };
        const state = { outputSystem: { catalog } };
        const elements = subElementsFixture();
        let revision = 4;
        let committed;
        initSubUI(state, elements, { applyMutation: async (kind, fields) => {
            committed = fields.processing;
            // The POST is real success, but a failed GET leaves revision 4.
            return { revision: ++revision, live_applied: true };
        } });
        SubwooferUI.renderSubwooferPanel();
        elements.effectsSubwooferLevel.value = '-21';
        await SubwooferUI.saveSubwooferDebounced(0);
        const displayed = Number(elements.effectsSubwooferLevel.value);
        elements.effectsSubwooferSub2Level.value = '-1';
        await SubwooferUI.saveSubwooferDebounced(0);
        const savedNeighbor = committed.sub1.level_db;
        catalog.revision = 6;
        config.processing = committed;
        SubwooferUI.renderSubwooferPanel();
        catalog.revision = 7;
        config.processing.sub1 = { ...config.processing.sub1, level_db: -22 };
        SubwooferUI.renderSubwooferPanel();
        assert.equal(displayed, -21, 'failed post-save refresh must not repaint the pre-save trim');
        assert.equal(savedNeighbor, -21, 'neighbor save must not overwrite the acknowledged trim');
        assert.equal(Number(elements.effectsSubwooferLevel.value), -22, 'fresh server state must release the acknowledged draft');
    }

    // A later failed save must not discard an earlier acknowledged baseline
    // that a failed refresh has not yet put into the catalog.
    {
        const catalog = subCatalog();
        const config = catalog.modes['stereo-sub'];
        config.topology.sub_mode = 'dual-mono';
        config.topology.sub_roles = ['sub1', 'sub2'];
        config.processing = { sub1: { level_db: 0 }, sub2: { level_db: 0 } };
        const state = { outputSystem: { catalog } };
        const elements = subElementsFixture();
        let calls = 0;
        let committed;
        initSubUI(state, elements, { applyMutation: async (kind, fields) => {
            if (++calls === 2) throw new Error('neighbor save failed');
            committed = fields.processing;
            return { revision: calls === 1 ? 5 : 6, live_applied: true };
        } });
        SubwooferUI.renderSubwooferPanel();
        elements.effectsSubwooferLevel.value = '-21';
        await SubwooferUI.saveSubwooferDebounced(0);
        elements.effectsSubwooferSub2Level.value = '-1';
        await assert.rejects(SubwooferUI.saveSubwooferDebounced(0), /neighbor save failed/);
        SubwooferUI.renderSubwooferPanel();
        const displayed = Number(elements.effectsSubwooferLevel.value);
        elements.effectsSubwooferSub2Level.value = '-2';
        await SubwooferUI.saveSubwooferDebounced(0);
        const savedNeighbor = committed.sub1.level_db;
        catalog.revision = 6;
        config.processing = committed;
        SubwooferUI.renderSubwooferPanel();
        assert.equal(displayed, -21, 'failed later save must retain the earlier acknowledged trim');
        assert.equal(savedNeighbor, -21, 'retrying the neighbor must retain the earlier successful trim');
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
