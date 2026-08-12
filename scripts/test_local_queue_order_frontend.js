#!/usr/bin/env node
// The selected local queue keeps the supplied album/playlist order.

const assert = require('assert/strict');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const source = fs.readFileSync(path.join(__dirname, '..', 'static', 'app.js'), 'utf8');
const match = /function\s+getTrackIdsInLibraryOrder\s*\(/.exec(source);
assert.ok(match, 'missing getTrackIdsInLibraryOrder');
const brace = source.indexOf('{', match.index);
let depth = 0;
let end = -1;
for (let index = brace; index < source.length; index += 1) {
    if (source[index] === '{') depth += 1;
    else if (source[index] === '}' && --depth === 0) {
        end = index + 1;
        break;
    }
}
assert.notEqual(end, -1, 'unterminated getTrackIdsInLibraryOrder');

const sandbox = {
    state: {
        library: {
            tracks: ['a', 'b', 'c', 'd', 'e', 'f'].map(id => ({ id })),
        },
    },
};
vm.createContext(sandbox);
vm.runInContext(source.slice(match.index, end), sandbox);

assert.deepEqual(
    Array.from(sandbox.getTrackIdsInLibraryOrder(['d', 'a', 'b', 'c', 'e', 'f'])),
    ['d', 'a', 'b', 'c', 'e', 'f'],
);
assert.deepEqual(
    Array.from(sandbox.getTrackIdsInLibraryOrder(['a', 'missing', 'a', 'b'])),
    ['a', 'b'],
);
console.log('ok - local queue order is preserved');
