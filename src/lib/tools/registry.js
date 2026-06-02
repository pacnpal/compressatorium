// Frontend tool registry, mirrors docs/DESIGN_tool_plugin_architecture.md §3.7
// field-for-field, extended with the ModeSpec metadata defined in
// app/services/tools/spec.py.
//
// The registry is the single source of truth for tool identity. All
// call sites (UI store, sidebar, SSE URL building, group labels, mode
// metadata) look up via the helpers below, there is no hardcoded
// `if (tool === 'chdman')` branching, and no map at the top of this
// file that needs editing when adding a 4th tool. To add a tool:
// declare one entry in TOOLS, register its plugin on the backend,
// add the binary to the Dockerfile. That's it.

import { api } from '$lib/api/endpoints.js';

const CHDMAN_SOURCE_EXTS = ['.gdi', '.iso', '.cue', '.bin'];
const CHDMAN_VERIFY_EXTS = ['.chd'];

const DOLPHIN_SOURCE_EXTS = ['.iso', '.gcz', '.wia', '.rvz', '.wbfs'];
// `.iso` is intentionally absent from DOLPHIN_VERIFY_EXTS even
// though dolphin-tool itself can verify GC/Wii ISOs. Most ISOs in
// any given directory are CHDMAN CD/DVD sources, and
// `toolForVerifyPath()` returns the first tool whose verify list
// matches, adding `.iso` here would route every CD ISO row's
// Info/Verify action into /api/dolphin-verify (which fails for
// non-Dolphin ISOs and confuses users). Users who want to verify a
// GC/Wii ISO have two paths today: compress to `.rvz` first, then
// verify the `.rvz`; or use RowActionsMenu's verify-from-output
// flow, which targets any Dolphin output already declared in
// `entry.outputs` (added in round 16). A future per-tool picker
// in RowActionsMenu would let the user disambiguate directly.
const DOLPHIN_VERIFY_EXTS = ['.gcz', '.wia', '.rvz', '.wbfs'];

const Z3DS_SOURCE_EXTS = ['.cci', '.cia', '.3ds'];
const Z3DS_VERIFY_EXTS = ['.zcci', '.zcia', '.z3ds'];
const Z3DS_OUT_MAP = { '.cci': '.zcci', '.cia': '.zcia', '.3ds': '.z3ds' };

// Switch (nsz). Both directions are "sources" depending on the chosen mode.
const NSZ_COMPRESS_EXTS = ['.nsp', '.xci'];
const NSZ_VERIFY_EXTS = ['.nsz', '.xcz'];
const NSZ_SOURCE_EXTS = [...NSZ_COMPRESS_EXTS, ...NSZ_VERIFY_EXTS];
const NSZ_OUT_MAP = {
  '.nsp': '.nsz', '.xci': '.xcz', '.nsz': '.nsp', '.xcz': '.xci',
};

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
 * @property {string} verifyPrefix              URL segment for /api/{prefix}-verify (or empty for /api/verify)
 * @property {string[]} sourceExts
 * @property {string[]} verifyExts
 * @property {string[]} modeGroups
 * @property {Record<string,string>} groups     group id → human label
 * @property {string} defaultMode               wire-mode the workspace selects when this tool is activated
 * @property {string} [glyph]                   short text affordance for the sidebar / dashboard (1-2 chars)
 * @property {string} [accent]                  CSS color or token for badges / chips (falls back to --accent)
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
    // chdman is the original endpoint set, so it has no URL prefix:
    // /api/verify, /api/verify-batch. Other tools use /api/<prefix>-verify.
    verifyPrefix: '',
    sourceExts: CHDMAN_SOURCE_EXTS,
    verifyExts: CHDMAN_VERIFY_EXTS,
    modeGroups: ['create', 'extract', 'copy'],
    groups: { create: 'Create', extract: 'Extract', copy: 'Copy' },
    defaultMode: 'createcd',
    glyph: 'CD',
    accent: 'var(--badge-cd)',
    // Compression options. chdman accepts a comma-separated codec list;
    // CompressionPicker offers the union of these as toggleable chips.
    compressionCodecs: [
      // Full codec catalog from the README + legacy UI. Backend
      // forwards arbitrary tokens to `chdman -c`, so adding entries
      // here only widens what the user can pick; chdman itself
      // ignores incompatible combinations for the active mode.
      { value: 'zlib',  label: 'zlib',  hint: 'Fast, baseline compatibility' },
      { value: 'zstd',  label: 'zstd',  hint: 'Modern general-purpose codec' },
      { value: 'lzma',  label: 'lzma',  hint: 'Slower, better ratio' },
      { value: 'lzma2', label: 'lzma2', hint: 'Best ratio, slowest' },
      { value: 'huff',  label: 'huff',  hint: 'Huffman entropy coder' },
      { value: 'flac',  label: 'flac',  hint: 'For CD audio tracks' },
      { value: 'cdzl',  label: 'cdzl',  hint: 'CD-zlib variant' },
      { value: 'cdzs',  label: 'cdzs',  hint: 'CD-zstd variant' },
      { value: 'cdlz',  label: 'cdlz',  hint: 'CD-lzma variant' },
      { value: 'cdfl',  label: 'cdfl',  hint: 'CD-FLAC variant' },
      { value: 'avhu',  label: 'avhu',  hint: 'Audio/video Huffman' },
    ],
    compressionStyle: 'multi',  // multi-codec list joined with commas
    modes: [
      { mode: 'createraw', kind: 'create',  label: 'Create Raw', group: 'create',
        outputExt: '.chd', inputExtensions: CHDMAN_SOURCE_EXTS,
        supportsCompression: true,  supportsCompressionLevel: false,
        supportsDeleteOnVerify: true, allowsArchiveInput: true },
      { mode: 'createhd',  kind: 'create',  label: 'Create HD',  group: 'create',
        outputExt: '.chd', inputExtensions: CHDMAN_SOURCE_EXTS,
        supportsCompression: true,  supportsCompressionLevel: false,
        supportsDeleteOnVerify: true, allowsArchiveInput: true },
      { mode: 'createcd',  kind: 'create',  label: 'Create CD',  group: 'create',
        outputExt: '.chd', inputExtensions: CHDMAN_SOURCE_EXTS,
        supportsCompression: true,  supportsCompressionLevel: false,
        supportsDeleteOnVerify: true, allowsArchiveInput: true },
      { mode: 'createdvd', kind: 'create',  label: 'Create DVD', group: 'create',
        outputExt: '.chd', inputExtensions: CHDMAN_SOURCE_EXTS,
        supportsCompression: true,  supportsCompressionLevel: false,
        supportsDeleteOnVerify: true, allowsArchiveInput: true },
      { mode: 'createld',  kind: 'create',  label: 'Create LD',  group: 'create',
        outputExt: '.chd', inputExtensions: CHDMAN_SOURCE_EXTS,
        supportsCompression: true,  supportsCompressionLevel: false,
        supportsDeleteOnVerify: true, allowsArchiveInput: true },
      { mode: 'extractraw', kind: 'extract', label: 'Extract Raw', group: 'extract',
        outputExt: '.raw', inputExtensions: ['.chd'],
        supportsCompression: false, supportsCompressionLevel: false,
        supportsDeleteOnVerify: false, allowsArchiveInput: false },
      { mode: 'extracthd',  kind: 'extract', label: 'Extract HD',  group: 'extract',
        outputExt: '.raw', inputExtensions: ['.chd'],
        supportsCompression: false, supportsCompressionLevel: false,
        supportsDeleteOnVerify: false, allowsArchiveInput: false },
      { mode: 'extractcd',  kind: 'extract', label: 'Extract CD',  group: 'extract',
        outputExt: '.cue', inputExtensions: ['.chd'],
        supportsCompression: false, supportsCompressionLevel: false,
        supportsDeleteOnVerify: false, allowsArchiveInput: false },
      { mode: 'extractdvd', kind: 'extract', label: 'Extract DVD', group: 'extract',
        outputExt: '.iso', inputExtensions: ['.chd'],
        supportsCompression: false, supportsCompressionLevel: false,
        supportsDeleteOnVerify: false, allowsArchiveInput: false },
      { mode: 'extractld',  kind: 'extract', label: 'Extract LD',  group: 'extract',
        outputExt: '.avi', inputExtensions: ['.chd'],
        supportsCompression: false, supportsCompressionLevel: false,
        supportsDeleteOnVerify: false, allowsArchiveInput: false },
      { mode: 'copy', kind: 'copy', label: 'Copy / Recompress', group: 'copy',
        outputExt: '.chd', inputExtensions: ['.chd'],
        supportsCompression: true, supportsCompressionLevel: false,
        supportsDeleteOnVerify: true, allowsArchiveInput: false },
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
    groups: { dolphin: 'Dolphin' },
    defaultMode: 'dolphin_rvz',
    glyph: 'GC',
    accent: 'var(--badge-dat-match)',
    // Dolphin RVZ / WIA accept exactly one codec + a compression level.
    // GCZ ignores both; dolphin_iso is an extract op.
    compressionCodecs: [
      { value: 'zstd',  label: 'zstd',  hint: 'Recommended, level 19 matches MAME Redump' },
      { value: 'bzip2', label: 'bzip2', hint: 'Good ratio, slower' },
      { value: 'lzma',  label: 'lzma' },
      { value: 'lzma2', label: 'lzma2' },
      { value: 'none',  label: 'No compression' },
    ],
    compressionStyle: 'single-with-level',  // one codec + numeric level (0-22)
    compressionLevelRange: { min: 1, max: 22, default: 19 },
    modes: [
      { mode: 'dolphin_rvz', kind: 'compress', label: 'Dolphin RVZ', group: 'dolphin',
        outputExt: '.rvz', inputExtensions: DOLPHIN_SOURCE_EXTS,
        supportsCompression: true,  supportsCompressionLevel: true,
        supportsDeleteOnVerify: true, allowsArchiveInput: true },
      { mode: 'dolphin_wia', kind: 'compress', label: 'Dolphin WIA', group: 'dolphin',
        outputExt: '.wia', inputExtensions: DOLPHIN_SOURCE_EXTS,
        supportsCompression: true,  supportsCompressionLevel: true,
        supportsDeleteOnVerify: true, allowsArchiveInput: true },
      { mode: 'dolphin_gcz', kind: 'compress', label: 'Dolphin GCZ', group: 'dolphin',
        outputExt: '.gcz', inputExtensions: DOLPHIN_SOURCE_EXTS,
        supportsCompression: false, supportsCompressionLevel: false,
        supportsDeleteOnVerify: true, allowsArchiveInput: true },
      { mode: 'dolphin_iso', kind: 'extract', label: 'Dolphin ISO', group: 'dolphin',
        outputExt: '.iso', inputExtensions: DOLPHIN_SOURCE_EXTS,
        supportsCompression: false, supportsCompressionLevel: false,
        supportsDeleteOnVerify: true, allowsArchiveInput: true },
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
    groups: { z3ds: '3DS' },
    defaultMode: 'z3ds_compress',
    glyph: '3DS',
    accent: 'var(--badge-dvd)',
    // z3ds_compressor uses fixed compression; no codec or level controls.
    compressionCodecs: [],
    compressionStyle: 'none',
    modes: [
      { mode: 'z3ds_compress', kind: 'compress', label: 'Compress 3DS', group: 'z3ds',
        outputExt: null, inputExtensions: Z3DS_SOURCE_EXTS,
        supportsCompression: false, supportsCompressionLevel: false,
        supportsDeleteOnVerify: true, allowsArchiveInput: true },
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
  {
    id: 'nsz',
    label: 'Switch',
    hint: 'Compress and decompress Nintendo Switch dumps (NSP/XCI ↔ NSZ/XCZ). Needs your own prod.keys.',
    verifyPrefix: 'nsz',
    sourceExts: NSZ_SOURCE_EXTS,
    verifyExts: NSZ_VERIFY_EXTS,
    modeGroups: ['nsz'],
    groups: { nsz: 'Nintendo Switch' },
    defaultMode: 'nsz_compress',
    glyph: 'NSW',
    accent: 'var(--badge-dat-match)',
    // nsz exposes a zstandard level plus a solid/block layout. We model the
    // layout as the "codec" dropdown and the level as the slider, reusing the
    // single-with-level picker. The wire value is "<solid|block>:<level>".
    compressionStyle: 'single-with-level',
    compressionCodecs: [
      { value: 'solid', label: 'Solid (smaller)',
        hint: 'Best ratio; must be decompressed to run. nsz default for NSZ.' },
      { value: 'block', label: 'Block (random access)',
        hint: 'Slightly larger; supports playing/installing without full decompression.' },
    ],
    compressionLevelRange: { min: 1, max: 22, default: 18 },
    modes: [
      { mode: 'nsz_compress', kind: 'compress', label: 'Compress (NSP/XCI → NSZ/XCZ)',
        group: 'nsz',
        outputExt: null, inputExtensions: NSZ_COMPRESS_EXTS,
        supportsCompression: false, supportsCompressionLevel: true,
        supportsDeleteOnVerify: true, allowsArchiveInput: false },
      { mode: 'nsz_decompress', kind: 'extract', label: 'Decompress (NSZ/XCZ → NSP/XCI)',
        group: 'nsz',
        outputExt: null, inputExtensions: NSZ_VERIFY_EXTS,
        supportsCompression: false, supportsCompressionLevel: false,
        supportsDeleteOnVerify: false, allowsArchiveInput: false },
    ],
    getInfo: (path) => api.getNszInfo(path),
    verify: (path, opts) => api.verifyNsz(path, opts),
    verifyBatch: (paths, opts) => api.verifyBatchNsz(paths, opts),
    productPath: (path) => {
      const m = /\.(nsp|xci|nsz|xcz)$/i.exec(path);
      if (!m) return path;
      const ext = `.${m[1].toLowerCase()}`;
      return swapExt(path, NSZ_OUT_MAP[ext] ?? ext);
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

/** Build the single/batch verify URL from the tool's verifyPrefix. */
function deriveVerifyUrl(tool, kind) {
  if (!tool) return null;
  const seg = tool.verifyPrefix ? `${tool.verifyPrefix}-verify` : 'verify';
  return kind === 'batch' ? `/api/${seg}-batch/events` : `/api/${seg}/events`;
}

export const registry = {
  /** All registered tools, in declared order. */
  all: () => TOOLS,

  /** Set of registered tool ids, replaces hardcoded VALID_TOOLS in callers. */
  ids: () => new Set(TOOLS.map((t) => t.id)),

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

  /** Verify URL, single or batch, for a tool id. Derived from verifyPrefix. */
  verifyUrl: (toolId, kind) => deriveVerifyUrl(byId.get(toolId), kind),

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

  /**
   * Group label lookup. Each tool owns its group → label map via the
   * `groups` field; falls back to the raw group id so a new tool with
   * an unknown group still renders something readable.
   */
  groupLabel: (group, toolId) => {
    if (toolId) {
      const tool = byId.get(toolId);
      if (tool?.groups?.[group]) return tool.groups[group];
    }
    // Fallback: search every tool for the first matching group label
    // (useful when the caller doesn't know which tool owns the group).
    for (const tool of TOOLS) {
      if (tool.groups?.[group]) return tool.groups[group];
    }
    return group;
  },

  /** Default wire-mode for a tool, used when the workspace switches tools. */
  defaultMode: (toolId) => byId.get(toolId)?.defaultMode
    ?? byId.get(toolId)?.modes[0]?.mode
    ?? null,

  /**
   * Distinct source extensions across all registered tools, in
   * declaration order. Used by FileList's filter dropdown so adding a
   * new tool surfaces its inputs automatically, no hardcoded list.
   */
  allSourceExts: () => {
    const seen = new Set();
    const out = [];
    for (const tool of TOOLS) {
      for (const ext of tool.sourceExts) {
        if (!seen.has(ext)) { seen.add(ext); out.push(ext); }
      }
    }
    return out;
  },

  /** Distinct verify (output-class) extensions across all tools. */
  allVerifyExts: () => {
    const seen = new Set();
    const out = [];
    for (const tool of TOOLS) {
      for (const ext of tool.verifyExts) {
        if (!seen.has(ext)) { seen.add(ext); out.push(ext); }
      }
    }
    return out;
  },

  /**
   * Union of source + verify extensions, every extension the user
   * might want to filter by in a directory listing. Adding a tool
   * automatically surfaces its inputs and outputs in the filter
   * dropdown; no UI edits required.
   */
  allFilterableExts: () => {
    const seen = new Set();
    const out = [];
    for (const tool of TOOLS) {
      for (const ext of tool.sourceExts) {
        if (!seen.has(ext)) { seen.add(ext); out.push(ext); }
      }
      for (const ext of tool.verifyExts) {
        if (!seen.has(ext)) { seen.add(ext); out.push(ext); }
      }
    }
    return out;
  },
};
