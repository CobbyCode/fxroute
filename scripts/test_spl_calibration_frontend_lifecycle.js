#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only
const assert = require('assert/strict');

function response(data = {}, ok = true) {
    return { ok, json: async () => data };
}

function deferred() {
    let resolve;
    const promise = new Promise((done) => { resolve = done; });
    return { promise, resolve };
}

const tick = () => new Promise((resolve) => setImmediate(resolve));

function fixture(fetchOverride) {
    delete require.cache[require.resolve('../static/measurement_spl_calibration.js')];
    const spl = require('../static/measurement_spl_calibration.js');
    const button = { disabled: false, textContent: '' };
    const status = { textContent: '' };
    const action = { textContent: '' };
    let hidden = true;
    const panel = { classList: { add: () => { hidden = true; }, remove: () => { hidden = false; } } };
    const calls = [];
    spl.init({
        getElements: () => ({ splCalibrationNoise: button, splCalibrationStatus: status,
            splCalibrationAutoStatus: action, splCalibrationPanel: panel, splCalibrationMeasured: { value: '83' } }),
        fetch: async (url, options = {}) => {
            const body = options.body ? JSON.parse(options.body) : undefined;
            const enabled = body ? body.enabled : undefined;
            calls.push({ url, enabled, body });
            if (fetchOverride) {
                const result = fetchOverride(url, options, enabled);
                if (result !== undefined) return result;
            }
            return response(url.endsWith('/spl-calibration')
                ? { noise_active: false, operation_active: false, automatic: { available: false },
                    output_profile: { id: 'dev', label: 'Dev' } }
                : { status: enabled ? 'playing' : 'stopped' });
        },
        openModal: () => {}, closeModal: () => {},
        setTimeout: (callback, ms) => ms === 1000 ? callback() : setTimeout(callback, ms),
    });
    return { spl, button, status, action, calls, hidden: () => hidden };
}

async function testFailedStopKeepsRetryableState() {
    let fail = true;
    const f = fixture((_url, _options, enabled) => enabled === false && fail
        ? response({ detail: 'DSP restore unavailable' }, false) : undefined);
    await f.spl.openSplCalibration();
    await f.spl.toggleSplCalibrationNoise();
    await f.spl.toggleSplCalibrationNoise();
    assert.notEqual(f.button.textContent, 'Start noise', 'failed cleanup must not offer a new start');
    assert.match(f.status.textContent, /DSP restore unavailable/);
    fail = false;
    await f.spl.toggleSplCalibrationNoise();
    assert.equal(f.calls.at(-1).enabled, false, 'retry must stop, not start another session');
    assert.equal(f.button.textContent, 'Start noise');
    await f.spl.toggleSplCalibrationNoise();
    await f.spl.toggleSplCalibrationNoise();
    assert.equal(f.button.textContent, 'Start noise');
}

async function testFailedCloseRemainsVisible() {
    const f = fixture((_url, _options, enabled) => enabled === false
        ? response({ detail: 'Cleanup failed' }, false) : undefined);
    await f.spl.openSplCalibration();
    await f.spl.toggleSplCalibrationNoise();
    await f.spl.closeSplCalibration();
    assert.equal(f.hidden(), false, 'failed stop must not silently close the dialog');
    assert.match(f.status.textContent, /Cleanup failed/);
}

async function testIdleCloseStillRequestsIdempotentStop() {
    const f = fixture();
    await f.spl.openSplCalibration();
    await f.spl.closeSplCalibration();
    await f.spl.closeSplCalibration();
    assert.deepEqual(f.calls.filter((c) => c.enabled !== undefined).map((c) => c.enabled), [false, false]);
    assert.equal(f.hidden(), true);
    assert.equal(f.button.textContent, 'Start noise');
}

async function testCloseDrainsPendingStartAndStopsLateNoise() {
    const started = deferred();
    let active = false;
    const f = fixture((_url, _options, enabled) => {
        if (enabled === true) return started.promise.then(() => { active = true; return response({ status: 'playing' }); });
        if (enabled === false) { active = false; return response({ status: 'stopped' }); }
    });
    await f.spl.openSplCalibration();
    const start = f.spl.toggleSplCalibrationNoise();
    await tick();
    const close = f.spl.closeSplCalibration();
    await tick();
    assert.equal(f.hidden(), false, 'close must await the outstanding start');
    started.resolve();
    await Promise.all([start, close]);
    assert.equal(active, false, 'a start arriving after the first stop must also be stopped');
    assert.equal(f.hidden(), true);
    assert.equal(f.button.textContent, 'Start noise');
}

async function testConcurrentStopsShareCleanup() {
    const stopped = deferred();
    const f = fixture((_url, _options, enabled) => enabled === false ? stopped.promise : undefined);
    await f.spl.openSplCalibration();
    await f.spl.toggleSplCalibrationNoise();
    const one = f.spl.stopSplCalibrationOperation();
    const two = f.spl.stopSplCalibrationOperation();
    await tick();
    assert.equal(f.calls.filter((c) => c.enabled === false).length, 1);
    assert.equal(f.button.disabled, true);
    stopped.resolve(response({ status: 'stopped' }));
    await Promise.all([one, two]);
    assert.equal(f.button.disabled, false);
    assert.equal(f.button.textContent, 'Start noise');
}

async function testEndedOperationWithPendingRestoreOffersRestoreRetry() {
    const pending = { detail: 'SPL calibration ended without restoring its audio state (DSP restore unavailable). The output is muted for safety.', output_muted: true };
    let restored = false;
    let stops = 0;
    const f = fixture((url, _options, enabled) => {
        if (url.endsWith('/spl-calibration')) {
            return response({ noise_active: false, operation_active: false, restore_pending: restored ? null : pending,
                automatic: { available: false } });
        }
        if (enabled === false && ++stops <= 2) return response({ detail: pending.detail }, false);
        if (enabled === false) restored = true;
    });
    await f.spl.openSplCalibration();
    assert.equal(f.button.textContent, 'Retry restore', 'an ended operation with a pending restore offers its retry');
    assert.equal(f.action.textContent, 'Audio not restored. Output muted. Retry restore.');
    assert.match(f.status.textContent, /muted for safety/);
    await f.spl.toggleSplCalibrationNoise();
    assert.equal(f.calls.at(-2).enabled, false, 'the retry must stop/restore, never start');
    assert.equal(f.button.textContent, 'Retry restore');
    assert.doesNotMatch(f.action.textContent, /Cleanup pending/, 'the operation itself has ended');
    assert.match(f.status.textContent, /DSP restore unavailable/);
    await f.spl.toggleSplCalibrationNoise();
    assert.equal(f.button.textContent, 'Retry restore');
    await f.spl.toggleSplCalibrationNoise();
    assert.equal(f.calls.filter((c) => c.enabled === true).length, 0);
    assert.equal(f.button.textContent, 'Start noise');
    assert.equal(f.status.textContent, 'Audio restored.');
    assert.equal(f.action.textContent, 'Manual SPL measurement');
}

async function testFailedStopReportsAbandonedRestoreInsteadOfCleanup() {
    let abandoned = false;
    const f = fixture((url, _options, enabled) => {
        if (url.endsWith('/spl-calibration')) {
            return response(abandoned
                ? { noise_active: false, operation_active: false, automatic: { available: false },
                    restore_pending: { detail: 'Restore failed. The output is muted for safety.', output_muted: true } }
                : { noise_active: false, operation_active: false, automatic: { available: false } });
        }
        if (enabled === false) {
            abandoned = true;
            return response({ detail: 'SPL calibration ended without restoring its audio state' }, false);
        }
    });
    await f.spl.openSplCalibration();
    await f.spl.toggleSplCalibrationNoise();
    assert.equal(f.button.textContent, 'Stop noise');
    await f.spl.toggleSplCalibrationNoise();
    assert.equal(f.button.textContent, 'Retry restore');
    assert.equal(f.action.textContent, 'Audio not restored. Output muted. Retry restore.');
    assert.match(f.status.textContent, /without restoring its audio state/);
}

async function testSilentBackendOwnerOffersCleanupNotStart() {
    const f = fixture((url) => url.endsWith('/spl-calibration') ? response({
        noise_active: false, operation_active: true, automatic: { available: false },
    }) : undefined);
    await f.spl.openSplCalibration();
    await f.spl.toggleSplCalibrationNoise();
    assert.equal(f.calls.at(-1).enabled, false);
}

async function testCloseDuringCountdownPreventsStart() {
    const countdown = [];
    const f = fixture();
    f.spl.init({ setTimeout: (callback, ms) => ms === 1000 ? countdown.push(callback) : setTimeout(callback, ms) });
    await f.spl.openSplCalibration();
    const start = f.spl.toggleSplCalibrationNoise();
    await f.spl.closeSplCalibration();
    countdown.shift()();
    await start;
    assert.equal(f.calls.filter((c) => c.enabled === true).length, 0);
    assert.equal(f.hidden(), true);
}

async function testLateOpenResponseCannotOverwriteStop() {
    const loaded = deferred();
    const f = fixture((url) => url.endsWith('/spl-calibration') ? loaded.promise : undefined);
    const open = f.spl.openSplCalibration();
    await f.spl.closeSplCalibration();
    loaded.resolve(response({ noise_active: true, operation_active: true, automatic: { available: false } }));
    await open;
    assert.equal(f.button.textContent, 'Start noise');
    assert.equal(f.hidden(), true);
}

async function testLostStartResponseKeepsServerOwnerRetryable() {
    let loads = 0;
    const f = fixture((url, _options, enabled) => {
        if (url.endsWith('/spl-calibration')) return response({
            noise_active: ++loads > 1, operation_active: loads > 1, automatic: { available: false },
        });
        if (enabled === true) return Promise.reject(new Error('Connection lost'));
    });
    await f.spl.openSplCalibration();
    await f.spl.toggleSplCalibrationNoise();
    assert.notEqual(f.button.textContent, 'Start noise');
    await f.spl.toggleSplCalibrationNoise();
    assert.equal(f.calls.at(-1).enabled, false);
    assert.equal(f.button.textContent, 'Start noise');
}

async function testApplyDrainsStartBeforeSaving() {
    const started = deferred();
    const f = fixture((url, _options, enabled) => {
        if (enabled === true) return started.promise;
        if (url.endsWith('/apply')) return response({ required_adjustment_db: 0, calibrated: true });
    });
    await f.spl.openSplCalibration();
    const start = f.spl.toggleSplCalibrationNoise();
    await tick();
    const save = f.spl.saveSplCalibration();
    await tick();
    assert.equal(f.calls.some((c) => c.url.endsWith('/apply')), false, 'apply must await start cleanup');
    started.resolve(response({ status: 'playing' }));
    await Promise.all([start, save]);
    assert.equal(f.button.textContent, 'Start noise');
    assert.match(f.status.textContent, /Saved for Dev:/);
}

async function testStalledStartCancellationTimesOutAndCanRetry() {
    const deadlines = new Map();
    let timerId = 0;
    const f = fixture((_url, options, enabled) => enabled === true ? new Promise((_resolve, reject) => {
        options.signal?.addEventListener('abort', () => reject(new Error('Start aborted')));
    }) : undefined);
    f.spl.init({
        setTimeout: (callback, ms) => {
            if (ms === 1000) { callback(); return 0; }
            deadlines.set(++timerId, callback); return timerId;
        },
        clearTimeout: (id) => deadlines.delete(id),
    });
    await f.spl.openSplCalibration();
    const start = f.spl.toggleSplCalibrationNoise();
    await tick();
    const close = f.spl.closeSplCalibration();
    await tick();
    assert.equal(deadlines.size, 1, 'cancel must have a bounded start-response drain');
    [...deadlines.values()][0]();
    await Promise.all([start, close]);
    assert.equal(f.hidden(), false);
    assert.equal(f.button.disabled, false);
    assert.notEqual(f.button.textContent, 'Start noise');
    await f.spl.closeSplCalibration();
    assert.equal(f.hidden(), true);
    assert.equal(f.button.textContent, 'Start noise');
}

async function testApplyPreventsNewStartUntilResponseCompletes() {
    const applied = deferred();
    const f = fixture((url) => url.endsWith('/apply') ? applied.promise : undefined);
    await f.spl.openSplCalibration();
    const save = f.spl.saveSplCalibration();
    await tick();
    assert.equal(f.button.disabled, true, 'apply owns the lifecycle until its response completes');
    await f.spl.toggleSplCalibrationNoise();
    assert.equal(f.calls.filter((c) => c.enabled === true).length, 0);
    applied.resolve(response({ required_adjustment_db: 0, calibrated: true }));
    await save;
    assert.equal(f.button.disabled, false);
    await f.spl.toggleSplCalibrationNoise();
    assert.equal(f.button.textContent, 'Stop noise');
}

async function testStalledStopTimesOutAndCanRetry() {
    const deadlines = new Map();
    let timerId = 0;
    let stall = true;
    const f = fixture((_url, options, enabled) => enabled === false && stall ? new Promise((_resolve, reject) => {
        options.signal?.addEventListener('abort', () => reject(new Error('Stop aborted')));
    }) : undefined);
    f.spl.init({
        setTimeout: (callback, ms) => {
            if (ms === 1000) { callback(); return 0; }
            deadlines.set(++timerId, callback); return timerId;
        },
        clearTimeout: (id) => deadlines.delete(id),
    });
    await f.spl.openSplCalibration();
    await f.spl.toggleSplCalibrationNoise();
    const close = f.spl.closeSplCalibration();
    await tick();
    assert.equal(deadlines.size, 1, 'the stop fetch itself must have a deadline');
    [...deadlines.values()][0]();
    await close;
    assert.equal(f.hidden(), false);
    assert.equal(f.button.disabled, false);
    assert.notEqual(f.button.textContent, 'Start noise');
    stall = false;
    await f.spl.closeSplCalibration();
    assert.equal(f.hidden(), true);
}

async function testCloseAbortsStalledApplyAndReopenCanStart() {
    const f = fixture((url, options) => url.endsWith('/apply') ? new Promise((_resolve, reject) => {
        options.signal?.addEventListener('abort', () => reject(new Error('Apply aborted')));
    }) : undefined);
    await f.spl.openSplCalibration();
    const save = f.spl.saveSplCalibration();
    await tick();
    await f.spl.closeSplCalibration();
    await f.spl.openSplCalibration();
    assert.equal(f.button.disabled, false);
    await f.spl.toggleSplCalibrationNoise();
    assert.equal(f.button.textContent, 'Stop noise');
    await save;
}

async function testApplyTimeoutReleasesControlsAndReportsFailure() {
    const deadlines = new Map();
    let timerId = 0;
    const f = fixture((url, options) => url.endsWith('/apply') ? new Promise((_resolve, reject) => {
        options.signal.addEventListener('abort', () => reject(new Error('Apply aborted')));
    }) : undefined);
    f.spl.init({
        setTimeout: (callback, ms) => {
            if (ms === 1000) { callback(); return 0; }
            deadlines.set(++timerId, callback); return timerId;
        },
        clearTimeout: (id) => deadlines.delete(id),
    });
    await f.spl.openSplCalibration();
    const save = f.spl.saveSplCalibration();
    await tick();
    assert.equal(deadlines.size, 1);
    [...deadlines.values()][0]();
    await save;
    assert.equal(f.button.disabled, false);
    assert.match(f.status.textContent, /timed out/);
}

async function testApplySendsTheOutputTheReadingBelongsTo() {
    const f = fixture();
    await f.spl.openSplCalibration();
    await f.spl.saveSplCalibration();
    const apply = f.calls.find((c) => c.url.endsWith('/apply'));
    assert.deepEqual(apply.body, { measured_spl_db: 83, output_profile_id: 'dev' });
    assert.match(f.status.textContent, /^Saved for Dev: 83\.0 dB SPL measured/);
}

async function testTheNoiseStartOutputWinsOverThePanelOpenOutput() {
    const f = fixture((url, _options, enabled) => enabled === true
        ? response({ status: 'playing', output_profile: { id: 'second', label: 'Second' } }) : undefined);
    await f.spl.openSplCalibration();
    await f.spl.toggleSplCalibrationNoise();
    await f.spl.saveSplCalibration();
    assert.equal(f.calls.find((c) => c.url.endsWith('/apply')).body.output_profile_id, 'second');
}

async function testAnAutomaticResultCarriesItsOutputToTheSave() {
    const f = fixture((url) => {
        if (url.endsWith('/spl-calibration')) return response({ noise_active: false, operation_active: false,
            automatic: { available: true, microphone_model: 'UMIK-1' }, output_profile: { id: 'dev', label: 'Dev' } });
        if (url.endsWith('/automatic')) return response({ measured_spl_db: 82.5, required_adjustment_db: 0.5,
            microphone_model: 'UMIK-1', output_profile: { id: 'measured-on', label: 'Measured On' } });
    });
    await f.spl.openSplCalibration();
    await f.spl.toggleSplCalibrationNoise();
    assert.match(f.status.textContent, /Save \/ Apply to use it for Measured On\.$/);
    await f.spl.saveSplCalibration();
    assert.equal(f.calls.find((c) => c.url.endsWith('/apply')).body.output_profile_id, 'measured-on');
}

async function testNoSaveWithoutAKnownOutput() {
    const f = fixture((url) => url.endsWith('/spl-calibration')
        ? response({ noise_active: false, operation_active: false, automatic: { available: false }, output_profile: { id: '', label: '' } })
        : undefined);
    await f.spl.openSplCalibration();
    await f.spl.saveSplCalibration();
    assert.equal(f.calls.some((c) => c.url.endsWith('/apply')), false);
    assert.match(f.status.textContent, /output could not be identified/);
    assert.equal(f.button.disabled, false);
}

async function testAnOutputChangeIsReportedAndNothingIsClaimedSaved() {
    const f = fixture((url) => url.endsWith('/apply')
        ? response({ detail: 'The output changed since the measurement. Measure again for the current output.' }, false) : undefined);
    await f.spl.openSplCalibration();
    await f.spl.saveSplCalibration();
    assert.equal(f.status.textContent, 'The output changed since the measurement. Measure again for the current output.');
    assert.equal(f.button.disabled, false);
}

async function testReopeningForgetsTheEarlierOutput() {
    let profile = { id: 'first', label: 'First' };
    const f = fixture((url) => url.endsWith('/spl-calibration')
        ? response({ noise_active: false, operation_active: false, automatic: { available: false }, output_profile: profile })
        : undefined);
    await f.spl.openSplCalibration();
    profile = { id: 'second', label: 'Second' };
    await f.spl.closeSplCalibration();
    await f.spl.openSplCalibration();
    await f.spl.saveSplCalibration();
    assert.equal(f.calls.find((c) => c.url.endsWith('/apply')).body.output_profile_id, 'second');
}

(async () => {
    const tests = [testFailedStopKeepsRetryableState, testFailedCloseRemainsVisible, testIdleCloseStillRequestsIdempotentStop,
        testEndedOperationWithPendingRestoreOffersRestoreRetry, testFailedStopReportsAbandonedRestoreInsteadOfCleanup,
        testCloseDrainsPendingStartAndStopsLateNoise, testConcurrentStopsShareCleanup,
        testSilentBackendOwnerOffersCleanupNotStart, testCloseDuringCountdownPreventsStart,
        testLateOpenResponseCannotOverwriteStop, testLostStartResponseKeepsServerOwnerRetryable,
        testApplyDrainsStartBeforeSaving, testStalledStartCancellationTimesOutAndCanRetry,
        testApplyPreventsNewStartUntilResponseCompletes, testStalledStopTimesOutAndCanRetry,
        testCloseAbortsStalledApplyAndReopenCanStart, testApplyTimeoutReleasesControlsAndReportsFailure,
        testApplySendsTheOutputTheReadingBelongsTo, testTheNoiseStartOutputWinsOverThePanelOpenOutput,
        testAnAutomaticResultCarriesItsOutputToTheSave, testNoSaveWithoutAKnownOutput,
        testAnOutputChangeIsReportedAndNothingIsClaimedSaved, testReopeningForgetsTheEarlierOutput];
    let failed = 0;
    for (const test of tests) {
        try { await test(); console.log(`${test.name}: ok`); }
        catch (error) { failed++; console.error(`${test.name}: ${error.stack}`); }
    }
    assert.equal(failed, 0, `${failed} frontend lifecycle regressions failed`);
})().catch((error) => { console.error(error); process.exitCode = 1; });
