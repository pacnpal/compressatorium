// Tool descriptor table for the frontend.
//
// app.js / api.js used to branch on per-tool identity (chdman / dolphin /
// z3ds) at ~10 sites — primary-tool label/hint/filters, MODE_GROUPS, the
// CHDInfoModal info-method routing, FileList badges + selectability, the
// resolveSourceProduct preferredKinds ladder (duplicated in DeleteModal +
// BulkDeleteModal), and the verify-batch dispatch. Phase 8 lifts those
// branches into this descriptor + a small set of helpers.
//
// The helpers also drive the new (Phase 7) FileEntry.outputs /
// FileEntry.convertible_by fields the backend now ships alongside the
// legacy per-tool booleans, so app.js can drop the booleans entirely.

import { api } from './api.js';

const COMMON_FILTERS = [
    { value: '', label: 'All Types' },
    { value: '.zip,.7z,.rar', label: 'Archives' },
    { value: '.chd', label: 'CHD Files' }
];
const CUSTOM_FILTER = { value: 'custom', label: 'Custom...' };

const Z3DS_OUTPUT_EXTENSION_BY_SOURCE = {
    '.3ds': '.z3ds',
    '.cci': '.zcci',
    '.cia': '.zcia'
};

function _ext(path) {
    if (typeof path !== 'string') return '';
    const lower = path.toLowerCase();
    const lastSlash = Math.max(lower.lastIndexOf('/'), lower.lastIndexOf('\\'));
    const filename = lastSlash >= 0 ? lower.slice(lastSlash + 1) : lower;
    const dot = filename.lastIndexOf('.');
    if (dot <= 0) return '';
    return filename.slice(dot);
}

// --- outputs / convertible_by helpers -------------------------------------

export function outputFor(entry, toolId) {
    if (!entry || !Array.isArray(entry.outputs)) return null;
    return entry.outputs.find(o => o?.tool_id === toolId) || null;
}

export function entryOutputExists(entry, toolId) {
    return outputFor(entry, toolId) !== null;
}

export function entryOutputReady(entry, toolId) {
    return outputFor(entry, toolId)?.exists === true;
}

export function entryOutputPath(entry, toolId) {
    return outputFor(entry, toolId)?.path || null;
}

export function entryConvertibleBy(entry, toolId) {
    return Array.isArray(entry?.convertible_by) && entry.convertible_by.includes(toolId);
}

// /api/files/archive (and the archives[] members in /api/files/search)
// keep the legacy CHD-only dict shape — they don't carry outputs /
// convertible_by. Convert that shape to the unified one when synthesizing
// archive-member entries on the FE.
export function synthesizeArchiveOutputs(file) {
    const convertible_by = file?.convertible ? ['chdman'] : [];
    const outputs = [];
    if (file?.has_chd && file?.chd_path) {
        const ready = Boolean(file.chd_ready);
        outputs.push({
            tool_id: 'chdman',
            exists: ready,
            ready,
            path: file.chd_path
        });
    }
    return { convertible_by, outputs };
}

function get3dsProductFromPath(path) {
    const ext = _ext(path);
    const outExt = Z3DS_OUTPUT_EXTENSION_BY_SOURCE[ext];
    if (!outExt || typeof path !== 'string' || !path.toLowerCase().endsWith(ext)) {
        return null;
    }
    return `${path.slice(0, -ext.length)}${outExt}`;
}

// --- TOOLS descriptor table -----------------------------------------------
//
// Each entry pairs a backend tool id with the FE-side bits app.js used to
// inline. Source/verify/info extension lists mirror the backend tool's
// input_extensions / verify_extensions / info-route accept-set. productPath
// consults entry.outputs first and falls back to deriving from the source
// path so callers that synthesize entries without outputs[] still work.

export const TOOLS = [
    {
        id: 'chdman',
        verifyKind: 'chd',
        label: 'CHDMAN',
        sourceExts: ['.gdi', '.iso', '.cue', '.bin'],
        verifyExts: ['.chd'],
        infoExts: ['.chd'],
        hint: ({ html }) => html`Convert disc images to CHD format • Supports CD/DVD/LaserDisc`,
        filters: [
            ...COMMON_FILTERS,
            { value: '.cue,.bin,.gdi,.iso', label: 'Disc Images' },
            { value: '.iso', label: 'ISO Files' },
            CUSTOM_FILTER
        ],
        modeGroups: [
            { id: 'create', label: 'Create CHD', options: [
                { value: 'createcd', label: 'Create CD CHD (Dreamcast, PS1, Sega CD)' },
                { value: 'createdvd', label: 'Create DVD CHD (PSP, PS2)' },
                { value: 'createraw', label: 'Create Raw CHD' },
                { value: 'createhd', label: 'Create HD CHD' },
                { value: 'createld', label: 'Create LaserDisc CHD' }
            ]},
            { id: 'extract', label: 'Extract from CHD', options: [
                { value: 'extractcd', label: 'Extract CD (cue/bin)' },
                { value: 'extractdvd', label: 'Extract DVD (iso)' },
                { value: 'extractraw', label: 'Extract Raw' },
                { value: 'extracthd', label: 'Extract HD' },
                { value: 'extractld', label: 'Extract LaserDisc (avi)' }
            ]},
            { id: 'copy', label: 'Copy / Recompress', options: [
                { value: 'copy', label: 'Copy / Recompress CHD' }
            ]}
        ],
        getInfo: (path) => api.getCHDInfo(path),
        verify: (path, opts) => api.verifyCHD(path, opts),
        verifyBatch: (paths, opts) => api.verifyBatchCHDs(paths, opts),
        productPath: (entry) => {
            const out = outputFor(entry, 'chdman');
            if (!out) return null;
            if (out.path) return out.path;
            if (typeof entry?.path !== 'string') return null;
            return entry.path.replace(/\.[^.]+$/, '.chd');
        },
        badges: {
            exists: { label: 'CHD exists', title: 'A CHD file already exists for this source' },
            convertible: { label: 'Convertible', title: 'Can be converted to CHD' }
        }
    },
    {
        id: 'dolphin',
        verifyKind: 'dolphin',
        label: 'Dolphin',
        sourceExts: ['.iso', '.gcz', '.wia', '.rvz', '.wbfs'],
        verifyExts: ['.rvz', '.wia', '.gcz', '.wbfs', '.iso'],
        infoExts: ['.rvz', '.wia', '.gcz', '.wbfs'],
        hint: ({ html }) => html`Convert GameCube/Wii disc images • Supports RVZ, WIA, GCZ, ISO formats`,
        filters: [
            ...COMMON_FILTERS,
            { value: '.iso,.rvz,.wia,.gcz,.wbfs', label: 'GameCube/Wii Images' },
            { value: '.iso', label: 'ISO Files' },
            CUSTOM_FILTER
        ],
        modeGroups: [
            { id: 'dolphin', label: 'Dolphin (GameCube/Wii)', options: [
                { value: 'dolphin_rvz', label: 'Convert to RVZ' },
                { value: 'dolphin_wia', label: 'Convert to WIA' },
                { value: 'dolphin_gcz', label: 'Convert to GCZ' },
                { value: 'dolphin_iso', label: 'Convert to ISO (extract)' }
            ]}
        ],
        getInfo: (path) => api.getDolphinInfo(path),
        verify: (path, opts) => api.verifyDolphin(path, opts),
        verifyBatch: (paths, opts) => api.verifyBatchDolphin(paths, opts),
        productPath: (entry) => {
            const out = outputFor(entry, 'dolphin');
            if (!out || typeof entry?.path !== 'string') return null;
            if (out.path) return out.path;
            const ext = _ext(entry.path);
            if (!ext || !entry.path.toLowerCase().endsWith(ext)) return null;
            if (ext === '.iso') return `${entry.path.slice(0, -ext.length)}.rvz`;
            return entry.path;
        },
        badges: {}
    },
    {
        id: 'z3ds',
        verifyKind: 'z3ds',
        label: '3DS',
        sourceExts: ['.3ds', '.cci', '.cia'],
        verifyExts: ['.z3ds', '.zcci', '.zcia'],
        infoExts: ['.3ds', '.cci', '.cia', '.z3ds', '.zcci', '.zcia'],
        hint: ({ html }) => html`Compress Nintendo 3DS ROMs • Supports .cci/.cia/.3ds (cart & CIA) → .zcci/.zcia/.z3ds • ~50% size reduction • <strong>Decrypted ROMs only</strong>`,
        filters: [
            ...COMMON_FILTERS,
            { value: '.cci,.cia,.3ds', label: '3DS ROMs' },
            CUSTOM_FILTER
        ],
        modeGroups: [
            { id: 'z3ds', label: 'Nintendo 3DS', options: [
                { value: 'z3ds_compress', label: 'Compress to ZCCI/ZCIA/Z3DS' }
            ]}
        ],
        getInfo: (path) => api.getZ3DSInfo(path),
        verify: (path, opts) => api.verify3DS(path, opts),
        verifyBatch: (paths, opts) => api.verifyBatchZ3DS(paths, opts),
        productPath: (entry) => {
            const out = outputFor(entry, 'z3ds');
            if (!out) return null;
            if (out.path) return out.path;
            return get3dsProductFromPath(entry?.path);
        },
        badges: {
            exists: { label: 'Z3DS exists', title: 'A compressed 3DS file (.zcci/.zcia/.z3ds) already exists' },
            convertible: { label: '3DS ROM', title: 'Nintendo 3DS ROM - Can be compressed to ZCCI/ZCIA/Z3DS format' }
        }
    }
];

const TOOLS_BY_ID = Object.fromEntries(TOOLS.map(t => [t.id, t]));
const TOOLS_BY_VERIFY_KIND = Object.fromEntries(TOOLS.map(t => [t.verifyKind, t]));

export function getTool(id) {
    return TOOLS_BY_ID[id] || null;
}

export function getToolByVerifyKind(kind) {
    return TOOLS_BY_VERIFY_KIND[kind] || null;
}

export function getToolByMode(mode) {
    for (const tool of TOOLS) {
        for (const group of tool.modeGroups) {
            if (group.options.some(opt => opt.value === mode)) return tool;
        }
    }
    return null;
}

export function getToolByVerifyExt(path) {
    const ext = _ext(path);
    if (!ext) return null;
    return TOOLS.find(t => t.verifyExts.includes(ext)) || null;
}

// Flat MODE_GROUPS, derived from each tool's modeGroups in TOOLS order
// (chdman → dolphin → z3ds), so the rendered group order matches the
// pre-migration literal in app.js.
export const MODE_GROUPS = TOOLS.flatMap(t => t.modeGroups);

// Tool ordering for preferredKinds resolution. When picking which output
// to surface for a source entry (DeleteModal / BulkDeleteModal), prefer
// the tool that matches the current primary-tool ("isoHandling") setting,
// then fall through to the others.
export const TOOL_ORDER_BY_ISO_HANDLING = {
    dolphin: ['dolphin', 'chdman', 'z3ds'],
    z3ds: ['z3ds', 'chdman', 'dolphin'],
    chdman: ['chdman', 'dolphin', 'z3ds']
};

export function resolveSourceProduct(entry, isoHandling) {
    if (!entry?.path) return null;
    const order = TOOL_ORDER_BY_ISO_HANDLING[isoHandling] || TOOL_ORDER_BY_ISO_HANDLING.chdman;
    for (const toolId of order) {
        const tool = getTool(toolId);
        if (!tool) continue;
        const path = tool.productPath(entry);
        if (path) return { path, kind: tool.verifyKind, toolId };
    }
    return null;
}
