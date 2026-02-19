import { api } from '../../../api.js';
import { consumeIgirPreselectedInput } from '../../bridges/igirPreselectBridge.js';
import { html, useCallback, useEffect, useState } from '../../runtime/preactRuntime.js';
import {
    IGIR_ARCHIVE_COMMANDS,
    IGIR_COMMANDS,
    IGIR_COPY_MOVE_COMMANDS,
    IGIR_FILTER_PRESETS,
    IGIR_WORKFLOW_GOALS,
    IGIR_WRITE_COMMANDS,
} from '../../constants/igirConstants.js';
import { DatBrowser } from './DatBrowser.js';
import { IgirDirectoryPicker } from './IgirDirectoryPicker.js';
import { IgirJobCard } from './IgirJobCard.js';

export function IgirView({ volumes, notify }) {
    // Commands
    const [selectedCommands, setSelectedCommands] = useState(new Set());

    // Input / Output
    const [inputPaths, setInputPaths] = useState([]);
    const [outputPath, setOutputPath] = useState('');
    const [selectedDats, setSelectedDats] = useState(new Set());

    // Options
    const [showFilters, setShowFilters] = useState(false);
    const [show1G1R, setShow1G1R] = useState(false);
    const [showOrganization, setShowOrganization] = useState(false);
    const [showWriting, setShowWriting] = useState(false);
    const [showAdvanced, setShowAdvanced] = useState(false);

    // Filter flags
    const [filterFlags, setFilterFlags] = useState({});
    const [filterRegex, setFilterRegex] = useState('');
    const [filterRegexExclude, setFilterRegexExclude] = useState('');
    const [filterLanguage, setFilterLanguage] = useState('');
    const [filterRegion, setFilterRegion] = useState('');

    // 1G1R
    const [single, setSingle] = useState(false);
    const [preferLanguage, setPreferLanguage] = useState('');
    const [preferRegion, setPreferRegion] = useState('');
    const [preferRevision, setPreferRevision] = useState('');
    const [preferVerified, setPreferVerified] = useState(false);
    const [preferGood, setPreferGood] = useState(false);
    const [preferRetail, setPreferRetail] = useState(false);
    const [preferParent, setPreferParent] = useState(false);
    const [preferGameRegex, setPreferGameRegex] = useState('');
    const [preferRomRegex, setPreferRomRegex] = useState('');

    // Organization
    const [dirMirror, setDirMirror] = useState(false);
    const [dirDatName, setDirDatName] = useState(false);
    const [dirDatDescription, setDirDatDescription] = useState(false);
    const [dirLetter, setDirLetter] = useState(false);
    const [dirLetterCount, setDirLetterCount] = useState('');
    const [dirLetterLimit, setDirLetterLimit] = useState('');
    const [dirLetterGroup, setDirLetterGroup] = useState(false);
    const [dirGameSubdir, setDirGameSubdir] = useState('');
    const [fixExtension, setFixExtension] = useState('');

    // Writing
    const [overwrite, setOverwrite] = useState(false);
    const [overwriteInvalid, setOverwriteInvalid] = useState(false);
    const [symlinkRelative, setSymlinkRelative] = useState(false);

    // New fields
    const [dirDatMirror, setDirDatMirror] = useState(false);
    const [datIgnoreParentClone, setDatIgnoreParentClone] = useState(false);
    const [removeHeaders, setRemoveHeaders] = useState('');
    const [patchPaths, setPatchPaths] = useState([]);
    const [inputExclude, setInputExclude] = useState('');

    // Advanced
    const [datThreads, setDatThreads] = useState('');
    const [readerThreads, setReaderThreads] = useState('');
    const [writerThreads, setWriterThreads] = useState('');
    const [inputChecksumQuick, setInputChecksumQuick] = useState(false);
    const [inputChecksumMin, setInputChecksumMin] = useState('');
    const [verbose, setVerbose] = useState(0);
    const [cleanDryRun, setCleanDryRun] = useState(false);
    const [inputChecksumMax, setInputChecksumMax] = useState('');
    const [inputChecksumArchives, setInputChecksumArchives] = useState('');
    const [linkMode, setLinkMode] = useState('hardlink');
    const [moveDeleteDirs, setMoveDeleteDirs] = useState('');
    const [zipFormat, setZipFormat] = useState('');
    const [zipExclude, setZipExclude] = useState('');
    const [zipDatName, setZipDatName] = useState(false);
    const [filterCategoryRegex, setFilterCategoryRegex] = useState('');
    const [playlistExtensions, setPlaylistExtensions] = useState('');
    const [mergeRoms, setMergeRoms] = useState('');
    const [mergeDiscs, setMergeDiscs] = useState(false);
    const [excludeDisks, setExcludeDisks] = useState(false);
    const [allowExcessSets, setAllowExcessSets] = useState(false);
    const [allowIncompleteSets, setAllowIncompleteSets] = useState(false);
    const [cleanExclude, setCleanExclude] = useState('');
    const [cleanBackup, setCleanBackup] = useState('');
    const [reportOutput, setReportOutput] = useState('');
    const [fixdatOutput, setFixdatOutput] = useState('');
    const [dir2datOutput, setDir2datOutput] = useState('');
    const [writeRetry, setWriteRetry] = useState('');
    const [disableCache, setDisableCache] = useState(false);
    const [cachePath, setCachePath] = useState('');
    const [tempDir, setTempDir] = useState('');
    const [headerGlob, setHeaderGlob] = useState('');
    const [trimmedGlob, setTrimmedGlob] = useState('');
    const [trimScanArchives, setTrimScanArchives] = useState(false);

    // Validation & preview
    const [validation, setValidation] = useState(null);
    const [validating, setValidating] = useState(false);
    const [executing, setExecuting] = useState(false);
    const [autoSetupBusy, setAutoSetupBusy] = useState(false);
    const [autoSetupApplied, setAutoSetupApplied] = useState(false);
    const [autoSetupGoal, setAutoSetupGoal] = useState('first_sort');
    const [workflowGoals, setWorkflowGoals] = useState(IGIR_WORKFLOW_GOALS);
    const [preflight, setPreflight] = useState(null);
    const [executeConfirmText, setExecuteConfirmText] = useState('');

    // Jobs
    const [igirJobs, setIgirJobs] = useState([]);
    const [showIgirJobs, setShowIgirJobs] = useState(true);

    // Load igir jobs on mount and pick up pre-selected input
    useEffect(() => {
        api.getIgirJobs().then(setIgirJobs).catch(() => {});
        const preselectedPath = consumeIgirPreselectedInput();
        if (preselectedPath) {
            setInputPaths(prev => {
                const path = preselectedPath;
                return prev.includes(path) ? prev : [...prev, path];
            });
        }
    }, []);

    useEffect(() => {
        if (!workflowGoals.some(goal => goal.id === autoSetupGoal) && workflowGoals.length > 0) {
            setAutoSetupGoal(workflowGoals[0].id);
        }
    }, [workflowGoals, autoSetupGoal]);

    // Subscribe to igir job events
    useEffect(() => {
        const unsubscribe = api.subscribeToIgirJobs(({ type, data }) => {
            if (data?.job) {
                setIgirJobs(prev => {
                    const idx = prev.findIndex(j => j.id === data.job.id);
                    if (idx >= 0) {
                        const next = [...prev];
                        next[idx] = data.job;
                        return next;
                    }
                    return [data.job, ...prev];
                });
            }
            if (type === 'complete' || type === 'error' || type === 'cancelled') {
                // Refresh job list after terminal event
                api.getIgirJobs().then(setIgirJobs).catch(() => {});
            }
        });
        return unsubscribe;
    }, []);

    const buildRequest = useCallback(() => {
        const req = {
            commands: Array.from(selectedCommands),
            input_paths: inputPaths,
        };
        if (outputPath.trim()) req.output_path = outputPath.trim();
        if (selectedDats.size > 0) req.dat_paths = Array.from(selectedDats);

        // Filters
        const filterBooleans = [
            'no_bios', 'only_bios', 'no_device', 'only_device', 'no_unlicensed', 'only_unlicensed',
            'only_retail', 'no_debug', 'only_debug', 'no_demo', 'only_demo', 'no_beta', 'only_beta',
            'no_sample', 'only_sample', 'no_prototype', 'only_prototype', 'no_program', 'only_program',
            'no_aftermarket', 'only_aftermarket', 'no_homebrew', 'only_homebrew', 'no_unverified', 'only_unverified',
            'no_bad', 'only_bad'
        ];
        for (const flag of filterBooleans) {
            if (filterFlags[flag]) req[flag] = true;
        }
        if (filterRegex.trim()) req.filter_regex = filterRegex.trim();
        if (filterRegexExclude.trim()) req.filter_regex_exclude = filterRegexExclude.trim();
        if (filterCategoryRegex.trim()) req.filter_category_regex = filterCategoryRegex.trim();
        if (filterLanguage.trim()) req.filter_language = filterLanguage.split(',').map(s => s.trim()).filter(Boolean);
        if (filterRegion.trim()) req.filter_region = filterRegion.split(',').map(s => s.trim()).filter(Boolean);

        // 1G1R
        if (single) {
            req.single = true;
            if (preferLanguage.trim()) req.prefer_language = preferLanguage.split(',').map(s => s.trim()).filter(Boolean);
            if (preferRegion.trim()) req.prefer_region = preferRegion.split(',').map(s => s.trim()).filter(Boolean);
            if (preferRevision) req.prefer_revision = preferRevision;
            if (preferVerified) req.prefer_verified = true;
            if (preferGood) req.prefer_good = true;
            if (preferRetail) req.prefer_retail = true;
            if (preferParent) req.prefer_parent = true;
            if (preferGameRegex.trim()) req.prefer_game_regex = preferGameRegex.trim();
            if (preferRomRegex.trim()) req.prefer_rom_regex = preferRomRegex.trim();
        }

        // Organization
        if (dirMirror) req.dir_mirror = true;
        if (dirDatName) req.dir_dat_name = true;
        if (dirDatDescription) req.dir_dat_description = true;
        if (dirLetter) req.dir_letter = true;
        if (dirLetterCount) req.dir_letter_count = parseInt(dirLetterCount, 10);
        if (dirLetterLimit) req.dir_letter_limit = parseInt(dirLetterLimit, 10);
        if (dirLetterGroup) req.dir_letter_group = true;
        if (dirGameSubdir) req.dir_game_subdir = dirGameSubdir;
        if (fixExtension) req.fix_extension = fixExtension;

        // Writing
        if (overwrite) req.overwrite = true;
        if (overwriteInvalid) req.overwrite_invalid = true;
        if (selectedCommands.has('link')) {
            req.link_mode = linkMode || 'hardlink';
            if (req.link_mode === 'symlink' && symlinkRelative) req.symlink_relative = true;
        }
        if (selectedCommands.has('move') && moveDeleteDirs) req.move_delete_dirs = moveDeleteDirs;
        if (selectedCommands.has('zip') && zipFormat) req.zip_format = zipFormat;
        if (selectedCommands.has('zip') && zipExclude.trim()) req.zip_exclude = zipExclude.trim();
        if (selectedCommands.has('zip') && zipDatName) req.zip_dat_name = true;

        // New fields
        if (dirDatMirror) req.dir_dat_mirror = true;
        if (datIgnoreParentClone) req.dat_ignore_parent_clone = true;
        if (removeHeaders) req.remove_headers = removeHeaders;
        if (headerGlob.trim()) req.header = headerGlob.trim();
        if (trimmedGlob.trim()) req.trimmed_glob = trimmedGlob.trim();
        if (trimScanArchives) req.trim_scan_archives = true;
        if (patchPaths.length > 0) req.patch = patchPaths;
        if (inputExclude.trim()) req.input_exclude = inputExclude.split(',').map(s => s.trim()).filter(Boolean);
        if (cleanExclude.trim()) req.clean_exclude = cleanExclude.split(',').map(s => s.trim()).filter(Boolean);
        if (cleanBackup.trim()) req.clean_backup = cleanBackup.trim();
        if (reportOutput.trim()) req.report_output = reportOutput.trim();
        if (fixdatOutput.trim()) req.fixdat_output = fixdatOutput.trim();
        if (dir2datOutput.trim()) req.dir2dat_output = dir2datOutput.trim();
        if (playlistExtensions.trim()) req.playlist_extensions = playlistExtensions.trim();
        if (mergeRoms) req.merge_roms = mergeRoms;
        if (mergeDiscs) req.merge_discs = true;
        if (excludeDisks) req.exclude_disks = true;
        if (allowExcessSets) req.allow_excess_sets = true;
        if (allowIncompleteSets) req.allow_incomplete_sets = true;

        // Advanced
        if (datThreads) req.dat_threads = parseInt(datThreads, 10);
        if (readerThreads) req.reader_threads = parseInt(readerThreads, 10);
        if (writerThreads) req.writer_threads = parseInt(writerThreads, 10);
        if (writeRetry !== '') req.write_retry = parseInt(writeRetry, 10);
        if (inputChecksumQuick) req.input_checksum_quick = true;
        if (inputChecksumMin) req.input_checksum_min = inputChecksumMin;
        if (inputChecksumMax) req.input_checksum_max = inputChecksumMax;
        if (inputChecksumArchives) req.input_checksum_archives = inputChecksumArchives;
        if (disableCache) req.disable_cache = true;
        if (cachePath.trim()) req.cache_path = cachePath.trim();
        if (tempDir.trim()) req.temp_dir = tempDir.trim();
        if (verbose > 0) req.verbose = verbose;
        if (cleanDryRun) req.clean_dry_run = true;

        return req;
    }, [selectedCommands, inputPaths, outputPath, selectedDats, filterFlags,
        filterRegex, filterRegexExclude, filterCategoryRegex, filterLanguage, filterRegion,
        single, preferLanguage, preferRegion, preferRevision, preferVerified,
        preferGood, preferRetail, preferParent, preferGameRegex, preferRomRegex,
        dirMirror, dirDatName, dirDatDescription, dirLetter, dirLetterCount,
        dirLetterLimit, dirLetterGroup, dirGameSubdir, fixExtension,
        overwrite, overwriteInvalid, linkMode, symlinkRelative, moveDeleteDirs,
        zipFormat, zipExclude, zipDatName,
        dirDatMirror, datIgnoreParentClone, removeHeaders, patchPaths, inputExclude,
        headerGlob, trimmedGlob, trimScanArchives,
        cleanExclude, cleanBackup, reportOutput, fixdatOutput, dir2datOutput,
        playlistExtensions, mergeRoms, mergeDiscs, excludeDisks, allowExcessSets,
        allowIncompleteSets, datThreads, readerThreads, writerThreads, writeRetry,
        inputChecksumQuick, inputChecksumMin, inputChecksumMax, inputChecksumArchives,
        disableCache, cachePath, tempDir, verbose, cleanDryRun]);

    const handleValidate = useCallback(() => {
        const req = buildRequest();
        setValidating(true);
        api.validateIgirRequest(req).then(result => {
            setValidation(result);
            setValidating(false);
        }).catch(err => {
            setValidation({ valid: false, errors: [err.message], warnings: [], command_preview: '' });
            setValidating(false);
        });
    }, [buildRequest]);

    const handleRunDryRun = useCallback(() => {
        const req = buildRequest();
        setValidating(true);
        api.igirDryRunExecute(req).then(result => {
            setValidation({
                valid: true,
                errors: [],
                warnings: result.warnings || [],
                command_preview: result.command_preview || '',
            });
            if (Array.isArray(result.clean_dry_run_results) && result.clean_dry_run_results.length > 0) {
                notify(`Clean dry-run found ${result.clean_dry_run_results.length} candidate file(s)`, 'info');
            } else {
                notify('Clean dry-run completed with no candidate files', 'info');
            }
            setValidating(false);
        }).catch(err => {
            setValidation({ valid: false, errors: [err.message], warnings: [], command_preview: '' });
            setValidating(false);
        });
    }, [buildRequest, notify]);

    const handleAutoSetup = useCallback(() => {
        if (inputPaths.length === 0) {
            notify('Select at least one input directory first', 'info');
            return;
        }

        setAutoSetupBusy(true);
        api.getIgirQuickSetup(inputPaths, autoSetupGoal).then((recommendation) => {
            const recommendedCommands = Array.isArray(recommendation.commands)
                ? recommendation.commands.filter(Boolean)
                : [];
            const recommendedDats = Array.isArray(recommendation.dat_paths)
                ? recommendation.dat_paths.filter(Boolean)
                : [];
            const recommendedLanguages = Array.isArray(recommendation.prefer_language)
                ? recommendation.prefer_language
                : [];
            const recommendedRegions = Array.isArray(recommendation.prefer_region)
                ? recommendation.prefer_region
                : [];
            const workflowOptions = Array.isArray(recommendation.workflows)
                ? recommendation.workflows
                    .map((workflow) => {
                        const id = typeof workflow.id === 'string' ? workflow.id : '';
                        if (!id) return null;
                        return {
                            id,
                            label: typeof workflow.label === 'string' && workflow.label.trim()
                                ? workflow.label
                                : id,
                        };
                    })
                    .filter(Boolean)
                : [];

            if (recommendedCommands.length > 0) {
                setSelectedCommands(new Set(recommendedCommands));
            }
            setOutputPath(recommendation.output_path || '');
            setSelectedDats(new Set(recommendedDats));
            setCleanDryRun(Boolean(recommendation.clean_dry_run));
            setDirDatName(Boolean(recommendation.dir_dat_name));
            setDirDatDescription(Boolean(recommendation.dir_dat_description));
            setDirDatMirror(Boolean(recommendation.dir_dat_mirror));
            setDirLetter(Boolean(recommendation.dir_letter));
            setMergeRoms(typeof recommendation.merge_roms === 'string' ? recommendation.merge_roms : '');
            setMergeDiscs(Boolean(recommendation.merge_discs));
            setExcludeDisks(Boolean(recommendation.exclude_disks));
            setAllowExcessSets(Boolean(recommendation.allow_excess_sets));
            setAllowIncompleteSets(Boolean(recommendation.allow_incomplete_sets));
            setLinkMode(recommendation.link_mode || 'hardlink');
            setMoveDeleteDirs(recommendation.move_delete_dirs || '');
            setZipFormat(recommendation.zip_format || '');
            setZipExclude(recommendation.zip_exclude || '');
            setZipDatName(Boolean(recommendation.zip_dat_name));
            setDatIgnoreParentClone(Boolean(recommendation.dat_ignore_parent_clone));
            setPlaylistExtensions(recommendation.playlist_extensions || '');
            if (workflowOptions.length > 0) {
                setWorkflowGoals(workflowOptions);
            }
            if (recommendation.workflow_id) {
                setAutoSetupGoal(recommendation.workflow_id);
            }

            if (recommendation.filter_preset === 'retail') {
                setFilterFlags({ ...IGIR_FILTER_PRESETS.retail.flags });
            } else {
                const inferredFlags = {};
                const knownFlags = [
                    'no_bios', 'only_bios', 'no_device', 'only_device', 'no_unlicensed', 'only_unlicensed',
                    'only_retail', 'no_debug', 'only_debug', 'no_demo', 'only_demo', 'no_beta', 'only_beta',
                    'no_sample', 'only_sample', 'no_prototype', 'only_prototype', 'no_program', 'only_program',
                    'no_aftermarket', 'only_aftermarket', 'no_homebrew', 'only_homebrew', 'no_unverified',
                    'only_unverified', 'no_bad', 'only_bad'
                ];
                for (const key of knownFlags) {
                    if (recommendation[key]) inferredFlags[key] = true;
                }
                setFilterFlags(inferredFlags);
            }
            if (recommendation.single) {
                setSingle(true);
            } else {
                setSingle(false);
            }
            setPreferLanguage(recommendedLanguages.join(','));
            setPreferRegion(recommendedRegions.join(','));
            setPreferVerified(Boolean(recommendation.prefer_verified));
            setPreferGood(Boolean(recommendation.prefer_good));
            setPreferRetail(Boolean(recommendation.prefer_retail));
            setPreferParent(Boolean(recommendation.prefer_parent));

            setAutoSetupApplied(true);
            setValidation(null);

            const datCount = recommendedDats.length;
            const requiresDats = recommendation.requires_dats !== false;
            notify(
                datCount > 0
                    ? `Auto-setup applied: ${datCount} DAT ${datCount === 1 ? 'match' : 'matches'} selected`
                    : (requiresDats
                        ? 'Auto-setup applied: no DAT matches found, review DAT selection'
                        : 'Auto-setup applied: this workflow does not require DAT files'),
                datCount > 0 || !requiresDats ? 'success' : 'warning',
            );
            if (recommendation.warning) {
                notify(recommendation.warning, 'warning');
            }
            api.trackIgirFeatureEvent('igir_autoconfig_applied').catch(() => {});
        }).catch((err) => {
            notify(`Auto-setup failed: ${err.message}`, 'error');
        }).finally(() => {
            setAutoSetupBusy(false);
        });
    }, [inputPaths, notify, autoSetupGoal]);

    const handleExecute = useCallback(() => {
        const req = buildRequest();
        setExecuting(true);
        api.igirPreflight(req).then((result) => {
            setPreflight(result);
            setValidation({
                valid: result.valid,
                errors: result.errors || [],
                warnings: result.warnings || [],
                command_preview: result.command_preview || '',
            });

            if (!result.valid) {
                throw new Error('Preflight failed: fix validation errors before executing');
            }

            if (result.requires_confirmation && executeConfirmText.trim().toUpperCase() !== 'RUN') {
                throw new Error('Type RUN in confirmation box to execute destructive operations');
            }

            return api.createIgirJob(req);
        }).then(job => {
            notify('igir job created', 'success');
            setIgirJobs(prev => [job, ...prev]);
            setValidation(null);
            setPreflight(null);
            setExecuteConfirmText('');
            if (autoSetupApplied) {
                api.trackIgirFeatureEvent('igir_autoconfig_executed').catch(() => {});
                setAutoSetupApplied(false);
            }
        }).catch(err => {
            notify(err.message, 'error');
        }).finally(() => {
            setExecuting(false);
        });
    }, [buildRequest, notify, autoSetupApplied, executeConfirmText]);

    const handleCancelJob = useCallback((jobId) => {
        api.cancelIgirJob(jobId).then(() => {
            api.getIgirJobs().then(setIgirJobs).catch(() => {});
        }).catch(err => notify(err.message, 'error'));
    }, [notify]);

    const handleClearCompleted = useCallback(() => {
        api.deleteCompletedIgirJobs().then(() => {
            api.getIgirJobs().then(setIgirJobs).catch(() => {});
        }).catch(err => notify(err.message, 'error'));
    }, [notify]);

    const handleCancelAll = useCallback(() => {
        api.cancelAllIgirJobs().then(() => {
            api.getIgirJobs().then(setIgirJobs).catch(() => {});
            notify('All igir jobs cancelled', 'success');
        }).catch(err => notify(err.message, 'error'));
    }, [notify]);

    const toggleCommand = useCallback((cmd) => {
        setSelectedCommands(prev => {
            const next = new Set(prev);
            if (next.has(cmd)) {
                next.delete(cmd);
            } else {
                // Enforce only one write command
                if (IGIR_WRITE_COMMANDS.has(cmd)) {
                    for (const wc of IGIR_WRITE_COMMANDS) {
                        if (wc !== cmd) next.delete(wc);
                    }
                }
                next.add(cmd);
            }
            const hasCopyMove = Array.from(next).some(c => IGIR_COPY_MOVE_COMMANDS.has(c));
            if (!hasCopyMove) {
                for (const archiveCmd of IGIR_ARCHIVE_COMMANDS) {
                    next.delete(archiveCmd);
                }
            }
            return next;
        });
    }, []);

    const applyFilterPreset = useCallback((preset) => {
        setFilterFlags({ ...(preset.flags || {}) });

        // Always normalize 1G1R state so non-1G1R presets clear prior values.
        const g = preset.oneG1R || null;
        setSingle(Boolean(g?.single));
        setPreferLanguage(g?.preferLanguage || '');
        setPreferRegion(g?.preferRegion || '');
        setPreferRevision(g?.preferRevision || '');
        setPreferVerified(Boolean(g?.preferVerified));
        setPreferGood(Boolean(g?.preferGood));
        setPreferRetail(Boolean(g?.preferRetail));
        setPreferParent(Boolean(g?.preferParent));
        setPreferGameRegex(g?.preferGameRegex || '');
        setPreferRomRegex(g?.preferRomRegex || '');
    }, []);

    const toggleFilterFlag = useCallback((flag) => {
        setFilterFlags(prev => ({ ...prev, [flag]: !prev[flag] }));
    }, []);

    const handleToggleDat = useCallback((path) => {
        setSelectedDats(prev => {
            const next = new Set(prev);
            if (next.has(path)) next.delete(path);
            else next.add(path);
            return next;
        });
    }, []);

    const handleSelectAllDats = useCallback(() => {
        api.searchDats().then(results => {
            setSelectedDats(new Set(results.map(d => d.path)));
        });
    }, []);

    const handleDeselectAllDats = useCallback(() => {
        setSelectedDats(new Set());
    }, []);

    const hasWriteCommand = Array.from(selectedCommands).some(c => IGIR_WRITE_COMMANDS.has(c));
    const hasCopyMoveCommand = Array.from(selectedCommands).some(c => IGIR_COPY_MOVE_COMMANDS.has(c));
    const hasDestructiveConfig = selectedCommands.has('move')
        || (selectedCommands.has('clean') && !cleanDryRun)
        || overwrite
        || overwriteInvalid;
    const executeNeedsConfirmation = (preflight && preflight.requires_confirmation) || hasDestructiveConfig;

    const filterFlagDefs = [
        ['BIOS', 'no_bios', 'only_bios'],
        ['Device', 'no_device', 'only_device'],
        ['Licensed', 'no_unlicensed', 'only_unlicensed'],
        ['Debug', 'no_debug', 'only_debug'],
        ['Demo', 'no_demo', 'only_demo'],
        ['Beta', 'no_beta', 'only_beta'],
        ['Sample', 'no_sample', 'only_sample'],
        ['Prototype', 'no_prototype', 'only_prototype'],
        ['Program', 'no_program', 'only_program'],
        ['Aftermarket', 'no_aftermarket', 'only_aftermarket'],
        ['Homebrew', 'no_homebrew', 'only_homebrew'],
        ['Verified', 'no_unverified', 'only_unverified'],
        ['Bad', 'no_bad', 'only_bad'],
    ];

    const activeJobCount = igirJobs.filter(j => j.status === 'queued' || j.status === 'processing').length;
    const completedJobCount = igirJobs.filter(j => j.status === 'completed' || j.status === 'failed' || j.status === 'cancelled').length;

    return html`
        <div class="igir-view">
            <div class="igir-main">
                <!-- Commands -->
                <div class="igir-section">
                    <h3 class="igir-section-title">Commands</h3>
                    <div class="igir-commands">
                        ${IGIR_COMMANDS.map(cmd => {
                            const isSelected = selectedCommands.has(cmd.value);
                            const isDisabled = IGIR_ARCHIVE_COMMANDS.has(cmd.value) && !hasCopyMoveCommand;
                            return html`
                                <label
                                    class=${`igir-cmd ${cmd.group} ${isSelected ? 'selected' : ''} ${isDisabled ? 'disabled' : ''}`}
                                    key=${cmd.value}
                                    title=${cmd.description + (isDisabled ? ' (requires copy or move)' : '')}
                                >
                                    <input
                                        type="checkbox"
                                        checked=${isSelected}
                                        disabled=${isDisabled}
                                        onChange=${() => toggleCommand(cmd.value)}
                                    />
                                    <span>${cmd.label}</span>
                                </label>
                            `;
                        })}
                    </div>
                </div>

                <!-- Input / Output -->
                <div class="igir-section">
                    <h3 class="igir-section-title">Input / Output</h3>
                    <${IgirDirectoryPicker}
                        volumes=${volumes}
                        label="Input Directories"
                        selectedPaths=${inputPaths}
                        onAddPath=${(p) => setInputPaths(prev => prev.includes(p) ? prev : [...prev, p])}
                        onRemovePath=${(p) => setInputPaths(prev => prev.filter(x => x !== p))}
                        multiple=${true}
                    />
                    ${hasWriteCommand && html`
                        <div class="igir-output-dir">
                            <label class="igir-field-label">Output Directory</label>
                            <${IgirDirectoryPicker}
                                volumes=${volumes}
                                label="Output Directory"
                                selectedPaths=${outputPath ? [outputPath] : []}
                                onAddPath=${(p) => setOutputPath(p)}
                                onRemovePath=${() => setOutputPath('')}
                                multiple=${false}
                            />
                        </div>
                    `}
                    <div class="igir-field" style="margin-top: 8px">
                        <label>Input Exclude Patterns (comma-separated globs)</label>
                        <input type="text" value=${inputExclude} onInput=${(e) => setInputExclude(e.target.value)} placeholder="e.g. **/*.nfo, **/*.txt" />
                    </div>
                </div>

                <!-- DAT Files -->
                <div class="igir-section">
                    <h3 class="igir-section-title">DAT Files ${selectedDats.size > 0 ? `(${selectedDats.size})` : ''}</h3>
                    <${DatBrowser}
                        selectedDats=${selectedDats}
                        onToggleDat=${handleToggleDat}
                        onSelectAll=${handleSelectAllDats}
                        onDeselectAll=${handleDeselectAllDats}
                    />
                </div>

                <!-- Options Accordion -->
                <div class="igir-section">
                    <h3 class="igir-section-title igir-accordion-trigger" onClick=${() => setShowFilters(!showFilters)}>
                        ${showFilters ? '▼' : '▶'} Filtering
                    </h3>
                    ${showFilters && html`
                        <div class="igir-accordion-content">
                            <div class="igir-filter-presets">
                                ${Object.entries(IGIR_FILTER_PRESETS).map(([key, preset]) => html`
                                    <button
                                        key=${key}
                                        class="btn btn-sm btn-secondary"
                                        onClick=${() => applyFilterPreset(preset)}
                                        title=${preset.description}
                                    >
                                        ${preset.label}
                                    </button>
                                `)}
                            </div>
                            <div class="igir-filter-grid">
                                ${filterFlagDefs.map(([label, noFlag, onlyFlag]) => html`
                                    <div class="igir-filter-pair" key=${label}>
                                        <span class="igir-filter-label">${label}</span>
                                        <label class="igir-filter-toggle" title="Exclude ${label.toLowerCase()}">
                                            <input type="checkbox" checked=${!!filterFlags[noFlag]} onChange=${() => toggleFilterFlag(noFlag)} />
                                            <span>No</span>
                                        </label>
                                        <label class="igir-filter-toggle" title="Only ${label.toLowerCase()}">
                                            <input type="checkbox" checked=${!!filterFlags[onlyFlag]} onChange=${() => toggleFilterFlag(onlyFlag)} />
                                            <span>Only</span>
                                        </label>
                                    </div>
                                `)}
                            </div>
                            <label class="igir-filter-toggle" title="Only retail releases">
                                <input type="checkbox" checked=${!!filterFlags.only_retail} onChange=${() => toggleFilterFlag('only_retail')} />
                                <span>Only Retail</span>
                            </label>
                            <div class="igir-field">
                                <label>Filter Regex</label>
                                <input type="text" value=${filterRegex} onInput=${(e) => setFilterRegex(e.target.value)} placeholder="e.g. Mario" />
                            </div>
                            <div class="igir-field">
                                <label>Exclude Regex</label>
                                <input type="text" value=${filterRegexExclude} onInput=${(e) => setFilterRegexExclude(e.target.value)} placeholder="e.g. Demo|Beta" />
                            </div>
                            <div class="igir-field">
                                <label>Languages (comma-separated)</label>
                                <input type="text" value=${filterLanguage} onInput=${(e) => setFilterLanguage(e.target.value)} placeholder="e.g. EN,JA,FR" />
                            </div>
                            <div class="igir-field">
                                <label>Regions (comma-separated)</label>
                                <input type="text" value=${filterRegion} onInput=${(e) => setFilterRegion(e.target.value)} placeholder="e.g. USA,EUR,JPN" />
                            </div>
                        </div>
                    `}
                </div>

                <div class="igir-section">
                    <h3 class="igir-section-title igir-accordion-trigger" onClick=${() => setShow1G1R(!show1G1R)}>
                        ${show1G1R ? '▼' : '▶'} 1G1R (One Game One ROM)
                    </h3>
                    ${show1G1R && html`
                        <div class="igir-accordion-content">
                            <label class="igir-toggle-option">
                                <input type="checkbox" checked=${single} onChange=${(e) => setSingle(e.target.checked)} />
                                <span>Enable 1G1R (--single)</span>
                            </label>
                            ${single && html`
                                <div class="igir-1g1r-prefs">
                                    <div class="igir-field">
                                        <label>Preferred Languages (ordered, comma-separated)</label>
                                        <input type="text" value=${preferLanguage} onInput=${(e) => setPreferLanguage(e.target.value)} placeholder="e.g. EN,JA" />
                                    </div>
                                    <div class="igir-field">
                                        <label>Preferred Regions (ordered, comma-separated)</label>
                                        <input type="text" value=${preferRegion} onInput=${(e) => setPreferRegion(e.target.value)} placeholder="e.g. USA,EUR,JPN" />
                                    </div>
                                    <div class="igir-field">
                                        <label>Revision Preference</label>
                                        <select value=${preferRevision} onChange=${(e) => setPreferRevision(e.target.value)}>
                                            <option value="">None</option>
                                            <option value="newer">Newer</option>
                                            <option value="older">Older</option>
                                        </select>
                                    </div>
                                    <div class="igir-toggle-row">
                                        <label class="igir-toggle-option">
                                            <input type="checkbox" checked=${preferVerified} onChange=${(e) => setPreferVerified(e.target.checked)} />
                                            <span>Prefer Verified</span>
                                        </label>
                                        <label class="igir-toggle-option">
                                            <input type="checkbox" checked=${preferGood} onChange=${(e) => setPreferGood(e.target.checked)} />
                                            <span>Prefer Good</span>
                                        </label>
                                        <label class="igir-toggle-option">
                                            <input type="checkbox" checked=${preferRetail} onChange=${(e) => setPreferRetail(e.target.checked)} />
                                            <span>Prefer Retail</span>
                                        </label>
                                        <label class="igir-toggle-option">
                                            <input type="checkbox" checked=${preferParent} onChange=${(e) => setPreferParent(e.target.checked)} />
                                            <span>Prefer Parent</span>
                                        </label>
                                    </div>
                                    <div class="igir-field">
                                        <label>Prefer Game Regex</label>
                                        <input type="text" value=${preferGameRegex} onInput=${(e) => setPreferGameRegex(e.target.value)} placeholder="Custom game name preference pattern" />
                                    </div>
                                    <div class="igir-field">
                                        <label>Prefer ROM Regex</label>
                                        <input type="text" value=${preferRomRegex} onInput=${(e) => setPreferRomRegex(e.target.value)} placeholder="Custom ROM name preference pattern" />
                                    </div>
                                </div>
                            `}
                        </div>
                    `}
                </div>

                <div class="igir-section">
                    <h3 class="igir-section-title igir-accordion-trigger" onClick=${() => setShowOrganization(!showOrganization)}>
                        ${showOrganization ? '▼' : '▶'} Output Organization
                    </h3>
                    ${showOrganization && html`
                        <div class="igir-accordion-content">
                            <div class="igir-toggle-row">
                                <label class="igir-toggle-option">
                                    <input type="checkbox" checked=${dirMirror} onChange=${(e) => setDirMirror(e.target.checked)} />
                                    <span>Mirror Input (--dir-mirror)</span>
                                </label>
                                <label class="igir-toggle-option">
                                    <input type="checkbox" checked=${dirDatName} onChange=${(e) => setDirDatName(e.target.checked)} />
                                    <span>By DAT Name (--dir-dat-name)</span>
                                </label>
                                <label class="igir-toggle-option">
                                    <input type="checkbox" checked=${dirDatDescription} onChange=${(e) => setDirDatDescription(e.target.checked)} />
                                    <span>By DAT Description</span>
                                </label>
                                <label class="igir-toggle-option">
                                    <input type="checkbox" checked=${dirDatMirror} onChange=${(e) => setDirDatMirror(e.target.checked)} />
                                    <span>By DAT Mirror (--dir-dat-mirror)</span>
                                </label>
                                <label class="igir-toggle-option">
                                    <input type="checkbox" checked=${dirLetter} onChange=${(e) => setDirLetter(e.target.checked)} />
                                    <span>By Letter (--dir-letter)</span>
                                </label>
                            </div>
                            ${dirLetter && html`
                                <div class="igir-letter-options">
                                    <div class="igir-field">
                                        <label>Letter Count</label>
                                        <input type="number" min="1" value=${dirLetterCount} onInput=${(e) => setDirLetterCount(e.target.value)} />
                                    </div>
                                    <div class="igir-field">
                                        <label>Letter Limit</label>
                                        <input type="number" min="1" value=${dirLetterLimit} onInput=${(e) => setDirLetterLimit(e.target.value)} />
                                    </div>
                                    <label class="igir-toggle-option">
                                        <input type="checkbox" checked=${dirLetterGroup} onChange=${(e) => setDirLetterGroup(e.target.checked)} />
                                        <span>Group Letters</span>
                                    </label>
                                </div>
                            `}
                            <div class="igir-field">
                                <label>Game Subdirectory</label>
                                <select value=${dirGameSubdir} onChange=${(e) => setDirGameSubdir(e.target.value)}>
                                    <option value="">Default</option>
                                    <option value="never">Never</option>
                                    <option value="multiple">Multiple</option>
                                    <option value="always">Always</option>
                                </select>
                            </div>
                            <div class="igir-field">
                                <label>Fix Extension</label>
                                <select value=${fixExtension} onChange=${(e) => setFixExtension(e.target.value)}>
                                    <option value="">Default</option>
                                    <option value="auto">Auto</option>
                                    <option value="always">Always</option>
                                    <option value="never">Never</option>
                                </select>
                            </div>
                        </div>
                    `}
                </div>

                <div class="igir-section">
                    <h3 class="igir-section-title igir-accordion-trigger" onClick=${() => setShowWriting(!showWriting)}>
                        ${showWriting ? '▼' : '▶'} Writing Options
                    </h3>
                    ${showWriting && html`
                        <div class="igir-accordion-content">
                            <div class="igir-toggle-row">
                                <label class="igir-toggle-option">
                                    <input type="checkbox" checked=${overwrite} onChange=${(e) => setOverwrite(e.target.checked)} />
                                    <span>Overwrite existing</span>
                                </label>
                                <label class="igir-toggle-option">
                                    <input type="checkbox" checked=${overwriteInvalid} onChange=${(e) => setOverwriteInvalid(e.target.checked)} />
                                    <span>Overwrite invalid</span>
                                </label>
                            </div>
                            ${selectedCommands.has('link') && html`
                                <div class="igir-field">
                                    <label>Link Mode</label>
                                    <select value=${linkMode} onChange=${(e) => setLinkMode(e.target.value)}>
                                        <option value="hardlink">Hardlink</option>
                                        <option value="symlink">Symlink</option>
                                        <option value="reflink">Reflink</option>
                                    </select>
                                </div>
                                <div class="igir-toggle-row">
                                    <label class="igir-toggle-option">
                                        <input
                                            type="checkbox"
                                            checked=${symlinkRelative}
                                            disabled=${linkMode !== 'symlink'}
                                            onChange=${(e) => setSymlinkRelative(e.target.checked)}
                                        />
                                        <span>Relative symlinks</span>
                                    </label>
                                </div>
                            `}
                            ${selectedCommands.has('move') && html`
                                <div class="igir-field">
                                    <label>Move Delete Dirs</label>
                                    <select value=${moveDeleteDirs} onChange=${(e) => setMoveDeleteDirs(e.target.value)}>
                                        <option value="">Default</option>
                                        <option value="auto">Auto</option>
                                        <option value="always">Always</option>
                                        <option value="never">Never</option>
                                    </select>
                                </div>
                            `}
                            ${selectedCommands.has('zip') && html`
                                <div class="igir-field">
                                    <label>Zip Format</label>
                                    <select value=${zipFormat} onChange=${(e) => setZipFormat(e.target.value)}>
                                        <option value="">Default</option>
                                        <option value="torrentzip">torrentzip</option>
                                        <option value="rvzstd">rvzstd</option>
                                    </select>
                                </div>
                                <div class="igir-field">
                                    <label>Zip Exclude Glob</label>
                                    <input type="text" value=${zipExclude} onInput=${(e) => setZipExclude(e.target.value)} placeholder="e.g. *.cue" />
                                </div>
                                <label class="igir-toggle-option">
                                    <input type="checkbox" checked=${zipDatName} onChange=${(e) => setZipDatName(e.target.checked)} />
                                    <span>Zip by DAT Name</span>
                                </label>
                            `}
                        </div>
                    `}
                </div>

                <div class="igir-section">
                    <h3 class="igir-section-title igir-accordion-trigger" onClick=${() => setShowAdvanced(!showAdvanced)}>
                        ${showAdvanced ? '▼' : '▶'} Advanced
                    </h3>
                    ${showAdvanced && html`
                        <div class="igir-accordion-content">
                            <div class="igir-thread-options">
                                <div class="igir-field">
                                    <label>DAT Threads</label>
                                    <input type="number" min="1" value=${datThreads} onInput=${(e) => setDatThreads(e.target.value)} />
                                </div>
                                <div class="igir-field">
                                    <label>Reader Threads</label>
                                    <input type="number" min="1" value=${readerThreads} onInput=${(e) => setReaderThreads(e.target.value)} />
                                </div>
                                <div class="igir-field">
                                    <label>Writer Threads</label>
                                    <input type="number" min="1" value=${writerThreads} onInput=${(e) => setWriterThreads(e.target.value)} />
                                </div>
                                <div class="igir-field">
                                    <label>Write Retry</label>
                                    <input type="number" min="0" value=${writeRetry} onInput=${(e) => setWriteRetry(e.target.value)} />
                                </div>
                            </div>
                            <div class="igir-toggle-row">
                                <label class="igir-toggle-option">
                                    <input type="checkbox" checked=${inputChecksumQuick} onChange=${(e) => setInputChecksumQuick(e.target.checked)} />
                                    <span>Quick Checksum</span>
                                </label>
                                <label class="igir-toggle-option">
                                    <input type="checkbox" checked=${cleanDryRun} onChange=${(e) => setCleanDryRun(e.target.checked)} />
                                    <span>Clean Dry Run</span>
                                </label>
                                <label class="igir-toggle-option">
                                    <input type="checkbox" checked=${datIgnoreParentClone} onChange=${(e) => setDatIgnoreParentClone(e.target.checked)} />
                                    <span>Ignore Parent/Clone (--dat-ignore-parent-clone)</span>
                                </label>
                                <label class="igir-toggle-option">
                                    <input type="checkbox" checked=${disableCache} onChange=${(e) => setDisableCache(e.target.checked)} />
                                    <span>Disable Cache</span>
                                </label>
                            </div>
                            <div class="igir-field">
                                <label>Header Detection Glob</label>
                                <input type="text" value=${headerGlob} onInput=${(e) => setHeaderGlob(e.target.value)} placeholder="e.g. *.nes" />
                            </div>
                            <div class="igir-field">
                                <label>Remove Headers</label>
                                <select value=${removeHeaders} onChange=${(e) => setRemoveHeaders(e.target.value)}>
                                    <option value="">None</option>
                                    <option value="all">All</option>
                                    <option value="known">Known</option>
                                </select>
                            </div>
                            <div class="igir-field">
                                <label>Trim Detection Glob</label>
                                <input type="text" value=${trimmedGlob} onInput=${(e) => setTrimmedGlob(e.target.value)} placeholder="e.g. *.gba" />
                            </div>
                            <label class="igir-toggle-option">
                                <input type="checkbox" checked=${trimScanArchives} onChange=${(e) => setTrimScanArchives(e.target.checked)} />
                                <span>Trim Scan Archives</span>
                            </label>
                            <${IgirDirectoryPicker}
                                volumes=${volumes}
                                label="Patch Files / Directories"
                                selectedPaths=${patchPaths}
                                onAddPath=${(p) => setPatchPaths(prev => prev.includes(p) ? prev : [...prev, p])}
                                onRemovePath=${(p) => setPatchPaths(prev => prev.filter(x => x !== p))}
                                multiple=${true}
                            />
                            <div class="igir-field">
                                <label>Filter Category Regex</label>
                                <input type="text" value=${filterCategoryRegex} onInput=${(e) => setFilterCategoryRegex(e.target.value)} placeholder="e.g. /Games|Demos/i" />
                            </div>
                            <div class="igir-field">
                                <label>Minimum Checksum</label>
                                <select value=${inputChecksumMin} onChange=${(e) => setInputChecksumMin(e.target.value)}>
                                    <option value="">Default</option>
                                    <option value="CRC32">CRC32</option>
                                    <option value="MD5">MD5</option>
                                    <option value="SHA1">SHA1</option>
                                    <option value="SHA256">SHA256</option>
                                </select>
                            </div>
                            <div class="igir-field">
                                <label>Maximum Checksum</label>
                                <select value=${inputChecksumMax} onChange=${(e) => setInputChecksumMax(e.target.value)}>
                                    <option value="">Default</option>
                                    <option value="CRC32">CRC32</option>
                                    <option value="MD5">MD5</option>
                                    <option value="SHA1">SHA1</option>
                                    <option value="SHA256">SHA256</option>
                                </select>
                            </div>
                            <div class="igir-field">
                                <label>Archive Checksum Strategy</label>
                                <select value=${inputChecksumArchives} onChange=${(e) => setInputChecksumArchives(e.target.value)}>
                                    <option value="">Default</option>
                                    <option value="auto">Auto</option>
                                    <option value="always">Always</option>
                                    <option value="never">Never</option>
                                </select>
                            </div>
                            <div class="igir-field">
                                <label>Merge ROMs Mode</label>
                                <select value=${mergeRoms} onChange=${(e) => setMergeRoms(e.target.value)}>
                                    <option value="">Disabled</option>
                                    <option value="fullnonmerged">fullnonmerged</option>
                                    <option value="nonmerged">nonmerged</option>
                                    <option value="split">split</option>
                                    <option value="merged">merged</option>
                                </select>
                            </div>
                            <div class="igir-toggle-row">
                                <label class="igir-toggle-option">
                                    <input type="checkbox" checked=${mergeDiscs} onChange=${(e) => setMergeDiscs(e.target.checked)} />
                                    <span>Merge Discs</span>
                                </label>
                                <label class="igir-toggle-option">
                                    <input type="checkbox" checked=${excludeDisks} onChange=${(e) => setExcludeDisks(e.target.checked)} />
                                    <span>Exclude CHD Disks</span>
                                </label>
                                <label class="igir-toggle-option">
                                    <input type="checkbox" checked=${allowExcessSets} onChange=${(e) => setAllowExcessSets(e.target.checked)} />
                                    <span>Allow Excess Sets</span>
                                </label>
                                <label class="igir-toggle-option">
                                    <input type="checkbox" checked=${allowIncompleteSets} onChange=${(e) => setAllowIncompleteSets(e.target.checked)} />
                                    <span>Allow Incomplete Sets</span>
                                </label>
                            </div>
                            <div class="igir-field">
                                <label>Playlist Extensions</label>
                                <input type="text" value=${playlistExtensions} onInput=${(e) => setPlaylistExtensions(e.target.value)} placeholder=".cue,.gdi,.mdf,.chd" />
                            </div>
                            <div class="igir-field">
                                <label>Clean Exclude (comma globs)</label>
                                <input type="text" value=${cleanExclude} onInput=${(e) => setCleanExclude(e.target.value)} placeholder="e.g. **/*.txt" />
                            </div>
                            <div class="igir-field">
                                <label>Clean Backup Directory</label>
                                <input type="text" value=${cleanBackup} onInput=${(e) => setCleanBackup(e.target.value)} placeholder="/path/to/backup" />
                            </div>
                            <div class="igir-field">
                                <label>Report Output Path</label>
                                <input type="text" value=${reportOutput} onInput=${(e) => setReportOutput(e.target.value)} placeholder="/path/to/report.csv" />
                            </div>
                            <div class="igir-field">
                                <label>Fixdat Output Directory</label>
                                <input type="text" value=${fixdatOutput} onInput=${(e) => setFixdatOutput(e.target.value)} placeholder="/path/to/fixdats" />
                            </div>
                            <div class="igir-field">
                                <label>Dir2Dat Output Directory</label>
                                <input type="text" value=${dir2datOutput} onInput=${(e) => setDir2datOutput(e.target.value)} placeholder="/path/to/dir2dat" />
                            </div>
                            <div class="igir-field">
                                <label>Cache Path</label>
                                <input type="text" value=${cachePath} onInput=${(e) => setCachePath(e.target.value)} placeholder="/path/to/igir.cache" />
                            </div>
                            <div class="igir-field">
                                <label>Temp Directory</label>
                                <input type="text" value=${tempDir} onInput=${(e) => setTempDir(e.target.value)} placeholder="/path/to/temp" />
                            </div>
                            <div class="igir-field">
                                <label>Verbosity</label>
                                <select value=${verbose} onChange=${(e) => setVerbose(parseInt(e.target.value, 10))}>
                                    <option value="0">Normal</option>
                                    <option value="1">Verbose (-v)</option>
                                    <option value="2">Very Verbose (-vv)</option>
                                    <option value="3">Debug (-vvv)</option>
                                </select>
                            </div>
                        </div>
                    `}
                </div>

                <!-- Preview & Execute -->
                <div class="igir-section igir-execute-section">
                    <div class="igir-execute-actions">
                        <div class="igir-field" style="min-width: 220px;">
                            <label>Auto-Setup Workflow</label>
                            <select value=${autoSetupGoal} onChange=${(e) => setAutoSetupGoal(e.target.value)}>
                                ${workflowGoals.map((goal) => html`
                                    <option value=${goal.id}>${goal.label}</option>
                                `)}
                            </select>
                        </div>
                        <button
                            class="btn btn-secondary"
                            onClick=${handleAutoSetup}
                            disabled=${autoSetupBusy || inputPaths.length === 0}
                            title="Auto-configure commands, DATs, and safe defaults from selected input paths"
                        >
                            ${autoSetupBusy ? 'Auto-Setup...' : 'Auto-Setup'}
                        </button>
                        <button
                            class="btn btn-secondary"
                            onClick=${handleValidate}
                            disabled=${validating || selectedCommands.size === 0}
                        >
                            ${validating ? 'Validating...' : 'Validate & Preview'}
                        </button>
                        <button
                            class="btn btn-secondary"
                            onClick=${handleRunDryRun}
                            disabled=${validating || inputPaths.length === 0 || selectedDats.size === 0}
                            title="Execute clean dry-run preview (safe mode, requires DAT selection)"
                        >
                            ${validating ? 'Running Dry-Run...' : 'Run Clean Dry-Run'}
                        </button>
                        <button
                            class="btn btn-primary"
                            onClick=${handleExecute}
                            disabled=${executing || selectedCommands.size === 0 || inputPaths.length === 0}
                        >
                            ${executing ? 'Creating Job...' : 'Execute'}
                        </button>
                    </div>
                    ${executeNeedsConfirmation && html`
                        <div class="igir-field" style="margin-top: 8px;">
                            <label>Destructive operation detected. Type <code>RUN</code> to execute.</label>
                            <input
                                type="text"
                                value=${executeConfirmText}
                                onInput=${(e) => setExecuteConfirmText(e.target.value)}
                                placeholder="Type RUN to confirm"
                            />
                        </div>
                    `}
                    ${preflight && preflight.risk_factors && preflight.risk_factors.length > 0 && html`
                        <div class="igir-validation invalid">
                            <div class="igir-validation-warnings">
                                <strong>Preflight Risk Factors:</strong>
                                <ul>${preflight.risk_factors.map(r => html`<li>${r}</li>`)}</ul>
                            </div>
                        </div>
                    `}
                    ${validation && html`
                        <div class="igir-validation ${validation.valid ? 'valid' : 'invalid'}">
                            ${!validation.valid && validation.errors.length > 0 && html`
                                <div class="igir-validation-errors">
                                    <strong>Errors:</strong>
                                    <ul>${validation.errors.map(e => html`<li>${e}</li>`)}</ul>
                                </div>
                            `}
                            ${validation.warnings.length > 0 && html`
                                <div class="igir-validation-warnings">
                                    <strong>Warnings:</strong>
                                    <ul>${validation.warnings.map(w => html`<li>${w}</li>`)}</ul>
                                </div>
                            `}
                            ${validation.command_preview && html`
                                <div class="igir-command-preview">
                                    <strong>Command:</strong>
                                    <code>${validation.command_preview}</code>
                                </div>
                            `}
                        </div>
                    `}
                </div>
            </div>

            <!-- Igir Jobs Panel -->
            <div class="igir-jobs-panel">
                <div class="igir-jobs-header">
                    <h3>igir Jobs ${igirJobs.length > 0 ? `(${igirJobs.length})` : ''}</h3>
                    <div class="igir-jobs-actions">
                        ${activeJobCount > 0 && html`
                            <button class="btn btn-sm btn-secondary" onClick=${handleCancelAll}>
                                Cancel All
                            </button>
                        `}
                        ${completedJobCount > 0 && html`
                            <button class="btn btn-sm btn-secondary" onClick=${handleClearCompleted}>
                                Clear Done
                            </button>
                        `}
                        <button
                            class="btn btn-sm btn-secondary"
                            onClick=${() => api.getIgirJobs().then(setIgirJobs)}
                        >
                            ↻
                        </button>
                    </div>
                </div>
                <div class="igir-jobs-list">
                    ${igirJobs.length === 0
                        ? html`<div class="igir-jobs-empty">No igir jobs. Configure and execute above.</div>`
                        : igirJobs.map(job => html`
                            <${IgirJobCard} key=${job.id} job=${job} onCancel=${handleCancelJob} />
                        `)
                    }
                </div>
            </div>
        </div>
    `;
}

// ============ Main App ============

