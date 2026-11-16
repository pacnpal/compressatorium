// The level a codec+level control holds (Dolphin RVZ/WIA, nsz). Shared by the
// conversion store's picker and the Rule editor, so both keep a level the
// backend takes. `range` is `{ min, max, default }`, from
// `registry.levelRangeFor(mode, codec)`. No imports: the store's node harness
// loads this file as it ships.

/**
 * The level after a mode or codec change: kept when `range` takes it, else the
 * codec's default (lzma2 after zstd at 19 lands on 5, not 9).
 */
export function fitLevel(range, level) {
  const n = Number.parseInt(level, 10);
  return n >= range.min && n <= range.max ? n : (range.default ?? range.min);
}

/**
 * A level typed into the box, pulled to the nearest end of `range` (19 on
 * lzma2 is 9). Null for a blank or non-number entry, which the caller keeps
 * as typed (an empty box mid-edit) or reads as "use the default".
 */
export function clampLevel(range, level) {
  const n = Number.parseInt(level, 10);
  if (!Number.isFinite(n)) return null;
  return Math.min(Math.max(n, range.min), range.max);
}
