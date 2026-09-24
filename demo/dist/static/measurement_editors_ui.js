// SPDX-License-Identifier: AGPL-3.0-only
/** Measurement PEQ, house-curve and convolver editor rendering. */
(function (root, factory) {
    const api = factory(root);
    root.FXRouteMeasurementEditorsUI = api;
    if (typeof module === 'object' && module.exports) module.exports = api;
})(typeof globalThis !== 'undefined' ? globalThis : window, function (root) {
    'use strict';

    const ui = root.FXRouteMeasurementUI || {};
    let deps = {
        getState: () => ({ dsp: {} }),
        getElements: () => ({}),
        getDocument: () => root.document,
        getInputElementType: () => root.HTMLInputElement,
        escapeHtml: (value) => String(value == null ? '' : value),
        ensureCustomHouseCurveState: () => ({ points: [] }),
        getCustomHouseCurvePointSlot: () => -1,
        getCustomHouseCurvePointColor: () => '',
        getMeasurementPeqPresetName: () => '',
        getMeasurementPeqDraftMode: () => null,
        isPeqCreateInFlight: () => false,
        measurementBankSumsBothInputs: () => false,
        getMeasurementConvolverCurveOptions: () => [],
        getMeasurementConvolverSourceSelectionState: () => ({ take: {} }),
        analyzeMeasurementConvolverSide: () => null,
        getMeasurementConvolverDraftPhaseMismatch: () => '',
        isConvolverCreateInFlight: () => false,
        getMeasurementConvolverCurve: () => ({ label: '' }),
        getMeasurementConvolverTimingDelta: () => null,
        getMeasurementDirectArrivalTiming: () => null,
        getMeasurementConvolverMeasurementForSide: () => null,
        formatMeasurementConvolverTimingRelation: () => '',
        getMeasurementConvolverItemName: () => '',
        buildMeasurementConvolverWarnings: () => [],
        getMeasurementPeqActiveFilter: () => null,
        updateMeasurementPeqFilter: () => {},
        updateCustomHouseCurvePoint: () => {},
        stepMeasurementPeqFrequency: () => null,
        stepMeasurementPeqGain: () => null,
        stepMeasurementPeqQ: () => null,
        selectMeasurementPeqFilter: () => {},
        addMeasurementPeqFilter: () => null,
        addCustomHouseCurvePoint: () => null,
        deleteCustomHouseCurvePoint: () => {},
        deleteMeasurementPeqFilter: () => {},
        handleMeasurementPeqNumberInputArrowKey: () => {},
        focusMeasurementPeqPanelContext: () => {},
        renderMeasurementPanel: () => {},
        scheduleMeasurementGraphRender: () => {},
    };

    function init(overrides) {
        deps = Object.assign(deps, overrides || {});
    }

    function getMeasurementSlotChipStyle(color = '', active = false) {
        if (!color) return '';
        return `style="border-color:${deps.escapeHtml(color)}66;background:${deps.escapeHtml(color)}22;${active ? `color:#08110d;background:${deps.escapeHtml(color)};box-shadow:0 0 0 2px ${deps.escapeHtml(color)}66, 0 0 0 4px rgba(248,250,252,0.28);` : ''}"`;
    }

    function renderMeasurementSlotChip({ label, index, color = '', active = false, occupied = false, attributes = '' }) {
        const classes = `measurement-slot-chip measurement-peq-chip${active ? ' is-active' : ''}${occupied ? '' : ' is-empty'}`;
        return `<button type="button" class="${classes}" ${getMeasurementSlotChipStyle(color, active)} data-measurement-slot-index="${index}" ${attributes}>${deps.escapeHtml(label)}</button>`;
    }

    function renderMeasurementPanelEditorsSection({ measurementState, current, measurements, graphEntries, assistMode, activeEditor, graphView, frequencyView, peq, conv, activePeqFilter }) {
        const elements = deps.getElements();
        if (elements.measurementPeqPanel) {
            elements.measurementPeqPanel.classList.toggle('hidden', !frequencyView || activeEditor !== 'peq' || (!peq.enabled && !peq.filters.length));
        }
        const customHouseCurve = deps.ensureCustomHouseCurveState();
        const activeCustomPoint = customHouseCurve.points.find((point) => point.id === customHouseCurve.activePointId) || null;
        if (elements.measurementCustomHouseCurvePanel) {
            elements.measurementCustomHouseCurvePanel.classList.toggle('hidden', !frequencyView || activeEditor !== 'houseCurve');
        }
        if (elements.measurementCustomHouseCurveChips) {
            elements.measurementCustomHouseCurveChips.innerHTML = Array.from({ length: 8 }, (_, index) => {
                const point = customHouseCurve.points.find((candidate) => Number(candidate.slot) === index) || null;
                const active = point && point.id === customHouseCurve.activePointId;
                const color = point ? deps.getCustomHouseCurvePointColor(point, index) : '';
                return renderMeasurementSlotChip({
                    label: `P${index + 1}`,
                    index,
                    color,
                    active,
                    occupied: !!point,
                    attributes: `data-custom-house-curve-slot="${index}" data-custom-house-curve-point="${point ? deps.escapeHtml(point.id) : ''}"`,
                });
            }).join('');
        }
        if (elements.measurementCustomHouseCurveEditor) {
            const activeCustomSlot = activeCustomPoint ? deps.getCustomHouseCurvePointSlot(activeCustomPoint.id) : -1;
            const activeCustomColor = activeCustomPoint ? deps.getCustomHouseCurvePointColor(activeCustomPoint, activeCustomSlot) : '';
            elements.measurementCustomHouseCurveEditor.innerHTML = activeCustomPoint ? `
                <div class="measurement-peq-editor-grid" data-custom-house-curve-editor-slot="${activeCustomSlot}" style="border-color:${deps.escapeHtml(activeCustomColor)}66;">
                    <div class="measurement-custom-house-curve-slot-label" style="color:${deps.escapeHtml(activeCustomColor)};">P${activeCustomSlot + 1}</div>
                    <div class="field-group measurement-peq-direct-input-field">
                        <label for="measurement-custom-house-curve-frequency">Frequency (Hz)</label>
                        <input id="measurement-custom-house-curve-frequency" class="url-input measurement-peq-number-input" type="number" min="20" max="20000" step="1" value="${Math.round(activeCustomPoint.freqHz)}" data-custom-house-curve-field="freqHz">
                    </div>
                    <div class="field-group measurement-peq-direct-input-field">
                        <label for="measurement-custom-house-curve-gain">Gain (dB)</label>
                        <input id="measurement-custom-house-curve-gain" class="url-input measurement-peq-number-input" type="number" min="-24" max="24" step="0.1" value="${Number(activeCustomPoint.gainDb).toFixed(1)}" data-custom-house-curve-field="gainDb">
                    </div>
                    <div class="measurement-peq-editor-actions">
                        <button type="button" class="btn-danger" data-custom-house-curve-delete="${deps.escapeHtml(activeCustomPoint.id)}">Delete</button>
                    </div>
                </div>` : '<div class="measurement-peq-editor-empty">Use P1-P8 to add up to 8 curve points.</div>';
        }
        if (elements.measurementCustomHouseCurveName && deps.getDocument().activeElement !== elements.measurementCustomHouseCurveName) {
            elements.measurementCustomHouseCurveName.value = customHouseCurve.name || '';
        }
        if (elements.measurementCustomHouseCurveCreateBtn) {
            elements.measurementCustomHouseCurveCreateBtn.disabled = customHouseCurve.points.length < 2 || !String(customHouseCurve.name || '').trim() || customHouseCurve.saving;
            elements.measurementCustomHouseCurveCreateBtn.textContent = customHouseCurve.saving ? 'Creating…' : 'Create Target Curve';
        }
        if (elements.measurementConvolverPanel) {
            elements.measurementConvolverPanel.classList.toggle('hidden', !frequencyView || assistMode !== 'convolver');
        }
        if (elements.measurementPeqChips) {
            elements.measurementPeqChips.innerHTML = Array.from({ length: 12 }, (_, index) => {
                const filter = peq.filters[index] || null;
                const active = filter && filter.id === peq.activeFilterId;
                return renderMeasurementSlotChip({
                    label: `F${index + 1}`,
                    index,
                    color: filter ? filter.color : '',
                    active,
                    occupied: !!filter,
                    attributes: `data-measurement-peq-slot="${index}" data-measurement-peq-chip="${filter ? deps.escapeHtml(filter.id) : ''}"`,
                });
            }).join('');
        }
        if (elements.measurementPeqEditor) {
            if (!activePeqFilter) {
                elements.measurementPeqEditor.innerHTML = '<div class="measurement-peq-editor-empty">Use F1-F12 or the graph near the fixed 0 dB line to add up to 12 temporary filters.</div>';
            } else {
                const hideFreqQ = activePeqFilter.type === 'gain';
                elements.measurementPeqEditor.innerHTML = `
                    <div class="measurement-peq-editor-grid">
                        <div class="field-group">
                            <label for="measurement-peq-type">Type</label>
                            <select id="measurement-peq-type" class="url-input" data-measurement-peq-field="type">
                                ${ui.measurementPeqTypes.map((type) => `<option value="${type}" ${activePeqFilter.type === type ? 'selected' : ''}>${ui.measurementPeqTypeLabels[type] || type}</option>`).join('')}
                            </select>
                        </div>
                        ${hideFreqQ ? '' : `
                        <div class="field-group measurement-peq-direct-input-field">
                            <label for="measurement-peq-freq">Frequency (Hz)</label>
                            <div class="measurement-peq-stepper">
                                <button type="button" class="btn-secondary measurement-peq-step-btn" data-measurement-peq-frequency-step="-1" aria-label="Decrease frequency">−</button>
                                <input id="measurement-peq-freq" class="url-input measurement-peq-number-input" type="number" min="20" max="20000" step="1" inputmode="numeric" value="${Math.round(activePeqFilter.freqHz || 1000)}" data-measurement-peq-field="freqHz">
                                <button type="button" class="btn-secondary measurement-peq-step-btn" data-measurement-peq-frequency-step="1" aria-label="Increase frequency">+</button>
                            </div>
                        </div>`}
                        <div class="field-group measurement-peq-direct-input-field">
                            <label for="measurement-peq-gain">Gain (dB)</label>
                            <div class="measurement-peq-stepper">
                                <button type="button" class="btn-secondary measurement-peq-step-btn" data-measurement-peq-gain-step="-1" aria-label="Decrease gain">−</button>
                                <input id="measurement-peq-gain" class="url-input measurement-peq-number-input" type="number" min="-24" max="24" step="0.1" inputmode="decimal" value="${Number(activePeqFilter.gainDb || 0).toFixed(1)}" data-measurement-peq-field="gainDb">
                                <button type="button" class="btn-secondary measurement-peq-step-btn" data-measurement-peq-gain-step="1" aria-label="Increase gain">+</button>
                            </div>
                        </div>
                        ${hideFreqQ ? '' : `
                        <div class="field-group measurement-peq-direct-input-field">
                            <label for="measurement-peq-q">Q</label>
                            <div class="measurement-peq-stepper">
                                <button type="button" class="btn-secondary measurement-peq-step-btn" data-measurement-peq-q-step="-1" aria-label="Decrease Q">−</button>
                                <input id="measurement-peq-q" class="url-input measurement-peq-number-input" type="number" min="0.1" max="20" step="0.1" inputmode="decimal" aria-keyshortcuts="ArrowUp ArrowDown" value="${Number(activePeqFilter.q || 1).toFixed(2)}" data-measurement-peq-field="q">
                                <button type="button" class="btn-secondary measurement-peq-step-btn" data-measurement-peq-q-step="1" aria-label="Increase Q">+</button>
                            </div>
                        </div>`}
                        <div class="measurement-peq-editor-actions">
                            <button type="button" class="btn-danger" data-measurement-peq-delete="${deps.escapeHtml(activePeqFilter.id)}">Delete</button>
                        </div>
                    </div>
                `;
            }
        }
        const peqDraftLeftCount = peq.draft?.leftBands?.length || 0;
        const peqDraftRightCount = peq.draft?.rightBands?.length || 0;
        if (elements.measurementPeqDraftSummary) {
            const draftMode = peqDraftLeftCount && peqDraftRightCount ? 'LR draft ready' : (peqDraftRightCount ? 'R draft ready' : (peqDraftLeftCount ? 'L draft ready' : 'no draft staged'));
            elements.measurementPeqDraftSummary.innerHTML = `<div>Draft: ${deps.escapeHtml(draftMode)} · L: ${peqDraftLeftCount} bands · R: ${peqDraftRightCount} bands</div>`;
        }
        if (elements.measurementPeqPresetName) {
            const hasDraft = !!peqDraftLeftCount || !!peqDraftRightCount;
            // Like the convolver field: show the stable auto suggestion even
            // before Take, and keep it editable from the moment a name stands
            // in the field. A typed name is stored touched, so a later Take
            // never overwrites it; Create still requires a staged draft.
            const nameValue = peq.draft?.presetName || deps.getMeasurementPeqPresetName(deps.getMeasurementPeqDraftMode(peq) || 'both');
            if (deps.getDocument().activeElement !== elements.measurementPeqPresetName) {
                elements.measurementPeqPresetName.value = nameValue;
            }
            elements.measurementPeqPresetName.disabled = deps.isPeqCreateInFlight();
            elements.measurementPeqPresetName.placeholder = hasDraft ? 'Preset name' : 'Type a name, or Take L/R/Both to stage';
        }
        if (elements.measurementPeqTakeLeftBtn) elements.measurementPeqTakeLeftBtn.disabled = !peq.filters.length;
        if (elements.measurementPeqTakeRightBtn) elements.measurementPeqTakeRightBtn.disabled = !peq.filters.length;
        if (elements.measurementPeqTakeBothBtn) elements.measurementPeqTakeBothBtn.disabled = !peq.filters.length;
        if (elements.measurementPeqCreateBtn) elements.measurementPeqCreateBtn.disabled = (!peqDraftLeftCount && !peqDraftRightCount) || !String(peq.draft?.presetName || '').trim() || deps.isPeqCreateInFlight();
        syncMeasurementSummedSubTakeModes();

    }

    function syncMeasurementSummedSubTakeModes() {
        /* Steer a mono bank toward the takes that compile there: the engine sums
         * both inputs into one output role, so a dual PEQ needs identical L/R
         * bands and a convolver needs a mono IR file. A mono bank offers one
         * Take Mono button; side takes are hidden. Disabled buttons keep the
         * tooltip as the reason. */
        const elements = deps.getElements();
        const summed = deps.measurementBankSumsBothInputs();
        for (const button of [elements.measurementPeqTakeLeftBtn, elements.measurementPeqTakeRightBtn,
            elements.measurementConvolverTakeRightBtn, elements.measurementConvolverTakeBothBtn]) {
            button?.classList?.toggle('hidden', summed);
        }
        if (elements.measurementPeqTakeBothBtn) elements.measurementPeqTakeBothBtn.textContent = summed ? 'Take Mono' : 'Take Both';
        if (elements.measurementConvolverTakeLeftBtn) elements.measurementConvolverTakeLeftBtn.textContent = summed ? 'Take Mono' : 'Take L';
        const bothReason = summed ? 'This area is fed by both inputs: its bank needs a mono IR, so take a single side.' : '';
        const sideReason = summed ? 'This area is fed by both inputs: take Both to stage the identical L/R correction.' : '';
        for (const button of [elements.measurementPeqTakeLeftBtn, elements.measurementPeqTakeRightBtn,
                              elements.measurementConvolverTakeBothBtn]) {
            if (!button) continue;
            if (summed) {
                button.disabled = true;
                button.title = button === elements.measurementConvolverTakeBothBtn ? bothReason : sideReason;
            } else if (button.dataset.summedSub === 'true') {
                // Leave disabled to the render pass; only clear the stale marker.
                button.dataset.summedSub = 'false';
                button.title = '';
            }
        }
    }

    function renderMeasurementPanelConvolverSection({ measurementState, current, measurements, graphEntries, assistMode, activeEditor, graphView, frequencyView, peq, conv, activePeqFilter }) {
        const elements = deps.getElements();
        if (elements.measurementConvolverTarget) {
            const optionsHtml = deps.getMeasurementConvolverCurveOptions().map((curve) => `<option value="${deps.escapeHtml(curve.key)}" ${conv.targetCurve === curve.key ? 'selected' : ''}>${deps.escapeHtml(curve.label || curve.shortLabel || curve.key)}</option>`).join('');
            if (elements.measurementConvolverTarget.innerHTML !== optionsHtml) elements.measurementConvolverTarget.innerHTML = optionsHtml;
            elements.measurementConvolverTarget.value = conv.targetCurve;
        }
        if (elements.measurementConvolverRangeStart && deps.getDocument().activeElement !== elements.measurementConvolverRangeStart) elements.measurementConvolverRangeStart.value = String(Math.round(conv.rangeStartHz));
        if (elements.measurementConvolverRangeEnd && deps.getDocument().activeElement !== elements.measurementConvolverRangeEnd) elements.measurementConvolverRangeEnd.value = String(Math.round(conv.rangeEndHz));
        if (elements.measurementConvolverMaxBoost) elements.measurementConvolverMaxBoost.value = String(conv.maxBoostDb);
        if (elements.measurementConvolverMaxCut) elements.measurementConvolverMaxCut.value = String(conv.maxCutDb);
        if (elements.measurementConvolverDipGuard) elements.measurementConvolverDipGuard.value = conv.dipGuard;
        if (elements.measurementConvolverSampleRate) elements.measurementConvolverSampleRate.value = String(measurementState.measurementSampleRate || '48000');
        if (elements.measurementConvolverPhaseMode) elements.measurementConvolverPhaseMode.value = conv.phaseMode;
        if (elements.measurementConvolverIrLength) elements.measurementConvolverIrLength.value = String(conv.irLength);
        const convolverSourceSelection = deps.getMeasurementConvolverSourceSelectionState();
        const convAnalyses = ['left', 'right'].map((side) => deps.analyzeMeasurementConvolverSide(side));
        const left = convAnalyses[0];
        const right = convAnalyses[1];
        const leftDraft = conv.draft?.left || null;
        const rightDraft = conv.draft?.right || null;
        const hasConvolverDraft = !!leftDraft || !!rightDraft;
        const draftPhaseMismatch = deps.getMeasurementConvolverDraftPhaseMismatch(conv);
        const isCreatingConvolverPreset = !!conv.creatingPreset || deps.isConvolverCreateInFlight();
        if (elements.measurementConvolverSummary) {
            const curve = deps.getMeasurementConvolverCurve(conv.targetCurve);
            const hasCreatedConvolver = (deps.getState().dsp?.assistStack || []).some((item) => item.type === 'convolver');
            let draftStatus;
            const draftDetails = [];
            const currentTimingDelta = left && right
                ? deps.getMeasurementConvolverTimingDelta(
                    deps.getMeasurementDirectArrivalTiming(deps.getMeasurementConvolverMeasurementForSide('left')),
                    deps.getMeasurementDirectArrivalTiming(deps.getMeasurementConvolverMeasurementForSide('right')),
                )
                : null;
            const summaryTimingDelta = leftDraft && rightDraft
                ? deps.getMeasurementConvolverTimingDelta(leftDraft.timing, rightDraft.timing)
                : currentTimingDelta;
            if (isCreatingConvolverPreset) {
                draftStatus = 'Creating convolver preset...';
            } else if (draftPhaseMismatch) {
                draftStatus = 'Draft phase does not match the selected phase type. Take L/R again.';
            } else if (leftDraft && rightDraft) {
                const timingDelta = summaryTimingDelta;
                if (timingDelta) {
                    draftStatus = `Draft ready · ${deps.formatMeasurementConvolverTimingRelation(timingDelta)}`;
                } else {
                    draftStatus = 'Draft ready · Timing unavailable';
                }
            } else if (leftDraft || rightDraft) {
                draftStatus = 'Draft ready · Timing unavailable';
            } else {
                draftStatus = hasCreatedConvolver ? 'Convolver preset created' : 'No draft staged';
                if (currentTimingDelta && ui.measurementConvolverAlignedPhaseModes.includes(conv.phaseMode)) {
                    draftStatus += ` · ${deps.formatMeasurementConvolverTimingRelation(currentTimingDelta)}`;
                }
            }
            if (summaryTimingDelta && (leftDraft && rightDraft || (left && right && ui.measurementConvolverAlignedPhaseModes.includes(conv.phaseMode)))) {
                if (summaryTimingDelta.absMs > ui.MEASUREMENT_CONVOLVER_TIMING_SAFETY_LIMIT_MS) {
                    draftDetails.push('Filter not created because timing offset exceeds safety limit.');
                }
            }
            if (conv.draft?.notice) draftDetails.push(conv.draft.notice);
            if (hasConvolverDraft) {
                const stagedPhaseModes = [...new Set([leftDraft, rightDraft].map(ui.getMeasurementConvolverDraftPhaseMode).filter(Boolean))];
                if (stagedPhaseModes.length) {
                    draftDetails.push(`Staged phase: ${stagedPhaseModes.map(ui.getMeasurementConvolverPhaseLabel).join(' + ')}`);
                }
            }
            elements.measurementConvolverSummary.innerHTML = `
                <div><strong>${deps.escapeHtml(curve.label)}</strong> · ${deps.escapeHtml(ui.getMeasurementConvolverTypeLabel(conv.quality))} · Max Boost +${conv.maxBoostDb} dB · Max Cut ${conv.maxCutDb} dB · Dip Guard ${deps.escapeHtml(conv.dipGuard)}</div>
                <div>Range data. L: ${left ? `${left.points} pts, gain ${ui.formatMeasurementConvolverGain(left.autoGainDb)} (energy ${ui.formatMeasurementConvolverGain(left.energyGainDb ?? 0)})` : 'none'} · R: ${right ? `${right.points} pts, gain ${ui.formatMeasurementConvolverGain(right.autoGainDb)} (energy ${ui.formatMeasurementConvolverGain(right.energyGainDb ?? 0)})` : 'none'}</div>
                <div>${deps.escapeHtml(draftStatus)}</div>
                ${draftDetails.map((detail) => `<div>${deps.escapeHtml(detail)}</div>`).join('')}
            `;
        }
        if (elements.measurementConvolverPresetName) {
            const draftMode = ui.getMeasurementConvolverPreviewMode(left, right, leftDraft, rightDraft);
            const draftPhaseMode = ui.getMeasurementConvolverDraftPhaseMode(leftDraft || rightDraft) || conv.phaseMode;
            const previewGainDb = ui.getMeasurementConvolverPreviewGain(draftMode, left, right, leftDraft, rightDraft);
            const nameValue = hasConvolverDraft
                ? (conv.draft?.presetName || deps.getMeasurementConvolverItemName(draftMode, previewGainDb, { phaseMode: draftPhaseMode }))
                : deps.getMeasurementConvolverItemName(draftMode, previewGainDb, { phaseMode: conv.phaseMode });
            if (deps.getDocument().activeElement !== elements.measurementConvolverPresetName) {
                elements.measurementConvolverPresetName.value = nameValue;
            }
            elements.measurementConvolverPresetName.disabled = !!draftPhaseMismatch || isCreatingConvolverPreset;
            elements.measurementConvolverPresetName.placeholder = hasConvolverDraft ? 'Preset name' : 'Type a name, or Take L/R/Both to stage';
        }
        if (elements.measurementConvolverWarnings) {
            const warnings = deps.buildMeasurementConvolverWarnings(convAnalyses);
            elements.measurementConvolverWarnings.innerHTML = warnings.map((warning) => `<div>${deps.escapeHtml(warning)}</div>`).join('');
            elements.measurementConvolverWarnings.classList.toggle('hidden', !warnings.length);
        }
        if (elements.measurementConvolverTakeLeftBtn) {
            elements.measurementConvolverTakeLeftBtn.disabled = !left || !convolverSourceSelection.take.left || isCreatingConvolverPreset;
            elements.measurementConvolverTakeLeftBtn.classList.toggle('is-active', !!left && convolverSourceSelection.take.left && !isCreatingConvolverPreset);
            elements.measurementConvolverTakeLeftBtn.setAttribute('aria-pressed', !!left && convolverSourceSelection.take.left && !isCreatingConvolverPreset ? 'true' : 'false');
        }
        if (elements.measurementConvolverTakeRightBtn) {
            elements.measurementConvolverTakeRightBtn.disabled = !right || !convolverSourceSelection.take.right || isCreatingConvolverPreset;
            elements.measurementConvolverTakeRightBtn.classList.toggle('is-active', !!right && convolverSourceSelection.take.right && !isCreatingConvolverPreset);
            elements.measurementConvolverTakeRightBtn.setAttribute('aria-pressed', !!right && convolverSourceSelection.take.right && !isCreatingConvolverPreset ? 'true' : 'false');
        }
        if (elements.measurementConvolverTakeBothBtn) {
            elements.measurementConvolverTakeBothBtn.disabled = !left || !right || !convolverSourceSelection.take.both || isCreatingConvolverPreset;
            elements.measurementConvolverTakeBothBtn.classList.toggle('is-active', !!left && !!right && convolverSourceSelection.take.both && !isCreatingConvolverPreset);
            elements.measurementConvolverTakeBothBtn.setAttribute('aria-pressed', !!left && !!right && convolverSourceSelection.take.both && !isCreatingConvolverPreset ? 'true' : 'false');
        }
        if (elements.measurementConvolverCreateBtn) {
            elements.measurementConvolverCreateBtn.disabled = !hasConvolverDraft || !!draftPhaseMismatch || !String(conv.draft?.presetName || '').trim() || isCreatingConvolverPreset;
            elements.measurementConvolverCreateBtn.textContent = isCreatingConvolverPreset ? 'Creating...' : 'Create Convolver Preset';
        }
    }

    function commitCustomHouseCurveField(input) {
        const custom = deps.ensureCustomHouseCurveState();
        if (!custom.activePointId) return;
        deps.updateCustomHouseCurvePoint(custom.activePointId, { [input.dataset.customHouseCurveField]: Number(input.value) });
    }

    function commitPeqEditorField(input, reRender) {
        const activeFilter = deps.getMeasurementPeqActiveFilter();
        if (!activeFilter) return;
        const field = input.dataset.measurementPeqField;
        const value = field === 'type' ? input.value : Number(input.value);
        deps.updateMeasurementPeqFilter(activeFilter.id, { [field]: value });
        if (reRender) deps.renderMeasurementPanel();
        deps.scheduleMeasurementGraphRender();
    }

    function handleMeasurementPeqStepClick(button) {
        const activeFilter = deps.getMeasurementPeqActiveFilter();
        if (!activeFilter) return;
        const container = deps.getElements().measurementPeqEditor;
        if (button.dataset.measurementPeqFrequencyStep !== undefined) {
            const input = container?.querySelector('#measurement-peq-freq');
            const step = Number(input?.step) || 1;
            const direction = Number(button.dataset.measurementPeqFrequencyStep || '0');
            const nextValue = deps.stepMeasurementPeqFrequency(activeFilter.id, direction, step);
            if (nextValue === null) return;
            if (input) input.value = String(nextValue);
        } else if (button.dataset.measurementPeqGainStep !== undefined) {
            const input = container?.querySelector('#measurement-peq-gain');
            const step = Number(input?.step) || 0.1;
            const direction = Number(button.dataset.measurementPeqGainStep || '0');
            const nextValue = deps.stepMeasurementPeqGain(activeFilter.id, direction, step);
            if (nextValue === null) return;
            if (input) input.value = nextValue.toFixed(1);
        } else {
            const input = container?.querySelector('#measurement-peq-q');
            const step = Number(input?.step) || 0.1;
            const direction = Number(button.dataset.measurementPeqQStep || '0');
            const nextValue = deps.stepMeasurementPeqQ(activeFilter.id, direction, step);
            if (nextValue === null) return;
            if (input) input.value = nextValue.toFixed(2);
        }
        deps.scheduleMeasurementGraphRender();
        deps.focusMeasurementPeqPanelContext();
    }

    let editorDelegationBound = false;
    // Dynamic panel regions rebuild their innerHTML on every render; their
    // listeners therefore live once on the stable containers via delegation.
    function bindMeasurementEditorDelegation() {
        if (editorDelegationBound) return;
        editorDelegationBound = true;
        const elements = deps.getElements();
        elements.measurementPeqChips?.addEventListener('click', (event) => {
            const button = event.target.closest('[data-measurement-peq-slot]');
            if (!button) return;
            const filterId = button.dataset.measurementPeqChip;
            if (filterId) {
                deps.selectMeasurementPeqFilter(filterId);
            } else {
                const created = deps.addMeasurementPeqFilter();
                if (!created) return;
            }
            deps.renderMeasurementPanel();
            deps.scheduleMeasurementGraphRender();
            deps.focusMeasurementPeqPanelContext();
        });
        elements.measurementCustomHouseCurveChips?.addEventListener('click', (event) => {
            const button = event.target.closest('[data-custom-house-curve-slot]');
            if (!button) return;
            const custom = deps.ensureCustomHouseCurveState();
            const pointId = button.dataset.customHouseCurvePoint;
            if (pointId) custom.activePointId = pointId;
            else if (!deps.addCustomHouseCurvePoint({ slot: Number(button.dataset.customHouseCurveSlot) })) return;
            deps.renderMeasurementPanel();
            deps.scheduleMeasurementGraphRender();
        });
        elements.measurementCustomHouseCurveEditor?.addEventListener('input', (event) => {
            const input = event.target.closest('[data-custom-house-curve-field]');
            if (!input) return;
            commitCustomHouseCurveField(input);
            deps.scheduleMeasurementGraphRender();
        });
        elements.measurementCustomHouseCurveEditor?.addEventListener('change', (event) => {
            const input = event.target.closest('[data-custom-house-curve-field]');
            if (!input) return;
            commitCustomHouseCurveField(input);
            deps.renderMeasurementPanel();
            deps.scheduleMeasurementGraphRender();
        });
        elements.measurementCustomHouseCurveEditor?.addEventListener('click', (event) => {
            const button = event.target.closest('[data-custom-house-curve-delete]');
            if (!button) return;
            deps.deleteCustomHouseCurvePoint(button.dataset.customHouseCurveDelete);
            deps.renderMeasurementPanel();
            deps.scheduleMeasurementGraphRender();
        });
        elements.measurementPeqEditor?.addEventListener('keydown', (event) => {
            if (!(event.target instanceof (deps.getInputElementType())) || event.target.type !== 'number') return;
            if (!event.target.matches('[data-measurement-peq-field]')) return;
            deps.handleMeasurementPeqNumberInputArrowKey(event);
        });
        elements.measurementPeqEditor?.addEventListener('input', (event) => {
            const input = event.target.closest('[data-measurement-peq-field]');
            if (!input) return;
            commitPeqEditorField(input, input.dataset.measurementPeqField === 'type');
        });
        elements.measurementPeqEditor?.addEventListener('change', (event) => {
            const input = event.target.closest('[data-measurement-peq-field]');
            if (!input) return;
            commitPeqEditorField(input, true);
        });
        elements.measurementPeqEditor?.addEventListener('click', (event) => {
            const stepButton = event.target.closest('[data-measurement-peq-frequency-step], [data-measurement-peq-gain-step], [data-measurement-peq-q-step]');
            if (stepButton) {
                handleMeasurementPeqStepClick(stepButton);
                return;
            }
            const deleteButton = event.target.closest('[data-measurement-peq-delete]');
            if (!deleteButton) return;
            deps.deleteMeasurementPeqFilter(deleteButton.dataset.measurementPeqDelete);
            deps.renderMeasurementPanel();
            deps.scheduleMeasurementGraphRender();
        });
    }

    return {
        init,
        renderMeasurementPanelEditorsSection,
        renderMeasurementPanelConvolverSection,
        syncMeasurementSummedSubTakeModes,
        renderMeasurementSlotChip,
        getMeasurementSlotChipStyle,
        bindMeasurementEditorDelegation,
    };
});
