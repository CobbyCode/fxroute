#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only

const assert = require('assert/strict');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const repoRoot = path.resolve(__dirname, '..');
const appSource = fs.readFileSync(path.join(repoRoot, 'static', 'app.js'), 'utf8');
const htmlSource = fs.readFileSync(path.join(repoRoot, 'static', 'index.html'), 'utf8');
const plain = (value) => JSON.parse(JSON.stringify(value));

function extractFunction(name) {
    const marker = new RegExp(`(?:async\\s+)?function\\s+${name}\\s*\\(`);
    const match = marker.exec(appSource);
    assert.ok(match, `missing function ${name}`);
    const start = match.index;
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
        if (char === "'" || char === '"' || char === '`') {
            quote = char;
        } else if (char === '{') {
            depth += 1;
        } else if (char === '}' && --depth === 0) {
            return appSource.slice(start, index + 1);
        }
    }
    throw new Error(`unterminated function ${name}`);
}

async function main() {
    const requests = [];
    const state = { measurement: {}, easyeffects: {} };
    const context = {
        state,
        measurementPeqPalette: ['#1', '#2', '#3', '#4'],
        MeasurementDsp: {
            clampMeasurementPeqFrequency: (value) => Math.max(20, Math.min(20000, Number(value))),
            clampMeasurementPeqGain: (value) => Math.max(-24, Math.min(24, Number(value))),
            clampMeasurementPeqQ: (value) => Math.max(0.1, Math.min(20, Number(value))),
        },
        elements: {},
        showToast: () => {},
        showMeasurementPeqTakeFeedback: () => {},
        renderMeasurementPanel: () => {},
        validatePeqBands: () => '',
        normalizePeqEqMode: (value) => value,
        collectEffectsExtras: () => ({}),
        fetchEffects: async () => {},
        fetch: async (url, options) => {
            requests.push({ url, options });
            return { ok: true, json: async () => ({ preset: { name: 'Twelve filters' } }) };
        },
        console,
        Date,
        Math,
    };
    vm.createContext(context);
    const functions = [
        'getDefaultMeasurementPeqFilter',
        'getDefaultMeasurementPeqState',
        'ensureMeasurementPeqState',
        'ensureCustomHouseCurveState',
        'ensureMeasurementConvolverState',
        'getMeasurementActiveEditor',
        'setMeasurementActiveEditor',
        'clampMeasurementPeqFrequency',
        'clampMeasurementPeqGain',
        'clampMeasurementPeqQ',
        'addMeasurementPeqFilter',
        'measurementPeqFilterToBand',
        'getMeasurementPeqNameSuffix',
        'getMeasurementPeqDraftMode',
        'getMeasurementPeqPresetName',
        'takeMeasurementPeqToPreset',
        'createMeasurementPeqPresetFromDraft',
    ].map(extractFunction).join('\n');
    vm.runInContext(`let peqCreateInFlight = false;\n${functions}`, context);

    const inputs = Array.from({ length: 12 }, (_, index) => ({
        type: index % 2 ? 'bell' : 'notch',
        freqHz: 80 + (index * 137),
        gainDb: -7 + index,
        q: 0.5 + (index * 0.25),
    }));
    inputs.forEach((filter) => assert.ok(context.addMeasurementPeqFilter(filter)));
    assert.equal(state.measurement.peqAssistant.filters.length, 12, 'all twelve temporary filter slots must be usable');
    assert.equal(context.addMeasurementPeqFilter({ freqHz: 9999 }), null, 'a thirteenth filter must still be rejected');

    context.takeMeasurementPeqToPreset('both');
    const expectedBands = inputs.map((filter) => ({
        filterType: filter.type,
        frequencyHz: filter.freqHz,
        gainDb: filter.gainDb,
        q: filter.q,
        delayMs: 0,
    }));
    assert.deepEqual(plain(state.measurement.peqAssistant.draft.leftBands), expectedBands);
    assert.deepEqual(plain(state.measurement.peqAssistant.draft.rightBands), expectedBands);

    await context.createMeasurementPeqPresetFromDraft();
    assert.equal(requests.length, 1, 'preset creation must make one request');
    const payload = JSON.parse(requests[0].options.body);
    assert.deepEqual(payload.peq.params.leftBands, expectedBands, 'left preset bands must be complete and ordered');
    assert.deepEqual(payload.peq.params.rightBands, expectedBands, 'right preset bands must be complete and ordered');

    assert.match(appSource, /peq\.filters\.length >= 12/, 'PEQ assistant guard must enforce the twelve-filter limit');
    assert.match(appSource, /supports up to 12 filters/, 'limit toast must describe twelve filters');
    assert.match(appSource, /Array\.from\(\{ length: 12 \}/, 'PEQ assistant must render twelve slots');
    assert.match(appSource, /const filter = peq\.filters\[index\] \|\| null/, 'unpopulated slots, including F9-F12, must remain unset');
    assert.match(appSource, /F1-F12[^<']*up to 12 temporary filters/, 'empty-state help must describe F1-F12');
    assert.match(appSource, /\$\{peq\.filters\.length\}\/12 assistant filters/, 'counter must use the twelve-filter limit');
    assert.match(htmlSource, /up to 12 temporary filters/, 'panel help must describe the twelve-filter limit');

    console.log('ok PEQ assistant: twelve slots accepted, thirteenth rejected; F1-F12 markers updated');
    console.log('ok PEQ preset: twelve ordered bands transferred completely to L/R request payload');
}

main().catch((error) => {
    console.error(error);
    process.exit(1);
});
