// SPDX-License-Identifier: AGPL-3.0-only
/**
 * FXRoute Speaker Align frontend helpers.
 *
 * Pure payload/visibility/status helpers plus no direct fetch: the flow
 * module owns backend calls through the injected api object. Browser-loadable
 * UMD, no build step; Node-testable via require().
 */
(function (root, factory) {
    const api = factory(root);
    root.FXRouteSpeakerAlign = api;
    if (typeof module === 'object' && module.exports) module.exports = api;
})(typeof globalThis !== 'undefined' ? globalThis : window, function () {
    'use strict';

    const SIDES = ['left', 'right'];
    const TERMINAL_STATUSES = ['committed', 'trial-done', 'unconfirmed', 'failed', 'cancelled'];

    function trimmed(value) {
        return String(value == null ? '' : value).trim();
    }

    function buildSpeakerAlignPayload({
        side, inputId, micChannel, referenceChannel,
        referenceId, microphonePositionId, dryRun,
    }) {
        const normalizedSide = trimmed(side);
        if (normalizedSide !== 'left' && normalizedSide !== 'right') {
            throw new Error('Speaker Align side must be left or right');
        }
        const normalizedInput = trimmed(inputId);
        if (!normalizedInput) {
            throw new Error('Speaker Align requires a capture input id');
        }
        const normalizedRefChannel = trimmed(referenceChannel);
        if (!normalizedRefChannel) {
            throw new Error('Speaker Align requires an electrical reference input channel');
        }
        const normalizedReference = trimmed(referenceId);
        if (!normalizedReference) {
            throw new Error('Speaker Align requires an upstream reference identity');
        }
        const normalizedPosition = trimmed(microphonePositionId);
        if (!normalizedPosition) {
            throw new Error('Speaker Align requires a microphone position identity');
        }
        return {
            side: normalizedSide,
            input_id: normalizedInput,
            mic_input_channel: trimmed(micChannel) || '1',
            reference_input_channel: normalizedRefChannel,
            reference_id: normalizedReference,
            microphone_position_id: normalizedPosition,
            dry_run: dryRun === true,
        };
    }

    function defaultReferenceId(inputId, referenceChannel) {
        const input = trimmed(inputId) || 'mic';
        const channel = trimmed(referenceChannel) || '2';
        return `${input}:ch${channel}:upstream`;
    }

    function defaultMicrophonePositionId() {
        return 'seat-1-fixed';
    }

    function speakerAlignVisible(catalog) {
        if (!catalog || typeof catalog !== 'object') return false;
        const modeConfig = catalog.modes && catalog.modes[catalog.active_mode];
        if (!modeConfig?.crossover_enabled) return false;
        const wayCount = modeConfig && modeConfig.topology
            ? Number(modeConfig.topology.way_count || 0) : 0;
        return Number.isFinite(wayCount) && wayCount >= 2;
    }

    function formatSpeakerAlignStatus(job) {
        const record = job || {};
        const side = trimmed(record.side) || 'speaker';
        const status = trimmed(record.status) || 'unknown';
        const message = trimmed(record.message);
        const result = record.result || {};
        if (status === 'committed') {
            const revision = result.committed_revision;
            const revisionText = Number.isInteger(revision) ? ` at revision ${revision}` : '';
            return `Speaker Align ${side} committed${revisionText}.`;
        }
        if (status === 'trial-done') {
            const confirmed = result.confirmed === true;
            return confirmed
                ? `Speaker Align ${side} trial confirmed without committing.`
                : `Speaker Align ${side} trial did not confirm; nothing changed.`;
        }
        if (status === 'unconfirmed') {
            return `Speaker Align ${side} did not confirm acoustically; the start rendering was retained.`;
        }
        if (status === 'failed') {
            return trimmed(record.error) || message || `Speaker Align ${side} failed.`;
        }
        if (status === 'cancelled' || status === 'cancelling') {
            return message || `Speaker Align ${side} cancelled.`;
        }
        return message || `Speaker Align ${side}: ${status}.`;
    }

    return {
        SIDES,
        TERMINAL_STATUSES,
        buildSpeakerAlignPayload,
        defaultReferenceId,
        defaultMicrophonePositionId,
        speakerAlignVisible,
        formatSpeakerAlignStatus,
    };
});
