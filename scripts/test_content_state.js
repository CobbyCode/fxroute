#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only
// Regression test for the shared content-state renderer (setContentState in
// static/app.js). Library, Radio and TIDAL browse all render Loading / Empty /
// Error into this one vocabulary; the test pins the class/display contract so
// no area can silently fall back to ad-hoc bare text again.

const assert = require('assert/strict');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const source = fs.readFileSync(path.join(__dirname, '..', 'static', 'app.js'), 'utf8');

function extractFunction(name) {
    const match = new RegExp(`function\\s+${name}\\s*\\(`).exec(source);
    assert.ok(match, `missing ${name}`);
    const brace = source.indexOf('{', match.index);
    assert.notEqual(brace, -1, `missing body ${name}`);
    let depth = 0, quote = '', escaped = false;
    for (let index = brace; index < source.length; index += 1) {
        const char = source[index];
        if (quote) {
            if (escaped) escaped = false;
            else if (char === '\\') escaped = true;
            else if (char === quote) quote = '';
            continue;
        }
        if (`'\"\``.includes(char)) quote = char;
        else if (char === '{') depth += 1;
        else if (char === '}' && --depth === 0) return source.slice(match.index, index + 1);
    }
    throw new Error(`unterminated ${name}`);
}

const sandbox = {};
vm.createContext(sandbox);
vm.runInContext(extractFunction('setContentState'), sandbox);
const setContentState = sandbox.setContentState;

function makeEl(initialClass) {
    const el = {
        _classes: new Set((initialClass || '').split(/\s+/).filter(Boolean)),
        classList: {
            add: (c) => el._classes.add(c),
            remove: (c) => el._classes.delete(c),
            contains: (c) => el._classes.has(c),
        },
        hidden: false,
        style: { display: '' },
        textContent: '',
        set className(v) {
            el._classes = new Set((v || '').split(/\s+/).filter(Boolean));
        },
        get className() {
            return Array.from(el._classes).join(' ');
        },
    };
    return el;
}

// Missing / null element must be a silent no-op.
setContentState(null, 'empty', 'x');
setContentState(undefined, 'loading', 'x');

// Loading: shows, swaps to the loading modifier, keeps the message.
{
    const el = makeEl('content-state content-state--loading');
    setContentState(el, 'loading', 'Loading…');
    assert.equal(el.className, 'content-state content-state--loading', 'loading class');
    assert.equal(el.textContent, 'Loading…', 'loading message');
    assert.equal(el.hidden, false, 'loading visible');
    assert.equal(el.classList.contains('hidden'), false, 'loading no hidden class');
}

// Empty: same base, empty modifier, message preserved.
{
    const el = makeEl('content-state content-state--loading');
    setContentState(el, 'empty', 'No favorites yet.');
    assert.equal(el.className, 'content-state content-state--empty', 'empty class');
    assert.equal(el.textContent, 'No favorites yet.', 'empty message');
    assert.equal(el.classList.contains('hidden'), false, 'empty visible');
}

// Error: uses the error modifier, message still rendered as plain text.
{
    const el = makeEl('content-state content-state--empty');
    setContentState(el, 'error', 'Backend is unavailable.');
    assert.equal(el.className, 'content-state content-state--error', 'error class');
    assert.equal(el.textContent, 'Backend is unavailable.', 'error message');
}

// Hide: re-adds the hidden class and clears the slot; state is unambiguous.
{
    const el = makeEl('content-state content-state--loading');
    setContentState(el, 'none', '');
    assert.equal(el.classList.contains('hidden'), true, 'hidden class on hide');
    assert.equal(el.textContent, '', 'text cleared on hide');
}

// A previously hidden element can be shown again (hidden class removed).
{
    const el = makeEl('content-state content-state--empty hidden');
    setContentState(el, 'empty', 'No playlists yet.');
    assert.equal(el.classList.contains('hidden'), false, 'hidden removed when shown');
    assert.equal(el.className, 'content-state content-state--empty', 'class reset on show');
}

// Undefined message renders an empty but present slot.
{
    const el = makeEl('');
    setContentState(el, 'empty', undefined);
    assert.equal(el.textContent, '', 'undefined message -> empty text');
    assert.equal(el.className, 'content-state content-state--empty', 'class set for empty message');
}

console.log(`PASS  scripts/test_content_state.js (${6} checks)`);
