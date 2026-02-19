import { html, useState } from '../runtime/preactRuntime.js';

export function RenameModal({ entry, onRename, onClose }) {
    const [newName, setNewName] = useState(entry?.name || '');
    const [loading, setLoading] = useState(false);
    const [error, setError] = useState(null);

    if (!entry) return null;

    const handleSubmit = async (e) => {
        e.preventDefault();
        if (!newName.trim() || newName === entry.name) return;

        setLoading(true);
        setError(null);
        try {
            await onRename(entry.path, newName.trim());
            onClose();
        } catch (err) {
            setError(err.message);
        } finally {
            setLoading(false);
        }
    };

    return html`
        <div class="modal-overlay" onClick=${onClose}>
            <div class="modal" onClick=${(e) => e.stopPropagation()}>
                <div class="modal-header">
                    <h3>Rename</h3>
                    <button class="modal-close" onClick=${onClose} title="Close">×</button>
                </div>
                <form onSubmit=${handleSubmit} class="modal-body" style="padding: 15px;">
                    <p style="margin-bottom: 10px; color: var(--text-secondary);">
                        Current name: <strong>${entry.name}</strong>
                    </p>
                    <input
                        type="text"
                        value=${newName}
                        onInput=${(e) => setNewName(e.target.value)}
                        placeholder="Enter new name"
                        style="width: 100%; padding: 10px; margin-bottom: 15px; border-radius: 4px; border: 1px solid var(--border); background: var(--bg-primary); color: var(--text-primary);"
                        autoFocus
                    />
                    ${error && html`
                        <p style="color: var(--error); margin-bottom: 15px; font-size: 0.85rem;">${error}</p>
                    `}
                    <div style="display: flex; gap: 10px; justify-content: flex-end;">
                        <button type="button" class="btn btn-secondary" onClick=${onClose} disabled=${loading}>
                            Cancel
                        </button>
                        <button
                            type="submit"
                            class="btn btn-primary"
                            disabled=${loading || !newName.trim() || newName === entry.name}
                        >
                            ${loading ? 'Renaming...' : 'Rename'}
                        </button>
                    </div>
                </form>
            </div>
        </div>
    `;
}

