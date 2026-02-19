import { api } from '../../api.js';
import { useCallback } from '../runtime/preactRuntime.js';
import {
    AUTO_QUEUE_CAP_PROMPT_THRESHOLD,
    AUTO_QUEUE_RECOMMENDED_CAP,
} from '../constants/uiConstants.js';

export function useSearchQueueActions({
    currentPath,
    searchMode,
    autoQueueing,
    converting,
    autoQueuePrompt,
    notify,
    capturePreSearchView,
    setLoading,
    setEntriesError,
    setSearchResults,
    setSearchMode,
    setCurrentPage,
    setEntries,
    setLastSelectedIndex,
    startConversionSafely,
    canSelectEntry,
    setAutoQueuePrompt,
    setAutoQueueing,
    setChdMetadata,
    setForceRescanRunning,
    forceRescanRunning,
}) {
    const buildSearchEntries = useCallback((results) => {
        if (!results) return [];
        const files = Array.isArray(results.files) ? results.files : [];
        const archives = Array.isArray(results.archives) ? results.archives : [];

        return [
            ...files.map((file) => ({
                ...file,
                type: 'file',
                convertible: Boolean(file.convertible),
                dolphin_convertible: Boolean(file.dolphin_convertible),
                has_rvz: Boolean(file.has_rvz),
                dolphin_ready: Boolean(file.dolphin_ready),
                dolphin_path: file.dolphin_path || null,
                z3ds_convertible: Boolean(file.z3ds_convertible),
                has_z3ds: Boolean(file.has_z3ds),
                z3ds_ready: Boolean(file.z3ds_ready),
                z3ds_path: file.z3ds_path || null,
                chd_ready: Boolean(file.chd_ready),
            })),
            ...archives.map((archive) => ({
                ...archive,
                name: `${archive.name} (in ${archive.archive_path.split('/').pop()})`,
                type: 'file',
                convertible: Boolean(archive.convertible),
                dolphin_convertible: Boolean(archive.dolphin_convertible),
                has_rvz: false,
                dolphin_ready: false,
                dolphin_path: null,
                has_z3ds: false,
                z3ds_ready: false,
                z3ds_path: null,
                chd_ready: Boolean(archive.chd_ready),
                is_archive_item: true,
                chd_path: archive.chd_path,
            })),
        ];
    }, []);

    const queueAutoDiscoveredPaths = useCallback(async (pathsToQueue, totalFound = null) => {
        const total = Number.isFinite(totalFound) ? totalFound : pathsToQueue.length;
        if (!Array.isArray(pathsToQueue) || pathsToQueue.length === 0) {
            notify('ℹ No compatible files found for current mode', 'info');
            return false;
        }

        const queued = await startConversionSafely(pathsToQueue);
        if (queued) {
            if (total > pathsToQueue.length) {
                notify(`✓ Auto-queued ${pathsToQueue.length}/${total} file(s)`, 'success');
            } else {
                notify(`✓ Auto-queued ${pathsToQueue.length} file(s)`, 'success');
            }
            api.trackFeatureEvent('auto_queue_folder_queued', pathsToQueue.length).catch(() => {});
        }
        return queued;
    }, [notify, startConversionSafely]);

    const handleSearch = useCallback(async () => {
        if (!currentPath) {
            notify('⚠ No path selected', 'error');
            return;
        }

        if (!searchMode) {
            capturePreSearchView();
        }

        setLoading(true);
        setEntriesError(null);
        notify('🔍 Searching for convertible files...', 'info');

        try {
            const results = await api.searchFiles(currentPath, true, true);
            setSearchResults(results);
            setSearchMode(true);
            setCurrentPage(1);
            const combined = buildSearchEntries(results);
            setEntries(combined);
            setCurrentPage(1);
            setLastSelectedIndex(null);

            if (combined.length === 0) {
                notify('ℹ No convertible files found', 'info');
            } else {
                notify(`✓ Found ${combined.length} convertible file(s)`, 'success');
            }
        } catch (err) {
            setEntriesError(err.message);
            notify(`✗ Search failed: ${err.message}`, 'error');
            console.error('Search failed:', err);
        } finally {
            setLoading(false);
        }
    }, [
        currentPath,
        searchMode,
        capturePreSearchView,
        setLoading,
        setEntriesError,
        notify,
        setSearchResults,
        setSearchMode,
        setCurrentPage,
        buildSearchEntries,
        setEntries,
        setLastSelectedIndex,
    ]);

    const handleAutoQueueFolder = useCallback(async () => {
        if (!currentPath) {
            notify('⚠ No path selected', 'error');
            return;
        }
        if (autoQueueing || converting) return;

        setAutoQueueing(true);
        api.trackFeatureEvent('auto_queue_folder_clicked').catch(() => {});
        notify('⚡ Auto-queue scanning folder...', 'info');

        try {
            const results = await api.searchFiles(currentPath, true, true);
            const combined = buildSearchEntries(results);
            const compatiblePaths = Array.from(
                new Set(
                    combined
                        .filter((entry) => canSelectEntry(entry))
                        .map((entry) => entry.path),
                ),
            );

            if (compatiblePaths.length === 0) {
                notify('ℹ No compatible files found for current mode', 'info');
                return;
            }

            if (compatiblePaths.length > AUTO_QUEUE_CAP_PROMPT_THRESHOLD) {
                setAutoQueuePrompt({
                    paths: compatiblePaths,
                    total: compatiblePaths.length,
                    recommendedCap: Math.min(AUTO_QUEUE_RECOMMENDED_CAP, compatiblePaths.length),
                });
                notify(
                    `Found ${compatiblePaths.length} compatible files. Choose queue cap.`,
                    'info',
                );
                return;
            }

            await queueAutoDiscoveredPaths(compatiblePaths, compatiblePaths.length);
        } catch (err) {
            notify(`✗ Auto-queue failed: ${err.message}`, 'error');
            console.error('Auto-queue failed:', err);
        } finally {
            setAutoQueueing(false);
        }
    }, [
        currentPath,
        autoQueueing,
        converting,
        setAutoQueueing,
        notify,
        buildSearchEntries,
        canSelectEntry,
        setAutoQueuePrompt,
        queueAutoDiscoveredPaths,
    ]);

    const handleAutoQueuePromptCap = useCallback(async (cap) => {
        if (!autoQueuePrompt) return;
        const parsedCap = Number.parseInt(`${cap}`, 10);
        if (!Number.isFinite(parsedCap) || parsedCap <= 0) {
            notify('Enter a valid cap greater than 0', 'warning');
            return;
        }

        const total = autoQueuePrompt.total;
        const limitedPaths = autoQueuePrompt.paths.slice(0, Math.min(parsedCap, total));
        setAutoQueuePrompt(null);
        setAutoQueueing(true);
        try {
            await queueAutoDiscoveredPaths(limitedPaths, total);
        } finally {
            setAutoQueueing(false);
        }
    }, [autoQueuePrompt, notify, setAutoQueuePrompt, queueAutoDiscoveredPaths, setAutoQueueing]);

    const handleAutoQueuePromptAll = useCallback(async () => {
        if (!autoQueuePrompt) return;
        const allPaths = autoQueuePrompt.paths;
        const total = autoQueuePrompt.total;
        setAutoQueuePrompt(null);
        setAutoQueueing(true);
        try {
            await queueAutoDiscoveredPaths(allPaths, total);
        } finally {
            setAutoQueueing(false);
        }
    }, [autoQueuePrompt, setAutoQueuePrompt, queueAutoDiscoveredPaths, setAutoQueueing]);

    const handleScanMetadata = useCallback(async (force = false) => {
        const shouldForce = force === true;
        try {
            const res = await api.scanMetadata(shouldForce);
            if (res.status === 'scanning') {
                if (shouldForce) {
                    setChdMetadata(new Map());
                    if (!forceRescanRunning) {
                        setForceRescanRunning(true);
                    }
                }
                notify('Metadata scan already in progress', 'info');
            } else {
                if (shouldForce) {
                    setChdMetadata(new Map());
                    setForceRescanRunning(true);
                }
                notify(
                    shouldForce ? 'Started forced metadata scan' : 'Started background metadata scan',
                    'success',
                );
            }
        } catch (err) {
            notify(`Failed to start scan: ${err.message}`, 'error');
        }
    }, [setChdMetadata, forceRescanRunning, setForceRescanRunning, notify]);

    return {
        buildSearchEntries,
        queueAutoDiscoveredPaths,
        handleSearch,
        handleAutoQueueFolder,
        handleAutoQueuePromptCap,
        handleAutoQueuePromptAll,
        handleScanMetadata,
    };
}
