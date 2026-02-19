import { html } from '../runtime/preactRuntime.js';

export function DeletePlanModal({ plan, onConfirm, onClose, verificationLabel, title }) {
    if (!plan) return null;

    const items = Array.isArray(plan.items) ? plan.items : [];
    const hasIssues = items.some(item =>
        (item.errors && item.errors.length) ||
        (item.unsafe_paths && item.unsafe_paths.length) ||
        (item.missing_paths && item.missing_paths.length)
    );

    const getBaseName = (path) => (path || '').split('/').pop() || path;

    return html`
        <div class="modal-overlay" onClick=${onClose}>
            <div class="modal" onClick=${(e) => e.stopPropagation()} style="max-width: 540px;">
                <div class="modal-header">
                    <h3>${title || 'Confirm delete after verify'}</h3>
                    <button class="modal-close" onClick=${onClose} title="Close">×</button>
                </div>
                <div class="modal-body" style="padding: 15px;">
                    <p style="color: var(--text-secondary); margin-bottom: 12px;">
                        The files below will be deleted <strong>after</strong> a successful conversion${verificationLabel ? ` and ${verificationLabel} verification` : ''}.
                    </p>
                    <div style="max-height: 240px; overflow-y: auto; padding: 10px; background: var(--bg-primary); border-radius: 4px; margin-bottom: 15px;">
                        ${items.map(item => html`
                            <div style="margin-bottom: 12px;">
                                <div style="font-weight: 600; font-size: 0.85rem; color: var(--text-primary);">
                                    ${getBaseName(item.source_path)}
                                </div>
                                ${(item.delete_paths || []).map(p => html`
                                    <div style="font-size: 0.8rem; color: var(--text-secondary);">${p}</div>
                                `)}
                                ${item.warnings && item.warnings.length > 0 && html`
                                    <div style="font-size: 0.8rem; color: var(--warning); margin-top: 4px;">
                                        ${item.warnings.join('; ')}
                                    </div>
                                `}
                                ${item.missing_paths && item.missing_paths.length > 0 && html`
                                    <div style="font-size: 0.8rem; color: var(--warning); margin-top: 4px;">
                                        Missing: ${item.missing_paths.map(getBaseName).join(', ')}
                                    </div>
                                `}
                                ${item.unsafe_paths && item.unsafe_paths.length > 0 && html`
                                    <div style="font-size: 0.8rem; color: var(--error); margin-top: 4px;">
                                        Unsafe references: ${item.unsafe_paths.join('; ')}
                                    </div>
                                `}
                                ${item.errors && item.errors.length > 0 && html`
                                    <div style="font-size: 0.8rem; color: var(--error); margin-top: 4px;">
                                        ${item.errors.join('; ')}
                                    </div>
                                `}
                            </div>
                        `)}
                    </div>
                    ${hasIssues && html`
                        <p style="color: var(--error); margin-bottom: 12px; font-size: 0.85rem;">
                            Delete-on-verify is blocked due to missing or unsafe paths. Fix the sources or disable the option to continue.
                        </p>
                    `}
                    <div style="display: flex; gap: 10px; justify-content: flex-end;">
                        <button class="btn btn-secondary" onClick=${onClose}>
                            Cancel
                        </button>
                        <button class="btn btn-primary" onClick=${onConfirm} disabled=${hasIssues}>
                            Confirm compress + delete
                        </button>
                    </div>
                </div>
            </div>
        </div>
    `;
}

