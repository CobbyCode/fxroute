#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only
// A panel render must preserve setup placement, capture-input options, and
// focused selects while keeping the single/split reference presentation.

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const root = path.resolve(__dirname, '..');
const page = fs.readFileSync(path.join(root, 'static/index.html'), 'utf8');
const panel = require('../static/measurement_panel_ui.js');
assert.ok(page.indexOf('/static/measurement_panel_ui.js?') < page.indexOf('/static/app.js?') && page.includes('/static/measurement_panel_ui.js?'));

function element() {
    const classes = new Set();
    return {
        classList: {
            add: (name) => classes.add(name),
            remove: (name) => classes.delete(name),
            toggle: (name, enabled) => enabled ? classes.add(name) : classes.delete(name),
            contains: (name) => classes.has(name),
        },
        innerHTML: '', textContent: '', value: '', disabled: false, title: '', parentElement: null,
        attributes: {},
        setAttribute(name, value) { this.attributes[name] = value; },
        getAttribute(name) { return this.attributes[name]; },
        appendChild(child) { child.parentElement = this; },
    };
}

const names = [
    'measurementMain', 'measurementSetupCard', 'measurementSetupToggleBtn', 'measurementSetupStatus',
    'measurementModeNote', 'measurementInputGroup', 'measurementInputSelect',
    'measurementConvolverSampleRate', 'measurementInputRefreshBtn', 'measurementInputChannelGrid',
    'measurementReferenceSingleGroup', 'measurementReferenceLeftGroup', 'measurementReferenceRightGroup',
    'measurementMicInputChannelSelect', 'measurementReferenceInputChannelSelect',
    'measurementReferenceInputChannelLeftSelect', 'measurementReferenceInputChannelRightSelect',
    'measurementReferenceWarning',
];
const elements = Object.fromEntries(names.map((name) => [name, element()]));
const controlsHost = element();
elements.measurementSetupToggleBtn.closest = () => controlsHost;
const smoothing = element();
smoothing.attributes['data-measurement-smoothing'] = '1/6-oct';
const view = element();
view.attributes['data-measurement-view'] = 'freq';
const eventOrder = [];
const focused = new Set();
panel.init({
    getElements: () => elements,
    getDocument: () => ({
        querySelectorAll(selector) {
            eventOrder.push(selector);
            return selector === '[data-measurement-smoothing]' ? [smoothing] : [view];
        },
    }),
    renderMeasurementArea: () => eventOrder.push('area'),
    isSelectFocused: (select) => focused.has(select),
    getSelectedMeasurementInput: () => ({ supportedRates: [44100, 48000] }),
    getSelectedMeasurementInputChannelCount: () => 4,
    getMeasurementReferenceWarning: () => 'Reference conflict',
    measurementModeReady: () => true,
    escapeHtml: (value) => String(value).replaceAll('&', '&amp;').replaceAll('<', '&lt;'),
    formatRateKhz: (rate) => `${rate / 1000} kHz`,
});

const measurementState = {
    setupOpen: true, startInFlight: false, modeNote: 'Host capture',
    displaySmoothing: '1/6-oct', inputs: [{ id: 'mic', label: 'Mic & <line>', supportedRates: [44100, 48000] }],
    selectedInputId: 'mic', selectedMicInputChannel: '2', selectedReferenceInputChannel: '3',
    selectedReferenceInputChannelLeft: '3', selectedReferenceInputChannelRight: '4',
    hostCaptureAvailable: true, measurementSampleRate: '44100',
};
panel.renderMeasurementPanelSetupSection({ measurementState, frequencyView: true, graphView: 'freq' });
assert.equal(elements.measurementSetupStatus.parentElement, elements.measurementSetupCard);
assert.equal(elements.measurementMain.classList.contains('is-setup'), true);
assert.equal(elements.measurementSetupCard.classList.contains('hidden'), false);
assert.equal(elements.measurementModeNote.textContent, 'Host capture');
assert.deepEqual(eventOrder, ['area', '[data-measurement-smoothing]', '[data-measurement-view]']);
assert.equal(smoothing.attributes['aria-pressed'], 'true');
assert.equal(view.attributes['aria-pressed'], 'true');

panel.renderMeasurementPanelInputsSection({ measurementState });
assert.match(elements.measurementInputSelect.innerHTML, /Mic &amp; &lt;line>/);
assert.equal(elements.measurementInputSelect.disabled, false);
assert.match(elements.measurementConvolverSampleRate.innerHTML, /44\.1 kHz/);
assert.equal(elements.measurementConvolverSampleRate.value, '44100');
assert.equal(elements.measurementInputChannelGrid.classList.contains('is-split'), true);
assert.equal(elements.measurementReferenceSingleGroup.classList.contains('hidden'), true);
assert.equal(elements.measurementReferenceLeftGroup.classList.contains('hidden'), false);
assert.match(elements.measurementReferenceInputChannelRightSelect.innerHTML, /value="4" selected/);
assert.equal(elements.measurementReferenceWarning.textContent, 'Reference conflict');
assert.equal(elements.measurementReferenceWarning.classList.contains('hidden'), false);

const protectedSelects = [elements.measurementInputSelect, elements.measurementConvolverSampleRate,
    elements.measurementMicInputChannelSelect, elements.measurementReferenceInputChannelLeftSelect];
for (const select of protectedSelects) {
    focused.add(select);
    select.innerHTML = 'editing selection';
    select.disabled = false;
}
measurementState.inputsLoading = true;
measurementState.startInFlight = true;
measurementState.setupOpen = false;
panel.renderMeasurementPanelSetupSection({ measurementState, frequencyView: false, graphView: 'ir' });
panel.renderMeasurementPanelInputsSection({ measurementState });
assert.equal(elements.measurementSetupStatus.parentElement, controlsHost);
assert.equal(elements.measurementSetupCard.classList.contains('hidden'), true);
assert.equal(elements.measurementSetupToggleBtn.disabled, true);
assert.equal(smoothing.disabled, true);
assert.equal(smoothing.title, 'Only available in frequency view.');
for (const select of protectedSelects) {
    assert.equal(select.innerHTML, 'editing selection');
    assert.equal(select.disabled, false);
}
assert.equal(elements.measurementInputRefreshBtn.textContent, 'Detecting…');
assert.equal(elements.measurementInputRefreshBtn.disabled, true);
assert.equal(elements.measurementReferenceInputChannelRightSelect.disabled, true);

const navigationListeners = [];
const navigationDocument = { activeElement: null };
const chips = Array.from({ length: 3 }, () => ({
    disabled: false,
    focus(options) { navigationDocument.activeElement = this; assert.deepEqual(options, { preventScroll: true }); },
}));
chips[1].disabled = true;
const chipRow = {
    addEventListener: (type, handler) => { assert.equal(type, 'keydown'); navigationListeners.push(handler); },
    querySelectorAll: (selector) => { assert.equal(selector, '[data-measurement-view]'); return chips; },
};
panel.init({ getDocument: () => navigationDocument });
panel.bindChipArrowKeyNavigation(chipRow, '[data-measurement-view]');
panel.bindChipArrowKeyNavigation(chipRow, '[data-measurement-view]');
assert.equal(navigationListeners.length, 1);
navigationDocument.activeElement = chips[2];
let prevented = 0;
navigationListeners[0]({ key: 'ArrowRight', preventDefault: () => prevented++ });
assert.equal(navigationDocument.activeElement, chips[0]);
navigationListeners[0]({ key: 'ArrowLeft', preventDefault: () => prevented++ });
assert.equal(navigationDocument.activeElement, chips[2]);
assert.equal(prevented, 2);

console.log('measurement panel setup/inputs rendering: ok');
