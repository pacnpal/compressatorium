import { html } from '../runtime/preactRuntime.js';

export function ClearDoneModal({ total, onConfirm, onClose, busy }) {
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

