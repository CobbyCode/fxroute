// SPDX-License-Identifier: AGPL-3.0-only
// Activate a Global preset through the same revisioned bank mutation as the UI.
async function assignGlobalPreset(fetch, preset) {
    const response = await fetch('/api/audio/output-state');
    const state = await response.json();
    if (!response.ok) throw new Error(JSON.stringify(state));
    const applied = await fetch('/api/audio/output-state/apply', {
        method: 'POST', body: JSON.stringify({ expected_revision: state.revision,
            mutation: { kind: 'set_bank_preset', mode: state.active_mode,
                bank_id: 'global', preset } }),
    });
    const result = await applied.json();
    if (!applied.ok) throw new Error(JSON.stringify(result));
    return result;
}

module.exports = { assignGlobalPreset };
