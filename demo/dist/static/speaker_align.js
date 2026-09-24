// SPDX-License-Identifier: AGPL-3.0-only
/**
 * FXRoute Speaker Align frontend helpers.
 *
 * Pure payload/visibility/status helpers plus no direct fetch: the flow
 * module owns backend calls through the injected api object. Browser-loadable
 * UMD, no build step; Node-testable via require().
 *
 * Time-domain Before/After: Before is the band-isolated way arrivals from
 * the shared planning take (proposal.arrival_ms), After the band-isolated
 * way arrivals from the shared verification take
 * (check.after_arrival_ms). The view derives both lanes on one shared ms
 * axis without any additional measurement. Runs built with
 * buildSpeakerAlignRun preserve the full dataset (ways, delay/gain
 * corrections, QC/isolation, metadata); per-way frequency points travel
 * along when the job captured them.
 */
(function (root, factory) {
    const api = factory(root);
    root.FXRouteSpeakerAlign = api;
    if (typeof module === 'object' && module.exports) module.exports = api;
})(typeof globalThis !== 'undefined' ? globalThis : window, function () {
    'use strict';

    const SIDES = ['left', 'right'];
    const TERMINAL_STATUSES = ['committed', 'trial-done', 'unconfirmed', 'failed', 'cancelled'];
    const RUN_SCHEMA = 'speaker-align-run-v1';
    const RUN_KIND = 'speaker-align-run-v1';
    const WAY_COLORS = ['#4caf8a', '#7aa2f7', '#e0af68', '#bb9af7'];

    function trimmed(value) {
        return String(value == null ? '' : value).trim();
    }

    function finiteNumber(value) {
        const num = Number(value);
        return Number.isFinite(num) ? num : null;
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

    function readRoleMap(value, label) {
        if (!value || typeof value !== 'object' || Array.isArray(value)) {
            throw new Error(`Speaker Align run ${label} must be an object`);
        }
        const entries = Object.entries(value);
        if (!entries.length) throw new Error(`Speaker Align run ${label} must not be empty`);
        const out = {};
        for (const [role, arrival] of entries) {
            if (!role || typeof role !== 'string') throw new Error(`Speaker Align run ${label} carries no way name`);
            const num = finiteNumber(arrival);
            if (num === null) throw new Error(`Speaker Align run ${label} arrival for ${role} must be finite`);
            out[role] = num;
        }
        return out;
    }

    function resultWays(result) {
        const before = readRoleMap(result?.proposal?.arrival_ms, 'before');
        const after = readRoleMap(result?.check?.after_arrival_ms, 'after');
        const beforeWays = Object.keys(before).sort();
        const afterWays = Object.keys(after).sort();
        if (beforeWays.join('|') !== afterWays.join('|')) {
            throw new Error('Speaker Align run Before and After name different ways');
        }
        return beforeWays;
    }

    function timeDomainView(runOrResult) {
        const source = runOrResult || {};
        let before;
        let after;
        let ways;
        let referenceRole = null;
        let qc = null;
        let corrections = null;
        let side = null;
        if (source.before && source.after && Array.isArray(source.ways)) {
            if (source.before.source !== 'planning-take') {
                throw new Error('Speaker Align run Before must come from the planning take');
            }
            if (source.after.source !== 'verification-take') {
                throw new Error('Speaker Align run After must come from the verification take');
            }
            ways = [...source.ways].sort();
            before = readRoleMap(source.before.arrival_ms, 'before');
            after = readRoleMap(source.after.arrival_ms, 'after');
            referenceRole = source.reference_role || null;
            qc = source.qc || null;
            corrections = source.corrections || null;
            side = source.side || null;
        } else if (source.time_domain && source.time_domain.before && source.time_domain.after) {
            const lanes = source.time_domain;
            if (lanes.before.source !== 'planning-take' || lanes.after.source !== 'verification-take') {
                throw new Error('Speaker Align time domain must bind Before to planning and After to verification');
            }
            ways = [...(lanes.ways || Object.keys(lanes.before.arrival_ms))].sort();
            before = readRoleMap(lanes.before.arrival_ms, 'before');
            after = readRoleMap(lanes.after.arrival_ms, 'after');
            side = source.side || null;
            qc = source.check ? {
                confirmed: source.check.confirmed,
                max_residual_ms: finiteNumber(source.check.max_residual_ms),
                before_spread_ms: finiteNumber(source.check.before_spread_ms),
                tolerance_ms: finiteNumber(source.check.tolerance_ms),
            } : null;
            corrections = source.proposal ? {
                added_delay_ms: { ...source.proposal.added_delay_ms },
                added_gain_db: { ...(source.proposal.added_gain_db || {}) },
            } : null;
            referenceRole = source.proposal?.reference_role || null;
        } else {
            ways = resultWays(source);
            before = readRoleMap(source.proposal.arrival_ms, 'before');
            after = readRoleMap(source.check.after_arrival_ms, 'after');
            side = source.side || null;
            referenceRole = source.proposal?.reference_role || null;
            qc = source.check || null;
            corrections = source.proposal ? {
                added_delay_ms: { ...source.proposal.added_delay_ms },
                added_gain_db: { ...(source.proposal.added_gain_db || {}) },
            } : null;
        }
        if (Object.keys(before).sort().join('|') !== ways.join('|')
            || Object.keys(after).sort().join('|') !== ways.join('|')) {
            throw new Error('Speaker Align run Before/After must name exactly the run ways');
        }
        const beforeSpread = Math.max(...ways.map(role => before[role])) - Math.min(...ways.map(role => before[role]));
        const afterSpread = Math.max(...ways.map(role => after[role])) - Math.min(...ways.map(role => after[role]));
        const lowest = Math.min(...ways.map(role => Math.min(before[role], after[role])));
        const highest = Math.max(...ways.map(role => Math.max(before[role], after[role])));
        const span = highest - lowest;
        const margin = Math.max(0.25, span * 0.15);
        return {
            schema: RUN_SCHEMA,
            side,
            ways: [...ways],
            window_ms: [lowest - margin, highest + margin],
            lanes: {
                before: { source: 'planning-take', arrival_ms: { ...before }, spread_ms: beforeSpread },
                after: { source: 'verification-take', arrival_ms: { ...after }, spread_ms: afterSpread },
            },
            reference_role: referenceRole,
            qc,
            corrections,
        };
    }

    function lanePositions(arrivalMs, ways, windowMs, width) {
        const [low, high] = windowMs;
        const span = (high - low) || 1;
        const positions = {};
        for (const role of ways) {
            const ratio = (arrivalMs[role] - low) / span;
            positions[role] = Math.min(1, Math.max(0, ratio)) * width;
        }
        return positions;
    }

    function renderSpeakerAlignTimeDomain(viewOrResult, side) {
        let view;
        try {
            view = timeDomainView(viewOrResult);
        } catch (error) {
            return '';
        }
        const ways = view.ways;
        const label = side === 'right' ? 'Right' : side === 'left' ? 'Left' : (view.side === 'right' ? 'Right' : view.side === 'left' ? 'Left' : 'Speaker');
        const width = 560;
        const laneHeight = 34;
        const top = 22;
        const [low, high] = view.window_ms;
        const formatMs = value => Number.isFinite(Number(value)) ? Number(value).toFixed(3) : '—';
        const lanes = [
            { key: 'before', title: `Before · planning take · spread ${formatMs(view.lanes.before.spread_ms)} ms` },
            { key: 'after', title: `After · verification take · spread ${formatMs(view.lanes.after.spread_ms)} ms` },
        ];
        const laneSvg = lanes.map((lane, laneIndex) => {
            const arrival = view.lanes[lane.key].arrival_ms;
            const positions = lanePositions(arrival, ways, view.window_ms, width);
            const y = top + laneIndex * laneHeight;
            const markers = ways.map((role, index) => {
                const x = positions[role].toFixed(1);
                const color = WAY_COLORS[index % WAY_COLORS.length];
                const ref = role === view.reference_role ? ' · ref' : '';
                return `<g class="speaker-align-time-marker" data-way="${escapeHtml(role)}" data-lane="${lane.key}">`
                    + `<line x1="${x}" y1="${y - 9}" x2="${x}" y2="${y + 9}" stroke="${color}" stroke-width="2.5"/>`
                    + `<circle cx="${x}" cy="${y}" r="3.2" fill="${color}"/>`
                    + `<title>${escapeHtml(wayLabel(role))}${escapeHtml(ref)}: ${formatMs(arrival[role])} ms (${lane.key === 'before' ? 'planning' : 'verification'} take)</title>`
                    + `</g>`;
            }).join('');
            const wayText = ways.map((role, index) => {
                const x = positions[role];
                const color = WAY_COLORS[index % WAY_COLORS.length];
                const short = escapeHtml(wayLabel(role).slice(0, 1));
                const above = laneIndex === 0;
                const ty = above ? y - 13 : y + 19;
                return `<text x="${x.toFixed(1)}" y="${ty}" text-anchor="middle" font-size="9" fill="${color}">${short}</text>`;
            }).join('');
            return `<text x="0" y="${y - 12}" font-size="10" fill="currentColor" opacity="0.75">${escapeHtml(lane.title)}</text>`
                + `<line x1="0" y1="${y}" x2="${width}" y2="${y}" stroke="currentColor" stroke-width="1" opacity="0.35"/>`
                + markers + wayText;
        }).join('');
        const ticks = [low, (low + high) / 2, high].map(value => {
            const ratio = (value - low) / ((high - low) || 1);
            const x = (ratio * width).toFixed(1);
            const y = top + lanes.length * laneHeight - 4;
            return `<g><line x1="${x}" y1="${top - 4}" x2="${x}" y2="${y}" stroke="currentColor" stroke-width="0.5" opacity="0.25"/>`
                + `<text x="${x}" y="${y + 12}" text-anchor="middle" font-size="9" opacity="0.7">${formatMs(value)} ms</text></g>`;
        }).join('');
        const height = top + lanes.length * laneHeight + 22;
        return `<figure class="speaker-align-time" data-side="${escapeHtml(label.toLowerCase())}">`
            + `<figcaption>${escapeHtml(label)} speaker · time domain Before/After · shared ms axis</figcaption>`
            + `<svg class="speaker-align-time-svg" viewBox="0 0 ${width} ${height}" width="100%" height="${height}" role="img" aria-label="${escapeHtml(label)} speaker time-domain Before from the planning take and After from the verification take on a shared millisecond axis">`
            + `<desc>Before (planning take) spread ${formatMs(view.lanes.before.spread_ms)} ms, After (verification take) spread ${formatMs(view.lanes.after.spread_ms)} ms.</desc>`
            + ticks + laneSvg + `</svg>`
            + `<div class="speaker-align-time-legend">${ways.map((role, index) => {
                const color = WAY_COLORS[index % WAY_COLORS.length];
                const beforeMs = formatMs(view.lanes.before.arrival_ms[role]);
                const afterMs = formatMs(view.lanes.after.arrival_ms[role]);
                const ref = role === view.reference_role ? ' · ref' : '';
                return `<span class="speaker-align-time-legend-item"><span class="speaker-align-time-swatch" style="background:${color}"></span>${escapeHtml(wayLabel(role))}${escapeHtml(ref)} ${beforeMs} → ${afterMs} ms</span>`;
            }).join('')}</div>`
            + `</figure>`;
    }

    function cleanFrequencyPanel(panel, role) {
        if (!panel || typeof panel !== 'object') return null;
        const out = {};
        for (const key of ['trusted_points', 'review_points']) {
            const points = panel[key];
            if (!Array.isArray(points) || !points.length) continue;
            const cleaned = [];
            for (const point of points) {
                if (!Array.isArray(point) || point.length !== 2) return null;
                const frequency = finiteNumber(point[0]);
                const level = finiteNumber(point[1]);
                if (frequency === null || level === null || frequency <= 0) return null;
                cleaned.push([frequency, level]);
            }
            cleaned.sort((a, b) => a[0] - b[0]);
            out[key] = cleaned;
        }
        return Object.keys(out).length ? out : null;
    }

    function buildSpeakerAlignRun(result, meta) {
        const options = meta || {};
        const side = trimmed(options.side || result?.side);
        if (side !== 'left' && side !== 'right') {
            throw new Error('Speaker Align run side must be left or right');
        }
        const proposal = result?.proposal;
        const check = result?.check;
        if (!proposal || typeof proposal !== 'object' || !check || typeof check !== 'object') {
            throw new Error('Speaker Align run needs proposal and check documents');
        }
        const before = readRoleMap(proposal.arrival_ms, 'before');
        const after = readRoleMap(check.after_arrival_ms, 'after');
        const ways = Object.keys(before).sort();
        if (ways.join('|') !== Object.keys(after).sort().join('|')) {
            throw new Error('Speaker Align run Before and After name different ways');
        }
        const addedDelay = readRoleMap(proposal.added_delay_ms, 'delay correction');
        const addedGain = proposal.added_gain_db ? readRoleMap(proposal.added_gain_db, 'gain correction') : Object.fromEntries(ways.map(role => [role, 0]));
        const wayLevels = proposal.way_levels_db ? readRoleMap(proposal.way_levels_db, 'way level') : Object.fromEntries(ways.map(role => [role, 0]));
        if (Object.keys(addedDelay).sort().join('|') !== ways.join('|')
            || Object.keys(addedGain).sort().join('|') !== ways.join('|')
            || Object.keys(wayLevels).sort().join('|') !== ways.join('|')) {
            throw new Error('Speaker Align run corrections must name exactly the run ways');
        }
        const referenceRole = proposal.reference_role;
        if (typeof referenceRole !== 'string' || !before[referenceRole] && before[referenceRole] !== 0) {
            throw new Error('Speaker Align run reference role is not one of the ways');
        }
        const startRevision = proposal.start_revision;
        if (!Number.isInteger(startRevision)) throw new Error('Speaker Align run carries no start revision');
        const fingerprint = proposal.processing_fingerprint;
        if (!fingerprint || typeof fingerprint !== 'string') throw new Error('Speaker Align run carries no processing fingerprint');
        const maxResidual = finiteNumber(check.max_residual_ms);
        const beforeSpread = finiteNumber(check.before_spread_ms);
        const tolerance = finiteNumber(check.tolerance_ms);
        if (maxResidual === null || beforeSpread === null || tolerance === null) {
            throw new Error('Speaker Align run QC timing must be finite');
        }
        const planningIsolation = proposal.planning_isolation_db && typeof proposal.planning_isolation_db === 'object'
            ? { ...proposal.planning_isolation_db } : {};
        const afterIsolation = check.way_isolation_db && typeof check.way_isolation_db === 'object'
            ? { ...check.way_isolation_db } : {};
        const afterLevels = check.after_way_levels_db && typeof check.after_way_levels_db === 'object'
            ? readRoleMap(check.after_way_levels_db, 'after level') : {};
        if (Object.keys(afterLevels).length && Object.keys(afterLevels).sort().join('|') !== ways.join('|')) {
            throw new Error('Speaker Align run after levels name different ways');
        }
        const frequencySource = options.frequency || result?.way_frequency || result?.frequency || null;
        let frequency = null;
        if (frequencySource && typeof frequencySource === 'object') {
            frequency = {};
            for (const role of ways) {
                const panel = cleanFrequencyPanel(frequencySource[role], role);
                if (panel) frequency[role] = panel;
            }
            if (!Object.keys(frequency).length) frequency = null;
        }
        const now = new Date().toISOString().replace(/\.\d+Z$/, 'Z');
        return {
            schema: RUN_SCHEMA,
            id: trimmed(options.runId || options.id || result?.run_id || '') || `speaker-align-${side}-${Date.now()}`,
            created_at: trimmed(options.createdAt || result?.created_at || '') || now,
            side,
            job_id: trimmed(options.jobId || result?.job_id || ''),
            dry_run: options.dryRun === true || result?.dry_run === true,
            confirmed: options.confirmed !== undefined ? options.confirmed === true : check.confirmed === true,
            committed_revision: options.committedRevision !== undefined ? options.committedRevision : (result?.committed_revision ?? null),
            sample_rate_hz: Number.isInteger(options.sampleRateHz) ? options.sampleRateHz
                : (Number.isInteger(result?.sample_rate_hz) ? result.sample_rate_hz : null),
            ways: [...ways],
            reference_role: referenceRole,
            arrival_source: typeof proposal.arrival_source === 'string' ? proposal.arrival_source : 'shared-planning-take',
            before: {
                source: 'planning-take',
                arrival_ms: Object.fromEntries(ways.map(role => [role, before[role]])),
                way_isolation_db: { ...planningIsolation },
                way_levels_db: Object.fromEntries(ways.map(role => [role, wayLevels[role]])),
            },
            after: {
                source: 'verification-take',
                arrival_ms: Object.fromEntries(ways.map(role => [role, after[role]])),
                way_isolation_db: { ...afterIsolation },
                way_levels_db: { ...afterLevels },
            },
            corrections: {
                added_delay_ms: Object.fromEntries(ways.map(role => [role, addedDelay[role]])),
                added_gain_db: Object.fromEntries(ways.map(role => [role, addedGain[role]])),
            },
            qc: {
                confirmed: check.confirmed === true,
                reasons: Array.isArray(check.reasons) ? check.reasons.map(String) : [],
                warnings: Array.isArray(check.warnings) ? check.warnings.map(String) : [],
                max_residual_ms: maxResidual,
                before_spread_ms: beforeSpread,
                tolerance_ms: tolerance,
                pairs: Array.isArray(check.pairs) ? check.pairs : [],
                gain_spread_db: finiteNumber(check.gain_spread_db),
                before_gain_spread_db: finiteNumber(check.before_gain_spread_db),
                gain_tolerance_db: finiteNumber(check.gain_tolerance_db),
                isolation_margin_db: finiteNumber(check.isolation_margin_db),
            },
            metadata: {
                start_revision: startRevision,
                processing_fingerprint: fingerprint,
                provenance: result?.provenance && typeof result.provenance === 'object' ? { ...result.provenance } : {},
                params: options.params && typeof options.params === 'object' && Object.keys(options.params).length ? { ...options.params }
                    : (result?.params && typeof result.params === 'object' ? { ...result.params } : {}),
            },
            ...(frequency ? { frequency } : {}),
        };
    }

    function runToMeasurement(run, name) {
        if (!run || typeof run !== 'object' || run.schema !== RUN_SCHEMA) {
            throw new Error('Speaker Align run schema must be speaker-align-run-v1');
        }
        const view = timeDomainView(run);
        const label = trimmed(name) || `Speaker Align ${run.side} · ${run.confirmed ? 'verified' : 'not verified'}`;
        const colors = WAY_COLORS;
        const traces = [];
        const frequency = run.frequency || {};
        run.ways.forEach((role, index) => {
            const panel = frequency[role];
            const points = panel?.trusted_points || panel?.review_points;
            if (Array.isArray(points) && points.length) {
                traces.push({
                    kind: 'speaker-align-way-response',
                    label: `${label} · ${role}`,
                    color: colors[index % colors.length],
                    role: 'trusted',
                    points: points.map(point => [Number(point[0]), Number(point[1])]),
                });
            }
        });
        void view;
        return {
            id: run.id,
            name: label,
            created_at: run.created_at,
            channel: run.side,
            measurement_kind: RUN_KIND,
            speaker_align: JSON.parse(JSON.stringify(run)),
            analysis: { method: 'speaker-align-run-v1', speaker_align: JSON.parse(JSON.stringify(run)) },
            traces,
        };
    }

    function measurementToRun(measurement) {
        if (!measurement || typeof measurement !== 'object') {
            throw new Error('Speaker Align measurement must be an object');
        }
        const run = measurement.speaker_align
            || (measurement.analysis && measurement.analysis.speaker_align);
        if (!run || typeof run !== 'object') {
            throw new Error('Saved measurement carries no Speaker Align run');
        }
        if (run.schema !== RUN_SCHEMA) {
            throw new Error('Speaker Align run schema must be speaker-align-run-v1');
        }
        const view = timeDomainView(run);
        void view;
        return JSON.parse(JSON.stringify(run));
    }

    function renderSpeakerAlignResult(result, side) {
        if (!result?.proposal || !result?.check) return '';
        const proposal = result.proposal;
        const number = value => Number.isFinite(value) ? value.toFixed(3) : '—';
        const rows = Object.keys(proposal.arrival_ms).map(role => `<tr><th scope="row">${wayLabel(role)}${role === proposal.reference_role ? ' · ref' : ''}</th><td>${number(proposal.arrival_ms[role])}</td><td>+${number(proposal.added_delay_ms[role])}</td><td>${number(result.check.after_arrival_ms?.[role])}</td></tr>`).join('');
        const table = `<div class="speaker-align-table-wrap"><table class="speaker-align-table"><caption>${side === 'right' ? 'Right' : 'Left'} speaker · ${result.confirmed ? 'Verified' : 'Not verified'} · ms</caption><thead><tr><th scope="col">Way</th><th scope="col">Before</th><th scope="col">Delay added</th><th scope="col">After</th></tr></thead><tbody>${rows}</tbody></table></div>`;
        return table + renderSpeakerAlignTimeDomain(result, side);
    }

    return {
        SIDES,
        TERMINAL_STATUSES,
        RUN_SCHEMA,
        RUN_KIND,
        buildSpeakerAlignPayload,
        defaultReferenceId,
        defaultMicrophonePositionId,
        speakerAlignVisible,
        formatSpeakerAlignStatus,
        wayLabel,
        renderSpeakerAlignResult,
        timeDomainView,
        renderSpeakerAlignTimeDomain,
        buildSpeakerAlignRun,
        runToMeasurement,
        measurementToRun,
    };
});
