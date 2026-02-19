export function buildCompressionValue(selection, options) {
    if (selection.includes('none')) return 'none';
    const ordered = options
        .filter((opt) => opt.value !== 'none' && selection.includes(opt.value))
        .map((opt) => opt.value);
    return ordered.length ? ordered.join(',') : null;
}

export function cloneSelectionMap(selection) {
    if (!(selection instanceof Map)) return new Map();
    return new Map(selection);
}
