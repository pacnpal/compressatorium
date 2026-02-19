import { html, useState } from '../runtime/preactRuntime.js';

export function AutoQueueCapModal({ total, recommendedCap, onConfirmCap, onConfirmAll, onClose, busy }) {
    if (!total || total <= 0) return null;

    const initialCap = Math.max(1, Math.min(recommendedCap || total, total));
    const [capValue, setCapValue] = useState(String(initialCap));
    const parsedCap = Number.parseInt(capValue, 10);
    const normalizedCap = Number.isFinite(parsedCap)
        ? Math.max(1, Math.min(parsedCap, total))
        : null;
    const canQueueCap = !busy && normalizedCap != null;

    return html`
        <div class="modal-overlay" onClick=${busy ? null : onClose}>
            <div class="modal" onClick=${(e) => e.stopPropagation()} style="max-width: 500px;">
                <div class="modal-header">
                    <h3>Auto Queue Folder</h3>
                    ${!busy && html`<button class="modal-close" onClick=${onClose} title="Close">×</button>`}
                </div>
                <div class="modal-body" style="padding: 15px;">
                    <p style="margin-bottom: 12px; color: var(--text-secondary);">
                        Found <strong>${total}</strong> compatible file(s). Queueing all at once may create a long backlog.
                    </p>
                    <p style="margin-bottom: 12px; color: var(--text-secondary);">
                        Optionally cap how many files to queue now.
                    </p>
                    <div style="margin-bottom: 15px;">
                        <label style="display: block; font-size: 0.85rem; color: var(--text-secondary); margin-bottom: 6px;">
                            Queue first N files
                        </label>
                        <input
                            type="number"
                            min="1"
                            max=${total}
                            value=${capValue}
                            onInput=${(e) => setCapValue(e.target.value)}
                            style="width: 100%; padding: 10px; border-radius: 4px; border: 1px solid var(--border); background: var(--bg-primary); color: var(--text-primary);"
                            disabled=${busy}
                        />
                        <div style="margin-top: 6px; color: var(--text-secondary); font-size: 0.8rem;">
                            Recommended: ${initialCap}
                        </div>
                    </div>
                    <div style="display: flex; gap: 10px; justify-content: flex-end;">
                        <button class="btn btn-secondary" onClick=${onClose} disabled=${busy}>
                            Cancel
                        </button>
                        <button class="btn btn-secondary" onClick=${onConfirmAll} disabled=${busy}>
                            ${busy ? 'Queueing...' : 'Queue All'}
                        </button>
                        <button
                            class="btn btn-primary"
                            onClick=${() => onConfirmCap(normalizedCap)}
                            disabled=${!canQueueCap}
                        >
                            ${busy ? 'Queueing...' : 'Queue with Cap'}
                        </button>
                    </div>
                </div>
            </div>
        </div>
    `;
}

