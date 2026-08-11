#!/usr/bin/env node
// Structured transition errors must remain readable in playback controls.

const assert = require('assert/strict');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const source = fs.readFileSync(path.join(__dirname, '..', 'static', 'app.js'), 'utf8');
const match = /function\s+formatTransitionErrorDetail\s*\(/.exec(source);
assert.ok(match, 'missing formatTransitionErrorDetail');
const brace = source.indexOf('{', match.index);
let depth = 0;
let end = -1;
for (let index = brace; index < source.length; index += 1) {
    if (source[index] === '{') depth += 1;
    if (source[index] === '}' && --depth === 0) {
        end = index + 1;
        break;
    }
}
assert.notEqual(end, -1, 'unterminated formatTransitionErrorDetail');

const sandbox = {};
vm.createContext(sandbox);
vm.runInContext(source.slice(match.index, end), sandbox);
const format = sandbox.formatTransitionErrorDetail;

assert.equal(format('Playback failed', 'fallback'), 'Playback failed');
assert.equal(
    format({ message: 'Playback failed', stage: 'target-source-start' }, 'fallback'),
    'Playback failed (stage: target-source-start)',
);
assert.equal(format({ message: 'Playback failed' }, 'fallback'), 'Playback failed');
assert.equal(format({}, 'fallback'), 'fallback');
console.log('ok — structured transition errors stay readable');
