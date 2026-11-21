// Folder paths as the folder picker walks them. A Drive can be `/` itself, so
// the root keeps its slash and every join and containment check knows it.
// Pure, so the Node tests load it as it ships.

/** *p* without trailing slashes; `/` stays `/`. */
export function trimPath(p) {
  const s = String(p ?? '');
  const t = s.replace(/\/+$/, '');
  return t === '' && s.startsWith('/') ? '/' : t;
}

/** True when *p* is *root* or inside it. */
export function isWithin(p, root) {
  const r = trimPath(root);
  const q = trimPath(p);
  if (r === '/') return q.startsWith('/');
  return q === r || q.startsWith(`${r}/`);
}

/** *folder* under *parent*. */
export function joinPath(parent, folder) {
  const r = trimPath(parent);
  return r === '/' ? `/${folder}` : `${r}/${folder}`;
}

/** The folder above *p*; `/` for a top-level folder and for `/` itself. */
export function parentPath(p) {
  const q = trimPath(p);
  const cut = q.lastIndexOf('/');
  return cut <= 0 ? '/' : q.slice(0, cut);
}

/** `[{ label, path }]` from *drive*'s top folder down to *path*. */
export function crumbsFor(drive, path) {
  const root = trimPath(drive.path);
  const out = [{ label: drive.name || root, path: root }];
  const rest = root === '/' ? path : path.slice(root.length);
  let at = root;
  for (const part of String(rest).split('/').filter(Boolean)) {
    at = joinPath(at, part);
    out.push({ label: part, path: at });
  }
  return out;
}
