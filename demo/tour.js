// Demo tour: a short guided walkthrough of the main FXRoute surfaces.
// Pure demo-layer UI (same pattern as boot.js): it navigates the real
// tabs itself, highlights the step target, and runs a few small actions
// so the demo feels alive (a track starts, the DSP preset visibly moves
// the output meter). The real frontend is never modified.
//
// The step data is exposed as window.FXROUTE_DEMO_TOUR before any DOM
// access so the static regression test (scripts/test_demo_tour.js) can
// validate it without a browser. Append ?notour to the URL to disable.
(function () {
    'use strict';

    if (window.FXROUTE_DEMO_TOUR_LOADED) return;
    window.FXROUTE_DEMO_TOUR_LOADED = true;

    var STEPS = [
        {
            id: 'playback',
            tab: null,
            target: '#playback-bar',
            title: 'Wiedergabe & Live-Meter',
            text: 'Der Fußbereich steuert die Wiedergabe: Transport, Titel-Info und das Live-Ausgangs-Meter der DSP-Kette. Ein Titel startet gerade mit — die Segmente bewegen sich wie echtes Audiopegel, inklusive gelegentlicher Peaks nahe 0 dB.',
            action: 'play-track',
        },
        {
            id: 'radio',
            tab: '#tab-btn-radio',
            target: '#tab-radio',
            title: 'Radio',
            text: 'Über 100 Internet-Radiostationen, vom Jazz-Sender bis zur Klassik-Stream. Station im Suchfeld finden, antippen und sofort hören — mit Live-Metadaten (Titel & Interpret) im Now-Playing.',
        },
        {
            id: 'library',
            tab: '#tab-btn-library',
            target: '#tab-library',
            title: 'Musikbibliothek',
            text: 'Der lokale Katalog — hier „NAS Library 1" — mit Alben, Titeln und Favoriten. Beim Wechsel oder Refresh läuft ein kurzer Scan mit hochlaufenden Zählern, genau wie auf dem echten Gerät.',
        },
        {
            id: 'dsp',
            tab: '#tab-btn-effects',
            target: '#tab-effects',
            title: 'DSP & Klangbearbeitung',
            text: 'Zehn Presets von Direct bis zu den Convolver-Kernels, dazu PEQ, Limiter, Loudness und mehr. Gerade wird der Preset „+6" geladen — beobachte, wie das Ausgangs-Meter im Fußbereich ansteigt und der Limiter greift.',
            action: 'preset-plus6',
        },
        {
            id: 'qobuz',
            tab: '#tab-btn-qobuz',
            target: '#tab-qobuz',
            title: 'Streaming-Provider',
            text: 'Spotify, Qobuz und TIDAL als echte Remote-Transporte: eigene Kataloge mit Cover-Artwork, Wiedergabe und Qualitäts-Facts — Qobuz streamt zum Beispiel FLAC bis 24 bit / 96 kHz.',
        },
        {
            id: 'settings',
            tab: null,
            target: '#settings-panel',
            title: 'Technische Einstellungen',
            text: 'Hier verwaltet fxroute die Musikbibliotheken (Lokal, NAS Library 1 & 2 — Wechsel per Dropdown), die Streaming-Provider, Messung, Subwoofer und mehr. Alles ist simuliert, aber der komplette Funktionsumfang ist bedienbar.',
            action: 'open-settings',
        },
        {
            id: 'summary',
            tab: null,
            target: null,
            title: 'Das kann fxroute',
            text: 'fxroute bündelt simulierte Wiedergabe mit Live-Meter, DSP-Kette, Radio, Streaming-Provider, Messung & Raumkorrektur — alles in einer Web-Demo. Viel Spaß beim Ausprobieren!',
        },
    ];

    window.FXROUTE_DEMO_TOUR = { steps: STEPS };

    // DOM wiring runs only in the browser; vm test contexts (and the
    // ?notour opt-out) leave the module inert.
    if (typeof document === 'undefined' || typeof window.location === 'undefined') return;
    if (/(?:^|[?&])notour(?:&|$)/.test(window.location.search || '')) return;

    var startLink = document.getElementById('demo-tour-start');
    if (!startLink) return;

    var currentIndex = -1;
    var backdrop = null;
    var card = null;
    var highlight = null;

    function el(tag, className, text) {
        var node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== undefined && text !== null) node.textContent = text;
        return node;
    }

    function buildOverlay() {
        backdrop = el('div', 'demo-tour-backdrop');
        card = el('div', 'demo-tour-card');
        card.setAttribute('role', 'dialog');
        card.setAttribute('aria-modal', 'true');
        card.setAttribute('aria-labelledby', 'demo-tour-title');
        backdrop.appendChild(card);
        document.body.appendChild(backdrop);
    }

    function renderStep(index) {
        var step = STEPS[index];
        currentIndex = index;

        // Clean up the previous highlight.
        if (highlight) {
            highlight.classList.remove('demo-tour-target');
            highlight = null;
        }
        if (step.tab) {
            var tabBtn = document.querySelector(step.tab);
            if (tabBtn && typeof tabBtn.click === 'function') tabBtn.click();
        }
        if (step.action) runAction(step.action);

        var target = step.target ? document.querySelector(step.target) : null;
        if (target) {
            target.classList.add('demo-tour-target');
            highlight = target;
            if (typeof target.scrollIntoView === 'function') {
                target.scrollIntoView({ block: 'center', behavior: 'smooth' });
            }
        }

        card.textContent = '';
        card.appendChild(el('div', 'demo-tour-progress', 'Schritt ' + (index + 1) + ' / ' + STEPS.length));
        card.appendChild(el('h3', 'demo-tour-title', step.title));
        card.appendChild(el('p', 'demo-tour-text', step.text));

        var actions = el('div', 'demo-tour-actions');
        if (index > 0) {
            actions.appendChild(button('Zurück', 'demo-tour-btn demo-tour-back', function () {
                renderStep(index - 1);
            }));
        }
        actions.appendChild(button('Überspringen', 'demo-tour-btn demo-tour-skip', finish));
        var nextLabel = index === STEPS.length - 1 ? 'Fertig' : 'Weiter';
        actions.appendChild(button(nextLabel, 'demo-tour-btn demo-tour-next', function () {
            if (index + 1 < STEPS.length) renderStep(index + 1);
            else finish();
        }));
        card.appendChild(actions);

        var dots = el('div', 'demo-tour-dots');
        STEPS.forEach(function (_, i) {
            dots.appendChild(el('span', 'demo-tour-dot' + (i === index ? ' is-active' : '')));
        });
        card.appendChild(dots);

        // Let the tab switch / action render, then anchor the card.
        setTimeout(function () { positionCard(target); }, 80);
        var nextBtn = card.querySelector('.demo-tour-next');
        if (nextBtn) nextBtn.focus();
    }

    function button(label, className, onClick) {
        var btn = el('button', className, label);
        btn.type = 'button';
        btn.addEventListener('click', onClick);
        return btn;
    }

    function positionCard(target) {
        if (!card) return;
        var pad = 14;
        var vw = document.documentElement.clientWidth;
        var vh = document.documentElement.clientHeight;
        var cardW = card.offsetWidth;
        var cardH = card.offsetHeight;
        if (!target) {
            card.style.left = Math.round((vw - cardW) / 2) + 'px';
            card.style.top = Math.round((vh - cardH) / 2) + 'px';
            return;
        }
        var r = target.getBoundingClientRect();
        var left = Math.max(pad, Math.min(vw - cardW - pad, r.left + (r.width - cardW) / 2));
        var top;
        if (r.top > cardH + pad + 60) {
            top = r.top - cardH - pad;      // above the target
        } else {
            top = Math.min(vh - cardH - pad, r.bottom + pad);   // below
        }
        card.style.left = Math.round(Math.max(pad, left)) + 'px';
        card.style.top = Math.round(Math.max(pad, top)) + 'px';
    }

    function runAction(action) {
        var S = window.FXROUTE_DEMO_STATE;
        if (action === 'play-track') {
            // Only start if nothing is playing yet, so an already live demo
            // is not disturbed; the meter animates either way.
            if (S && typeof S.getPlayback === 'function' && !S.getPlayback().playing
                && typeof S.playLocal === 'function' && S.localTracks && S.localTracks.length) {
                S.playLocal(S.localTracks[0].id);
            }
        } else if (action === 'preset-plus6') {
            // Moves the audible level so the meter visibly jumps.
            post('/api/dsp/presets/load', { preset_name: '+6' });
        } else if (action === 'open-settings') {
            var panel = document.querySelector('#settings-panel');
            if (panel && panel.classList.contains('hidden')) {
                var openBtn = document.querySelector('#open-settings');
                if (openBtn) openBtn.click();
            }
        }
    }

    function post(url, body) {
        if (typeof fetch !== 'function') return;
        fetch(url, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body || {}),
        }).catch(function () {});
    }

    function finish() {
        // Leave the demo in a neutral state: restore the Direct preset and
        // close the settings dialog if the tour opened it.
        post('/api/dsp/presets/load', { preset_name: 'Direct' });
        var panel = document.querySelector('#settings-panel');
        if (panel && !panel.classList.contains('hidden')) {
            var closeBtn = document.querySelector('#close-settings');
            if (closeBtn) closeBtn.click();
        }
        if (highlight) {
            highlight.classList.remove('demo-tour-target');
            highlight = null;
        }
        if (backdrop && backdrop.parentNode) backdrop.parentNode.removeChild(backdrop);
        backdrop = null;
        card = null;
        currentIndex = -1;
        if (startLink && typeof startLink.focus === 'function') startLink.focus();
    }

    function onKeydown(event) {
        if (!backdrop) return;
        if (event.key === 'Escape') {
            event.preventDefault();
            finish();
        } else if (event.key === 'ArrowRight' && currentIndex >= 0) {
            event.preventDefault();
            if (currentIndex + 1 < STEPS.length) renderStep(currentIndex + 1);
            else finish();
        } else if (event.key === 'ArrowLeft' && currentIndex > 0) {
            event.preventDefault();
            renderStep(currentIndex - 1);
        }
    }

    startLink.addEventListener('click', function (event) {
        event.preventDefault();
        if (backdrop) return;   // already running
        buildOverlay();
        renderStep(0);
        document.addEventListener('keydown', onKeydown);
    });
})();