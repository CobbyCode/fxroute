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
            text: 'The footer controls playback: transport, track info, and the live output meter of the DSP chain — a combo meter with slow VU for level and a fast peak-hold that catches clipping.',
            action: 'play-track',
        },
        {
            id: 'radio',
            tab: '#tab-btn-radio',
            target: '#tab-radio',
            title: 'Radio',
            text: 'One search covers it all — type “Groove Salad” to filter your stations, the curated catalog and the web at the same time, then tap any result to play it instantly.',
            action: 'search-radio-groove',
        },
        {
            id: 'library',
            tab: '#tab-btn-library',
            target: '#tab-library',
            title: 'Music library',
            text: 'Browse albums, tracks and folders — switch views or type “Jazz” to filter the catalog and see several Jazz albums appear. Mark favorites with the heart icon.',
            action: 'search-library-jazz',
        },
        {
            id: 'dsp',
            tab: '#tab-btn-effects',
            target: '#tab-effects',
            // The A/B control sits deep inside the DSP panel; on phones the
            // bottom-pinned tour card would cover it, so highlight the
            // toggle itself (the exact control the step pulses).
            targetMobile: '#effects-compare-toggle',
            title: 'DSP & audio processing',
            text: 'Compare two filter presets with A/B — pick A and B, then switch instantly. Output extras — protection limiter with headroom, autogain, loudness and tone controls — run globally on top of every preset. Loudness is calibrated to your playback level.',
            action: 'dsp-demo',
        },
        {
            id: 'measurement',
            tab: '#tab-btn-effects',
            target: '.measurement-card-controls',
            // On phones the controls card is taller than the viewport; the
            // sweep toggle is the compact entry point the step describes.
            targetMobile: '#measurement-sweep-toggle',
            title: 'Measurement & room correction',
            text: 'Measure your room to correct it: run a single or repeated L/R sweep, combine speaker and room captures in Advanced mode, then save what you want to keep. From a measurement the assistant builds PEQ bands or a full convolution filter — Create Convolver Preset turns it into a correction kernel. SPL Calibration pins the loudness reference to your target level.',
            action: 'measurement-demo',
        },
        {
            id: 'qobuz',
            tab: null,
            target: '.tabs',
            title: 'Streaming providers',
            text: 'Spotify, Qobuz and TIDAL each bring their own catalog and cover artwork — open any tab to browse albums, tracks and playlists, then play directly. The footer shows the active source.',
            action: 'cycle-providers',
        },
        {
            id: 'settings',
            tab: null,
            target: '#settings-panel',
            title: 'Technical settings',
            text: 'This is where fxroute is configured: audio output, providers, source inputs, device name and maintenance. Providers are managed here — install, hide or remove them. The buttons pulse briefly as a demo.',
            action: 'open-settings',
        },
        {
            id: 'summary',
            tab: null,
            target: null,
            title: 'What fxroute can do',
            text: 'fxroute plays from the local library, radio and streaming providers and shapes the sound with a full DSP chain — filter presets, convolver, PEQ, subwoofer integration and output extras — plus advanced measurement and room correction, all from the browser. Have fun exploring!',
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

        // Wide screens can highlight the whole panel; narrow screens switch
        // to a compact target (the primary control) so the highlight and the
        // tour card fit without scrolling the described area away.
        var sel = step.target;
        if (step.targetMobile && document.documentElement.clientWidth <= 640) sel = step.targetMobile;
        var target = sel ? document.querySelector(sel) : null;
        if (target) {
            highlightTarget(target);
            if (typeof target.scrollIntoView === 'function') {
                // Centering a tall panel pushes its described top out of
                // view; align tall targets to their start instead so the
                // controls the step talks about are actually on screen.
                var rect = target.getBoundingClientRect();
                var block = (rect.height > (document.documentElement.clientHeight || 600) * 0.6) ? 'start' : 'center';
                target.scrollIntoView({ block: block, behavior: 'smooth' });
            }
        }

        card.textContent = '';
        var header = el('div', 'demo-tour-header');
        header.appendChild(el('div', 'demo-tour-progress', 'Step ' + (index + 1) + ' / ' + STEPS.length));
        var closeBtn = el('button', 'demo-tour-close', '✕');
        closeBtn.type = 'button';
        closeBtn.setAttribute('aria-label', 'Close tour');
        closeBtn.title = 'Close tour';
        closeBtn.addEventListener('click', finish);
        header.appendChild(closeBtn);
        card.appendChild(header);
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

        // The tab switch / action render first, then the card anchors.
        // The smooth scroll is async, so re-position a few times instead of
        // measuring a mid-animation rect and never correcting it.
        setTimeout(function () {
            ensureInteractive();
            positionCard(target);
        }, 120);
        setTimeout(function () {
            ensureInteractive();
            positionCard(target);
        }, 320);
        setTimeout(function () {
            ensureInteractive();
            positionCard(target);
        }, 560);
        setTimeout(function () {
            ensureInteractive();
            positionCard(target);
        }, 900);
        var nextBtn = card.querySelector('.demo-tour-next');
        if (nextBtn) nextBtn.focus();
    }

    function button(label, className, onClick) {
        var btn = el('button', className, label);
        btn.type = 'button';
        btn.addEventListener('click', onClick);
        return btn;
    }

    // Brief attention pulse used by the demo actions; a no-op on hidden or
    // missing elements so steps never depend on panel visibility. Pulses
    // stay visible long enough to read while the choreography advances.
    function pulse(node, ms) {
        if (!node) return;
        node.classList.add('demo-tour-pulse');
        setTimeout(function () { node.classList.remove('demo-tour-pulse'); }, ms || 1300);
    }

    // Close the Measurement assistant overlay if the tour opened it. The
    // overlay sits above the tabs, so a step that navigates elsewhere must
    // close it first or its backdrop swallows every click.
    function closeMeasurementPanel() {
        var panel = document.getElementById('measurement-panel');
        if (panel && !panel.classList.contains('hidden')) {
            var closeBtn = document.getElementById('measurement-close');
            if (closeBtn) closeBtn.click();
        }
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
        if (r.bottom + pad + cardH <= vh - padBottom) {
            top = r.bottom + pad;                       // below the target
        } else if (r.top - pad - cardH >= pad) {
            // Above the target, clamped so the card itself stays in view
            // (the target can still be scrolled into view below it).
            top = Math.max(pad, Math.min(r.top - pad - cardH, vh - cardH - padBottom));
        } else if (r.top <= pad && r.bottom >= vh - padBottom) {
            // Full-height modal: pin the card to the bottom so the dialog
            // header (close button) stays reachable above it.
            top = vh - cardH - padBottom;
        } else {
            // No clean spot: keep the target's visible mass on the opposite
            // side of the card instead of covering it blindly.
            var vTop = Math.max(r.top, pad);
            var vBottom = Math.min(r.bottom, vh - padBottom);
            var vCenter = vTop < vBottom ? (vTop + vBottom) / 2 : (r.top + r.bottom) / 2;
            top = (vCenter < vh / 2) ? vh - cardH - padBottom : pad;
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
            } else if (action === 'play-radio') {
                // Legacy: switch the footer to a curated station.
                var stationId = S && S.catalogStations && S.catalogStations[0] && S.catalogStations[0].id;
                if (stationId && typeof S.playRadio === 'function') S.playRadio(stationId);
            } else if (action === 'search-radio-groove') {
                var inp = document.getElementById('station-search');
                if (inp) {
                    inp.value = 'Groove Salad';
                    inp.dispatchEvent(new Event('input', { bubbles: true }));
                    inp.dispatchEvent(new Event('search', { bubbles: true }));
                    var clr = document.getElementById('station-search-clear');
                    if (clr) clr.disabled = false;
                    if (typeof inp.focus === 'function') try { inp.focus(); } catch (e) {}
                }
            } else if (action === 'refresh-library') {
                post('/api/library/refresh', {});
            } else if (action === 'search-library-jazz') {
                var libInp = document.getElementById('library-search');
                if (libInp) {
                    var viewAlbums = document.getElementById('library-view-albums');
                    var viewTracks = document.getElementById('library-view-tracks');
                    var viewFolders = document.getElementById('library-view-folders');
                    // Brief view cycle: Tracks -> Folders -> Albums, then filter Jazz.
                    if (viewTracks && typeof viewTracks.click === 'function') viewTracks.click();
                    setTimeout(function () {
                        if (viewFolders && typeof viewFolders.click === 'function') viewFolders.click();
                        setTimeout(function () {
                            if (viewAlbums && typeof viewAlbums.click === 'function') viewAlbums.click();                                    setTimeout(function () {
                                        var curInp = document.getElementById('library-search');
                                        if (!curInp) return;
                                        curInp.value = 'Jazz';
                                        curInp.dispatchEvent(new Event('input', { bubbles: true }));
                                        curInp.dispatchEvent(new Event('search', { bubbles: true }));
                                        var clr2 = document.getElementById('library-search-clear');
                                        if (clr2) clr2.disabled = false;
                                        if (typeof curInp.focus === 'function') try { curInp.focus(); } catch (e2) {}
                                    }, 320);
                                }, 450);
                            }, 420);
                }
            } else if (action === 'play-qobuz') {
                post('/api/streaming/qobuz/demo_start', {});
            } else if (action === 'preset-plus6') {
                post('/api/dsp/presets/load', { preset_name: '+6' });
            } else if (action === 'dsp-demo') {
                post('/api/dsp/presets/load', { preset_name: '+6' });
                post('/api/dsp/compare', { presetA: 'Direct', presetB: '+6', activeSide: 'B' });
                post('/api/dsp/extras', { headroomEnabled: true, headroomGainDb: -3, loudnessEnabled: true, loudnessStrength: 5 });
                // Animate the Compare A/B control so the filter switch is
                // visible: pulse the button, then actually click it to flip
                // the badge (A -> B -> A) like a user would.
                function pulseCompareBtn() {
                    var btn = document.getElementById('effects-compare-toggle');
                    if (!btn) return null;
                    btn.classList.add('demo-tour-pulse');
                    setTimeout(function () { btn.classList.remove('demo-tour-pulse'); }, 1300);
                    return btn;
                }
                setTimeout(function () {
                    var btn = pulseCompareBtn();
                    if (btn && typeof btn.click === 'function') try { btn.click(); } catch (eA) {}
                    else post('/api/dsp/compare', { presetA: 'Direct', presetB: '+6', activeSide: 'A' });
                    setTimeout(function () {
                        var btn2 = pulseCompareBtn();
                        if (btn2 && typeof btn2.click === 'function') try { btn2.click(); } catch (eB) {}
                        else post('/api/dsp/compare', { presetA: 'Direct', presetB: '+6', activeSide: 'B' });
                    }, 950);
                }, 1100);
            } else if (action === 'measurement-demo') {
                var mPanel = document.getElementById('measurement-panel');
                if (mPanel && mPanel.classList.contains('hidden')) {
                    var measureBtn = document.getElementById('effects-measure-open');
                    if (measureBtn && typeof measureBtn.click === 'function') measureBtn.click();
                }
                // Pulse the workflow entries so the step shows what lives
                // here: Start Sweep (single / LR repeat / Advanced), Auto
                // Sub Optimize and SPL Calibration. On wide screens the
                // sweep menu opens to reveal the repeat + advanced choices,
                // then closes again; on narrow screens the tour card sits
                // right below the sweep toggle, so the menu and the lower
                // buttons would be hidden behind it — keep it to the entry.
                var wide = document.documentElement.clientWidth > 640;
                setTimeout(function () {
                    var sweep = document.getElementById('measurement-sweep-toggle');
                    if (sweep) pulse(sweep);
                    if (!wide) return;
                    setTimeout(function () {
                        if (sweep && sweep.getAttribute('aria-expanded') !== 'true'
                            && typeof sweep.click === 'function') sweep.click();
                        setTimeout(function () {
                            var rep = document.getElementById('measurement-repeat-start');
                            var adv = document.getElementById('measurement-hybrid-open');
                            if (rep) pulse(rep);
                            if (adv) pulse(adv);
                            setTimeout(function () {
                                if (sweep && sweep.getAttribute('aria-expanded') === 'true'
                                    && typeof sweep.click === 'function') sweep.click();
                                var sub = document.getElementById('measurement-auto-sub-start');
                                var spl = document.getElementById('measurement-spl-calibration-open');
                                // The auto-sub group only shows in subwoofer
                                // output modes; skip it when hidden.
                                if (sub && sub.offsetParent !== null) pulse(sub);
                                if (spl) pulse(spl);
                            }, 900);
                        }, 800);
                    }, 700);
                }, 400);
            } else if (action === 'cycle-providers') {
                // Never leave the Measurement assistant overlay open: its
                // backdrop would block the provider tab clicks below.
                closeMeasurementPanel();
                var spBtn = document.getElementById('tab-btn-spotify');
                var qbBtn = document.getElementById('tab-btn-qobuz');
                var tdBtn = document.getElementById('tab-btn-tidal');
                // Make hidden provider tabs reachable in the demo even before discovery.
                [spBtn, qbBtn, tdBtn].forEach(function (btn) {
                    if (btn) { btn.hidden = false; btn.style.display = ''; }
                });
                post('/api/spotify/demo_start', {});
                if (spBtn && typeof spBtn.click === 'function') spBtn.click();
                setTimeout(function () {
                    post('/api/streaming/qobuz/demo_start', {});
                    if (qbBtn && typeof qbBtn.click === 'function') qbBtn.click();
                }, 1000);
                setTimeout(function () {
                    if (tdBtn && typeof tdBtn.click === 'function') tdBtn.click();
                }, 1800);
                setTimeout(function () {
                    post('/api/streaming/qobuz/demo_start', {});
                    if (qbBtn && typeof qbBtn.click === 'function') qbBtn.click();
                }, 2400);
            } else if (action === 'open-settings') {
                var panel = document.querySelector('#settings-panel');
                if (panel && panel.classList.contains('hidden')) {
                    var openBtn = document.querySelector('#open-settings');
                    if (openBtn) openBtn.click();
                }
                setTimeout(function () {
                    var rows = document.querySelectorAll('#settings-providers-list .settings-provider-row');
                    rows.forEach(function (row, idx) {
                        setTimeout(function () {
                            row.classList.add('demo-tour-pulse');
                            setTimeout(function () { row.classList.remove('demo-tour-pulse'); }, 1300);
                        }, idx * 320);
                    });
                }, 400);
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
        // close any dialog the tour opened (settings or measurement).
        post('/api/dsp/presets/load', { preset_name: 'Direct' });
        closeMeasurementPanel();
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