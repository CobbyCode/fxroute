#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only
//
// Convolver/PEQ preset creation regression: the effects delay UI was removed,
// so collectEffectsExtras() no longer returns delay fields. Every multipart
// preset-creation form builder must therefore stop appending delay_* fields;
// sending String(undefined) -> "undefined" makes the FastAPI float Form fields
// reject the request with 422 and every "Create Convolver Preset" (Take L, R,
// Both) fails.

const assert = require('assert/strict');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const repoRoot = path.resolve(__dirname, '..');
const appSource = fs.readFileSync(path.join(repoRoot, 'static', 'app.js'), 'utf8');
const indexSource = fs.readFileSync(path.join(repoRoot, 'static', 'index.html'), 'utf8');

function extractFunction(name) {
    const match = new RegExp(`(?:async\\s+)?function\\s+${name}\\s*\\(`).exec(appSource);
    assert.ok(match, `missing function ${name}`);
    let parenDepth = 1;
    let braceStart = -1;
    for (let index = match.index + match[0].length; index < appSource.length; index += 1) {
        if (appSource[index] === '(') parenDepth += 1;
        if (appSource[index] === ')') parenDepth -= 1;
        if (parenDepth === 0) {
            braceStart = appSource.indexOf('{', index);
            break;
        }
    }
    assert.notEqual(braceStart, -1, `missing function body ${name}`);
    let depth = 0;
    let quote = '';
    let escaped = false;
    for (let index = braceStart; index < appSource.length; index += 1) {
        const char = appSource[index];
        if (quote) {
            if (escaped) escaped = false;
            else if (char === '\\') escaped = true;
            else if (char === quote) quote = '';
            continue;
        }
        if (char === "'" || char === '"' || char === '`') quote = char;
        else if (char === '{') depth += 1;
        else if (char === '}') {
            depth -= 1;
            if (depth === 0) return appSource.slice(match.index, index + 1);
        }
    }
    throw new Error(`unterminated function ${name}`);
}

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

function makeContext() {
    const elements = {
        effectsLimiterEnabled: { checked: false },
        effectsHeadroomEnabled: { checked: true },
        effectsHeadroomGainDb: { value: '-3' },
        effectsAutogainEnabled: { checked: false },
        effectsAutogainTargetDb: { value: '-12' },
        effectsLoudnessEnabled: { checked: false },
        effectsLoudnessStrength: { value: '10' },
        effectsLoudnessFftSize: { value: '4096' },
        effectsBassEnabled: { checked: false },
        effectsBassAmount: { value: '0' },
        effectsToneEffectEnabled: { checked: false },
        effectsToneEffectMode: { value: 'crystalizer' },
    };
    const context = { elements, window: {} };
    vm.createContext(context);
    vm.runInContext(`
        ${extractConst('EFFECTS_HEADROOM_ALLOWED_GAIN_DB')}
        ${extractConst('EFFECTS_AUTOGAIN_ALLOWED_TARGET_DB')}
        ${extractConst('EFFECTS_LOUDNESS_ALLOWED_FFT_SIZE')}
        ${extractConst('EFFECTS_LOUDNESS_LEGACY_STRENGTHS')}
        ${extractConst('EFFECTS_TONE_EFFECT_MODES')}
        ${extractFunction('normalizeEffectsHeadroomGainDb')}
        ${extractFunction('normalizeEffectsAutogainTargetDb')}
        ${extractFunction('normalizeEffectsLoudnessFftSize')}
        ${extractFunction('normalizeEffectsLoudnessStrength')}
        ${extractFunction('normalizeEffectsToneEffectMode')}
        ${extractFunction('collectEffectsExtras')}
        ${extractFunction('appendMeasurementConvolverExtras')}
    `, context);
    return context;
}

function main() {
    // 1. Runtime: the shared extras form builder must only append defined,
    //    parseable values (no leftover reads of removed extras fields).
    const context = makeContext();
    const formData = new Map();
    context.FormData = class FormData {
        append(name, value) { formData.set(String(name), String(value)); }
    };
    vm.runInContext('appendMeasurementConvolverExtras(new FormData())', context);
    assert.ok(formData.get('preset_name') === undefined, 'builder must not invent a preset name');
    assert.equal(formData.get('load_after_create'), 'false');
    assert.equal(formData.get('headroom_enabled'), 'true');
    assert.equal(formData.get('headroom_gain_db'), '-3');
    assert.equal(formData.get('autogain_target_db'), '-12');
    assert.equal(formData.get('tone_effect_mode'), 'crystalizer');
    for (const [name, value] of formData) {
        assert.ok(value !== 'undefined' && value !== 'NaN' && value !== 'null',
            `form field ${name} carries an invalid value: ${value}`);
        assert.ok(!name.startsWith('delay_'),
            `delay UI was removed; ${name} must not be sent (backend rejects it)`);
    }

    // 2. Static: no multipart builder may append delay_* fields anywhere in
    //    app.js; the endpoints keep them optional with defaults.
    assert.ok(!/formData\.append\('delay_/.test(appSource),
        "stale delay_* form fields found; collectEffectsExtras() no longer provides them (422)");

    // 3. collectEffectsExtras() must not reference the removed delay elements.
    assert.ok(!/delayEnabled|delayLeftMs|delayRightMs/.test(extractFunction('collectEffectsExtras')),
        'collectEffectsExtras() must not return removed delay fields');

    // Cached asset version must be bumped alongside the app.js change.
    assert.match(indexSource, /app\.js\?v=\d+\.\d+\.\d+/);

    console.log('convolver preset form extras tests: ok');
}

main();
