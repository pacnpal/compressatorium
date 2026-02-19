export const Z3DS_SOURCE_EXTENSIONS = ['.3ds', '.cci', '.cia'];
export const Z3DS_VERIFY_EXTENSIONS = ['.z3ds', '.zcci', '.zcia'];
export const Z3DS_INFO_EXTENSIONS = [...Z3DS_SOURCE_EXTENSIONS, ...Z3DS_VERIFY_EXTENSIONS];
export const Z3DS_OUTPUT_EXTENSION_BY_SOURCE = {
    '.3ds': '.z3ds',
    '.cci': '.zcci',
    '.cia': '.zcia'
};

export function getFileExtension(path) {
    if (typeof path !== 'string') return '';
    const lower = path.toLowerCase();
    const lastSlash = Math.max(lower.lastIndexOf('/'), lower.lastIndexOf('\\'));
    const filename = lastSlash >= 0 ? lower.slice(lastSlash + 1) : lower;
    const dot = filename.lastIndexOf('.');
    if (dot <= 0) return '';
    return filename.slice(dot);
}

export function is3dsSourceFile(path) {
    return Z3DS_SOURCE_EXTENSIONS.includes(getFileExtension(path));
}

export function is3dsVerifyFile(path) {
    return Z3DS_VERIFY_EXTENSIONS.includes(getFileExtension(path));
}

export function is3dsFile(path) {
    return Z3DS_INFO_EXTENSIONS.includes(getFileExtension(path));
}

export function get3dsProductPath(path) {
    if (typeof path !== 'string') return null;
    const ext = getFileExtension(path);
    const outExt = Z3DS_OUTPUT_EXTENSION_BY_SOURCE[ext];
    if (!outExt || !path.toLowerCase().endsWith(ext)) return null;
    return `${path.slice(0, -ext.length)}${outExt}`;
}

export function getDolphinProductPath(entry) {
    if (
        !entry
        || typeof entry.path !== 'string'
        || (!entry.has_rvz && !entry.dolphin_ready)
    ) return null;
    if (typeof entry.dolphin_path === 'string' && entry.dolphin_path) {
        return entry.dolphin_path;
    }
    const ext = getFileExtension(entry.path);
    if (!ext || !entry.path.toLowerCase().endsWith(ext)) return null;
    if (ext === '.iso') {
        return `${entry.path.slice(0, -ext.length)}.rvz`;
    }
    return entry.path;
}
