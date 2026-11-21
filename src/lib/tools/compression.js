// The compression a job runs with, in one shape for every picker:
// `{ codecs: string[], level: number|null }`. The convert panel's remembered
// per-tool preference, a Rule's two fields and the controlled CompressionPicker
// all go through here, so the wire format ("zlib,lzma", "zstd:19", "none", or
// null for the tool's own default) is written and read in one place.
//
// Pure apart from the registry (and its level helpers): the Node tests load it
// with the registry inlined.

import { registry } from './registry.js';
import { clampLevel, fitLevel } from './levels.js';

function takesCompression(mode) {
  const spec = registry.specFor(mode);
  return !!(spec?.supportsCompression || spec?.supportsCompressionLevel);
}

/** What a picker starts on for *mode*: its default codecs and their level. */
export function defaultCompression(mode) {
  const codecs = [...(registry.defaultCompressionForMode(mode) ?? [])];
  return { codecs, level: registry.levelRangeFor(mode, codecs[0])?.default ?? null };
}

/**
 * The one codec a single-codec mode (Dolphin, nsz, CSO) runs: the first one
 * chosen, unless *mode* doesn't take it (zstd on WIA), then the mode's own.
 */
export function effectiveCodec(mode, value) {
  const codec = (value?.codecs ?? []).find((c) => c && c !== 'none') ?? 'none';
  const allowed = registry.codecsForMode(mode).map((c) => c.value);
  if (allowed.length && !allowed.includes(codec)) {
    return registry.defaultCompressionForMode(mode)?.[0] ?? 'none';
  }
  return codec;
}

/** The level range of the codec *value* runs with, or null for none. */
export function levelRange(mode, value) {
  return registry.levelRangeFor(mode, effectiveCodec(mode, value));
}

/**
 * The wire string for *value*: null when *mode* takes no compression or
 * nothing it offers was chosen (the tool's own default), 'none' for none.
 */
export function formatCompression(mode, value) {
  if (!takesCompression(mode)) return null;
  const codecs = value?.codecs ?? [];
  const chosen = codecs.filter((c) => c && c !== 'none');
  if (codecs.includes('none') && chosen.length === 0) return 'none';
  if (registry.specFor(mode).supportsCompressionLevel) {
    const codec = effectiveCodec(mode, value);
    if (codec === 'none') return 'none';
    const range = registry.levelRangeFor(mode, codec);
    return range ? `${codec}:${fitLevel(range, value?.level)}` : codec;
  }
  // chdman's list is kept per tool: send only the codecs this mode takes (#471).
  const taken = registry.offeredCodecs(mode, chosen);
  return taken.length ? taken.join(',') : null;
}

/** The value a wire string stands for; blank or null is the mode's default. */
export function parseCompression(mode, wire) {
  if (wire == null || wire === '') return defaultCompression(mode);
  if (wire === 'none') return { codecs: ['none'], level: null };
  const [head, lvl] = String(wire).split(':');
  const codecs = head.split(',').map((c) => c.trim()).filter(Boolean);
  const n = Number.parseInt(lvl, 10);
  if (Number.isFinite(n)) return { codecs, level: n };
  return { codecs, level: registry.levelRangeFor(mode, codecs[0])?.default ?? null };
}

/** True when *value* sends what the mode's default would. */
export function isDefault(mode, value) {
  return formatCompression(mode, value) === formatCompression(mode, defaultCompression(mode));
}

/** A Rule's `{ compression, compression_level }` as a value. */
export function fromRule(rule) {
  const value = parseCompression(rule?.mode, rule?.compression);
  const n = Number.parseInt(rule?.compression_level, 10);
  return Number.isFinite(n) ? { ...value, level: n } : value;
}

/** *value* as a Rule's `{ compression, compression_level }`. */
export function toRule(mode, value) {
  const wire = formatCompression(mode, value);
  if (wire == null) return { compression: null, compression_level: null };
  const [codec, lvl] = wire.split(':');
  return { compression: codec, compression_level: lvl ? Number(lvl) : null };
}

/**
 * *value* with *codec* toggled in a multi-codec list. None clears the rest;
 * a codec the mode doesn't offer is dropped; past the tool's cap it is a no-op.
 */
export function toggleCodec(mode, value, codec) {
  if (codec === 'none') return { ...value, codecs: ['none'] };
  const current = registry.offeredCodecs(mode, value?.codecs ?? []).filter((c) => c !== 'none');
  if (current.includes(codec)) return { ...value, codecs: current.filter((c) => c !== codec) };
  if (current.length >= registry.maxCodecsFor(mode)) return value;
  return { ...value, codecs: [...current, codec] };
}

/** *value* on one codec, its level kept when that codec takes it. */
export function withCodec(mode, value, codec) {
  const range = registry.levelRangeFor(mode, codec);
  return { codecs: codec ? [codec] : [], level: range ? fitLevel(range, value?.level) : null };
}

/** *value* at *level*, pulled into its codec's range; blank stays blank. */
export function withLevel(mode, value, level) {
  const range = levelRange(mode, value);
  return { ...value, level: range ? clampLevel(range, level) : null };
}
