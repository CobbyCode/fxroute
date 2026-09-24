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
        if (!trimmed(referenceChannel)) return 'fxroute_dsp_sink.monitor';
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
        if (!modeConfig?.crossover_enabled || modeConfig.selected_bank !== 'global') return false;
        const wayCount = modeConfig && modeConfig.topology
            ? Number(modeConfig.topology.way_count || 0) : 0;
        const topology = modeConfig.topology;
        if (![2, 3, 4].includes(wayCount) || !Array.isArray(topology.issues) || topology.issues.length) return false;
        return ['left_ways', 'right_ways'].every(key => {
            const roles = topology[key];
            return Array.isArray(roles) && roles.length === wayCount && roles.every((role, index) => {
                const settings = modeConfig.processing?.[role];
                return settings && (index === 0 || settings.highpass?.frequency_hz > 0)
                    && (index === roles.length - 1 || settings.lowpass?.frequency_hz > 0);
            });
        });
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
            const check = result.check;
            const timing = check ? ` Spread ${Number(check.before_spread_ms).toFixed(3)} → ${Number(check.max_residual_ms).toFixed(3)} ms (limit ${Number(check.tolerance_ms).toFixed(3)} ms).` : '';
            return `Speaker Align ${side} verified and committed${revisionText}.${timing}`;
        }
        if (status === 'trial-done') {
            const confirmed = result.confirmed === true;
            return confirmed
                ? `Speaker Align ${side} trial confirmed without committing.`
                : `Speaker Align ${side} trial did not confirm; nothing changed.`;
        }
        if (status === 'unconfirmed') {
            return `Speaker Align ${side} not verified: ${(result.check?.reasons || []).join('; ')}. Previous delays restored.`;
        }
        if (status === 'failed') {
            return trimmed(record.error) || message || `Speaker Align ${side} failed.`;
        }
        if (status === 'cancelled' || status === 'cancelling') {
            return message || `Speaker Align ${side} cancelled.`;
        }
        return message || `Speaker Align ${side}: ${status}.`;
    }

    function wayLabel(role) {
        return { low: 'Low', low_mid: 'Low-Mid', mid: 'Mid', high: 'High' }[String(role).replace(/^(left|right)_/, '')] || 'Way';
    }

    function renderSpeakerAlignResult(result, side) {
        if (!result?.proposal || !result?.check) return '';
        const proposal = result.proposal;
        const number = value => Number.isFinite(value) ? value.toFixed(3) : '—';
        const rows = Object.keys(proposal.arrival_ms).map(role => `<tr><th scope="row">${wayLabel(role)}${role === proposal.reference_role ? ' · ref' : ''}</th><td>${number(proposal.arrival_ms[role])}</td><td>+${number(proposal.added_delay_ms[role])}</td><td>${number(result.check.after_arrival_ms?.[role])}</td></tr>`).join('');
        return `<div class="speaker-align-table-wrap"><table class="speaker-align-table"><caption>${side === 'right' ? 'Right' : 'Left'} speaker · ${result.confirmed ? 'Verified' : 'Not verified'} · ms</caption><thead><tr><th scope="col">Way</th><th scope="col">Before</th><th scope="col">Delay added</th><th scope="col">After</th></tr></thead><tbody>${rows}</tbody></table></div>`;
    }

    return {
        SIDES,
        TERMINAL_STATUSES,
        buildSpeakerAlignPayload,
        defaultReferenceId,
        defaultMicrophonePositionId,
        speakerAlignVisible,
        formatSpeakerAlignStatus,
        wayLabel,
        renderSpeakerAlignResult,
    };
});
