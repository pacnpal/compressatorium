import { api } from '../../api.js';
import { useEffect } from '../runtime/preactRuntime.js';

export function useLoadVolumesOnMount({
    setVolumesLoading,
    setVolumes,
    setVolumesError,
    setSelectedVolume,
    setCurrentPath,
    setShowHelp,
}) {
    useEffect(() => {
        setVolumesLoading(true);
        api.getVolumes()
            .then((vols) => {
                setVolumes(vols);
                setVolumesError(null);
                if (vols.length > 0) {
                    setSelectedVolume(vols[0]);
                    setCurrentPath(vols[0].path);
                }
                if (vols.length === 0) {
                    setShowHelp(true);
                }
            })
            .catch((err) => {
                setVolumesError(err.message);
                console.error('Failed to load volumes:', err);
            })
            .finally(() => setVolumesLoading(false));
    }, []);
}

export function useLoadVerifiedChdsOnMount({ setVerifiedCHDs }) {
    useEffect(() => {
        api.getVerifiedCHDs()
            .then((data) => {
                if (data && Array.isArray(data.verified)) {
                    setVerifiedCHDs(new Set(data.verified));
                }
            })
            .catch(() => {});
    }, []);
}

export function useLoadAppVersionOnMount({
    setAppVersion,
    setSearchAutoReturnToFileList,
}) {
    useEffect(() => {
        api.getVersion()
            .then((data) => {
                setAppVersion(data.version);
                if (typeof data.search_auto_return_to_file_list === 'boolean') {
                    setSearchAutoReturnToFileList(data.search_auto_return_to_file_list);
                }
            })
            .catch((err) => console.warn('Failed to fetch app version:', err));
    }, []);
}

export function useLoadEntriesOnPathChange({
    currentPath,
    setLoading,
    setEntriesError,
    setEntries,
    setSearchMode,
    setSearchResults,
}) {
    useEffect(() => {
        if (!currentPath) return;
        setLoading(true);
        setEntriesError(null);
        api.listFiles(currentPath)
            .then((data) => {
                setEntries(data.entries);
                setSearchMode(false);
                setSearchResults(null);
            })
            .catch((err) => {
                setEntriesError(err.message);
                console.error('Failed to list files:', err);
            })
            .finally(() => setLoading(false));
    }, [currentPath]);
}

export function useChdMetadataWarmCache({
    displayedEntries,
    forceRescanRunning,
    jobs,
    creatingJobs,
    chdMetadata,
    setChdMetadata,
}) {
    useEffect(() => {
        if (forceRescanRunning) return;
        const hasActiveWork = creatingJobs.length > 0
            || jobs.some((job) => ['creating', 'queued', 'processing'].includes(job.status));
        if (hasActiveWork) return;

        const chdPaths = displayedEntries
            .filter((entry) => entry.extension?.toLowerCase() === '.chd')
            .map((entry) => entry.path)
            .filter((path) => !chdMetadata.has(path));

        if (chdPaths.length === 0) return;

        api.getCHDMetadataBatch(chdPaths)
            .then((data) => {
                const cachedPaths = [];
                const uncachedPaths = [];

                Object.entries(data).forEach(([path, meta]) => {
                    if (meta.cached) {
                        cachedPaths.push([path, meta]);
                    } else {
                        uncachedPaths.push(path);
                    }
                });

                if (cachedPaths.length > 0) {
                    setChdMetadata((prev) => {
                        const next = new Map(prev);
                        cachedPaths.forEach(([path, meta]) => next.set(path, meta));
                        return next;
                    });
                }

                const fetchLimit = 3;
                const fetchUncached = async () => {
                    for (let i = 0; i < uncachedPaths.length; i += fetchLimit) {
                        const batch = uncachedPaths.slice(i, i + fetchLimit);
                        await Promise.all(batch.map(async (path) => {
                            try {
                                const info = await api.getCHDInfo(path);
                                setChdMetadata((prev) => {
                                    const next = new Map(prev);
                                    next.set(path, { media_type: info.media_type, cached: true });
                                    return next;
                                });
                            } catch {
                                setChdMetadata((prev) => {
                                    const next = new Map(prev);
                                    next.set(path, { media_type: null, cached: true });
                                    return next;
                                });
                            }
                        }));
                    }
                };

                if (uncachedPaths.length > 0) {
                    fetchUncached();
                }
            })
            .catch((err) => console.warn('Failed to fetch CHD metadata:', err));
    }, [displayedEntries, forceRescanRunning, jobs, creatingJobs, chdMetadata]);
}
