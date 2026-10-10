#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only
//
// renderChipRow (static/ui_helpers.js) draws the speaker-choice chips of the
// sweep menu and of the Advanced dialog: hidden below two chips, rebuilt only
// when a chip changes, otherwise updated in place.

const assert = require('assert/strict');
const { renderChipRow } = require('../static/ui_helpers.js');

function classList() {
    const classes = new Set();
    return { add: c => classes.add(c), remove: c => classes.delete(c), contains: c => classes.has(c),
        toggle: (c, on) => (on ? classes.add(c) : classes.delete(c)) };
}

// A row whose buttons are parsed from the markup it was given, so a rebuild
// (new button objects) differs from an in-place update.
function fakeRow() {
    const row = { classList: classList(), dataset: {}, buttons: [], html: '',
        get innerHTML() { return this.html; },
        set innerHTML(value) {
            this.html = value;
            this.buttons = [...value.matchAll(/<button ([^>]*)>([^<]*)<\/button>/g)].map((match) => {
                const attrs = Object.fromEntries([...match[1].matchAll(/([\w-]+)="([^"]*)"/g)].map(a => [a[1], a[2]]));
                return { attrs, textContent: match[2], classList: classList(), disabled: false,
                    getAttribute: name => attrs[name], setAttribute: (name, v) => { attrs[name] = String(v); } };
            });
        },
        querySelectorAll(selector) {
            const attribute = /^\[([\w-]+)\]$/.exec(selector)[1];
            return this.buttons.filter(button => attribute in button.attrs);
        } };
    return row;
}

const CHIPS = [
    { id: 'front', short: 'Front', label: 'Front L/R + Sub' },
    { id: 'rear', short: 'Rear', label: 'Rear L/R + Sub' },
];
const OPTIONS = { attribute: 'data-pick', selected: 'rear' };

// Chips carry the id attribute, the label as name and tooltip, and the selection.
{
    const row = fakeRow();
    assert.equal(renderChipRow(row, CHIPS, OPTIONS), true);
    assert.equal(row.classList.contains('hidden'), false);
    assert.deepEqual(row.buttons.map(b => [b.attrs['data-pick'], b.textContent, b.attrs['aria-label'],
        b.attrs['data-tooltip'], b.attrs['aria-pressed'], b.classList.contains('is-active'), b.disabled]), [
        ['front', 'Front', 'Front L/R + Sub', 'Front L/R + Sub', 'false', false, false],
        ['rear', 'Rear', 'Rear L/R + Sub', 'Rear L/R + Sub', 'true', true, false]]);
}

// A selection change and the disabled flag update the same buttons.
{
    const row = fakeRow();
    renderChipRow(row, CHIPS, OPTIONS);
    const before = [...row.buttons];
    renderChipRow(row, CHIPS, { attribute: 'data-pick', selected: 'front', disabled: true });
    row.buttons.forEach((button, index) => assert.equal(button, before[index], 'same button objects'));
    assert.deepEqual(row.buttons.map(b => [b.classList.contains('is-active'), b.disabled]), [[true, true], [false, true]]);
}

// A changed label under the same ids rebuilds, so a tooltip never goes stale.
{
    const row = fakeRow();
    renderChipRow(row, CHIPS, OPTIONS);
    const before = [...row.buttons];
    renderChipRow(row, [{ ...CHIPS[0], label: 'Front L/R' }, CHIPS[1]], OPTIONS);
    assert.equal(row.buttons[0].attrs['data-tooltip'], 'Front L/R');
    assert.notEqual(row.buttons[0], before[0]);
}

// Nothing to choose: hidden and empty, also when chips existed before.
for (const chips of [[], [CHIPS[0]], null]) {
    const row = fakeRow();
    renderChipRow(row, CHIPS, OPTIONS);
    assert.equal(renderChipRow(row, chips, OPTIONS), false);
    assert.equal(row.classList.contains('hidden'), true);
    assert.equal(row.innerHTML, '');
    assert.deepEqual(row.buttons, []);
}

// Labels are escaped inside the attributes, and a missing row is ignored.
{
    const row = fakeRow();
    renderChipRow(row, [{ id: 'a"b', short: '<A>', label: 'x" onclick="y' }, CHIPS[1]], OPTIONS);
    assert.doesNotMatch(row.innerHTML, /<A>|onclick="y/);
    assert.equal(renderChipRow(null, CHIPS, OPTIONS), false);
}

console.log('ui helpers chip row: ok');
