#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only
// Saved-list rendering retains selection, frozen-area labels, graph colors,
// details state and action availability.

const assert = require('node:assert/strict');
require('../static/measurement_ui.js');
const outputState = require('../static/output_state.js');
const { escapeHtml } = require('../static/ui_helpers.js');
const savedUi = require('../static/measurement_saved_ui.js');

const current = { id: 'current', name: 'Current' };
const measurements = [
    current,
    { id: 'mid', name: 'Left & <mid>', created_at: '2026-09-20T10:00:00Z', channel: 'left',
        input_device: { label: 'Mic A' }, input_channels: { mic: 1, electrical_reference: 3 },
        traces: [{ points: [[20, 0], [1000, 2]] }],
        measurement_target: { schema: 'fxroute.measurement-target', bank_id: 'left_mid', mode: 'stereo-sub' },
        measurement_target_stale: true },
    { id: 'legacy', name: 'Old sweep', created_at: '2026-09-19T10:00:00Z', channel: 'right',
        input_device: { label: 'Mic B' }, traces: [{ points: [[20, 0], [1000, 1]] }] },
];
const state = { measurement: {
    measurements, visibilityById: { mid: true, legacy: false },
    savedGroupOpen: true, saveInFlight: false, startInFlight: false,
} };
const elements = { measurementList: { innerHTML: '' } };
savedUi.init({
    getState: () => state,
    getElements: () => elements,
    getCurrentMeasurementEntry: () => current,
    getVisibleMeasurementColorById: () => ({ mid: '#60a5fa' }),
    getOutputSystemModule: () => outputState,
    getCompactDisplayName: (name) => name,
    escapeHtml,
});

const saved = savedUi.getSavedListMeasurements();
assert.deepEqual(saved.map((item) => item.id), ['mid', 'legacy']);
assert.deepEqual(savedUi.measurementAreaBadge(current), null);
assert.deepEqual(savedUi.measurementAreaBadge(measurements[2]), null);
assert.deepEqual(savedUi.measurementAreaBadge(measurements[1]), {
    label: 'Mid L', mode: 'stereo-sub', stale: true,
    title: 'Mid L · Stereo + Sub · measured area only',
});
savedUi.renderMeasurementPanelSavedListSection({ measurementState: state.measurement, measurements: saved });
let html = elements.measurementList.innerHTML;
assert.match(html, /<details class="measurement-saved-group" open>/);
assert.match(html, /<summary>Close saved \(2\)<\/summary>/);
assert.match(html, /data-measurement-toggle="mid" checked/);
assert.match(html, /data-measurement-toggle="legacy" >/);
assert.match(html, /measurement-area-badge is-stale" title="Mid L · Stereo \+ Sub · measured area only">Mid L<\/span>/);
assert.equal((html.match(/measurement-area-badge is-stale/g) || []).length, 1);
assert.match(html, /background:#60a5fa/);
assert.match(html, /Left &amp; &lt;mid&gt;/);
assert.match(html, /Mic In 1 · Ref In 3/);
assert.match(html, /data-measurement-select-all  /);
assert.match(html, /data-measurement-delete-selected /);
assert.match(html, /data-measurement-merge-selected disabled/);
assert.doesNotMatch(html, /data-measurement-toggle="current"/);

state.measurement.visibilityById.legacy = true;
state.measurement.savedGroupOpen = false;
state.measurement.saveInFlight = true;
savedUi.renderMeasurementPanelSavedListSection({ measurementState: state.measurement, measurements: saved });
html = elements.measurementList.innerHTML;
assert.match(html, /<details class="measurement-saved-group" >/);
assert.match(html, /<summary>Open saved \(2\)<\/summary>/);
assert.match(html, /data-measurement-select-all checked disabled/);
assert.match(html, /data-measurement-merge-selected  disabled/);
savedUi.renderMeasurementPanelSavedListSection({ measurementState: state.measurement, measurements: [] });
assert.equal(elements.measurementList.innerHTML, '');

console.log('measurement saved-list rendering: ok');
