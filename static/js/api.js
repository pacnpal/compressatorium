// API client for CHD Converter

const API_BASE = '/api';

export const api = {
    // Volumes
    async getVolumes() {
        const res = await fetch(`${API_BASE}/volumes`);
        if (!res.ok) throw new Error('Failed to fetch volumes');
        return res.json();
    },

    // Files
    async listFiles(path, showArchives = true) {
        const params = new URLSearchParams({ path, show_archives: showArchives });
        const res = await fetch(`${API_BASE}/files?${params}`);
        if (!res.ok) throw new Error('Failed to list files');
        return res.json();
    },

    async searchFiles(path, recursive = true, includeArchives = true) {
        const params = new URLSearchParams({
            path,
            recursive,
            include_archives: includeArchives
        });
        const res = await fetch(`${API_BASE}/files/search?${params}`);
        if (!res.ok) throw new Error('Failed to search files');
        return res.json();
    },

    async listArchive(path) {
        const params = new URLSearchParams({ path });
        const res = await fetch(`${API_BASE}/files/archive?${params}`);
        if (!res.ok) throw new Error('Failed to list archive');
        return res.json();
    },

    // Jobs
    async createJob(filePath, mode = 'createcd', outputDir = null) {
        const res = await fetch(`${API_BASE}/jobs`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                file_path: filePath,
                mode,
                output_dir: outputDir
            })
        });
        if (!res.ok) throw new Error('Failed to create job');
        return res.json();
    },

    async createBatchJobs(filePaths, mode = 'createcd', outputDir = null, duplicateAction = 'skip') {
        const res = await fetch(`${API_BASE}/jobs/batch`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                file_paths: filePaths,
                mode,
                output_dir: outputDir,
                duplicate_action: duplicateAction
            })
        });
        if (!res.ok) throw new Error('Failed to create jobs');
        return res.json();
    },

    async checkDuplicates(filePaths, outputDir = null) {
        const res = await fetch(`${API_BASE}/jobs/check-duplicates`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                file_paths: filePaths,
                output_dir: outputDir
            })
        });
        if (!res.ok) throw new Error('Failed to check duplicates');
        return res.json();
    },

    async getJobs() {
        const res = await fetch(`${API_BASE}/jobs`);
        if (!res.ok) throw new Error('Failed to fetch jobs');
        return res.json();
    },

    async getJob(jobId) {
        const res = await fetch(`${API_BASE}/jobs/${jobId}`);
        if (!res.ok) throw new Error('Failed to fetch job');
        return res.json();
    },

    async cancelJob(jobId) {
        const res = await fetch(`${API_BASE}/jobs/${jobId}`, { method: 'DELETE' });
        if (!res.ok) throw new Error('Failed to cancel job');
        return res.json();
    },

    async deleteCompletedJobs() {
        const res = await fetch(`${API_BASE}/jobs/completed`, { method: 'DELETE' });
        if (!res.ok) throw new Error('Failed to delete completed jobs');
        return res.json();
    },

    subscribeToJobs(onUpdate) {
        const eventSource = new EventSource(`${API_BASE}/jobs/events`);

        const safeParseAndUpdate = (type, e) => {
            try {
                if (e.data) {
                    onUpdate({ type, data: JSON.parse(e.data) });
                }
            } catch (err) {
                console.error(`Failed to parse SSE ${type} event:`, err, e.data);
            }
        };

        eventSource.addEventListener('progress', (e) => safeParseAndUpdate('progress', e));
        eventSource.addEventListener('complete', (e) => safeParseAndUpdate('complete', e));
        eventSource.addEventListener('error', (e) => safeParseAndUpdate('error', e));
        eventSource.addEventListener('status', (e) => safeParseAndUpdate('status', e));

        eventSource.onerror = (err) => {
            console.error('SSE connection error:', err);
            // Don't close - EventSource will auto-reconnect
        };

        return () => eventSource.close();
    },

    // CHD Info
    async getCHDInfo(path) {
        const params = new URLSearchParams({ path });
        const res = await fetch(`${API_BASE}/info?${params}`);
        if (!res.ok) throw new Error('Failed to get CHD info');
        return res.json();
    }
};

// Format file size
export function formatSize(bytes) {
    if (bytes == null || bytes === undefined) return '';
    if (bytes === 0) return '0 B';
    const k = 1024;
    const sizes = ['B', 'KB', 'MB', 'GB', 'TB'];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return parseFloat((bytes / Math.pow(k, i)).toFixed(2)) + ' ' + sizes[i];
}

// Get file icon
export function getFileIcon(entry) {
    if (entry.type === 'directory') return '📁';
    if (entry.type === 'archive') return '📦';
    const ext = entry.extension?.toLowerCase();
    if (ext === '.chd') return '💿';
    if (['.iso', '.gdi', '.cue', '.bin'].includes(ext)) return '💽';
    return '📄';
}
