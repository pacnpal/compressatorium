// Main CHD Converter App
import { api, formatSize, getFileIcon } from './api.js';

const { html, render, useState, useEffect, useCallback } = window;

// ============ Components ============

function VolumeList({ volumes, selectedVolume, onSelect }) {
    return html`
        <ul class="volume-list">
            ${volumes.map(vol => html`
                <li
                    key=${vol.path}
                    class="volume-item ${selectedVolume?.path === vol.path ? 'active' : ''}"
                    onClick=${() => onSelect(vol)}
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
                    >
                        ${crumb.name}
                    </span>
                </span>
            `)}
        </div>
    `;
}

function FileList({ entries, selectedFiles, onNavigate, onSelect, onToggleSelect, onShowInfo }) {
    if (!entries || entries.length === 0) {
        return html`
            <div class="empty-state">
                <div class="icon">📂</div>
                <p>No files found</p>
            </div>
        `;
    }

    const handleClick = (entry, e) => {
        if (entry.type === 'directory') {
            onNavigate(entry.path);
        } else if (entry.type === 'archive') {
            onNavigate(entry.path);
        } else if (entry.extension === '.chd') {
            onShowInfo(entry.path);
        } else if (entry.convertible) {
            onToggleSelect(entry);
        }
    };

    return html`
        <ul class="file-list">
            ${entries.map(entry => html`
                <li
                    key=${entry.path}
                    class="file-item ${selectedFiles.has(entry.path) ? 'selected' : ''}"
                    onClick=${(e) => handleClick(entry, e)}
                >
                    ${entry.convertible && html`
                        <input
                            type="checkbox"
                            class="checkbox"
                            checked=${selectedFiles.has(entry.path)}
                            onClick=${(e) => { e.stopPropagation(); onToggleSelect(entry); }}
                        />
                    `}
                    <span class="icon">${getFileIcon(entry)}</span>
                    <div class="info">
                        <div class="name">${entry.name}</div>
                        ${entry.size !== null && html`
                            <div class="meta">${formatSize(entry.size)}</div>
                        `}
                    </div>
                    ${entry.has_chd && html`
                        <span class="status has-chd">CHD exists</span>
                    `}
                    ${entry.convertible && !entry.has_chd && html`
                        <span class="status convertible">Convertible</span>
                    `}
                </li>
            `)}
        </ul>
    `;
}

function JobList({ jobs, onCancel, onRefresh }) {
    if (jobs.length === 0) {
        return html`
            <div class="empty-state">
                <div class="icon">⏳</div>
                <p>No conversion jobs</p>
            </div>
        `;
    }

    return html`
        <ul class="job-list">
            ${jobs.map(job => html`
                <li key=${job.id} class="job-item">
                    <div class="job-header">
                        <span class="job-name" title=${job.file_path}>${job.filename}</span>
                        <span class="job-status ${job.status}">${job.status}</span>
                    </div>
                    ${job.status === 'processing' && html`
                        <div class="progress-bar">
                            <div class="progress-fill" style="width: ${job.progress}%"></div>
                        </div>
                    `}
                    <div class="job-message">${job.message || job.error_message || ''}</div>
                    <div class="job-actions">
                        ${['queued', 'processing'].includes(job.status) && html`
                            <button class="btn btn-sm btn-secondary" onClick=${() => onCancel(job.id)}>
                                Cancel
                            </button>
                        `}
                        ${job.status === 'completed' && job.output_size && html`
                            <span class="meta">Output: ${formatSize(job.output_size)}</span>
                        `}
                    </div>
                </li>
            `)}
        </ul>
    `;
}

function CHDInfoModal({ path, onClose }) {
    const [info, setInfo] = useState(null);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState(null);

    useEffect(() => {
        if (path) {
            setLoading(true);
            api.getCHDInfo(path)
                .then(setInfo)
                .catch(e => setError(e.message))
                .finally(() => setLoading(false));
        }
    }, [path]);

    if (!path) return null;

    return html`
        <div class="modal-overlay" onClick=${onClose}>
            <div class="modal" onClick=${(e) => e.stopPropagation()}>
                <div class="modal-header">
                    <h3>CHD Information</h3>
                    <button class="modal-close" onClick=${onClose}>×</button>
                </div>
                ${loading && html`<div class="loading"><div class="spinner"></div>Loading...</div>`}
                ${error && html`<div class="error">${error}</div>`}
                ${info && html`
                    <div class="info-grid">
                        <span class="info-label">File</span>
                        <span class="info-value">${info.file}</span>

                        ${info.file_version && html`
                            <span class="info-label">Version</span>
                            <span class="info-value">${info.file_version}</span>
                        `}
                        ${info.logical_size && html`
                            <span class="info-label">Logical Size</span>
                            <span class="info-value">${info.logical_size}</span>
                        `}
                        ${info.chd_size && html`
                            <span class="info-label">CHD Size</span>
                            <span class="info-value">${info.chd_size}</span>
                        `}
                        ${info.compression && html`
                            <span class="info-label">Compression</span>
                            <span class="info-value">${info.compression}</span>
                        `}
                        ${info.ratio && html`
                            <span class="info-label">Ratio</span>
                            <span class="info-value">${info.ratio}</span>
                        `}
                        ${info.sha1 && html`
                            <span class="info-label">SHA1</span>
                            <span class="info-value">${info.sha1}</span>
                        `}
                    </div>
                    ${info.raw_data && html`
                        <details style="margin-top: 15px">
                            <summary style="cursor: pointer; color: var(--text-secondary)">Raw Output</summary>
                            <pre style="margin-top: 10px; font-size: 0.8rem; overflow-x: auto; padding: 10px; background: var(--bg-primary); border-radius: 4px">${info.raw_data}</pre>
                        </details>
                    `}
                `}
            </div>
        </div>
    `;
}

// ============ Main App ============

function App() {
    // State
    const [volumes, setVolumes] = useState([]);
    const [selectedVolume, setSelectedVolume] = useState(null);
    const [currentPath, setCurrentPath] = useState(null);
    const [entries, setEntries] = useState([]);
    const [selectedFiles, setSelectedFiles] = useState(new Map());
    const [jobs, setJobs] = useState([]);
    const [loading, setLoading] = useState(false);
    const [conversionMode, setConversionMode] = useState('createcd');
    const [outputDir, setOutputDir] = useState('');
    const [showCHDInfo, setShowCHDInfo] = useState(null);
    const [searchMode, setSearchMode] = useState(false);
    const [searchResults, setSearchResults] = useState(null);

    // Load volumes on mount
    useEffect(() => {
        api.getVolumes().then(vols => {
            setVolumes(vols);
            if (vols.length > 0) {
                setSelectedVolume(vols[0]);
                setCurrentPath(vols[0].path);
            }
        });
    }, []);

    // Load files when path changes
    useEffect(() => {
        if (currentPath) {
            setLoading(true);
            api.listFiles(currentPath)
                .then(data => {
                    setEntries(data.entries);
                    setSearchMode(false);
                    setSearchResults(null);
                })
                .catch(console.error)
                .finally(() => setLoading(false));
        }
    }, [currentPath]);

    // Subscribe to job updates
    useEffect(() => {
        const unsubscribe = api.subscribeToJobs((update) => {
            setJobs(prevJobs => {
                const idx = prevJobs.findIndex(j => j.id === update.data.job_id);
                if (idx === -1) return prevJobs;

                const newJobs = [...prevJobs];
                newJobs[idx] = {
                    ...newJobs[idx],
                    progress: update.data.progress ?? newJobs[idx].progress,
                    message: update.data.message ?? newJobs[idx].message,
                    status: update.type === 'complete' ? 'completed' :
                            update.type === 'error' ? 'failed' :
                            update.data.status ?? newJobs[idx].status,
                    error_message: update.data.error,
                    output_size: update.data.output_size
                };
                return newJobs;
            });
        });

        // Poll jobs periodically
        const interval = setInterval(() => {
            api.getJobs().then(setJobs).catch(() => {});
        }, 5000);

        return () => {
            unsubscribe();
            clearInterval(interval);
        };
    }, []);

    // Handlers
    const handleVolumeSelect = (vol) => {
        setSelectedVolume(vol);
        setCurrentPath(vol.path);
        setSelectedFiles(new Map());
    };

    const handleNavigate = (path) => {
        setCurrentPath(path);
        setSelectedFiles(new Map());
    };

    const handleToggleSelect = (entry) => {
        setSelectedFiles(prev => {
            const next = new Map(prev);
            if (next.has(entry.path)) {
                next.delete(entry.path);
            } else {
                next.set(entry.path, entry);
            }
            return next;
        });
    };

    const handleSelectAll = () => {
        const convertible = entries.filter(e => e.convertible && !e.has_chd);
        if (selectedFiles.size === convertible.length) {
            setSelectedFiles(new Map());
        } else {
            setSelectedFiles(new Map(convertible.map(e => [e.path, e])));
        }
    };

    const handleConvert = async () => {
        const paths = Array.from(selectedFiles.keys());
        if (paths.length === 0) return;

        try {
            const newJobs = await api.createBatchJobs(
                paths,
                conversionMode,
                outputDir || null
            );
            setJobs(prev => [...newJobs, ...prev]);
            setSelectedFiles(new Map());
        } catch (err) {
            console.error('Failed to create jobs:', err);
        }
    };

    const handleSearch = async () => {
        if (!currentPath) return;
        setLoading(true);
        try {
            const results = await api.searchFiles(currentPath, true, true);
            setSearchResults(results);
            setSearchMode(true);

            // Combine files and archive contents for display
            const combined = [
                ...results.files.map(f => ({
                    ...f,
                    type: 'file',
                    convertible: true
                })),
                ...results.archives.map(a => ({
                    ...a,
                    name: `${a.name} (in ${a.archive_path.split('/').pop()})`,
                    type: 'file',
                    convertible: true
                }))
            ];
            setEntries(combined);
        } catch (err) {
            console.error('Search failed:', err);
        } finally {
            setLoading(false);
        }
    };

    const handleCancelJob = async (jobId) => {
        try {
            await api.cancelJob(jobId);
            setJobs(prev => prev.filter(j => j.id !== jobId));
        } catch (err) {
            console.error('Failed to cancel job:', err);
        }
    };

    const convertibleCount = entries.filter(e => e.convertible && !e.has_chd).length;

    return html`
        <div class="container">
            <header>
                <h1><span>CHD</span> Converter</h1>
                <span>Convert game disc images to CHD format</span>
            </header>

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
                        />
                    </div>
                </div>

                <!-- Files Panel -->
                <div class="panel">
                    <div class="panel-header">
                        <h2>Files</h2>
                        <div>
                            <button class="btn btn-sm btn-secondary" onClick=${handleSearch}>
                                🔍 Search All
                            </button>
                        </div>
                    </div>

                    <${Breadcrumb}
                        path=${currentPath}
                        volume=${selectedVolume}
                        onNavigate=${handleNavigate}
                    />

                    ${searchMode && searchResults && html`
                        <div class="search-results">
                            <h3>Found ${searchResults.total_files} files, ${searchResults.total_in_archives} in archives</h3>
                        </div>
                    `}

                    <div class="toolbar">
                        <select value=${conversionMode} onChange=${(e) => setConversionMode(e.target.value)}>
                            <option value="createcd">CD Mode (Dreamcast, PS1, etc.)</option>
                            <option value="createdvd">DVD Mode (PSP, PS2, etc.)</option>
                        </select>
                        ${convertibleCount > 0 && html`
                            <button class="btn btn-sm btn-secondary" onClick=${handleSelectAll}>
                                ${selectedFiles.size === convertibleCount ? 'Deselect All' : `Select All (${convertibleCount})`}
                            </button>
                        `}
                        <button
                            class="btn btn-primary"
                            disabled=${selectedFiles.size === 0}
                            onClick=${handleConvert}
                        >
                            Convert ${selectedFiles.size > 0 ? `(${selectedFiles.size})` : ''}
                        </button>
                    </div>

                    <div class="output-dir-selector">
                        <label>Output directory:</label>
                        <input
                            type="text"
                            placeholder="Same as source (leave empty)"
                            value=${outputDir}
                            onInput=${(e) => setOutputDir(e.target.value)}
                        />
                    </div>

                    <div class="panel-content">
                        ${loading
                            ? html`<div class="loading"><div class="spinner"></div>Loading...</div>`
                            : html`<${FileList}
                                entries=${entries}
                                selectedFiles=${selectedFiles}
                                onNavigate=${handleNavigate}
                                onToggleSelect=${handleToggleSelect}
                                onShowInfo=${setShowCHDInfo}
                            />`
                        }
                    </div>
                </div>

                <!-- Jobs Panel -->
                <div class="panel jobs-panel">
                    <div class="panel-header">
                        <h2>Jobs</h2>
                        <button class="btn btn-sm btn-secondary" onClick=${() => api.getJobs().then(setJobs)}>
                            ↻ Refresh
                        </button>
                    </div>
                    <div class="panel-content">
                        <${JobList}
                            jobs=${jobs}
                            onCancel=${handleCancelJob}
                        />
                    </div>
                </div>
            </div>

            ${showCHDInfo && html`
                <${CHDInfoModal}
                    path=${showCHDInfo}
                    onClose=${() => setShowCHDInfo(null)}
                />
            `}
        </div>
    `;
}

// Render app
render(html`<${App} />`, document.getElementById('app'));
