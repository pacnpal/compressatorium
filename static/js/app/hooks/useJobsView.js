import { useEffect, useMemo } from '../runtime/preactRuntime.js';

export function useJobsView({
    jobs,
    creatingJobs,
    jobTab,
    jobItemsPerPage,
    jobCurrentPage,
    setJobCurrentPage,
    setJobTab,
}) {
    const queueJobs = useMemo(() => {
        const activeServerJobs = jobs.filter(
            (job) => !['completed', 'failed', 'cancelled'].includes(job.status),
        );
        return creatingJobs.length > 0
            ? [...creatingJobs, ...activeServerJobs]
            : activeServerJobs;
    }, [creatingJobs, jobs]);

    const completedJobs = useMemo(
        () => jobs.filter((job) => job.status === 'completed'),
        [jobs],
    );

    const issueJobs = useMemo(
        () => jobs.filter((job) => ['failed', 'cancelled'].includes(job.status)),
        [jobs],
    );

    const displayedJobs = useMemo(() => {
        if (jobTab === 'completed') return completedJobs;
        if (jobTab === 'issues') return issueJobs;
        return queueJobs;
    }, [jobTab, queueJobs, completedJobs, issueJobs]);

    const jobsPagination = useMemo(() => {
        const totalItems = displayedJobs.length;
        if (jobItemsPerPage === 'all') {
            return {
                totalItems,
                totalPages: 1,
                page: 1,
                start: totalItems > 0 ? 1 : 0,
                end: totalItems,
            };
        }

        const parsed = Number(jobItemsPerPage);
        const pageSize = Number.isFinite(parsed) && parsed > 0 ? parsed : (totalItems || 1);
        const totalPages = Math.max(1, Math.ceil(totalItems / pageSize));
        const page = Math.min(Math.max(jobCurrentPage, 1), totalPages);
        const start = totalItems > 0 ? ((page - 1) * pageSize) + 1 : 0;
        const end = totalItems > 0 ? Math.min(page * pageSize, totalItems) : 0;

        return { totalItems, totalPages, page, start, end };
    }, [displayedJobs.length, jobItemsPerPage, jobCurrentPage]);

    const paginatedJobs = useMemo(() => {
        if (jobItemsPerPage === 'all') return displayedJobs;
        const parsed = Number(jobItemsPerPage);
        const pageSize = Number.isFinite(parsed) && parsed > 0 ? parsed : displayedJobs.length;
        if (!pageSize) return displayedJobs;
        const start = (jobsPagination.page - 1) * pageSize;
        return displayedJobs.slice(start, start + pageSize);
    }, [displayedJobs, jobItemsPerPage, jobsPagination.page]);

    useEffect(() => {
        if (jobCurrentPage !== jobsPagination.page) {
            setJobCurrentPage(jobsPagination.page);
        }
    }, [jobCurrentPage, jobsPagination.page, setJobCurrentPage]);

    useEffect(() => {
        if (jobTab === 'issues' && issueJobs.length === 0) {
            setJobTab('queue');
            setJobCurrentPage(1);
        }
    }, [jobTab, issueJobs.length, setJobCurrentPage, setJobTab]);

    return {
        queueJobs,
        completedJobs,
        issueJobs,
        displayedJobs,
        jobsPagination,
        paginatedJobs,
    };
}
