#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only
// Footer loop button follows the active local queue.
//
// Regression: after closing the active library/TIDAL queue via X, the loop
// symbol stayed visible although no queue was active anymore. While a queue
// is active it must stay visible. Radio/Spotify rendering is untouched.
//
// This executes the VERBATIM render function extracted from static/app.js
// (no reimplementation) against stub footer buttons.

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

// --- structural guard: loop visibility must require an active queue -------
assert.ok(
    /const showLoop = nativeQueueActive && hasActiveQueue;/.test(src),
    'loop button must be gated on an active local queue like shuffle',
);

function makeButton() {
    const toggled = new Map();
    return {
        classList: { toggle: (name, force) => toggled.set(name, !!force) },
        setAttribute: () => {},
        hidden: (name) => toggled.get(name) === true,
        set disabled(_) {},
        set title(_) {},
        set textContent(_) {},
    };
}

function renderWith({ track, queue }) {
    const shuffleBtn = makeButton();
    const loopBtn = makeButton();
    const sandbox = {
        window: { __footerSource: 'local' },
        state: {
            playback: { current_track: track, queue },
            library: { shuffle: false, loop: false },
        },
        libraryModeRequestInFlight: false,
        isStreamingFooterSource: () => false,
        streamingFooterData: () => ({}),
        elements: { footerShuffleBtn: shuffleBtn, footerLoopBtn: loopBtn },
    };
    vm.createContext(sandbox);
    vm.runInContext(
        `${extractFunction(src, 'renderFooterModeButtons')}\nrenderFooterModeButtons();`,
        sandbox,
    );
    return { shuffleBtn, loopBtn };
}

const localTrack = { id: 'a', source: 'local' };
const tidalTrack = { id: 't1', source: 'tidal' };

// --- 1. active local queue: loop (and shuffle) stay visible -----------------
{
    const { shuffleBtn, loopBtn } = renderWith({
        track: localTrack,
        queue: { count: 12, shuffle: false, loop: false },
    });
    assert.equal(loopBtn.hidden('hidden'), false, 'loop must show while a queue is active');
    assert.equal(shuffleBtn.hidden('hidden'), false, 'shuffle must show while a queue is active');
}

// --- 2. closed queue: loop disappears like shuffle --------------------------
{
    const { shuffleBtn, loopBtn } = renderWith({
        track: localTrack,
        queue: { count: 0, shuffle: false, loop: false },
    });
    assert.equal(loopBtn.hidden('hidden'), true, 'loop must hide once no queue is active');
    assert.equal(shuffleBtn.hidden('hidden'), true, 'shuffle must hide once no queue is active');
}

// --- 3. same contract for TIDAL queues --------------------------------------
{
    const { loopBtn } = renderWith({
        track: tidalTrack,
        queue: { count: 5, shuffle: false, loop: false },
    });
    assert.equal(loopBtn.hidden('hidden'), false, 'loop must show while a TIDAL queue is active');
    const closed = renderWith({
        track: tidalTrack,
        queue: { count: 0, shuffle: false, loop: false },
    });
    assert.equal(closed.loopBtn.hidden('hidden'), true, 'loop must hide once no TIDAL queue is active');
}

console.log('PASS  scripts/test_footer_queue_buttons.js (3 visibility cases)');
