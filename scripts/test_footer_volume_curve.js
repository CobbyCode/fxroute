#!/usr/bin/env node
// Regression coverage for the global footer master slider mapping.

const assert = require('assert/strict');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const root = path.join(__dirname, '..');
const appSource = fs.readFileSync(path.join(root, 'static', 'app.js'), 'utf8');

function extractFunction(name) {
    const match = new RegExp(`function\\s+${name}\\s*\\(`).exec(appSource);
    assert.ok(match, `missing ${name}`);
    const brace = appSource.indexOf('{', match.index);
    assert.notEqual(brace, -1, `missing body ${name}`);
    let depth = 0;
    let quote = '';
    let escaped = false;
    for (let index = brace; index < appSource.length; index += 1) {
        const character = appSource[index];
        if (quote) {
            if (escaped) escaped = false;
            else if (character === '\\') escaped = true;
            else if (character === quote) quote = '';
            continue;
        }
        if (`'"\``.includes(character)) quote = character;
        else if (character === '{') depth += 1;
        else if (character === '}' && --depth === 0) return appSource.slice(match.index, index + 1);
    }
    throw new Error(`unterminated ${name}`);
}

const gammaDeclaration = /const\s+VOLUME_CURVE_GAMMA\s*=\s*[^;]+;/.exec(appSource);
assert.ok(gammaDeclaration, 'missing VOLUME_CURVE_GAMMA');
const volumeGraceDeclaration = /const\s+VOLUME_SYNC_GRACE_MS\s*=\s*[^;]+;/.exec(appSource);
assert.ok(volumeGraceDeclaration, 'missing VOLUME_SYNC_GRACE_MS');

const volumeSlider = {
    value: 0,
    style: {
        progress: '',
        setProperty(name, value) {
            if (name === '--range-progress') this.progress = value;
        },
    },
};
const volumeDisplay = { textContent: '' };
let sentVolume = null;
const sandbox = {
    elements: { volumeSlider, volumeDisplay },
    state: { playback: { volume: 0 } },
    queueVolumeSend(volume) { sentVolume = volume; },
    showVolumeDisplayTemporarily() {},
};

vm.createContext(sandbox);
vm.runInContext([
    gammaDeclaration[0],
    volumeGraceDeclaration[0],
    extractFunction('clampVolumeValue'),
    extractFunction('sliderVolumeToActualVolume'),
    extractFunction('actualVolumeToSliderValue'),
    extractFunction('setRangeProgress'),
    extractFunction('renderVolumeControlsFromActualVolume'),
    extractFunction('setLocalVolume'),
    extractFunction('handleVolumeChange'),
].join('\n'), sandbox);

for (const value of [0, 25, 50, 75, 100]) {
    assert.equal(
        sandbox.sliderVolumeToActualVolume(value),
        value,
        `footer slider ${value}% must set master to ${value}%`,
    );
    assert.equal(
        sandbox.actualVolumeToSliderValue(value),
        value,
        `master ${value}% must render as footer slider ${value}%`,
    );

    sentVolume = null;
    sandbox.handleVolumeChange({ target: { value: String(value) } });
    assert.equal(sandbox.state.playback.volume, value, `footer set must store master ${value}%`);
    assert.equal(sentVolume, value, `footer set must send master ${value}%`);
    assert.equal(volumeSlider.value, value, `footer slider must stay at ${value}% after setting`);
    assert.equal(volumeDisplay.textContent, `${value}%`, `footer must display ${value}% after setting`);

    sandbox.renderVolumeControlsFromActualVolume(value);
    assert.equal(volumeSlider.value, value, `external master ${value}% must set footer slider`);
    assert.equal(volumeDisplay.textContent, `${value}%`, `external master ${value}% must set footer display`);
    assert.equal(volumeSlider.style.progress, `${value}%`, `footer progress must match ${value}%`);
}

console.log('PASS  scripts/test_footer_volume_curve.js (neutral footer master mapping)');
