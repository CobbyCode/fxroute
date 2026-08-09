#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only

const assert = require('assert/strict');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const source = fs.readFileSync(path.join(__dirname, '..', 'static', 'app.js'), 'utf8');
const backendSource = fs.readFileSync(path.join(__dirname, '..', 'spl_calibration.py'), 'utf8');

function extractFunction(name) {
    const match = new RegExp(`(?:async\\s+)?function\\s+${name}\\s*\\(`).exec(source);
    assert.ok(match, `missing ${name}`);
    const braceStart = source.indexOf('{', match.index + match[0].length);
    let depth = 0;
    let quote = '';
    let escaped = false;
    for (let index = braceStart; index < source.length; index += 1) {
        const char = source[index];
        if (quote) {
            if (escaped) escaped = false;
            else if (char === '\\') escaped = true;
            else if (char === quote) quote = '';
            continue;
        }
        if (`'"\``.includes(char)) quote = char;
        else if (char === '{') depth += 1;
        else if (char === '}' && --depth === 0) return source.slice(match.index, index + 1);
    }
    throw new Error(`unterminated ${name}`);
}

const button = { disabled: false, textContent: 'Start noise' };
const status = { textContent: '' };
const measured = { value: '' };
const countdownLabels = [];
const fetchCalls = [];
let resolveAutomatic;

const context = vm.createContext({
    console,
    elements: {
        splCalibrationNoise: button,
        splCalibrationStatus: status,
        splCalibrationMeasured: measured,
    },
    setTimeout(callback) {
        countdownLabels.push(button.textContent);
        callback();
    },
    fetch(url, options = {}) {
        fetchCalls.push({ url, options, countdownLabels: [...countdownLabels] });
        if (url.endsWith('/automatic')) {
            return new Promise((resolve) => { resolveAutomatic = resolve; });
        }
        return Promise.resolve({ ok: true, json: async () => ({ status: 'stopped' }) });
    },
});

vm.runInContext(`
    let splCalibrationNoiseActive = false;
    let splCalibrationAutomaticAvailable = true;
    let splCalibrationAutomaticRunning = false;
    let splCalibrationOperationGeneration = 0;
    ${extractFunction('splCalibrationModeLabel')}
    ${extractFunction('resetSplCalibrationNoiseButton')}
    ${extractFunction('runSplCalibrationNoiseCountdown')}
    ${extractFunction('stopSplCalibrationOperation')}
    ${extractFunction('toggleSplCalibrationNoise')}
    this.modeLabel = splCalibrationModeLabel;
    this.toggleNoise = toggleSplCalibrationNoise;
`, context);

for (const model of ['UMIK-1', 'UMIK-2', 'UMM-6']) {
    assert.equal(
        context.modeLabel({ automatic: { available: true, microphone_model: model } }),
        `Automatic SPL measurement — ${model} detected`,
    );
}
assert.equal(context.modeLabel({ automatic: { available: false } }), 'Manual SPL measurement');

(async () => {
    const automaticRequest = context.toggleNoise();
    await new Promise((resolve) => setImmediate(resolve));

    assert.deepEqual(countdownLabels, [
        'Starting noise: 3',
        'Starting noise: 2',
        'Starting noise: 1',
    ]);
    assert.equal(fetchCalls.length, 1, 'noise must not be requested during countdown');
    assert.match(fetchCalls[0].url, /\/automatic$/);
    assert.equal(button.textContent, 'Cancel measurement');
    assert.equal(button.disabled, false);

    await context.toggleNoise();
    assert.equal(fetchCalls.length, 2);
    assert.match(fetchCalls[1].url, /\/noise$/);
    assert.equal(JSON.parse(fetchCalls[1].options.body).enabled, false);
    assert.equal(button.textContent, 'Start noise');
    assert.equal(button.disabled, false);
    assert.equal(status.textContent, 'Automatic SPL measurement cancelled.');

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
