#!/usr/bin/env node
'use strict';

// Contract: the room diagram renderer lives exclusively in
// static/measurement_flows.js. The former app.js renderHybridRoomDiagram
// wrapper is gone (it once reassigned step to {} in the call, so
// position/channel never reached the renderer); with no wrapper left the
// step/mode/complete arguments cannot be dropped or reassigned on the way.

const assert = require('assert');
const fs = require('fs');
const path = require('path');

const root = path.resolve(__dirname, '..');
const appSource = fs.readFileSync(path.join(root, 'static/app.js'), 'utf8');
const flowsSource = fs.readFileSync(path.join(root, 'static/measurement_flows.js'), 'utf8');
const indexSource = fs.readFileSync(path.join(root, 'static/index.html'), 'utf8');

assert.doesNotMatch(
    appSource,
    /\n(?:async function|function) renderHybridRoomDiagram\(/,
    'no app.js wrapper: the renderer lives in measurement_flows.js'
);
assert.match(
    flowsSource,
    /function renderHybridRoomDiagram\(step = \{\}, mode = 'stereo', complete = false\) \{/,
    'canonical renderer keeps the (step, mode, complete) signature and defaults'
);
assert.match(
    flowsSource,
    /renderHybridRoomDiagram,/,
    'canonical renderer is exported via FXRouteMeasurementFlows'
);
assert.ok(
    indexSource.indexOf('measurement_flows.js?v=') < indexSource.indexOf('app.js?v='),
    'flows module must load before app'
);

console.log('hybrid diagram wrapper passthrough: ok');
