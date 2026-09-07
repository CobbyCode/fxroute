// Fake WebSocket for the demo: replaces window.WebSocket so the unmodified
// FXRoute frontend connects to the in-page simulation. Pushes the same
// message types as the real backend (init, playback, spotify, qobuz,
// playback_peak_warning, dsp).
(function () {
    'use strict';

    if (window.FXROUTE_DEMO_WS_LOADED) return;
    window.FXROUTE_DEMO_WS_LOADED = true;

    const S = window.FXROUTE_DEMO_STATE;
    const api = window.FXROUTE_DEMO_API;

    const CONNECTING = 0;
    const OPEN = 1;
    const CLOSING = 2;
    const CLOSED = 3;

    const sockets = new Set();

    function broadcast(message) {
        const payload = JSON.stringify(message);
        for (const socket of sockets) {
            if (socket.readyState === OPEN && typeof socket.onmessage === 'function') {
                socket.onmessage({ data: payload, origin: '', source: null });
            }
        }
    }
    window.__demoBroadcast = function (type, data) {
        if (type === 'playback') {
            broadcast({ type: 'playback', data });
        } else if (type === 'playback_peak_warning') {
            broadcast({ type: 'playback_peak_warning', data });
        } else if (type === 'spotify') {
            broadcast({ type: 'spotify', data });
        } else if (type === 'qobuz') {
            broadcast({ type: 'qobuz', data });
        } else if (type === 'dsp') {
            broadcast({ type: 'dsp', data });
        } else if (type === 'measurement_done') {
            // No dedicated WS type in the app; the dsp push nudges reloads.
            broadcast({ type: 'dsp', data: api.easyeffectsStatus() });
        }
    };

    function initMessage() {
        const st = api.playbackPayload();
        st.dsp = api.easyeffectsStatus();
        return {
            type: 'init',
            data: {
                player: { state: st },
                stations: JSON.parse(JSON.stringify(S.stations.map(s => ({ ...s, catalog: [] })))),
                // No `catalog` field: the current frontend no longer implements
                // radioModule.setCatalogStations; the catalog is served over
                // the station-catalog HTTP routes instead.
                spotify: S.spotify.snapshot(),
                qobuz: S.qobuz.payload(),
                // Do NOT send library — the app clears tracks on a truthy value.
            },
        };
    }

    function sendTo(socket, message) {
        if (socket.readyState !== OPEN || typeof socket.onmessage !== 'function') return;
        socket.onmessage({ data: JSON.stringify(message), origin: '', source: null });
    }

    class DemoWebSocket {
        constructor(url) {
            this.url = String(url || '');
            this.readyState = CONNECTING;
            this.bufferedAmount = 0;
            this.extensions = '';
            this.protocol = '';
            this.binaryType = 'blob';
            this.onopen = null;
            this.onclose = null;
            this.onerror = null;
            this.onmessage = null;
            setTimeout(() => {
                if (this.readyState !== CONNECTING) return;
                this.readyState = OPEN;
                sockets.add(this);
                if (typeof this.onopen === 'function') this.onopen({ type: 'open', target: this });
                sendTo(this, initMessage());
            }, 40);
        }
        send() { /* no client->server commands in the app */ }
        close(code, reason) {
            if (this.readyState === CLOSING || this.readyState === CLOSED) return;
            this.readyState = CLOSING;
            setTimeout(() => {
                sockets.delete(this);
                this.readyState = CLOSED;
                if (typeof this.onclose === 'function') {
                    this.onclose({ wasClean: true, code: Number(code) || 1000, reason: String(reason || ''), target: this });
                }
            }, 10);
        }
        addEventListener() {}
        removeEventListener() {}
        dispatchEvent() { return false; }
    }

    // One playback pulse per second, plus alternating provider snapshots.
    setInterval(() => {
        if (!sockets.size) return;
        const payload = S.getPlayback();
        broadcast({ type: 'playback', data: payload });
        broadcast({ type: 'playback_peak_warning', data: payload.output_peak_warning });
        broadcast({ type: 'spotify', data: S.spotify.snapshot() });
        broadcast({ type: 'qobuz', data: S.qobuz.payload() });
        broadcast({ type: 'dsp', data: api.easyeffectsStatus() });
    }, 1000);

    window.DemoWebSocket = DemoWebSocket;
})();