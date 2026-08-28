// Demo streaming bridge: exposes the simulated providers to the real frontend
// module (window.FXRouteStreaming) the same way the real backend would.
(function () {
    'use strict';

    if (window.FXROUTE_DEMO_STREAMING_LOADED) return;
    window.FXROUTE_DEMO_STREAMING_LOADED = true;

    const S = window.FXROUTE_DEMO_STATE;

    // The real frontend's streaming module renders providers from status
    // payloads. Our fetch interceptor already answers every provider status
    // endpoint, so the extra work here is minimal: nothing to do beyond
    // keeping the module idempotent and exposing a hook for tests.
    window.FXROUTE_DEMO_STREAMING = {
        refresh: function () {
            window.__demoBroadcast && window.__demoBroadcast('playback', S.getPlayback());
        },
    };
})();