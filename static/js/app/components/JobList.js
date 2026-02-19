import { html } from '../runtime/preactRuntime.js';
import { formatSize } from '../../api.js';

export function JobList({
    jobs,
    onCancel,
    emptyTitle = 'No conversion jobs',
    emptyHelpText = 'Select files and click Convert to queue jobs',
}) {
    if (jobs.length === 0) {
        return html`
            <div class="empty-state">
                <div class="icon">⏳</div>
                <p>${emptyTitle}</p>
                <p class="help-text">${emptyHelpText}</p>
            </div>
        `;
    }

    const getStatusText = (job) => {
        switch (job.status) {
            case 'creating': return 'Creating job...';
            case 'queued': return 'Waiting in queue';
            case 'processing': return `Processing: ${job.progress}%`;
            case 'completed': return 'Completed';
            case 'failed': return 'Failed';
            case 'cancelled': return 'Cancelled';
            default: return job.status;
        }
    };

    const getStatusIcon = (job) => {
        switch (job.status) {
            case 'creating': return '⏳';
            case 'queued': return '⏸️';
            case 'processing': return '⚙️';
            case 'completed': return '✅';
            case 'failed': return '❌';
            case 'cancelled': return '🚫';
            default: return '📄';
        }
    };

    const getOutputDir = (path) => {
        if (!path) return 'Unknown';
        const parts = path.split('/');
        parts.pop(); // Remove filename
        return parts.length > 0 ? parts.join('/') : '/';
    };

    const getOutputFilename = (path) => {
        if (!path) return 'Unknown';
        return path.split('/').pop();
    };

    return html`
        <ul class="job-list">
            ${jobs.map(job => html`
                <li key=${job.id} class="job-item">
                    <div class="job-header">
                        <span class="job-status-icon" title=${getStatusText(job)}>${getStatusIcon(job)}</span>
                        <span class="job-name" title=${job.file_path}>${job.filename}</span>
                        <span class="job-status ${job.status}">${job.status}</span>
                    </div>

                    ${job.output_path && html`
                        <div class="job-output-info" style="font-size: 0.75rem; color: var(--text-secondary); margin: 4px 0; padding-left: 24px;">
                            <span title="Output: ${job.output_path}">→ ${getOutputFilename(job.output_path)}</span>
                            <span style="opacity: 0.7;"> in ${getOutputDir(job.output_path)}</span>
                        </div>
                    `}

                    ${job.status === 'creating' && html`
                        <div class="progress-bar">
                            <div class="progress-fill creating" style="width: 100%; animation: pulse 1.5s infinite;"></div>
                        </div>
                        <div class="progress-text" style="color: var(--text-secondary);">
                            Setting up job...
                        </div>
                    `}

                    ${job.status === 'queued' && html`
                        <div class="progress-text" style="color: var(--text-secondary);">
                            Waiting for other jobs to complete...
                        </div>
                    `}

                    ${job.status === 'processing' && html`
                        <div class="progress-bar">
                            <div class="progress-fill" style="width: ${job.progress}%"></div>
                        </div>
                        <div class="progress-text">
                            ${job.progress}% - ${job.message || 'Processing...'}
                        </div>
                    `}

                    ${job.status === 'completed' && html`
                        <div class="job-success" style="color: var(--success); font-size: 0.8rem; padding-left: 24px;">
                            Job complete${job.output_size ? ` - ${formatSize(job.output_size)}` : ''}
                        </div>
                    `}

                    ${job.error_message && html`
                        <div class="job-error" style="padding-left: 24px;">${job.error_message}</div>
                    `}

                    <div class="job-actions">
                        ${['queued', 'processing'].includes(job.status) && html`
                            <button class="btn btn-sm btn-secondary" onClick=${() => onCancel(job.id)} title="Cancel this job">
                                Cancel
                            </button>
                        `}
                    </div>
                </li>
            `)}
        </ul>
    `;
}

