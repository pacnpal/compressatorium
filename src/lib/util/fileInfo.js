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
// getInfo is the registry binding (chdman: getCHDInfo, dolphin:
// getDolphinInfo, rom-converto: getRomConvertoInfo, ...), no per-tool
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
