// File-type → Lucide icon mapping. Re-used by FileRow, JobRow, info modals,
// and anywhere else a single file/entry needs a visual affordance.
//
// Returns a Svelte component reference. Use as:
//   <script>
//     import { iconForEntry } from '$lib/util/fileIcon.js';
//     const Icon = $derived(iconForEntry(entry));
//   </script>
//   <Icon size={16} />

import Folder from '@lucide/svelte/icons/folder';
import Archive from '@lucide/svelte/icons/archive';
import Disc3 from '@lucide/svelte/icons/disc-3';
import Disc from '@lucide/svelte/icons/disc';
import Gamepad2 from '@lucide/svelte/icons/gamepad-2';
import File from '@lucide/svelte/icons/file';

const GAME_EXTS = new Set([
  '.rvz', '.wia', '.gcz', '.wbfs',
  '.3ds', '.cci', '.cia',
  '.z3ds', '.zcci', '.zcia',
]);
const DISC_EXTS = new Set(['.iso', '.gdi', '.cue', '.bin', '.cso', '.zso', '.dax']);

/**
 * @param {{ type?: string, extension?: string } | null | undefined} entry
 */
export function iconForEntry(entry) {
  if (!entry) return File;
  if (entry.type === 'directory') return Folder;
  if (entry.type === 'archive') return Archive;
  const ext = entry.extension?.toLowerCase() ?? '';
  if (ext === '.chd') return Disc3;
  if (GAME_EXTS.has(ext)) return Gamepad2;
  if (DISC_EXTS.has(ext)) return Disc;
  return File;
}
