#!/usr/bin/env node
// TIDAL playback uses the native FXRoute queue, so the global footer must
// expose the same shuffle/repeat modes as a local multi-track queue.

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
    let depth = 0;
    let quote = '';
    let escaped = false;
    for (let index = brace; index < source.length; index += 1) {
        const char = source[index];
        if (quote) {
            if (escaped) escaped = false;
            else if (char === '\\') escaped = true;
            else if (char === quote) quote = '';
            continue;
        }
        if (`'"\``.includes(char)) quote = char;
        else if (char === '{') depth += 1;
        else if (char === '}' && --depth === 0) return source.slice(match.index, index + 1);
    }
    throw new Error(`unterminated ${name}`);
}

function makeButton() {
    return {
        disabled: false,
        textContent: '',
        classList: {
            values: new Set(),
            add(...names) { names.forEach((name) => this.values.add(name)); },
            remove(...names) { names.forEach((name) => this.values.delete(name)); },
            toggle(name, force) {
                const enabled = force === undefined ? !this.values.has(name) : !!force;
                if (enabled) this.values.add(name);
                else this.values.delete(name);
                return enabled;
            },
            contains(name) { return this.values.has(name); },
        },
        setAttribute(name, value) { this[name] = value; },
    };
}

const sandbox = {
    elements: {
        footerShuffleBtn: makeButton(),
        footerLoopBtn: makeButton(),
        queueStatus: makeButton(),
        btnPrevious: makeButton(),
        btnNext: makeButton(),
        btnClearQueue: makeButton(),
    },
    state: {
        library: { shuffle: false, loop: false },
        playback: {
            current_track: { source: 'tidal', id: 'favorite-3' },
            queue: { count: 4, index: 2, shuffle: true, loop: true },
        },
    },
    window: { __footerSource: 'local' },
    playbackActionInFlight: false,
    libraryModeRequestInFlight: false,
    footerSingleTrackStartLockActive: () => false,
    isStreamingFooterSource: (source) => source === 'spotify' || source === 'qobuz',
    renderLibraryModeButtons: () => {},
};

vm.createContext(sandbox);
vm.runInContext(`${extractFunction('renderFooterModeButtons')}\n${extractFunction('renderQueueUI')}`, sandbox);
sandbox.renderQueueUI();
sandbox.renderFooterModeButtons();

assert.equal(sandbox.state.library.shuffle, true, 'TIDAL queue shuffle state must reach the footer');
assert.equal(sandbox.state.library.loop, true, 'TIDAL queue loop state must reach the footer');
assert.equal(sandbox.elements.footerShuffleBtn.classList.contains('hidden'), false,
    'Shuffle must be visible for a TIDAL multi-track queue');
assert.equal(sandbox.elements.footerLoopBtn.classList.contains('hidden'), false,
    'Repeat must be visible for a TIDAL queue');
assert.equal(sandbox.elements.footerShuffleBtn.disabled, false, 'TIDAL Shuffle must be enabled');
assert.equal(sandbox.elements.footerLoopBtn.disabled, false, 'TIDAL Repeat must be enabled');

console.log('PASS  scripts/test_tidal_footer_modes.js');
