#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only
// File lifecycle keeps busy flags, job guards and Convolver target updates.
const assert = require('node:assert/strict');
const calibration = require('../static/measurement_calibration.js');

class FormDataStub {
    constructor() { this.fields = new Map(); }
    append(key, value) { this.fields.set(key, value); }
}
const state = { measurement: {
    calibrationOptions: [{ id: 'cal-old', filename: 'Old.txt' }], selectedCalibrationRef: 'cal-old',
    houseCurveOptions: [{ id: 'house-old', filename: 'Old curve.txt' }],
    convolverAssistant: { targetCurve: 'neutral', draft: { left: null, right: null } },
} };
const elements = {
    measurementCalibrationFile: { value: '' }, measurementHouseCurveFile: { value: '' },
    measurementHouseCurveSelect: { value: 'house-old' },
};
const calls = [];
let confirmResult = true;
let response = { ok: true, json: async () => ({}) };
calibration.init({
    getState: () => state, getElements: () => elements,
    fetch: async (url, options) => { calls.push(['fetch', url, options?.method || 'GET']); return response; },
    getFormDataType: () => FormDataStub,
    renderMeasurementPanel: () => calls.push(['render', !!state.measurement.calibrationUpdating,
        !!state.measurement.calibrationDeleting, !!state.measurement.houseCurveUpdating]),
    showToast: (message, kind) => calls.push(['toast', message, kind]),
    confirm: () => { calls.push('confirm'); return confirmResult; },
    fetchMeasurements: () => calls.push('reload'),
    ensureMeasurementConvolverState: () => state.measurement.convolverAssistant,
    updateMeasurementConvolverField: (field, value) => { calls.push(['target', field, value]); state.measurement.convolverAssistant.targetCurve = value; },
});

async function main() {
    const m = state.measurement;
    assert.equal(calibration.measurementHasCalibrationSelected(), true);
    response = { ok: true, json: async () => ({ calibrations: [{ id: 'cal-new', filename: 'New.txt' }], active_calibration_file_id: 'cal-new' }) };
    await calibration.uploadMeasurementCalibration({ name: 'New.txt' });
    assert.deepEqual(calls.slice(0, 2), [['render', true, false, false], ['fetch', '/api/measurements/calibrations', 'POST']]);
    assert.equal(m.selectedCalibrationRef, 'cal-new');
    assert.equal(m.calibrationUpdating, false);
    assert.equal(elements.measurementCalibrationFile.value, '');
    calls.length = 0;
    m.activeJobId = 'active';
    await calibration.deleteSelectedMeasurementCalibration();
    assert.deepEqual(calls, [['toast', 'Cannot delete calibration during an active measurement', 'warning']]);
    m.activeJobId = '';
    response = { ok: true, json: async () => ({ calibrations: [], active_calibration_file_id: '' }) };
    calls.length = 0;
    await calibration.deleteSelectedMeasurementCalibration();
    assert.equal(calls[0], 'confirm');
    assert.deepEqual(calls[1], ['render', false, true, false]);
    assert.equal(m.calibrationDeleting, false);
    assert.equal(m.selectedCalibrationRef, '');

    response = { ok: true, json: async () => ({ house_curves: [{ id: 'new', filename: 'New curve.txt' }], uploaded_house_curve_id: 'new' }) };
    calls.length = 0;
    await calibration.uploadMeasurementHouseCurve({ name: 'New curve.txt' });
    assert.deepEqual(calls.slice(0, 2), [['render', false, false, true], ['fetch', '/api/measurements/house-curves', 'POST']]);
    assert.deepEqual(calls.find((call) => call[0] === 'target'), ['target', 'targetCurve', 'house:new']);
    assert.equal(m.houseCurveUpdating, false);
    assert.equal(elements.measurementHouseCurveFile.value, '');
    elements.measurementHouseCurveSelect.value = 'new';
    response = { ok: true, json: async () => ({ house_curves: [] }) };
    await calibration.deleteSelectedMeasurementHouseCurve();
    assert.equal(m.convolverAssistant.targetCurve, 'neutral');
    assert.equal(m.houseCurveDeleting, false);

    confirmResult = false;
    m.selectedCalibrationRef = 'cal-new';
    m.calibrationOptions = [{ id: 'cal-new', filename: 'New.txt' }];
    calls.length = 0;
    await calibration.deleteSelectedMeasurementCalibration();
    assert.deepEqual(calls, ['confirm']);
    console.log('measurement calibration and house-curve files: ok');
}
main().catch((error) => { console.error(error); process.exitCode = 1; });
