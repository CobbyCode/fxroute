// SPDX-License-Identifier: AGPL-3.0-only
/**
 * FXRoute crossover/speaker tile UI.
 * Canonical owner of the tile fetch/render, the way-mutation capture and the
 * tile control listeners. Output mutations run through the app's
 * applyOutputSystemMutation via callback; output-state ownership
 * (ensureOutputSystemBoxes) and bank/routing/measurement logic stay in
 * app.js. Browser-loadable UMD, no build step; Node-testable via require().
 */
(function (root, factory) {
    const api = factory(root);
    root.FXRouteCrossoverUI = api;
    if (typeof module === 'object' && module.exports) module.exports = api;
})(typeof globalThis !== 'undefined' ? globalThis : window, function (root) {
    'use strict';

    let deps = {
        getState: () => ({ crossover: { activeWay: null, response: null, busy: false, linkLR: false } }),
        getElements: () => ({}),
        showToast: () => {},
        ensureOutputBoxes: () => {},
        applyMutation: async () => null,
    };
    // Live cutoff drag on the response graph. The dragged kind stays recorded
    // until its save settles, so neither the frequency field nor the painted
    // curve snaps back to the stored value while the drag is still on screen.
    let _graphDrag = null;

    function init(overrides) {
        deps = Object.assign(deps, overrides || {});
        // A fresh tile never inherits a cutoff drag from a previous one.
        _graphDrag = null;
    }

    async function fetchCrossoverResponse() {
        deps.ensureOutputBoxes();
        const catalog = deps.getState().outputSystem.catalog;
        if (!catalog?.modes?.[catalog.active_mode]?.crossover_enabled) {
            deps.getState().crossover.response = null;
            renderCrossoverTile();
            return null;
        }
        try {
            const resp = await fetch('/api/audio/output-state/crossover-response');
            const data = await resp.json().catch(() => ({}));
            if (!resp.ok) throw new Error('Crossover response unavailable');
            deps.getState().crossover.response = data;
        } catch (e) {
            deps.getState().crossover.response = null;
        }
        renderCrossoverTile();
        return deps.getState().crossover.response;
    }

    function formatTrimValue(value) {
        return Number(value).toFixed(2);
    }

    function showTrimValue(input, value) {
        input.value = formatTrimValue(value);
        input._fxroutePreciseValue = Number(value);
        input._fxroutePreciseDisplay = input.value;
    }

    // Live cutoff drag on the response graph, painted through the view module.
    function crossoverGraphView() {
        return (root && root.FXRouteCrossoverView) || null;
    }

    function crossoverGraphPreview() {
        return _graphDrag ? { kind: _graphDrag.kind, frequency_hz: _graphDrag.frequency_hz } : null;
    }

    function crossoverFrequencyElement(kind) {
        return kind === 'highpass'
            ? deps.getElements().effectsCrossoverFrequencyHighpass
            : deps.getElements().effectsCrossoverFrequencyLowpass;
    }

    // The cutoff lines of the active way a graph drag may move: an applicable
    // stored filter that is not Off, never while a save is running and never
    // the sub-owned derived high-pass (the Subwoofer tile owns that one).
    function editableGraphKinds() {
        const state = deps.getState();
        const mod = (root && root.FXRouteCrossover) || null;
        const catalog = state.outputSystem && state.outputSystem.catalog;
        const active = state.crossover && state.crossover.activeWay;
        if (!mod || !catalog || !active) return [];
        if (state.crossover.busy || state.outputSystem.busy) return [];
        const modeConfig = catalog.modes[catalog.active_mode] || {};
        const settings = modeConfig.processing?.[active] || {};
        if (!settings) return [];
        const bass = modeConfig.bass_management || {};
        const subRoles = modeConfig.topology?.sub_roles || [];
        const applicable = mod.applicableFilters(active, { bass, subRoles });
        const derived = !settings.highpass && mod.derivedHighpassForRole
            ? mod.derivedHighpassForRole(active, bass, subRoles) : null;
        const kinds = [];
        if (applicable.includes('highpass') && !derived && settings.highpass) kinds.push('highpass');
        if (applicable.includes('lowpass') && settings.lowpass) kinds.push('lowpass');
        return kinds;
    }

    // The editable cutoff line under a pointer x, or null: a derived (sub HPF)
    // line, an Off direction or empty plot space yields no handle.
    function graphHandleAt(clientX) {
        const view = crossoverGraphView();
        const canvas = deps.getElements().effectsCrossoverGraph;
        if (!view || !canvas || typeof clientX !== 'number') return null;
        const state = deps.getState();
        const response = state.crossover && state.crossover.response;
        if (!response || !state.crossover.activeWay) return null;
        return view.crossoverGraphHandleAt(response.ways, state.crossover.activeWay, clientX, {
            canvas,
            canEdit: (kind) => editableGraphKinds().includes(kind),
        });
    }

    function renderCrossoverTile() {
        deps.ensureOutputBoxes();
        const mod = (root && root.FXRouteCrossover) || null;
        const catalog = deps.getState().outputSystem.catalog;
        const card = deps.getElements().effectsCrossoverCard;
        if (!card) return;
        const response = deps.getState().crossover.response;
        const roles = mod && response && response.crossover_enabled
            ? mod.orderedWays(response.ways) : [];
        card.classList.toggle('hidden', roles.length === 0);
        if (!roles.length) return;
        if (!roles.includes(deps.getState().crossover.activeWay)) deps.getState().crossover.activeWay = roles[0];
        const active = deps.getState().crossover.activeWay;
        const modeConfig = catalog.modes[catalog.active_mode] || {};
        const processing = modeConfig.processing || {};
        const settings = processing[active] || {};
        const busy = deps.getState().crossover.busy || deps.getState().outputSystem.busy;
        const bass = modeConfig.bass_management || {};
        const subRoles = modeConfig.topology?.sub_roles || [];
        const bassContext = { bass, subRoles };
        // Derived sub high-pass for the Low way: owned by the Subwoofer tile,
        // displayed read-only here. Falls back to the response payload when the
        // catalog is momentarily stale after a sub save. The response carries the
        // sub crossover for every speaker way (the plan runs each way through it),
        // but only the Low way replaces its own high-pass with it, so the fallback
        // stays Low-only: otherwise a mid/high way whose stored high-pass is Off
        // would render as if it still had one (locked Type, visible rows, no
        // "High-pass off" in the header).
        const responseDerived = mod.isLowWayRole && mod.isLowWayRole(active)
            ? (response.ways?.[active]?.derived_highpass || null) : null;
        const catalogDerived = mod.derivedHighpassForRole
            ? mod.derivedHighpassForRole(active, bass, subRoles) : null;
        const derivedHighpass = (!settings.highpass && (catalogDerived || responseDerived)) || null;
        // Linked pairs share one tab per way ("L/R · Low"); unlinked pairs go
        // back to separate L/R tabs. The checkbox keeps its place regardless.
        // Link is off by default and couples only the crossover filters; Trim
        // (Level/Align/Polarity) stays per-way and is hidden while linked.
        const linkedCrossover = deps.getState().crossover.linkLR === true;
        mod.renderWayTabs(deps.getElements().effectsCrossoverTabs, roles, active, (role) => {
            // A linked pair shares one tab carrying the canonical left role:
            // re-picking the visible pair keeps the current side, otherwise the
            // tab's role wins. Filters still mirror; trim stays per-way.
            const mate = mod.mirrorRole ? mod.mirrorRole(deps.getState().crossover.activeWay) : null;
            deps.getState().crossover.activeWay = (linkedCrossover && mate === role) ? deps.getState().crossover.activeWay : role;
            renderCrossoverTile();
        }, linkedCrossover);
        if (deps.getElements().effectsCrossoverGraph) {
            const view = (root && root.FXRouteCrossoverView) || null;
            if (view) view.drawCrossoverResponse(deps.getElements().effectsCrossoverGraph, response.ways, active,
                crossoverGraphPreview());
        }
        const applicable = mod.applicableFilters(active, bassContext);
        const showHighpass = applicable.includes('highpass');
        const showLowpass = applicable.includes('lowpass');
        if (deps.getElements().effectsCrossoverHighpassGroup) {
            deps.getElements().effectsCrossoverHighpassGroup.style.display = showHighpass ? '' : 'none';
        }
        if (deps.getElements().effectsCrossoverLowpassGroup) {
            deps.getElements().effectsCrossoverLowpassGroup.style.display = showLowpass ? '' : 'none';
        }
        if (deps.getElements().effectsCrossoverTrimGroup) {
            deps.getElements().effectsCrossoverTrimGroup.style.display = linkedCrossover ? 'none' : '';
        }
        // A cutoff line being dragged owns its frequency field: the dragged
        // value wins over the stored one (and over focus, which a canvas
        // pointer never holds) until the save settles.
        const preview = crossoverGraphPreview();
        const activeElement = (typeof document !== 'undefined' && document.activeElement) || null;
        const draggedValue = (kind) => (preview && preview.kind === kind
            ? String(preview.frequency_hz) : null);
        const draggedHighpass = draggedValue('highpass');
        const draggedLowpass = draggedValue('lowpass');
        if (deps.getElements().effectsCrossoverFrequencyHighpass
            && (draggedHighpass !== null || activeElement !== deps.getElements().effectsCrossoverFrequencyHighpass)) {
            deps.getElements().effectsCrossoverFrequencyHighpass.value = draggedHighpass
                ?? (settings.highpass?.frequency_hz ?? derivedHighpass?.frequency_hz ?? '');
            deps.getElements().effectsCrossoverFrequencyHighpass.disabled = !showHighpass || busy || !!derivedHighpass;
            deps.getElements().effectsCrossoverFrequencyHighpass.title = derivedHighpass
                ? `Set by the Subwoofer tile (${derivedHighpass.frequency_hz} Hz)` : '';
        }
        if (deps.getElements().effectsCrossoverFrequencyLowpass
            && (draggedLowpass !== null || activeElement !== deps.getElements().effectsCrossoverFrequencyLowpass)) {
            deps.getElements().effectsCrossoverFrequencyLowpass.value = draggedLowpass
                ?? (settings.lowpass?.frequency_hz ?? '');
            deps.getElements().effectsCrossoverFrequencyLowpass.disabled = !showLowpass || busy;
        }
        const families = ['off', ...Object.keys(catalog.capabilities?.filter_families || {})];
        for (const kind of ['highpass', 'lowpass']) {
            const derived = kind === 'highpass' ? derivedHighpass : null;
            const definition = settings[kind] || derived || {};
            // An unstored filter displays Off; saving it writes nothing. Off is a
            // valid operating state: that direction runs without a filter.
            const displayFamily = definition.family || 'off';
            const displaySlope = definition.slope_db_oct ?? null;
            const familyEl = kind === 'highpass' ? deps.getElements().effectsCrossoverFamilyHighpass : deps.getElements().effectsCrossoverFamilyLowpass;
            const slopeEl = kind === 'highpass' ? deps.getElements().effectsCrossoverSlopeHighpass : deps.getElements().effectsCrossoverSlopeLowpass;
            const kindBusy = busy || !applicable.includes(kind) || !!derived;
            const kindOff = displayFamily === 'off';
            if (familyEl) {
                const html = families.map((family) =>
                    `<option value="${mod.esc(family)}"${family === displayFamily ? ' selected' : ''}>${mod.esc(familyLabel(family))}</option>`).join('');
                if (familyEl.innerHTML !== html) familyEl.innerHTML = html;
                if (familyEl.value !== displayFamily) {
                    familyEl.value = displayFamily;
                }
                familyEl.disabled = kindBusy;
                if (kind === 'highpass') familyEl.title = derived
                    ? `Set by the Subwoofer tile (${derived.frequency_hz} Hz)` : '';
                if (kindOff) {
                    const freqEl = kind === 'highpass'
                        ? deps.getElements().effectsCrossoverFrequencyHighpass : deps.getElements().effectsCrossoverFrequencyLowpass;
                    if (freqEl) freqEl.disabled = true;
                }
            }
            if (slopeEl) {
                const slopes = kindOff ? [] : mod.slopesForFamily(familyEl?.value || displayFamily, catalog.capabilities);
                const html = slopes.map((slope) =>
                    `<option value="${slope}"${slope === displaySlope ? ' selected' : ''}>${slope}</option>`).join('');
                if (slopeEl.innerHTML !== html) slopeEl.innerHTML = html;
                if (displaySlope === null) {
                    slopeEl.value = '';
                } else if (String(slopeEl.value) !== String(displaySlope)) {
                    slopeEl.value = displaySlope;
                }
                slopeEl.disabled = kindBusy || kindOff;
                if (kind === 'highpass') slopeEl.title = derived
                    ? `Set by the Subwoofer tile (${derived.frequency_hz} Hz)` : '';
            }
            // Off disables the filter fully: hide its Frequency and Slope rows
            // so only Type stays visible. HPF and LPF switch independently.
            const freqGroup = kind === 'highpass'
                ? deps.getElements().effectsCrossoverFrequencyHighpassGroup : deps.getElements().effectsCrossoverFrequencyLowpassGroup;
            const slopeGroup = kind === 'highpass'
                ? deps.getElements().effectsCrossoverSlopeHighpassGroup : deps.getElements().effectsCrossoverSlopeLowpassGroup;
            if (freqGroup) freqGroup.style.display = kindOff ? 'none' : '';
            if (slopeGroup) slopeGroup.style.display = kindOff ? 'none' : '';
        }
        if (deps.getElements().effectsCrossoverLevel && ((typeof document !== 'undefined' && document.activeElement) || null) !== deps.getElements().effectsCrossoverLevel) {
            showTrimValue(deps.getElements().effectsCrossoverLevel, settings.level_db ?? 0);
            deps.getElements().effectsCrossoverLevel.disabled = busy;
        }
        if (deps.getElements().effectsCrossoverDelay && ((typeof document !== 'undefined' && document.activeElement) || null) !== deps.getElements().effectsCrossoverDelay) {
            showTrimValue(deps.getElements().effectsCrossoverDelay, settings.alignment_ms ?? 0);
            deps.getElements().effectsCrossoverDelay.disabled = busy;
        }
        if (deps.getElements().effectsCrossoverPolarity) {
            deps.getElements().effectsCrossoverPolarity.value = settings.polarity || 'normal';
            deps.getElements().effectsCrossoverPolarity.disabled = busy;
        }
        const wayCount = modeConfig.topology?.way_count || 0;
        // The bass high-pass is per side for an unlinked stereo sub pair, so the
        // active way decides which side's crossover the header shows.
        const summaryBass = mod.bassHighpass ? mod.bassHighpass(bass, subRoles, active) : null;
        if (deps.getElements().effectsCrossoverSummary) {
            const base = wayCount
                ? `${wayCount}-Way Stereo System`
                : 'Configure speaker ways in Output Routing first.';
            const parts = [base];
            if (summaryBass && wayCount) parts.push(`Sub HPF ${summaryBass.frequency_hz} Hz`);
            // A cleared direction is a valid Off state; name it here instead of
            // leaving only an empty Type select as its trace.
            const cleared = applicable
                .filter((kind) => !settings[kind] && !(kind === 'highpass' && derivedHighpass))
                .map((kind) => (kind === 'highpass' ? 'High-pass off' : 'Low-pass off'));
            parts.push(...cleared);
            renderSummary(deps.getElements().effectsCrossoverSummary, parts);
        }
        if (deps.getElements().effectsCrossoverLink) {
            deps.getElements().effectsCrossoverLink.checked = deps.getState().crossover.linkLR === true;
            deps.getElements().effectsCrossoverLink.disabled = busy;
        }
    }

    // Card summary line: a narrow card wraps between the " · " segments, never
    // inside one ("Sub HPF 80 Hz"). textContent stays the plain joined line; a
    // single part (a sentence) wraps normally.
    function renderSummary(el, parts) {
        if (!el) return;
        const text = parts.join(' · ');
        if (parts.length < 2 || typeof document === 'undefined' || typeof el.replaceChildren !== 'function') {
            el.textContent = text;
            return;
        }
        if (el.textContent === text && el.querySelector('.effects-summary-part')) return;
        const nodes = [];
        parts.forEach((part, index) => {
            if (index) nodes.push(document.createTextNode(' · '));
            const segment = document.createElement('span');
            segment.className = 'effects-summary-part';
            segment.textContent = part;
            nodes.push(segment);
        });
        el.replaceChildren(...nodes);
    }

    // Phone widths show the short family names the card summaries already use
    // (LR24, BW12); desktop keeps the full names. Same breakpoint as the phone
    // property-list layout in _responsive.css.
    const COMPACT_LABEL_QUERY = '(max-width: 520px)';
    const FAMILY_LABELS = { off: 'Off', 'linkwitz-riley': 'Linkwitz-Riley', butterworth: 'Butterworth', bessel: 'Bessel' };
    const COMPACT_FAMILY_LABELS = { ...FAMILY_LABELS, 'linkwitz-riley': 'LR', butterworth: 'BW' };
    let compactLabelQuery;

    function compactLabelMedia() {
        if (compactLabelQuery === undefined) {
            compactLabelQuery = root && typeof root.matchMedia === 'function'
                ? root.matchMedia(COMPACT_LABEL_QUERY) : null;
        }
        return compactLabelQuery;
    }

    function familyLabel(family) {
        const labels = compactLabelMedia()?.matches ? COMPACT_FAMILY_LABELS : FAMILY_LABELS;
        return labels[family] || family;
    }

    // Both tiles render their Type options through familyLabel, so crossing
    // the breakpoint relabels them in place; values and selection stay as-is.
    function relabelTypeSelects() {
        if (typeof document === 'undefined') return;
        for (const option of document.querySelectorAll('select.effects-crossover-type-select option')) {
            option.textContent = familyLabel(option.value);
        }
    }

    function watchCompactLabels() {
        const query = compactLabelMedia();
        if (!query) return;
        if (query.addEventListener) query.addEventListener('change', relabelTypeSelects);
        else if (query.addListener) query.addListener(relabelTypeSelects);
    }

    function collectCrossoverWayMutation() {
        deps.ensureOutputBoxes();
        const mod = (root && root.FXRouteCrossover) || null;
        const catalog = deps.getState().outputSystem.catalog;
        const active = deps.getState().crossover.activeWay;
        if (!mod || !catalog || !active) return null;
        const modeConfig = catalog.modes[catalog.active_mode] || {};
        const bassContext = { bass: modeConfig.bass_management || {},
            subRoles: modeConfig.topology?.sub_roles || [] };
        const applicable = mod.applicableFilters(active, bassContext);
        const readFreq = (el, fallback) => {
            if (!el || el.value === '' || el.value === null) return fallback;
            return mod.clampFrequencyHz(el.value);
        };
        const current = catalog.modes[catalog.active_mode]?.processing?.[active] || {};
        // The sub-owned Low high-pass is display-only: never persist it as a
        // stored way filter, even though the input shows its frequency.
        const derivedHighpass = !current.highpass && mod.derivedHighpassForRole
            ? mod.derivedHighpassForRole(active, bassContext.bass, bassContext.subRoles) : null;
        const highpassEnabled = applicable.includes('highpass') && !derivedHighpass;
        const wayCount = modeConfig.topology?.way_count || 0;
        const build = (kind, previous, enabled, freqEl, familyEl, slopeEl) => {
            if (!enabled) return previous ?? null;
            // Off clears the filter; the way then runs open in that direction.
            if (familyEl?.value === 'off') return null;
            const raw = freqEl ? freqEl.value : '';
            const family = familyEl?.value || previous?.family || 'linkwitz-riley';
            if ((raw === '' || raw === null) && !previous) {
                // Re-enabling a cleared filter: the frequency field is still
                // empty, so default to the starter frequency instead of dropping
                // the filter again.
                const fallback = (mod.starterFrequency
                    ? mod.starterFrequency(wayCount, active, kind) : null) ?? 1000;
                const slope = Number(slopeEl?.value || 24);
                return { family, slope_db_oct: slope, frequency_hz: mod.clampFrequencyHz(fallback) };
            }
            const slope = Number(slopeEl?.value || previous?.slope_db_oct || 24);
            return { family, slope_db_oct: slope, frequency_hz: readFreq(freqEl, previous?.frequency_hz ?? 1000) };
        };
        // Linked L/R maintains only the shared crossover filters. Trim
        // (Level/Align/Polarity) stays per-way and hidden while linked, so it
        // is omitted from the mutation and the backend keeps stored values.
        const linked = deps.getState().crossover.linkLR === true;
        const mutation = {
            kind: 'set_processing',
            mode: catalog.active_mode,
            role: active,
            highpass: build('highpass', current.highpass, highpassEnabled,
                deps.getElements().effectsCrossoverFrequencyHighpass,
                deps.getElements().effectsCrossoverFamilyHighpass, deps.getElements().effectsCrossoverSlopeHighpass),
            lowpass: build('lowpass', current.lowpass, applicable.includes('lowpass'),
                deps.getElements().effectsCrossoverFrequencyLowpass,
                deps.getElements().effectsCrossoverFamilyLowpass, deps.getElements().effectsCrossoverSlopeLowpass),
        };
        if (!linked) {
            // A displayed rounded value is not an edit: preserve the precise
            // stored trim when saving another control on this way.
            const trimValue = (el, stored) => el?.value === formatTrimValue(stored ?? 0)
                ? stored ?? 0 : el?.value ?? 0;
            mutation.level_db = mod.clampLevelDb(trimValue(deps.getElements().effectsCrossoverLevel, current.level_db));
            mutation.alignment_ms = mod.clampAlignmentMs(trimValue(deps.getElements().effectsCrossoverDelay, current.alignment_ms));
            mutation.polarity = deps.getElements().effectsCrossoverPolarity?.value === 'invert' ? 'invert' : 'normal';
        }
        return mutation;
    }

    async function saveCrossoverWay() {
        const fields = collectCrossoverWayMutation();
        if (!fields) return;
        // The L/R link shares only the crossover filter values with the mirror
        // way. Trim (level/align/polarity) stays per-way physical tuning and is
        // hidden while linked, so a linked save never touches it.
        const mod = (root && root.FXRouteCrossover) || null;
        const catalog = deps.getState().outputSystem.catalog;
        const mirror = mod?.mirrorRole ? mod.mirrorRole(fields.role) : null;
        const mirrorProcessing = catalog?.modes?.[catalog.active_mode]?.processing || {};
        const linked = deps.getState().crossover.linkLR === true && mirror && mirror !== fields.role
            && Object.prototype.hasOwnProperty.call(mirrorProcessing, mirror);
        deps.getState().crossover.busy = true;
        renderCrossoverTile();
        try {
            const { kind, ...rest } = fields;
            await deps.applyMutation(kind, rest, false, { quiet: true });
            if (linked) {
                const { role: _role, level_db: _level, alignment_ms: _align, polarity: _polarity, ...mirrorShared } = rest;
                await deps.applyMutation(kind, { ...mirrorShared, role: mirror }, false, { quiet: true });
            }
            await fetchCrossoverResponse();
        } finally {
            deps.getState().crossover.busy = false;
            renderCrossoverTile();
        }
    }

    async function maybeApplyCrossoverStarters() {
        /* First valid 2/3/4-way configuration: every way still empty, so seed
         * the sensible starter filters instead of asking. Partially configured
         * ways are manual edits and stay untouched. */
        const mod = (root && root.FXRouteCrossover) || null;
        const catalog = deps.getState().outputSystem.catalog;
        if (!mod || !catalog) return false;
        const modeConfig = catalog.modes[catalog.active_mode] || {};
        const wayCount = modeConfig.topology?.way_count || 0;
        if (![2, 3, 4].includes(wayCount)) return false;
        let starters;
        try {
            starters = mod.starterValues(wayCount);
        } catch (e) {
            return false;
        }
        const processing = modeConfig.processing || {};
        const roles = Object.keys(starters);
        const allEmpty = roles.length > 0 && roles.every((role) => {
            const current = processing[role] || {};
            return !current.highpass && !current.lowpass;
        });
        if (!allEmpty) return false;
        deps.getState().crossover.busy = true;
        renderCrossoverTile();
        try {
            for (const role of roles) {
                const wanted = starters[role] || {};
                await deps.applyMutation('set_processing',
                    { mode: catalog.active_mode, role, highpass: wanted.highpass ?? null, lowpass: wanted.lowpass ?? null },
                    false, { quiet: true });
            }
            deps.showToast(`Starter values applied (${wayCount}-way).`, 'success');
            await fetchCrossoverResponse();
        } finally {
            deps.getState().crossover.busy = false;
            renderCrossoverTile();
        }
        return true;
    }

        function wireCrossoverTile() {
            // Tile control listeners (moved verbatim from setupSettingsActions):
            // any value change saves the active way, the L/R link re-renders
            // immediately and only persists through the next save.
            const crossoverControlChanged = (event) => {
                void saveCrossoverWay();
                if (event.target === deps.getElements().effectsCrossoverLevel
                    || event.target === deps.getElements().effectsCrossoverDelay) {
                    // Capture the precise edit before compacting its display.
                    showTrimValue(event.target, event.target.value);
                }
            };
            for (const el of [deps.getElements().effectsCrossoverFrequencyHighpass, deps.getElements().effectsCrossoverFrequencyLowpass,
                deps.getElements().effectsCrossoverFamilyHighpass, deps.getElements().effectsCrossoverSlopeHighpass,
                deps.getElements().effectsCrossoverFamilyLowpass, deps.getElements().effectsCrossoverSlopeLowpass,
                deps.getElements().effectsCrossoverLevel,
                deps.getElements().effectsCrossoverDelay, deps.getElements().effectsCrossoverPolarity]) {
                if (el) el.addEventListener('change', crossoverControlChanged);
            }
            for (const el of [deps.getElements().effectsCrossoverLevel, deps.getElements().effectsCrossoverDelay]) {
                if (el) el.addEventListener('blur', () => { el.value = formatTrimValue(el.value); });
            }
            if (deps.getElements().effectsCrossoverLink) {
                deps.getElements().effectsCrossoverLink.addEventListener('change', (event) => {
                    deps.getState().crossover.linkLR = event.target.checked === true;
                    // The link state shows directly on the tabs (merged vs. split)
                    // and on the Trim group (hidden while linked), so the tile
                    // re-renders immediately instead of waiting for the next save
                    // or fetch.
                    renderCrossoverTile();
                });
            }
            wireCrossoverGraphDrag();
            watchCompactLabels();
        }

    // Cutoff drag: a press anywhere on the plot is ignored, only a press
    // within the handle column of an editable line starts a drag. The dragged
    // frequency lands in the same field the keyboard edits, so the release
    // commits through the ordinary way save (L/R link included).
    function wireCrossoverGraphDrag() {
        const canvas = deps.getElements().effectsCrossoverGraph;
        if (!canvas) return;
        const setDragClass = (name, on) => {
            if (canvas.classList && typeof canvas.classList.toggle === 'function') {
                canvas.classList.toggle(name, on === true);
            }
        };
        // Pointer capture keeps the drag alive outside the canvas, but a
        // browser that refuses it (pointer already gone) must not break the
        // drag: the handlers work without capture, just not beyond the edges.
        const capturePointer = (pointerId) => {
            try { canvas.setPointerCapture?.(pointerId); } catch (e) { /* no capture available */ }
        };
        const releasePointer = (pointerId) => {
            try { canvas.releasePointerCapture?.(pointerId); } catch (e) { /* never captured */ }
        };
        const moveDrag = (event) => {
            if (!_graphDrag || !_graphDrag.dragging || _graphDrag.pointerId !== event.pointerId) return;
            const view = crossoverGraphView();
            if (!view) return;
            const hz = view.crossoverGraphFrequencyAt(canvas, event.clientX);
            if (hz === null) return;
            const field = crossoverFrequencyElement(_graphDrag.kind);
            if (field) field.value = String(hz);
            _graphDrag.frequency_hz = hz;
            renderCrossoverTile();
        };
        canvas.addEventListener('pointerdown', (event) => {
            if (_graphDrag || (typeof event.button === 'number' && event.button !== 0)) return;
            const handle = graphHandleAt(event.clientX);
            if (!handle) return;
            // Keep the press on the canvas: no text selection, no native drag.
            if (typeof event.preventDefault === 'function') event.preventDefault();
            const field = crossoverFrequencyElement(handle.kind);
            if (field) field.value = String(handle.frequency_hz);
            _graphDrag = { kind: handle.kind, frequency_hz: handle.frequency_hz,
                startFrequency_hz: handle.frequency_hz, pointerId: event.pointerId, dragging: true };
            setDragClass('is-handle-drag', true);
            capturePointer(event.pointerId);
            renderCrossoverTile();
        });
        canvas.addEventListener('pointermove', (event) => {
            if (_graphDrag && _graphDrag.dragging) {
                moveDrag(event);
                return;
            }
            // Resize cursor only over a line the user may actually move. A
            // released drag still paints its committed position, but the
            // pointer is free again: hover feedback resumes at once.
            setDragClass('is-handle-hover', !!graphHandleAt(event.clientX));
        });
        canvas.addEventListener('pointerup', (event) => {
            if (!_graphDrag || !_graphDrag.dragging || _graphDrag.pointerId !== event.pointerId) return;
            moveDrag(event);
            const drag = _graphDrag;
            // Freeze the preview: from here on the pointer no longer moves the
            // cutoff, so a stray move cannot rewrite a value the save has
            // already captured.
            drag.dragging = false;
            setDragClass('is-handle-drag', false);
            releasePointer(drag.pointerId);
            if (drag.frequency_hz === drag.startFrequency_hz) {
                // A press without movement is not an edit: leave the stored
                // filter alone instead of saving the same value again.
                _graphDrag = null;
                renderCrossoverTile();
                return;
            }
            // Keep the preview until the save settles, so the field does not
            // flash the old value while the mutation is in flight.
            const pending = saveCrossoverWay();
            const done = () => {
                if (_graphDrag !== drag) return;
                _graphDrag = null;
                renderCrossoverTile();
            };
            void pending.then(done, done);
        });
        canvas.addEventListener('pointercancel', (event) => {
            if (!_graphDrag || !_graphDrag.dragging || _graphDrag.pointerId !== event.pointerId) return;
            // An interrupted drag never reached a save: drop the preview and
            // let the field snap back to the stored frequency. A cancel after
            // the release belongs to the committed save and is left alone.
            _graphDrag = null;
            setDragClass('is-handle-drag', false);
            renderCrossoverTile();
        });
        canvas.addEventListener('pointerleave', () => setDragClass('is-handle-hover', false));
    }

    return {
        init,
        fetchCrossoverResponse,
        renderCrossoverTile,
        familyLabel,
        renderSummary,
        collectCrossoverWayMutation,
        saveCrossoverWay,
        maybeApplyCrossoverStarters,
        wireCrossoverTile,
    };
});
