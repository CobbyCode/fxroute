#!/usr/bin/env node
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { execFileSync } = require('node:child_process');
const os = require('node:os');

const root = path.join(__dirname, '..');
const librarySource = fs.readFileSync(path.join(root, 'demo', 'data', 'library.js'), 'utf8');
const radioSource = fs.readFileSync(path.join(root, 'demo', 'data', 'radio.js'), 'utf8');
const measurementsSource = fs.readFileSync(path.join(root, 'demo', 'data', 'measurements.js'), 'utf8');
const stateSource = fs.readFileSync(path.join(root, 'demo', 'state.js'), 'utf8');
const bootSource = fs.readFileSync(path.join(root, 'demo', 'boot.js'), 'utf8');
const buildSource = fs.readFileSync(path.join(root, 'scripts', 'build_demo.py'), 'utf8');
const routesSource = fs.readFileSync(path.join(root, 'demo', 'routes.js'), 'utf8');
const appSource = fs.readFileSync(path.join(root, "demo", "dist", "static", "app.js"), 'utf8');
const htmlSource = fs.readFileSync(path.join(root, "demo", "dist", "index.html"), 'utf8');
const streamingSource = fs.readFileSync(path.join(root, "demo", "dist", "static", "streaming.js"), 'utf8');

// A fresh simulated page load: re-runs every demo fixture/module in a new
// VM context, exactly like a browser reload of the built demo page.
function makeDemoContext() {
    const ctx = {
        window: {},
        setInterval() { return 0; },
        clearInterval() {},
        Date,
        Math,
        console,
        URLSearchParams,
        fetch() { return Promise.reject(new Error('unexpected real fetch')); },
    };
    ctx.window = ctx;
    vm.createContext(ctx);
    vm.runInContext(librarySource, ctx);
    vm.runInContext(radioSource, ctx);
    vm.runInContext(measurementsSource, ctx);
    vm.runInContext(stateSource, ctx);
    vm.runInContext(routesSource, ctx);
    return ctx;
}

// Snapshot of the demo stock as the simulated API exposes it. The demo has
// no persistence layer for library state, so this must be identical after
// every fresh load, regardless of what a previous session mutated.
async function demoStock(ctx) {
    const albums = await (await ctx.fetch('/api/albums')).json();
    const tracks = await (await ctx.fetch('/api/tracks')).json();
    const playlists = await (await ctx.fetch('/api/playlists')).json();
    const tidalFavIds = await (await ctx.fetch('/api/streaming/tidal/favorites/ids')).json();
    return {
        albums_total: albums.length,
        albums_fav: albums.filter(a => a.favorite).length,
        tracks_total: tracks.length,
        tracks_fav: tracks.filter(t => t.favorite).length,
        artists: new Set(albums.map(a => a.artist)).size,
        playlists_total: playlists.length,
        tidal_albums: ctx.FXROUTE_DEMO_LIBRARY.tidalAlbums.length,
        tidal_tracks: ctx.FXROUTE_DEMO_LIBRARY.tidalTracks.length,
        tidal_artists: ctx.FXROUTE_DEMO_LIBRARY.tidalArtists.length,
        tidal_playlists: ctx.FXROUTE_DEMO_LIBRARY.tidalPlaylists.length,
        tidal_fav_tracks: tidalFavIds.tracks.length,
        tidal_fav_albums: tidalFavIds.albums.length,
        tidal_fav_artists: tidalFavIds.artists.length,
        tidal_fav_playlists: tidalFavIds.playlists.length,
        stations_saved: ctx.FXROUTE_DEMO_RADIO.savedStations.length,
        stations_catalog: ctx.FXROUTE_DEMO_RADIO.catalogStations.length,
    };
}

const context = makeDemoContext();
const state = context.FXROUTE_DEMO_STATE;
const demoFetch = context.fetch;

assert.match(bootSource, /demo-banner-close/);
assert.match(bootSource, /sessionStorage/);

// The demo boot script runs before the real measurement modules. The graph
// bridge must therefore be installed after those modules become available.
const bridgeTimers = [];
const bridgeContext = {
    window: {},
    document: {
        body: { appendChild() {} },
        createElement() {
            return {
                setAttribute() {},
                querySelector() { return { addEventListener() {} }; },
                className: '',
                innerHTML: '',
            };
        },
    },
    sessionStorage: { getItem() { return null; }, setItem() {} },
    DemoWebSocket: function DemoWebSocket() {},
    fetch() { return Promise.resolve({}); },
    setTimeout(fn, delay) { bridgeTimers.push({ fn, delay }); return bridgeTimers.length; },
    console,
};
bridgeContext.window = bridgeContext;
bridgeContext.FXRouteMeasurementUI = {};
vm.createContext(bridgeContext);
vm.runInContext(bootSource, bridgeContext);
const deferredBridge = bridgeTimers.find(timer => timer.delay === 0);
assert.ok(deferredBridge, 'measurement graph bridge should be deferred until frontend modules load');
bridgeContext.FXRouteMeasurementDsp = { smoothMeasurementTracePoints() {} };
deferredBridge.fn();
assert.equal(typeof bridgeContext.FXRouteMeasurementUI.smoothMeasurementTracePoints, 'function');

assert.match(buildSource, /data\/radio\.js/);
assert.match(buildSource, /state\.js\?v=\d+/);
assert.match(buildSource, /static_src\s*\/\s*['"]demo['"]|demo_art/);
assert.ok(fs.existsSync(path.join(root, 'static', 'demo', '1800ECLIPSE.jpg')));
assert.ok(fs.existsSync(path.join(root, 'demo', 'dist', 'static', 'demo', '1800ECLIPSE.jpg')));
// Web fonts referenced by style.css must also be copied into the dist snapshot.
assert.ok(fs.existsSync(path.join(root, 'demo', 'dist', 'static', 'fonts', 'Geist.woff2')));
assert.ok(fs.existsSync(path.join(root, 'demo', 'dist', 'static', 'fonts', 'GeistMono.woff2')));

// ── Asset mirror regression ────────────────────────────────────────────
// The demo must mirror the canonical product frontend, not a hand-maintained
// per-asset list: every file in main/static has to be present in
// demo/dist/static, except the small deliberate exclusion set (page shell,
// unreferenced logo/branding design sources — see STATIC_EXCLUDE in
// build_demo.py). Otherwise a newly added product asset silently 404s in the
// published demo, as the web fonts did once.
const excludedProductAssets = new Set(['index.html', 'fxroute-logo.jpg', 'fxroute-logo.png', 'branding']);
const frontendRoot = process.env.FXROUTE_FRONTEND_ROOT || root;
const mainStaticRoot = path.join(frontendRoot, 'static');
function collectFiles(dir, base) {
    const files = [];
    for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
        const abs = path.join(dir, entry.name);
        if (entry.isDirectory()) files.push(...collectFiles(abs, base));
        else files.push(path.relative(base, abs));
    }
    return files;
}
assert.ok(fs.existsSync(mainStaticRoot), 'canonical frontend static dir not found: ' + mainStaticRoot);
const isExcluded = (file) => excludedProductAssets.has(file)
    || excludedProductAssets.has(file.split(path.sep)[0]);
const mainStaticFiles = collectFiles(mainStaticRoot, mainStaticRoot)
    .filter((file) => !isExcluded(file));
const distStaticRoot = path.join(root, 'demo', 'dist', 'static');
const distStaticFiles = new Set(collectFiles(distStaticRoot, distStaticRoot));
for (const file of mainStaticFiles) {
    assert.ok(distStaticFiles.has(file), 'demo dist snapshot is missing product asset: ' + file);
}
// The exclusion set must stay in sync with the build script, and the build
// must mirror the canonical tree instead of maintaining a per-asset copy
// list, so future product assets land in the snapshot automatically.
assert.match(buildSource, /STATIC_EXCLUDE/);
for (const excluded of excludedProductAssets) {
    assert.ok(buildSource.includes(excluded), 'build STATIC_EXCLUDE is missing: ' + excluded);
}
assert.match(buildSource, /copytree/);
assert.match(buildSource, /ignore_patterns/);
assert.doesNotMatch(buildSource, /style\.css"\s*,\s*"app\.js"/);

// Every asset the built page references (src/href under /static/ or ./demo/)
// must exist in the self-contained snapshot, i.e. the page is 404-free when
// served standalone from demo/dist.
for (const m of htmlSource.matchAll(/(?:src|href)="((?:\.\/)?(?:static|demo)\/[^"?#]+)/g)) {
    const rel = m[1].replace(/^\.\//, '');
    assert.ok(
        fs.existsSync(path.join(root, 'demo', 'dist', rel)),
        'built page references missing asset: ' + m[1],
    );
}

// ── Base-path (subpath) build ──────────────────────────────────────────
// A subpath build (e.g. a GitHub Pages project site at /fxroute/) must
// prefix every absolute /static/ reference — built page, copied JS/CSS and
// the web manifest — so the snapshot works under the subpath. The demo
// layer keeps its relative ./demo/ references, which resolve under the
// subpath.
const subpathOut = fs.mkdtempSync(path.join(os.tmpdir(), 'fxroute-demo-'));
try {
    execFileSync('python3', ['scripts/build_demo.py', '--base-path', '/fxroute/', '--out', subpathOut],
        { cwd: root, stdio: 'pipe' });
    const subIndex = fs.readFileSync(path.join(subpathOut, 'index.html'), 'utf8');
    assert.match(subIndex, /src="\/fxroute\/static\/app\.js\?v=\d+\.\d+\.\d+"/);
    assert.doesNotMatch(subIndex, /src="\/static\/app\.js/);
    assert.match(subIndex, /src="\.\/demo\/boot\.js\?v=\d+"/);
    const subApp = fs.readFileSync(path.join(subpathOut, 'static', 'app.js'), 'utf8');
    assert.ok(subApp.includes('/fxroute/static/artwork-placeholder.svg'));
    assert.ok(!subApp.includes("'/static/artwork-placeholder.svg"));
    const subStyle = fs.readFileSync(path.join(subpathOut, 'static', 'style.css'), 'utf8');
    assert.ok(subStyle.includes("url('/fxroute/static/artwork-placeholder.svg?v=2')"));
    const subManifest = fs.readFileSync(path.join(subpathOut, 'static', 'site.webmanifest'), 'utf8');
    assert.ok(subManifest.includes('/fxroute/static/android-chrome-192x192.png'));
    const subRadio = fs.readFileSync(path.join(subpathOut, 'demo', 'data', 'radio.js'), 'utf8');
    assert.ok(subRadio.includes('/fxroute/static/station-art/groovesalad.png'));
    const subLibrary = fs.readFileSync(path.join(subpathOut, 'demo', 'data', 'library.js'), 'utf8');
    assert.ok(subLibrary.includes("'/fxroute/static/demo/'"));
} finally {
    fs.rmSync(subpathOut, { recursive: true, force: true });
}
const firstLibraryAlbum = context.FXROUTE_DEMO_LIBRARY.albums[0];
assert.match(firstLibraryAlbum.demo_cover_url, /^\/static\/demo\/.*\.jpg$/);
assert.match(appSource, /album\.demo_cover_url/);
assert.match(buildSource, /static_src\s*\/\s*['"]demo['"]|demo_art/);
assert.match(routesSource, /suspend_supported: true/);
assert.match(routesSource, /power_off_supported: true/);
assert.match(routesSource, /\/api\/system\/power\/suspend.*post/s);
assert.match(routesSource, /power_off_supported: true/);
assert.match(routesSource, /status: 'simulated'.*action: 'suspend'/s);
assert.match(routesSource, /status: 'simulated'.*action: 'power-off'/s);
assert.doesNotMatch(routesSource, /child_process|exec\(|spawn\(|systemctl|shutdown|loginctl/);
assert.match(htmlSource, /id="power-menu-toggle"[\s\S]*aria-haspopup="menu"/);
assert.match(htmlSource, /id="power-suspend"[\s\S]*>\s*[\s\S]*Suspend\s*</);
assert.match(htmlSource, /id="power-shutdown"[\s\S]*>\s*[\s\S]*Shut down\s*</);
assert.match(appSource, /power-menu-toggle/);
assert.match(appSource, /power-suspend/);
assert.match(appSource, /power-shutdown/);
assert.match(appSource, /setPowerMenuOpen\(!open\)/);
assert.match(appSource, /document\.addEventListener\('click', \(\) => setPowerMenuOpen\(false\)\)/);
assert.match(appSource, /document\.addEventListener\('keydown', ev => \{[\s\S]*Escape[\s\S]*setPowerMenuOpen\(false\)/);
assert.doesNotMatch(streamingSource, /Play demo track/);
assert.doesNotMatch(streamingSource, /data\.demo_boot \? 'demo-start'/);
// The demo serves the canonical streaming.js live; these assertions track
// the current canonical idle wording for the provider cards.
assert.match(streamingSource, /Spotify is not running/);
assert.match(streamingSource, /Nothing is playing/);
assert.match(routesSource, /id: 'spotify'[\s\S]*authenticated: true[\s\S]*connected: true/);
assert.match(routesSource, /id: 'tidal'[\s\S]*authenticated: true[\s\S]*connected: true/);

(async () => {
    const capabilities = await (await demoFetch('/api/system/power')).json();
    assert.equal(capabilities.suspend_supported, true);
    assert.equal(capabilities.power_off_supported, true);

    const suspend = await (await demoFetch('/api/system/power/suspend', { method: 'POST' })).json();
    const powerOff = await (await demoFetch('/api/system/power/power-off', { method: 'POST' })).json();
    assert.equal(JSON.stringify(suspend), JSON.stringify({ ok: true, status: 'simulated', action: 'suspend' }));
    assert.equal(JSON.stringify(powerOff), JSON.stringify({ ok: true, status: 'simulated', action: 'power-off' }));

    assert.ok(state.stations.some((station) => station.id === 'groovesalad'));
assert.ok(state.catalogStations.some((station) => station.id === 'rp-main'));
    assert.equal(state.catalogStations.some((station) => station.id === 'drumandbass'), false);

const radioTrack = state.playRadio('groovesalad');
const radio = state.getPlayback();
    assert.equal(radio.playback_owner, 'radio');
    assert.equal(radio.duration, 0);
    assert.equal(radio.current_track.duration, 0);
    assert.equal(radio.radio_metadata.duration_seconds, null);
    assert.equal(radio.radio_metadata.progress_seconds, null);
    // Radio station metadata mirrors the real backend (radio/metadata.py):
    // provider identity and timing/history shape depend on the station, and
    // stations whose integration delivers no metadata get none invented.
    const somaMeta = radio.radio_metadata;
    assert.equal(somaMeta.provider, 'somafm');
    assert.equal(somaMeta.source, 'provider');
    assert.ok(somaMeta.title);
    assert.ok(somaMeta.artist);
    assert.ok(somaMeta.album);
    // The real SomaFM songs API ships current + previous songs in one
    // response, so "Recently played" must be visible immediately.
    assert.equal(somaMeta.history.length, 3);
    assert.ok(somaMeta.history.every((e) => e.title && e.artist));
    assert.equal(somaMeta.history[0].title, 'Levee Stomp');
    assert.equal(somaMeta.duration_seconds, null);
    assert.equal(somaMeta.progress_seconds, null);
    assert.equal(somaMeta.cover_url, '/static/station-art/groovesalad.png');

    // SomaFM Live has no songs API in the real backend: plain ICY fallback.
    state.playRadio('live');
    const liveMeta = state.getPlayback().radio_metadata;
    assert.equal(liveMeta.provider, null);
    assert.equal(liveMeta.source, 'icy');
    assert.ok(liveMeta.title);
    assert.equal(liveMeta.artist, null);
    assert.equal(liveMeta.duration_seconds, null);
    assert.equal(liveMeta.history.length, 0);

    // Catalog stations: FIP delivers timed metadata (track slider, no history).
    await (await demoFetch('/api/station-catalog/fip-jazz/selection', { method: 'POST' })).json();
    state.playRadio('station_fip-jazz');
    const fipMeta = state.getPlayback().radio_metadata;
    assert.equal(fipMeta.provider, 'fip');
    assert.equal(fipMeta.source, 'provider');
    assert.ok(fipMeta.title);
    assert.ok(fipMeta.artist);
    assert.ok(fipMeta.duration_seconds > 0);
    assert.ok(fipMeta.progress_seconds >= 0);
    assert.equal(fipMeta.history.length, 0);

    // Radio Calico has no metadata provider in the real backend: ICY only,
    // no history, nothing invented.
    await (await demoFetch('/api/station-catalog/radio-calico/selection', { method: 'POST' })).json();
    state.playRadio('station_radio-calico');
    const calicoMeta = state.getPlayback().radio_metadata;
    assert.equal(calicoMeta.provider, null);
    assert.equal(calicoMeta.source, 'icy');
    assert.ok(calicoMeta.title);
    assert.equal(calicoMeta.artist, null);
    assert.equal(calicoMeta.duration_seconds, null);
    assert.equal(calicoMeta.history.length, 0);

    // KEXP delivers titles without timing and without history.
    await (await demoFetch('/api/station-catalog/kexp-main/selection', { method: 'POST' })).json();
    state.playRadio('station_kexp-main');
    const kexpMeta = state.getPlayback().radio_metadata;
    assert.equal(kexpMeta.provider, 'kexp');
    assert.equal(kexpMeta.source, 'provider');
    assert.ok(kexpMeta.title);
    assert.ok(kexpMeta.artist);
    assert.ok(kexpMeta.album);
    assert.equal(kexpMeta.duration_seconds, null);
    assert.equal(kexpMeta.history.length, 0);

    const initialSpotify = state.spotify.snapshot();
    assert.equal(initialSpotify.connected, true);
    assert.equal(initialSpotify.status, 'Paused');
    assert.ok(initialSpotify.title);
    assert.ok(initialSpotify.artUrl);
    assert.ok(initialSpotify.duration > 0);

    const spotifyTrack = state.spotify.demoStart();
    assert.ok(spotifyTrack);
    const spotify = state.spotify.snapshot();
    assert.equal(spotify.status, 'Playing');
    assert.equal(state.getPlayback().playback_owner, 'spotify');
    assert.ok(spotify.duration > 0);
    assert.ok(Array.isArray(state.spotify.list));
    assert.ok(state.spotify.list.length <= 4);
    state.spotify.toggleShuffle();
    assert.equal(state.spotify.snapshot().shuffle, true);
    state.spotify.cycleLoop();
    assert.equal(state.spotify.snapshot().loop, 'playlist');
    const spotifyTitle = state.spotify.snapshot().title;
    state.spotify.next();
    assert.notEqual(state.spotify.snapshot().title, spotifyTitle);

    const initialQobuz = state.qobuz.payload();
    assert.equal(initialQobuz.connected, true);
    assert.equal(initialQobuz.status, 'Paused');
    assert.ok(initialQobuz.title);
    assert.ok(initialQobuz.artUrl);
    assert.ok(initialQobuz.duration > 0);

    const qobuzTrack = state.qobuz.demoStart();
    assert.ok(qobuzTrack);
    const qobuz = state.qobuz.payload();
    assert.equal(qobuz.status, 'Playing');
    assert.equal(state.getPlayback().playback_owner, 'qobuz');
    assert.ok(qobuz.duration > 0);
    assert.ok(Array.isArray(state.qobuz.qlist));
    assert.ok(state.qobuz.qlist.length <= 4);
    state.qobuz.toggleShuffle();
    assert.equal(state.qobuz.payload().shuffle, true);
    state.qobuz.cycleLoop();
    assert.equal(state.qobuz.payload().loop, 'playlist');

    // ── Measurement simulation contract ─────────────────────────────────
    const directLeft = state.makeMeasurement({ role: 'direct', channel: 'left', id: 'demo_direct_l' });
    assert.equal(directLeft.analysis.direct_response.usable, true);
    assert.ok(directLeft.analysis.direct_response.points.length > 0);
    assert.equal(directLeft.analysis.reference_path.capture_mode, 'dual-channel');
    const directRight = state.makeMeasurement({ role: 'direct', channel: 'right', id: 'demo_direct_r' });
    const timingDelta = Math.abs(
        directRight.analysis.reference_path.acoustic_arrival_corrected_ms
        - directLeft.analysis.reference_path.acoustic_arrival_corrected_ms,
    );
    assert.ok(timingDelta <= 2.5, 'direct L/R timing delta must satisfy the position check');
    const mlpLeft = state.makeMeasurement({ role: 'mlp', channel: 'left', id: 'demo_mlp_l' });
    assert.ok(mlpLeft.analysis.complex_response.points.length > 0);
    assert.equal(mlpLeft.analysis.complex_response.points[0].length, 3);
    const mlpRight = state.makeMeasurement({ role: 'mlp', channel: 'right', id: 'demo_mlp_r' });
    const integration = state.makeMeasurement({ role: 'integration', channel: 'stereo', id: 'demo_int' });
    const lp = mlpLeft.analysis.complex_response.points;
    const rp = mlpRight.analysis.complex_response.points;
    const ip = integration.analysis.complex_response.points;
    assert.ok(lp.length > 0 && rp.length > 0 && ip.length > 0);
    // The integration complex response must equal the exact L+R sum so the
    // subwoofer alignment check passes ('ok', not 'poor').
    for (let idx = 0; idx < Math.min(lp.length, rp.length, ip.length); idx += 1) {
        const f = lp[idx][0];
        if (f < 20 || f > 500) continue;
        const pred = Math.hypot(lp[idx][1] + rp[idx][1], lp[idx][2] + rp[idx][2]);
        const meas = Math.hypot(ip[idx][1], ip[idx][2]);
        assert.ok(Math.abs(pred - meas) < 1e-9, 'integration response must match L+R sum at ' + f + ' Hz');
    }

    const lrJobId = state.startLrRepeatMeasurement({ base_name: 'Contract LR' });
    const lrPoll = state.jobPayload(lrJobId);
    assert.equal(lrPoll.job_kind, 'lr-repeat');
    assert.match(lrPoll.message, /L\/R repeat/);

    const splGet = await (await demoFetch('/api/measurements/spl-calibration')).json();
    assert.equal(splGet.automatic.available, true);
    assert.equal(splGet.automatic.microphone_model, 'UMIK-1');
    assert.equal(splGet.target_spl_db, 83);
    const splAuto = await (await demoFetch('/api/measurements/spl-calibration/automatic', { method: 'POST' })).json();
    assert.equal(splAuto.status, 'ok');
    assert.ok(splAuto.measured_spl_db >= 40 && splAuto.measured_spl_db <= 130);
    const splApply = await (await demoFetch('/api/measurements/spl-calibration/apply', { method: 'POST', body: JSON.stringify({ measured_spl_db: 82.6 }) })).json();
    assert.equal(splApply.status, 'ok');
    assert.equal(splApply.required_adjustment_db, 0.4);
    assert.equal(splApply.calibrated, true);

    const lrStart = await (await demoFetch('/api/measurements/lr-repeat/start', { method: 'POST', body: JSON.stringify({ base_name: 'Contract LR' }) })).json();
    assert.equal(lrStart.job.job_kind, 'lr-repeat');
    assert.equal(lrStart.job.status, 'running');
    const lrStartPoll = await (await demoFetch('/api/measurements/jobs/' + lrStart.job.id)).json();
    assert.equal(lrStartPoll.job.job_kind, 'lr-repeat');
    assert.ok(lrStartPoll.job.input_level);

    // ── Demo stock reset/restore ───────────────────────────────────────
    // Playing around in a session (unfavoriting, deleting/creating
    // playlists) must never thin out the demo: a fresh load has to rebuild
    // the full initial stock from the fixtures. Runs in its own isolated
    // session so it is independent of the mutations above.
    const session = makeDemoContext();
    const baselineStock = await demoStock(session);
    const sessionAlbums = await (await session.fetch('/api/albums')).json();
    const sessionTracks = await (await session.fetch('/api/tracks')).json();
    const favAlbum = sessionAlbums.find(a => a.favorite);
    const favTrack = sessionTracks.find(t => t.favorite);
    const anyTrack = sessionTracks[0];
    assert.ok(favAlbum && favTrack && anyTrack, 'demo fixtures must provide favorites and tracks');
    await session.fetch('/api/albums/' + encodeURIComponent(favAlbum.id) + '/favorite',
        { method: 'POST', body: JSON.stringify({ favorite: false }) });
    await session.fetch('/api/tracks/' + encodeURIComponent(favTrack.id) + '/favorite',
        { method: 'POST', body: JSON.stringify({ favorite: false }) });
    const sessionPlaylists = await (await session.fetch('/api/playlists')).json();
    for (const pl of sessionPlaylists.slice(0, 2)) {
        await session.fetch('/api/playlists/' + pl.id, { method: 'DELETE' });
    }
    await session.fetch('/api/playlists', { method: 'POST',
        body: JSON.stringify({ name: 'Reset Test', track_ids: [anyTrack.id] }) });
    for (const type of ['albums', 'tracks', 'artists', 'playlists']) {
        const list = await (await session.fetch('/api/streaming/tidal/favorites?type=' + type)).json();
        const item = list[0];
        assert.ok(item, 'tidal demo favorites must be non-empty for ' + type);
        await session.fetch('/api/streaming/tidal/' + type + '/' + encodeURIComponent(item.id) + '/favorite',
            { method: 'POST', body: JSON.stringify({ favorite: false }) });
    }
    const mutatedStock = await demoStock(session);
    assert.notDeepEqual(mutatedStock, baselineStock, 'reset-test mutations must change demo state');
    const restoredStock = await demoStock(makeDemoContext());
    assert.deepEqual(restoredStock, baselineStock, 'fresh reload must restore the full initial demo stock');

    console.log('ok demo behavior contract');
})();
