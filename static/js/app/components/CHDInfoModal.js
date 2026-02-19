import { api, isDolphinFile } from '../../api.js';
import { html, useEffect, useMemo, useState } from '../runtime/preactRuntime.js';
import { is3dsFile } from '../utils/fileTypeUtils.js';

export function CHDInfoModal({ path, onClose, infoMode, useDolphin }) {
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
                            <span class="info-value" style="font-family: monospace">${info.game_id}</span>
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

