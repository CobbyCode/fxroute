#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only

const assert = require('assert/strict');
const path = require('path');

global.window = global;
global.devicePixelRatio = 1;
global.requestAnimationFrame = (callback) => callback();

const dsp = require(path.join(__dirname, '..', 'static', 'measurement_dsp.js'));
require(path.join(__dirname, '..', 'static', 'measurement_ui.js'));
require(path.join(__dirname, '..', 'static', 'measurement_graph.js'));

const points = [
    [80, -8],
    [90, 6],
    [100, -4],
    [110, 8],
    [120, -6],
];
const expectedPoints = dsp.smoothMeasurementTracePoints(points, '1/3-oct');
assert.notDeepEqual(expectedPoints, points, 'fixture must exercise non-trivial smoothing');
const realSmoother = dsp.smoothMeasurementTracePoints;
let smoothingCalls = 0;
dsp.smoothMeasurementTracePoints = (...args) => {
    smoothingCalls += 1;
    return realSmoother(...args);
};
const current = { id: 'current', traces: [{ label: 'Current', points }] };
const saved = { id: 'saved', traces: [{ label: 'Saved', points }] };

window.FXRouteMeasurementGraph.init({
    getDisplaySmoothing: () => '1/3-oct',
    getMeasurementDisplayTraces: (measurement) => measurement.traces,
    getCurrentMeasurementEntries: () => [current],
    getVisibleMeasurementEntries: () => [saved],
    getVisibleMeasurementColorById: () => ({ saved: '#60a5fa' }),
});

const entries = window.FXRouteMeasurementGraph.getGraphMeasurementEntries();
assert.equal(entries.length, 2, 'current and saved measurements must both produce graph entries');
assert.equal(smoothingCalls, 2, 'each graph trace must use the canonical DSP smoother exactly once');
assert.deepEqual(entries[0].traces[0].points, expectedPoints);
assert.deepEqual(entries[1].traces[0].points, expectedPoints);
assert.equal(entries[0].current, true);
assert.equal(entries[1].current, false);

console.log('PASS test_measurement_graph_contract.js');
