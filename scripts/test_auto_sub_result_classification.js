#!/usr/bin/env node
// AutoSub result classification contract for the 2.1 UI flow.
//
// The GUI must keep three backend facts separate instead of blending them
// into one "weak" verdict:
//   - scoring confidence (result.confidence: clear|close|uncertain)
//   - the apply decision (result.apply_decision / result.applied)
//   - the comparative score (result.winner.score_pct)
//
// "Weak" is reserved for the backend's uncertain confidence. A result that
// was not applied is rendered as kept-current/rejected, never automatically
// as weak, and a sub-50 comparative score alone never implies weak.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const root = path.join(__dirname, '..');

function makeContext() {
    const ctx = {
        window: {},
        console,
        setInterval() { return 0; },
        clearInterval() {},
        setTimeout() { return 0; },
        clearTimeout() {},
    };
    ctx.window = ctx;
    vm.createContext(ctx);
    vm.runInContext(fs.readFileSync(path.join(root, 'static', 'measurement_flows.js'), 'utf8'), ctx);
    return ctx;
}

async function classify(result) {
    const ctx = makeContext();
    const MF = ctx.FXRouteMeasurementFlows;
    assert.ok(typeof MF.handleAutoSubResult === 'function', 'measurement_flows must expose handleAutoSubResult');
    const state = {
        measurement: {
            statusText: '',
            autoSubResult: null,
            autoSubMeasurements: [],
            autoSubInFlight: false,
            currentMeasurementSaved: true,
            currentMeasurementName: '',
        },
    };
    const toasts = [];
    const statusEl = { _text: '' };
    Object.defineProperty(statusEl, 'textContent', {
        get() { return this._text; },
        set(value) { this._text = String(value); },
    });
    MF.init({
        getState: () => state,
        getElements: () => ({ measurementAutoSubStatus: statusEl }),
        showToast: (text, type) => toasts.push({ text, type }),
        renderMeasurementPanel: () => {},
        isSubwoofer22Mode: (mode) => mode === 'subwoofer-2.2' || mode === 'subwoofer-2.2-stereo',
    });
    await MF.handleAutoSubResult({ status: 'completed', message: '', result });
    return { state, toasts };
}

function base21Result(overrides = {}) {
    return {
        mode: 'subwoofer-2.1',
        original_alignment_ms: 0.0,
        applied_alignment_ms: 0.78,
        suggested_alignment_ms: 0.78,
        applied: true,
        confidence: 'clear',
        apply_decision: 'applied_clear_confidence',
        winner: { delay_ms: 0.78, score_pct: 62.2, score_L_pct: 63.0, score_R_pct: 61.4 },
        coarse_winner: { delay_ms: 0.8, score_pct: 62.0 },
        fine_scan: { triggered: true, status: 'completed' },
        ...overrides,
    };
}

(async () => {
    // 1. Applied + clear confidence: honest success, confidence is shown.
    {
        const { state, toasts } = await classify(base21Result());
        assert.match(state.measurement.statusText, /AutoSub applied: 0\.78 ms \(was 0\.00 ms\)/);
        assert.doesNotMatch(state.measurement.statusText, /weak/i);
        assert.equal(toasts[0].type, 'success');
        assert.match(toasts[0].text, /confidence clear/);
    }

    // 2. Incumbent won (not applied): "kept current alignment", not weak,
    //    not phrased as a rejected suggestion.
    {
        const { state, toasts } = await classify(base21Result({
            applied: false,
            applied_alignment_ms: 0.0,
            suggested_alignment_ms: 0.0,
            apply_decision: 'not_applied_incumbent_better',
        }));
        assert.match(state.measurement.statusText, /AutoSub kept current alignment: 0\.00 ms/);
        assert.doesNotMatch(state.measurement.statusText, /weak/i);
        assert.doesNotMatch(state.measurement.statusText, /not applied/);
        assert.equal(toasts[0].type, 'warning');
        assert.match(toasts[0].text, /Kept current alignment: 0\.00 ms/);
        assert.doesNotMatch(toasts[0].text, /weak/i);
        assert.doesNotMatch(toasts[0].text, /not applied/);
    }

    // 3. Incumbent won, older backend without apply_decision: the delay
    //    equality alone must classify it as kept-current.
    {
        const { state } = await classify(base21Result({
            applied: false,
            applied_alignment_ms: 0.0,
            suggested_alignment_ms: 0.0,
            apply_decision: undefined,
        }));
        assert.match(state.measurement.statusText, /AutoSub kept current alignment: 0\.00 ms/);
        assert.doesNotMatch(state.measurement.statusText, /weak/i);
    }

    // 4. Confirmation gate rejected the applied winner: rendered as
    //    suggested/not-applied with the failure reason, not as weak.
    {
        const { state, toasts } = await classify(base21Result({
            applied: false,
            applied_alignment_ms: 0.0,
            apply_decision: 'reverted_to_original_state',
            confidence: 'close',
            confirmation_gate: { action: 'reverted_to_original' },
        }));
        assert.match(state.measurement.statusText, /AutoSub suggested: 0\.78 ms \(was 0\.00 ms, not applied · final check failed/);
        assert.doesNotMatch(state.measurement.statusText, /weak/i);
        assert.equal(toasts[0].type, 'warning');
        assert.match(toasts[0].text, /not applied · final check failed/);
        assert.doesNotMatch(toasts[0].text, /weak/i);
    }

    // 5. Uncertain confidence: the only case that justifies the weak verdict.
    {
        const { state, toasts } = await classify(base21Result({
            applied: false,
            applied_alignment_ms: 0.0,
            apply_decision: 'not_applied_uncertain_confidence',
            confidence: 'uncertain',
        }));
        assert.match(state.measurement.statusText, /AutoSub result weak\. Check with a normal 2\.1 measurement\./);
        assert.equal(toasts[0].type, 'error');
        assert.match(toasts[0].text, /confidence uncertain/);
    }

    // 6. A sub-50 comparative score alone must not imply weak.
    {
        const { state, toasts } = await classify(base21Result({
            winner: { delay_ms: 0.78, score_pct: 30.0, score_L_pct: 31.0, score_R_pct: 29.0 },
        }));
        assert.doesNotMatch(state.measurement.statusText, /weak/i);
        assert.equal(toasts[0].type, 'success');
    }

    // 7. Legacy payload without confidence: unchanged applied wording, no
    //    invented confidence and no weak verdict.
    {
        const { state, toasts } = await classify(base21Result({ confidence: undefined }));
        assert.match(state.measurement.statusText, /AutoSub applied: 0\.78 ms \(was 0\.00 ms\)/);
        assert.doesNotMatch(state.measurement.statusText, /weak/i);
        assert.doesNotMatch(toasts[0].text, /confidence/);
    }

    console.log('ok auto-sub result classification (weak only via confidence; kept/rejected honest)');
})();
