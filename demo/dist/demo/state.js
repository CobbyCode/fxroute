// Simulated FXRoute runtime state. Playback follows a single-owner model:
// exactly one source (local / radio / tidal / spotify / qobuz) owns the
// transport at any time. Starting a source pauses/clears other providers.
(function () {
    'use strict';

    const lib = window.FXROUTE_DEMO_LIBRARY;
    const localTracks = lib.tracks;
    const tidalTracksLib = lib.tidalTracks;
    const tidalAlbumsLib = lib.tidalAlbums;
    const tidalArtistsLib = lib.tidalArtists;
    const tidalPlaylistsLib = lib.tidalPlaylists;

    const radioCatalog = window.FXROUTE_DEMO_RADIO;
    const stations = radioCatalog.savedStations.map((station) => ({
        ...station,
        title: station.name,
        provider: 'SomaFM',
        artist: 'Radio',
        genres: [],
        logo_url: station.image_url,
    }));
    const catalogStations = radioCatalog.catalogStations.map((station) => ({
        ...station,
        title: station.name,
        artist: station.provider || 'Radio',
        genres: [],
        logo_url: station.image_url,
        is_saved: stations.some((saved) => saved.id === station.id),
        saved_station_id: stations.find((saved) => saved.id === station.id)?.id || null,
    }));

    // Provider tabs use a deliberately small queue, but each provider gets
    // its own tracks from distinct albums/artists (demo/data/library.js), so
    // switching tracks changes title, artist, album and cover together and
    // Spotify never shows Qobuz content (and vice versa). Each provider has
    // its own artwork key space.
    const spotifyProviderTracks = (lib.spotifyTracks && lib.spotifyTracks.length)
        ? lib.spotifyTracks.map((track) => ({ ...track, source: 'spotify' }))
        : localTracks.slice(0, 4).map((track) => ({
            ...track,
            source: 'spotify',
            spotify_art_url: lib.demoImage('spotify:' + (track.title || track.album || 'demo')),
            art_url: lib.demoImage('spotify:' + (track.title || track.album || 'demo')),
        }));
    const qobuzProviderTracks = (lib.qobuzTracks && lib.qobuzTracks.length)
        ? lib.qobuzTracks.map((track) => ({ ...track, source: 'qobuz' }))
        : localTracks.slice(0, 4).map((track) => ({
            ...track,
            source: 'qobuz',
            qobuz_art_url: lib.demoImage('qobuz:' + (track.title || track.album || 'demo')),
            art_url: lib.demoImage('qobuz:' + (track.title || track.album || 'demo')),
        }));

    // Radio streams have no finite track duration in the normal FXRoute UI.

    // ── Shared mutable DSP hooks (set by routes.js) ─────────────────────
    // The meter sim applies a single audible offset (dB): preset gain plus
    // the enabled tone extras. The protection limiter never raises the
    // level; it only clamps peaks that would exceed its threshold.
    let dspMeterOffsetDb = 0;
    let dspLimiterThresholdDb = -1;
    let dspLimiterEnabled = true;
    let dspExtras = {};
    let dspPresets = [];
    let dspActivePreset = 'Direct';

    function setDspSnapshot({ meterOffsetDb, limiterThresholdDb, limiterEnabled, extras, presets, activePreset }) {
        dspMeterOffsetDb = Number(meterOffsetDb || 0);
        if (limiterThresholdDb !== undefined && limiterThresholdDb !== null) {
            dspLimiterThresholdDb = Number(limiterThresholdDb);
        }
        if (limiterEnabled !== undefined && limiterEnabled !== null) {
            dspLimiterEnabled = !!limiterEnabled;
        }
        dspExtras = extras || {};
        dspPresets = presets || [];
        dspActivePreset = activePreset || 'Direct';
    }

    // ── Playback engine ─────────────────────────────────────────────────
    let currentSource = 'local';
    let currentTrack = null;
    let queue = [];
    let queueIndex = -1;
    let playing = false;
    let paused = false;
    let ended = false;
    let positionSec = 0;
    let loop = false;
    let shuffle = false;
    let volume = 38;
    let radioMetadata = null;
    let radioTimer = null;
    let lastTickTs = Date.now();

    const now = () => Date.now() / 1000;

    const meter = { vu_db_l: -60, vu_db_r: -60, vu_fresh: false };
    function peakSnapshot() {
        const limiting = dspLimiterEnabled && (meter.vu_db_l >= dspLimiterThresholdDb || meter.vu_db_r >= dspLimiterThresholdDb);
        const over = meter.vu_db_l > 0 || meter.vu_db_r > 0;
        const detected = !!(meter.vu_fresh && (limiting || over));
        return {
            available: true,
            detected,
            hold_ms: detected ? 500 : 0,
            threshold: 1.0,
            vu_db: (meter.vu_db_l + meter.vu_db_r) / 2,
            vu_db_l: meter.vu_db_l,
            vu_db_r: meter.vu_db_r,
            detected_l: !!(meter.vu_fresh && (over || (dspLimiterEnabled && meter.vu_db_l >= dspLimiterThresholdDb))),
            detected_r: !!(meter.vu_fresh && (over || (dspLimiterEnabled && meter.vu_db_r >= dspLimiterThresholdDb))),
            hold_ms_l: detected ? 500 : 0,
            hold_ms_r: detected ? 500 : 0,
            vu_fresh: !!meter.vu_fresh,
            vu_age_ms: 200,
            target: { description: 'DSP output monitor' },
            last_over_at: detected ? new Date().toISOString() : null,
            last_over_at_l: detected ? new Date().toISOString() : null,
            last_over_at_r: detected ? new Date().toISOString() : null,
            last_error: null,
        };
    }

    setInterval(() => {
        if (playing && !paused && currentTrack) {
            // Audible chain, deliberately small and clamped: the base program
            // sits around -13 dB so Direct stays green, +3 dB approaches the
            // limiter threshold and +6 dB regularly exceeds it.
            let levelL = -20 + Math.random() * 14 + dspMeterOffsetDb;
            let levelR = -20 + Math.random() * 14 + dspMeterOffsetDb;
            if (dspLimiterEnabled && Number.isFinite(dspLimiterThresholdDb)) {
                levelL = Math.min(levelL, dspLimiterThresholdDb + Math.random() * 1.5);
                levelR = Math.min(levelR, dspLimiterThresholdDb + Math.random() * 1.5);
            } else {
                levelL = Math.min(levelL, 3);
                levelR = Math.min(levelR, 3);
            }
            meter.vu_db_l = Math.round(levelL * 10) / 10;
            meter.vu_db_r = Math.round(levelR * 10) / 10;
            meter.vu_fresh = true;
        } else {
            meter.vu_db_l = -60;
            meter.vu_db_r = -60;
            meter.vu_fresh = false;
        }
        window.__demoBroadcast && window.__demoBroadcast('playback_peak_warning', peakSnapshot());
    }, 500);

    function clearProviderOwners(exclude) {
        if (exclude !== 'spotify') spotify.setPaused();
        if (exclude !== 'qobuz') qobuz.setPaused();
    }

    function trackPayload(track) {
        if (!track) return null;
        const cover = track.art_url || track.cover_url || track.image_url || '';
        const base = {
            id: track.id,
            title: track.title,
            artist: track.artist || '',
            album: track.album || '',
            source: track.source || currentSource,
            duration: Number(track.duration || 0),
            url: track.url || track.stream_url || '',
        };
        if (cover) {
            base.cover_url = cover;
            base.cover_available = true;
            base.artwork_url = cover;
            base.artwork_available = true;
            base.artwork_source = track.source === 'radio' ? 'radio' : (track.source === 'tidal' ? 'tidal' : 'library');
        }
        return base;
    }

    function currentTrackView() {
        return trackPayload(currentTrack);
    }

    // Native playback stream facts mirror the real normalization
    // (playback/stream_info.py): codec + `Lossless` profile, bit depth and
    // `samplerate_hz` — the field names the shared footer renderer reads
    // (formatRadioStreamLine). Spotify/Qobuz do not come through here; they
    // are remote-renderer owners with their own payload facts.
    function streamInfoFor(src, track = null) {
        if (src === 'local') return { codec: 'FLAC', bitrate_kbps: 1411, sample_rate: 48000 };
        if (src === 'tidal') return tidalStreamInfo(track);
        if (src === 'radio') return { codec: 'MP3', bitrate_kbps: 192, sample_rate: 44100 };
        if (src === 'spotify') return { codec: 'Ogg', bitrate_kbps: 320, sample_rate: 44100 };
        if (src === 'qobuz') return { codec: 'FLAC', bitrate_kbps: 1411, sample_rate: 96000 };
        return { codec: '', bitrate_kbps: 0, sample_rate: 0 };
    }

    // TIDAL facts per track, derived from the album's quality tier exactly
    // like the real stream resolution (streaming/tidal/playback.py):
    // HI_RES_LOSSLESS -> FLAC 24 bit/96 kHz, LOSSLESS -> FLAC 16 bit/44.1 kHz,
    // HIGH -> AAC 320 kbps/44.1 kHz. Lossless keeps codec/profile/bit
    // depth/rate; lossy AAC keeps the bitrate line instead (the renderer
    // drops the redundant 'Lossless' profile for lossless codecs).
    function tidalStreamInfo(track = null) {
        const quality = String(track?.audio_quality || '');
        if (quality === 'HI_RES_LOSSLESS') return { codec: 'FLAC', profile: 'Lossless', bit_depth: 24, samplerate_hz: 96000 };
        if (quality === 'LOSSLESS') return { codec: 'FLAC', profile: 'Lossless', bit_depth: 16, samplerate_hz: 44100 };
        if (quality === 'HIGH') return { codec: 'AAC', bitrate_kbps: 320, samplerate_hz: 44100 };
        return { codec: 'FLAC', bitrate_kbps: 1411 };
    }

    function playbackPayload() {
        return {
            playing,
            paused,
            ended,
            current_track: currentTrackView(),
            current_file: currentTrack ? String(currentTrack.url || '') : '',
            position: Math.round(positionSec),
            duration: Number(currentTrack?.duration || 0),
            volume,
            source_volume: 100,
            queue: {
                active: queue.length > 1,
                index: queueIndex,
                count: queue.length,
                mode: 'app_replace',
                tracks: queue.map(t => trackPayload(t)),
                loop: !!loop,
                shuffle: !!shuffle,
            },
            playback_owner: currentSource,
            live_title: currentSource === 'radio' && radioMetadata ? radioMetadata.title : null,
            radio_metadata: currentSource === 'radio' ? radioMetadata : null,
            metadata: (currentSource === 'radio' && radioMetadata) ? { 'icy-title': radioMetadata.title } : {},
            stream_info: streamInfoFor(currentSource, currentTrack),
            output_peak_warning: peakSnapshot(),
            _seq: (window.__demoSeq = (window.__demoSeq || 0) + 1),
        };
    }

    function radioMetadataProvider(station) {
        // Mirrors radio/metadata.py RadioMetadataService.provider_for: the
        // station identity/URL decides the provider, not the catalog label.
        const sid = String(station.id || '').replace(/^station_/, '').replace(/^radio_/, '');
        const url = String(station.stream_url || station.input_url || '').toLowerCase();
        if (sid === 'rp-main' || sid === 'rp-mellow' || sid === 'rp-rock' || sid === 'rp-global') return 'radio_paradise';
        if (sid.startsWith('fip-')) return 'fip';
        if (sid === 'kexp-main' || url.includes('kexp.streamguys')) return 'kexp';
        if (sid === 'live' || sid === 'defcon') return null;
        if (url.includes('somafm.com') || station.provider === 'SomaFM') return 'somafm';
        return null;
    }

    // Simulated live metadata: a new song appears roughly every 45 s and the
    // previous one rolls into the history list — the same shape the real
    // backend publishes (SomaFM songs API, FIP live meta, Radio Paradise
    // playlist, KEXP plays). Stations without a provider (plain ICY streams)
    // only get a combined title, exactly like the real ICY fallback.
    const RADIO_SIM_TRACKS = [
        { artist: 'Alistair Kade', title: 'Midnight Arcade', album: 'Neon Reverie' },
        { artist: 'Iris Nakamura', title: 'Parallax Drift', album: 'Glass Horizons' },
        { artist: 'Nordkap', title: 'Aurora Station', album: 'Polar Vectors' },
        { artist: 'The Bramble Trio', title: 'Copper Kettle', album: 'Riverstone' },
        { artist: 'Kestrel Wire', title: 'Concrete Cathedral', album: 'Load Bearing' },
        { artist: 'Delta Hollis', title: 'Levee Stomp', album: 'Bottomland' },
    ];
    let radioSimIndex = 0;
    let radioSimTitleUntil = 0;

    function radioSimSong() {
        const entry = RADIO_SIM_TRACKS[radioSimIndex % RADIO_SIM_TRACKS.length];
        radioSimIndex += 1;
        return entry;
    }

    function somafmHistoryForCurrent(currentIndex) {
        // The real SomaFM songs API returns the current song plus the
        // previous ones in a single response, so "Recently played" is filled
        // immediately on station start. Previous songs are the entries before
        // the current one, most recent first, capped like songs[1:4].
        const history = [];
        for (let back = 1; back <= 3; back += 1) {
            const index = (currentIndex - 1 - back + RADIO_SIM_TRACKS.length) % RADIO_SIM_TRACKS.length;
            const entry = RADIO_SIM_TRACKS[index];
            history.push({ artist: entry.artist, title: entry.title, album: entry.album, started_at: now() - back * 45 });
        }
        return history;
    }

    function makeRadioMetadata(track) {
        const stationId = String(track.id || '').replace(/^radio_/, '');
        const station = stations.find((item) => item.id === stationId) || {};
        // Mirror the real backend contract (radio/metadata.py): only the
        // providers that publish track timing (Radio Paradise, FIP) carry
        // duration/progress and get a running track slider in the UI.
        // SomaFM, KEXP and plain ICY stations report titles only — no
        // timing, no slider. History is only published by SomaFM, the
        // single provider whose real API delivers previously played songs;
        // it is present from the first response, exactly like the real one.
        const provider = radioMetadataProvider(station);
        const timed = provider === 'radio_paradise' || provider === 'fip';
        const entry = radioSimSong();
        const startedAt = now();
        const durationSeconds = timed ? 40 : null;
        const rotates = timed || provider === 'somafm' || provider === 'kexp';
        radioSimTitleUntil = rotates ? startedAt + (durationSeconds || 45) : 0;
        return {
            station_id: stationId,
            provider,
            track_id: String(radioSimIndex),
            artist: provider ? entry.artist : null,
            title: provider ? entry.title : `${entry.artist} - ${entry.title}`,
            album: provider === 'somafm' || provider === 'radio_paradise' || provider === 'kexp' ? entry.album : null,
            cover_url: station.image_url || '',
            started_at: provider ? startedAt : null,
            ends_at: timed ? startedAt + durationSeconds : null,
            duration_seconds: durationSeconds,
            progress_seconds: timed ? 0 : null,
            history: provider === 'somafm' ? somafmHistoryForCurrent(radioSimIndex) : [],
            source: provider ? 'provider' : 'icy',
            fetched_at: now(),
            stale: false,
        };
    }

    function rollRadioMetadata() {
        if (!playing || paused || currentSource !== 'radio' || !radioMetadata) return;
        const timed = Number(radioMetadata.duration_seconds) > 0;
        // SomaFM and KEXP rotate songs on the same wall-clock cadence as the
        // timed providers but carry no duration, so the UI keeps no slider.
        const rotates = timed || radioMetadata.provider === 'somafm' || radioMetadata.provider === 'kexp';
        const needsNew = radioMetadata.track_id === null || (rotates && now() >= radioSimTitleUntil);
        if (needsNew && radioMetadata.track_id !== null) {
            // History is provider-specific: only SomaFM publishes previously
            // played tracks (real somafm.com/songs/<slug>.json). The other
            // providers never fill history, so the cover detail shows no
            // invented entries.
            if (radioMetadata.provider === 'somafm') {
                radioMetadata.history = [
                    { artist: radioMetadata.artist, title: radioMetadata.title, album: radioMetadata.album || null, started_at: radioMetadata.started_at },
                    ...radioMetadata.history,
                ].slice(0, 3);
            }
        }
        if (needsNew) {
            const entry = radioSimSong();
            const startedAt = now();
            radioMetadata.track_id = String(radioSimIndex);
            radioMetadata.artist = radioMetadata.provider ? entry.artist : null;
            radioMetadata.title = radioMetadata.provider ? entry.title : `${entry.artist} - ${entry.title}`;
            radioMetadata.album = radioMetadata.provider === 'somafm' || radioMetadata.provider === 'radio_paradise' || radioMetadata.provider === 'kexp' ? entry.album : null;
            radioMetadata.started_at = radioMetadata.provider ? startedAt : null;
            radioMetadata.duration_seconds = timed ? 40 + Math.floor(Math.random() * 8) : null;
            radioMetadata.ends_at = timed ? startedAt + radioMetadata.duration_seconds : null;
            radioSimTitleUntil = rotates ? (timed ? startedAt + radioMetadata.duration_seconds : startedAt + 45) : 0;
        }
        radioMetadata.progress_seconds = timed && radioMetadata.started_at
            ? Math.max(0, Math.min(radioMetadata.duration_seconds || 0, now() - radioMetadata.started_at))
            : null;
        radioMetadata.fetched_at = now();
        radioMetadata.stale = false;
        emitPlayback();
    }

    function startRadioMetadataTimer() {
        clearRadioTimer();
        radioTimer = setInterval(rollRadioMetadata, 2500);
    }

    function clearRadioTimer() {
        if (radioTimer) { clearInterval(radioTimer); radioTimer = null; }
    }

    function startTrack(track, src, trackQueue, index) {
        clearRadioTimer();
        radioMetadata = null;
        playing = true;
        paused = false;
        ended = false;
        positionSec = 0;
        currentSource = src;
        currentTrack = track;
        queue = (Array.isArray(trackQueue) && trackQueue.length) ? trackQueue : [track];
        queueIndex = (typeof index === 'number' && index >= 0) ? index : 0;
        clearProviderOwners(src);
        if (src === 'radio') {
            radioMetadata = makeRadioMetadata(track);
            startRadioMetadataTimer();
        } else if (src === 'spotify') {
            spotify.adopt(track);
        } else if (src === 'qobuz') {
            qobuz.adopt(track);
        }
        emitPlayback();
    }

    function advanceRadioTrack() {
        if (!playing || paused || currentSource !== 'radio' || !radioMetadata) return;
        // Kept for parity with the real module; the demo drives metadata via
        // rollRadioMetadata on the radio timer instead.
        emitPlayback();
    }

    function tick() {
        if (!playing || paused) return;
        const nowMs = Date.now();
        const dt = (nowMs - lastTickTs) / 1000;
        lastTickTs = nowMs;
        if (currentSource === 'radio') return;
        const dur = Number(currentTrack?.duration || 0);
        if (dur <= 0) return;
        positionSec += dt;
        if (positionSec < dur) return;
        // Track ended.
        if (loop || queue.length <= 1) {
            if (loop) { positionSec = 0; emitPlayback(); return; }
            ended = true;
            playing = false;
            paused = false;
            positionSec = dur;
            emitPlayback();
            return;
        }
        if (queueIndex < queue.length - 1) {
            queueIndex += 1;
            switchToTrack(queue[queueIndex], queueIndex);
        } else {
            ended = true;
            playing = false;
            positionSec = dur;
            emitPlayback();
        }
    }
    setInterval(tick, 500);

    function switchToTrack(track, index) {
        if (!track) return;
        currentTrack = track;
        queueIndex = index;
        positionSec = 0;
        if (currentSource === 'spotify') spotify.adopt(track);
        else if (currentSource === 'qobuz') qobuz.adopt(track);
        emitPlayback();
    }

    function emitPlayback() {
        // Fire before broadcasting so rate changes land in the same event.
        if (typeof window.FXROUTE_DEMO_STATE?.onSourceChanged === 'function') {
            window.FXROUTE_DEMO_STATE.onSourceChanged();
        }
        window.__demoBroadcast && window.__demoBroadcast('playback', playbackPayload());
    }

    function playNext() {
        if (!queue.length) { playLocal(localTracks[0]?.id); return; }
        if (currentSource === 'spotify') { spotify.next(); return; }
        if (currentSource === 'qobuz') { qobuz.next(); return; }
        const nextIndex = (queueIndex + 1) % queue.length;
        switchToTrack(queue[nextIndex], nextIndex);
    }

    function playPrev() {
        if (positionSec > 5 || queueIndex <= 0) {
            positionSec = 0;
            emitPlayback();
            return;
        }
        switchToTrack(queue[queueIndex - 1], queueIndex - 1);
    }

    function togglePause() {
        if (!currentTrack) {
            playLocal(localTracks[0]?.id);
            return;
        }
        if (currentSource === 'spotify') { spotify.toggle(); return; }
        if (currentSource === 'qobuz') { qobuz.toggle(); return; }
        paused = !paused;
        playing = !paused;
        if (playing) lastTickTs = Date.now();
        emitPlayback();
    }

    // ── Play entry points (used by routes.js) ───────────────────────────
    function playLocal(trackId, queueIds) {
        const chosen = localTracks.find(t => t.id === trackId) || localTracks[0];
        let tracks = localTracks;
        if (Array.isArray(queueIds)) {
            const byId = new Map(localTracks.map(t => [t.id, t]));
            tracks = queueIds.map(id => byId.get(id)).filter(Boolean);
        }
        let idx = Math.max(0, tracks.findIndex(t => t.id === chosen.id));
        if (!tracks.length) tracks = [chosen];
        if (idx < 0) idx = 0;
        startTrack(chosen, 'local', tracks, idx);
        return chosen;
    }

    function playTidal(trackId, queueIds) {
        const pool = tidalTracksLib || [];
        const chosen = pool.find(t => t.id === trackId) || pool[0] || null;
        if (!chosen) return null;
        let tracks = pool;
        if (Array.isArray(queueIds)) {
            const idxMap = new Map(pool.map(t => [t.id, t]));
            tracks = queueIds.map(id => idxMap.get(id)).filter(Boolean);
        }
        const tidalTracks = tracks.map(t => ({ ...t, source: 'tidal' }));
        let idx = Math.max(0, tidalTracks.findIndex(t => String(t.id) === String(trackId)));
        if (idx < 0) idx = 0;
        startTrack({ ...chosen, source: 'tidal' }, 'tidal', tidalTracks, idx);
        return tidalTracks[idx];
    }

    function playRadio(stationId) {
        const station = stations.find(s => s.id === stationId) || stations[0];
        if (!station) return null;
        const track = {
            id: 'radio_' + station.id,
            title: station.title || station.name || 'Station',
            artist: station.artist || 'Radio',
            album: station.title || station.name || 'Radio',
            source: 'radio',
            duration: 0,
            cover_url: station.image_url || '',
            artwork_url: station.image_url || '',
            artwork_available: !!station.image_url,
            artwork_source: 'radio',
            stream_url: station.stream_url,
            url: station.stream_url,
        };
        startTrack(track, 'radio', [track], 0);
        return track;
    }

    // ── Qobuz provider (remote-style transport) ─────────────────────────
    const qobuz = {
        current: { ...qobuzProviderTracks[0], source: 'qobuz' },
        qidx: 0,
        qlist: qobuzProviderTracks.map((track) => ({ ...track, source: 'qobuz' })),
        playing: false,
        positionOffset: 0,
        lastTick: 0,
        shuffle: false,
        loop: 'none',
        demoBooted: false,
        demoStart() {
            const track = this.current || this.qlist[0];
            if (track) {
                this.qlist = qobuzProviderTracks.map((item) => ({ ...item, source: 'qobuz' }));
                this.adopt(track);
                currentSource = 'qobuz';
                currentTrack = { ...track, source: 'qobuz' };
                queue = this.qlist.slice();
                queueIndex = Math.max(0, this.qlist.findIndex((item) => item.id === track.id));
                positionSec = 0;
                playing = true;
                paused = false;
                ended = false;
                clearProviderOwners('qobuz');
                this.playing = true;
                this.lastTick = Date.now();
                this.demoBooted = true;
                clearRadioTimer();
                radioMetadata = null;
                emitPlayback();
                return track;
            }
            return null;
        },
        setPaused() { this.playing = false; },
        adopt(track) {
            this.current = {
                ...track,
                art_url: track.qobuz_art_url || track.art_url || track.cover_url || (lib.demoImage ? lib.demoImage('qobuz:' + (track.title || track.album)) : ''),
            };
            this.playing = true;
            this.positionOffset = 0;
            this.lastTick = Date.now();
            this.qidx = Math.max(0, this.qlist.findIndex((item) => item.id === track.id));
            if (currentSource === 'qobuz') {
                currentTrack = { ...this.current, source: 'qobuz' };
                queue = this.qlist.slice();
                queueIndex = this.qidx;
                positionSec = 0;
                paused = false;
            }
        },
        toggle() {
            if (!this.current) return this.demoStart();
            this.playing = !this.playing;
            this.lastTick = Date.now();
            if (this.playing) {
                clearProviderOwners('qobuz');
                currentSource = 'qobuz';
                currentTrack = { ...this.current, source: 'qobuz' };
                queue = this.qlist.slice();
                queueIndex = Math.max(0, this.qidx);
                playing = true;
                paused = false;
            } else {
                paused = true;
                playing = false;
            }
            emitPlayback();
        },
        position() {
            if (!this.current) return 0;
            if (this.playing) {
                this.positionOffset += (Date.now() - this.lastTick) / 1000;
                this.lastTick = Date.now();
            }
            const dur = Number(this.current.duration || 0);
            if (dur > 0 && this.positionOffset >= dur) { this.next(); return 0; }
            return this.positionOffset;
        },
        advance(dir) {
            const pool = (this.qlist.length ? this.qlist : queue);
            if (!pool.length) return;
            const curId = this.current?.id;
            const idx = pool.findIndex(t => String(t.id) === String(curId));
            const next = pool[(idx + dir + pool.length) % pool.length];
            this.adopt(next);
            emitPlayback();
        },
        next() { this.advance(1); },
        previous() { this.advance(-1); },
        seek(pos) { this.positionOffset = Math.max(0, Number(pos) || 0); this.lastTick = Date.now(); emitPlayback(); },
        toggleShuffle() { this.shuffle = !this.shuffle; },
        cycleLoop() { this.loop = this.loop === 'none' ? 'playlist' : this.loop === 'playlist' ? 'track' : 'none'; },
        queueInfo() {
            const pool = (this.qlist.length ? this.qlist : queue);
            if (!pool.length) return { queue_len: 0, queue_index: 0, next_track: null };
            const curId = this.current?.id;
            const idx = Math.max(0, pool.findIndex(t => String(t.id) === String(curId)));
            const next = pool[(idx + 1) % pool.length];
            return {
                queue_len: pool.length,
                queue_index: idx + 1,
                next_track: next ? { title: next.title, artist: next.artist } : null,
            };
        },
        payload() {
            const dur = Number(this.current?.duration || 0);
            return {
                installed: true,
                available: true,
                authenticated: true,
                connected: true,
                status: this.playing ? 'Playing' : (this.current ? 'Paused' : 'Stopped'),
                title: this.current?.title || '',
                artist: this.current?.artist || '',
                album: this.current?.album || '',
                artUrl: this.current?.art_url || this.current?.cover_url || '',
                position: Math.round(this.position()),
                duration: dur,
                shuffle: this.shuffle,
                loop: this.loop,
                volume,
                source: 'qobuz',
                footer_owner: this.playing ? 'qobuz' : null,
                // Real qbzd stream facts (streaming/qobuz/provider.py): every
                // Qobuz tier streams FLAC, so the footer tag renders the full
                // 'FLAC · 16/24 bit · rate kHz' line instead of the bare
                // hardware rate fallback.
                audio_format: 'flac',
                bit_depth: Number(this.current?.bit_depth) || null,
                sample_rate: Number(this.current?.sample_rate_hz) || null,
                capabilities: { transport: true, cover: true, progress: true, seek: true, shuffle: true, loop: true },
                ...this.queueInfo(),
            };
        },
    };

    // ── Spotify provider ────────────────────────────────────────────────
    // ── Spotify provider ────────────────────────────────────────────────
    const spotify = {
        current: null,
        playing: false,
        positionOffset: 0,
        lastTick: 0,
        shuffle: false,
        loop: 'none',
        list: spotifyProviderTracks.map((track) => ({ ...track, source: 'spotify' })),
        demoBooted: false,
        demoStart() {
            const track = this.current || this.list[0];
            if (track) {
                this.adopt(track);
                currentSource = 'spotify';
                currentTrack = { ...track, source: 'spotify' };
                queue = this.list.slice();
                queueIndex = Math.max(0, this.list.findIndex((item) => item.id === track.id));
                positionSec = 0;
                playing = true;
                paused = false;
                ended = false;
                clearProviderOwners('spotify');
                this.playing = true;
                this.lastTick = Date.now();
                this.demoBooted = true;
                clearRadioTimer();
                radioMetadata = null;
                emitPlayback();
                return track;
            }
            return null;
        },
        setPaused() { this.playing = false; },
        adopt(track) {
            const art = track.spotify_art_url || track.art_url || track.cover_url || (lib.demoImage ? lib.demoImage('spotify:' + (track.title || track.album)) : '');
            this.current = { ...track, art_url: art, title: track.title, artist: track.artist, album: track.album, duration: Number(track.duration || 0) };
            this.playing = true;
            this.positionOffset = 0;
            this.lastTick = Date.now();
            if (currentSource === 'spotify') {
                currentTrack = { ...this.current, source: 'spotify' };
                queue = this.list.slice();
                queueIndex = Math.max(0, this.list.findIndex((item) => item.id === track.id));
                positionSec = 0;
                paused = false;
            }
        },
        toggle() {
            if (!this.current) return this.demoStart();
            this.playing = !this.playing;
            this.lastTick = Date.now();
            if (this.playing) {
                clearProviderOwners('spotify');
                currentSource = 'spotify';
                currentTrack = { ...this.current, source: 'spotify' };
                queue = this.list.slice();
                queueIndex = Math.max(0, this.list.findIndex((item) => item.id === this.current.id));
                playing = true;
                paused = false;
            } else {
                paused = true;
                playing = false;
            }
            emitPlayback();
        },
        position() {
            if (!this.current) return 0;
            if (this.playing) {
                this.positionOffset += (Date.now() - this.lastTick) / 1000;
                this.lastTick = Date.now();
            }
            const dur = Number(this.current.duration || 0);
            if (dur > 0 && this.positionOffset >= dur) { this.next(); return 0; }
            return this.positionOffset;
        },
        advance(dir) {
            const pool = this.list || [];
            if (!pool.length) return;
            const curId = this.current?.id;
            const idx = pool.findIndex(t => String(t.id) === String(curId));
            const next = pool[(idx + dir + pool.length) % pool.length];
            this.adopt(next);
            emitPlayback();
        },
        next() { this.advance(1); },
        previous() { this.advance(-1); },
        seek(pos) { this.positionOffset = Math.max(0, Number(pos) || 0); this.lastTick = Date.now(); emitPlayback(); },
        toggleShuffle() { this.shuffle = !this.shuffle; },
        cycleLoop() { this.loop = this.loop === 'none' ? 'playlist' : this.loop === 'playlist' ? 'track' : 'none'; },
        queueInfo() {
            const pool = this.list || [];
            if (!pool.length) return { queue_len: 0, queue_index: 0, next_track: null };
            const curId = this.current?.id;
            const idx = Math.max(0, pool.findIndex(t => String(t.id) === String(curId)));
            const next = pool[(idx + 1) % pool.length];
            return {
                queue_len: pool.length,
                queue_index: idx + 1,
                next_track: next ? { title: next.title, artist: next.artist } : null,
            };
        },
        snapshot() {
            const cur = this.current;
            return {
                installed: true,
                available: true,
                source: 'spotify',
                connected: true,
                status: this.playing ? 'Playing' : (cur ? 'Paused' : 'Stopped'),
                artist: cur?.artist || '',
                title: cur?.title || '',
                album: cur?.album || '',
                artUrl: cur?.art_url || '',
                artwork_url: cur?.art_url || '',
                artwork_available: !!cur,
                artwork_source: cur ? 'spotify' : 'none',
                // MPRIS-shaped track id like playerctl reports (d-bus
                // object path), so track-change detection behaves like the
                // real payload rather than matching library ids.
                trackId: cur?.id ? 'spotify:track:' + cur.id : '',
                trackid: cur?.id ? 'spotify:track:' + cur.id : '',
                position: Math.round(this.position()),
                duration: cur?.duration || 0,
                shuffle: this.shuffle,
                loop: this.loop,
                volume,
                source_volume: 100,
                footer_owner: this.playing ? 'spotify' : null,
                // Real provider capability set (streaming/spotify/provider.py):
                // transport-only MPRIS surface plus volume and cover.
                capabilities: { transport: true, cover: true, progress: true, seek: true, shuffle: true, loop: true, volume: true },
                ...this.queueInfo(),
            };
        },
    };

    // Both connected provider cards start with a paused track selected. This
    // keeps the UI in the normal now-playing state without claiming playback
    // ownership until the user presses Play.
    spotify.current = { ...spotify.list[0], art_url: spotify.list[0]?.spotify_art_url || spotify.list[0]?.art_url || spotify.list[0]?.cover_url || lib.demoImage('spotify:' + (spotify.list[0]?.title || spotify.list[0]?.album || 'demo')) };
    spotify.positionOffset = 0;
    spotify.lastTick = Date.now();
    qobuz.current = { ...qobuz.current, art_url: qobuz.current?.qobuz_art_url || qobuz.current?.art_url || qobuz.current?.cover_url || lib.demoImage('qobuz:' + (qobuz.current?.title || qobuz.current?.album || 'demo')) };
    qobuz.positionOffset = 0;
    qobuz.lastTick = Date.now();

    // ── Measurements ────────────────────────────────────────────────────
    // Seeded with real saved measurements from .104
    // (demo/data/measurements.js); new demo sweeps reuse those real
    // datasets by name so the simulated result renders exactly like the
    // saved measurements.
    const savedMeasurementFixtures = (window.FXROUTE_DEMO_MEASUREMENTS
        && Array.isArray(window.FXROUTE_DEMO_MEASUREMENTS.savedMeasurements)
        ? window.FXROUTE_DEMO_MEASUREMENTS.savedMeasurements
        : []).slice();
    let measurements = savedMeasurementFixtures.slice();
    let measurementSeq = 0;
    let sweepFixtureIndex = 0;
    let lastJobId = 0;
    const jobs = Object.create(null);

    // Fixture roles follow the .104 measurement names: plain single sweeps
    // by channel, close-mic repeats as L/R-repeat sources, convolver
    // captures as filter Before/Reference slots.
    function fixtureByName(match, channel) {
        const normalized = String(channel || '').toLowerCase();
        return savedMeasurementFixtures.find((fixture) => {
            if (normalized && String(fixture.channel || '').toLowerCase() !== normalized
                && String(fixture.channel || '').toLowerCase() !== 'stereo') return false;
            return match.test(String(fixture.name || ''));
        }) || null;
    }
    function fixtureForChannel(channel) {
        const normalized = String(channel || 'left').toLowerCase();
        if (!savedMeasurementFixtures.length) return null;
        if (normalized === 'right') {
            return fixtureByName(/Sweep-R-Raw/, 'right')
                || fixtureByName(/Raw/, 'right')
                || savedMeasurementFixtures[1]
                || savedMeasurementFixtures[0];
        }
        return fixtureByName(/Sweep-L-Raw/, 'left')
            || fixtureByName(/Raw/, 'left')
            || savedMeasurementFixtures[0];
    }
    function fixtureForRepeat(channel) {
        const normalized = String(channel || 'left').toLowerCase();
        return fixtureByName(/Close/, normalized)
            || fixtureForChannel(normalized);
    }

    function makeComplexResponse(tracePoints, count = 160, maxHz = 2000) {
        const points = Array.isArray(tracePoints) ? tracePoints : [];
        if (!points.length) return { schema: 'fxroute.complex-response.v1', points: [] };
        const logMin = Math.log(20);
        const logMax = Math.log(Math.max(21, Math.min(maxHz, Number(points[points.length - 1][0]) || maxHz)));
        const freqs = [];
        for (let index = 0; index < count; index += 1) {
            freqs.push(Math.exp(logMin + ((logMax - logMin) * index) / Math.max(1, count - 1)));
        }
        const interpDb = (freq) => {
            if (freq <= points[0][0]) return Number(points[0][1]) || 0;
            if (freq >= points[points.length - 1][0]) return Number(points[points.length - 1][1]) || 0;
            let lo = 0;
            let hi = points.length - 1;
            while (hi - lo > 1) {
                const mid = (lo + hi) >> 1;
                if (Number(points[mid][0]) <= freq) lo = mid;
                else hi = mid;
            }
            const f0 = Number(points[lo][0]);
            const v0 = Number(points[lo][1]) || 0;
            const f1 = Number(points[hi][0]);
            const v1 = Number(points[hi][1]) || 0;
            return v0 + ((v1 - v0) * ((freq - f0) / Math.max(1e-9, f1 - f0)));
        };
        const complex = freqs.map((freq) => {
            const magnitude = Math.pow(10, interpDb(freq) / 20);
            return [Math.round(freq * 1000) / 1000, Math.round(magnitude * 1e6) / 1e6, 0];
        });
        return { schema: 'fxroute.complex-response.v1', points: complex };
    }

    function makeGatedDirectResponse(tracePoints, lowerLimitHz = 300) {
        const points = Array.isArray(tracePoints) ? tracePoints : [];
        const gated = points
            .filter((point) => Number(point[0]) >= lowerLimitHz)
            .map((point) => [Number(point[0]), Math.round((Number(point[1]) + 0.35) * 1000) / 1000]);
        return {
            usable: true,
            status: 'ok',
            retry_reason: '',
            direct_confidence: 1,
            gated_direct_lower_limit_hz: lowerLimitHz,
            points: gated,
        };
    }

    // Harmonizes reference timing for the demo: both direct captures use a
    // dual-channel acoustic reference with nearly identical arrival times so
    // the frontend L/R microphone position check passes (delta <= 2.5 ms).
    function harmonizeDirectTiming(measurement, channel) {
        const analysis = measurement.analysis = measurement.analysis || {};
        const reference = analysis.reference_path = analysis.reference_path || {};
        const impulse = analysis.impulse_response = analysis.impulse_response || {};
        const baseMs = String(channel || 'left').toLowerCase() === 'right' ? 84.8 : 84.4;
        reference.capture_mode = 'dual-channel';
        reference.timing_status = 'acoustic-only';
        reference.timing_label = 'Acoustic-only timing';
        reference.acoustic_arrival_corrected_ms = baseMs;
        reference.acoustic_arrival_corrected_samples = Math.round(baseMs * 48);
        reference.stability = 'host-reference';
        reference.confidence = 1;
        reference.usable = true;
        reference.electrical_reference_used = false;
        impulse.arrival_ms = baseMs;
        impulse.arrival_samples = Math.round(baseMs * 48);
        impulse.direct_confidence = 1;
        if (measurement.input_channels) {
            measurement.input_channels.electrical_reference = null;
        }
        analysis.sample_rate = 48000;
    }

    // Builds a complex response whose magnitude exactly matches the sum of
    // the two MLP captures. The integration step validates L+R against the
    // measured Main+Sub capture; identical magnitudes keep the residual
    // within the 'ok' band so the subwoofer alignment check passes.
    function makeIntegrationComplexResponse(mlpLeft, mlpRight) {
        const left = mlpLeft && mlpLeft.analysis && mlpLeft.analysis.complex_response ? mlpLeft.analysis.complex_response.points : [];
        const right = mlpRight && mlpRight.analysis && mlpRight.analysis.complex_response ? mlpRight.analysis.complex_response.points : [];
        if (!left.length || !right.length) return makeComplexResponse([]);
        const count = Math.min(left.length, right.length);
        const points = [];
        for (let index = 0; index < count; index += 1) {
            const frequency = Number(left[index][0]);
            const real = Number(left[index][1]) + Number(right[index][1]);
            const imag = Number(left[index][2]) + Number(right[index][2]);
            points.push([Math.round(frequency * 1000) / 1000, Math.round(real * 1e6) / 1e6, Math.round(imag * 1e6) / 1e6]);
        }
        return { schema: 'fxroute.complex-response.v1', points };
    }

    // Remembers the last MLP captures per channel so the integration step can
    // build its complex response as the exact L+R sum of the actual wizard
    // measurements (the frontend validates Main vs L+R in 20-500 Hz).
    const mlpCaptures = { left: null, right: null };

    function rememberMlpCapture(measurement) {
        if (!measurement || !measurement.analysis || !measurement.analysis.complex_response) return;
        const channel = String(measurement.channel || 'left').toLowerCase();
        mlpCaptures[channel === 'right' ? 'right' : 'left'] = measurement;
    }

    function prepareFixtureMeasurement(measurement, opts = {}) {
        const channel = String(opts.channel || measurement.channel || 'left').toLowerCase();
        const role = String(opts.role || measurement.measurement_role || 'single');
        const copy = JSON.parse(JSON.stringify(measurement));
        copy.channel = channel;
        copy.measurement_role = role;
        const tracePoints = (Array.isArray(copy.traces) && copy.traces[0] && Array.isArray(copy.traces[0].points))
            ? copy.traces[0].points
            : [];
        const analysis = copy.analysis = copy.analysis || {};
        if (role === 'direct') {
            analysis.direct_response = makeGatedDirectResponse(tracePoints);
            harmonizeDirectTiming(copy, channel);
        }
        if (role === 'mlp' || role === 'secondary') {
            analysis.complex_response = makeComplexResponse(tracePoints);
            if (role === 'mlp') rememberMlpCapture(copy);
        }
        if (role === 'integration') {
            analysis.complex_response = makeIntegrationComplexResponse(mlpCaptures.left, mlpCaptures.right);
        }
        if (role === 'integration' || role === 'mlp') {
            harmonizeDirectTiming(copy, channel);
        }
        return copy;
    }

    function makeMeasurement(opts = {}) {
        const id = opts.id || 'demo_meas_' + (++measurementSeq);
        const name = opts.name || 'Demo Measurement ' + measurementSeq;
        const created = new Date(Date.now() - (opts.createdOffsetMs || 0)).toISOString();
        if (savedMeasurementFixtures.length) {
            const role = String(opts.role || '').toLowerCase();
            const channel = String(opts.channel || 'left').toLowerCase();
            let source = null;
            if (role === 'direct') {
                source = fixtureByName(/Close/, channel) || fixtureForChannel(channel);
            } else if (role === 'mlp' || role === 'secondary') {
                source = fixtureByName(/Convolver/, channel) || fixtureForChannel(channel);
            } else if (role === 'integration') {
                source = fixtureByName(/L\/R-Convolver/, 'stereo')
                    || fixtureByName(/L\/R-Raw/, 'stereo')
                    || fixtureForChannel(channel);
            } else if (channel === 'right') {
                source = fixtureForChannel('right');
            } else if (channel === 'stereo') {
                source = fixtureByName(/L\/R-Raw/, 'stereo') || fixtureForChannel('left');
            } else {
                const singles = savedMeasurementFixtures.filter((fixture) =>
                    /Sweep-.*-Raw/.test(String(fixture.name || '')) && String(fixture.channel || '').toLowerCase() === 'left');
                const pool = singles.length ? singles : savedMeasurementFixtures;
                source = pool[sweepFixtureIndex % pool.length];
            }
            sweepFixtureIndex += 1;
            const measurement = prepareFixtureMeasurement(source, opts);
            measurement.id = id;
            measurement.name = name;
            measurement.created_at = created;
            if (opts.channel) measurement.channel = String(opts.channel).toLowerCase();
            if (opts.role) measurement.measurement_role = String(opts.role);
            if (opts.kind) measurement.measurement_kind = String(opts.kind);
            return measurement;
        }
        return {
            id,
            name,
            created_at: created,
            channel: opts.channel || 'left',
            measurement_kind: opts.kind || 'sweep',
            measurement_role: opts.role || 'single',
            traces: [],
            review_traces: [],
            analysis: {},
            summary: {},
            input_device: { id: 'demo_mic', label: 'Demo Microphone' },
            input_channels: { mic: '1' },
            calibration: {},
            audio_output_context: { mode: opts.mode || 'stereo', sample_rate: 48000 },
        };
    }

    function startMeasurement(opts = {}) {
        const id = 'demo_job_' + (++lastJobId);
        const measurement = makeMeasurement({ ...opts, id: 'demo_meas_' + id });
        jobs[id] = {
            id,
            status: 'running',
            started_at: new Date().toISOString(),
            progress_pct: 0,
            message: 'Sweep running…',
            measurement,
            result: null,
            timer: null,
        };
        jobs[id].timer = setInterval(() => {
            const j = jobs[id];
            if (!j) return;
            j.progress_pct = Math.min(100, j.progress_pct + 5 + Math.random() * 8);
            j.message = j.progress_pct >= 100 ? 'Processing measurement…' : 'Sweep running…';
            if (j.progress_pct >= 100) {
                clearInterval(j.timer);
                j.timer = null;
                j.status = 'completed';
                j.message = 'Measurement finished.';
                j.result = { measurement: j.measurement };
            }
        }, 300);
        return id;
    }

    function buildLrRepeatSummary(baseName, fixture, channel) {
        const copy = JSON.parse(JSON.stringify(fixture));
        copy.id = 'demo_lr_summary_' + channel + '_' + Date.now();
        copy.name = baseName + ' · ' + (channel === 'right' ? 'R' : 'L');
        copy.channel = channel;
        copy.measurement_kind = 'lr-repeat-summary';
        copy.measurement_role = '';
        copy.created_at = new Date().toISOString();
        const analysis = copy.analysis = copy.analysis || {};
        analysis.method = 'same-position-lr-repeat-paired-delta';
        analysis.sample_rate = 48000;
        const reference = analysis.reference_path = analysis.reference_path || {};
        const impulse = analysis.impulse_response = analysis.impulse_response || {};
        const arrivalMs = channel === 'right' ? 84.8 : 84.4;
        reference.capture_mode = 'dual-channel';
        reference.timing_status = 'lr-repeat';
        reference.timing_label = 'L/R repeat timing (paired)';
        reference.stability = 'stable';
        reference.usable = true;
        reference.confidence = 1;
        reference.electrical_reference_used = true;
        reference.acoustic_arrival_corrected_ms = arrivalMs;
        reference.acoustic_arrival_corrected_samples = Math.round(arrivalMs * 48);
        impulse.arrival_ms = arrivalMs;
        impulse.arrival_samples = Math.round(arrivalMs * 48);
        analysis.lr_repeat = {
            repeat_count: 3,
            pair_count: 3,
            accepted_runs: 3,
            rejected_runs: 0,
            accepted_run_numbers: [1, 2, 3],
            rejected_run_numbers: [],
            delta_center_ms: channel === 'right' ? -0.43 : 0.43,
            delta_spread_ms: 0.06,
            timing_method: 'paired-delta-cluster',
            timing_stable: true,
            electrical_reference_used: true,
            reference_source: 'electrical-input-channel-2',
            pre_averaged: false,
        };
        analysis.quality_checks = { status: 'pass', items: [] };
        return copy;
    }

    function startLrRepeatMeasurement(opts = {}) {
        const id = 'demo_job_' + (++lastJobId);
        const baseName = String(opts.base_name || '').trim() || ('L/R Repeat ' + new Date().toISOString().replace(/[-:]/g, '').slice(0, 15));
        const totalSweeps = 6;
        let sweepNumber = 0;
        const job = {
            id,
            job_kind: 'lr-repeat',
            channel: 'stereo',
            status: 'running',
            started_at: new Date().toISOString(),
            progress_pct: 0,
            message: 'L/R repeat queued.',
            result: null,
            timer: null,
            base_name: baseName,
        };
        jobs[id] = job;
        job.timer = setInterval(() => {
            if (!jobs[id]) return;
            sweepNumber += 1;
            const repeatIndex = Math.floor((sweepNumber - 1) / 2) + 1;
            const channel = (sweepNumber % 2 === 1) ? 'left' : 'right';
            job.progress_pct = Math.min(100, Math.round((sweepNumber / totalSweeps) * 100));
            if (sweepNumber < totalSweeps) {
                job.message = 'L/R repeat ' + sweepNumber + '/' + totalSweeps + ': ' + channel.toUpperCase() + repeatIndex + '…';
                return;
            }
            clearInterval(job.timer);
            job.timer = null;
            job.status = 'completed';
            job.message = 'L/R repeat finished. Review the combined L and R results, then save them together.';
            const left = buildLrRepeatSummary(baseName, fixtureForRepeat('left'), 'left');
            const right = buildLrRepeatSummary(baseName, fixtureForRepeat('right'), 'right');
            job.result = {
                measurements: [left, right],
                base_name: baseName,
                message: job.message,
                scope_note: 'FXRoute measures with a host-local sweep through the DSP chain.',
            };
        }, 420);
        return id;
    }

    function cancelJob(id) {
        const j = jobs[id];
        if (!j || j.status !== 'running') return;
        if (j.timer) clearInterval(j.timer);
        j.status = 'cancelled';
        j.message = 'Measurement cancelled.';
    }

    function jobPayload(id) {
        const j = jobs[id];
        if (!j) return null;
        const running = j.status === 'running';
        return {
            id: j.id,
            job_kind: j.job_kind || 'single',
            status: j.status,
            progress_pct: Math.round(j.progress_pct),
            message: j.message,
            started_at: j.started_at,
            result: j.status === 'completed' ? (j.result || { measurement: j.measurement }) : null,
            input_level: running ? { peak_dbfs: -6 + Math.random() * 4 - 2, clipped: false } : null,
        };
    }

    function addSavedMeasurement(measurement) {
        measurements.unshift(measurement);
        return measurement;
    }

    function getSavedMeasurements() {
        return measurements.slice();
    }

    function deleteSavedMeasurement(id) {
        measurements = measurements.filter(m => m.id !== id);
    }

    // ── SPL calibration state ───────────────────────────────────────────
    const spl = {
        noiseActive: false,
        operationActive: false,
        automaticAvailable: true,
        microphoneModel: 'UMIK-1',
        serialNumber: '7148364',
        calibrationFileId: 'demo-umm6',
        calibrationFilename: '7148364.txt',
        measuredSplDb: 82.6,
        targetSplDb: 83.0,
        calibration: null,
    };

    function splCalibrationPayload() {
        return {
            status: spl.operationActive ? 'measuring' : (spl.noiseActive ? 'playing' : 'idle'),
            target_spl_db: spl.targetSplDb,
            noise_lufs: -23.0,
            meter_hint: 'C-weighted / Slow',
            output_profile: {
                id: 'demo-output-profile',
                label: 'Demo Output',
            },
            loudness: {
                enabled: false,
                params: {
                    strength: 10,
                    fftSize: 4096,
                    volumeDb: 0,
                },
            },
            automatic: {
                available: spl.automaticAvailable,
                microphone_model: spl.microphoneModel,
                serial_number: spl.serialNumber,
                calibration_file_id: spl.calibrationFileId,
                calibration_filename: spl.calibrationFilename,
                checks: {
                    selected_input_is_umik1: true,
                    unique_connected_umik1: true,
                    calibration_selected: true,
                    sensitivity_factor_parsed: true,
                    calibration_serial_matches_filename: true,
                    internal_gain_18_db: true,
                    capture_gain_known: true,
                },
                reason: '',
            },
            noise_active: spl.noiseActive,
            operation_active: spl.operationActive,
        };
    }

    function splMeasureAutomatic() {
        spl.operationActive = true;
        spl.noiseActive = false;
        const measured = 82.6 + (Math.random() * 0.6 - 0.3);
        spl.measuredSplDb = Math.round(measured * 10) / 10;
        const adjustment = Math.round((spl.targetSplDb - spl.measuredSplDb) * 10) / 10;
        return {
            status: 'ok',
            measured_spl_db: spl.measuredSplDb,
            required_adjustment_db: adjustment,
            weighting: 'C',
            averaging_seconds: 3.0,
            microphone_model: spl.microphoneModel,
            serial_number: spl.serialNumber,
        };
    }

    function splApplyCalibration(measuredSplDb) {
        const measured = Number(measuredSplDb);
        if (!Number.isFinite(measured) || measured < 40 || measured > 130) {
            return { error: 'Measured SPL must be between 40 and 130 dB' };
        }
        const adjustment = Math.round((spl.targetSplDb - measured) * 10) / 10;
        const calibrated = Math.abs(adjustment) <= 1.0;
        spl.noiseActive = false;
        spl.operationActive = false;
        spl.measuredSplDb = measured;
        spl.calibration = {
            outputProfileId: 'demo-output-profile',
            outputProfileLabel: 'Demo Output',
            targetSplDb: spl.targetSplDb,
            measuredSplDb: measured,
            requiredAdjustmentDb: adjustment,
            calibrated,
            method: spl.automaticAvailable ? 'automatic' : 'manual',
            microphoneModel: spl.microphoneModel,
            calibrationFileId: spl.calibrationFileId,
            calibrationFilename: spl.calibrationFilename,
            createdAt: new Date().toISOString(),
        };
        return {
            status: 'ok',
            required_adjustment_db: adjustment,
            calibrated,
            calibration: spl.calibration,
        };
    }

    window.FXROUTE_DEMO_STATE = {
        lib,
        localTracks,
        tidalTracks: () => tidalTracksLib || [],
        tidalAlbums: () => tidalAlbumsLib || [],
        tidalArtists: () => tidalArtistsLib || [],
        tidalPlaylists: () => tidalPlaylistsLib || [],
        stations,
        catalogStations,
        playLocal,
        playTidal,
        playRadio,
        playNext,
        playPrev,
        togglePause,
        stop() {
            clearRadioTimer();
            playing = false;
            paused = false;
            ended = false;
            currentTrack = null;
            queue = [];
            queueIndex = -1;
            radioMetadata = null;
            spotify.setPaused();
            qobuz.setPaused();
            emitPlayback();
        },
        clearQueue() { this.stop(); },
        setVolume(v) { volume = Math.max(0, Math.min(100, Number(v) || 0)); emitPlayback(); },
        getVolume() { return volume; },
        setShuffle(on) { shuffle = !!on; },
        setLoop(on) { loop = !!on; },
        seek(pos) { positionSec = Math.max(0, Number(pos) || 0); emitPlayback(); },
        getPlayback: playbackPayload,
        getMeter() { return meter; },
        getPeak: peakSnapshot,
        peakSnapshot,
        setDspSnapshot,
        // measurement helpers (consumed by routes.js)
        startMeasurement,
        startLrRepeatMeasurement,
        cancelJob,
        jobPayload,
        addSavedMeasurement,
        getSavedMeasurements,
        deleteSavedMeasurement,
        makeMeasurement,
        // spl calibration helpers (consumed by routes.js)
        spl,
        splCalibrationPayload,
        splMeasureAutomatic,
        splApplyCalibration,
        // providers
        spotify,
        qobuz,
        // Simulation hook: routes.js mirrors the native-kHz behavior here so
        // the graph rate follows TIDAL Hi-Res/lossless playback like the real
        // system (see /api/audio/samplerate).
        onSourceChanged: null,
    };

    // Broadcast alias used by other modules.
    window.FXROUTE_DEMO_STATE = window.FXROUTE_DEMO_STATE || {};
    window.FXROUTE_DEMO_STATE.core = window.FXROUTE_DEMO_STATE_CORE || {};
    window.FXROUTE_DEMO_STATE_CORE = window.FXROUTE_DEMO_STATE_CORE || window.FXROUTE_DEMO_STATE;
    window.FXROUTE_DEMO_STATE.core = window.FXROUTE_DEMO_STATE_CORE.core || window.FXROUTE_DEMO_STATE_CORE;
    if (!window.__demoBroadcast) window.__demoBroadcast = function () {};
    window.FXROUTE_DEMO_STATE_CORE.__demoBroadcast = window.__demoBroadcast;
})();