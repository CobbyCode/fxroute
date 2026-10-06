// SPDX-License-Identifier: AGPL-3.0-only
// Number-field policy of the Tone, Crossover and Subwoofer steppers: value
// fields hold two decimals at most, whole-number fields (Center, Surround,
// Loudness Strength) no decimals, and minus/plus step by the field's own step
// without float residue. Pure helpers from ui_helpers.js, the nudge and the
// capture-phase document listeners from output_effects_ui.js.
'use strict';

const assert = require('assert');
const fs = require('fs');
const path = require('path');

globalThis.Element = class Element {};
const helpers = require('../static/ui_helpers.js');
globalThis.FXRouteUiHelpers = helpers;
const effects = require('../static/output_effects_ui.js');

// ---- helpers --------------------------------------------------------------
const field = (attrs = {}) => ({ getAttribute: (name) => (name in attrs ? attrs[name] : null) });
assert.equal(helpers.numberFieldDecimals(field()), 2, 'default is two decimals');
assert.equal(helpers.numberFieldDecimals(field({ 'data-decimals': '0' })), 0);
assert.equal(helpers.numberFieldDecimals(field({ 'data-decimals': '1' })), 1);
assert.equal(helpers.numberFieldDecimals(field({ 'data-decimals': '5' })), 2, 'never more than two');
assert.equal(helpers.numberFieldDecimals(field({ 'data-decimals': 'x' })), 2);
assert.equal(helpers.numberFieldDecimals(field({ 'data-decimals': '' })), 2);
assert.equal(helpers.numberFieldDecimals(null), 2);

assert.equal(helpers.limitDecimalText('1.2345', 2), '1.23');
assert.equal(helpers.limitDecimalText('-4.47049', 2), '-4.47');
assert.equal(helpers.limitDecimalText('1.2', 2), '1.2', 'shorter text is kept as typed');
assert.equal(helpers.limitDecimalText('1.20', 2), '1.20');
assert.equal(helpers.limitDecimalText('12', 2), '12');
assert.equal(helpers.limitDecimalText('3.5', 0), '3');
assert.equal(helpers.limitDecimalText('3.', 0), '3');
assert.equal(helpers.limitDecimalText('7', 0), '7');
assert.equal(helpers.limitDecimalText('', 2), '');

assert.equal(helpers.roundToDecimals(1.005, 2), 1.01, 'binary 1.005 still rounds half up');
assert.equal(helpers.roundToDecimals(-1.005, 2), -1.01);
assert.equal(helpers.roundToDecimals(7.8125, 2), 7.81);
assert.equal(helpers.roundToDecimals(0.1 + 0.2, 2), 0.3);
assert.equal(helpers.roundToDecimals(0.7 + 0.1, 2), 0.8);
assert.equal(helpers.roundToDecimals(2.5, 0), 3);
assert.equal(helpers.roundToDecimals(-2.5, 0), -3);
assert.equal(helpers.roundToDecimals(-0.001, 2), 0);
assert.ok(Object.is(helpers.roundToDecimals(-0.001, 2), 0), 'no negative zero');
assert.equal(helpers.roundToDecimals(1e-7, 2), 0, 'exponent notation input');
assert.equal(helpers.roundToDecimals('4.4', 2), 4.4);
assert.ok(Number.isNaN(helpers.roundToDecimals('x', 2)));

// ---- nudge ----------------------------------------------------------------
function stepper({ value, step = '1', min = '', max = '', decimals = null, disabled = false }) {
    const events = [];
    const input = {
        value,
        step,
        min,
        max,
        disabled,
        events,
        getAttribute: (name) => (name === 'data-decimals' ? decimals : null),
        focus() { events.push('focus'); },
        dispatchEvent(event) { events.push(event.type); return true; },
    };
    const control = { querySelector: () => input };
    const button = (direction) => ({ dataset: { stepper: direction }, closest: () => control });
    return { input, dec: button('dec'), inc: button('inc') };
}

{
    const s = stepper({ value: '0.7', step: '0.1', min: '-40', max: '40' });
    effects.nudgeStepperControl(s.inc);
    assert.equal(s.input.value, '0.8', '0.7 + 0.1 carries no float residue');
    assert.deepEqual(s.input.events, ['focus', 'input', 'change']);
    effects.nudgeStepperControl(s.dec);
    effects.nudgeStepperControl(s.dec);
    assert.equal(s.input.value, '0.6');
}
{
    // A value carrying more places than allowed (script, old data) steps to a two-decimal result.
    const s = stepper({ value: '-4.47049', step: '0.1', min: '-80', max: '24' });
    effects.nudgeStepperControl(s.inc);
    assert.equal(s.input.value, '-4.37');
    const t = stepper({ value: '7.8125', step: '0.1', min: '-40', max: '40' });
    effects.nudgeStepperControl(t.dec);
    assert.equal(t.input.value, '7.71');
}
{
    // Minus and plus are inverse steps at any two-decimal value.
    for (const start of ['1.23', '-0.07', '0.30', '39.5', '-39.5']) {
        const s = stepper({ value: start, step: '0.1', min: '-40', max: '40' });
        effects.nudgeStepperControl(s.inc);
        effects.nudgeStepperControl(s.dec);
        assert.equal(Number(s.input.value), Number(start), `${start} + 0.1 - 0.1`);
    }
}
{
    const s = stepper({ value: '7', step: '1', min: '0', max: '10', decimals: '0' });
    effects.nudgeStepperControl(s.inc);
    assert.equal(s.input.value, '8');
    const half = stepper({ value: '7', step: '0.5', min: '-20', max: '20' });
    effects.nudgeStepperControl(half.inc);
    assert.equal(half.input.value, '7.5');
    effects.nudgeStepperControl(half.dec);
    effects.nudgeStepperControl(half.dec);
    assert.equal(half.input.value, '6.5');
}
{
    // The bounds stop the step without an event; an empty or disabled field does nothing.
    const top = stepper({ value: '10', step: '1', min: '0', max: '10', decimals: '0' });
    effects.nudgeStepperControl(top.inc);
    assert.equal(top.input.value, '10');
    assert.deepEqual(top.input.events, []);
    const bottom = stepper({ value: '0', step: '1', min: '0', max: '10', decimals: '0' });
    effects.nudgeStepperControl(bottom.dec);
    assert.deepEqual(bottom.input.events, []);
    const floorValue = stepper({ value: '-79.95', step: '0.1', min: '-80', max: '24' });
    effects.nudgeStepperControl(floorValue.dec);
    assert.equal(floorValue.input.value, '-80', 'clamped to the minimum');
    const empty = stepper({ value: '', step: '1' });
    effects.nudgeStepperControl(empty.inc);
    assert.deepEqual(empty.input.events, []);
    const off = stepper({ value: '5', step: '1', disabled: true });
    effects.nudgeStepperControl(off.inc);
    assert.deepEqual(off.input.events, []);
}

// ---- document listeners ---------------------------------------------------
const handlers = {};
globalThis.document = {
    addEventListener(type, handler, capture) {
        assert.equal(capture, true, `${type} listens in the capture phase`);
        handlers[type] = handler;
    },
};
effects.wireNumberFieldLimits();
assert.deepEqual(Object.keys(handlers).sort(), ['beforeinput', 'change', 'input']);

class Field extends Element {
    constructor(value, { decimals = null, min = '' } = {}) {
        super();
        this.value = value;
        this.min = min;
        this.decimals = decimals;
    }
    matches(selector) { return selector === 'input.stepper-input'; }
    getAttribute(name) { return name === 'data-decimals' ? this.decimals : null; }
}
function typed(target, data) {
    let prevented = false;
    handlers.beforeinput({ target, data, preventDefault() { prevented = true; } });
    return prevented;
}

{
    const decimal = new Field('', { min: '-20' });
    const whole = new Field('', { decimals: '0', min: '0' });
    assert.equal(typed(decimal, '5'), false);
    assert.equal(typed(decimal, '.'), false, 'a decimal field takes the point');
    assert.equal(typed(decimal, '-'), false, 'a field with a negative range takes the sign');
    assert.equal(typed(decimal, 'e'), true, 'no exponent');
    assert.equal(typed(decimal, 'E'), true);
    assert.equal(typed(decimal, '+'), true);
    assert.equal(typed(whole, '-'), true, 'a 0..10 field takes no sign');
    assert.equal(typed(whole, '3'), false);
    assert.equal(typed(whole, '1e1'), true);
    const other = { matches: () => false };
    Object.setPrototypeOf(other, Element.prototype);
    assert.equal(typed(other, 'e'), false, 'other inputs are left alone');
    assert.equal(typed(decimal, null), false, 'deletions and pastes carry no data');
}
{
    const cut = (value, options) => {
        const target = new Field(value, options);
        handlers.input({ target });
        return target.value;
    };
    assert.equal(cut('-1.2345'), '-1.23');
    assert.equal(cut('7.8125'), '7.81');
    assert.equal(cut('1.2'), '1.2');
    assert.equal(cut('3000.125', {}), '3000.12');
    assert.equal(cut('2.75', { decimals: '0' }), '2');
    assert.equal(cut('7', { decimals: '0' }), '7');
    assert.equal(cut('', { decimals: '0' }), '');
    handlers.input({ target: { value: '1.2345' } });
}
{
    const settle = (value, options) => {
        const target = new Field(value, options);
        handlers.change({ target });
        return target.value;
    };
    assert.equal(settle('1.239'), '1.24');
    assert.equal(settle('-4.47049'), '-4.47');
    assert.equal(settle('1.5'), '1.5');
    assert.equal(settle('3.5', { decimals: '0' }), '4');
    assert.equal(settle(''), '');
}

// ---- the markup marks exactly the whole-number fields -----------------------
const html = fs.readFileSync(path.join(__dirname, '..', 'static', 'index.html'), 'utf8');
const inputs = [...html.matchAll(/<input[^>]*class="[^"]*\bstepper-input\b[^"]*"[^>]*>/g)].map((m) => m[0]);
assert.ok(inputs.length >= 13, `stepper inputs found: ${inputs.length}`);
const wholeNumber = inputs.filter((tag) => /data-decimals="0"/.test(tag)).map((tag) => tag.match(/id="([^"]+)"/)[1]);
assert.deepEqual(wholeNumber.sort(), ['effects-loudness-strength', 'effects-upmixer-center', 'effects-upmixer-surround']);
for (const tag of inputs) {
    if (/data-decimals="0"/.test(tag)) {
        assert.match(tag, /step="1"/, 'whole-number fields step by 1');
        assert.match(tag, /min="0"/);
    }
    assert.doesNotMatch(tag, /data-decimals="[1-9]/, 'decimal fields use the two-decimal default');
}

console.log('number field policy frontend checks passed');
