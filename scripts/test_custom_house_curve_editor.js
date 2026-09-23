#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only

const assert = require('assert/strict');
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const MeasurementUI = require('../static/measurement_ui.js');

const html = fs.readFileSync(path.join(__dirname, '..', 'static', 'index.html'), 'utf8');
const dspSource = fs.readFileSync(path.join(__dirname, '..', 'static', 'measurement_dsp.js'), 'utf8');

class TestFile {
    constructor(parts, name, options) { this.parts = parts; this.name = name; this.type = options.type; }
}
class TestFormData {
    constructor() { this.values = new Map(); }
    append(key, value) { this.values.set(key, value); }
}

async function main() {
    const requests = [];
    const state = { measurement: { houseCurveOptions: [{ id: 'old', filename: 'Custom House Curve 1', points: [[20, 0], [20000, 0]] }] } };
    const context = {
        MeasurementUI,
        state, elements: {}, File: TestFile, FormData: TestFormData, Date, Math,
        renderMeasurementPanel: () => {}, scheduleMeasurementGraphRender: () => {}, showToast: () => {},
        updateMeasurementConvolverField: (field, value) => { state.measurement.convolverAssistant = { targetCurve: value }; },
        fetch: async (url, options) => {
            requests.push({ url, options });
            return { ok: true, json: async () => ({
                uploaded_house_curve_id: 'new-Custom-House-Curve-2.txt',
                house_curves: [...state.measurement.houseCurveOptions, { id: 'new-Custom-House-Curve-2.txt', filename: 'Custom House Curve 2', points: [[20, 1], [20000, -2]] }],
            }) };
        },
    };
    vm.createContext(context);
    vm.runInContext(fs.readFileSync(path.join(__dirname, '..', 'static', 'measurement_calibration.js'), 'utf8'), context);
    const calibration = context.FXRouteMeasurementCalibration;
    calibration.init({
        getState: () => state, getElements: () => context.elements,
        getFileType: () => TestFile, getFormDataType: () => TestFormData,
        fetch: context.fetch,
        renderMeasurementPanel: context.renderMeasurementPanel,
        scheduleMeasurementGraphRender: context.scheduleMeasurementGraphRender,
        showToast: context.showToast,
        updateMeasurementConvolverField: context.updateMeasurementConvolverField,
        setMeasurementActiveEditor: () => {},
    });
    for (const name of [
        'applyMeasurementHouseCurveState', 'ensureCustomHouseCurveState', 'getCustomHouseCurveNameSuggestion',
        'addCustomHouseCurvePoint', 'updateCustomHouseCurvePoint', 'deleteCustomHouseCurvePoint',
        'serializeCustomHouseCurvePoints', 'createCustomHouseCurve',
    ]) context[name] = (...args) => calibration[name](...args);

    assert.equal(context.getCustomHouseCurveNameSuggestion(), 'Custom House Curve 2');
    const custom = context.ensureCustomHouseCurveState();
    custom.name = 'Custom House Curve 2';
    for (let index = 0; index < 8; index += 1) assert.ok(context.addCustomHouseCurvePoint());
    assert.equal(context.addCustomHouseCurvePoint(), null, 'ninth point rejected');
    const edited = custom.points[7];
    context.updateCustomHouseCurvePoint(edited.id, { freqHz: 25, gainDb: 3.4 });
    context.deleteCustomHouseCurvePoint(custom.points[2].id);
    assert.equal(custom.points.length, 7, 'point deleted');
    context.addCustomHouseCurvePoint();
    assert.equal(custom.points.length, 8, 'deleted slot reusable');
    custom.points.forEach((point, index) => context.updateCustomHouseCurvePoint(point.id, { freqHz: [800, 20, 20000, 200, 50, 5000, 100, 1000][index], gainDb: index - 3 }));

    const serialized = context.serializeCustomHouseCurvePoints(custom.points);
    const rows = serialized.trim().split('\n').map((line) => line.split(/\s+/).map(Number));
    assert.deepEqual(rows.map((row) => row[0]), [20, 50, 100, 200, 800, 1000, 5000, 20000]);
    assert.deepEqual(rows.map((row) => row[1]), [-2, 1, 3, 0, -3, 4, 2, -1]);

    await context.createCustomHouseCurve();
    assert.equal(requests.length, 1);
    assert.equal(requests[0].url, '/api/measurements/house-curves', 'existing upload endpoint reused');
    const uploaded = requests[0].options.body.values.get('house_curve_file');
    assert.equal(uploaded.name, 'Custom House Curve 2.txt');
    assert.equal(uploaded.parts[0], serialized, 'all sorted points sent in compatible text format');
    assert.equal(state.measurement.convolverAssistant.targetCurve, 'house:new-Custom-House-Curve-2.txt');
    assert.ok(state.measurement.houseCurveOptions.some((curve) => curve.id === 'new-Custom-House-Curve-2.txt'));

    assert.match(html, /Create Target Curve/);
    const panelSource = fs.readFileSync(path.join(__dirname, '..', 'static', 'measurement_panel_ui.js'), 'utf8');
    assert.match(panelSource, /Create Custom House Curve…/);
    assert.match(dspSource, /Math\.log10\(frequency\).*Math\.log10\(leftHz\)/s, 'existing target interpolation remains logarithmic');
    console.log('ok custom house curve: eight editable/deletable points, sorted compatible upload, collision-free name, immediate target selection');
}

main().catch((error) => { console.error(error); process.exit(1); });
