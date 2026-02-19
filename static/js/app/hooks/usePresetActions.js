import { api } from '../../api.js';
import { useCallback, useEffect } from '../runtime/preactRuntime.js';
import {
    CONVERSION_PRESETS_STORAGE_KEY,
    ISO_TOOL_STORAGE_KEY,
    MAX_CONVERSION_PRESETS,
} from '../constants/uiConstants.js';
import { makeConversionPresetId } from '../utils/conversionPresetUtils.js';
import { normalizeDolphinLevel } from '../utils/uiHelpers.js';

export function usePersistIsoHandling({ isoHandling }) {
    useEffect(() => {
        try {
            localStorage.setItem(ISO_TOOL_STORAGE_KEY, isoHandling);
        } catch (err) {
            // Ignore persistence failures (private mode, disabled storage).
        }
    }, [isoHandling]);
}

export function usePersistConversionPresets({ conversionPresets }) {
    useEffect(() => {
        try {
            localStorage.setItem(
                CONVERSION_PRESETS_STORAGE_KEY,
                JSON.stringify(conversionPresets),
            );
        } catch (err) {
            // Ignore persistence failures (private mode, disabled storage).
        }
    }, [conversionPresets]);
}

export function useConversionPresetActions({
    conversionPresets,
    setConversionPresets,
    selectedPresetId,
    setSelectedPresetId,
    isoHandling,
    setIsoHandling,
    conversionMode,
    setConversionMode,
    compressionSelection,
    setCompressionSelection,
    dolphinCompressionLevel,
    setDolphinCompressionLevel,
    outputDir,
    setOutputDir,
    deleteOnVerify,
    setDeleteOnVerify,
    notify,
}) {
    const applyConversionPreset = useCallback((presetId) => {
        if (!presetId) return;
        const preset = conversionPresets.find((item) => item.id === presetId);
        if (!preset) {
            notify('Selected preset no longer exists', 'warning');
            setSelectedPresetId('');
            return;
        }

        setIsoHandling(preset.isoHandling);
        setConversionMode(preset.conversionMode);
        setCompressionSelection(
            Array.isArray(preset.compressionSelection) && preset.compressionSelection.length
                ? [...preset.compressionSelection]
                : ['zlib'],
        );
        setDolphinCompressionLevel(normalizeDolphinLevel(preset.dolphinCompressionLevel));
        setOutputDir(preset.outputDir || '');
        setDeleteOnVerify(Boolean(preset.deleteOnVerify));
        setSelectedPresetId(preset.id);

        notify(`Applied preset: ${preset.name}`, 'success');
        api.trackFeatureEvent('conversion_preset_applied').catch(() => {});
    }, [conversionPresets, notify]);

    const handlePresetSave = useCallback(() => {
        const rawName = window.prompt('Preset name');
        if (rawName == null) return;
        const name = rawName.trim();
        if (!name) {
            notify('Preset name is required', 'warning');
            return;
        }

        const payload = {
            isoHandling: isoHandling === 'dolphin' || isoHandling === 'z3ds' ? isoHandling : 'chdman',
            conversionMode,
            compressionSelection: [...compressionSelection],
            dolphinCompressionLevel: normalizeDolphinLevel(dolphinCompressionLevel),
            outputDir: outputDir || '',
            deleteOnVerify: Boolean(deleteOnVerify),
            updatedAt: new Date().toISOString(),
        };

        const existing = conversionPresets.find(
            (preset) => preset.name.toLowerCase() === name.toLowerCase(),
        );

        if (existing) {
            setConversionPresets((prev) => prev.map((preset) => (
                preset.id === existing.id
                    ? { ...preset, name, ...payload }
                    : preset
            )));
            setSelectedPresetId(existing.id);
            notify(`Updated preset: ${name}`, 'success');
        } else {
            const id = makeConversionPresetId();
            const nextPreset = { id, name, ...payload };
            setConversionPresets((prev) => [nextPreset, ...prev].slice(0, MAX_CONVERSION_PRESETS));
            setSelectedPresetId(id);
            notify(`Saved preset: ${name}`, 'success');
        }

        api.trackFeatureEvent('conversion_preset_saved').catch(() => {});
    }, [
        conversionPresets,
        conversionMode,
        compressionSelection,
        deleteOnVerify,
        dolphinCompressionLevel,
        isoHandling,
        notify,
        outputDir,
    ]);

    const handlePresetDelete = useCallback(() => {
        if (!selectedPresetId) {
            notify('Select a preset to delete', 'info');
            return;
        }

        const preset = conversionPresets.find((item) => item.id === selectedPresetId);
        if (!preset) {
            setSelectedPresetId('');
            notify('Selected preset no longer exists', 'warning');
            return;
        }

        if (!window.confirm(`Delete preset "${preset.name}"?`)) {
            return;
        }

        setConversionPresets((prev) => prev.filter((item) => item.id !== preset.id));
        setSelectedPresetId('');
        notify(`Deleted preset: ${preset.name}`, 'info');
    }, [conversionPresets, notify, selectedPresetId]);

    return {
        applyConversionPreset,
        handlePresetSave,
        handlePresetDelete,
    };
}
