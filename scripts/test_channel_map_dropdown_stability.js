#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only
//
// Settings -> Output Routing dropdown stability regression:
// the single hardware routing grid must only be rebuilt when its rendered
// options actually change and no routing select is being operated.

const assert = require('assert/strict');

const OutputState = require('../static/output_state.js');

const CHANNELS = 18;

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
    };
    return grid;
}

function catalog() {
    return {
        capabilities: {
            modes: ['stereo', 'stereo-sub'],
            roles: { 'stereo-sub': ['main_l', 'main_r', 'sub1', 'sub2'] },
        },
    };
}

function render(grid, assignments, disabled = false) {
    global.document = { activeElement: render.active || null };
    OutputState.renderRoutingGrid(grid, catalog(), 'stereo-sub', assignments, CHANNELS, disabled);
}

function main() {
    const assignments = Array.from({ length: CHANNELS }, (_, i) =>
        (['main_l', 'main_r', 'sub1', 'off'][i] || 'off'));
    {
        const grid = makeGrid();
        render.active = null;
        render(grid, assignments);
        assert.equal(grid.stats.writes, 1);
        assert.equal(grid.children.length, CHANNELS);
    }
    {
        const grid = makeGrid();
        render(grid, assignments);
        const before = grid.children;
        render(grid, assignments);
        render(grid, assignments);
        assert.equal(grid.stats.writes, 1);
        assert.equal(grid.children, before);
    }
    {
        const grid = makeGrid();
        render(grid, assignments);
        const focused = grid.children[0];
        render.active = focused;
        grid.contains = () => true;
        const changed = assignments.slice();
        changed[0] = 'sub1';
        render(grid, changed);
        assert.equal(grid.stats.writes, 1);
        assert.equal(grid.children[0], focused);
    }
    console.log('channel map dropdown stability tests: ok');
}

try {
    main();
} catch (error) {
    console.error(error);
    process.exit(1);
}
