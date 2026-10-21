// SPDX-License-Identifier: AGPL-3.0-only
/**
 * Automatic PEQ suggestion for the measurement PEQ assistant.
 *
 * Fits at most 12 bell/shelf filters so that measurement + filters follows
 * the target curve. Pure and deterministic (no DOM, no state): the result is
 * only a proposal that the editor loads into F1-F12.
 *
 * 1. The trusted trace(s) are resampled onto a fixed 1/24-octave grid,
 *    independent of the display smoothing; several sources are dB-averaged.
 * 2. Variable fractional-octave smoothing (1/12 oct in the modal bass,
 *    1/3 oct above 1 kHz, 1/2 oct in the top octaves) plus a dip-free
 *    envelope: a morphological closing about one octave wide removes every
 *    dip narrower than that, however deep.
 * 3. Free level offset (median over the passband) and a correction band
 *    that leaves the speaker's own low/high roll-off alone.
 * 4. Desired correction: peaks are cut as measured; boosts follow only the
 *    dip-free envelope, at most a few dB below a second, two-octave
 *    envelope, and fade out for deficits deeper than 6-12 dB, all under
 *    frequency-dependent caps. Narrow dips and cancellations (room modes,
 *    boundary interference) are therefore never filled.
 * 5. Greedy placement (largest cost reduction among area- and peak-ranked
 *    candidates), joint Levenberg-Marquardt refinement of every parameter on
 *    the summed biquad response, then pruning of filters that do not earn
 *    their place. Every filter must lower the weighted squared error by a
 *    minimum amount, and each filter's own response energy is penalised, so
 *    a well-matching curve gets few or no filters and filters do not work
 *    against each other.
 */
(function (root, factory) {
    const api = factory(root);
    root.FXRouteMeasurementPeqAuto = api;
    if (typeof module === 'object' && module.exports) module.exports = api;
})(typeof globalThis !== 'undefined' ? globalThis : window, function (root) {
    'use strict';

    const dsp = root.FXRouteMeasurementDsp || {};

    // Same rate as the graph preview of every PEQ filter.
    const SAMPLE_RATE = 48000;
    const GRID_MIN_HZ = 20;
    const GRID_MAX_HZ = 20000;
    const STEPS_PER_OCTAVE = 24;
    const STEP_OCT = 1 / STEPS_PER_OCTAVE;
    const MAX_FILTERS = 12;
    // A trusted trace has 192 points; a sparse curve cannot support a fit.
    const MIN_SOURCE_POINTS = 24;
    const MIN_BAND_OCTAVES = 1;
    // 1/3-octave deficit that marks the speaker's own roll-off at a band end.
    const ROLLOFF_DB = 6;
    // Passband for the level offset: points within this span of the upper 1/3-octave level.
    const PASSBAND_SPAN_DB = 10;
    // Boost allowed beyond the dip-free envelope, and beyond the wider one.
    const DIP_SLACK_DB = 1;
    const HOLE_SLACK_DB = 3;
    // A 1/3-octave deficit this deep is a cancellation rather than a tonal
    // error, whatever its width: the fill falls from the cap to DEEP_FILL_DB.
    const DEEP_DEFICIT_DB = [6, 12];
    const DEEP_FILL_DB = 2;
    // Boost fades in over this distance from a band end.
    const EDGE_TAPER_OCT = 0.5;
    const TOTAL_MAX_CUT_DB = -18;
    const BELL_GAIN_RANGE = [-15, 6];
    const SHELF_GAIN_RANGE = { low_shelf: [-12, 0], high_shelf: [-12, 6] };
    const SHELF_Q_RANGE = [0.5, 0.8];
    const LOW_SHELF_HZ = [25, 500];
    const HIGH_SHELF_HZ = [1000, 16000];
    const BELL_Q_MIN = 0.3;
    // Overshooting a wanted boost (or boosting where none is wanted) costs more than falling short.
    const OVERBOOST_FACTOR = 1.4;
    // Deepening an unfilled dip is less audible than a remaining peak.
    const DIP_ZONE_FACTOR = 0.6;
    // Total boost/cut limits are effectively hard.
    const LIMIT_WEIGHT = 100;
    // Penalty on each filter's own squared response (dB^2 x octave).
    const EFFORT_WEIGHT = 0.03;
    const MIN_GAIN_DB = 0.5;
    // A filter must lower the cost (dB^2 x octave) by at least this much,
    // e.g. fully correct a 2.5 dB peak 1/6 octave wide or 1 dB over an octave.
    const MIN_COST_GAIN = 0.6;
    const GOOD_FIT = { rmsDb: 0.75, maxDb: 2 };
    const ALREADY_MATCHING = { rmsDb: 1, maxDb: 2.5 };
    const CANDIDATE_ITERATIONS = 25;
    const JOINT_ITERATIONS = 30;
    const PRUNE_ITERATIONS = 20;
    const POLISH_ITERATIONS = 60;

    // Piecewise tables, interpolated over log frequency.
    // Analysis resolution in octaves. The trusted trace is already averaged
    // over 1/6 octave by the analyzer, so modal peaks keep their height in
    // the bass; above ~1 kHz only the 1/3-octave tonal balance is corrected,
    // finer in-room structure there depends on the microphone position.
    const ANALYSIS_WINDOW_OCT = [[100, 1 / 24], [1000, 1 / 3], [8000, 1 / 2]];
    // Width of the dip-free envelope: in a room, dips narrower than about an
    // octave are interference or modal cancellations that a boost cannot fix.
    const DIP_CLOSING_WINDOW_OCT = [[200, 1], [8000, 1.5]];
    // A second, wider envelope bounds holes about as wide as the first
    // window: at most HOLE_SLACK_DB below their wider surroundings is filled.
    const HOLE_CLOSING_WINDOW_OCT = [[200, 2], [8000, 3]];
    const ENVELOPE_SMOOTHING_OCT = [[GRID_MIN_HZ, 1 / 6]];
    // Smoothing of the curve that decides level and band ends.
    const BAND_WINDOW_OCT = [[GRID_MIN_HZ, 1 / 3]];
    // Boost ceiling: deep-bass boosts cost excursion and amplifier power;
    // the in-room treble droop is mostly the speaker's power response, and
    // boosting it flat would make the direct sound bright.
    const BOOST_CAP_DB = [[30, 3], [50, 6], [2000, 6], [6000, 3], [12000, 2]];
    // In-room response above ~5 kHz is the least repeatable part.
    const FREQUENCY_WEIGHT = [[4000, 1], [12000, 0.5]];
    // Narrow cuts only in the modal region; above it peaks are broad
    // features or position dependent, so the filters stay wide.
    const BELL_CUT_Q_MAX = [[100, 10], [400, 5], [2000, 3], [6000, 2]];
    // Boosts stay broad (at least ~0.7 octave): a narrow boost would fill a dip.
    const BELL_BOOST_Q_MAX = [[200, 2], [2000, 1.5]];
    // Same-sign filters closer than this are tried as one filter.
    const MERGE_DISTANCE_OCT = 0.25;

    const PARAM_KEYS = ['logFreq', 'gainDb', 'logQ'];
    const PARAM_STEPS = [0.01, 0.02, 0.01];
    const PARAM_MAX_STEP = [0.5, 6, 0.7];

    let cachedGrid = null;

    function clamp(value, low, high) {
        return Math.min(high, Math.max(low, value));
    }

    function logInterp(frequencyHz, table) {
        if (frequencyHz <= table[0][0]) return table[0][1];
        for (let index = 1; index < table.length; index += 1) {
            const [rightHz, rightValue] = table[index];
            if (frequencyHz <= rightHz) {
                const [leftHz, leftValue] = table[index - 1];
                const ratio = Math.log(frequencyHz / leftHz) / Math.log(rightHz / leftHz);
                return leftValue + ((rightValue - leftValue) * ratio);
            }
        }
        return table[table.length - 1][1];
    }

    function getGrid() {
        if (cachedGrid) return cachedGrid;
        const count = Math.floor(Math.log2(GRID_MAX_HZ / GRID_MIN_HZ) * STEPS_PER_OCTAVE) + 1;
        const grid = {
            count,
            frequencies: new Float64Array(count),
            z1r: new Float64Array(count),
            z1i: new Float64Array(count),
            z2r: new Float64Array(count),
            z2i: new Float64Array(count),
        };
        for (let index = 0; index < count; index += 1) {
            const frequency = GRID_MIN_HZ * (2 ** (index / STEPS_PER_OCTAVE));
            const omega = (2 * Math.PI * frequency) / SAMPLE_RATE;
            grid.frequencies[index] = frequency;
            grid.z1r[index] = Math.cos(omega);
            grid.z1i[index] = -Math.sin(omega);
            grid.z2r[index] = Math.cos(2 * omega);
            grid.z2i[index] = -Math.sin(2 * omega);
        }
        cachedGrid = grid;
        return grid;
    }

    function filterResponse(grid, filter, out = new Float64Array(grid.count)) {
        const coefficients = dsp.getMeasurementPeqBiquadCoefficients(filter, SAMPLE_RATE);
        if (!coefficients) return out.fill(dsp.clampMeasurementPeqGain(filter.gainDb));
        const { b0, b1, b2, a0, a1, a2 } = coefficients;
        for (let index = 0; index < grid.count; index += 1) {
            const nr = b0 + (b1 * grid.z1r[index]) + (b2 * grid.z2r[index]);
            const ni = (b1 * grid.z1i[index]) + (b2 * grid.z2i[index]);
            const dr = a0 + (a1 * grid.z1r[index]) + (a2 * grid.z2r[index]);
            const di = (a1 * grid.z1i[index]) + (a2 * grid.z2i[index]);
            out[index] = 10 * Math.log10(Math.max(1e-18, ((nr * nr) + (ni * ni)) / Math.max(1e-18, (dr * dr) + (di * di))));
        }
        return out;
    }

    function resampleTrace(grid, points = []) {
        const sorted = (Array.isArray(points) ? points : [])
            .map((point) => [Number(point?.[0]), Number(point?.[1])])
            .filter(([frequency, level]) => frequency > 0 && Number.isFinite(frequency) && Number.isFinite(level))
            .sort((left, right) => left[0] - right[0]);
        const out = new Float64Array(grid.count).fill(NaN);
        if (sorted.length < 2) return out;
        const firstHz = sorted[0][0];
        const lastHz = sorted[sorted.length - 1][0];
        let cursor = 0;
        for (let index = 0; index < grid.count; index += 1) {
            const frequency = grid.frequencies[index];
            if (frequency < firstHz || frequency > lastHz) continue;
            while (cursor < sorted.length - 2 && sorted[cursor + 1][0] < frequency) cursor += 1;
            const [leftHz, leftDb] = sorted[cursor];
            const [rightHz, rightDb] = sorted[cursor + 1];
            const ratio = rightHz > leftHz ? Math.log(frequency / leftHz) / Math.log(rightHz / leftHz) : 0;
            out[index] = leftDb + ((rightDb - leftDb) * clamp(ratio, 0, 1));
        }
        return out;
    }

    function averageCurves(grid, curves) {
        const out = new Float64Array(grid.count).fill(NaN);
        for (let index = 0; index < grid.count; index += 1) {
            let sum = 0;
            let complete = true;
            for (const curve of curves) {
                if (!Number.isFinite(curve[index])) {
                    complete = false;
                    break;
                }
                sum += curve[index];
            }
            if (complete && curves.length) out[index] = sum / curves.length;
        }
        return out;
    }

    // Gaussian fractional-octave smoothing with a frequency-dependent window
    // (same kernel shape as the display smoothing).
    function smoothOnGrid(grid, values, windowTable) {
        const out = new Float64Array(grid.count).fill(NaN);
        for (let index = 0; index < grid.count; index += 1) {
            if (!Number.isFinite(values[index])) continue;
            const halfWindowSteps = (logInterp(grid.frequencies[index], windowTable) / 2) * STEPS_PER_OCTAVE;
            const reach = Math.ceil(halfWindowSteps * 2.2);
            let sum = 0;
            let total = 0;
            for (let neighbor = Math.max(0, index - reach); neighbor <= Math.min(grid.count - 1, index + reach); neighbor += 1) {
                const value = values[neighbor];
                if (!Number.isFinite(value)) continue;
                const distance = (neighbor - index) / halfWindowSteps;
                const weight = Math.exp(-(distance * distance) * 1.7);
                sum += weight * value;
                total += weight;
            }
            out[index] = sum / total;
        }
        return out;
    }

    function runningExtreme(grid, values, windowTable, pick) {
        const out = new Float64Array(grid.count).fill(NaN);
        for (let index = 0; index < grid.count; index += 1) {
            if (!Number.isFinite(values[index])) continue;
            const reach = Math.round((logInterp(grid.frequencies[index], windowTable) / 2) * STEPS_PER_OCTAVE);
            let extreme = values[index];
            for (let neighbor = Math.max(0, index - reach); neighbor <= Math.min(grid.count - 1, index + reach); neighbor += 1) {
                if (Number.isFinite(values[neighbor])) extreme = pick(extreme, values[neighbor]);
            }
            out[index] = extreme;
        }
        return out;
    }

    // Dip-free envelope: closing (running max, then running min) erases every
    // dip narrower than the window and keeps peaks, slopes and broad deficits.
    function closeDips(grid, values, windowTable) {
        const dilated = runningExtreme(grid, values, windowTable, Math.max);
        const closed = runningExtreme(grid, dilated, windowTable, Math.min);
        return smoothOnGrid(grid, closed, ENVELOPE_SMOOTHING_OCT);
    }

    function weightedQuantile(values, weights, quantile) {
        const pairs = [];
        let totalWeight = 0;
        for (let index = 0; index < values.length; index += 1) {
            if (!Number.isFinite(values[index]) || !(weights[index] > 0)) continue;
            pairs.push([values[index], weights[index]]);
            totalWeight += weights[index];
        }
        if (!pairs.length) return NaN;
        pairs.sort((left, right) => left[0] - right[0]);
        let accumulated = 0;
        for (const [value, weight] of pairs) {
            accumulated += weight;
            if (accumulated >= quantile * totalWeight) return value;
        }
        return pairs[pairs.length - 1][0];
    }

    function correctionConfidence(sources, frequencyHz) {
        let boost = 0;
        let cut = 0;
        for (const source of sources) {
            const points = Array.isArray(source.confidence) ? source.confidence : [];
            const confidence = dsp.getMeasurementCorrectionConfidence(points, frequencyHz);
            boost += confidence.boostConfidence;
            cut += confidence.cutConfidence;
        }
        return { boost: boost / sources.length, cut: cut / sources.length };
    }

    function buildProblem(sources, targetPoints) {
        const grid = getGrid();
        const measured = averageCurves(grid, sources.map((source) => resampleTrace(grid, source.points)));
        const smooth = smoothOnGrid(grid, measured, ANALYSIS_WINDOW_OCT);
        const envelope = closeDips(grid, smooth, DIP_CLOSING_WINDOW_OCT);
        const wideEnvelope = closeDips(grid, smooth, HOLE_CLOSING_WINDOW_OCT);
        const broad = smoothOnGrid(grid, measured, BAND_WINDOW_OCT);
        const count = grid.count;
        const deviation = new Float64Array(count).fill(NaN);
        const envelopeDeviation = new Float64Array(count).fill(NaN);
        const wideEnvelopeDeviation = new Float64Array(count).fill(NaN);
        const broadDeviation = new Float64Array(count).fill(NaN);
        const baseWeight = new Float64Array(count);
        let first = -1;
        let last = -1;
        for (let index = 0; index < count; index += 1) {
            const frequency = grid.frequencies[index];
            baseWeight[index] = logInterp(frequency, FREQUENCY_WEIGHT);
            if (!Number.isFinite(smooth[index])) continue;
            const targetDb = dsp.getMeasurementConvolverCurveDbFromPoints(targetPoints, frequency);
            deviation[index] = smooth[index] - targetDb;
            envelopeDeviation[index] = envelope[index] - targetDb;
            wideEnvelopeDeviation[index] = wideEnvelope[index] - targetDb;
            broadDeviation[index] = broad[index] - targetDb;
            if (first < 0) first = index;
            last = index;
        }
        if (first < 0 || (last - first) * STEP_OCT < MIN_BAND_OCTAVES) return { reason: 'no-data' };

        // Level offset: median deviation over the passband, i.e. the points
        // near the top of the 1/3-octave curve. Roll-offs and stop bands of
        // a band-limited way (sub, satellite) do not drag the level down.
        const upper = weightedQuantile(broadDeviation, baseWeight, 0.9);
        const passbandWeight = baseWeight.map((weight, index) => (broadDeviation[index] >= upper - PASSBAND_SPAN_DB ? weight : 0));
        const levelDb = weightedQuantile(deviation, passbandWeight, 0.5);

        // Correction band: drop a continuous roll-off at either end.
        let low = first;
        while (low < last && !(broadDeviation[low] >= levelDb - ROLLOFF_DB)) low += 1;
        let high = last;
        while (high > low && !(broadDeviation[high] >= levelDb - ROLLOFF_DB)) high -= 1;
        for (let index = low; index <= high; index += 1) {
            if (!Number.isFinite(deviation[index])) return { reason: 'no-data' };
        }
        if ((high - low) * STEP_OCT < MIN_BAND_OCTAVES) return { reason: 'no-band' };
        const lowHz = grid.frequencies[low];
        const highHz = grid.frequencies[high];

        const inBand = new Uint8Array(count);
        const desired = new Float64Array(count);
        const requested = new Float64Array(count);
        const cap = new Float64Array(count);
        const sqrtWeight = new Float64Array(count);
        const dipZone = new Uint8Array(count);
        for (let index = low; index <= high; index += 1) {
            const frequency = grid.frequencies[index];
            const edgeOct = Math.min(Math.log2(frequency / lowHz), Math.log2(highHz / frequency));
            const boostCap = logInterp(frequency, BOOST_CAP_DB) * clamp(edgeOct / EDGE_TAPER_OCT, 0, 1);
            const confidence = correctionConfidence(sources, frequency);
            const wanted = levelDb - deviation[index];
            const fillable = Math.min(
                wanted,
                levelDb - envelopeDeviation[index] + DIP_SLACK_DB,
                levelDb - wideEnvelopeDeviation[index] + HOLE_SLACK_DB,
            );
            const broadDeficit = levelDb - broadDeviation[index];
            const depth = clamp((broadDeficit - DEEP_DEFICIT_DB[0]) / (DEEP_DEFICIT_DB[1] - DEEP_DEFICIT_DB[0]), 0, 1);
            const fillLimit = boostCap + ((Math.min(boostCap, DEEP_FILL_DB) - boostCap) * depth);
            inBand[index] = 1;
            requested[index] = wanted;
            cap[index] = boostCap;
            sqrtWeight[index] = Math.sqrt(baseWeight[index] * STEP_OCT);
            desired[index] = wanted <= 0
                ? Math.max(wanted, TOTAL_MAX_CUT_DB) * confidence.cut
                : clamp(fillable, 0, fillLimit) * confidence.boost;
            dipZone[index] = wanted - desired[index] > DIP_SLACK_DB ? 1 : 0;
        }
        return {
            reason: '',
            grid,
            count,
            low,
            high,
            lowHz,
            highHz,
            levelDb,
            inBand,
            desired,
            requested,
            cap,
            baseWeight,
            sqrtWeight,
            dipZone,
            sqrtLimitWeight: Math.sqrt(LIMIT_WEIGHT * STEP_OCT),
            residualLength: (2 * count) + MAX_FILTERS,
        };
    }

    function stateToFilter(state) {
        return { type: state.type, freqHz: 2 ** state.logFreq, gainDb: state.gainDb, q: Math.exp(state.logQ) };
    }

    function frequencyBoundsHz(problem, type) {
        const [low, high] = type === 'low_shelf' ? LOW_SHELF_HZ : (type === 'high_shelf' ? HIGH_SHELF_HZ : [GRID_MIN_HZ, GRID_MAX_HZ]);
        const lowHz = Math.max(problem.lowHz, low);
        return [lowHz, Math.max(lowHz, Math.min(problem.highHz, high))];
    }

    function projectState(problem, state) {
        const [lowHz, highHz] = frequencyBoundsHz(problem, state.type);
        state.logFreq = clamp(state.logFreq, Math.log2(lowHz), Math.log2(highHz));
        const frequency = 2 ** state.logFreq;
        if (state.type === 'bell') {
            state.gainDb = clamp(state.gainDb, BELL_GAIN_RANGE[0], BELL_GAIN_RANGE[1]);
            const cutQMax = logInterp(frequency, BELL_CUT_Q_MAX);
            const qMax = state.gainDb > 0 ? Math.min(cutQMax, logInterp(frequency, BELL_BOOST_Q_MAX)) : cutQMax;
            state.logQ = clamp(state.logQ, Math.log(BELL_Q_MIN), Math.log(qMax));
        } else {
            const [gainLow, gainHigh] = SHELF_GAIN_RANGE[state.type];
            state.gainDb = clamp(state.gainDb, gainLow, gainHigh);
            state.logQ = clamp(state.logQ, Math.log(SHELF_Q_RANGE[0]), Math.log(SHELF_Q_RANGE[1]));
        }
        return state;
    }

    // Residual vector: weighted in-band error, boost/cut limit excess (a
    // boost outside the band always exceeds its zero cap), one effort term
    // per filter.
    function computeResiduals(problem, total, efforts, out) {
        const { count } = problem;
        for (let index = 0; index < count; index += 1) {
            const correction = total[index];
            let main = 0;
            if (problem.inBand[index]) {
                const error = correction - problem.desired[index];
                let factor = 1;
                if (error > 0 && problem.desired[index] >= 0) factor = OVERBOOST_FACTOR;
                else if (error < 0 && problem.dipZone[index]) factor = DIP_ZONE_FACTOR;
                main = problem.sqrtWeight[index] * error * factor;
            }
            out[index] = main;
            let excess = 0;
            if (correction > problem.cap[index]) excess = correction - problem.cap[index];
            else if (correction < TOTAL_MAX_CUT_DB) excess = correction - TOTAL_MAX_CUT_DB;
            out[count + index] = problem.sqrtLimitWeight * excess;
        }
        for (let slot = 0; slot < MAX_FILTERS; slot += 1) {
            out[(2 * count) + slot] = slot < efforts.length ? Math.sqrt(EFFORT_WEIGHT * efforts[slot]) : 0;
        }
        return out;
    }

    function responseEffort(response) {
        let sum = 0;
        for (let index = 0; index < response.length; index += 1) sum += response[index] * response[index];
        return sum * STEP_OCT;
    }

    function sumSquares(values) {
        let sum = 0;
        for (let index = 0; index < values.length; index += 1) sum += values[index] * values[index];
        return sum;
    }

    function totalResponse(problem, states) {
        const total = new Float64Array(problem.count);
        const response = new Float64Array(problem.count);
        for (const state of states) {
            filterResponse(problem.grid, stateToFilter(state), response);
            for (let index = 0; index < problem.count; index += 1) total[index] += response[index];
        }
        return total;
    }

    function costOf(problem, states) {
        const residuals = new Float64Array(problem.residualLength);
        const efforts = states.map((state) => responseEffort(filterResponse(problem.grid, stateToFilter(state))));
        computeResiduals(problem, totalResponse(problem, states), efforts, residuals);
        return sumSquares(residuals);
    }

    function solveLinearSystem(matrix, vector) {
        const size = vector.length;
        const rows = matrix.map((row, index) => [...row, vector[index]]);
        for (let column = 0; column < size; column += 1) {
            let pivot = column;
            for (let row = column + 1; row < size; row += 1) {
                if (Math.abs(rows[row][column]) > Math.abs(rows[pivot][column])) pivot = row;
            }
            if (!(Math.abs(rows[pivot][column]) > 1e-15)) return null;
            [rows[column], rows[pivot]] = [rows[pivot], rows[column]];
            for (let row = column + 1; row < size; row += 1) {
                const factor = rows[row][column] / rows[column][column];
                if (!factor) continue;
                for (let cursor = column; cursor <= size; cursor += 1) rows[row][cursor] -= factor * rows[column][cursor];
            }
        }
        const solution = new Array(size).fill(0);
        for (let row = size - 1; row >= 0; row -= 1) {
            let sum = rows[row][size];
            for (let cursor = row + 1; cursor < size; cursor += 1) sum -= rows[row][cursor] * solution[cursor];
            solution[row] = sum / rows[row][row];
        }
        return solution.every(Number.isFinite) ? solution : null;
    }

    // Projected Levenberg-Marquardt over (log2 f, gain, ln Q) of the active
    // filters; the others stay fixed. Mutates states, returns the cost.
    function optimizeStates(problem, states, activeIndices, maxIterations) {
        const { grid, count, residualLength } = problem;
        const responses = states.map((state) => filterResponse(grid, stateToFilter(state)));
        const total = new Float64Array(count);
        responses.forEach((response) => {
            for (let index = 0; index < count; index += 1) total[index] += response[index];
        });
        const efforts = responses.map(responseEffort);
        let residuals = computeResiduals(problem, total, efforts, new Float64Array(residualLength));
        let cost = sumSquares(residuals);
        const paramCount = activeIndices.length * PARAM_KEYS.length;
        if (!paramCount) return cost;
        const jacobian = Array.from({ length: paramCount }, () => new Float64Array(residualLength));
        const probeResponse = new Float64Array(count);
        const probeTotal = new Float64Array(count);
        const probeResiduals = new Float64Array(residualLength);
        let damping = 1e-3;
        for (let iteration = 0; iteration < maxIterations; iteration += 1) {
            activeIndices.forEach((stateIndex, slot) => {
                PARAM_KEYS.forEach((key, param) => {
                    const probe = { ...states[stateIndex], [key]: states[stateIndex][key] + PARAM_STEPS[param] };
                    filterResponse(grid, stateToFilter(probe), probeResponse);
                    for (let index = 0; index < count; index += 1) {
                        probeTotal[index] = total[index] - responses[stateIndex][index] + probeResponse[index];
                    }
                    const probeEfforts = efforts.slice();
                    probeEfforts[stateIndex] = responseEffort(probeResponse);
                    computeResiduals(problem, probeTotal, probeEfforts, probeResiduals);
                    const column = jacobian[(slot * PARAM_KEYS.length) + param];
                    for (let row = 0; row < residualLength; row += 1) {
                        column[row] = (probeResiduals[row] - residuals[row]) / PARAM_STEPS[param];
                    }
                });
            });
            const normal = Array.from({ length: paramCount }, () => new Array(paramCount).fill(0));
            const gradient = new Array(paramCount).fill(0);
            for (let left = 0; left < paramCount; left += 1) {
                const leftColumn = jacobian[left];
                let gradientSum = 0;
                for (let row = 0; row < residualLength; row += 1) gradientSum += leftColumn[row] * residuals[row];
                gradient[left] = gradientSum;
                for (let right = left; right < paramCount; right += 1) {
                    const rightColumn = jacobian[right];
                    let sum = 0;
                    for (let row = 0; row < residualLength; row += 1) sum += leftColumn[row] * rightColumn[row];
                    normal[left][right] = sum;
                    normal[right][left] = sum;
                }
            }
            let accepted = null;
            for (let attempt = 0; attempt < 8 && !accepted; attempt += 1) {
                const damped = normal.map((row, index) => row.map((value, column) => (
                    index === column ? (value * (1 + damping)) + 1e-9 : value
                )));
                const delta = solveLinearSystem(damped, gradient.map((value) => -value));
                if (delta) {
                    const trialStates = states.map((state) => ({ ...state }));
                    activeIndices.forEach((stateIndex, slot) => {
                        PARAM_KEYS.forEach((key, param) => {
                            const step = clamp(delta[(slot * PARAM_KEYS.length) + param], -PARAM_MAX_STEP[param], PARAM_MAX_STEP[param]);
                            trialStates[stateIndex][key] += step;
                        });
                        projectState(problem, trialStates[stateIndex]);
                    });
                    const trialResponses = responses.slice();
                    const trialTotal = total.slice();
                    const trialEfforts = efforts.slice();
                    activeIndices.forEach((stateIndex) => {
                        const response = filterResponse(grid, stateToFilter(trialStates[stateIndex]));
                        for (let index = 0; index < count; index += 1) trialTotal[index] += response[index] - responses[stateIndex][index];
                        trialResponses[stateIndex] = response;
                        trialEfforts[stateIndex] = responseEffort(response);
                    });
                    const trialResiduals = computeResiduals(problem, trialTotal, trialEfforts, new Float64Array(residualLength));
                    const trialCost = sumSquares(trialResiduals);
                    if (trialCost < cost) {
                        accepted = { trialStates, trialResponses, trialTotal, trialEfforts, trialResiduals, trialCost };
                    }
                }
                damping = accepted ? Math.max(damping / 3, 1e-7) : damping * 4;
            }
            if (!accepted) break;
            const improvement = cost - accepted.trialCost;
            accepted.trialStates.forEach((state, index) => Object.assign(states[index], state));
            accepted.trialResponses.forEach((response, index) => { responses[index] = response; });
            total.set(accepted.trialTotal);
            accepted.trialEfforts.forEach((effort, index) => { efforts[index] = effort; });
            residuals = accepted.trialResiduals;
            cost = accepted.trialCost;
            if (improvement < 1e-7 * Math.max(1, cost)) break;
        }
        return cost;
    }

    function bandwidthToQ(bandwidthOct) {
        const ratio = 2 ** bandwidthOct;
        return Math.sqrt(ratio) / (ratio - 1);
    }

    function newState(problem, type, frequencyHz, gainDb, q) {
        return projectState(problem, { type, logFreq: Math.log2(frequencyHz), gainDb, logQ: Math.log(q) });
    }

    // Start points for the next filter: the residual's sign-consistent
    // segments ranked by area (broad deviations first) and by peak height.
    function proposeCandidates(problem, total) {
        const { grid, low, high } = problem;
        const segments = [];
        let segment = null;
        for (let index = low; index <= high; index += 1) {
            const residual = problem.desired[index] - total[index];
            const sign = residual > 0.5 ? 1 : (residual < -0.5 ? -1 : 0);
            if (!sign || !segment || segment.sign !== sign) {
                if (segment) segments.push(segment);
                segment = sign ? { sign, start: index, end: index, area: 0, peakIndex: index, peak: 0, sum: 0 } : null;
            }
            if (!segment) continue;
            segment.end = index;
            segment.area += problem.baseWeight[index] * Math.abs(residual) * STEP_OCT;
            segment.sum += residual;
            if (Math.abs(residual) > Math.abs(segment.peak)) {
                segment.peak = residual;
                segment.peakIndex = index;
            }
        }
        if (segment) segments.push(segment);
        // Wide enough that a residual the constraints cannot improve (e.g. a
        // Q-limited mode) does not hide the next correctable one.
        const byArea = [...segments].sort((left, right) => right.area - left.area).slice(0, 6);
        const byPeak = [...segments].sort((left, right) => Math.abs(right.peak) - Math.abs(left.peak)).slice(0, 4);
        const selected = [...new Set([...byArea, ...byPeak])];
        const candidates = [];
        for (const item of selected) {
            let left = item.peakIndex;
            let right = item.peakIndex;
            const halfHeight = Math.abs(item.peak) / 2;
            while (left > item.start && Math.abs(problem.desired[left - 1] - total[left - 1]) >= halfHeight) left -= 1;
            while (right < item.end && Math.abs(problem.desired[right + 1] - total[right + 1]) >= halfHeight) right += 1;
            const bandwidthOct = Math.max((right - left + 1) * STEP_OCT, 1 / 12);
            candidates.push(newState(problem, 'bell', grid.frequencies[item.peakIndex], item.peak, bandwidthToQ(bandwidthOct)));
            const spanOct = (item.end - item.start + 1) * STEP_OCT;
            const meanResidual = item.sum / (item.end - item.start + 1);
            if (spanOct >= 1 && item.sign < 0 && item.start - low <= STEPS_PER_OCTAVE / 3) {
                candidates.push(newState(problem, 'low_shelf', grid.frequencies[item.end] / (2 ** (1 / 3)), meanResidual, 0.7));
            }
            if (spanOct >= 1 && high - item.end <= STEPS_PER_OCTAVE / 3) {
                candidates.push(newState(problem, 'high_shelf', grid.frequencies[item.start] * (2 ** (1 / 3)), meanResidual, 0.7));
            }
        }
        return candidates;
    }

    function measureFit(problem, total) {
        let weighted = 0;
        let weightSum = 0;
        let maxAbs = 0;
        for (let index = problem.low; index <= problem.high; index += 1) {
            const error = total[index] - problem.desired[index];
            weighted += problem.baseWeight[index] * error * error;
            weightSum += problem.baseWeight[index];
            maxAbs = Math.max(maxAbs, Math.abs(error));
        }
        return { rmsDb: Math.sqrt(weighted / Math.max(1e-12, weightSum)), maxDb: maxAbs };
    }

    function fitIsWithin(problem, total, limits) {
        const fit = measureFit(problem, total);
        return fit.rmsDb <= limits.rmsDb && fit.maxDb <= limits.maxDb;
    }

    function cloneStates(states) {
        return states.map((state) => ({ ...state }));
    }

    function allIndices(states) {
        return states.map((_, index) => index);
    }

    function fitStates(problem, maxFilters) {
        if (fitIsWithin(problem, new Float64Array(problem.count), ALREADY_MATCHING)) return [];
        let states = [];
        let cost = costOf(problem, []);
        while (states.length < maxFilters) {
            let best = null;
            for (const candidate of proposeCandidates(problem, totalResponse(problem, states))) {
                const trial = [...cloneStates(states), candidate];
                const trialCost = optimizeStates(problem, trial, [trial.length - 1], CANDIDATE_ITERATIONS);
                if (!best || trialCost < best.cost) best = { states: trial, cost: trialCost };
            }
            if (!best || cost - best.cost < MIN_COST_GAIN) break;
            states = best.states;
            cost = optimizeStates(problem, states, allIndices(states), JOINT_ITERATIONS);
            if (fitIsWithin(problem, totalResponse(problem, states), GOOD_FIT)) break;
        }
        // Prune: drop the filter whose removal (after re-fitting the rest)
        // costs least, as long as it contributed less than a new filter must.
        while (states.length) {
            let best = null;
            for (let index = 0; index < states.length; index += 1) {
                const trial = cloneStates(states.filter((_, cursor) => cursor !== index));
                const trialCost = optimizeStates(problem, trial, allIndices(trial), PRUNE_ITERATIONS);
                if (trialCost - cost < MIN_COST_GAIN && (!best || trialCost < best.cost)) best = { states: trial, cost: trialCost };
            }
            if (!best) break;
            states = best.states;
            cost = best.cost;
        }
        ({ states, cost } = mergeNeighbours(problem, states, cost));
        optimizeStates(problem, states, allIndices(states), POLISH_ITERATIONS);
        return states;
    }

    // Two same-sign filters of one type close together usually stack to
    // reach a depth or width one filter can give: keep one if it does
    // nearly as well.
    function mergeNeighbours(problem, states, cost) {
        let current = states;
        let currentCost = cost;
        let merged = true;
        while (merged) {
            merged = false;
            for (let left = 0; left < current.length && !merged; left += 1) {
                for (let right = left + 1; right < current.length && !merged; right += 1) {
                    const first = current[left];
                    const second = current[right];
                    if (first.type !== second.type || Math.sign(first.gainDb) !== Math.sign(second.gainDb)) continue;
                    if (Math.abs(first.logFreq - second.logFreq) > MERGE_DISTANCE_OCT) continue;
                    const weightSum = Math.abs(first.gainDb) + Math.abs(second.gainDb);
                    const single = projectState(problem, {
                        type: first.type,
                        logFreq: ((first.logFreq * Math.abs(first.gainDb)) + (second.logFreq * Math.abs(second.gainDb))) / weightSum,
                        gainDb: first.gainDb + second.gainDb,
                        logQ: Math.min(first.logQ, second.logQ),
                    });
                    const trial = [...cloneStates(current.filter((_, index) => index !== left && index !== right)), single];
                    const trialCost = optimizeStates(problem, trial, allIndices(trial), PRUNE_ITERATIONS);
                    if (trialCost - currentCost < MIN_COST_GAIN) {
                        current = trial;
                        currentCost = trialCost;
                        merged = true;
                    }
                }
            }
        }
        return { states: current, cost: currentCost };
    }

    function roundFilter(filter) {
        return {
            type: filter.type,
            freqHz: Math.round(dsp.clampMeasurementPeqFrequency(filter.freqHz)),
            gainDb: Number(dsp.clampMeasurementPeqGain(filter.gainDb).toFixed(1)),
            q: Number(dsp.clampMeasurementPeqQ(filter.q).toFixed(2)),
        };
    }

    function describeFit(problem, filters) {
        const total = new Float64Array(problem.count);
        const response = new Float64Array(problem.count);
        filters.forEach((filter) => {
            filterResponse(problem.grid, filter, response);
            for (let index = 0; index < problem.count; index += 1) total[index] += response[index];
        });
        let before = 0;
        let after = 0;
        let weightSum = 0;
        let maxBoostDb = 0;
        let maxCutDb = 0;
        for (let index = problem.low; index <= problem.high; index += 1) {
            const weight = problem.baseWeight[index];
            const deviationBefore = -problem.requested[index];
            const deviationAfter = deviationBefore + total[index];
            before += weight * deviationBefore * deviationBefore;
            after += weight * deviationAfter * deviationAfter;
            weightSum += weight;
        }
        for (let index = 0; index < problem.count; index += 1) {
            maxBoostDb = Math.max(maxBoostDb, total[index]);
            maxCutDb = Math.min(maxCutDb, total[index]);
        }
        const round2 = (value) => Math.round(value * 100) / 100;
        return {
            rmsBeforeDb: round2(Math.sqrt(before / weightSum)),
            rmsAfterDb: round2(Math.sqrt(after / weightSum)),
            maxBoostDb: round2(maxBoostDb),
            maxCutDb: round2(maxCutDb),
            fit: measureFit(problem, total),
        };
    }

    /**
     * Suggest PEQ filters for measured + filters ~= target (+ free level).
     * sources: [{ points: [[Hz, dB], ...], confidence?: hybrid constraints }]
     * targetPoints: target curve [[Hz, dB], ...]
     * Returns { reason, filters: [{ type, freqHz, gainDb, q }], bandHz, levelDb, ...stats };
     * reason is '' with filters, 'matching' when none are needed, or
     * 'no-data' / 'no-band' when the source cannot be corrected.
     */
    function suggestMeasurementPeqFilters({ sources = [], targetPoints = [[20, 0], [20000, 0]], maxFilters = MAX_FILTERS } = {}) {
        // Every source must be usable: an L/R average must not silently become one side.
        const usable = Array.isArray(sources) ? sources : [];
        if (!usable.length || !usable.every((source) => Array.isArray(source?.points) && source.points.length >= MIN_SOURCE_POINTS)) {
            return { reason: 'no-data', filters: [] };
        }
        const problem = buildProblem(usable, Array.isArray(targetPoints) && targetPoints.length ? targetPoints : [[20, 0], [20000, 0]]);
        if (problem.reason) return { reason: problem.reason, filters: [] };
        const limit = clamp(Math.floor(Number(maxFilters) || 0), 0, MAX_FILTERS);
        const filters = fitStates(problem, limit)
            .map((state) => roundFilter(stateToFilter(state)))
            .filter((filter) => Math.abs(filter.gainDb) >= MIN_GAIN_DB)
            .sort((left, right) => left.freqHz - right.freqHz);
        return {
            reason: filters.length ? '' : 'matching',
            filters,
            bandHz: [Math.round(problem.lowHz), Math.round(problem.highHz)],
            levelDb: Math.round(problem.levelDb * 100) / 100,
            sourceCount: usable.length,
            ...describeFit(problem, filters),
        };
    }

    return {
        suggestMeasurementPeqFilters,
        // Exposed for tests and diagnostics.
        buildProblem,
        filterResponse,
        getGrid,
        smoothOnGrid,
        logInterp,
    };
});
