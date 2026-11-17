// Pure rules for the Patch dialog (PatchModal.svelte, #428). The server checks
// all of this again; these only decide what the UI offers and promises.

const ARCHIVES = ['.zip', '.7z'];

function lower(s) {
  return typeof s === 'string' ? s.toLowerCase() : '';
}

function endsWithAny(path, exts) {
  const p = lower(path);
  return exts.some((e) => p.endsWith(e));
}

/** The file name of a server path (POSIX: only `/` separates folders). */
function baseName(path) {
  return path.slice(path.lastIndexOf('/') + 1);
}

/** Whether a name is an IPS or BPS patch, by its extension. */
export function isPatchFile(name) {
  return endsWithAny(name, ['.ips', '.bps']);
}

/**
 * Whether the Patch action is offered for a file-list row: a loose file, a
 * .zip/.7z, or a member of one, and never a folder or a disc Image.
 * `isDisc(path)` is the registry's disc check (`registry.discImages`), the
 * mirror of the backend's `disc_extensions()`.
 */
export function canPatch(entry, isDisc) {
  if (!entry || typeof entry.path !== 'string') return false;
  if (entry.type === 'directory') return false;
  const path = entry.path;
  if (path.includes('::')) {
    const [archive, member] = path.split('::', 2);
    return endsWithAny(archive, ARCHIVES) && !isDisc(member);
  }
  if (entry.type === 'archive') return endsWithAny(path, ARCHIVES);
  return !isDisc(path);
}

/**
 * The output the backend writes for a loose ROM or a named member:
 * `<ROM stem> (Patched).7z` beside it (numbered if taken). A whole archive is
 * named after the ROM inside it, which only the server reads: null.
 */
export function patchedName(path) {
  const member = path.includes('::') ? path.split('::', 2)[1] : null;
  if (!member && endsWithAny(path, ARCHIVES)) return null;
  const name = baseName(member ?? path);
  const dot = name.lastIndexOf('.');
  const stem = dot > 0 ? name.slice(0, dot) : name;
  return `${stem} (Patched).7z`;
}

/** The POST /api/patch body: the Image and exactly one patch source. */
export function patchRequest(imagePath, { source, patchPath, uploadId }) {
  if (source === 'upload') return { image_path: imagePath, upload_id: uploadId };
  return { image_path: imagePath, patch_path: (patchPath ?? '').trim() };
}
