// API client for Compressatorium

const API_BASE = '/api';

/**
 * Build a validated same-origin API URL using the URL constructor.
 * @param {string} endpoint - API endpoint path (e.g., '/files')
 * @param {URLSearchParams|string} [query] - Optional query string or URLSearchParams
 * @returns {string} Relative API URL string
 */
function buildApiUrl(endpoint, query) {
    const url = new URL(API_BASE + endpoint, window.location.origin);
    if (query instanceof URLSearchParams) {
        url.search = query.toString();
    } else if (typeof query === 'string' && query) {
        url.search = query;
    }
    return url.pathname + url.search;
}

export const api = {
    // Version
    async getVersion() {
        const res = await fetch(`${API_BASE}/version`);
        if (!res.ok) throw new Error('Failed to fetch version');
        return res.json();
    },

    // Volumes
    async getVolumes() {
        const res = await fetch(`${API_BASE}/volumes`);
        if (!res.ok) throw new Error('Failed to fetch volumes');
        return res.json();
    },

    // Files
    async listFiles(path, showArchives = true) {
        const params = new URLSearchParams({ path, show_archives: showArchives });
        const res = await fetch(buildApiUrl('/files', params));
        if (!res.ok) throw new Error('Failed to list files');
        return res.json();
    },

    async searchFiles(path, recursive = true, includeArchives = true) {
        const params = new URLSearchParams({
            path,
            recursive,
            include_archives: includeArchives
        });
        const res = await fetch(buildApiUrl('/files/search', params));
        if (!res.ok) throw new Error('Failed to search files');
        return res.json();
    },

    async listArchive(path) {
        const params = new URLSearchParams({ path });
        const res = await fetch(buildApiUrl('/files/archive', params));
        if (!res.ok) throw new Error('Failed to list archive');
        return res.json();
    },

    // Jobs
    async createJob(filePath, mode = 'createcd', outputDir = null, compression = null, deleteOnVerify = false) {
        const res = await fetch(`${API_BASE}/jobs`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                file_path: filePath,
                mode,
                output_dir: outputDir,
                compression,
                delete_on_verify: deleteOnVerify
            })
        });
        if (!res.ok) {
            const error = await res.json().catch(() => ({ detail: 'Failed to create job' }));
            throw new Error(error.detail || 'Failed to create job');
        }
        return res.json();
    },

    async createBatchJobs(filePaths, mode = 'createcd', outputDir = null, duplicateAction = 'skip', compression = null, deleteOnVerify = false) {
        const res = await fetch(`${API_BASE}/jobs/batch`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                file_paths: filePaths,
                mode,
                output_dir: outputDir,
                duplicate_action: duplicateAction,
                compression,
                delete_on_verify: deleteOnVerify
            })
        });
        if (!res.ok) {
            const error = await res.json().catch(() => ({ detail: 'Failed to create jobs' }));
            throw new Error(error.detail || 'Failed to create jobs');
        }
        return res.json();
    },

    async checkDuplicates(filePaths, outputDir = null, mode = 'createcd') {
        const res = await fetch(`${API_BASE}/jobs/check-duplicates`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                file_paths: filePaths,
                output_dir: outputDir,
                mode
            })
        });
        if (!res.ok) {
            const error = await res.json().catch(() => ({ detail: 'Failed to check duplicates' }));
            throw new Error(error.detail || 'Failed to check duplicates');
        }
        return res.json();
    },

    async getDeletePlan(filePaths, mode = 'createcd') {
        const res = await fetch(`${API_BASE}/jobs/delete-plan`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                file_paths: filePaths,
                mode
            })
        });
        if (!res.ok) {
            const error = await res.json().catch(() => ({ detail: 'Failed to build delete plan' }));
            throw new Error(error.detail || 'Failed to build delete plan');
        }
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
        eventSource.addEventListener('cancelled', (e) => safeParseAndUpdate('cancelled', e));

        eventSource.onerror = (err) => {
            console.error('SSE connection error:', err);
            // Don't close - EventSource will auto-reconnect
        };

        return () => eventSource.close();
    },

    // CHD Info
    async getCHDInfo(path) {
        const params = new URLSearchParams({ path });
        const res = await fetch(buildApiUrl('/info', params));
        if (!res.ok) throw new Error('Failed to get CHD info');
        return res.json();
    },

    // Dolphin disc info
    async getDolphinInfo(path) {
        const params = new URLSearchParams({ path });
        const res = await fetch(buildApiUrl('/dolphin-info', params));
        if (!res.ok) throw new Error('Failed to get disc info');
        return res.json();
    },

    async verifyCHD(path, { onProgress } = {}) {
        if (onProgress) {
            return new Promise((resolve, reject) => {
                const params = new URLSearchParams({ path });
                const eventSource = new EventSource(buildApiUrl('/verify/events', params));

                const cleanup = () => eventSource.close();

                eventSource.addEventListener('verify_progress', (e) => {
                    try {
                        if (e.data) {
                            onProgress(JSON.parse(e.data));
                        }
                    } catch (err) {
                        console.error('Failed to parse verify progress event:', err, e.data);
                    }
                });

                eventSource.addEventListener('verify_complete', (e) => {
                    cleanup();
                    try {
                        resolve(JSON.parse(e.data));
                    } catch (err) {
                        reject(err);
                    }
                });

                eventSource.addEventListener('verify_error', (e) => {
                    cleanup();
                    let message = 'CHD verification failed';
                    try {
                        const data = JSON.parse(e.data);
                        message = data.message || message;
                    } catch (err) {
                        console.error('Failed to parse verify error event:', err, e.data);
                    }
                    reject(new Error(message));
                });

                eventSource.onerror = (err) => {
                    cleanup();
                    reject(new Error('Verification connection error'));
                };
            });
        }

        const params = new URLSearchParams({ path });
        const res = await fetch(buildApiUrl('/verify', params));
        if (!res.ok) throw new Error('Failed to verify CHD');
        return res.json();
    },

    async verifyDolphin(path, { onProgress } = {}) {
        if (onProgress) {
            return new Promise((resolve, reject) => {
                const params = new URLSearchParams({ path });
                const eventSource = new EventSource(buildApiUrl('/dolphin-verify/events', params));

                const cleanup = () => eventSource.close();

                eventSource.addEventListener('verify_progress', (e) => {
                    try {
                        if (e.data) {
                            onProgress(JSON.parse(e.data));
                        }
                    } catch (err) {
                        console.error('Failed to parse verify progress event:', err, e.data);
                    }
                });

                eventSource.addEventListener('verify_complete', (e) => {
                    cleanup();
                    try {
                        resolve(JSON.parse(e.data));
                    } catch (err) {
                        reject(err);
                    }
                });

                eventSource.addEventListener('verify_error', (e) => {
                    cleanup();
                    let message = 'Disc verification failed';
                    try {
                        const data = JSON.parse(e.data);
                        message = data.message || message;
                    } catch (err) {
                        console.error('Failed to parse verify error event:', err, e.data);
                    }
                    reject(new Error(message));
                });

                eventSource.onerror = (err) => {
                    cleanup();
                    reject(new Error('Verification connection error'));
                };
            });
        }

        const params = new URLSearchParams({ path });
        const res = await fetch(buildApiUrl('/dolphin-verify', params));
        if (!res.ok) throw new Error('Failed to verify disc');
        return res.json();
    },

    async getVerifiedCHDs() {
        const res = await fetch(`${API_BASE}/verified`);
        if (!res.ok) throw new Error('Failed to fetch verified CHDs');
        return res.json();
    },

    async getCHDMetadataBatch(paths) {
        if (!paths || paths.length === 0) return {};
        const res = await fetch(`${API_BASE}/chd-metadata`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ paths })
        });
        if (!res.ok) throw new Error('Failed to fetch CHD metadata');
        return res.json();
    },

    async scanMetadata(force = false) {
        const params = force ? '?force=true' : '';
        const res = await fetch(`${API_BASE}/chd-metadata/scan${params}`, { method: 'POST' });
        if (!res.ok) throw new Error('Failed to start metadata scan');
        return res.json();
    },

    async getScanStatus() {
        const res = await fetch(`${API_BASE}/chd-metadata/scan/status`);
        if (!res.ok) throw new Error('Failed to get scan status');
        return res.json();
    },

    // File operations
    async renameFile(path, newName) {
        const params = new URLSearchParams({ path, new_name: newName });
        const res = await fetch(buildApiUrl('/files/rename', params), { method: 'POST' });
        if (!res.ok) {
            const error = await res.json().catch(() => ({ detail: 'Failed to rename' }));
            throw new Error(error.detail || 'Failed to rename');
        }
        return res.json();
    },

    async deleteFile(path) {
        const params = new URLSearchParams({ path });
        const res = await fetch(buildApiUrl('/files/delete', params), { method: 'DELETE' });
        if (!res.ok) {
            const error = await res.json().catch(() => ({ detail: 'Failed to delete' }));
            throw new Error(error.detail || 'Failed to delete');
        }
        return res.json();
    },

    async deleteBatch(paths) {
        const res = await fetch(`${API_BASE}/files/delete-batch`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ paths })
        });
        if (!res.ok) {
            const error = await res.json().catch(() => ({ detail: 'Failed to delete files' }));
            throw new Error(error.detail || 'Failed to delete files');
        }
        return res.json();
    },

    async verifyBatchCHDs(paths, { onProgress, onFileComplete } = {}) {
        const response = await fetch(`${API_BASE}/verify-batch/events`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ paths })
        });

        if (!response.ok) {
            const error = await response.json().catch(() => ({ detail: 'Failed to start batch verification' }));
            throw new Error(error.detail || 'Failed to start batch verification');
        }

        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';
        let result = { total: 0, verified: 0, failed: 0 };

        const parseSSEEvent = (eventText) => {
            const lines = eventText.split('\n');
            let eventType = null;
            let eventData = null;

            for (const line of lines) {
                if (line.startsWith('event:')) {
                    eventType = line.slice(6).trim();
                } else if (line.startsWith('data:')) {
                    eventData = line.slice(5).trim();
                }
            }

            if (!eventType || !eventData) return null;

            try {
                const parsed = JSON.parse(eventData);
                if (typeof parsed !== 'object' || parsed === null) return null;
                return { type: eventType, data: parsed };
            } catch (err) {
                console.error('Failed to parse SSE event data:', err);
                return null;
            }
        };

        const processEvent = (event) => {
            if (!event) return false;

            switch (event.type) {
                case 'verify_batch_start':
                    if (onProgress) onProgress({ type: 'start', ...event.data });
                    break;
                case 'verify_batch_progress':
                    if (onProgress) onProgress({ type: 'progress', ...event.data });
                    break;
                case 'verify_batch_file_progress':
                    if (onProgress) onProgress({ type: 'file_progress', ...event.data });
                    break;
                case 'verify_batch_file_complete':
                    if (onFileComplete) onFileComplete(event.data);
                    if (onProgress) onProgress({ type: 'file_complete', ...event.data });
                    break;
                case 'verify_batch_complete':
                    result = event.data;
                    return true; // Signal completion
            }
            return false;
        };

        let streamActive = true;
        while (streamActive) {
            const { done, value } = await reader.read();
            if (done) {
                streamActive = false;
                continue;
            }

            buffer += decoder.decode(value, { stream: true });

            // Split on double newlines (SSE event separator)
            const events = buffer.split('\n\n');
            buffer = events.pop() || ''; // Keep incomplete event in buffer

            for (const eventText of events) {
                if (eventText.trim()) {
                    const event = parseSSEEvent(eventText);
                    if (processEvent(event)) {
                        return result;
                    }
                }
            }
        }

        return result;
    },
    async verifyBatchDolphin(paths, { onProgress, onFileComplete } = {}) {
        const response = await fetch(`${API_BASE}/dolphin-verify-batch/events`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ paths })
        });

        if (!response.ok) {
            const error = await response.json().catch(() => ({ detail: 'Failed to start batch verification' }));
            throw new Error(error.detail || 'Failed to start batch verification');
        }

        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';
        let result = { total: 0, verified: 0, failed: 0 };

        const parseSSEEvent = (eventText) => {
            const lines = eventText.split('\n');
            let eventType = null;
            let eventData = null;

            for (const line of lines) {
                if (line.startsWith('event:')) {
                    eventType = line.slice(6).trim();
                } else if (line.startsWith('data:')) {
                    eventData = line.slice(5).trim();
                }
            }

            if (!eventType || !eventData) return null;

            try {
                const parsed = JSON.parse(eventData);
                if (typeof parsed !== 'object' || parsed === null) return null;
                return { type: eventType, data: parsed };
            } catch (err) {
                console.error('Failed to parse SSE event data:', err);
                return null;
            }
        };

        const processEvent = (event) => {
            if (!event) return false;

            switch (event.type) {
                case 'verify_batch_start':
                    if (onProgress) onProgress({ type: 'start', ...event.data });
                    break;
                case 'verify_batch_progress':
                    if (onProgress) onProgress({ type: 'progress', ...event.data });
                    break;
                case 'verify_batch_file_progress':
                    if (onProgress) onProgress({ type: 'file_progress', ...event.data });
                    break;
                case 'verify_batch_file_complete':
                    if (onFileComplete) onFileComplete(event.data);
                    if (onProgress) onProgress({ type: 'file_complete', ...event.data });
                    break;
                case 'verify_batch_complete':
                    result = event.data;
                    return true; // Signal completion
            }
            return false;
        };

        let streamActive = true;
        while (streamActive) {
            const { done, value } = await reader.read();
            if (done) {
                streamActive = false;
                continue;
            }

            buffer += decoder.decode(value, { stream: true });

            // Split on double newlines (SSE event separator)
            const events = buffer.split('\n\n');
            buffer = events.pop() || ''; // Keep incomplete event in buffer

            for (const eventText of events) {
                if (eventText.trim()) {
                    const event = parseSSEEvent(eventText);
                    if (processEvent(event)) {
                        return result;
                    }
                }
            }
        }

        return result;
    }
};

// Format file size
export function formatSize(bytes) {
    if (bytes == null || bytes === undefined) return '';
    if (bytes === 0) return '0 B';
    const k = 1024;
    const sizes = ['B', 'KB', 'MB', 'GB', 'TB'];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return Number.parseFloat((bytes / Math.pow(k, i)).toFixed(2)) + ' ' + sizes[i];
}

// Get file icon
export function getFileIcon(entry) {
    if (entry.type === 'directory') return '📁';
    if (entry.type === 'archive') return '📦';
    const ext = entry.extension?.toLowerCase();
    if (ext === '.chd') return '💿';
    if (['.rvz', '.wia', '.gcz', '.wbfs'].includes(ext)) return '🎮';
    if (['.iso', '.gdi', '.cue', '.bin'].includes(ext)) return '💽';
    return '📄';
}

export const DOLPHIN_EXTENSIONS = ['.rvz', '.wia', '.gcz', '.wbfs'];

export function isDolphinFile(path) {
    const ext = path.split('.').pop();
    return DOLPHIN_EXTENSIONS.includes('.' + ext.toLowerCase());
}
