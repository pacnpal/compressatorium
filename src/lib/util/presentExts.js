// The type filter's choices (#597): the registry's extensions that the open
// folder actually has, in the registry's order. Pure, for the Node tests.

function lowerName(row) {
  return String(row?.name ?? row?.path ?? '').toLowerCase();
}

function collect(rows, names) {
  for (const row of rows ?? []) {
    // A folder's name says nothing about what it holds.
    if (row?.type !== 'directory') names.push(lowerName(row));
    collect(row?.parts, names);
  }
  return names;
}

/**
 * @param {object[]} rows the folder's rows, with the files folded under them
 * @param {string[]} all every extension the registry can filter on
 * @param {string|null} [picked] the filter in use, kept so it can be cleared
 */
export function presentExts(rows, all, picked = null) {
  const names = collect(rows, []);
  // Each file counts under its longest matching extension (.nkit.iso, not .iso).
  const longestFirst = [...(all ?? [])].sort((a, b) => b.length - a.length);
  const found = new Set();
  for (const name of names) {
    const hit = longestFirst.find((ext) => name.endsWith(ext.toLowerCase()));
    if (hit) found.add(hit);
  }
  if (picked) found.add(picked);
  return (all ?? []).filter((ext) => found.has(ext));
}
