import { html } from '../runtime/preactRuntime.js';

export function DuplicateModal({ duplicates, onAction, onClose }) {
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

