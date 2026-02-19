import { html } from '../runtime/preactRuntime.js';

export function VolumeList({ volumes, selectedVolume, onSelect, loading, error }) {
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
