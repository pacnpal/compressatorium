// Frontend tool registry. The tools, their extensions and every mode's
// metadata come from the backend's `GET /api/tools` (`tools`, one
// `ModeSpec.as_json()` per mode), loaded once at start by `registry.load()`
// (App.svelte). Nothing here mirrors the backend: what stays in this file is
// UI only, keyed by tool id in `TOOL_UI` (glyphs, accents, the words for
// codecs and groups, the verify URL prefix, the product-path guess), so a new
// tool or mode needs no frontend edit to appear (docs/ADDING_PLATFORMS_AND_TOOLS.md).
//
// All call sites (UI store, sidebar, SSE URL building, group labels, mode
// metadata) look up via the helpers below; there is no hardcoded
// `if (tool === 'chdman')` branching.

import { api } from '$lib/api/endpoints.js';

/**
 * @typedef {Object} ModeEntry
 * Every `ModeSpec` field, camelCased (`outputExt`, `inputExtensions`,
 * `supportsDeleteOnVerify`, `deleteProof`, `sourceKeptReason`, ...), plus:
 * @property {{value: string, label: string, hint?: string,
 *   levelRange?: {min: number, max: number, default: number}}[]} [compressionCodecs]
 *   the codecs this mode takes, worded from its tool's catalog
 * @property {string[]} [defaultCompression]  the mode's own default codec
 * @property {boolean} [supportsSplit]       makeps3iso -s 4 GB FAT32 split (opt-in)
 */

/**
 * @typedef {Object} ToolDescriptor
 * @property {string} id
 * @property {string} label
 * @property {string} hint
 * @property {string} verifyPrefix              URL segment for /api/{prefix}-verify (or empty for /api/verify)
 * @property {string[]} sourceExts
 * @property {string[]} verifyExts
 * @property {string[]} discExts               files that are one disc of a multi-disc game
 * @property {Record<string,string>} groups     group id → human label
 * @property {string} defaultMode               wire-mode the workspace selects when this tool is activated
 * @property {string} [glyph]                   short text affordance for the sidebar (1-3 chars)
 * @property {string} [accent]                  CSS color or token for badges / chips (falls back to --accent)
 * @property {boolean} [contentRefined]        backend offers it only on files whose CONTENT matched
 *                                            (its id in `convertible_by` outranks extension claims)
 * @property {ModeEntry[]} modes
 * @property {(path: string) => Promise<object>} [getInfo]
 * @property {(path: string, opts?: object) => Promise<object>} [verify]
 * @property {(paths: string[], opts?: object) => Promise<object>} [verifyBatch]
 * @property {(path: string, mode?: string) => string} productPath
 */

/** Replace the trailing extension on a path, preserving the rest. */
function swapExt(path, newExt) {
  return path.replace(/\.[^./\\]+$/, newExt);
}

/**
 * Fallback compression-level range for the slider when a tool declares no
 * `compressionLevelRange`. Every level-capable tool sets its own (Dolphin
 * default 19, nsz 18), so this is only a safety net — but it lives here once
 * rather than re-inlined as `{ min: 1, max: 22, default: 19 }` in the
 * conversion store and the CompressionPicker.
 */
export const DEFAULT_COMPRESSION_LEVEL_RANGE = Object.freeze({ min: 1, max: 22, default: 19 });

// The words for chdman's codecs. Each create mode takes the ones its
// `ModeSpec.codecs` names (#471); this order is the order the pickers show.
const CHDMAN_CODECS = [
  { value: 'none',  label: 'No compression' },
  { value: 'zlib',  label: 'zlib',  hint: 'Fast, baseline compatibility' },
  { value: 'zstd',  label: 'zstd',  hint: 'Modern general-purpose codec' },
  { value: 'lzma',  label: 'lzma',  hint: 'Slower, better ratio' },
  { value: 'huff',  label: 'huff',  hint: 'Huffman entropy coder' },
  { value: 'flac',  label: 'flac',  hint: 'For CD audio tracks' },
  { value: 'cdzl',  label: 'cdzl',  hint: 'CD-zlib variant' },
  { value: 'cdzs',  label: 'cdzs',  hint: 'CD-zstd variant' },
  { value: 'cdlz',  label: 'cdlz',  hint: 'CD-lzma variant' },
  { value: 'cdfl',  label: 'cdfl',  hint: 'CD-FLAC variant' },
  { value: 'avhu',  label: 'avhu',  hint: 'Audio/video Huffman' },
];

const Z3DS_OUT_MAP = {
  '.cci': '.zcci', '.cia': '.zcia', '.3ds': '.z3ds',
  '.cxi': '.zcxi', '.3dsx': '.z3dsx',
  '.zcci': '.cci', '.zcia': '.cia', '.z3ds': '.3ds',
  '.zcxi': '.cxi', '.z3dsx': '.3dsx',
};
const NSZ_OUT_MAP = {
  '.nsp': '.nsz', '.xci': '.xcz', '.nsz': '.nsp', '.xcz': '.xci',
};

/**
 * What only the UI knows about each tool, keyed by tool id. Everything else
 * (extensions, modes, codecs, level ranges, defaults) is the backend's.
 * A tool missing here still works, with the plain defaults in `uiFor`.
 */
const TOOL_UI = {
  chdman: {
    hint: 'Convert CD / DVD / LaserDisc images to and from CHD.',
    // chdman is the original endpoint set, so it has no URL prefix:
    // /api/verify, /api/verify-batch. Other tools use /api/<prefix>-verify.
    verifyPrefix: '',
    groups: { create: 'Create', extract: 'Extract', copy: 'Copy' },
    defaultMode: 'createcd',
    glyph: 'CD',
    accent: 'var(--badge-cd)',
    // Copy / Recompress names no codecs (they follow the input CHD), so it is
    // offered this whole catalog.
    compressionCodecs: CHDMAN_CODECS,
    compressionStyle: 'multi',  // multi-codec list joined with commas
    // chdman's `-c` takes at most four codecs. Declared on the tool, not in
    // whichever picker happens to be on screen.
    maxCodecs: 4,
    verify: (path, opts) => api.verifyCHD(path, opts),
    verifyBatch: (paths, opts) => api.verifyBatchCHDs(paths, opts),
    productPath: (path) => swapExt(path, '.chd'),
  },
  dolphin: {
    hint: 'Compress GameCube / Wii discs to RVZ, WIA, or GCZ.',
    verifyPrefix: 'dolphin',
    // `.iso` is intentionally not a Verify row action here even though
    // dolphin-tool can verify GC/Wii ISOs: most ISOs in a directory are CHDMAN
    // CD/DVD sources, and `toolForVerifyPath()` returns the first tool whose
    // verify list matches, so every CD ISO would be sent to /api/dolphin-verify.
    // A GC/Wii ISO is verified through its output (RowActionsMenu).
    verifyExts: ['.gcz', '.wia', '.rvz', '.wbfs'],
    groups: { dolphin: 'Dolphin' },
    defaultMode: 'dolphin_rvz',
    glyph: 'GC',
    accent: 'var(--badge-dat-match)',
    compressionCodecs: [
      { value: 'zstd',  label: 'zstd',  hint: 'Recommended, level 19 matches MAME Redump' },
      { value: 'bzip2', label: 'bzip2', hint: 'Good ratio, slower' },
      { value: 'lzma',  label: 'lzma' },
      { value: 'lzma2', label: 'lzma2' },
      { value: 'none',  label: 'No compression' },
    ],
    compressionStyle: 'single-with-level',  // one codec + its level
    compressionLevelRange: { min: 1, max: 22, default: 19 },
    verify: (path, opts) => api.verifyDolphin(path, opts),
    verifyBatch: (paths, opts) => api.verifyBatchDolphin(paths, opts),
    productPath: (path) => swapExt(path, '.rvz'),
  },
  z3ds: {
    defaultCompression: ['zlib'],
    hint: 'Compress and decompress Nintendo 3DS ROMs (.3ds/.cci/.cia/.cxi/.3dsx ↔ Z3DS).',
    verifyPrefix: 'z3ds',
    groups: { z3ds: '3DS' },
    defaultMode: 'z3ds_compress',
    glyph: '3DS',
    accent: 'var(--badge-dvd)',
    // z3ds_compressor uses fixed compression; no codec or level controls.
    compressionStyle: 'none',
    verify: (path, opts) => api.verify3DS(path, opts),
    verifyBatch: (paths, opts) => api.verifyBatchZ3DS(paths, opts),
    productPath: (path) => {
      const m = /\.(3ds|cci|cia|cxi|3dsx|zcci|zcia|z3ds|zcxi|z3dsx)$/i.exec(path);
      if (!m) return path;
      const ext = `.${m[1].toLowerCase()}`;
      return swapExt(path, Z3DS_OUT_MAP[ext] ?? ext);
    },
  },
  nsz: {
    // nsz's "codec" is the solid/block layout the picker models; its own
    // default for NSZ is solid.
    defaultCompression: ['solid'],
    hint: 'Compress and decompress Nintendo Switch dumps (NSP/XCI ↔ NSZ/XCZ). Needs your own prod.keys.',
    verifyPrefix: 'nsz',
    groups: { nsz: 'Nintendo Switch' },
    defaultMode: 'nsz_compress',
    glyph: 'NSW',
    accent: 'var(--badge-dat-match)',
    // A zstandard level plus a solid/block layout: the layout is the "codec"
    // dropdown and the level the slider. The wire value is "<solid|block>:<level>".
    compressionStyle: 'single-with-level',
    compressionCodecs: [
      { value: 'solid', label: 'Solid (smaller)',
        hint: 'Best ratio; must be decompressed to run. nsz default for NSZ.' },
      { value: 'block', label: 'Block (random access)',
        hint: 'Slightly larger; supports playing/installing without full decompression.' },
    ],
    compressionLevelRange: { min: 1, max: 22, default: 18 },
    verify: (path, opts) => api.verifyNsz(path, opts),
    verifyBatch: (paths, opts) => api.verifyBatchNsz(paths, opts),
    productPath: (path) => {
      const m = /\.(nsp|xci|nsz|xcz)$/i.exec(path);
      if (!m) return path;
      const ext = `.${m[1].toLowerCase()}`;
      return swapExt(path, NSZ_OUT_MAP[ext] ?? ext);
    },
  },
  cso: {
    hint: 'Compress PSP / PS2 ISO images to CSO / CSO v2 / ZSO / DAX (and back).',
    verifyPrefix: 'cso',
    groups: { cso: 'CSO / ZSO / DAX', chain: 'CSO → CHD' },
    defaultMode: 'cso_compress',
    glyph: 'CSO',
    accent: 'var(--badge-cso)',
    // The output format is fixed by the mode; the dropdown picks an effort
    // preset (no numeric level, so the picker hides the slider).
    compressionCodecs: [
      { value: 'default', label: 'Default', hint: 'Balanced (maxcso default trials)' },
      { value: 'fast', label: 'Fast', hint: 'Fastest, larger output (--fast)' },
      { value: 'max', label: 'Max', hint: 'Smallest, slowest (extra deflate/lz4 trials)' },
    ],
    compressionStyle: 'single-with-level',
    verify: (path, opts) => api.verifyCso(path, opts),
    verifyBatch: (paths, opts) => api.verifyBatchCso(paths, opts),
    // mode lets ISO sources resolve to the right target (.cso vs .zso); it
    // defaults to the primary CSO product when the caller has no mode in hand.
    productPath: (path, mode = 'cso_compress') => {
      if (/\.iso$/i.test(path)) {
        const ext = mode === 'zso_compress' ? '.zso'
          : mode === 'dax_compress' ? '.dax'
          : '.cso';
        return swapExt(path, ext);
      }
      if (/\.(cso|zso|dax)$/i.test(path)) {
        return swapExt(path, mode === 'cso_to_chd' ? '.chd' : '.iso');
      }
      return path;
    },
  },
  romz: {
    hint: 'Compress Game Boy / GBC / GBA / DS ROM dumps to .7z / .zip (and back).',
    verifyPrefix: 'romz',
    groups: { romz: 'GB / GBC / GBA / DS' },
    defaultMode: 'romz_7z',
    glyph: 'ROM',
    accent: 'var(--badge-romz)',
    compressionCodecs: [
      { value: 'max', label: 'Max', hint: 'Smallest (-mx9 -md=256m -mfb=273)' },
      { value: 'default', label: 'Default', hint: 'Balanced (-mx7)' },
      { value: 'fast', label: 'Fast', hint: 'Fastest, larger output (-mx1)' },
    ],
    compressionStyle: 'single-with-level',
    verify: (path, opts) => api.verifyRomz(path, opts),
    verifyBatch: (paths, opts) => api.verifyBatchRomz(paths, opts),
    // Compress keeps the ROM extension in front of the archive suffix
    // (Game.gba -> Game.gba.7z); extract strips the archive suffix back off.
    productPath: (path, mode = 'romz_7z') => {
      if (/\.(gb|gbc|gba|nds)$/i.test(path)) {
        return path + (mode === 'romz_zip' ? '.zip' : '.7z');
      }
      if (/\.(7z|zip)$/i.test(path)) return path.replace(/\.(7z|zip)$/i, '');
      return path;
    },
  },
  makeps3iso: {
    defaultCompression: ['zlib'],
    hint: 'Build a PS3 .iso from a decrypted disc/JB folder (a PS3_GAME/ root). No keys, no decryption.',
    // No verify endpoint: its only check is a PARAM.SFO readback in the job.
    verifyPrefix: '',
    groups: { makeps3iso: 'PS3 Folder → ISO' },
    defaultMode: 'folder_to_iso',
    glyph: 'PS3',
    accent: 'var(--badge-dvd)',
    compressionStyle: 'none',
    // makeps3iso -s: split the output into ~4 GB parts for FAT32 targets.
    modes: { folder_to_iso: { supportsSplit: true } },
    // No file-path Info: a folder has no extension to route on.
    noInfo: true,
    // The product is a sibling "<folder>.iso" (folder name, not a stem swap).
    productPath: (path) => `${(path ?? '').replace(/[/\\]+$/, '')}.iso`,
  },
  nkit: {
    hint: 'Restore an NKit-shrunk GameCube / Wii image back to a full .iso.',
    // No verify: nkit2iso has none; the restore checks the CRC32 inline.
    verifyPrefix: '',
    groups: { nkit: 'NKit → ISO', chain: 'One-step pipeline' },
    defaultMode: 'nkit_restore',
    glyph: 'NKT',
    accent: 'var(--badge-dvd)',
    compressionStyle: 'none',
    // Compound extension: strip the whole ".nkit.iso" / ".nkit.gcz". `mode`
    // picks the chain's .rvz over the plain restore's .iso.
    productPath: (path, mode = 'nkit_restore') => (path ?? '').replace(
      /\.nkit\.(iso|gcz)$/i, mode === 'nkit_to_rvz' ? '.rvz' : '.iso',
    ),
  },
  jwud: {
    hint: 'Compress and decompress Wii U disc images (WUD ↔ WUX). No keys needed.',
    verifyPrefix: 'jwud',
    groups: { jwud: 'Wii U' },
    defaultMode: 'jwud_compress',
    glyph: 'WUD',
    accent: 'var(--badge-wiiu)',
    // WUX has no codec or level; the dropdown carries JWUDTool's own
    // verification pass instead, as nsz's carries solid/block.
    compressionCodecs: [
      { value: 'verify', label: 'Verify against source',
        hint: 'JWUDTool re-reads both images and compares them byte for byte. Doubles the runtime.' },
      { value: 'noverify', label: 'Skip verification (faster)',
        hint: 'Passes -noVerify: about half the runtime, but the result is not checked against the source.' },
    ],
    compressionStyle: 'single-with-level',
    verify: (path, opts) => api.verifyJwud(path, opts),
    verifyBatch: (paths, opts) => api.verifyBatchJwud(paths, opts),
    productPath: (path) => {
      if (/\.wud$/i.test(path)) return swapExt(path, '.wux');
      if (/\.wux$/i.test(path)) return swapExt(path, '.wud');
      return path;
    },
  },
  xiso: {
    hint: 'Trim Xbox / Xbox 360 disc images to XISO for xemu, Xenia and modded consoles.',
    verifyPrefix: 'xiso',
    // Offered only on files the backend's Xbox filesystem probe matched. The
    // Info modal also tries this tool first for such a row.
    contentRefined: true,
    groups: { xiso: 'Xbox ISO → XISO' },
    defaultMode: 'xiso_rewrite',
    glyph: 'XBX',
    accent: 'var(--badge-dvd)',
    compressionStyle: 'none',
    verify: (path, opts) => api.verifyXiso(path, opts),
    verifyBatch: (paths, opts) => api.verifyBatchXiso(paths, opts),
    // `Game.iso` -> `Game.xiso.iso`; a product maps to itself.
    productPath: (path) => {
      if (/\.xiso\.iso$/i.test(path)) return path;
      if (/\.iso$/i.test(path)) return path.replace(/\.iso$/i, '.xiso.iso');
      return path;
    },
  },
  rom_converto: {
    hint: 'The converter RomM ships, for formats nothing else here writes: Xbox 360 ISO to ZAR, PSP PBP to ISO, and 3DS CIA to CCI and back.',
    verifyPrefix: 'rom-converto',
    // Only the ZAR is a Verify row action; the ISO / CCI / CIA outputs are
    // checked inside the job that wrote them.
    verifyExts: ['.zar'],
    // Offered only on files the backend's probe matched, as for xiso.
    contentRefined: true,
    groups: { xenon: 'Xbox 360', psp: 'PSP', ctr: '3DS' },
    defaultMode: 'zar_compress',
    glyph: 'RC',
    accent: 'var(--badge-dvd)',
    compressionStyle: 'none',
    verify: (path, opts) => api.verifyRomConverto(path, opts),
    verifyBatch: (paths, opts) => api.verifyBatchRomConverto(paths, opts),
    productPath: (path, mode = 'zar_compress') => {
      if (mode === 'pbp_to_iso') return swapExt(path, '.iso');
      if (mode === 'cia_to_cci') return swapExt(path, '.cci');
      if (mode === 'cci_to_cia') return swapExt(path, '.cia');
      if (/\.zar$/i.test(path)) return path;
      if (/\.xiso\.iso$/i.test(path)) return path.replace(/\.xiso\.iso$/i, '.zar');
      if (/\.iso$/i.test(path)) return path.replace(/\.iso$/i, '.zar');
      return path;
    },
  },
  exodos: {
    hint: 'Repack eXoDOS game zips for RomM\'s in-browser DOS player (DOSBox Pure).',
    verifyPrefix: 'exodos',
    // Not contentRefined: a .zip is an archive row, which never carries a
    // convertible_by claim; plan_job skips a zip that is not a DOS game.
    groups: { exodos: 'eXoDOS → RomM' },
    defaultMode: 'exodos_repack',
    glyph: 'DOS',
    accent: 'var(--badge-romz)',
    compressionStyle: 'none',
    // No Info: the product is a zip with a known layout, nothing beyond name
    // and size to show.
    noInfo: true,
    verify: (path, opts) => api.verifyExodos(path, opts),
    verifyBatch: (paths, opts) => api.verifyBatchExodos(paths, opts),
    productPath: (path) => {
      if (/\.dosz$/i.test(path)) return path;
      if (/\.zip$/i.test(path)) return path.replace(/\.zip$/i, '.dosz');
      return path;
    },
  },
  rar: {
    hint: 'Unpack a multi-part RAR set (usenet) to the one ROM or disc image inside it.',
    // No Verify row action: a set is checked by extracting it.
    verifyPrefix: '',
    groups: { rar: 'RAR set → file' },
    defaultMode: 'rar_extract',
    glyph: 'RAR',
    accent: 'var(--badge-romz)',
    compressionStyle: 'none',
    noInfo: true,
    // The product is named after the file inside the set, which only the
    // backend can read, so the set maps to itself.
    productPath: (path) => path,
  },
};

/** A tool's UI extras, with plain defaults for a tool `TOOL_UI` doesn't know. */
function uiFor(id) {
  return {
    verifyPrefix: id.replace(/_/g, '-'),
    groups: {},
    compressionStyle: 'none',
    productPath: (path) => path,
    ...TOOL_UI[id],
  };
}

/** `snake_case` → `camelCase`. */
function camel(key) {
  return key.replace(/_([a-z])/g, (_m, c) => c.toUpperCase());
}

/** A codec's `{min, max, default}` from a mode's `codec_levels`, or undefined. */
function levelRangeIn(codecLevels, codec) {
  const hit = (codecLevels ?? []).find((l) => l.codec === codec);
  return hit ? { min: hit.min, max: hit.max, default: hit.default } : undefined;
}

/** `catalog`'s entries for `values` (its order), each with its level range. */
function wordedCodecs(catalog, values, codecLevels) {
  const known = catalog.filter((c) => values.includes(c.value));
  const extra = values.filter((v) => !catalog.some((c) => c.value === v))
    .map((value) => ({ value, label: value }));
  return [...known, ...extra].map((c) => {
    const levelRange = levelRangeIn(codecLevels, c.value);
    return levelRange ? { ...c, levelRange } : { ...c };
  });
}

/** One mode's entry: every ModeSpec field camelCased, plus its worded codecs. */
function modeEntry(spec, ui) {
  const entry = Object.fromEntries(Object.entries(spec).map(([k, v]) => [camel(k), v]));
  delete entry.defaultCompression;
  if (spec.default_compression) entry.defaultCompression = [spec.default_compression];
  if (spec.codecs?.length) {
    entry.compressionCodecs = wordedCodecs(ui.compressionCodecs ?? [], spec.codecs, spec.codec_levels);
  }
  return { ...entry, ...ui.modes?.[spec.mode] };
}

/**
 * The descriptors from a `/api/tools` payload. A mode run by the chain tool
 * (it has `steps`) is listed under the tool its first step runs, which is
 * where the UI offers it (CSO → CHD under CSO); a tool left with no modes of
 * its own (the chain shell) is not listed.
 */
function buildTools(payload) {
  const tools = (payload?.tools ?? []).map((t) => {
    const ui = uiFor(t.id);
    const { modes: _modeExtras, noInfo, defaultCompression, verifyExts, ...extras } = ui;
    return {
      ...extras,
      id: t.id,
      label: t.label,
      sourceExts: t.input_extensions,
      verifyExts: verifyExts ?? t.verify_extensions,
      discExts: t.disc_extensions,
      defaultCompression: t.default_compression ? [t.default_compression] : defaultCompression,
      // A codec's level range is the backend's, from whichever mode takes it.
      compressionCodecs: (ui.compressionCodecs ?? []).map((c) => {
        const levelRange = t.modes
          .map((m) => levelRangeIn(m.codec_levels, c.value)).find(Boolean);
        return levelRange ? { ...c, levelRange } : { ...c };
      }),
      ...(noInfo ? {} : { getInfo: (path) => api.getToolInfo(t.id, path) }),
      _payloadModes: t.modes,
      modes: [],
    };
  });
  const byIdNow = new Map(tools.map((t) => [t.id, t]));
  for (const tool of tools) {
    for (const spec of tool._payloadModes) {
      const owner = byIdNow.get(spec.steps?.[0]?.tool_id) ?? tool;
      owner.modes.push(modeEntry(spec, uiFor(owner.id)));
    }
  }
  for (const tool of tools) delete tool._payloadModes;
  return tools.filter((t) => t.modes.length);
}

/** @type {ToolDescriptor[]} — empty until `registry.load()` */
let TOOLS = [];
let byId = new Map();
let byMode = new Map();
// Compound extensions any tool declares (`.nkit.iso`, `.nkit.gcz`, `.xiso.iso`),
// longest first, for `displayExtension` below.
let COMPOUND_EXTS = [];

function index() {
  byId = new Map(TOOLS.map((t) => [t.id, t]));
  byMode = new Map(TOOLS.flatMap((t) => t.modes.map((m) => [m.mode, { tool: t, spec: m }])));
  COMPOUND_EXTS = [...new Set(
    TOOLS.flatMap((t) => [...t.sourceExts, ...t.verifyExts])
      .map((e) => e.toLowerCase())
      .filter((e) => e.lastIndexOf('.') > 0),
  )].sort((a, b) => b.length - a.length);
}

/** Whether a tool can answer Info (its `getInfo` binding is set). */
function hasInfoBinding(tool) {
  return typeof tool?.getInfo === 'function';
}

function endsWithAny(path, exts) {
  return matchedExtLength(path, exts) > 0;
}

/**
 * Length of the LONGEST extension in `exts` that `path` ends with, else 0.
 * Mirrors the backend's `utils.path_utils.match_extension`, and the length is
 * what lets callers rank a compound claim (`.nkit.iso`) above the generic tail
 * (`.iso`) that other tools own.
 */
function matchedExtLength(path, exts) {
  if (!path) return 0;
  const lower = path.toLowerCase();
  let best = 0;
  for (const ext of exts) {
    if (lower.endsWith(ext) && ext.length > best) best = ext.length;
  }
  return best;
}

/**
 * Tools claiming `path` as a source, MOST SPECIFIC first (declared order
 * breaks ties). An NKit image is claimed by `nkit` via the compound
 * `.nkit.iso` and by chdman / Dolphin / CSO via the plain `.iso` tail; the
 * specific claim has to win, or the Info modal walks the generic tools first
 * and Dolphin's header reader "succeeds" on an NKit file (it has a real GC/Wii
 * disc header) and reports the shrunk image as if it were a plain disc.
 */
function sourceToolsBySpecificity(path) {
  return TOOLS
    .map((t) => ({ t, n: matchedExtLength(path, t.sourceExts) }))
    .filter((x) => x.n > 0)
    .sort((a, b) => b.n - a.n)
    .map((x) => x.t);
}

/** Verify-class tool whose verifyExts match the path (extension only). */
function matchVerifyTool(path) {
  if (!path) return null;
  return TOOLS.find((t) => endsWithAny(path, t.verifyExts)) ?? null;
}

/** Build the single/batch verify URL from the tool's verifyPrefix. */
function deriveVerifyUrl(tool, kind) {
  if (!tool) return null;
  const seg = tool.verifyPrefix ? `${tool.verifyPrefix}-verify` : 'verify';
  return kind === 'batch' ? `/api/${seg}-batch/events` : `/api/${seg}/events`;
}

export const registry = {
  /**
   * Fill the registry from a `GET /api/tools` payload (its `tools`). Called
   * once at start, by App.svelte, before any view renders; every method
   * below reads what this built. Until then the registry is empty. A payload
   * that names no tools (an error body, a broken reply) changes nothing and
   * answers false, so it can never empty a loaded registry.
   */
  load: (payload) => {
    const tools = buildTools(payload);
    if (!tools.length) return false;
    TOOLS = tools;
    index();
    return true;
  },

  /** Whether `load` has run with a payload that named tools. */
  get loaded() {
    return TOOLS.length > 0;
  },

  /** All registered tools, in declared order. */
  all: () => TOOLS,

  /** The paths that are disc Images by any tool's `discExts` (extension only). */
  discImages: (paths) =>
    paths.filter((p) => TOOLS.some((t) => endsWithAny(p, t.discExts ?? []))),

  /** Set of registered tool ids, replaces hardcoded VALID_TOOLS in callers. */
  ids: () => new Set(TOOLS.map((t) => t.id)),

  /** Lookup by tool id (`'chdman'`, `'dolphin'`, `'z3ds'`). */
  forTool: (id) => byId.get(id),

  /** Mode metadata (ModeEntry) for a wire-mode value. */
  specFor: (mode) => byMode.get(mode)?.spec,

  /** The owning tool for a wire-mode value. */
  toolForMode: (mode) => byMode.get(mode)?.tool,

  /** How many codecs this mode's tool accepts at once (Infinity if unbounded). */
  maxCodecsFor: (mode) => byMode.get(mode)?.tool?.maxCodecs ?? Infinity,

  /** The codecs a picker offers for `mode`: the mode's own list, else its tool's. */
  codecsForMode: (mode) => {
    const entry = byMode.get(mode);
    return entry?.spec?.compressionCodecs ?? entry?.tool?.compressionCodecs ?? [];
  },

  /**
   * The codecs in `selection` that `mode` offers (#471, #474). chdman's list
   * is kept per tool and a Rule's may predate its mode naming codecs, so a CD
   * codec can still be selected on Create DVD, which can't use it. The convert
   * panel sends, and the Rule editor shows and counts, only these. A mode that
   * offers none keeps the whole selection. Mirrors the backend's
   * `without_unusable_codecs`.
   */
  offeredCodecs: (mode, selection) => {
    const offered = registry.codecsForMode(mode).map((c) => c.value);
    return offered.length ? selection.filter((c) => offered.includes(c)) : [...selection];
  },

  /** The codec(s) a picker starts on for `mode`: the mode's own, else its tool's. */
  defaultCompressionForMode: (mode) => {
    const entry = byMode.get(mode);
    return entry?.spec?.defaultCompression ?? entry?.tool?.defaultCompression;
  },

  /**
   * The level range `{min, max, default}` of the codec a job in `mode` runs
   * with: `codec`, or the mode's default when blank (#363). Null when there is
   * no level to set: the mode takes none, or it ranges levels per codec
   * (Dolphin: lzma2 1-9, zstd 1-22) and that codec has none (`none`, which is
   * WIA's default). A mode with one range for every codec (nsz) gets the
   * tool's, else the shared default. Mirrors the backend's
   * `ModeSpec.takes_no_level`; every level control shows only on non-null.
   */
  levelRangeFor: (mode, codec) => {
    const entry = byMode.get(mode);
    if (!entry?.spec?.supportsCompressionLevel) return null;
    const codecs = entry.spec.compressionCodecs ?? entry.tool?.compressionCodecs ?? [];
    if (!codecs.some((c) => c.levelRange)) {
      return entry.tool?.compressionLevelRange ?? DEFAULT_COMPRESSION_LEVEL_RANGE;
    }
    const effective = codec || (entry.spec.defaultCompression ?? entry.tool?.defaultCompression)?.[0];
    return codecs.find((c) => c.value === effective)?.levelRange ?? null;
  },

  /** Pick the tool whose verify_extensions match a given file path, or null.
   *  Pure extension match — use `verifyToolForPath` when an entry's backend
   *  `verifiable_by` refinement should also apply (e.g. romz archive rows). */
  toolForVerifyPath: (path) => matchVerifyTool(path),

  /**
   * Verify tool for a concrete file, narrowed by the backend's per-file
   * `verifiable_by` refinement when provided. The extension match stays the
   * source of truth — it encodes deliberate UX exclusions the backend's
   * broader verify_extensions don't (e.g. `.iso` is intentionally NOT routed
   * to Dolphin verify) — and `verifiable_by` may only NARROW it, never broaden
   * it (romz claims `.7z`/`.zip` by extension but only single-ROM archives are
   * actually verifiable). Pass the listing's `entry.verifiable_by`; omit it
   * (undefined) for paths the listing doesn't annotate, leaving the plain
   * extension match. Shared by the row menu and the bulk-verify paths so they
   * gate identically.
   */
  verifyToolForPath: (path, verifiableBy) => {
    const tool = matchVerifyTool(path);
    if (!tool) return null;
    if (Array.isArray(verifiableBy) && !verifiableBy.includes(tool.id)) {
      return null;
    }
    return tool;
  },

  /**
   * The tool whose Info a file row's Info action calls, or null for none.
   * The row's verify owner wins (it reads the product it made), then, on a
   * plain file row, the first source-claiming tool. Either way only a tool
   * with a `getInfo` binding qualifies, so a verifiable product with nothing
   * to describe (the eXoDOS `.dosz`) gets no Info action rather than one that
   * throws. Archive rows skip the source fallback: romz lists `.7z`/`.zip` as
   * sources, and archive Info belongs to the backend-gated verify owner.
   */
  infoToolForRow: (path, verifiableBy, isArchive) => {
    const owner = registry.verifyToolForPath(path, verifiableBy);
    if (owner) return hasInfoBinding(owner) ? owner : null;
    if (isArchive || !path || typeof path !== 'string') return null;
    return sourceToolsBySpecificity(path).find(hasInfoBinding) ?? null;
  },

  /** Tools whose source extensions match the given path (convertible sources),
   *  most-specific claim first — see `sourceToolsBySpecificity`. */
  toolsForSourcePath: (path) => sourceToolsBySpecificity(path),

  /**
   * Info-capable tools for a path, richest-first, deduped by id. A path
   * can be claimed by more than one tool (a raw .iso is both a chdman
   * create source and a Dolphin disc) and only one actually reads it, so
   * callers try these in order and keep the first getInfo() that returns.
   * Order: most-specific claim first, with the verify-path owner winning ties.
   * Tools without a getInfo binding are skipped.
   */
  infoToolsForPath: (path) => {
    if (!path) return [];
    // Rank EVERY claim — source or verify — by matched-extension length, so the
    // tool that understands the container comes first. The verify owner must
    // not simply be prepended: for `Game.nkit.gcz` that is Dolphin (via its
    // generic `.gcz`), whose reader parses the GCZ and its embedded disc header
    // successfully, so the modal would stop there and never reach /nkit-info.
    // Verify ownership only breaks ties at equal specificity, which preserves
    // the old order for every single-suffix path (e.g. a `.chd` still starts at
    // chdman). Dedup by reference; skip tools without a getInfo binding.
    const verifyOwner = matchVerifyTool(path);
    const ordered = TOOLS
      .map((t) => ({
        t,
        n: Math.max(
          matchedExtLength(path, t.sourceExts),
          matchedExtLength(path, t.verifyExts),
        ),
        v: t === verifyOwner ? 1 : 0,
      }))
      .filter((x) => x.n > 0)
      .sort((a, b) => b.n - a.n || b.v - a.v)
      .map((x) => x.t);
    const out = [];
    for (const t of ordered) {
      if (t && typeof t.getInfo === 'function' && !out.includes(t)) {
        out.push(t);
      }
    }
    return out;
  },

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
   * The extension to SHOW for a listing row: the longest declared compound
   * extension the file's name ends with (`Game.xiso.iso` -> `.xiso.iso`),
   * otherwise the row's own `extension`.
   *
   * Display only. The backend deliberately records just the trailing suffix
   * (`.iso`) in `entry.extension`, and sorting, DAT-match gating and the file
   * icons all key off that stored value, so they are unchanged by this.
   */
  displayExtension: (entry) => {
    const ext = entry?.extension ?? '';
    if (!ext || entry.type === 'directory' || entry.type === 'archive') return ext;
    const name = String(entry.name ?? entry.path ?? '').toLowerCase();
    const tail = ext.toLowerCase();
    const hit = COMPOUND_EXTS.find(
      (c) => c.endsWith(tail) && name.length > c.length && name.endsWith(c),
    );
    return hit ?? ext;
  },

  /**
   * The `contentRefined` tools whose backend content probe claimed this row,
   * i.e. those listed in its `convertible_by`, in registry order. Returns
   * `null` (not `[]`) when the row carries no `convertible_by` at all, so a
   * caller can tell "the probe said no" apart from "the listing didn't say".
   *
   * The one place this claim is read: the selection gate narrows a
   * content-refined mode to these rows, and the Info modal tries these tools
   * before the generic extension owners.
   */
  contentClaimsFor: (entry) => {
    const by = entry?.convertible_by;
    if (!Array.isArray(by)) return null;
    return TOOLS.filter((t) => t.contentRefined && by.includes(t.id));
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
