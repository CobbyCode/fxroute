#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only
//
// PEQ preset-name edit regression (mirrors the convolver contract): the
// field shows the stable auto suggestion even before Take, stays editable
// from the moment a name stands in it, a typed name wins over Take/Apply,
// and without input the suggested auto name is kept and used at creation.

const assert = require('assert/strict');
const fs = require('fs');
const path = require('path');
require('../static/measurement_dsp.js');
require('../static/measurement_ui.js');
const peqEditor = require('../static/measurement_peq_editor.js');

const source = fs.readFileSync(path.join(__dirname, '..', 'static', 'app.js'), 'utf8');
const editorsSource = fs.readFileSync(path.join(__dirname, '..', 'static', 'measurement_editors_ui.js'), 'utf8');

function extractFunction(name, from = source) {
    const match = new RegExp(`(?:async\\s+)?function\\s+${name}\\s*\\(`).exec(from);
    assert.ok(match, `missing ${name}`);
    let parenDepth = 1;
    let brace = -1;
    for (let index = match.index + match[0].length; index < from.length; index += 1) {
        if (from[index] === '(') parenDepth += 1;
        if (from[index] === ')') parenDepth -= 1;
        if (parenDepth === 0) { brace = from.indexOf('{', index); break; }
    }
    assert.notEqual(brace, -1, `missing body ${name}`);
    let depth = 0, quote = '', escaped = false;
    for (let index = brace; index < from.length; index += 1) {
        const char = from[index];
        if (quote) {
            if (escaped) escaped = false;
            else if (char === '\\') escaped = true;
            else if (char === quote) quote = '';
            continue;
        }
        if (`'"\``.includes(char)) quote = char;
        else if (char === '{') depth += 1;
        else if (char === '}' && --depth === 0) return from.slice(match.index, index + 1);
    }
    throw new Error(`unterminated ${name}`);
}

function makeContext() {
    const state = { measurement: {} };
    peqEditor.init({
        getState: () => state,
        showToast: () => {},
        getElements: () => ({}),
        setTimeout: () => 1,
        renderMeasurementPanel: () => {},
    });
    return { ...peqEditor, state };
}

function peqWithFilters(ctx, count = 2) {
    const peq = ctx.ensureMeasurementPeqState();
    peq.filters = Array.from({ length: count }, (_, index) => ({ id: `f${index}`, freqHz: 1000, gainDb: -1, q: 1 }));
    return peq;
}

const AUTO_RE = /^PEQ LR Measurement 2f( \d{6})?$/;

// 1. Untouched field holding the staged auto name: default flow preserved.
{
    const ctx = makeContext();
    peqWithFilters(ctx);
    ctx.takeMeasurementPeqToPreset('both');
    const peq = ctx.ensureMeasurementPeqState();
    assert.match(peq.draft.presetName, /^PEQ LR Measurement 2f \d{6}$/);
    assert.equal(ctx.resolveMeasurementPeqPresetName(peq, peq.draft.presetName, 'both'), peq.draft.presetName);
}

// 2. Pre-take typing is kept by Take (user input has precedence).
{
    const ctx = makeContext();
    peqWithFilters(ctx);
    const peq = ctx.ensureMeasurementPeqState();
    peq.draft.presetName = 'PreTake PEQ';
    peq.draft.nameTouched = true;
    ctx.takeMeasurementPeqToPreset('both');
    assert.equal(ctx.ensureMeasurementPeqState().draft.presetName, 'PreTake PEQ');
    assert.ok((ctx.ensureMeasurementPeqState().draft.leftBands || []).length > 0, 'bands must still stage');
}

// 3. Edited field wins over a stale auto draft at create resolution.
{
    const ctx = makeContext();
    const peq = { draft: { presetName: 'PEQ LR Measurement 2f 043055', nameTouched: true } };
    assert.equal(ctx.resolveMeasurementPeqPresetName(peq, 'My PEQ Name', 'both'), 'My PEQ Name');
    assert.equal(ctx.resolveMeasurementPeqPresetName(peq, '  Spaced  ', 'both'), 'Spaced');
}

// 4. Empty/blank field falls back to the draft, then to a generated name.
{
    const ctx = makeContext();
    peqWithFilters(ctx);
    const peq = { draft: { presetName: 'PEQ LR Measurement 2f 043055', nameTouched: false } };
    assert.equal(ctx.resolveMeasurementPeqPresetName(peq, '', 'both'), 'PEQ LR Measurement 2f 043055');
    assert.equal(ctx.resolveMeasurementPeqPresetName(peq, '   ', 'left'), 'PEQ LR Measurement 2f 043055');
    assert.match(ctx.resolveMeasurementPeqPresetName({ draft: { presetName: '' } }, '', 'both'), AUTO_RE);
    assert.match(ctx.resolveMeasurementPeqPresetName(null, undefined, 'right'), /^PEQ R Measurement 2f \d{6}$/);
}

// 5. Stable preview suggestion without unique suffix before Take.
{
    const ctx = makeContext();
    peqWithFilters(ctx, 3);
    assert.equal(ctx.getMeasurementPeqPresetName('both'), 'PEQ LR Measurement 3f');
    assert.equal(ctx.getMeasurementPeqPresetName('left'), 'PEQ L Measurement 3f');
}

console.log('peq preset name edit tests: ok');

// Source contract: the PEQ field shows the stable suggestion pre-take
// (no empty value), is editable from that moment (no !hasDraft gate),
// and Take still respects a touched name.
{
    const renderFn = extractFunction('renderMeasurementPanelEditorsSection', editorsSource);
    assert.match(renderFn, /const nameValue = peq\.draft\?\.presetName \|\| deps\.getMeasurementPeqPresetName\(/);
    assert.match(renderFn, /measurementPeqPresetName\.disabled = deps\.isPeqCreateInFlight\(\)/);
    assert.doesNotMatch(renderFn, /measurementPeqPresetName\.disabled = !hasDraft/);
    const takeFn = extractFunction('takeMeasurementPeqToPreset', fs.readFileSync(path.join(__dirname, '..', 'static', 'measurement_peq_editor.js'), 'utf8'));
    assert.match(takeFn, /if\s*\(!peq\.draft\.nameTouched\)\s*peq\.draft\.presetName\s*=/);
    console.log('peq preset name field-availability contract: ok');
}
