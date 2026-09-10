#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only
//
// Convolver preset-name edit regression: the visible Preset Name field is
// authoritative at Create time. Whatever stands in the field is saved;
// an untouched field still holds the staged auto name, preserving the
// default flow. Guards the resolveMeasurementConvolverItemName helper
// against field-vs-draft divergence (render between last keystroke and
// click, in-flight draft reset, programmatic clicks).

const assert = require('assert/strict');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const MeasurementUI = require('../static/measurement_ui.js');

const source = fs.readFileSync(path.join(__dirname, '..', 'static', 'app.js'), 'utf8');

function extractFunction(name) {
    const match = new RegExp(`(?:async\\s+)?function\\s+${name}\\s*\\(`).exec(source);
    assert.ok(match, `missing ${name}`);
    let parenDepth = 1;
    let brace = -1;
    for (let index = match.index + match[0].length; index < source.length; index += 1) {
        if (source[index] === '(') parenDepth += 1;
        if (source[index] === ')') parenDepth -= 1;
        if (parenDepth === 0) { brace = source.indexOf('{', index); break; }
    }
    assert.notEqual(brace, -1, `missing body ${name}`);
    let depth = 0, quote = '', escaped = false;
    for (let index = brace; index < source.length; index += 1) {
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

function makeContext() {
    const state = {
        measurement: {
            measurementSampleRate: '48000',
            convolverAssistant: {
                targetCurve: 'neutral',
                rangeStartHz: 20,
                rangeEndHz: 250,
                phaseMode: 'minimum',
                quality: 'minimum_8192',
                draft: { left: {}, right: {}, presetName: '', nameTouched: false, notice: '' },
            },
        },
    };
    const context = {
        MeasurementUI,
        measurementConvolverCurves: MeasurementUI.measurementConvolverCurves,
        measurementConvolverPhaseModes: MeasurementUI.measurementConvolverPhaseModes,
        measurementConvolverTapOptions: MeasurementUI.measurementConvolverTapOptions,
        state,
        formatMeasurementConvolverGain: (...args) => MeasurementUI.formatMeasurementConvolverGain(...args),
        getMeasurementConvolverNameSuffix: (...args) => MeasurementUI.getMeasurementConvolverNameSuffix(...args),
        getMeasurementConvolverPhaseTag: (...args) => MeasurementUI.getMeasurementConvolverPhaseTag(...args),
        getMeasurementConvolverCurveOptions: MeasurementUI.getMeasurementConvolverCurveOptions
            ? (...args) => MeasurementUI.getMeasurementConvolverCurveOptions(...args)
            : () => [{ key: 'neutral', label: 'Neutral', shortLabel: 'Neutral' }],
        getMeasurementConvolverCurve: (key) => {
            const options = (MeasurementUI.getMeasurementConvolverCurveOptions
                ? MeasurementUI.getMeasurementConvolverCurveOptions()
                : [{ key: 'neutral', label: 'Neutral', shortLabel: 'Neutral' }]);
            return options.find((curve) => curve.key === key) || { key: 'neutral', label: 'Neutral', shortLabel: 'Neutral' };
        },
        clampMeasurementConvolverFrequency: MeasurementUI.clampMeasurementConvolverFrequency
            || ((value, fallback = 20) => {
                const numeric = Number(value);
                return Math.min(20000, Math.max(20, Number.isFinite(numeric) ? numeric : fallback));
            }),
        getMeasurementConvolverTypeKeys: MeasurementUI.getMeasurementConvolverTypeKeys,
        getMeasurementConvolverPhaseModeForType: MeasurementUI.getMeasurementConvolverPhaseModeForType,
        getMeasurementConvolverFirLengthForType: MeasurementUI.getMeasurementConvolverFirLengthForType,
    };
    vm.createContext(context);
    vm.runInContext([
        'getDefaultMeasurementConvolverState',
        'ensureMeasurementConvolverState',
        'getMeasurementConvolverItemName',
        'resolveMeasurementConvolverItemName',
    ].map(extractFunction).join('\n'), context);
    return context;
}

const AUTO = 'Conv LR Min Neutral 20-250Hz -4dB 043055';

// 1. Untouched field holding the staged auto name: default flow preserved.
{
    const ctx = makeContext();
    const conv = { draft: { presetName: AUTO, nameTouched: false } };
    assert.equal(ctx.resolveMeasurementConvolverItemName(conv, AUTO, 'both', -4), AUTO);
}

// 2. Edited field wins over a stale auto draft: the reported edit path.
{
    const ctx = makeContext();
    const conv = { draft: { presetName: AUTO, nameTouched: true } };
    assert.equal(ctx.resolveMeasurementConvolverItemName(conv, 'My Custom Name', 'both', -4), 'My Custom Name');
}

// 3. Field wins even when it only differs by surrounding whitespace handling.
{
    const ctx = makeContext();
    const conv = { draft: { presetName: AUTO, nameTouched: true } };
    assert.equal(ctx.resolveMeasurementConvolverItemName(conv, '  Spaced Name  ', 'both', -4), 'Spaced Name');
}

// 4. Empty field falls back to the draft (e.g. element missing at click time).
{
    const ctx = makeContext();
    const conv = { draft: { presetName: AUTO, nameTouched: false } };
    assert.equal(ctx.resolveMeasurementConvolverItemName(conv, '', 'both', -4), AUTO);
    assert.equal(ctx.resolveMeasurementConvolverItemName(conv, undefined, 'both', -4), AUTO);
    assert.equal(ctx.resolveMeasurementConvolverItemName(conv, null, 'left', -2), AUTO);
}

// 5. Whitespace-only field falls back to the draft.
{
    const ctx = makeContext();
    const conv = { draft: { presetName: AUTO, nameTouched: true } };
    assert.equal(ctx.resolveMeasurementConvolverItemName(conv, '   ', 'both', -4), AUTO);
}

// 6. Empty field and empty draft generate a fresh unique auto name.
{
    const ctx = makeContext();
    const conv = { draft: { presetName: '', nameTouched: false } };
    const generated = ctx.resolveMeasurementConvolverItemName(conv, '', 'both', -4);
    assert.match(generated, /^Conv LR \S+ Neutral 20-250Hz .*dB \d{6}$/);
}

// 7. Missing draft object does not crash; falls back to generated name.
{
    const ctx = makeContext();
    const generated = ctx.resolveMeasurementConvolverItemName({}, '', 'left', -2);
    assert.match(generated, /^Conv L \S+ Neutral 20-250Hz .*dB \d{6}$/);
    const missing = ctx.resolveMeasurementConvolverItemName(null, '', 'right', -3);
    assert.match(missing, /^Conv R \S+ Neutral 20-250Hz .*dB \d{6}$/);
}

console.log('convolver preset name edit tests: ok');

// Source contract: like the PEQ field, the convolver name input must be
// editable whenever it shows a name (staged auto name or pre-take
// preview) — the disabled gate may only cover phase mismatch and the
// in-flight creation, never the missing draft. Typed text still marks
// the draft touched so Take keeps it.
{
    const renderSection = extractFunction('renderMeasurementPanelConvolverSection');
    assert.match(
        renderSection,
        /measurementConvolverPresetName\.disabled\s*=\s*!!draftPhaseMismatch\s*\|\|\s*isCreatingConvolverPreset/,
    );
    assert.doesNotMatch(
        renderSection,
        /measurementConvolverPresetName\.disabled\s*=\s*!hasConvolverDraft/,
    );
    const takeFn = extractFunction('takeMeasurementConvolverToDraft');
    assert.match(takeFn, /if\s*\(!conv\.draft\.nameTouched\)\s*conv\.draft\.presetName\s*=/);
    console.log('convolver preset name field-availability contract: ok');
}
