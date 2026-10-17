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
    };

    function init(overrides) {
        deps = Object.assign(deps, overrides || {});
    }

    function outputState() {
        return (root && root.FXRouteOutputState) || null;
    }

    // Easy/Expert view of the DSP tab, kept per browser like the grid/list
    // views. Easy (Global only) is the default without a stored choice.
    const EXPERT_VIEW_STORAGE_KEY = 'fx-dsp-expert';

    function readStoredExpertView() {
        try {
            return localStorage.getItem(EXPERT_VIEW_STORAGE_KEY) === '1';
        } catch (e) {
            return false;
        }
    }

    // The switch changes the view only: the output state is not written, the
    // catalog is read again through the other view.
    async function setExpertView(expert) {
        const system = deps.getState().outputSystem;
        if (!system || !!system.expert === !!expert) return;
        system.expert = !!expert;
        try {
            localStorage.setItem(EXPERT_VIEW_STORAGE_KEY, expert ? '1' : '0');
        } catch (e) {
            // Storage unavailable: the choice lasts for this page only.
        }
        await deps.refreshCatalog(true);
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
    let effectsDeleteDraft = null;
    let effectsDeleteInFlight = false;

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
            return outputState()?.bankBinding(catalog, deps.getState().measurement?.globalGroup) || null;
        } catch (e) {
            return null;
        }
    }

    function outputSystemBankOwner() {
        const catalog = (deps.getState().outputSystem || {}).catalog;
        if (!catalog) return null;
        try {
            return outputState()?.bankOwner(catalog) || null;
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
        // Strict per-bank stock: only the bank's own presets plus the
        // built-ins. No foreign presets are merged in, so switching banks can
        // never leak another bank's stock.
        return mod.presetsForBank(entries, bankId, catalog.active_mode);
    }

    function visiblePresetNamesForBank() {
        return visiblePresetEntriesForBank().map((entry) => entry?.name).filter(Boolean);
    }

    function appendBankBindingFields(formData) {
        const binding = outputSystemBankBinding();
        if (!binding || !formData || typeof formData.append !== 'function') return false;
        formData.append('bank_mode', binding.bank_mode);
        formData.append('bank_id', binding.bank_id);
        if (binding.bank_group) formData.append('bank_group', binding.bank_group);
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
        // Importing or creating a filter needs a bank to commit into.
        const area = deps.measurementArea();
        if (area?.available !== false && area?.importable !== false) return true;
        deps.showToast('Select a filter bank for import or measurement.', 'warning');
        return false;
    }

    function requireMeasurableArea() {
        // A sweep only needs an area to measure; Ind. Banks measures the whole system.
        if (deps.measurementArea()?.available !== false) return true;
        deps.showToast('Select a filter bank for measurement.', 'warning');
        return false;
    }

    function renderEffectsBankSelector() {
        const mod = (root && root.FXRouteOutputState) || null;
        const catalog = (deps.getState().outputSystem || {}).catalog;
        if (!mod || !deps.getElements().effectsBankSelect) return;
        const expert = !!deps.getState().outputSystem?.expert;
        // The Expert switch is offered wherever a bank selection exists.
        const expertToggle = deps.getElements().effectsExpertToggle;
        if (expertToggle) {
            expertToggle.checked = expert;
            expertToggle.closest('.effects-expert-toggle')?.classList.toggle('hidden',
                !catalog || !mod.bankSelectorVisible(catalog.modes?.[catalog.active_mode]));
        }
        if (!catalog) {
            deps.getElements().effectsBankSelect.innerHTML = '';
            deps.getElements().effectsBankSelect.closest('.effects-bank-row')?.classList.add('hidden');
            return;
        }
        mod.renderBankSelector(deps.getElements().effectsBankSelect, catalog, catalog.active_mode || 'stereo', expert);
        deps.getElements().effectsBankSelect.disabled = !!deps.getState().outputSystem.busy || effectsCompareLoadInFlight;
        deps.renderMeasurementArea();
        renderBankImportTarget();
        syncBankActionButtons();
    }

    function renderSweepGroupRow() {
        /* Measure narrows Global or Ind. Banks to a configured speaker group.
         * Chips are only rebuilt when the routed groups change: replacing the
         * clicked button would read as an outside click and close the menu. */
        const row = deps.getElements().measurementSweepGroupRow;
        const mod = outputState();
        if (!row || !mod) return;
        const catalog = (deps.getState().outputSystem || {}).catalog;
        const measurement = deps.getState().measurement || {};
        const allBanks = catalog?.modes?.[catalog.active_mode]?.selected_bank === 'all';
        const choice = catalog ? mod.globalGroupChoice(catalog,
            allBanks ? measurement.allBanksGroup : measurement.globalGroup) : null;
        root.FXRouteUiHelpers.renderChipRow(row, choice ? choice.options : [], {
            attribute: 'data-sweep-group',
            selected: choice ? choice.selected : '',
            disabled: !!measurement.startInFlight,
        });
    }

    function selectEffectsGroup(group) {
        const measurement = deps.getState().measurement;
        const catalog = (deps.getState().outputSystem || {}).catalog;
        const field = catalog?.modes?.[catalog.active_mode]?.selected_bank === 'all' ? 'allBanksGroup' : 'globalGroup';
        if (!measurement || measurement[field] === group) return;
        measurement[field] = group;
        renderEffectsBankSelector();
        syncEffectsDeleteButton();
    }

    function syncBankActionButtons() {
        /* Ind. Banks switches A/B jointly and can be measured as a whole system,
         * but owns no bank: there is nothing to import or combine there. */
        const area = deps.measurementArea();
        const aggregate = area?.available === false || area?.importable === false;
        const measureButton = deps.getElements().effectsMeasureOpenBtn;
        if (measureButton) {
            measureButton.classList?.toggle('hidden', area?.available === false);
            measureButton.disabled = area?.available === false;
            measureButton.title = area?.available === false ? 'Select a filter bank to measure.' : '';
        }
        const importButton = deps.getElements().effectsToggleImportBtn;
        if (importButton) {
            importButton.classList?.toggle('hidden', aggregate);
            importButton.disabled = aggregate;
            importButton.title = aggregate ? 'Ind. Banks only switches A/B; select a filter bank to import.' : '';
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
        const importable = area?.available !== false && area?.importable !== false;
        if (note) note.textContent = !importable ? 'Select a filter bank before importing.'
            : `Target: ${area?.label || 'Global'} · ${mono ? 'Mono' : 'Stereo L/R'}`;
        // Surround Global imports for one configured speaker group, chosen here
        // like the measured group; the chips set the same selection.
        const catalog = deps.getState().outputSystem?.catalog;
        const measurement = deps.getState().measurement || {};
        const choice = importable && catalog ? outputState()?.globalGroupChoice(catalog, measurement.globalGroup) : null;
        root.FXRouteUiHelpers?.renderChipRow(deps.getElements().effectsImportGroupRow, choice ? choice.options : [], {
            attribute: 'data-import-group',
            selected: choice ? choice.selected : '',
            disabled: !!measurement.startInFlight,
        });
        root.FXRouteUiHelpers?.renderChipRow(deps.getElements().effectsPeqGroupRow, choice ? choice.options : [], {
            attribute: 'data-peq-group',
            selected: choice ? choice.selected : '',
            disabled: !!measurement.startInFlight,
        });
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
        if (deps.getElements().effectsRewDualCreatePresetBtn) deps.getElements().effectsRewDualCreatePresetBtn.disabled = !importable;
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

    // A/B compare lives in the output-state banks; nothing to compare
    // until the catalog is loaded.
    function getEffectsCompareState() {
        return outputState()?.compareState((deps.getState().outputSystem || {}).catalog) || {
            compare: getEmptyEffectsCompareState(),
            activePreset: '',
            effectiveActiveSide: null,
            presetA: '',
            presetB: '',
        };
    }

    function setEffectsCompareLoadBusy(isBusy) {
        effectsCompareLoadInFlight = !!isBusy;
        const compare = getEffectsCompareState();
        const busy = effectsCompareLoadInFlight || !!deps.getState().outputSystem?.busy;
        // Ind. Banks lists the bank presets of each slot in place of the picker;
        // Surround Global lists one picker per speaker group instead.
        const slotRows = !!compare.aggregate || !!compare.groupSlots;
        if (deps.getElements().effectsCompareA) deps.getElements().effectsCompareA.disabled = busy || slotRows;
        if (deps.getElements().effectsCompareB) deps.getElements().effectsCompareB.disabled = busy || slotRows;
        [deps.getElements().effectsCompareA, deps.getElements().effectsCompareB].forEach(select => {
            select?.closest?.('.select-tooltip-wrap')?.classList.toggle('hidden', slotRows);
        });
        [deps.getElements().effectsCompareABanks, deps.getElements().effectsCompareBBanks].forEach(list => {
            list?.classList?.toggle('hidden', !slotRows);
            list?.querySelectorAll?.('select').forEach(select => { select.disabled = busy; });
        });
        const unavailable = !!deps.getState().outputSystem?.catalog && !(compare.effectiveActiveSide === 'A' ? compare.canB : compare.canA);
        if (deps.getElements().effectsCompareToggle) {
            deps.getElements().effectsCompareToggle.classList?.toggle('hidden', unavailable);
            deps.getElements().effectsCompareToggle.disabled = busy || unavailable;
            deps.getElements().effectsCompareToggle.title = unavailable ? (compare.aggregate
                ? 'Assign preset B in at least one bank first.' : 'Select preset B to compare.') : '';
        }
        if (deps.getElements().effectsBankSelect) deps.getElements().effectsBankSelect.disabled = busy;
        syncEffectsDeleteButton();
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
        return `<a href="${deps.escapeHtml(deps.presetFileUrl(cleanName, outputSystemBankOwner()))}" data-tooltip="${deps.escapeHtml(cleanName)}">${deps.escapeHtml(displayName)}</a>`;
    }

    // Under Listening/Chain: active presets outside the current bank selection.
    function renderEffectsBankPresets() {
        const target = deps.getElements().effectsCompareBanks;
        if (!target) return;
        const catalog = deps.getState().outputSystem?.catalog;
        const mod = outputState();
        const entries = catalog && typeof mod?.activePresetOverview === 'function'
            ? mod.activePresetOverview(catalog) : [];
        const expert = !!deps.getState().outputSystem?.expert;
        const visible = entries.filter(entry => expert || entry.ids.includes('global') || entry.preset !== 'Neutral');
        const html = visible.map((entry) => {
            const name = entry.preset || 'Per-channel';
            return `<span class="effects-compare-bank" data-tooltip="${deps.escapeHtml(`${entry.label}: ${name}`)}">`
                + `<span class="effects-compare-bank-key">${deps.escapeHtml(entry.label)}</span> `
                + `<span class="effects-compare-bank-preset">${deps.escapeHtml(mod.compactPresetName(name))}</span></span>`;
        }).join(' ');
        // Unchanged markup stays in place, so an open tooltip is not rebuilt on every push.
        if (target.innerHTML !== html) target.innerHTML = html;
    }

    // Ind. Banks slots: the preset each switched bank plays on that side. A bank
    // without a B stays on A; in a mixed state each bank marks the side it is on.
    function renderAllBanksSlots(effectiveActiveSide) {
        const catalog = deps.getState().outputSystem?.catalog;
        const mod = outputState();
        const rows = catalog && typeof mod?.allBanksCompareOverview === 'function' ? mod.allBanksCompareOverview(catalog) : [];
        [['A', deps.getElements().effectsCompareABanks], ['B', deps.getElements().effectsCompareBBanks]].forEach(([slot, target]) => {
            if (!target) return;
            const html = rows.map((row) => {
                const preset = slot === 'A' ? row.presetA : row.presetB;
                const staysOnA = slot === 'B' && !preset && !row.canB;
                const name = preset || (staysOnA ? 'stays on A' : 'Per-channel');
                const tooltip = staysOnA ? `${row.label}: no preset B, stays on A` : `${row.label}: ${name}`;
                const classes = `effects-compare-slot-bank${staysOnA ? ' is-fixed' : ''}`
                    + `${!effectiveActiveSide && row.side === slot ? ' is-heard' : ''}`;
                return `<li class="${classes}" data-tooltip="${deps.escapeHtml(tooltip)}">`
                    + `<span class="effects-compare-slot-bank-key">${deps.escapeHtml(row.label)}</span>`
                    + `<span class="effects-compare-slot-bank-preset">${deps.escapeHtml(name)}</span></li>`;
            }).join('') || '<li class="effects-compare-slot-bank is-fixed">No area banks</li>';
            if (target.innerHTML !== html) target.innerHTML = html;
        });
    }

    // Surround Global rows (null elsewhere): per routed speaker group, the
    // preset each slot selects and the presets of that group.
    function globalGroupRows() {
        const catalog = deps.getState().outputSystem?.catalog;
        const mod = outputState();
        if (!catalog || typeof mod?.globalGroupSlots !== 'function') return null;
        return mod.globalGroupSlots(catalog, visiblePresetEntriesForBank());
    }

    function groupSlotOptions(row, slot) {
        const selected = slot === 'A' ? row.presetA : row.presetB;
        const option = (value, label) => `<option value="${deps.escapeHtml(value)}"${value === selected ? ' selected' : ''}>`
            + `${deps.escapeHtml(label)}</option>`;
        // Generated names differ at their end, so a long one keeps its last
        // word; the picker tooltip names the selected preset in full.
        const compact = name => outputState().compactPresetName(name, 28);
        return option('Neutral', 'Neutral')
            + row.options.map(name => option(name, compact(name))).join('') + option('Direct', 'Direct · all groups');
    }

    // Surround Global slots: one preset picker per speaker group in A and B.
    // A/B switches the whole selection; every listed group plays at once.
    function renderGlobalGroupSlots(rows) {
        [['A', deps.getElements().effectsCompareABanks], ['B', deps.getElements().effectsCompareBBanks]].forEach(([slot, target]) => {
            if (!target) return;
            const html = rows.map((row) => {
                const preset = slot === 'A' ? row.presetA : row.presetB;
                const tooltip = `Preset ${slot} · ${row.label}: ${preset}`;
                return `<li class="effects-compare-slot-bank effects-compare-slot-group" data-group="${row.group}">`
                    + `<label class="effects-compare-slot-bank-key" for="effects-compare-${slot.toLowerCase()}-${row.group}">${deps.escapeHtml(row.label)}</label>`
                    + `<select id="effects-compare-${slot.toLowerCase()}-${row.group}" class="url-input compare-select compare-group-select"`
                    + ` data-slot="${slot}" data-group="${row.group}" data-tooltip="${deps.escapeHtml(tooltip)}">`
                    + `${groupSlotOptions(row, slot)}</select></li>`;
            }).join('') || '<li class="effects-compare-slot-bank is-fixed">No speaker groups</li>';
            // Rebuilding an open <select> on every push would close it.
            if (target.innerHTML !== html) target.innerHTML = html;
        });
    }

    async function handleGlobalGroupChange(select) {
        if (effectsCompareLoadInFlight) return;
        const catalog = deps.getState().outputSystem?.catalog;
        const current = getEffectsCompareState();
        if (!catalog || !current.groupSlots) return;
        const { slot, group } = select.dataset;
        const fields = { mode: catalog.active_mode, bank_id: 'global' };
        Object.assign(fields, { [slot === 'A' ? 'preset_a' : 'preset_b']: select.value, group });
        if (!current.effectiveActiveSide) fields.active_side = slot;
        try {
            await deps.applyMutation('set_bank_preset', fields, false);
        } finally {
            renderEffectsCompare();
        }
    }

    function renderEffectsCompare() {
        const fx = deps.getState().dsp;
        const presetEntries = fx.presets || [];
        const visibleEntries = deps.getState().outputSystem?.catalog ? visiblePresetEntriesForBank() : presetEntries.slice();
        const presets = visibleEntries.map(p => p.name);
        // The selected bank's entries: another bank may hold a preset of the same name.
        const presetMap = new Map(visibleEntries.map(preset => [preset.name, preset]));
        const { compare, activePreset, effectiveActiveSide, presetA, presetB, aggregate, groupSlots, canA, canB } = getEffectsCompareState();

        if (!deps.getElements().effectsCompareRow) return;
        renderEffectsBankPresets();
        if (!deps.getState().outputSystem?.catalog || (!aggregate && presets.length === 0)) {
            deps.getElements().effectsCompareRow.style.display = 'none';
            syncBankActionButtons();
            return;
        }
        deps.getElements().effectsCompareRow.style.display = '';

        // Rebuilding <option> lists on every WS push closes an open dropdown.
        // Only touch the DOM when the values actually changed.
        // Ind. Banks owns no presets: its hidden selects hold one aggregate
        // label each, and the slots list the bank presets instead.
        const optionsA = aggregate ? '<option>Ind. Banks · A</option>' :
            (!presetA ? '<option value="">Per-channel presets</option>' : '') + presets.map(n => `<option value="${deps.escapeHtml(n)}" ${n === presetA ? 'selected' : ''}>${deps.escapeHtml(n)}</option>`).join('');
        if (deps.getElements().effectsCompareA.innerHTML !== optionsA) {
            deps.getElements().effectsCompareA.innerHTML = optionsA;
        }
        const optionsB = aggregate ? '<option>Ind. Banks · B</option>' :
            (!presetB && canB ? '<option value="" selected>Per-channel presets</option>' : '')
            + presets.map(n => `<option value="${deps.escapeHtml(n)}" ${n === (presetB || (canB ? '' : 'Neutral')) ? 'selected' : ''}>${deps.escapeHtml(n)}</option>`).join('');
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
        // Ind. Banks: the status names the side only; the slots list the banks.
        deps.getElements().effectsCompareChain?.classList?.toggle('hidden', !!aggregate);
        if (aggregate) {
            if (deps.getElements().effectsCompareActive) deps.getElements().effectsCompareActive.textContent = effectiveActiveSide
                ? `Listening: ${effectiveActiveSide} · Ind. Banks` : 'Listening: Mixed A/B';
            renderAllBanksSlots(effectiveActiveSide);
        } else if (groupSlots) {
            // Surround Global: the slots list each group's preset; the chain names
            // the groups the listened selection filters.
            if (deps.getElements().effectsCompareActive) deps.getElements().effectsCompareActive.textContent = effectiveActiveSide
                ? `Listening: ${effectiveActiveSide}` : `Listening: ${activePreset || '—'}`;
            if (deps.getElements().effectsCompareChain) deps.getElements().effectsCompareChain.textContent = activePreset === 'Direct'
                ? 'Filters: bypassed (Direct)' : activePreset && activePreset !== 'Neutral' ? `Filters: ${activePreset}` : 'Filters: none';
            renderGlobalGroupSlots(globalGroupRows() || []);
        }
        if (deps.getElements().effectsCompareToggle) deps.getElements().effectsCompareToggle.textContent = 'Compare A/B';
        syncEffectsDeleteButton();
        const badge = document.getElementById('effects-compare-active-badge');
        if (badge) {
            badge.classList.toggle('is-side-a', effectiveActiveSide === 'A');
            badge.classList.toggle('is-side-b', effectiveActiveSide === 'B');
        }
        document.querySelectorAll('.effects-compare-slot').forEach((slotEl) => {
            const slot = slotEl.dataset.compareSlot;
            const slotAvailable = aggregate || groupSlots ? !!(slot === 'A' ? canA : canB) : !!(slot === 'A' ? presetA : presetB);
            slotEl.classList.toggle('is-active', effectiveActiveSide === slot && (slotAvailable || aggregate));
            slotEl.classList.toggle('is-armed', effectiveActiveSide !== slot && slotAvailable);
        });
        renderEffectsBankSelector();
        setEffectsCompareLoadBusy(effectsCompareLoadInFlight);
    }

    // Surround Global deletes from every routed group on the listened side,
    // independently of the measurement/import target and Easy/Expert view.
    function activeDeletablePresets() {
        const { activePreset, effectiveActiveSide, groupSlots, aggregate } = getEffectsCompareState();
        if (aggregate) return [];
        const catalog = deps.getState().outputSystem?.catalog;
        const config = catalog?.modes?.[catalog.active_mode];
        const bank = config?.banks?.[config.selected_bank];
        if (groupSlots && effectiveActiveSide !== 'A' && effectiveActiveSide !== 'B') return [];
        const entries = groupSlots
            ? (globalGroupRows() || []).map(row => ({
                name: effectiveActiveSide === 'B' ? row.presetB : row.presetA, label: row.label, group: row.group,
            }))
            : [{ name: activePreset, label: bank?.label || '', group: '' }];
        return entries.filter(entry => entry.name && entry.name !== 'Direct' && entry.name !== 'Neutral');
    }

    function effectsDeleteContext() {
        return JSON.stringify([outputSystemBankOwner(), getEffectsCompareState().effectiveActiveSide, activeDeletablePresets()]);
    }

    // Delete targets the selected bank's active preset, as in Global; Ind.
    // Banks has no Delete.
    function syncEffectsDeleteButton() {
        const button = deps.getElements().effectsDeleteBtn;
        if (!button) return;
        const { aggregate } = getEffectsCompareState();
        button.classList.toggle('hidden', !!aggregate);
        button.disabled = effectsDeleteInFlight || !!deps.getState().outputSystem?.busy
            || !deps.getState().dsp?.available || activeDeletablePresets().length === 0;
        const tooltip = 'Delete active presets';
        if (button.dataset) button.dataset.tooltip = tooltip;
        button.setAttribute?.('aria-label', tooltip);
    }

    function syncEffectsDeleteDialog() {
        const elements = deps.getElements();
        const multiple = (effectsDeleteDraft?.presets.length || 0) > 1;
        const inputs = Array.from(elements.effectsDeleteList?.querySelectorAll('input[data-delete-index]') || []);
        const selected = multiple ? inputs.filter(input => input.checked).length : effectsDeleteDraft ? 1 : 0;
        if (elements.effectsDeleteConfirm) {
            elements.effectsDeleteConfirm.textContent = multiple ? `Delete (${selected})` : 'Delete';
            elements.effectsDeleteConfirm.disabled = effectsDeleteInFlight || selected === 0;
        }
        if (elements.effectsDeleteCancel) elements.effectsDeleteCancel.disabled = effectsDeleteInFlight;
        inputs.forEach(input => { input.disabled = effectsDeleteInFlight; });
    }

    function closeEffectsDeleteDialog() {
        if (effectsDeleteInFlight) return;
        const panel = deps.getElements().effectsDeletePanel;
        effectsDeleteDraft = null;
        panel?.classList.add('hidden');
        root.FXRouteModal?.close(panel);
        if (deps.getElements().effectsDeleteBtn?.disabled) {
            const compare = deps.getElements().effectsCompareToggle;
            const fallback = compare && !compare.disabled && !compare.classList.contains('hidden')
                ? compare : document.querySelector?.('.tab-btn[data-tab="effects"]');
            fallback?.focus();
        }
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
                deps.getElements().effectsCombineSaveBtn.title = 'Ind. Banks owns no presets; select a concrete filterbank.';
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
            if (deps.getElements().effectsStatus) deps.getElements().effectsStatus.innerHTML = '<div style="color: var(--danger);">Ind. Banks owns no presets; select a concrete filterbank.</div>';
            deps.showToast('Ind. Banks owns no presets; select a concrete filterbank', 'error');
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
            const owner = outputSystemBankOwner();
            const resp = await fetch('/api/dsp/presets/combine', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    presetName: validation.presetName,
                    presetNames: validation.selectedPresets,
                    ...(owner || {}),
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

    async function loadEffectsComparePreset(newSide) {
        if (effectsCompareLoadInFlight) return;
        const catalog = deps.getState().outputSystem?.catalog;
        if (!catalog) return;
        setEffectsCompareLoadBusy(true);
        try {
            const bankId = catalog.modes[catalog.active_mode].selected_bank;
            await deps.applyMutation('set_bank_preset', {
                mode: catalog.active_mode, bank_id: bankId, active_side: newSide,
            }, false);
            renderEffectsCompare();
        } finally {
            setEffectsCompareLoadBusy(false);
        }
    }

    async function handleEffectsCompareSelectionChange(slot) {
        if (effectsCompareLoadInFlight) return;
        const catalog = deps.getState().outputSystem?.catalog;
        if (!catalog) {
            renderEffectsCompare();
            return;
        }
        const current = getEffectsCompareState();
        if (current.aggregate) return;
        const selectedValue = (slot === 'A' ? deps.getElements().effectsCompareA?.value : deps.getElements().effectsCompareB?.value) || null;
        const fields = { mode: catalog.active_mode, bank_id: catalog.modes[catalog.active_mode].selected_bank,
            [slot === 'A' ? 'preset_a' : 'preset_b']: selectedValue };
        if (selectedValue && (!current.effectiveActiveSide || current.effectiveActiveSide === slot)) fields.active_side = slot;
        if (!selectedValue && slot === 'B' && current.effectiveActiveSide === 'B') fields.active_side = 'A';
        await deps.applyMutation('set_bank_preset', fields, false);
        renderEffectsCompare();
    }

    async function toggleComparePreset() {
        if (effectsCompareLoadInFlight) return;
        try {
            const catalog = deps.getState().outputSystem?.catalog;
            if (!catalog) return;
            const compareState = getEffectsCompareState();
            const side = compareState.effectiveActiveSide === 'A' ? 'B' : 'A';
            if (compareState.aggregate) {
                if (!(side === 'B' ? compareState.canB : compareState.canA)) {
                    deps.showToast('Assign preset B in at least one bank first.', 'warning');
                    return;
                }
                await deps.applyMutation('switch_all_banks', {
                    mode: catalog.active_mode, active_side: side,
                }, false);
                renderEffectsCompare();
                return;
            }
            if (!(side === 'B' ? compareState.canB : compareState.canA)) return;
            await loadEffectsComparePreset(side);
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
        [deps.getElements().effectsCompareABanks, deps.getElements().effectsCompareBBanks].forEach(list => {
            list?.addEventListener?.('change', async (event) => {
                if (!event.target?.classList?.contains('compare-group-select')) return;
                try {
                    await handleGlobalGroupChange(event.target);
                } catch (e) {
                    console.error('compare group preset change error:', e);
                    deps.showToast(e.message || 'Failed to select preset', 'error');
                }
            });
        });
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

    function deleteEffectsPreset() {
        if (effectsDeleteInFlight) return;
        const presets = activeDeletablePresets();
        if (!presets.length) {
            deps.showToast('No active preset to delete', 'warning');
            return;
        }
        if (!deps.getState().dsp?.available || deps.getState().outputSystem?.busy) return;
        const elements = deps.getElements();
        if (!elements.effectsDeletePanel || !elements.effectsDeleteList) return;
        effectsDeleteDraft = { presets, owner: outputSystemBankOwner() || {}, context: effectsDeleteContext() };
        const multiple = presets.length > 1;
        elements.effectsDeleteList.innerHTML = presets.map((preset, index) => {
            const copy = `<span class="effects-delete-copy"><strong>${deps.escapeHtml(preset.label)}</strong>`
                + `<span>${deps.escapeHtml(preset.name)}</span></span>`;
            return multiple ? `<label class="effects-delete-item"><input type="checkbox" data-delete-index="${index}" checked>${copy}</label>`
                : `<div class="effects-delete-item">${copy}</div>`;
        }).join('');
        const side = getEffectsCompareState().effectiveActiveSide;
        if (elements.effectsDeleteDescription) elements.effectsDeleteDescription.textContent =
            (side ? `Listening: ${side} · ` : '') + (multiple ? 'Select the presets to delete.' : 'Delete this preset?');
        syncEffectsDeleteDialog();
        elements.effectsDeletePanel.classList.remove('hidden');
        root.FXRouteModal?.open(elements.effectsDeletePanel, {
            opener: elements.effectsDeleteBtn, initialFocus: elements.effectsDeleteCancel,
            onEscape: closeEffectsDeleteDialog,
        });
    }

    async function confirmEffectsPresetDelete() {
        if (!effectsDeleteDraft || effectsDeleteInFlight) return;
        const { presets, owner, context } = effectsDeleteDraft;
        const inputs = Array.from(deps.getElements().effectsDeleteList?.querySelectorAll('input[data-delete-index]') || []);
        const selected = presets.length === 1 ? presets : presets.filter((preset, index) =>
            inputs.some(input => Number(input.dataset.deleteIndex) === index && input.checked));
        if (!selected.length) return;
        if (context !== effectsDeleteContext() || !deps.getState().dsp?.available || deps.getState().outputSystem?.busy) {
            closeEffectsDeleteDialog();
            deps.showToast('Active presets changed. Reopen Delete active to review them.', 'warning');
            return;
        }
        effectsDeleteInFlight = true;
        syncEffectsDeleteDialog();
        syncEffectsDeleteButton();
        let deletedCount = 0;
        try {
            for (const preset of selected) {
                const resp = await fetch('/api/dsp/presets/delete', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ preset_name: preset.name, ...owner }),
                });
                const data = await resp.json();
                if (!resp.ok) throw new Error(formatError(data.detail, typeof data.detail === 'string' ? data.detail : 'Preset delete failed'));
                deletedCount++;
            }
            deps.showToast(selected.length === 1 ? `Deleted preset: ${selected[0].name}` : `Deleted ${selected.length} presets`, 'success');
        } catch (e) {
            const progress = deletedCount ? `Deleted ${deletedCount} of ${selected.length} presets. ` : '';
            deps.showToast(progress + (e.message || 'Preset delete failed'), 'error');
        } finally {
            try {
                await deps.fetchEffects();
                await deps.refreshCatalog(true);
            } catch (e) {
                deps.showToast(e.message || 'Preset refresh failed', 'error');
            } finally {
                effectsDeleteInFlight = false;
                syncEffectsDeleteButton();
                closeEffectsDeleteDialog();
            }
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
            deps.getElements().effectsDeleteConfirm?.addEventListener('click', confirmEffectsPresetDelete);
            deps.getElements().effectsDeleteCancel?.addEventListener('click', closeEffectsDeleteDialog);
            deps.getElements().effectsDeleteList?.addEventListener('change', syncEffectsDeleteDialog);
            deps.getElements().effectsDeletePanel?.querySelector('.manage-overlay-backdrop')
                ?.addEventListener('click', closeEffectsDeleteDialog);
            deps.getElements().effectsExpertToggle?.addEventListener('change', (event) => {
                void setExpertView(!!event.target.checked);
            });
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
        readStoredExpertView,
        setExpertView,
        getDefaultEffectsCombineDraft,
        normalizeEffectsCombineDraft,
        setEffectsImportPanelOpen,
        outputSystemBankBinding,
        outputSystemBankOwner,
        visiblePresetEntriesForBank,
        visiblePresetNamesForBank,
        appendBankBindingFields,
        bankBindingJson,
        measurementPeqParams,
        requireConcreteFilterBank,
        requireMeasurableArea,
        renderEffectsBankSelector,
        selectEffectsGroup,
        renderSweepGroupRow,
        syncBankActionButtons,
        renderBankImportTarget,
        readTextFile,
        getDualFilterFileKind,
        populateDualFilterTextareaFromFile,
        createDualFilterPreset,
        getEmptyEffectsCompareState,
        getEffectsCompareState,
        setEffectsCompareLoadBusy,
        getEffectsChainLabelForPreset,
        getCompactDisplayName,
        renderPresetDownloadLink,
        renderEffectsCompare,
        syncEffectsDeleteButton,
        getEffectsCombineValidationState,
        renderEffectsCombine,
        createCombinedEffectsPreset,
        loadEffectsComparePreset,
        handleEffectsCompareSelectionChange,
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
