#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only
// Saved-list rendering retains selection, frozen-area labels, graph colors,
// details state and action availability.

const assert = require('node:assert/strict');
const fs = require('node:fs');
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
// Open/close belongs to the accordion summary alone: the separate Close
// button and its handler are gone, so a stray click cannot fold the group.
assert.doesNotMatch(html, /data-measurement-close-saved|measurement-saved-close-action/);
assert.match(html, /class="measurement-saved-toolbar">\s*<label class="measurement-list-meta measurement-select-all-toggle">/);
assert.match(html, /<div class="measurement-saved-toolbar-selection">\s*<button[^>]*data-measurement-delete-selected[\s\S]*?<button[^>]*data-measurement-merge-selected[^>]*>[\s\S]*?<\/div>/);
assert.match(html, /class="measurement-list-meta measurement-list-date">Sep 20, 2026/);

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

// Delegation remains on the stable list, including a capture-phase toggle.
const listeners = {};
const calls = [];
elements.measurementList.addEventListener = (type, handler, capture) => {
    (listeners[type] ||= []).push({ handler, capture });
};
savedUi.init({
    renderMeasurementPanel: () => calls.push('render'),
    mergeSelectedMeasurements: () => calls.push('merge'),
    deleteSelectedMeasurements: () => calls.push('delete'),
});
savedUi.bindMeasurementSavedListDelegation();
savedUi.bindMeasurementSavedListDelegation();
assert.deepEqual(Object.fromEntries(Object.entries(listeners).map(([key, value]) => [key, value.length])),
    { toggle: 1, change: 1, click: 1 });
assert.equal(listeners.toggle[0].capture, true);
const summary = { textContent: '' };
class Details {
    open = false;
    querySelector() { return summary; }
}
savedUi.init({ getDetailsElementType: () => Details });
listeners.toggle[0].handler({ target: new Details() });
assert.equal(state.measurement.savedGroupOpen, false);
assert.equal(summary.textContent, 'Open saved (2)');
listeners.change[0].handler({ target: {
    dataset: { measurementToggle: 'mid' }, checked: false,
    matches: (selector) => selector === '[data-measurement-toggle]',
} });
assert.equal(state.measurement.visibilityById.mid, false);
assert.equal(state.measurement.savedGroupOpen, true);
assert.deepEqual(calls, ['render']);
listeners.change[0].handler({ target: {
    checked: true, matches: (selector) => selector === '[data-measurement-select-all]',
} });
assert.deepEqual([state.measurement.visibilityById.mid, state.measurement.visibilityById.legacy], [true, true]);
listeners.click[0].handler({ target: { closest: (selector) => selector === '[data-measurement-merge-selected]' ? {} : null } });
assert.deepEqual(calls, ['render', 'render', 'merge']);
listeners.click[0].handler({ target: { closest: (selector) => selector === '[data-measurement-delete-selected]' ? {} : null } });
assert.deepEqual(calls, ['render', 'render', 'merge', 'delete']);
// The removed Close control is inert: only the summary folds the group.
listeners.click[0].handler({ target: { closest: (selector) => selector === '[data-measurement-close-saved]' ? {} : null } });
assert.equal(state.measurement.savedGroupOpen, true);
assert.deepEqual(calls, ['render', 'render', 'merge', 'delete']);

// The layout contract the narrow-viewport fix depends on: the grid tracks may
// shrink below their content, the run name truncates, and the free-form meta
// wraps anywhere so an unbroken ALSA id cannot widen the card.
const measurementCss = fs.readFileSync(require.resolve('../static/css/_measurement.css'), 'utf8');
const responsiveCss = fs.readFileSync(require.resolve('../static/css/_responsive.css'), 'utf8');
for (const selector of ['.measurement-list', '.measurement-saved-list', '.measurement-list-item']) {
    assert.match(measurementCss, new RegExp(`\\${selector}\\s*\\{[^}]*grid-template-columns:\\s*minmax\\(0,\\s*1fr\\)`),
        `${selector} must not be floored at its min-content width`);
}
assert.match(measurementCss, /\.measurement-list-meta,\s*\.measurement-list-points\s*\{[^}]*overflow-wrap:\s*anywhere/,
    'long device strings must be able to break inside the card');
assert.match(measurementCss, /\.measurement-list-title\s*\{[^}]*min-width:\s*0/);
assert.match(measurementCss, /\.measurement-toggle\s*\{[^}]*flex-wrap:\s*wrap[^}]*max-width:\s*100%/);
assert.match(measurementCss, /\.measurement-list-title a\s*\{[^}]*text-overflow:\s*ellipsis/);
// The fixed-format values keep their own line; the free-form meta takes the slack.
assert.match(measurementCss, /\.measurement-list-date,\s*\.measurement-list-points\s*\{[^}]*white-space:\s*nowrap[^}]*flex:\s*0 0 auto/);
assert.match(measurementCss, /\.measurement-list-row > \.measurement-list-meta:not\(\.measurement-list-date\)\s*\{[^}]*flex:\s*1 1 auto/);
for (const source of [measurementCss, responsiveCss]) {
    assert.doesNotMatch(source, /measurement-saved-close-action/,
        'the redundant Close button is fully removed');
}

console.log('measurement saved-list rendering: ok');
