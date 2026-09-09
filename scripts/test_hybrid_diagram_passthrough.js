#!/usr/bin/env node
'use strict';

// Contract: the app.js renderHybridRoomDiagram wrapper must pass its step,
// mode and complete arguments through to MeasurementFlows unchanged.
// Regression: the wrapper used to call
//   MeasurementFlows.renderHybridRoomDiagram(step = {}, mode = 'stereo', complete = false)
// reassigning step to {} in the call, so position/channel never reached the
// renderer and no diagram highlight was ever set via the app.js path.
//
// The test executes the real wrapper source extracted from static/app.js
// (balanced-brace scan, no reimplementation) against a stubbed
// MeasurementFlows and asserts the renderer receives the exact arguments.

const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const root = path.resolve(__dirname, '..');
const appSource = fs.readFileSync(path.join(root, 'static/app.js'), 'utf8');

function extractGlobalFunction(source, name) {
    const marker = `\nfunction ${name}(`;
    const start = source.indexOf(marker);
    assert.notStrictEqual(start, -1, `wrapper ${name} must exist in static/app.js`);
    // Skip the parameter list (it may contain braces in defaults like `step = {}`).
    let i = source.indexOf('(', start);
    let parens = 0;
    let quote = null;
    for (; i < source.length; i++) {
        const ch = source[i];
        if (quote) {
            if (ch === '\\') i++;
            else if (ch === quote) quote = null;
            continue;
        }
        if (ch === '"' || ch === "'" || ch === '`') quote = ch;
        else if (ch === '(') parens++;
        else if (ch === ')') {
            parens--;
            if (parens === 0) break;
        }
    }
    i = source.indexOf('{', i);
    let depth = 0;
    quote = null;
    let lineComment = false;
    let blockComment = false;
    for (; i < source.length; i++) {
        const ch = source[i];
        const next = source[i + 1];
        if (lineComment) {
            if (ch === '\n') lineComment = false;
            continue;
        }
        if (blockComment) {
            if (ch === '*' && next === '/') {
                blockComment = false;
                i++;
            }
            continue;
        }
        if (quote) {
            if (ch === '\\') {
                i++;
                continue;
            }
            if (ch === quote) quote = null;
            continue;
        }
        if (ch === '/' && next === '/') {
            lineComment = true;
            i++;
            continue;
        }
        if (ch === '/' && next === '*') {
            blockComment = true;
            i++;
            continue;
        }
        if (ch === '"' || ch === "'" || ch === '`') {
            quote = ch;
            continue;
        }
        if (ch === '{') depth++;
        if (ch === '}') {
            depth--;
            if (depth === 0) return source.slice(start + 1, i + 1);
        }
    }
    throw new Error(`unbalanced braces in wrapper ${name}`);
}

const wrapperSource = extractGlobalFunction(appSource, 'renderHybridRoomDiagram');
assert(
    wrapperSource.includes('MeasurementFlows.renderHybridRoomDiagram'),
    'wrapper must delegate to MeasurementFlows.renderHybridRoomDiagram'
);

const calls = [];
const sandbox = {
    MeasurementFlows: {
        renderHybridRoomDiagram(...args) {
            calls.push(args);
            return 'rendered';
        },
    },
};
vm.createContext(sandbox);
vm.runInContext(wrapperSource, sandbox);

const step = { position: 'secondary-left', channel: 'left', role: 'secondary' };
const returned = vm.runInContext(
    `renderHybridRoomDiagram(__step, 'subwoofer-2.1', true)`,
    Object.assign(sandbox, { __step: step })
);
assert.strictEqual(returned, 'rendered');
assert.strictEqual(calls.length, 1, 'renderer must be called exactly once');
const [gotStep, gotMode, gotComplete] = calls[0];
// Cross-realm comparison: compare serialized content, not prototypes.
assert.strictEqual(
    JSON.stringify(gotStep),
    JSON.stringify(step),
    `step must arrive unchanged, got ${JSON.stringify(gotStep)}`
);
assert.strictEqual(gotStep.position, 'secondary-left', 'position must survive the wrapper');
assert.strictEqual(gotStep.channel, 'left', 'channel must survive the wrapper');
assert.strictEqual(gotMode, 'subwoofer-2.1');
assert.strictEqual(gotComplete, true);

// Defaults still apply when the caller passes nothing.
calls.length = 0;
vm.runInContext('renderHybridRoomDiagram()', sandbox);
assert.strictEqual(calls.length, 1);
assert.strictEqual(JSON.stringify(calls[0][0]), '{}');
assert.strictEqual(calls[0][1], 'stereo');
assert.strictEqual(calls[0][2], false);

console.log('hybrid diagram wrapper passthrough: ok');
