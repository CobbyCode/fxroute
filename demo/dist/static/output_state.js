// SPDX-License-Identifier: AGPL-3.0-only
/**
 * FXRoute output-state UI helpers (multichannel routing and area banks).
 * Pure catalog/mutation helpers plus thin DOM renderers for the Output
 * System settings section and the A/B bank selector. Browser-loadable UMD,
 * no build step; Node-testable via require().
 */
(function (root, factory) {
    const api = factory(root);
    root.FXRouteOutputState = api;
    if (typeof module === 'object' && module.exports) module.exports = api;
})(typeof globalThis !== 'undefined' ? globalThis : window, function (root) {
    'use strict';

const OUTPUT_MODES = ['stereo', 'crossover'];
const MUTATION_KINDS = ['set_routing', 'switch_mode', 'select_bank', 'set_bank_preset',
    'set_processing', 'set_bass', 'set_extras'];

function esc(value) {
    return String(value ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;')
        .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

function roleLabel(roleId) {
    if (roleId === 'global') return 'Global';
    if (roleId === 'off') return 'Off';
    const raw = String(roleId || '').split('_').filter(Boolean);
    if (!raw.length) return String(roleId || '');
    const words = [];
    for (let index = 0; index < raw.length; index += 1) {
        const part = raw[index];
        if (part === 'low' && raw[index + 1] === 'mid') {
            words.push('Low-Mid');
            index += 1;
        } else if (/^sub\d+$/.test(part)) {
            words.push(`Sub ${part.slice(3)}`);
        } else if (part === 'main') {
            words.push('Main');
        } else {
            words.push(part.charAt(0).toUpperCase() + part.slice(1));
        }
    }
    return words.join(' ');
}

function rolesForMode(mode, capabilities) {
    const roles = capabilities && capabilities.roles && capabilities.roles[mode];
    return Array.isArray(roles) ? roles.slice() : [];
}

function modeLabel(mode) {
    return mode === 'crossover' ? 'Crossover' : 'Stereo';
}

function subModeLabel(subMode) {
    if (subMode === 'stereo') return 'Stereo subs';
    if (subMode === 'dual-mono') return 'Dual-mono subs';
    if (subMode === 'mono') return 'Mono sub';
    return '';
}

function topologySummary(topology) {
    const topo = topology || {};
    const issues = Array.isArray(topo.issues) ? topo.issues.filter(Boolean) : [];
    const bits = [];
    if (typeof topo.way_count === 'number' && topo.way_count > 0) {
        bits.push(`${topo.way_count}-Way`);
    } else {
        bits.push('Stereo');
    }
    const sub = subModeLabel(topo.sub_mode);
    if (sub) bits.push(sub);
    let text = bits.join(' · ');
    if (issues.length) text += ` — ${issues[0]}`;
    return text;
}

function bankOptions(modeConfig, capabilities) {
    const banks = (modeConfig && modeConfig.banks) || {};
    const canonical = rolesForMode(modeConfig && modeConfig.topology && modeConfig.topology.mode, capabilities);
    const ordered = ['global', ...canonical.filter((id) => id !== 'global' && id in banks)];
    for (const id of Object.keys(banks)) {
        if (!ordered.includes(id)) ordered.push(id);
    }
    return ordered.map((id) => ({ id, label: roleLabel(id) }));
}

function bankInfoLine(bank) {
    if (!bank) return 'No bank selected';
    const preset = bank.preset || '—';
    const side = bank.active_side === 'B' ? 'B' : 'A';
    const other = bank.preset_b ? ` · B: ${bank.preset_b}` : '';
    return `Listening: ${preset} · A: ${bank.preset_a || '—'}${other} (slot ${side})`;
}

function buildMutation(kind, fields) {
    if (!MUTATION_KINDS.includes(kind)) throw new Error(`Unknown mutation kind: ${kind}`);
    return { kind, ...(fields || {}) };
}

async function fetchCatalog(fetchImpl) {
    const resp = await fetchImpl('/api/audio/output-state');
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) throw new Error((data && data.detail) || 'Output state catalog failed');
    return data;
}

async function applyMutation(fetchImpl, catalog, getCatalog, mutation) {
    const attempt = async (revision) => {
        const resp = await fetchImpl('/api/audio/output-state/apply', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ expected_revision: revision, mutation }),
        });
        const data = await resp.json().catch(() => ({}));
        if (resp.ok) return { data, catalog };
        if (resp.status === 409) {
            const fresh = await getCatalog();
            throw Object.assign(
                new Error((data && data.detail && data.detail.message) || 'Output state changed; refreshed'),
                { status: 409, data, catalog: fresh });
        }
        const detail = data && data.detail;
        throw new Error(typeof detail === 'string' ? detail : `Output state apply failed (${resp.status})`);
    };
    try {
        return await attempt(catalog.revision);
    } catch (error) {
        if (error && error.status === 409 && error.catalog) {
            const fresh = error.catalog;
            const resp = await fetchImpl('/api/audio/output-state/apply', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ expected_revision: fresh.revision, mutation }),
            });
            const data = await resp.json().catch(() => ({}));
            if (resp.ok) return { data, catalog: fresh };
            throw Object.assign(
                new Error((data && data.detail && data.detail.message) || 'Output state apply failed'),
                { status: resp.status, data, catalog: fresh });
        }
        throw error;
    }
}

function renderModeSelect(select, catalog, activeMode) {
    if (!select) return;
    const html = OUTPUT_MODES.map((mode) =>
        `<option value="${mode}"${mode === activeMode ? ' selected' : ''}>${modeLabel(mode)}</option>`).join('');
    if (select.innerHTML !== html) select.innerHTML = html;
    if (select.value !== activeMode) select.value = activeMode;
}

function renderRoutingGrid(grid, catalog, mode, assignments, channelCount, disabled) {
    if (!grid) return;
    const roles = ['off', ...rolesForMode(mode, catalog && catalog.capabilities)];
    const cells = [];
    for (let index = 0; index < channelCount; index += 1) {
        const current = assignments[index] || 'off';
        const options = roles.map((role) =>
            `<option value="${esc(role)}"${role === current ? ' selected' : ''}>${esc(roleLabel(role))}</option>`).join('');
        cells.push(`<div class="settings-routing-cell"><label for="os-routing-out-${index + 1}">Out ${index + 1}</label>`
            + `<select id="os-routing-out-${index + 1}" class="url-input" data-routing-output="${index}" aria-label="Output ${index + 1} role"${disabled ? ' disabled' : ''}>${options}</select></div>`);
    }
    const html = cells.join('');
    const signature = [html, assignments.join(','), disabled ? 'busy' : 'idle'].join('|');
    const active = typeof document !== 'undefined' && document.activeElement
        && grid.contains(document.activeElement);
    if (!active && grid.dataset.routingSignature !== signature) {
        grid.innerHTML = html;
        grid.dataset.routingSignature = signature;
    }
}

function renderBankSelector(select, info, catalog, mode) {
    const modeConfig = catalog && catalog.modes && catalog.modes[mode];
    if (!select) return null;
    if (!modeConfig) {
        select.innerHTML = '';
        if (info) info.textContent = '';
        return null;
    }
    const options = bankOptions(modeConfig, catalog.capabilities);
    const html = options.map((entry) =>
        `<option value="${esc(entry.id)}"${entry.id === modeConfig.selected_bank ? ' selected' : ''}>${esc(entry.label)}</option>`).join('');
    if (select.innerHTML !== html) select.innerHTML = html;
    if (select.value !== modeConfig.selected_bank) select.value = modeConfig.selected_bank;
    const bank = modeConfig.banks[modeConfig.selected_bank];
    if (info) info.textContent = bankInfoLine(bank);
    return bank || null;
}

    return {
        OUTPUT_MODES,
        MUTATION_KINDS,
        esc,
        roleLabel,
        rolesForMode,
        modeLabel,
        subModeLabel,
        topologySummary,
        bankOptions,
        bankInfoLine,
        buildMutation,
        fetchCatalog,
        applyMutation,
        renderModeSelect,
        renderRoutingGrid,
        renderBankSelector,
    };
});
