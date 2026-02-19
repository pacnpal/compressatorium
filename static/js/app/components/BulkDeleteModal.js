import { api } from '../../api.js';
import { html, useEffect, useState } from '../runtime/preactRuntime.js';
import { getDolphinProductPath, get3dsProductPath } from '../utils/fileTypeUtils.js';
import { getModeTerm } from '../utils/uiHelpers.js';

export function BulkDeleteModal({ entries, verifiedCHDs, onDelete, onVerify, onClose, onRefresh, isoHandling }) {
    const [step, setStep] = useState(1); // 1 = review, 2 = verifying, 3 = confirm
    const [deleting, setDeleting] = useState(false);
    const [error, setError] = useState(null);
    const [result, setResult] = useState(null);
    const [verifyState, setVerifyState] = useState({ running: false, total: 0, verified: 0, failed: 0, current: null });
    const [skipVerification, setSkipVerification] = useState(false);

    // Reset state when entries change
    const entriesKey = entries ? entries.map(e => e.path).join('|') : '';
    useEffect(() => {
        if (!entriesKey) return;
        setStep(1);
        setDeleting(false);
        setError(null);
        setResult(null);
        setVerifyState({ running: false, total: 0, verified: 0, failed: 0, current: null });
        setSkipVerification(false);
    }, [entriesKey]);

    if (!entries || entries.length === 0) return null;

    const fileTerm = getModeTerm(isoHandling, 'file');
    const verifyTerm = getModeTerm(isoHandling, 'verification');
    const productTerm = getModeTerm(isoHandling, 'product');
    const resolveSourceProduct = (entry) => {
        if (!entry || !entry.path) return null;

        const getChdPath = () => (
            entry.has_chd ? entry.path.replace(/\.[^.]+$/, '.chd') : null
        );
        const getDolphinPath = () => (
            entry.dolphin_ready ? getDolphinProductPath(entry) : null
        );
        const getZ3dsPath = () => (
            entry.z3ds_ready ? (entry.z3ds_path || get3dsProductPath(entry.path)) : null
        );

        const preferredKinds = isoHandling === 'dolphin'
            ? ['dolphin', 'chd', 'z3ds']
            : isoHandling === 'z3ds'
                ? ['z3ds', 'chd', 'dolphin']
                : ['chd', 'dolphin', 'z3ds'];

        for (const kind of preferredKinds) {
            const productPath = kind === 'dolphin'
                ? getDolphinPath()
                : kind === 'z3ds'
                    ? getZ3dsPath()
                    : getChdPath();
            if (productPath) {
                return { path: productPath, kind };
            }
        }

        return null;
    };

    // Categorize files
    const sourceFiles = entries.filter(e =>
        ['.iso', '.gdi', '.cue', '.bin', '.3ds', '.cci', '.cia'].includes(e.extension?.toLowerCase())
    );
    const chdFiles = entries.filter(e => e.extension?.toLowerCase() === '.chd');
    const dolphinFiles = entries.filter(e => ['.rvz', '.wia', '.gcz', '.wbfs'].includes(e.extension?.toLowerCase()));
    const z3dsFiles = entries.filter(e => ['.3ds', '.cci', '.cia'].includes(e.extension?.toLowerCase()));
    const archives = entries.filter(e => e.type === 'archive');
    const otherFiles = entries.filter(e =>
        !sourceFiles.includes(e) && !chdFiles.includes(e) && !dolphinFiles.includes(e) && !z3dsFiles.includes(e) && !archives.includes(e)
    );
    const sourceProductByPath = new Map();
    for (const entry of sourceFiles) {
        const product = resolveSourceProduct(entry);
        if (product) {
            sourceProductByPath.set(entry.path, product);
        }
    }

    // Check verification status for source files
    const sourceFilesWithProduct = sourceFiles.filter(e => sourceProductByPath.has(e.path));
    const unverifiedSourceFiles = sourceFilesWithProduct.filter(e => {
        const product = sourceProductByPath.get(e.path);
        return product && !verifiedCHDs.has(product.path);
    });
    const sourceFilesWithoutProduct = sourceFiles.filter(e => !sourceProductByPath.has(e.path));
    const hasUnverifiedProducts = unverifiedSourceFiles.length > 0;
    const hasDangerousDeletes = sourceFilesWithoutProduct.length > 0;

    const handleVerifyAll = async () => {
        const itemsToVerify = unverifiedSourceFiles.map(e => {
            const product = sourceProductByPath.get(e.path);
            return product ? { path: product.path, filename: e.name, kind: product.kind } : null;
        }).filter(Boolean);

        if (itemsToVerify.length === 0) {
            setStep(3);
            return;
        }

        setStep(2);
        setVerifyState({ running: true, total: itemsToVerify.length, verified: 0, failed: 0, current: null });

        try {
            // Separate by kind for batch verification
            const chdPaths = itemsToVerify.filter(item => item.kind === 'chd').map(item => item.path);
            const dolphinPaths = itemsToVerify.filter(item => item.kind === 'dolphin').map(item => item.path);
            const z3dsPaths = itemsToVerify.filter(item => item.kind === 'z3ds').map(item => item.path);

            let currentVerified = 0;
            let currentFailed = 0;

            if (chdPaths.length > 0) {
                let chdStartHandled = false;
                await api.verifyBatchCHDs(chdPaths, {
                    onProgress: (update) => {
                        if (update.type === 'start') {
                            if (!chdStartHandled && Number.isFinite(update.total)) {
                                chdStartHandled = true;
                                setVerifyState(prev => ({
                                    ...prev,
                                    total: prev.total - chdPaths.length + update.total
                                }));
                            }
                        } else if (update.type === 'progress' || update.type === 'file_progress') {
                            setVerifyState(prev => ({ ...prev, current: update.filename || update.path }));
                        }
                    },
                    onFileComplete: (data) => {
                        currentVerified += data.valid ? 1 : 0;
                        currentFailed += data.valid ? 0 : 1;
                        setVerifyState(prev => ({
                            ...prev,
                            verified: currentVerified,
                            failed: currentFailed,
                            current: null
                        }));
                        if (data.valid && onVerify) {
                            onVerify(data.path);
                        }
                    }
                });
            }

            if (dolphinPaths.length > 0) {
                let dolphinStartHandled = false;
                await api.verifyBatchDolphin(dolphinPaths, {
                    onProgress: (update) => {
                        if (update.type === 'start') {
                            if (!dolphinStartHandled && Number.isFinite(update.total)) {
                                dolphinStartHandled = true;
                                setVerifyState(prev => ({
                                    ...prev,
                                    total: prev.total - dolphinPaths.length + update.total
                                }));
                            }
                        } else if (update.type === 'progress' || update.type === 'file_progress') {
                            setVerifyState(prev => ({ ...prev, current: update.filename || update.path }));
                        }
                    },
                    onFileComplete: (data) => {
                        currentVerified += data.valid ? 1 : 0;
                        currentFailed += data.valid ? 0 : 1;
                        setVerifyState(prev => ({
                            ...prev,
                            verified: currentVerified,
                            failed: currentFailed,
                            current: null
                        }));
                        if (data.valid && onVerify) {
                            onVerify(data.path);
                        }
                    }
                });
            }

            if (z3dsPaths.length > 0) {
                let z3dsStartHandled = false;
                await api.verifyBatchZ3DS(z3dsPaths, {
                    onProgress: (update) => {
                        if (update.type === 'start') {
                            if (!z3dsStartHandled && Number.isFinite(update.total)) {
                                z3dsStartHandled = true;
                                setVerifyState(prev => ({
                                    ...prev,
                                    total: prev.total - z3dsPaths.length + update.total
                                }));
                            }
                        } else if (update.type === 'progress' || update.type === 'file_progress') {
                            setVerifyState(prev => ({ ...prev, current: update.filename || update.path }));
                        }
                    },
                    onFileComplete: (data) => {
                        currentVerified += data.valid ? 1 : 0;
                        currentFailed += data.valid ? 0 : 1;
                        setVerifyState(prev => ({
                            ...prev,
                            verified: currentVerified,
                            failed: currentFailed,
                            current: null
                        }));
                        if (data.valid && onVerify) {
                            onVerify(data.path);
                        }
                    }
                });
            }

        } catch (err) {
            setError(`Verification failed: ${err.message}`);
        } finally {
            setVerifyState(prev => ({ ...prev, running: false, current: null }));
            setStep(3);
        }
    };

    const handleDelete = async () => {
        setDeleting(true);
        setError(null);
        try {
            const paths = entries.map(e => e.path);
            const deleteResult = await api.deleteBatch(paths);
            setResult(deleteResult);
            if (deleteResult.success > 0 && onRefresh) {
                onRefresh();
            }
            if (deleteResult.failed === 0) {
                onClose();
            }
        } catch (err) {
            setError(err.message);
        } finally {
            setDeleting(false);
        }
    };

    return html`
        <div class="modal-overlay" onClick=${onClose}>
            <div class="modal" onClick=${(e) => e.stopPropagation()} style="max-width: 500px;">
                <div class="modal-header">
                    <h3 style="color: var(--error);">⚠️ Delete ${entries.length} ${fileTerm}${entries.length > 1 ? 's' : ''}</h3>
                    <button class="modal-close" onClick=${onClose} title="Close">×</button>
                </div>
                <div class="modal-body" style="padding: 15px;">
                    ${step === 1 && html`
                        <div style="max-height: 200px; overflow-y: auto; margin-bottom: 15px; padding: 10px; background: var(--bg-primary); border-radius: 4px;">
                            ${sourceFilesWithProduct.length > 0 && html`
                                <div style="margin-bottom: 10px;">
                                    <strong style="color: var(--text-primary);">Source files with ${productTerm} (${sourceFilesWithProduct.length}):</strong>
                                    ${sourceFilesWithProduct.map(e => {
        const product = sourceProductByPath.get(e.path);
        const isVerified = product && verifiedCHDs.has(product.path);
        return html`
                                            <div key=${e.path} style="font-size: 0.85rem; padding: 2px 0; color: ${isVerified ? 'var(--success)' : 'var(--warning)'};">
                                                ${isVerified ? '✓' : '⚠'} ${e.name}
                                            </div>
                                        `;
    })}
                                </div>
                            `}
                            ${sourceFilesWithoutProduct.length > 0 && html`
                                <div style="margin-bottom: 10px;">
                                    <strong style="color: var(--error);">Source files WITHOUT ${productTerm} (${sourceFilesWithoutProduct.length}):</strong>
                                    ${sourceFilesWithoutProduct.map(e => html`
                                        <div key=${e.path} style="font-size: 0.85rem; padding: 2px 0; color: var(--error);">
                                            ❌ ${e.name}
                                        </div>
                                    `)}
                                </div>
                            `}
                            ${chdFiles.length > 0 && html`
                                <div style="margin-bottom: 10px;">
                                    <strong style="color: var(--text-primary);">CHD files (${chdFiles.length}):</strong>
                                    ${chdFiles.map(e => html`
                                        <div key=${e.path} style="font-size: 0.85rem; padding: 2px 0;">💿 ${e.name}</div>
                                    `)}
                                </div>
                            `}
                            ${dolphinFiles.length > 0 && html`
                                <div style="margin-bottom: 10px;">
                                    <strong style="color: var(--text-primary);">Dolphin files (${dolphinFiles.length}):</strong>
                                    ${dolphinFiles.map(e => html`
                                        <div key=${e.path} style="font-size: 0.85rem; padding: 2px 0;">🐬 ${e.name}</div>
                                    `)}
                                </div>
                            `}
                            ${z3dsFiles.length > 0 && html`
                                <div style="margin-bottom: 10px;">
                                    <strong style="color: var(--text-primary);">3DS files (${z3dsFiles.length}):</strong>
                                    ${z3dsFiles.map(e => html`
                                        <div key=${e.path} style="font-size: 0.85rem; padding: 2px 0;">🎮 ${e.name}</div>
                                    `)}
                                </div>
                            `}
                            ${archives.length > 0 && html`
                                <div style="margin-bottom: 10px;">
                                    <strong style="color: var(--text-primary);">Archives (${archives.length}):</strong>
                                    ${archives.map(e => html`
                                        <div key=${e.path} style="font-size: 0.85rem; padding: 2px 0;">📦 ${e.name}</div>
                                    `)}
                                </div>
                            `}
                            ${otherFiles.length > 0 && html`
                                <div>
                                    <strong style="color: var(--text-primary);">Other files (${otherFiles.length}):</strong>
                                    ${otherFiles.map(e => html`
                                        <div key=${e.path} style="font-size: 0.85rem; padding: 2px 0;">📄 ${e.name}</div>
                                    `)}
                                </div>
                            `}
                        </div>

                        ${hasDangerousDeletes && html`
                            <div style="background: var(--bg-tertiary); padding: 12px; border-radius: 4px; margin-bottom: 15px; border: 1px solid var(--error);">
                                <p style="color: var(--error); margin: 0;">
                                    ⚠️ <strong>WARNING:</strong> ${sourceFilesWithoutProduct.length} source file${sourceFilesWithoutProduct.length > 1 ? 's have' : ' has'} no ${productTerm} backup. Deleting will result in data loss!
                                </p>
                            </div>
                        `}

                        ${hasUnverifiedProducts && !hasDangerousDeletes && html`
                            <div style="background: var(--bg-tertiary); padding: 12px; border-radius: 4px; margin-bottom: 15px; border: 1px solid var(--warning);">
                                <p style="color: var(--warning); margin: 0;">
                                    ⚠️ ${unverifiedSourceFiles.length} source file${unverifiedSourceFiles.length > 1 ? 's have' : ' has'} unverified ${productTerm}${unverifiedSourceFiles.length > 1 ? 's' : ''}. We recommend verifying before deletion.
                                </p>
                            </div>
                        `}

                        <p style="color: var(--text-secondary); margin-bottom: 15px;">This action cannot be undone.</p>

                        ${error && html`
                            <p style="color: var(--error); margin-bottom: 15px; font-size: 0.85rem;">${error}</p>
                        `}

                        <div style="display: flex; flex-direction: column; gap: 10px;">
                            ${hasUnverifiedProducts && html`
                                <button class="btn btn-primary" onClick=${handleVerifyAll}>
                                    🔍 Verify All ${productTerm}s First (${unverifiedSourceFiles.length})
                                </button>
                            `}
                            <button
                                class="btn ${hasUnverifiedProducts || hasDangerousDeletes ? 'btn-secondary' : 'btn-primary'}"
                                style="${hasDangerousDeletes ? 'background: var(--error);' : ''}"
                                onClick=${() => { setSkipVerification(hasUnverifiedProducts); setStep(3); }}
                            >
                                ${hasDangerousDeletes ? 'Delete Anyway (Data Loss!)' : hasUnverifiedProducts ? 'Skip Verification' : 'Continue to Delete'}
                            </button>
                            <button class="btn btn-secondary" onClick=${onClose}>Cancel</button>
                        </div>
                    `}

                    ${step === 2 && html`
                        <div style="text-align: center; padding: 20px;">
                            <div class="spinner" style="margin: 0 auto 15px;"></div>
                            <p style="color: var(--text-primary); margin-bottom: 10px;">
                                Verifying ${productTerm} files... ${verifyState.verified + verifyState.failed}/${verifyState.total}
                            </p>
                            ${verifyState.current && html`
                                <p style="color: var(--text-secondary); font-size: 0.85rem;">${verifyState.current}</p>
                            `}
                            <div style="margin-top: 15px; font-size: 0.85rem;">
                                <span style="color: var(--success);">✓ ${verifyState.verified} verified</span>
                                ${verifyState.failed > 0 && html`
                                    <span style="color: var(--error); margin-left: 15px;">✗ ${verifyState.failed} failed</span>
                                `}
                            </div>
                        </div>
                    `}

                    ${step === 3 && html`
                        ${verifyState.total > 0 && html`
                            <div style="background: var(--bg-tertiary); padding: 12px; border-radius: 4px; margin-bottom: 15px; border: 1px solid ${verifyState.failed > 0 ? 'var(--warning)' : 'var(--success)'};">
                                <p style="color: ${verifyState.failed > 0 ? 'var(--warning)' : 'var(--success)'}; margin: 0;">
                                    ${verifyState.failed > 0
                    ? `⚠️ Verification complete: ${verifyState.verified} passed, ${verifyState.failed} failed`
                    : `✓ All ${verifyState.verified} ${productTerm}${verifyState.verified > 1 ? 's' : ''} verified successfully`
                }
                                </p>
                            </div>
                        `}

                        ${skipVerification && html`
                            <div style="background: var(--bg-tertiary); padding: 12px; border-radius: 4px; margin-bottom: 15px; border: 1px solid var(--warning);">
                                <p style="color: var(--warning); margin: 0;">
                                    ⚠️ Proceeding without ${verifyTerm} verification.
                                </p>
                            </div>
                        `}

                        ${result && html`
                            <div style="background: var(--bg-tertiary); padding: 12px; border-radius: 4px; margin-bottom: 15px; border: 1px solid ${result.failed > 0 ? 'var(--warning)' : 'var(--success)'};">
                                <p style="color: ${result.failed > 0 ? 'var(--warning)' : 'var(--success)'}; margin: 0;">
                                    ${result.failed > 0
                    ? `Deleted ${result.success} file${result.success !== 1 ? 's' : ''}, ${result.failed} failed`
                    : `✓ Successfully deleted ${result.success} file${result.success !== 1 ? 's' : ''}`
                }
                                </p>
                                ${result.failed > 0 && result.results && html`
                                    <div style="margin-top: 10px; font-size: 0.85rem;">
                                        ${result.results.filter(r => !r.success).map(r => html`
                                            <div key=${r.path} style="color: var(--error);">
                                                ✗ ${r.path.split('/').pop()}: ${r.error}
                                            </div>
                                        `)}
                                    </div>
                                `}
                            </div>
                        `}

                        ${!result && html`
                            <p style="color: var(--text-secondary); margin-bottom: 15px;">
                                Confirm deletion of ${entries.length} ${fileTerm}${entries.length > 1 ? 's' : ''}?
                            </p>
                        `}

                        ${error && html`
                            <p style="color: var(--error); margin-bottom: 15px; font-size: 0.85rem;">${error}</p>
                        `}

                        <div style="display: flex; flex-direction: column; gap: 10px;">
                            ${!result && html`
                                <button
                                    class="btn btn-primary"
                                    onClick=${handleDelete}
                                    disabled=${deleting}
                                    style="${hasDangerousDeletes ? 'background: var(--error);' : ''}"
                                >
                                    ${deleting ? 'Deleting...' : `Delete ${entries.length} ${fileTerm}${entries.length > 1 ? 's' : ''}`}
                                </button>
                            `}
                            <button class="btn btn-secondary" onClick=${onClose}>
                                ${result ? 'Close' : 'Cancel'}
                            </button>
                        </div>
                    `}
                </div>
            </div>
        </div>
    `;
}

