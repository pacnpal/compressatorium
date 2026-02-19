import { api } from '../../api.js';
import { html, useEffect, useState } from '../runtime/preactRuntime.js';
import { getDolphinProductPath, get3dsProductPath } from '../utils/fileTypeUtils.js';
import { getModeTerm } from '../utils/uiHelpers.js';

export function DeleteModal({ entry, verifiedCHDs, verifyProgress, onDelete, onVerify, onClose, isoHandling }) {
    const [step, setStep] = useState(1); // 1 = initial, 2 = verification/confirm, 3 = final confirm
    const [verifying, setVerifying] = useState(false);
    const [verificationResult, setVerificationResult] = useState(null);
    const [deleting, setDeleting] = useState(false);
    const [error, setError] = useState(null);
    const [archiveScan, setArchiveScan] = useState({ loading: false, total: 0, chds: [], error: null });
    const [archiveVerify, setArchiveVerify] = useState({ running: false, total: 0, verified: 0, failed: 0, errors: [] });
    const [archiveVerifyAttempted, setArchiveVerifyAttempted] = useState(false);
    const [archiveVerifySkipped, setArchiveVerifySkipped] = useState(false);

    const entryPath = entry ? entry.path : '';
    const entryExt = entry ? entry.extension?.toLowerCase() : null;
    const isSourceFile = entry ? [
        '.iso', '.gdi', '.cue', '.bin', '.3ds', '.cci', '.cia',
    ].includes(entryExt) : false;
    const isArchive = entry ? entry.type === 'archive' : false;
    const fileTerm = getModeTerm(isoHandling, 'file');
    const resolveSourceProduct = (sourceEntry) => {
        if (!sourceEntry || !sourceEntry.path) return null;

        const getChdPath = () => (
            sourceEntry.has_chd ? sourceEntry.path.replace(/\.[^.]+$/, '.chd') : null
        );
        const getDolphinPath = () => (
            sourceEntry.dolphin_ready ? getDolphinProductPath(sourceEntry) : null
        );
        const getZ3dsPath = () => (
            sourceEntry.z3ds_ready ? (sourceEntry.z3ds_path || get3dsProductPath(sourceEntry.path)) : null
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

    const resolvedProduct = (isSourceFile && entry && !isArchive) ? resolveSourceProduct(entry) : null;
    const productPath = resolvedProduct?.path || null;
    const termsIsoHandling = resolvedProduct?.kind === 'dolphin'
        ? 'dolphin'
        : resolvedProduct?.kind === 'z3ds'
            ? 'z3ds'
            : 'chdman';
    const verifyTerm = getModeTerm(termsIsoHandling, 'verification');
    const productTerm = getModeTerm(termsIsoHandling, 'product');

    const hasProduct = Boolean(productPath);
    const isAlreadyVerified = productPath && verifiedCHDs.has(productPath);
    const verifyStatus = productPath && verifyProgress ? verifyProgress.get(productPath) : null;
    const archiveVerifiedCount = archiveScan.chds.filter((path) => verifiedCHDs.has(path)).length;
    const archiveUnverified = archiveScan.chds.filter((path) => !verifiedCHDs.has(path));
    const archiveNeedsVerify = archiveUnverified.length > 0;

    useEffect(() => {
        if (!entryPath) return;
        setStep(1);
        setVerifying(false);
        setVerificationResult(null);
        setDeleting(false);
        setError(null);
        setArchiveVerifyAttempted(false);
        setArchiveVerifySkipped(false);
        setArchiveVerify({ running: false, total: 0, verified: 0, failed: 0, errors: [] });
        setArchiveScan({ loading: false, total: 0, chds: [], error: null });
    }, [entryPath]);

    useEffect(() => {
        if (!entryPath || !isArchive) return;
        let cancelled = false;
        setArchiveScan({ loading: true, total: 0, chds: [], error: null });
        api.listArchive(entryPath)
            .then((data) => {
                if (cancelled) return;
                const files = Array.isArray(data?.files) ? data.files : [];
                const chds = files
                    .filter((file) => file.has_chd && file.chd_path)
                    .map((file) => file.chd_path);
                setArchiveScan({
                    loading: false,
                    total: data?.total ?? files.length,
                    chds,
                    error: null
                });
            })
            .catch((err) => {
                if (cancelled) return;
                setArchiveScan({
                    loading: false,
                    total: 0,
                    chds: [],
                    error: err.message || 'Failed to scan archive'
                });
            });
        return () => {
            cancelled = true;
        };
    }, [entryPath, isArchive]);

    if (!entry) return null;

    const handleVerify = async () => {
        if (!productPath) return;
        setVerifying(true);
        setError(null);
        try {
            const result = await onVerify(productPath, entry);
            setVerificationResult(result);
            if (result.valid) {
                setStep(3);
            }
        } catch (err) {
            setError(err.message);
        } finally {
            setVerifying(false);
        }
    };

    const handleArchiveVerify = async () => {
        if (archiveScan.loading || archiveVerify.running) return;
        setArchiveVerifyAttempted(true);
        setArchiveVerifySkipped(false);
        const chdPaths = (archiveScan.chds || []).filter((path) => !verifiedCHDs.has(path));
        if (chdPaths.length === 0) {
            setArchiveVerify({
                running: false,
                total: 0,
                verified: archiveScan.chds.length,
                failed: 0,
                errors: []
            });
            setStep(3);
            return;
        }

        let verified = 0;
        let failed = 0;
        const errors = [];
        setArchiveVerify({ running: true, total: chdPaths.length, verified: 0, failed: 0, errors: [] });
        for (const path of chdPaths) {
            try {
                const result = await onVerify(path);
                if (result?.valid) {
                    verified += 1;
                } else {
                    failed += 1;
                    errors.push({ path, message: result?.message || 'Verification failed' });
                }
            } catch (err) {
                failed += 1;
                errors.push({ path, message: err.message || 'Verification failed' });
            }
            setArchiveVerify((prev) => ({
                ...prev,
                verified,
                failed
            }));
        }
        setArchiveVerify({ running: false, total: chdPaths.length, verified, failed, errors });
        setStep(3);
    };

    const handleDelete = async () => {
        setDeleting(true);
        setError(null);
        try {
            await onDelete(entry.path);
            onClose();
        } catch (err) {
            setError(err.message);
            setDeleting(false);
        }
    };

    return html`
        <div class="modal-overlay" onClick=${onClose}>
            <div class="modal" onClick=${(e) => e.stopPropagation()}>
                <div class="modal-header">
                    <h3 style="color: var(--error);">⚠️ Delete File</h3>
                    <button class="modal-close" onClick=${onClose} title="Close">×</button>
                </div>
                <div class="modal-body" style="padding: 15px;">
                    <p style="margin-bottom: 15px;">
                        Are you sure you want to delete: <br/>
                        <strong style="word-break: break-all;">${entry.name}</strong>
                    </p>

                    ${step === 1 && html`
                        ${isArchive && html`
                            <div style="background: var(--bg-tertiary); padding: 12px; border-radius: 4px; margin-bottom: 15px;">
                                ${archiveScan.loading && html`
                                    <p style="color: var(--text-secondary);">Scanning archive for images and CHDs...</p>
                                `}
                                ${!archiveScan.loading && !archiveScan.error && html`
                                    <p style="color: var(--text-primary); margin-bottom: 6px;">
                                        Found ${archiveScan.total} convertible image${archiveScan.total === 1 ? '' : 's'}.
                                    </p>
                                    <p style="color: var(--text-secondary);">
                                        CHD files detected: ${archiveScan.chds.length}${archiveScan.chds.length > 0 ? ` (${archiveVerifiedCount} verified)` : ''}
                                    </p>
                                    ${archiveScan.chds.length > 0 && !archiveNeedsVerify && html`
                                        <p style="color: var(--success);">✓ All CHDs already verified</p>
                                    `}
                                `}
                                ${archiveScan.error && html`
                                    <p style="color: var(--warning);">
                                        ⚠️ Could not scan archive contents: ${archiveScan.error}
                                    </p>
                                `}
                            </div>
                        `}
                        ${isSourceFile && hasProduct && !isArchive && html`
                            <div style="background: var(--bg-tertiary); padding: 12px; border-radius: 4px; margin-bottom: 15px;">
                                <p style="color: var(--success); margin-bottom: 8px;">✓ A ${productTerm} file exists for this source</p>
                                ${isAlreadyVerified ? html`
                                    <p style="color: var(--success);">✓ ${productTerm} has been verified</p>
                                ` : html`
                                    <p style="color: var(--warning);">
                                        ⚠️ ${productTerm} has not been verified. We recommend verifying before deleting the source.
                                    </p>
                                `}
                            </div>
                        `}
                        ${isSourceFile && !hasProduct && !isArchive && html`
                            <div style="background: var(--bg-tertiary); padding: 12px; border-radius: 4px; margin-bottom: 15px; border: 1px solid var(--error);">
                                <p style="color: var(--error);">
                                    ⚠️ <strong>WARNING:</strong> No ${productTerm} file exists for this source file. Deleting it will result in data loss!
                                </p>
                            </div>
                        `}
                        <p style="color: var(--text-secondary); margin-bottom: 15px;">This action cannot be undone.</p>
                        ${error && html`
                            <p style="color: var(--error); margin-bottom: 15px; font-size: 0.85rem;">${error}</p>
                        `}
                        <div style="display: flex; flex-direction: column; gap: 10px;">
                            ${isArchive && html`
                                ${archiveVerify.running && html`
                                    <div style="color: var(--text-secondary); font-size: 0.85rem;">
                                        Verifying CHDs... ${archiveVerify.verified + archiveVerify.failed}/${archiveVerify.total}
                                    </div>
                                `}
                                ${archiveNeedsVerify && html`
                                    <button class="btn btn-primary" onClick=${handleArchiveVerify} disabled=${archiveScan.loading || archiveVerify.running}>
                                        ${archiveVerify.running ? 'Verifying CHDs...' : '🔍 Verify CHDs First'}
                                    </button>
                                `}
                                <button
                                    class="btn btn-secondary"
                                    onClick=${() => { setArchiveVerifySkipped(archiveNeedsVerify); setStep(3); }}
                                    disabled=${archiveScan.loading || archiveVerify.running}
                                >
                                    ${archiveNeedsVerify ? 'Skip Verification' : 'Continue to Delete'}
                                </button>
                            `}
                            ${isSourceFile && hasProduct && !isAlreadyVerified && !isArchive && html`
                                <button class="btn btn-primary" onClick=${handleVerify} disabled=${verifying}>
                                    ${verifying ? `Verifying ${verifyTerm}...` : `🔍 Verify ${verifyTerm} First`}
                                </button>
                            `}
                            ${verifying && verifyStatus && !isArchive && html`
                                <div style="color: var(--text-secondary); font-size: 0.85rem;">
                                    ${verifyStatus.progress != null ? `Progress: ${verifyStatus.progress}%` : (verifyStatus.message || 'Verifying...')}
                                </div>
                            `}
                            ${!isArchive && html`
                                <button
                                    class="btn btn-secondary"
                                    onClick=${() => setStep(isSourceFile && hasProduct && isAlreadyVerified ? 3 : 2)}
                                >
                                    ${isAlreadyVerified ? 'Continue to Delete' : 'Skip Verification'}
                                </button>
                            `}
                            <button class="btn btn-secondary" onClick=${onClose}>Cancel</button>
                        </div>
                    `}

                    ${step === 2 && !isArchive && html`
                        <div style="background: var(--bg-tertiary); padding: 12px; border-radius: 4px; margin-bottom: 15px; border: 1px solid var(--warning);">
                            <p style="color: var(--warning);">
                                ⚠️ You're about to delete a file without ${verifyTerm} verification.
                            </p>
                        </div>
                        ${verificationResult && !verificationResult.valid && html`
                            <div style="background: var(--bg-tertiary); padding: 12px; border-radius: 4px; margin-bottom: 15px; border: 1px solid var(--error);">
                                <p style="color: var(--error);">
                                    ❌ ${verifyTerm} verification failed: ${verificationResult.message}
                                </p>
                            </div>
                        `}
                        <p style="color: var(--text-secondary); margin-bottom: 15px;">
                            Are you <strong>absolutely sure</strong> you want to proceed?
                        </p>
                        ${error && html`
                            <p style="color: var(--error); margin-bottom: 15px; font-size: 0.85rem;">${error}</p>
                        `}
                        <div style="display: flex; flex-direction: column; gap: 10px;">
                            <button class="btn btn-secondary" style="background: var(--error);" onClick=${handleDelete} disabled=${deleting}>
                                ${deleting ? 'Deleting...' : 'Yes, Delete Anyway'}
                            </button>
                            <button class="btn btn-secondary" onClick=${onClose}>Cancel</button>
                        </div>
                    `}

                    ${step === 3 && html`
                        ${isArchive && html`
                            ${archiveVerifyAttempted && archiveVerify.failed === 0 && archiveVerify.total > 0 && html`
                                <div style="background: var(--bg-tertiary); padding: 12px; border-radius: 4px; margin-bottom: 15px; border: 1px solid var(--success);">
                                    <p style="color: var(--success);">
                                        ✓ Verified ${archiveVerify.verified} CHD${archiveVerify.verified === 1 ? '' : 's'} successfully.
                                    </p>
                                </div>
                            `}
                            ${!archiveVerifyAttempted && archiveScan.chds.length > 0 && !archiveNeedsVerify && html`
                                <div style="background: var(--bg-tertiary); padding: 12px; border-radius: 4px; margin-bottom: 15px; border: 1px solid var(--success);">
                                    <p style="color: var(--success);">
                                        ✓ All CHDs already verified.
                                    </p>
                                </div>
                            `}
                            ${(archiveVerify.failed > 0 || archiveVerifySkipped || archiveScan.error) && html`
                                <div style="background: var(--bg-tertiary); padding: 12px; border-radius: 4px; margin-bottom: 15px; border: 1px solid var(--warning);">
                                    <p style="color: var(--warning); margin-bottom: 6px;">
                                        ⚠️ Some CHDs were not verified.
                                    </p>
                                    ${archiveVerify.failed > 0 && html`
                                        <p style="color: var(--warning);">Failed verifications: ${archiveVerify.failed}</p>
                                    `}
                                    ${archiveVerifySkipped && html`
                                        <p style="color: var(--warning);">Verification was skipped.</p>
                                    `}
                                    ${archiveScan.error && html`
                                        <p style="color: var(--warning);">Archive scan failed, CHDs may be missing.</p>
                                    `}
                                </div>
                            `}
                            ${!archiveScan.error && html`
                                <p style="color: var(--text-secondary); margin-bottom: 15px;">
                                    Confirm deletion of the archive file?
                                </p>
                            `}
                            ${archiveScan.error && html`
                                <p style="color: var(--text-secondary); margin-bottom: 15px;">
                                    Archive contents could not be scanned. Delete anyway?
                                </p>
                            `}
                        `}
                        ${!isArchive && html`
                            ${verificationResult && verificationResult.valid && html`
                                <div style="background: var(--bg-tertiary); padding: 12px; border-radius: 4px; margin-bottom: 15px; border: 1px solid var(--success);">
                                    <p style="color: var(--success);">
                                        ✓ ${productTerm} verified successfully! Safe to delete source file.
                                    </p>
                                </div>
                            `}
                            ${isAlreadyVerified && !verificationResult && html`
                                <div style="background: var(--bg-tertiary); padding: 12px; border-radius: 4px; margin-bottom: 15px; border: 1px solid var(--success);">
                                    <p style="color: var(--success);">
                                        ✓ ${productTerm} was previously verified. Safe to delete source file.
                                    </p>
                                </div>
                            `}
                            <p style="color: var(--text-secondary); margin-bottom: 15px;">
                                Confirm deletion of the source file?
                            </p>
                        `}
                        ${error && html`
                            <p style="color: var(--error); margin-bottom: 15px; font-size: 0.85rem;">${error}</p>
                        `}
                        <div style="display: flex; flex-direction: column; gap: 10px;">
                            <button class="btn btn-primary" onClick=${handleDelete} disabled=${deleting}>
                                ${deleting ? 'Deleting...' : isArchive ? 'Delete Archive' : `Delete ${fileTerm}`}
                            </button>
                            <button class="btn btn-secondary" onClick=${onClose}>Cancel</button>
                        </div>
                    `}
                </div>
            </div>
        </div>
    `;
}

