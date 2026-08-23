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
    [20, -6],
    [40, -3],
    [80, 4],
    [160, -2],
    [320, 1],
];
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
assert.deepEqual(entries[0].traces[0].points, dsp.smoothMeasurementTracePoints(points, '1/3-oct'));
assert.deepEqual(entries[1].traces[0].points, dsp.smoothMeasurementTracePoints(points, '1/3-oct'));
assert.equal(entries[0].current, true);
assert.equal(entries[1].current, false);

console.log('PASS test_measurement_graph_contract.js');
