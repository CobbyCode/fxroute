#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only
// Regression coverage for the global footer master slider mapping.
//
// The volume curve and send-state live in static/playback_core.js (single
// owner); the streaming-owner footer renderer lives in
// static/playback_ui.js. This drives both real modules through their init
// deps instead of poking module internals.

const assert = require('assert/strict');
const fs = require('fs');
const path = require('path');

const root = path.join(__dirname, '..');
const coreSource = fs.readFileSync(path.join(root, 'static', 'playback_core.js'), 'utf8');
const uiSource = fs.readFileSync(path.join(root, 'static', 'playback_ui.js'), 'utf8');
const UiHelpers = require('../static/ui_helpers.js');

// Volume curve constants live in the core module (single owner).
assert.ok(/const\s+VOLUME_CURVE_GAMMA\s*=\s*[^;]+;/.test(coreSource), 'missing VOLUME_CURVE_GAMMA');
assert.ok(/const\s+VOLUME_SYNC_GRACE_MS\s*=\s*[^;]+;/.test(coreSource), 'missing VOLUME_SYNC_GRACE_MS');

global.window = { __footerSource: 'spotify' };
global.document = { getElementById: () => null, hidden: false };

const PlaybackCore = require('../static/playback_core.js');
const PlaybackUI = require('../static/playback_ui.js');

function makeClassList() {
    const values = new Set();
    return {
        toggle: (name, force) => {
            const on = force === undefined ? !values.has(name) : !!force;
            if (on) values.add(name);
            else values.delete(name);
        },
        add: (name) => values.add(name),
        remove: (name) => values.delete(name),
        contains: (name) => values.has(name),
    };
}

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
const elements = {
    volumeSlider,
    volumeDisplay,
    playbackBar: { classList: makeClassList() },
    seekRow: { classList: makeClassList() },
};
const state = {
    playback: { volume: 0, current_track: null, playing: false, paused: false },
    library: {},
    samplerate: {},
    settings: {},
};

const sentVolumes = [];
const toasts = [];
PlaybackCore.init({
    getState: () => state,
    getElements: () => elements,
    fetchFn: async (url, options) => {
        const body = JSON.parse(options.body);
        sentVolumes.push(body.volume);
        return { ok: true, json: async () => ({ volume: body.volume }) };
    },
    showToast: (message) => { toasts.push(message); },
    setRangeProgress: (...args) => UiHelpers.setRangeProgress(...args),
    updatePlaybackUI: () => {},
    showVolumeDisplayTemporarily: () => {},
});
PlaybackUI.init({
    getState: () => state,
    getElements: () => elements,
    formatTime: () => '0:00',
    setRangeProgress: (...args) => UiHelpers.setRangeProgress(...args),
    isStreamingFooterSource: (...args) => PlaybackCore.isStreamingFooterSource(...args),
    streamingFooterData: () => null,
    footerContentFreezeActive: () => false,
    footerDebug: () => {},
    applyRemoteVolume: (...args) => PlaybackCore.applyRemoteVolume(...args),
    renderVolumeControlsFromActualVolume: (...args) => PlaybackCore.renderVolumeControlsFromActualVolume(...args),
    isVolumeGestureActive: () => PlaybackCore.isVolumeGestureActive(),
    isVolumeRequestInFlight: () => PlaybackCore.isVolumeRequestInFlight(),
    getPendingVolume: () => PlaybackCore.getPendingVolume(),
    renderTrackFavoriteButton: () => {},
});

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

(async () => {
    // Neutral 1:1 mapping (gamma 1.0): slider, master, send and display agree.
    for (const value of [0, 25, 50, 75, 100]) {
        assert.equal(PlaybackCore.sliderVolumeToActualVolume(value), value, `footer slider ${value}% must set master to ${value}%`);
        assert.equal(PlaybackCore.actualVolumeToSliderValue(value), value, `master ${value}% must render as footer slider ${value}%`);

        sentVolumes.length = 0;
        const masterBefore = state.playback.volume;
        PlaybackCore.handleVolumeChange({ target: { value: String(value) } });
        assert.equal(state.playback.volume, value, `footer set must store master ${value}%`);
        assert.equal(volumeSlider.value, value, `footer slider must stay at ${value}% after setting`);
        assert.equal(volumeDisplay.textContent, `${value}%`, `footer must display ${value}% after setting`);
        await sleep(250); // volume send debounce (120ms) + fetch round-trip
        if (value !== masterBefore) {
            assert.ok(sentVolumes.includes(value), `footer set must send master ${value}% (sent: ${sentVolumes})`);
        } else {
            assert.equal(sentVolumes.length, 0, `unchanged master ${value}% must not resend (sent: ${sentVolumes})`);
        }

        PlaybackCore.renderVolumeControlsFromActualVolume(value);
        assert.equal(volumeSlider.value, value, `external master ${value}% must set footer slider`);
        assert.equal(volumeDisplay.textContent, `${value}%`, `external master ${value}% must set footer display`);
        assert.equal(volumeSlider.style.progress, `${value}%`, `footer progress must match ${value}%`);
        // End the gesture like the slider change handler does, and let the
        // sync grace expire before the next value.
        PlaybackCore.setVolumeGestureActive(false);
        await sleep(750);
    }

    // Streaming poll must not replace a pending local volume (send in flight).
    PlaybackCore.handleVolumeChange({ target: { value: '75' } });
    PlaybackUI.updateFooterForStreamingOwner({ available: false, volume: 25 });
    assert.equal(state.playback.volume, 75, 'streaming poll must not replace a pending local volume');
    assert.equal(volumeSlider.value, 75, 'streaming poll must not move the slider during a volume request');
    await sleep(250);
    PlaybackCore.setVolumeGestureActive(false);

    // Streaming poll must not replace a locally confirmed volume during grace.
    PlaybackUI.updateFooterForStreamingOwner({ available: false, volume: 25 });
    assert.equal(state.playback.volume, 75, 'streaming poll must not replace a locally confirmed volume during grace');
    assert.equal(volumeSlider.value, 75, 'streaming poll must not move the slider during volume sync grace');

    // Streaming poll must not replace a queued local volume.
    PlaybackCore.handleVolumeChange({ target: { value: '75' } });
    PlaybackUI.updateFooterForStreamingOwner({ available: false, volume: 25 });
    assert.equal(state.playback.volume, 75, 'streaming poll must not replace a queued local volume');
    assert.equal(volumeSlider.value, 75, 'streaming poll must not move the slider while a volume send is queued');
    await sleep(250);
    PlaybackCore.setVolumeGestureActive(false);
    await sleep(750);

    // Outside local sync the remote volume applies and renders.
    PlaybackUI.updateFooterForStreamingOwner({ available: false, volume: 25 });
    assert.equal(state.playback.volume, 25, 'streaming poll must apply a remote volume outside local sync');
    assert.equal(volumeSlider.value, 25, 'streaming poll must render a remote volume outside local sync');

    console.log('PASS  scripts/test_footer_volume_curve.js (neutral footer master mapping)');
})().catch((error) => {
    console.error(error);
    process.exit(1);
});
