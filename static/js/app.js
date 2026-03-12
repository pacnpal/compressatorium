// Main Compressatorium App
import { api, formatSize, getFileIcon, isDolphinFile } from './api.js';

const { html, render, useState, useEffect, useRef, useCallback, useMemo } = window;
const ISO_TOOL_STORAGE_KEY = 'primary_tool_preference';
const SHOW_METADATA_JOBS_STORAGE_KEY = 'compressatorium_show_metadata_jobs';
const DEFAULT_DOLPHIN_COMPRESSION_LEVEL = '5';
const DEFAULT_PAGE_SIZE = '50';
const DEFAULT_SEARCH_AUTO_RETURN_TO_FILE_LIST = true;
const MAX_VISIBLE_CREATING_PLACEHOLDERS = 100;
const PAGE_SIZE_OPTIONS = [
    { value: '25', label: '25' },
    { value: '50', label: '50' },
    { value: '100', label: '100' },
    { value: '250', label: '250' },
    { value: 'all', label: 'All' }
];
const isMacMetadataName = (name) => name === '.DS_Store' || name.startsWith('._') || name === '__MACOSX';
const Z3DS_SOURCE_EXTENSIONS = ['.3ds', '.cci', '.cia'];
const Z3DS_VERIFY_EXTENSIONS = ['.z3ds', '.zcci', '.zcia'];
const Z3DS_INFO_EXTENSIONS = [...Z3DS_SOURCE_EXTENSIONS, ...Z3DS_VERIFY_EXTENSIONS];
const Z3DS_OUTPUT_EXTENSION_BY_SOURCE = {
    '.3ds': '.z3ds',
    '.cci': '.zcci',
    '.cia': '.zcia'
};

const getFileExtension = (path) => {
    if (typeof path !== 'string') return '';
    const lower = path.toLowerCase();
    const lastSlash = Math.max(lower.lastIndexOf('/'), lower.lastIndexOf('\\'));
    const filename = lastSlash >= 0 ? lower.slice(lastSlash + 1) : lower;
    const dot = filename.lastIndexOf('.');
    if (dot <= 0) return '';
    return filename.slice(dot);
};

const is3dsSourceFile = (path) => Z3DS_SOURCE_EXTENSIONS.includes(getFileExtension(path));
const is3dsVerifyFile = (path) => Z3DS_VERIFY_EXTENSIONS.includes(getFileExtension(path));
const is3dsFile = (path) => Z3DS_INFO_EXTENSIONS.includes(getFileExtension(path));

const get3dsProductPath = (path) => {
    if (typeof path !== 'string') return null;
    const ext = getFileExtension(path);
    const outExt = Z3DS_OUTPUT_EXTENSION_BY_SOURCE[ext];
    if (!outExt || !path.toLowerCase().endsWith(ext)) return null;
    return `${path.slice(0, -ext.length)}${outExt}`;
};

const getDolphinProductPath = (entry) => {
    if (
        !entry
        || typeof entry.path !== 'string'
        || (!entry.has_rvz && !entry.dolphin_ready)
    ) return null;
    if (typeof entry.dolphin_path === 'string' && entry.dolphin_path) {
        return entry.dolphin_path;
    }
    const ext = getFileExtension(entry.path);
    if (!ext || !entry.path.toLowerCase().endsWith(ext)) return null;
    if (ext === '.iso') {
        return `${entry.path.slice(0, -ext.length)}.rvz`;
    }
    return entry.path;
};

const getModeTerm = (isoHandling, kind) => {
    if (kind === 'file') return 'file';

    if (isoHandling === 'dolphin') {
        if (kind === 'product') return 'Dolphin output';
        if (kind === 'verification') return 'disc image';
    } else if (isoHandling === 'z3ds') {
        if (kind === 'product') return 'compressed 3DS';
        if (kind === 'verification') return '3DS output';
    } else {
        if (kind === 'product') return 'CHD';
        if (kind === 'verification') return 'CHD';
    }

    return 'item';
};

const normalizeDolphinLevel = (value) => {
    const raw = `${value ?? ''}`.trim();
    if (!raw) return DEFAULT_DOLPHIN_COMPRESSION_LEVEL;
    if (!/^\d+$/.test(raw)) return DEFAULT_DOLPHIN_COMPRESSION_LEVEL;
    return raw;
};

const getPrimaryToolLabel = (toolSelection) => {
    if (toolSelection === 'chdman') return 'CHDMAN';
    if (toolSelection === 'dolphin') return 'Dolphin';
    if (toolSelection === 'z3ds') return '3DS';
    return 'None selected';
};

const getFilterOptions = (tool) => {
    const common = [
        { value: '', label: 'All Types' },
        { value: '.zip,.7z,.rar', label: 'Archives' },
        { value: '.chd', label: 'CHD Files' }
    ];

    if (tool === 'dolphin') {
        return [
            ...common,
            { value: '.iso,.rvz,.wia,.gcz,.wbfs', label: 'GameCube/Wii Images' },
            { value: '.iso', label: 'ISO Files' },
            { value: 'custom', label: 'Custom...' }
        ];
    }

    if (tool === 'z3ds') {
        return [
            ...common,
            { value: '.cci,.cia,.3ds', label: '3DS ROMs' },
            { value: 'custom', label: 'Custom...' }
        ];
    }

    // Default (CHDMAN)
    return [
        ...common,
        { value: '.cue,.bin,.gdi,.iso', label: 'Disc Images' },
        { value: '.iso', label: 'ISO Files' },
        { value: 'custom', label: 'Custom...' }
    ];
};



const getPrimaryToolHint = (toolSelection) => {
    if (toolSelection === null) {
        return html`<span role="img" aria-label="Warning">⚠️</span> Please select your primary tool above to get started`;
    }
    if (toolSelection === 'chdman') {
        return html`Convert disc images to CHD format • Supports CD/DVD/LaserDisc`;
    }
    if (toolSelection === 'dolphin') {
        return html`Convert GameCube/Wii disc images • Supports RVZ, WIA, GCZ, ISO formats`;
    }
    if (toolSelection === 'z3ds') {
        return html`Compress Nintendo 3DS ROMs • Supports .cci/.cia/.3ds (cart & CIA) → .zcci/.zcia/.z3ds • ~50% size reduction • <strong>Decrypted ROMs only</strong>`;
    }
    return html`Current: ${getPrimaryToolLabel(toolSelection)}`;
};

const MODE_GROUPS = [
    {
        id: 'create',
        label: 'Create CHD',
        options: [
            { value: 'createcd', label: 'Create CD CHD (Dreamcast, PS1, Sega CD)' },
            { value: 'createdvd', label: 'Create DVD CHD (PSP, PS2)' },
            { value: 'createraw', label: 'Create Raw CHD' },
            { value: 'createhd', label: 'Create HD CHD' },
            { value: 'createld', label: 'Create LaserDisc CHD' }
        ]
    },
    {
        id: 'extract',
        label: 'Extract from CHD',
        options: [
            { value: 'extractcd', label: 'Extract CD (cue/bin)' },
            { value: 'extractdvd', label: 'Extract DVD (iso)' },
            { value: 'extractraw', label: 'Extract Raw' },
            { value: 'extracthd', label: 'Extract HD' },
            { value: 'extractld', label: 'Extract LaserDisc (avi)' }
        ]
    },
    {
        id: 'copy',
        label: 'Copy / Recompress',
        options: [
            { value: 'copy', label: 'Copy / Recompress CHD' }
        ]
    },
    {
        id: 'dolphin',
        label: 'Dolphin (GameCube/Wii)',
        options: [
            { value: 'dolphin_rvz', label: 'Convert to RVZ (recommended)' },
            { value: 'dolphin_wia', label: 'Convert to WIA' },
            { value: 'dolphin_gcz', label: 'Convert to GCZ' },
            { value: 'dolphin_iso', label: 'Convert to ISO (extract)' }
        ]
    },
    {
        id: 'z3ds',
        label: 'Nintendo 3DS',
        options: [
            { value: 'z3ds_compress', label: 'Compress to ZCCI/ZCIA/Z3DS' }
        ]
    }
];

// ============ Help Component ============

function HelpPanel({ onClose, isoHandling }) {
    const toolLabel = getPrimaryToolLabel(isoHandling);
    return html`
        <div class="help-panel">
            <div class="help-header">
                <h3>Quick Start Guide</h3>
                <button class="btn btn-sm btn-secondary" onClick=${onClose}>×</button>
            </div>
            <div class="help-content">
                <h4>How to use Compressatorium</h4>
                <ol>
                    <li><strong>Select Primary Tool</strong> - Choose CHDMAN, Dolphin, or 3DS at the top</li>
                    <li><strong>Select a Volume</strong> - Choose a mounted directory from the left panel</li>
                    <li><strong>Browse Files</strong> - Navigate through folders to find your disc images</li>
                    <li><strong>Select Files</strong> - Click checkboxes next to files you want to convert</li>
                    <li><strong>Choose Mode</strong>:
                        <ul>
                            <li><em>CHDMAN</em> - Create/Extract/Copy CHD files (CD/DVD/LaserDisc)</li>
                            <li><em>Dolphin</em> - Convert GameCube/Wii images (RVZ/WIA/GCZ/ISO)</li>
                            <li><em>3DS</em> - Compress Nintendo 3DS ROMs (.cci/.cia/.3ds → .zcci/.zcia/.z3ds)</li>
                        </ul>
                    </li>
                    <li><strong>Queue</strong> - Click the action button to add jobs to the queue</li>
                </ol>
                <h4>File Types</h4>
                <ul>
                    <li>💽 <strong>.gdi, .cue, .bin</strong> - Can be converted to CHD (CHDMAN)</li>
                    <li>🧭 <strong>.iso</strong> - Handled by ${toolLabel} for info/verify operations</li>
                    <li>💿 <strong>.chd</strong> - MAME CHD format (click to view information)</li>
                    <li>🎮 <strong>.rvz, .wia, .gcz, .wbfs</strong> - GameCube/Wii images (Dolphin)</li>
                    <li>🎮 <strong>.cci, .cia, .3ds</strong> - Nintendo 3DS ROMs:
                        <ul style="margin-top: 5px; font-size: 0.9em;">
                            <li><em>.cci</em> - CCI (Cart Image) format, cartridge dumps</li>
                            <li><em>.cia</em> - CIA (Installable Archive) format, updates/DLC</li>
                            <li><em>.3ds</em> - Alternative cart dump format (same as .cci)</li>
                            <li><em>Outputs:</em> .zcci, .zcia, .z3ds (compressed with ZStandard)</li>
                        </ul>
                    </li>
                    <li>📦 <strong>.zip, .7z, .rar</strong> - Archives (click to browse contents)</li>
                </ul>
                <h4>Compression Tips</h4>
                <ul>
                    <li><strong>CHDMAN:</strong> zlib is most compatible; lzma yields smaller files but slower encoding</li>
                    <li><strong>Dolphin:</strong> RVZ is recommended for best compression with fast decompression</li>
                    <li><strong>3DS:</strong> Uses seekable ZStandard compression (~50% size reduction)
                        <ul style="margin-top: 5px; font-size: 0.9em;">
                            <li>Natively supported by Azahar emulator (v2123+)</li>
                            <li>.3ds and .cci are the same format with different extensions</li>
                            <li>ROMs must be decrypted before compression</li>
                        </ul>
                    </li>
                    <li><strong>Delete-on-verify:</strong> Automatically removes source files after successful conversion</li>
                </ul>
                <p class="compression-note">
                    Omitting <code>-c</code> would use chdman defaults; this app always sends an explicit choice to avoid surprises.
                </p>
                <h4>Dolphin Formats</h4>
                <ul>
                    <li><strong>RVZ</strong> is the recommended format for Dolphin emulator.</li>
                    <li><strong>zstd</strong> compression gives the best speed/size balance for RVZ.</li>
                    <li><strong>Compression levels</strong> are required for RVZ/WIA (for example: <code>zstd:5</code>).</li>
                    <li><strong>WIA</strong> is an older compressed format; prefer RVZ for new conversions.</li>
                    <li><strong>GCZ</strong> uses fixed deflate compression (no codec selection).</li>
                    <li><strong>ISO</strong> output extracts to uncompressed disc image.</li>
                </ul>
            </div>
        </div>
    `;
}

// ============ Components ============

function VolumeList({ volumes, selectedVolume, onSelect, loading, error }) {
    if (error) {
        return html`
            <div class="error-state">
                <p>Failed to load volumes</p>
                <p class="error-detail">${error}</p>
                <button class="btn btn-sm btn-primary" onClick=${() => location.reload()}>
                    Retry
                </button>
            </div>
        `;
    }

    if (loading) {
        return html`<div class="loading"><div class="spinner"></div>Loading volumes...</div>`;
    }

    if (volumes.length === 0) {
        return html`
            <div class="empty-state">
                <img src="/static/images/logo.png" alt="" class="empty-state-logo" />
                <p>No volumes configured</p>
                <p class="help-text">Mount directories under /data/* or set COMPRESSATORIUM_VOLUMES explicitly</p>
            </div>
        `;
    }

    return html`
        <ul class="volume-list">
            ${volumes.map(vol => html`
                <li
                    key=${vol.path}
                    class="volume-item ${selectedVolume?.path === vol.path ? 'active' : ''}"
                    onClick=${() => onSelect(vol)}
                    title="Path: ${vol.path}"
                >
                    <span class="icon">💾</span>
                    <span>${vol.name}</span>
                </li>
            `)}
        </ul>
    `;
}

function Breadcrumb({ path, volume, onNavigate }) {
    if (!path || !volume) return null;

    const parts = path.replace(volume.path, '').split('/').filter(Boolean);

    const crumbs = [
        { name: volume.name, path: volume.path }
    ];

    let currentPath = volume.path;
    for (const part of parts) {
        currentPath = currentPath + '/' + part;
        crumbs.push({ name: part, path: currentPath });
    }

    return html`
        <div class="breadcrumb">
            ${crumbs.map((crumb, i) => html`
                <span key=${crumb.path}>
                    ${i > 0 && html`<span class="breadcrumb-separator">/</span>`}
                    <span
                        class="breadcrumb-item"
                        onClick=${() => onNavigate(crumb.path)}
                        title=${crumb.path}
                    >
                        ${crumb.name}
                    </span>
                </span>
            `)}
        </div>
    `;
}

function FileList({ entries, selectedFiles, canSelect, onNavigate, onToggleSelect, onShowInfo, onBrowseArchive, onRename, onDelete, onVerify, onCompress, conversionMode, verifiedCHDs, verifyProgress, chdMetadata, error, sortBy, sortOrder, onSort, onSelectAll, allSelected, isoHandling, onToggleIsoHandling }) {
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

function JobList({
    jobs,
    onCancel,
    emptyTitle = 'No conversion jobs',
    emptyHelpText = 'Select files and click Convert to queue jobs',
}) {
    if (jobs.length === 0) {
        return html`
            <div class="empty-state">
                <div class="icon">⏳</div>
                <p>${emptyTitle}</p>
                <p class="help-text">${emptyHelpText}</p>
            </div>
        `;
    }

    const getStatusText = (job) => {
        switch (job.status) {
            case 'creating': return 'Creating job...';
            case 'queued': return 'Waiting in queue';
            case 'processing': return `Processing: ${job.progress}%`;
            case 'completed': return 'Completed';
            case 'failed': return 'Failed';
            case 'cancelled': return 'Cancelled';
            default: return job.status;
        }
    };

    const getStatusIcon = (job) => {
        switch (job.status) {
            case 'creating': return '⏳';
            case 'queued': return '⏸️';
            case 'processing': return '⚙️';
            case 'completed': return '✅';
            case 'failed': return '❌';
            case 'cancelled': return '🚫';
            default: return '📄';
        }
    };

    const getOutputDir = (path) => {
        if (!path) return 'Unknown';
        const parts = path.split('/');
        parts.pop(); // Remove filename
        return parts.length > 0 ? parts.join('/') : '/';
    };

    const getOutputFilename = (path) => {
        if (!path) return 'Unknown';
        return path.split('/').pop();
    };

    return html`
        <ul class="job-list">
            ${jobs.map(job => html`
                <li key=${job.id} class="job-item">
                    <div class="job-header">
                        <span class="job-status-icon" title=${getStatusText(job)}>${getStatusIcon(job)}</span>
                        <span class="job-name" title=${job.file_path}>${job.filename}</span>
                        <span class="job-status ${job.status}">${job.status}</span>
                    </div>

                    ${job.output_path && html`
                        <div class="job-output-info" style="font-size: 0.75rem; color: var(--text-secondary); margin: 4px 0; padding-left: 24px;">
                            <span title="Output: ${job.output_path}">→ ${getOutputFilename(job.output_path)}</span>
                            <span style="opacity: 0.7;"> in ${getOutputDir(job.output_path)}</span>
                        </div>
                    `}

                    ${job.status === 'creating' && html`
                        <div class="progress-bar">
                            <div class="progress-fill creating" style="width: 100%; animation: pulse 1.5s infinite;"></div>
                        </div>
                        <div class="progress-text" style="color: var(--text-secondary);">
                            Setting up job...
                        </div>
                    `}

                    ${job.status === 'queued' && html`
                        <div class="progress-text" style="color: var(--text-secondary);">
                            Waiting for other jobs to complete...
                        </div>
                    `}

                    ${job.status === 'processing' && html`
                        <div class="progress-bar">
                            <div class="progress-fill" style="width: ${job.progress}%"></div>
                        </div>
                        <div class="progress-text">
                            ${job.progress}% - ${job.message || 'Processing...'}
                        </div>
                    `}

                    ${job.status === 'completed' && html`
                        <div class="job-success" style="color: var(--success); font-size: 0.8rem; padding-left: 24px;">
                            ${job.mode === 'metadata_scan'
                                ? (job.message || 'Scan complete')
                                : `Job complete${job.output_size ? ` - ${formatSize(job.output_size)}` : ''}`}
                        </div>
                    `}

                    ${job.error_message && html`
                        <div class="job-error" style="padding-left: 24px;">${job.error_message}</div>
                    `}

                    <div class="job-actions">
                        ${['queued', 'processing'].includes(job.status) && job.mode !== 'metadata_scan' && html`
                            <button class="btn btn-sm btn-secondary" onClick=${() => onCancel(job.id)} title="Cancel this job">
                                Cancel
                            </button>
                        `}
                    </div>
                </li>
            `)}
        </ul>
    `;
}

function CHDInfoModal({ path, onClose, infoMode, useDolphin }) {
    const [info, setInfo] = useState(null);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState(null);

    const mode = useMemo(() => {
        if (infoMode === 'dolphin' || infoMode === 'z3ds' || infoMode === 'chd') {
            return infoMode;
        }
        if (Boolean(useDolphin) || (path ? isDolphinFile(path) : false)) {
            return 'dolphin';
        }
        if (path ? is3dsFile(path) : false) {
            return 'z3ds';
        }
        return 'chd';
    }, [infoMode, useDolphin, path]);

    const dolphin = mode === 'dolphin';
    const z3ds = mode === 'z3ds';

    useEffect(() => {
        if (path) {
            setLoading(true);
            setError(null);
            const fetchInfo = dolphin
                ? api.getDolphinInfo(path)
                : z3ds
                    ? api.getZ3DSInfo(path)
                    : api.getCHDInfo(path);
            fetchInfo
                .then(setInfo)
                .catch(e => setError(e.message))
                .finally(() => setLoading(false));
        }
    }, [path, dolphin, z3ds]);

    if (!path) return null;

    const filename = path.split('/').pop();
    const title = dolphin ? 'Disc Information' : (z3ds ? '3DS ROM Information' : 'CHD Information');
    const loadingText = dolphin ? 'Loading disc info...' : (z3ds ? 'Loading 3DS ROM info...' : 'Loading CHD info...');
    const errorText = dolphin ? 'Failed to read disc image' : (z3ds ? 'Failed to read 3DS ROM' : 'Failed to read CHD file');

    return html`
        <div class="modal-overlay" onClick=${onClose}>
            <div class="modal" onClick=${(e) => e.stopPropagation()}>
                <div class="modal-header">
                    <h3>${title}: ${filename}</h3>
                    <button class="modal-close" onClick=${onClose} title="Close">×</button>
                </div>
                ${loading && html`<div class="loading"><div class="spinner"></div>${loadingText}</div>`}
                ${error && html`
                    <div class="error-state">
                        <p>${errorText}</p>
                        <p class="error-detail">${error}</p>
                    </div>
                `}
                ${info && !dolphin && !z3ds && html`
                    <div class="info-grid">
                        <span class="info-label">File</span>
                        <span class="info-value">${filename}</span>

                        ${info.media_type && html`
                            <span class="info-label">Media Type</span>
                            <span class="info-value">${info.media_type.toUpperCase()}</span>
                        `}
                        ${info.game_id && html`
                            <span class="info-label">Game ID</span>
                            <span class="info-value">${info.game_id}</span>
                        `}
                        ${info.title && html`
                            <span class="info-label">Title</span>
                            <span class="info-value">${info.title}</span>
                        `}
                        ${info.file_version && html`
                            <span class="info-label">CHD Version</span>
                            <span class="info-value">${info.file_version}</span>
                        `}
                        ${info.logical_size && html`
                            <span class="info-label">Logical Size</span>
                            <span class="info-value">${info.logical_size}</span>
                        `}
                        ${info.chd_size && html`
                            <span class="info-label">Compressed Size</span>
                            <span class="info-value">${info.chd_size}</span>
                        `}
                        ${info.compression && html`
                            <span class="info-label">Compression</span>
                            <span class="info-value">${info.compression}</span>
                        `}
                        ${info.ratio && html`
                            <span class="info-label">Compression Ratio</span>
                            <span class="info-value">${info.ratio}</span>
                        `}
                        ${info.hunk_size && html`
                            <span class="info-label">Hunk Size</span>
                            <span class="info-value">${info.hunk_size}</span>
                        `}
                        ${info.total_hunks && html`
                            <span class="info-label">Total Hunks</span>
                            <span class="info-value">${info.total_hunks}</span>
                        `}
                        ${info.sha1 && html`
                            <span class="info-label">SHA1</span>
                            <span class="info-value" style="font-family: monospace; font-size: 0.75rem">${info.sha1}</span>
                        `}
                        ${info.data_sha1 && html`
                            <span class="info-label">Data SHA1</span>
                            <span class="info-value" style="font-family: monospace; font-size: 0.75rem">${info.data_sha1}</span>
                        `}
                    </div>
                    ${info.raw_data && html`
                        <details style="margin-top: 15px">
                            <summary style="cursor: pointer; color: var(--text-secondary)">Show Raw Output</summary>
                            <pre style="margin-top: 10px; font-size: 0.75rem; overflow-x: auto; padding: 10px; background: var(--bg-primary); border-radius: 4px; white-space: pre-wrap">${info.raw_data}</pre>
                        </details>
                    `}
                `}
                ${info && z3ds && html`
                    <div class="info-grid">
                        <span class="info-label">File</span>
                        <span class="info-value">${filename}</span>

                        ${info.format && html`
                            <span class="info-label">Format</span>
                            <span class="info-value">${info.format}</span>
                        `}
                        ${info.extension && html`
                            <span class="info-label">Extension</span>
                            <span class="info-value" style="font-family: monospace">${info.extension}</span>
                        `}
                        <span class="info-label">Compressed</span>
                        <span class="info-value">${info.compressed ? 'Yes' : 'No'}</span>

                        ${info.compression_type && html`
                            <span class="info-label">Compression</span>
                            <span class="info-value">${info.compression_type}</span>
                        `}
                        ${info.size_display && html`
                            <span class="info-label">File Size</span>
                            <span class="info-value">${info.size_display}</span>
                        `}
                        ${typeof info.size === 'number' && html`
                            <span class="info-label">Bytes</span>
                            <span class="info-value">${info.size.toLocaleString()}</span>
                        `}
                    </div>
                `}
                ${info && dolphin && html`
                    <div class="info-grid">
                        <span class="info-label">File</span>
                        <span class="info-value">${filename}</span>

                        ${info.game_name && html`
                            <span class="info-label">Game Name</span>
                            <span class="info-value">${info.game_name}</span>
                        `}
                        ${info.game_id && html`
                            <span class="info-label">Game ID</span>
                            <span class="info-value">${info.game_id}</span>
                        `}
                        ${info.title_id && html`
                            <span class="info-label">Title ID</span>
                            <span class="info-value" style="font-family: monospace">${info.title_id}</span>
                        `}
                        ${info.disc_number && html`
                            <span class="info-label">Disc Number</span>
                            <span class="info-value">${info.disc_number}</span>
                        `}
                        ${info.revision && html`
                            <span class="info-label">Revision</span>
                            <span class="info-value">${info.revision}</span>
                        `}
                        ${info.region && html`
                            <span class="info-label">Region</span>
                            <span class="info-value">${info.region}</span>
                        `}
                        ${info.format && html`
                            <span class="info-label">Format</span>
                            <span class="info-value">${info.format}</span>
                        `}
                        ${info.compression && html`
                            <span class="info-label">Compression</span>
                            <span class="info-value">${info.compression}</span>
                        `}
                        ${info.block_size && html`
                            <span class="info-label">Block Size</span>
                            <span class="info-value">${info.block_size}</span>
                        `}
                        ${info.file_size && html`
                            <span class="info-label">File Size</span>
                            <span class="info-value">${info.file_size}</span>
                        `}
                    </div>
                    ${info.raw_data && html`
                        <details style="margin-top: 15px">
                            <summary style="cursor: pointer; color: var(--text-secondary)">Show Raw Output</summary>
                            <pre style="margin-top: 10px; font-size: 0.75rem; overflow-x: auto; padding: 10px; background: var(--bg-primary); border-radius: 4px; white-space: pre-wrap">${info.raw_data}</pre>
                        </details>
                    `}
                `}
            </div>
        </div>
    `;
}

function DuplicateModal({ duplicates, onAction, onClose }) {
    if (!duplicates || duplicates.length === 0) return null;

    const existingCount = duplicates.filter(d => d.exists).length;

    return html`
        <div class="modal-overlay" onClick=${onClose}>
            <div class="modal" onClick=${(e) => e.stopPropagation()}>
                <div class="modal-header">
                    <h3>Duplicate Output Files Found</h3>
                    <button class="modal-close" onClick=${onClose} title="Close">×</button>
                </div>
                <div class="modal-body" style="padding: 15px;">
                    <p style="margin-bottom: 15px;">
                        <strong>${existingCount}</strong> of ${duplicates.length} selected file(s) already have output files.
                    </p>
                    <div style="max-height: 200px; overflow-y: auto; margin-bottom: 15px; padding: 10px; background: var(--bg-primary); border-radius: 4px;">
                        ${duplicates.filter(d => d.exists).map(d => html`
                            <div key=${d.file_path} style="margin-bottom: 8px; font-size: 0.85rem;">
                                <div style="color: var(--text-primary);">${d.file_path.split('/').pop()}</div>
                                <div style="color: var(--text-secondary); font-size: 0.75rem;">→ ${d.output_path.split('/').pop()}</div>
                            </div>
                        `)}
                    </div>
                    <p style="margin-bottom: 15px; color: var(--text-secondary);">How would you like to handle these duplicates?</p>
                    <div style="display: flex; flex-direction: column; gap: 10px;">
                        <button class="btn btn-primary" onClick=${() => onAction('rename')} style="width: 100%;">
                            Rename (create unique output files)
                        </button>
                        <button class="btn btn-secondary" onClick=${() => onAction('overwrite')} style="width: 100%;">
                            Overwrite existing files
                        </button>
                        <button class="btn btn-secondary" onClick=${() => onAction('skip')} style="width: 100%;">
                            Skip duplicates (convert only new files)
                        </button>
                        <button class="btn btn-secondary" onClick=${onClose} style="width: 100%;">
                            Cancel
                        </button>
                    </div>
                </div>
            </div>
        </div>
    `;
}

function CancelAllJobsModal({ total, queued, processing, onConfirm, onClose, busy }) {
    if (!total || total <= 0) return null;

    return html`
        <div class="modal-overlay" onClick=${busy ? null : onClose}>
            <div class="modal" onClick=${(e) => e.stopPropagation()} style="max-width: 460px;">
                <div class="modal-header">
                    <h3>Cancel All Active Jobs?</h3>
                    ${!busy && html`<button class="modal-close" onClick=${onClose} title="Close">×</button>`}
                </div>
                <div class="modal-body" style="padding: 15px;">
                    <p style="margin-bottom: 12px; color: var(--text-secondary);">
                        This will request cancellation for all active jobs.
                    </p>
                    <div style="margin-bottom: 15px; padding: 10px; background: var(--bg-primary); border-radius: 4px;">
                        <div><strong>Total:</strong> ${total}</div>
                        <div><strong>Queued:</strong> ${queued}</div>
                        <div><strong>Processing:</strong> ${processing}</div>
                    </div>
                    <div style="display: flex; gap: 10px; justify-content: flex-end;">
                        <button class="btn btn-secondary" onClick=${onClose} disabled=${busy}>
                            Keep Running
                        </button>
                        <button class="btn btn-primary" onClick=${onConfirm} disabled=${busy}>
                            ${busy ? 'Cancelling...' : 'Cancel All Jobs'}
                        </button>
                    </div>
                </div>
            </div>
        </div>
    `;
}

function ClearDoneModal({ total, onConfirm, onClose, busy }) {
    if (!total || total <= 0) return null;

    return html`
        <div class="modal-overlay" onClick=${busy ? null : onClose}>
            <div class="modal" onClick=${(e) => e.stopPropagation()} style="max-width: 440px;">
                <div class="modal-header">
                    <h3>Clear Completed Jobs?</h3>
                    ${!busy && html`<button class="modal-close" onClick=${onClose} title="Close">×</button>`}
                </div>
                <div class="modal-body" style="padding: 15px;">
                    <p style="margin-bottom: 12px; color: var(--text-secondary);">
                        This will remove ${total} completed/failed/cancelled job${total === 1 ? '' : 's'} from the list.
                    </p>
                    <div style="display: flex; gap: 10px; justify-content: flex-end;">
                        <button class="btn btn-secondary" onClick=${onClose} disabled=${busy}>
                            Keep History
                        </button>
                        <button class="btn btn-primary" onClick=${onConfirm} disabled=${busy}>
                            ${busy ? 'Clearing...' : 'Clear Done'}
                        </button>
                    </div>
                </div>
            </div>
        </div>
    `;
}

function DeletePlanModal({ plan, onConfirm, onClose, verificationLabel, title }) {
    if (!plan) return null;

    const items = Array.isArray(plan.items) ? plan.items : [];
    const hasIssues = items.some(item =>
        (item.errors && item.errors.length) ||
        (item.unsafe_paths && item.unsafe_paths.length) ||
        (item.missing_paths && item.missing_paths.length)
    );

    const getBaseName = (path) => (path || '').split('/').pop() || path;

    return html`
        <div class="modal-overlay" onClick=${onClose}>
            <div class="modal" onClick=${(e) => e.stopPropagation()} style="max-width: 540px;">
                <div class="modal-header">
                    <h3>${title || 'Confirm delete after verify'}</h3>
                    <button class="modal-close" onClick=${onClose} title="Close">×</button>
                </div>
                <div class="modal-body" style="padding: 15px;">
                    <p style="color: var(--text-secondary); margin-bottom: 12px;">
                        The files below will be deleted <strong>after</strong> a successful conversion${verificationLabel ? ` and ${verificationLabel} verification` : ''}.
                    </p>
                    <div style="max-height: 240px; overflow-y: auto; padding: 10px; background: var(--bg-primary); border-radius: 4px; margin-bottom: 15px;">
                        ${items.map(item => html`
                            <div style="margin-bottom: 12px;">
                                <div style="font-weight: 600; font-size: 0.85rem; color: var(--text-primary);">
                                    ${getBaseName(item.source_path)}
                                </div>
                                ${(item.delete_paths || []).map(p => html`
                                    <div style="font-size: 0.8rem; color: var(--text-secondary);">${p}</div>
                                `)}
                                ${item.warnings && item.warnings.length > 0 && html`
                                    <div style="font-size: 0.8rem; color: var(--warning); margin-top: 4px;">
                                        ${item.warnings.join('; ')}
                                    </div>
                                `}
                                ${item.missing_paths && item.missing_paths.length > 0 && html`
                                    <div style="font-size: 0.8rem; color: var(--warning); margin-top: 4px;">
                                        Missing: ${item.missing_paths.map(getBaseName).join(', ')}
                                    </div>
                                `}
                                ${item.unsafe_paths && item.unsafe_paths.length > 0 && html`
                                    <div style="font-size: 0.8rem; color: var(--error); margin-top: 4px;">
                                        Unsafe references: ${item.unsafe_paths.join('; ')}
                                    </div>
                                `}
                                ${item.errors && item.errors.length > 0 && html`
                                    <div style="font-size: 0.8rem; color: var(--error); margin-top: 4px;">
                                        ${item.errors.join('; ')}
                                    </div>
                                `}
                            </div>
                        `)}
                    </div>
                    ${hasIssues && html`
                        <p style="color: var(--error); margin-bottom: 12px; font-size: 0.85rem;">
                            Delete-on-verify is blocked due to missing or unsafe paths. Fix the sources or disable the option to continue.
                        </p>
                    `}
                    <div style="display: flex; gap: 10px; justify-content: flex-end;">
                        <button class="btn btn-secondary" onClick=${onClose}>
                            Cancel
                        </button>
                        <button class="btn btn-primary" onClick=${onConfirm} disabled=${hasIssues}>
                            Confirm compress + delete
                        </button>
                    </div>
                </div>
            </div>
        </div>
    `;
}

function RenameModal({ entry, onRename, onClose }) {
    const [newName, setNewName] = useState(entry?.name || '');
    const [loading, setLoading] = useState(false);
    const [error, setError] = useState(null);

    if (!entry) return null;

    const handleSubmit = async (e) => {
        e.preventDefault();
        if (!newName.trim() || newName === entry.name) return;

        setLoading(true);
        setError(null);
        try {
            await onRename(entry.path, newName.trim());
            onClose();
        } catch (err) {
            setError(err.message);
        } finally {
            setLoading(false);
        }
    };

    return html`
        <div class="modal-overlay" onClick=${onClose}>
            <div class="modal" onClick=${(e) => e.stopPropagation()}>
                <div class="modal-header">
                    <h3>Rename</h3>
                    <button class="modal-close" onClick=${onClose} title="Close">×</button>
                </div>
                <form onSubmit=${handleSubmit} class="modal-body" style="padding: 15px;">
                    <p style="margin-bottom: 10px; color: var(--text-secondary);">
                        Current name: <strong>${entry.name}</strong>
                    </p>
                    <input
                        type="text"
                        value=${newName}
                        onInput=${(e) => setNewName(e.target.value)}
                        placeholder="Enter new name"
                        style="width: 100%; padding: 10px; margin-bottom: 15px; border-radius: 4px; border: 1px solid var(--border); background: var(--bg-primary); color: var(--text-primary);"
                        autoFocus
                    />
                    ${error && html`
                        <p style="color: var(--error); margin-bottom: 15px; font-size: 0.85rem;">${error}</p>
                    `}
                    <div style="display: flex; gap: 10px; justify-content: flex-end;">
                        <button type="button" class="btn btn-secondary" onClick=${onClose} disabled=${loading}>
                            Cancel
                        </button>
                        <button
                            type="submit"
                            class="btn btn-primary"
                            disabled=${loading || !newName.trim() || newName === entry.name}
                        >
                            ${loading ? 'Renaming...' : 'Rename'}
                        </button>
                    </div>
                </form>
            </div>
        </div>
    `;
}

function DeleteModal({ entry, verifiedCHDs, verifyProgress, onDelete, onVerify, onClose, isoHandling }) {
    const [step, setStep] = useState(1); // 1 = initial, 2 = verification/confirm, 3 = final confirm
    const [verifying, setVerifying] = useState(false);
    const [verificationResult, setVerificationResult] = useState(null);
    const [deleting, setDeleting] = useState(false);
    const [error, setError] = useState(null);
    const [archiveScan, setArchiveScan] = useState({ loading: false, total: 0, chds: [], error: null });
    const [archiveVerify, setArchiveVerify] = useState({ running: false, total: 0, verified: 0, failed: 0, errors: [] });
    const [archiveVerifyAttempted, setArchiveVerifyAttempted] = useState(false);
    const [archiveVerifySkipped, setArchiveVerifySkipped] = useState(false);

    const entryPath = entry ? entry.path : '';
    const entryExt = entry ? entry.extension?.toLowerCase() : null;
    const isSourceFile = entry ? [
        '.iso', '.gdi', '.cue', '.bin', '.3ds', '.cci', '.cia',
    ].includes(entryExt) : false;
    const isArchive = entry ? entry.type === 'archive' : false;
    const fileTerm = getModeTerm(isoHandling, 'file');
    const resolveSourceProduct = (sourceEntry) => {
        if (!sourceEntry || !sourceEntry.path) return null;

        const getChdPath = () => (
            sourceEntry.has_chd ? sourceEntry.path.replace(/\.[^.]+$/, '.chd') : null
        );
        const getDolphinPath = () => (
            sourceEntry.dolphin_ready ? getDolphinProductPath(sourceEntry) : null
        );
        const getZ3dsPath = () => (
            sourceEntry.z3ds_ready ? (sourceEntry.z3ds_path || get3dsProductPath(sourceEntry.path)) : null
        );

        const preferredKinds = isoHandling === 'dolphin'
            ? ['dolphin', 'chd', 'z3ds']
            : isoHandling === 'z3ds'
                ? ['z3ds', 'chd', 'dolphin']
                : ['chd', 'dolphin', 'z3ds'];

        for (const kind of preferredKinds) {
            const productPath = kind === 'dolphin'
                ? getDolphinPath()
                : kind === 'z3ds'
                    ? getZ3dsPath()
                    : getChdPath();
            if (productPath) {
                return { path: productPath, kind };
            }
        }

        return null;
    };

    const resolvedProduct = (isSourceFile && entry && !isArchive) ? resolveSourceProduct(entry) : null;
    const productPath = resolvedProduct?.path || null;
    const termsIsoHandling = resolvedProduct?.kind === 'dolphin'
        ? 'dolphin'
        : resolvedProduct?.kind === 'z3ds'
            ? 'z3ds'
            : 'chdman';
    const verifyTerm = getModeTerm(termsIsoHandling, 'verification');
    const productTerm = getModeTerm(termsIsoHandling, 'product');

    const hasProduct = Boolean(productPath);
    const isAlreadyVerified = productPath && verifiedCHDs.has(productPath);
    const verifyStatus = productPath && verifyProgress ? verifyProgress.get(productPath) : null;
    const archiveVerifiedCount = archiveScan.chds.filter((path) => verifiedCHDs.has(path)).length;
    const archiveUnverified = archiveScan.chds.filter((path) => !verifiedCHDs.has(path));
    const archiveNeedsVerify = archiveUnverified.length > 0;

    useEffect(() => {
        if (!entryPath) return;
        setStep(1);
        setVerifying(false);
        setVerificationResult(null);
        setDeleting(false);
        setError(null);
        setArchiveVerifyAttempted(false);
        setArchiveVerifySkipped(false);
        setArchiveVerify({ running: false, total: 0, verified: 0, failed: 0, errors: [] });
        setArchiveScan({ loading: false, total: 0, chds: [], error: null });
    }, [entryPath]);

    useEffect(() => {
        if (!entryPath || !isArchive) return;
        let cancelled = false;
        setArchiveScan({ loading: true, total: 0, chds: [], error: null });
        api.listArchive(entryPath)
            .then((data) => {
                if (cancelled) return;
                const files = Array.isArray(data?.files) ? data.files : [];
                const chds = files
                    .filter((file) => file.has_chd && file.chd_path)
                    .map((file) => file.chd_path);
                setArchiveScan({
                    loading: false,
                    total: data?.total ?? files.length,
                    chds,
                    error: null
                });
            })
            .catch((err) => {
                if (cancelled) return;
                setArchiveScan({
                    loading: false,
                    total: 0,
                    chds: [],
                    error: err.message || 'Failed to scan archive'
                });
            });
        return () => {
            cancelled = true;
        };
    }, [entryPath, isArchive]);

    if (!entry) return null;

    const handleVerify = async () => {
        if (!productPath) return;
        setVerifying(true);
        setError(null);
        try {
            const result = await onVerify(productPath, entry);
            setVerificationResult(result);
            if (result.valid) {
                setStep(3);
            }
        } catch (err) {
            setError(err.message);
        } finally {
            setVerifying(false);
        }
    };

    const handleArchiveVerify = async () => {
        if (archiveScan.loading || archiveVerify.running) return;
        setArchiveVerifyAttempted(true);
        setArchiveVerifySkipped(false);
        const chdPaths = (archiveScan.chds || []).filter((path) => !verifiedCHDs.has(path));
        if (chdPaths.length === 0) {
            setArchiveVerify({
                running: false,
                total: 0,
                verified: archiveScan.chds.length,
                failed: 0,
                errors: []
            });
            setStep(3);
            return;
        }

        let verified = 0;
        let failed = 0;
        const errors = [];
        setArchiveVerify({ running: true, total: chdPaths.length, verified: 0, failed: 0, errors: [] });
        for (const path of chdPaths) {
            try {
                const result = await onVerify(path);
                if (result?.valid) {
                    verified += 1;
                } else {
                    failed += 1;
                    errors.push({ path, message: result?.message || 'Verification failed' });
                }
            } catch (err) {
                failed += 1;
                errors.push({ path, message: err.message || 'Verification failed' });
            }
            setArchiveVerify((prev) => ({
                ...prev,
                verified,
                failed
            }));
        }
        setArchiveVerify({ running: false, total: chdPaths.length, verified, failed, errors });
        setStep(3);
    };

    const handleDelete = async () => {
        setDeleting(true);
        setError(null);
        try {
            await onDelete(entry.path);
            onClose();
        } catch (err) {
            setError(err.message);
            setDeleting(false);
        }
    };

    return html`
        <div class="modal-overlay" onClick=${onClose}>
            <div class="modal" onClick=${(e) => e.stopPropagation()}>
                <div class="modal-header">
                    <h3 style="color: var(--error);">⚠️ Delete File</h3>
                    <button class="modal-close" onClick=${onClose} title="Close">×</button>
                </div>
                <div class="modal-body" style="padding: 15px;">
                    <p style="margin-bottom: 15px;">
                        Are you sure you want to delete: <br/>
                        <strong style="word-break: break-all;">${entry.name}</strong>
                    </p>

                    ${step === 1 && html`
                        ${isArchive && html`
                            <div style="background: var(--bg-tertiary); padding: 12px; border-radius: 4px; margin-bottom: 15px;">
                                ${archiveScan.loading && html`
                                    <p style="color: var(--text-secondary);">Scanning archive for images and CHDs...</p>
                                `}
                                ${!archiveScan.loading && !archiveScan.error && html`
                                    <p style="color: var(--text-primary); margin-bottom: 6px;">
                                        Found ${archiveScan.total} convertible image${archiveScan.total === 1 ? '' : 's'}.
                                    </p>
                                    <p style="color: var(--text-secondary);">
                                        CHD files detected: ${archiveScan.chds.length}${archiveScan.chds.length > 0 ? ` (${archiveVerifiedCount} verified)` : ''}
                                    </p>
                                    ${archiveScan.chds.length > 0 && !archiveNeedsVerify && html`
                                        <p style="color: var(--success);">✓ All CHDs already verified</p>
                                    `}
                                `}
                                ${archiveScan.error && html`
                                    <p style="color: var(--warning);">
                                        ⚠️ Could not scan archive contents: ${archiveScan.error}
                                    </p>
                                `}
                            </div>
                        `}
                        ${isSourceFile && hasProduct && !isArchive && html`
                            <div style="background: var(--bg-tertiary); padding: 12px; border-radius: 4px; margin-bottom: 15px;">
                                <p style="color: var(--success); margin-bottom: 8px;">✓ A ${productTerm} file exists for this source</p>
                                ${isAlreadyVerified ? html`
                                    <p style="color: var(--success);">✓ ${productTerm} has been verified</p>
                                ` : html`
                                    <p style="color: var(--warning);">
                                        ⚠️ ${productTerm} has not been verified. We recommend verifying before deleting the source.
                                    </p>
                                `}
                            </div>
                        `}
                        ${isSourceFile && !hasProduct && !isArchive && html`
                            <div style="background: var(--bg-tertiary); padding: 12px; border-radius: 4px; margin-bottom: 15px; border: 1px solid var(--error);">
                                <p style="color: var(--error);">
                                    ⚠️ <strong>WARNING:</strong> No ${productTerm} file exists for this source file. Deleting it will result in data loss!
                                </p>
                            </div>
                        `}
                        <p style="color: var(--text-secondary); margin-bottom: 15px;">This action cannot be undone.</p>
                        ${error && html`
                            <p style="color: var(--error); margin-bottom: 15px; font-size: 0.85rem;">${error}</p>
                        `}
                        <div style="display: flex; flex-direction: column; gap: 10px;">
                            ${isArchive && html`
                                ${archiveVerify.running && html`
                                    <div style="color: var(--text-secondary); font-size: 0.85rem;">
                                        Verifying CHDs... ${archiveVerify.verified + archiveVerify.failed}/${archiveVerify.total}
                                    </div>
                                `}
                                ${archiveNeedsVerify && html`
                                    <button class="btn btn-primary" onClick=${handleArchiveVerify} disabled=${archiveScan.loading || archiveVerify.running}>
                                        ${archiveVerify.running ? 'Verifying CHDs...' : '🔍 Verify CHDs First'}
                                    </button>
                                `}
                                <button
                                    class="btn btn-secondary"
                                    onClick=${() => { setArchiveVerifySkipped(archiveNeedsVerify); setStep(3); }}
                                    disabled=${archiveScan.loading || archiveVerify.running}
                                >
                                    ${archiveNeedsVerify ? 'Skip Verification' : 'Continue to Delete'}
                                </button>
                            `}
                            ${isSourceFile && hasProduct && !isAlreadyVerified && !isArchive && html`
                                <button class="btn btn-primary" onClick=${handleVerify} disabled=${verifying}>
                                    ${verifying ? `Verifying ${verifyTerm}...` : `🔍 Verify ${verifyTerm} First`}
                                </button>
                            `}
                            ${verifying && verifyStatus && !isArchive && html`
                                <div style="color: var(--text-secondary); font-size: 0.85rem;">
                                    ${verifyStatus.progress != null ? `Progress: ${verifyStatus.progress}%` : (verifyStatus.message || 'Verifying...')}
                                </div>
                            `}
                            ${!isArchive && html`
                                <button
                                    class="btn btn-secondary"
                                    onClick=${() => setStep(isSourceFile && hasProduct && isAlreadyVerified ? 3 : 2)}
                                >
                                    ${isAlreadyVerified ? 'Continue to Delete' : 'Skip Verification'}
                                </button>
                            `}
                            <button class="btn btn-secondary" onClick=${onClose}>Cancel</button>
                        </div>
                    `}

                    ${step === 2 && !isArchive && html`
                        <div style="background: var(--bg-tertiary); padding: 12px; border-radius: 4px; margin-bottom: 15px; border: 1px solid var(--warning);">
                            <p style="color: var(--warning);">
                                ⚠️ You're about to delete a file without ${verifyTerm} verification.
                            </p>
                        </div>
                        ${verificationResult && !verificationResult.valid && html`
                            <div style="background: var(--bg-tertiary); padding: 12px; border-radius: 4px; margin-bottom: 15px; border: 1px solid var(--error);">
                                <p style="color: var(--error);">
                                    ❌ ${verifyTerm} verification failed: ${verificationResult.message}
                                </p>
                            </div>
                        `}
                        <p style="color: var(--text-secondary); margin-bottom: 15px;">
                            Are you <strong>absolutely sure</strong> you want to proceed?
                        </p>
                        ${error && html`
                            <p style="color: var(--error); margin-bottom: 15px; font-size: 0.85rem;">${error}</p>
                        `}
                        <div style="display: flex; flex-direction: column; gap: 10px;">
                            <button class="btn btn-secondary" style="background: var(--error);" onClick=${handleDelete} disabled=${deleting}>
                                ${deleting ? 'Deleting...' : 'Yes, Delete Anyway'}
                            </button>
                            <button class="btn btn-secondary" onClick=${onClose}>Cancel</button>
                        </div>
                    `}

                    ${step === 3 && html`
                        ${isArchive && html`
                            ${archiveVerifyAttempted && archiveVerify.failed === 0 && archiveVerify.total > 0 && html`
                                <div style="background: var(--bg-tertiary); padding: 12px; border-radius: 4px; margin-bottom: 15px; border: 1px solid var(--success);">
                                    <p style="color: var(--success);">
                                        ✓ Verified ${archiveVerify.verified} CHD${archiveVerify.verified === 1 ? '' : 's'} successfully.
                                    </p>
                                </div>
                            `}
                            ${!archiveVerifyAttempted && archiveScan.chds.length > 0 && !archiveNeedsVerify && html`
                                <div style="background: var(--bg-tertiary); padding: 12px; border-radius: 4px; margin-bottom: 15px; border: 1px solid var(--success);">
                                    <p style="color: var(--success);">
                                        ✓ All CHDs already verified.
                                    </p>
                                </div>
                            `}
                            ${(archiveVerify.failed > 0 || archiveVerifySkipped || archiveScan.error) && html`
                                <div style="background: var(--bg-tertiary); padding: 12px; border-radius: 4px; margin-bottom: 15px; border: 1px solid var(--warning);">
                                    <p style="color: var(--warning); margin-bottom: 6px;">
                                        ⚠️ Some CHDs were not verified.
                                    </p>
                                    ${archiveVerify.failed > 0 && html`
                                        <p style="color: var(--warning);">Failed verifications: ${archiveVerify.failed}</p>
                                    `}
                                    ${archiveVerifySkipped && html`
                                        <p style="color: var(--warning);">Verification was skipped.</p>
                                    `}
                                    ${archiveScan.error && html`
                                        <p style="color: var(--warning);">Archive scan failed, CHDs may be missing.</p>
                                    `}
                                </div>
                            `}
                            ${!archiveScan.error && html`
                                <p style="color: var(--text-secondary); margin-bottom: 15px;">
                                    Confirm deletion of the archive file?
                                </p>
                            `}
                            ${archiveScan.error && html`
                                <p style="color: var(--text-secondary); margin-bottom: 15px;">
                                    Archive contents could not be scanned. Delete anyway?
                                </p>
                            `}
                        `}
                        ${!isArchive && html`
                            ${verificationResult && verificationResult.valid && html`
                                <div style="background: var(--bg-tertiary); padding: 12px; border-radius: 4px; margin-bottom: 15px; border: 1px solid var(--success);">
                                    <p style="color: var(--success);">
                                        ✓ ${productTerm} verified successfully! Safe to delete source file.
                                    </p>
                                </div>
                            `}
                            ${isAlreadyVerified && !verificationResult && html`
                                <div style="background: var(--bg-tertiary); padding: 12px; border-radius: 4px; margin-bottom: 15px; border: 1px solid var(--success);">
                                    <p style="color: var(--success);">
                                        ✓ ${productTerm} was previously verified. Safe to delete source file.
                                    </p>
                                </div>
                            `}
                            <p style="color: var(--text-secondary); margin-bottom: 15px;">
                                Confirm deletion of the source file?
                            </p>
                        `}
                        ${error && html`
                            <p style="color: var(--error); margin-bottom: 15px; font-size: 0.85rem;">${error}</p>
                        `}
                        <div style="display: flex; flex-direction: column; gap: 10px;">
                            <button class="btn btn-primary" onClick=${handleDelete} disabled=${deleting}>
                                ${deleting ? 'Deleting...' : isArchive ? 'Delete Archive' : `Delete ${fileTerm}`}
                            </button>
                            <button class="btn btn-secondary" onClick=${onClose}>Cancel</button>
                        </div>
                    `}
                </div>
            </div>
        </div>
    `;
}

function BulkDeleteModal({ entries, verifiedCHDs, onDelete, onVerify, onClose, onRefresh, isoHandling }) {
    const [step, setStep] = useState(1); // 1 = review, 2 = verifying, 3 = confirm
    const [deleting, setDeleting] = useState(false);
    const [error, setError] = useState(null);
    const [result, setResult] = useState(null);
    const [verifyState, setVerifyState] = useState({ running: false, total: 0, verified: 0, failed: 0, current: null });
    const [skipVerification, setSkipVerification] = useState(false);

    // Reset state when entries change
    const entriesKey = entries ? entries.map(e => e.path).join('|') : '';
    useEffect(() => {
        if (!entriesKey) return;
        setStep(1);
        setDeleting(false);
        setError(null);
        setResult(null);
        setVerifyState({ running: false, total: 0, verified: 0, failed: 0, current: null });
        setSkipVerification(false);
    }, [entriesKey]);

    if (!entries || entries.length === 0) return null;

    const fileTerm = getModeTerm(isoHandling, 'file');
    const verifyTerm = getModeTerm(isoHandling, 'verification');
    const productTerm = getModeTerm(isoHandling, 'product');
    const resolveSourceProduct = (entry) => {
        if (!entry || !entry.path) return null;

        const getChdPath = () => (
            entry.has_chd ? entry.path.replace(/\.[^.]+$/, '.chd') : null
        );
        const getDolphinPath = () => (
            entry.dolphin_ready ? getDolphinProductPath(entry) : null
        );
        const getZ3dsPath = () => (
            entry.z3ds_ready ? (entry.z3ds_path || get3dsProductPath(entry.path)) : null
        );

        const preferredKinds = isoHandling === 'dolphin'
            ? ['dolphin', 'chd', 'z3ds']
            : isoHandling === 'z3ds'
                ? ['z3ds', 'chd', 'dolphin']
                : ['chd', 'dolphin', 'z3ds'];

        for (const kind of preferredKinds) {
            const productPath = kind === 'dolphin'
                ? getDolphinPath()
                : kind === 'z3ds'
                    ? getZ3dsPath()
                    : getChdPath();
            if (productPath) {
                return { path: productPath, kind };
            }
        }

        return null;
    };

    // Categorize files
    const sourceFiles = entries.filter(e =>
        ['.iso', '.gdi', '.cue', '.bin', '.3ds', '.cci', '.cia'].includes(e.extension?.toLowerCase())
    );
    const chdFiles = entries.filter(e => e.extension?.toLowerCase() === '.chd');
    const dolphinFiles = entries.filter(e => ['.rvz', '.wia', '.gcz', '.wbfs'].includes(e.extension?.toLowerCase()));
    const z3dsFiles = entries.filter(e => ['.3ds', '.cci', '.cia'].includes(e.extension?.toLowerCase()));
    const archives = entries.filter(e => e.type === 'archive');
    const otherFiles = entries.filter(e =>
        !sourceFiles.includes(e) && !chdFiles.includes(e) && !dolphinFiles.includes(e) && !z3dsFiles.includes(e) && !archives.includes(e)
    );
    const sourceProductByPath = new Map();
    for (const entry of sourceFiles) {
        const product = resolveSourceProduct(entry);
        if (product) {
            sourceProductByPath.set(entry.path, product);
        }
    }

    // Check verification status for source files
    const sourceFilesWithProduct = sourceFiles.filter(e => sourceProductByPath.has(e.path));
    const unverifiedSourceFiles = sourceFilesWithProduct.filter(e => {
        const product = sourceProductByPath.get(e.path);
        return product && !verifiedCHDs.has(product.path);
    });
    const sourceFilesWithoutProduct = sourceFiles.filter(e => !sourceProductByPath.has(e.path));
    const hasUnverifiedProducts = unverifiedSourceFiles.length > 0;
    const hasDangerousDeletes = sourceFilesWithoutProduct.length > 0;

    const handleVerifyAll = async () => {
        const itemsToVerify = unverifiedSourceFiles.map(e => {
            const product = sourceProductByPath.get(e.path);
            return product ? { path: product.path, filename: e.name, kind: product.kind } : null;
        }).filter(Boolean);

        if (itemsToVerify.length === 0) {
            setStep(3);
            return;
        }

        setStep(2);
        setVerifyState({ running: true, total: itemsToVerify.length, verified: 0, failed: 0, current: null });

        try {
            // Separate by kind for batch verification
            const chdPaths = itemsToVerify.filter(item => item.kind === 'chd').map(item => item.path);
            const dolphinPaths = itemsToVerify.filter(item => item.kind === 'dolphin').map(item => item.path);
            const z3dsPaths = itemsToVerify.filter(item => item.kind === 'z3ds').map(item => item.path);

            let currentVerified = 0;
            let currentFailed = 0;

            if (chdPaths.length > 0) {
                let chdStartHandled = false;
                await api.verifyBatchCHDs(chdPaths, {
                    onProgress: (update) => {
                        if (update.type === 'start') {
                            if (!chdStartHandled && Number.isFinite(update.total)) {
                                chdStartHandled = true;
                                setVerifyState(prev => ({
                                    ...prev,
                                    total: prev.total - chdPaths.length + update.total
                                }));
                            }
                        } else if (update.type === 'progress' || update.type === 'file_progress') {
                            setVerifyState(prev => ({ ...prev, current: update.filename || update.path }));
                        }
                    },
                    onFileComplete: (data) => {
                        currentVerified += data.valid ? 1 : 0;
                        currentFailed += data.valid ? 0 : 1;
                        setVerifyState(prev => ({
                            ...prev,
                            verified: currentVerified,
                            failed: currentFailed,
                            current: null
                        }));
                        if (data.valid && onVerify) {
                            onVerify(data.path);
                        }
                    }
                });
            }

            if (dolphinPaths.length > 0) {
                let dolphinStartHandled = false;
                await api.verifyBatchDolphin(dolphinPaths, {
                    onProgress: (update) => {
                        if (update.type === 'start') {
                            if (!dolphinStartHandled && Number.isFinite(update.total)) {
                                dolphinStartHandled = true;
                                setVerifyState(prev => ({
                                    ...prev,
                                    total: prev.total - dolphinPaths.length + update.total
                                }));
                            }
                        } else if (update.type === 'progress' || update.type === 'file_progress') {
                            setVerifyState(prev => ({ ...prev, current: update.filename || update.path }));
                        }
                    },
                    onFileComplete: (data) => {
                        currentVerified += data.valid ? 1 : 0;
                        currentFailed += data.valid ? 0 : 1;
                        setVerifyState(prev => ({
                            ...prev,
                            verified: currentVerified,
                            failed: currentFailed,
                            current: null
                        }));
                        if (data.valid && onVerify) {
                            onVerify(data.path);
                        }
                    }
                });
            }

            if (z3dsPaths.length > 0) {
                let z3dsStartHandled = false;
                await api.verifyBatchZ3DS(z3dsPaths, {
                    onProgress: (update) => {
                        if (update.type === 'start') {
                            if (!z3dsStartHandled && Number.isFinite(update.total)) {
                                z3dsStartHandled = true;
                                setVerifyState(prev => ({
                                    ...prev,
                                    total: prev.total - z3dsPaths.length + update.total
                                }));
                            }
                        } else if (update.type === 'progress' || update.type === 'file_progress') {
                            setVerifyState(prev => ({ ...prev, current: update.filename || update.path }));
                        }
                    },
                    onFileComplete: (data) => {
                        currentVerified += data.valid ? 1 : 0;
                        currentFailed += data.valid ? 0 : 1;
                        setVerifyState(prev => ({
                            ...prev,
                            verified: currentVerified,
                            failed: currentFailed,
                            current: null
                        }));
                        if (data.valid && onVerify) {
                            onVerify(data.path);
                        }
                    }
                });
            }

        } catch (err) {
            setError(`Verification failed: ${err.message}`);
        } finally {
            setVerifyState(prev => ({ ...prev, running: false, current: null }));
            setStep(3);
        }
    };

    const handleDelete = async () => {
        setDeleting(true);
        setError(null);
        try {
            const paths = entries.map(e => e.path);
            const deleteResult = await api.deleteBatch(paths);
            setResult(deleteResult);
            if (deleteResult.success > 0 && onRefresh) {
                onRefresh();
            }
            if (deleteResult.failed === 0) {
                onClose();
            }
        } catch (err) {
            setError(err.message);
        } finally {
            setDeleting(false);
        }
    };

    return html`
        <div class="modal-overlay" onClick=${onClose}>
            <div class="modal" onClick=${(e) => e.stopPropagation()} style="max-width: 500px;">
                <div class="modal-header">
                    <h3 style="color: var(--error);">⚠️ Delete ${entries.length} ${fileTerm}${entries.length > 1 ? 's' : ''}</h3>
                    <button class="modal-close" onClick=${onClose} title="Close">×</button>
                </div>
                <div class="modal-body" style="padding: 15px;">
                    ${step === 1 && html`
                        <div style="max-height: 200px; overflow-y: auto; margin-bottom: 15px; padding: 10px; background: var(--bg-primary); border-radius: 4px;">
                            ${sourceFilesWithProduct.length > 0 && html`
                                <div style="margin-bottom: 10px;">
                                    <strong style="color: var(--text-primary);">Source files with ${productTerm} (${sourceFilesWithProduct.length}):</strong>
                                    ${sourceFilesWithProduct.map(e => {
        const product = sourceProductByPath.get(e.path);
        const isVerified = product && verifiedCHDs.has(product.path);
        return html`
                                            <div key=${e.path} style="font-size: 0.85rem; padding: 2px 0; color: ${isVerified ? 'var(--success)' : 'var(--warning)'};">
                                                ${isVerified ? '✓' : '⚠'} ${e.name}
                                            </div>
                                        `;
    })}
                                </div>
                            `}
                            ${sourceFilesWithoutProduct.length > 0 && html`
                                <div style="margin-bottom: 10px;">
                                    <strong style="color: var(--error);">Source files WITHOUT ${productTerm} (${sourceFilesWithoutProduct.length}):</strong>
                                    ${sourceFilesWithoutProduct.map(e => html`
                                        <div key=${e.path} style="font-size: 0.85rem; padding: 2px 0; color: var(--error);">
                                            ❌ ${e.name}
                                        </div>
                                    `)}
                                </div>
                            `}
                            ${chdFiles.length > 0 && html`
                                <div style="margin-bottom: 10px;">
                                    <strong style="color: var(--text-primary);">CHD files (${chdFiles.length}):</strong>
                                    ${chdFiles.map(e => html`
                                        <div key=${e.path} style="font-size: 0.85rem; padding: 2px 0;">💿 ${e.name}</div>
                                    `)}
                                </div>
                            `}
                            ${dolphinFiles.length > 0 && html`
                                <div style="margin-bottom: 10px;">
                                    <strong style="color: var(--text-primary);">Dolphin files (${dolphinFiles.length}):</strong>
                                    ${dolphinFiles.map(e => html`
                                        <div key=${e.path} style="font-size: 0.85rem; padding: 2px 0;">🐬 ${e.name}</div>
                                    `)}
                                </div>
                            `}
                            ${z3dsFiles.length > 0 && html`
                                <div style="margin-bottom: 10px;">
                                    <strong style="color: var(--text-primary);">3DS files (${z3dsFiles.length}):</strong>
                                    ${z3dsFiles.map(e => html`
                                        <div key=${e.path} style="font-size: 0.85rem; padding: 2px 0;">🎮 ${e.name}</div>
                                    `)}
                                </div>
                            `}
                            ${archives.length > 0 && html`
                                <div style="margin-bottom: 10px;">
                                    <strong style="color: var(--text-primary);">Archives (${archives.length}):</strong>
                                    ${archives.map(e => html`
                                        <div key=${e.path} style="font-size: 0.85rem; padding: 2px 0;">📦 ${e.name}</div>
                                    `)}
                                </div>
                            `}
                            ${otherFiles.length > 0 && html`
                                <div>
                                    <strong style="color: var(--text-primary);">Other files (${otherFiles.length}):</strong>
                                    ${otherFiles.map(e => html`
                                        <div key=${e.path} style="font-size: 0.85rem; padding: 2px 0;">📄 ${e.name}</div>
                                    `)}
                                </div>
                            `}
                        </div>

                        ${hasDangerousDeletes && html`
                            <div style="background: var(--bg-tertiary); padding: 12px; border-radius: 4px; margin-bottom: 15px; border: 1px solid var(--error);">
                                <p style="color: var(--error); margin: 0;">
                                    ⚠️ <strong>WARNING:</strong> ${sourceFilesWithoutProduct.length} source file${sourceFilesWithoutProduct.length > 1 ? 's have' : ' has'} no ${productTerm} backup. Deleting will result in data loss!
                                </p>
                            </div>
                        `}

                        ${hasUnverifiedProducts && !hasDangerousDeletes && html`
                            <div style="background: var(--bg-tertiary); padding: 12px; border-radius: 4px; margin-bottom: 15px; border: 1px solid var(--warning);">
                                <p style="color: var(--warning); margin: 0;">
                                    ⚠️ ${unverifiedSourceFiles.length} source file${unverifiedSourceFiles.length > 1 ? 's have' : ' has'} unverified ${productTerm}${unverifiedSourceFiles.length > 1 ? 's' : ''}. We recommend verifying before deletion.
                                </p>
                            </div>
                        `}

                        <p style="color: var(--text-secondary); margin-bottom: 15px;">This action cannot be undone.</p>

                        ${error && html`
                            <p style="color: var(--error); margin-bottom: 15px; font-size: 0.85rem;">${error}</p>
                        `}

                        <div style="display: flex; flex-direction: column; gap: 10px;">
                            ${hasUnverifiedProducts && html`
                                <button class="btn btn-primary" onClick=${handleVerifyAll}>
                                    🔍 Verify All ${productTerm}s First (${unverifiedSourceFiles.length})
                                </button>
                            `}
                            <button
                                class="btn ${hasUnverifiedProducts || hasDangerousDeletes ? 'btn-secondary' : 'btn-primary'}"
                                style="${hasDangerousDeletes ? 'background: var(--error);' : ''}"
                                onClick=${() => { setSkipVerification(hasUnverifiedProducts); setStep(3); }}
                            >
                                ${hasDangerousDeletes ? 'Delete Anyway (Data Loss!)' : hasUnverifiedProducts ? 'Skip Verification' : 'Continue to Delete'}
                            </button>
                            <button class="btn btn-secondary" onClick=${onClose}>Cancel</button>
                        </div>
                    `}

                    ${step === 2 && html`
                        <div style="text-align: center; padding: 20px;">
                            <div class="spinner" style="margin: 0 auto 15px;"></div>
                            <p style="color: var(--text-primary); margin-bottom: 10px;">
                                Verifying ${productTerm} files... ${verifyState.verified + verifyState.failed}/${verifyState.total}
                            </p>
                            ${verifyState.current && html`
                                <p style="color: var(--text-secondary); font-size: 0.85rem;">${verifyState.current}</p>
                            `}
                            <div style="margin-top: 15px; font-size: 0.85rem;">
                                <span style="color: var(--success);">✓ ${verifyState.verified} verified</span>
                                ${verifyState.failed > 0 && html`
                                    <span style="color: var(--error); margin-left: 15px;">✗ ${verifyState.failed} failed</span>
                                `}
                            </div>
                        </div>
                    `}

                    ${step === 3 && html`
                        ${verifyState.total > 0 && html`
                            <div style="background: var(--bg-tertiary); padding: 12px; border-radius: 4px; margin-bottom: 15px; border: 1px solid ${verifyState.failed > 0 ? 'var(--warning)' : 'var(--success)'};">
                                <p style="color: ${verifyState.failed > 0 ? 'var(--warning)' : 'var(--success)'}; margin: 0;">
                                    ${verifyState.failed > 0
                    ? `⚠️ Verification complete: ${verifyState.verified} passed, ${verifyState.failed} failed`
                    : `✓ All ${verifyState.verified} ${productTerm}${verifyState.verified > 1 ? 's' : ''} verified successfully`
                }
                                </p>
                            </div>
                        `}

                        ${skipVerification && html`
                            <div style="background: var(--bg-tertiary); padding: 12px; border-radius: 4px; margin-bottom: 15px; border: 1px solid var(--warning);">
                                <p style="color: var(--warning); margin: 0;">
                                    ⚠️ Proceeding without ${verifyTerm} verification.
                                </p>
                            </div>
                        `}

                        ${result && html`
                            <div style="background: var(--bg-tertiary); padding: 12px; border-radius: 4px; margin-bottom: 15px; border: 1px solid ${result.failed > 0 ? 'var(--warning)' : 'var(--success)'};">
                                <p style="color: ${result.failed > 0 ? 'var(--warning)' : 'var(--success)'}; margin: 0;">
                                    ${result.failed > 0
                    ? `Deleted ${result.success} file${result.success !== 1 ? 's' : ''}, ${result.failed} failed`
                    : `✓ Successfully deleted ${result.success} file${result.success !== 1 ? 's' : ''}`
                }
                                </p>
                                ${result.failed > 0 && result.results && html`
                                    <div style="margin-top: 10px; font-size: 0.85rem;">
                                        ${result.results.filter(r => !r.success).map(r => html`
                                            <div key=${r.path} style="color: var(--error);">
                                                ✗ ${r.path.split('/').pop()}: ${r.error}
                                            </div>
                                        `)}
                                    </div>
                                `}
                            </div>
                        `}

                        ${!result && html`
                            <p style="color: var(--text-secondary); margin-bottom: 15px;">
                                Confirm deletion of ${entries.length} ${fileTerm}${entries.length > 1 ? 's' : ''}?
                            </p>
                        `}

                        ${error && html`
                            <p style="color: var(--error); margin-bottom: 15px; font-size: 0.85rem;">${error}</p>
                        `}

                        <div style="display: flex; flex-direction: column; gap: 10px;">
                            ${!result && html`
                                <button
                                    class="btn btn-primary"
                                    onClick=${handleDelete}
                                    disabled=${deleting}
                                    style="${hasDangerousDeletes ? 'background: var(--error);' : ''}"
                                >
                                    ${deleting ? 'Deleting...' : `Delete ${entries.length} ${fileTerm}${entries.length > 1 ? 's' : ''}`}
                                </button>
                            `}
                            <button class="btn btn-secondary" onClick=${onClose}>
                                ${result ? 'Close' : 'Cancel'}
                            </button>
                        </div>
                    `}
                </div>
            </div>
        </div>
    `;
}

function BulkVerifyModal({ verifyItems, onComplete, onClose }) {
    const [state, setState] = useState({
        running: false,
        total: 0,
        verified: 0,
        failed: 0,
        current: null,
        currentProgress: null,
        results: [],
        error: null,
        complete: false
    });

    // Reset and start verification when paths change
    const pathsKey = verifyItems ? verifyItems.map(item => item.path).join('|') : '';
    useEffect(() => {
        if (!pathsKey || verifyItems.length === 0) return;

        let cancelled = false;

        const runVerification = async () => {
            setState({
                running: true,
                total: verifyItems.length,
                verified: 0,
                failed: 0,
                current: null,
                currentProgress: null,
                results: [],
                error: null,
                complete: false
            });

            try {
                const allChd = verifyItems.every(item => item.kind === 'chd');
                const allDolphin = verifyItems.every(item => item.kind === 'dolphin');
                const allZ3DS = verifyItems.every(item => item.kind === 'z3ds');
                let result = { verified: 0, failed: 0, total: verifyItems.length };

                if (allChd) {
                    const chdPaths = verifyItems.map(item => item.path);
                    result = await api.verifyBatchCHDs(chdPaths, {
                        onProgress: (update) => {
                            if (cancelled) return;
                            if (update.type === 'start') {
                                // Use server-validated total (may be less than client count if paths were filtered)
                                setState(prev => ({
                                    ...prev,
                                    total: update.total
                                }));
                            } else if (update.type === 'progress') {
                                setState(prev => ({
                                    ...prev,
                                    current: update.filename
                                }));
                            } else if (update.type === 'file_progress') {
                                setState(prev => ({
                                    ...prev,
                                    current: update.filename,
                                    currentProgress: update.progress
                                }));
                            }
                        },
                        onFileComplete: (data) => {
                            if (cancelled) return;
                            setState(prev => ({
                                ...prev,
                                verified: data.verified,
                                failed: data.failed,
                                current: null,
                                currentProgress: null,
                                results: [...prev.results, data]
                            }));
                        }
                    });
                } else if (allDolphin) {
                    const dolphinPaths = verifyItems.map(item => item.path);
                    result = await api.verifyBatchDolphin(dolphinPaths, {
                        onProgress: (update) => {
                            if (cancelled) return;
                            if (update.type === 'start') {
                                // Use server-validated total (may be less than client count if paths were filtered)
                                setState(prev => ({
                                    ...prev,
                                    total: update.total
                                }));
                            } else if (update.type === 'progress' || update.type === 'file_progress') {
                                setState(prev => ({
                                    ...prev,
                                    current: update.filename || update.path
                                }));
                            }
                        },
                        onFileComplete: (data) => {
                            if (cancelled) return;
                            setState(prev => ({
                                ...prev,
                                verified: data.verified,
                                failed: data.failed,
                                current: null,
                                currentProgress: null,
                                results: [...prev.results, data]
                            }));
                        }
                    });
                } else if (allZ3DS) {
                    const z3dsPaths = verifyItems.map(item => item.path);
                    result = await api.verifyBatchZ3DS(z3dsPaths, {
                        onProgress: (update) => {
                            if (cancelled) return;
                            if (update.type === 'start') {
                                setState(prev => ({
                                    ...prev,
                                    total: update.total
                                }));
                            } else if (update.type === 'progress' || update.type === 'file_progress') {
                                setState(prev => ({
                                    ...prev,
                                    current: update.filename || update.path,
                                    currentProgress: update.progress
                                }));
                            }
                        },
                        onFileComplete: (data) => {
                            if (cancelled) return;
                            setState(prev => ({
                                ...prev,
                                verified: data.verified,
                                failed: data.failed,
                                current: null,
                                currentProgress: null,
                                results: [...prev.results, data]
                            }));
                        }
                    });
                } else {
                    const chdPaths = verifyItems.filter(item => item.kind === 'chd').map(item => item.path);
                    const dolphinPaths = verifyItems.filter(item => item.kind === 'dolphin').map(item => item.path);
                    const z3dsPaths = verifyItems.filter(item => item.kind === 'z3ds').map(item => item.path);

                    let verified = 0;
                    let failed = 0;
                    let chdTotal = chdPaths.length;
                    let dolphinTotal = dolphinPaths.length;
                    let z3dsTotal = z3dsPaths.length;

                    const runBatch = async (kind, paths) => {
                        const baseVerified = verified;
                        const baseFailed = failed;

                        let verifyFn;
                        if (kind === 'dolphin') verifyFn = api.verifyBatchDolphin.bind(api);
                        else if (kind === 'z3ds') verifyFn = api.verifyBatchZ3DS.bind(api);
                        else verifyFn = api.verifyBatchCHDs.bind(api);

                        const batchResult = await verifyFn(paths, {
                            onProgress: (update) => {
                                if (cancelled) return;
                                if (update.type === 'start') {
                                    if (kind === 'dolphin') {
                                        dolphinTotal = update.total;
                                    } else if (kind === 'z3ds') {
                                        z3dsTotal = update.total;
                                    } else {
                                        chdTotal = update.total;
                                    }
                                    setState(prev => ({
                                        ...prev,
                                        total: chdTotal + dolphinTotal + z3dsTotal
                                    }));
                                } else if (update.type === 'progress') {
                                    setState(prev => ({
                                        ...prev,
                                        current: update.filename
                                    }));
                                } else if (update.type === 'file_progress') {
                                    setState(prev => ({
                                        ...prev,
                                        current: update.filename,
                                        currentProgress: update.progress
                                    }));
                                }
                            },
                            onFileComplete: (data) => {
                                if (cancelled) return;
                                const cumulativeVerified = baseVerified + data.verified;
                                const cumulativeFailed = baseFailed + data.failed;
                                verified = cumulativeVerified;
                                failed = cumulativeFailed;
                                setState(prev => ({
                                    ...prev,
                                    verified: cumulativeVerified,
                                    failed: cumulativeFailed,
                                    current: null,
                                    currentProgress: null,
                                    results: [...prev.results, data]
                                }));
                            }
                        });

                        verified = baseVerified + batchResult.verified;
                        failed = baseFailed + batchResult.failed;
                        setState(prev => ({
                            ...prev,
                            verified,
                            failed,
                            current: null,
                            currentProgress: null
                        }));
                    };

                    if (chdPaths.length > 0) {
                        await runBatch('chd', chdPaths);
                    }
                    if (dolphinPaths.length > 0) {
                        await runBatch('dolphin', dolphinPaths);
                    }
                    if (z3dsPaths.length > 0) {
                        await runBatch('z3ds', z3dsPaths);
                    }

                    result = { verified, failed, total: chdTotal + dolphinTotal + z3dsTotal };
                }

                if (cancelled) return;
                setState(prev => ({
                    ...prev,
                    running: false,
                    complete: true,
                    verified: result.verified,
                    failed: result.failed
                }));

                if (onComplete) {
                    onComplete(result);
                }
            } catch (err) {
                if (cancelled) return;
                setState(prev => ({
                    ...prev,
                    running: false,
                    error: err.message
                }));
            }
        };

        runVerification();

        return () => {
            cancelled = true;
        };
    }, [pathsKey]);

    if (!verifyItems || verifyItems.length === 0) return null;

    return html`
        <div class="modal-overlay" onClick=${state.running ? null : onClose}>
            <div class="modal" onClick=${(e) => e.stopPropagation()} style="max-width: 500px;">
                <div class="modal-header">
                    <h3>🔍 Verify ${verifyItems.length} File${verifyItems.length > 1 ? 's' : ''}</h3>
                    ${!state.running && html`
                        <button class="modal-close" onClick=${onClose} title="Close">×</button>
                    `}
                </div>
                <div class="modal-body" style="padding: 15px;">
                    ${state.running && html`
                        <div style="text-align: center; padding: 20px;">
                            <div class="spinner" style="margin: 0 auto 15px;"></div>
                            <p style="color: var(--text-primary); margin-bottom: 10px;">
                                Verifying files... ${state.verified + state.failed}/${state.total}
                            </p>
                            ${state.current && html`
                                <p style="color: var(--text-secondary); font-size: 0.85rem; margin-bottom: 5px;">
                                    ${state.current}
                                </p>
                            `}
                            ${state.currentProgress != null && html`
                                <div style="width: 100%; height: 4px; background: var(--bg-tertiary); border-radius: 2px; overflow: hidden;">
                                    <div style="width: ${state.currentProgress}%; height: 100%; background: var(--accent); transition: width 0.3s;"></div>
                                </div>
                            `}
                        </div>
                        <div style="margin-top: 15px; text-align: center; font-size: 0.85rem;">
                            <span style="color: var(--success);">✓ ${state.verified} verified</span>
                            ${state.failed > 0 && html`
                                <span style="color: var(--error); margin-left: 15px;">✗ ${state.failed} failed</span>
                            `}
                        </div>
                    `}

                    ${state.complete && html`
                        <div style="background: var(--bg-tertiary); padding: 12px; border-radius: 4px; margin-bottom: 15px; border: 1px solid ${state.failed > 0 ? 'var(--warning)' : 'var(--success)'};">
                            <p style="color: ${state.failed > 0 ? 'var(--warning)' : 'var(--success)'}; margin: 0; font-weight: bold;">
                                ${state.failed > 0
                ? `Verification complete: ${state.verified} passed, ${state.failed} failed`
                : `✓ All ${state.verified} file${state.verified > 1 ? 's' : ''} verified successfully!`
            }
                            </p>
                        </div>

                        ${state.results.length > 0 && html`
                            <div style="max-height: 200px; overflow-y: auto; padding: 10px; background: var(--bg-primary); border-radius: 4px;">
                                ${state.results.map(r => html`
                                    <div key=${r.path} style="font-size: 0.85rem; padding: 4px 0; color: ${r.valid ? 'var(--success)' : 'var(--error)'};">
                                        ${r.valid ? '✓' : '✗'} ${r.filename}
                                        ${!r.valid && r.message && html`
                                            <span style="color: var(--text-secondary);"> - ${r.message}</span>
                                        `}
                                    </div>
                                `)}
                            </div>
                        `}

                        <div style="margin-top: 15px;">
                            <button class="btn btn-primary" onClick=${onClose} style="width: 100%;">
                                Close
                            </button>
                        </div>
                    `}

                    ${state.error && html`
                        <div style="background: var(--bg-tertiary); padding: 12px; border-radius: 4px; margin-bottom: 15px; border: 1px solid var(--error);">
                            <p style="color: var(--error); margin: 0;">
                                ✗ Error: ${state.error}
                            </p>
                        </div>
                        <button class="btn btn-secondary" onClick=${onClose} style="width: 100%;">
                            Close
                        </button>
                    `}
                </div>
            </div>
        </div>
    `;
}

const buildCompressionValue = (selection, options) => {
    if (selection.includes('none')) return 'none';
    const ordered = options
        .filter((opt) => opt.value !== 'none' && selection.includes(opt.value))
        .map((opt) => opt.value);
    return ordered.length ? ordered.join(',') : null;
};

const cloneSelectionMap = (selection) => {
    if (!(selection instanceof Map)) return new Map();
    return new Map(selection);
};

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
    const [_hiddenJobIds, _setHiddenJobIds] = useState(new Set());
    const [loading, setLoading] = useState(false);
    const [conversionMode, setConversionMode] = useState('createcd');
    const [isoHandling, setIsoHandling] = useState(() => {
        try {
            const stored = localStorage.getItem(ISO_TOOL_STORAGE_KEY);
            return stored === 'chdman' || stored === 'dolphin' || stored === 'z3ds' ? stored : null;
        } catch (err) {
            return null;
        }
    });
    const [compressionSelection, setCompressionSelection] = useState(['zlib']);
    const [dolphinCompressionLevel, setDolphinCompressionLevel] = useState(DEFAULT_DOLPHIN_COMPRESSION_LEVEL);
    const [showCompressionHelp, setShowCompressionHelp] = useState(false);
    const [customFilterMode, setCustomFilterMode] = useState(false);
    const [outputDir, setOutputDir] = useState('');
    const [deleteOnVerify, setDeleteOnVerify] = useState(false);
    const [deletePlan, setDeletePlan] = useState(null); // { plan, paths, duplicateAction }
    const [showCHDInfo, setShowCHDInfo] = useState(null);
    const [searchMode, setSearchMode] = useState(false);
    const [searchResults, setSearchResults] = useState(null);
    const [showHelp, setShowHelp] = useState(false);
    const [notification, setNotification] = useState(null);
    const [converting, setConverting] = useState(false);
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
    const [showMetadataJobs, setShowMetadataJobs] = useState(() => {
        try {
            return localStorage.getItem(SHOW_METADATA_JOBS_STORAGE_KEY) === 'true';
        } catch (err) {
            return false;
        }
    });

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

    useEffect(() => {
        try {
            localStorage.setItem(ISO_TOOL_STORAGE_KEY, isoHandling);
        } catch (err) {
            // Ignore persistence failures (private mode, disabled storage).
        }
    }, [isoHandling]);

    useEffect(() => {
        try {
            localStorage.setItem(SHOW_METADATA_JOBS_STORAGE_KEY, showMetadataJobs ? 'true' : 'false');
        } catch (err) {
            // Ignore persistence failures (private mode, disabled storage).
        }
    }, [showMetadataJobs]);

    // Refresh file list for current directory or archive (transparent merge to avoid flicker)
    const refreshFileList = useCallback((showSpinner = false) => {
        const path = currentPathRef.current;
        const archivePath = currentArchivePathRef.current;

        if (searchMode) return;

        // If we're viewing an archive, refresh the archive contents
        if (archivePath) {
            if (showSpinner) setLoading(true);
            api.listArchive(archivePath)
                .then(archiveData => {
                    if (!archiveData || !archiveData.files) return;
                    setEntriesError(null);

                    const newArchiveEntries = archiveData.files.map(file => ({
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

                    // Merge entries transparently to preserve UI stability
                    setEntries(prevEntries => {
                        if (prevEntries.length === newArchiveEntries.length) {
                            const hasChanges = newArchiveEntries.some((newEntry, i) => {
                                const oldEntry = prevEntries[i];
                                return oldEntry.name !== newEntry.name ||
                                    oldEntry.path !== newEntry.path ||
                                    oldEntry.size !== newEntry.size ||
                                    oldEntry.convertible !== newEntry.convertible ||
                                    oldEntry.has_chd !== newEntry.has_chd ||
                                    oldEntry.chd_ready !== newEntry.chd_ready;
                            });
                            if (!hasChanges) return prevEntries;
                        }
                        return newArchiveEntries;
                    });
                })
                .catch(err => {
                    console.error('Failed to refresh archive contents:', err);
                })
                .finally(() => {
                    if (showSpinner) setLoading(false);
                });
            return;
        }

        // Otherwise refresh the directory contents
        if (path) {
            if (showSpinner) setLoading(true);
            api.listFiles(path)
                .then(data => {
                    setEntriesError(null);
                    // Merge entries transparently to preserve UI stability
                    setEntries(prevEntries => {
                        const newEntries = data.entries;
                        // If the lists are identical in length and names, check for actual changes
                        if (prevEntries.length === newEntries.length) {
                            const hasChanges = newEntries.some((newEntry, i) => {
                                const oldEntry = prevEntries[i];
                                return oldEntry.name !== newEntry.name ||
                                    oldEntry.path !== newEntry.path ||
                                    oldEntry.size !== newEntry.size ||
                                    oldEntry.type !== newEntry.type ||
                                    oldEntry.convertible !== newEntry.convertible ||
                                    oldEntry.dolphin_convertible !== newEntry.dolphin_convertible ||
                                    oldEntry.z3ds_convertible !== newEntry.z3ds_convertible ||
                                    oldEntry.has_chd !== newEntry.has_chd ||
                                    oldEntry.has_rvz !== newEntry.has_rvz ||
                                    oldEntry.dolphin_ready !== newEntry.dolphin_ready ||
                                    oldEntry.dolphin_path !== newEntry.dolphin_path ||
                                    oldEntry.has_z3ds !== newEntry.has_z3ds ||
                                    oldEntry.z3ds_ready !== newEntry.z3ds_ready ||
                                    oldEntry.z3ds_path !== newEntry.z3ds_path ||
                                    oldEntry.chd_ready !== newEntry.chd_ready;
                            });
                            // Only update if there are actual changes
                            if (!hasChanges) return prevEntries;
                        }
                        return newEntries;
                    });
                })
                .catch(err => {
                    setEntriesError(err.message); // Set error for the main file list
                    console.error('Failed to list files:', err);
                })
                .finally(() => {
                    if (showSpinner) setLoading(false);
                });
        }
    }, [searchMode]);

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

    // Load volumes on mount
    useEffect(() => {
        setVolumesLoading(true);
        api.getVolumes()
            .then(vols => {
                setVolumes(vols);
                setVolumesError(null);
                if (vols.length > 0) {
                    setSelectedVolume(vols[0]);
                    setCurrentPath(vols[0].path);
                }
                // Show help on first visit if no volumes
                if (vols.length === 0) {
                    setShowHelp(true);
                }
            })
            .catch(err => {
                setVolumesError(err.message);
                console.error('Failed to load volumes:', err);
            })
            .finally(() => setVolumesLoading(false));
    }, []);

    useEffect(() => {
        api.getVerifiedCHDs()
            .then(data => {
                if (data && Array.isArray(data.verified)) {
                    setVerifiedCHDs(new Set(data.verified));
                }
            })
            .catch(() => { });
    }, []);

    // Filtered and sorted entries based on file type filter and sort settings
    const displayedEntries = useMemo(() => {
        // First, filter entries
        let filtered = entries;
        if (fileTypeFilter) {
            const exts = fileTypeFilter.split(',').map(e => e.toLowerCase().trim());
            filtered = entries.filter(e =>
                e.type === 'directory' || e.type === 'archive' || exts.includes(e.extension?.toLowerCase())
            );
        }

        // Then, sort entries (directories and archives always first)
        const getStatusPriority = (entry) => {
            if (entry.type === 'directory') return 0;
            if (entry.type === 'archive') return 1;
            if (entry.has_chd || entry.has_rvz || entry.has_z3ds) return 2;
            if (entry.convertible || entry.dolphin_convertible || entry.z3ds_convertible) return 3;
            return 4;
        };

        return [...filtered].sort((a, b) => {
            // Directories always first
            if (a.type === 'directory' && b.type !== 'directory') return -1;
            if (b.type === 'directory' && a.type !== 'directory') return 1;
            // Archives second
            if (a.type === 'archive' && b.type !== 'archive' && b.type !== 'directory') return -1;
            if (b.type === 'archive' && a.type !== 'archive' && a.type !== 'directory') return 1;

            // Within same category, sort by selected column
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
                end: totalItems
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
    }, [currentPage, pagination.page]);

    const queueJobs = useMemo(() => {
        const activeServerJobs = jobs.filter(
            (job) => !['completed', 'failed', 'cancelled'].includes(job.status)
                  && (showMetadataJobs || job.mode !== 'metadata_scan'),
        );
        return creatingJobs.length > 0
            ? [...creatingJobs, ...activeServerJobs]
            : activeServerJobs;
    }, [creatingJobs, jobs, showMetadataJobs]);

    const completedJobs = useMemo(
        () => jobs.filter(
            (job) => job.status === 'completed'
                  && (showMetadataJobs || job.mode !== 'metadata_scan'),
        ),
        [jobs, showMetadataJobs],
    );

    const issueJobs = useMemo(
        () => jobs.filter(
            (job) => ['failed', 'cancelled'].includes(job.status)
                  && (showMetadataJobs || job.mode !== 'metadata_scan'),
        ),
        [jobs, showMetadataJobs],
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
                end: totalItems
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
    }, [jobCurrentPage, jobsPagination.page]);

    useEffect(() => {
        if (jobTab === 'issues' && issueJobs.length === 0) {
            setJobTab('queue');
            setJobCurrentPage(1);
        }
    }, [jobTab, issueJobs.length]);

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

    // Poll forced metadata scan status so badge refresh resumes when backend finishes.
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

    // Fetch CHD metadata for displayed CHD files
    useEffect(() => {
        if (forceRescanRunning) return;
        const hasActiveWork = creatingJobs.length > 0
            || jobs.some(j => ['creating', 'queued', 'processing'].includes(j.status));
        if (hasActiveWork) return;

        const chdPaths = displayedEntries
            .filter(e => e.extension?.toLowerCase() === '.chd')
            .map(e => e.path)
            .filter(p => !chdMetadata.has(p));

        if (chdPaths.length === 0) return;

        // First, check what's cached
        api.getCHDMetadataBatch(chdPaths)
            .then(data => {
                const cachedPaths = [];
                const uncachedPaths = [];

                Object.entries(data).forEach(([path, meta]) => {
                    if (meta.cached) {
                        cachedPaths.push([path, meta]);
                    } else {
                        uncachedPaths.push(path);
                    }
                });

                // Update with cached results
                if (cachedPaths.length > 0) {
                    setChdMetadata(prev => {
                        const next = new Map(prev);
                        cachedPaths.forEach(([path, meta]) => next.set(path, meta));
                        return next;
                    });
                }

                // Trigger info fetch for uncached files (this populates the backend cache)
                // Limit concurrent fetches to avoid overwhelming the server
                const fetchLimit = 3;
                const fetchUncached = async () => {
                    for (let i = 0; i < uncachedPaths.length; i += fetchLimit) {
                        const batch = uncachedPaths.slice(i, i + fetchLimit);
                        await Promise.all(batch.map(async (path) => {
                            try {
                                const info = await api.getCHDInfo(path);
                                setChdMetadata(prev => {
                                    const next = new Map(prev);
                                    next.set(path, { media_type: info.media_type, cached: true });
                                    return next;
                                });
                            } catch (e) {
                                // Mark as fetched but with no badge
                                setChdMetadata(prev => {
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
            .catch(err => console.warn('Failed to fetch CHD metadata:', err)); // Silently fail - badges are optional
    }, [displayedEntries, forceRescanRunning, jobs, creatingJobs]);

    // Load app version on mount
    useEffect(() => {
        api.getVersion()
            .then(data => {
                setAppVersion(data.version);
                if (typeof data.search_auto_return_to_file_list === 'boolean') {
                    setSearchAutoReturnToFileList(data.search_auto_return_to_file_list);
                }
            })
            .catch(err => console.warn('Failed to fetch app version:', err));
    }, []);

    // Load files when path changes
    useEffect(() => {
        if (currentPath) {
            setLoading(true);
            setEntriesError(null);
            api.listFiles(currentPath)
                .then(data => {
                    setEntries(data.entries);
                    setSearchMode(false);
                    setSearchResults(null);
                })
                .catch(err => {
                    setEntriesError(err.message);
                    console.error('Failed to list files:', err);
                })
                .finally(() => setLoading(false));
        }
    }, [currentPath]);

    // Subscribe to job updates
    useEffect(() => {
        // Helper to merge server jobs with local state, respecting hidden jobs
        const mergeJobs = (serverJobs, currentJobs, currentHiddenIds) => {
            // Filter out hidden jobs from server response
            const visibleServerJobs = serverJobs.filter(j => !currentHiddenIds.has(j.id));


            // Merge: prefer server state but keep local jobs that aren't on server yet
            const mergedJobs = [];
            const seenIds = new Set();

            // Add all visible server jobs (they have the authoritative state)
            for (const serverJob of visibleServerJobs) {
                mergedJobs.push(serverJob);
                seenIds.add(serverJob.id);
            }

            // Add any local jobs that aren't from the server yet (optimistic placeholders only)
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
            if (deferJobUiUpdatesRef.current) {
                return;
            }
            _setHiddenJobIds(currentHidden => {
                setJobs(prev => {
                    const merged = mergeJobs(serverJobs, prev, currentHidden);
                    const visibleIds = new Set(merged.map(j => j.id));
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

        // Fetch initial jobs list
        api.getJobs()
            .then(applyPolledJobs)
            .catch(() => { });

        const applyQueuedJobUpdates = (queuedUpdates) => {
            if (queuedUpdates.length === 0) return;

            const completedNames = [];
            let failedCount = 0;
            let cancelledCount = 0;
            const verifiedPathsToAdd = new Set();
            const verifiedPathsToRemove = new Set();

            setJobs(prevJobs => {
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
                    const statusUpdate = update.type === 'complete' ? 'completed' :
                        update.type === 'error' ? 'failed' :
                            update.type === 'cancelled' ? 'cancelled' :
                                update.data.status ?? prevJob.status;
                    const isTerminalUpdate = update.type === 'complete'
                        || update.type === 'error'
                        || update.type === 'cancelled';

                    // Progress events are still throttled per job, but now after
                    // SSE events have already been coalesced into a small batch.
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
                        output_size: update.data.output_size ?? prevJob.output_size
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
                    if (unchanged) {
                        continue;
                    }

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
                // Refresh file list with debounce to avoid churn on rapid batch completions.
                scheduleCompletionRefresh();
                if (completedNames.length === 1) {
                    notify(`Completed: ${completedNames[0]}`, 'success');
                } else {
                    notify(`Completed ${completedNames.length} jobs`, 'success');
                }
            }
            if (failedCount > 0) {
                notify(
                    failedCount === 1 ? '1 job failed' : `${failedCount} jobs failed`,
                    'error',
                );
            }
            if (cancelledCount > 0) {
                notify(
                    cancelledCount === 1 ? '1 job cancelled' : `${cancelledCount} jobs cancelled`,
                    'info',
                );
            }

            if (verifiedPathsToAdd.size > 0 || verifiedPathsToRemove.size > 0) {
                setVerifiedCHDs(prev => {
                    const next = new Set(prev);
                    for (const path of verifiedPathsToAdd) {
                        next.add(path);
                    }
                    for (const path of verifiedPathsToRemove) {
                        next.delete(path);
                    }
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

            if (!force && jobUpdateFlushTimeoutRef.current) {
                return;
            }

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

            // Keep only the latest pending update for each job to prevent UI lag
            // from high-frequency progress messages.
            queuedJobUpdatesRef.current.set(jobId, update);

            const isTerminalUpdate = update.type === 'complete'
                || update.type === 'error'
                || update.type === 'cancelled';
            flushQueuedJobUpdates(isTerminalUpdate);
        });

        // Poll jobs periodically - merge instead of replace
        const interval = setInterval(() => {
            api.getJobs()
                .then(applyPolledJobs)
                .catch(() => { });

            // Check for stuck state
            api.checkStuckStatus()
                .then(status => {
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

    // Auto-refresh file list when enabled
    useEffect(() => {
        const hasActiveWork = creatingJobs.length > 0
            || jobs.some(j => ['creating', 'queued', 'processing'].includes(j.status));
        if (!autoRefresh || !currentPath || searchMode || hasActiveWork) return;

        const interval = setInterval(() => {
            refreshFileList(false); // Silent refresh, no spinner
        }, 3000); // Refresh every 3 seconds

        return () => clearInterval(interval);
    }, [autoRefresh, currentPath, searchMode, refreshFileList, jobs, creatingJobs]);

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

    // File operations handlers
    const handleRename = async (path, newName) => {
        await api.renameFile(path, newName);
        notify(`✓ Renamed to ${newName}`, 'success');
        if (path.toLowerCase().endsWith('.chd')) {
            const lastSlash = path.lastIndexOf('/');
            const newPath = lastSlash >= 0 ? `${path.slice(0, lastSlash)}/${newName}` : newName;
            setVerifiedCHDs(prev => {
                if (!prev.has(path)) return prev;
                const next = new Set(prev);
                next.delete(path);
                next.add(newPath);
                return next;
            });
        }
        refreshFileList(true);
    };

    const handleDelete = async (path) => {
        await api.deleteFile(path);
        notify('✓ File deleted', 'success');
        if (path.toLowerCase().endsWith('.chd')) {
            setVerifiedCHDs(prev => {
                if (!prev.has(path)) return prev;
                const next = new Set(prev);
                next.delete(path);
                return next;
            });
        }
        refreshFileList(true);
    };

    const handleVerify = async (productPath, entry = null) => {
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

        setVerifyProgress(prev => new Map(prev).set(verifyPath, { progress: 0, message: 'Starting verification...' }));
        try {
            const result = await verifyFn(verifyPath, {
                onProgress: (update) => {
                    setVerifyProgress(prev => {
                        const next = new Map(prev);
                        next.set(verifyPath, {
                            progress: update.progress,
                            message: update.message
                        });
                        return next;
                    });
                }
            });
            if (result.valid) {
                setVerifiedCHDs(prev => new Set([...prev, verifyPath]));
                notify(`✓ ${label} verified successfully`, 'success');
            } else {
                notify(`✗ ${label} verification failed: ${result.message}`, 'error');
            }
            return result;
        } catch (err) {
            notify(`✗ ${label} verification failed: ${err.message}`, 'error');
            throw err;
        } finally {
            setVerifyProgress(prev => {
                const next = new Map(prev);
                next.delete(verifyPath);
                return next;
            });
        }
    };

    // Get selected entries that can be deleted (files and archives, not directories or archive members)
    const getDeletableSelection = useCallback(() => {
        const entries = [];
        for (const [path, entry] of selectedFiles) {
            // Exclude directories, archive members (paths like "archive.zip::game.iso" can't be deleted individually)
            if (entry && entry.type !== 'directory' && !entry.is_archive_item && !path.includes('::')) {
                entries.push(entry);
            }
        }
        return entries;
    }, [selectedFiles]);

    // Get selected file paths for verification (CHD + Dolphin formats)
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

    // Handle bulk delete button click
    const handleBulkDeleteClick = useCallback(() => {
        const entries = getDeletableSelection();
        if (entries.length === 0) {
            notify('⚠ No deletable files selected', 'error');
            return;
        }
        setBulkDeleteEntries(entries);
    }, [getDeletableSelection, notify]);

    // Handle bulk verify button click
    const handleBulkVerifyClick = useCallback(() => {
        const items = getVerifiableItems();
        if (items.length === 0) {
            notify('⚠ No verifiable files selected', 'error');
            return;
        }
        const isoItems = items.filter(item => item.kind === 'iso');
        const _3dsItems = items.filter(item => item.kind === 'z3ds');
        let finalItems = items.filter(item => item.kind !== 'iso' && item.kind !== 'z3ds');

        if (isoItems.length > 0) {
            if (isoHandling === 'dolphin') {
                finalItems = finalItems.concat(isoItems.map(item => ({ ...item, kind: 'dolphin' })));
            } else {
                notify('ISO verification uses Dolphin tools. Switch ISO handling to Dolphin to verify ISO files.', 'info');
            }
        }
        if (_3dsItems.length > 0) {
            finalItems = finalItems.concat(_3dsItems.map(item => ({ ...item, kind: 'z3ds' })));
        }

        if (finalItems.length === 0) {
            notify('⚠ No files selected for verification', 'error');
            return;
        }
        setBulkVerifyItems(finalItems);
    }, [getVerifiableItems, notify, isoHandling]);

    // Handle bulk verify completion - update verified CHDs set
    const handleBulkVerifyComplete = useCallback((result) => {
        // Refresh verified CHDs from server to ensure consistency
        api.getVerifiedCHDs()
            .then(data => {
                if (data && Array.isArray(data.verified)) {
                    setVerifiedCHDs(new Set(data.verified));
                }
            })
            .catch(() => { });

        if (result.verified > 0) {
            notify(`✓ Verified ${result.verified} file${result.verified > 1 ? 's' : ''}${result.failed > 0 ? `, ${result.failed} failed` : ''}`,
                result.failed > 0 ? 'warning' : 'success');
        }
        // Clear selection after bulk verify
        setSelectedFiles(new Map());
    }, [notify]);

    // Handle adding a single verified CHD to the set (called from BulkDeleteModal)
    const handleAddVerifiedCHD = useCallback((chdPath) => {
        setVerifiedCHDs(prev => new Set([...prev, chdPath]));
    }, []);

    // Handle bulk delete modal refresh
    const handleBulkDeleteRefresh = useCallback(() => {
        refreshFileList(true);
        // Clear selection after successful bulk delete
        setSelectedFiles(new Map());
        // Refresh verified CHDs from server to remove deleted CHD paths from cache
        api.getVerifiedCHDs()
            .then(data => {
                if (data && Array.isArray(data.verified)) {
                    setVerifiedCHDs(new Set(data.verified));
                }
            })
            .catch(() => { });
    }, [refreshFileList]);

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

    const handleSearch = async () => {
        if (!currentPath) {
            notify('⚠ No path selected', 'error');
            return;
        }

        if (!searchMode) {
            capturePreSearchView();
        }

        setLoading(true);
        setEntriesError(null);
        notify('🔍 Searching for convertible files...', 'info');

        try {
            const results = await api.searchFiles(currentPath, true, true);
            setSearchResults(results);
            setSearchMode(true);
            setCurrentPage(1);

            const combined = [
                ...results.files.map(f => ({
                    ...f,
                    type: 'file',
                    convertible: Boolean(f.convertible),
                    dolphin_convertible: Boolean(f.dolphin_convertible),
                    has_rvz: Boolean(f.has_rvz),
                    dolphin_ready: Boolean(f.dolphin_ready),
                    dolphin_path: f.dolphin_path || null,
                    z3ds_convertible: Boolean(f.z3ds_convertible),
                    has_z3ds: Boolean(f.has_z3ds),
                    z3ds_ready: Boolean(f.z3ds_ready),
                    z3ds_path: f.z3ds_path || null,
                    chd_ready: Boolean(f.chd_ready)
                })),
                ...results.archives.map(a => ({
                    ...a,
                    name: `${a.name} (in ${a.archive_path.split('/').pop()})`,
                    type: 'file',
                    convertible: Boolean(a.convertible),
                    dolphin_convertible: Boolean(a.dolphin_convertible),
                    has_rvz: false,
                    dolphin_ready: false,
                    dolphin_path: null,
                    has_z3ds: false,
                    z3ds_ready: false,
                    z3ds_path: null,
                    chd_ready: Boolean(a.chd_ready),
                    is_archive_item: true,
                    chd_path: a.chd_path
                }))
            ];
            setEntries(combined);
            setCurrentPage(1);
            setLastSelectedIndex(null); // Reset shift-selection anchor

            if (combined.length === 0) {
                notify('ℹ No convertible files found', 'info');
            } else {
                notify(`✓ Found ${combined.length} convertible file(s)`, 'success');
            }
        } catch (err) {
            setEntriesError(err.message);
            notify(`✗ Search failed: ${err.message}`, 'error');
            console.error('Search failed:', err);
        } finally {
            setLoading(false);
        }
    };

    const handleScanMetadata = async (force = false) => {
        const shouldForce = force === true;
        try {
            const res = await api.scanMetadata(shouldForce);
            if (res.status === 'scanning') {
                if (shouldForce) {
                    setChdMetadata(new Map());
                    if (!forceRescanRunning) {
                        setForceRescanRunning(true);
                    }
                }
                notify('Metadata scan already in progress', 'info');
            } else {
                if (shouldForce) {
                    setChdMetadata(new Map());
                    setForceRescanRunning(true);
                }
                notify(
                    shouldForce ? 'Started forced metadata scan' : 'Started background metadata scan',
                    'success'
                );
            }
        } catch (err) {
            notify(`Failed to start scan: ${err.message}`, 'error');
        }
    };

    const handleCancelJob = async (jobId) => {
        try {
            await api.cancelJob(jobId);
            setJobs(prev => prev.map(j =>
                j.id === jobId
                    ? {
                        ...j,
                        status: j.status === 'queued' ? 'cancelled' : j.status,
                        message: j.status === 'queued' ? j.message : 'Cancelling...'
                    }
                    : j
            ));
            notify('Cancellation requested', 'info');
        } catch (err) {
            notify(`Failed to cancel: ${err.message}`, 'error');
            console.error('Failed to cancel job:', err);
        }
    };

    const handleRequestCancelAll = () => {
        const activeCount = jobs.filter(j => ['queued', 'processing'].includes(j.status) && j.mode !== 'metadata_scan').length;
        if (activeCount === 0) {
            notify('No active jobs to cancel', 'info');
            return;
        }
        setShowCancelAllModal(true);
    };

    const handleCancelAllJobs = async () => {
        if (cancellingAllJobs) return;
        setCancellingAllJobs(true);
        try {
            const result = await api.cancelAllJobs();
            setJobs(prev => prev.map((job) => {
                if (job.mode === 'metadata_scan') return job;
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
    };

    const handleRequestClearCompleted = () => {
        const completedCount = jobs.filter(j => ['completed', 'failed', 'cancelled'].includes(j.status)).length;
        if (completedCount === 0) {
            notify('No completed jobs to clear', 'info');
            return;
        }
        setShowClearDoneModal(true);
    };

    const handleClearCompleted = async () => {
        if (clearingCompletedJobs) return;

        // Find all completed/failed/cancelled jobs
        const completedJobs = jobs.filter(j => ['completed', 'failed', 'cancelled'].includes(j.status));

        if (completedJobs.length === 0) return;
        setClearingCompletedJobs(true);

        // Immediately hide these jobs from the UI
        const idsToHide = completedJobs.map(j => j.id);
        _setHiddenJobIds(prev => {
            const next = new Set(prev);
            idsToHide.forEach(id => next.add(id));
            return next;
        });
        setJobs(prev => prev.filter(j => !['completed', 'failed', 'cancelled'].includes(j.status)));

        // Delete all completed jobs from the server in one request
        try {
            await api.deleteCompletedJobs();
            // Successfully deleted - clean up hidden set
            _setHiddenJobIds(prev => {
                const next = new Set(prev);
                idsToHide.forEach(id => next.delete(id));
                return next;
            });
        } catch (err) {
            // If deletion fails, jobs will reappear on next poll
            // Remove from hidden set so they become visible again
            console.error('Failed to delete completed jobs:', err);
            _setHiddenJobIds(prev => {
                const next = new Set(prev);
                idsToHide.forEach(id => next.delete(id));
                return next;
            });
            notify('Failed to clear completed jobs', 'error');
        } finally {
            setClearingCompletedJobs(false);
            setShowClearDoneModal(false);
        }
    };

    const handleRecoverStuck = async () => {
        if (recoveringStuck) return;

        setRecoveringStuck(true);
        try {
            const result = await api.recoverStuckJobs();
            notify(`Recovery completed: removed ${result.removed_locks || 0} stale lock(s)`, 'success');
            // Immediately check stuck status again
            const status = await api.checkStuckStatus();
            setStuckState(status);
        } catch (err) {
            notify(`Recovery failed: ${err.message}`, 'error');
            console.error('Failed to recover stuck jobs:', err);
        } finally {
            setRecoveringStuck(false);
        }
    };


    const queuedJobsCount = jobs.filter(j => j.status === 'queued' && j.mode !== 'metadata_scan').length;
    const processingJobsCount = jobs.filter(j => j.status === 'processing' && j.mode !== 'metadata_scan').length;
    const activeJobsCount = queuedJobsCount + processingJobsCount;
    const hasActiveJobs = activeJobsCount > 0;
    const hasCompletedJobs = jobs.some(j =>
        ['completed', 'failed', 'cancelled'].includes(j.status)
        && (showMetadataJobs || j.mode !== 'metadata_scan')
    );
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
                </div>
            <div class="iso-tool-hint${needsIsoSelection ? ' iso-tool-hint-warning' : ''}">
                ${getPrimaryToolHint(isoHandling)}
            </div>
        </div>

            ${showHelp && html`<${HelpPanel} onClose=${() => setShowHelp(false)} isoHandling=${isoHandling} />`}

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
                            <label class="metadata-jobs-toggle" title="Include metadata scan jobs in the jobs list">
                                <input
                                    type="checkbox"
                                    id="show-metadata-jobs"
                                    checked=${showMetadataJobs}
                                    onChange=${() => {
                                        setShowMetadataJobs(prev => !prev);
                                        setJobCurrentPage(1);
                                    }}
                                    aria-label="Show metadata scan jobs"
                                />
                                <span>Show scans</span>
                            </label>
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

// Render app
render(html`<${App} />`, document.getElementById('app'));
