import { useEffect, useMemo } from '../runtime/preactRuntime.js';

export function useFileEntriesView({
    entries,
    fileTypeFilter,
    sortBy,
    sortOrder,
    itemsPerPage,
    currentPage,
    setCurrentPage,
}) {
    const displayedEntries = useMemo(() => {
        let filtered = entries;
        if (fileTypeFilter) {
            const exts = fileTypeFilter.split(',').map((ext) => ext.toLowerCase().trim());
            filtered = entries.filter((entry) => (
                entry.type === 'directory'
                || entry.type === 'archive'
                || exts.includes(entry.extension?.toLowerCase())
            ));
        }

        const getStatusPriority = (entry) => {
            if (entry.type === 'directory') return 0;
            if (entry.type === 'archive') return 1;
            if (entry.has_chd || entry.has_rvz || entry.has_z3ds) return 2;
            if (entry.convertible || entry.dolphin_convertible || entry.z3ds_convertible) return 3;
            return 4;
        };

        return [...filtered].sort((a, b) => {
            if (a.type === 'directory' && b.type !== 'directory') return -1;
            if (b.type === 'directory' && a.type !== 'directory') return 1;
            if (a.type === 'archive' && b.type !== 'archive' && b.type !== 'directory') return -1;
            if (b.type === 'archive' && a.type !== 'archive' && a.type !== 'directory') return 1;

            let cmp = 0;
            switch (sortBy) {
                case 'name':
                    cmp = a.name.localeCompare(b.name, undefined, { numeric: true, sensitivity: 'base' });
                    break;
                case 'size':
                    cmp = (a.size || 0) - (b.size || 0);
                    break;
                case 'status':
                    cmp = getStatusPriority(a) - getStatusPriority(b);
                    break;
                default:
                    cmp = 0;
            }
            return sortOrder === 'asc' ? cmp : -cmp;
        });
    }, [entries, fileTypeFilter, sortBy, sortOrder]);

    const pagination = useMemo(() => {
        const totalItems = displayedEntries.length;
        if (itemsPerPage === 'all') {
            return {
                totalItems,
                totalPages: 1,
                page: 1,
                start: totalItems > 0 ? 1 : 0,
                end: totalItems,
            };
        }

        const parsed = Number(itemsPerPage);
        const pageSize = Number.isFinite(parsed) && parsed > 0 ? parsed : (totalItems || 1);
        const totalPages = Math.max(1, Math.ceil(totalItems / pageSize));
        const page = Math.min(Math.max(currentPage, 1), totalPages);
        const start = totalItems > 0 ? ((page - 1) * pageSize) + 1 : 0;
        const end = totalItems > 0 ? Math.min(page * pageSize, totalItems) : 0;

        return { totalItems, totalPages, page, start, end };
    }, [displayedEntries.length, itemsPerPage, currentPage]);

    const paginatedEntries = useMemo(() => {
        if (itemsPerPage === 'all') return displayedEntries;
        const parsed = Number(itemsPerPage);
        const pageSize = Number.isFinite(parsed) && parsed > 0 ? parsed : displayedEntries.length;
        if (!pageSize) return displayedEntries;
        const start = (pagination.page - 1) * pageSize;
        return displayedEntries.slice(start, start + pageSize);
    }, [displayedEntries, itemsPerPage, pagination.page]);

    useEffect(() => {
        if (currentPage !== pagination.page) {
            setCurrentPage(pagination.page);
        }
    }, [currentPage, pagination.page, setCurrentPage]);

    return { displayedEntries, pagination, paginatedEntries };
}
