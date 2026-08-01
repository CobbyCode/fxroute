#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only

const assert = require('assert/strict');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const root = path.join(__dirname, '..');
const source = fs.readFileSync(path.join(root, 'static', 'app.js'), 'utf8');
const style = fs.readFileSync(path.join(root, 'static', 'style.css'), 'utf8');

function extractFunction(name) {
    const match = new RegExp(`function\\s+${name}\\s*\\(`).exec(source);
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
        if (`'\"\``.includes(char)) quote = char;
        else if (char === '{') depth += 1;
        else if (char === '}' && --depth === 0) return source.slice(match.index, index + 1);
    }
    throw new Error(`unterminated ${name}`);
}

const state = {
    measurement: {
        assistMode: 'peq',
        activeEditor: 'none',
        convolverAssistant: { targetCurve: 'neutral', dragMode: null },
        peqAssistant: { enabled: false, filters: [], dragFilterId: 'dragging' },
        customHouseCurve: {
            open: false,
            displayTarget: 'actual',
            points: [{ id: 'draft-point', slot: 0, freqHz: 20, gainDb: 0 }],
            activePointId: 'draft-point',
            dragPointId: 'draft-point',
        },
    },
};
const context = {
    state,
    escapeHtml: (value) => String(value),
    renderMeasurementPanel: () => {},
    scheduleMeasurementGraphRender: () => {},
    getMeasurementConvolverCurveOptions: () => [
        { key: 'neutral' },
        { key: 'harman' },
        { key: 'house:known' },
    ],
    ensureMeasurementConvolverState: () => state.measurement.convolverAssistant,
    ensureMeasurementPeqState: () => state.measurement.peqAssistant,
};
vm.createContext(context);
vm.runInContext([
    'ensureCustomHouseCurveState',
    'getMeasurementActiveEditor',
    'getMeasurementRestorableTargetCurve',
    'setMeasurementActiveEditor',
    'setMeasurementAssistMode',
].map(extractFunction).join('\n'), context);

// 1–2. Neutral -> Custom -> re-activate the already selected PEQ method:
// editor closes, Neutral remains selected, PEQ becomes visible/enabled.
context.setMeasurementActiveEditor('houseCurve');
assert.equal(state.measurement.customHouseCurve.open, true);
const draftSnapshot = JSON.stringify(state.measurement.customHouseCurve.points);
context.setMeasurementAssistMode('peq');
assert.equal(state.measurement.activeEditor, 'peq');
assert.equal(state.measurement.customHouseCurve.open, false);
assert.equal(state.measurement.customHouseCurve.displayTarget, 'actual');
assert.equal(state.measurement.convolverAssistant.targetCurve, 'neutral');
assert.equal(state.measurement.peqAssistant.enabled, true);
assert.equal(JSON.stringify(state.measurement.customHouseCurve.points), draftSnapshot, 'custom draft survives PEQ activation');

// 3. Re-open Custom and verify the same draft is still present.
context.setMeasurementActiveEditor('houseCurve');
assert.equal(state.measurement.customHouseCurve.open, true);
assert.equal(JSON.stringify(state.measurement.customHouseCurve.points), draftSnapshot);

// 4. Custom -> Convolver restores the last real target, not the editor sentinel.
state.measurement.convolverAssistant.targetCurve = 'harman';
context.setMeasurementAssistMode('convolver');
assert.equal(state.measurement.activeEditor, 'none');
assert.equal(state.measurement.customHouseCurve.open, false);
assert.equal(state.measurement.convolverAssistant.targetCurve, 'harman');
assert.equal(JSON.stringify(state.measurement.customHouseCurve.points), draftSnapshot, 'custom draft survives Convolver activation');

// 5. If the previous target disappeared, the exact fallback is Neutral.
context.setMeasurementActiveEditor('houseCurve');
state.measurement.convolverAssistant.targetCurve = 'house:deleted';
context.setMeasurementAssistMode('convolver');
assert.equal(state.measurement.convolverAssistant.targetCurve, 'neutral');
assert.equal(state.measurement.activeEditor, 'none');

// Shared slot geometry/state contract for both rendered chip groups.
assert.match(source, /renderMeasurementSlotChip\(\{[\s\S]*label: `P\$\{index \+ 1\}`/);
assert.match(source, /renderMeasurementSlotChip\(\{[\s\S]*label: `F\$\{index \+ 1\}`/);
assert.match(source, /measurement-slot-chip measurement-peq-chip/);
assert.match(style, /\.measurement-slot-chip,\s*\.measurement-peq-chip\s*\{/);
assert.match(style, /\.measurement-slot-chip\s*\{[\s\S]*flex: 0 0 3\.25rem/);
assert.match(style, /\.measurement-peq-chip\.is-empty\s*\{[\s\S]*border-style: dashed/);
assert.match(source, /elements\.measurementAssistMode\.addEventListener\('focus'/);
assert.match(source, /elements\.measurementAssistMode\.addEventListener\('blur'/);
assert.match(source, /elements\.measurementAssistMode\.addEventListener\('click', \(\) => \{/);
assert.match(source, /window\.setTimeout\(activateSelectedMethod, 0\)/);
console.log('ok HCE-003: PEQ reactivation, draft preservation, Convolver target restore/Neutral fallback, shared F/P slot geometry');
