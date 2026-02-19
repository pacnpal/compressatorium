import { html } from '../runtime/preactRuntime.js';

export function CancelAllJobsModal({ total, queued, processing, onConfirm, onClose, busy }) {
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

