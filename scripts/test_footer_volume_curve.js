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
    let lineComment = false;
    let blockComment = false;
    for (let index = brace; index < appSource.length; index += 1) {
        const character = appSource[index];
        const nextCharacter = appSource[index + 1];
        if (lineComment) {
            if (character === '\n') lineComment = false;
            continue;
        }
        if (blockComment) {
            if (character === '*' && nextCharacter === '/') {
                blockComment = false;
                index += 1;
            }
            continue;
        }
        if (quote) {
            if (escaped) escaped = false;
            else if (character === '\\') escaped = true;
            else if (character === quote) quote = '';
            continue;
        }
        if (character === '/' && nextCharacter === '/') {
            lineComment = true;
            index += 1;
            continue;
        }
        if (character === '/' && nextCharacter === '*') {
            blockComment = true;
            index += 1;
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
    window: { __footerSource: 'spotify' },
    isStreamingFooterSource() { return true; },
    footerContentFreezeActive() { return false; },
    renderTrackFavoriteButton() {},
    updatePlaybackCover() {},
    renderFooterModeButtons() {},
    setFooterProgressState() {},
    renderPeakWarningBadge() {},
    document: { getElementById() { return null; } },
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
    extractFunction('applyRemoteVolume'),
    extractFunction('handleVolumeChange'),
    extractFunction('updateFooterForStreamingOwner'),
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

function setVolumeSyncState({ requestInFlight, graceActive, pending = null }) {
    vm.runInContext(`
        volumeGestureActive = false;
        volumeRequestInFlight = ${requestInFlight};
        pendingVolume = ${pending};
        optimisticVolume = 75;
        lastConfirmedVolume = 25;
        volumeSyncGraceUntil = ${graceActive ? 'Date.now() + VOLUME_SYNC_GRACE_MS' : '0'};
        state.playback.volume = 75;
        renderVolumeControlsFromActualVolume(75);
    `, sandbox);
}

setVolumeSyncState({ requestInFlight: true, graceActive: false });
sandbox.updateFooterForStreamingOwner({ available: false, volume: 25 });
assert.equal(sandbox.state.playback.volume, 75, 'streaming poll must not replace a pending local volume');
assert.equal(volumeSlider.value, 75, 'streaming poll must not move the slider during a volume request');

setVolumeSyncState({ requestInFlight: false, graceActive: true });
sandbox.updateFooterForStreamingOwner({ available: false, volume: 25 });
assert.equal(sandbox.state.playback.volume, 75, 'streaming poll must not replace a locally confirmed volume during grace');
assert.equal(volumeSlider.value, 75, 'streaming poll must not move the slider during volume sync grace');

setVolumeSyncState({ requestInFlight: false, graceActive: false, pending: 75 });
sandbox.updateFooterForStreamingOwner({ available: false, volume: 25 });
assert.equal(sandbox.state.playback.volume, 75, 'streaming poll must not replace a queued local volume');
assert.equal(volumeSlider.value, 75, 'streaming poll must not move the slider while a volume send is queued');

setVolumeSyncState({ requestInFlight: false, graceActive: false });
sandbox.updateFooterForStreamingOwner({ available: false, volume: 25 });
assert.equal(sandbox.state.playback.volume, 25, 'streaming poll must apply a remote volume outside local sync');
assert.equal(volumeSlider.value, 25, 'streaming poll must render a remote volume outside local sync');

console.log('PASS  scripts/test_footer_volume_curve.js (neutral footer master mapping)');
