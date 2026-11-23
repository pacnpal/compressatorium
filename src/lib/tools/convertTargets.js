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
    const path = String(entry.path ?? entry.name ?? '');
    // A file inside an archive, for a mode that can't take one (Copy).
    if (path.includes('::') && !spec.allowsArchiveInput) return false;
    // A content-refined tool (Xbox) only where the backend's probe claimed
    // the file; with no claims at all, the extension decides.
    if (tool.contentRefined && Array.isArray(entry.convertible_by)
      && !entry.convertible_by.includes(tool.id)) return false;
    const name = path.split('::').pop().toLowerCase();
    return exts.some((ext) => name.endsWith(ext));
  });
}

// The kinds that make a new file of the game (not an unpack or a copy).
const MAKES_NEW = new Set(['create', 'compress']);

// A mode Auto leaves alone that still makes a new file of the game: what a
// skipped line may offer in one click.
function isManual(spec) {
  return spec.autoTarget === false && MAKES_NEW.has(spec.kind);
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
 *
 * Every value is one `/api/jobs/auto-plan` can resolve (#598). The server
 * takes a format only to Auto-eligible modes (`format_candidates` skips
 * `auto_target=False`) and a mode id to itself, so a format is listed only
 * where an Auto-eligible mode writes it. A mode Auto leaves alone is listed
 * by its mode id, and marked `manual: true` (the one-click offer's source),
 * unless an Auto-eligible mode here already makes its format.
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
      const manual = isManual(spec);
      if (group !== 'Make smaller') {
        const option = {
          value: spec.mode, label: PLAIN_MODE_LABELS[spec.mode] ?? spec.label, hint: tool.label,
          ...(manual ? { manual } : {}),
        };
        (group === 'Unpack' ? unpack : other).push(option);
        continue;
      }
      const format = spec.outputExt ? formatLabel(spec.outputExt) : PLAIN_MODE_LABELS[spec.mode];
      // A format only for an Auto-eligible mode: the server resolves no other.
      const byFormat = !perMode && spec.outputExt && spec.autoTarget !== false;
      const key = byFormat ? spec.outputExt : spec.mode;
      const found = smaller.get(key);
      if (found) {
        if (!found.tools.includes(tool.label)) found.tools.push(tool.label);
      } else {
        smaller.set(key, {
          value: key,
          label: perMode ? `${format ?? spec.label}: ${spec.label}` : (format ?? spec.label),
          tools: [tool.label],
          ext: spec.outputExt,
          manual,
        });
      }
    }
  }
  // A mode Auto leaves alone whose format an Auto-eligible mode here already
  // makes adds nothing the format option doesn't (createraw beside CHD).
  if (!perMode) {
    for (const [key, o] of [...smaller]) {
      if (o.manual && o.ext && key !== o.ext && smaller.has(o.ext)) smaller.delete(key);
    }
  }
  const groups = [
    { group: 'Recommended', options: [{ ...BEST_FORMAT }] },
    {
      group: 'Make smaller',
      options: [...smaller.values()].map(({ tools, ext: _ext, manual, ...o }) => ({
        ...o, hint: tools.join(', '), ...(manual ? { manual } : {}),
      })),
    },
    { group: 'Unpack', options: unpack },
    { group: 'Other', options: other },
  ];
  return groups.filter((g) => g.options.length > 0);
}

/** The picker's `manual` options for one entry. */
function manualOptions(entry, ready) {
  return convertTargets({ entries: [entry], ready })
    .flatMap((g) => g.options).filter((o) => o.manual);
}

/**
 * Modes Auto leaves alone that could still take every one of *entries*
 * (#597): `[{ value, label }]`, for a skipped line's one-click "Convert to".
 * Drawn from the picker itself (`convertTargets`' `manual` options, #598), so
 * an offer is always a value the picker lists and the server resolves. By the
 * registry's `autoTarget` flag alone (the eXoDOS repack is one), so no tool is
 * named here. `ready`: the tool ids that can run here.
 */
export function offeredInstead(entries, ready = null) {
  const list = (entries ?? []).filter(Boolean);
  if (!list.length) return [];
  const perEntry = [];
  for (const entry of list) perEntry.push(manualOptions(entry, ready));
  const [first, ...rest] = perEntry;
  return first
    .filter((o) => rest.every((opts) => opts.some((x) => x.value === o.value)))
    .map(({ value, label }) => ({ value, label }));
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
  const name = String(path ?? '').split('::').pop().split(/[\\/]/).pop().toLowerCase()
    // A split build's first part (`Game.iso.0`) is the image itself.
    .replace(/(\.[a-z0-9]+)\.\d+$/, '$1');
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
  const best = smaller?.options[0]?.label ?? null;
  // Already in it: Auto skips this as "Already RVZ", though the tool could
  // recompress it, so there is nothing to become.
  return best && best === formatOfPath(entry?.path ?? entry?.name) ? null : best;
}

/**
 * What a converter that can't run here would have made of *paths* (#591):
 * the format of the Convert-to `target` the user picked, when that converter
 * makes it; its first Make-smaller format for them under Best (''); null when
 * the tool isn't known or the picked target is another tool's, so the line
 * never names a format the user didn't ask for.
 */
export function missingFormat({ tool: label, paths = [], target = '' }) {
  const tool = registry.all().find((t) => t.label === label);
  if (!tool) return null;
  if (target) {
    const spec = specsForTarget(target).find((s) => tool.modes.includes(s));
    if (!spec) return null;
    return spec.outputExt ? formatLabel(spec.outputExt) : (PLAIN_MODE_LABELS[spec.mode] ?? null);
  }
  const entries = paths.map((path) => ({ path, type: 'file' }));
  const smaller = convertTargets({ entries, toolIds: [tool.id] })
    .find((g) => g.group === 'Make smaller');
  return smaller?.options[0]?.label ?? null;
}

/** The label of a Convert-to value: a format (`.chd`), a mode id, or '' (Best). */
export function targetLabel(value) {
  if (!value) return BEST_FORMAT.label;
  if (value.startsWith('.')) return formatLabel(value);
  return PLAIN_MODE_LABELS[value] ?? registry.specFor(value)?.label ?? value;
}
