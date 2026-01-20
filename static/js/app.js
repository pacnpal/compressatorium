// Main CHD Converter App
import { api, formatSize, getFileIcon } from './api.js';

const { html, render, useState, useEffect, useCallback } = window;

// ============ Help Component ============

function HelpPanel({ onClose }) {
    return html`
        <div class="help-panel">
            <div class="help-header">
                <h3>Quick Start Guide</h3>
                <button class="btn btn-sm btn-secondary" onClick=${onClose}>×</button>
            </div>
            <div class="help-content">
                <h4>How to use CHD Converter</h4>
                <ol>
                    <li><strong>Select a Volume</strong> - Choose a mounted directory from the left panel</li>
                    <li><strong>Browse Files</strong> - Navigate through folders to find your disc images</li>
                    <li><strong>Select Files</strong> - Click checkboxes next to files you want to convert</li>
                    <li><strong>Choose Mode</strong>:
                        <ul>
                            <li><em>CD Mode</em> - For Dreamcast (.gdi), PlayStation 1, Sega CD, etc.</li>
                            <li><em>DVD Mode</em> - For PSP (.iso), PlayStation 2, etc.</li>
                        </ul>
                    </li>
                    <li><strong>Convert</strong> - Click the Convert button to start</li>
                </ol>
                <h4>File Types</h4>
                <ul>
                    <li>💽 <strong>.gdi, .iso, .cue, .bin</strong> - Can be converted to CHD</li>
                    <li>💿 <strong>.chd</strong> - Click to view file information</li>
                    <li>📦 <strong>.zip, .7z, .rar</strong> - Archives (click to browse contents)</li>
                </ul>
                <h4>Tips</h4>
                <ul>
                    <li>Files showing "CHD exists" already have a converted version</li>
                    <li>Use "Search All" to find all convertible files recursively</li>
                    <li>Set a custom output directory or leave empty to save alongside source</li>
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
                <div class="icon">📂</div>
                <p>No volumes configured</p>
                <p class="help-text">Mount directories using CHD_VOLUMES environment variable</p>
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

function FileList({ entries, selectedFiles, onNavigate, onToggleSelect, onShowInfo, error }) {
    if (error) {
        return html`
            <div class="error-state">
                <div class="icon">⚠️</div>
                <p>Failed to load files</p>
                <p class="error-detail">${error}</p>
            </div>
        `;
    }

    if (!entries || entries.length === 0) {
        return html`
            <div class="empty-state">
                <div class="icon">📂</div>
                <p>No files found</p>
                <p class="help-text">This folder is empty or contains no supported files</p>
            </div>
        `;
    }

    const handleClick = (entry, e) => {
        if (entry.type === 'directory') {
            onNavigate(entry.path);
        } else if (entry.type === 'archive') {
            // For archives, show a message - archive browsing requires extraction
            alert(`Archive: ${entry.name}\n\nUse "Search All" to find convertible files inside archives.`);
        } else if (entry.extension === '.chd') {
            onShowInfo(entry.path);
        } else if (entry.convertible) {
            onToggleSelect(entry);
        }
    };

    const getTooltip = (entry) => {
        if (entry.type === 'directory') return `Open folder: ${entry.name}`;
        if (entry.type === 'archive') return `Archive: ${entry.name} - Use Search All to find files inside`;
        if (entry.extension === '.chd') return `Click to view CHD info`;
        if (entry.convertible) return entry.has_chd ? `Already converted` : `Click to select for conversion`;
        return entry.name;
    };

    return html`
        <ul class="file-list">
            ${entries.map(entry => html`
                <li
                    key=${entry.path}
                    class="file-item ${selectedFiles.has(entry.path) ? 'selected' : ''}"
                    onClick=${(e) => handleClick(entry, e)}
                    title=${getTooltip(entry)}
                >
                    ${entry.convertible && !entry.has_chd && html`
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
                        ${entry.size != null && entry.size !== undefined && html`
                            <div class="meta">${formatSize(entry.size)}</div>
                        `}
                    </div>
                    ${entry.has_chd && html`
                        <span class="status has-chd" title="A CHD file already exists for this source">CHD exists</span>
                    `}
                    ${entry.convertible && !entry.has_chd && html`
                        <span class="status convertible" title="Can be converted to CHD">Convertible</span>
                    `}
                </li>
            `)}
        </ul>
    `;
}

function JobList({ jobs, onCancel }) {
    if (jobs.length === 0) {
        return html`
            <div class="empty-state">
                <div class="icon">⏳</div>
                <p>No conversion jobs</p>
                <p class="help-text">Select files and click Convert to start</p>
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
                        <div class="progress-text">${job.progress}%</div>
                    `}
                    ${job.message && !job.message.startsWith('temp:') && html`
                        <div class="job-message">${job.message}</div>
                    `}
                    ${job.error_message && html`
                        <div class="job-error">${job.error_message}</div>
                    `}
                    <div class="job-actions">
                        ${['queued', 'processing'].includes(job.status) && html`
                            <button class="btn btn-sm btn-secondary" onClick=${() => onCancel(job.id)} title="Cancel this job">
                                Cancel
                            </button>
                        `}
                        ${job.status === 'completed' && job.output_size && html`
                            <span class="meta" title="Size of the output CHD file">Output: ${formatSize(job.output_size)}</span>
                        `}
                        ${job.status === 'completed' && html`
                            <span class="success-icon" title="Conversion completed successfully">✓</span>
                        `}
                        ${job.status === 'failed' && html`
                            <span class="error-icon" title="Conversion failed">✗</span>
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
            setError(null);
            api.getCHDInfo(path)
                .then(setInfo)
                .catch(e => setError(e.message))
                .finally(() => setLoading(false));
        }
    }, [path]);

    if (!path) return null;

    const filename = path.split('/').pop();

    return html`
        <div class="modal-overlay" onClick=${onClose}>
            <div class="modal" onClick=${(e) => e.stopPropagation()}>
                <div class="modal-header">
                    <h3>CHD Information: ${filename}</h3>
                    <button class="modal-close" onClick=${onClose} title="Close">×</button>
                </div>
                ${loading && html`<div class="loading"><div class="spinner"></div>Loading CHD info...</div>`}
                ${error && html`
                    <div class="error-state">
                        <p>Failed to read CHD file</p>
                        <p class="error-detail">${error}</p>
                    </div>
                `}
                ${info && html`
                    <div class="info-grid">
                        <span class="info-label">File</span>
                        <span class="info-value">${filename}</span>

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
            </div>
        </div>
    `;
}

// ============ Main App ============

function App() {
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
    const [loading, setLoading] = useState(false);
    const [conversionMode, setConversionMode] = useState('createcd');
    const [outputDir, setOutputDir] = useState('');
    const [showCHDInfo, setShowCHDInfo] = useState(null);
    const [searchMode, setSearchMode] = useState(false);
    const [searchResults, setSearchResults] = useState(null);
    const [showHelp, setShowHelp] = useState(false);
    const [notification, setNotification] = useState(null);

    // Show notification
    const notify = (message, type = 'info') => {
        setNotification({ message, type });
        setTimeout(() => setNotification(null), 4000);
    };

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

                // Show notification on completion
                if (update.type === 'complete') {
                    notify(`Completed: ${newJobs[idx].filename}`, 'success');
                } else if (update.type === 'error') {
                    notify(`Failed: ${newJobs[idx].filename}`, 'error');
                }

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
            notify(`Started ${newJobs.length} conversion job(s)`, 'success');
        } catch (err) {
            notify(`Failed to create jobs: ${err.message}`, 'error');
            console.error('Failed to create jobs:', err);
        }
    };

    const handleSearch = async () => {
        if (!currentPath) return;
        setLoading(true);
        setEntriesError(null);
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

            if (combined.length === 0) {
                notify('No convertible files found', 'info');
            } else {
                notify(`Found ${combined.length} convertible file(s)`, 'success');
            }
        } catch (err) {
            setEntriesError(err.message);
            notify(`Search failed: ${err.message}`, 'error');
            console.error('Search failed:', err);
        } finally {
            setLoading(false);
        }
    };

    const handleCancelJob = async (jobId) => {
        try {
            await api.cancelJob(jobId);
            setJobs(prev => prev.map(j =>
                j.id === jobId ? { ...j, status: 'cancelled' } : j
            ));
            notify('Job cancelled', 'info');
        } catch (err) {
            notify(`Failed to cancel: ${err.message}`, 'error');
            console.error('Failed to cancel job:', err);
        }
    };

    const handleClearCompleted = () => {
        setJobs(prev => prev.filter(j => !['completed', 'failed', 'cancelled'].includes(j.status)));
    };

    const convertibleCount = entries.filter(e => e.convertible && !e.has_chd).length;
    const hasCompletedJobs = jobs.some(j => ['completed', 'failed', 'cancelled'].includes(j.status));

    return html`
        <div class="container">
            ${notification && html`
                <div class="notification ${notification.type}">
                    ${notification.message}
                </div>
            `}

            <header>
                <div>
                    <h1><span>CHD</span> Converter</h1>
                    <span class="subtitle">Convert game disc images to CHD format</span>
                </div>
                <button
                    class="btn btn-secondary help-btn"
                    onClick=${() => setShowHelp(!showHelp)}
                    title="Show help"
                >
                    ${showHelp ? 'Hide Help' : '? Help'}
                </button>
            </header>

            ${showHelp && html`<${HelpPanel} onClose=${() => setShowHelp(false)} />`}

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
                                    onClick=${() => handleNavigate(currentPath)}
                                    title="Clear search and show folder contents"
                                >
                                    ← Back
                                </button>
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

                    ${searchMode && searchResults && html`
                        <div class="search-results">
                            <h3>Found ${searchResults.total_files} file(s), ${searchResults.total_in_archives} in archives</h3>
                        </div>
                    `}

                    <div class="toolbar">
                        <select
                            value=${conversionMode}
                            onChange=${(e) => setConversionMode(e.target.value)}
                            title="Select conversion mode based on your disc type"
                        >
                            <option value="createcd">CD Mode (Dreamcast, PS1, Sega CD)</option>
                            <option value="createdvd">DVD Mode (PSP, PS2)</option>
                        </select>
                        ${convertibleCount > 0 && html`
                            <button
                                class="btn btn-sm btn-secondary"
                                onClick=${handleSelectAll}
                                title=${selectedFiles.size === convertibleCount ? 'Deselect all files' : 'Select all convertible files'}
                            >
                                ${selectedFiles.size === convertibleCount ? 'Deselect All' : `Select All (${convertibleCount})`}
                            </button>
                        `}
                        <button
                            class="btn btn-primary"
                            disabled=${selectedFiles.size === 0}
                            onClick=${handleConvert}
                            title=${selectedFiles.size > 0 ? `Convert ${selectedFiles.size} selected file(s) to CHD` : 'Select files to convert'}
                        >
                            Convert ${selectedFiles.size > 0 ? `(${selectedFiles.size})` : ''}
                        </button>
                    </div>

                    <div class="output-dir-selector">
                        <label title="Leave empty to save CHD files next to source files">Output directory:</label>
                        <input
                            type="text"
                            placeholder="Same as source (leave empty)"
                            value=${outputDir}
                            onInput=${(e) => setOutputDir(e.target.value)}
                            title="Optional: Specify a custom directory for output CHD files"
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
                                error=${entriesError}
                            />`
                        }
                    </div>
                </div>

                <!-- Jobs Panel -->
                <div class="panel jobs-panel">
                    <div class="panel-header">
                        <h2>Jobs ${jobs.length > 0 ? `(${jobs.length})` : ''}</h2>
                        <div class="header-actions">
                            ${hasCompletedJobs && html`
                                <button
                                    class="btn btn-sm btn-secondary"
                                    onClick=${handleClearCompleted}
                                    title="Remove completed, failed, and cancelled jobs from the list"
                                >
                                    Clear Done
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
