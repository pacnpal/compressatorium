// What the File info modal shows for a path: the first candidate tool's
// getInfo() that answers, else the basics the listing already knew.
// Pure (no DOM, no Svelte), so it runs under Node in the tests.

import { formatSize } from '../api/format.js';

// Whatever the directory listing already knows about the file. Used as
// a last resort so the modal always shows something rather than just an
// error when no tool can parse the file.
export function basicInfo(entry) {
  if (!entry) return {};
  const out = {};
  if (entry.name) out.name = entry.name;
  if (entry.path) out.path = entry.path;
  if (entry.type) out.type = entry.type;
  if (typeof entry.size === 'number') out.size = formatSize(entry.size);
  if (entry.extension) out.extension = entry.extension;
  if (entry.media_type) out.media_type = entry.media_type;
  return out;
}

// The note shown over the basics when a tool ran and couldn't read the file.
export const NO_TOOL_READ_NOTE = 'No tool could read this file, showing the basics.';

// A tool's info route answers 503 when the tool can't run here; for a tool that
// isn't ready the detail is the server's not_ready_message ("<Display name>
// isn't ready: <reason>"). fetchJson puts the status on err.status and the
// detail in err.message, so this reads the same for every tool.
const UNAVAILABLE = 503;

// Walk the candidate tools and keep the first getInfo() that returns.
// getInfo is the registry binding (every tool: api.getToolInfo(id, path),
// GET /api/info/{tool}), no per-tool
// branches here. If none answers, fall back to the basics the listing already
// knew; `fellBack` flags that for the UI and `notes` says why, one line each:
// today's line when a tool ran and couldn't read the file, and the server's
// own reason for each tool that couldn't run (#486). A later candidate that
// answers wins and `notes` is empty.
export async function loadInfo(tools, path, entry) {
  const unavailable = [];
  let readFailed = false;
  for (const t of tools) {
    try {
      return { info: await t.getInfo(path), fellBack: false, notes: [] };
    } catch (e) {
      if (e?.status === UNAVAILABLE && e.message) unavailable.push(e.message);
      else readFailed = true; // this tool can't read the file; try the next
    }
  }
  const basics = basicInfo(entry);
  if (!Object.keys(basics).length) {
    throw new Error(
      unavailable.length ? unavailable.join(' ') : 'No info available for this file.',
    );
  }
  const head = readFailed || !unavailable.length ? NO_TOOL_READ_NOTE : 'Showing the basics.';
  return { info: basics, fellBack: true, notes: [head, ...unavailable] };
}

// Short words a tool's info keys use that read as initials, not words.
const INITIALS = new Set([
  'cd', 'chd', 'cia', 'crc', 'crc32', 'cso', 'dat', 'dvd', 'gc', 'hd', 'id', 'iso',
  'md5', 'nsp', 'nsz', 'ps3', 'rom', 'rvz', 'sha1', 'tmd', 'wbfs', 'wia', 'wud', 'wux',
]);

/** A tool's info key as a label: `hunk_size` -> "Hunk size", `game_id` -> "Game ID". */
export function infoLabel(key) {
  const words = String(key).split(/[_\s]+/).filter(Boolean)
    .map((w) => (INITIALS.has(w.toLowerCase()) ? w.toUpperCase() : w.toLowerCase()));
  if (!words.length) return String(key);
  words[0] = words[0][0].toUpperCase() + words[0].slice(1);
  return words.join(' ');
}

function isEmpty(v) {
  return v == null || v === ''
    || (Array.isArray(v) && v.length === 0)
    || (typeof v === 'object' && !Array.isArray(v) && Object.keys(v).length === 0);
}

// A boolean reads as a word.
function plain(value) {
  if (value === true) return 'Yes';
  if (value === false) return 'No';
  return value;
}

/**
 * Split a tool's info into what the modal shows: `rows`, the plain facts with
 * readable labels, and `details`, a tool's raw output and nested data, kept
 * behind a fold. Empty fields are dropped, and so is any field that only
 * repeats the file's own *path* (the modal's heading names the file).
 * Booleans read Yes/No. A field with a `<key>_display` twin shows once, as
 * the readable twin under the plain key's label (#591). `omit` leaves out the
 * keys the Info card already shows, with their twins.
 */
export function infoRows(info, path = null, { omit = [] } = {}) {
  const rows = [];
  const details = [];
  if (!info || typeof info !== 'object') return { rows, details };
  const skip = new Set(omit);
  for (const [key, value] of Object.entries(info)) {
    const twinOf = key.endsWith('_display') ? key.slice(0, -'_display'.length) : null;
    const base = twinOf ?? key;
    if (skip.has(base)) continue;
    // The twin stands in for its plain key wherever that key is there too.
    if (twinOf && !isEmpty(info[twinOf])) continue;
    const twin = twinOf ? null : info[`${key}_display`];
    const shown = isEmpty(twin) ? value : twin;
    if (isEmpty(shown) || (path && shown === path)) continue;
    const row = { key: base, label: infoLabel(base), value: plain(shown) };
    const raw = /^raw(_|$)/i.test(key) || typeof shown === 'object';
    (raw ? details : rows).push(row);
  }
  return { rows, details };
}

// The info keys that name the game, and its console, richest first.
const GAME_KEYS = ['title', 'game_title', 'game_name'];
const CONSOLE_KEYS = ['platform', 'console'];

function firstOf(info, keys) {
  for (const key of keys) {
    const value = info?.[key];
    if (!isEmpty(value) && typeof value !== 'object') return { key, value };
  }
  return null;
}

/**
 * The short card Info opens on (#591): the game, its console, its format,
 * whether it is in its best format (`best`, a sentence the modal makes from
 * the row and the registry, or null to leave it out), and its size, once.
 * A DAT or Hasheous `match` names the game and console before the tool does.
 * `used` lists the info keys the card took, for `infoRows`' `omit`.
 */
export function infoCard({ info = {}, match = null, format = null, best = null } = {}) {
  const matched = match?.matched ? match : null;
  const game = firstOf(info, GAME_KEYS);
  const consoleName = firstOf(info, CONSOLE_KEYS);
  const rows = [
    cardRow('game', 'Game', matched?.game_name || game?.value),
    cardRow('console', 'Console', matched?.platform || consoleName?.value),
    // The reader's own format ("NKit v01 (GameCube)") before the file name's.
    cardRow('format', 'Format', isEmpty(info?.format) ? format : info.format),
    cardRow('best', 'In its best format', best),
    cardRow('size', 'Size', cardSize(info)),
  ].filter(Boolean);
  const used = ['format', 'size', 'file', 'name', 'path', 'extension', 'type'];
  if (game) used.push(game.key);
  if (consoleName) used.push(consoleName.key);
  return { rows, used };
}

// One card row, or null when there is nothing to say.
function cardRow(key, label, value) {
  return isEmpty(value) ? null : { key, label, value: plain(value) };
}

// The size once: the tool's readable twin, else the bytes made readable.
function cardSize(info) {
  if (!isEmpty(info?.size_display)) return info.size_display;
  return typeof info?.size === 'number' ? formatSize(info.size) : info?.size;
}

/** Why Info is off for a row, for the menu item: "no reader for .dosz". */
export function infoUnavailable(entry, { inArchive = false } = {}) {
  if (inArchive) return 'not inside an archive';
  if (entry?.type === 'directory') return 'not for folders';
  const ext = entry?.extension || /\.[^./\\]+$/.exec(String(entry?.name ?? ''))?.[0];
  return ext ? `no reader for ${ext.toLowerCase()}` : 'no reader for this file';
}
