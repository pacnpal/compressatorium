// A folder as people read it: "{drive} › {path inside it}", from the Drives
// (`/api/volumes`) the app knows. A folder on no known Drive shows as it is.

function trimmed(p) {
  return String(p ?? '').replace(/\/+$/, '');
}

export function drivePath(path, volumes = []) {
  const p = trimmed(path);
  if (!p) return '';
  const drive = [...(volumes ?? [])]
    .filter((v) => v?.path && (p === trimmed(v.path) || p.startsWith(`${trimmed(v.path)}/`)))
    .sort((a, b) => trimmed(b.path).length - trimmed(a.path).length)[0];
  if (!drive) return p;
  const name = drive.name || trimmed(drive.path);
  const rest = p.slice(trimmed(drive.path).length).replace(/^\/+/, '').split('/').filter(Boolean);
  return [name, ...rest].join(' › ');
}
