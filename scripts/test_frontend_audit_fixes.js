#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only
// Regression tests for the 10 confirmed frontend-audit fixes: library
// delete/albums error state, measurement poll/cancel recovery, compare
// single-wire, Qobuz footer ownership, seek commit semantics, subwoofer
// retry, extras feedback reset and saved-list refresh on partial delete.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const repoRoot = path.resolve(__dirname, '..');
if (typeof global.window === 'undefined') global.window = global;
if (typeof global.confirm === 'undefined') global.confirm = () => true;

const Library = require('../static/library_ui.js');
require('../static/measurement_ui.js');
const Job = require('../static/measurement_job.js');
const BankUI = require('../static/output_bank_ui.js');
const EffectsUI = require('../static/output_effects_ui.js');
const StreamingRuntime = require('../static/streaming_runtime.js');
const PlaybackUI = require('../static/playback_ui.js');
const SubwooferUI = require('../static/subwoofer_ui.js');
const SavedActions = require('../static/measurement_saved_actions.js');

function stubEl() {
    return {
        classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
        setAttribute() {}, removeAttribute() {}, appendChild() {}, focus() {},
        querySelector: () => null, querySelectorAll: () => [],
        addEventListener() {}, removeEventListener() {},
        textContent: '', innerHTML: '', disabled: false, value: '', checked: false, files: [],
    };
}

async function main() {
    // 1. Library Delete Selected reads selection through deps.getState().
    {
        const toasts = [];
        const posts = [];
        const state = { library: { tracks: [], playlists: [], viewMode: 'tracks', currentFolder: '', searchQuery: '', selectedTrackIds: ['t1', 't2'], scanStatus: null, scanning: false, albums: [], albumsLoaded: true }, playlists: [] };
        // Only the elements this flow touches; everything else stays
        // undefined so unrelated panels are skipped, not stubbed truthy.
        const elements = { deleteSelectedTracksBtn: stubEl(), tracksList: stubEl() };
        Library.init({ getState: () => state, getElements: () => elements, showToast: (m, k) => toasts.push([m, k]) });
        const realFetch = global.fetch;
        global.fetch = async (url, options) => {
            if (String(url) === '/api/tracks/delete') {
                posts.push(JSON.parse(options.body));
                return { ok: true, json: async () => ({ deleted: ['t1', 't2'], errors: [] }) };
            }
            if (String(url) === '/api/tracks') return { ok: true, json: async () => [] };
            if (String(url) === '/api/library/status') return { ok: true, json: async () => ({ scanning: false }) };
            if (String(url) === '/api/albums') return { ok: true, json: async () => [] };
            throw new Error(`unexpected fetch ${url}`);
        };
        global.document = { querySelector: () => null };
        global.window.FXRouteContentState = { set() {}, hide() {} };
        try {
            await Library.deleteSelectedTracks();
            assert.deepEqual(posts, [{ track_ids: ['t1', 't2'] }], 'delete posts the selected ids');
            assert.deepEqual(state.library.selectedTrackIds, [], 'selection cleared after delete');
            assert.ok(toasts.some(([m, k]) => k === 'success' && /Deleted 2/.test(m)), 'success toast');
            // Empty selection toasts instead of fetching.
            posts.length = 0;
            await Library.deleteSelectedTracks();
            assert.equal(posts.length, 0, 'no request without selection');
            assert.ok(toasts.some(([m]) => /select tracks first/i.test(m)), 'empty-selection toast');
        } finally {
            global.fetch = realFetch;
            delete global.document;
            delete global.window.FXRouteContentState;
        }
    }

    // 2a. Sweep poll tolerates transport blips, then recovers.
    {
        const calls = [];
        const state = { measurement: { jobGeneration: 3, activeJobId: 'job-1', activeMeasurementKind: 'single', startInFlight: true, cancelRequested: false, repeatJobActive: false, statusText: '', measurements: [], visibilityById: {}, reviewVisibilityById: {} } };
        let failures = 2;
        Job.init({
            getState: () => state, getElements: () => ({}),
            fetch: async () => {
                if (failures > 0) { failures -= 1; throw new Error('socket hang up'); }
                return { ok: true, json: async () => ({ job: { id: 'job-1', status: 'completed', message: 'done' } }) };
            },
            showToast: (m, k) => calls.push(['toast', m, k]),
            renderMeasurementPanel: () => {},
            normalizeMeasurementKind: (k) => k, formatMeasurementInputLevelText: () => '',
            getMeasurementJobStatus: (j) => j.status, getMeasurementJobResultMeasurement: () => null,
            getMeasurementTimingInfo: () => ({}), normalizeMeasurementEntry: (e) => e,
            measurementModeReady: () => true, measurementRepeatBlockedReason: () => '',
            syncMeasurementRepeatNote: () => {}, setMeasurementSweepMenuOpen: () => {},
            syncAutoSubButton: () => {}, syncSpeakerAlignButton: () => {},
            cancelHybridWizardMeasurement: async () => {}, cancelAutoSubOptimize: async () => {},
            cancelSpeakerAlign: async () => {}, postRuntimeDebugSnapshot: async () => {},
            formatTransitionErrorDetail: (d, f) => (typeof d === 'string' ? d : f),
            sleep: async () => {},
        });
        await Job.pollMeasurementJob('job-1', 3);
        assert.equal(state.measurement.activeJobId, '', 'completed poll clears the job');
        assert.equal(state.measurement.statusText, 'done');
    }

    // 2b. Sweep poll resolves a gone job instead of hanging.
    {
        const toasts = [];
        const state = { measurement: { jobGeneration: 3, activeJobId: 'job-gone', activeMeasurementKind: 'single', startInFlight: true, cancelRequested: false, repeatJobActive: true, statusText: 'running', measurements: [], visibilityById: {}, reviewVisibilityById: {} } };
        Job.init({
            getState: () => state,
            getElements: () => ({ measurementRepeatStartBtn: stubEl(), measurementRepeatNote: stubEl(), measurementSweepToggleBtn: stubEl(), measurementSweepMenu: stubEl(), measurementHybridHeaderActions: stubEl() }),
            fetch: async () => ({ ok: false, status: 404, json: async () => ({}) }),
            showToast: (m, k) => toasts.push([m, k]),
            renderMeasurementPanel: () => {},
            normalizeMeasurementKind: (k) => k, formatMeasurementInputLevelText: () => '',
            getMeasurementJobStatus: (j) => j.status, getMeasurementJobResultMeasurement: () => null,
            getMeasurementTimingInfo: () => ({}), normalizeMeasurementEntry: (e) => e,
            measurementModeReady: () => true, measurementRepeatBlockedReason: () => '',
            syncMeasurementRepeatNote: () => {}, setMeasurementSweepMenuOpen: () => {},
            syncAutoSubButton: () => {}, syncSpeakerAlignButton: () => {},
            cancelHybridWizardMeasurement: async () => {}, cancelAutoSubOptimize: async () => {},
            cancelSpeakerAlign: async () => {}, postRuntimeDebugSnapshot: async () => {},
            formatTransitionErrorDetail: (d, f) => (typeof d === 'string' ? d : f),
            sleep: async () => {},
        });
        await Job.pollMeasurementJob('job-gone', 3);
        assert.equal(state.measurement.activeJobId, '', 'gone job clears activeJobId');
        assert.equal(state.measurement.startInFlight, false);
        assert.match(state.measurement.statusText, /no longer available/);
        assert.ok(toasts.some(([, k]) => k === 'error'), 'gone job toasts an error');
    }

    // 2c. Persistent transport failure exhausts into a cleared state.
    {
        const state = { measurement: { jobGeneration: 3, activeJobId: 'job-down', activeMeasurementKind: 'single', startInFlight: true, cancelRequested: false, repeatJobActive: true, statusText: 'running', measurements: [], visibilityById: {}, reviewVisibilityById: {} } };
        Job.init({
            getState: () => state,
            getElements: () => ({ measurementRepeatStartBtn: stubEl(), measurementRepeatNote: stubEl(), measurementSweepToggleBtn: stubEl(), measurementSweepMenu: stubEl(), measurementHybridHeaderActions: stubEl() }),
            fetch: async () => { throw new Error('network down'); },
            showToast: () => {},
            renderMeasurementPanel: () => {},
            normalizeMeasurementKind: (k) => k, formatMeasurementInputLevelText: () => '',
            getMeasurementJobStatus: (j) => j.status, getMeasurementJobResultMeasurement: () => null,
            getMeasurementTimingInfo: () => ({}), normalizeMeasurementEntry: (e) => e,
            measurementModeReady: () => true, measurementRepeatBlockedReason: () => '',
            syncMeasurementRepeatNote: () => {}, setMeasurementSweepMenuOpen: () => {},
            syncAutoSubButton: () => {}, syncSpeakerAlignButton: () => {},
            cancelHybridWizardMeasurement: async () => {}, cancelAutoSubOptimize: async () => {},
            cancelSpeakerAlign: async () => {}, postRuntimeDebugSnapshot: async () => {},
            formatTransitionErrorDetail: (d, f) => (typeof d === 'string' ? d : f),
            sleep: async () => {},
        });
        await Job.pollMeasurementJob('job-down', 3);
        assert.equal(state.measurement.activeJobId, '', 'exhausted poll clears activeJobId');
        assert.equal(state.measurement.startInFlight, false);
    }

    // 2d. Cancel against a gone job clears instead of throwing.
    {
        const state = { measurement: { jobGeneration: 5, activeJobId: 'job-gone', activeMeasurementKind: 'single', startInFlight: true, cancelRequested: false, repeatJobActive: true, statusText: 'running' } };
        Job.init({
            getState: () => state,
            getElements: () => ({ measurementRepeatStartBtn: stubEl(), measurementRepeatNote: stubEl(), measurementSweepToggleBtn: stubEl(), measurementSweepMenu: stubEl(), measurementHybridHeaderActions: stubEl() }),
            fetch: async () => ({ ok: false, status: 410, json: async () => ({}) }),
            showToast: () => {},
            renderMeasurementPanel: () => {},
            normalizeMeasurementKind: (k) => k, formatMeasurementInputLevelText: () => '',
            getMeasurementJobStatus: (j) => j.status, getMeasurementJobResultMeasurement: () => null,
            getMeasurementTimingInfo: () => ({}), normalizeMeasurementEntry: (e) => e,
            measurementModeReady: () => true, measurementRepeatBlockedReason: () => '',
            syncMeasurementRepeatNote: () => {}, setMeasurementSweepMenuOpen: () => {},
            syncAutoSubButton: () => {}, syncSpeakerAlignButton: () => {},
            cancelHybridWizardMeasurement: async () => {}, cancelAutoSubOptimize: async () => {},
            cancelSpeakerAlign: async () => {}, postRuntimeDebugSnapshot: async () => {},
            formatTransitionErrorDetail: (d, f) => (typeof d === 'string' ? d : f),
            sleep: async () => {},
        });
        await Job.cancelMeasurement();
        assert.equal(state.measurement.activeJobId, '', 'gone cancel clears activeJobId');
        assert.equal(state.measurement.startInFlight, false);
    }

    // 3. Albums failure surfaces an error instead of a refetch loop.
    {
        const toasts = [];
        const state = { library: { tracks: [], viewMode: 'tracks', searchQuery: '', selectedTrackIds: [], scanStatus: null, scanning: false, albums: [], albumsLoaded: false, albumsError: null }, playlists: [] };
        Library.init({ getState: () => state, getElements: () => ({}), showToast: (m, k) => toasts.push([m, k]) });
        const realFetch = global.fetch;
        global.fetch = async () => ({ ok: false, status: 500, json: async () => ({ detail: 'boom' }) });
        try {
            await Library.fetchAlbums();
            assert.equal(state.library.albumsLoaded, false, 'failed fetch keeps albums unloaded');
            assert.match(state.library.albumsError || '', /Failed to fetch albums/, 'error recorded');
            assert.ok(toasts.some(([, k]) => k === 'error'), 'failure toasts');
        } finally {
            global.fetch = realFetch;
        }
        const libSource = fs.readFileSync(path.join(repoRoot, 'static', 'library_ui.js'), 'utf8');
        assert.ok(/albumsError/.test(libSource), 'render path consults the albums error state');
        const fetchAlbumsBody = libSource.slice(libSource.indexOf('async function fetchAlbums()'), libSource.indexOf('function renderAlbums()'));
        assert.ok(!/if \(!res\.ok\) return;/.test(fetchAlbumsBody), 'fetchAlbums no longer swallows HTTP errors');
    }

    // 5. Compare controls bind exactly once.
    {
        const listeners = {};
        const track = (name) => ({ addEventListener: (type) => { listeners[`${name}:${type}`] = (listeners[`${name}:${type}`] || 0) + 1; } });
        BankUI.init({ getState: () => ({ dsp: {} }), getElements: () => ({
            effectsCompareToggle: track('toggle'), effectsCompareA: track('a'), effectsCompareB: track('b'),
        }) });
        BankUI.setupEffectsCompareActions();
        BankUI.setupEffectsCompareActions();
        assert.equal(listeners['toggle:click'], 1, 'toggle bound once');
        assert.equal(listeners['a:change'], 1, 'A bound once');
        assert.equal(listeners['b:change'], 1, 'B bound once');
        const effectsSource = fs.readFileSync(path.join(repoRoot, 'static', 'output_effects_ui.js'), 'utf8');
        assert.ok(!/FXRouteBankUI\.setupEffectsCompareActions\(\);/.test(effectsSource), 'no second wire call from the effects module');
    }

    // 6. Qobuz transport paints the footer only as the owner.
    {
        const painted = [];
        StreamingRuntime.init({
            apiPostJson: async () => ({ status: 'Playing', title: 'Qobuz track' }),
            reconcileFooterSource: () => {},
            updateFooterForStreamingOwner: (data) => painted.push(data),
            showToast: () => {}, maybeShowStreamingQueueCue: () => false,
        });
        global.window.__footerSource = 'spotify';
        await StreamingRuntime.qobuzCommand('play');
        assert.equal(painted.length, 0, 'no footer paint while Spotify owns it');
        global.window.__footerSource = 'qobuz';
        await StreamingRuntime.qobuzCommand('play');
        assert.equal(painted.length, 1, 'footer paints when Qobuz owns it');
        delete global.window.__footerSource;
        delete global.window.__qobuzLastData;
    }

    // 7. Seek commits keyboard change, ignores idle noise, sends once.
    {
        const sliderListeners = {};
        const slider = { value: '30000', addEventListener: (t, fn) => { sliderListeners[t] = fn; } };
        const seeks = [];
        const state = { playback: { duration: 120, position: 10 } };
        PlaybackUI.init({
            getState: () => state,
            getElements: () => ({ seekSlider: slider, playbackBar: { classList: { contains: () => false } }, seekCurrent: { textContent: '' } }),
            setRangeProgress: () => {}, formatTime: (s) => `${s}`,
            isStreamingFooterSource: () => false, streamingFooterData: () => null,
            doSeek: (pos) => seeks.push(pos), qobuzSeek: () => {}, spotifySeek: () => {},
        });
        PlaybackUI.initSeek();
        assert.ok(typeof sliderListeners.change === 'function', 'change listener wired');
        PlaybackUI.seekChange();
        PlaybackUI.seekEnd({ type: 'change' });
        assert.equal(seeks.length, 1, 'keyboard change commits one seek');
        PlaybackUI.seekEnd({ type: 'mouseup' });
        assert.equal(seeks.length, 1, 'idle mouseup is a no-op');
        // Pointer drag: down, off-element document release, then change.
        seeks.length = 0;
        sliderListeners.mousedown();
        PlaybackUI.seekChange();
        PlaybackUI.seekEnd({ type: 'mouseup' });
        assert.equal(seeks.length, 1, 'drag release commits once');
        PlaybackUI.seekEnd({ type: 'change' });
        assert.equal(seeks.length, 1, 'trailing change does not double-send');
    }

    // 7b. Streaming seek sends once per gesture.
    {
        const slider = { value: '500', addEventListener: () => {} };
        const sent = [];
        PlaybackUI.init({
            getState: () => ({ playback: { duration: 0 } }),
            getElements: () => ({ seekSlider: slider, playbackBar: { classList: { contains: () => false } }, seekCurrent: { textContent: '' } }),
            setRangeProgress: () => {}, formatTime: (s) => `${s}`,
            isStreamingFooterSource: () => true, streamingFooterData: () => ({ duration: 200 }),
            doSeek: () => { throw new Error('local seek must not run for streaming'); },
            qobuzSeek: () => {}, spotifySeek: (pos) => sent.push(pos),
        });
        global.window.__footerSource = 'spotify';
        const listeners = {};
        slider.addEventListener = (t, fn) => { listeners[t] = fn; };
        PlaybackUI.initSeek();
        listeners.mousedown();
        listeners.mouseup({ type: 'mouseup' });
        listeners.change({ type: 'change' });
        assert.equal(sent.length, 1, 'streaming drag sends exactly once');
        PlaybackUI.seekEnd({ type: 'mouseup' });
        assert.equal(sent.length, 1, 'idle document release sends nothing');
        delete global.window.__footerSource;
    }

    // 8. Failed subwoofer save stays retryable with identical values.
    {
        global.FXRouteOutputState = { subwooferView: () => ({ mode: 'subwoofer-2.1', roles: ['sub'] }) };
        const elements = {
            effectsSubwooferFrequencyNumber: { value: '80' }, effectsSubwooferFamily: { value: 'linkwitz-riley' },
            effectsSubwooferSlope: { value: '24' }, effectsSubwooferLink: null,
            effectsSubwooferLevel: { value: '0' }, effectsSubwooferDelay: { value: '0' },
            effectsSubwooferPolarity: { value: 'normal' }, effectsSubwooferPreview: null,
            effectsSubwooferCard: null, effectsSubwooferFeedback: stubEl(),
        };
        let mutations = 0;
        let failNext = true;
        SubwooferUI.init({
            getState: () => ({ outputSystem: { catalog: { active_mode: 'subwoofer-2.1' } } }),
            getElements: () => elements,
            getActiveEditing: () => new Set(),
            applyMutation: async () => {
                mutations += 1;
                if (failNext) throw new Error('Output mode changed before sub settings commit');
                return { ok: true };
            },
        });
        await SubwooferUI.saveSubwooferDebounced(0).catch(() => {});
        assert.equal(mutations, 1, 'first save attempted');
        failNext = false;
        await SubwooferUI.saveSubwooferDebounced(0).catch(() => {});
        assert.equal(mutations, 2, 'identical retry really saves after failure');
        delete global.FXRouteOutputState;
    }

    // 9. Successful extras save clears a previous failure label.
    {
        const feedback = { textContent: '', className: '' };
        const elements = new Proxy({ effectsExtrasFeedback: feedback }, {
            get: (t, p) => (p in t ? t[p] : stubEl()),
        });
        const state = { dsp: { available: false, presets: [], preset_count: 0 } };
        EffectsUI.init({ getState: () => state, getElements: () => elements, showToast: () => {} });
        const realFetch = global.fetch;
        let failNext = true;
        global.window.FXRouteBankUI = { isCompareLoadBusy: () => false };
        global.fetch = async () => (failNext
            ? { ok: false, json: async () => ({ detail: 'offline' }) }
            : { ok: true, json: async () => ({ extras: {} }) });
        try {
            await EffectsUI._doSaveEffectsExtras();
            assert.equal(feedback.textContent, 'Failed', 'failure label set');
            failNext = false;
            await EffectsUI._doSaveEffectsExtras();
            assert.equal(feedback.textContent, '', 'success clears the failure label');
        } finally {
            global.fetch = realFetch;
            delete global.window.FXRouteBankUI;
        }
    }

    // 10. Partial multi-delete still refreshes the saved list.
    {
        const reloads = [];
        const toasts = [];
        const state = { measurement: { saveInFlight: false, startInFlight: false, statusText: '', visibilityById: {}, reviewVisibilityById: {}, measurements: [] } };
        SavedActions.init({
            getState: () => state,
            getVisibleMeasurementEntries: () => [{ id: 'm1', name: 'one' }, { id: 'm2', name: 'two' }],
            fetch: async (url) => {
                if (String(url).endsWith('/m1')) return { ok: true, json: async () => ({}) };
                return { ok: false, status: 500, json: async () => ({ detail: 'locked' }) };
            },
            showToast: (m, k) => toasts.push([m, k]),
            renderMeasurementPanel: () => {},
            fetchMeasurements: async () => reloads.push('reload'),
            formatTransitionErrorDetail: (d, f) => (typeof d === 'string' ? d : f),
            confirm: () => true,
        });
        await SavedActions.deleteSelectedMeasurements();
        assert.ok(reloads.length >= 1, 'list refreshes even on partial failure');
        assert.ok(toasts.some(([, k]) => k === 'error'), 'partial failure toasts');
    }

    console.log('frontend audit fixes: ok');
}

main().catch((error) => { console.error(error); process.exitCode = 1; });
