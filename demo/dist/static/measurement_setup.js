// SPDX-License-Identifier: AGPL-3.0-only
/** Measurement capture-input settings and reference selection. */
(function (root, factory) {
    const api = factory(root);
    root.FXRouteMeasurementSetup = api;
    if (typeof module === 'object' && module.exports) module.exports = api;
})(typeof globalThis !== 'undefined' ? globalThis : window, function (root) {
    'use strict';

    let deps = {
        getState: () => ({ measurement: {} }),
        fetch: (...args) => root.fetch(...args),
        renderMeasurementPanel: () => {},
        measurementModeNoteText: () => '',
        describeMeasurementScope: () => '',
    };
    let measurementSettingsRevision = 0;
    let measurementInputsRequestRevision = 0;

    function init(overrides) {
        deps = Object.assign(deps, overrides || {});
    }

    function getMeasurementSettingsRevision() {
        return measurementSettingsRevision;
    }

    function applyMeasurementSetupSettings(settings = {}, fields = null) {
        if (!settings || typeof settings !== 'object') return;
        const applies = (field) => !fields || fields.has(field);
        if (applies('selectedInputId') && Object.prototype.hasOwnProperty.call(settings, 'selectedInputId')) {
            deps.getState().measurement.selectedInputLegacyId = String(settings.selectedInputId || '');
        }
        if (applies('selectedInputKey') && Object.prototype.hasOwnProperty.call(settings, 'selectedInputKey')) {
            deps.getState().measurement.selectedInputKey = String(settings.selectedInputKey || '');
        }
        if ((applies('selectedInputId') || applies('selectedInputKey')) && Object.prototype.hasOwnProperty.call(settings, 'selectedInputConfigured')) {
            deps.getState().measurement.selectedInputConfigured = !!settings.selectedInputConfigured;
        }
        if (applies('selectedMicInputChannel') && Object.prototype.hasOwnProperty.call(settings, 'selectedMicInputChannel')) {
            deps.getState().measurement.selectedMicInputChannel = String(settings.selectedMicInputChannel || '1');
        }
        if (applies('selectedReferenceInputChannel') && Object.prototype.hasOwnProperty.call(settings, 'selectedReferenceInputChannel')) {
            deps.getState().measurement.selectedReferenceInputChannel = String(settings.selectedReferenceInputChannel || '');
        }
        if (applies('selectedReferenceInputChannelLeft') && Object.prototype.hasOwnProperty.call(settings, 'selectedReferenceInputChannelLeft')) {
            deps.getState().measurement.selectedReferenceInputChannelLeft = String(settings.selectedReferenceInputChannelLeft || '');
        }
        if (applies('selectedReferenceInputChannelRight') && Object.prototype.hasOwnProperty.call(settings, 'selectedReferenceInputChannelRight')) {
            deps.getState().measurement.selectedReferenceInputChannelRight = String(settings.selectedReferenceInputChannelRight || '');
        }
        if (applies('measurementSampleRate') && Object.prototype.hasOwnProperty.call(settings, 'measurementSampleRate')) {
            deps.getState().measurement.measurementSampleRate = String(settings.measurementSampleRate || '48000');
        }
        normalizeMeasurementInputChannelSelections();
    }

    async function saveMeasurementSetupSettings(patch = {}) {
        const revision = ++measurementSettingsRevision;
        try {
            const resp = await deps.fetch('/api/measurements/settings', {
                method: 'PATCH',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(patch),
            });
            const data = await resp.json().catch(() => ({}));
            if (!resp.ok) throw new Error(data.detail || 'Failed to save measurement settings');
            if (revision === measurementSettingsRevision) {
                applyMeasurementSetupSettings(data.measurement_settings || {}, new Set(Object.keys(patch)));
            }
            deps.renderMeasurementPanel();
        } catch (error) {
            console.error('saveMeasurementSetupSettings failed', error);
        }
    }

    function applyMeasurementInputSelection(inputId) {
        deps.getState().measurement.selectedInputId = String(inputId || '');
        const selectedInput = getSelectedMeasurementInput();
        deps.getState().measurement.selectedInputKey = selectedInput?.persistentId || '';
        deps.getState().measurement.selectedInputConfigured = !!selectedInput;
        deps.getState().measurement.selectedInputUnavailable = false;
        normalizeMeasurementInputChannelSelections();
        void saveMeasurementSetupSettings({
            selectedInputId: deps.getState().measurement.selectedInputId,
            selectedInputKey: deps.getState().measurement.selectedInputKey,
            selectedMicInputChannel: deps.getState().measurement.selectedMicInputChannel || '1',
            selectedReferenceInputChannel: deps.getState().measurement.selectedReferenceInputChannel || '',
            selectedReferenceInputChannelLeft: deps.getState().measurement.selectedReferenceInputChannelLeft || '',
            selectedReferenceInputChannelRight: deps.getState().measurement.selectedReferenceInputChannelRight || '',
        });
        deps.renderMeasurementPanel();
    }

    async function fetchMeasurementInputs() {
        const requestRevision = ++measurementInputsRequestRevision;
        deps.getState().measurement.inputsLoading = true;
        deps.renderMeasurementPanel();
        const revisionAtStart = measurementSettingsRevision;
        try {
            const resp = await deps.fetch('/api/measurements/inputs');
            if (!resp.ok) throw new Error('Failed to fetch measurement inputs');
            const data = await resp.json();
            if (requestRevision !== measurementInputsRequestRevision) return;
            const inputs = Array.isArray(data.inputs) && data.inputs.length
                ? data.inputs.map((input, index) => ({
                    id: String(input.id || `input-${index + 1}`),
                    label: String(input.label || input.id || `Input ${index + 1}`),
                    note: String(input.note || ''),
                    channels: Math.max(1, Number(input.channels || 1)),
                    supportedRates: Array.isArray(input.supported_rates) ? input.supported_rates.map(Number).filter(rate => Number.isFinite(rate) && rate > 0) : [],
                    measurementSampleRate: Number(input.measurement_sample_rate || input.sample_rate || 0),
                    nodeName: String(input.node_name || ''),
                    persistentId: String(input.persistent_id || ''),
                }))
                : [];
            const previousInputId = deps.getState().measurement.selectedInputId;
            const previousInputKey = deps.getState().measurement.selectedInputKey;
            const selection = data.selection && typeof data.selection === 'object' ? data.selection : {};
            // A settings save that started while this request was in flight means
            // the response reflects pre-change settings; its selection snapshot is
            // stale and must not clobber a deliberate re-selection.
            const selectionStale = measurementSettingsRevision !== revisionAtStart;
            deps.getState().measurement.inputs = inputs;
            deps.getState().measurement.hostCaptureAvailable = !!data.capture_available && !!inputs.length;
            deps.getState().measurement.captureAvailable = deps.getState().measurement.hostCaptureAvailable;
            deps.getState().measurement.modeNote = deps.measurementModeNoteText();
            if (!selectionStale) {
                deps.getState().measurement.selectedInputId = String(selection.input_id || '');
                deps.getState().measurement.selectedInputKey = String(selection.persistent_id || previousInputKey || '');
                deps.getState().measurement.selectedInputConfigured = !!selection.configured;
                deps.getState().measurement.selectedInputUnavailable = !!selection.unavailable;
            }
            const selectedMeasurementInput = inputs.find(input => input.id === deps.getState().measurement.selectedInputId);
            if (selectedMeasurementInput?.measurementSampleRate > 0) {
                deps.getState().measurement.measurementSampleRate = String(selectedMeasurementInput.measurementSampleRate);
            }
            normalizeMeasurementInputChannelSelections();
            if (!selectionStale && !deps.getState().measurement.startInFlight && !deps.getState().measurement.activeJobId
                && !deps.getState().measurement.selectedInputUnavailable && deps.getState().measurement.hostCaptureAvailable) {
                deps.getState().measurement.statusText = deps.describeMeasurementScope(data.scope_note);
            }
            if (!selectionStale && deps.getState().measurement.selectedInputId && (
                !deps.getState().measurement.selectedInputConfigured
                || previousInputId !== deps.getState().measurement.selectedInputId
                || previousInputKey !== deps.getState().measurement.selectedInputKey
            )) {
                deps.getState().measurement.selectedInputConfigured = true;
                void saveMeasurementSetupSettings({
                    selectedInputId: deps.getState().measurement.selectedInputId,
                    selectedInputKey: deps.getState().measurement.selectedInputKey,
                });
            }
        } catch (error) {
            if (requestRevision !== measurementInputsRequestRevision) return;
            console.error('fetchMeasurementInputs failed', error);
            deps.getState().measurement.inputs = [];
            deps.getState().measurement.hostCaptureAvailable = false;
            deps.getState().measurement.captureAvailable = false;
            deps.getState().measurement.modeNote = deps.measurementModeNoteText();
            if (measurementSettingsRevision === revisionAtStart) {
                deps.getState().measurement.selectedInputId = '';
                deps.getState().measurement.selectedInputUnavailable = deps.getState().measurement.selectedInputConfigured;
                deps.getState().measurement.statusText = error.message || 'Failed to load capture inputs';
            }
        } finally {
            if (requestRevision === measurementInputsRequestRevision) {
                deps.getState().measurement.inputsLoading = false;
                deps.renderMeasurementPanel();
            }
        }
    }

    function getSelectedMeasurementInput() {
        const measurementState = deps.getState().measurement || {};
        return (measurementState.inputs || []).find(input => input.id === measurementState.selectedInputId) || null;
    }

    function getSelectedMeasurementInputChannelCount() {
        return Math.max(1, Number(getSelectedMeasurementInput()?.channels || 1));
    }

    function normalizeMeasurementInputChannelSelections() {
        const measurementState = deps.getState().measurement || {};
        const selectedInput = getSelectedMeasurementInput();
        const channelCountKnown = !!selectedInput;
        const channelCount = channelCountKnown ? Math.max(1, Number(selectedInput.channels || 1)) : 1;
        if (channelCountKnown) {
            const micChannel = Math.max(1, Math.min(channelCount, Number(measurementState.selectedMicInputChannel || 1)));
            measurementState.selectedMicInputChannel = String(micChannel);
        } else if (!measurementState.selectedMicInputChannel) {
            measurementState.selectedMicInputChannel = '1';
        }

        const normalizeReferenceChannel = (value) => {
            if (!value) return '';
            const referenceChannel = Number(value);
            return Number.isFinite(referenceChannel) && referenceChannel >= 1 && (!channelCountKnown || referenceChannel <= channelCount)
                ? String(referenceChannel)
                : '';
        };

        if (!channelCountKnown) {
            // The input topology is still unknown: the settings response of
            // /api/measurements can resolve before /api/measurements/inputs, so
            // deciding between split and shared references here would be a guess.
            // The persisted settings are authoritative — validate them and leave
            // their shape alone. Collapsing a stored L/R pair (3/4) onto the shared
            // value would silently lose Ref R for the rest of the session. The
            // topology-aware pass below runs again once the input list is loaded.
            measurementState.selectedReferenceInputChannelLeft = normalizeReferenceChannel(measurementState.selectedReferenceInputChannelLeft);
            measurementState.selectedReferenceInputChannelRight = normalizeReferenceChannel(measurementState.selectedReferenceInputChannelRight);
            measurementState.selectedReferenceInputChannel = normalizeReferenceChannel(measurementState.selectedReferenceInputChannel);
            return;
        }

        const splitReferences = channelCount >= 3;
        let left = normalizeReferenceChannel(measurementState.selectedReferenceInputChannelLeft);
        let right = normalizeReferenceChannel(measurementState.selectedReferenceInputChannelRight);
        if (!splitReferences) {
            // 2-channel (and single-channel) interfaces keep one shared reference.
            left = normalizeReferenceChannel(measurementState.selectedReferenceInputChannel);
            right = left;
        } else if (!left && !right) {
            // Seed the split fields from a previously selected shared reference.
            left = right = normalizeReferenceChannel(measurementState.selectedReferenceInputChannel);
        }
        if (left === measurementState.selectedMicInputChannel) left = '';
        if (right === measurementState.selectedMicInputChannel) right = '';
        measurementState.selectedReferenceInputChannelLeft = left;
        measurementState.selectedReferenceInputChannelRight = right;
        // Shared field: the single 2-channel selection, or the common L/R value.
        measurementState.selectedReferenceInputChannel = left && left === right ? left : (left || right || '');
    }

    function getMeasurementReferenceWarning() {
        // A split L/R reference can never collide with the mic channel here:
        // normalizeMeasurementInputChannelSelections() clears the affected side as
        // soon as the input topology is known, so modelling that conflict would
        // describe a state the UI cannot reach. The one reachable conflict is the
        // shared reference while the topology is still unknown — that pass only
        // validates persisted values and deliberately leaves their shape alone.
        const measurementState = deps.getState().measurement || {};
        const reference = String(measurementState.selectedReferenceInputChannel || '');
        if (!reference) return '';
        if (reference !== String(measurementState.selectedMicInputChannel || '')) return '';
        return 'Electrical reference disabled: mic and reference must use different input channels.';
    }

    function appendMeasurementReferenceFields(formData) {
        normalizeMeasurementInputChannelSelections();
        const measurementState = deps.getState().measurement || {};
        if (getSelectedMeasurementInputChannelCount() >= 3) {
            formData.append('reference_input_channel_left', measurementState.selectedReferenceInputChannelLeft || '');
            formData.append('reference_input_channel_right', measurementState.selectedReferenceInputChannelRight || '');
        }
        formData.append('reference_input_channel', getMeasurementReferenceWarning() ? '' : (measurementState.selectedReferenceInputChannel || ''));
    }

    return {
        init, getMeasurementSettingsRevision,
        applyMeasurementSetupSettings, saveMeasurementSetupSettings,
        applyMeasurementInputSelection, fetchMeasurementInputs,
        getSelectedMeasurementInput, getSelectedMeasurementInputChannelCount,
        normalizeMeasurementInputChannelSelections, getMeasurementReferenceWarning,
        appendMeasurementReferenceFields,
    };
});
