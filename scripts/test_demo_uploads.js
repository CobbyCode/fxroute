#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only
//
// Upload flows of the simulated backend: preset create/import endpoints read
// the uploaded file like dsp.manager does (name from the file, payload
// validation, filters carried into the preset) and the library upload answers
// the XMLHttpRequest the real UI posts its file with.

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const root = path.join(__dirname, '..');

function makeContext() {
    const ctx = { console, Date, Math, URLSearchParams, FormData, File, Blob,
        setInterval() { return 0; }, clearInterval() {}, setTimeout,
        fetch() { throw new Error('Unexpected real fetch'); } };
    ctx.window = ctx;
    vm.createContext(ctx);
    for (const file of ['data/library.js', 'data/library2.js', 'data/library3.js', 'data/radio.js',
        'data/measurements.js', 'data/alignment.js', 'state.js', 'routes.js', 'xhr.js']) {
        vm.runInContext(fs.readFileSync(path.join(root, 'demo', file), 'utf8'), ctx);
    }
    return ctx;
}

const ctx = makeContext();

const NATIVE_PRESET = {
    schema: 'fxroute.dsp.preset',
    version: 1,
    metadata: { name: 'Metadata Name That Loses', source_presets: [] },
    chain: [{ id: 'equalizer#0', type: 'equalizer', enabled: true, params: {
        channelMode: 'dual', eqMode: 'IIR',
        leftBands: [{ filterType: 'bell', frequencyHz: 80, gainDb: -4, q: 1.1, enabled: true }],
        rightBands: [{ filterType: 'bell', frequencyHz: 80, gainDb: -4, q: 1.1, enabled: true }],
    }, mix: {} }],
};

const LEGACY_PRESET = {
    output: {
        blocklist: [],
        'equalizer#0': { bypass: false, 'split-channels': false, mode: 'IIR', 'num-bands': 2,
            'input-gain': 0, 'output-gain': -1,
            left: { band0: { type: 'Bell', frequency: 120, gain: 3, q: 1.4, mute: false },
                band1: { type: 'Hi-shelf', frequency: 6000, gain: -2, q: 0.7, mute: true } } },
        plugins_order: ['equalizer#0'],
    },
};

function jsonFile(name, payload) {
    return new File([typeof payload === 'string' ? payload : JSON.stringify(payload)], name, { type: 'application/json' });
}

// A minimal ZIP whose first local entry is named `entry`: the demo reads that
// name for the bundle's preset name, exactly like the backend's rule.
function zipFile(name, entry) {
    const nameBytes = Buffer.from(entry, 'utf8');
    const header = Buffer.alloc(30);
    header.write('PK\x03\x04', 0, 'binary');
    header.writeUInt16LE(20, 4);
    header.writeUInt32LE(nameBytes.length, 26);
    const body = Buffer.concat([header, nameBytes]);
    return new File([body], name, { type: 'application/zip' });
}

function uploadFile(name, content) {
    return new File([content], name, { type: 'audio/flac' });
}

async function post(url, fields) {
    const form = new FormData();
    for (const [key, value] of Object.entries(fields)) form.append(key, value);
    const response = await ctx.fetch(url, { method: 'POST', body: form });
    const text = await response.text();
    let data = null;
    try { data = JSON.parse(text); } catch (e) { data = { raw: text }; }
    return { status: response.status, data };
}

async function get(url) {
    const response = await ctx.fetch(url, {});
    return JSON.parse(await response.text());
}

async function state() {
    return get('/api/audio/output-state');
}

const BINDING = { bank_mode: 'stereo-sub', bank_id: 'global' };

(async () => {
    // ── A native preset JSON is named after the uploaded file ───────────
    let current = await state();
    const imported = await post('/api/dsp/presets/import-json', {
        file: jsonFile('Measured Front LR.json', NATIVE_PRESET),
        ...BINDING, expected_revision: String(current.revision),
    });
    assert.equal(imported.status, 200, JSON.stringify(imported.data));
    assert.equal(imported.data.status, 'ok');
    assert.equal(imported.data.preset.name, 'Measured Front LR', 'the file name names the preset');
    assert.equal(imported.data.preset.filename, 'Measured Front LR.json');
    assert.equal(imported.data.bank.assigned, true);
    assert.equal(imported.data.bank.bank_id, 'global');
    assert.equal(imported.data.bank.revision, current.revision + 1);
    assert.equal(typeof imported.data.active_preset, 'string');

    const stock = await get('/api/dsp/presets');
    const entry = stock.presets.find(preset => preset.name === 'Measured Front LR');
    assert.ok(entry, 'the imported preset is selectable');
    assert.equal(entry.bank, 'global');
    assert.equal(entry.mode, 'stereo-sub');
    assert.equal(entry.peq.params.leftBands[0].gainDb, -4, 'the imported filters reach the preset entry');

    current = await state();
    assert.equal(current.modes['stereo-sub'].banks.global.preset, 'Measured Front LR',
        'the import lands in the addressed bank');

    // ── A legacy EasyEffects preset translates into PEQ bands ───────────
    const legacy = await post('/api/dsp/presets/import-json', {
        file: jsonFile('Legacy EQ.json', LEGACY_PRESET),
        ...BINDING, expected_revision: String(current.revision),
    });
    assert.equal(legacy.status, 200, JSON.stringify(legacy.data));
    assert.equal(legacy.data.preset.name, 'Legacy EQ');
    const legacyEntry = (await get('/api/dsp/presets')).presets.find(preset => preset.name === 'Legacy EQ');
    assert.equal(legacyEntry.peq.params.bands.length, 2);
    assert.equal(legacyEntry.peq.params.bands[0].filterType, 'bell');
    assert.equal(legacyEntry.peq.params.bands[0].frequencyHz, 120);
    assert.equal(legacyEntry.peq.params.bands[1].filterType, 'high_shelf');
    assert.equal(legacyEntry.peq.params.bands[1].enabled, false, 'a muted band stays disabled');
    assert.equal(legacyEntry.peq.params.outputGainDb, -1);

    // ── A stale revision is refused before anything is written ──────────
    current = await state();
    const conflicted = await post('/api/dsp/presets/import-json', {
        file: jsonFile('Stale Import.json', NATIVE_PRESET),
        ...BINDING, expected_revision: String(current.revision - 1),
    });
    assert.equal(conflicted.status, 409);
    assert.equal(conflicted.data.detail.code, 'bank-assign-conflict');
    assert.equal(conflicted.data.detail.assigned, false);
    assert.equal(conflicted.data.detail.revision, current.revision);
    assert.equal((await get('/api/dsp/presets')).presets.some(preset => preset.name === 'Stale Import'), false,
        'a conflicted import leaves no preset behind');
    assert.equal((await state()).revision, current.revision, 'a conflicted import moves no bank');

    // ── Re-importing the same file overwrites the preset ────────────────
    const changed = JSON.parse(JSON.stringify(NATIVE_PRESET));
    changed.chain[0].params.leftBands[0].gainDb = -6;
    const again = await post('/api/dsp/presets/import-json', {
        file: jsonFile('Measured Front LR.json', changed),
        ...BINDING, expected_revision: String((await state()).revision),
    });
    assert.equal(again.status, 200, JSON.stringify(again.data));
    const overwritten = (await get('/api/dsp/presets')).presets
        .filter(preset => preset.name === 'Measured Front LR' && preset.bank === 'global');
    assert.equal(overwritten.length, 1, 'a re-import replaces the file instead of adding a preset');
    assert.equal(overwritten[0].peq.params.leftBands[0].gainDb, -6, 'the overwrite carries the new filters');
    // A preset name is unique within its bank only: the low bank keeps its own.
    const otherBank = await post('/api/dsp/presets/import-json', {
        file: jsonFile('Measured Front LR.json', NATIVE_PRESET),
        bank_mode: 'stereo-sub', bank_id: 'low', expected_revision: String((await state()).revision),
    });
    assert.equal(otherBank.status, 200, JSON.stringify(otherBank.data));
    assert.equal((await state()).modes['stereo-sub'].banks.low.preset, 'Measured Front LR');
    assert.equal((await get('/api/dsp/presets')).presets
        .filter(preset => preset.name === 'Measured Front LR').length, 2);

    // ── The file names the preset, not a form field ────────────────────
    const namedByFile = await post('/api/dsp/presets/import-json', {
        file: jsonFile('File Wins.json', NATIVE_PRESET), preset_name: 'Form Field Loses',
        ...BINDING, expected_revision: String((await state()).revision),
    });
    assert.equal(namedByFile.data.preset.name, 'File Wins');
    assert.equal((await get('/api/dsp/presets')).presets.some(preset => preset.name === 'Form Field Loses'), false);
    const unnamedIr = await post('/api/dsp/presets/create-with-ir', {
        file: uploadFile('No Name.irs', Buffer.alloc(64, 1)),
        ...BINDING, expected_revision: String(namedByFile.data.bank.revision),
    });
    assert.equal(unnamedIr.status, 400);
    assert.equal(unnamedIr.data.detail, 'preset_name is required');

    // ── Rejected payloads ──────────────────────────────────────────────
    const revision = (await state()).revision;
    for (const [label, file, message] of [
        ['invalid JSON', jsonFile('Broken.json', '{not json'), /Preset JSON is invalid/],
        ['foreign JSON', jsonFile('Foreign.json', { hello: 'world' }), /neither a native nor EasyEffects output preset/],
        ['built-in name', jsonFile('Direct.json', NATIVE_PRESET), /is a built-in preset/],
    ]) {
        const rejected = await post('/api/dsp/presets/import-json', {
            file, ...BINDING, expected_revision: String(revision),
        });
        assert.equal(rejected.status, 400, `${label} is refused`);
        assert.match(String(rejected.data.detail), message);
        assert.equal((await state()).revision, revision, `${label} moves nothing`);
    }
    for (const name of ['Broken', 'Foreign']) {
        assert.equal((await get('/api/dsp/presets')).presets.some(preset => preset.name === name), false);
    }

    // ── A bundle keeps the archive's own preset name and reports its IRs ──
    const bundleRevision = (await state()).revision;
    const bundle = await post('/api/dsp/presets/import-bundle', {
        file: zipFile('Studio Bundle.zip', 'Studio Correction.json'),
        ...BINDING, expected_revision: String(bundleRevision),
    });
    assert.equal(bundle.status, 200, JSON.stringify(bundle.data));
    assert.equal(bundle.data.preset.name, 'Studio Correction', "the bundle's member names the preset");
    assert.deepEqual(bundle.data.irs, []);
    const plainBundle = await post('/api/dsp/presets/import-bundle', {
        file: zipFile('Plain Bundle.zip', 'preset.json'),
        ...BINDING, expected_revision: String(bundle.data.bank.revision),
    });
    assert.equal(plainBundle.data.preset.name, 'Plain Bundle', 'a plain preset.json falls back to the archive name');
    const notZip = await post('/api/dsp/presets/import-bundle', {
        file: new File([Buffer.from('nope')], 'NotABundle.zip', { type: 'application/zip' }),
        ...BINDING, expected_revision: String(plainBundle.data.bank.revision),
    });
    assert.equal(notZip.status, 400);
    assert.equal(notZip.data.detail, 'Invalid ZIP archive');

    // ── An IR upload creates a convolver preset, other files are refused ──
    const irRevision = (await state()).revision;
    const convolver = await post('/api/dsp/presets/create-with-ir', {
        preset_name: 'Room Correction', file: uploadFile('Room Correction.irs', Buffer.alloc(2048, 1)),
        ...BINDING, expected_revision: String(irRevision),
    });
    assert.equal(convolver.status, 200, JSON.stringify(convolver.data));
    assert.equal(convolver.data.preset.name, 'Room Correction');
    const convolverEntry = (await get('/api/dsp/presets')).presets.find(preset => preset.name === 'Room Correction');
    assert.equal(convolverEntry.convolver, true);
    assert.ok((await get('/api/dsp/presets')).irs.some(ir => ir.name === 'Room Correction'),
        'the created convolver shows up as an IR');
    const badIr = await post('/api/dsp/presets/create-with-ir', {
        preset_name: 'Bogus', file: uploadFile('Bogus.txt', 'not an IR'),
        ...BINDING, expected_revision: String(convolver.data.bank.revision),
    });
    assert.equal(badIr.status, 400);
    assert.match(String(badIr.data.detail), /Unsupported IR file type/);
    const emptyIr = await post('/api/dsp/presets/create-with-ir', {
        preset_name: 'Empty', file: uploadFile('Empty.irs', ''),
        ...BINDING, expected_revision: String(convolver.data.bank.revision),
    });
    assert.equal(emptyIr.status, 400);
    assert.equal(emptyIr.data.detail, 'The IR file is empty');

    // ── REW text imports parse like the backend's parser ────────────────
    const rewRevision = (await state()).revision;
    const rew = await post('/api/dsp/presets/import-rew-peq', {
        preset_name: 'REW Front LR',
        file: new File(['Filter 1: ON PK Fc 63 Hz Gain -3.5 dB Q 1.2\nFilter 2: ON PK Fc 210 Hz Gain 2.0 dB Q 3.0\n'],
            'REW Front LR.txt', { type: 'text/plain' }),
        ...BINDING, expected_revision: String(rewRevision),
    });
    assert.equal(rew.status, 200, JSON.stringify(rew.data));
    assert.equal(rew.data.preset.name, 'REW Front LR');
    const rewEntry = (await get('/api/dsp/presets')).presets.find(preset => preset.name === 'REW Front LR');
    assert.equal(rewEntry.peq.params.bands.length, 2);
    assert.equal(rewEntry.peq.params.bands[0].frequencyHz, 63);
    assert.equal(rewEntry.peq.params.bands[0].gainDb, -3.5);
    const badRew = await post('/api/dsp/presets/import-rew-peq', {
        preset_name: 'Bad REW',
        file: new File(['Filter 1: ON LS Fc 63 Hz Gain -3.5 dB Q 1.2\n'], 'Bad REW.txt', { type: 'text/plain' }),
        ...BINDING, expected_revision: String(rew.data.bank.revision),
    });
    assert.equal(badRew.status, 400);
    assert.match(String(badRew.data.detail), /Unsupported REW PEQ filter type/);
    assert.equal((await post('/api/dsp/presets/import-rew-peq', {
        file: new File(['Filter 1: ON PK Fc 63 Hz Gain -3.5 dB Q 1.2\n'], 'x.txt', { type: 'text/plain' }),
        ...BINDING, expected_revision: String(rew.data.bank.revision),
    })).data.detail, 'preset_name is required');

    // ── The dual import separates REW text from dual IR files ───────────
    const dualRevision = (await state()).revision;
    const dualText = await post('/api/dsp/presets/import-filter-dual', {
        preset_name: 'Dual REW', left_text: 'Filter 1: ON PK Fc 80 Hz Gain -2 dB Q 1.0',
        right_text: 'Filter 1: ON PK Fc 85 Hz Gain -2.5 dB Q 1.1',
        ...BINDING, expected_revision: String(dualRevision),
    });
    assert.equal(dualText.status, 200, JSON.stringify(dualText.data));
    assert.equal(dualText.data.import_kind, 'dual-peq');
    const dualEntry = (await get('/api/dsp/presets')).presets.find(preset => preset.name === 'Dual REW');
    assert.equal(dualEntry.peq.params.channelMode, 'dual');
    assert.equal(dualEntry.peq.params.rightBands[0].frequencyHz, 85);
    const dualIr = await post('/api/dsp/presets/import-filter-dual', {
        preset_name: 'Dual IR', left_file: uploadFile('Left.irs', Buffer.alloc(64, 2)),
        right_file: uploadFile('Right.irs', Buffer.alloc(64, 3)),
        ...BINDING, expected_revision: String(dualText.data.bank.revision),
    });
    assert.equal(dualIr.status, 200, JSON.stringify(dualIr.data));
    assert.equal(dualIr.data.import_kind, 'dual-convolver');
    assert.equal(dualIr.data.ir, 'Left.irs');
    const mixed = await post('/api/dsp/presets/import-filter-dual', {
        preset_name: 'Mixed', left_file: uploadFile('Left.irs', Buffer.alloc(64, 2)),
        ...BINDING, expected_revision: String(dualIr.data.bank.revision),
    });
    assert.equal(mixed.status, 400);
    assert.equal(mixed.data.detail, 'Provide both Left and Right files, or neither');

    // ── Surround Global imports need and keep their speaker group ───────
    const surround = (await get('/api/audio/output-state')).modes.surround;
    const surroundGlobal = await post('/api/dsp/presets/import-json', {
        file: jsonFile('Center Correction.json', NATIVE_PRESET),
        bank_mode: 'surround', bank_id: 'global', expected_revision: String((await state()).revision),
    });
    assert.equal(surroundGlobal.status, 400);
    assert.equal(surroundGlobal.data.detail, 'Surround Global presets need a speaker group');
    const grouped = await post('/api/dsp/presets/import-json', {
        file: jsonFile('Center Correction.json', NATIVE_PRESET),
        bank_mode: 'surround', bank_id: 'global', bank_group: 'center',
        expected_revision: String((await state()).revision),
    });
    assert.equal(grouped.status, 200, JSON.stringify(grouped.data));
    const groupedEntry = (await get('/api/dsp/presets')).presets.find(preset => preset.name === 'Center Correction');
    assert.equal(groupedEntry.group, 'center');
    assert.equal(groupedEntry.mode, 'surround');
    assert.ok(surround, 'the surround mode stays available');

    // ── Library upload: an audio file joins the active catalog ──────────
    const beforeTracks = (await get('/api/tracks')).length;
    const uploaded = await post('/api/library/upload', { file: uploadFile('New Song.flac', Buffer.alloc(32, 7)) });
    assert.equal(uploaded.status, 200, JSON.stringify(uploaded.data));
    assert.equal(uploaded.data.status, 'uploaded');
    assert.equal(uploaded.data.kind, 'audio');
    assert.equal(uploaded.data.filename, 'New Song.flac');
    assert.equal(uploaded.data.message, 'Uploaded New Song.flac');
    assert.equal(uploaded.data.track_count, beforeTracks + 1);
    const tracks = await get('/api/tracks');
    const newTrack = tracks.find(track => track.title === 'New Song');
    assert.ok(newTrack, 'the uploaded file shows up in the library');
    assert.equal(newTrack.album, 'Uploads');
    const albums = await get('/api/albums');
    assert.ok(albums.some(album => album.name === 'Uploads' && album.track_count >= 1),
        'the upload folder is an album');

    const duplicate = await post('/api/library/upload', { file: uploadFile('New Song.flac', Buffer.alloc(32, 7)) });
    assert.equal(duplicate.status, 409);
    assert.equal(duplicate.data.detail, 'A file with that name already exists');
    const unsupported = await post('/api/library/upload', { file: uploadFile('Notes.txt', 'hello') });
    assert.equal(unsupported.status, 400);
    assert.equal(unsupported.data.detail, 'Unsupported file type');

    const zipUpload = await post('/api/library/upload', { file: zipFile('Live Set.zip', 'preset.json') });
    assert.equal(zipUpload.status, 200, JSON.stringify(zipUpload.data));
    assert.equal(zipUpload.data.kind, 'zip');
    assert.equal(zipUpload.data.folder, 'Live Set');
    assert.equal(zipUpload.data.imported_track_count, 1);
    assert.match(zipUpload.data.message, /Imported 1 track from Live Set\.zip/);
    assert.ok((await get('/api/albums')).some(album => album.name === 'Live Set'));

    const target = (await get('/api/tracks'))[0];
    const playlist = await post('/api/library/upload', {
        file: new File([`#EXTM3U\n${target.path}\n`], 'Favorites.m3u', { type: 'audio/x-mpegurl' }),
    });
    assert.equal(playlist.status, 200, JSON.stringify(playlist.data));
    assert.equal(playlist.data.kind, 'playlist');
    assert.equal(playlist.data.imported_playlist_count, 1);
    assert.equal(playlist.data.playlist.track_count, 1);
    assert.ok((await get('/api/playlists')).some(entry => entry.name === 'Favorites'));
    const unmatched = await post('/api/library/upload', {
        file: new File(['#EXTM3U\ndoes-not-exist.flac\n'], 'Nothing.m3u', { type: 'audio/x-mpegurl' }),
    });
    assert.equal(unmatched.status, 400);
    assert.equal(unmatched.data.detail, 'Playlist did not match any library tracks');

    // ── The XHR shim answers the upload the library UI posts ────────────
    const xhrResult = await new Promise((resolve, reject) => {
        const xhr = new ctx.XMLHttpRequest();
        const progress = [];
        let loaded = false;
        xhr.upload.addEventListener('progress', (event) => progress.push(event.loaded));
        xhr.addEventListener('load', () => {
            loaded = true;
            resolve({ status: xhr.status, text: xhr.responseText, progress, event: 'load' });
        });
        xhr.addEventListener('error', () => reject(new Error('upload failed')));
        xhr.open('POST', '/api/library/upload', true);
        xhr.send(new FormData().append
            ? (() => { const form = new FormData(); form.append('file', uploadFile('XHR Song.flac', Buffer.alloc(16, 3))); return form; })()
            : new FormData());
        setTimeout(() => { if (!loaded) reject(new Error('the upload never settled')); }, 3000);
    });
    assert.equal(xhrResult.status, 200);
    assert.equal(JSON.parse(xhrResult.text).filename, 'XHR Song.flac');
    assert.ok(xhrResult.progress.length >= 2, 'the upload reports progress');
    assert.ok((await get('/api/tracks')).some(track => track.title === 'XHR Song'),
        'an XHR upload lands in the library too');

    console.log('Demo preset and library upload tests passed');
})().catch((error) => {
    console.error(error);
    process.exit(1);
});
