// SPDX-License-Identifier: AGPL-3.0-only
/** Saved measurement actions: save, delete and merge. */
(function (root, factory) {
    const api = factory(root);
    root.FXRouteMeasurementSavedActions = api;
    if (typeof module === 'object' && module.exports) module.exports = api;
})(typeof globalThis !== 'undefined' ? globalThis : window, function (root) {
    'use strict';

    let deps = {
        getState: () => ({ measurement: {} }),
        fetch: (...args) => root.fetch(...args),
        showToast: () => {},
        renderMeasurementPanel: () => {},
        fetchMeasurements: async () => {},
        formatTransitionErrorDetail: (_detail, fallback) => fallback,
        normalizeMeasurementEntry: (entry) => entry,
        getVisibleMeasurementEntries: () => [],
        confirm: (message) => root.confirm(message),
        prompt: (message, defaultValue) => root.prompt(message, defaultValue),
    };

    function init(overrides) {
        deps = Object.assign(deps, overrides || {});
    }

    async function saveCurrentMeasurement() {
        const current = deps.getState().measurement.currentMeasurement;
        const autoSubMeasurements = Array.isArray(deps.getState().measurement.autoSubMeasurements)
            ? deps.getState().measurement.autoSubMeasurements
            : [];
        const hasAutoSub = autoSubMeasurements.length > 0;
        const hasPending = !hasAutoSub && current && !deps.getState().measurement.currentMeasurementSaved;
        if (!hasAutoSub && !hasPending) return;
        if (deps.getState().measurement.saveInFlight) return;

        const repeatMeasurements = Array.isArray(deps.getState().measurement.pendingRepeatMeasurements)
            ? deps.getState().measurement.pendingRepeatMeasurements
            : [];
        const baseName = hasAutoSub
            ? ((deps.getState().measurement.currentMeasurementName || '').trim() || 'AutoSub')
            : ((deps.getState().measurement.currentMeasurementName || '').trim() || 'L/R Repeat');
        let payload;
        if (hasAutoSub) {
            // Save AutoSub measurements: split each entry by its channel traces
            const measurements = [];
            autoSubMeasurements.forEach((measurement) => {
                const traces = Array.isArray(measurement.traces) ? measurement.traces : [];
                traces.forEach((trace) => {
                    const ch = String(trace.role || trace.channel || '').toLowerCase() === 'right' ? 'right' : 'left';
                    const traceLabel = String(trace.label || measurement.name || 'AutoSub');
                    const suffix = traceLabel.replace(/^AutoSub\s+/, '');
                    const name = suffix ? `${baseName} ${suffix}` : baseName;
                    measurements.push({
                        id: `${measurement.id}-${ch}`,
                        name,
                        channel: ch,
                        traces: [{...trace, channel: ch}],
                        measurement_kind: measurement.measurement_kind || 'auto_sub',
                        autosub_meta: measurement.autosub_meta || null,
                    });
                });
            });
            payload = { measurements };
        } else if (repeatMeasurements.length) {
            payload = {
                measurements: repeatMeasurements.map((measurement) => {
                    const item = JSON.parse(JSON.stringify(measurement));
                    item.name = `${baseName} · ${String(item.channel || '').toLowerCase() === 'right' ? 'R' : 'L'}`;
                    return item;
                }),
            };
        } else {
            payload = JSON.parse(JSON.stringify(current));
            payload.name = (deps.getState().measurement.currentMeasurementName || current.name || '').trim() || current.name || 'Measurement';
        }

        deps.getState().measurement.saveInFlight = true;
        deps.getState().measurement.statusText = 'Saving current measurement…';
        deps.renderMeasurementPanel();
        try {
            const resp = await deps.fetch('/api/measurements/save', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload),
            });
            const data = await resp.json().catch(() => ({}));
            if (!resp.ok) throw new Error(deps.formatTransitionErrorDetail(data.detail, 'Failed to save measurement'));
            const savedMeasurements = Array.isArray(data.measurements)
                ? data.measurements.map((measurement, index) => deps.normalizeMeasurementEntry(measurement, index))
                : [deps.normalizeMeasurementEntry(data.measurement || payload, 0)];
            const saved = savedMeasurements[0];
            savedMeasurements.forEach((measurement) => {
                deps.getState().measurement.visibilityById[measurement.id] = true;
                deps.getState().measurement.reviewVisibilityById[measurement.id] = false;
            });
            deps.getState().measurement.currentMeasurement = null;
            deps.getState().measurement.pendingRepeatMeasurements = [];
            deps.getState().measurement.autoSubMeasurements = [];
            deps.getState().measurement.currentMeasurementSaved = false;
            deps.getState().measurement.currentMeasurementName = '';
            deps.getState().measurement.statusText = 'Measurement saved.';
            await deps.fetchMeasurements();
            deps.showToast('Measurement saved', 'success');
        } catch (error) {
            console.error('saveCurrentMeasurement failed', {
                message: error?.message || String(error),
                name: error?.name || '',
                error,
            });
            deps.getState().measurement.statusText = error.message || 'Failed to save measurement';
            deps.showToast(deps.getState().measurement.statusText, 'error');
        } finally {
            deps.getState().measurement.saveInFlight = false;
            deps.renderMeasurementPanel();
        }
    }

    async function deleteMeasurement(measurementId, measurementName = 'Measurement') {
        if (!measurementId || deps.getState().measurement.saveInFlight || deps.getState().measurement.startInFlight) return;
        if (!deps.confirm(`Delete saved measurement \"${measurementName}\"?`)) return;
        deps.getState().measurement.saveInFlight = true;
        deps.getState().measurement.statusText = 'Deleting saved measurement…';
        deps.renderMeasurementPanel();
        try {
            const resp = await deps.fetch(`/api/measurements/${encodeURIComponent(measurementId)}`, { method: 'DELETE' });
            const data = await resp.json().catch(() => ({}));
            if (!resp.ok) throw new Error(deps.formatTransitionErrorDetail(data.detail, 'Failed to delete measurement'));
            delete deps.getState().measurement.visibilityById[measurementId];
            delete deps.getState().measurement.reviewVisibilityById[measurementId];
            deps.getState().measurement.statusText = 'Saved runs updated.';
            await deps.fetchMeasurements();
        } catch (error) {
            console.error('deleteMeasurement failed', error);
            deps.getState().measurement.statusText = error.message || 'Failed to delete measurement';
            deps.showToast(deps.getState().measurement.statusText, 'error');
        } finally {
            deps.getState().measurement.saveInFlight = false;
            deps.renderMeasurementPanel();
        }
    }

    async function deleteSelectedMeasurements() {
        if (deps.getState().measurement.saveInFlight || deps.getState().measurement.startInFlight) return;
        const measurements = deps.getVisibleMeasurementEntries();
        if (!measurements.length) {
            deps.showToast('No saved measurements selected', 'warning');
            return;
        }
        const label = measurements.length === 1 ? `saved measurement \"${measurements[0].name}\"` : `${measurements.length} saved measurements`;
        if (!deps.confirm(`Delete ${label}?`)) return;
        deps.getState().measurement.saveInFlight = true;
        deps.getState().measurement.statusText = `Deleting ${measurements.length === 1 ? 'saved measurement' : 'saved measurements'}…`;
        deps.renderMeasurementPanel();
        let deletedCount = 0;
        try {
            for (const measurement of measurements) {
                const resp = await deps.fetch(`/api/measurements/${encodeURIComponent(measurement.id)}`, { method: 'DELETE' });
                const data = await resp.json().catch(() => ({}));
                if (!resp.ok) throw new Error(deps.formatTransitionErrorDetail(data.detail, `Failed to delete ${measurement.name}`));
                delete deps.getState().measurement.visibilityById[measurement.id];
                delete deps.getState().measurement.reviewVisibilityById[measurement.id];
                deletedCount += 1;
            }
            deps.getState().measurement.statusText = 'Saved runs updated.';
            await deps.fetchMeasurements();
        } catch (error) {
            console.error('deleteSelectedMeasurements failed', error);
            deps.getState().measurement.statusText = error.message || 'Failed to delete selected measurements';
            deps.showToast(deps.getState().measurement.statusText, 'error');
        } finally {
            deps.getState().measurement.saveInFlight = false;
            deps.renderMeasurementPanel();
        }
    }

    async function mergeSelectedMeasurements() {
        if (deps.getState().measurement.saveInFlight || deps.getState().measurement.startInFlight) return;
        const measurements = deps.getVisibleMeasurementEntries();
        if (measurements.length < 2) {
            deps.showToast('Select at least two saved measurements to merge', 'warning');
            return;
        }
        const defaultName = `Merged ${measurements.length} measurements`;
        const requestedName = deps.prompt('Name for merged measurement file:', defaultName);
        if (requestedName === null) return;
        const name = requestedName.trim() || defaultName;

        deps.getState().measurement.saveInFlight = true;
        deps.getState().measurement.statusText = `Merging ${measurements.length} saved measurements…`;
        deps.renderMeasurementPanel();
        try {
            const resp = await deps.fetch('/api/measurements/merge', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    name,
                    measurementIds: measurements.map(measurement => measurement.id),
                }),
            });
            const responseText = await resp.text();
            let data = {};
            try {
                data = responseText ? JSON.parse(responseText) : {};
            } catch (_error) {
                data = {};
            }
            if (!resp.ok) throw new Error(deps.formatTransitionErrorDetail(data.detail, responseText.trim() || 'Failed to merge selected measurements'));
            const merged = deps.normalizeMeasurementEntry(data.measurement || {}, 0);
            if (merged.id) {
                deps.getState().measurement.visibilityById[merged.id] = true;
                deps.getState().measurement.reviewVisibilityById[merged.id] = false;
            }
            deps.getState().measurement.statusText = 'Merged measurement saved.';
            await deps.fetchMeasurements();
            if (merged.id) deps.getState().measurement.visibilityById[merged.id] = true;
            deps.showToast(`Created merged measurement: ${merged.name || name}`, 'success');
        } catch (error) {
            console.error('mergeSelectedMeasurements failed', error);
            deps.getState().measurement.statusText = error.message || 'Failed to merge selected measurements';
            deps.showToast(deps.getState().measurement.statusText, 'error');
        } finally {
            deps.getState().measurement.saveInFlight = false;
            deps.renderMeasurementPanel();
        }
    }

    return {
        init,
        saveCurrentMeasurement, deleteMeasurement, deleteSelectedMeasurements, mergeSelectedMeasurements,
    };
});
