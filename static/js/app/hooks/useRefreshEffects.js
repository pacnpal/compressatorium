import { api } from '../../api.js';
import { useCallback, useEffect } from '../runtime/preactRuntime.js';

export function useRefreshFileList({
    searchMode,
    currentPathRef,
    currentArchivePathRef,
    setLoading,
    setEntriesError,
    setEntries,
}) {
    return useCallback((showSpinner = false) => {
        const path = currentPathRef.current;
        const archivePath = currentArchivePathRef.current;

        if (searchMode) return;

        if (archivePath) {
            if (showSpinner) setLoading(true);
            api.listArchive(archivePath)
                .then((archiveData) => {
                    if (!archiveData || !archiveData.files) return;
                    setEntriesError(null);

                    const newArchiveEntries = archiveData.files.map((file) => ({
                        name: file.name,
                        path: `${archivePath}::${file.internal_path}`,
                        type: 'file',
                        size: file.size,
                        extension: file.extension,
                        convertible: file.convertible,
                        has_chd: file.has_chd || false,
                        has_rvz: false,
                        dolphin_ready: false,
                        dolphin_path: null,
                        has_z3ds: false,
                        z3ds_ready: false,
                        z3ds_path: null,
                        chd_ready: Boolean(file.chd_ready),
                        output_stem: file.output_stem,
                        chd_path: file.chd_path,
                        is_archive_item: true,
                        archive_path: archivePath,
                    }));

                    setEntries((prevEntries) => {
                        if (prevEntries.length === newArchiveEntries.length) {
                            const hasChanges = newArchiveEntries.some((newEntry, i) => {
                                const oldEntry = prevEntries[i];
                                return oldEntry.name !== newEntry.name
                                    || oldEntry.path !== newEntry.path
                                    || oldEntry.size !== newEntry.size
                                    || oldEntry.convertible !== newEntry.convertible
                                    || oldEntry.has_chd !== newEntry.has_chd
                                    || oldEntry.chd_ready !== newEntry.chd_ready;
                            });
                            if (!hasChanges) return prevEntries;
                        }
                        return newArchiveEntries;
                    });
                })
                .catch((err) => {
                    console.error('Failed to refresh archive contents:', err);
                })
                .finally(() => {
                    if (showSpinner) setLoading(false);
                });
            return;
        }

        if (path) {
            if (showSpinner) setLoading(true);
            api.listFiles(path)
                .then((data) => {
                    setEntriesError(null);
                    setEntries((prevEntries) => {
                        const newEntries = data.entries;
                        if (prevEntries.length === newEntries.length) {
                            const hasChanges = newEntries.some((newEntry, i) => {
                                const oldEntry = prevEntries[i];
                                return oldEntry.name !== newEntry.name
                                    || oldEntry.path !== newEntry.path
                                    || oldEntry.size !== newEntry.size
                                    || oldEntry.type !== newEntry.type
                                    || oldEntry.convertible !== newEntry.convertible
                                    || oldEntry.dolphin_convertible !== newEntry.dolphin_convertible
                                    || oldEntry.z3ds_convertible !== newEntry.z3ds_convertible
                                    || oldEntry.has_chd !== newEntry.has_chd
                                    || oldEntry.has_rvz !== newEntry.has_rvz
                                    || oldEntry.dolphin_ready !== newEntry.dolphin_ready
                                    || oldEntry.dolphin_path !== newEntry.dolphin_path
                                    || oldEntry.has_z3ds !== newEntry.has_z3ds
                                    || oldEntry.z3ds_ready !== newEntry.z3ds_ready
                                    || oldEntry.z3ds_path !== newEntry.z3ds_path
                                    || oldEntry.chd_ready !== newEntry.chd_ready;
                            });
                            if (!hasChanges) return prevEntries;
                        }
                        return newEntries;
                    });
                })
                .catch((err) => {
                    setEntriesError(err.message);
                    console.error('Failed to list files:', err);
                })
                .finally(() => {
                    if (showSpinner) setLoading(false);
                });
        }
    }, [searchMode]);
}

export function useScheduleCompletionRefresh({
    completionRefreshTimeoutRef,
    refreshFileList,
    COMPLETION_REFRESH_DEBOUNCE_MS,
}) {
    const scheduleCompletionRefresh = useCallback(() => {
        if (completionRefreshTimeoutRef.current) {
            clearTimeout(completionRefreshTimeoutRef.current);
        }
        completionRefreshTimeoutRef.current = setTimeout(() => {
            refreshFileList(false);
        }, COMPLETION_REFRESH_DEBOUNCE_MS);
    }, [refreshFileList]);

    useEffect(() => () => {
        if (completionRefreshTimeoutRef.current) {
            clearTimeout(completionRefreshTimeoutRef.current);
        }
    }, []);

    return scheduleCompletionRefresh;
}

export function useForceRescanStatusPolling({
    forceRescanRunning,
    setChdMetadata,
    setForceRescanRunning,
    notify,
}) {
    useEffect(() => {
        if (!forceRescanRunning) return;

        let cancelled = false;
        let timeoutId = null;
        let failureCount = 0;
        const maxFailures = 5;

        const pollStatus = async () => {
            try {
                const status = await api.getScanStatus();
                if (cancelled) return;
                failureCount = 0;

                if (status?.scanning) {
                    timeoutId = setTimeout(pollStatus, 1500);
                    return;
                }

                setChdMetadata(new Map());
                setForceRescanRunning(false);
            } catch (err) {
                if (cancelled) return;
                failureCount += 1;

                if (failureCount >= maxFailures) {
                    setForceRescanRunning(false);
                    notify('Metadata scan status unavailable; resuming badge refresh.', 'warning');
                    return;
                }

                if (failureCount === 1) {
                    notify(`Failed to get scan status: ${err.message}`, 'error');
                }

                const delay = Math.min(5000, 1500 + failureCount * 750);
                timeoutId = setTimeout(pollStatus, delay);
            }
        };

        timeoutId = setTimeout(pollStatus, 1000);

        return () => {
            cancelled = true;
            if (timeoutId) clearTimeout(timeoutId);
        };
    }, [forceRescanRunning]);
}

export function useAutoRefreshFileList({
    autoRefresh,
    currentPath,
    searchMode,
    refreshFileList,
    jobs,
    creatingJobs,
}) {
    useEffect(() => {
        const hasActiveWork = creatingJobs.length > 0
            || jobs.some((job) => ['creating', 'queued', 'processing'].includes(job.status));
        if (!autoRefresh || !currentPath || searchMode || hasActiveWork) return;

        const interval = setInterval(() => {
            refreshFileList(false);
        }, 3000);

        return () => clearInterval(interval);
    }, [autoRefresh, currentPath, searchMode, refreshFileList, jobs, creatingJobs]);
}
