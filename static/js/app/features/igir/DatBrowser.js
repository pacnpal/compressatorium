import { api, formatSize } from '../../../api.js';
import { html, useCallback, useEffect, useState } from '../../runtime/preactRuntime.js';

export function DatBrowser({ selectedDats, onToggleDat, onSelectAll, onDeselectAll }) {
    const [datTree, setDatTree] = useState(null);
    const [datLoading, setDatLoading] = useState(false);
    const [datError, setDatError] = useState(null);
    const [expandedDirs, setExpandedDirs] = useState(new Set());
    const [subdirListings, setSubdirListings] = useState({});
    const [datSearch, setDatSearch] = useState('');
    const [allDats, setAllDats] = useState(null);

    useEffect(() => {
        setDatLoading(true);
        api.listDats().then(data => {
            setDatTree(data);
            setDatLoading(false);
        }).catch(err => {
            setDatError(err.message);
            setDatLoading(false);
        });
    }, []);

    const handleToggleDir = useCallback((dirPath) => {
        setExpandedDirs(prev => {
            const next = new Set(prev);
            if (next.has(dirPath)) {
                next.delete(dirPath);
            } else {
                next.add(dirPath);
            }
            return next;
        });
        if (!subdirListings[dirPath]) {
            api.listDats(dirPath).then(data => {
                setSubdirListings(prev => ({
                    ...prev,
                    [dirPath]: data
                }));
            });
        }
    }, [subdirListings]);

    const handleSearch = useCallback(() => {
        if (!datSearch.trim()) {
            setAllDats(null);
            return;
        }
        api.searchDats().then(results => {
            const filtered = results.filter(d =>
                d.name.toLowerCase().includes(datSearch.toLowerCase())
            );
            setAllDats(filtered);
        });
    }, [datSearch]);

    const renderEntries = (entries, prefix = '') => {
        if (!entries || entries.length === 0) return html`<div class="dat-empty">No DAT files found</div>`;
        return entries.map(entry => {
            const isSelected = selectedDats.has(entry.path);
            return html`
                <label class="dat-entry" key=${entry.path}>
                    <input
                        type="checkbox"
                        checked=${isSelected}
                        onChange=${() => onToggleDat(entry.path)}
                    />
                    <span class="dat-name" title=${entry.path}>${entry.name}</span>
                    <span class="dat-size">${formatSize(entry.size)}</span>
                </label>
            `;
        });
    };

    const renderSubdirs = (subdirs, parentPath = '') => {
        if (!subdirs || subdirs.length === 0) return null;
        return subdirs.map(dir => {
            const dirPath = parentPath ? `${parentPath}/${dir}` : dir;
            const isExpanded = expandedDirs.has(dirPath);
            const subdata = subdirListings[dirPath];
            return html`
                <div class="dat-dir" key=${dirPath}>
                    <div class="dat-dir-header" onClick=${() => handleToggleDir(dirPath)}>
                        <span class="dat-dir-arrow">${isExpanded ? '▼' : '▶'}</span>
                        <span class="dat-dir-name">${dir}</span>
                    </div>
                    ${isExpanded && !subdata && html`
                        <div class="dat-dir-content">Loading...</div>
                    `}
                    ${isExpanded && subdata && html`
                        <div class="dat-dir-content">
                            ${renderEntries(subdata.entries)}
                            ${renderSubdirs(subdata.subdirectories, dirPath)}
                        </div>
                    `}
                </div>
            `;
        });
    };

    if (datLoading) return html`<div class="loading"><div class="spinner"></div>Loading DATs...</div>`;
    if (datError) return html`<div class="error-state">${datError}</div>`;

    const displayEntries = allDats || (datTree?.entries || []);
    const displaySubdirs = allDats ? [] : (datTree?.subdirectories || []);

    return html`
        <div class="dat-browser">
            <div class="dat-browser-toolbar">
                <input
                    type="text"
                    class="dat-search-input"
                    placeholder="Search DAT files..."
                    value=${datSearch}
                    onInput=${(e) => setDatSearch(e.target.value)}
                    onKeyDown=${(e) => e.key === 'Enter' && handleSearch()}
                />
                <button class="btn btn-sm btn-secondary" onClick=${handleSearch}>Search</button>
            </div>
            <div class="dat-browser-actions">
                <button class="btn btn-sm btn-secondary" onClick=${onSelectAll} disabled=${displayEntries.length === 0}>
                    Select All
                </button>
                <button class="btn btn-sm btn-secondary" onClick=${onDeselectAll} disabled=${selectedDats.size === 0}>
                    Deselect All
                </button>
                <span class="dat-count">${selectedDats.size} selected</span>
            </div>
            <div class="dat-browser-list">
                ${renderEntries(displayEntries)}
                ${renderSubdirs(displaySubdirs)}
            </div>
        </div>
    `;
}

