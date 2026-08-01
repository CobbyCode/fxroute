#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only

const assert = require('assert/strict');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const source = fs.readFileSync(path.join(__dirname, '..', 'static', 'app.js'), 'utf8');
const html = fs.readFileSync(path.join(__dirname, '..', 'static', 'index.html'), 'utf8');
const dspSource = fs.readFileSync(path.join(__dirname, '..', 'static', 'measurement_dsp.js'), 'utf8');

function extractFunction(name) {
    const match = new RegExp(`(?:async\\s+)?function\\s+${name}\\s*\\(`).exec(source);
    assert.ok(match, `missing ${name}`);
    let parenDepth = 1;
    let brace = -1;
    for (let index = match.index + match[0].length; index < source.length; index += 1) {
        if (source[index] === '(') parenDepth += 1;
        if (source[index] === ')') parenDepth -= 1;
        if (parenDepth === 0) {
            brace = source.indexOf('{', index);
            break;
        }
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
    vm.runInContext([
        'applyMeasurementHouseCurveState', 'ensureCustomHouseCurveState', 'getCustomHouseCurveNameSuggestion',
        'addCustomHouseCurvePoint', 'updateCustomHouseCurvePoint', 'deleteCustomHouseCurvePoint',
        'serializeCustomHouseCurvePoints', 'createCustomHouseCurve',
    ].map(extractFunction).join('\n'), context);

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
    assert.match(source, /Create Custom House Curve…/);
    assert.match(dspSource, /Math\.log10\(frequency\).*Math\.log10\(leftHz\)/s, 'existing target interpolation remains logarithmic');
    console.log('ok custom house curve: eight editable/deletable points, sorted compatible upload, collision-free name, immediate target selection');
}

main().catch((error) => { console.error(error); process.exit(1); });
