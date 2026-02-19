import { formatSize, getFileIcon } from '../../api.js';
import { html, useEffect, useRef } from '../runtime/preactRuntime.js';
import {
    getDolphinProductPath,
    get3dsProductPath,
    is3dsFile,
    is3dsVerifyFile,
} from '../utils/fileTypeUtils.js';
import { isMacMetadataName } from '../utils/uiHelpers.js';

export function FileList({ entries, selectedFiles, canSelect, onNavigate, onToggleSelect, onShowInfo, onBrowseArchive, onRename, onDelete, onVerify, onCompress, conversionMode, verifiedCHDs, verifyProgress, chdMetadata, error, sortBy, sortOrder, onSort, onSelectAll, allSelected, isoHandling, onToggleIsoHandling, onOrganize }) {
    const visibleEntries = Array.isArray(entries)
        ? entries.filter((entry) => !isMacMetadataName(entry?.name || ''))
        : [];

    if (error) {
        return html`
            <div class="error-state">
                <div class="icon">⚠️</div>
                <p>Failed to load files</p>
                <p class="error-detail">${error}</p>
            </div>
        `;
    }

    if (visibleEntries.length === 0) {
        return html`
            <div class="empty-state">
                <img src="/static/images/logo.png" alt="" class="empty-state-logo" />
                <p>No files found</p>
                <p class="help-text">This folder is empty or contains no supported files</p>
            </div>
        `;
    }

    const isArchiveItem = (entry) => entry.is_archive_item || entry.in_archive;
    const isoIsDolphin = isoHandling === 'dolphin';
    const isoToggleEnabled = isoHandling === 'dolphin' || isoHandling === 'chdman';
    const isoModeLabel = isoHandling === 'dolphin'
        ? 'Dolphin'
        : isoHandling === 'chdman'
            ? 'CHDMAN'
            : '3DS mode';

    const handleClick = (entry, e) => {
        const ext = entry.extension?.toLowerCase();
        const isDolphinInfo = ['.rvz', '.wia', '.gcz', '.wbfs'].includes(ext)
            || (isoIsDolphin && ext === '.iso');
        const is3dsInfo = is3dsFile(entry.path);
        if (entry.type === 'directory') {
            onNavigate(entry.path);
        } else if (entry.type === 'archive') {
            // For archives, browse contents
            onBrowseArchive && onBrowseArchive(entry.path);
        } else if (entry.extension === '.chd' && !isArchiveItem(entry)) {
            // For CHD files, show info (but checkbox still works for selection)
            onShowInfo(entry.path);
        } else if (isArchiveItem(entry) && entry.chd_ready && entry.chd_path) {
            // For archive members with a converted CHD, show info for the output file
            onShowInfo && onShowInfo(entry.chd_path);
        } else if (isDolphinInfo && !isArchiveItem(entry)) {
            onShowInfo && onShowInfo(entry.path);
        } else if (is3dsInfo && !isArchiveItem(entry)) {
            onShowInfo && onShowInfo(entry.path);
        } else {
            // For all other files, toggle selection (pass event for shift-click support)
            onToggleSelect(entry, e);
        }
    };

    const getVerifiablePath = (entry) => {
        const ext = entry.extension?.toLowerCase();
        if (entry.chd_path && entry.chd_ready) return entry.chd_path;
        if (!isArchiveItem(entry) && entry.dolphin_ready) {
            const dolphinPath = getDolphinProductPath(entry);
            if (dolphinPath) return dolphinPath;
        }
        if (
            (['.rvz', '.wia', '.gcz', '.wbfs'].includes(ext) || (isoIsDolphin && ext === '.iso'))
            && !isArchiveItem(entry)
        ) {
            return entry.path;
        }
        if (!isArchiveItem(entry) && is3dsVerifyFile(entry.path)) {
            return entry.path;
        }
        if (!isArchiveItem(entry) && entry.z3ds_ready) {
            return entry.z3ds_path || get3dsProductPath(entry.path);
        }
        if (ext === '.chd') return entry.path;
        return null;
    };
    const getChdPath = getVerifiablePath;

    const handleVerifyClick = async (e, entry) => {
        e.stopPropagation();
        const chdPath = getChdPath(entry);
        if (!chdPath) return;
        await onVerify(chdPath, entry);
    };

    const handleIsoToggle = (e) => {
        e.stopPropagation();
        if (!isoToggleEnabled) return;
        if (onToggleIsoHandling) {
            onToggleIsoHandling();
        }
    };

    const getTooltip = (entry) => {
        const ext = entry.extension?.toLowerCase();
        if (entry.type === 'directory') return `Open folder: ${entry.name}`;
        if (entry.type === 'archive') {
            if (entry.archive_items != null) {
                if (entry.archive_items > 0) {
                    return `Archive: ${entry.name} (${entry.archive_items} image${entry.archive_items === 1 ? '' : 's'}) - Click to browse contents`;
                }
                return `Archive: ${entry.name} (no convertible images found)`;
            }
            return `Archive: ${entry.name} - Click to browse contents`;
        }
        if (ext === '.chd' && !isArchiveItem(entry)) return 'Click to view CHD info';
        if (isArchiveItem(entry) && entry.chd_ready && entry.chd_path) return 'Click to view output CHD info';
        if (isArchiveItem(entry) && entry.has_chd && !entry.chd_ready) return 'CHD conversion in progress';
        if (!isArchiveItem(entry) && ext === '.iso' && !isoIsDolphin) {
            return 'ISO info uses Dolphin tools. Switch Primary Tool to Dolphin for disc info/verify.';
        }
        if (
            (['.rvz', '.wia', '.gcz', '.wbfs'].includes(ext)
                || (isoIsDolphin && ext === '.iso'))
            && !isArchiveItem(entry)
        ) return 'Click to view disc info';
        if (!isArchiveItem(entry) && is3dsFile(entry.path)) return 'Click to view 3DS ROM info';
        if (canSelect(entry)) return 'Click to select';
        if (entry.convertible) return entry.has_chd ? 'Already converted' : entry.name;
        return entry.name;
    };

    const isVerified = (entry) => {
        const chdPath = getChdPath(entry);
        return !!(chdPath && verifiedCHDs && verifiedCHDs.has(chdPath));
    };

    const getSortIndicator = (column) => {
        if (sortBy !== column) return '';
        return sortOrder === 'asc' ? ' ▲' : ' ▼';
    };

    // Count selectable entries for the header checkbox
    const selectableCount = entries.filter(e => canSelect(e)).length;
    const hasSelectableEntries = selectableCount > 0;
    const selectedCount = entries.filter(e => canSelect(e) && selectedFiles.has(e.path)).length;
    const isIndeterminate = hasSelectableEntries && selectedCount > 0 && selectedCount < selectableCount;
    const selectAllRef = useRef(null);

    useEffect(() => {
        if (selectAllRef.current) {
            selectAllRef.current.indeterminate = isIndeterminate;
        }
    }, [isIndeterminate, selectableCount]);

    const handleSelectAllClick = (e) => {
        e.stopPropagation();
        if (selectAllRef.current) {
            selectAllRef.current.indeterminate = false;
        }
        onSelectAll();
    };

    return html`
        <div class="file-list-container">
            <div class="file-list-header">
                <div class="header-cell header-checkbox">
                    <input
                        type="checkbox"
                        class="checkbox"
                        checked=${hasSelectableEntries && allSelected}
                        disabled=${!hasSelectableEntries}
                        ref=${selectAllRef}
                        onClick=${handleSelectAllClick}
                        title=${hasSelectableEntries
            ? (allSelected ? 'Deselect all on this page' : `Select all on this page (${selectableCount})`)
            : 'No selectable files'}
                        aria-checked=${isIndeterminate ? 'mixed' : (hasSelectableEntries && allSelected ? 'true' : 'false')}
                    />
                </div>
                <div 
                    class="header-cell header-name sortable" 
                    onClick=${() => onSort('name')}
                    onKeyDown=${(e) => (e.key === 'Enter' || e.key === ' ') && onSort('name')}
                    role="button"
                    tabindex="0"
                    aria-sort=${sortBy === 'name' ? (sortOrder === 'asc' ? 'ascending' : 'descending') : 'none'}
                >
                    Name${getSortIndicator('name')}
                </div>
                <div 
                    class="header-cell header-size sortable" 
                    onClick=${() => onSort('size')}
                    onKeyDown=${(e) => (e.key === 'Enter' || e.key === ' ') && onSort('size')}
                    role="button"
                    tabindex="0"
                    aria-sort=${sortBy === 'size' ? (sortOrder === 'asc' ? 'ascending' : 'descending') : 'none'}
                >
                    Size${getSortIndicator('size')}
                </div>
                <div 
                    class="header-cell header-status sortable" 
                    onClick=${() => onSort('status')}
                    onKeyDown=${(e) => (e.key === 'Enter' || e.key === ' ') && onSort('status')}
                    role="button"
                    tabindex="0"
                    aria-sort=${sortBy === 'status' ? (sortOrder === 'asc' ? 'ascending' : 'descending') : 'none'}
                >
                    Status${getSortIndicator('status')}
                </div>
                <div class="header-cell header-actions"></div>
            </div>
            <ul class="file-list">
                ${visibleEntries.map(entry => {
                const chdPath = getChdPath(entry);
                const isVerifying = chdPath && verifyProgress && verifyProgress.has(chdPath);
                const entryExt = entry.extension?.toLowerCase();

                // Mode compatibility checks for inline compress button
                const isCreateMode = conversionMode.startsWith('create');
                const isExtractMode = conversionMode.startsWith('extract');
                const isDolphinMode = conversionMode.startsWith('dolphin_');
                const isZ3dsMode = conversionMode === 'z3ds_compress';

                const canInlineCompress =
                    // CHDMAN create modes only: source images -> CHD
                    (isCreateMode && isoHandling === 'chdman' && entry.convertible && !entry.has_chd) ||
                    // CHDMAN extract/copy modes: CHD inputs only
                    ((isExtractMode || conversionMode === 'copy') && entryExt === '.chd') ||
                    // Dolphin modes only
                    (isDolphinMode && isoHandling === 'dolphin' && entry.dolphin_convertible) ||
                    // 3DS mode only
                    (isZ3dsMode && isoHandling === 'z3ds' && entry.z3ds_convertible && !entry.has_z3ds);

                const isIsoEntry = entryExt === '.iso';
                const isDolphinExt = ['.rvz', '.wia', '.gcz', '.wbfs'].includes(entryExt)
                    || (isoIsDolphin && entryExt === '.iso');
                const canVerify = chdPath && (
                    entry.extension === '.chd'
                    || (isDolphinExt && !isArchiveItem(entry))
                    || (!isArchiveItem(entry) && is3dsVerifyFile(chdPath))
                    || (isArchiveItem(entry) && entry.chd_ready)
                );
                const archiveItems = entry.archive_items;
                const archiveHasChd = entry.archive_has_chd;
                return html`
                <li
                    key=${entry.path}
                    class="file-item ${selectedFiles.has(entry.path) ? 'selected' : ''}"
                    onClick=${(e) => handleClick(entry, e)}
                    title=${getTooltip(entry)}
                >
                    <div class="file-cell file-checkbox">
                        ${entry.type !== 'directory' && entry.type !== 'archive' && html`
                            <input
                                type="checkbox"
                                class="checkbox"
                                checked=${selectedFiles.has(entry.path)}
                                disabled=${!canSelect(entry)}
                                onClick=${(e) => { e.stopPropagation(); onToggleSelect(entry, e); }}
                            />
                        `}
                    </div>
                    <div class="file-cell file-name">
                        <span class="icon">${getFileIcon(entry)}</span>
                        <span class="name" title=${entry.name}>${entry.name}</span>
                        ${entry.type !== 'directory' && entry.type !== 'archive' && !canSelect(entry) && html`
                            <span class="incompatible-warning" title="This file cannot be selected in the current conversion mode">⚠️</span>
                        `}
                    </div>
                    <div class="file-cell file-size">
                        ${entry.size != null && entry.size !== undefined ? formatSize(entry.size) : ''}
                    </div>
                    <div class="file-cell file-status">
                        ${entry.type !== 'archive' && entry.has_chd && html`
                            <span class="status has-chd" title="A CHD file already exists for this source">CHD exists</span>
                        `}
                        ${entry.type !== 'archive' && entry.has_z3ds && html`
                            <span class="status has-chd" title="A compressed 3DS file (.zcci/.zcia/.z3ds) already exists">Z3DS exists</span>
                        `}
                        ${entry.type !== 'archive' && entry.convertible && !entry.has_chd && html`
                            <span class="status convertible" title="Can be converted to CHD">Convertible</span>
                        `}
                        ${entry.type !== 'archive' && entry.z3ds_convertible && !entry.has_z3ds && html`
                            <span class="status convertible" title="Nintendo 3DS ROM - Can be compressed to ZCCI/ZCIA/Z3DS format">3DS ROM</span>
                        `}
                        ${entry.type === 'archive' && archiveItems != null && archiveItems > 0 && html`
                            <span class="status convertible" title="Convertible images inside this archive">
                                ${archiveItems} image${archiveItems === 1 ? '' : 's'}
                            </span>
                        `}
                        ${entry.type === 'archive' && archiveItems === 0 && html`
                            <span class="status" title="No convertible images found in this archive">No images</span>
                        `}
                        ${entry.type === 'archive' && archiveHasChd != null && archiveHasChd > 0 && html`
                            <span class="status has-chd" title="CHD files already exist for this archive">
                                ${archiveHasChd}/${archiveItems} CHD
                            </span>
                        `}
                        ${isIsoEntry && !isArchiveItem(entry) && (isoToggleEnabled
                        ? html`
                                <button
                                    type="button"
                                    class="status iso-handling iso-toggle"
                                    title="Click to toggle ISO handling"
                                    onClick=${handleIsoToggle}
                                >
                                    ISO: ${isoModeLabel}
                                </button>
                            `
                        : html`
                                <span
                                    class="status iso-handling"
                                    title="ISO workflows are unavailable while Primary Tool is set to 3DS"
                                >
                                    ISO: ${isoModeLabel}
                                </span>
                            `
                    )}
                        ${isVerified(entry) && html`
                            <span class="status verified" title="Integrity verified">✓ Verified</span>
                        `}
                        ${isVerifying && html`
                            <span class="status convertible" title="Verifying integrity">
                                ${(() => {
                            const status = chdPath ? verifyProgress.get(chdPath) : null;
                            if (!status) return 'Verifying...';
                            if (status.progress != null) return `Verifying ${status.progress}%`;
                            return status.message || 'Verifying...';
                        })()}
                            </span>
                        `}
                        ${entry.extension === '.chd' && chdMetadata && (() => {
                        const meta = chdMetadata.get(entry.path);
                        if (meta?.media_type === 'dvd') {
                            return html`<span class="status media-badge dvd" title="DVD Format">DVD</span>`;
                        }
                        if (meta?.media_type === 'cd') {
                            return html`<span class="status media-badge cd" title="CD Format">CD</span>`;
                        }
                        return null;
                    })()}
                    </div>
                    <div class="file-cell file-actions-cell" onClick=${(e) => e.stopPropagation()}>
                        ${entry.type === 'directory' && onOrganize && html`
                            <button
                                class="btn-icon"
                                onClick=${(e) => { e.stopPropagation(); onOrganize(entry); }}
                                title="Organize this directory with igir"
                            >
                                📋
                            </button>
                        `}
                        ${canInlineCompress && html`
                            <button
                                class="btn-icon"
                                onClick=${(e) => { e.stopPropagation(); onCompress(entry); }}
                                title="Compress/Convert this file now"
                            >
                                ⚡
                            </button>
                        `}
                        ${canVerify && !isVerified(entry) && html`
                            <button
                                class="btn-icon"
                                onClick=${(e) => handleVerifyClick(e, entry)}
                                title="Verify integrity"
                                disabled=${isVerifying}
                            >
                                ${isVerifying ? '⏳' : '🔍'}
                            </button>
                        `}
                        ${!isArchiveItem(entry) && html`
                            <button
                                class="btn-icon"
                                onClick=${(e) => { e.stopPropagation(); onRename(entry); }}
                                title="Rename"
                            >
                                ✏️
                            </button>
                            <button
                                class="btn-icon btn-danger"
                                onClick=${(e) => { e.stopPropagation(); onDelete(entry); }}
                                title="Delete"
                            >
                                🗑️
                            </button>
                        `}
                    </div>
                </li>
                `;
            })}
            </ul>
        </div>
    `;
}

