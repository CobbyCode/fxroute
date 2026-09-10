#!/usr/bin/env node
// Demo download + stream-facts contract: every demo response exposes blob()
// so the frontend download paths (resp.blob -> URL.createObjectURL) work;
// playlist export serves an M3U8 body, selection download and the
// certificate link serve file bodies instead of 404ing; qobuz demo_start
// is reachable; and local stream facts carry the real per-track rate under
// samplerate_hz (never a hardcoded 48 kHz under the wrong field name).
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const root = path.join(__dirname, '..');

function makeDemoContext() {
    const ctx = {
        window: {},
        setInterval() { return 0; },
        clearInterval() {},
        Date,
        Math,
        console,
        URLSearchParams,
        FormData,
        fetch() { return Promise.reject(new Error('unexpected real fetch')); },
    };
    ctx.window = ctx;
    vm.createContext(ctx);
    for (const file of ['demo/data/library.js', 'demo/data/library2.js', 'demo/data/radio.js',
        'demo/data/measurements.js', 'demo/state.js', 'demo/routes.js']) {
        vm.runInContext(fs.readFileSync(path.join(root, file), 'utf8'), ctx);
    }
    return ctx;
}

function post(fetch, url, body) {
    return fetch(url, { method: 'POST', body: JSON.stringify(body || {}) });
}

(async () => {
    const ctx = makeDemoContext();
    const fetch = ctx.fetch;

    // Every response exposes headers + blob() (the download filename
    // parser and triggerBlobDownload need both).
    const probe = await fetch('/api/status');
    assert.equal(typeof probe.headers.get, 'function', 'responses expose headers.get()');
    assert.equal(typeof probe.blob, 'function', 'responses expose blob()');

    // ── Playlist export (M3U8), active catalog ─────────────────────────
    const pl2 = await (await fetch('/api/playlists')).json();
    const sampler = pl2.find(p => p.id === 'playlist_d2-sampler');
    assert.ok(sampler, 'library-2 sampler playlist present');
    const exp = await fetch('/api/playlists/' + sampler.id + '/export');
    assert.equal(exp.status, 200, 'playlist export is not swallowed by the CRUD regex');
    assert.ok(exp.ok);
    const expDisposition = exp.headers.get('Content-Disposition');
    assert.match(expDisposition, /\.m3u8/, 'export names a .m3u8 file: ' + expDisposition);
    const m3u = await (await exp.blob()).text();
    assert.ok(m3u.startsWith('#EXTM3U'), 'm3u8 starts with the header');
    assert.equal((m3u.match(/#EXTINF:/g) || []).length, sampler.track_count,
        'one EXTINF line per playlist track');

    // Main-catalog playlist exports too.
    await post(fetch, '/api/music-libraries/select', { id: 'local' });
    const localPls = await (await fetch('/api/playlists')).json();
    const morning = localPls.find(p => p.id === 'playlist_morning');
    assert.ok(morning, 'main-catalog playlist present');
    const m3uLocal = await (await (await fetch('/api/playlists/' + morning.id + '/export')).blob()).text();
    assert.equal((m3uLocal.match(/#EXTINF:/g) || []).length, morning.track_count,
        'main-catalog export has one EXTINF per track');

    // ── Selection download ─────────────────────────────────────────────
    const localTracks = await (await fetch('/api/tracks')).json();
    const selected = localTracks.slice(0, 3);
    const dl = await post(fetch, '/api/tracks/download', { track_ids: selected.map(t => t.id) });
    assert.equal(dl.status, 200, 'selection download responds 200');
    const dlText = await (await dl.blob()).text();
    for (const track of selected) {
        assert.ok(dlText.includes(track.title), 'download body lists ' + track.title);
    }

    // ── Certificate download ───────────────────────────────────────────
    const cert = await fetch('/api/certificate/local-root');
    assert.equal(cert.status, 200, 'certificate endpoint no longer 404s');
    const certText = await (await cert.blob()).text();
    assert.match(certText, /BEGIN CERTIFICATE/, 'certificate body is PEM-shaped');
    assert.match(cert.headers.get('Content-Disposition'), /\.crt/);

    // ── Qobuz demo_start reachable ─────────────────────────────────────
    const q = await post(fetch, '/api/streaming/qobuz/demo_start');
    assert.equal(q.status, 200, 'qobuz demo_start command is reachable');
    const qStatus = await (await fetch('/api/streaming/qobuz/status')).json();
    assert.equal(qStatus.status, 'Playing', 'qobuz demo_start starts playback');

    // ── Local stream facts: real per-track samplerate_hz ───────────────
    await post(fetch, '/api/play', { track_id: 'local_demo_summer-on-the-block_1' });
    let st = (await (await fetch('/api/status')).json()).stream_info;
    assert.equal(st.samplerate_hz, 96000, 'hi-res local track reports 96 kHz');
    await post(fetch, '/api/play', { track_id: 'local_demo_color-radio_1' });
    st = (await (await fetch('/api/status')).json()).stream_info;
    assert.equal(st.samplerate_hz, 48000, '48 kHz local track reports 48 kHz');
    assert.equal(st.sample_rate, undefined, 'stream_info no longer carries the wrong field name');

    console.log('ok demo downloads + stream facts contract');
})().catch((err) => {
    console.error(err);
    process.exit(1);
});