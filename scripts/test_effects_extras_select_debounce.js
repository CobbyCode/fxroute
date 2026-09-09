#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only
//
// Headroom/select save-timing regression:
// the value controls mix discrete SELECTs (headroom gain, autogain target,
// loudness FFT, tone mode) and numeric INPUTs (loudness strength, bass
// amount). SELECTs are atomic choices with no typing to protect, so their
// input/change events must save with the short toggle debounce. The long
// typing debounce restarted on every flip, so live A/B switching between
// headroom stages (e.g. -1 dB vs -6 dB) never fired a save and only a
// checkbox toggle (toggle debounce) applied the pending value.

const assert = require('assert/strict');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const repoRoot = path.resolve(__dirname, '..');
const appSource = fs.readFileSync(path.join(repoRoot, 'static', 'app.js'), 'utf8');

function extractConst(name) {
    const match = new RegExp(`const ${name}\\s*=`).exec(appSource);
    assert.ok(match, `missing const ${name}`);
    let depth = 0;
    let quote = '';
    let escaped = false;
    for (let index = match.index; index < appSource.length; index += 1) {
        const char = appSource[index];
        if (quote) {
            if (escaped) escaped = false;
            else if (char === '\\') escaped = true;
            else if (char === quote) quote = '';
            continue;
        }
        if (char === "'" || char === '"' || char === '`') quote = char;
        else if (char === '{' || char === '(' || char === '[') depth += 1;
        else if (char === '}' || char === ')' || char === ']') depth -= 1;
        else if (char === ';' && depth === 0) return appSource.slice(match.index, index + 1);
    }
    throw new Error(`unterminated const ${name}`);
}

// Extract the focus-tracking forEach wiring block verbatim: locate the last
// array entry (stable across versions) and balance from its forEach call.
function extractValueControlWiring() {
    const lastEntry = 'elements.effectsToneEffectMode,';
    const entryIndex = appSource.indexOf(lastEntry);
    assert.notEqual(entryIndex, -1, 'missing value-control array');
    const forEachIndex = appSource.indexOf('.forEach(el => {', entryIndex);
    assert.notEqual(forEachIndex, -1, 'missing value-control forEach');
    const arrayStart = appSource.lastIndexOf('[', forEachIndex);
    assert.notEqual(arrayStart, -1, 'missing value-control array start');
    let braceStart = appSource.indexOf('{', forEachIndex);
    let depth = 0;
    let quote = '';
    let escaped = false;
    let lineComment = false;
    for (let index = braceStart; index < appSource.length; index += 1) {
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
            if (depth === 0) {
                const tail = appSource.slice(index, index + 3);
                assert.ok(tail.startsWith('})'), 'unexpected forEach tail ' + JSON.stringify(tail));
                return appSource.slice(arrayStart, index + 2);
            }
        }
    }
    throw new Error('unterminated value-control wiring block');
}

function makeStub(tagName, value = '') {
    const listeners = new Map();
    return {
        tagName,
        value,
        checked: false,
        listeners,
        addEventListener(type, fn) {
            if (!listeners.has(type)) listeners.set(type, []);
            listeners.get(type).push(fn);
        },
        dispatch(type) {
            for (const fn of listeners.get(type) || []) fn();
        },
    };
}

function makeEnv() {
    const wiring = extractValueControlWiring();
    const elements = {
        effectsHeadroomGainDb: makeStub('SELECT', '-1'),
        effectsAutogainTargetDb: makeStub('SELECT', '-12'),
        effectsLoudnessStrength: makeStub('INPUT', '10'),
        effectsLoudnessFftSize: makeStub('SELECT', '4096'),
        effectsBassAmount: makeStub('INPUT', '0'),
        effectsToneEffectMode: makeStub('SELECT', 'crystalizer'),
    };
    // Manual clock: deterministic debounce timing without real waiting.
    let now = 0;
    let nextId = 1;
    const pending = new Map();
    const saveDelays = [];
    const context = {
        elements,
        _activeEditing: new Set(),
        saveDelays,
        saveEffectsExtrasDebounced(delayMs) {
            context.window.clearTimeout(context._timer);
            if (delayMs <= 0) {
                saveDelays.push({ at: now, delayMs });
                return;
            }
            context._timer = context.window.setTimeout(() => {
                saveDelays.push({ at: now, delayMs });
            }, delayMs);
        },
        _timer: null,
        window: {
            setTimeout(fn, ms) {
                const id = nextId++;
                pending.set(id, { at: now + ms, fn });
                return id;
            },
            clearTimeout(id) {
                pending.delete(id);
            },
        },
    };
    vm.createContext(context);
    vm.runInContext(
        `${extractConst('EFFECTS_EXTRAS_TOGGLE_DEBOUNCE_MS')}\n`
        + `${extractConst('EFFECTS_EXTRAS_VALUE_DEBOUNCE_MS')}\n`
        + wiring,
        context,
    );
    return {
        context,
        elements,
        saveDelays,
        advance(ms) {
            const target = now + ms;
            for (;;) {
                let dueId = null;
                let dueAt = Infinity;
                for (const [id, task] of pending) {
                    if (task.at <= target && task.at < dueAt) {
                        dueId = id;
                        dueAt = task.at;
                    }
                }
                if (dueId === null) break;
                now = dueAt;
                const task = pending.get(dueId);
                pending.delete(dueId);
                task.fn();
            }
            now = target;
        },
    };
}

async function main() {
    const TOGGLE = 800;
    const VALUE = 2000;
    {
        const env = makeEnv();
        assert.equal(vm.runInContext('EFFECTS_EXTRAS_TOGGLE_DEBOUNCE_MS', env.context), TOGGLE);
        assert.equal(vm.runInContext('EFFECTS_EXTRAS_VALUE_DEBOUNCE_MS', env.context), VALUE);
    }

    // 1. SELECT change (headroom -1 -> -6) saves with the short toggle delay.
    {
        const env = makeEnv();
        const select = env.elements.effectsHeadroomGainDb;
        select.value = '-6';
        select.dispatch('input');
        select.dispatch('change');
        env.advance(799);
        assert.equal(env.saveDelays.length, 0, 'select must not wait for the typing debounce');
        env.advance(1);
        assert.equal(env.saveDelays.length, 1, 'select change must save after the toggle debounce');
        assert.equal(env.saveDelays[0].delayMs, TOGGLE);
    }

    // 2. Numeric INPUT keeps the long typing debounce (typing protection).
    {
        const env = makeEnv();
        const input = env.elements.effectsBassAmount;
        input.value = '3.5';
        input.dispatch('input');
        env.advance(800);
        assert.equal(env.saveDelays.length, 0, 'numeric input must keep the typing debounce');
        env.advance(1200);
        assert.equal(env.saveDelays.length, 1, 'numeric input must save after the typing debounce');
        assert.equal(env.saveDelays[0].delayMs, VALUE);
    }

    // 3. A/B flipping the headroom select applies the latest value instead
    //    of restarting a long debounce forever: flips at t=0/500/1000 fire
    //    once, 800 ms after the last flip.
    {
        const env = makeEnv();
        const select = env.elements.effectsHeadroomGainDb;
        select.value = '-6';
        select.dispatch('change');
        env.advance(500);
        select.value = '-1';
        select.dispatch('change');
        env.advance(500);
        select.value = '-6';
        select.dispatch('change');
        env.advance(799);
        assert.equal(env.saveDelays.length, 0, 'no save before the toggle debounce elapses');
        env.advance(1);
        assert.equal(env.saveDelays.length, 1, 'A/B flips must collapse into exactly one save');
        assert.equal(select.value, '-6', 'latest select value wins');
    }

    // 4. All four SELECTs share the short delay; both numeric INPUTs keep the
    //    long one.
    {
        const env = makeEnv();
        const selects = [
            env.elements.effectsHeadroomGainDb,
            env.elements.effectsAutogainTargetDb,
            env.elements.effectsLoudnessFftSize,
            env.elements.effectsToneEffectMode,
        ];
        for (const select of selects) {
            select.dispatch('change');
            env.advance(800);
            const last = env.saveDelays[env.saveDelays.length - 1];
            assert.ok(last && last.delayMs === TOGGLE, 'select must use the toggle debounce');
        }
    }

    console.log('effects extras select debounce tests: ok');
}

main().catch((error) => {
    console.error(error);
    process.exit(1);
});
