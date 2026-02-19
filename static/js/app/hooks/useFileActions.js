import { api, isDolphinFile } from '../../api.js';
import { useCallback } from '../runtime/preactRuntime.js';
import {
    getDolphinProductPath,
    get3dsProductPath,
    is3dsFile,
    is3dsSourceFile,
    is3dsVerifyFile,
} from '../utils/fileTypeUtils.js';

export function useFileMutationHandlers({
    notify,
    isoHandling,
    refreshFileList,
    setVerifiedCHDs,
    setVerifyProgress,
}) {
    const handleRename = useCallback(async (path, newName) => {
        await api.renameFile(path, newName);
        notify(`✓ Renamed to ${newName}`, 'success');
        if (path.toLowerCase().endsWith('.chd')) {
            const lastSlash = path.lastIndexOf('/');
            const newPath = lastSlash >= 0 ? `${path.slice(0, lastSlash)}/${newName}` : newName;
            setVerifiedCHDs((prev) => {
                if (!prev.has(path)) return prev;
                const next = new Set(prev);
                next.delete(path);
                next.add(newPath);
                return next;
            });
        }
        refreshFileList(true);
    }, [notify, refreshFileList]);

    const handleDelete = useCallback(async (path) => {
        await api.deleteFile(path);
        notify('✓ File deleted', 'success');
        if (path.toLowerCase().endsWith('.chd')) {
            setVerifiedCHDs((prev) => {
                if (!prev.has(path)) return prev;
                const next = new Set(prev);
                next.delete(path);
                return next;
            });
        }
        refreshFileList(true);
    }, [notify, refreshFileList]);

    const handleVerify = useCallback(async (productPath, entry = null) => {
        const isArchiveItem = entry?.is_archive_item || entry?.in_archive || (typeof productPath === 'string' && productPath.includes('::'));
        const isIso = typeof productPath === 'string' && productPath.toLowerCase().endsWith('.iso');
        const is3dsSource = is3dsSourceFile(productPath);

        let forceDolphin = false;
        let force3ds = false;

        if (isIso && !isArchiveItem) {
            if (isoHandling !== 'dolphin') {
                notify('ISO verification uses Dolphin tools. Switch ISO handling to Dolphin to verify.', 'info');
                return;
            }
            forceDolphin = true;
        } else if (is3dsSource && !isArchiveItem) {
            if (isoHandling !== 'z3ds') {
                notify('3DS verification requires 3DS mode. Switch ISO handling to 3DS to verify.', 'info');
                return;
            }
            force3ds = true;
        }

        const verifyPath = force3ds && is3dsSourceFile(productPath)
            ? (entry?.z3ds_path || get3dsProductPath(productPath) || productPath)
            : productPath;
        const dolphin = forceDolphin || isDolphinFile(verifyPath);
        const z3ds = force3ds || is3dsFile(verifyPath);
        const verifyFn = dolphin ? api.verifyDolphin.bind(api) : (z3ds ? api.verify3DS.bind(api) : api.verifyCHD.bind(api));
        const label = dolphin ? 'Disc' : (z3ds ? '3DS ROM' : 'CHD');

        setVerifyProgress((prev) => new Map(prev).set(verifyPath, { progress: 0, message: 'Starting verification...' }));
        try {
            const result = await verifyFn(verifyPath, {
                onProgress: (update) => {
                    setVerifyProgress((prev) => {
                        const next = new Map(prev);
                        next.set(verifyPath, {
                            progress: update.progress,
                            message: update.message,
                        });
                        return next;
                    });
                },
            });
            if (result.valid) {
                setVerifiedCHDs((prev) => new Set([...prev, verifyPath]));
                notify(`✓ ${label} verified successfully`, 'success');
            } else {
                notify(`✗ ${label} verification failed: ${result.message}`, 'error');
            }
            return result;
        } catch (err) {
            notify(`✗ ${label} verification failed: ${err.message}`, 'error');
            throw err;
        } finally {
            setVerifyProgress((prev) => {
                const next = new Map(prev);
                next.delete(verifyPath);
                return next;
            });
        }
    }, [isoHandling, notify]);

    return {
        handleRename,
        handleDelete,
        handleVerify,
    };
}

export function useBulkFileActions({
    selectedFiles,
    notify,
    isoHandling,
    setBulkDeleteEntries,
    setBulkVerifyItems,
    setVerifiedCHDs,
    setSelectedFiles,
    refreshFileList,
}) {
    const getDeletableSelection = useCallback(() => {
        const entries = [];
        for (const [path, entry] of selectedFiles) {
            if (entry && entry.type !== 'directory' && !entry.is_archive_item && !path.includes('::')) {
                entries.push(entry);
            }
        }
        return entries;
    }, [selectedFiles]);

    const getVerifiableItems = useCallback(() => {
        const items = [];
        for (const [path, entry] of selectedFiles) {
            if (!entry) continue;
            const ext = entry.extension?.toLowerCase();
            const isArchiveItem = entry.is_archive_item || entry.in_archive;
            const filename = entry.name || path.split('/').pop();

            if (entry.chd_path && entry.chd_ready) {
                items.push({ path: entry.chd_path, filename, kind: 'chd' });
                continue;
            }
            if (ext === '.chd') {
                items.push({ path, filename, kind: 'chd' });
                continue;
            }
            if (!isArchiveItem && ['.rvz', '.wia', '.gcz', '.wbfs'].includes(ext)) {
                items.push({ path, filename, kind: 'dolphin' });
                continue;
            }
            if (!isArchiveItem && entry.dolphin_ready) {
                const dolphinPath = getDolphinProductPath(entry);
                if (dolphinPath) {
                    items.push({ path: dolphinPath, filename, kind: 'dolphin' });
                    continue;
                }
            }
            if (!isArchiveItem && ext === '.iso') {
                items.push({ path, filename, kind: 'iso' });
            }
            if (!isArchiveItem && is3dsVerifyFile(path)) {
                items.push({ path, filename, kind: 'z3ds' });
                continue;
            }
            if (!isArchiveItem && is3dsSourceFile(path) && entry.z3ds_ready) {
                const productPath = entry.z3ds_path || get3dsProductPath(path);
                if (productPath) {
                    items.push({ path: productPath, filename, kind: 'z3ds' });
                }
            }
        }
        return items;
    }, [selectedFiles]);

    const handleBulkDeleteClick = useCallback(() => {
        const entries = getDeletableSelection();
        if (entries.length === 0) {
            notify('⚠ No deletable files selected', 'error');
            return;
        }
        setBulkDeleteEntries(entries);
    }, [getDeletableSelection, notify]);

    const handleBulkVerifyClick = useCallback(() => {
        const items = getVerifiableItems();
        if (items.length === 0) {
            notify('⚠ No verifiable files selected', 'error');
            return;
        }
        const isoItems = items.filter((item) => item.kind === 'iso');
        const _3dsItems = items.filter((item) => item.kind === 'z3ds');
        let finalItems = items.filter((item) => item.kind !== 'iso' && item.kind !== 'z3ds');

        if (isoItems.length > 0) {
            if (isoHandling === 'dolphin') {
                finalItems = finalItems.concat(isoItems.map((item) => ({ ...item, kind: 'dolphin' })));
            } else {
                notify('ISO verification uses Dolphin tools. Switch ISO handling to Dolphin to verify ISO files.', 'info');
            }
        }
        if (_3dsItems.length > 0) {
            finalItems = finalItems.concat(_3dsItems.map((item) => ({ ...item, kind: 'z3ds' })));
        }

        if (finalItems.length === 0) {
            notify('⚠ No files selected for verification', 'error');
            return;
        }
        setBulkVerifyItems(finalItems);
    }, [getVerifiableItems, notify, isoHandling]);

    const handleBulkVerifyComplete = useCallback((result) => {
        api.getVerifiedCHDs()
            .then((data) => {
                if (data && Array.isArray(data.verified)) {
                    setVerifiedCHDs(new Set(data.verified));
                }
            })
            .catch(() => {});

        if (result.verified > 0) {
            notify(
                `✓ Verified ${result.verified} file${result.verified > 1 ? 's' : ''}${result.failed > 0 ? `, ${result.failed} failed` : ''}`,
                result.failed > 0 ? 'warning' : 'success',
            );
        }
        setSelectedFiles(new Map());
    }, [notify]);

    const handleAddVerifiedCHD = useCallback((chdPath) => {
        setVerifiedCHDs((prev) => new Set([...prev, chdPath]));
    }, []);

    const handleBulkDeleteRefresh = useCallback(() => {
        refreshFileList(true);
        setSelectedFiles(new Map());
        api.getVerifiedCHDs()
            .then((data) => {
                if (data && Array.isArray(data.verified)) {
                    setVerifiedCHDs(new Set(data.verified));
                }
            })
            .catch(() => {});
    }, [refreshFileList]);

    return {
        getDeletableSelection,
        getVerifiableItems,
        handleBulkDeleteClick,
        handleBulkVerifyClick,
        handleBulkVerifyComplete,
        handleAddVerifiedCHD,
        handleBulkDeleteRefresh,
    };
}
