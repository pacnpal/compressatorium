// Main Compressatorium App
import { api, formatSize, isDolphinFile } from '../api.js';
import { setIgirPreselectedInput } from './bridges/igirPreselectBridge.js';
import { html, render, useState, useEffect, useRef, useCallback, useMemo } from './runtime/preactRuntime.js';
import {
    getFileExtension,
    is3dsFile,
    is3dsSourceFile,
    Z3DS_OUTPUT_EXTENSION_BY_SOURCE,
} from './utils/fileTypeUtils.js';
import { buildCompressionValue, cloneSelectionMap } from './utils/stateUtils.js';
import {
    DEFAULT_DOLPHIN_COMPRESSION_LEVEL,
    MAX_VISIBLE_CREATING_PLACEHOLDERS,
    DEFAULT_PAGE_SIZE,
    DEFAULT_SEARCH_AUTO_RETURN_TO_FILE_LIST,
    ISO_TOOL_STORAGE_KEY,
    MODE_GROUPS,
    PAGE_SIZE_OPTIONS,
} from './constants/uiConstants.js';
import { Breadcrumb } from './components/Breadcrumb.js';
import { VolumeList } from './components/VolumeList.js';
import { AutoQueueCapModal } from './components/AutoQueueCapModal.js';
import { CancelAllJobsModal } from './components/CancelAllJobsModal.js';
import { CHDInfoModal } from './components/CHDInfoModal.js';
import { ClearDoneModal } from './components/ClearDoneModal.js';
import { DeletePlanModal } from './components/DeletePlanModal.js';
import { DeleteModal } from './components/DeleteModal.js';
import { DuplicateModal } from './components/DuplicateModal.js';
import { BulkDeleteModal } from './components/BulkDeleteModal.js';
import { BulkVerifyModal } from './components/BulkVerifyModal.js';
import { FileList } from './components/FileList.js';
import { HelpPanel } from './components/HelpPanel.js';
import { JobList } from './components/JobList.js';
import { RenameModal } from './components/RenameModal.js';
import { IgirView } from './features/igir/IgirView.js';
import { useFileEntriesView } from './hooks/useFileEntriesView.js';
import {
    useChdMetadataWarmCache,
    useLoadAppVersionOnMount,
    useLoadEntriesOnPathChange,
    useLoadVerifiedChdsOnMount,
    useLoadVolumesOnMount,
} from './hooks/useFileBrowserEffects.js';
import { useBulkFileActions, useFileMutationHandlers } from './hooks/useFileActions.js';
import { useJobAdminActions } from './hooks/useJobAdminActions.js';
import { useJobQueueEvents } from './hooks/useJobQueueEvents.js';
import { useJobsView } from './hooks/useJobsView.js';
import {
    useConversionPresetActions,
    usePersistConversionPresets,
    usePersistIsoHandling,
} from './hooks/usePresetActions.js';
import {
    useAutoRefreshFileList,
    useForceRescanStatusPolling,
    useRefreshFileList,
    useScheduleCompletionRefresh,
} from './hooks/useRefreshEffects.js';
import { useSearchQueueActions } from './hooks/useSearchQueueActions.js';
import { loadStoredConversionPresets } from './utils/conversionPresetUtils.js';
import {
    getFilterOptions,
    getPrimaryToolHint,
    normalizeDolphinLevel,
} from './utils/uiHelpers.js';

// ============ Main App ============

function App() {
    const PROGRESS_RENDER_THROTTLE_MS = 250;
    const COMPLETION_REFRESH_DEBOUNCE_MS = 500;
    const JOB_UPDATE_BATCH_WINDOW_MS = 100;

    // State
    const [volumes, setVolumes] = useState([]);
    const [volumesLoading, setVolumesLoading] = useState(true);
    const [volumesError, setVolumesError] = useState(null);
    const [selectedVolume, setSelectedVolume] = useState(null);
    const [currentPath, setCurrentPath] = useState(null);
    const [entries, setEntries] = useState([]);
    const [entriesError, setEntriesError] = useState(null);
    const [selectedFiles, setSelectedFiles] = useState(new Map());
    const [jobs, setJobs] = useState([]);
    const [creatingJobs, setCreatingJobs] = useState([]);
    const [, setHiddenJobIds] = useState(new Set());
    const [loading, setLoading] = useState(false);
    const [conversionMode, setConversionMode] = useState('createcd');
    const [isoHandling, setIsoHandling] = useState(() => {
        try {
            const stored = localStorage.getItem(ISO_TOOL_STORAGE_KEY);
            return stored === 'chdman' || stored === 'dolphin' || stored === 'z3ds' || stored === 'igir' ? stored : null;
        } catch {
            return null;
        }
    });
    const [compressionSelection, setCompressionSelection] = useState(['zlib']);
    const [dolphinCompressionLevel, setDolphinCompressionLevel] = useState(DEFAULT_DOLPHIN_COMPRESSION_LEVEL);
    const [showCompressionHelp, setShowCompressionHelp] = useState(false);
    const [customFilterMode, setCustomFilterMode] = useState(false);
    const [outputDir, setOutputDir] = useState('');
    const [deleteOnVerify, setDeleteOnVerify] = useState(false);
    const [conversionPresets, setConversionPresets] = useState(() => loadStoredConversionPresets());
    const [selectedPresetId, setSelectedPresetId] = useState('');
    const [deletePlan, setDeletePlan] = useState(null); // { plan, paths, duplicateAction }
    const [showCHDInfo, setShowCHDInfo] = useState(null);
    const [searchMode, setSearchMode] = useState(false);
    const [searchResults, setSearchResults] = useState(null);
    const [showHelp, setShowHelp] = useState(false);
    const [notification, setNotification] = useState(null);
    const [converting, setConverting] = useState(false);
    const [autoQueueing, setAutoQueueing] = useState(false);
    const [autoQueuePrompt, setAutoQueuePrompt] = useState(null); // { paths, total, recommendedCap }
    const [duplicateCheck, setDuplicateCheck] = useState(null); // { duplicates: [], paths: [] }
    const [autoRefresh, setAutoRefresh] = useState(true); // Auto-refresh file list
    const [currentArchivePath, setCurrentArchivePath] = useState(null); // Track current archive being viewed
    const [renameTarget, setRenameTarget] = useState(null); // Entry to rename
    const [deleteTarget, setDeleteTarget] = useState(null); // Entry to delete
    const [bulkDeleteEntries, setBulkDeleteEntries] = useState(null); // Entries for bulk delete
    const [bulkVerifyItems, setBulkVerifyItems] = useState(null); // Items for bulk verify
    const [verifiedCHDs, setVerifiedCHDs] = useState(new Set()); // Set of verified CHD paths
    const [verifyProgress, setVerifyProgress] = useState(new Map());
    const [fileTypeFilter, setFileTypeFilter] = useState(null); // null = all, or ".chd", ".zip,.7z,.rar", etc.
    const [lastSelectedIndex, setLastSelectedIndex] = useState(null); // For shift-click range selection
    const [chdMetadata, setChdMetadata] = useState(new Map()); // path -> { media_type: "dvd"|"cd"|null }
    const [forceRescanRunning, setForceRescanRunning] = useState(false);
    const [appVersion, setAppVersion] = useState(null); // App version from backend
    const [searchAutoReturnToFileList, setSearchAutoReturnToFileList] = useState(DEFAULT_SEARCH_AUTO_RETURN_TO_FILE_LIST);
    const [sortBy, setSortBy] = useState('name'); // 'name', 'size', 'status'
    const [sortOrder, setSortOrder] = useState('asc'); // 'asc', 'desc'
    const [itemsPerPage, setItemsPerPage] = useState(DEFAULT_PAGE_SIZE);
    const [currentPage, setCurrentPage] = useState(1);
    const [jobTab, setJobTab] = useState('queue');
    const [jobItemsPerPage, setJobItemsPerPage] = useState(DEFAULT_PAGE_SIZE);
    const [jobCurrentPage, setJobCurrentPage] = useState(1);
    const [stuckState, setStuckState] = useState(null); // Stuck state detection: { is_stuck, queued_count, processing_count }
    const [recoveringStuck, setRecoveringStuck] = useState(false); // Recovery in progress
    const [showCancelAllModal, setShowCancelAllModal] = useState(false);
    const [cancellingAllJobs, setCancellingAllJobs] = useState(false);
    const [showClearDoneModal, setShowClearDoneModal] = useState(false);
    const [clearingCompletedJobs, setClearingCompletedJobs] = useState(false);

    // Ref to track current path for use in callbacks
    const currentPathRef = useRef(null);
    currentPathRef.current = currentPath;

    // Ref to track current archive path for use in callbacks
    const currentArchivePathRef = useRef(null);
    currentArchivePathRef.current = currentArchivePath;
    const progressRenderAtRef = useRef(new Map()); // jobId -> timestamp
    const queuedJobUpdatesRef = useRef(new Map()); // jobId -> latest SSE payload
    const jobUpdateFlushTimeoutRef = useRef(null);
    const completionRefreshTimeoutRef = useRef(null);
    const preSearchViewRef = useRef(null); // List/archive view snapshot before Search All
    const deferJobUiUpdatesRef = useRef(false); // Pause job-driven rerenders during active select interactions

    // Show notification
    const notify = (message, type = 'info') => {
        setNotification({ message, type });
        setTimeout(() => setNotification(null), 4000);
    };

    const beginUiSelectionInteraction = useCallback(() => {
        deferJobUiUpdatesRef.current = true;
    }, []);

    const endUiSelectionInteraction = useCallback(() => {
        deferJobUiUpdatesRef.current = false;
    }, []);

    const capturePreSearchView = useCallback(() => {
        preSearchViewRef.current = {
            entries,
            entriesError,
            currentArchivePath,
            selectedFiles: cloneSelectionMap(selectedFiles),
            currentPage,
            lastSelectedIndex
        };
    }, [entries, entriesError, currentArchivePath, selectedFiles, currentPage, lastSelectedIndex]);

    const restorePreSearchView = useCallback(() => {
        const snapshot = preSearchViewRef.current;
        if (!snapshot) {
            notify('No previous file list view is available yet.', 'info');
            return false;
        }

        setSearchMode(false);
        setSearchResults(null);
        setEntries(snapshot.entries || []);
        setEntriesError(snapshot.entriesError || null);
        setCurrentArchivePath(snapshot.currentArchivePath || null);
        setSelectedFiles(cloneSelectionMap(snapshot.selectedFiles));
        setCurrentPage(snapshot.currentPage || 1);
        setLastSelectedIndex(snapshot.lastSelectedIndex ?? null);
        return true;
    }, [notify]);

    usePersistIsoHandling({ isoHandling });
    usePersistConversionPresets({ conversionPresets });

    const refreshFileList = useRefreshFileList({
        searchMode,
        currentPathRef,
        currentArchivePathRef,
        setLoading,
        setEntriesError,
        setEntries,
    });

    const scheduleCompletionRefresh = useScheduleCompletionRefresh({
        completionRefreshTimeoutRef,
        refreshFileList,
        COMPLETION_REFRESH_DEBOUNCE_MS,
    });

    useLoadVolumesOnMount({
        setVolumesLoading,
        setVolumes,
        setVolumesError,
        setSelectedVolume,
        setCurrentPath,
        setShowHelp,
    });

    useLoadVerifiedChdsOnMount({ setVerifiedCHDs });

    const { displayedEntries, pagination, paginatedEntries } = useFileEntriesView({
        entries,
        fileTypeFilter,
        sortBy,
        sortOrder,
        itemsPerPage,
        currentPage,
        setCurrentPage,
    });

    const {
        queueJobs,
        completedJobs,
        issueJobs,
        jobsPagination,
        paginatedJobs,
    } = useJobsView({
        jobs,
        creatingJobs,
        jobTab,
        jobItemsPerPage,
        jobCurrentPage,
        setJobCurrentPage,
        setJobTab,
    });

    // Prune selected files to only include visible entries when filter changes
    // This prevents hidden selections from causing unexpected behavior during conversion
    useEffect(() => {
        if (!fileTypeFilter) return; // No filter = all visible, no pruning needed

        const visiblePaths = new Set(displayedEntries.map(e => e.path));
        setSelectedFiles(prev => {
            let hasHidden = false;
            for (const path of prev.keys()) {
                if (!visiblePaths.has(path)) {
                    hasHidden = true;
                    break;
                }
            }
            if (!hasHidden) return prev;

            // Prune to only visible selections
            const next = new Map();
            for (const [path, entry] of prev) {
                if (visiblePaths.has(path)) {
                    next.set(path, entry);
                }
            }
            return next;
        });
    }, [displayedEntries, fileTypeFilter]);

    useForceRescanStatusPolling({
        forceRescanRunning,
        setChdMetadata,
        setForceRescanRunning,
        notify,
    });

    useChdMetadataWarmCache({
        displayedEntries,
        forceRescanRunning,
        jobs,
        creatingJobs,
        chdMetadata,
        setChdMetadata,
    });

    useLoadAppVersionOnMount({
        setAppVersion,
        setSearchAutoReturnToFileList,
    });

    useLoadEntriesOnPathChange({
        currentPath,
        setLoading,
        setEntriesError,
        setEntries,
        setSearchMode,
        setSearchResults,
    });

    useJobQueueEvents({
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
    });

    useAutoRefreshFileList({
        autoRefresh,
        currentPath,
        searchMode,
        refreshFileList,
        jobs,
        creatingJobs,
    });

    useEffect(() => {
        setLastSelectedIndex(null);
    }, [pagination.page, itemsPerPage]);

    // Handlers
    const handleVolumeSelect = (vol) => {
        setSelectedVolume(vol);
        setCurrentPath(vol.path);
        setSelectedFiles(new Map());
        setCurrentArchivePath(null); // Exit archive view when changing volumes
        preSearchViewRef.current = null;
        setCurrentPage(1);
        setLastSelectedIndex(null); // Reset shift-selection anchor
    };

    const handleNavigate = (path) => {
        setCurrentPath(path);
        setSelectedFiles(new Map());
        setCurrentArchivePath(null); // Exit archive view when navigating directories
        preSearchViewRef.current = null;
        setCurrentPage(1);
        setLastSelectedIndex(null); // Reset shift-selection anchor
    };

    const handleBrowseArchive = async (archivePath) => {
        setLoading(true);
        setEntriesError(null);
        const archiveName = archivePath.split('/').pop();
        notify(`📦 Loading archive: ${archiveName}...`, 'info');

        try {
            const archiveData = await api.listArchive(archivePath);

            if (!archiveData || !archiveData.files || archiveData.files.length === 0) {
                notify(`ℹ No convertible files found in ${archiveName}`, 'info');
                setEntries([]);
                setCurrentArchivePath(null);
                setSelectedFiles(new Map());
                setCurrentPage(1);
                setLastSelectedIndex(null);
                return;
            }

            const archiveEntries = archiveData.files.map(file => ({
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
                archive_path: archivePath
            }));

            setCurrentArchivePath(archivePath); // Track that we're in archive view
            setEntries(archiveEntries);
            setSelectedFiles(new Map());
            setSearchMode(false);
            setSearchResults(null);
            setCurrentPage(1);
            setLastSelectedIndex(null); // Reset shift-selection anchor
            notify(`✓ Loaded ${archiveEntries.length} file(s) from ${archiveName}`, 'success');
        } catch (err) {
            setEntriesError(err.message);
            console.error('Failed to browse archive:', err);
            notify(`✗ Failed to browse archive: ${err.message}`, 'error');
            setCurrentArchivePath(null);
        } finally {
            setLoading(false);
        }
    };

    const isIsoPath = (path) => typeof path === 'string'
        && path.toLowerCase().endsWith('.iso')
        && !path.includes('::');

    const handleShowInfo = (path) => {
        if (!path) return;
        const isIso = isIsoPath(path);
        if (isIso) {
            if (isoHandling !== 'dolphin') {
                notify('ISO info uses Dolphin tools. Switch ISO handling to Dolphin to view disc info.', 'info');
                return;
            }
            setShowCHDInfo({ path, infoMode: 'dolphin' });
            return;
        }
        if (is3dsFile(path)) {
            setShowCHDInfo({ path, infoMode: 'z3ds' });
            return;
        }
        if (isDolphinFile(path)) {
            setShowCHDInfo({ path, infoMode: 'dolphin' });
            return;
        }
        setShowCHDInfo({ path, infoMode: 'chd' });
    };

    const handleIsoHandlingToggle = useCallback(() => {
        setIsoHandling(prev => {
            if (prev === 'z3ds') {
                notify('ISO toggle is unavailable in 3DS mode. Switch Primary Tool to CHDMAN or Dolphin first.', 'info');
                return prev;
            }
            const next = prev === 'dolphin' ? 'chdman' : 'dolphin';
            notify(`ISO handling set to ${next === 'dolphin' ? 'Dolphin' : 'CHDMAN'}`, 'info');
            return next;
        });
    }, []);

    const handleToggleSelect = (entry, event) => {
        const index = paginatedEntries.findIndex(e => e.path === entry.path);

        // Handle shift-click range selection
        if (event?.shiftKey && lastSelectedIndex !== null && lastSelectedIndex !== index && index !== -1) {
            const start = Math.min(lastSelectedIndex, index);
            const end = Math.max(lastSelectedIndex, index);
            const range = paginatedEntries.slice(start, end + 1).filter(e => canSelectEntry(e));

            setSelectedFiles(prev => {
                const next = new Map(prev);
                range.forEach(e => next.set(e.path, e));
                return next;
            });
        } else {
            // Single toggle (existing behavior)
            setSelectedFiles(prev => {
                const next = new Map(prev);
                if (next.has(entry.path)) {
                    next.delete(entry.path);
                } else {
                    next.set(entry.path, entry);
                }
                return next;
            });
        }

        setLastSelectedIndex(index !== -1 ? index : null);
    };

    const handleSelectAll = () => {
        const selectable = paginatedEntries.filter(e => canSelectEntry(e));
        if (selectable.length === 0) return;
        setSelectedFiles(prev => {
            const next = new Map(prev);
            const allOnPageSelected = selectable.every(e => next.has(e.path));
            if (allOnPageSelected) {
                selectable.forEach(e => next.delete(e.path));
            } else {
                selectable.forEach(e => next.set(e.path, e));
            }
            return next;
        });
    };

    const handleSort = (column) => {
        if (sortBy === column) {
            // Toggle order if same column
            setSortOrder(prev => prev === 'asc' ? 'desc' : 'asc');
        } else {
            // New column, default to ascending
            setSortBy(column);
            setSortOrder('asc');
        }
        // Reset shift-click anchor when sort changes (order changes)
        setLastSelectedIndex(null);
    };

    const {
        applyConversionPreset,
        handlePresetSave,
        handlePresetDelete,
    } = useConversionPresetActions({
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
    });

    const {
        handleRename,
        handleDelete,
        handleVerify,
    } = useFileMutationHandlers({
        notify,
        isoHandling,
        refreshFileList,
        setVerifiedCHDs,
        setVerifyProgress,
    });

    const {
        getDeletableSelection,
        getVerifiableItems,
        handleBulkDeleteClick,
        handleBulkVerifyClick,
        handleBulkVerifyComplete,
        handleAddVerifiedCHD,
        handleBulkDeleteRefresh,
    } = useBulkFileActions({
        selectedFiles,
        notify,
        isoHandling,
        setBulkDeleteEntries,
        setBulkVerifyItems,
        setVerifiedCHDs,
        setSelectedFiles,
        refreshFileList,
    });

    // Helper to calculate expected output path
    const getExpectedOutputPath = (filePath, entry = null) => {
        // Get the filename (handle archive paths like "archive.zip::game.iso")
        const rawName = (filePath.includes('::') ? filePath.split('::').pop() : filePath);
        const filename = rawName.split('/').pop();
        const sourceExt = getFileExtension(rawName);
        const isArchiveItem = filePath.includes('::');
        // Build a safe stem for archive members to avoid collisions
        let stem;
        if (isArchiveItem) {
            if (entry && entry.output_stem) {
                stem = entry.output_stem;
            } else {
                const parentParts = rawName.split('/').slice(0, -1).filter(Boolean);
                const safePrefix = parentParts.length ? parentParts.join('_') + '_' : '';
                stem = safePrefix + filename.replace(/\.[^.]+$/, '');
            }
        } else {
            stem = filename.replace(/\.[^.]+$/, '');
        }
        let outputFilename = `${stem}.chd`;
        if (conversionMode === 'copy') {
            outputFilename = `${stem}_copy.chd`;
        } else if (conversionMode === 'extractcd') {
            outputFilename = `${stem}.cue`;
        } else if (conversionMode === 'extractdvd') {
            outputFilename = `${stem}.iso`;
        } else if (conversionMode === 'extractraw' || conversionMode === 'extracthd') {
            outputFilename = `${stem}.raw`;
        } else if (conversionMode === 'extractld') {
            outputFilename = `${stem}.avi`;
        } else if (conversionMode === 'dolphin_rvz') {
            outputFilename = `${stem}.rvz`;
        } else if (conversionMode === 'dolphin_wia') {
            outputFilename = `${stem}.wia`;
        } else if (conversionMode === 'dolphin_gcz') {
            outputFilename = `${stem}.gcz`;
        } else if (conversionMode === 'z3ds_compress') {
            outputFilename = `${stem}${Z3DS_OUTPUT_EXTENSION_BY_SOURCE[sourceExt] || '.z3ds'}`;
        }

        // Determine output directory
        let outDir;
        if (outputDir) {
            outDir = outputDir;
        } else if (filePath.includes('::')) {
            // For archive files, output goes next to the archive
            outDir = filePath.split('::')[0].split('/').slice(0, -1).join('/');
        } else {
            // For regular files, output goes next to the source
            outDir = filePath.split('/').slice(0, -1).join('/');
        }

        return `${outDir}/${outputFilename}`;
    };

    const requestDeletePlan = async (paths, duplicateAction) => {
        try {
            const plan = await api.getDeletePlan(paths, conversionMode);
            setDeletePlan({ plan, paths, duplicateAction });
        } catch (err) {
            notify(`✗ Failed to build delete plan: ${err.message}`, 'error');
        }
    };

    const handleDeletePlanConfirm = async () => {
        if (!deletePlan) return;
        const { paths, duplicateAction } = deletePlan;
        setDeletePlan(null);
        await executeConversion(paths, duplicateAction);
    };

    const handleDeletePlanClose = () => {
        setDeletePlan(null);
    };

    const maybeConfirmDeletePlan = async (paths, duplicateAction) => {
        if (deleteOnVerify && !deleteOnVerifyDisabled) {
            await requestDeletePlan(paths, duplicateAction);
            return false;
        }
        return executeConversion(paths, duplicateAction);
    };

    // Execute conversion with specified duplicate action
    const executeConversion = async (paths, duplicateAction = 'skip') => {
        if (hasMultipleDolphinCodecs) {
            notify('Dolphin formats support only one compression codec at a time', 'error');
            return false;
        }
        const isoInputs = paths.filter((path) => isIsoPath(path));
        if (isoInputs.length > 0) {
            if (isoHandling === null) {
                notify('Please select an ISO handling method (CHDMAN or Dolphin) before converting ISO files.', 'error');
                return false;
            }
            if (isoHandling === 'dolphin' && !isDolphinMode) {
                notify('ISO handling is set to Dolphin. Select a Dolphin mode to convert ISO files.', 'error');
                return false;
            }
            if (isoHandling === 'chdman' && isDolphinMode) {
                notify('ISO handling is set to CHDMAN. Select a CHDMAN create mode to convert ISO files.', 'error');
                return false;
            }
        }
        // Build optimistic placeholder jobs so the user sees immediate feedback
        const visiblePaths = paths.slice(0, MAX_VISIBLE_CREATING_PLACEHOLDERS);
        const hiddenPlaceholderCount = Math.max(0, paths.length - visiblePaths.length);
        const placeholders = visiblePaths.map((p, i) => {
            const entry = selectedFiles.get(p);
            return {
                id: `pending-${Date.now()}-${i}`,
                file_path: p,
                filename: (p.includes('::') ? p.split('::').pop() : p).split('/').pop(),
                mode: conversionMode,
                status: 'creating',
                progress: 0,
                message: 'Setting up job...',
                output_path: getExpectedOutputPath(p, entry)
            };
        });
        setCreatingJobs(placeholders);
        if (hiddenPlaceholderCount > 0) {
            notify(
                `Queueing ${paths.length} jobs. Showing ${visiblePaths.length} pending rows for responsiveness.`,
                'info',
            );
        }

        setConverting(true);
        try {
            notify(`⏳ Queueing ${paths.length} job(s)...`, 'info');

            const newJobs = await api.createBatchJobs(
                paths,
                conversionMode,
                outputDir || null,
                duplicateAction,
                compressionSupported ? getCompressionValue() : null,
                deleteOnVerify && !deleteOnVerifyDisabled
            );

            // Clear placeholders and prepend real jobs
            setCreatingJobs([]);
            setJobs(prev => [...newJobs, ...prev]);
            setSelectedFiles(new Map());

            if (newJobs.length > 0) {
                notify(`✓ Queued ${newJobs.length} job(s)`, 'success');
            } else {
                notify('ℹ No jobs created (all files were skipped)', 'info');
            }
            if (searchMode && searchAutoReturnToFileList) {
                restorePreSearchView();
            }
            return true;
        } catch (err) {
            const errorMsg = err.message || 'Unknown error occurred';
            // Mark placeholders as failed so user sees what went wrong
            setCreatingJobs(prev => prev.map(j => ({ ...j, status: 'failed', error_message: errorMsg, message: `Failed to create: ${errorMsg}` })));
            notify(`✗ Failed to create jobs: ${errorMsg}`, 'error');
            console.error('Failed to create jobs:', err);
            return false;
        } finally {
            // Remove failed placeholders after a short delay
            setTimeout(() => setCreatingJobs(prev => prev.filter(j => j.status !== 'failed')), 2500);
            setConverting(false);
        }
    };

    const handleConvert = async () => {
        const paths = Array.from(selectedFiles.keys());
        if (paths.length === 0) {
            notify('⚠ Please select at least one file', 'error');
            return;
        }

        // Strict Mode Validation
        // Identify invalid files for the current isoHandling mode
        const invalidFiles = paths.filter(path => {
            const is3dsSource = is3dsSourceFile(path);
            const is3dsRelated = is3dsFile(path);

            if (isoHandling === 'z3ds') {
                return !is3dsSource; // 3DS mode requires 3DS source files
            } else {
                return is3dsRelated; // Other modes refuse 3DS files
            }
        });

        if (invalidFiles.length > 0) {
            const modeName = isoHandling === 'z3ds' ? '3DS' : (isoHandling === 'dolphin' ? 'Dolphin' : 'CHDMAN');
            notify(`⛔ Compatibility Error: ${invalidFiles.length} file(s) are incompatible with ${modeName} mode. Please deselect them.`, 'error');
            return;
        }

        await startConversionSafely(paths);
    };

    const handleInlineCompress = async (entry) => {
        if (!entry) return;
        // Check if file is valid for current mode before proceeding?
        // The button shouldn't be visible if not, so we assume valid.
        await startConversionSafely([entry.path]);
    };

    const startConversionSafely = async (paths) => {
        // Show loading state immediately to prevent UI appearing frozen
        setConverting(true);

        // Check for duplicates
        try {
            const duplicates = await api.checkDuplicates(paths, outputDir || null, conversionMode);
            const hasDuplicates = duplicates.some(d => d.exists);

            if (hasDuplicates) {
                // Show duplicate handling modal (pause converting state while modal is shown)
                setConverting(false);
                setDuplicateCheck({ duplicates, paths });
                return false;
            }

            // No duplicates, proceed directly (executeConversion will manage converting state)
            setConverting(false);
            return await maybeConfirmDeletePlan(paths, 'skip');
        } catch (err) {
            setConverting(false);
            notify(`✗ Failed to check for duplicates: ${err.message}`, 'error');
            console.error('Duplicate check failed:', err);
            return false;
        }
    };

    const handleDuplicateAction = async (action) => {
        if (!duplicateCheck) return;

        const { paths } = duplicateCheck;
        setDuplicateCheck(null); // Close modal

        await maybeConfirmDeletePlan(paths, action);
    };

    const compressionOptions = [
        { value: 'none', label: 'No compression', description: 'Stores data without compression.' },
        { value: 'zlib', label: 'zlib', description: 'Deflate compression. Broad compatibility.' },
        { value: 'zstd', label: 'zstd', description: 'High performance and ratio, but older software may not support it.' },
        { value: 'lzma', label: 'lzma', description: 'High compression ratio, slower.' },
        { value: 'huff', label: 'huff', description: 'Huffman coding.' },
        { value: 'flac', label: 'flac', description: 'Audio (stereo 16-bit 44.1kHz PCM). Good for audio data.' },
        { value: 'cdzl', label: 'cdzl', description: 'CD-ROM data: zlib for audio and subchannel.' },
        { value: 'cdzs', label: 'cdzs', description: 'CD-ROM data: zstd for audio and subchannel.' },
        { value: 'cdlz', label: 'cdlz', description: 'CD-ROM data: LZMA for audio + zlib for subchannel.' },
        { value: 'cdfl', label: 'cdfl', description: 'CD-ROM data: FLAC for audio + zlib for subchannel.' },
        { value: 'avhu', label: 'avhu', description: 'Huffman for A/V data (LaserDisc).' }
    ];

    const dolphinCompressionOptions = [
        { value: 'none', label: 'No compression', description: 'Uncompressed output.' },
        { value: 'zstd', label: 'zstd', description: 'Best balance of speed and compression (recommended).' },
        { value: 'bzip2', label: 'bzip2', description: 'Good compression, slower.' },
        { value: 'lzma', label: 'lzma', description: 'High compression ratio.' },
        { value: 'lzma2', label: 'lzma2', description: 'Improved LZMA variant.' },
    ];

    const isCreateMode = conversionMode.startsWith('create');
    const isExtractMode = conversionMode.startsWith('extract');
    const isCopyMode = conversionMode === 'copy';
    const isDolphinMode = conversionMode.startsWith('dolphin_');
    const isZ3dsMode = conversionMode === 'z3ds_compress';
    const isDolphinCompressible = isDolphinMode && !['dolphin_iso', 'dolphin_gcz'].includes(conversionMode);
    const activeCompressionOptions = isDolphinCompressible ? dolphinCompressionOptions : compressionOptions;
    const compressionSupported = isCreateMode || isCopyMode || isDolphinCompressible;
    const dolphinCodecValues = isDolphinCompressible
        ? new Set(activeCompressionOptions.map((opt) => opt.value))
        : null;
    const selectedDolphinCodec = isDolphinCompressible
        ? (compressionSelection.find((value) => value !== 'none' && dolphinCodecValues.has(value)) || 'none')
        : null;
    const dolphinLevelEnabled = Boolean(isDolphinCompressible && selectedDolphinCodec && selectedDolphinCodec !== 'none');
    const normalizedDolphinLevel = normalizeDolphinLevel(dolphinCompressionLevel);
    const hasMultipleDolphinCodecs = isDolphinCompressible
        && compressionSelection.filter((value) => value !== 'none').length > 1;
    const hasArchiveSelection = useMemo(() => {
        for (const [path, entry] of selectedFiles) {
            if (path.includes('::') || entry?.is_archive_item) {
                return true;
            }
        }
        return false;
    }, [selectedFiles]);
    const deleteOnVerifySupported = isCreateMode || isCopyMode || isDolphinMode || isZ3dsMode;
    const deleteOnVerifyDisabled = !deleteOnVerifySupported;
    const deleteOnVerifyLabel = isCopyMode
        ? 'Delete original CHD after copy + verify'
        : isZ3dsMode
            ? 'Delete source after compress'
            : 'Delete source after convert + verify';
    const getDeleteOnVerifyNote = () => {
        if (!deleteOnVerifySupported) {
            return 'Available only for create/copy/Dolphin/3DS modes.';
        }
        if (hasArchiveSelection) {
            return 'Archive inputs will delete the entire archive after verification.';
        }
        if (isCopyMode) {
            return 'Warning: this deletes the original CHD after the copy verifies.';
        }
        if (isDolphinMode) {
            return 'Runs Dolphin disc verification and deletes the original source if it passes.';
        }
        if (isZ3dsMode) {
            return 'Runs 3DS integrity verification and deletes the original source if it passes.';
        }
        return 'Runs CHD verification and deletes the original source (including .cue/.gdi track files) if it passes.';
    };
    const deleteOnVerifyNote = getDeleteOnVerifyNote();

    const getDeleteOnVerifyTitle = () => {
        if (isDolphinMode) {
            return 'Verify output disc image, then delete the source files';
        }
        if (isZ3dsMode) {
            return 'Verify compressed 3DS output, then delete the source files';
        }
        return 'Verify output CHD, then delete the source files';
    };
    const deleteOnVerifyTitle = getDeleteOnVerifyTitle();
    const outputTitle = isExtractMode
        ? 'Optional: Specify a custom directory for extracted files'
        : isDolphinMode
            ? 'Optional: Specify a custom directory for output disc images'
            : isZ3dsMode
                ? 'Optional: Specify a custom directory for compressed 3DS files'
                : 'Optional: Specify a custom directory for output CHD files';
    const outputHint = isExtractMode
        ? 'Leave empty to save extracted files next to source files.'
        : isDolphinMode
            ? 'Leave empty to save Dolphin files next to source files.'
            : isZ3dsMode
                ? 'Leave empty to save compressed files next to source files.'
                : 'Leave empty to save CHD files next to source files.';
    const selectedEntries = useMemo(() => Array.from(selectedFiles.values()), [selectedFiles]);
    const modeVisibility = useMemo(() => {
        if (selectedEntries.length === 0) {
            if (isoHandling === 'dolphin') {
                return { create: false, extract: false, copy: false, dolphin: true, z3ds: false };
            }
            if (isoHandling === 'z3ds') {
                return { create: false, extract: false, copy: false, dolphin: false, z3ds: true };
            }
            return { create: true, extract: true, copy: true, dolphin: false, z3ds: false };
        }
        let allowCreate = true;
        let allowExtract = true;
        let allowCopy = true;
        let allowDolphin = true;
        let allowZ3ds = true;
        for (const entry of selectedEntries) {
            const ext = entry.extension?.toLowerCase();
            const isIso = ext === '.iso';
            const isChd = ext === '.chd';
            const inArchive = Boolean(entry.is_archive_item || entry.in_archive || entry.path?.includes('::'));
            const canDolphin = entry.dolphin_convertible === true
                && !inArchive
                && (!isIso || isoHandling === 'dolphin');
            const canChdCreate = entry.convertible === true
                && !isChd
                && (!isIso || isoHandling !== 'dolphin');
            const canZ3ds = entry.z3ds_convertible === true && !inArchive;
            allowCreate = allowCreate && canChdCreate;
            allowExtract = allowExtract && isChd;
            allowCopy = allowCopy && isChd;
            allowDolphin = allowDolphin && canDolphin;
            allowZ3ds = allowZ3ds && canZ3ds;
        }
        if (isoHandling === 'dolphin') {
            return {
                create: false,
                extract: false,
                copy: false,
                dolphin: allowDolphin,
                z3ds: false
            };
        }
        if (isoHandling === 'z3ds') {
            return {
                create: false,
                extract: false,
                copy: false,
                dolphin: false,
                z3ds: allowZ3ds
            };
        }
        return {
            create: allowCreate,
            extract: allowExtract,
            copy: allowCopy,
            dolphin: false,
            z3ds: false
        };
    }, [selectedEntries, isoHandling]);
    const visibleModeGroups = useMemo(() => {
        const filtered = MODE_GROUPS.filter((group) => modeVisibility[group.id]);
        if (filtered.length) return filtered;
        if (isoHandling === 'dolphin') {
            const dolphinGroup = MODE_GROUPS.find((group) => group.id === 'dolphin');
            return dolphinGroup ? [dolphinGroup] : MODE_GROUPS;
        }
        if (isoHandling === 'z3ds') {
            const z3dsGroup = MODE_GROUPS.find((group) => group.id === 'z3ds');
            return z3dsGroup ? [z3dsGroup] : MODE_GROUPS;
        }
        const chdGroups = MODE_GROUPS.filter((group) => group.id !== 'dolphin' && group.id !== 'z3ds');
        return chdGroups.length ? chdGroups : MODE_GROUPS;
    }, [modeVisibility, isoHandling]);
    useEffect(() => {
        const hasCurrent = visibleModeGroups.some((group) =>
            group.options.some((opt) => opt.value === conversionMode)
        );
        if (!hasCurrent) {
            const fallback = visibleModeGroups[0]?.options[0]?.value;
            if (fallback) {
                setConversionMode(fallback);
            }
        }
    }, [visibleModeGroups, conversionMode]);
    const compressionMetaText = !compressionSupported
        ? 'Compression options not applicable for this mode'
        : isDolphinCompressible
            ? (selectedDolphinCodec === 'none'
                ? 'No compression (-c none)'
                : `Codec: ${selectedDolphinCodec} • Level: ${normalizedDolphinLevel}`)
            : (compressionSelection.includes('none')
                ? 'No compression (-c none)'
                : `${compressionSelection.length}/${isDolphinCompressible ? activeCompressionOptions.length : 4} codecs selected`);

    useEffect(() => {
        if (deleteOnVerifyDisabled && deleteOnVerify) {
            setDeleteOnVerify(false);
        }
    }, [deleteOnVerifyDisabled, deleteOnVerify]);

    useEffect(() => {
        if (!isDolphinCompressible) {
            return;
        }
        const allowed = new Set(activeCompressionOptions.map((opt) => opt.value));
        const filtered = compressionSelection.filter((value) => allowed.has(value));
        if (filtered.length === 0) {
            if (compressionSelection.length !== 1 || compressionSelection[0] !== 'none') {
                setCompressionSelection(['none']);
            }
            return;
        }
        const unique = Array.from(new Set(filtered));
        if (unique.length > 1) {
            const preferred = activeCompressionOptions.find(
                (opt) => opt.value !== 'none' && unique.includes(opt.value)
            );
            const next = preferred ? [preferred.value] : ['none'];
            if (next.length !== compressionSelection.length || next[0] !== compressionSelection[0]) {
                setCompressionSelection(next);
            }
            return;
        }
        if (compressionSelection.length !== 1 || compressionSelection[0] !== unique[0]) {
            setCompressionSelection([unique[0]]);
        }
    }, [isDolphinCompressible, activeCompressionOptions, compressionSelection]);

    useEffect(() => {
        if (!deleteOnVerify && deletePlan) {
            setDeletePlan(null);
        }
    }, [deleteOnVerify, deletePlan]);

    useEffect(() => {
        const failures = [];
        if (buildCompressionValue(['none'], compressionOptions) !== 'none') {
            failures.push('none');
        }
        if (buildCompressionValue(['zlib'], compressionOptions) !== 'zlib') {
            failures.push('zlib');
        }
        if (buildCompressionValue(['lzma', 'zlib'], compressionOptions) !== 'zlib,lzma') {
            failures.push('multi');
        }
        if (failures.length) {
            console.error('Compression self-check failed:', failures.join(', '));
        }
    }, []);

    const toggleCompression = (value) => {
        if (!compressionSupported) {
            notify('Compression options are available only for create/copy modes', 'info');
            return;
        }
        const dolphinCompressible = conversionMode.startsWith('dolphin_')
            && !['dolphin_iso', 'dolphin_gcz'].includes(conversionMode);
        setCompressionSelection((prev) => {
            if (value === 'none') {
                return ['none'];
            }
            if (dolphinCompressible) {
                if (prev.length === 1 && prev[0] === value) {
                    return ['none'];
                }
                return [value];
            }

            const next = new Set(prev);
            if (next.has(value)) {
                next.delete(value);
            } else {
                if (next.size >= 4) {
                    notify('You can select up to 4 compression codecs', 'info');
                    return Array.from(next);
                }
                next.add(value);
            }
            next.delete('none');
            if (next.size === 0) {
                return ['none'];
            }
            return Array.from(next);
        });
    };

    const getCompressionValue = () => {
        const baseValue = buildCompressionValue(compressionSelection, activeCompressionOptions);
        if (!isDolphinCompressible) {
            return baseValue;
        }
        if (!baseValue || baseValue === 'none') {
            return baseValue;
        }
        const level = normalizeDolphinLevel(dolphinCompressionLevel);
        return `${baseValue}:${level}`;
    };

    const canSelectEntry = (entry) => {
        if (!entry || entry.type === 'directory' || entry.type === 'archive') return false;
        if (entry.extension === '.iso') {
            if (isDolphinMode && isoHandling === 'chdman') return false;
            if (!isDolphinMode && isoHandling === 'dolphin') return false;
        }
        if (isDolphinMode) {
            return entry.dolphin_convertible === true;
        }
        if (isZ3dsMode) {
            return entry.z3ds_convertible === true;
        }
        if (isExtractMode || isCopyMode) {
            return entry.extension === '.chd';
        }
        if (conversionMode === 'createcd' || conversionMode === 'createdvd') {
            return entry.convertible;
        }
        if (isCreateMode) {
            return entry.extension !== '.chd';
        }
        return false;
    };

    useEffect(() => {
        setSelectedFiles(prev => {
            if (prev.size === 0) return prev;
            let removed = 0;
            const next = new Map();
            prev.forEach((entry, path) => {
                if (canSelectEntry(entry)) {
                    next.set(path, entry);
                } else {
                    removed += 1;
                }
            });
            if (removed > 0) {
                notify(`ℹ Cleared ${removed} incompatible selection(s) for this mode`, 'info');
                return next;
            }
            return prev;
        });
    }, [conversionMode]);

    const getModeWarnings = () => {
        const entries = Array.from(selectedFiles.values());
        if (!entries.length) return [];
        const cdMax = 900 * 1024 * 1024;
        const dvdMin = 1200 * 1024 * 1024;
        const isDiscImage = (entry) => {
            const ext = entry.extension?.toLowerCase();
            return ext === '.iso' || ext === '.bin';
        };
        const withSize = entries.filter((e) => isDiscImage(e) && typeof e.size === 'number' && e.size > 0);
        const dvdLikely = withSize.filter((e) => e.size >= dvdMin);
        const cdLikely = withSize.filter((e) => e.size <= cdMax);
        const warnings = [];
        if (conversionMode === 'createcd' && dvdLikely.length) {
            const sample = dvdLikely.slice(0, 2).map((e) => `${e.name} (${formatSize(e.size)})`).join(', ');
            warnings.push(`Some selected files look DVD-sized but CD mode is selected. Consider DVD mode. ${sample}${dvdLikely.length > 2 ? ` (+${dvdLikely.length - 2} more)` : ''}`);
        }
        if (conversionMode === 'createdvd' && cdLikely.length) {
            const sample = cdLikely.slice(0, 2).map((e) => `${e.name} (${formatSize(e.size)})`).join(', ');
            warnings.push(`Some selected files look CD-sized but DVD mode is selected. Consider CD mode. ${sample}${cdLikely.length > 2 ? ` (+${cdLikely.length - 2} more)` : ''}`);
        }
        return warnings;
    };

    const getActionLabel = () => {
        if (isDolphinMode) return 'Convert';
        if (isZ3dsMode) return 'Compress';
        if (isExtractMode) return 'Extract';
        if (isCopyMode) return 'Copy';
        return 'Convert';
    };

    const {
        handleSearch,
        handleAutoQueueFolder,
        handleAutoQueuePromptCap,
        handleAutoQueuePromptAll,
        handleScanMetadata,
    } = useSearchQueueActions({
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
    });

    const {
        handleCancelJob,
        handleRequestCancelAll,
        handleCancelAllJobs,
        handleRequestClearCompleted,
        handleClearCompleted,
        handleRecoverStuck,
    } = useJobAdminActions({
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
    });


    const queuedJobsCount = jobs.filter(j => j.status === 'queued').length;
    const processingJobsCount = jobs.filter(j => j.status === 'processing').length;
    const activeJobsCount = queuedJobsCount + processingJobsCount;
    const hasActiveJobs = activeJobsCount > 0;
    const hasCompletedJobs = jobs.some(j => ['completed', 'failed', 'cancelled'].includes(j.status));
    const selectableEntriesOnPage = paginatedEntries.filter(e => canSelectEntry(e));
    const allSelectedOnPage = selectableEntriesOnPage.length > 0
        && selectableEntriesOnPage.every((entry) => selectedFiles.has(entry.path));
    const needsIsoSelection = isoHandling === null;
    const handlePageChange = (nextPage) => {
        const bounded = Math.min(Math.max(nextPage, 1), pagination.totalPages);
        setCurrentPage(bounded);
        setLastSelectedIndex(null);
    };
    const handleJobPageChange = (nextPage) => {
        const bounded = Math.min(Math.max(nextPage, 1), jobsPagination.totalPages);
        setJobCurrentPage(bounded);
    };
    const queueTabJobsCount = queueJobs.length;
    const completedTabJobsCount = completedJobs.length;
    const issuesTabJobsCount = issueJobs.length;
    const totalJobsCount = queueTabJobsCount + completedTabJobsCount + issuesTabJobsCount;
    const showIssuesTab = issuesTabJobsCount > 0 || jobTab === 'issues';

    return html`
        <div class="container">
            ${notification && html`
                <div class="notification ${notification.type}">
                    ${notification.message}
                </div>
            `}

            <header>
                <div class="header-brand">
                    <img src="/static/images/logo.png" alt="" class="header-logo" />
                    <div>
                        <h1><span>Compressatorium</span></h1>
                        <span class="subtitle">Convert and compress game disc images</span>
                    </div>
                </div>
                <div class="header-actions">
                    <button
                        class="btn btn-secondary help-btn"
                        onClick=${() => handleScanMetadata(false)}
                        title="Scan all volumes for CHD metadata"
                    >
                        Scan Metadata
                    </button>
                    <button
                        class="btn btn-secondary help-btn"
                        onClick=${() => handleScanMetadata(true)}
                        title="Rescan all CHD metadata (ignore cache)"
                    >
                        Force Rescan
                    </button>
                    <button
                        class="btn btn-secondary help-btn"
                        onClick=${() => setShowHelp(!showHelp)}
                        title="Show help"
                    >
                        ${showHelp ? 'Hide Help' : '? Help'}
                    </button>
                </div>
            </header>

            <div class="iso-tool-banner${needsIsoSelection ? ' iso-tool-banner-warning' : ''}">
                <div class="iso-tool-title">Primary Tool${needsIsoSelection ? ' - Selection Required' : ''}</div>
                <div class="iso-tool-options" role="radiogroup" aria-label="Primary tool selection">
                    <label class="iso-option">
                        <input
                            type="radio"
                            name="iso-tool"
                            value="chdman"
                            checked=${isoHandling === 'chdman'}
                            onChange=${() => setIsoHandling('chdman')}
                        />
                        <div class="iso-option-text">
                            <strong>CHDMAN</strong>
                            <span>CHD conversion (CD/DVD/LD)</span>
                        </div>
                    </label>
                    <label class="iso-option">
                        <input
                            type="radio"
                            name="iso-tool"
                            value="dolphin"
                            checked=${isoHandling === 'dolphin'}
                            onChange=${() => setIsoHandling('dolphin')}
                        />
                        <div class="iso-option-text">
                            <strong>Dolphin</strong>
                            <span>GameCube/Wii (RVZ/WIA/GCZ)</span>
                        </div>
                    </label>
                    <label class="iso-option">
                        <input
                            type="radio"
                            name="iso-tool"
                            value="z3ds"
                            checked=${isoHandling === 'z3ds'}
                            onChange=${() => setIsoHandling('z3ds')}
                        />
                        <div class="iso-option-text">
                            <strong>3DS</strong>
                            <span>Nintendo 3DS ROMs</span>
                        </div>
                    </label>
                    <label class="iso-option">
                        <input
                            type="radio"
                            name="iso-tool"
                            value="igir"
                            checked=${isoHandling === 'igir'}
                            onChange=${() => setIsoHandling('igir')}
                        />
                        <div class="iso-option-text">
                            <strong>igir</strong>
                            <span>ROM collection manager</span>
                        </div>
                    </label>
                </div>
            <div class="iso-tool-hint${needsIsoSelection ? ' iso-tool-hint-warning' : ''}">
                ${getPrimaryToolHint(isoHandling)}
            </div>
        </div>

            ${showHelp && html`<${HelpPanel} onClose=${() => setShowHelp(false)} isoHandling=${isoHandling} />`}

            ${isoHandling === 'igir' ? html`
                <${IgirView} volumes=${volumes} notify=${notify} />
            ` : html`
            <div class="main-layout">
                <!-- Volumes Panel -->
                <div class="panel">
                    <div class="panel-header">
                        <h2>Volumes</h2>
                    </div>
                    <div class="panel-content">
                        <${VolumeList}
                            volumes=${volumes}
                            selectedVolume=${selectedVolume}
                            onSelect=${handleVolumeSelect}
                            loading=${volumesLoading}
                            error=${volumesError}
                        />
                    </div>
                </div>

                <!-- Files Panel -->
                <div class="panel">
                    <div class="panel-header">
                        <h2>Files</h2>
                        <div class="header-actions">
                            ${searchMode && html`
                                <button
                                    class="btn btn-sm btn-secondary"
                                    onClick=${restorePreSearchView}
                                    title="Return to the file list view from before Search All"
                                >
                                    ← File List
                                </button>
                            `}
                            ${!searchMode && html`
                                <button
                                    class="btn btn-sm btn-secondary"
                                    onClick=${() => refreshFileList(true)}
                                    disabled=${loading || !currentPath}
                                    title="Refresh file list"
                                >
                                    ↻
                                </button>
                                <label class="auto-refresh-toggle" title="Auto-refresh file list every 3 seconds">
                                    <input
                                        type="checkbox"
                                        checked=${autoRefresh}
                                        onChange=${(e) => setAutoRefresh(e.target.checked)}
                                    />
                                    <span class="auto-refresh-label">Live${autoRefresh ? ' ●' : ''}</span>
                                </label>
                            `}
                            <button
                                class="btn btn-sm btn-secondary"
                                onClick=${handleSearch}
                                disabled=${loading || !currentPath}
                                title="Search recursively for all convertible files"
                            >
                                🔍 Search All
                            </button>
                            <button
                                class="btn btn-sm btn-primary"
                                onClick=${handleAutoQueueFolder}
                                disabled=${loading || !currentPath || converting || autoQueueing}
                                title="Automatically scan, select compatible files, and queue conversion jobs"
                            >
                                ${autoQueueing ? '⏳ Auto Queue...' : '⚡ Auto Queue Folder'}
                            </button>
                        </div>
                    </div>

                    <${Breadcrumb}
                        path=${currentPath}
                        volume=${selectedVolume}
                        onNavigate=${handleNavigate}
                    />

                    ${currentArchivePath && html`
                        <div class="archive-indicator">
                            <button
                                class="btn btn-sm btn-secondary"
                                onClick=${() => {
                setCurrentArchivePath(null);
                setSelectedFiles(new Map());
                setCurrentPage(1);
                setLastSelectedIndex(null);
                refreshFileList(true);
            }}
                                title="Return to folder view"
                            >
                                ← Back
                            </button>
                            <span class="archive-name" title=${currentArchivePath}>
                                📦 Viewing: ${currentArchivePath.split('/').pop()}
                            </span>
                        </div>
                    `}

                    ${searchMode && searchResults && html`
                        <div class="search-results">
                            <h3>Found ${searchResults.total_files} file(s), ${searchResults.total_in_archives} in archives</h3>
                        </div>
                    `}

                    <div class="toolbar">
                        <div class="toolbar-row">
                            <div class="toolbar-group">
                                <span class="toolbar-label">Mode</span>
                                <select
                                    value=${conversionMode}
                                    onFocus=${beginUiSelectionInteraction}
                                    onBlur=${endUiSelectionInteraction}
                                    onMouseDown=${beginUiSelectionInteraction}
                                    onChange=${(e) => {
            endUiSelectionInteraction();
            setConversionMode(e.target.value);
        }}
                                    title="Select conversion mode based on your disc type"
                                >
                                    ${visibleModeGroups.map((group) => html`
                                        <optgroup label=${group.label}>
                                            ${group.options.map((opt) => html`
                                                <option value=${opt.value}>${opt.label}</option>
                                            `)}
                                        </optgroup>
                                    `)}
                                </select>
                                <div class="toolbar-hint">
                                    ${isoHandling === 'dolphin'
            ? 'Switch Primary Tool to CHDMAN to see CHD modes.'
            : isoHandling === 'z3ds'
                ? null
                : 'Switch Primary Tool to Dolphin to see Dolphin modes.'}
                                </div>
                            </div>
                        </div>

                        <div class="toolbar-row actions" style="justify-content: space-between;">
                            <div class="toolbar-group">
                                <span class="toolbar-label">Presets</span>
                                <select
                                    value=${selectedPresetId}
                                    onFocus=${beginUiSelectionInteraction}
                                    onBlur=${endUiSelectionInteraction}
                                    onMouseDown=${beginUiSelectionInteraction}
                                    onChange=${(e) => {
            endUiSelectionInteraction();
            const presetId = e.target.value;
            setSelectedPresetId(presetId);
            if (presetId) {
                applyConversionPreset(presetId);
            }
        }}
                                    title="Apply a saved conversion preset"
                                >
                                    <option value="">Saved presets...</option>
                                    ${conversionPresets.map((preset) => html`
                                        <option value=${preset.id}>
                                            ${preset.name}
                                        </option>
                                    `)}
                                </select>
                            </div>
                            <div class="toolbar-actions">
                                <button
                                    class="btn btn-sm btn-secondary"
                                    onClick=${handlePresetSave}
                                    title="Save current conversion settings as a preset"
                                >
                                    Save Preset
                                </button>
                                <button
                                    class="btn btn-sm btn-secondary"
                                    onClick=${handlePresetDelete}
                                    disabled=${!selectedPresetId}
                                    title="Delete selected preset"
                                >
                                    Delete Preset
                                </button>
                            </div>
                        </div>
                        
                        ${!isZ3dsMode && html`
                            <div class="toolbar-row">
                                <div class="compression-group" role="group" aria-label="Compression options">
                                    <span class="compression-label">Compression</span>
                                    <div class="compression-options">
                                        ${activeCompressionOptions.map((opt) => html`
                                            <label class="compression-option" title=${opt.description}>
                                                <input
                                                    type="checkbox"
                                                    checked=${compressionSelection.includes(opt.value)}
                                                    disabled=${!compressionSupported}
                                                    onChange=${() => toggleCompression(opt.value)}
                                                />
                                                <span>${opt.label}</span>
                                            </label>
                                        `)}
                                    </div>
                                    ${isDolphinCompressible && html`
                                        <div class="compression-level">
                                            <span class="compression-level-label">Level</span>
                                            <input
                                                type="number"
                                                inputmode="numeric"
                                                min="1"
                                                max="22"
                                                step="1"
                                                value=${dolphinCompressionLevel}
                                                disabled=${!compressionSupported || !dolphinLevelEnabled}
                                                onInput=${(e) => setDolphinCompressionLevel(e.target.value)}
                                                onBlur=${(e) => setDolphinCompressionLevel(normalizeDolphinLevel(e.target.value))}
                                                title="Dolphin codecs require a compression level"
                                            />
                                            <span class="compression-level-hint">
                                                ${dolphinLevelEnabled ? 'Higher = smaller, slower.' : 'Select a codec to set level.'}
                                            </span>
                                        </div>
                                    `}
                                    <div class="compression-meta">
                                        <span>${compressionMetaText || 'Compression options not applicable for this mode'}</span>
                                        <button class="btn btn-sm btn-secondary" onClick=${() => setShowCompressionHelp(v => !v)}>
                                            ${showCompressionHelp ? 'Hide Info' : 'Compression Info'}
                                        </button>
                                    </div>
                                    ${hasMultipleDolphinCodecs && html`
                                        <div class="compression-warning" role="alert">
                                            Dolphin formats support only one compression codec.
                                        </div>
                                    `}
                                    <span class="compression-hint">
                                        ${isDolphinCompressible ? 'Choose one codec and set a level for Dolphin formats.' : 'Choose up to 4 codecs. zlib is the most compatible option.'}
                                    </span>
                                </div>
                            </div>
                        `}

                        <div class="toolbar-row actions" style="justify-content: space-between;">
                            <div class="toolbar-group">
                                <span class="toolbar-label">Filter</span>
                                ${customFilterMode ? html`
                                    <div class="custom-filter-input">
                                        <input
                                            type="text"
                                            class="filter-input"
                                            placeholder=".ext1, .ext2"
                                            value=${fileTypeFilter || ''}
                                            onInput=${(e) => {
                setFileTypeFilter(e.target.value || null);
                setCurrentPage(1);
                setLastSelectedIndex(null);
            }}
                                            title="Enter comma-separated extensions (e.g. .bin, .cue)"
                                        />
                                        <button
                                            class="btn btn-sm btn-secondary"
                                            onClick=${() => {
                setCustomFilterMode(false);
                setFileTypeFilter(null);
                setCurrentPage(1);
                setLastSelectedIndex(null);
            }}
                                            title="Clear custom filter"
                                        >
                                            ✕
                                        </button>
                                    </div>
                                ` : html`
                                    <select
                                        class="file-type-filter"
                                        value=${fileTypeFilter || ''}
                                        onFocus=${beginUiSelectionInteraction}
                                        onBlur=${endUiSelectionInteraction}
                                        onMouseDown=${beginUiSelectionInteraction}
                                        onChange=${(e) => {
                endUiSelectionInteraction();
                if (e.target.value === 'custom') {
                    setCustomFilterMode(true);
                    setFileTypeFilter('');
                } else {
                    setFileTypeFilter(e.target.value || null);
                }
                setCurrentPage(1);
                setLastSelectedIndex(null);
            }}
                                        title="Filter files by type"
                                    >
                                        ${getFilterOptions(isoHandling).map(opt => html`
                                            <option value=${opt.value}>${opt.label}</option>
                                        `)}
                                    </select>
                                `}
                            </div>
                            <div class="toolbar-actions">
                                <button
                                    class="btn btn-primary"
                                    disabled=${selectedFiles.size === 0 || converting}
                                    onClick=${handleConvert}
                                    title=${converting ? `${getActionLabel()}...` : selectedFiles.size > 0 ? `${getActionLabel()} ${selectedFiles.size} selected file(s)` : `Select files to ${getActionLabel().toLowerCase()}`}
                                >
                                    ${converting
            ? html`<span class="spinner" style="display: inline-block; width: 12px; height: 12px; margin-right: 8px; border-width: 2px;"></span>${getActionLabel()}...`
            : `${getActionLabel()} ${selectedFiles.size > 0 ? `(${selectedFiles.size})` : ''}`
        }
                                </button>
                                ${getDeletableSelection().length > 0 && html`
                                    <button
                                        class="btn btn-sm btn-secondary"
                                        onClick=${handleBulkDeleteClick}
                                        title="Delete ${getDeletableSelection().length} selected file(s)"
                                    >
                                        🗑️ Delete (${getDeletableSelection().length})
                                    </button>
                                `}
                                ${getVerifiableItems().length > 0 && html`
                                    <button
                                        class="btn btn-sm btn-secondary"
                                        onClick=${handleBulkVerifyClick}
                                        title="Verify ${getVerifiableItems().length} selected file(s)"
                                    >
                                        🔍 Verify (${getVerifiableItems().length})
                                    </button>
                                `}
                            </div>
                        </div>
                    </div>

                    ${showCompressionHelp && html`
                        <div class="compression-help">
                            <h4>${isDolphinMode ? 'Dolphin Compression Guide' : 'Compression Guide'}</h4>
                            ${isDolphinMode ? html`
                                <ul>
                                    <li><strong>No compression</strong>: stores data uncompressed (<code>-c none</code>).</li>
                                    <li><strong>zstd</strong>: best balance of speed and size (recommended).</li>
                                    <li><strong>bzip2</strong>: good compression, slower.</li>
                                    <li><strong>lzma/lzma2</strong>: highest compression, slowest.</li>
                                    <li><strong>Level</strong>: required for Dolphin codecs; higher means smaller files but slower encoding.</li>
                                    <li><strong>GCZ</strong>: fixed deflate compression (no codec/level selection).</li>
                                    <li><strong>ISO</strong>: uncompressed extraction.</li>
                                </ul>
                                <p class="compression-note">
                                    If unsure, start with <strong>zstd</strong> at level <strong>${normalizedDolphinLevel}</strong>.
                                </p>
                            ` : html`
                                <ul>
                                    <li><strong>No compression</strong>: passes <code>-c none</code> for uncompressed output.</li>
                                    <li><strong>zlib</strong>: best overall compatibility.</li>
                                    <li><strong>zstd</strong>: fast and small, but older software may not support it.</li>
                                    <li><strong>lzma</strong>: highest compression, slowest.</li>
                                    <li><strong>huff</strong>: Huffman coding, moderate compression.</li>
                                    <li><strong>flac</strong>: audio-only compression for stereo PCM audio.</li>
                                    <li><strong>cdzl/cdzs/cdlz/cdfl</strong>: CD-specific mixes of audio/subchannel codecs.</li>
                                    <li><strong>avhu</strong>: Huffman for A/V (LaserDisc).</li>
                                </ul>
                                <p class="compression-note">
                                    If unsure, choose <strong>zlib</strong>. It's the most compatible choice.
                                </p>
                                <p class="compression-note">
                                    Omitting <code>-c</code> would use chdman defaults; this app always sends an explicit choice.
                                </p>
                            `}
                        </div>
                    `}

                    ${getModeWarnings().map((warning, idx) => html`
                        <div key=${`mode-warning-${idx}`} class="mode-warning">
                            ⚠️ ${warning}
                        </div>
                    `)}

                    <div class="conversion-options">
                        <div class="option-card">
                            <span class="option-label">Output directory</span>
                            <input
                                type="text"
                                placeholder="Same as source (leave empty)"
                                value=${outputDir}
                                onInput=${(e) => setOutputDir(e.target.value)}
                                title=${outputTitle}
                            />
                            <span class="option-hint">${outputHint}</span>
                        </div>
                        <div class="option-card">
                            <span class="option-label">Post-conversion</span>
                            <label class="toggle-option" title=${deleteOnVerifyTitle}>
                                <input
                                    type="checkbox"
                                    checked=${deleteOnVerify}
                                    disabled=${deleteOnVerifyDisabled}
                                    onChange=${(e) => setDeleteOnVerify(e.target.checked)}
                                />
                                <span>${deleteOnVerifyLabel}</span>
                            </label>
                            <span class="option-hint">${deleteOnVerifyNote}</span>
                        </div>
                    </div>

                    ${selectedFiles.size > 0 && html`
                        <div class="output-dir-display">
                            <span class="icon">📁</span>
                            <span class="path" title=${outputDir || currentPath || 'Source file location'}>
                                <strong>Output:</strong> ${outputDir || currentPath || 'Same folder as source files'}
                            </span>
                            <span style="opacity: 0.7;">(${selectedFiles.size} file${selectedFiles.size > 1 ? 's' : ''} selected)</span>
                        </div>
                    `}

                    <div class="pagination-controls">
                        <div class="pagination-summary" aria-live="polite" aria-atomic="true">
                            ${pagination.totalItems > 0
            ? `Showing ${pagination.start}-${pagination.end} of ${pagination.totalItems} item${pagination.totalItems === 1 ? '' : 's'}`
            : 'Showing 0 items'}
                        </div>
                        <div class="pagination-actions">
                            <label class="pagination-page-size">
                                <span>Items per page</span>
                                <select
                                    value=${itemsPerPage}
                                    onFocus=${beginUiSelectionInteraction}
                                    onBlur=${endUiSelectionInteraction}
                                    onMouseDown=${beginUiSelectionInteraction}
                                    onChange=${(e) => {
            endUiSelectionInteraction();
            setItemsPerPage(e.target.value);
            setCurrentPage(1);
            setLastSelectedIndex(null);
        }}
                                    title="Select how many files/folders to show per page"
                                >
                                    ${PAGE_SIZE_OPTIONS.map((opt) => html`
                                        <option value=${opt.value}>${opt.label}</option>
                                    `)}
                                </select>
                            </label>
                            <div class="pagination-nav">
                                <button
                                    class="btn btn-sm btn-secondary"
                                    onClick=${() => handlePageChange(pagination.page - 1)}
                                    disabled=${pagination.page <= 1}
                                    title="Previous page"
                                >
                                    ← Prev
                                </button>
                                <span class="pagination-page-indicator">
                                    Page ${pagination.page} of ${pagination.totalPages}
                                </span>
                                <button
                                    class="btn btn-sm btn-secondary"
                                    onClick=${() => handlePageChange(pagination.page + 1)}
                                    disabled=${pagination.page >= pagination.totalPages}
                                    title="Next page"
                                >
                                    Next →
                                </button>
                            </div>
                        </div>
                    </div>

                    <div class="panel-content">
                        ${loading
            ? html`<div class="loading"><div class="spinner"></div>Loading...</div>`
            : html`<${FileList}
                                entries=${paginatedEntries}
                                selectedFiles=${selectedFiles}
                                canSelect=${canSelectEntry}
                                onNavigate=${handleNavigate}
                                onToggleSelect=${handleToggleSelect}
                                onShowInfo=${handleShowInfo}
                                onBrowseArchive=${handleBrowseArchive}
                                onRename=${setRenameTarget}
                                onDelete=${setDeleteTarget}
                                onVerify=${handleVerify}
                                onCompress=${handleInlineCompress}
                                conversionMode=${conversionMode}
                                verifiedCHDs=${verifiedCHDs}
                                verifyProgress=${verifyProgress}
                                chdMetadata=${chdMetadata}
                                error=${entriesError}
                                sortBy=${sortBy}
                                sortOrder=${sortOrder}
                                onSort=${handleSort}
                                onSelectAll=${handleSelectAll}
                                allSelected=${allSelectedOnPage}
                                isoHandling=${isoHandling}
                                onToggleIsoHandling=${handleIsoHandlingToggle}
                                onOrganize=${(entry) => {
                                    setIsoHandling('igir');
                                    // Preserve existing one-shot preselected input behavior through the bridge.
                                    setIgirPreselectedInput(entry.path);
                                }}
                            />`
        }
                    </div>
                </div>

                <!-- Jobs Panel -->
                <div class="panel jobs-panel">
                    <div class="panel-header">
                        <h2>Jobs ${totalJobsCount > 0 ? `(${totalJobsCount})` : ''}</h2>
                        <div class="header-actions">
                            ${stuckState?.is_stuck && html`
                                <button
                                    class="btn btn-sm btn-warning-pulse"
                                    onClick=${handleRecoverStuck}
                                    disabled=${recoveringStuck}
                                    title="Jobs are stuck waiting. Click to attempt recovery by cleaning up stale locks."
                                >
                                    ${recoveringStuck ? '⏳ Recovering...' : '🔧 Fix Stuck Jobs'}
                                </button>
                            `}
                            ${hasCompletedJobs && html`
                                <button
                                    class="btn btn-sm btn-secondary"
                                    onClick=${handleRequestClearCompleted}
                                    title="Remove completed, failed, and cancelled jobs from the list"
                                >
                                    Clear Done
                                </button>
                            `}
                            ${hasActiveJobs && html`
                                <button
                                    class="btn btn-sm btn-secondary"
                                    onClick=${handleRequestCancelAll}
                                    title="Cancel all queued and processing jobs"
                                >
                                    Cancel All
                                </button>
                            `}
                            <button
                                class="btn btn-sm btn-secondary"
                                onClick=${() => api.getJobs().then(setJobs)}
                                title="Refresh job list"
                            >
                                ↻
                            </button>
                        </div>
                    </div>
                    ${stuckState?.is_stuck && html`
                        <div class="stuck-warning">
                            <div class="stuck-warning-content">
                                <span class="stuck-warning-icon">⚠️</span>
                                <div>
                                    <strong>Jobs Stuck:</strong> ${stuckState.queued_count} ${stuckState.queued_count === 1 ? 'job' : 'jobs'} waiting but none processing.
                                    <div class="stuck-warning-details">
                                        This usually happens due to stale locks. Click "Fix Stuck Jobs" to attempt automatic recovery.
                                    </div>
                                </div>
                            </div>
                        </div>
                    `}
                    <div class="job-tabs" role="tablist" aria-label="Job list tabs">
                        <button
                            class=${`job-tab ${jobTab === 'queue' ? 'active' : ''}`}
                            role="tab"
                            aria-selected=${jobTab === 'queue' ? 'true' : 'false'}
                            onClick=${() => {
            setJobTab('queue');
            setJobCurrentPage(1);
        }}
                        >
                            Queue (${queueTabJobsCount})
                        </button>
                        <button
                            class=${`job-tab ${jobTab === 'completed' ? 'active' : ''}`}
                            role="tab"
                            aria-selected=${jobTab === 'completed' ? 'true' : 'false'}
                            onClick=${() => {
            setJobTab('completed');
            setJobCurrentPage(1);
        }}
                        >
                            Completed (${completedTabJobsCount})
                        </button>
                        ${showIssuesTab && html`
                            <button
                                class=${`job-tab ${jobTab === 'issues' ? 'active' : ''}`}
                                role="tab"
                                aria-selected=${jobTab === 'issues' ? 'true' : 'false'}
                                onClick=${() => {
                setJobTab('issues');
                setJobCurrentPage(1);
            }}
                            >
                                Failed/Cancelled (${issuesTabJobsCount})
                            </button>
                        `}
                    </div>
                    <div class="pagination-controls">
                        <div class="pagination-summary" aria-live="polite" aria-atomic="true">
                            ${jobsPagination.totalItems > 0
            ? `Showing ${jobsPagination.start}-${jobsPagination.end} of ${jobsPagination.totalItems} job${jobsPagination.totalItems === 1 ? '' : 's'}`
            : 'Showing 0 jobs'}
                        </div>
                        <div class="pagination-actions">
                            <label class="pagination-page-size">
                                <span>Jobs per page</span>
                                <select
                                    value=${jobItemsPerPage}
                                    onFocus=${beginUiSelectionInteraction}
                                    onBlur=${endUiSelectionInteraction}
                                    onMouseDown=${beginUiSelectionInteraction}
                                    onChange=${(e) => {
            endUiSelectionInteraction();
            setJobItemsPerPage(e.target.value);
            setJobCurrentPage(1);
        }}
                                    title="Select how many jobs to show per page"
                                >
                                    ${PAGE_SIZE_OPTIONS.map((opt) => html`
                                        <option value=${opt.value}>${opt.label}</option>
                                    `)}
                                </select>
                            </label>
                            <div class="pagination-nav">
                                <button
                                    class="btn btn-sm btn-secondary"
                                    onClick=${() => handleJobPageChange(jobsPagination.page - 1)}
                                    disabled=${jobsPagination.page <= 1}
                                    title="Previous jobs page"
                                >
                                    ← Prev
                                </button>
                                <span class="pagination-page-indicator">
                                    Page ${jobsPagination.page} of ${jobsPagination.totalPages}
                                </span>
                                <button
                                    class="btn btn-sm btn-secondary"
                                    onClick=${() => handleJobPageChange(jobsPagination.page + 1)}
                                    disabled=${jobsPagination.page >= jobsPagination.totalPages}
                                    title="Next jobs page"
                                >
                                    Next →
                                </button>
                            </div>
                        </div>
                    </div>
                    <div class="panel-content">
                        <${JobList}
                            jobs=${paginatedJobs}
                            onCancel=${handleCancelJob}
                            emptyTitle=${jobTab === 'completed'
            ? 'No completed jobs'
            : jobTab === 'issues'
                ? 'No failed or cancelled jobs'
                : 'No queued jobs'}
                            emptyHelpText=${jobTab === 'completed'
            ? 'Successfully completed jobs will appear here.'
            : jobTab === 'issues'
                ? 'Failed and cancelled jobs will appear here when they happen.'
                : 'Select files and click Convert to queue jobs'}
                        />
                    </div>
                </div>
            </div>
            `}

            ${showCHDInfo && html`
                <${CHDInfoModal}
                    path=${showCHDInfo.path}
                    infoMode=${showCHDInfo.infoMode}
                    onClose=${() => setShowCHDInfo(null)}
                />
            `}

            ${showCancelAllModal && html`
                <${CancelAllJobsModal}
                    total=${activeJobsCount}
                    queued=${queuedJobsCount}
                    processing=${processingJobsCount}
                    busy=${cancellingAllJobs}
                    onConfirm=${handleCancelAllJobs}
                    onClose=${() => setShowCancelAllModal(false)}
                />
            `}

            ${showClearDoneModal && html`
                <${ClearDoneModal}
                    total=${jobs.filter(j => ['completed', 'failed', 'cancelled'].includes(j.status)).length}
                    busy=${clearingCompletedJobs}
                    onConfirm=${handleClearCompleted}
                    onClose=${() => setShowClearDoneModal(false)}
                />
            `}

            ${deletePlan && html`
                <${DeletePlanModal}
                    plan=${deletePlan.plan}
                    verificationLabel=${isZ3dsMode ? null : (isDolphinMode ? 'disc image' : 'CHD')}
                    title=${isZ3dsMode ? 'Confirm delete after compress' : null}
                    onConfirm=${handleDeletePlanConfirm}
                    onClose=${handleDeletePlanClose}
                />
            `}

            ${duplicateCheck && html`
                <${DuplicateModal}
                    duplicates=${duplicateCheck.duplicates}
                    onAction=${handleDuplicateAction}
                    onClose=${() => setDuplicateCheck(null)}
                />
            `}

            ${autoQueuePrompt && html`
                <${AutoQueueCapModal}
                    total=${autoQueuePrompt.total}
                    recommendedCap=${autoQueuePrompt.recommendedCap}
                    onConfirmCap=${handleAutoQueuePromptCap}
                    onConfirmAll=${handleAutoQueuePromptAll}
                    onClose=${() => setAutoQueuePrompt(null)}
                    busy=${autoQueueing || converting}
                />
            `}

            ${renameTarget && html`
                <${RenameModal}
                    entry=${renameTarget}
                    onRename=${handleRename}
                    onClose=${() => setRenameTarget(null)}
                />
            `}

            ${deleteTarget && html`
                <${DeleteModal}
                    entry=${deleteTarget}
                    verifiedCHDs=${verifiedCHDs}
                    verifyProgress=${verifyProgress}
                    onDelete=${handleDelete}
                    onVerify=${handleVerify}
                    onClose=${() => setDeleteTarget(null)}
                    isoHandling=${isoHandling}
                />
            `}

            ${bulkDeleteEntries && html`
                <${BulkDeleteModal}
                    entries=${bulkDeleteEntries}
                    verifiedCHDs=${verifiedCHDs}
                    onDelete=${handleDelete}
                    onVerify=${handleAddVerifiedCHD}
                    onClose=${() => setBulkDeleteEntries(null)}
                    onRefresh=${handleBulkDeleteRefresh}
                    isoHandling=${isoHandling}
                />
            `}

            ${bulkVerifyItems && html`
                <${BulkVerifyModal}
                    verifyItems=${bulkVerifyItems}
                    onComplete=${handleBulkVerifyComplete}
                    onClose=${() => setBulkVerifyItems(null)}
                />
            `}

            <footer class="app-footer">
                <img src="/static/images/logo.png" alt="" class="footer-logo" />
                <span>Compressatorium${appVersion ? ` v${appVersion}` : ''}</span>
                <a href="https://github.com/pacnpal/Compressatorium" target="_blank" rel="noopener noreferrer">GitHub</a>
            </footer>
        </div>
    `;
}

export function mountApp(rootElement) {
    render(html`<${App} />`, rootElement);
}
