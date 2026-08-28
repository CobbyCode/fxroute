#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only
//
// Output-extras save coupling regression:
// loudnessEnabled presence in the POST /api/dsp/extras body is the canonical
// Loudness/Volume transition signal, so the frontend must send it only on an
// actual enabled-state change.  Unrelated extras saves (limiter, bass,
// crystalizer, ...) must never carry the flag, while a real loudness toggle
// keeps it and rapid/combined edits lose nothing.

const assert = require('assert/strict');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const repoRoot = path.resolve(__dirname, '..');
const appSource = fs.readFileSync(path.join(repoRoot, 'static', 'app.js'), 'utf8');
const indexSource = fs.readFileSync(path.join(repoRoot, 'static', 'index.html'), 'utf8');

function extractFunction(name) {
    const match = new RegExp(`(?:async\\s+)?function\\s+${name}\\s*\\(`).exec(appSource);
    assert.ok(match, `missing function ${name}`);
    let parenDepth = 1;
    let braceStart = -1;
    for (let index = match.index + match[0].length; index < appSource.length; index += 1) {
        if (appSource[index] === '(') parenDepth += 1;
        if (appSource[index] === ')') parenDepth -= 1;
        if (parenDepth === 0) {
            braceStart = appSource.indexOf('{', index);
            break;
        }
    }
    assert.notEqual(braceStart, -1, `missing function body ${name}`);
    let depth = 0;
    let quote = '';
    let escaped = false;
    for (let index = braceStart; index < appSource.length; index += 1) {
        const char = appSource[index];
        if (quote) {
            if (escaped) escaped = false;
            else if (char === '\\') escaped = true;
            else if (char === quote) quote = '';
            continue;
        }
        if (char === "'" || char === '"' || char === '`') quote = char;
        else if (char === '{') depth += 1;
        else if (char === '}') {
            depth -= 1;
            if (depth === 0) return appSource.slice(match.index, index + 1);
        }
    }
    throw new Error(`unterminated function ${name}`);
}

function extractConst(name) {
    const match = new RegExp(`const ${name}\\s*=`).exec(appSource);
    assert.ok(match, `missing const ${name}`);
    let depth = 0;
    let quote = '';
    let escaped = false;
    for (let index = match.index; index < appSource.length; index += 1) {
        const char = appSource[index];
        if (quote) {
            if (escaped) escaped = false;
            else if (char === '\\') escaped = true;
            else if (char === quote) quote = '';
            continue;
        }
        if (char === "'" || char === '"' || char === '`') quote = char;
        else if (char === '{' || char === '(' || char === '[') depth += 1;
        else if (char === '}' || char === ')' || char === ']') depth -= 1;
        else if (char === ';' && depth === 0) return appSource.slice(match.index, index + 1);
    }
    throw new Error(`unterminated const ${name}`);
}

function serverExtras({ loudnessEnabled = false, limiterEnabled = false } = {}) {
    return {
        limiter: { enabled: limiterEnabled, params: { thresholdDb: -1.0, attackMs: 5.0, releaseMs: 50.0, lookaheadMs: 5.0, stereoLinkPercent: 100.0 } },
        headroom: { enabled: false, params: { gainDb: -3.0 } },
        autogain: { enabled: false, params: { targetDb: -12.0 } },
        loudness: { enabled: loudnessEnabled, params: { fftSize: 4096, strength: 10, volumeDb: 0.0, calibration: {}, calibrationProfiles: {} } },
        delay: { enabled: false, params: { leftMs: 0.0, rightMs: 0.0 } },
        bass_enhancer: { enabled: false, params: { amount: 0.0, harmonics: 8.5, scope: 100.0, blend: 0.0 } },
        tone_effect: { enabled: false, mode: 'crystalizer' },
    };
}

function okResponse(extras) {
    return { ok: true, json: async () => ({ status: 'ok', extras, updated_presets: 1, skipped_presets: [] }) };
}

function makeContext({ responses = [], stateDsp = null } = {}) {
    const fetchCalls = [];
    const toasts = [];
    const queue = responses.slice();
    const state = { dsp: stateDsp };
    const elements = {
        effectsLimiterEnabled: { checked: false },
        effectsHeadroomEnabled: { checked: false },
        effectsHeadroomGainDb: { value: '-3' },
        effectsAutogainEnabled: { checked: false },
        effectsAutogainTargetDb: { value: '-12' },
        effectsLoudnessEnabled: { checked: false },
        effectsLoudnessStrength: { value: '10' },
        effectsLoudnessFftSize: { value: '4096' },
        effectsBassEnabled: { checked: false },
        effectsBassAmount: { value: '0' },
        effectsToneEffectEnabled: { checked: false },
        effectsToneEffectMode: { value: 'crystalizer' },
        effectsExtrasFeedback: null,
    };
    const context = {
        state,
        elements,
        console,
        window: { setTimeout, clearTimeout },
        fetch: async (url, opts) => {
            const call = { url, opts, body: JSON.parse(opts.body) };
            fetchCalls.push(call);
            const response = queue.length ? queue.shift() : okResponse(state.dsp?.global_extras || {});
            return typeof response === 'function' ? response(call) : response;
        },
        showToast: (message) => { toasts.push(message); },
        renderEffects: () => {},
    };
    vm.createContext(context);
    vm.runInContext(`
        let _extrasDebounceTimer = null;
        let effectsExtrasSaveInFlight = false;
        let effectsExtrasPendingResave = false;
        let effectsCompareLoadInFlight = false;
        const EFFECTS_EXTRAS_TOGGLE_DEBOUNCE_MS = 0;
        ${extractConst('EFFECTS_HEADROOM_ALLOWED_GAIN_DB')}
        ${extractConst('EFFECTS_AUTOGAIN_ALLOWED_TARGET_DB')}
        ${extractConst('EFFECTS_LOUDNESS_ALLOWED_FFT_SIZE')}
        ${extractConst('EFFECTS_LOUDNESS_LEGACY_STRENGTHS')}
        ${extractConst('EFFECTS_TONE_EFFECT_MODES')}
        ${extractFunction('normalizeEffectsHeadroomGainDb')}
        ${extractFunction('normalizeEffectsAutogainTargetDb')}
        ${extractFunction('normalizeEffectsLoudnessFftSize')}
        ${extractFunction('normalizeEffectsLoudnessStrength')}
        ${extractFunction('normalizeEffectsToneEffectMode')}
        ${extractFunction('collectEffectsExtras')}
        ${extractFunction('buildEffectsExtrasSaveBody')}
        ${extractFunction('setEffectsExtrasFeedback')}
        ${extractFunction('saveEffectsExtrasDebounced')}
        ${extractFunction('_doSaveEffectsExtras')}
    `, context);
    return { context, state, elements, fetchCalls, toasts };
}

const tick = () => new Promise(resolve => setTimeout(resolve, 0));

async function main() {
    // buildEffectsExtrasSaveBody: presence is decided against the last known
    // server state; unknown server state stays conservative.
    const helperContext = {};
    vm.createContext(helperContext);
    vm.runInContext(extractFunction('buildEffectsExtrasSaveBody'), helperContext);
    const build = helperContext.buildEffectsExtrasSaveBody;
    assert.deepEqual(Object.keys(build({ loudnessEnabled: true, bassAmount: 3 }, { loudness: { enabled: true } })), ['bassAmount']);
    assert.ok('loudnessEnabled' in build({ loudnessEnabled: true }, { loudness: { enabled: false } }));
    assert.ok('loudnessEnabled' in build({ loudnessEnabled: false }, { loudness: { enabled: true } }));
    assert.ok('loudnessEnabled' in build({ loudnessEnabled: true }, undefined));
    assert.deepEqual(build({ limiterEnabled: true }, { loudness: { enabled: false } }).limiterEnabled, true);

    // 1. Limiter-only change: loudnessEnabled must stay out of the body so the
    //    backend never treats the save as a Loudness/Volume transition.
    {
        const env = makeContext({
            responses: [okResponse(serverExtras({ limiterEnabled: true }))],
            stateDsp: { global_extras: serverExtras() },
        });
        env.elements.effectsLimiterEnabled.checked = true;
        env.context._doSaveEffectsExtras('saving');
        await tick(); await tick();
        assert.equal(env.fetchCalls.length, 1);
        assert.equal(env.fetchCalls[0].url, '/api/dsp/extras');
        assert.equal(env.fetchCalls[0].opts.method, 'POST');
        assert.ok(!('loudnessEnabled' in env.fetchCalls[0].body), 'limiter save must not carry loudnessEnabled');
        assert.equal(env.fetchCalls[0].body.limiterEnabled, true);
        assert.equal(env.fetchCalls[0].body.bassEnabled, false);
        assert.ok(env.state.dsp.global_extras.limiter.enabled, 'response must update the client extras state');
        assert.ok(!env.state.dsp.global_extras.loudness.enabled);
    }

    // 2. Loudness on: actual transition -> flag present with the new value.
    {
        const env = makeContext({
            responses: [okResponse(serverExtras({ loudnessEnabled: true }))],
            stateDsp: { global_extras: serverExtras() },
        });
        env.elements.effectsLoudnessEnabled.checked = true;
        env.context._doSaveEffectsExtras('saving');
        await tick(); await tick();
        assert.equal(env.fetchCalls.length, 1);
        assert.equal(env.fetchCalls[0].body.loudnessEnabled, true);
        assert.ok(env.state.dsp.global_extras.loudness.enabled);
    }

    // 3. Loudness off: same for the disable direction.
    {
        const env = makeContext({
            responses: [okResponse(serverExtras({ loudnessEnabled: false }))],
            stateDsp: { global_extras: serverExtras({ loudnessEnabled: true }) },
        });
        env.elements.effectsLoudnessEnabled.checked = true; // rendered server state
        env.elements.effectsLoudnessEnabled.checked = false;
        env.context._doSaveEffectsExtras('saving');
        await tick(); await tick();
        assert.equal(env.fetchCalls[0].body.loudnessEnabled, false);
        assert.ok(!env.state.dsp.global_extras.loudness.enabled);
    }

    // 4. Combined change: loudness toggle plus bass edit in one save must not
    //    lose either part.
    {
        const env = makeContext({
            responses: [okResponse(serverExtras({ loudnessEnabled: true }))],
            stateDsp: { global_extras: serverExtras() },
        });
        env.elements.effectsLoudnessEnabled.checked = true;
        env.elements.effectsBassEnabled.checked = true;
        env.elements.effectsBassAmount.value = '35';
        env.context._doSaveEffectsExtras('saving');
        await tick(); await tick();
        assert.equal(env.fetchCalls.length, 1);
        assert.equal(env.fetchCalls[0].body.loudnessEnabled, true);
        assert.equal(env.fetchCalls[0].body.bassEnabled, true);
        assert.equal(env.fetchCalls[0].body.bassAmount, 35);
    }

    // 5. Rapid consecutive edits: the in-flight guard defers the second save
    //    and the resave rebuilds the body against the updated server state,
    //    so the loudness flag appears exactly once and nothing is lost.
    {
        let releaseFirst;
        const firstResponse = {
            ok: true,
            json: () => new Promise(resolve => {
                releaseFirst = () => resolve({
                    status: 'ok',
                    extras: serverExtras({ loudnessEnabled: true }),
                });
            }),
        };
        const env = makeContext({
            responses: [firstResponse, okResponse(serverExtras({ loudnessEnabled: true, limiterEnabled: true }))],
            stateDsp: { global_extras: serverExtras() },
        });
        env.elements.effectsLoudnessEnabled.checked = true;
        const first = env.context._doSaveEffectsExtras('saving');
        await tick();
        env.elements.effectsLimiterEnabled.checked = true;
        env.context._doSaveEffectsExtras('saving'); // in flight -> deferred
        await tick();
        assert.equal(env.fetchCalls.length, 1, 'second save must wait for the first');
        releaseFirst();
        await first;
        await tick(); await tick(); await tick();
        assert.equal(env.fetchCalls.length, 2, 'deferred save must be resaved');
        assert.equal(env.fetchCalls[0].body.loudnessEnabled, true, 'first save carries the transition');
        assert.ok(!('loudnessEnabled' in env.fetchCalls[1].body), 'resave after applied transition must not re-flag loudness');
        assert.equal(env.fetchCalls[1].body.limiterEnabled, true, 'deferred limiter change must not be lost');
        await tick();
        assert.equal(env.fetchCalls.length, 2, 'no extra save loop afterwards');
    }

    // 6. A failed loudness save keeps the transition flag on the retry.
    {
        const env = makeContext({
            responses: [
                { ok: false, json: async () => ({ detail: 'boom' }) },
                okResponse(serverExtras({ loudnessEnabled: true })),
            ],
            stateDsp: { global_extras: serverExtras() },
        });
        env.elements.effectsLoudnessEnabled.checked = true;
        env.context._doSaveEffectsExtras('saving');
        await tick(); await tick();
        assert.equal(env.fetchCalls.length, 1);
        assert.equal(env.fetchCalls[0].body.loudnessEnabled, true);
        assert.deepEqual(env.toasts, ['boom']);
        env.context._doSaveEffectsExtras('saving');
        await tick(); await tick();
        assert.equal(env.fetchCalls.length, 2);
        assert.equal(env.fetchCalls[1].body.loudnessEnabled, true, 'retry must still carry the transition');
        assert.ok(env.state.dsp.global_extras.loudness.enabled);
    }

    // Cached asset version must be bumped alongside the app.js change.
    assert.match(indexSource, /app\.js\?v=\d+\.\d+\.\d+/);

    console.log('effects extras save frontend tests: ok');
}

main().catch((error) => {
    console.error(error);
    process.exitCode = 1;
});
