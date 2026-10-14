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

const OUTPUT_MODES = ['stereo', 'stereo-sub', 'surround'];
const MUTATION_KINDS = ['set_routing', 'switch_mode', 'select_bank', 'set_bank_preset',
    'switch_all_banks', 'set_crossover', 'set_subwoofers', 'set_processing', 'set_level_calibration',
    'set_extras', 'set_av_sync'];
const FAMILY_LABELS = { 'linkwitz-riley': 'Linkwitz-Riley', butterworth: 'Butterworth', bessel: 'Bessel' };
const FAMILY_PREFIXES = { 'linkwitz-riley': 'LR', butterworth: 'BW', bessel: 'BS' };
const SUB_SIDES = ['left', 'right'];
// Surround bass-management speaker groups, in display order.
const SURROUND_GROUPS = ['front', 'center', 'side', 'rear'];
const SURROUND_GROUP_LABELS = { front: 'Front L/R', center: 'Center', side: 'Side L/R', rear: 'Rear L/R' };

function filterLabel(family, slope) {
    return `${FAMILY_PREFIXES[family] || String(family || '')}${slope}`;
}

function normalizedCrossover(definition, fallback) {
    const source = definition || {};
    const family = FAMILY_LABELS[source.family] ? source.family : (fallback?.family || 'linkwitz-riley');
    return {
        family,
        slope_db_oct: Number(source.slope_db_oct) || fallback?.slope_db_oct || 24,
        frequency_hz: Number(source.frequency_hz) || fallback?.frequency_hz || 80,
    };
}

// Effective sub crossover of one side. Mono, Dual-Mono and a coupled Stereo
// pair run the shared filter; only an unlinked Stereo pair resolves per side.
function bassCrossoverForSide(bass, side, stereo) {
    const source = bass || {};
    const shared = normalizedCrossover(null, {
        family: source.family, slope_db_oct: source.slope_db_oct, frequency_hz: source.frequency_hz });
    if (!stereo || source.sub_link !== false || !SUB_SIDES.includes(side)) return shared;
    const override = (source.sub_filters || {})[side];
    return override ? normalizedCrossover(override, shared) : shared;
}

// Configured Surround speaker groups; Front covers Main L/R and crossover ways.
function surroundGroups(topology) {
    const roles = Array.isArray(topology?.roles) ? topology.roles : [];
    const present = {
        front: roles.some(role => /^(main_[lr]|left_|right_)/.test(role)),
        center: roles.includes('center'),
        side: roles.includes('side_l') || roles.includes('side_r'),
        rear: roles.includes('rear_l') || roles.includes('rear_r'),
    };
    return SURROUND_GROUPS.filter(group => present[group]);
}

// Effective crossover and high-pass switch of one Surround speaker group:
// linked groups share the mode crossover and Main high-pass switch.
function bassCrossoverForGroup(bass, group) {
    const source = bass || {};
    const shared = normalizedCrossover(null, {
        family: source.family, slope_db_oct: source.slope_db_oct, frequency_hz: source.frequency_hz });
    const sharedHighpass = source.main_highpass_enabled !== false;
    const override = source.sub_link === false ? (source.sub_filters || {})[group] : null;
    if (!override) return { ...shared, highpass_enabled: sharedHighpass };
    return { ...normalizedCrossover(override, shared), highpass_enabled: override.highpass_enabled !== false };
}

function esc(value) {
    return String(value ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;')
        .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

function roleLabel(roleId, mode) {
    if (mode === 'surround' && roleId === 'main') return 'Front L/R';
    if (mode === 'surround' && roleId === 'main_l') return 'Front L';
    if (mode === 'surround' && roleId === 'main_r') return 'Front R';
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

// Output Routing assigns roles to hardware outputs. With only two outputs
// the mapping is fixed (Out 1 = Main L, Out 2 = Main R), so the whole
// section stays hidden; it is only meaningful with more than two outputs.
function routingVisible(channelCount) {
    return Number(channelCount) > 2;
}

// Status line under the Mode selector. Stereo + Sub without a routed sub
// role plays mains only, so the line points to the routing instead.
function modeHint(mode, topology, channelCount) {
    const subs = Array.isArray(topology?.sub_roles) ? topology.sub_roles : [];
    if (mode === 'stereo-sub' && !subs.length) {
        return 'No subwoofer configured — assign a Sub output below.';
    }
    if (mode === 'surround') {
        return `Surround · ${Number(channelCount) || 0} hardware outputs · LFE → ${subs.length ? 'Sub 1' + (subs.includes('sub2') ? ' / Sub 2' : '') : 'Front L/R'}`;
    }
    return `${modeLabel(mode)} · ${Number(channelCount) || 0} hardware outputs`;
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
    const stereo = topology.sub_mode === 'stereo';
    const surround = catalog?.active_mode === 'surround';
    const shared = bassCrossoverForSide(bass, 'left', false);
    const left = bassCrossoverForSide(bass, 'left', stereo);
    const right = bassCrossoverForSide(bass, 'right', stereo);
    return {
        mode: { mono: 'subwoofer-2.1', 'dual-mono': 'subwoofer-2.2', stereo: 'subwoofer-2.2-stereo' }[topology.sub_mode] || 'stereo',
        roles, sub_mode: topology.sub_mode || 'none',
        subwoofer: { crossover_frequency_hz: shared.frequency_hz,
            slope: filterLabel(shared.family, shared.slope_db_oct),
            family: shared.family, slope_db_oct: shared.slope_db_oct,
            main_highpass_enabled: bass.main_highpass_enabled ?? true,
            sub_level_db: first.level_db || 0, sub_alignment_ms: first.alignment_ms || 0,
            sub_polarity: first.polarity || 'normal' },
        subwoofers: { sub1: first, sub2: processing[roles[1]] || {} },
        // Shared and per-side sub crossover; the link switch is only offered
        // for a true Stereo sub pair.
        crossover: { ...shared, label: filterLabel(shared.family, shared.slope_db_oct),
            link: bass.sub_link !== false, stereo, surround,
            left, right, left_label: filterLabel(left.family, left.slope_db_oct),
            right_label: filterLabel(right.family, right.slope_db_oct),
            // Surround reuses the link/tab scheme for its speaker groups.
            groups: surround ? surroundGroups(topology).map(id => {
                const shape = bassCrossoverForGroup(bass, id);
                return { id, name: SURROUND_GROUP_LABELS[id], ...shape,
                    label: filterLabel(shape.family, shape.slope_db_oct) };
            }) : [] },
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
        bits.push(topo.mode === 'surround' ? 'Surround' : 'Stereo');
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

// Same rule as the backend (measurement/speaker_scope.plan_area): sub banks
// are owned by Auto Sub Optimize, not by per-speaker calibration runs.
function isSubwooferBank(bankId) {
    return String(bankId || '').startsWith('sub');
}

// Surround Global is measured and filtered per speaker group together with the
// subs (Front L/R + Sub, Center + Sub, ...); a group needs a routed speaker.
function globalGroupOptions(modeConfig) {
    if (modeConfig?.topology?.mode !== 'surround') return [];
    const subs = (modeConfig.topology.sub_roles || []).length > 0;
    return surroundGroups(modeConfig.topology).map(id => ({
        id, name: SURROUND_GROUP_LABELS[id], short: SURROUND_GROUP_LABELS[id].replace(/ L\/R$/, ''),
        label: SURROUND_GROUP_LABELS[id] + (subs ? ' + Sub' : ''),
        channel_mode: id === 'center' ? 'mono' : 'stereo' }));
}

function resolveGlobalGroup(modeConfig, group) {
    const options = globalGroupOptions(modeConfig);
    return options.find(option => option.id === group) || options[0] || null;
}

// Global and All Banks use the same routed speaker-group selection.
// Null in every concrete bank and other output mode.
function globalGroupChoice(catalog, group) {
    const mode = catalog && catalog.active_mode;
    const modeConfig = mode && catalog.modes && catalog.modes[mode];
    const selected = String(modeConfig?.selected_bank || 'global');
    if (mode !== 'surround' || !['global', 'all'].includes(selected)) return null;
    const options = globalGroupOptions(modeConfig);
    return options.length ? { options, selected: resolveGlobalGroup(modeConfig, group).id } : null;
}

// Side chips of a group area name the speaker they sweep: "Rear L", "Rear L+R".
function groupSideLabels(groupOption, sides) {
    const suffix = { left: 'L', stereo: 'L+R', right: 'R' };
    return Object.fromEntries(sides.map(side => [side, `${groupOption.short} ${suffix[side]}`]));
}

function measurementArea(catalog, bankId, group) {
    const mode = (catalog && catalog.active_mode) || 'stereo';
    const modeConfig = (catalog && catalog.modes && catalog.modes[mode]) || {};
    const selected = String(bankId || modeConfig.selected_bank || 'global');
    const bank = modeConfig.banks?.[selected];
    const groupedBank = mode === 'surround' && ['global', 'all'].includes(selected);
    const groupOption = groupedBank ? resolveGlobalGroup(modeConfig, group) : null;
    // All Banks measures the system or a group as currently listened (every bank on
    // its A/B side). It owns no bank, so a result can be saved but not imported.
    const allBanks = selected === 'all';
    const routed = (modeConfig.topology?.roles || []).length > 0;
    const listened = allBanks ? modeConfig.all_banks?.active_side : null;
    const aggregateLabel = allBanks ? `All Banks${listened ? ` · ${listened}` : ''}` : 'Global';
    const label = groupOption ? `${aggregateLabel} · ${groupOption.label}`
        : allBanks ? aggregateLabel : bank?.label || roleLabel(selected);
    const available = !(allBanks && !routed) && !(groupedBank && !groupOption);
    const channel_mode = groupOption ? groupOption.channel_mode : bank?.channel_mode || 'stereo';
    // A stereo area (an L/R pair bank or Global) can be swept per side or
    // together; a mono area has no sides and always sweeps both inputs at
    // once (a summed mono target needs both to reach operating level).
    const sides = available && channel_mode === 'stereo' ? ['left', 'stereo', 'right'] : [];
    const repeat_supported = available && channel_mode === 'stereo' && !isSubwooferBank(selected);
    const implicitStereo = selected === 'global' && !bankSelectorVisible(modeConfig);
    const polarity_supported = available && !isSubwooferBank(selected)
        && selected !== 'all' && (selected !== 'global' || implicitStereo) && !groupOption;
    const repeat_note = !available ? 'Select a filter bank for measurement.'
        : repeat_supported ? '' : channel_mode === 'stereo'
            ? `${label}: use a single sweep or Auto Sub Optimize.` : `${label} is a mono target. Use a single sweep.`;
    const groupNote = groupOption
        ? 'Only selected speakers and subs play.'
        : allBanks ? 'Whole system as listened (every bank on its A/B side); not importable.' : '';
    return {
        bank_id: selected,
        label,
        channel: 'stereo',
        channel_mode,
        available,
        sides,
        note: !available ? repeat_note : groupNote || (selected === 'global'
            ? 'Whole system; all area banks active.'
            : `Only ${label} stays audible; every other output is muted for this sweep.`),
        repeat_supported,
        repeat_note,
        polarity_supported,
        ...(groupOption ? { group: groupOption.id } : {}),
        ...(groupOption && sides.length ? { side_labels: groupSideLabels(groupOption, sides) } : {}),
        ...(allBanks ? { importable: false } : {}),
    };
}

function bankOptions(modeConfig) {
    const banks = modeConfig?.banks || {};
    return [{ id: 'global', label: 'Global' }, { id: 'all', label: 'All Banks' },
        ...Object.entries(banks).filter(([id]) => id !== 'global').map(([id, bank]) => ({ id, label: bank.label || roleLabel(id) }))];
}

function bankSelectorVisible(modeConfig) {
    const roles = modeConfig?.topology?.roles || [];
    if (modeConfig?.topology?.mode === 'surround') return roles.length > 0;
    return roles.length > 0 && !(roles.length === 2 && roles.includes('main_l') && roles.includes('main_r'));
}

function bankBinding(catalog, group) {
    if (!catalog) return null;
    const modeConfig = catalog.modes?.[catalog.active_mode];
    const bankId = modeConfig?.selected_bank || 'global';
    if (bankId === 'all') throw new Error('Select a filter bank for import or measurement.');
    const groupOption = catalog.active_mode === 'surround' && bankId === 'global'
        ? resolveGlobalGroup(modeConfig, group) : null;
    return { bank_mode: catalog.active_mode, bank_id: bankId, expected_revision: catalog.revision,
        ...(groupOption ? { bank_group: groupOption.id } : {}) };
}

// Bank and output mode whose preset library a combine, delete or download addresses.
function bankOwner(catalog) {
    if (!catalog) return null;
    const bankId = catalog.modes?.[catalog.active_mode]?.selected_bank || 'global';
    if (bankId === 'all') throw new Error('All Banks owns no presets; select a concrete filterbank.');
    return { bank_mode: catalog.active_mode, bank_id: bankId };
}

const BUILTIN_BANK_PRESETS = ['Direct', 'Neutral'];

function isBuiltinBankPreset(name) {
    return BUILTIN_BANK_PRESETS.includes(String(name || ''));
}

// Every bank lists its own presets, Global one set per output mode; a preset
// name is unique within its bank only. The built-ins belong to every bank.
function presetsForBank(presets, bankId, mode) {
    if (!Array.isArray(presets)) return [];
    // All Banks owns no presets: it only switches A/B jointly.
    if (!bankId || bankId === 'all') return [];
    return presets.filter((entry) => {
        if (isBuiltinBankPreset(entry?.name)) return true;
        return entry?.bank === bankId && (bankId !== 'global' || entry.mode === mode);
    });
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

// Long preset names keep their start and their last word: generated names
// tell the filter kind first and differ at the end.
function compactPresetName(name, maxChars = 20) {
    const clean = String(name || '').trim();
    if (clean.length <= maxChars) return clean;
    const words = clean.split(/\s+/);
    const tail = words[words.length - 1];
    let head = '';
    for (const word of words.slice(0, -1)) {
        const next = head ? `${head} ${word}` : word;
        if (next.length + 1 + tail.length > maxChars) break;
        head = next;
    }
    if (head) return `${head}…${tail}`;
    return `${clean.slice(0, Math.max(1, maxChars - 1)).trimEnd()}…`;
}

// Numbered banks in a row with the same merge key share one entry ("Sub 1/2");
// a null key never merges.
function mergeNumberedBanks(items) {
    const entries = [];
    for (const item of items) {
        const numbered = /^(.*\D)(\d+)$/.exec(item.label);
        const previous = entries[entries.length - 1];
        if (numbered && item.key !== null && previous && previous.family === numbered[1] && previous.key === item.key) {
            previous.ids.push(...item.ids);
            previous.label = `${previous.label}/${numbered[2]}`;
            continue;
        }
        entries.push({ ...item, ids: [...item.ids], family: numbered ? numbered[1] : null });
    }
    return entries.map(({ family, key, ...entry }) => entry);
}

// Active preset of every bank in the active mode's chain, Global first.
// A bank whose channels listen to different presets has no single preset.
function activePresetOverview(catalog) {
    const mode = catalog?.active_mode;
    const banks = catalog?.modes?.[mode]?.banks;
    if (!banks) return [];
    const ids = Object.keys(banks).sort((a, b) => (b === 'global') - (a === 'global'));
    return mergeNumberedBanks(ids.map((id) => {
        const bank = banks[id] || {};
        const preset = typeof bank.preset === 'string' && bank.preset ? bank.preset : null;
        return { ids: [id], label: bank.label || roleLabel(id, mode), preset, key: preset };
    }));
}

// A and B slot of every area bank that All Banks switches jointly; Global is
// not switched. A bank without a B stays on A (canB false); a null slot with
// canB means per-channel presets. Banks merge only on equal slots and side.
function allBanksCompareOverview(catalog) {
    const mode = catalog?.active_mode;
    const banks = catalog?.modes?.[mode]?.banks;
    if (!banks) return [];
    return mergeNumberedBanks(Object.keys(banks).filter((id) => id !== 'global').map((id) => {
        const bank = banks[id] || {};
        const presetA = typeof bank.preset_a === 'string' && bank.preset_a ? bank.preset_a : null;
        const presetB = typeof bank.preset_b === 'string' && bank.preset_b ? bank.preset_b : null;
        const canB = !!(bank.can_b ?? presetB);
        const side = bank.active_side === 'A' || bank.active_side === 'B' ? bank.active_side : null;
        const singleSlots = !!presetA && (!!presetB || !canB);
        return { ids: [id], label: bank.label || roleLabel(id, mode), presetA, presetB, canB, side,
            key: singleSlots ? JSON.stringify([presetA, presetB, side]) : null };
    }));
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
    const measured = mutation.kind === 'set_level_calibration';
    if (measured && (!Number.isInteger(mutation.state_revision) || mutation.state_revision < 0)) {
        throw new Error('Level Matching requires its original revision; measure again');
    }
    try {
        return await attempt(measured ? mutation.state_revision : catalog.revision);
    } catch (error) {
        if (!measured && error && error.status === 409 && error.catalog) {
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
            `<option value="${esc(role)}"${role === current ? ' selected' : ''}>${esc(roleLabel(role, mode))}</option>`).join('');
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
        routingVisible,
        modeHint,
        subModeLabel,
        FAMILY_LABELS,
        SUB_SIDES,
        SURROUND_GROUPS,
        SURROUND_GROUP_LABELS,
        filterLabel,
        bassCrossoverForSide,
        surroundGroups,
        bassCrossoverForGroup,
        subwooferView,
        topologySummary,
        summedRoleIds,
        isSubwooferBank,
        globalGroupOptions,
        resolveGlobalGroup,
        globalGroupChoice,
        measurementArea,
        bankOptions,
        bankSelectorVisible,
        bankBinding,
        bankOwner,
        isBuiltinBankPreset,
        presetsForBank,
        peqParams,
        compareState,
        compactPresetName,
        activePresetOverview,
        allBanksCompareOverview,
        buildMutation,
        fetchCatalog,
        applyMutation,
        renderModeSelect,
        renderRoutingGrid,
        renderBankSelector,
    };
});
