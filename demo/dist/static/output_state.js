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

const OUTPUT_MODES = ['stereo', 'stereo-sub'];
const MUTATION_KINDS = ['set_routing', 'switch_mode', 'select_bank', 'set_bank_preset',
    'switch_all_banks', 'set_crossover', 'set_subwoofers', 'set_processing', 'set_extras'];

function esc(value) {
    return String(value ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;')
        .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

function roleLabel(roleId) {
    if (roleId === 'global') return 'Global';
    if (roleId === 'all') return 'All Banks';
    if (['main', 'low', 'low_mid', 'mid', 'high', 'sub'].includes(roleId)) {
        return `${roleId.split('_').map(word => word.charAt(0).toUpperCase() + word.slice(1)).join('-')} L/R`;
    }
    if (roleId === 'off') return 'Off';
    if (/^(left|right)_/.test(roleId)) {
        const [side, ...way] = roleId.split('_');
        return `${way.map(word => word.charAt(0).toUpperCase() + word.slice(1)).join('-')} ${side === 'left' ? 'L' : 'R'}`;
    }
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
    return { stereo: 'Stereo', 'stereo-sub': 'Stereo + Sub', surround: 'Surround' }[mode] || mode;
}

// Two hardware outputs can only carry Main L/R, so the Mode selector is
// hidden and the system is internally fixed Stereo. Three or more outputs
// can additionally route subs.
function modeSelectorVisible(channelCount) {
    return Number(channelCount) >= 3;
}

function subModeLabel(subMode) {
    if (subMode === 'stereo') return 'Stereo subs';
    if (subMode === 'dual-mono') return 'Dual-mono subs';
    if (subMode === 'mono') return 'Mono sub';
    return '';
}

// Adapter for the existing sub tile and optimizer algorithms; never persisted.
function subwooferView(catalog) {
    const config = catalog?.modes?.[catalog.active_mode];
    const topology = config?.topology || {};
    const roles = topology.sub_roles || [];
    const processing = config?.processing || {};
    const first = processing[roles[0]] || {};
    const offset = Math.max(0, ...(topology.roles || []).map(role => -(processing[role]?.alignment_ms || 0)));
    const bass = config?.bass_management || {};
    return {
        mode: { mono: 'subwoofer-2.1', 'dual-mono': 'subwoofer-2.2', stereo: 'subwoofer-2.2-stereo' }[topology.sub_mode] || 'stereo',
        roles, sub_mode: topology.sub_mode || 'none',
        subwoofer: { crossover_frequency_hz: bass.frequency_hz ?? 80, slope: 'LR24',
            main_highpass_enabled: bass.main_highpass_enabled ?? true,
            sub_level_db: first.level_db || 0, sub_alignment_ms: first.alignment_ms || 0,
            sub_polarity: first.polarity || 'normal' },
        subwoofers: { sub1: first, sub2: processing[roles[1]] || {} },
        derived_main_delay_ms: offset,
        derived_sub1_delay_ms: offset + (first.alignment_ms || 0),
        derived_sub2_delay_ms: offset + (processing[roles[1]]?.alignment_ms || 0),
        routing: { status: (catalog?.device?.routing?.[catalog.active_mode] || [])
            .map((role, index) => role !== 'off' ? `Out ${index + 1} ${roleLabel(role)}` : '')
            .filter(Boolean).join(' · ') },
    };
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

// Sub roles the routing sums from both inputs at 0.5 gain: only a real
// sub_l/sub_r pair feeds each sub from its own side, so a lone sub_l in a mono
// routing is a summed output, not a left-sided one.
function summedRoleIds(topology) {
    const topo = topology || {};
    const subs = Array.isArray(topo.sub_roles) ? topo.sub_roles.map(String).filter(Boolean) : [];
    if (!subs.length || String(topo.sub_mode || '') === 'stereo') return [];
    return subs;
}

function measurementArea(catalog, bankId) {
    const mode = (catalog && catalog.active_mode) || 'stereo';
    const modeConfig = (catalog && catalog.modes && catalog.modes[mode]) || {};
    const selected = String(bankId || modeConfig.selected_bank || 'global');
    const bank = modeConfig.banks?.[selected];
    const label = bank?.label || roleLabel(selected);
    const available = selected !== 'all';
    const channel_mode = bank?.channel_mode || 'stereo';
    const repeat_supported = available && channel_mode === 'stereo';
    const repeat_note = !available ? 'Select a filter bank for measurement.'
        : repeat_supported ? '' : `${label} is a mono target. Use a single sweep.`;
    return {
        bank_id: selected,
        label,
        channel: 'stereo',
        channel_mode,
        available,
        note: !available ? repeat_note : selected === 'global'
            ? 'Whole system: Global plus every area bank stay audible for this sweep.'
            : `Only ${label} stays audible; every other output is muted for this sweep.`,
        repeat_supported,
        repeat_note,
    };
}

function bankOptions(modeConfig) {
    const banks = modeConfig?.banks || {};
    return [{ id: 'global', label: 'Global' }, { id: 'all', label: 'All Banks' },
        ...Object.entries(banks).filter(([id]) => id !== 'global').map(([id, bank]) => ({ id, label: bank.label || roleLabel(id) }))];
}

function bankSelectorVisible(modeConfig) {
    const roles = modeConfig?.topology?.roles || [];
    return roles.length > 0 && !(roles.length === 2 && roles.includes('main_l') && roles.includes('main_r'));
}

function bankBinding(catalog) {
    if (!catalog) return null;
    const bankId = catalog.modes?.[catalog.active_mode]?.selected_bank || 'global';
    if (bankId === 'all') throw new Error('Select a filter bank for import or measurement.');
    return { bank_mode: catalog.active_mode, bank_id: bankId, expected_revision: catalog.revision };
}

function peqParams(channelMode, leftBands, rightBands, eqMode) {
    return channelMode === 'mono'
        ? { channelMode: 'stereo-linked', eqMode, bands: leftBands }
        : { channelMode: 'dual', eqMode, leftBands, rightBands };
}

function compareState(catalog) {
    const config = catalog?.modes?.[catalog.active_mode];
    if (!config) return null;
    const aggregate = config.selected_bank === 'all';
    const bank = aggregate ? config.all_banks : config.banks?.[config.selected_bank];
    if (!bank) return null;
    const presetA = bank.preset_a || '';
    const presetB = bank.preset_b || '';
    return {
        compare: { presetA, presetB, activeSide: bank.active_side },
        activePreset: bank.preset || '', effectiveActiveSide: bank.active_side,
        presetA, presetB, aggregate, canA: bank.can_a ?? !!presetA, canB: bank.can_b ?? !!presetB,
    };
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
    const html = (catalog?.capabilities?.modes || OUTPUT_MODES).map((mode) =>
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
        cells.push(`<div class="settings-routing-cell"><label for="settings-routing-out-${index + 1}">Out ${index + 1}</label>`
            + `<select id="settings-routing-out-${index + 1}" class="url-input" data-routing-output="${index}" aria-label="Output ${index + 1} role"${disabled ? ' disabled' : ''}>${options}</select></div>`);
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

function renderBankSelector(select, catalog, mode) {
    const modeConfig = catalog && catalog.modes && catalog.modes[mode];
    if (!select) return null;
    if (!modeConfig) {
        select.innerHTML = '';
        return null;
    }
    const options = bankOptions(modeConfig, catalog.capabilities);
    const html = options.map((entry) =>
        `<option value="${esc(entry.id)}"${entry.id === modeConfig.selected_bank ? ' selected' : ''}>${esc(entry.label)}</option>`).join('');
    if (select.innerHTML !== html) select.innerHTML = html;
    if (select.value !== modeConfig.selected_bank) select.value = modeConfig.selected_bank;
    const bank = modeConfig.banks[modeConfig.selected_bank];
    select.closest('.effects-bank-row')?.classList.toggle('hidden', !bankSelectorVisible(modeConfig));
    return bank || null;
}

    return {
        OUTPUT_MODES,
        MUTATION_KINDS,
        esc,
        roleLabel,
        rolesForMode,
        modeLabel,
        modeSelectorVisible,
        subModeLabel,
        subwooferView,
        topologySummary,
        summedRoleIds,
        measurementArea,
        bankOptions,
        bankSelectorVisible,
        bankBinding,
        peqParams,
        compareState,
        buildMutation,
        fetchCatalog,
        applyMutation,
        renderModeSelect,
        renderRoutingGrid,
        renderBankSelector,
    };
});
