import { api } from '../../../api.js';
import { html, useCallback, useEffect, useState } from '../../runtime/preactRuntime.js';

export function IgirJobCard({ job, onCancel }) {
    const [tick, setTick] = useState(0);
    const [logData, setLogData] = useState(null);
    const [logLoading, setLogLoading] = useState(false);
    const [logOpen, setLogOpen] = useState(false);

    const isActive = job.status === 'queued' || job.status === 'processing';
    const isDone = job.status === 'completed' || job.status === 'failed' || job.status === 'cancelled';

    useEffect(() => {
        if (!isActive || !job.started_at) return;
        const id = setInterval(() => setTick(t => t + 1), 1000);
        return () => clearInterval(id);
    }, [isActive, job.started_at]);

    const loadLog = useCallback(async () => {
        if (logData || logLoading) return;
        setLogLoading(true);
        try {
            const data = await api.getIgirJobLog(job.id);
            setLogData(data);
        } catch (e) {
            setLogData({ lines: [], line_count: 0, error: e.message });
        } finally {
            setLogLoading(false);
        }
    }, [job.id, logData, logLoading]);

    const handleLogToggle = useCallback(() => {
        const next = !logOpen;
        setLogOpen(next);
        if (next && !logData && !logLoading) loadLog();
    }, [logOpen, logData, logLoading, loadLog]);

    const elapsed = job.started_at && !job.completed_at
        ? Math.floor((Date.now() - new Date(job.started_at).getTime()) / 1000)
        : job.started_at && job.completed_at
            ? Math.floor((new Date(job.completed_at).getTime() - new Date(job.started_at).getTime()) / 1000)
            : 0;
    const mins = Math.floor(elapsed / 60);
    const secs = elapsed % 60;
    const elapsedStr = mins > 0 ? `${mins}m ${secs}s` : `${secs}s`;
    const statusClass = job.status === 'completed' ? 'success'
        : job.status === 'failed' ? 'error'
            : job.status === 'cancelled' ? 'warning'
                : '';

    return html`
        <div class="igir-job-card ${statusClass}">
            <div class="igir-job-header">
                <div class="igir-job-commands">
                    ${job.commands.map(cmd => html`<span class="igir-cmd-badge">${cmd}</span>`)}
                </div>
                <span class="igir-job-status ${job.status}">${job.status}</span>
            </div>
            <div class="igir-job-paths">
                <span class="igir-job-input" title=${(job.input_paths || []).join(', ')}>
                    Input: ${(job.input_paths || []).map(p => p.split('/').pop()).join(', ')}
                </span>
                ${job.output_path && html`
                    <span class="igir-job-output" title=${job.output_path}>
                        Output: ${job.output_path.split('/').pop()}
                    </span>
                `}
            </div>
            ${job.command_preview && html`
                <details class="igir-job-cmd-details">
                    <summary>Command</summary>
                    <pre class="igir-job-cmd-preview">${job.command_preview}</pre>
                </details>
            `}
            ${job.status === 'processing' && html`
                <div class="igir-job-progress">
                    <div class="progress-bar">
                        <div class="progress-fill" style="width: ${job.progress}%"></div>
                    </div>
                    <div class="igir-job-progress-info">
                        <span>${job.progress}%</span>
                        ${job.phase && html`<span class="igir-job-phase">${job.phase}</span>`}
                        ${job.files_total > 0 && html`
                            <span>${job.files_processed}/${job.files_total} files</span>
                        `}
                        <span>${elapsedStr}</span>
                    </div>
                </div>
            `}
            ${job.message && html`<div class="igir-job-message">${job.message}</div>`}
            ${job.error_message && html`<div class="igir-job-error">${job.error_message}</div>`}
            ${job.status === 'completed' && elapsed > 0 && html`
                <div class="igir-job-elapsed">Completed in ${elapsedStr}</div>
            `}
            ${job.report_output && html`
                <details class="igir-job-report-details">
                    <summary>Report Output</summary>
                    <pre class="igir-job-report-output">${job.report_output}</pre>
                </details>
            `}
            ${job.clean_dry_run_results && job.clean_dry_run_results.length > 0 && html`
                <details class="igir-job-report-details">
                    <summary>Clean Dry Run Results (${job.clean_dry_run_results.length})</summary>
                    <pre class="igir-job-report-output">${job.clean_dry_run_results.join('\n')}</pre>
                </details>
            `}
            ${isDone && html`
                <div class="igir-job-log-section">
                    <button class="btn btn-sm btn-secondary igir-job-log-btn" onClick=${handleLogToggle}>
                        ${logOpen ? 'Hide' : 'View'} Output Log${logLoading ? '...' : ''}
                    </button>
                    ${logOpen && logData && html`
                        <div class="igir-job-output-log-wrapper">
                            <div class="igir-job-log-header">${logData.line_count} line${logData.line_count !== 1 ? 's' : ''}</div>
                            <pre class="igir-job-output-log">${logData.lines.join('\n') || '(no output captured)'}</pre>
                        </div>
                    `}
                </div>
            `}
            ${job.options_summary && html`<div class="igir-job-summary">${job.options_summary}</div>`}
            ${isActive && html`
                <button class="btn btn-sm btn-secondary igir-job-cancel" onClick=${() => onCancel(job.id)}>
                    Cancel
                </button>
            `}
        </div>
    `;
}

