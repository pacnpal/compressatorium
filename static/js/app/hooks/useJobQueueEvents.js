import { api } from '../../api.js';
import { useEffect } from '../runtime/preactRuntime.js';

export function useJobQueueEvents({
    scheduleCompletionRefresh,
    notify,
    setJobs,
    setVerifiedCHDs,
    setStuckState,
    setHiddenJobIds,
    deferJobUiUpdatesRef,
    progressRenderAtRef,
    queuedJobUpdatesRef,
    jobUpdateFlushTimeoutRef,
    PROGRESS_RENDER_THROTTLE_MS,
    JOB_UPDATE_BATCH_WINDOW_MS,
}) {
    useEffect(() => {
        const mergeJobs = (serverJobs, currentJobs, currentHiddenIds) => {
            const visibleServerJobs = serverJobs.filter((job) => !currentHiddenIds.has(job.id));
            const mergedJobs = [];
            const seenIds = new Set();

            for (const serverJob of visibleServerJobs) {
                mergedJobs.push(serverJob);
                seenIds.add(serverJob.id);
            }

            for (const localJob of currentJobs) {
                if (!seenIds.has(localJob.id) && !currentHiddenIds.has(localJob.id)) {
                    if (localJob.id.startsWith('pending-')) {
                        mergedJobs.push(localJob);
                    }
                }
            }

            return mergedJobs;
        };

        const applyPolledJobs = (serverJobs) => {
            if (deferJobUiUpdatesRef.current) return;
            setHiddenJobIds((currentHidden) => {
                setJobs((prev) => {
                    const merged = mergeJobs(serverJobs, prev, currentHidden);
                    const visibleIds = new Set(merged.map((job) => job.id));
                    for (const key of progressRenderAtRef.current.keys()) {
                        if (!visibleIds.has(key)) {
                            progressRenderAtRef.current.delete(key);
                        }
                    }
                    return merged;
                });
                return currentHidden;
            });
        };

        api.getJobs().then(applyPolledJobs).catch(() => {});

        const applyQueuedJobUpdates = (queuedUpdates) => {
            if (queuedUpdates.length === 0) return;

            const completedNames = [];
            let failedCount = 0;
            let cancelledCount = 0;
            const verifiedPathsToAdd = new Set();
            const verifiedPathsToRemove = new Set();

            setJobs((prevJobs) => {
                let nextJobs = prevJobs;
                let didMutate = false;
                let jobIndexById = null;

                const ensureMutable = () => {
                    if (!didMutate) {
                        nextJobs = [...prevJobs];
                        didMutate = true;
                    }
                    return nextJobs;
                };

                const ensureJobIndex = () => {
                    if (jobIndexById !== null) return jobIndexById;
                    jobIndexById = new Map();
                    for (let i = 0; i < nextJobs.length; i += 1) {
                        jobIndexById.set(nextJobs[i].id, i);
                    }
                    return jobIndexById;
                };

                for (const update of queuedUpdates) {
                    const jobId = update?.data?.job_id;
                    if (!jobId) continue;

                    const idx = ensureJobIndex().get(jobId);
                    const hydratedJob = update?.data?.job;
                    if (idx == null) {
                        if (hydratedJob) {
                            const mutableJobs = ensureMutable();
                            mutableJobs.push(hydratedJob);
                            ensureJobIndex().set(hydratedJob.id, mutableJobs.length - 1);
                        }
                        continue;
                    }

                    const prevJob = nextJobs[idx];
                    const statusUpdate = update.type === 'complete' ? 'completed'
                        : update.type === 'error' ? 'failed'
                            : update.type === 'cancelled' ? 'cancelled'
                                : update.data.status ?? prevJob.status;
                    const isTerminalUpdate = update.type === 'complete'
                        || update.type === 'error'
                        || update.type === 'cancelled';

                    if (!isTerminalUpdate && !hydratedJob) {
                        const now = Date.now();
                        const lastPaintAt = progressRenderAtRef.current.get(jobId) || 0;
                        const nextProgress = update.data.progress ?? prevJob.progress;
                        if (
                            statusUpdate === prevJob.status
                            && nextProgress > prevJob.progress
                            && (now - lastPaintAt) < PROGRESS_RENDER_THROTTLE_MS
                        ) {
                            continue;
                        }
                        progressRenderAtRef.current.set(jobId, now);
                    }

                    const updatedJob = {
                        ...prevJob,
                        ...(hydratedJob || {}),
                        progress: update.data.progress ?? prevJob.progress,
                        message: update.data.message ?? prevJob.message,
                        status: statusUpdate,
                        error_message: update.data.error ?? prevJob.error_message,
                        output_size: update.data.output_size ?? prevJob.output_size,
                    };

                    const unchanged = (
                        prevJob.status === updatedJob.status
                        && prevJob.progress === updatedJob.progress
                        && prevJob.message === updatedJob.message
                        && prevJob.error_message === updatedJob.error_message
                        && prevJob.output_size === updatedJob.output_size
                        && prevJob.started_at === updatedJob.started_at
                        && prevJob.completed_at === updatedJob.completed_at
                        && prevJob.output_path === updatedJob.output_path
                    );
                    if (unchanged) continue;

                    ensureMutable()[idx] = updatedJob;
                    ensureJobIndex().set(updatedJob.id, idx);

                    if (isTerminalUpdate) {
                        progressRenderAtRef.current.delete(jobId);
                    }

                    if (update.type === 'complete') {
                        if (updatedJob.filename) completedNames.push(updatedJob.filename);
                        if (update.data.verified && update.data.output_path) {
                            verifiedPathsToAdd.add(update.data.output_path);
                        }
                        if (update.data.source_deleted && updatedJob.file_path?.toLowerCase().endsWith('.chd')) {
                            verifiedPathsToRemove.add(updatedJob.file_path);
                        }
                    } else if (update.type === 'error') {
                        failedCount += 1;
                    } else if (update.type === 'cancelled') {
                        cancelledCount += 1;
                    }
                }

                return didMutate ? nextJobs : prevJobs;
            });

            if (completedNames.length > 0) {
                scheduleCompletionRefresh();
                if (completedNames.length === 1) {
                    notify(`Completed: ${completedNames[0]}`, 'success');
                } else {
                    notify(`Completed ${completedNames.length} jobs`, 'success');
                }
            }
            if (failedCount > 0) {
                notify(failedCount === 1 ? '1 job failed' : `${failedCount} jobs failed`, 'error');
            }
            if (cancelledCount > 0) {
                notify(cancelledCount === 1 ? '1 job cancelled' : `${cancelledCount} jobs cancelled`, 'info');
            }

            if (verifiedPathsToAdd.size > 0 || verifiedPathsToRemove.size > 0) {
                setVerifiedCHDs((prev) => {
                    const next = new Set(prev);
                    for (const path of verifiedPathsToAdd) next.add(path);
                    for (const path of verifiedPathsToRemove) next.delete(path);
                    return next;
                });
            }
        };

        const flushQueuedJobUpdates = (force = false) => {
            if (deferJobUiUpdatesRef.current) {
                if (!jobUpdateFlushTimeoutRef.current) {
                    jobUpdateFlushTimeoutRef.current = setTimeout(() => {
                        jobUpdateFlushTimeoutRef.current = null;
                        flushQueuedJobUpdates(true);
                    }, JOB_UPDATE_BATCH_WINDOW_MS);
                }
                return;
            }

            if (!force && jobUpdateFlushTimeoutRef.current) return;

            if (!force) {
                jobUpdateFlushTimeoutRef.current = setTimeout(() => {
                    jobUpdateFlushTimeoutRef.current = null;
                    flushQueuedJobUpdates(true);
                }, JOB_UPDATE_BATCH_WINDOW_MS);
                return;
            }

            if (jobUpdateFlushTimeoutRef.current) {
                clearTimeout(jobUpdateFlushTimeoutRef.current);
                jobUpdateFlushTimeoutRef.current = null;
            }

            const queuedUpdates = Array.from(queuedJobUpdatesRef.current.values());
            queuedJobUpdatesRef.current.clear();
            applyQueuedJobUpdates(queuedUpdates);
        };

        const unsubscribe = api.subscribeToJobs((update) => {
            const jobId = update?.data?.job_id;
            if (!jobId) return;

            queuedJobUpdatesRef.current.set(jobId, update);

            const isTerminalUpdate = update.type === 'complete'
                || update.type === 'error'
                || update.type === 'cancelled';
            flushQueuedJobUpdates(isTerminalUpdate);
        });

        const interval = setInterval(() => {
            api.getJobs().then(applyPolledJobs).catch(() => {});
            api.checkStuckStatus()
                .then((status) => {
                    if (deferJobUiUpdatesRef.current) return;
                    setStuckState(status);
                })
                .catch(() => {
                    if (deferJobUiUpdatesRef.current) return;
                    setStuckState(null);
                });
        }, 4000);

        return () => {
            unsubscribe();
            if (jobUpdateFlushTimeoutRef.current) {
                clearTimeout(jobUpdateFlushTimeoutRef.current);
                jobUpdateFlushTimeoutRef.current = null;
            }
            queuedJobUpdatesRef.current.clear();
            clearInterval(interval);
        };
    }, [scheduleCompletionRefresh]);
}
