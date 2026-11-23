// Pick console's best guess (#597): the console a game's folder is named for
// ("PlayStation", "psx"), else the one the folder's other games were
// recognized as. Null when neither says. Pure, for the Node tests.

function folderOf(path) {
  const text = String(path ?? '').split('::')[0];
  const cut = Math.max(text.lastIndexOf('/'), text.lastIndexOf('\\'));
  return cut < 0 ? '' : text.slice(0, cut);
}

function plainWord(text) {
  return String(text ?? '').toLowerCase().replace(/[^a-z0-9]/g, '');
}

// The deepest folder on the path named for a console, by slug or by name.
function byFolderName(paths, platforms) {
  const known = {};
  for (const p of platforms ?? []) {
    known[plainWord(p.slug)] = p.slug;
    known[plainWord(p.name)] = p.slug;
  }
  for (const path of paths) {
    const segments = folderOf(path).split(/[\\/]/).reverse();
    const hit = segments.map(plainWord).find((word) => word && known[word]);
    if (hit) return known[hit];
  }
  return null;
}

// The console most of the same folders' other games were recognized as.
function byNeighbours(paths, plan) {
  const folders = new Set(paths.map(folderOf));
  const votes = {};
  for (const row of plan ?? []) {
    if (!row?.platform || !folders.has(folderOf(row.file_path))) continue;
    votes[row.platform] = (votes[row.platform] ?? 0) + 1;
  }
  const ranked = Object.entries(votes).sort((a, b) => b[1] - a[1]);
  return ranked[0]?.[0] ?? null;
}

/**
 * @param {{ paths: string[], plan?: object[], platforms?: {slug: string, name: string}[] }} args
 * @returns {string|null} a platform slug
 */
export function guessConsole({ paths = [], plan = [], platforms = [] } = {}) {
  return byFolderName(paths, platforms) ?? byNeighbours(paths, plan);
}
