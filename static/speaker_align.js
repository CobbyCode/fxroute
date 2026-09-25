// SPDX-License-Identifier: AGPL-3.0-only
/**
 * FXRoute Speaker Align frontend helpers.
 *
 * Pure payload/visibility/status helpers plus no direct fetch: the flow
 * module owns backend calls through the injected api object. Browser-loadable
 * UMD, no build step; Node-testable via require().
 *
 * The Speaker Align section shows the numeric result only (delay, gain,
 * isolation, verification, status). Before (planning take) and After
 * (verification take) are normal measurements in the job result; the flow
 * module hands them to the normal measurement graph and save flow.
 */
(function (root, factory) {
    const api = factory(root);
    root.FXRouteSpeakerAlign = api;
    if (typeof module === 'object' && module.exports) module.exports = api;
})(typeof globalThis !== 'undefined' ? globalThis : window, function () {
    'use strict';

    const SIDES = ['left', 'right'];
    const TERMINAL_STATUSES = ['committed', 'trial-done', 'unconfirmed', 'failed', 'cancelled'];
    const TAKES = ['before', 'after'];

    function trimmed(value) {
        return String(value == null ? '' : value).trim();
    }

    // Missing values stay missing: Number(null) would read as 0.
    function numberOrNaN(value) {
        return value === null || value === undefined || value === '' ? NaN : Number(value);
    }

    function escapeHtml(value) {
        return String(value == null ? '' : value)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;');
    }

    function buildSpeakerAlignPayload({
        side, inputId, micChannel, referenceChannel,
        referenceChannelLeft, referenceChannelRight,
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
            reference_input_channel_left: trimmed(referenceChannelLeft),
            reference_input_channel_right: trimmed(referenceChannelRight),
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
        // The numbers live in the compact result block; the status line
        // keeps the outcome and any advisories.
        if (status === 'committed') {
            const revision = result.committed_revision;
            const revisionText = Number.isInteger(revision) ? ` at revision ${revision}` : '';
            const warnings = Array.isArray(result.check?.warnings) ? result.check.warnings.filter(text => String(text ?? '').trim()) : [];
            const warningText = warnings.length ? ` Advisories: ${warnings.join('; ')}.` : '';
            return `Speaker Align ${side} timing verified and committed${revisionText}.${warningText}`;
        }
        if (status === 'trial-done') {
            const confirmed = result.confirmed === true;
            const warnings = Array.isArray(result.check?.warnings) ? result.check.warnings.filter(text => String(text ?? '').trim()) : [];
            const warningText = warnings.length ? ` Advisories: ${warnings.join('; ')}.` : '';
            return confirmed
                ? `Speaker Align ${side} trial confirmed without committing.${warningText}`
                : `Speaker Align ${side} trial did not confirm; nothing changed.${warningText}`;
        }
        if (status === 'unconfirmed') {
            const reasons = result.check?.reasons || [];
            const warnings = Array.isArray(result.check?.warnings) ? result.check.warnings.filter(text => String(text ?? '').trim()) : [];
            const warningText = warnings.length ? ` Advisories: ${warnings.join('; ')}.` : '';
            return `Speaker Align ${side} not verified: ${reasons.join('; ')}. Previous delays restored.${warningText}`;
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

    function takeMeasurements(result) {
        const measurements = result?.measurements;
        if (!measurements || typeof measurements !== 'object') return [];
        return TAKES.map(take => measurements[take])
            .filter(measurement => measurement && typeof measurement === 'object'
                && Array.isArray(measurement.traces) && measurement.traces.length);
    }

    function resultStatusLabel(result) {
        if (Number.isInteger(result.committed_revision)) return `Verified · committed rev ${result.committed_revision}`;
        if (result.confirmed === true) return result.dry_run === true ? 'Trial confirmed · not committed' : 'Verified';
        return 'Not verified · previous delays kept';
    }

    function renderSpeakerAlignResult(result, side) {
        if (!result?.proposal || !result?.check) return '';
        const proposal = result.proposal;
        const check = result.check;
        const signed = (value, digits, unit) => Number.isFinite(value) ? `${value >= 0 ? '+' : ''}${value.toFixed(digits)} ${unit}` : '—';
        const plain = (value, digits) => Number.isFinite(value) ? value.toFixed(digits) : '—';
        const isolation = role => {
            const planning = numberOrNaN(proposal.planning_isolation_db?.[role]);
            const verification = numberOrNaN(check.way_isolation_db?.[role]);
            if (!Number.isFinite(planning) && !Number.isFinite(verification)) return '—';
            return `${plain(planning, 1)} → ${plain(verification, 1)} dB`;
        };
        const rows = Object.keys(proposal.added_delay_ms || {}).map(role => {
            const ref = role === proposal.reference_role ? ' · ref' : '';
            return `<tr><th scope="row">${escapeHtml(wayLabel(role))}${ref}</th>`
                + `<td>${signed(numberOrNaN(proposal.added_delay_ms[role]), 3, 'ms')}</td>`
                + `<td>${signed(numberOrNaN(proposal.added_gain_db?.[role]), 2, 'dB')}</td>`
                + `<td>${isolation(role)}</td></tr>`;
        }).join('');
        const label = side === 'right' ? 'Right' : 'Left';
        const table = `<div class="speaker-align-table-wrap"><table class="speaker-align-table">`
            + `<caption>${label} speaker · ${escapeHtml(resultStatusLabel(result))}</caption>`
            + `<thead><tr><th scope="col">Way</th><th scope="col">Delay</th><th scope="col">Gain</th>`
            + `<th scope="col" title="Planning take → verification take">Isolation</th></tr></thead>`
            + `<tbody>${rows}</tbody></table></div>`;
        const timing = `spread ${plain(numberOrNaN(check.before_spread_ms), 3)} → ${plain(numberOrNaN(check.max_residual_ms), 3)} ms`
            + ` (limit ${plain(numberOrNaN(check.tolerance_ms), 3)} ms)`;
        const beforeGainSpread = numberOrNaN(check.before_gain_spread_db);
        const gainSpread = numberOrNaN(check.gain_spread_db);
        const levelFrom = Number.isFinite(beforeGainSpread) ? `${plain(beforeGainSpread, 2)} → ` : '';
        const level = Number.isFinite(gainSpread)
            ? ` · level spread ${levelFrom}${plain(gainSpread, 2)} dB (advisory ${plain(numberOrNaN(check.gain_tolerance_db), 2)} dB)`
            : '';
        return table + `<p class="speaker-align-verification">Verification: ${timing}${level}</p>`;
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
        takeMeasurements,
        renderSpeakerAlignResult,
    };
});
