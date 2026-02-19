import { api } from '../../api.js';
import { useCallback } from '../runtime/preactRuntime.js';

export function useJobAdminActions({
    jobs,
    cancellingAllJobs,
    clearingCompletedJobs,
    recoveringStuck,
    notify,
    setJobs,
    setShowCancelAllModal,
    setCancellingAllJobs,
    setShowClearDoneModal,
    setClearingCompletedJobs,
    setRecoveringStuck,
    setStuckState,
    setHiddenJobIds,
}) {
    const handleCancelJob = useCallback(async (jobId) => {
        try {
            await api.cancelJob(jobId);
            setJobs((prev) => prev.map((job) => (
                job.id === jobId
                    ? {
                        ...job,
                        status: job.status === 'queued' ? 'cancelled' : job.status,
                        message: job.status === 'queued' ? job.message : 'Cancelling...',
                    }
                    : job
            )));
            notify('Cancellation requested', 'info');
        } catch (err) {
            notify(`Failed to cancel: ${err.message}`, 'error');
            console.error('Failed to cancel job:', err);
        }
    }, [notify]);

    const handleRequestCancelAll = useCallback(() => {
        const activeCount = jobs.filter((job) => ['queued', 'processing'].includes(job.status)).length;
        if (activeCount === 0) {
            notify('No active jobs to cancel', 'info');
            return;
        }
        setShowCancelAllModal(true);
    }, [jobs, notify]);

    const handleCancelAllJobs = useCallback(async () => {
        if (cancellingAllJobs) return;
        setCancellingAllJobs(true);
        try {
            const result = await api.cancelAllJobs();
            setJobs((prev) => prev.map((job) => {
                if (job.status === 'queued') {
                    return { ...job, status: 'cancelled' };
                }
                if (job.status === 'processing') {
                    return { ...job, message: 'Cancelling...' };
                }
                return job;
            }));
            setShowCancelAllModal(false);
            notify(`Cancellation requested for ${result.requested || 0} job(s)`, 'info');
        } catch (err) {
            notify(`Failed to cancel all jobs: ${err.message}`, 'error');
            console.error('Failed to cancel all jobs:', err);
        } finally {
            setCancellingAllJobs(false);
        }
    }, [cancellingAllJobs, notify]);

    const handleRequestClearCompleted = useCallback(() => {
        const completedCount = jobs.filter((job) => ['completed', 'failed', 'cancelled'].includes(job.status)).length;
        if (completedCount === 0) {
            notify('No completed jobs to clear', 'info');
            return;
        }
        setShowClearDoneModal(true);
    }, [jobs, notify]);

    const handleClearCompleted = useCallback(async () => {
        if (clearingCompletedJobs) return;

        const completedJobs = jobs.filter((job) => ['completed', 'failed', 'cancelled'].includes(job.status));
        if (completedJobs.length === 0) return;
        setClearingCompletedJobs(true);

        const idsToHide = completedJobs.map((job) => job.id);
        setHiddenJobIds((prev) => {
            const next = new Set(prev);
            idsToHide.forEach((id) => next.add(id));
            return next;
        });
        setJobs((prev) => prev.filter((job) => !['completed', 'failed', 'cancelled'].includes(job.status)));

        try {
            await api.deleteCompletedJobs();
            setHiddenJobIds((prev) => {
                const next = new Set(prev);
                idsToHide.forEach((id) => next.delete(id));
                return next;
            });
        } catch (err) {
            console.error('Failed to delete completed jobs:', err);
            setHiddenJobIds((prev) => {
                const next = new Set(prev);
                idsToHide.forEach((id) => next.delete(id));
                return next;
            });
            notify('Failed to clear completed jobs', 'error');
        } finally {
            setClearingCompletedJobs(false);
            setShowClearDoneModal(false);
        }
    }, [clearingCompletedJobs, jobs, notify]);

    const handleRecoverStuck = useCallback(async () => {
        if (recoveringStuck) return;

        setRecoveringStuck(true);
        try {
            const result = await api.recoverStuckJobs();
            notify(`Recovery completed: removed ${result.removed_locks || 0} stale lock(s)`, 'success');
            const status = await api.checkStuckStatus();
            setStuckState(status);
        } catch (err) {
            notify(`Recovery failed: ${err.message}`, 'error');
            console.error('Failed to recover stuck jobs:', err);
        } finally {
            setRecoveringStuck(false);
        }
    }, [recoveringStuck, notify]);

    return {
        handleCancelJob,
        handleRequestCancelAll,
        handleCancelAllJobs,
        handleRequestClearCompleted,
        handleClearCompleted,
        handleRecoverStuck,
    };
}
