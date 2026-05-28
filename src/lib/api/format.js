// Pure format helpers (no DOM, no network).

const SIZE_UNITS = ['B', 'KB', 'MB', 'GB', 'TB'];

export function formatSize(bytes) {
  if (bytes == null) return '';
  if (bytes === 0) return '0 B';
  const k = 1024;
  const i = Math.max(
    0,
    Math.min(Math.floor(Math.log(bytes) / Math.log(k)), SIZE_UNITS.length - 1),
  );
  return `${Number.parseFloat((bytes / k ** i).toFixed(2))} ${SIZE_UNITS.at(i)}`;
}

export function getFileIcon(entry) {
  if (!entry) return '📄';
  if (entry.type === 'directory') return '📁';
  if (entry.type === 'archive') return '📦';
  const ext = entry.extension?.toLowerCase();
  if (ext === '.chd') return '💿';
  if (['.rvz', '.wia', '.gcz', '.wbfs', '.3ds', '.cci', '.cia', '.z3ds', '.zcci', '.zcia'].includes(ext)) {
    return '🎮';
  }
  if (['.iso', '.gdi', '.cue', '.bin'].includes(ext)) return '💽';
  return '📄';
}

export const DOLPHIN_EXTENSIONS = ['.rvz', '.wia', '.gcz', '.wbfs'];

export function isDolphinFile(path) {
  if (!path) return false;
  const ext = path.split('.').pop();
  if (!ext) return false;
  return DOLPHIN_EXTENSIONS.includes(`.${ext.toLowerCase()}`);
}
