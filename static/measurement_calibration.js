// SPDX-License-Identifier: AGPL-3.0-only
/** Measurement calibration files and house-curve editor/file lifecycle. */
(function (root, factory) {
    const api = factory(root);
    root.FXRouteMeasurementCalibration = api;
    if (typeof module === 'object' && module.exports) module.exports = api;
})(typeof globalThis !== 'undefined' ? globalThis : window, function (root) {
    'use strict';

    let deps = {
        getState: () => ({ measurement: {} }),
        getElements: () => ({}),
        fetch: (...args) => root.fetch(...args),
        getFileType: () => root.File,
        getFormDataType: () => root.FormData,
        confirm: (message) => root.confirm(message),
        showToast: () => {},
        renderMeasurementPanel: () => {},
        scheduleMeasurementGraphRender: () => {},
        fetchMeasurements: async () => {},
        updateMeasurementConvolverField: () => {},
        ensureMeasurementConvolverState: () => ({}),
        setMeasurementActiveEditor: () => {},
        measurementXToFrequency: () => 20,
        measurementYToDb: () => 0,
        triggerBlobDownload: () => {},
        getDownloadFilenameFromResponse: () => '',
    };

    function init(overrides) {
        deps = Object.assign(deps, overrides || {});
    }

    function applyMeasurementFileCatalog(data) {
        const measurement = deps.getState().measurement;
        measurement.calibrationOptions = Array.isArray(data.calibrations) ? data.calibrations : [];
        measurement.selectedCalibrationRef = String(data.active_calibration_file_id || '');
        measurement.houseCurveOptions = Array.isArray(data.house_curves) ? data.house_curves : [];
    }

    function validateMeasurementCalibrationSelection() {
        const measurement = deps.getState().measurement;
        if (measurement.selectedCalibrationRef && !measurement.calibrationOptions.some(item => item.id === measurement.selectedCalibrationRef)) {
            measurement.selectedCalibrationRef = '';
        }
    }

    function measurementHasCalibrationSelected() {
        return !!(deps.getState().measurement.calibrationFilename || deps.getState().measurement.selectedCalibrationRef);
    }

    function applyMeasurementCalibrationState(data) {
        if (!data || typeof data !== 'object') return;
        deps.getState().measurement.calibrationOptions = Array.isArray(data.calibrations) ? data.calibrations : deps.getState().measurement.calibrationOptions;
        deps.getState().measurement.selectedCalibrationRef = String(data.active_calibration_file_id || '');
        if (deps.getState().measurement.selectedCalibrationRef && !deps.getState().measurement.calibrationOptions.some(item => item.id === deps.getState().measurement.selectedCalibrationRef)) {
            deps.getState().measurement.selectedCalibrationRef = '';
        }
        deps.getState().measurement.calibrationFilename = '';
        if (deps.getElements().measurementCalibrationFile) deps.getElements().measurementCalibrationFile.value = '';
    }

    async function setActiveMeasurementCalibration(calibrationFileId) {
        deps.getState().measurement.calibrationUpdating = true;
        deps.renderMeasurementPanel();
        try {
            const resp = await deps.fetch('/api/measurements/calibrations/active', {
                method: 'PATCH',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ calibration_file_id: calibrationFileId || '' }),
            });
            const data = await resp.json().catch(() => ({}));
            if (!resp.ok) throw new Error(data.detail || 'Failed to save calibration selection');
            applyMeasurementCalibrationState(data);
            deps.showToast(calibrationFileId ? 'Calibration file selected' : 'Calibration disabled', 'success');
        } catch (error) {
            console.error('setActiveMeasurementCalibration failed', error);
            deps.getState().measurement.statusText = error.message || 'Failed to save calibration selection';
            deps.showToast(deps.getState().measurement.statusText, 'error');
            await deps.fetchMeasurements();
        } finally {
            deps.getState().measurement.calibrationUpdating = false;
            deps.renderMeasurementPanel();
        }
    }

    async function uploadMeasurementCalibration(file) {
        if (!file) return;
        deps.getState().measurement.calibrationUpdating = true;
        deps.getState().measurement.calibrationFilename = file.name || 'calibration.txt';
        deps.renderMeasurementPanel();
        const formData = new (deps.getFormDataType())();
        formData.append('calibration_file', file);
        try {
            const resp = await deps.fetch('/api/measurements/calibrations', { method: 'POST', body: formData });
            const data = await resp.json().catch(() => ({}));
            if (!resp.ok) throw new Error(data.detail || 'Failed to upload calibration file');
            applyMeasurementCalibrationState(data);
            deps.showToast('Calibration file uploaded and selected', 'success');
        } catch (error) {
            console.error('uploadMeasurementCalibration failed', error);
            deps.getState().measurement.statusText = error.message || 'Failed to upload calibration file';
            deps.showToast(deps.getState().measurement.statusText, 'error');
        } finally {
            deps.getState().measurement.calibrationUpdating = false;
            deps.renderMeasurementPanel();
        }
    }

    async function downloadSelectedMeasurementCalibration() {
        const calibrationId = deps.getState().measurement.selectedCalibrationRef || '';
        const selected = (deps.getState().measurement.calibrationOptions || []).find(option => option.id === calibrationId);
        if (!calibrationId || !selected) return;
        deps.getState().measurement.calibrationExporting = true;
        deps.renderMeasurementPanel();
        try {
            const resp = await deps.fetch(`/api/measurements/calibrations/${encodeURIComponent(calibrationId)}/export`);
            if (!resp.ok) {
                const data = await resp.json().catch(() => ({}));
                throw new Error(data.detail || 'Failed to export calibration file');
            }
            deps.triggerBlobDownload(await resp.blob(), deps.getDownloadFilenameFromResponse(resp, selected.filename || 'calibration.txt'));
        } catch (error) {
            console.error('downloadSelectedMeasurementCalibration failed', error);
            deps.getState().measurement.statusText = error.message || 'Failed to export calibration file';
            deps.showToast(deps.getState().measurement.statusText, 'error');
        } finally {
            deps.getState().measurement.calibrationExporting = false;
            deps.renderMeasurementPanel();
        }
    }

    async function deleteSelectedMeasurementCalibration() {
        const calibrationId = deps.getState().measurement.selectedCalibrationRef || '';
        const selected = (deps.getState().measurement.calibrationOptions || []).find(option => option.id === calibrationId);
        if (!calibrationId || !selected) {
            deps.showToast('No calibration file selected to delete', 'warning');
            return;
        }
        if (deps.getState().measurement.startInFlight || deps.getState().measurement.activeJobId) {
            deps.showToast('Cannot delete calibration during an active measurement', 'warning');
            return;
        }
        if (!deps.confirm(`Delete calibration file "${selected.filename || calibrationId}"?`)) return;
        deps.getState().measurement.calibrationDeleting = true;
        deps.renderMeasurementPanel();
        try {
            const resp = await deps.fetch(`/api/measurements/calibrations/${encodeURIComponent(calibrationId)}`, { method: 'DELETE' });
            const data = await resp.json().catch(() => ({}));
            if (!resp.ok) throw new Error(data.detail || 'Failed to delete calibration file');
            applyMeasurementCalibrationState(data);
            deps.showToast('Calibration file deleted', 'success');
        } catch (error) {
            console.error('deleteSelectedMeasurementCalibration failed', error);
            deps.getState().measurement.statusText = error.message || 'Failed to delete calibration file';
            deps.showToast(deps.getState().measurement.statusText, 'error');
        } finally {
            deps.getState().measurement.calibrationDeleting = false;
            deps.renderMeasurementPanel();
        }
    }

    function applyMeasurementHouseCurveState(data) {
        if (!data || typeof data !== 'object') return;
        deps.getState().measurement.houseCurveOptions = Array.isArray(data.house_curves) ? data.house_curves : deps.getState().measurement.houseCurveOptions;
        deps.getState().measurement.houseCurveFilename = '';
        if (deps.getElements().measurementHouseCurveFile) deps.getElements().measurementHouseCurveFile.value = '';
    }

    async function uploadMeasurementHouseCurve(file) {
        if (!file) return;
        deps.getState().measurement.houseCurveUpdating = true;
        deps.getState().measurement.houseCurveFilename = file.name || 'house-curve.txt';
        deps.renderMeasurementPanel();
        const formData = new (deps.getFormDataType())();
        formData.append('house_curve_file', file);
        try {
            const resp = await deps.fetch('/api/measurements/house-curves', { method: 'POST', body: formData });
            const data = await resp.json().catch(() => ({}));
            if (!resp.ok) throw new Error(data.detail || 'Failed to upload house curve file');
            applyMeasurementHouseCurveState(data);
            const uploadedId = data.uploaded_house_curve_id ? `house:${data.uploaded_house_curve_id}` : '';
            if (uploadedId) deps.updateMeasurementConvolverField('targetCurve', uploadedId);
            deps.showToast('House curve uploaded and selected', 'success');
        } catch (error) {
            console.error('uploadMeasurementHouseCurve failed', error);
            deps.getState().measurement.statusText = error.message || 'Failed to upload house curve file';
            deps.showToast(deps.getState().measurement.statusText, 'error');
        } finally {
            deps.getState().measurement.houseCurveUpdating = false;
            deps.renderMeasurementPanel();
        }
    }

    function ensureCustomHouseCurveState() {
        const measurement = deps.getState().measurement || (deps.getState().measurement = {});
        if (!measurement.customHouseCurve || typeof measurement.customHouseCurve !== 'object') {
            measurement.customHouseCurve = { open: false, displayTarget: 'actual', points: [], activePointId: null, dragPointId: null, name: '', nameTouched: false, saving: false };
        }
        const custom = measurement.customHouseCurve;
        custom.displayTarget = custom.displayTarget === 'editing-custom-house-curve' ? custom.displayTarget : 'actual';
        if (!Array.isArray(custom.points)) custom.points = [];
        const usedSlots = new Set();
        custom.points = custom.points.slice(0, 8);
        custom.points.forEach((point) => {
            let slot = Number(point?.slot);
            if (!Number.isInteger(slot) || slot < 0 || slot >= 8 || usedSlots.has(slot)) {
                slot = Array.from({ length: 8 }, (_, candidate) => candidate).find((candidate) => !usedSlots.has(candidate));
            }
            usedSlots.add(slot);
            point.slot = slot;
        });
        if (!custom.points.some((point) => point.id === custom.activePointId)) custom.activePointId = custom.points[0]?.id || null;
        if (!custom.points.some((point) => point.id === custom.dragPointId)) custom.dragPointId = null;
        return custom;
    }

    function getCustomHouseCurveNameSuggestion() {
        const normalize = (value) => String(value || '').trim().toLowerCase().replace(/[-_.]+/g, ' ').replace(/\s+/g, ' ');
        const used = new Set((deps.getState().measurement?.houseCurveOptions || []).map((curve) => normalize(curve.filename)));
        let index = 1;
        while (used.has(normalize('Custom House Curve ' + index))) index += 1;
        return 'Custom House Curve ' + index;
    }

    function openCustomHouseCurveEditor() {
        const custom = ensureCustomHouseCurveState();
        deps.setMeasurementActiveEditor('houseCurve');
        if (!custom.points.length) addCustomHouseCurvePoint({ slot: 0, freqHz: 20, gainDb: 0 });
        if (!custom.nameTouched || !custom.name.trim()) custom.name = getCustomHouseCurveNameSuggestion();
        deps.renderMeasurementPanel();
        deps.scheduleMeasurementGraphRender();
    }

    function handleMeasurementTargetCurveSelection(value) {
        if (value === 'create-custom-house-curve' || value === 'editing-custom-house-curve') {
            openCustomHouseCurveEditor();
            return;
        }
        deps.setMeasurementActiveEditor('none');
        deps.updateMeasurementConvolverField('targetCurve', value);
        deps.renderMeasurementPanel();
        deps.scheduleMeasurementGraphRender();
    }

    function addCustomHouseCurvePoint(defaults = {}) {
        const custom = ensureCustomHouseCurveState();
        const fallbackFrequencies = [20, 40, 80, 160, 320, 1000, 5000, 20000];
        const requestedSlot = Number(defaults.slot);
        const freeSlots = Array.from({ length: 8 }, (_, slot) => slot).filter((slot) => !custom.points.some((point) => Number(point.slot) === slot));
        const slot = Number.isInteger(requestedSlot) && requestedSlot >= 0 && requestedSlot < 8 && freeSlots.includes(requestedSlot)
            ? requestedSlot
            : freeSlots[0];
        if (slot === undefined) return null;
        const point = {
            id: 'house-point-' + Date.now() + '-' + Math.random().toString(16).slice(2),
            slot,
            freqHz: Math.round(Math.min(20000, Math.max(20, Number(defaults.freqHz) || fallbackFrequencies[slot]))),
            gainDb: Number(Math.min(24, Math.max(-24, Number(defaults.gainDb) || 0)).toFixed(1)),
        };
        custom.points.push(point);
        custom.activePointId = point.id;
        return point;
    }

    function resetCustomHouseCurveDraft() {
        const custom = ensureCustomHouseCurveState();
        custom.points = [];
        custom.activePointId = null;
        custom.dragPointId = null;
        addCustomHouseCurvePoint({ slot: 0, freqHz: 20, gainDb: 0 });
    }

    function updateCustomHouseCurvePoint(pointId, updates = {}) {
        const point = ensureCustomHouseCurveState().points.find((item) => item.id === pointId);
        if (!point) return;
        if (updates.freqHz !== undefined) point.freqHz = Math.round(Math.min(20000, Math.max(20, Number(updates.freqHz) || 20)));
        if (updates.gainDb !== undefined) point.gainDb = Number(Math.min(24, Math.max(-24, Number(updates.gainDb) || 0)).toFixed(1));
    }

    function addCustomHouseCurvePointAtPosition({ x, y, bounds, range }) {
        const frequencyHz = deps.measurementXToFrequency(x, bounds);
        const gainDb = Math.min(range.maxDb, Math.max(range.minDb, deps.measurementYToDb(y, bounds, range)));
        return addCustomHouseCurvePoint({ freqHz: frequencyHz, gainDb: gainDb });
    }

    function deleteCustomHouseCurvePoint(pointId) {
        const custom = ensureCustomHouseCurveState();
        custom.points = custom.points.filter((point) => point.id !== pointId);
        custom.activePointId = custom.points[0]?.id || null;
    }

    function serializeCustomHouseCurvePoints(points) {
        return points
            .map((point) => [Number(point.freqHz), Number(point.gainDb)])
            .sort((left, right) => left[0] - right[0])
            .map(([frequency, gain]) => String(frequency) + '\t' + gain.toFixed(1))
            .join('\n') + '\n';
    }

    async function createCustomHouseCurve() {
        const custom = ensureCustomHouseCurveState();
        const name = String(custom.name || '').trim();
        if (!name || custom.points.length < 2 || custom.saving) return;
        const sorted = custom.points.map((point) => [Number(point.freqHz), Number(point.gainDb)]).sort((left, right) => left[0] - right[0]);
        if (sorted.some((point, index) => index && point[0] <= sorted[index - 1][0])) {
            deps.showToast('House curve frequencies must be unique', 'warning');
            return;
        }
        custom.saving = true;
        deps.renderMeasurementPanel();
        const safeFilename = name + '.txt';
        const file = new (deps.getFileType())([serializeCustomHouseCurvePoints(custom.points)], safeFilename, { type: 'text/plain' });
        const formData = new (deps.getFormDataType())();
        formData.append('house_curve_file', file);
        try {
            const resp = await deps.fetch('/api/measurements/house-curves', { method: 'POST', body: formData });
            const data = await resp.json().catch(() => ({}));
            if (!resp.ok) throw new Error(data.detail || 'Failed to create target curve');
            applyMeasurementHouseCurveState(data);
            const uploadedKey = data.uploaded_house_curve_id ? 'house:' + data.uploaded_house_curve_id : '';
            if (uploadedKey) deps.updateMeasurementConvolverField('targetCurve', uploadedKey);
            deps.setMeasurementActiveEditor('none');
            custom.points = [];
            custom.activePointId = null;
            custom.name = '';
            custom.nameTouched = false;
            deps.showToast('Target curve created and selected', 'success');
        } catch (error) {
            deps.getState().measurement.statusText = error.message || 'Failed to create target curve';
            deps.showToast(deps.getState().measurement.statusText, 'error');
        } finally {
            custom.saving = false;
            deps.renderMeasurementPanel();
            deps.scheduleMeasurementGraphRender();
        }
    }

    async function downloadSelectedMeasurementHouseCurve() {
        const houseCurveId = deps.getElements().measurementHouseCurveSelect ? (deps.getElements().measurementHouseCurveSelect.value || '') : '';
        const selected = (deps.getState().measurement.houseCurveOptions || []).find(option => option.id === houseCurveId);
        if (!houseCurveId || !selected) return;
        deps.getState().measurement.houseCurveExporting = true;
        deps.renderMeasurementPanel();
        try {
            const resp = await deps.fetch(`/api/measurements/house-curves/${encodeURIComponent(houseCurveId)}/export`);
            if (!resp.ok) {
                const data = await resp.json().catch(() => ({}));
                throw new Error(data.detail || 'Failed to export house curve file');
            }
            deps.triggerBlobDownload(await resp.blob(), deps.getDownloadFilenameFromResponse(resp, `${selected.filename || 'house-curve'}.txt`));
        } catch (error) {
            console.error('downloadSelectedMeasurementHouseCurve failed', error);
            deps.getState().measurement.statusText = error.message || 'Failed to export house curve file';
            deps.showToast(deps.getState().measurement.statusText, 'error');
        } finally {
            deps.getState().measurement.houseCurveExporting = false;
            deps.renderMeasurementPanel();
        }
    }

    async function deleteSelectedMeasurementHouseCurve() {
        const houseCurveId = deps.getElements().measurementHouseCurveSelect ? (deps.getElements().measurementHouseCurveSelect.value || '') : '';
        const selected = (deps.getState().measurement.houseCurveOptions || []).find(option => option.id === houseCurveId);
        if (!houseCurveId || !selected) {
            deps.showToast('No house curve file selected to delete', 'warning');
            return;
        }
        if (!deps.confirm(`Delete house curve file "${selected.filename || houseCurveId}"?`)) return;
        deps.getState().measurement.houseCurveDeleting = true;
        deps.renderMeasurementPanel();
        try {
            const resp = await deps.fetch(`/api/measurements/house-curves/${encodeURIComponent(houseCurveId)}`, { method: 'DELETE' });
            const data = await resp.json().catch(() => ({}));
            if (!resp.ok) throw new Error(data.detail || 'Failed to delete house curve file');
            applyMeasurementHouseCurveState(data);
            const conv = deps.ensureMeasurementConvolverState();
            if (conv.targetCurve === `house:${houseCurveId}`) conv.targetCurve = 'neutral';
            deps.showToast('House curve file deleted', 'success');
        } catch (error) {
            console.error('deleteSelectedMeasurementHouseCurve failed', error);
            deps.getState().measurement.statusText = error.message || 'Failed to delete house curve file';
            deps.showToast(deps.getState().measurement.statusText, 'error');
        } finally {
            deps.getState().measurement.houseCurveDeleting = false;
            deps.renderMeasurementPanel();
        }
    }

    return {
        init, applyMeasurementFileCatalog, validateMeasurementCalibrationSelection,
        measurementHasCalibrationSelected, applyMeasurementCalibrationState,
        setActiveMeasurementCalibration, uploadMeasurementCalibration,
        downloadSelectedMeasurementCalibration, deleteSelectedMeasurementCalibration,
        applyMeasurementHouseCurveState, uploadMeasurementHouseCurve,
        ensureCustomHouseCurveState, getCustomHouseCurveNameSuggestion,
        openCustomHouseCurveEditor, handleMeasurementTargetCurveSelection,
        addCustomHouseCurvePoint, resetCustomHouseCurveDraft, updateCustomHouseCurvePoint,
        addCustomHouseCurvePointAtPosition, deleteCustomHouseCurvePoint,
        serializeCustomHouseCurvePoints, createCustomHouseCurve,
        downloadSelectedMeasurementHouseCurve, deleteSelectedMeasurementHouseCurve,
    };
});
