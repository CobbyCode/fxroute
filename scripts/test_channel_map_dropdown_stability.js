#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only
//
// Settings -> Channel Map dropdown stability regression:
// renderSettingsPanel() rebuilt the routing grid with innerHTML on every call.
// While the settings dialog is open a 2.5 s status poll (fetchAudioSourceOverview
// + fetchHardwareStatus) re-renders it, so replacing the <select> nodes closed an
// open native dropdown within one poll interval and the focused select lost
// focus, leaving a selection possible only by clicking very fast.
//
// The grid must only be rebuilt when its rendered options actually change and no
// routing select is being operated; the deferred state then applies after the
// user leaves the select. This test extracts the real block from app.js and
// pins that behaviour, including a real selection reaching the save path.

const assert = require('assert/strict');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const repoRoot = path.resolve(__dirname, '..');
const appSource = fs.readFileSync(path.join(repoRoot, 'static', 'app.js'), 'utf8');

const SIGNALS = [
    { id: 0, label: 'Off' },
    { id: 1, label: 'Main L' },
    { id: 2, label: 'Main R' },
    { id: 3, label: 'Sub 1' },
    { id: 4, label: 'Sub 2' },
];
const CHANNELS = 18;

// Extract the Channel Map render block verbatim: balance braces from the grid
// guard, ignoring strings, template literals and line comments.
function extractRoutingBlock() {
    const marker = 'if (routingAvailable && elements.settingsRoutingGrid) {';
    const start = appSource.indexOf(marker);
    assert.notEqual(start, -1, 'missing Channel Map routing grid render block');
    let depth = 0;
    let quote = '';
    let escaped = false;
    let lineComment = false;
    for (let index = start; index < appSource.length; index += 1) {
        const char = appSource[index];
        const next = appSource[index + 1];
        if (lineComment) {
            if (char === '\n') lineComment = false;
            continue;
        }
        if (quote) {
            if (escaped) escaped = false;
            else if (char === '\\') escaped = true;
            else if (char === quote) quote = '';
            continue;
        }
        if (char === '/' && next === '/') {
            lineComment = true;
            index += 1;
            continue;
        }
        if (char === "'" || char === '"' || char === '`') quote = char;
        else if (char === '{') depth += 1;
        else if (char === '}') {
            depth -= 1;
            if (depth === 0) return appSource.slice(start, index + 1);
        }
    }
    throw new Error('unterminated Channel Map routing grid block');
}

const ROUTING_BLOCK = extractRoutingBlock();

// Minimal grid stub: innerHTML assignment recreates the cell selects, exactly
// like the browser does, so node identity reveals a rebuild.
function makeGrid() {
    const grid = {
        stats: { writes: 0 },
        dataset: {},
        children: [],
        get innerHTML() {
            return grid.stats.html;
        },
        set innerHTML(value) {
            grid.stats.writes += 1;
            grid.stats.html = value;
            const cells = value.split('<div class="settings-routing-cell">').slice(1);
            grid.children = cells.map((cell, index) => ({
                tagName: 'SELECT',
                id: `settings-routing-out-${index + 1}`,
                dataset: { routingOutput: String(index) },
                value: '',
                disabled: / disabled>/.test(cell),
            }));
        },
        contains(node) {
            return grid.children.includes(node);
        },
        querySelectorAll() {
            return grid.children.slice();
        },
    };
    return grid;
}

function makeEnv(assignments = Array.from({ length: CHANNELS }, (_, i) => (i < 4 ? i + 1 : 0))) {
    const grid = makeGrid();
    const context = {
        elements: { settingsRoutingGrid: grid },
        document: { activeElement: null },
        routingAvailable: true,
        outputRouting: { signals: SIGNALS, assignments },
        _audioRoutingInProgress: false,
        _audioOutputModeSwitchInProgress: false,
        escapeHtml: (value) => String(value),
        body: { tagName: 'BODY' },
    };
    vm.createContext(context);
    // Function scope so the block's const bindings can be re-executed.
    const render = () => vm.runInContext(`(function () {\n${ROUTING_BLOCK}\n})()`, context);
    return { context, grid, render, assignments };
}

function main() {
    // 1. First render builds the full matrix: one select per hardware output.
    {
        const env = makeEnv();
        env.render();
        assert.equal(env.grid.stats.writes, 1, 'first render must build the grid');
        assert.equal(env.grid.children.length, CHANNELS, 'one dropdown per output');
        assert.deepEqual(
            env.grid.children.slice(0, 6).map((sel) => sel.value),
            ['1', '2', '3', '4', '0', '0'],
            'saved assignments are shown',
        );
    }

    // 2. Unchanged state must not touch the DOM: this is what let the periodic
    //    settings refresh destroy an open dropdown and drop its focus.
    {
        const env = makeEnv();
        env.render();
        const before = env.grid.children;
        env.render();
        env.render();
        assert.equal(env.grid.stats.writes, 1, 'unchanged state must not rebuild the grid');
        assert.equal(env.grid.children, before, 'dropdown nodes must keep their identity');
    }

    // 3. A state change while a dropdown is open is deferred: the user keeps the
    //    open select (and any pending choice) until they leave it.
    {
        const env = makeEnv();
        env.render();
        const focused = env.grid.children[0];
        env.context.document.activeElement = focused;
        env.assignments[0] = 3;
        env.render();
        assert.equal(env.grid.stats.writes, 1, 'must not rebuild while a routing select is operated');
        assert.equal(env.grid.children[0], focused, 'the open dropdown must survive the refresh');
        assert.equal(env.context.document.activeElement, focused, 'focus must stay on the dropdown');
    }

    // 4. Every Channel Map dropdown is protected, not just the first.
    {
        for (const index of [0, 5, 11, 17]) {
            const env = makeEnv();
            env.render();
            const focused = env.grid.children[index];
            env.context.document.activeElement = focused;
            env.assignments[index] = 4;
            env.render();
            assert.equal(env.grid.stats.writes, 1, `output ${index + 1} must keep its open dropdown`);
            assert.equal(env.grid.children[index], focused, `output ${index + 1} node must survive`);
        }
    }

    // 5. After the user leaves the select the deferred state is applied.
    {
        const env = makeEnv();
        env.render();
        env.context.document.activeElement = env.grid.children[0];
        env.assignments[0] = 3;
        env.render();
        assert.equal(env.grid.stats.writes, 1, 'still deferred while focused');
        env.context.document.activeElement = env.context.body;
        env.render();
        assert.equal(env.grid.stats.writes, 2, 'state applies once the select is left');
        assert.equal(env.grid.children[0].value, '3', 'the saved assignment is rendered');
    }

    // 6. Changes that are not attributable to interaction still rebuild, and the
    //    in-flight flag still disables the matrix.
    {
        const env = makeEnv();
        env.render();
        env.assignments[2] = 0;
        env.render();
        assert.equal(env.grid.stats.writes, 2, 'a real matrix change must re-render');
        assert.equal(env.grid.children[2].value, '0', 'the changed assignment is rendered');
        env.context._audioRoutingInProgress = true;
        env.render();
        assert.equal(env.grid.stats.writes, 3, 'the busy flag re-renders the matrix');
        assert.ok(env.grid.children.every((sel) => sel.disabled), 'saving disables the dropdowns');
    }

    console.log('channel map dropdown stability tests: ok');
}

try {
    main();
} catch (error) {
    console.error(error);
    process.exit(1);
}
