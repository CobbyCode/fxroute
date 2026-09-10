// Demo boot: swaps in the fake transport before the real frontend loads,
// then injects a small demo banner into the header.
(function () {
    'use strict';

    if (window.FXROUTE_DEMO_BOOTED) return;
    window.FXROUTE_DEMO_BOOTED = true;

    window.WebSocket = window.DemoWebSocket;

    // Arm the boot realism cycle: the first /api/library/status and
    // /api/music-libraries calls after page load replay a short library
    // scan and a share-discovery rescan (routes.js consumes this flag
    // lazily, since this file loads after it).
    window.__demoArmBootScan = true;

    // Bridge used by the real frontend: measurement_graph.js calls
    // ui.smoothMeasurementTracePoints on the MeasurementUI module, but the
    // implementation lives in MeasurementDsp. The real frontend modules are
    // loaded after this demo boot script (the built demo moves boot.js into
    // the pre-script block), so retry until both modules exist.
    function installMeasurementSmoothingBridge() {
        const graphUi = window.FXRouteMeasurementUI || null;
        const graphDsp = window.FXRouteMeasurementDsp || null;
        if (graphUi && graphDsp && typeof graphUi.smoothMeasurementTracePoints !== 'function'
            && typeof graphDsp.smoothMeasurementTracePoints === 'function') {
            graphUi.smoothMeasurementTracePoints = graphDsp.smoothMeasurementTracePoints;
            return true;
        }
        return false;
    }
    if (!installMeasurementSmoothingBridge()) {
        if (typeof window.setTimeout === 'function') {
            window.setTimeout(installMeasurementSmoothingBridge, 0);
        }
        if (typeof window.setInterval === 'function' && typeof window.clearInterval === 'function') {
            const bridgeTimer = window.setInterval(function () {
                if (installMeasurementSmoothingBridge()) window.clearInterval(bridgeTimer);
            }, 50);
            if (typeof window.addEventListener === 'function') {
                window.addEventListener('load', function () {
                    installMeasurementSmoothingBridge();
                    window.clearInterval(bridgeTimer);
                });
            }
        }
    }

    const bannerStorageKey = 'fxroute-demo-banner-hidden';
    let bannerHidden = false;
    try { bannerHidden = sessionStorage.getItem(bannerStorageKey) === '1'; } catch (e) {}
    const banner = document.createElement('div');
    banner.id = 'demo-banner';
    banner.className = 'demo-banner' + (bannerHidden ? ' is-hidden' : '');
    banner.setAttribute('role', 'status');
    const tourDisabled = typeof window.location !== 'undefined'
        && /(?:^|[?&])notour(?:&|$)/.test(window.location.search || '');
    banner.innerHTML = '<strong>FXRoute Web-Demo</strong> — simulation, no real audio playback · '
        + (tourDisabled ? '' : '<a href="#" id="demo-tour-start" class="demo-banner-tour">Take the tour</a> · ')
        + '<a href="https://github.com/' + (window.FXROUTE_DEMO_REPO || 'CobbyCode/fxroute') + '" target="_blank" rel="noopener">GitHub project</a>'
        + '<button type="button" class="demo-banner-close" aria-label="Hide demo notice" title="Hide demo notice">×</button>';
    banner.querySelector('.demo-banner-close').addEventListener('click', function () {
        banner.classList.add('is-hidden');
        try { sessionStorage.setItem(bannerStorageKey, '1'); } catch (e) {}
    });
    document.body.appendChild(banner);

    // Park the banner just above the real playback footer instead of relying
    // on a hardcoded footer height: the footer is 112px+ tall (plus safe-area)
    // and its z-index (100) beats the banner's (60), so an overlapping strip
    // would sit behind it. The demo scripts run before the footer markup
    // exists, so poll until it is laid out and re-measure on resize. (The
    // whole block is inert in non-DOM contexts such as the vm tests.)
    if (typeof document !== 'undefined' && typeof document.getElementById === 'function') {
        function positionBanner() {
            const footer = document.getElementById('playback-bar');
            if (!footer || typeof banner.getBoundingClientRect !== 'function') return false;
            const rect = footer.getBoundingClientRect();
            const viewportH = window.innerHeight || document.documentElement.clientHeight;
            banner.style.bottom = Math.max(10, viewportH - rect.top + 12) + 'px';
            return true;
        }
        if (typeof window.addEventListener === 'function') {
            window.addEventListener('resize', function () { positionBanner(); });
            window.addEventListener('load', function () { positionBanner(); });
        }
        (function pollBanner() {
            if (!positionBanner() && typeof window.setTimeout === 'function') {
                window.setTimeout(pollBanner, 120);
            }
        })();
    }

    // Pre-fetch measurement inputs after a short delay so the UI is ready.
    setTimeout(function () {
        fetch('/api/measurements/inputs').catch(function () {});
    }, 1200);
})();