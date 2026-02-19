import { api } from '../../../api.js';
import { html, useCallback, useState } from '../../runtime/preactRuntime.js';

export function IgirDirectoryPicker({ volumes, label, selectedPaths, onAddPath, onRemovePath, multiple }) {
    const [browseVolume, setBrowseVolume] = useState(null);
    const [browsePath, setBrowsePath] = useState(null);
    const [browseEntries, setBrowseEntries] = useState([]);
    const [browseLoading, setBrowseLoading] = useState(false);
    const [showBrowser, setShowBrowser] = useState(false);

    const handleBrowse = useCallback((path) => {
        setBrowseLoading(true);
        setBrowsePath(path);
        api.listFiles(path, false).then(data => {
            const dirs = (data.entries || []).filter(e => e.type === 'directory');
            setBrowseEntries(dirs);
            setBrowseLoading(false);
        }).catch(() => setBrowseLoading(false));
    }, []);

    const handleSelectVolume = useCallback((vol) => {
        setBrowseVolume(vol);
        handleBrowse(vol.path);
    }, [handleBrowse]);

    const handleSelectDir = useCallback(() => {
        if (browsePath) {
            onAddPath(browsePath);
            if (!multiple) setShowBrowser(false);
        }
    }, [browsePath, onAddPath, multiple]);

    return html`
        <div class="igir-dir-picker">
            <div class="igir-dir-picker-label">${label}</div>
            <div class="igir-dir-picker-selected">
                ${selectedPaths.length === 0 && html`<span class="igir-dir-none">None selected</span>`}
                ${selectedPaths.map(p => html`
                    <div class="igir-dir-selected-item" key=${p}>
                        <span class="igir-dir-path" title=${p}>${p}</span>
                        <button class="btn btn-sm btn-secondary" onClick=${() => onRemovePath(p)} title="Remove">✕</button>
                    </div>
                `)}
            </div>
            <button class="btn btn-sm btn-secondary" onClick=${() => setShowBrowser(!showBrowser)}>
                ${showBrowser ? 'Close Browser' : `Browse ${label}...`}
            </button>
            ${showBrowser && html`
                <div class="igir-dir-browser">
                    <div class="igir-dir-volumes">
                        ${volumes.map(v => html`
                            <button
                                key=${v.path}
                                class=${`btn btn-sm ${browseVolume?.path === v.path ? 'btn-primary' : 'btn-secondary'}`}
                                onClick=${() => handleSelectVolume(v)}
                            >
                                ${v.name}
                            </button>
                        `)}
                    </div>
                    ${browsePath && html`
                        <div class="igir-dir-breadcrumb">
                            ${browsePath}
                        </div>
                    `}
                    ${browseLoading
                        ? html`<div class="loading"><div class="spinner"></div></div>`
                        : html`
                            <div class="igir-dir-list">
                                ${browsePath && browseVolume && browsePath !== browseVolume.path && html`
                                    <div
                                        class="igir-dir-entry"
                                        onClick=${() => {
                                            const parent = browsePath.split('/').slice(0, -1).join('/');
                                            handleBrowse(parent || browseVolume.path);
                                        }}
                                    >
                                        <span>📁 ..</span>
                                    </div>
                                `}
                                ${browseEntries.map(entry => html`
                                    <div
                                        class="igir-dir-entry"
                                        key=${entry.path}
                                        onClick=${() => handleBrowse(entry.path)}
                                    >
                                        <span>📁 ${entry.name}</span>
                                    </div>
                                `)}
                                ${browseEntries.length === 0 && html`<div class="igir-dir-empty">No subdirectories</div>`}
                            </div>
                            <button class="btn btn-sm btn-primary igir-dir-select-btn" onClick=${handleSelectDir}>
                                Select This Directory
                            </button>
                        `
                    }
                </div>
            `}
        </div>
    `;
}

