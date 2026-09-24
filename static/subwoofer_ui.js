// SPDX-License-Identifier: AGPL-3.0-only
/**
 * FXRoute subwoofer tile UI.
 * Canonical owner of the subwoofer display, controls, preview graph and the
 * debounced save queue including the measurement preflush: a measurement
 * starts only after a pending subwoofer save committed. Output mutations run
 * through the app's applyOutputSystemMutation via callback; the shared edit
 * guard (_activeEditing) stays owned by app.js and is read through an
 * accessor. The crossover response graph stays in the crossover modules.
 * Browser-loadable UMD, no build step; Node-testable via require().
 */
(function (root, factory) {
    const api = factory(root);
    root.FXRouteSubwooferUI = api;
    if (typeof module === 'object' && module.exports) module.exports = api;
})(typeof globalThis !== 'undefined' ? globalThis : window, function (root) {
    'use strict';

    let deps = {
        getState: () => ({ outputSystem: { catalog: null } }),
        getElements: () => ({}),
        getActiveEditing: () => new Set(),
        applyMutation: async () => null,
    };

    function init(overrides) {
        deps = Object.assign(deps, overrides || {});
    }

    // Type label owned by the crossover tile module; this tile only displays it.
    function crossoverUiLabel(family) {
        const ui = (root && root.FXRouteCrossoverUI) || null;
        return ui && typeof ui.familyLabel === 'function' ? ui.familyLabel(family) : family;
    }

    function outputSystem() {
        return (root && root.FXRouteOutputState) || null;
    }

    // Sub crossover families, mirroring the backend FILTER_SLOPES keys. The
    // authoritative slope lists come from catalog.capabilities.filter_families;
    // this table only guards drafts before the catalog is available.
    const SUB_CROSSOVER_FAMILIES = ['linkwitz-riley', 'butterworth', 'bessel'];

    let _subwooferPreviewDrawFrame = null;
    let _subwooferSelectedSide = 'left';
    let _subwooferSaveTimer = null;
    let _subwooferSavePromise = null;
    let _subwooferPendingSave = null;
    let _subwooferLastRequestedSignature = '';
    const SUBWOOFER_COMMIT_DEBOUNCE_MS = 600;

    function defaultSubCrossoverSlope(family) {
        return family === 'linkwitz-riley' ? 24 : 12;
    }

    function clampSubCrossoverFrequency(value) {
        return Math.max(40, Math.min(200, Math.round(Number(value) || 80)));
    }

    function normalizeSubCrossoverShape(input = {}, fallback = {}) {
        const family = SUB_CROSSOVER_FAMILIES.includes(String(input.family))
            ? String(input.family) : (fallback.family || 'linkwitz-riley');
        const rawSlope = Math.round(Number(input.slope_db_oct));
        const slope = Number.isFinite(rawSlope) && rawSlope >= 6 && rawSlope <= 72 && rawSlope % 6 === 0
            ? rawSlope : (Number(fallback.slope_db_oct) || defaultSubCrossoverSlope(family));
        return { family, slope_db_oct: slope,
            frequency_hz: clampSubCrossoverFrequency(input.frequency_hz ?? fallback.frequency_hz) };
    }

    function subCrossoverSettings(view, fallback = {}) {
        const source = view?.crossover || {};
        const shared = normalizeSubCrossoverShape(source, { family: fallback.family,
            slope_db_oct: fallback.slope_db_oct, frequency_hz: fallback.frequency_hz });
        const side = (key) => normalizeSubCrossoverShape(source[key], shared);
        return { ...shared, link: source.link !== false, stereo: !!source.stereo,
            left: side('left'), right: side('right') };
    }

    function normalizeSubwooferSettings(input = {}) {
        const frequency = Math.max(40, Math.min(200, Math.round(Number(input.crossover_frequency_hz ?? input.crossoverFrequencyHz ?? 80) || 80)));
        const shape = normalizeSubCrossoverShape({
            family: input.family ?? input.crossover_family,
            slope_db_oct: input.slope_db_oct ?? input.crossover_slope_db_oct,
            frequency_hz: frequency });
        // -80 dB floor: 2.2 AutoSub mutes inactive subs to -80 (backend and DSP
        // runtime accept -80..12 for 2.2; 2.1 persists clamp to -24 server-side,
        // so a wider frontend clamp converges on save instead of silently
        // raising a muted sub to -24 on display and re-save.
        const level = Math.max(-80, Math.min(12, Number(input.sub_level_db ?? input.subLevelDb ?? 0) || 0));
        const alignment = Math.max(-40, Math.min(40, Number(input.sub_alignment_ms ?? input.subAlignmentMs ?? 0) || 0));
        const polarity = String(input.sub_polarity ?? input.subPolarity ?? 'normal').toLowerCase() === 'invert' ? 'invert' : 'normal';
        const roundedAlignment = Math.round(alignment * 100) / 100;
        return {
            crossover_frequency_hz: frequency,
            slope: `${familyPrefix(shape.family)}${shape.slope_db_oct}`,
            family: shape.family,
            slope_db_oct: shape.slope_db_oct,
            main_highpass_enabled: input.main_highpass_enabled ?? input.mainHighpassEnabled ?? true ? true : false,
            sub_level_db: Math.round(level * 10) / 10,
            sub_alignment_ms: roundedAlignment,
            sub_polarity: polarity,
            sub_link: input.sub_link !== false,
            left_crossover: normalizeSubCrossoverShape(input.left_crossover, shape),
            right_crossover: normalizeSubCrossoverShape(input.right_crossover, shape),
        };
    }

    function familyPrefix(family) {
        return { 'linkwitz-riley': 'LR', butterworth: 'BW', bessel: 'BS' }[family] || String(family || '');
    }

    function subCrossoverLabel(shape) {
        return `${familyPrefix(shape?.family)}${shape?.slope_db_oct}`;
    }

    function normalizeSingleSubwooferSettings(input = {}) {
        // Same -80 dB floor as normalizeSubwooferSettings: must round-trip the
        // 2.2 AutoSub mute level instead of clamping the display to -24 and
        // unmuting (+56 dB) on the next UI save.
        const level = Math.max(-80, Math.min(12, Number(input.level_db ?? input.levelDb ?? 0) || 0));
        const alignment = Math.max(-40, Math.min(40, Number(input.alignment_ms ?? input.alignmentMs ?? 0) || 0));
        const polarity = String(input.polarity ?? 'normal').toLowerCase() === 'invert' ? 'invert' : 'normal';
        return {
            level_db: Math.round(level * 10) / 10,
            alignment_ms: Math.round(alignment * 100) / 100,
            polarity,
        };
    }

    function subwoofer21ToSub22Sub(subwoofer = {}) {
        const normalized = normalizeSubwooferSettings(subwoofer || {});
        return normalizeSingleSubwooferSettings({
            level_db: normalized.sub_level_db,
            alignment_ms: normalized.sub_alignment_ms,
            polarity: normalized.sub_polarity,
        });
    }

    function getSubwooferGlobalSettings(outputMode = {}, fallback = {}) {
        return normalizeSubwooferSettings({
            ...(fallback || {}),
            ...(outputMode.subwoofer || {}),
            crossover_frequency_hz: outputMode.crossover_frequency_hz ?? outputMode.subwoofer?.crossover_frequency_hz ?? fallback?.crossover_frequency_hz,
            family: outputMode.crossover?.family ?? fallback?.family,
            slope_db_oct: outputMode.crossover?.slope_db_oct ?? fallback?.slope_db_oct,
            main_highpass_enabled: outputMode.main_highpass_enabled ?? outputMode.subwoofer?.main_highpass_enabled ?? fallback?.main_highpass_enabled,
            sub_level_db: outputMode.subwoofers?.sub1?.level_db ?? outputMode.subwoofer?.sub_level_db ?? fallback?.sub_level_db,
            sub_alignment_ms: outputMode.subwoofers?.sub1?.alignment_ms ?? outputMode.subwoofer?.sub_alignment_ms ?? fallback?.sub_alignment_ms,
            sub_polarity: outputMode.subwoofers?.sub1?.polarity ?? outputMode.subwoofer?.sub_polarity ?? fallback?.sub_polarity,
        });
    }

    function normalizeSubwoofersSettings(subwoofers = {}, fallbackSubwoofer = {}) {
        const fallbackSub = subwoofer21ToSub22Sub(fallbackSubwoofer);
        return {
            sub1: normalizeSingleSubwooferSettings(subwoofers?.sub1 || fallbackSub),
            sub2: normalizeSingleSubwooferSettings(subwoofers?.sub2 || {}),
        };
    }

    function readSubCrossoverShape(frequencyEl, familyEl, slopeEl, fallback) {
        return normalizeSubCrossoverShape({
            frequency_hz: frequencyEl?.value, family: familyEl?.value,
            slope_db_oct: slopeEl?.value,
        }, fallback);
    }

    function collectSubCrossoverDraft() {
        const layout = subCrossoverSettings(routedSubwooferView());
        const shared = readSubCrossoverShape(deps.getElements().effectsSubwooferFrequencyNumber,
            deps.getElements().effectsSubwooferFamily, deps.getElements().effectsSubwooferSlope, layout);
        const linked = deps.getElements().effectsSubwooferLink
            ? !!deps.getElements().effectsSubwooferLink.checked : layout.link !== false;
        const split = layout.stereo && !linked;
        const left = split ? readSubCrossoverShape(deps.getElements().effectsSubwooferLeftFrequency,
            deps.getElements().effectsSubwooferLeftFamily, deps.getElements().effectsSubwooferLeftSlope, layout.left) : shared;
        const right = split ? readSubCrossoverShape(deps.getElements().effectsSubwooferRightFrequency,
            deps.getElements().effectsSubwooferRightFamily, deps.getElements().effectsSubwooferRightSlope, layout.right) : shared;
        return {
            ...shared,
            sub_link: !split,
            sub_filters: { left: { ...left }, right: { ...right } },
        };
    }

    function collectSubwooferSettings() {
        const crossover = collectSubCrossoverDraft();
        return normalizeSubwooferSettings({
            crossover_frequency_hz: crossover.frequency_hz,
            family: crossover.family,
            slope_db_oct: crossover.slope_db_oct,
            main_highpass_enabled: subMainHighpassEnabled(),
            sub_level_db: deps.getElements().effectsSubwooferLevel?.value || 0,
            sub_alignment_ms: deps.getElements().effectsSubwooferDelay?.value || 0,
            sub_polarity: deps.getElements().effectsSubwooferPolarity?.value || 'normal',
            sub_link: crossover.sub_link,
            left_crossover: crossover.sub_filters.left,
            right_crossover: crossover.sub_filters.right,
        });
    }

    function collectSubwoofer22Settings() {
        const subwoofer = collectSubwooferSettings();
        return {
            subwoofer,
            subwoofers: {
                sub1: subwoofer21ToSub22Sub(subwoofer),
                sub2: normalizeSingleSubwooferSettings({
                    level_db: deps.getElements().effectsSubwooferSub2Level?.value || 0,
                    alignment_ms: deps.getElements().effectsSubwooferSub2Delay?.value || 0,
                    polarity: deps.getElements().effectsSubwooferSub2Polarity?.value || 'normal',
                }),
            },
        };
    }

    function isSubwoofer22Mode(mode) {
        return ['subwoofer-2.2', 'subwoofer-2.2-stereo'].includes(mode);
    }

    function isSubwooferModeName(mode) {
        return ['subwoofer-2.1', 'subwoofer-2.2', 'subwoofer-2.2-stereo'].includes(mode);
    }

    async function flushSubwooferSettingsBeforeMeasurement() {
        const outputMode = routedSubwooferView();
        if (!isSubwooferModeName(outputMode.mode)) return;

        let pendingPromise = null;
        if (_subwooferPendingSave) {
            pendingPromise = _subwooferPendingSave.start();
        }
        if (pendingPromise) {
            await pendingPromise;
        } else if (_subwooferSavePromise) {
            await _subwooferSavePromise;
        }
    }

    function routedSubwooferView() {
        return outputSystem()?.subwooferView(deps.getState().outputSystem?.catalog) || { mode: 'stereo', roles: [] };
    }

    function getSubwooferPreviewSettingsFromState() {
        return routedSubwooferView().subwoofer || normalizeSubwooferSettings({});
    }

    function primeSubwooferPreview() {
        if (!deps.getElements().effectsSubwooferPreview) return;
        drawSubwooferPreview(getSubwooferPreviewSettingsFromState());
    }

    function requestSubwooferPreviewRedrawFromState() {
        if (!deps.getElements().effectsSubwooferPreview) return;
        scheduleSubwooferPreviewDraw(getSubwooferPreviewSettingsFromState());
    }

    function scheduleSubwooferPreviewDraw(subwoofer) {
        if (!deps.getElements().effectsSubwooferPreview) return;
        const normalized = normalizeSubwooferSettings(subwoofer || {});
        if (_subwooferPreviewDrawFrame) window.cancelAnimationFrame(_subwooferPreviewDrawFrame);
        _subwooferPreviewDrawFrame = window.requestAnimationFrame(() => {
            _subwooferPreviewDrawFrame = null;
            drawSubwooferPreview(normalized);
            window.requestAnimationFrame(() => drawSubwooferPreview(normalized));
        });
    }

    function formatSubwooferDelayMs(value) {
        const numeric = Number(value);
        return Number.isFinite(numeric) ? numeric.toFixed(2) : '0.00';
    }

    function subwooferSelectedSide() {
        return _subwooferSelectedSide === 'right' ? 'right' : 'left';
    }

    function setSubwooferSelectedSide(side) {
        _subwooferSelectedSide = side === 'right' ? 'right' : 'left';
    }

    function renderSubwooferPanel() {
        const _activeEditing = deps.getActiveEditing();
        const outputMode = routedSubwooferView();
        if (!deps.getState().outputSystem?.catalog) {
            deps.getElements().effectsSubwooferCard?.classList.add('hidden');
            return;
        }
        const mode = outputMode.mode || 'stereo';
        const isSubwooferMode = isSubwooferModeName(mode);
        const is22Mode = isSubwoofer22Mode(mode);
        const is22StereoMode = mode === 'subwoofer-2.2-stereo';
        deps.getElements().effectsSubwooferCard?.classList.toggle('hidden', !isSubwooferMode);
        deps.getElements().effectsSubwooferCard?.classList.toggle('is-subwoofer-22', is22Mode);
        if (!isSubwooferMode) {
            setSubwooferFeedback('');
            return;
        }
        const subwoofer = is22Mode
            ? getSubwooferGlobalSettings(outputMode, outputMode.subwoofer || {})
            : normalizeSubwooferSettings(outputMode.subwoofer || {});
        const subwoofers = normalizeSubwoofersSettings(outputMode.subwoofers || {}, subwoofer);
        const crossoverLayout = subCrossoverSettings(outputMode, subwoofer);
        const splitSides = crossoverLayout.stereo && crossoverLayout.link === false;
        const selectedSide = subwooferSelectedSide();
        deps.getElements().effectsSubwooferCard?.classList.toggle('is-crossover-split', splitSides);
        // Coupled layouts (2.1, Dual-Mono, linked Stereo) share one crossover
        // block. An unlinked Stereo pair serves a single side at a time: the
        // tabs above the graph pick it, the other side's block stays hidden but
        // keeps its stored values for the save.
        deps.getElements().effectsSubwooferSharedCrossover?.classList.toggle('hidden', splitSides);
        deps.getElements().effectsSubwooferLeftCrossover?.classList.toggle('hidden', !splitSides || selectedSide !== 'left');
        deps.getElements().effectsSubwooferRightCrossover?.classList.toggle('hidden', !splitSides || selectedSide !== 'right');
        // Crossover card label follows the link state: unlinked shows the
        // selected side, linked shows Global.
        if (deps.getElements().effectsSubwooferGlobalLabel) deps.getElements().effectsSubwooferGlobalLabel.textContent = splitSides ? (selectedSide === 'right' ? 'Sub R' : 'Sub L') : 'Global';
        // The tab row mirrors the speaker tile: Link L/R plus the side tabs sit
        // above the graph instead of inside a card. Linked stereo shows one
        // common Sub L/R tab; unlinked shows Sub L and Sub R separately. The
        // row stays populated either way, so the checkbox never moves.
        deps.getElements().effectsSubwooferTabRow?.classList.toggle('hidden', !crossoverLayout.stereo);
        deps.getElements().effectsSubwooferSideTabs?.classList.toggle('hidden', !crossoverLayout.stereo);
        const showBothSubTab = crossoverLayout.stereo && !splitSides;
        if (deps.getElements().effectsSubwooferTabLeft) {
            const active = splitSides && selectedSide === 'left';
            deps.getElements().effectsSubwooferTabLeft.classList.toggle('hidden', !splitSides);
            deps.getElements().effectsSubwooferTabLeft.classList.toggle('is-active', active);
            deps.getElements().effectsSubwooferTabLeft.setAttribute('aria-selected', active ? 'true' : 'false');
        }
        if (deps.getElements().effectsSubwooferTabRight) {
            const active = splitSides && selectedSide === 'right';
            deps.getElements().effectsSubwooferTabRight.classList.toggle('hidden', !splitSides);
            deps.getElements().effectsSubwooferTabRight.classList.toggle('is-active', active);
            deps.getElements().effectsSubwooferTabRight.setAttribute('aria-selected', active ? 'true' : 'false');
        }
        if (deps.getElements().effectsSubwooferTabBoth) {
            deps.getElements().effectsSubwooferTabBoth.classList.toggle('hidden', !showBothSubTab);
            deps.getElements().effectsSubwooferTabBoth.classList.toggle('is-active', showBothSubTab);
            deps.getElements().effectsSubwooferTabBoth.setAttribute('aria-selected', showBothSubTab ? 'true' : 'false');
        }
        deps.getElements().effectsSubwooferLinkWrap?.classList.toggle('hidden', !crossoverLayout.stereo);
        renderSubwooferCrossover(crossoverLayout);
        if (deps.getElements().effectsSubwooferRouting) {
            const hpf = (subwoofer.main_highpass_enabled ?? true) ? 'on' : 'off';
            deps.getElements().effectsSubwooferRouting.textContent = splitSides
                ? `Sub L ${subCrossoverLabel(crossoverLayout.left)} @ ${crossoverLayout.left.frequency_hz} Hz`
                    + ` · Sub R ${subCrossoverLabel(crossoverLayout.right)} @ ${crossoverLayout.right.frequency_hz} Hz`
                    + ` · Main HPF ${hpf}`
                : `Crossover ${crossoverLayout.frequency_hz} Hz · ${subCrossoverLabel(crossoverLayout)} · Main HPF ${hpf}`;
        }
        if (deps.getElements().effectsSubwooferModeBadge) {
            deps.getElements().effectsSubwooferModeBadge.textContent = outputSystem().subModeLabel(outputMode.sub_mode);
            deps.getElements().effectsSubwooferModeBadge.classList.toggle('is-active', true);
        }
        const [firstLabel, secondLabel] = outputMode.roles.map(outputSystem().roleLabel);
        // The sub name lives in the card header; the three controls keep the
        // short Trim-style labels so they never wrap.
        if (deps.getElements().effectsSubwooferLevelLabel) deps.getElements().effectsSubwooferLevelLabel.textContent = 'Level';
        if (deps.getElements().effectsSubwooferDelayLabel) deps.getElements().effectsSubwooferDelayLabel.textContent = 'Align';
        if (deps.getElements().effectsSubwooferPolarityLabel) deps.getElements().effectsSubwooferPolarityLabel.textContent = 'Polarity';
        if (deps.getElements().effectsSubwooferSub1GroupLabel) deps.getElements().effectsSubwooferSub1GroupLabel.textContent = firstLabel;
        if (deps.getElements().effectsSubwooferSub2GroupLabel) deps.getElements().effectsSubwooferSub2GroupLabel.textContent = secondLabel || '';
        if (deps.getElements().effectsSubwooferSub2LevelLabel) deps.getElements().effectsSubwooferSub2LevelLabel.textContent = 'Level';
        if (deps.getElements().effectsSubwooferSub2DelayLabel) deps.getElements().effectsSubwooferSub2DelayLabel.textContent = 'Align';
        if (deps.getElements().effectsSubwooferSub2PolarityLabel) deps.getElements().effectsSubwooferSub2PolarityLabel.textContent = 'Polarity';
        deps.getElements().effectsSubwooferSub2Fields?.forEach(field => field.classList.toggle('hidden', !is22Mode));
        deps.getElements().effectsSubwooferDerivedDelays?.classList.toggle('hidden', !is22Mode);
        applySubMainHighpass(subwoofer.main_highpass_enabled !== false);
        if (deps.getElements().effectsSubwooferLevel && !_activeEditing.has(deps.getElements().effectsSubwooferLevel)) {
            deps.getElements().effectsSubwooferLevel.value = String(subwoofer.sub_level_db);
        }
        if (deps.getElements().effectsSubwooferDelay && !_activeEditing.has(deps.getElements().effectsSubwooferDelay)) {
            deps.getElements().effectsSubwooferDelay.value = String(subwoofer.sub_alignment_ms);
        }
        if (deps.getElements().effectsSubwooferPolarity && !_activeEditing.has(deps.getElements().effectsSubwooferPolarity)) {
            deps.getElements().effectsSubwooferPolarity.value = subwoofer.sub_polarity;
        }
        if (deps.getElements().effectsSubwooferSub2Level && !_activeEditing.has(deps.getElements().effectsSubwooferSub2Level)) {
            deps.getElements().effectsSubwooferSub2Level.value = String(subwoofers.sub2.level_db);
        }
        if (deps.getElements().effectsSubwooferSub2Delay && !_activeEditing.has(deps.getElements().effectsSubwooferSub2Delay)) {
            deps.getElements().effectsSubwooferSub2Delay.value = String(subwoofers.sub2.alignment_ms);
        }
        if (deps.getElements().effectsSubwooferSub2Polarity && !_activeEditing.has(deps.getElements().effectsSubwooferSub2Polarity)) {
            deps.getElements().effectsSubwooferSub2Polarity.value = subwoofers.sub2.polarity;
        }
        if (is22Mode) {
            if (deps.getElements().effectsSubwooferDdMain) deps.getElements().effectsSubwooferDdMain.textContent = formatSubwooferDelayMs(outputMode.derived_main_delay_ms);
            if (deps.getElements().effectsSubwooferDdSub1) deps.getElements().effectsSubwooferDdSub1.textContent = formatSubwooferDelayMs(outputMode.derived_sub1_delay_ms);
            if (deps.getElements().effectsSubwooferDdSub2) deps.getElements().effectsSubwooferDdSub2.textContent = formatSubwooferDelayMs(outputMode.derived_sub2_delay_ms);
        }
        // The plot paints the shared curve, or one curve per side while a Stereo
        // pair is unlinked. The catalog view carries the split only as a link
        // flag, so hand the resolved per-side shapes to the preview.
        scheduleSubwooferPreviewDraw({
            ...subwoofer,
            sub_link: !splitSides,
            left_crossover: crossoverLayout.left,
            right_crossover: crossoverLayout.right,
        });
    }

    function subCrossoverSlopes(family) {
        const slopes = deps.getState().outputSystem?.catalog?.capabilities?.filter_families?.[family];
        if (Array.isArray(slopes) && slopes.length) return slopes.slice();
        return family === 'linkwitz-riley'
            ? [12, 24, 36, 48, 60, 72]
            : [6, 12, 18, 24, 30, 36, 42, 48, 54, 60, 66, 72];
    }

    function applySubCrossoverShape(frequencyEl, familyEl, slopeEl, shape) {
        const _activeEditing = deps.getActiveEditing();
        if (frequencyEl && !_activeEditing.has(frequencyEl)) {
            frequencyEl.value = String(shape.frequency_hz);
        }
        if (familyEl) {
            const html = SUB_CROSSOVER_FAMILIES.map((family) =>
                `<option value="${family}"${family === shape.family ? ' selected' : ''}>${crossoverUiLabel(family)}</option>`).join('');
            if (familyEl.innerHTML !== html) familyEl.innerHTML = html;
            if (familyEl.value !== shape.family) familyEl.value = shape.family;
        }
        if (slopeEl) {
            const slopes = subCrossoverSlopes(shape.family);
            const html = slopes.map((slope) =>
                `<option value="${slope}"${slope === shape.slope_db_oct ? ' selected' : ''}>${slope}</option>`).join('');
            if (slopeEl.innerHTML !== html) slopeEl.innerHTML = html;
            if (String(slopeEl.value) !== String(shape.slope_db_oct)) slopeEl.value = String(shape.slope_db_oct);
        }
    }

    function renderSubwooferCrossover(layout) {
        const _activeEditing = deps.getActiveEditing();
        applySubCrossoverShape(deps.getElements().effectsSubwooferFrequencyNumber,
            deps.getElements().effectsSubwooferFamily, deps.getElements().effectsSubwooferSlope, layout);
        applySubCrossoverShape(deps.getElements().effectsSubwooferLeftFrequency,
            deps.getElements().effectsSubwooferLeftFamily, deps.getElements().effectsSubwooferLeftSlope, layout.left);
        applySubCrossoverShape(deps.getElements().effectsSubwooferRightFrequency,
            deps.getElements().effectsSubwooferRightFamily, deps.getElements().effectsSubwooferRightSlope, layout.right);
        if (deps.getElements().effectsSubwooferLink && !_activeEditing.has(deps.getElements().effectsSubwooferLink)) {
            deps.getElements().effectsSubwooferLink.checked = layout.link !== false;
        }
        const roles = routedSubwooferView().roles || [];
        const labels = { left: roles[0], right: roles[1] };
        // The tabs above the graph carry the side names; the single visible
        // crossover block needs no extra side heading.
        if (deps.getElements().effectsSubwooferTabLeft) {
            deps.getElements().effectsSubwooferTabLeft.textContent = labels.left
                ? outputSystem().roleLabel(labels.left) : 'Sub L';
        }
        if (deps.getElements().effectsSubwooferTabRight) {
            deps.getElements().effectsSubwooferTabRight.textContent = labels.right
                ? outputSystem().roleLabel(labels.right) : 'Sub R';
        }
    }

    function applySubMainHighpass(enabled) {
        const _activeEditing = deps.getActiveEditing();
        for (const el of [deps.getElements().effectsSubwooferMainHighpass,
            deps.getElements().effectsSubwooferLeftMainHighpass, deps.getElements().effectsSubwooferRightMainHighpass]) {
            if (el && !_activeEditing.has(el)) el.value = enabled ? 'on' : 'off';
        }
    }

    function subMainHighpassEnabled() {
        const split = deps.getElements().effectsSubwooferCard?.classList.contains('is-crossover-split') === true;
        if (split) {
            const selected = subwooferSelectedSide() === 'right'
                ? deps.getElements().effectsSubwooferRightMainHighpass
                : deps.getElements().effectsSubwooferLeftMainHighpass;
            if (selected) return (selected.value || 'on') !== 'off';
        }
        const el = deps.getElements().effectsSubwooferMainHighpass
            || deps.getElements().effectsSubwooferLeftMainHighpass || deps.getElements().effectsSubwooferRightMainHighpass;
        return (el?.value || 'on') !== 'off';
    }

    function clearSubwooferActiveEditing() {
        const _activeEditing = deps.getActiveEditing();
        [
            deps.getElements().effectsSubwooferFrequencyNumber,
            deps.getElements().effectsSubwooferFamily,
            deps.getElements().effectsSubwooferSlope,
            deps.getElements().effectsSubwooferLeftFrequency,
            deps.getElements().effectsSubwooferLeftFamily,
            deps.getElements().effectsSubwooferLeftSlope,
            deps.getElements().effectsSubwooferRightFrequency,
            deps.getElements().effectsSubwooferRightFamily,
            deps.getElements().effectsSubwooferRightSlope,
            deps.getElements().effectsSubwooferLink,
            deps.getElements().effectsSubwooferMainHighpass,
            deps.getElements().effectsSubwooferLeftMainHighpass,
            deps.getElements().effectsSubwooferRightMainHighpass,
            deps.getElements().effectsSubwooferLevel,
            deps.getElements().effectsSubwooferDelay,
            deps.getElements().effectsSubwooferPolarity,
            deps.getElements().effectsSubwooferSub2Level,
            deps.getElements().effectsSubwooferSub2Delay,
            deps.getElements().effectsSubwooferSub2Polarity,
        ].forEach((el) => {
            if (el) _activeEditing.delete(el);
        });
    }

    function getSubwooferPreviewLayout(width, height) {
        const pad = { left: 56, right: 24, top: 18, bottom: 24 };
        return {
            pad,
            plotW: Math.max(1, width - pad.left - pad.right),
            plotH: Math.max(1, height - pad.top - pad.bottom),
        };
    }

    function drawSubwooferPreview(subwoofer) {
        const canvas = deps.getElements().effectsSubwooferPreview;
        if (!canvas) return;
        const ctx = canvas.getContext('2d');
        if (!ctx) return;
        const settings = normalizeSubwooferSettings(subwoofer || {});
        const displayWidth = Math.max(320, Math.round(canvas.clientWidth || canvas.width || 560));
        const displayHeight = Math.max(112, Math.round(canvas.clientHeight || canvas.height || 132));
        const dpr = (typeof window !== 'undefined' && window.devicePixelRatio) || 1;
        const targetWidth = Math.round(displayWidth * dpr);
        const targetHeight = Math.round(displayHeight * dpr);
        if (canvas.width !== targetWidth || canvas.height !== targetHeight) {
            canvas.width = targetWidth;
            canvas.height = targetHeight;
        }
        ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
        const width = displayWidth;
        const height = displayHeight;
        const { pad, plotW, plotH } = getSubwooferPreviewLayout(width, height);
        const minHz = 20;
        const maxHz = 300;
        const minDb = -18;
        const maxDb = 0;
        const mod = (root && root.FXRouteCrossover) || null;
        const sharedShape = { family: settings.family, slope_db_oct: settings.slope_db_oct,
            frequency_hz: settings.crossover_frequency_hz };
        // An unlinked Stereo pair paints both sides: the selected one solid, the
        // other one dimmed. Anything coupled, Mono or Dual-Mono paints the shared shape.
        const selectedSide = subwooferSelectedSide();
        const shapes = settings.sub_link === false
            ? (selectedSide === 'right'
                ? [{ shape: settings.right_crossover, alpha: 1 }, { shape: settings.left_crossover, alpha: 0.42 }]
                : [{ shape: settings.left_crossover, alpha: 1 }, { shape: settings.right_crossover, alpha: 0.42 }])
            : [{ shape: sharedShape, alpha: 1 }];
        const xForHz = (hz) => pad.left + ((Math.log10(hz) - Math.log10(minHz)) / (Math.log10(maxHz) - Math.log10(minHz))) * plotW;
        const yForDb = (db) => pad.top + ((maxDb - db) / (maxDb - minDb)) * plotH;
        const responseDb = (shape, hz, highpass) => (mod?.crossoverMagnitudeDb
            ? mod.crossoverMagnitudeDb(shape, hz, highpass ? 'highpass' : 'lowpass')
            : -20 * Math.log10(1 + Math.pow(Math.max(1e-9, highpass ? shape.frequency_hz / hz : hz / shape.frequency_hz), 4)));
        ctx.clearRect(0, 0, width, height);
        ctx.fillStyle = '#08111f';
        ctx.fillRect(0, 0, width, height);
        ctx.fillStyle = 'rgba(255,255,255,0.035)';
        ctx.fillRect(pad.left, pad.top, plotW, plotH);
        ctx.strokeStyle = 'rgba(255,255,255,0.10)';
        ctx.lineWidth = 1;
        const frequencyLabels = [40, 80, 120, 200];
        const dbLabels = [0, -6, -12, -18];
        frequencyLabels.forEach((hz) => {
            const x = xForHz(hz);
            ctx.beginPath();
            ctx.moveTo(x, pad.top);
            ctx.lineTo(x, pad.top + plotH);
            ctx.stroke();
        });
        dbLabels.forEach((db) => {
            const y = yForDb(db);
            ctx.beginPath();
            ctx.moveTo(pad.left, y);
            ctx.lineTo(pad.left + plotW, y);
            ctx.stroke();
        });
        ctx.fillStyle = 'rgba(229,231,235,0.54)';
        ctx.font = '500 10px system-ui, sans-serif';
        ctx.textAlign = 'right';
        ctx.textBaseline = 'middle';
        dbLabels.forEach((db) => {
            const y = yForDb(db);
            ctx.fillText(`${db} dB`, pad.left - 8, y);
        });
        ctx.textAlign = 'center';
        ctx.textBaseline = 'alphabetic';
        frequencyLabels.forEach((hz) => {
            ctx.fillText(`${hz} Hz`, xForHz(hz), height - 8);
        });
        ctx.textAlign = 'start';
        const drawCurve = (shape, highpass, color, alpha) => {
            ctx.globalAlpha = alpha;
            ctx.beginPath();
            for (let i = 0; i <= 160; i += 1) {
                const t = i / 160;
                const hz = Math.pow(10, Math.log10(minHz) + t * (Math.log10(maxHz) - Math.log10(minHz)));
                const db = responseDb(shape, hz, highpass) + (highpass ? 0 : settings.sub_level_db || 0);
                const x = xForHz(hz);
                const y = yForDb(Math.max(minDb, Math.min(maxDb, db)));
                if (i === 0) ctx.moveTo(x, y);
                else ctx.lineTo(x, y);
            }
            ctx.strokeStyle = color;
            ctx.lineWidth = 3;
            ctx.stroke();
            ctx.globalAlpha = 1;
        };
        for (const { shape, alpha } of shapes) {
            drawCurve(shape, false, '#6ee7b7', alpha);
            if (settings.main_highpass_enabled) drawCurve(shape, true, '#93c5fd', alpha);
        }
        ctx.font = '600 11px system-ui, sans-serif';
        ctx.textAlign = 'center';
        ctx.textBaseline = 'middle';
        shapes.forEach(({ shape, alpha }, index) => {
            const crossover = Number(shape?.frequency_hz) || settings.crossover_frequency_hz;
            const markerX = xForHz(crossover);
            ctx.globalAlpha = alpha;
            ctx.strokeStyle = 'rgba(255,255,255,0.72)';
            ctx.lineWidth = 1.5;
            ctx.beginPath();
            ctx.moveTo(markerX, pad.top);
            ctx.lineTo(markerX, pad.top + plotH);
            ctx.stroke();
            const markerLabel = `${crossover} Hz`;
            const markerLabelWidth = ctx.measureText(markerLabel).width + 12;
            const markerLabelX = Math.max(pad.left + markerLabelWidth / 2 + 2, Math.min(pad.left + plotW - markerLabelWidth / 2 - 2, markerX));
            const markerLabelY = index === 0 ? pad.top + plotH * 0.72 : pad.top + plotH * 0.72 + 20;
            ctx.fillStyle = 'rgba(12,18,28,0.82)';
            ctx.fillRect(markerLabelX - markerLabelWidth / 2, markerLabelY - 9, markerLabelWidth, 18);
            ctx.strokeStyle = 'rgba(255,255,255,0.16)';
            ctx.lineWidth = 1;
            ctx.strokeRect(markerLabelX - markerLabelWidth / 2, markerLabelY - 9, markerLabelWidth, 18);
            ctx.fillStyle = '#e5e7eb';
            ctx.fillText(markerLabel, markerLabelX, markerLabelY);
            ctx.globalAlpha = 1;
        });
        ctx.textAlign = 'start';
    }

    function setSubwooferFeedback(message, cls = '') {
        if (!deps.getElements().effectsSubwooferFeedback) return;
        deps.getElements().effectsSubwooferFeedback.textContent = message;
        deps.getElements().effectsSubwooferFeedback.className = 'effects-extras-feedback' + (cls ? ' ' + cls : '');
    }

    function updateSubwooferDraftFromControls() {
        const mode = routedSubwooferView().mode;
        const settings = isSubwoofer22Mode(mode) ? collectSubwoofer22Settings() : collectSubwooferSettings();
        scheduleSubwooferPreviewDraw(settings.subwoofer || settings);
        return settings;
    }

    function beginSubwooferSave(pending) {
        const previousSave = _subwooferSavePromise;
        const run = (async () => {
            if (previousSave) await previousSave;
            _subwooferLastRequestedSignature = pending.signature;
            const catalog = deps.getState().outputSystem.catalog;
            if (!catalog || catalog.active_mode !== pending.mode) throw new Error('Output mode changed before sub settings commit');
            const result = await deps.applyMutation('set_subwoofers', pending.settings, false);
            if (!result) throw new Error('Subwoofer settings save was superseded before commit');
            return result;
        })();
        _subwooferSavePromise = run;
        // Saves run strictly sequentially (each awaits its predecessor), so an
        // older run can never overwrite a newer run's error feedback. Success
        // stays silent; only failures report into the tile.
        run.then(
            () => {
                if (_subwooferSavePromise === run) _subwooferSavePromise = null;
                releaseSubwooferLinkGuard();
                // Success stays silent, but clears a previous error so the tile
                // never reports a failure the latest commit already fixed.
                setSubwooferFeedback('');
            },
            (error) => {
                if (_subwooferSavePromise === run) _subwooferSavePromise = null;
                releaseSubwooferLinkGuard();
                setSubwooferFeedback(error?.message || 'Subwoofer settings save failed', 'error');
            },
        );
        return run;
    }

    function releaseSubwooferLinkGuard() {
        const _activeEditing = deps.getActiveEditing();
        if (deps.getElements().effectsSubwooferLink) _activeEditing.delete(deps.getElements().effectsSubwooferLink);
    }

    function createPendingSubwooferSave(mode, settings, signature) {
        let resolvePending;
        let rejectPending;
        const promise = new Promise((resolve, reject) => {
            resolvePending = resolve;
            rejectPending = reject;
        });
        const pending = {
            mode,
            settings,
            signature,
            promise,
            started: false,
            reject: rejectPending,
            start: null,
        };
        pending.start = () => {
            if (pending.started) return pending.promise;
            pending.started = true;
            if (_subwooferPendingSave === pending) _subwooferPendingSave = null;
            _subwooferSaveTimer = null;
            beginSubwooferSave(pending).then(resolvePending, rejectPending);
            return pending.promise;
        };
        // Background debounced saves are intentionally fire-and-forget, but their
        // rejection remains observable to the measurement preflush if awaited.
        promise.catch(() => {});
        return pending;
    }

    function cancelPendingSubwooferSave(reason = 'Subwoofer settings save superseded') {
        if (_subwooferSaveTimer !== null) {
            window.clearTimeout(_subwooferSaveTimer);
            _subwooferSaveTimer = null;
        }
        const pending = _subwooferPendingSave;
        _subwooferPendingSave = null;
        if (pending && !pending.started) pending.reject(new Error(reason));
    }

    function saveSubwooferDebounced(delayMs = SUBWOOFER_COMMIT_DEBOUNCE_MS) {
        cancelPendingSubwooferSave();
        const controls = updateSubwooferDraftFromControls();
        const catalog = deps.getState().outputSystem.catalog;
        if (!catalog) return Promise.resolve(null);
        const mode = catalog.active_mode;
        const bass = controls.subwoofer || controls;
        const subs = controls.subwoofers || { sub1: subwoofer21ToSub22Sub(bass) };
        const settings = { mode, frequency_hz: bass.crossover_frequency_hz,
            family: bass.family, slope_db_oct: bass.slope_db_oct,
            sub_link: bass.sub_link !== false,
            // Both side overrides always travel along: a coupled save mirrors the
            // shared shape so a later unlink starts from the audible values.
            sub_filters: { left: bass.left_crossover, right: bass.right_crossover },
            main_highpass_enabled: bass.main_highpass_enabled,
            processing: Object.fromEntries(routedSubwooferView().roles.map((role, index) => [role, subs[`sub${index + 1}`]])) };
        const signature = JSON.stringify(settings);
        if (signature === _subwooferLastRequestedSignature) {
            return _subwooferSavePromise || Promise.resolve(null);
        }
        const pending = createPendingSubwooferSave(mode, settings, signature);
        _subwooferPendingSave = pending;
        _subwooferSaveTimer = window.setTimeout(() => pending.start(), delayMs);
        return pending.promise;
    }

        function wireSubwooferControls() {
            const _activeEditing = deps.getActiveEditing();
            [
                deps.getElements().effectsSubwooferFrequencyNumber,
                deps.getElements().effectsSubwooferFamily,
                deps.getElements().effectsSubwooferSlope,
                deps.getElements().effectsSubwooferLeftFrequency,
                deps.getElements().effectsSubwooferLeftFamily,
                deps.getElements().effectsSubwooferLeftSlope,
                deps.getElements().effectsSubwooferRightFrequency,
                deps.getElements().effectsSubwooferRightFamily,
                deps.getElements().effectsSubwooferRightSlope,
                deps.getElements().effectsSubwooferLevel,
                deps.getElements().effectsSubwooferDelay,
                deps.getElements().effectsSubwooferPolarity,
                deps.getElements().effectsSubwooferSub2Level,
                deps.getElements().effectsSubwooferSub2Delay,
                deps.getElements().effectsSubwooferSub2Polarity,
            ].forEach(el => {
                if (!el) return;
                el.addEventListener('focus', () => _activeEditing.add(el));
                el.addEventListener('input', () => {
                    updateSubwooferDraftFromControls();
                });
                el.addEventListener('change', () => saveSubwooferDebounced(0));
                el.addEventListener('blur', () => {
                    _activeEditing.delete(el);
                    saveSubwooferDebounced(0);
                });
            });
            // A family switch can invalidate the stored slope (a 24 dB/oct
            // Linkwitz-Riley has no Butterworth/Bessel counterpart at 6 dB steps
            // beyond the shared multiples), so the slope list follows it.
            const subFamilySwitched = (familyEl, slopeEl) => {
                if (!familyEl || !slopeEl) return;
                const slopes = subCrossoverSlopes(familyEl.value);
                const wanted = Math.round(Number(slopeEl.value) || 0) || 24;
                const chosen = slopes.includes(wanted) ? wanted
                    : slopes.reduce((best, slope) => (Math.abs(slope - wanted) < Math.abs(best - wanted) ? slope : best), slopes[0]);
                const html = slopes.map((slope) => `<option value="${slope}">${slope}</option>`).join('');
                if (slopeEl.innerHTML !== html) slopeEl.innerHTML = html;
                slopeEl.value = String(chosen);
            };
            for (const [familyEl, slopeEl] of [
                [deps.getElements().effectsSubwooferFamily, deps.getElements().effectsSubwooferSlope],
                [deps.getElements().effectsSubwooferLeftFamily, deps.getElements().effectsSubwooferLeftSlope],
                [deps.getElements().effectsSubwooferRightFamily, deps.getElements().effectsSubwooferRightSlope],
            ]) {
                if (familyEl) familyEl.addEventListener('change', () => subFamilySwitched(familyEl, slopeEl));
            }
            // The Main highpass switch is one global flag; its side copies mirror it.
            for (const el of [deps.getElements().effectsSubwooferMainHighpass,
                deps.getElements().effectsSubwooferLeftMainHighpass, deps.getElements().effectsSubwooferRightMainHighpass]) {
                if (!el) continue;
                el.addEventListener('change', () => {
                    applySubMainHighpass(el.value !== 'off');
                    saveSubwooferDebounced(0);
                });
            }
            // Unlinked Stereo: the Sub L / Sub R tabs pick which side the single
            // crossover block serves. No save: the stored per-side values only change
            // through the block's own controls or the graph drag. The common Sub L/R
            // tab (linked) selects nothing; it only re-affirms the coupled view.
            for (const tab of [deps.getElements().effectsSubwooferTabLeft, deps.getElements().effectsSubwooferTabRight,
                deps.getElements().effectsSubwooferTabBoth]) {
                if (!tab) continue;
                tab.addEventListener('click', () => {
                    if (tab.dataset.subSide) setSubwooferSelectedSide(tab.dataset.subSide);
                    renderSubwooferPanel();
                });
            }
            if (deps.getElements().effectsSubwooferLink) {
                deps.getElements().effectsSubwooferLink.addEventListener('change', () => {
                    // A checkbox has no blur to end its edit, so the toggle guards
                    // itself until the debounced save released it: the render must not
                    // flip it back to the stored link state in the meantime.
                    _activeEditing.add(deps.getElements().effectsSubwooferLink);
                    renderSubwooferPanel();
                    saveSubwooferDebounced(0);
                });
            }
            if (deps.getElements().effectsSubwooferPreview) {
                let draggingCrossover = false;
                const splitPreview = () => deps.getElements().effectsSubwooferCard?.classList.contains('is-crossover-split') === true;
                // Graph drag serves the shared crossover, or the currently selected
                // side while a Stereo pair is unlinked.
                const dragTargetFrequencyInput = () => {
                    if (!splitPreview()) return deps.getElements().effectsSubwooferFrequencyNumber;
                    return subwooferSelectedSide() === 'right'
                        ? deps.getElements().effectsSubwooferRightFrequency
                        : deps.getElements().effectsSubwooferLeftFrequency;
                };
                const updateFromPointer = (event, commit = false) => {
                    const rect = deps.getElements().effectsSubwooferPreview.getBoundingClientRect();
                    const layout = getSubwooferPreviewLayout(rect.width || 560, rect.height || 132);
                    const plotW = Math.max(1, layout.plotW);
                    const t = Math.max(0, Math.min(1, (event.clientX - rect.left - layout.pad.left) / plotW));
                    const minHz = 20;
                    const maxHz = 300;
                    const hz = Math.round(Math.pow(10, Math.log10(minHz) + t * (Math.log10(maxHz) - Math.log10(minHz))));
                    const target = dragTargetFrequencyInput();
                    if (target) target.value = String(Math.max(40, Math.min(200, hz)));
                    if (commit) saveSubwooferDebounced(0);
                    else updateSubwooferDraftFromControls();
                };
                deps.getElements().effectsSubwooferPreview.addEventListener('pointerdown', (event) => {
                    draggingCrossover = true;
                    const target = dragTargetFrequencyInput();
                    if (target) _activeEditing.add(target);
                    deps.getElements().effectsSubwooferPreview.setPointerCapture?.(event.pointerId);
                    updateFromPointer(event, false);
                });
                deps.getElements().effectsSubwooferPreview.addEventListener('pointermove', (event) => {
                    if (draggingCrossover) updateFromPointer(event, false);
                });
                deps.getElements().effectsSubwooferPreview.addEventListener('pointerup', (event) => {
                    if (!draggingCrossover) return;
                    draggingCrossover = false;
                    updateFromPointer(event, true);
                    const target = dragTargetFrequencyInput();
                    if (target) _activeEditing.delete(target);
                });
                deps.getElements().effectsSubwooferPreview.addEventListener('pointercancel', () => {
                    draggingCrossover = false;
                    const target = dragTargetFrequencyInput();
                    if (target) _activeEditing.delete(target);
                });
                if ('ResizeObserver' in window) {
                    const resizeObserver = new ResizeObserver(() => requestSubwooferPreviewRedrawFromState());
                    resizeObserver.observe(deps.getElements().effectsSubwooferPreview);
                    deps.getElements().effectsSubwooferPreview._fxrouteResizeObserver = resizeObserver;
                } else {
                    window.addEventListener('resize', requestSubwooferPreviewRedrawFromState);
                }
            }
        }

    return {
        init,
        defaultSubCrossoverSlope,
        clampSubCrossoverFrequency,
        normalizeSubCrossoverShape,
        subCrossoverSettings,
        normalizeSubwooferSettings,
        familyPrefix,
        subCrossoverLabel,
        normalizeSingleSubwooferSettings,
        subwoofer21ToSub22Sub,
        getSubwooferGlobalSettings,
        normalizeSubwoofersSettings,
        readSubCrossoverShape,
        collectSubCrossoverDraft,
        collectSubwooferSettings,
        collectSubwoofer22Settings,
        isSubwoofer22Mode,
        isSubwooferModeName,
        flushSubwooferSettingsBeforeMeasurement,
        routedSubwooferView,
        getSubwooferPreviewSettingsFromState,
        primeSubwooferPreview,
        requestSubwooferPreviewRedrawFromState,
        scheduleSubwooferPreviewDraw,
        formatSubwooferDelayMs,
        subwooferSelectedSide,
        setSubwooferSelectedSide,
        renderSubwooferPanel,
        subCrossoverSlopes,
        applySubCrossoverShape,
        renderSubwooferCrossover,
        applySubMainHighpass,
        subMainHighpassEnabled,
        clearSubwooferActiveEditing,
        getSubwooferPreviewLayout,
        drawSubwooferPreview,
        setSubwooferFeedback,
        updateSubwooferDraftFromControls,
        beginSubwooferSave,
        releaseSubwooferLinkGuard,
        createPendingSubwooferSave,
        cancelPendingSubwooferSave,
        saveSubwooferDebounced,
        wireSubwooferControls,
    };
});
