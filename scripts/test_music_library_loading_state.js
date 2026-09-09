#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only
// Music Library selector must never present a bare "Local" option as a
// finished result while discovery is still in flight (cold backend scan
// takes seconds on multi-host LANs). It shows a disabled loading state
// until the response lands, then the real list.
//
// Executes the VERBATIM musicLibrarySelectModel extracted from
// static/app.js (no reimplementation).

const assert = require('assert/strict');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const src = fs.readFileSync(path.join(__dirname, '..', 'static', 'app.js'), 'utf8');

function extractFunction(source, name) {
    const match = new RegExp(`function\\s+${name}\\s*\\(`).exec(source);
    assert.ok(match, `missing ${name}`);
    const paren = source.indexOf('(', match.index);
    let pdepth = 0, quote = '', escaped = false, i = paren;
    for (; i < source.length; i += 1) {
        const c = source[i];
        if (quote) {
            if (escaped) escaped = false;
            else if (c === '\\') escaped = true;
            else if (c === quote) quote = '';
            continue;
        }
        if ('\'"`'.includes(c)) quote = c;
        else if (c === '(') pdepth += 1;
        else if (c === ')' && --pdepth === 0) break;
    }
    const brace = source.indexOf('{', i);
    let depth = 0;
    quote = ''; escaped = false;
    for (let j = brace; j < source.length; j += 1) {
        const c = source[j];
        if (quote) {
            if (escaped) escaped = false;
            else if (c === '\\') escaped = true;
            else if (c === quote) quote = '';
            continue;
        }
        if ('\'"`'.includes(c)) quote = c;
        else if (c === '{') depth += 1;
        else if (c === '}' && --depth === 0) return source.slice(match.index, j + 1);
    }
    throw new Error(`unterminated ${name}`);
}

// Structural guard: the fetch path must mark loading before the request
// and clear it on both outcomes, otherwise the model above never engages.
const fetchSrc = extractFunction(src, 'fetchMusicLibraries');
assert.ok(fetchSrc.includes('loading: true'), 'fetch must set loading before the request');
assert.ok(fetchSrc.includes('loading: false'), 'fetch must clear loading after the response');

const fns = extractFunction(src, 'musicLibrarySelectModel');

function model(input) {
    const sandbox = {};
    sandbox.globalThis = sandbox;
    vm.createContext(sandbox);
    // escapeHtml is incidental to the select-model under test (plain labels
    // below); stub it so the brace scanner never has to parse regex literals.
    vm.runInContext(`function escapeHtml(text) { return String(text ?? ''); }\n${fns}\nthis.__out = musicLibrarySelectModel(${JSON.stringify(input)});`, sandbox);
    return vm.runInContext('__out', sandbox);
}

const LIBS = [
    { id: 'local', label: 'Local — Music' },
    { id: 'smb:192.168.178.100:Music-Demo', label: 'SMB — 192 / Music-Demo' },
    { id: 'manual', label: 'Add network share manually…' },
];

let failures = 0;
function check(name, fn) {
    try {
        fn();
        console.log(`ok  ${name}`);
    } catch (e) {
        failures += 1;
        console.log(`FAIL  ${name}: ${e.message}`);
    }
}

check('loading without cache shows disabled placeholder, never bare Local', () => {
    const m = model({ active_id: 'local', libraries: [], pending: false, loading: true });
    assert.match(m.html, /Discovering network shares/);
    assert.equal(m.value, '');
    assert.equal(m.disabled, true);
    assert.doesNotMatch(m.html, /value="local"/);
});

check('loaded list renders local, smb and manual, select enabled', () => {
    const m = model({ active_id: 'local', libraries: LIBS, pending: false, loading: false });
    assert.match(m.html, /Music-Demo/);
    assert.match(m.html, /manually/);
    assert.equal(m.value, 'local');
    assert.equal(m.disabled, false);
});

check('loading with cached list keeps the list, select disabled', () => {
    const m = model({ active_id: 'local', libraries: LIBS, pending: false, loading: true });
    assert.match(m.html, /Music-Demo/);
    assert.equal(m.disabled, true);
});

check('idle without cache keeps legacy Local fallback, select enabled', () => {
    const m = model({ active_id: 'local', libraries: [], pending: false, loading: false });
    assert.match(m.html, /value="local"/);
    assert.equal(m.disabled, false);
});

check('pending selection disables the select', () => {
    const m = model({ active_id: 'local', libraries: LIBS, pending: true, loading: false });
    assert.equal(m.disabled, true);
});

check('scanning without smb entry shows disabled scanning placeholder', () => {
    const m = model({
        active_id: 'local',
        libraries: [
            { id: 'local', type: 'local', label: 'Local — Music' },
            { id: 'manual', type: 'action', label: 'Add network share manually…' },
        ],
        pending: false,
        loading: false,
        scanning: true,
    });
    assert.match(m.html, /Scanning network shares/);
    assert.equal(m.value, '');
    assert.equal(m.disabled, true);
    assert.doesNotMatch(m.html, /value="local"/);
});

check('scanning with cached smb entry keeps the list visible', () => {
    const m = model({
        active_id: 'local',
        libraries: [
            { id: 'local', type: 'local', label: 'Local — Music' },
            { id: 'smb:192.168.178.100:Music-Demo', type: 'smb', label: 'SMB — 192 / Music-Demo' },
            { id: 'manual', type: 'action', label: 'Add network share manually…' },
        ],
        pending: false,
        loading: false,
        scanning: true,
    });
    assert.match(m.html, /Music-Demo/);
    assert.equal(m.value, 'local');
    assert.equal(m.disabled, false);
});

if (failures) {
    console.error(`${failures} case(s) failed`);
    process.exit(1);
}
console.log('music library loading state: ok');
