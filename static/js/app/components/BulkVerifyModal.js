import { api } from '../../api.js';
import { html, useEffect, useState } from '../runtime/preactRuntime.js';

export function BulkVerifyModal({ verifyItems, onComplete, onClose }) {
    const [state, setState] = useState({
        running: false,
        total: 0,
        verified: 0,
        failed: 0,
        current: null,
        currentProgress: null,
        results: [],
        error: null,
        complete: false
    });

    // Reset and start verification when paths change
    const pathsKey = verifyItems ? verifyItems.map(item => item.path).join('|') : '';
    useEffect(() => {
        if (!pathsKey || verifyItems.length === 0) return;

        let cancelled = false;

        const runVerification = async () => {
            setState({
                running: true,
                total: verifyItems.length,
                verified: 0,
                failed: 0,
                current: null,
                currentProgress: null,
                results: [],
                error: null,
                complete: false
            });

            try {
                const allChd = verifyItems.every(item => item.kind === 'chd');
                const allDolphin = verifyItems.every(item => item.kind === 'dolphin');
                const allZ3DS = verifyItems.every(item => item.kind === 'z3ds');
                let result = { verified: 0, failed: 0, total: verifyItems.length };

                if (allChd) {
                    const chdPaths = verifyItems.map(item => item.path);
                    result = await api.verifyBatchCHDs(chdPaths, {
                        onProgress: (update) => {
                            if (cancelled) return;
                            if (update.type === 'start') {
                                // Use server-validated total (may be less than client count if paths were filtered)
                                setState(prev => ({
                                    ...prev,
                                    total: update.total
                                }));
                            } else if (update.type === 'progress') {
                                setState(prev => ({
                                    ...prev,
                                    current: update.filename
                                }));
                            } else if (update.type === 'file_progress') {
                                setState(prev => ({
                                    ...prev,
                                    current: update.filename,
                                    currentProgress: update.progress
                                }));
                            }
                        },
                        onFileComplete: (data) => {
                            if (cancelled) return;
                            setState(prev => ({
                                ...prev,
                                verified: data.verified,
                                failed: data.failed,
                                current: null,
                                currentProgress: null,
                                results: [...prev.results, data]
                            }));
                        }
                    });
                } else if (allDolphin) {
                    const dolphinPaths = verifyItems.map(item => item.path);
                    result = await api.verifyBatchDolphin(dolphinPaths, {
                        onProgress: (update) => {
                            if (cancelled) return;
                            if (update.type === 'start') {
                                // Use server-validated total (may be less than client count if paths were filtered)
                                setState(prev => ({
                                    ...prev,
                                    total: update.total
                                }));
                            } else if (update.type === 'progress' || update.type === 'file_progress') {
                                setState(prev => ({
                                    ...prev,
                                    current: update.filename || update.path
                                }));
                            }
                        },
                        onFileComplete: (data) => {
                            if (cancelled) return;
                            setState(prev => ({
                                ...prev,
                                verified: data.verified,
                                failed: data.failed,
                                current: null,
                                currentProgress: null,
                                results: [...prev.results, data]
                            }));
                        }
                    });
                } else if (allZ3DS) {
                    const z3dsPaths = verifyItems.map(item => item.path);
                    result = await api.verifyBatchZ3DS(z3dsPaths, {
                        onProgress: (update) => {
                            if (cancelled) return;
                            if (update.type === 'start') {
                                setState(prev => ({
                                    ...prev,
                                    total: update.total
                                }));
                            } else if (update.type === 'progress' || update.type === 'file_progress') {
                                setState(prev => ({
                                    ...prev,
                                    current: update.filename || update.path,
                                    currentProgress: update.progress
                                }));
                            }
                        },
                        onFileComplete: (data) => {
                            if (cancelled) return;
                            setState(prev => ({
                                ...prev,
                                verified: data.verified,
                                failed: data.failed,
                                current: null,
                                currentProgress: null,
                                results: [...prev.results, data]
                            }));
                        }
                    });
                } else {
                    const chdPaths = verifyItems.filter(item => item.kind === 'chd').map(item => item.path);
                    const dolphinPaths = verifyItems.filter(item => item.kind === 'dolphin').map(item => item.path);
                    const z3dsPaths = verifyItems.filter(item => item.kind === 'z3ds').map(item => item.path);

                    let verified = 0;
                    let failed = 0;
                    let chdTotal = chdPaths.length;
                    let dolphinTotal = dolphinPaths.length;
                    let z3dsTotal = z3dsPaths.length;

                    const runBatch = async (kind, paths) => {
                        const baseVerified = verified;
                        const baseFailed = failed;

                        let verifyFn;
                        if (kind === 'dolphin') verifyFn = api.verifyBatchDolphin.bind(api);
                        else if (kind === 'z3ds') verifyFn = api.verifyBatchZ3DS.bind(api);
                        else verifyFn = api.verifyBatchCHDs.bind(api);

                        const batchResult = await verifyFn(paths, {
                            onProgress: (update) => {
                                if (cancelled) return;
                                if (update.type === 'start') {
                                    if (kind === 'dolphin') {
                                        dolphinTotal = update.total;
                                    } else if (kind === 'z3ds') {
                                        z3dsTotal = update.total;
                                    } else {
                                        chdTotal = update.total;
                                    }
                                    setState(prev => ({
                                        ...prev,
                                        total: chdTotal + dolphinTotal + z3dsTotal
                                    }));
                                } else if (update.type === 'progress') {
                                    setState(prev => ({
                                        ...prev,
                                        current: update.filename
                                    }));
                                } else if (update.type === 'file_progress') {
                                    setState(prev => ({
                                        ...prev,
                                        current: update.filename,
                                        currentProgress: update.progress
                                    }));
                                }
                            },
                            onFileComplete: (data) => {
                                if (cancelled) return;
                                const cumulativeVerified = baseVerified + data.verified;
                                const cumulativeFailed = baseFailed + data.failed;
                                verified = cumulativeVerified;
                                failed = cumulativeFailed;
                                setState(prev => ({
                                    ...prev,
                                    verified: cumulativeVerified,
                                    failed: cumulativeFailed,
                                    current: null,
                                    currentProgress: null,
                                    results: [...prev.results, data]
                                }));
                            }
                        });

                        verified = baseVerified + batchResult.verified;
                        failed = baseFailed + batchResult.failed;
                        setState(prev => ({
                            ...prev,
                            verified,
                            failed,
                            current: null,
                            currentProgress: null
                        }));
                    };

                    if (chdPaths.length > 0) {
                        await runBatch('chd', chdPaths);
                    }
                    if (dolphinPaths.length > 0) {
                        await runBatch('dolphin', dolphinPaths);
                    }
                    if (z3dsPaths.length > 0) {
                        await runBatch('z3ds', z3dsPaths);
                    }

                    result = { verified, failed, total: chdTotal + dolphinTotal + z3dsTotal };
                }

                if (cancelled) return;
                setState(prev => ({
                    ...prev,
                    running: false,
                    complete: true,
                    verified: result.verified,
                    failed: result.failed
                }));

                if (onComplete) {
                    onComplete(result);
                }
            } catch (err) {
                if (cancelled) return;
                setState(prev => ({
                    ...prev,
                    running: false,
                    error: err.message
                }));
            }
        };

        runVerification();

        return () => {
            cancelled = true;
        };
    }, [pathsKey]);

    if (!verifyItems || verifyItems.length === 0) return null;

    return html`
        <div class="modal-overlay" onClick=${state.running ? null : onClose}>
            <div class="modal" onClick=${(e) => e.stopPropagation()} style="max-width: 500px;">
                <div class="modal-header">
                    <h3>🔍 Verify ${verifyItems.length} File${verifyItems.length > 1 ? 's' : ''}</h3>
                    ${!state.running && html`
                        <button class="modal-close" onClick=${onClose} title="Close">×</button>
                    `}
                </div>
                <div class="modal-body" style="padding: 15px;">
                    ${state.running && html`
                        <div style="text-align: center; padding: 20px;">
                            <div class="spinner" style="margin: 0 auto 15px;"></div>
                            <p style="color: var(--text-primary); margin-bottom: 10px;">
                                Verifying files... ${state.verified + state.failed}/${state.total}
                            </p>
                            ${state.current && html`
                                <p style="color: var(--text-secondary); font-size: 0.85rem; margin-bottom: 5px;">
                                    ${state.current}
                                </p>
                            `}
                            ${state.currentProgress != null && html`
                                <div style="width: 100%; height: 4px; background: var(--bg-tertiary); border-radius: 2px; overflow: hidden;">
                                    <div style="width: ${state.currentProgress}%; height: 100%; background: var(--accent); transition: width 0.3s;"></div>
                                </div>
                            `}
                        </div>
                        <div style="margin-top: 15px; text-align: center; font-size: 0.85rem;">
                            <span style="color: var(--success);">✓ ${state.verified} verified</span>
                            ${state.failed > 0 && html`
                                <span style="color: var(--error); margin-left: 15px;">✗ ${state.failed} failed</span>
                            `}
                        </div>
                    `}

                    ${state.complete && html`
                        <div style="background: var(--bg-tertiary); padding: 12px; border-radius: 4px; margin-bottom: 15px; border: 1px solid ${state.failed > 0 ? 'var(--warning)' : 'var(--success)'};">
                            <p style="color: ${state.failed > 0 ? 'var(--warning)' : 'var(--success)'}; margin: 0; font-weight: bold;">
                                ${state.failed > 0
                ? `Verification complete: ${state.verified} passed, ${state.failed} failed`
                : `✓ All ${state.verified} file${state.verified > 1 ? 's' : ''} verified successfully!`
            }
                            </p>
                        </div>

                        ${state.results.length > 0 && html`
                            <div style="max-height: 200px; overflow-y: auto; padding: 10px; background: var(--bg-primary); border-radius: 4px;">
                                ${state.results.map(r => html`
                                    <div key=${r.path} style="font-size: 0.85rem; padding: 4px 0; color: ${r.valid ? 'var(--success)' : 'var(--error)'};">
                                        ${r.valid ? '✓' : '✗'} ${r.filename}
                                        ${!r.valid && r.message && html`
                                            <span style="color: var(--text-secondary);"> - ${r.message}</span>
                                        `}
                                    </div>
                                `)}
                            </div>
                        `}

                        <div style="margin-top: 15px;">
                            <button class="btn btn-primary" onClick=${onClose} style="width: 100%;">
                                Close
                            </button>
                        </div>
                    `}

                    ${state.error && html`
                        <div style="background: var(--bg-tertiary); padding: 12px; border-radius: 4px; margin-bottom: 15px; border: 1px solid var(--error);">
                            <p style="color: var(--error); margin: 0;">
                                ✗ Error: ${state.error}
                            </p>
                        </div>
                        <button class="btn btn-secondary" onClick=${onClose} style="width: 100%;">
                            Close
                        </button>
                    `}
                </div>
            </div>
        </div>
    `;
}

