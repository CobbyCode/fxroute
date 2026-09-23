#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only
// Editor rendering keeps chip slots, typed names, take availability and
// convolver draft/timing feedback intact.

const assert = require('node:assert/strict');
const ui = require('../static/measurement_ui.js');
const { escapeHtml } = require('../static/ui_helpers.js');
const editors = require('../static/measurement_editors_ui.js');

function element() {
    const classes = new Set();
    return {
        innerHTML: '', textContent: '', value: '', disabled: false, placeholder: '', title: '', dataset: {}, attributes: {},
        classList: {
            add: (name) => classes.add(name),
            remove: (name) => classes.delete(name),
            toggle: (name, enabled) => enabled ? classes.add(name) : classes.delete(name),
            contains: (name) => classes.has(name),
        },
        setAttribute(name, value) { this.attributes[name] = value; },
    };
}

const names = [
    'measurementPeqPanel', 'measurementCustomHouseCurvePanel', 'measurementCustomHouseCurveChips',
    'measurementCustomHouseCurveEditor', 'measurementCustomHouseCurveName', 'measurementCustomHouseCurveCreateBtn',
    'measurementConvolverPanel', 'measurementPeqChips', 'measurementPeqEditor', 'measurementPeqDraftSummary',
    'measurementPeqPresetName', 'measurementPeqTakeLeftBtn', 'measurementPeqTakeRightBtn',
    'measurementPeqTakeBothBtn', 'measurementPeqCreateBtn', 'measurementConvolverTarget',
    'measurementConvolverRangeStart', 'measurementConvolverRangeEnd', 'measurementConvolverMaxBoost',
    'measurementConvolverMaxCut', 'measurementConvolverDipGuard', 'measurementConvolverSampleRate',
    'measurementConvolverPhaseMode', 'measurementConvolverIrLength', 'measurementConvolverSummary',
    'measurementConvolverPresetName', 'measurementConvolverWarnings', 'measurementConvolverTakeLeftBtn',
    'measurementConvolverTakeRightBtn', 'measurementConvolverTakeBothBtn', 'measurementConvolverCreateBtn',
];
const elements = Object.fromEntries(names.map((name) => [name, element()]));
let activeElement = null;
let summed = false;
let peqCreating = false;
let convCreating = false;
let phaseMismatch = '';
let rightAnalysis = null;
let timingDelta = null;
let warnings = ['Select one measurement'];
const custom = { points: [{ id: 'point-1', slot: 0, freqHz: 20, gainDb: 1.5 }],
    activePointId: 'point-1', name: 'Custom curve', saving: false };
const state = { dsp: { assistStack: [] } };
editors.init({
    getState: () => state,
    getElements: () => elements,
    getDocument: () => ({ activeElement }),
    escapeHtml,
    ensureCustomHouseCurveState: () => custom,
    getCustomHouseCurvePointSlot: () => 0,
    getCustomHouseCurvePointColor: () => '#60a5fa',
    getMeasurementPeqPresetName: () => 'PEQ LR Measurement 1f',
    getMeasurementPeqDraftMode: () => 'both',
    isPeqCreateInFlight: () => peqCreating,
    measurementBankSumsBothInputs: () => summed,
    getMeasurementConvolverCurveOptions: () => [{ key: 'neutral', label: 'Neutral', shortLabel: 'Neutral' }],
    getMeasurementConvolverSourceSelectionState: () => ({ take: { left: true, right: !!rightAnalysis, both: !!rightAnalysis } }),
    analyzeMeasurementConvolverSide: (side) => side === 'left'
        ? { side, points: 6, autoGainDb: -4, energyGainDb: -1 } : rightAnalysis,
    getMeasurementConvolverDraftPhaseMismatch: () => phaseMismatch,
    isConvolverCreateInFlight: () => convCreating,
    getMeasurementConvolverCurve: () => ({ label: 'Neutral', shortLabel: 'Neutral' }),
    getMeasurementConvolverTimingDelta: () => timingDelta,
    getMeasurementDirectArrivalTiming: () => null,
    getMeasurementConvolverMeasurementForSide: () => null,
    formatMeasurementConvolverTimingRelation: () => 'R arrives 7 ms later',
    getMeasurementConvolverItemName: () => 'Conv L Min Neutral 20-250Hz -4dB',
    buildMeasurementConvolverWarnings: () => warnings,
});

assert.equal(editors.getMeasurementSlotChipStyle(''), '');
assert.match(editors.renderMeasurementSlotChip({ label: 'F1', index: 0, color: '#60a5fa', active: true, occupied: true, attributes: 'data-measurement-peq-slot="0"' }),
    /measurement-slot-chip measurement-peq-chip is-active[^>]*data-measurement-slot-index="0" data-measurement-peq-slot="0">F1<\/button>/);
assert.match(editors.renderMeasurementSlotChip({ label: 'F2', index: 1 }), /measurement-peq-chip is-empty/);

const peq = { enabled: true, filters: [{ id: 'filter-1', type: 'bell', color: '#60a5fa', freqHz: 900, gainDb: 2, q: 1 }],
    activeFilterId: 'filter-1', draft: { leftBands: [{ type: 'bell' }], rightBands: [], presetName: '', nameTouched: false } };
const conv = { targetCurve: 'neutral', rangeStartHz: 20, rangeEndHz: 250,
    maxBoostDb: 6, maxCutDb: -9, dipGuard: 'off', phaseMode: 'minimum', irLength: '8192', quality: 'minimum_8192',
    draft: { left: null, right: null, presetName: '', nameTouched: false, notice: '' } };
const ctx = { measurementState: { measurementSampleRate: '48000' },
    current: null, measurements: [], graphEntries: [], assistMode: 'peq', activeEditor: 'peq',
    graphView: 'freq', frequencyView: true, peq, conv, activePeqFilter: peq.filters[0] };

editors.renderMeasurementPanelEditorsSection(ctx);
assert.match(elements.measurementPeqChips.innerHTML, /data-measurement-peq-slot="11"/);
assert.match(elements.measurementPeqEditor.innerHTML, /value="900" data-measurement-peq-field="freqHz"/);
assert.match(elements.measurementPeqDraftSummary.innerHTML, /L draft ready · L: 1 bands · R: 0 bands/);
assert.equal(elements.measurementPeqPresetName.value, 'PEQ LR Measurement 1f');
assert.equal(elements.measurementPeqPresetName.disabled, false);
assert.equal(elements.measurementPeqCreateBtn.disabled, true);
assert.match(elements.measurementCustomHouseCurveChips.innerHTML, /data-custom-house-curve-slot="7"/);
assert.equal(elements.measurementCustomHouseCurvePanel.classList.contains('hidden'), true);

elements.measurementPeqPresetName.value = 'Typed PEQ';
elements.measurementCustomHouseCurveName.value = 'Typed curve';
activeElement = elements.measurementPeqPresetName;
editors.renderMeasurementPanelEditorsSection(ctx);
assert.equal(elements.measurementPeqPresetName.value, 'Typed PEQ');
elements.measurementCustomHouseCurveName.value = 'Typed curve';
activeElement = elements.measurementCustomHouseCurveName;
editors.renderMeasurementPanelEditorsSection({ ...ctx, activeEditor: 'houseCurve' });
assert.equal(elements.measurementCustomHouseCurveName.value, 'Typed curve');
assert.equal(elements.measurementCustomHouseCurvePanel.classList.contains('hidden'), false);
assert.match(elements.measurementCustomHouseCurveEditor.innerHTML, /value="1\.5" data-custom-house-curve-field="gainDb"/);

activeElement = elements.measurementConvolverRangeStart;
elements.measurementConvolverRangeStart.value = '37';
editors.renderMeasurementPanelConvolverSection(ctx);
assert.equal(elements.measurementConvolverRangeStart.value, '37');
assert.equal(elements.measurementConvolverRangeEnd.value, '250');
assert.match(elements.measurementConvolverSummary.innerHTML, /6 pts, gain -4dB/);
assert.equal(elements.measurementConvolverTakeLeftBtn.disabled, false);
assert.equal(elements.measurementConvolverTakeRightBtn.disabled, true);
assert.equal(elements.measurementConvolverPresetName.value, 'Conv L Min Neutral 20-250Hz -4dB');
assert.equal(elements.measurementConvolverPresetName.disabled, false);
assert.match(elements.measurementConvolverWarnings.innerHTML, /Select one measurement/);

summed = true;
editors.syncMeasurementSummedSubTakeModes();
assert.equal(elements.measurementPeqTakeLeftBtn.classList.contains('hidden'), true);
assert.equal(elements.measurementConvolverTakeBothBtn.classList.contains('hidden'), true);
assert.equal(elements.measurementPeqTakeBothBtn.textContent, 'Take Mono');
assert.equal(elements.measurementConvolverTakeLeftBtn.textContent, 'Take Mono');
assert.equal(elements.measurementConvolverTakeBothBtn.disabled, true);
assert.match(elements.measurementConvolverTakeBothBtn.title, /mono IR/);
summed = false;
elements.measurementPeqTakeLeftBtn.dataset.summedSub = 'true';
elements.measurementPeqTakeLeftBtn.disabled = true;
editors.syncMeasurementSummedSubTakeModes();
assert.equal(elements.measurementPeqTakeLeftBtn.disabled, true);
assert.equal(elements.measurementPeqTakeLeftBtn.dataset.summedSub, 'false');
assert.equal(elements.measurementPeqTakeLeftBtn.title, '');

activeElement = elements.measurementConvolverPresetName;
elements.measurementConvolverPresetName.value = 'Typed convolver';
conv.draft.left = { timing: { available: true }, phaseMode: 'minimum_aligned' };
conv.draft.right = { timing: { available: true }, phaseMode: 'minimum_aligned' };
conv.draft.presetName = 'Staged convolver';
conv.phaseMode = 'minimum_aligned';
phaseMismatch = 'minimum';
rightAnalysis = { side: 'right', points: 6, autoGainDb: -3, energyGainDb: 0 };
timingDelta = { absMs: 7.5 };
warnings = [];
convCreating = true;
editors.renderMeasurementPanelConvolverSection(ctx);
assert.equal(elements.measurementConvolverPresetName.value, 'Typed convolver');
assert.equal(elements.measurementConvolverPresetName.disabled, true);
assert.equal(elements.measurementConvolverCreateBtn.disabled, true);
assert.match(elements.measurementConvolverSummary.innerHTML, /Creating convolver preset/);
assert.match(elements.measurementConvolverSummary.innerHTML, /timing offset exceeds safety limit/);
assert.equal(elements.measurementConvolverTakeBothBtn.disabled, true);

console.log('measurement editor/convolver rendering: ok');
