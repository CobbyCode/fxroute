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
// Demo DSP stock mirrors .104: the +3/+6 dB entries are real selectable
// filter presets (no headroom), the convolver kernels ship as presets +
// IRs, and the limiter default matches .104 (on, -1 dB).
assert.match(routesSource, /presetEntry\('\+3'\)/);
assert.match(routesSource, /presetEntry\('\+6'\)/);
assert.match(routesSource, /Conv LR MinAlign Harman 30-300Hz -7dB/);
assert.match(routesSource, /Conv LR HybAlign BK 30-3000Hz -7dB/);
assert.doesNotMatch(routesSource, /Room Curve/);
assert.doesNotMatch(routesSource, /Vocal Boost/);
assert.match(routesSource, /thresholdDb: -1\.0/);
assert.match(routesSource, /presetMeterGainDb/);
assert.match(routesSource, /demoMeterOffsetDb/);
assert.doesNotMatch(routesSource, /Math\.abs\(dspHeadroom\)/);
assert.match(routesSource, /\/api\/dsp\/extras' && !post/);
assert.match(routesSource, /\/api\/streaming\/providers\/admin/);
assert.match(routesSource, /\/api\/system\/device-name/);
assert.match(routesSource, /\/api\/streaming\/qobuz\/auth\/login/);
assert.match(routesSource, /\/api\/measurements\/calibrations\/active/);
assert.match(htmlSource, /id="tidal-login-panel"/);
assert.match(htmlSource, /id="qobuz-login-panel"/);
assert.match(htmlSource, /id="settings-device-name-apply"/);

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
    // Spotify and Qobuz must draw from distinct catalogs: same metadata
    // (title, artist, album, cover) never appears on the other provider.
    const spotifyIds = new Set(state.spotify.list.map(t => String(t.id)));
    const qobuzIds = new Set(state.qobuz.qlist.map(t => String(t.id)));
    assert.ok(![...spotifyIds].some(id => qobuzIds.has(id)), 'Spotify/Qobuz demo queues must be disjoint');
    const spotifyTitles = new Set(state.spotify.list.map(t => t.title));
    const qobuzTitles = new Set(state.qobuz.qlist.map(t => t.title));
    assert.ok(![...spotifyTitles].some(title => qobuzTitles.has(title)), 'Spotify/Qobuz must not share track titles');
    // Each provider queue rotates its own album/artist so next/prev changes
    // title, artist, album and cover together.
    const spAlbum = new Set(state.spotify.list.map(t => t.album)).size;
    assert.ok(spAlbum >= 2, 'Spotify queue must span multiple albums');
    const qbAlbum = new Set(state.qobuz.qlist.map(t => t.album)).size;
    assert.ok(qbAlbum >= 2, 'Qobuz queue must span multiple albums');
    state.qobuz.toggleShuffle();
    assert.equal(state.qobuz.payload().shuffle, true);
    state.qobuz.cycleLoop();
    assert.equal(state.qobuz.payload().loop, 'playlist');

    // The demo starts in 2.2 mode on the 4-channel interface, with crossover
    // and derived sub delays visible (seeded like a configured system).
    const outputs = await (await demoFetch('/api/audio/outputs')).json();
    assert.equal(outputs.output_mode.mode, 'subwoofer-2.2');
    assert.equal(outputs.selected_output.channels, 4);
    assert.equal(outputs.output_mode.subwoofer.crossover_frequency_hz, 80);
    assert.equal(outputs.output_mode.subwoofer.slope, 'LR24');
    assert.ok(outputs.output_mode.derived_sub1_delay_ms > 0);
    assert.ok(outputs.output_mode.derived_sub2_delay_ms > 0);

    // ── Measurement simulation contract ─────────────────────────────────
    // Fixtures are the current .104 stock, assigned by name: plain Raw
    // sweeps per channel, Close-mic repeats as direct/L-R sources,
    // Convolver captures as mlp/secondary slots, stereo captures as the
    // integration slot, plus hybrid models, repeat summaries and the
    // auto-sub Before/After pair.
    const savedNames = context.FXROUTE_DEMO_MEASUREMENTS.savedMeasurements.map(m => m.name);
    for (const expected of ['Sweep-L-Raw', 'Sweep-R-Raw', 'Sweep-L/R-Raw',
        'Sweep-L-Convolver', 'Sweep-R-Convolver', 'Sweep-L/R-Convolver',
        'Sweep-Close-L-1', 'Sweep-Close-R-1',
        'Advanced 2.2 Mono · L', 'Advanced 2.2 Mono · R',
        'Auto-Sub-Optimize-2.2 Before L', 'Auto-Sub-Optimize-2.2 Before R',
        'Auto-Sub-Optimize-2.2 After L', 'Auto-Sub-Optimize-2.2 After R']) {
        assert.ok(savedNames.includes(expected), 'demo fixtures must include ' + expected);
    }
    assert.ok(savedNames.some(name => /^L\/R Repeat .* · L$/.test(name)));
    assert.ok(savedNames.some(name => /^L\/R Repeat .* · R$/.test(name)));
    const autosubKinds = new Set(context.FXROUTE_DEMO_MEASUREMENTS.savedMeasurements
        .filter(m => /Auto-Sub/.test(m.name)).map(m => m.measurement_kind));
    assert.deepEqual([...autosubKinds], ['auto_sub']);
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

    // ── Playback meter simulation contract ──────────────────────────────
    // Only the audible chain moves the visible level: +3/+6 dB filter
    // presets (real presets, no headroom), autogain, loudness, bass
    // enhancer; the protection limiter only clamps into limiting. The
    // snapshot flags limiting (level at/above the -1 dB threshold) and
    // clipping (above 0 dB), so Direct at -13 dB stays clean while a hot
    // +6 dB program into the limiter trips detection.
    state.playLocal(state.localTracks[0].id);
    const meterOf = () => state.getPeak();
    await (await demoFetch('/api/dsp/presets/load', { method: 'POST', body: JSON.stringify({ preset_name: 'Direct' }) })).json();
    state.getMeter().vu_db_l = -13; state.getMeter().vu_db_r = -13; state.getMeter().vu_fresh = true;
    assert.equal(meterOf().detected, false);
    await (await demoFetch('/api/dsp/presets/load', { method: 'POST', body: JSON.stringify({ preset_name: '+6' }) })).json();
    state.getMeter().vu_db_l = 0.5; state.getMeter().vu_db_r = 0.5; state.getMeter().vu_fresh = true;
    assert.equal(meterOf().detected, true);
    state.getMeter().vu_db_l = -0.5; state.getMeter().vu_db_r = -0.5; state.getMeter().vu_fresh = true;
    assert.equal(meterOf().detected, true);
    await (await demoFetch('/api/dsp/extras', { method: 'POST', body: JSON.stringify({ loudnessEnabled: true, loudnessStrength: 10 }) })).json();
    const loudExtras = await (await demoFetch('/api/dsp/presets')).json();
    assert.equal(loudExtras.global_extras.loudness.enabled, true);
    await (await demoFetch('/api/dsp/extras', { method: 'POST', body: JSON.stringify({ loudnessEnabled: false }) })).json();
    await (await demoFetch('/api/dsp/presets/load', { method: 'POST', body: JSON.stringify({ preset_name: 'Direct' }) })).json();
    state.getMeter().vu_db_l = -13; state.getMeter().vu_db_r = -13; state.getMeter().vu_fresh = true;
    assert.equal(meterOf().detected, false);
    const dspAfterMeter = await (await demoFetch('/api/dsp/presets')).json();
    assert.ok(dspAfterMeter.presets.some(p => p.name === '+3'));
    assert.ok(dspAfterMeter.presets.some(p => p.name === '+6'));
    assert.equal(dspAfterMeter.active_preset, 'Direct');

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

    // ── Auto Sub Optimize: staged run with complete mode-aware result ───
    // The run must advance through queued/running stages (with live baseline
    // data for the graph) and finish with a full 2.2 result — finite Sub 1 /
    // Sub 2 alignment and derived delays, never '? ms'.
    const autoSubStart = await (await demoFetch('/api/measurements/auto-sub-optimize/start', { method: 'POST' })).json();
    assert.equal(autoSubStart.job.status, 'queued');
    const autoSubId = autoSubStart.job.id;
    // The immediate HTTP poll is still queued (the run has not progressed
    // yet); staged progression is exercised deterministically through the
    // exported payload builder with injected elapsed time.
    const autoSubQueuedPoll = await (await demoFetch('/api/measurements/auto-sub-optimize/jobs/' + autoSubId)).json();
    assert.equal(autoSubQueuedPoll.job.status, 'queued');
    const stageAt = (ms) => context.FXROUTE_DEMO_API.autoSubJobPayload(autoSubId, ms);
    const baselineStage = stageAt(1500);
    assert.equal(baselineStage.status, 'running');
    assert.ok(baselineStage.progress && baselineStage.progress.stage === 'coarse');
    assert.ok(baselineStage.baseline_measurement, 'running job must push baseline for the live graph');
    assert.ok(Array.isArray(baselineStage.baseline_measurement.traces)
        && baselineStage.baseline_measurement.traces.length);
    assert.equal(stageAt(3000).progress.stage, 'sub1_coarse');
    assert.equal(stageAt(3000).progress.candidate_current > 0, true);
    assert.equal(stageAt(5000).progress.stage, 'sub2_coarse');
    assert.equal(stageAt(5000).progress.candidate_current > 0, true);
    assert.equal(stageAt(7000).progress.stage, 'fine');
    assert.equal(stageAt(8000).progress.stage, 'combined_matrix');
    assert.ok(stageAt(8000).progress.sweep_current > 0);
    const autoSubDone = context.FXROUTE_DEMO_API.autoSubJobPayload(autoSubId, 100000);
    assert.equal(autoSubDone.status, 'completed');
    const autoSubResult = autoSubDone.result;
    assert.equal(autoSubResult.mode, 'subwoofer-2.2');
    assert.equal(autoSubResult.applied, true);
    assert.ok(Number.isFinite(autoSubResult.applied_sub1_alignment_ms));
    assert.ok(Number.isFinite(autoSubResult.applied_sub2_alignment_ms));
    assert.ok(Number.isFinite(autoSubResult.original_sub1_alignment_ms));
    assert.ok(Number.isFinite(autoSubResult.original_sub2_alignment_ms));
    assert.ok(autoSubResult.applied_sub1_alignment_ms !== autoSubResult.original_sub1_alignment_ms);
    assert.ok(Number.isFinite(autoSubResult.derived_main_delay_ms));
    assert.ok(Number.isFinite(autoSubResult.derived_sub1_delay_ms));
    assert.ok(Number.isFinite(autoSubResult.derived_sub2_delay_ms));
    assert.ok(autoSubResult.sub1_coarse_winner && Number.isFinite(autoSubResult.sub1_coarse_winner.delay_ms));
    assert.ok(autoSubResult.sub2_coarse_winner && Number.isFinite(autoSubResult.sub2_coarse_winner.delay_ms));
    assert.ok(Number.isFinite(autoSubResult.left_score_pct));
    assert.ok(Number.isFinite(autoSubResult.right_score_pct));
    assert.ok(Number.isFinite(autoSubResult.overall_score_pct));
    assert.ok(autoSubResult.winner && Number.isFinite(autoSubResult.winner.score_pct));
    assert.ok(autoSubResult.baseline_measurement && Array.isArray(autoSubResult.baseline_measurement.traces));
    assert.ok(autoSubResult.confirmation_measurement && Array.isArray(autoSubResult.confirmation_measurement.traces));
    // The applied alignment must land in the audio-output model so the
    // subwoofer card shows the new derived delays after the run.
    const outputsAfterAutoSub = await (await demoFetch('/api/audio/outputs')).json();
    assert.equal(outputsAfterAutoSub.output_mode.derived_sub1_delay_ms, autoSubResult.applied_sub1_alignment_ms);
    assert.equal(outputsAfterAutoSub.output_mode.derived_sub2_delay_ms, autoSubResult.applied_sub2_alignment_ms);
    assert.equal(outputsAfterAutoSub.output_mode.subwoofers.sub1.alignment_ms, autoSubResult.applied_sub1_alignment_ms);
    // 2.1 mode must still produce a single-sub result with coarse/fine winners.
    await (await demoFetch('/api/audio/output-mode', { method: 'POST', body: JSON.stringify({ mode: 'subwoofer-2.1' }) })).json();
    const autoSub21Start = await (await demoFetch('/api/measurements/auto-sub-optimize/start', { method: 'POST' })).json();
    const autoSub21Done = context.FXROUTE_DEMO_API.autoSubJobPayload(autoSub21Start.job.id, 100000);
    assert.equal(autoSub21Done.status, 'completed');
    assert.equal(autoSub21Done.result.mode, 'subwoofer-2.1');
    assert.ok(Number.isFinite(autoSub21Done.result.applied_alignment_ms));
    assert.ok(autoSub21Done.result.coarse_winner && Number.isFinite(autoSub21Done.result.coarse_winner.delay_ms));
    assert.ok(autoSub21Done.result.runner_up && Number.isFinite(autoSub21Done.result.runner_up.delay_ms));
    assert.ok(autoSub21Done.result.fine_scan && autoSub21Done.result.fine_scan.status === 'completed');
    // 2.2 Stereo Bass: per-side stages and winners (Left Sub / Right Sub).
    await (await demoFetch('/api/audio/output-mode', { method: 'POST', body: JSON.stringify({ mode: 'subwoofer-2.2-stereo' }) })).json();
    const sbOutputs = await (await demoFetch('/api/audio/outputs')).json();
    assert.match(sbOutputs.output_mode.routing.status, /Left Sub/);
    assert.match(sbOutputs.output_mode.routing.status, /Right Sub/);
    const autoSubSbStart = await (await demoFetch('/api/measurements/auto-sub-optimize/start', { method: 'POST' })).json();
    const sbStageAt = (ms) => context.FXROUTE_DEMO_API.autoSubJobPayload(autoSubSbStart.job.id, ms);
    assert.equal(sbStageAt(3000).progress.stage, 'left_sub');
    assert.equal(sbStageAt(5000).progress.stage, 'right_sub');
    const autoSubSbDone = sbStageAt(100000);
    assert.equal(autoSubSbDone.status, 'completed');
    assert.equal(autoSubSbDone.result.mode, 'subwoofer-2.2-stereo');
    assert.ok(autoSubSbDone.result.left_winner && Number.isFinite(autoSubSbDone.result.left_winner.delay_ms));
    assert.ok(autoSubSbDone.result.right_winner && Number.isFinite(autoSubSbDone.result.right_winner.delay_ms));
    assert.ok(Number.isFinite(autoSubSbDone.result.applied_sub1_alignment_ms));
    assert.ok(Number.isFinite(autoSubSbDone.result.applied_sub2_alignment_ms));
    assert.ok(Number.isFinite(autoSubSbDone.result.overall_score_pct));
    assert.ok(autoSubSbDone.result.winner && Number.isFinite(autoSubSbDone.result.winner.overall_score_pct));
    const sbOutputsAfter = await (await demoFetch('/api/audio/outputs')).json();
    assert.equal(sbOutputsAfter.output_mode.derived_sub1_delay_ms, autoSubSbDone.result.applied_sub1_alignment_ms);
    assert.equal(sbOutputsAfter.output_mode.derived_sub2_delay_ms, autoSubSbDone.result.applied_sub2_alignment_ms);
    // Restore the demo's default 2.2 mode.
    await (await demoFetch('/api/audio/output-mode', { method: 'POST', body: JSON.stringify({ mode: 'subwoofer-2.2' }) })).json();

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
