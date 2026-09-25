#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only

const assert = require('assert/strict');
const fs = require('fs');
const path = require('path');

const backendSource = fs.readFileSync(path.join(__dirname, '..', 'measurement', 'spl_calibration.py'), 'utf8');
const appSource = fs.readFileSync(path.join(__dirname, '..', 'static', 'app.js'), 'utf8');
const splSource = fs.readFileSync(path.join(__dirname, '..', 'static', 'measurement_spl_calibration.js'), 'utf8');

const spl = require('../static/measurement_spl_calibration.js');

const button = { disabled: false, textContent: 'Start noise' };
const status = { textContent: '' };
const measured = { value: '' };
const autoStatus = { textContent: '' };
const panel = { classList: { remove: () => {}, add: () => {} } };
const countdownLabels = [];
const fetchCalls = [];
let resolveAutomatic;

spl.init({
    getElements: () => ({
        splCalibrationNoise: button,
        splCalibrationStatus: status,
        splCalibrationMeasured: measured,
        splCalibrationAutoStatus: autoStatus,
        splCalibrationPanel: panel,
    }),
    fetch: (url, options = {}) => {
        fetchCalls.push({ url, options, countdownLabels: [...countdownLabels] });
        if (url.endsWith('/automatic')) {
            return new Promise((resolve) => { resolveAutomatic = resolve; });
        }
        if (url.endsWith('/spl-calibration')) {
            return Promise.resolve({ ok: true, json: async () => ({ noise_active: false, automatic: { available: true, microphone_model: 'UMIK-1' } }) });
        }
        return Promise.resolve({ ok: true, json: async () => ({ status: 'stopped' }) });
    },
    fetchEffects: async () => {},
    openModal: () => {},
    closeModal: () => {},
    setTimeout: (callback) => {
        countdownLabels.push(button.textContent);
        callback();
    },
});

for (const model of ['UMIK-1', 'UMIK-2', 'UMM-6']) {
    assert.equal(
        spl.splCalibrationModeLabel({ automatic: { available: true, microphone_model: model } }),
        `Automatic SPL measurement: ${model} detected`,
    );
}
assert.equal(spl.splCalibrationModeLabel({ automatic: { available: false } }), 'Manual SPL measurement');
// The SPL noise button is wired straight to the SPL module; that wiring
// lives with setupEffectsActions in output_effects_ui.js (no app.js wrapper).
assert.doesNotMatch(appSource, /\n(?:async function|function) toggleSplCalibrationNoise\(/);
const effectsSource = fs.readFileSync(path.join(__dirname, '..', 'static', 'output_effects_ui.js'), 'utf8');
assert.match(effectsSource, /FXRouteMeasurementSplCalibration\.toggleSplCalibrationNoise/);
assert.match(splSource, /function toggleSplCalibrationNoise\(\) \{/);

(async () => {
    await spl.openSplCalibration();
    const automaticRequest = spl.toggleSplCalibrationNoise();
    await new Promise((resolve) => setImmediate(resolve));

    assert.deepEqual(countdownLabels, [
        'Starting noise: 3',
        'Starting noise: 2',
        'Starting noise: 1',
    ]);
    assert.equal(fetchCalls.filter((call) => call.url.endsWith('/automatic')).length, 1, 'noise must not be requested during countdown');
    const autoCall = fetchCalls.find((call) => call.url.endsWith('/automatic'));
    assert.ok(autoCall);
    assert.equal(button.textContent, 'Cancel measurement');
    assert.equal(button.disabled, false);
    // The running action shows next to the noise button; the bottom status
    // line waits for the outcome.
    assert.equal(autoStatus.textContent, 'Measuring SPL…');
    assert.equal(status.textContent, '');

    await spl.toggleSplCalibrationNoise();
    const noiseCalls = fetchCalls.filter((call) => call.url.endsWith('/noise'));
    assert.equal(noiseCalls.length, 1);
    assert.equal(JSON.parse(noiseCalls[0].options.body).enabled, false);
    assert.equal(button.textContent, 'Start noise');
    assert.equal(button.disabled, false);
    assert.equal(status.textContent, 'Automatic SPL measurement cancelled.');
    assert.equal(autoStatus.textContent, 'Automatic SPL measurement: UMIK-1 detected');

    resolveAutomatic({
        ok: false,
        json: async () => ({ detail: 'SPL calibration was stopped' }),
    });
    await automaticRequest;
    assert.equal(button.textContent, 'Start noise', 'late automatic response must not leave idle state');

    assert.doesNotMatch(backendSource, /uniquely identified supported UMIK USB capture device/);
    assert.match(backendSource, /loudnorm=I=-23:TP=-3:LRA=7,afade=t=in:st=0:d=1,/);
    assert.match(backendSource, /fxroute-spl-calibration-pink-noise-v2\.wav/);
    console.log('SPL calibration frontend UX: ok');
})().catch((error) => {
    console.error(error);
    process.exitCode = 1;
});
