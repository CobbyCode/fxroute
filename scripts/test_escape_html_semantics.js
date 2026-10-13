#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only
// Structural + behavioural checks for escapeHtml semantics and the
// capture-input option render path.
//
// escapeHtml must only collapse null/undefined to ''; every other value
// (including numeric 0, which is falsy) is stringified and escaped, so an
// id of 0 can never silently render as an empty option value. The
// capture-input <option> values must go through escapeHtml like every other
// interpolated attribute value in the render path.

const assert = require('assert/strict');
const fs = require('fs');
const path = require('path');

const root = path.join(__dirname, '..');
const appSource = fs.readFileSync(path.join(root, 'static', 'app.js'), 'utf8');

// Canonical implementation lives in ui_helpers.js; app.js keeps a thin
// delegating wrapper so existing call sites stay unchanged.
const { escapeHtml } = require('../static/ui_helpers.js');
assert.ok(typeof escapeHtml === 'function', 'static/ui_helpers.js must export escapeHtml');
assert.ok(/function escapeHtml\(text\)/.test(appSource), 'static/app.js must keep an escapeHtml wrapper');
assert.ok(/FXRouteUiHelpers/.test(appSource), 'static/app.js escapeHtml wrapper must delegate to ui_helpers.js');

assert.equal(escapeHtml(0), '0', 'numeric 0 must survive escaping');
assert.equal(escapeHtml(null), '', 'null must collapse to empty');
assert.equal(escapeHtml(undefined), '', 'undefined must collapse to empty');
assert.equal(escapeHtml(''), '', 'empty string stays empty');
assert.equal(escapeHtml(48000), '48000', 'numbers stringify unchanged');
assert.equal(escapeHtml(false), 'false', 'booleans stringify, never collapse');
assert.equal(
    escapeHtml('<script>&"\''),
    '&lt;script&gt;&amp;&quot;&#39;',
    'all five characters are escaped (attribute-safe)'
);

// Render through the panel module with the real escaping helper. A hostile
// id must be safe inside the option attribute; numeric zero stays visible.
const panel = require('../static/measurement_panel_ui.js');
const inputSelect = { innerHTML: '', value: '', disabled: false };
panel.init({
    getElements: () => ({ measurementInputSelect: inputSelect }),
    isSelectFocused: () => false,
    getSelectedMeasurementInputChannelCount: () => 1,
    measurementModeReady: () => true,
    escapeHtml,
});
panel.renderMeasurementPanelInputsSection({ measurementState: { inputs: [{ id: '<script>&"\'', label: 'mic' }] } });
assert.equal(inputSelect.innerHTML, '<option value="&lt;script&gt;&amp;&quot;&#39;" >mic</option>');
panel.renderMeasurementPanelInputsSection({ measurementState: { inputs: [{ id: 0, label: 'mic' }] } });
assert.equal(inputSelect.innerHTML, '<option value="0" >mic</option>');

console.log('PASS scripts/test_escape_html_semantics.js');
