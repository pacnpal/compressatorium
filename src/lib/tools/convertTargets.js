// The choices of a "Convert to" select, grouped by what they do, and narrowed
// to what can read the games at hand or what a console allows. One place, so
// the Convert panel, the RomM Browse and a console's settings offer the same
// words for the same thing.
//
// Pure apart from the registry: the Node tests load it with the registry inlined.

import { registry } from './registry.js';

// Modes that change a game's container without making it smaller. Listed
// under Other, never under Make smaller.
export const OTHER_MODES = new Set([
  'pbp_to_iso', 'cia_to_cci', 'cci_to_cia', 'folder_to_iso', 'exodos_repack',
]);

// Plain labels for the modes whose registry label names the tool's operation
// rather than what comes out ("Extract CD" is "BIN/CUE from CHD").
export const PLAIN_MODE_LABELS = Object.freeze({
  extractraw: 'Raw image from CHD',
  extracthd: 'Hard disk image from CHD',
  extractcd: 'BIN/CUE from CHD',
  extractdvd: 'ISO from CHD',
  extractld: 'LaserDisc video from CHD',
  dolphin_iso: 'ISO from RVZ/WIA/GCZ',
  z3ds_decompress: '3DS game from Z3DS',
  nsz_decompress: 'NSP/XCI from NSZ/XCZ',
  cso_decompress: 'ISO from CSO/ZSO/DAX',
  romz_extract: 'Game from 7z/zip',
  nkit_restore: 'ISO from NKit',
  jwud_decompress: 'WUD from WUX',
  rar_extract: 'File from a RAR set',
  copy: 'Recompress CHD',
  pbp_to_iso: 'ISO from PSP PBP',
  cia_to_cci: 'CCI from CIA',
  cci_to_cia: 'CIA from CCI',
  folder_to_iso: 'ISO from a PS3 folder',
  exodos_repack: 'eXoDOS game for RomM',
  // Make-smaller modes whose output follows the input's extension.
  z3ds_compress: 'Z3DS',
  nsz_compress: 'NSZ/XCZ',
});

// Formats whose name isn't just the extension in capitals.
const FORMAT_LABELS = Object.freeze({ '.xiso.iso': 'XISO' });

export const BEST_FORMAT = Object.freeze({ value: '', label: 'Best format for each game' });

function formatLabel(ext) {
  return FORMAT_LABELS[ext] ?? ext.replace(/^\./, '').toUpperCase();
}

function groupOf(spec) {
  if (OTHER_MODES.has(spec.mode) || spec.kind === 'copy') return 'Other';
  if (spec.kind === 'extract') return 'Unpack';
  return 'Make smaller';
}

// True when *spec* (of *tool*) reads at least one of *entries*.
function readsAny(spec, tool, entries) {
  const folders = Array.isArray(spec.inputKinds) && spec.inputKinds.includes('directory');
  const exts = spec.inputExtensions ?? [];
  return entries.some((entry) => {
    if (folders) {
      return entry?.type === 'directory' && (entry.convertible_by ?? []).includes(tool.id);
    }
    if (!entry || entry.type === 'directory') return false;
    const name = String(entry.path ?? entry.name ?? '').split('::').pop().toLowerCase();
    return exts.some((ext) => name.endsWith(ext));
  });
}

/**
 * Groups for a "Convert to" select:
 * `[{ group, options: [{ value, label, hint? }] }]`, empty groups left out.
 *
 * - `entries`: FileEntry rows; only modes that can read one of them are kept.
 * - `modeIds` / `toolIds`: a console's allowed modes and tools.
 * - `ready`: the tool ids that can run here; others are left out.
 * - `perMode`: list each Make-smaller mode on its own (value = mode id)
 *   instead of one option per format (value = the format, `.chd`).
 */
export function convertTargets({
  entries = null, modeIds = null, toolIds = null, ready = null, perMode = false,
} = {}) {
  const readySet = ready ? new Set(ready) : null;
  const smaller = new Map();
  const unpack = [];
  const other = [];
  for (const tool of registry.all()) {
    if (toolIds && !toolIds.includes(tool.id)) continue;
    if (readySet && !readySet.has(tool.id)) continue;
    for (const spec of tool.modes ?? []) {
      if (modeIds && !modeIds.includes(spec.mode)) continue;
      if (entries && !readsAny(spec, tool, entries)) continue;
      const group = groupOf(spec);
      if (group !== 'Make smaller') {
        const option = {
          value: spec.mode, label: PLAIN_MODE_LABELS[spec.mode] ?? spec.label, hint: tool.label,
        };
        (group === 'Unpack' ? unpack : other).push(option);
        continue;
      }
      const format = spec.outputExt ? formatLabel(spec.outputExt) : PLAIN_MODE_LABELS[spec.mode];
      const key = perMode || !spec.outputExt ? spec.mode : spec.outputExt;
      const found = smaller.get(key);
      if (found) {
        if (!found.tools.includes(tool.label)) found.tools.push(tool.label);
      } else {
        smaller.set(key, {
          value: key,
          label: perMode ? `${format ?? spec.label}: ${spec.label}` : (format ?? spec.label),
          tools: [tool.label],
        });
      }
    }
  }
  const groups = [
    { group: 'Recommended', options: [{ ...BEST_FORMAT }] },
    {
      group: 'Make smaller',
      options: [...smaller.values()].map(({ tools, ...o }) => ({ ...o, hint: tools.join(', ') })),
    },
    { group: 'Unpack', options: unpack },
    { group: 'Other', options: other },
  ];
  return groups.filter((g) => g.options.length > 0);
}

/**
 * The mode specs a Convert-to value names: a mode id is that mode, a format
 * (`.chd`) every Make-smaller mode that writes it, and '' (Best) none.
 */
export function specsForTarget(value) {
  if (!value) return [];
  if (!value.startsWith('.')) {
    const spec = registry.specFor(value);
    return spec ? [spec] : [];
  }
  const found = [];
  for (const tool of registry.all()) {
    for (const spec of tool.modes ?? []) {
      if (groupOf(spec) === 'Make smaller' && spec.outputExt === value) found.push(spec);
    }
  }
  return found;
}

/** A file's format by its name ('Game.chd' -> 'CHD'), or null with no extension. */
export function formatOfPath(path) {
  const name = String(path ?? '').split('::').pop().split(/[\\/]/).pop().toLowerCase();
  const compound = Object.keys(FORMAT_LABELS).find((ext) => name.endsWith(ext));
  if (compound) return FORMAT_LABELS[compound];
  const dot = name.lastIndexOf('.');
  return dot > 0 ? formatLabel(name.slice(dot)) : null;
}

/**
 * The format a row would most likely become: the first Make-smaller option
 * that reads it, or null when nothing here makes it smaller. For the row's
 * "→ CHD" hint; the plan is what decides.
 */
export function likelyFormat(entry, ready = null) {
  const smaller = convertTargets({ entries: [entry], ready })
    .find((g) => g.group === 'Make smaller');
  return smaller?.options[0]?.label ?? null;
}

/** The label of a Convert-to value: a format (`.chd`), a mode id, or '' (Best). */
export function targetLabel(value) {
  if (!value) return BEST_FORMAT.label;
  if (value.startsWith('.')) return formatLabel(value);
  return PLAIN_MODE_LABELS[value] ?? registry.specFor(value)?.label ?? value;
}
