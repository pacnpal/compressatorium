import {
    CONVERSION_PRESETS_STORAGE_KEY,
    DEFAULT_DOLPHIN_COMPRESSION_LEVEL,
    MAX_CONVERSION_PRESETS,
} from '../constants/uiConstants.js';

function normalizeStoredDolphinLevel(value) {
    const raw = `${value ?? ''}`.trim();
    if (!raw) return DEFAULT_DOLPHIN_COMPRESSION_LEVEL;
    if (!/^\d+$/.test(raw)) return DEFAULT_DOLPHIN_COMPRESSION_LEVEL;
    return raw;
}

export const loadStoredConversionPresets = () => {
    try {
        const raw = localStorage.getItem(CONVERSION_PRESETS_STORAGE_KEY);
        if (!raw) return [];
        const parsed = JSON.parse(raw);
        if (!Array.isArray(parsed)) return [];
        const validIsoHandling = new Set(['chdman', 'dolphin', 'z3ds']);
        const validPresets = [];
        for (const candidate of parsed) {
            if (!candidate || typeof candidate !== 'object') continue;
            if (typeof candidate.id !== 'string' || !candidate.id.trim()) continue;
            if (typeof candidate.name !== 'string' || !candidate.name.trim()) continue;
            if (typeof candidate.conversionMode !== 'string' || !candidate.conversionMode.trim()) continue;
            if (!validIsoHandling.has(candidate.isoHandling)) continue;
            const compressionSelection = Array.isArray(candidate.compressionSelection)
                ? candidate.compressionSelection.filter((value) => typeof value === 'string' && value.trim())
                : ['zlib'];
            validPresets.push({
                id: candidate.id,
                name: candidate.name.trim(),
                isoHandling: candidate.isoHandling,
                conversionMode: candidate.conversionMode,
                compressionSelection: compressionSelection.length ? compressionSelection : ['zlib'],
                dolphinCompressionLevel: normalizeStoredDolphinLevel(candidate.dolphinCompressionLevel),
                outputDir: typeof candidate.outputDir === 'string' ? candidate.outputDir : '',
                deleteOnVerify: Boolean(candidate.deleteOnVerify),
                updatedAt: typeof candidate.updatedAt === 'string' ? candidate.updatedAt : ''
            });
        }
        return validPresets.slice(0, MAX_CONVERSION_PRESETS);
    } catch (err) {
        return [];
    }
};

export const makeConversionPresetId = () => `preset-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
