// SPDX-License-Identifier: AGPL-3.0-only
/**
 * FXRoute output bank UI: bank selection, A/B compare binding and the
 * bank-bound import/export UI (stereo file, dual-filter, preset JSON/bundle,
 * REW PEQ, convolver IR, preset delete). Catalog and mutation control stay in
 * output_system_controller.js, domain logic in output_state.js; effects
 * editors (PEQ/convolver takes), the measurement editors and the main
 * effects render stay in app.js behind explicit callbacks. Compare-busy and
 * import-flight flags live here. Browser-loadable UMD, no build step;
 * Node-testable via require().
 */
(function (root, factory) {
    const api = factory(root);
    root.FXRouteBankUI = api;
    if (typeof module === 'object' && module.exports) module.exports = api;
})(typeof globalThis !== 'undefined' ? globalThis : window, function (root) {
    'use strict';

    let deps = {
        getState: () => ({ dsp: {}, outputSystem: {} }),
        getElements: () => ({}),
        showToast: () => {},
        escapeHtml: (value) => String(value == null ? '' : value),
        fetchEffects: async () => {},
        refreshCatalog: async () => null,
        collectEffectsExtras: () => ({}),
        measurementBankSumsBothInputs: () => false,
        presetFileUrl: () => '',
        renderEffects: () => {},
        renderMeasurementArea: () => {},
        measurementArea: () => null,
        applyMutation: async () => null,
        confirmDialog: (message) => (typeof confirm === 'function' ? confirm(message) : false),
    };

    function init(overrides) {
        deps = Object.assign(deps, overrides || {});
    }

    function outputState() {
        return (root && root.FXRouteOutputState) || null;
    }

    // Transition-error formatter owned by api.js; this module only displays it.
    function formatError(detail, fallback) {
        const api = (root && root.FXRouteApi) || null;
        return api && typeof api.formatTransitionErrorDetail === 'function'
            ? api.formatTransitionErrorDetail(detail, fallback) : fallback;
    }

    let effectsCompareLoadInFlight = false;
    let effectsCompareActionsWired = false;
    let _bankImportChannelMode = null;
    let effectsImportInFlight = false;

    function normalizeEffectsCompareSelection(compare = {}) {
        const presetA = typeof compare.presetA === 'string' ? compare.presetA : '';
        let presetB = typeof compare.presetB === 'string' ? compare.presetB : '';
        const activeSide = compare.activeSide === 'A' || compare.activeSide === 'B' ? compare.activeSide : null;
        if (presetA && presetB && presetA === presetB) {
            presetB = '';
        }
        return { presetA, presetB, activeSide };
    }

    function resolveEffectsCompareState(compare, presets = [], activePreset = '') {
        const server = normalizeEffectsCompareSelection(compare || {});
        const presetSet = new Set((presets || []).filter(Boolean));
        const presetA = presetSet.has(server.presetA) ? server.presetA : (activePreset && presetSet.has(activePreset) ? activePreset : '');
        const presetB = presetSet.has(server.presetB) ? server.presetB : '';
        const activeSide = server.activeSide === 'A' || server.activeSide === 'B' ? server.activeSide : null;
        return normalizeEffectsCompareSelection({ presetA, presetB, activeSide });
    }

    async function saveEffectsCompareState(compare) {
        try {
            const api = (root && root.FXRouteApi) || null;
            await api.apiPostJson('/api/dsp/compare', compare);
        } catch (e) {
            console.warn('Failed to persist effects compare state', e);
        }
    }

    function getDefaultEffectsCombineDraft() {
        return {
            preset1: '',
            preset2: '',
            preset3: '',
            presetName: '',
        };
    }

    function normalizeEffectsCombineDraft(draft = {}, presets = []) {
        const presetSet = new Set((presets || []).filter(Boolean));
        const chosen = [];
        const pickUnique = (value) => {
            const preset = presetSet.has(value) ? value : '';
            if (!preset || chosen.includes(preset)) return '';
            chosen.push(preset);
            return preset;
        };
        return {
            preset1: pickUnique(draft.preset1),
            preset2: pickUnique(draft.preset2),
            preset3: pickUnique(draft.preset3),
            presetName: typeof draft.presetName === 'string' ? draft.presetName : '',
        };
    }

    function setEffectsImportPanelOpen(shouldOpen) {
        if (!deps.getElements().effectsImportPanel || !deps.getElements().effectsToggleImportBtn) return;
        deps.getElements().effectsImportPanel.classList.toggle('hidden', !shouldOpen);
        deps.getElements().effectsToggleImportBtn.setAttribute('aria-expanded', shouldOpen ? 'true' : 'false');
        deps.getElements().effectsToggleImportBtn.textContent = shouldOpen ? 'Close Import' : 'Import';
    }

    function outputSystemBankBinding() {
        const catalog = (deps.getState().outputSystem || {}).catalog;
        try {
            return outputState()?.bankBinding(catalog) || null;
        } catch (e) {
            return null;
        }
    }

    function outputSystemCombineBank() {
        const catalog = (deps.getState().outputSystem || {}).catalog;
        if (!catalog) return null;
        try {
            return outputState()?.combineBank(catalog) || null;
        } catch (e) {
            return null;
        }
    }

    function visiblePresetEntriesForBank() {
        const entries = (deps.getState().dsp?.presets || []);
        const mod = (root && root.FXRouteOutputState) || null;
        const catalog = (deps.getState().outputSystem || {}).catalog;
        if (!catalog || !mod || typeof mod.presetsForBank !== 'function') return entries.slice();
        const bankId = catalog.modes?.[catalog.active_mode]?.selected_bank || 'global';
        // Strict per-bank stock: only the bank's own presets (plus built-ins;
        // untagged legacy reads as Global). No foreign presets are merged in,
        // so switching banks can never leak another bank's stock.
        return mod.presetsForBank(entries, bankId);
    }

    function visiblePresetNamesForBank() {
        return visiblePresetEntriesForBank().map((entry) => entry?.name).filter(Boolean);
    }

    function appendBankBindingFields(formData) {
        const binding = outputSystemBankBinding();
        if (!binding || !formData || typeof formData.append !== 'function') return false;
        formData.append('bank_mode', binding.bank_mode);
        formData.append('bank_id', binding.bank_id);
        formData.append('expected_revision', String(binding.expected_revision));
        return true;
    }

    function bankBindingJson() {
        return outputSystemBankBinding() || {};
    }

    function measurementPeqParams(leftBands, rightBands, eqMode) {
        const mod = (root && root.FXRouteOutputState) || null;
        const channelMode = deps.measurementArea()?.channel_mode;
        if (mod && typeof mod.peqParams === 'function') {
            return mod.peqParams(channelMode, leftBands, rightBands, eqMode);
        }
        return { channelMode: 'dual', eqMode, leftBands, rightBands };
    }

    function requireConcreteFilterBank() {
        if (deps.measurementArea()?.available !== false) return true;
        deps.showToast('Select a filter bank for import or measurement.', 'warning');
        return false;
    }

    function renderEffectsBankSelector() {
        const mod = (root && root.FXRouteOutputState) || null;
        const catalog = (deps.getState().outputSystem || {}).catalog;
        if (!mod || !deps.getElements().effectsBankSelect) return;
        if (!catalog) {
            deps.getElements().effectsBankSelect.innerHTML = '';
            deps.getElements().effectsBankSelect.closest('.effects-bank-row')?.classList.add('hidden');
            return;
        }
        mod.renderBankSelector(deps.getElements().effectsBankSelect, catalog, catalog.active_mode || 'stereo');
        deps.getElements().effectsBankSelect.disabled = !!deps.getState().outputSystem.busy || effectsCompareLoadInFlight;
        deps.renderMeasurementArea();
        renderBankImportTarget();
        syncBankActionButtons();
    }

    function syncBankActionButtons() {
        /* All Banks only switches A/B jointly across the area banks: there is
         * nothing to measure, import or combine there, so those entries stay
         * disabled. */
        const aggregate = deps.measurementArea()?.available === false;
        for (const button of [deps.getElements().effectsMeasureOpenBtn, deps.getElements().effectsToggleImportBtn]) {
            if (!button) continue;
            button.disabled = aggregate;
            button.title = aggregate ? 'All Banks only switches A/B; select a filter bank to measure or import.' : '';
        }
        if (deps.getElements().effectsCombineSaveBtn) {
            renderEffectsCombine();
        }
        if (deps.getElements().effectsRewDualCreatePresetBtn) {
            deps.getElements().effectsRewDualCreatePresetBtn.disabled = aggregate;
        }
        if (deps.getElements().effectsPeqCreatePresetBtn) {
            deps.getElements().effectsPeqCreatePresetBtn.disabled = aggregate;
        }
    }

    function renderBankImportTarget() {
        const area = deps.measurementArea();
        const mono = area?.channel_mode === 'mono';
        const note = document.getElementById('effects-import-bank-target');
        if (note) note.textContent = area?.available === false ? 'Select a filter bank before importing.'
            : `Target: ${area?.label || 'Global'} · ${mono ? 'Mono' : 'Stereo L/R'}`;
        // Mono banks get a single import field: the stereo file area is hidden
        // and the remaining pane accepts every mono-supported format. A stale
        // file selection from the other mode is cleared on switching only, so
        // unrelated re-renders never eat a chosen file.
        if (_bankImportChannelMode !== null && _bankImportChannelMode !== mono) {
            if (deps.getElements().effectsImportFile) deps.getElements().effectsImportFile.value = '';
            if (deps.getElements().effectsRewLeftFile) deps.getElements().effectsRewLeftFile.value = '';
            if (deps.getElements().effectsRewRightFile) deps.getElements().effectsRewRightFile.value = '';
            updateEffectsImportUi();
        }
        _bankImportChannelMode = mono;
        document.getElementById('effects-import-file-head')?.classList.toggle('hidden', mono);
        document.getElementById('effects-import-area')?.classList.toggle('hidden', mono);
        const right = deps.getElements().effectsRewRightText?.closest('.effects-rew-pane');
        right?.classList.toggle('hidden', mono);
        document.querySelector('.effects-rew-dual-grid')?.classList.toggle('is-mono', mono);
        document.getElementById('effects-peq-right-bands')?.closest('.effects-peq-pane')?.classList.toggle('hidden', mono);
        const peqLeftLabel = document.getElementById('effects-peq-left-bands-label');
        if (peqLeftLabel) peqLeftLabel.textContent = mono ? 'Mono bands' : 'Left bands';
        const splitTitle = document.getElementById('effects-import-split-title');
        if (splitTitle) splitTitle.textContent = mono ? 'Mono filter' : 'Left / Right';
        const splitMeta = document.getElementById('effects-import-split-meta');
        if (splitMeta) splitMeta.textContent = mono
            ? 'Mono .irs/.wav, REW text, preset or bundle'
            : 'Mono .irs/.wav or REW .txt per side';
        const leftUploadText = document.querySelector('#effects-rew-left-area .upload-area-text');
        if (leftUploadText) leftUploadText.innerHTML = mono
            ? 'Drop .irs/.wav/.txt/.json/.zip or <u>browse</u>'
            : 'Drop Left .irs/.wav/.txt or <u>browse</u>';
        if (deps.getElements().effectsRewLeftFile) {
            deps.getElements().effectsRewLeftFile.accept = mono
                ? '.irs,.wav,.txt,.json,.zip,text/plain,audio/wav,application/json,application/zip'
                : '.irs,.wav,.txt,text/plain,audio/wav';
        }
        const leftLabel = document.querySelector('label[for="effects-rew-left-text"]');
        if (leftLabel) leftLabel.textContent = mono ? 'Mono filter' : 'Left filter';
        if (deps.getElements().effectsRewLeftText) deps.getElements().effectsRewLeftText.placeholder = mono ? 'Paste mono REW text' : 'Paste Left REW text';
        if (deps.getElements().effectsRewDualCreatePresetBtn) deps.getElements().effectsRewDualCreatePresetBtn.disabled = area?.available === false;
    }

    async function readTextFile(file) {
        return await file.text();
    }

    function getDualFilterFileKind(file) {
        const name = (file?.name || '').toLowerCase();
        if (name.endsWith('.txt')) return 'rew-text';
        if (name.endsWith('.irs') || name.endsWith('.wav')) return 'convolver';
        if (name.endsWith('.json')) return 'preset-json';
        if (name.endsWith('.zip')) return 'preset-bundle';
        return null;
    }

    async function populateDualFilterTextareaFromFile(side, file) {
        if (!file) return;
        const kind = getDualFilterFileKind(file);
        const target = side === 'left' ? deps.getElements().effectsRewLeftText : deps.getElements().effectsRewRightText;
        if (!target) return;
        if (kind !== 'rew-text') {
            target.value = '';
            return;
        }
        try {
            target.value = await readTextFile(file);
        } catch (e) {
            deps.showToast(`Failed to read ${side} filter text file`, 'error');
        }
    }

    async function createDualFilterPreset() {
        if (!requireConcreteFilterBank()) return;
        const mono = deps.measurementBankSumsBothInputs();
        const presetName = deps.getElements().effectsRewDualPresetName?.value?.trim() || '';
        const leftText = deps.getElements().effectsRewLeftText?.value?.trim() || '';
        const rightText = mono ? leftText : deps.getElements().effectsRewRightText?.value?.trim() || '';
        const leftFile = deps.getElements().effectsRewLeftFile?.files?.[0] || null;
        const rightFile = mono ? leftFile : deps.getElements().effectsRewRightFile?.files?.[0] || null;
        const leftFileKind = getDualFilterFileKind(leftFile);
        const rightFileKind = getDualFilterFileKind(rightFile);
        const usingDualFiles = !!leftFile && !!rightFile;
        const usingDualConvolverFiles = usingDualFiles && leftFileKind === 'convolver' && rightFileKind === 'convolver';
        // The single mono field also takes preset files; a stereo bank has a
        // dedicated file area for those, so they do not belong per side.
        const monoSingleKind = mono && leftFile ? leftFileKind : null;
        const monoPresetFile = monoSingleKind === 'preset-json' || monoSingleKind === 'preset-bundle';

        if (!presetName) {
            if (deps.getElements().effectsStatus) deps.getElements().effectsStatus.innerHTML = '<div style="color: var(--danger);">Please enter a preset name.</div>';
            deps.showToast('Please enter a preset name', 'error');
            deps.getElements().effectsRewDualPresetName?.focus();
            return;
        }
        if (!mono && (leftFileKind === 'preset-json' || leftFileKind === 'preset-bundle'
            || rightFileKind === 'preset-json' || rightFileKind === 'preset-bundle')) {
            if (deps.getElements().effectsStatus) deps.getElements().effectsStatus.innerHTML = '<div style="color: var(--danger);">Preset .json/.zip files go in the Stereo file area above.</div>';
            deps.showToast('Preset .json/.zip files go in the Stereo file area above', 'error');
            return;
        }
        if (usingDualFiles && leftFileKind !== rightFileKind) {
            if (deps.getElements().effectsStatus) deps.getElements().effectsStatus.innerHTML = '<div style="color: var(--danger);">Use the same file type on Left and Right.</div>';
            deps.showToast('Use the same file type on Left and Right', 'error');
            return;
        }
        if (!usingDualConvolverFiles && (leftFileKind === 'convolver' || rightFileKind === 'convolver')) {
            if (deps.getElements().effectsStatus) deps.getElements().effectsStatus.innerHTML = '<div style="color: var(--danger);">Provide both Left and Right files.</div>';
            deps.showToast('Provide both Left and Right files', 'error');
            return;
        }
        if (!usingDualConvolverFiles && !leftText && !monoPresetFile) {
            if (deps.getElements().effectsStatus) deps.getElements().effectsStatus.innerHTML = `<div style="color: var(--danger);">Please provide ${mono ? 'a mono filter file or text' : 'Left filter text or file'}.</div>`;
            deps.showToast(mono ? 'Please provide a mono filter file or text' : 'Please provide Left filter text or file', 'error');
            deps.getElements().effectsRewLeftText?.focus();
            return;
        }
        if (!mono && !usingDualConvolverFiles && !rightText) {
            if (deps.getElements().effectsStatus) deps.getElements().effectsStatus.innerHTML = '<div style="color: var(--danger);">Please provide Right filter text or file.</div>';
            deps.showToast('Please provide Right filter text or file', 'error');
            deps.getElements().effectsRewRightText?.focus();
            return;
        }

        if (deps.getElements().effectsRewDualCreatePresetBtn) deps.getElements().effectsRewDualCreatePresetBtn.disabled = true;
        if (deps.getElements().effectsStatus) deps.getElements().effectsStatus.innerHTML = `<div>Creating dual filter preset: <strong>${deps.escapeHtml(presetName)}</strong>…</div>`;
        try {
            const extras = deps.collectEffectsExtras();
            const formData = new FormData();
            formData.append('preset_name', presetName);
            formData.append('left_text', leftText);
            formData.append('right_text', rightText);
            formData.append('load_after_create', 'false');
            formData.append('limiter_enabled', extras.limiterEnabled ? 'true' : 'false');
            formData.append('headroom_enabled', extras.headroomEnabled ? 'true' : 'false');
            formData.append('headroom_gain_db', String(extras.headroomGainDb));
            formData.append('autogain_enabled', extras.autogainEnabled ? 'true' : 'false');
            formData.append('autogain_target_db', String(extras.autogainTargetDb));
            formData.append('bass_enabled', extras.bassEnabled ? 'true' : 'false');
            formData.append('bass_amount', String(extras.bassAmount));
            formData.append('tone_effect_enabled', extras.toneEffectEnabled ? 'true' : 'false');
            formData.append('tone_effect_mode', extras.toneEffectMode);
            let endpoint = '/api/dsp/presets/import-filter-dual';
            if (mono) {
                formData.delete('left_text');
                formData.delete('right_text');
                if (monoPresetFile) {
                    endpoint = monoSingleKind === 'preset-json'
                        ? '/api/dsp/presets/import-json' : '/api/dsp/presets/import-bundle';
                    formData.append('file', leftFile);
                } else {
                    endpoint = usingDualConvolverFiles ? '/api/dsp/presets/create-with-ir' : '/api/dsp/presets/import-rew-peq';
                    formData.append('file', usingDualConvolverFiles ? leftFile
                        : new File([leftText], `${presetName}.txt`, { type: 'text/plain' }));
                }
            } else {
                // REW files populate editable text; send the visible values.
                if (usingDualConvolverFiles) {
                    formData.append('left_file', leftFile);
                    formData.append('right_file', rightFile);
                }
            }
            appendBankBindingFields(formData);

            const resp = await fetch(endpoint, {
                method: 'POST',
                body: formData,
            });
            const data = await resp.json().catch(() => ({}));
            if (!resp.ok) throw new Error(data.detail || 'Dual filter import failed');
            await deps.fetchEffects();
            void deps.refreshCatalog(true);
            if (deps.getElements().effectsRewLeftText) deps.getElements().effectsRewLeftText.value = '';
            if (deps.getElements().effectsRewRightText) deps.getElements().effectsRewRightText.value = '';
            if (deps.getElements().effectsRewLeftFile) deps.getElements().effectsRewLeftFile.value = '';
            if (deps.getElements().effectsRewRightFile) deps.getElements().effectsRewRightFile.value = '';
            const leftFilename = document.getElementById('effects-rew-left-filename');
            const rightFilename = document.getElementById('effects-rew-right-filename');
            if (leftFilename) leftFilename.textContent = '';
            if (rightFilename) rightFilename.textContent = '';
            if (deps.getElements().effectsRewDualPresetName) deps.getElements().effectsRewDualPresetName.value = '';
            const importedKind = !mono && data.import_kind === 'dual-convolver' ? 'Dual convolver'
                : !mono ? 'Dual PEQ'
                : monoSingleKind === 'preset-json' ? 'Preset'
                : monoSingleKind === 'preset-bundle' ? 'Preset bundle' : 'Mono filter';
            if (deps.getElements().effectsStatus) deps.getElements().effectsStatus.innerHTML = '';
            deps.showToast(monoPresetFile
                ? `Imported ${importedKind.toLowerCase()}: ${data.preset.name}`
                : `Created ${importedKind.toLowerCase()} preset: ${data.preset.name}`, 'success');
        } catch (e) {
            if (deps.getElements().effectsStatus) deps.getElements().effectsStatus.innerHTML = `<div style="color: var(--danger);">${deps.escapeHtml(e.message || 'Dual filter import failed')}</div>`;
            deps.showToast(e.message || 'Dual filter import failed', 'error');
        } finally {
            if (deps.getElements().effectsRewDualCreatePresetBtn) deps.getElements().effectsRewDualCreatePresetBtn.disabled = false;
        }
    }

    function getEmptyEffectsCompareState() {
        return { presetA: '', presetB: '', activeSide: null };
    }

    function getEffectsCompareState() {
        const bankState = outputState()?.compareState((deps.getState().outputSystem || {}).catalog);
        if (bankState) return bankState;
        const fx = deps.getState().dsp || {};
        const compare = normalizeEffectsCompareSelection(fx.compare || getEmptyEffectsCompareState());
        const activePreset = fx.active_preset || '';
        const effectiveActiveSide = getEffectiveEffectsCompareSide(compare, activePreset);
        return {
            compare,
            activePreset,
            effectiveActiveSide,
            presetA: compare.presetA || activePreset || '',
            presetB: compare.presetB || '',
        };
    }

    function getEffectiveEffectsCompareSide(compare, activePreset) {
        if (activePreset && compare?.presetA === activePreset) return 'A';
        if (activePreset && compare?.presetB === activePreset) return 'B';
        return compare?.activeSide || null;
    }

    function setEffectsCompareLoadBusy(isBusy) {
        effectsCompareLoadInFlight = !!isBusy;
        const compare = getEffectsCompareState();
        const busy = effectsCompareLoadInFlight || !!deps.getState().outputSystem?.busy;
        if (deps.getElements().effectsCompareA) deps.getElements().effectsCompareA.disabled = busy || !!compare.aggregate;
        if (deps.getElements().effectsCompareB) deps.getElements().effectsCompareB.disabled = busy || !!compare.aggregate;
        const unavailable = !!deps.getState().outputSystem?.catalog && !(compare.effectiveActiveSide === 'A' ? compare.canB : compare.canA);
        if (deps.getElements().effectsCompareToggle) {
            deps.getElements().effectsCompareToggle.disabled = busy || unavailable;
            deps.getElements().effectsCompareToggle.title = unavailable ? (compare.aggregate
                ? 'Assign preset B in every configured bank first.' : 'Select preset B to compare.') : '';
        }
        if (deps.getElements().effectsBankSelect) deps.getElements().effectsBankSelect.disabled = busy;
    }

    function getEffectsChainLabelForPreset(presetName, presetMap = new Map()) {
        if (!presetName) return 'Chain: —';
        const preset = presetMap.get(presetName);
        const sourcePresets = Array.isArray(preset?.source_presets)
            ? preset.source_presets.map(name => String(name || '').trim()).filter(Boolean)
            : [];
        if (sourcePresets.length >= 2) {
            return `Chain: ${sourcePresets.join(' → ')}`;
        }
        return 'Chain: Single preset';
    }

    function getCompactDisplayName(name = '', maxChars = 24) {
        const cleanName = String(name || '').trim();
        if (!cleanName || cleanName.length <= maxChars) return cleanName;
        return `${cleanName.slice(0, Math.max(1, maxChars)).trimEnd()}…`;
    }

    function renderPresetDownloadLink(presetName = '') {
        const cleanName = String(presetName || '').trim();
        if (!cleanName) return '—';
        const displayName = getCompactDisplayName(cleanName, 24);
        return `<a href="${deps.escapeHtml(deps.presetFileUrl(cleanName))}" data-tooltip="${deps.escapeHtml(cleanName)}">${deps.escapeHtml(displayName)}</a>`;
    }

    function renderEffectsCompare() {
        const fx = deps.getState().dsp;
        const presetEntries = fx.presets || [];
        const visibleEntries = deps.getState().outputSystem?.catalog ? visiblePresetEntriesForBank() : presetEntries.slice();
        const presets = visibleEntries.map(p => p.name);
        const presetMap = new Map(presetEntries.map(preset => [preset.name, preset]));
        const { compare, activePreset, effectiveActiveSide, presetA, presetB, aggregate } = getEffectsCompareState();

        if (!deps.getElements().effectsCompareRow) return;
        if (!aggregate && presets.length === 0) {
            deps.getElements().effectsCompareRow.style.display = 'none';
            syncBankActionButtons();
            return;
        }
        deps.getElements().effectsCompareRow.style.display = '';

        // Rebuilding <option> lists on every WS push closes an open dropdown.
        // Only touch the DOM when the values actually changed.
        // All Banks owns no presets: both disabled selects show one aggregate
        // A/B label, and the chain line names the involved area banks below.
        const aggregateBanks = aggregate ? (() => {
            const modeConfig = (deps.getState().outputSystem?.catalog?.modes || {})[deps.getState().outputSystem?.catalog?.active_mode] || {};
            return Object.entries(modeConfig.banks || {}).filter(([id]) => id !== 'global');
        })() : [];
        const aggregateLabel = (id, bank) => bank?.label || outputState()?.roleLabel?.(id) || id;
        const optionsA = aggregate ? '<option>All Banks · A</option>' :
            (!presetA ? '<option value="">Per-channel presets</option>' : '') + presets.map(n => `<option value="${deps.escapeHtml(n)}" ${n === presetA ? 'selected' : ''}>${deps.escapeHtml(n)}</option>`).join('');
        if (deps.getElements().effectsCompareA.innerHTML !== optionsA) {
            deps.getElements().effectsCompareA.innerHTML = optionsA;
        }
        const optionsB = aggregate ? '<option>All Banks · B</option>' : [`<option value="" ${!presetB ? 'selected' : ''}>${!presetB && getEffectsCompareState().canB ? 'Per-channel presets' : 'Select preset…'}</option>`].concat(
                presets.map(n => `<option value="${deps.escapeHtml(n)}" ${n === presetB ? 'selected' : ''}>${deps.escapeHtml(n)}</option>`)
            ).join('');
        if (deps.getElements().effectsCompareB.innerHTML !== optionsB) {
            deps.getElements().effectsCompareB.innerHTML = optionsB;
        }

        let activeLabel = 'Listening: —';
        let chainPresetName = '';
        if (effectiveActiveSide === 'A' && compare.presetA) {
            activeLabel = `Listening: A · ${compare.presetA}`;
            chainPresetName = compare.presetA;
        } else if (effectiveActiveSide === 'B' && compare.presetB) {
            activeLabel = `Listening: B · ${compare.presetB}`;
            chainPresetName = compare.presetB;
        } else if (activePreset) {
            activeLabel = `Listening: ${activePreset}`;
            chainPresetName = activePreset;
        }
        if (deps.getElements().effectsCompareActive) {
            if (effectiveActiveSide === 'A' && compare.presetA) {
                deps.getElements().effectsCompareActive.innerHTML = `Listening: A · ${renderPresetDownloadLink(compare.presetA)}`;
            } else if (effectiveActiveSide === 'B' && compare.presetB) {
                deps.getElements().effectsCompareActive.innerHTML = `Listening: B · ${renderPresetDownloadLink(compare.presetB)}`;
            } else if (activePreset) {
                deps.getElements().effectsCompareActive.innerHTML = `Listening: ${renderPresetDownloadLink(activePreset)}`;
            } else {
                deps.getElements().effectsCompareActive.textContent = activeLabel;
            }
        }
        if (deps.getElements().effectsCompareChain) {
            const chainLabel = getEffectsChainLabelForPreset(chainPresetName, presetMap);
            deps.getElements().effectsCompareChain.textContent = chainLabel;
        }
        if (aggregate) {
            if (deps.getElements().effectsCompareActive) deps.getElements().effectsCompareActive.textContent = effectiveActiveSide
                ? `Listening: ${effectiveActiveSide} · All Banks` : 'Listening: Mixed A/B';
            if (deps.getElements().effectsCompareChain) deps.getElements().effectsCompareChain.textContent =
                aggregateBanks.map(([id, bank]) => aggregateLabel(id, bank)).join(' · ') || 'No area banks';
        }
        if (deps.getElements().effectsCompareToggle) deps.getElements().effectsCompareToggle.textContent = aggregate
            ? `Switch all to ${effectiveActiveSide === 'A' ? 'B' : 'A'}` : 'Compare A/B';
        deps.getElements().effectsDeleteBtn?.classList.toggle('hidden', !!aggregate);
        const badge = document.getElementById('effects-compare-active-badge');
        if (badge) {
            badge.classList.toggle('is-side-a', effectiveActiveSide === 'A');
            badge.classList.toggle('is-side-b', effectiveActiveSide === 'B');
        }
        document.querySelectorAll('.effects-compare-slot').forEach((slotEl) => {
            const slot = slotEl.dataset.compareSlot;
            const slotPreset = slot === 'A' ? presetA : presetB;
            slotEl.classList.toggle('is-active', effectiveActiveSide === slot && (!!slotPreset || aggregate));
            slotEl.classList.toggle('is-armed', effectiveActiveSide !== slot && !!slotPreset);
        });
        renderEffectsBankSelector();
        setEffectsCompareLoadBusy(effectsCompareLoadInFlight);
    }

    function getEffectsCombineValidationState() {
        const preset1 = deps.getElements().effectsCombinePreset1?.value || '';
        const preset2 = deps.getElements().effectsCombinePreset2?.value || '';
        const preset3 = deps.getElements().effectsCombinePreset3?.value || '';
        const presetName = deps.getElements().effectsCombinePresetName?.value?.trim() || '';
        const selectedPresets = [preset1, preset2, preset3].filter(Boolean);
        const isDuplicateSelection = new Set(selectedPresets).size !== selectedPresets.length;
        const catalog = deps.getState().outputSystem?.catalog;
        const aggregate = catalog ? catalog.modes?.[catalog.active_mode]?.selected_bank === 'all' : false;
        let crossBank = false;
        if (catalog && !aggregate && selectedPresets.length) {
            const visible = new Set(visiblePresetNamesForBank());
            crossBank = selectedPresets.some((name) => !visible.has(name));
        }
        return {
            preset1,
            preset2,
            preset3,
            presetName,
            selectedPresets,
            aggregate,
            crossBank,
            isValid: selectedPresets.length >= 2 && !!presetName && !isDuplicateSelection && !aggregate && !crossBank,
            isDuplicateSelection,
        };
    }

    function renderEffectsCombine() {
        const fx = deps.getState().dsp || {};
        const allNames = (fx.presets || []).map(p => p.name);
        const presets = deps.getState().outputSystem?.catalog ? visiblePresetNamesForBank() : allNames;
        const draft = fx.combineDraft || getDefaultEffectsCombineDraft();
        if (!deps.getElements().effectsCombinePreset1 || !deps.getElements().effectsCombinePreset2 || !deps.getElements().effectsCombinePreset3 || !deps.getElements().effectsCombinePresetName) return;

        const normalized = normalizeEffectsCombineDraft(draft, allNames);
        fx.combineDraft = normalized;

        // Rebuilding <option> lists on every WS push closes an open dropdown.
        // Only touch the DOM when the values actually changed.
        const options1 = [`<option value="" ${!normalized.preset1 ? 'selected' : ''}>Select preset…</option>`].concat(
            presets.map(n => `<option value="${deps.escapeHtml(n)}" ${n === normalized.preset1 ? 'selected' : ''}>${deps.escapeHtml(n)}</option>`)
        ).join('');
        if (deps.getElements().effectsCombinePreset1.innerHTML !== options1) {
            deps.getElements().effectsCombinePreset1.innerHTML = options1;
        }
        const options2 = [`<option value="" ${!normalized.preset2 ? 'selected' : ''}>Select preset…</option>`].concat(
            presets.map(n => `<option value="${deps.escapeHtml(n)}" ${n === normalized.preset2 ? 'selected' : ''}>${deps.escapeHtml(n)}</option>`)
        ).join('');
        if (deps.getElements().effectsCombinePreset2.innerHTML !== options2) {
            deps.getElements().effectsCombinePreset2.innerHTML = options2;
        }
        const options3 = [`<option value="" ${!normalized.preset3 ? 'selected' : ''}>Optional…</option>`].concat(
            presets.map(n => `<option value="${deps.escapeHtml(n)}" ${n === normalized.preset3 ? 'selected' : ''}>${deps.escapeHtml(n)}</option>`)
        ).join('');
        if (deps.getElements().effectsCombinePreset3.innerHTML !== options3) {
            deps.getElements().effectsCombinePreset3.innerHTML = options3;
        }
        if (document.activeElement !== deps.getElements().effectsCombinePresetName) {
            deps.getElements().effectsCombinePresetName.value = normalized.presetName || '';
        }

        const validation = getEffectsCombineValidationState();
        if (deps.getElements().effectsCombineSaveBtn) {
            deps.getElements().effectsCombineSaveBtn.disabled = !fx.available || !validation.isValid;
            if (validation.aggregate) {
                deps.getElements().effectsCombineSaveBtn.title = 'All Banks owns no presets; select a concrete filterbank.';
            } else if (validation.crossBank) {
                deps.getElements().effectsCombineSaveBtn.title = 'Combine only presets from the selected bank.';
            } else {
                deps.getElements().effectsCombineSaveBtn.title = '';
            }
        }
    }

    async function createCombinedEffectsPreset() {
        const validation = getEffectsCombineValidationState();
        if (validation.aggregate) {
            if (deps.getElements().effectsStatus) deps.getElements().effectsStatus.innerHTML = '<div style="color: var(--danger);">All Banks owns no presets; select a concrete filterbank.</div>';
            deps.showToast('All Banks owns no presets; select a concrete filterbank', 'error');
            return;
        }
        if (validation.crossBank) {
            if (deps.getElements().effectsStatus) deps.getElements().effectsStatus.innerHTML = '<div style="color: var(--danger);">Combine only presets from the selected bank.</div>';
            deps.showToast('Combine only presets from the selected bank', 'error');
            return;
        }
        if (validation.isDuplicateSelection) {
            if (deps.getElements().effectsStatus) deps.getElements().effectsStatus.innerHTML = '<div style="color: var(--danger);">Each selected preset must be different.</div>';
            deps.showToast('Each selected preset must be different', 'error');
            return;
        }
        if (validation.selectedPresets.length < 2) {
            if (deps.getElements().effectsStatus) deps.getElements().effectsStatus.innerHTML = '<div style="color: var(--danger);">Choose at least two presets to combine.</div>';
            deps.showToast('Choose at least two presets to combine', 'error');
            return;
        }
        if (!validation.presetName) {
            if (deps.getElements().effectsStatus) deps.getElements().effectsStatus.innerHTML = '<div style="color: var(--danger);">Please enter a new preset name.</div>';
            deps.showToast('Please enter a new preset name', 'error');
            deps.getElements().effectsCombinePresetName?.focus();
            return;
        }

        if (deps.getElements().effectsCombineSaveBtn) deps.getElements().effectsCombineSaveBtn.disabled = true;
        if (deps.getElements().effectsStatus) deps.getElements().effectsStatus.innerHTML = `<div>Saving combined preset: <strong>${deps.escapeHtml(validation.presetName)}</strong>…</div>`;
        try {
            const combineBank = outputSystemCombineBank();
            const resp = await fetch('/api/dsp/presets/combine', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    presetName: validation.presetName,
                    presetNames: validation.selectedPresets,
                    ...(combineBank || {}),
                }),
            });
            const data = await resp.json().catch(() => ({}));
            if (!resp.ok) throw new Error(data.detail || 'Combined preset save failed');
            deps.getState().dsp.combineDraft = getDefaultEffectsCombineDraft();
            await deps.fetchEffects();
            if (deps.getElements().effectsStatus) deps.getElements().effectsStatus.innerHTML = '';
            deps.showToast(`Created combined preset: ${data.preset.name}`, 'success');
        } catch (e) {
            if (deps.getElements().effectsStatus) deps.getElements().effectsStatus.innerHTML = `<div style="color: var(--danger);">${deps.escapeHtml(e.message || 'Combined preset save failed')}</div>`;
            deps.showToast(e.message || 'Combined preset save failed', 'error');
        } finally {
            renderEffectsCombine();
        }
    }

    async function loadEffectsComparePreset(target, newSide, targetA, targetB) {
        if (effectsCompareLoadInFlight) return;
        setEffectsCompareLoadBusy(true);
        try {
            const catalog = deps.getState().outputSystem?.catalog;
            if (catalog) {
                const bankId = catalog.modes[catalog.active_mode].selected_bank;
                await deps.applyMutation('set_bank_preset', {
                    mode: catalog.active_mode, bank_id: bankId, active_side: newSide,
                }, false);
                renderEffectsCompare();
                return;
            }
            const resp = await fetch('/api/dsp/presets/load', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ preset_name: target }),
            });
            const data = await resp.json();
            if (!resp.ok) throw new Error(data.detail || 'Failed to load preset');
            deps.getState().dsp.active_preset = target;
            deps.getState().dsp.compare = {
                presetA: targetA,
                presetB: targetB,
                activeSide: newSide,
            };
            await saveEffectsCompareState(deps.getState().dsp.compare);
            deps.renderEffects();
        } finally {
            setEffectsCompareLoadBusy(false);
        }
    }

    async function handleEffectsCompareSelectionChange(slot) {
        if (effectsCompareLoadInFlight) return;
        const catalog = deps.getState().outputSystem?.catalog;
        if (catalog) {
            const current = getEffectsCompareState();
            if (current.aggregate) return;
            const selectedValue = (slot === 'A' ? deps.getElements().effectsCompareA?.value : deps.getElements().effectsCompareB?.value) || null;
            if (selectedValue && selectedValue === (slot === 'A' ? current.presetB : current.presetA)) {
                deps.showToast('A and B must use different presets', 'warning');
                renderEffectsCompare();
                return;
            }
            const fields = { mode: catalog.active_mode, bank_id: catalog.modes[catalog.active_mode].selected_bank,
                [slot === 'A' ? 'preset_a' : 'preset_b']: selectedValue };
            if (selectedValue && (!current.effectiveActiveSide || current.effectiveActiveSide === slot)) fields.active_side = slot;
            if (!selectedValue && slot === 'B' && current.effectiveActiveSide === 'B') fields.active_side = 'A';
            await deps.applyMutation('set_bank_preset', fields, false);
            renderEffectsCompare();
            return;
        }
        deps.getState().dsp.compare = normalizeEffectsCompareSelection(deps.getState().dsp.compare || getEmptyEffectsCompareState());

        const previousCompare = {
            presetA: deps.getState().dsp.compare.presetA || '',
            presetB: deps.getState().dsp.compare.presetB || '',
            activeSide: deps.getState().dsp.compare.activeSide || null,
        };
        const activePreset = deps.getState().dsp?.active_preset || '';
        const effectiveActiveSide = getEffectiveEffectsCompareSide(previousCompare, activePreset);

        let targetA = deps.getElements().effectsCompareA?.value || '';
        let targetB = deps.getElements().effectsCompareB?.value || '';
        if (targetA && targetB && targetA === targetB) {
            if (slot === 'B') {
                targetB = '';
                if (deps.getElements().effectsCompareB) deps.getElements().effectsCompareB.value = '';
                deps.showToast('A and B must use different presets', 'warning');
            } else {
                targetB = '';
            }
        }
        deps.getState().dsp.compare = normalizeEffectsCompareSelection({
            presetA: targetA,
            presetB: targetB,
            activeSide: deps.getState().dsp.compare.activeSide || null,
        });
        await saveEffectsCompareState(deps.getState().dsp.compare);

        const selectedValue = slot === 'A' ? targetA : targetB;
        const shouldAutoload = !!selectedValue && selectedValue !== activePreset && (!effectiveActiveSide || effectiveActiveSide === slot);

        if (shouldAutoload) {
            await loadEffectsComparePreset(selectedValue, slot, deps.getState().dsp.compare.presetA, deps.getState().dsp.compare.presetB);
        } else {
            renderEffectsCompare();
        }
    }

    function getEffectsCompareToggleTarget({ effectiveActiveSide, activePreset, presetA, presetB }) {
        if (effectiveActiveSide === 'A' && presetB) return { target: presetB, side: 'B' };
        if (effectiveActiveSide === 'B' && presetA) return { target: presetA, side: 'A' };
        if (presetB && presetA === activePreset) return { target: presetB, side: 'B' };
        if (presetA) return { target: presetA, side: 'A' };
        if (presetB) return { target: presetB, side: 'B' };
        return { target: null, side: null };
    }

    async function toggleComparePreset() {
        if (effectsCompareLoadInFlight) return;
        try {
            const compareState = getEffectsCompareState();
            if (compareState.aggregate) {
                const side = compareState.effectiveActiveSide === 'A' ? 'B' : 'A';
                if (!(side === 'B' ? compareState.canB : compareState.canA)) {
                    deps.showToast('Assign preset B in every configured bank first.', 'warning');
                    return;
                }
                await deps.applyMutation('switch_all_banks', {
                    mode: deps.getState().outputSystem.catalog.active_mode, active_side: side,
                }, false);
                renderEffectsCompare();
                return;
            }
            if (deps.getState().outputSystem?.catalog) {
                const side = compareState.effectiveActiveSide === 'A' ? 'B' : 'A';
                if (!(side === 'B' ? compareState.canB : compareState.canA)) return;
                await loadEffectsComparePreset(null, side, compareState.presetA, compareState.presetB);
                return;
            }
            if (!compareState.presetA && !compareState.presetB) {
                deps.showToast('Select a preset in A or B first', 'warning');
                return;
            }

            const { target, side } = getEffectsCompareToggleTarget(compareState);
            if (!target || !side) {
                deps.showToast('Select a preset in A or B first', 'warning');
                return;
            }

            await loadEffectsComparePreset(target, side, compareState.presetA, compareState.presetB);
        } catch (e) {
            console.error('toggleComparePreset error:', e);
            deps.showToast(e.message || 'Failed to toggle preset', 'error');
        }
    }

    function setupEffectsCompareActions() {
        // Idempotent: setupEffectsActions() reaches this through wireBankUi(),
        // so a second call must not stack duplicate Compare/A/B listeners
        // (each would fire a second identical POST per change).
        if (effectsCompareActionsWired) return;
        effectsCompareActionsWired = true;
        if (deps.getElements().effectsCompareToggle) {
            deps.getElements().effectsCompareToggle.addEventListener('click', toggleComparePreset);
        }
        if (deps.getElements().effectsCompareA) {
            deps.getElements().effectsCompareA.addEventListener('change', async () => {
                try {
                    await handleEffectsCompareSelectionChange('A');
                } catch (e) {
                    console.error('compare preset A change error:', e);
                    deps.showToast(e.message || 'Failed to load preset', 'error');
                }
            });
        }
        if (deps.getElements().effectsCompareB) {
            deps.getElements().effectsCompareB.addEventListener('change', async () => {
                try {
                    await handleEffectsCompareSelectionChange('B');
                } catch (e) {
                    console.error('compare preset B change error:', e);
                    deps.showToast(e.message || 'Failed to load preset', 'error');
                }
            });
        }
    }

    function detectEffectsImportType(file) {
        if (!file || !file.name) return null;
        const lowerName = file.name.toLowerCase();
        if (lowerName.endsWith('.irs') || lowerName.endsWith('.wav')) return 'convolver';
        if (lowerName.endsWith('.json')) return 'preset-json';
        if (lowerName.endsWith('.zip')) return 'preset-bundle';
        return null;
    }

    function updateEffectsImportUi() {
        const file = deps.getElements().effectsImportFile?.files?.[0] || null;
        const detectedType = detectEffectsImportType(file);
        if (deps.getElements().effectsImportFile) {
            deps.getElements().effectsImportFile.accept = '.irs,.wav,.json,.zip,audio/wav,application/json,application/zip';
        }
        if (deps.getElements().effectsImportFilename) {
            if (!file) {
                deps.getElements().effectsImportFilename.textContent = 'Stereo .irs/.wav IR · preset .json · bundle .zip';
            } else if (detectedType === 'convolver' || detectedType === 'preset-json' || detectedType === 'preset-bundle') {
                deps.getElements().effectsImportFilename.textContent = file.name;
            } else {
                deps.getElements().effectsImportFilename.textContent = `Unsupported file: ${file.name}`;
            }
        }
        const importArea = document.getElementById('effects-import-area');
        if (importArea) {
            importArea.classList.toggle('is-ready', detectedType === 'convolver' || detectedType === 'preset-json' || detectedType === 'preset-bundle');
        }
    }

    function handleEffectsImportFileChange() {
        updateEffectsImportUi();
        const file = deps.getElements().effectsImportFile?.files?.[0] || null;
        if (detectEffectsImportType(file)) {
            void submitEffectsImport();
        }
    }

    async function submitEffectsImport() {
        if (!requireConcreteFilterBank()) return;
        const file = deps.getElements().effectsImportFile?.files?.[0];
        const detectedType = detectEffectsImportType(file);
        if (!file) {
            deps.getElements().effectsStatus.innerHTML = '<div style="color: var(--danger);">Please choose an import file first.</div>';
            deps.showToast('Please choose an import file first', 'error');
            return;
        }
        if (detectedType === 'convolver') {
            return createConvolverPreset();
        }
        if (detectedType === 'preset-json') {
            return importEffectsPresetJson();
        }
        if (detectedType === 'preset-bundle') {
            return importEffectsPresetBundle();
        }
        deps.getElements().effectsStatus.innerHTML = '<div style="color: var(--danger);">Unsupported import file type. Use .irs, .wav, preset .json, or bundle .zip.</div>';
        deps.showToast('Unsupported import file type', 'error');
    }

    async function importEffectsPresetJson() {
        if (!requireConcreteFilterBank()) return;
        if (effectsImportInFlight) {
            deps.showToast('Import already in progress', 'warning');
            return;
        }
        effectsImportInFlight = true;
        const file = deps.getElements().effectsImportFile?.files?.[0] || null;
        if (!file) {
            effectsImportInFlight = false;
            deps.showToast('Please select a preset JSON file first', 'error');
            return;
        }
        const formData = new FormData();
        formData.append('load_after_create', 'false');
        formData.append('file', file);
        appendBankBindingFields(formData);
        if (deps.getElements().effectsStatus) deps.getElements().effectsStatus.innerHTML = `<div>Importing preset: <strong>${deps.escapeHtml(file.name)}</strong>…</div>`;
        const importArea = document.getElementById('effects-import-area');
        if (importArea) importArea.classList.add('is-busy');
        try {
            const resp = await fetch('/api/dsp/presets/import-json', {
                method: 'POST',
                body: formData,
            });
            const data = await resp.json().catch(() => ({}));
            if (!resp.ok) throw new Error(data.detail || 'Preset JSON import failed');
            await deps.fetchEffects();
            if (deps.getElements().effectsImportFile) deps.getElements().effectsImportFile.value = '';
            updateEffectsImportUi();
            if (deps.getElements().effectsStatus) deps.getElements().effectsStatus.innerHTML = '';
            deps.showToast(`Imported preset: ${data.preset?.name || file.name}`, 'success');
        } catch (e) {
            if (deps.getElements().effectsStatus) deps.getElements().effectsStatus.innerHTML = `<div style="color: var(--danger);">${deps.escapeHtml(e.message)}</div>`;
            deps.showToast(e.message || 'Preset JSON import failed', 'error');
        } finally {
            if (importArea) importArea.classList.remove('is-busy');
            effectsImportInFlight = false;
        }
    }

    async function importEffectsPresetBundle() {
        if (!requireConcreteFilterBank()) return;
        if (effectsImportInFlight) {
            deps.showToast('Import already in progress', 'warning');
            return;
        }
        effectsImportInFlight = true;
        const file = deps.getElements().effectsImportFile?.files?.[0] || null;
        if (!file) {
            effectsImportInFlight = false;
            deps.showToast('Please select a preset bundle first', 'error');
            return;
        }
        const formData = new FormData();
        formData.append('load_after_create', 'false');
        formData.append('file', file);
        appendBankBindingFields(formData);
        if (deps.getElements().effectsStatus) deps.getElements().effectsStatus.innerHTML = `<div>Importing bundle: <strong>${deps.escapeHtml(file.name)}</strong>…</div>`;
        const importArea = document.getElementById('effects-import-area');
        if (importArea) importArea.classList.add('is-busy');
        try {
            const resp = await fetch('/api/dsp/presets/import-bundle', {
                method: 'POST',
                body: formData,
            });
            const data = await resp.json().catch(() => ({}));
            if (!resp.ok) throw new Error(data.detail || 'Preset bundle import failed');
            await deps.fetchEffects();
            if (deps.getElements().effectsImportFile) deps.getElements().effectsImportFile.value = '';
            updateEffectsImportUi();
            if (deps.getElements().effectsStatus) deps.getElements().effectsStatus.innerHTML = '';
            const irCount = Array.isArray(data.irs) ? data.irs.length : 0;
            deps.showToast(`Imported preset bundle: ${data.preset?.name || file.name}${irCount ? ` (${irCount} IR)` : ''}`, 'success');
        } catch (e) {
            if (deps.getElements().effectsStatus) deps.getElements().effectsStatus.innerHTML = `<div style="color: var(--danger);">${deps.escapeHtml(e.message)}</div>`;
            deps.showToast(e.message || 'Preset bundle import failed', 'error');
        } finally {
            if (importArea) importArea.classList.remove('is-busy');
            effectsImportInFlight = false;
        }
    }

    async function createConvolverPreset() {
        if (!requireConcreteFilterBank()) return;
        if (effectsImportInFlight) {
            deps.showToast('Import already in progress', 'warning');
            return;
        }
        effectsImportInFlight = true;
        const file = deps.getElements().effectsImportFile.files[0];
        const presetName = file ? file.name.replace(/\.[^.]+$/, '') : '';
        if (!file) {
            effectsImportInFlight = false;
            deps.showToast('Please select a stereo IR file first', 'error');
            return;
        }
        const extras = deps.collectEffectsExtras();
        const formData = new FormData();
        formData.append('preset_name', presetName);
        formData.append('load_after_create', 'false');
        formData.append('limiter_enabled', extras.limiterEnabled ? 'true' : 'false');
        formData.append('headroom_enabled', extras.headroomEnabled ? 'true' : 'false');
        formData.append('headroom_gain_db', String(extras.headroomGainDb));
        formData.append('autogain_enabled', extras.autogainEnabled ? 'true' : 'false');
        formData.append('autogain_target_db', String(extras.autogainTargetDb));
        formData.append('bass_enabled', extras.bassEnabled ? 'true' : 'false');
        formData.append('bass_amount', String(extras.bassAmount));
        formData.append('tone_effect_enabled', extras.toneEffectEnabled ? 'true' : 'false');
        formData.append('tone_effect_mode', extras.toneEffectMode);
        formData.append('file', file);
        appendBankBindingFields(formData);
        if (deps.getElements().effectsStatus) deps.getElements().effectsStatus.innerHTML = `<div>Importing: <strong>${deps.escapeHtml(presetName)}</strong>…</div>`;
        const importArea = document.getElementById('effects-import-area');
        if (importArea) importArea.classList.add('is-busy');
        try {
            const resp = await fetch('/api/dsp/presets/create-with-ir', {
                method: 'POST',
                body: formData,
            });
            const data = await resp.json();
            if (!resp.ok) throw new Error(formatError(data.detail, 'Preset creation failed'));
            await deps.fetchEffects();
            void deps.refreshCatalog(true);
            deps.getElements().effectsImportFile.value = '';
            updateEffectsImportUi();
            if (deps.getElements().effectsStatus) deps.getElements().effectsStatus.innerHTML = '';
            deps.showToast(`Imported preset: ${data.preset.name}`, 'success');
        } catch (e) {
            if (deps.getElements().effectsStatus) deps.getElements().effectsStatus.innerHTML = `<div style="color: var(--danger);">${deps.escapeHtml(e.message)}</div>`;
            deps.showToast(e.message || 'Preset creation failed', 'error');
        } finally {
            if (importArea) importArea.classList.remove('is-busy');
            effectsImportInFlight = false;
        }
    }

    async function deleteEffectsPreset() {
        const presetName = getEffectsCompareState().activePreset;
        if (!presetName) {
            deps.showToast('No active preset to delete', 'warning');
            return;
        }
        if (presetName === 'Direct' || presetName === 'Neutral') {
            deps.showToast(`Preset "${presetName}" is built-in and cannot be deleted`, 'error');
            return;
        }
        if (!deps.confirmDialog(`Delete preset "${presetName}"?`)) return;
        deps.getElements().effectsDeleteBtn.disabled = true;
        try {
            const resp = await fetch('/api/dsp/presets/delete', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ preset_name: presetName }),
            });
            const data = await resp.json();
            if (!resp.ok) throw new Error(data.detail || 'Preset delete failed');
            await deps.fetchEffects();
            void deps.refreshCatalog(true);
            deps.showToast(`Deleted preset: ${presetName}`, 'success');
        } catch (e) {
            deps.showToast(e.message || 'Preset delete failed', 'error');
        } finally {
            deps.getElements().effectsDeleteBtn.disabled = false;
        }
    }

    async function importRewPeqPreset() {
        if (!requireConcreteFilterBank()) return;
        const file = deps.getElements().effectsImportFile?.files?.[0];
        const presetName = file ? file.name.replace(/\.[^.]+$/, '') : '';
        if (!file) {
            if (deps.getElements().effectsStatus) deps.getElements().effectsStatus.innerHTML = '<div style="color: var(--danger);">Please choose a REW text file.</div>';
            deps.showToast('Please choose a REW text file', 'error');
            return;
        }
        if (!file) {
            if (deps.getElements().effectsStatus) deps.getElements().effectsStatus.innerHTML = '<div style="color: var(--danger);">Please choose a REW text file.</div>';
            deps.showToast('Please choose a REW text file', 'error');
            return;
        }
        const extras = deps.collectEffectsExtras();
        const formData = new FormData();
        formData.append('preset_name', presetName);
        formData.append('load_after_create', 'false');
        formData.append('limiter_enabled', extras.limiterEnabled ? 'true' : 'false');
        formData.append('headroom_enabled', extras.headroomEnabled ? 'true' : 'false');
        formData.append('headroom_gain_db', String(extras.headroomGainDb));
        formData.append('autogain_enabled', extras.autogainEnabled ? 'true' : 'false');
        formData.append('autogain_target_db', String(extras.autogainTargetDb));
        formData.append('bass_enabled', extras.bassEnabled ? 'true' : 'false');
        formData.append('bass_amount', String(extras.bassAmount));
        formData.append('tone_effect_enabled', extras.toneEffectEnabled ? 'true' : 'false');
        formData.append('tone_effect_mode', extras.toneEffectMode);
        formData.append('file', file);
        appendBankBindingFields(formData);
        if (deps.getElements().effectsStatus) deps.getElements().effectsStatus.innerHTML = `<div>Importing REW PEQ: <strong>${deps.escapeHtml(presetName)}</strong>…</div>`;
        try {
            const resp = await fetch('/api/dsp/presets/import-rew-peq', {
                method: 'POST',
                body: formData,
            });
            const data = await resp.json().catch(() => ({}));
            if (!resp.ok) throw new Error(data.detail || 'REW PEQ import failed');
            await deps.fetchEffects();
            void deps.refreshCatalog(true);
            deps.getElements().effectsImportFile.value = '';
            updateEffectsImportUi();
            if (deps.getElements().effectsStatus) deps.getElements().effectsStatus.innerHTML = '';
            deps.showToast(`Imported REW PEQ: ${data.preset.name}`, 'success');
        } catch (e) {
            deps.getElements().effectsStatus.innerHTML = `<div style="color: var(--danger);">REW PEQ import failed: ${deps.escapeHtml(e.message)}</div>`;
            deps.showToast(e.message || 'REW PEQ import failed', 'error');
        }
    }

        function wireBankUi() {
            // Bank/compare/import control listeners (moved verbatim from
            // setupEffectsActions): preset delete, import panel toggle, stereo
            // import file, dual-filter create, and the combine draft inputs.
            // Compare dropdowns bind through setupEffectsCompareActions below.
            if (deps.getElements().effectsDeleteBtn) deps.getElements().effectsDeleteBtn.addEventListener('click', deleteEffectsPreset);
            if (deps.getElements().effectsToggleImportBtn) {
                deps.getElements().effectsToggleImportBtn.addEventListener('click', () => {
                    const shouldOpen = deps.getElements().effectsImportPanel?.classList.contains('hidden');
                    setEffectsImportPanelOpen(!!shouldOpen);
                });
            }
            if (deps.getElements().effectsImportFile) deps.getElements().effectsImportFile.addEventListener('change', handleEffectsImportFileChange);
            if (deps.getElements().effectsRewDualCreatePresetBtn) deps.getElements().effectsRewDualCreatePresetBtn.addEventListener('click', createDualFilterPreset);
            if (deps.getElements().effectsCombinePreset1) {
                deps.getElements().effectsCombinePreset1.addEventListener('change', (event) => {
                    deps.getState().dsp.combineDraft = deps.getState().dsp.combineDraft || getDefaultEffectsCombineDraft();
                    deps.getState().dsp.combineDraft.preset1 = event.target.value;
                    if (deps.getState().dsp.combineDraft.preset1 && deps.getState().dsp.combineDraft.preset1 === deps.getState().dsp.combineDraft.preset2) {
                        deps.getState().dsp.combineDraft.preset2 = '';
                        if (deps.getElements().effectsCombinePreset2) deps.getElements().effectsCombinePreset2.value = '';
                    }
                    renderEffectsCombine();
                });
            }
            if (deps.getElements().effectsCombinePreset2) {
                deps.getElements().effectsCombinePreset2.addEventListener('change', (event) => {
                    deps.getState().dsp.combineDraft = deps.getState().dsp.combineDraft || getDefaultEffectsCombineDraft();
                    deps.getState().dsp.combineDraft.preset2 = event.target.value;
                    renderEffectsCombine();
                });
            }
            if (deps.getElements().effectsCombinePreset3) {
                deps.getElements().effectsCombinePreset3.addEventListener('change', (event) => {
                    deps.getState().dsp.combineDraft = deps.getState().dsp.combineDraft || getDefaultEffectsCombineDraft();
                    deps.getState().dsp.combineDraft.preset3 = event.target.value;
                    renderEffectsCombine();
                });
            }
            if (deps.getElements().effectsCombinePresetName) {
                deps.getElements().effectsCombinePresetName.addEventListener('input', (event) => {
                    deps.getState().dsp.combineDraft = deps.getState().dsp.combineDraft || getDefaultEffectsCombineDraft();
                    deps.getState().dsp.combineDraft.presetName = event.target.value;
                    renderEffectsCombine();
                });
            }
            if (deps.getElements().effectsCombineSaveBtn) deps.getElements().effectsCombineSaveBtn.addEventListener('click', createCombinedEffectsPreset);
            setupEffectsCompareActions();
        }

    function isCompareLoadBusy() {
        return effectsCompareLoadInFlight;
    }

    return {
        init,
        normalizeEffectsCompareSelection,
        resolveEffectsCompareState,
        saveEffectsCompareState,
        getDefaultEffectsCombineDraft,
        normalizeEffectsCombineDraft,
        setEffectsImportPanelOpen,
        outputSystemBankBinding,
        outputSystemCombineBank,
        visiblePresetEntriesForBank,
        visiblePresetNamesForBank,
        appendBankBindingFields,
        bankBindingJson,
        measurementPeqParams,
        requireConcreteFilterBank,
        renderEffectsBankSelector,
        syncBankActionButtons,
        renderBankImportTarget,
        readTextFile,
        getDualFilterFileKind,
        populateDualFilterTextareaFromFile,
        createDualFilterPreset,
        getEmptyEffectsCompareState,
        getEffectsCompareState,
        getEffectiveEffectsCompareSide,
        setEffectsCompareLoadBusy,
        getEffectsChainLabelForPreset,
        getCompactDisplayName,
        renderPresetDownloadLink,
        renderEffectsCompare,
        getEffectsCombineValidationState,
        renderEffectsCombine,
        createCombinedEffectsPreset,
        loadEffectsComparePreset,
        handleEffectsCompareSelectionChange,
        getEffectsCompareToggleTarget,
        toggleComparePreset,
        setupEffectsCompareActions,
        detectEffectsImportType,
        updateEffectsImportUi,
        handleEffectsImportFileChange,
        submitEffectsImport,
        importEffectsPresetJson,
        importEffectsPresetBundle,
        createConvolverPreset,
        deleteEffectsPreset,
        importRewPeqPreset,
        wireBankUi,
        isCompareLoadBusy,
    };
});
