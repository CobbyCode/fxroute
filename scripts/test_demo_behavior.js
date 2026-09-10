#!/usr/bin/env node
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { execFileSync } = require('node:child_process');
const os = require('node:os');

const root = path.join(__dirname, '..');
const librarySource = fs.readFileSync(path.join(root, 'demo', 'data', 'library.js'), 'utf8');
const library2Source = fs.readFileSync(path.join(root, 'demo', 'data', 'library2.js'), 'utf8');
const library3Source = fs.readFileSync(path.join(root, 'demo', 'data', 'library3.js'), 'utf8');
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
        FormData,
        fetch() { return Promise.reject(new Error('unexpected real fetch')); },
    };
    ctx.window = ctx;
    vm.createContext(ctx);
    vm.runInContext(librarySource, ctx);
    vm.runInContext(library2Source, ctx);
    vm.runInContext(library3Source, ctx);
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
const serveSource = fs.readFileSync(path.join(root, 'scripts', 'serve_demo.py'), 'utf8');
assert.match(serveSource, /from urllib\.parse import unquote, urlsplit/);
assert.match(serveSource, /path = unquote\(urlsplit\(self\.path\)\.path\)/);
assert.match(buildSource, /static_src\s*\/\s*['"]demo['"]|demo_art/);
assert.ok(fs.existsSync(path.join(root, 'static', 'demo', 's1-nova-static-midnight-relay.jpg')));
assert.ok(fs.existsSync(path.join(root, 'demo', 'dist', 'static', 'demo', 's1-nova-static-midnight-relay.jpg')));
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
// Every demo cover URL must resolve to a real pooled file, so no cover URL may
// carry percent-encoding (it 404s on the live demo route).
const demoArtFiles = new Set(fs.readdirSync(path.join(root, 'static', 'demo')));
for (const artist of context.FXROUTE_DEMO_LIBRARY.tidalArtists) {
    assert.ok(artist.image_url && !artist.image_url.includes('%'),
        `cover URL must not be percent-encoded for ${artist.name}: ${artist.image_url}`);
    assert.ok(demoArtFiles.has(decodeURIComponent(artist.image_url.split('/').pop().replace(/\.jpg$/, '')) + '.jpg'),
        `cover file missing for ${artist.name}: ${artist.image_url}`);
}
const marlowe = context.FXROUTE_DEMO_LIBRARY.tidalArtists.find(a => a.id === 't_artist_01');
assert.ok(marlowe && marlowe.image_url === '/static/demo/t-the-marlowe-ensemble-velvet-skyline.jpg');
assert.ok(fs.existsSync(path.join(root, 'static', 'demo', 't-the-marlowe-ensemble-velvet-skyline.jpg')));
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

    // ── Source mode: stereo-pair input simulation ────────────────────
    // Mirrors the real stereo-pair abstraction: the MOTU M4 multichannel
    // interface is offered as adjacent stereo pairs (never mono channels),
    // bluetooth-input lands on a genuinely active streaming source, and
    // switching modes/inputs keeps the overview consistent.
    const sourceGet = () => demoFetch('/api/audio/source-mode').then((r) => r.json());
    const sourcePost = (payload) => demoFetch('/api/audio/source-mode',
        { method: 'POST', body: JSON.stringify(payload) }).then((r) => r.json().then((d) => ({ status: r.status, ok: r.ok, data: d })));
    const initialSource = await sourceGet();
    assert.equal(initialSource.mode, 'app-playback');
    // JSON-stringify for cross-realm comparison: the demo VM hands back
    // live objects whose Array prototype differs from this realm's.
    assert.equal(JSON.stringify(initialSource.inputs.map((i) => i.label)),
        JSON.stringify(['MOTU M4 · Input 1–2', 'MOTU M4 · Input 3–4', 'USB S/PDIF · Input']));
    assert.ok(initialSource.inputs.every((i) => Array.isArray(i.pair_channels) && i.pair_channels.length === 2));
    assert.ok(!initialSource.inputs.some((i) => /Input [12]$/.test(i.label)),
        'no single mono channel may appear as a stereo source');
    assert.equal(JSON.stringify(initialSource.inputs.map((i) => [i.left_channel, i.right_channel])),
        JSON.stringify([['FL', 'FR'], ['RL', 'RR'], ['FL', 'FR']]));
    assert.equal(initialSource.bluetooth.selectable, true);
    assert.equal(initialSource.bluetooth.state, 'streaming');
    assert.equal(initialSource.bluetooth.connected_device, 'Demo Phone');

    const ext34 = await sourcePost({ mode: 'external-input', inputKey: 'alsa_input.usb-MOTU_M4-00.analog-surround-40::pair:3-4' });
    assert.equal(ext34.status, 200);
    assert.equal(ext34.data.mode, 'external-input');
    assert.equal(ext34.data.selected_input.key, 'alsa_input.usb-MOTU_M4-00.analog-surround-40::pair:3-4');
    assert.equal(ext34.data.selected_input.label, 'MOTU M4 · Input 3–4');
    assert.equal(JSON.stringify(ext34.data.selected_input.pair_channels), JSON.stringify([3, 4]));
    assert.equal(ext34.data.current_input.key, ext34.data.selected_input.key);
    assert.ok(ext34.data.inputs.find((i) => i.key === ext34.data.selected_input.key).is_selected);

    const extUnknown = await sourcePost({ mode: 'external-input', inputKey: 'no-such-input' });
    assert.equal(extUnknown.status, 400);

    const bt = await sourcePost({ mode: 'bluetooth-input' });
    assert.equal(bt.status, 200);
    assert.equal(bt.data.mode, 'bluetooth-input');
    assert.equal(bt.data.bluetooth.state, 'streaming');
    assert.equal(bt.data.bluetooth.connected_device, 'Demo Phone');
    assert.ok(bt.data.bluetooth.active_codec);

    const back = await sourcePost({ mode: 'app-playback' });
    assert.equal(back.data.mode, 'app-playback');
    assert.equal(back.data.selected_input.key, 'alsa_input.usb-MOTU_M4-00.analog-surround-40::pair:3-4');
    const freshSource = await sourceGet();
    assert.equal(freshSource.mode, 'app-playback');

    assert.ok(state.stations.some((station) => station.id === 'groovesalad'));
assert.ok(state.catalogStations.some((station) => station.id === 'rp-main'));
    assert.equal(state.catalogStations.some((station) => station.id === 'drumandbass'), false);
    // The demo catalog mirrors the real curated catalog (radio/stations.py
    // STATION_CATALOG): 4 Radio Paradise + 38 SomaFM + 11 FIP + 8 Other,
    // no demo-only providers.
    assert.equal(state.catalogStations.length, 61);
    assert.equal(state.catalogStations.some((station) => station.provider === 'BBC'), false);
    for (const id of ['groovesalad', 'groovesalad2', 'gsclassic']) {
        assert.ok(state.catalogStations.some((station) => station.id === id), 'tour search target present: ' + id);
    }

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
    assert.ok(state.spotify.list.length >= 12, 'Spotify catalog must be stocked (not a 4-track stub)');
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
    assert.ok(state.qobuz.qlist.length >= 12, 'Qobuz catalog must be stocked (not a 4-track stub)');
    // Real qbzd stream facts: the footer pill must render the full
    // 'FLAC · 16/24bit · ratekHz' line, not the bare hardware rate.
    assert.equal(qobuz.audio_format, 'flac');
    assert.equal(qobuz.bit_depth, 24);
    assert.equal(qobuz.sample_rate, 96000);
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
    // Distinct cover pools: the provider artwork namespaces must never
    // collapse onto the same pooled files, or both tabs render identical art.
    const spCovers = new Set(state.spotify.list.map(t => t.art_url));
    const qbCovers = new Set(state.qobuz.qlist.map(t => t.art_url));
    assert.ok(![...spCovers].some(url => qbCovers.has(url)),
        'Spotify/Qobuz cover pools must be disjoint');
    // Every queued Qobuz track carries complete provider stream facts so a
    // track switch keeps the full quality line (never collapses to the rate).
    for (const track of state.qobuz.qlist) {
        assert.equal(track.audio_format ?? 'flac', 'flac');
        assert.ok(track.bit_depth, 'qobuz track must carry bit_depth');
        assert.ok(track.sample_rate_hz, 'qobuz track must carry sample_rate_hz');
    }
    state.qobuz.toggleShuffle();
    assert.equal(state.qobuz.payload().shuffle, true);
    state.qobuz.cycleLoop();
    assert.equal(state.qobuz.payload().loop, 'playlist');

    // ── TIDAL footer stream facts ────────────────────────────────────────
    // TIDAL is native playback: the footer renders the radio/local
    // stream_info line, so the demo payload must carry the real
    // normalization shape (codec + Lossless profile, bit_depth,
    // samplerate_hz) derived per track from the album's quality tier — one
    // static line for the whole catalog would flatten the tiers.
    const tidalTierCases = [
        ['t_album_01_t1', 'HI_RES_LOSSLESS', { codec: 'FLAC', profile: 'Lossless', bit_depth: 24, samplerate_hz: 96000 }, 96000],
        ['t_album_02_t1', 'LOSSLESS', { codec: 'FLAC', profile: 'Lossless', bit_depth: 16, samplerate_hz: 44100 }, 44100],
        ['t_album_06_t1', 'HIGH', { codec: 'AAC', bitrate_kbps: 320, samplerate_hz: 44100 }, 44100],
    ];
    for (const [tidalTrackId, tier, facts, graphRate] of tidalTierCases) {
        const tidalTrack = state.playTidal(tidalTrackId);
        assert.ok(tidalTrack, 'tidal track ' + tidalTrackId + ' must exist');
        assert.equal(tidalTrack.audio_quality, tier);
        const tidalPlayback = state.getPlayback();
        assert.equal(tidalPlayback.playback_owner, 'tidal');
        // Field-wise compare: stream_info is built inside the demo VM realm.
        for (const [key, value] of Object.entries(facts)) {
            assert.equal(tidalPlayback.stream_info[key], value,
                'stream_info.' + key + ' for ' + tidalTrackId);
        }
        assert.equal(Object.keys(tidalPlayback.stream_info).length, Object.keys(facts).length);
        // Native playback moves the graph rate like the real system: the
        // effective output rate in the footer follows the tier (96 kHz
        // Hi-Res, 44.1 kHz lossless/AAC), not the stale idle rate.
        const rate = await (await demoFetch('/api/audio/samplerate')).json();
        assert.equal(rate.active_rate, graphRate, 'graph rate for ' + tier);
    }
    state.stop();

    // ── Spotify footer parity ────────────────────────────────────────────
    // The real system pins the graph to 44.1 kHz while Spotify plays
    // (coordinator_source_rate -> SPOTIFY_PREARM_SAMPLE_RATE_HZ), so the
    // real Spotify footer pill shows '44.1kHz' — never the 48 kHz idle
    // rate. A fixed policy must pin the graph regardless of source.
    state.spotify.demoStart();
    assert.equal(state.getPlayback().playback_owner, 'spotify');
    let sr = await (await demoFetch('/api/audio/samplerate')).json();
    assert.equal(sr.active_rate, 44100, 'spotify pins the graph at 44.1 kHz');
    const spotifyPayload = state.spotify.snapshot();
    // Payload shape must match the real playerctl/provider surface.
    assert.ok(spotifyPayload.trackId.startsWith('spotify:track:'), 'MPRIS-shaped trackId');
    assert.equal(spotifyPayload.capabilities.volume, true);
    assert.equal(spotifyPayload.capabilities.audio_format, undefined, 'no invented Spotify format facts');
    state.stop();
    sr = await (await demoFetch('/api/audio/samplerate')).json();
    assert.equal(sr.active_rate, 48000, 'idle graph returns to 48 kHz');
    // Fixed policy wins over any source rate (effective_playback_rate).
    await demoFetch('/api/audio/samplerate', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ mode: 'fixed', rate: 96000 }) });
    state.spotify.demoStart();
    sr = await (await demoFetch('/api/audio/samplerate')).json();
    assert.equal(sr.active_rate, 96000, 'fixed policy overrides the spotify pin');
    await demoFetch('/api/audio/samplerate', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ mode: 'auto' }) });
    state.stop();

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
    // integration slot, plus hybrid models, repeat summaries and the real
    // auto-sub Before/After runs per mode and target.
    const savedNames = context.FXROUTE_DEMO_MEASUREMENTS.savedMeasurements.map(m => m.name);
    // Auto-sub fixtures mirror the real .104 files 1:1 — one single-trace
    // file per channel, so names carry Before/After plus the side.
    for (const expected of ['Sweep-L-Raw', 'Sweep-R-Raw', 'Sweep-L/R-Raw',
        'Sweep-L-Convolver', 'Sweep-R-Convolver', 'Sweep-L/R-Convolver',
        'Sweep-Close-L-1', 'Sweep-Close-R-1',
        'Advanced 2.2 Mono · L', 'Advanced 2.2 Mono · R',
        'Auto-Sub-Optimize-2.1 (Neutral) · Before L', 'Auto-Sub-Optimize-2.1 (Neutral) · Before R',
        'Auto-Sub-Optimize-2.1 (Neutral) · After L', 'Auto-Sub-Optimize-2.1 (Neutral) · After R',
        'Auto-Sub-Optimize-2.2 (Neutral) · Before L', 'Auto-Sub-Optimize-2.2 (Neutral) · Before R',
        'Auto-Sub-Optimize-2.2 (Neutral) · After L', 'Auto-Sub-Optimize-2.2 (Neutral) · After R',
        'Auto-Sub-Optimize-2.2 (BK) · Before L', 'Auto-Sub-Optimize-2.2 (BK) · Before R',
        'Auto-Sub-Optimize-2.2 (BK) · After L', 'Auto-Sub-Optimize-2.2 (BK) · After R',
        'Auto-Sub-Optimize-2.2-Stereo (Neutral) · Before L', 'Auto-Sub-Optimize-2.2-Stereo (Neutral) · Before R',
        'Auto-Sub-Optimize-2.2-Stereo (Neutral) · After L', 'Auto-Sub-Optimize-2.2-Stereo (Neutral) · After R',
        'Auto-Sub-Optimize-2.2-Stereo (BK) · Before L', 'Auto-Sub-Optimize-2.2-Stereo (BK) · Before R',
        'Auto-Sub-Optimize-2.2-Stereo (BK) · After L', 'Auto-Sub-Optimize-2.2-Stereo (BK) · After R']) {
        assert.ok(savedNames.includes(expected), 'demo fixtures must include ' + expected);
    }
    assert.ok(savedNames.some(name => /^L\/R Repeat .* · L$/.test(name)));
    assert.ok(savedNames.some(name => /^L\/R Repeat .* · R$/.test(name)));
    const autosubKinds = new Set(context.FXROUTE_DEMO_MEASUREMENTS.savedMeasurements
        .filter(m => /Auto-Sub/.test(m.name)).map(m => m.measurement_kind));
    assert.deepEqual([...autosubKinds], ['auto_sub']);
    // Auto-sub fixtures must carry the real labels and band stats: the
    // saved list renders input label · channel on the left and points +
    // frequency range on the right, never "No graph data". Channels are
    // per-file left/right exactly like the real .104 saves.
    for (const m of context.FXROUTE_DEMO_MEASUREMENTS.savedMeasurements.filter(x => /Auto-Sub/.test(x.name))) {
        assert.equal(m.input_device.label, 'Capture input');
        assert.ok(['left', 'right'].includes(m.channel), m.name + ' needs a real left/right channel');
        assert.equal(m.traces.length, 1);
        assert.ok(m.summary && m.summary.point_count >= 189, m.name + ' needs real point count');
        assert.ok(m.summary.min_hz >= 20 && m.summary.min_hz <= 23, m.name + ' needs real min_hz');
        assert.equal(m.summary.max_hz, 20000);
    }
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
    // Here: hand-set VU levels alone never trip the peak detector (holds
    // latch only from the independent raw-peak path), and preset/loudness
    // changes round-trip through the demo DSP API. The post-limiter tap
    // itself (an engaged limiter caps the visible level) is covered by
    // scripts/test_demo_meter_and_scan.js.
    state.playLocal(state.localTracks[0].id);
    const meterOf = () => state.getPeak();
    await (await demoFetch('/api/dsp/presets/load', { method: 'POST', body: JSON.stringify({ preset_name: 'Direct' }) })).json();
    state.getMeter().vu_db_l = -13; state.getMeter().vu_db_r = -13; state.getMeter().vu_fresh = true;
    assert.equal(meterOf().detected, false);
    state.getMeter().vu_db_l = 0.5; state.getMeter().vu_db_r = 0.5; state.getMeter().vu_fresh = true;
    assert.equal(meterOf().detected, false, 'smoothed VU levels must not latch peaks');
    await (await demoFetch('/api/dsp/presets/load', { method: 'POST', body: JSON.stringify({ preset_name: '+6' }) })).json();
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

    // ── Album About + Discover Similar contract ─────────────────────────
    // About texts ride the same backend fields (artist_description /
    // album_description) the real UI renders as collapsible <details>;
    // Discover reuses the demo library itself (same genre/decade first),
    // max 6 items, never the album itself, 404 for unknown ids.
    // This section covers the main catalog, so select Local first (the
    // demo presents SMB_Demo_Library-1 by default).
    await demoFetch('/api/music-libraries/select', { method: 'POST', body: JSON.stringify({ id: 'local' }) });
    const demoAlbums = await (await demoFetch('/api/albums')).json();
    const afterRain = demoAlbums.find(a => a.id === 'after-the-rain');
    assert.ok(afterRain && afterRain.artist_description && afterRain.artist_description.includes('Blue Meridian'));
    assert.equal(afterRain.album_description || '', '');
    const colorRadio = demoAlbums.find(a => a.name === 'Color Radio');
    assert.ok(colorRadio && colorRadio.album_description && colorRadio.album_description.length > 40);
    assert.ok(colorRadio.artist_description && colorRadio.artist_description.length > 40);
    for (const album of [afterRain, colorRadio]) {
        const disc = await (await demoFetch('/api/albums/' + encodeURIComponent(album.id) + '/discover')).json();
        assert.equal(disc.album_id, album.id);
        assert.ok(Array.isArray(disc.items) && disc.items.length > 0 && disc.items.length <= 6);
        assert.ok(disc.items.every(item => item.id !== album.id), 'discover must not suggest the album itself');
        assert.ok(disc.items.every(item => item.name && item.artist && item.coverUrl));
    }
    const afterRainDisc = await (await demoFetch('/api/albums/after-the-rain/discover')).json();
    assert.ok(afterRainDisc.items.some(item => item.artist !== 'Blue Meridian'), 'discover must reach beyond the same artist');
    const unknownDisc = await demoFetch('/api/albums/does-not-exist/discover');
    assert.equal(unknownDisc.status, 404);

    // ── TIDAL About + Discover Similar contract ─────────────────────────
    // Same enrichment fields the backend attaches (enrichment.about /
    // enrichment.similar on the artist detail; enrichment.artist.about /
    // enrichment.similar on the album detail). About texts are static
    // demo bios; similar cards reuse only artists from the demo TIDAL
    // catalog with stored art so every card resolves to a browsable
    // artist — the real UI behavior, no new logic.
    const tidalArtists = context.FXROUTE_DEMO_LIBRARY.tidalArtists;
    assert.ok(tidalArtists.length >= 10);
    assert.ok(tidalArtists.every(a => context.FXROUTE_DEMO_LIBRARY.tidalArtistAbout[a.name]
        && context.FXROUTE_DEMO_LIBRARY.tidalArtistAbout[a.name].length > 40));
    for (const artistId of ['t_artist_01', 't_artist_05', 't_artist_14']) {
        const detail = await (await demoFetch('/api/streaming/tidal/artists/' + artistId)).json();
        assert.ok(detail.enrichment && detail.enrichment.about && detail.enrichment.about.length > 40);
        assert.ok(Array.isArray(detail.enrichment.similar) && detail.enrichment.similar.length === 6);
        assert.ok(detail.enrichment.similar.every(item => item.artist && item.provider_artist_id && item.art_url),
            'similar cards need artist + provider id + art');
        assert.ok(detail.enrichment.similar.every(item => item.artist !== detail.name),
            'similar must not include the artist itself');
        assert.ok(detail.enrichment.similar.every(item => tidalArtists.some(a => a.id === item.provider_artist_id && a.name === item.artist)),
            'every similar card must resolve to a demo catalog artist');
    }
    const tidalAlbum = await (await demoFetch('/api/streaming/tidal/albums/t_album_01')).json();
    assert.ok(tidalAlbum.enrichment && tidalAlbum.enrichment.artist && tidalAlbum.enrichment.artist.about
        && tidalAlbum.enrichment.artist.about.length > 40);
    assert.ok(Array.isArray(tidalAlbum.enrichment.similar) && tidalAlbum.enrichment.similar.length === 6);
    assert.ok(tidalAlbum.enrichment.similar.every(item => tidalArtists.some(a => a.id === item.provider_artist_id)));
    // Album track rows render thumb + subtitle from the track dict itself
    // (same normalized shape as the real provider boundary:
    // id/title/artist/album/art_url/duration) — no placeholders.
    const tidalAlbumTracks = await (await demoFetch('/api/streaming/tidal/albums/t_album_01/tracks')).json();
    assert.ok(tidalAlbumTracks.length > 0, 'tidal album serves its tracks');
    for (const track of tidalAlbumTracks) {
        assert.ok(track.artist && track.album, 'album track carries artist + album');
        assert.ok(track.art_url && track.art_url.endsWith('.jpg'), 'album track carries its cover: ' + track.title);
    }
    const unknownTidalArtist = await demoFetch('/api/streaming/tidal/artists/does-not-exist');
    assert.equal(unknownTidalArtist.status, 404);
    // Playlist covers mirror the local collage: a montage of the member
    // album covers, not a random pool image.
    for (const pid of ['t_playlist_01', 't_playlist_03']) {
        const pl = await (await demoFetch('/api/streaming/tidal/playlists/' + pid)).json();
        assert.match(pl.art_url, new RegExp('^/static/demo/tpl-' + pid + '\\.jpg$'), 'playlist shows its album montage: ' + pid);
    }

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
    // The run must advance through queued/running stages (with the real
    // Before curves for the live graph) and finish with the real
    // Before/After pair of the matching .104 run — finite Sub 1 / Sub 2
    // alignment and derived delays, never '? ms', never a full sweep.
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
    // The live baseline must already be real Before curves, not a sweep.
    assert.ok(baselineStage.baseline_measurement.traces.every(t => /^Before /.test(t.label)),
        'live baseline must show Before curves, got: ' + baselineStage.baseline_measurement.traces.map(t => t.label).join(','));
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
    // The finished 2.2 run replays the Neutral pair by default (the demo
    // starts in 2.2 mode with Target Curve = Neutral) — single-trace
    // per-channel files exactly like the real saves.
    assert.equal(autoSubResult.baseline_measurement.traces.length, 1);
    assert.equal(autoSubResult.baseline_measurement.channel, 'left');
    assert.ok(autoSubResult.baseline_measurement.traces.every(t => /^Before /.test(t.label)),
        '2.2 baseline must be Before curves, got: ' + autoSubResult.baseline_measurement.traces.map(t => t.label).join(','));
    assert.ok(autoSubResult.confirmation_measurement.traces.every(t => /^After /.test(t.label)),
        '2.2 confirmation must be After curves, got: ' + autoSubResult.confirmation_measurement.traces.map(t => t.label).join(','));
    assert.match(autoSubResult.baseline_measurement.name, /2\.2 \(Neutral\) · Before L/);
    assert.match(autoSubResult.confirmation_measurement.name, /2\.2 \(Neutral\) · After L/);
    assert.equal(autoSubDone.target_curve.key, 'neutral');
    // With a BK snapshot the same mode must replay the BK pair instead.
    const bkForm22 = new context.FormData();
    bkForm22.append('target_curve_snapshot', JSON.stringify({ key: 'bk', label: 'Bruel & Kjaer-style', provenance: 'built_in', points: [[20, 2], [20000, -3.5]] }));
    const autoSubBk22Start = await (await demoFetch('/api/measurements/auto-sub-optimize/start', { method: 'POST', body: bkForm22 })).json();
    const autoSubBk22Done = context.FXROUTE_DEMO_API.autoSubJobPayload(autoSubBk22Start.job.id, 100000);
    assert.equal(autoSubBk22Done.target_curve.key, 'bk');
    assert.match(autoSubBk22Done.result.baseline_measurement.name, /2\.2 \(BK\) · Before L/);
    assert.match(autoSubBk22Done.result.confirmation_measurement.name, /2\.2 \(BK\) · After L/);
    // Bass-heavy targets without their own run (Harman, Bass Shelf)
    // replay the BK pair — audibly closer than Neutral — while Neutral
    // keeps the Neutral pair.
    for (const bassKey of ['harman', 'bass_shelf']) {
        const bassForm = new context.FormData();
        bassForm.append('target_curve_snapshot', JSON.stringify({ key: bassKey, label: bassKey, provenance: 'built_in', points: [[20, 4], [20000, 0]] }));
        const bassStart = await (await demoFetch('/api/measurements/auto-sub-optimize/start', { method: 'POST', body: bassForm })).json();
        const bassDone = context.FXROUTE_DEMO_API.autoSubJobPayload(bassStart.job.id, 100000);
        assert.equal(bassDone.target_curve.key, bassKey);
        assert.match(bassDone.result.baseline_measurement.name, /2\.2 \(BK\) · Before L/);
        assert.match(bassDone.result.confirmation_measurement.name, /2\.2 \(BK\) · After L/);
    }
    // The BK run applies its own delays — assert them right away, before
    // the later mode switches move the output state on.
    const outputsAfterAutoSub = await (await demoFetch('/api/audio/outputs')).json();
    assert.equal(outputsAfterAutoSub.output_mode.derived_sub1_delay_ms, autoSubBk22Done.result.applied_sub1_alignment_ms);
    assert.equal(outputsAfterAutoSub.output_mode.subwoofers.sub1.alignment_ms, autoSubBk22Done.result.applied_sub1_alignment_ms);
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
    // The 2.1 run replays the real Neutral pair.
    assert.ok(autoSub21Done.result.baseline_measurement.traces.every(t => /^Before /.test(t.label)));
    assert.ok(autoSub21Done.result.confirmation_measurement.traces.every(t => /^After /.test(t.label)));
    assert.match(autoSub21Done.result.baseline_measurement.name, /2\.1 \(Neutral\) · Before L/);
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
    // Default target is Neutral, so stereo replays the Neutral pair here.
    assert.match(autoSubSbDone.result.baseline_measurement.name, /2\.2-Stereo \(Neutral\) · Before L/);
    assert.match(autoSubSbDone.result.confirmation_measurement.name, /2\.2-Stereo \(Neutral\) · After L/);
    // With a BK snapshot the same mode must replay the BK pair instead.
    const bkForm = new context.FormData();
    bkForm.append('target_curve_snapshot', JSON.stringify({ key: 'bk', label: 'Bruel & Kjaer-style', provenance: 'built_in', points: [[20, 2], [20000, -3.5]] }));
    const autoSubSbBkStart = await (await demoFetch('/api/measurements/auto-sub-optimize/start', { method: 'POST', body: bkForm })).json();
    const autoSubSbBkDone = context.FXROUTE_DEMO_API.autoSubJobPayload(autoSubSbBkStart.job.id, 100000);
    assert.equal(autoSubSbBkDone.target_curve.key, 'bk');
    assert.match(autoSubSbBkDone.result.baseline_measurement.name, /2\.2-Stereo \(BK\) · Before L/);
    assert.match(autoSubSbBkDone.result.confirmation_measurement.name, /2\.2-Stereo \(BK\) · After L/);
    assert.ok(autoSubSbBkDone.result.baseline_measurement.traces.every(t => /^Before /.test(t.label)));
    assert.ok(autoSubSbBkDone.result.confirmation_measurement.traces.every(t => /^After /.test(t.label)));
    // The extra BK run applies its own delays, so re-assert against the BK
    // result (not the earlier Neutral one).
    const sbBkOutputsAfter = await (await demoFetch('/api/audio/outputs')).json();
    assert.equal(sbBkOutputsAfter.output_mode.derived_sub1_delay_ms, autoSubSbBkDone.result.applied_sub1_alignment_ms);
    assert.equal(sbBkOutputsAfter.output_mode.derived_sub2_delay_ms, autoSubSbBkDone.result.applied_sub2_alignment_ms);
    // Restore the demo's default 2.2 mode.
    await (await demoFetch('/api/audio/output-mode', { method: 'POST', body: JSON.stringify({ mode: 'subwoofer-2.2' }) })).json();

    // ── Demo stock reset/restore ───────────────────────────────────────
    // Playing around in a session (unfavoriting, deleting/creating
    // playlists) must never thin out the demo: a fresh load has to rebuild
    // the full initial stock from the fixtures. Runs in its own isolated
    // session so it is independent of the mutations above.
    const session = makeDemoContext();
    // The demo presents SMB_Demo_Library-1 by default; pin the stock
    // reset/restore contract to the main catalog (Local) so the fixtures
    // that ship the product demo keep being exercised.
    await session.fetch('/api/music-libraries/select', { method: 'POST', body: JSON.stringify({ id: 'local' }) });
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
    const restoredCtx = makeDemoContext();
    await restoredCtx.fetch('/api/music-libraries/select', { method: 'POST', body: JSON.stringify({ id: 'local' }) });
    const restoredStock = await demoStock(restoredCtx);
    assert.deepEqual(restoredStock, baselineStock, 'fresh reload must restore the full initial demo stock');

    console.log('ok demo behavior contract');
})();
