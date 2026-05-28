// Frontend tool registry — mirrors DESIGN_tool_plugin_architecture.md §3.7
// field-for-field, extended with the ModeSpec metadata defined in
// app/services/tools/spec.py so the UI never branches by tool id.

import { api } from '$lib/api/endpoints.js';

const CHDMAN_SOURCE_EXTS = ['.gdi', '.iso', '.cue', '.bin'];
const CHDMAN_VERIFY_EXTS = ['.chd'];

const DOLPHIN_SOURCE_EXTS = ['.iso', '.gcz', '.wia', '.rvz', '.wbfs'];
const DOLPHIN_VERIFY_EXTS = ['.iso', '.gcz', '.wia', '.rvz', '.wbfs'];

const Z3DS_SOURCE_EXTS = ['.cci', '.cia', '.3ds'];
const Z3DS_VERIFY_EXTS = ['.zcci', '.zcia', '.z3ds'];
const Z3DS_OUT_MAP = { '.cci': '.zcci', '.cia': '.zcia', '.3ds': '.z3ds' };

const VERIFY_URL = Object.freeze({
  chdman: Object.freeze({
    single: '/api/verify/events',
    batch: '/api/verify-batch/events',
  }),
  dolphin: Object.freeze({
    single: '/api/dolphin-verify/events',
    batch: '/api/dolphin-verify-batch/events',
  }),
  z3ds: Object.freeze({
    single: '/api/z3ds-verify/events',
    batch: '/api/z3ds-verify-batch/events',
  }),
});

/**
 * @typedef {Object} ModeEntry
 * @property {string} mode
 * @property {'create'|'extract'|'copy'|'compress'} kind
 * @property {string} label
 * @property {string} group
 * @property {string|null} outputExt
 * @property {string[]} inputExtensions
 * @property {boolean} supportsCompression
 * @property {boolean} supportsCompressionLevel
 * @property {boolean} supportsDeleteOnVerify
 * @property {boolean} allowsArchiveInput
 */

/**
 * @typedef {Object} ToolDescriptor
 * @property {string} id
 * @property {string} label
 * @property {string} hint
 * @property {string} verifyPrefix
 * @property {string[]} sourceExts
 * @property {string[]} verifyExts
 * @property {string[]} modeGroups
 * @property {ModeEntry[]} modes
 * @property {(path: string) => Promise<object>} getInfo
 * @property {(path: string, opts?: object) => Promise<object>} verify
 * @property {(paths: string[], opts?: object) => Promise<object>} verifyBatch
 * @property {(path: string) => string} productPath
 */

/** Replace the trailing extension on a path, preserving the rest. */
function swapExt(path, newExt) {
  return path.replace(/\.[^./\\]+$/, newExt);
}

/** @type {ToolDescriptor[]} */
export const TOOLS = [
  {
    id: 'chdman',
    label: 'CHDMAN',
    hint: 'Convert CD / DVD / LaserDisc images to and from CHD.',
    verifyPrefix: 'chd',
    sourceExts: CHDMAN_SOURCE_EXTS,
    verifyExts: CHDMAN_VERIFY_EXTS,
    modeGroups: ['create', 'extract', 'copy'],
    modes: [
      {
        mode: 'createraw',
        kind: 'create',
        label: 'Create Raw',
        group: 'create',
        outputExt: '.chd',
        inputExtensions: CHDMAN_SOURCE_EXTS,
        supportsCompression: true,
        supportsCompressionLevel: false,
        supportsDeleteOnVerify: true,
        allowsArchiveInput: true,
      },
      {
        mode: 'createhd',
        kind: 'create',
        label: 'Create HD',
        group: 'create',
        outputExt: '.chd',
        inputExtensions: CHDMAN_SOURCE_EXTS,
        supportsCompression: true,
        supportsCompressionLevel: false,
        supportsDeleteOnVerify: true,
        allowsArchiveInput: true,
      },
      {
        mode: 'createcd',
        kind: 'create',
        label: 'Create CD',
        group: 'create',
        outputExt: '.chd',
        inputExtensions: CHDMAN_SOURCE_EXTS,
        supportsCompression: true,
        supportsCompressionLevel: false,
        supportsDeleteOnVerify: true,
        allowsArchiveInput: true,
      },
      {
        mode: 'createdvd',
        kind: 'create',
        label: 'Create DVD',
        group: 'create',
        outputExt: '.chd',
        inputExtensions: CHDMAN_SOURCE_EXTS,
        supportsCompression: true,
        supportsCompressionLevel: false,
        supportsDeleteOnVerify: true,
        allowsArchiveInput: true,
      },
      {
        mode: 'createld',
        kind: 'create',
        label: 'Create LD',
        group: 'create',
        outputExt: '.chd',
        inputExtensions: CHDMAN_SOURCE_EXTS,
        supportsCompression: true,
        supportsCompressionLevel: false,
        supportsDeleteOnVerify: true,
        allowsArchiveInput: true,
      },
      {
        mode: 'extractraw',
        kind: 'extract',
        label: 'Extract Raw',
        group: 'extract',
        outputExt: '.raw',
        inputExtensions: ['.chd'],
        supportsCompression: false,
        supportsCompressionLevel: false,
        supportsDeleteOnVerify: false,
        allowsArchiveInput: false,
      },
      {
        mode: 'extracthd',
        kind: 'extract',
        label: 'Extract HD',
        group: 'extract',
        outputExt: '.raw',
        inputExtensions: ['.chd'],
        supportsCompression: false,
        supportsCompressionLevel: false,
        supportsDeleteOnVerify: false,
        allowsArchiveInput: false,
      },
      {
        mode: 'extractcd',
        kind: 'extract',
        label: 'Extract CD',
        group: 'extract',
        outputExt: '.cue',
        inputExtensions: ['.chd'],
        supportsCompression: false,
        supportsCompressionLevel: false,
        supportsDeleteOnVerify: false,
        allowsArchiveInput: false,
      },
      {
        mode: 'extractdvd',
        kind: 'extract',
        label: 'Extract DVD',
        group: 'extract',
        outputExt: '.iso',
        inputExtensions: ['.chd'],
        supportsCompression: false,
        supportsCompressionLevel: false,
        supportsDeleteOnVerify: false,
        allowsArchiveInput: false,
      },
      {
        mode: 'extractld',
        kind: 'extract',
        label: 'Extract LD',
        group: 'extract',
        outputExt: '.avi',
        inputExtensions: ['.chd'],
        supportsCompression: false,
        supportsCompressionLevel: false,
        supportsDeleteOnVerify: false,
        allowsArchiveInput: false,
      },
      {
        mode: 'copy',
        kind: 'copy',
        label: 'Copy / Recompress',
        group: 'copy',
        outputExt: '.chd',
        inputExtensions: ['.chd'],
        supportsCompression: true,
        supportsCompressionLevel: false,
        supportsDeleteOnVerify: true,
        allowsArchiveInput: false,
      },
    ],
    getInfo: (path) => api.getCHDInfo(path),
    verify: (path, opts) => api.verifyCHD(path, opts),
    verifyBatch: (paths, opts) => api.verifyBatchCHDs(paths, opts),
    productPath: (path) => swapExt(path, '.chd'),
  },
  {
    id: 'dolphin',
    label: 'Dolphin',
    hint: 'Compress GameCube / Wii discs to RVZ, WIA, or GCZ.',
    verifyPrefix: 'dolphin',
    sourceExts: DOLPHIN_SOURCE_EXTS,
    verifyExts: DOLPHIN_VERIFY_EXTS,
    modeGroups: ['dolphin'],
    modes: [
      {
        mode: 'dolphin_rvz',
        kind: 'compress',
        label: 'Dolphin RVZ',
        group: 'dolphin',
        outputExt: '.rvz',
        inputExtensions: DOLPHIN_SOURCE_EXTS,
        supportsCompression: true,
        supportsCompressionLevel: true,
        supportsDeleteOnVerify: true,
        allowsArchiveInput: false,
      },
      {
        mode: 'dolphin_wia',
        kind: 'compress',
        label: 'Dolphin WIA',
        group: 'dolphin',
        outputExt: '.wia',
        inputExtensions: DOLPHIN_SOURCE_EXTS,
        supportsCompression: true,
        supportsCompressionLevel: true,
        supportsDeleteOnVerify: true,
        allowsArchiveInput: false,
      },
      {
        mode: 'dolphin_gcz',
        kind: 'compress',
        label: 'Dolphin GCZ',
        group: 'dolphin',
        outputExt: '.gcz',
        inputExtensions: DOLPHIN_SOURCE_EXTS,
        supportsCompression: false,
        supportsCompressionLevel: false,
        supportsDeleteOnVerify: true,
        allowsArchiveInput: false,
      },
      {
        mode: 'dolphin_iso',
        kind: 'extract',
        label: 'Dolphin ISO',
        group: 'dolphin',
        outputExt: '.iso',
        inputExtensions: DOLPHIN_SOURCE_EXTS,
        supportsCompression: false,
        supportsCompressionLevel: false,
        supportsDeleteOnVerify: true,
        allowsArchiveInput: false,
      },
    ],
    getInfo: (path) => api.getDolphinInfo(path),
    verify: (path, opts) => api.verifyDolphin(path, opts),
    verifyBatch: (paths, opts) => api.verifyBatchDolphin(paths, opts),
    productPath: (path) => swapExt(path, '.rvz'),
  },
  {
    id: 'z3ds',
    label: '3DS',
    hint: 'Compress Nintendo 3DS ROMs (.3ds, .cci, .cia).',
    verifyPrefix: 'z3ds',
    sourceExts: Z3DS_SOURCE_EXTS,
    verifyExts: Z3DS_VERIFY_EXTS,
    modeGroups: ['z3ds'],
    modes: [
      {
        mode: 'z3ds_compress',
        kind: 'compress',
        label: 'Compress 3DS',
        group: 'z3ds',
        outputExt: null,
        inputExtensions: Z3DS_SOURCE_EXTS,
        supportsCompression: false,
        supportsCompressionLevel: false,
        supportsDeleteOnVerify: true,
        allowsArchiveInput: false,
      },
    ],
    getInfo: (path) => api.getZ3DSInfo(path),
    verify: (path, opts) => api.verify3DS(path, opts),
    verifyBatch: (paths, opts) => api.verifyBatchZ3DS(paths, opts),
    productPath: (path) => {
      const m = /\.(3ds|cci|cia)$/i.exec(path);
      if (!m) return path;
      const ext = `.${m[1].toLowerCase()}`;
      return swapExt(path, Z3DS_OUT_MAP[ext] ?? ext);
    },
  },
];

const byId = new Map(TOOLS.map((t) => [t.id, t]));
const byMode = new Map(
  TOOLS.flatMap((t) => t.modes.map((m) => [m.mode, { tool: t, spec: m }])),
);

function endsWithAny(path, exts) {
  if (!path) return false;
  const lower = path.toLowerCase();
  return exts.some((ext) => lower.endsWith(ext));
}

export const registry = {
  /** All registered tools, in declared order. */
  all: () => TOOLS,

  /** Lookup by tool id (`'chdman'`, `'dolphin'`, `'z3ds'`). */
  forTool: (id) => byId.get(id),

  /** Mode metadata (ModeEntry) for a wire-mode value. */
  specFor: (mode) => byMode.get(mode)?.spec,

  /** The owning tool for a wire-mode value. */
  toolForMode: (mode) => byMode.get(mode)?.tool,

  /** Pick the tool whose verify_extensions match a given file path, or null. */
  toolForVerifyPath: (path) => TOOLS.find((t) => endsWithAny(path, t.verifyExts)) ?? null,

  /** Tools whose source extensions match the given path (convertible sources). */
  toolsForSourcePath: (path) => TOOLS.filter((t) => endsWithAny(path, t.sourceExts)),

  /** Build a verify URL — single or batch — for a tool id. */
  verifyUrl: (toolId, kind) => VERIFY_URL[toolId]?.[kind] ?? null,

  /** Group a tool's modes by `group` for menu rendering. */
  modesByGroup: (toolId) => {
    const tool = byId.get(toolId);
    if (!tool) return new Map();
    const out = new Map();
    for (const mode of tool.modes) {
      const list = out.get(mode.group) ?? [];
      list.push(mode);
      out.set(mode.group, list);
    }
    return out;
  },

  /** Mode-kind → human label for tab/section headings. */
  groupLabel: (group) => {
    switch (group) {
      case 'create':
        return 'Create';
      case 'extract':
        return 'Extract';
      case 'copy':
        return 'Copy';
      case 'dolphin':
        return 'Dolphin';
      case 'z3ds':
        return '3DS';
      default:
        return group;
    }
  },
};
