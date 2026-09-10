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
            title: 'Playback & live meter',
            text: 'The footer controls playback: transport, track info and the live output meter of the DSP chain.',
            action: 'play-track',
        },
        {
            id: 'radio',
            tab: '#tab-btn-radio',
            target: '#tab-radio',
            title: 'Radio',
            text: '60+ internet radio stations. Find a station in the search box, tap it and playback starts right away.',
        },
        {
            id: 'library',
            tab: '#tab-btn-library',
            target: '#tab-library',
            title: 'Music library',
            text: 'Browse albums, tracks and favorites — search the catalog and mark favorites. A refresh runs a short scan with progress counters.',
        },
        {
            id: 'dsp',
            tab: '#tab-btn-effects',
            target: '#tab-effects',
            title: 'DSP & audio processing',
            text: 'Filter presets, PEQ, limiter, loudness and more. The “+6” preset is loading right now — watch the output meter climb and the limiter engage.',
            action: 'preset-plus6',
        },
        {
            id: 'qobuz',
            tab: '#tab-btn-qobuz',
            target: '#tab-qobuz',
            title: 'Streaming providers',
            text: 'Spotify, Qobuz and TIDAL with their own catalogs, cover artwork and quality facts — Qobuz tracks show FLAC up to 24 bit / 96 kHz.',
        },
        {
            id: 'settings',
            tab: null,
            target: '#settings-panel',
            title: 'Technical settings',
            text: 'This is where fxroute manages the music libraries (Local, NAS Library 1 & 2 — switch via the dropdown), streaming providers, measurement, subwoofer and more. Everything is simulated, but the full feature set is usable.',
            action: 'open-settings',
        },
        {
            id: 'summary',
            tab: null,
            target: null,
            title: 'What fxroute can do',
            text: 'fxroute bundles simulated playback with a live meter, DSP chain, radio, streaming providers, measurement & room correction — all in a web demo.',
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
    var savedTargetStyle = null;

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
        // The card is a sibling of the backdrop, not a child: the backdrop's
        // z-index would otherwise cap the whole subtree, letting a huge
        // highlighted panel (z 1200) paint over the card and swallow every
        // button click (seen on the library step).
        document.body.appendChild(backdrop);
        document.body.appendChild(card);
    }

    // The frontend's overlay manager marks every body-level sibling of an
    // opened dialog as inert (focus trap). That sweeps up the tour overlay
    // whenever a step opens a real dialog (settings), killing all clicks.
    // The tour is not part of the app's background, so re-assert it after
    // every step render (and once more after the 80 ms layout timer, when
    // the dialog's own open handler has definitely run).
    function ensureInteractive() {
        if (backdrop) backdrop.inert = false;
        if (card) card.inert = false;
    }

    function renderStep(index) {
        var step = STEPS[index];
        currentIndex = index;

        // Clean up the previous highlight.
        unhighlight();
        if (step.tab) {
            var tabBtn = document.querySelector(step.tab);
            if (tabBtn && typeof tabBtn.click === 'function') tabBtn.click();
        }
        if (step.action) runAction(step.action);

        var target = step.target ? document.querySelector(step.target) : null;
        if (target) {
            highlightTarget(target);
            if (typeof target.scrollIntoView === 'function') {
                target.scrollIntoView({ block: 'center', behavior: 'smooth' });
            }
        }

        card.textContent = '';
        card.appendChild(el('div', 'demo-tour-progress', 'Step ' + (index + 1) + ' / ' + STEPS.length));
        var titleEl = el('h3', 'demo-tour-title', step.title);
        titleEl.id = 'demo-tour-title'; // aria-labelledby on the card references this id
        card.appendChild(titleEl);
        card.appendChild(el('p', 'demo-tour-text', step.text));

        var actions = el('div', 'demo-tour-actions');
        if (index > 0) {
            actions.appendChild(button('Back', 'demo-tour-btn demo-tour-back', function () {
                renderStep(index - 1);
            }));
        }
        actions.appendChild(button('Skip', 'demo-tour-btn demo-tour-skip', finish));
        var nextLabel = index === STEPS.length - 1 ? 'Done' : 'Next';
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

        ensureInteractive();

        // Let the tab switch / action render, then anchor the card.
        setTimeout(function () {
            ensureInteractive();
            positionCard(target);
        }, 80);
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
        // Small screens: keep the controls clear of the home-indicator
        // safe area (matches the demo.css compact-card breakpoint).
        var padBottom = (vw <= 480 || vh <= 500) ? 30 : pad;
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
            top = Math.min(vh - cardH - padBottom, r.bottom + pad);   // below
        }
        card.style.left = Math.round(Math.max(pad, left)) + 'px';
        card.style.top = Math.round(Math.max(pad, top)) + 'px';
    }

    // The highlight must never change the element's layout: raise only the
    // stacking order, and only position elements that are not positioned
    // already (the playback footer is position: fixed and must stay so).
    function highlightTarget(target) {
        savedTargetStyle = {
            position: target.style.position,
            zIndex: target.style.zIndex,
        };
        var computed = (typeof window.getComputedStyle === 'function')
            ? window.getComputedStyle(target) : null;
        var pos = computed ? computed.position : '';
        if (!pos || pos === 'static') target.style.position = 'relative';
        target.style.zIndex = '1200';
        target.classList.add('demo-tour-target');
        highlight = target;
    }
    function unhighlight() {
        if (!highlight) return;
        highlight.classList.remove('demo-tour-target');
        if (savedTargetStyle) {
            highlight.style.position = savedTargetStyle.position;
            highlight.style.zIndex = savedTargetStyle.zIndex;
            savedTargetStyle = null;
        }
        highlight = null;
    }

    // Demo actions must never be able to break the tour: a failing action
    // (e.g. a broadcast error while starting a track) is swallowed so the
    // step still renders.
    function runAction(action) {
        try {
            var S = window.FXROUTE_DEMO_STATE;
            if (action === 'play-track') {
                // Only start if nothing is playing yet, so an already live
                // demo is not disturbed; the meter animates either way.
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
        } catch (err) {
            // Swallow: the tour itself must stay usable.
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
        unhighlight();
        if (backdrop && backdrop.parentNode) backdrop.parentNode.removeChild(backdrop);
        if (card && card.parentNode) card.parentNode.removeChild(card);
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