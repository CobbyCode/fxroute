#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only
// Focused frontend tests for the output-system controller: catalog fetch
// and revisioned mutation orchestration keep their exact downstream refresh
// order and side effects; output_state.js stays the domain module underneath.
// app.js keeps thin delegating wrappers.

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const repoRoot = path.resolve(__dirname, '..');
const appSource = fs.readFileSync(path.join(repoRoot, 'static', 'app.js'), 'utf8');
const indexSource = fs.readFileSync(path.join(repoRoot, 'static', 'index.html'), 'utf8');
const controllerSource = fs.readFileSync(path.join(repoRoot, 'static', 'output_system_controller.js'), 'utf8');
require('../static/output_state.js');
const Controller = require('../static/output_system_controller.js');

// Module boundary: canonical owner plus app.js delegation and script order.
for (const name of ['fetchOutputSystemCatalog', 'applyOutputSystemMutation']) {
    assert.match(controllerSource, new RegExp(`function ${name}\\(`), `controller must own ${name}`);
    assert.match(appSource, new RegExp(`function ${name}\\(`), `app.js must keep a ${name} wrapper`);
}
assert.match(appSource, /FXRouteOutputSystemController/, 'app.js wrappers must delegate to the controller');
assert.match(indexSource, /output_system_controller\.js\?v=\d+\.\d+\.\d+/);
assert.ok(indexSource.indexOf('output_system_controller.js') < indexSource.indexOf('/static/app.js'),
    'controller must load before app.js');
// Ownership stays put: boxes, section render and bank binding remain in app.js,
// the revision/409/retry core remains in output_state.js.
assert.match(appSource, /function ensureOutputSystemBoxes\(/, 'box ownership stays in app.js');
assert.match(appSource, /function renderOutputSystemSection\(/, 'section render stays in app.js');
assert.match(controllerSource, /deps\.renderOutputSection\(\)/, 'controller refreshes via callback');
assert.match(controllerSource, /deps\.syncCrossover\(/, 'crossover follows via callback');
assert.doesNotMatch(controllerSource, /renderCrossoverTile\(\)/, 'no tile render inside the controller');
assert.doesNotMatch(controllerSource, /renderSubwooferPanel\(\)/, 'no subwoofer render inside the controller');

function catalogFixture(crossoverEnabled = true) {
    return { active_mode: 'stereo', revision: 5,
        modes: { stereo: { crossover_enabled: crossoverEnabled } } };
}

function makeHarness({ catalog = catalogFixture(), busy = false, fetchImpl = null } = {}) {
    const state = { outputSystem: { catalog, busy },
        crossover: { activeWay: null, response: { stale: true }, busy: false, linkLR: false } };
    const calls = [];
    const toasts = [];
    Controller.init({
        getState: () => state,
        showToast: (message, type) => toasts.push([message, type]),
        ensureOutputBoxes: () => {
            if (!state.outputSystem) state.outputSystem = { catalog: null, busy: false };
        },
        outputSystemModule: () => globalThis.FXRouteOutputState,
        renderOutputSection: () => calls.push('renderOutputSection'),
        renderBankSelector: () => calls.push('renderBankSelector'),
        renderCompare: () => calls.push('renderCompare'),
        syncCrossover: (enabled) => calls.push(`syncCrossover:${enabled}`),
        syncSpeakerAlign: () => calls.push('syncSpeakerAlign'),
        renderSubwoofer: () => calls.push('renderSubwoofer'),
        syncAutoSub: () => calls.push('syncAutoSub'),
        reportMutationError: (html) => calls.push(`reportMutationError:${html}`),
        syncCompareBusy: () => calls.push('syncCompareBusy'),
    });
    if (fetchImpl) globalThis.fetch = fetchImpl;
    return { state, calls, toasts };
}

const realFetch = globalThis.fetch;
const freshCatalog = (revision) => ({ active_mode: 'stereo', revision,
    modes: { stereo: { crossover_enabled: true } } });

(async () => {
    // Fetch uses the cache unless forced, then refreshes in fixed order.
    {
        const { state, calls } = makeHarness();
        const same = await Controller.fetchOutputSystemCatalog();
        assert.equal(same.revision, 5, 'cached catalog returns without fetch');
        assert.deepEqual(calls, [], 'cache hit renders nothing');
        assert.deepEqual(state.crossover.response, { stale: true }, 'cache hit touches no tile state');
    }
    {
        const { state, calls } = makeHarness({
            fetchImpl: async () => ({ ok: true, json: async () => freshCatalog(6) }) });
        const catalog = await Controller.fetchOutputSystemCatalog(true);
        assert.equal(catalog.revision, 6);
        assert.deepEqual(calls, ['renderOutputSection', 'renderCompare',
            'syncCrossover:true', 'syncSpeakerAlign', 'renderSubwoofer', 'syncAutoSub']);
        globalThis.fetch = realFetch;
    }
    {
        // A mode without crossover clears the stale response through the callback.
        const { state, calls } = makeHarness({ catalog: catalogFixture(false),
            fetchImpl: async () => ({ ok: true, json: async () => catalogFixture(false) }) });
        await Controller.fetchOutputSystemCatalog(true);
        assert.ok(calls.includes('syncCrossover:false'));
        globalThis.fetch = realFetch;
    }
    {
        // A failed refresh retains the last known catalog and still paints.
        const { state, calls } = makeHarness({
            fetchImpl: async () => { throw new Error('down'); } });
        const catalog = await Controller.fetchOutputSystemCatalog(true);
        assert.equal(catalog?.revision, 5, 'failed refresh must retain the last known catalog');
        assert.equal(state.outputSystem.catalog?.revision, 5);
        assert.ok(calls.includes('renderOutputSection'));
        globalThis.fetch = realFetch;
    }

    {
        const { state } = makeHarness({ catalog: null,
            fetchImpl: async () => { throw new Error('initial fetch failed'); } });
        assert.equal(await Controller.fetchOutputSystemCatalog(true), null);
        assert.equal(state.outputSystem.catalog, null);
        globalThis.fetch = realFetch;
    }

    // A delayed poll cannot replace a catalog from a later completed fetch.
    for (const invalid of [null, {}, { revision: 6 },
        { active_mode: 'stereo', modes: { stereo: {} } },
        { active_mode: 'stereo', revision: -1, modes: { stereo: {} } },
        { active_mode: 'stereo', revision: '6', modes: { stereo: {} } },
        { active_mode: 'stereo', revision: 6, modes: {} },
        { active_mode: 'stereo', revision: 6, modes: { stereo: null } }]) {
        for (const previous of [null, catalogFixture()]) {
            const { state, calls } = makeHarness({ catalog: previous,
                fetchImpl: async () => ({ ok: true, json: async () => invalid }) });
            assert.equal(await Controller.fetchOutputSystemCatalog(true), previous,
                'malformed refresh must not replace the last usable catalog');
            assert.equal(state.outputSystem.catalog, previous);
            assert.ok(calls.includes('renderSubwoofer'), 'failed catalog reads do not abort downstream rendering');
            globalThis.fetch = async () => ({ ok: true, json: async () => freshCatalog(7) });
            assert.equal((await Controller.fetchOutputSystemCatalog(true)).revision, 7,
                'a later valid catalog recovers normally');
            globalThis.fetch = realFetch;
        }
    }

    // A delayed poll cannot replace a catalog from a later completed fetch.
    {
        let release;
        const gate = new Promise(resolve => { release = resolve; });
        let reads = 0;
        const { state } = makeHarness({ fetchImpl: async () => {
            const revision = ++reads === 1 ? 6 : 7;
            if (revision === 6) await gate;
            return { ok: true, json: async () => freshCatalog(revision) };
        } });
        const old = Controller.fetchOutputSystemCatalog(true);
        await Controller.fetchOutputSystemCatalog(true);
        release();
        await old;
        assert.equal(state.outputSystem.catalog.revision, 7, 'late poll must not roll back committed output state');
        globalThis.fetch = realFetch;
    }

    // An obsolete failed request cannot erase the newer revision guard and
    // thereby allow another old successful poll to roll state back.
    {
        let releaseOld, releaseFailure;
        const oldGate = new Promise(resolve => { releaseOld = resolve; });
        const failureGate = new Promise(resolve => { releaseFailure = resolve; });
        let reads = 0;
        const { state } = makeHarness({ fetchImpl: async () => {
            const request = ++reads;
            if (request === 1) {
                await oldGate;
                return { ok: true, json: async () => freshCatalog(6) };
            }
            if (request === 2) {
                await failureGate;
                throw new Error('obsolete poll failed');
            }
            return { ok: true, json: async () => freshCatalog(7) };
        } });
        const old = Controller.fetchOutputSystemCatalog(true);
        const failure = Controller.fetchOutputSystemCatalog(true);
        await Controller.fetchOutputSystemCatalog(true);
        releaseFailure();
        await failure;
        const afterFailure = state.outputSystem.catalog?.revision;
        releaseOld();
        await old;
        assert.equal(afterFailure, 7, 'obsolete failed poll must retain the newer accepted catalog');
        assert.equal(state.outputSystem.catalog.revision, 7, 'old success must still be rejected after the failed poll');
        globalThis.fetch = realFetch;
    }

    // Even a failing poll started after the new revision must not erase the
    // guard while an older successful response is still outstanding.
    {
        let releaseOld;
        const oldGate = new Promise(resolve => { releaseOld = resolve; });
        let reads = 0;
        const { state } = makeHarness({ fetchImpl: async () => {
            const request = ++reads;
            if (request === 1) {
                await oldGate;
                return { ok: true, json: async () => freshCatalog(6) };
            }
            if (request === 3) throw new Error('newer refresh failed');
            return { ok: true, json: async () => freshCatalog(7) };
        } });
        const old = Controller.fetchOutputSystemCatalog(true);
        await Controller.fetchOutputSystemCatalog(true);
        await Controller.fetchOutputSystemCatalog(true);
        const afterFailure = state.outputSystem.catalog?.revision;
        releaseOld();
        await old;
        assert.equal(afterFailure, 7, 'failed refresh must preserve the last known revision');
        assert.equal(state.outputSystem.catalog.revision, 7, 'old response cannot replace state after a newer failed refresh');
        globalThis.fetch = realFetch;
    }

    // Mutation: busy guard short-circuits without side effects.
    {
        const { calls, toasts } = makeHarness({ busy: true,
            fetchImpl: async () => { throw new Error('must not fetch'); } });
        const result = await Controller.applyOutputSystemMutation('set_routing', {}, 'done');
        assert.equal(result, null);
        assert.deepEqual(calls, []);
        assert.deepEqual(toasts, []);
        globalThis.fetch = realFetch;
    }

    // Mutation success: busy paint, apply, refetch, toasts, busy release.
    {
        const seen = [];
        const { state, calls, toasts } = makeHarness({
            fetchImpl: async (url, options) => {
                seen.push(url);
                if (url.endsWith('/apply')) {
                    return { ok: true, json: async () => ({ revision: 6, live_applied: true }) };
                }
                return { ok: true, json: async () => freshCatalog(6) };
            } });
        const data = await Controller.applyOutputSystemMutation('set_routing',
            { mode: 'stereo', assignments: [] }, 'Routing saved');
        assert.equal(data.revision, 6);
        assert.equal(state.outputSystem.catalog.revision, 6, 'refetch commits the new revision');
        assert.equal(state.outputSystem.busy, false);
        assert.deepEqual(calls.slice(0, 2), ['renderOutputSection', 'renderBankSelector']);
        assert.ok(calls.includes('syncCrossover:true'), 'refetch fans out in order');
        assert.ok(calls.includes('syncCompareBusy'), 'compare busy lifts in finally');
        assert.deepEqual(toasts, [['Routing saved', 'success']]);
        globalThis.fetch = realFetch;
    }

    // Mutation failure: error render plus error toast, null result.
    {
        const { state, calls, toasts } = makeHarness({
            fetchImpl: async () => ({ ok: false, status: 500, json: async () => ({ detail: 'nope-17' }) }) });
        const result = await Controller.applyOutputSystemMutation('set_routing', {}, 'done');
        assert.equal(result, null);
        assert.equal(state.outputSystem.busy, false);
        assert.ok(calls.some((call) => call.startsWith('reportMutationError:')
            && call.includes('nope-17')), 'backend message reaches the feedback row');
        assert.deepEqual(toasts, [['nope-17', 'error']]);
        assert.ok(calls.includes('syncCompareBusy'));
        globalThis.fetch = realFetch;
    }

    // Quiet mode suppresses the live-apply note but keeps the success toast.
    {
        const { toasts } = makeHarness({
            fetchImpl: async (url) => {
                if (url.endsWith('/apply')) {
                    return { ok: true, json: async () => ({ revision: 6,
                        live_applied: false, live_reason: 'dsp-busy' }) };
                }
                return { ok: true, json: async () => freshCatalog(6) };
            } });
        await Controller.applyOutputSystemMutation('set_routing', {}, 'Routing saved', { quiet: true });
        assert.deepEqual(toasts, [['Routing saved', 'success']]);
        globalThis.fetch = realFetch;
    }

    console.log('output system controller frontend tests: ok');
})().catch((error) => { globalThis.fetch = realFetch; console.error(error); process.exit(1); });
