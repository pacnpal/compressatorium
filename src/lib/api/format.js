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
