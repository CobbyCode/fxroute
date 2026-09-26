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
 * module hands them to the normal measurement graph and save flow. Their
 * timing timeline puts both takes on one time base anchored on the
 * reference way; the IR graph always draws a take that carries it as one
 * lane on that time base, whatever else is visible.
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
    const TIMELINE_SCHEMA = 'fxroute.speaker-align-timeline.v1';
    const WAY_COLORS = { low: '#60a5fa', low_mid: '#a78bfa', mid: '#f472b6', high: '#f59e0b' };

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

    // Backend reasons and advisories read "<numbers>: <plain reason>"; the
    // numbers are in the result table, the status line keeps the plain part.
    function plainReasons(list) {
        return (Array.isArray(list) ? list : [])
            .map(text => trimmed(text))
            .filter(Boolean)
            .map(text => (text.includes(': ') ? text.slice(text.indexOf(': ') + 2) : text));
    }

    // One outcome sentence for the panel status line.
    function formatSpeakerAlignStatus(job) {
        const record = job || {};
        const side = trimmed(record.side);
        const name = side === 'left' || side === 'right'
            ? `Speaker Align ${side === 'right' ? 'Right' : 'Left'}` : 'Speaker Align';
        const status = trimmed(record.status) || 'unknown';
        const result = record.result || {};
        const warnings = plainReasons(result.check?.warnings);
        const note = warnings.length ? ` Note: ${warnings.join('; ')}.` : '';
        if (status === 'committed') return `${name} verified and applied.${note}`;
        if (status === 'trial-done') {
            return result.confirmed === true
                ? `${name} trial verified; nothing applied.${note}`
                : `${name} trial not verified; nothing changed.${note}`;
        }
        if (status === 'unconfirmed') {
            const reasons = plainReasons(result.check?.reasons);
            return `${name} not verified${reasons.length ? `: ${reasons.join('; ')}` : ''}. Previous delays kept.${note}`;
        }
        if (status === 'failed') {
            const reason = trimmed(record.error) || trimmed(record.message).replace(/^Speaker alignment failed:\s*/i, '');
            return reason ? `${name} failed: ${reason}` : `${name} failed.`;
        }
        if (status === 'cancelled' || status === 'cancelling') return `${name} cancelled.`;
        return trimmed(record.message) || `${name}: ${status}.`;
    }

    function wayKey(role) {
        return String(role).replace(/^(left|right)_/, '');
    }

    function wayLabel(role) {
        return { low: 'Low', low_mid: 'Low-Mid', mid: 'Mid', high: 'High' }[wayKey(role)] || 'Way';
    }

    function wayColor(role) {
        return WAY_COLORS[wayKey(role)] || '#e5e7eb';
    }

    function finitePoints(points) {
        return (Array.isArray(points) ? points : [])
            .filter(point => Array.isArray(point) && point.length === 2
                && Number.isFinite(numberOrNaN(point[0])) && Number.isFinite(numberOrNaN(point[1])))
            .map(point => [Number(point[0]), Number(point[1])]);
    }

    // One take's ways on the alignment's common time base: zero is the
    // reference way's arrival, the way the alignment leaves undelayed.
    function takeTimeline(measurement) {
        const timeline = measurement?.analysis?.speaker_align_timeline;
        if (!timeline || timeline.schema !== TIMELINE_SCHEMA) return null;
        const arrivals = timeline.arrival_ms || {};
        const ways = Object.keys(timeline.ways || {})
            .filter(role => Number.isFinite(numberOrNaN(arrivals[role])))
            .map(role => ({ role, label: wayLabel(role), color: wayColor(role),
                arrivalMs: Number(arrivals[role]), points: finitePoints(timeline.ways[role]) }));
        if (ways.length < 2 || !ways.some(way => way.role === timeline.reference_role)) return null;
        return { referenceRole: timeline.reference_role, ways, fullBand: finitePoints(timeline.full_band) };
    }

    function timelineTicks(minMs, maxMs, widthPx) {
        const target = Math.max(4, Math.floor((Number(widthPx) || 640) / 72));
        const step = [0.05, 0.1, 0.2, 0.25, 0.5, 1, 2, 5, 10, 20, 50]
            .find(candidate => (maxMs - minMs) / candidate <= target) || 100;
        const ticks = [];
        for (let tick = Math.ceil(minMs / step) * step; tick <= maxMs + 1e-9; tick += step) {
            ticks.push(Number(tick.toFixed(6)));
        }
        return ticks;
    }

    // Timing lanes of the IR graph: one lane per take that carries a
    // timeline, all on one millisecond axis. The axis auto-ranges over the
    // lanes' arrivals like the frequency view's dB range over its traces.
    function timelineView(entries, { widthPx } = {}) {
        const lanes = [];
        (Array.isArray(entries) ? entries : []).forEach((entry) => {
            const timeline = takeTimeline(entry);
            if (!timeline) return;
            const take = entry.speaker_align_take?.take === 'after' ? 'after' : 'before';
            const side = entry.speaker_align_take?.side === 'right' ? 'Right' : 'Left';
            const arrivals = timeline.ways.map(way => way.arrivalMs);
            lanes.push({
                ...timeline,
                take,
                label: `${side} · ${take === 'after' ? 'After' : 'Before'}${entry.current ? '' : ' (saved)'}`,
                color: entry.graphColor || '',
                side,
                current: !!entry.current,
                spreadMs: Math.max(...arrivals) - Math.min(...arrivals),
            });
        });
        if (!lanes.length) return null;
        // Lanes keep the graph's entry order. Only within one pair (After
        // directly followed by Before of the same side, both current or both
        // saved) Before goes on top.
        for (let index = 0; index + 1 < lanes.length; index += 1) {
            const [first, second] = [lanes[index], lanes[index + 1]];
            if (first.take === 'after' && second.take === 'before'
                    && first.side === second.side && first.current === second.current) {
                lanes[index] = second;
                lanes[index + 1] = first;
                index += 1;
            }
        }
        const arrivals = lanes.flatMap(lane => lane.ways.map(way => way.arrivalMs));
        const earliest = Math.min(0, ...arrivals);
        const latest = Math.max(0, ...arrivals);
        const span = latest - earliest;
        const minMs = earliest - Math.max(1, span * 0.5);
        const maxMs = latest + Math.max(2, span * 0.75);
        return { lanes, minMs, maxMs, ticks: timelineTicks(minMs, maxMs, widthPx) };
    }

    // The IR graph's two parts. A Speaker Align take with a timeline always
    // draws as a timing lane; every other entry keeps the normal IR overlay.
    // Where an entry lands depends on that entry alone, never on the order or
    // visibility of the others.
    function irParts(entries, { widthPx } = {}) {
        const list = Array.isArray(entries) ? entries : [];
        return { timeline: timelineView(list, { widthPx }),
            plainEntries: list.filter(entry => !takeTimeline(entry)) };
    }

    function signedMs(value) {
        const rounded = Math.abs(value) < 0.0005 ? 0 : value;
        return `${rounded > 0 ? '+' : rounded < 0 ? '−' : ''}${Math.abs(rounded).toFixed(3)}`;
    }

    // Before reads the spread the proposal corrects, After the residual the
    // verification judged: the same numbers as the result table.
    function timelineSpreadText(lane) {
        return `${lane.take === 'after' ? 'residual' : 'spread'} ${lane.spreadMs.toFixed(3)} ms`;
    }

    function timelineSummary(view) {
        if (!view?.lanes?.length) return '';
        const references = [...new Set(view.lanes.map(lane => wayLabel(lane.referenceRole)))];
        const reference = references.length === 1 ? references[0] : 'reference way';
        const lanes = view.lanes.map(lane => `${lane.label} ${timelineSpreadText(lane)}`).join(' · ');
        return `Timing: 0 ms = ${reference} arrival (not delayed) · ${lanes}`;
    }

    function timelineHoverText(view, laneIndex, timeMs) {
        const lane = view?.lanes?.[laneIndex];
        if (!lane || !Number.isFinite(timeMs)) return '';
        const nearest = lane.ways.reduce((best, way) =>
            (!best || Math.abs(way.arrivalMs - timeMs) < Math.abs(best.arrivalMs - timeMs) ? way : best), null);
        return `${lane.label} · ${signedMs(timeMs)} ms · ${nearest.label} arrival ${signedMs(nearest.arrivalMs)} ms`;
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
            + `<th scope="col" data-tooltip="Planning take → verification take">Isolation</th></tr></thead>`
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
        wayColor,
        takeMeasurements,
        renderSpeakerAlignResult,
        takeTimeline,
        timelineView,
        irParts,
        timelineSpreadText,
        timelineSummary,
        timelineHoverText,
        signedMs,
    };
});
