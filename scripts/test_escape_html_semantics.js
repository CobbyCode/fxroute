#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only
// Structural + behavioural checks for escapeHtml semantics and the
// sample-rate option render path.
//
// escapeHtml must only collapse null/undefined to ''; every other value
// (including numeric 0, which is falsy) is stringified and escaped, so a
// sample rate of 0 can never silently render as an empty option value. The
// sample-rate <option> values must go through escapeHtml like every other
// interpolated attribute value in the render path.

const assert = require('assert/strict');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const root = path.join(__dirname, '..');
const appSource = fs.readFileSync(path.join(root, 'static', 'app.js'), 'utf8');

// Extract the real escapeHtml implementation from app.js and run it in a
// fresh context, so the test pins the shipped function, not a copy.
const escapeHtmlMatch = appSource.match(/function escapeHtml\(text\) \{[\s\S]*?\n\}/);
assert.ok(escapeHtmlMatch, 'static/app.js must define escapeHtml');
const sandbox = {};
vm.createContext(sandbox);
vm.runInContext(escapeHtmlMatch[0], sandbox);
const escapeHtml = sandbox.escapeHtml;

assert.equal(escapeHtml(0), '0', 'numeric 0 must survive escaping');
assert.equal(escapeHtml(null), '', 'null must collapse to empty');
assert.equal(escapeHtml(undefined), '', 'undefined must collapse to empty');
assert.equal(escapeHtml(''), '', 'empty string stays empty');
assert.equal(escapeHtml(48000), '48000', 'sample rates stringify unchanged');
assert.equal(escapeHtml(false), 'false', 'booleans stringify, never collapse');
assert.equal(
    escapeHtml('<script>&"\''),
    '&lt;script&gt;&amp;&quot;&#39;',
    'all five characters are escaped (attribute-safe)'
);

// Sample-rate options: the option value interpolation must escape the rate
// (consistent with the other interpolated attribute values in the file).
const sampleRateLine = appSource
    .split('\n')
    .find((line) => line.includes('measurementConvolverSampleRate.innerHTML'));
assert.ok(sampleRateLine, 'app.js must render measurementConvolverSampleRate options');
assert.ok(
    /<option value="\$\{escapeHtml\(rate\)\}"\>/.test(sampleRateLine),
    'sample-rate option values must be escaped: ' + sampleRateLine.trim()
);
assert.ok(
    !/<option value="\$\{rate\}"\>/.test(sampleRateLine),
    'no raw rate interpolation may remain: ' + sampleRateLine.trim()
);

console.log('PASS scripts/test_escape_html_semantics.js');