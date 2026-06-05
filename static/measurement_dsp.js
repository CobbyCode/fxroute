// SPDX-License-Identifier: AGPL-3.0-only
/**
 * FXRoute measurement/convolver DSP helpers.
 * Browser-loadable, no build step.
 */
(function (root, factory) {
    const api = factory(root);
    root.FXRouteMeasurementDsp = api;
    if (typeof module === 'object' && module.exports) module.exports = api;
})(typeof globalThis !== 'undefined' ? globalThis : window, function (root) {
    'use strict';

    const HYBRID_MIN_FULL_HZ = 180;
    const HYBRID_LINEAR_FULL_HZ = 550;

    function measurementSmoothingHalfWindowOctaves(mode = '1/6-oct') {
        switch (String(mode || '1/6-oct')) {
            case 'raw': return 0;
            case '1/1-oct': return 0.5;
            case '1/3-oct': return 1 / 6;
            case '1/6-oct':
            default:
                return 1 / 12;
        }
    }

    function smoothMeasurementTracePoints(points = [], mode = '1/6-oct') {
        if (!Array.isArray(points) || points.length < 5 || mode === 'raw') return points;
        const halfWindow = measurementSmoothingHalfWindowOctaves(mode);
        if (!(halfWindow > 0)) return points;
        return points.map(([frequency, level]) => {
            const logFrequency = Math.log2(Math.max(1e-9, frequency));
            let weightedLevel = 0;
            let totalWeight = 0;
            for (let cursor = 0; cursor < points.length; cursor += 1) {
                const [neighborFrequency, neighborLevel] = points[cursor];
                const logDelta = Math.abs(Math.log2(Math.max(1e-9, neighborFrequency)) - logFrequency);
                if (logDelta > halfWindow * 2.2) continue;
                const distance = logDelta / halfWindow;
                const weight = Math.exp(-(distance * distance) * 1.7);
                weightedLevel += neighborLevel * weight;
                totalWeight += weight;
            }
            return [frequency, totalWeight > 0 ? weightedLevel / totalWeight : level];
        });
    }

    function clampMeasurementConvolverFrequency(value, fallback = 20) {
        const numeric = Number(value);
        return Math.min(20000, Math.max(20, Number.isFinite(numeric) ? numeric : fallback));
    }

    function getMeasurementConvolverCurveDbFromPoints(points = [[20, 0], [20000, 0]], frequencyHz = 20) {
        const frequency = clampMeasurementConvolverFrequency(frequencyHz);
        if (!Array.isArray(points) || !points.length) return 0;
        if (frequency <= points[0][0]) return points[0][1];
        for (let index = 1; index < points.length; index += 1) {
            const [rightHz, rightDb] = points[index];
            const [leftHz, leftDb] = points[index - 1];
            if (frequency <= rightHz) {
                const ratio = (Math.log10(frequency) - Math.log10(leftHz)) / Math.max(1e-9, Math.log10(rightHz) - Math.log10(leftHz));
                return leftDb + ((rightDb - leftDb) * Math.min(1, Math.max(0, ratio)));
            }
        }
        return points[points.length - 1][1];
    }

    function getMeasurementConvolverAdaptiveDipGuardStrength(frequencyHz) {
        const frequency = clampMeasurementConvolverFrequency(frequencyHz);
        const ratio = (Math.log10(frequency) - Math.log10(40)) / Math.max(1e-9, Math.log10(8000) - Math.log10(40));
        return 0.25 + (0.65 * Math.min(1, Math.max(0, ratio)));
    }

    function applyMeasurementConvolverDipGuard(requestedCorrections, index, mode = 'off') {
        const current = requestedCorrections[index];
        if (!current || mode === 'off' || current.requestedDb <= 0) return { correctionDb: current?.requestedDb || 0, reductionDb: 0 };
        const radius = 2;
        const neighbors = [];
        for (let neighborIndex = Math.max(0, index - radius); neighborIndex <= Math.min(requestedCorrections.length - 1, index + radius); neighborIndex += 1) {
            if (neighborIndex !== index) neighbors.push(requestedCorrections[neighborIndex].requestedDb);
        }
        if (neighbors.length < 2) return { correctionDb: current.requestedDb, reductionDb: 0 };
        neighbors.sort((a, b) => a - b);
        const middle = Math.floor(neighbors.length / 2);
        const localMedian = neighbors.length % 2 ? neighbors[middle] : ((neighbors[middle - 1] + neighbors[middle]) / 2);
        const dipExcessDb = current.requestedDb - localMedian;
        const freeFillDb = mode === 'adaptive' ? 1.5 : 2.5;
        if (dipExcessDb <= freeFillDb) return { correctionDb: current.requestedDb, reductionDb: 0 };
        const strength = mode === 'adaptive' ? getMeasurementConvolverAdaptiveDipGuardStrength(current.frequency) : 0.5;
        const reductionDb = (dipExcessDb - freeFillDb) * strength;
        return {
            correctionDb: current.requestedDb - reductionDb,
            reductionDb,
        };
    }

    function analyzeMeasurementConvolverCorrections(points = [], curvePoints = [[20, 0], [20000, 0]], settings = {}) {
        const maxBoostDb = Number(settings.maxBoostDb ?? 6);
        const maxCutDb = Number(settings.maxCutDb ?? -9);
        const dipGuard = String(settings.dipGuard || 'off');
        const safetyMarginDb = Math.max(0, Number(settings.safetyMarginDb) || 1);
        const autoGainEnabled = settings.autoGainEnabled !== false;
        const requestedCorrections = points.map(([frequency, measuredDb]) => {
            const targetDb = getMeasurementConvolverCurveDbFromPoints(curvePoints, frequency);
            const requestedDb = targetDb - measuredDb;
            return { frequency, targetDb, measuredDb, requestedDb };
        });
        let dipGuardReductionMaxDb = 0;
        const corrections = requestedCorrections.map((item, index) => {
            const guarded = applyMeasurementConvolverDipGuard(requestedCorrections, index, dipGuard);
            dipGuardReductionMaxDb = Math.max(dipGuardReductionMaxDb, guarded.reductionDb || 0);
            return {
                ...item,
                dipGuardDb: Math.round((guarded.reductionDb || 0) * 10) / 10,
                correctionDb: Math.min(maxBoostDb, Math.max(maxCutDb, guarded.correctionDb)),
            };
        });
        const maxPositive = Math.max(0, ...corrections.map((item) => item.correctionDb));
        const minCorrection = Math.min(...corrections.map((item) => item.correctionDb));
        const autoGainDb = autoGainEnabled ? Math.round((-(maxPositive + safetyMarginDb)) * 2) / 2 : 0;
        const lowBassBoost = corrections.some((item) => item.frequency < 40 && item.correctionDb > 0.25);
        return { corrections, maxPositive, minCorrection, autoGainDb, lowBassBoost, dipGuardReductionMaxDb: Math.round(dipGuardReductionMaxDb * 10) / 10 };
    }

    function interpolateMeasurementConvolverCorrection(analysis, frequencyHz, autoGainDb) {
        const corrections = analysis?.corrections || [];
        if (!corrections.length) return autoGainDb;
        if (frequencyHz < corrections[0].frequency || frequencyHz > corrections[corrections.length - 1].frequency) return autoGainDb;
        for (let index = 1; index < corrections.length; index += 1) {
            const left = corrections[index - 1];
            const right = corrections[index];
            if (frequencyHz <= right.frequency) {
                const span = Math.max(1e-9, Math.log10(right.frequency) - Math.log10(left.frequency));
                const ratio = (Math.log10(Math.max(1, frequencyHz)) - Math.log10(left.frequency)) / span;
                const correctionDb = left.correctionDb + ((right.correctionDb - left.correctionDb) * Math.min(1, Math.max(0, ratio)));
                return correctionDb + autoGainDb;
            }
        }
        return corrections[corrections.length - 1].correctionDb + autoGainDb;
    }

    function buildMeasurementConvolverMagnitudeBins(analysis, sampleRate, length, autoGainDb) {
        const half = Math.floor(length / 2);
        const magnitudes = new Float64Array(half + 1);
        for (let bin = 0; bin <= half; bin += 1) {
            const frequency = (bin * sampleRate) / length;
            const gainDb = interpolateMeasurementConvolverCorrection(analysis, Math.max(20, frequency), autoGainDb);
            magnitudes[bin] = 10 ** (gainDb / 20);
        }
        return magnitudes;
    }

    function buildMeasurementConvolverLinearImpulseFromMagnitudes(magnitudes, length) {
        const half = Math.floor(length / 2);
        const impulse = new Float32Array(length);
        const shift = half;
        for (let n = 0; n < length; n += 1) {
            let sum = magnitudes[0] + (magnitudes[half] * Math.cos(Math.PI * n));
            for (let bin = 1; bin < half; bin += 1) {
                sum += 2 * magnitudes[bin] * Math.cos((2 * Math.PI * bin * n) / length);
            }
            impulse[(n + shift) % length] = sum / length;
        }
        return impulse;
    }

    function fftMeasurementConvolverComplex(real, imag, inverse = false) {
        const n = real.length;
        for (let i = 1, j = 0; i < n; i += 1) {
            let bit = n >> 1;
            for (; j & bit; bit >>= 1) j ^= bit;
            j ^= bit;
            if (i < j) {
                [real[i], real[j]] = [real[j], real[i]];
                [imag[i], imag[j]] = [imag[j], imag[i]];
            }
        }
        for (let len = 2; len <= n; len <<= 1) {
            const angle = (inverse ? 2 : -2) * Math.PI / len;
            const wLenReal = Math.cos(angle);
            const wLenImag = Math.sin(angle);
            for (let i = 0; i < n; i += len) {
                let wReal = 1;
                let wImag = 0;
                for (let j = 0; j < len / 2; j += 1) {
                    const uReal = real[i + j];
                    const uImag = imag[i + j];
                    const vReal = (real[i + j + (len / 2)] * wReal) - (imag[i + j + (len / 2)] * wImag);
                    const vImag = (real[i + j + (len / 2)] * wImag) + (imag[i + j + (len / 2)] * wReal);
                    real[i + j] = uReal + vReal;
                    imag[i + j] = uImag + vImag;
                    real[i + j + (len / 2)] = uReal - vReal;
                    imag[i + j + (len / 2)] = uImag - vImag;
                    const nextWReal = (wReal * wLenReal) - (wImag * wLenImag);
                    wImag = (wReal * wLenImag) + (wImag * wLenReal);
                    wReal = nextWReal;
                }
            }
        }
        if (inverse) {
            for (let i = 0; i < n; i += 1) {
                real[i] /= n;
                imag[i] /= n;
            }
        }
    }

    function buildMeasurementConvolverMinimumSpectrum(magnitudes, length) {
        const half = Math.floor(length / 2);
        const real = new Float64Array(length);
        const imag = new Float64Array(length);
        for (let bin = 0; bin <= half; bin += 1) real[bin] = Math.log(Math.max(1e-7, magnitudes[bin]));
        for (let bin = half + 1; bin < length; bin += 1) real[bin] = real[length - bin];
        fftMeasurementConvolverComplex(real, imag, true);
        for (let index = 1; index < half; index += 1) {
            real[index] *= 2;
            imag[index] *= 2;
        }
        for (let index = half + 1; index < length; index += 1) {
            real[index] = 0;
            imag[index] = 0;
        }
        fftMeasurementConvolverComplex(real, imag, false);
        const spectrumReal = new Float64Array(length);
        const spectrumImag = new Float64Array(length);
        for (let bin = 0; bin < length; bin += 1) {
            const magnitude = Math.exp(Math.max(-24, Math.min(24, real[bin])));
            spectrumReal[bin] = magnitude * Math.cos(imag[bin]);
            spectrumImag[bin] = magnitude * Math.sin(imag[bin]);
        }
        return { real: spectrumReal, imag: spectrumImag };
    }

    function getMeasurementConvolverHybridLinearWeight(frequencyHz) {
        if (frequencyHz <= HYBRID_MIN_FULL_HZ) return 0;
        if (frequencyHz >= HYBRID_LINEAR_FULL_HZ) return 1;
        const ratio = (Math.log(Math.max(1, frequencyHz)) - Math.log(HYBRID_MIN_FULL_HZ))
            / Math.max(1e-9, Math.log(HYBRID_LINEAR_FULL_HZ) - Math.log(HYBRID_MIN_FULL_HZ));
        return 0.5 - (0.5 * Math.cos(Math.PI * Math.min(1, Math.max(0, ratio))));
    }

    function getMeasurementConvolverHybridTransition() {
        return {
            hybridMinHz: HYBRID_MIN_FULL_HZ,
            hybridLinearHz: HYBRID_LINEAR_FULL_HZ,
        };
    }

    function buildMeasurementConvolverHybridSpectrum(magnitudes, sampleRate, length) {
        const half = Math.floor(length / 2);
        const minimumSpectrum = buildMeasurementConvolverMinimumSpectrum(magnitudes, length);
        const real = new Float64Array(length);
        const imag = new Float64Array(length);
        const delaySamples = half;
        for (let bin = 0; bin < length; bin += 1) {
            const mirroredBin = bin <= half ? bin : length - bin;
            const frequency = (mirroredBin * sampleRate) / length;
            const linearWeight = getMeasurementConvolverHybridLinearWeight(frequency);
            const minimumWeight = 1 - linearWeight;
            const linearReal = magnitudes[mirroredBin] || 0;
            const blendedReal = (minimumSpectrum.real[bin] * minimumWeight) + (linearReal * linearWeight);
            const blendedImag = minimumSpectrum.imag[bin] * minimumWeight;
            const phase = (-2 * Math.PI * bin * delaySamples) / length;
            const delayReal = Math.cos(phase);
            const delayImag = Math.sin(phase);
            real[bin] = (blendedReal * delayReal) - (blendedImag * delayImag);
            imag[bin] = (blendedReal * delayImag) + (blendedImag * delayReal);
        }
        return { real, imag };
    }

    function buildMeasurementConvolverImpulseFromSpectrum(real, imag) {
        const length = real.length;
        const workReal = new Float64Array(real);
        const workImag = new Float64Array(imag);
        fftMeasurementConvolverComplex(workReal, workImag, true);
        const impulse = new Float32Array(length);
        for (let index = 0; index < length; index += 1) impulse[index] = workReal[index];
        return impulse;
    }

    function buildMeasurementConvolverImpulse(analysis, sampleRate, length, autoGainDb, phaseMode = 'linear') {
        const magnitudes = buildMeasurementConvolverMagnitudeBins(analysis, sampleRate, length, autoGainDb);
        if (phaseMode === 'minimum') {
            const spectrum = buildMeasurementConvolverMinimumSpectrum(magnitudes, length);
            return buildMeasurementConvolverImpulseFromSpectrum(spectrum.real, spectrum.imag);
        }
        if (phaseMode === 'hybrid_aligned') {
            const spectrum = buildMeasurementConvolverHybridSpectrum(magnitudes, sampleRate, length);
            return buildMeasurementConvolverImpulseFromSpectrum(spectrum.real, spectrum.imag);
        }
        return buildMeasurementConvolverLinearImpulseFromMagnitudes(magnitudes, length);
    }

    function writeMeasurementConvolverWav(channels, sampleRate) {
        const channelCount = channels.length;
        const frameCount = channels[0]?.length || 0;
        const bytesPerSample = 4;
        const bitsPerSample = 32;
        const formatTag = 3; // WAVE_FORMAT_IEEE_FLOAT
        const dataBytes = frameCount * channelCount * bytesPerSample;
        const buffer = new ArrayBuffer(44 + dataBytes);
        const view = new DataView(buffer);
        const writeString = (offset, value) => Array.from(value).forEach((char, index) => view.setUint8(offset + index, char.charCodeAt(0)));
        const peakBeforeExport = getMeasurementConvolverChannelPeak(channels);
        writeString(0, 'RIFF');
        view.setUint32(4, 36 + dataBytes, true);
        writeString(8, 'WAVE');
        writeString(12, 'fmt ');
        view.setUint32(16, 16, true);
        view.setUint16(20, formatTag, true);
        view.setUint16(22, channelCount, true);
        view.setUint32(24, sampleRate, true);
        view.setUint32(28, sampleRate * channelCount * bytesPerSample, true);
        view.setUint16(32, channelCount * bytesPerSample, true);
        view.setUint16(34, bitsPerSample, true);
        writeString(36, 'data');
        view.setUint32(40, dataBytes, true);
        let offset = 44;
        for (let frame = 0; frame < frameCount; frame += 1) {
            for (let channel = 0; channel < channelCount; channel += 1) {
                const sample = channels[channel]?.[frame];
                const exportSample = Number.isFinite(sample) ? sample : 0;
                view.setFloat32(offset, exportSample, true);
                offset += 4;
            }
        }
        const peakAfterExportReadback = getMeasurementConvolverFloat32WavDataPeak(view, 44, frameCount, channelCount);
        console.debug('[measurement-convolver-wav-export]', {
            format: 'float32',
            sampleRate,
            channels: channelCount,
            frames: frameCount,
            peakBeforeExport,
            peakAfterExportReadback,
            clippedSampleCount: 0,
        });
        return new root.Blob([buffer], { type: 'audio/wav' });
    }

    function getMeasurementConvolverChannelPeak(channels) {
        let peak = 0;
        for (let channel = 0; channel < channels.length; channel += 1) {
            const data = channels[channel] || [];
            for (let frame = 0; frame < data.length; frame += 1) {
                const sample = data[frame];
                const absValue = Math.abs(Number.isFinite(sample) ? sample : 0);
                if (absValue > peak) peak = absValue;
            }
        }
        return peak;
    }

    function getMeasurementConvolverFloat32WavDataPeak(view, dataOffset, frameCount, channelCount) {
        let peak = 0;
        let offset = dataOffset;
        for (let frame = 0; frame < frameCount; frame += 1) {
            for (let channel = 0; channel < channelCount; channel += 1) {
                const sample = view.getFloat32(offset, true);
                const absValue = Math.abs(Number.isFinite(sample) ? sample : 0);
                if (absValue > peak) peak = absValue;
                offset += 4;
            }
        }
        return peak;
    }

    function getSortedNumericValues(values = []) {
        return values.filter(value => Number.isFinite(value)).sort((a, b) => a - b);
    }

    function getValueQuantile(sortedValues = [], quantile = 0.5) {
        if (!sortedValues.length) return 0;
        if (sortedValues.length === 1) return sortedValues[0];
        const clamped = Math.min(1, Math.max(0, quantile));
        const position = (sortedValues.length - 1) * clamped;
        const lowerIndex = Math.floor(position);
        const upperIndex = Math.ceil(position);
        if (lowerIndex === upperIndex) return sortedValues[lowerIndex];
        const weight = position - lowerIndex;
        return sortedValues[lowerIndex] + ((sortedValues[upperIndex] - sortedValues[lowerIndex]) * weight);
    }

    function getMeasurementGraphRange(entries = []) {
        const focusValues = [];
        entries.forEach(entry => {
            (entry.traces || []).forEach(trace => {
                (trace.points || []).forEach(([, level]) => {
                    focusValues.push(level);
                });
            });
        });
        const values = getSortedNumericValues(focusValues);
        if (!values.length) {
            return { minDb: -18, maxDb: 18 };
        }
        const useRobustWindow = values.length >= 24;
        const rawMinDb = useRobustWindow ? Math.min(getValueQuantile(values, 0.02), 0) : Math.min(...values, 0);
        const rawMaxDb = useRobustWindow ? Math.max(getValueQuantile(values, 0.98), 0) : Math.max(...values, 0);
        const peakAbs = Math.max(Math.abs(rawMinDb), Math.abs(rawMaxDb), 9);
        const paddedPeakAbs = Math.ceil((peakAbs + 3) / 3) * 3;
        return {
            minDb: -Math.min(24, paddedPeakAbs),
            maxDb: Math.min(24, paddedPeakAbs),
        };
    }

    function measurementFrequencyToX(frequency, bounds) {
        const minLog = Math.log10(20);
        const maxLog = Math.log10(20000);
        const valueLog = Math.log10(Math.min(20000, Math.max(20, frequency)));
        return bounds.left + ((valueLog - minLog) / (maxLog - minLog)) * bounds.width;
    }

    function measurementDbToY(level, bounds, range) {
        const ratio = (range.maxDb - level) / Math.max(1, range.maxDb - range.minDb);
        return bounds.top + ratio * bounds.height;
    }

    function measurementXToFrequency(x, bounds) {
        const minLog = Math.log10(20);
        const maxLog = Math.log10(20000);
        const ratio = Math.min(1, Math.max(0, (x - bounds.left) / Math.max(1, bounds.width)));
        return 10 ** (minLog + ((maxLog - minLog) * ratio));
    }

    function measurementYToDb(y, bounds, range) {
        const ratio = Math.min(1, Math.max(0, (y - bounds.top) / Math.max(1, bounds.height)));
        return range.maxDb - (ratio * (range.maxDb - range.minDb));
    }

    function clampMeasurementPeqFrequency(value) {
        return Math.min(20000, Math.max(20, Number(value) || 20));
    }

    function clampMeasurementPeqGain(value) {
        return Math.min(24, Math.max(-24, Number(value) || 0));
    }

    function clampMeasurementPeqQ(value) {
        return Math.min(20, Math.max(0.1, Number(value) || 1));
    }

    function getMeasurementPeqFilterMagnitude(filter = {}, frequencyHz = 1000, sampleRate = 48000) {
        const type = String(filter.type || 'bell');
        if (type === 'gain') return clampMeasurementPeqGain(filter.gainDb || 0);

        const freq = clampMeasurementPeqFrequency(filter.freqHz || 1000);
        const q = clampMeasurementPeqQ(filter.q || 1);
        const gainDb = clampMeasurementPeqGain(filter.gainDb || 0);
        const A = 10 ** (gainDb / 40);
        const w0 = (2 * Math.PI * freq) / sampleRate;
        const cos = Math.cos(w0);
        const sin = Math.sin(w0);
        const alpha = sin / (2 * q);
        let b0 = 1; let b1 = 0; let b2 = 0; let a0 = 1; let a1 = 0; let a2 = 0;

        if (type === 'bell') {
            b0 = 1 + (alpha * A);
            b1 = -2 * cos;
            b2 = 1 - (alpha * A);
            a0 = 1 + (alpha / A);
            a1 = -2 * cos;
            a2 = 1 - (alpha / A);
        } else if (type === 'notch') {
            b0 = 1;
            b1 = -2 * cos;
            b2 = 1;
            a0 = 1 + alpha;
            a1 = -2 * cos;
            a2 = 1 - alpha;
        } else if (type === 'low_shelf' || type === 'high_shelf') {
            const shelfAlpha = sin / 2 * Math.sqrt(Math.max(0, (A + (1 / A)) * ((1 / q) - 1) + 2));
            const beta = 2 * Math.sqrt(A) * shelfAlpha;
            if (type === 'low_shelf') {
                b0 = A * ((A + 1) - ((A - 1) * cos) + beta);
                b1 = 2 * A * ((A - 1) - ((A + 1) * cos));
                b2 = A * ((A + 1) - ((A - 1) * cos) - beta);
                a0 = (A + 1) + ((A - 1) * cos) + beta;
                a1 = -2 * ((A - 1) + ((A + 1) * cos));
                a2 = (A + 1) + ((A - 1) * cos) - beta;
            } else {
                b0 = A * ((A + 1) + ((A - 1) * cos) + beta);
                b1 = -2 * A * ((A - 1) + ((A + 1) * cos));
                b2 = A * ((A + 1) + ((A - 1) * cos) - beta);
                a0 = (A + 1) - ((A - 1) * cos) + beta;
                a1 = 2 * ((A - 1) - ((A + 1) * cos));
                a2 = (A + 1) - ((A - 1) * cos) - beta;
            }
        } else if (type === 'low_pass' || type === 'high_pass') {
            if (type === 'low_pass') {
                b0 = (1 - cos) / 2;
                b1 = 1 - cos;
                b2 = (1 - cos) / 2;
            } else {
                b0 = (1 + cos) / 2;
                b1 = -(1 + cos);
                b2 = (1 + cos) / 2;
            }
            a0 = 1 + alpha;
            a1 = -2 * cos;
            a2 = 1 - alpha;
        }

        const omega = (2 * Math.PI * clampMeasurementPeqFrequency(frequencyHz)) / sampleRate;
        const z1r = Math.cos(-omega);
        const z1i = Math.sin(-omega);
        const z2r = Math.cos(-2 * omega);
        const z2i = Math.sin(-2 * omega);
        const nr = b0 + (b1 * z1r) + (b2 * z2r);
        const ni = (b1 * z1i) + (b2 * z2i);
        const dr = a0 + (a1 * z1r) + (a2 * z2r);
        const di = (a1 * z1i) + (a2 * z2i);
        const numerator = Math.hypot(nr, ni);
        const denominator = Math.max(1e-9, Math.hypot(dr, di));
        return 20 * Math.log10(Math.max(1e-9, numerator / denominator));
    }

    return {
        measurementSmoothingHalfWindowOctaves,
        smoothMeasurementTracePoints,
        clampMeasurementConvolverFrequency,
        getMeasurementConvolverCurveDbFromPoints,
        getMeasurementConvolverAdaptiveDipGuardStrength,
        applyMeasurementConvolverDipGuard,
        analyzeMeasurementConvolverCorrections,
        interpolateMeasurementConvolverCorrection,
        buildMeasurementConvolverMagnitudeBins,
        buildMeasurementConvolverLinearImpulseFromMagnitudes,
        fftMeasurementConvolverComplex,
        buildMeasurementConvolverMinimumSpectrum,
        getMeasurementConvolverHybridTransition,
        buildMeasurementConvolverHybridSpectrum,
        buildMeasurementConvolverImpulseFromSpectrum,
        buildMeasurementConvolverImpulse,
        writeMeasurementConvolverWav,
        getSortedNumericValues,
        getValueQuantile,
        getMeasurementGraphRange,
        measurementFrequencyToX,
        measurementDbToY,
        measurementXToFrequency,
        measurementYToDb,
        clampMeasurementPeqFrequency,
        clampMeasurementPeqGain,
        clampMeasurementPeqQ,
        getMeasurementPeqFilterMagnitude,
    };
});
