// All REST endpoints to the FastAPI backend, grouped by feature.
// Direct port of static/js/api.js; every URL, header, and payload preserved.

import { API_BASE, CONFIRM, buildApiUrl, fetchJson, jsonPost } from './client.js';
import { sseConnectNamed, verifyEventSource } from './sse.js';
import { sseFetchPost } from './sseFetch.js';

const dispatchBatchEvent = (event, { onProgress, onFileComplete }) => {
  switch (event.type) {
    case 'verify_batch_start':
      onProgress?.({ type: 'start', ...event.data });
      return false;
    case 'verify_batch_progress':
      onProgress?.({ type: 'progress', ...event.data });
      return false;
    case 'verify_batch_file_progress':
      onProgress?.({ type: 'file_progress', ...event.data });
      return false;
    case 'verify_batch_file_complete':
      onFileComplete?.(event.data);
      onProgress?.({ type: 'file_complete', ...event.data });
      return false;
    case 'verify_batch_complete':
      return event.data; // signals completion with final result
    default:
      return false;
  }
};

const runBatchVerify = async (url, paths, { onProgress, onFileComplete, signal } = {}) => {
  let finalResult = { total: 0, verified: 0, failed: 0 };
  let completed = false;
  await sseFetchPost(
    url,
    { paths },
    {
      signal,
      fallbackMessage: 'Failed to start batch verification',
      onEvent: (event) => {
        const result = dispatchBatchEvent(event, { onProgress, onFileComplete });
        if (result && typeof result === 'object') {
          finalResult = result;
          completed = true;
        }
      },
    },
  );
  if (!completed) {
    if (signal?.aborted) {
      throw new DOMException('Aborted', 'AbortError');
    }
    // A stream that drains without verify_batch_complete is a failure mode
    // (proxy timeout, backend crash). Surface it rather than silently
    // returning the zero-counts default, callers update UI based on the
    // returned result and would otherwise show "0 verified" as success.
    const err = new Error('Batch verification stream ended before completion');
    err.partial = finalResult;
    throw err;
  }
  return finalResult;
};

export const api = {
  // ─── Version ──────────────────────────────────────────────────────────
  getVersion: () => fetchJson(`${API_BASE}/version`, undefined, 'Failed to fetch version'),

  // ─── Notifications (webhook / Apprise destinations) ─────────────────────
  // The server never returns a saved URL; edits that omit `url` keep it.
  getNotifications: () =>
    fetchJson(`${API_BASE}/notifications`, undefined, 'Failed to load notifications'),
  addNotificationDestination: (body) =>
    jsonPost(`${API_BASE}/notifications/destinations`, body, {}, 'Failed to add destination'),
  editNotificationDestination: (id, body) =>
    fetchJson(
      `${API_BASE}/notifications/destinations/${encodeURIComponent(id)}`,
      {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      },
      'Failed to save destination',
    ),
  deleteNotificationDestination: (id) =>
    fetchJson(
      `${API_BASE}/notifications/destinations/${encodeURIComponent(id)}`,
      { method: 'DELETE' },
      'Failed to remove destination',
    ),
  testNotificationDestination: (id) =>
    jsonPost(
      `${API_BASE}/notifications/destinations/${encodeURIComponent(id)}/test`,
      {},
      {},
      'Test failed',
    ),
  testNotificationDraft: (body) =>
    jsonPost(`${API_BASE}/notifications/test`, body, {}, 'Test failed'),

  // ─── Ledger (permanent record of finished jobs) ────────────────────────
  getLedgerRecent: (limit = 5) =>
    fetchJson(buildApiUrl('/ledger/recent', { limit }), undefined, 'Failed to fetch ledger'),
  getLedgerPage: (query) =>
    fetchJson(buildApiUrl('/ledger', query), undefined, 'Failed to fetch ledger'),
  getLedgerFacets: () =>
    fetchJson(`${API_BASE}/ledger/facets`, undefined, 'Failed to fetch ledger filters'),
  getLedgerSavings: () =>
    fetchJson(`${API_BASE}/ledger/savings`, undefined, 'Failed to fetch savings'),

  // ─── Volumes ──────────────────────────────────────────────────────────
  getVolumes: () => fetchJson(`${API_BASE}/volumes`, undefined, 'Failed to fetch volumes'),

  // ─── Preferences (server-stored UI layout) ─────────────────────────────
  getPreferences: () =>
    fetchJson(`${API_BASE}/preferences`, undefined, 'Failed to fetch preferences'),
  getConversionPrefs: () =>
    fetchJson(
      `${API_BASE}/preferences/conversion`,
      undefined,
      'Failed to fetch compression preferences',
    ),
  putConversionPrefs(prefs) {
    return fetchJson(
      `${API_BASE}/preferences/conversion`,
      {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(prefs),
      },
      'Failed to save compression preferences',
    );
  },
  putPreferences(layout) {
    return fetchJson(
      `${API_BASE}/preferences`,
      {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(layout),
      },
      'Failed to save preferences',
    );
  },

  // ─── Files ────────────────────────────────────────────────────────────
  // summarizeArchives=false keeps the directory listing fast in folders with
  // thousands of archives: archive rows come back without member counts /
  // verifiable_by, which the browser then hydrates via getArchiveSummaryBatch
  // (mirroring CHD media_type hydration). Pass true for a one-shot inline
  // summary when not hydrating separately.
  listFiles(path, showArchives = true, summarizeArchives = false) {
    const params = new URLSearchParams({
      path,
      show_archives: String(showArchives),
      summarize_archives: String(summarizeArchives),
    });
    return fetchJson(buildApiUrl('/files', params), undefined, 'Failed to list files');
  },

  // Per-archive summaries (member counts, verifiable_by) for the lazy listing.
  getArchiveSummaryBatch(paths) {
    if (!paths?.length) return Promise.resolve({});
    return jsonPost(
      buildApiUrl('/archive-summary'), { paths }, {}, 'Failed to fetch archive summaries',
    );
  },

  searchFiles(path, recursive = true, includeArchives = true) {
    const params = new URLSearchParams({
      path,
      recursive: String(recursive),
      include_archives: String(includeArchives),
    });
    return fetchJson(buildApiUrl('/files/search', params), undefined, 'Failed to search files');
  },

  listArchive(path) {
    const params = new URLSearchParams({ path });
    return fetchJson(buildApiUrl('/files/archive', params), undefined, 'Failed to list archive');
  },

  renameFile(path, newName) {
    const params = new URLSearchParams({ path, new_name: newName });
    return fetchJson(
      buildApiUrl('/files/rename', params),
      { method: 'POST', headers: { 'X-CHD-Action-Confirm': CONFIRM.RENAME_FILE } },
      'Failed to rename',
    );
  },

  deleteFile(path, { recursive = false } = {}) {
    const params = new URLSearchParams({ path });
    if (recursive) params.set('recursive', 'true');
    const headers = {
      'X-CHD-Action-Confirm': recursive ? CONFIRM.RECURSIVE_DELETE : CONFIRM.DELETE_FILE,
    };
    return fetchJson(
      buildApiUrl('/files/delete', params),
      { method: 'DELETE', headers },
      'Failed to delete',
    );
  },

  /** Preview the .m3u the selected discs would make. Writes nothing. */
  playlistPreview(paths, title) {
    return jsonPost(
      `${API_BASE}/files/playlist/preview`,
      title == null ? { paths } : { paths, title },
      {},
      'Failed to preview the playlist',
    );
  },

  /** Write the previewed playlist. A taken name is a 409 with `err.detail.suggested_name`. */
  createPlaylist(paths, fileName) {
    return jsonPost(
      `${API_BASE}/files/playlist`,
      { paths, file_name: fileName },
      {},
      'Failed to create the playlist',
    );
  },

  deleteBatch(paths) {
    return jsonPost(
      `${API_BASE}/files/delete-batch`,
      { paths },
      { headers: { 'X-CHD-Action-Confirm': CONFIRM.DELETE_FILE } },
      'Failed to delete files',
    );
  },

  // ─── Jobs ─────────────────────────────────────────────────────────────
  createJob(
    filePath,
    mode = 'createcd',
    outputDir = null,
    compression = null,
    deleteOnVerify = false,
    split = false,
    containerOnly = false,
  ) {
    return jsonPost(
      `${API_BASE}/jobs`,
      {
        file_path: filePath,
        mode,
        output_dir: outputDir,
        compression,
        delete_on_verify: deleteOnVerify,
        container_only: containerOnly,
        split,
      },
      {},
      'Failed to create job',
    );
  },

  createBatchJobs(
    filePaths,
    mode = 'createcd',
    outputDir = null,
    duplicateAction = 'skip',
    compression = null,
    deleteOnVerify = false,
    split = false,
    containerOnly = false,
  ) {
    return jsonPost(
      `${API_BASE}/jobs/batch`,
      {
        file_paths: filePaths,
        mode,
        output_dir: outputDir,
        duplicate_action: duplicateAction,
        compression,
        delete_on_verify: deleteOnVerify,
        container_only: containerOnly,
        split,
      },
      {},
      'Failed to create jobs',
    );
  },

  // Auto: the backend works out each Image's Platform and default target.
  // `signal` aborts the request (the convert panel unmounting mid-plan).
  getAutoPlan(filePaths, outputDir = null, { signal } = {}) {
    return jsonPost(
      `${API_BASE}/jobs/auto-plan`,
      { file_paths: filePaths, output_dir: outputDir },
      signal ? { signal } : {},
      'Failed to plan Auto conversion',
    );
  },

  // `items`: the confirmed plan's actionable rows, `{ file_path, mode }`. The
  // server replans them and answers 409 with the new plan when one changed.
  createAutoJobs(items, outputDir = null, duplicateAction = 'skip') {
    return jsonPost(
      `${API_BASE}/jobs/auto`,
      { items, output_dir: outputDir, duplicate_action: duplicateAction },
      {},
      'Failed to create Auto jobs',
    );
  },

  checkDuplicates(filePaths, outputDir = null, mode = 'createcd') {
    return jsonPost(
      `${API_BASE}/jobs/check-duplicates`,
      { file_paths: filePaths, output_dir: outputDir, mode },
      {},
      'Failed to check duplicates',
    );
  },

  getDeletePlan(filePaths, mode = 'createcd', outputDir = null, duplicateAction = 'skip') {
    return jsonPost(
      `${API_BASE}/jobs/delete-plan`,
      { file_paths: filePaths, mode, output_dir: outputDir, duplicate_action: duplicateAction },
      {},
      'Failed to build delete plan',
    );
  },

  getJobs: () => fetchJson(`${API_BASE}/jobs`, undefined, 'Failed to fetch jobs'),

  // Counts of finished jobs the backend history cap (MAX_JOB_HISTORY) has
  // already evicted. /jobs only returns what is still retained, so this is
  // what turns a capped list into a true total. Live updates arrive as
  // `history` events on the job stream; this is the hydration read.
  //
  // `since` is the caller's last applied `seq`. Passing it also returns the
  // ids evicted after that point — read /jobs first, then pass the cursor
  // here, and a job evicted between the two reads is named rather than
  // counted twice (once as a row still in the list, once in the totals).
  //
  // `generation` is the backend process the cursor came from. Without it, a
  // cursor minted before a backend restart reads as a huge sequence against a
  // log that restarted at 0, so the reply names nothing — the exact
  // double-count the cursor exists to prevent.
  getJobHistoryOverflow(since, generation) {
    const params = typeof since === 'number' && since >= 0
      ? new URLSearchParams({ since: String(since) })
      : undefined;
    if (params && generation) params.set('generation', generation);
    return fetchJson(
      buildApiUrl('/jobs/history-overflow', params),
      undefined,
      'Failed to fetch job history totals',
    );
  },

  getJob: (jobId) => fetchJson(`${API_BASE}/jobs/${jobId}`, undefined, 'Failed to fetch job'),

  cancelJob: (jobId) =>
    fetchJson(`${API_BASE}/jobs/${jobId}`, { method: 'DELETE' }, 'Failed to cancel job'),

  cancelAllJobs: () =>
    fetchJson(
      `${API_BASE}/jobs/cancel-all`,
      { method: 'POST', headers: { 'X-CHD-Action-Confirm': CONFIRM.CANCEL_ALL_JOBS } },
      'Failed to cancel all jobs',
    ),

  deleteCompletedJobs: () =>
    fetchJson(
      `${API_BASE}/jobs/completed`,
      { method: 'DELETE', headers: { 'X-CHD-Action-Confirm': CONFIRM.CLEAR_COMPLETED_JOBS } },
      'Failed to delete completed jobs',
    ),

  checkStuckStatus: () =>
    fetchJson(`${API_BASE}/jobs/stuck-status`, undefined, 'Failed to check stuck status'),

  recoverStuckJobs: () =>
    fetchJson(`${API_BASE}/jobs/recover`, { method: 'POST' }, 'Failed to recover stuck jobs'),

  /**
   * Subscribe to the global job event stream. Auto-reconnects on error.
   * Returns an unsubscribe function.
   *
   * Includes `snapshot` so the one-time hydration emission the backend
   * sends on connect (and re-sends after each reconnect) flows through
   * the same handler as live updates. See convert.py:event_generator.
   *
   * `history` carries the counts of finished jobs the backend's history cap
   * has evicted — absolute totals, re-sent whenever they change.
   *
   * @param {(evt: { type: string, data: any }) => void} onEvent
   * @param {{onOpen?: () => void, onReconnecting?: () => void}} [status]
   */
  subscribeToJobs(onEvent, status) {
    const conn = sseConnectNamed(
      `${API_BASE}/jobs/events`,
      ['snapshot', 'progress', 'complete', 'error', 'status', 'cancelled', 'history'],
      onEvent,
      status,
    );
    return () => conn.close();
  },

  // ─── Tool info ────────────────────────────────────────────────────────
  getCHDInfo(path) {
    const params = new URLSearchParams({ path });
    return fetchJson(buildApiUrl('/info', params), undefined, 'Failed to get CHD info');
  },

  getDolphinInfo(path) {
    const params = new URLSearchParams({ path });
    return fetchJson(buildApiUrl('/dolphin-info', params), undefined, 'Failed to get disc info');
  },

  getZ3DSInfo(path) {
    const params = new URLSearchParams({ path });
    return fetchJson(buildApiUrl('/z3ds-info', params), undefined, 'Failed to get 3DS ROM info');
  },

  getNszInfo(path) {
    const params = new URLSearchParams({ path });
    return fetchJson(buildApiUrl('/nsz-info', params), undefined, 'Failed to get Switch file info');
  },

  getCsoInfo(path) {
    const params = new URLSearchParams({ path });
    return fetchJson(buildApiUrl('/cso-info', params), undefined, 'Failed to get CSO info');
  },

  getRomzInfo(path) {
    const params = new URLSearchParams({ path });
    return fetchJson(buildApiUrl('/romz-info', params), undefined, 'Failed to get ROM info');
  },

  // NKit sources only. There is no verifyNkit/verifyBatchNkit counterpart:
  // nkit2iso has no verify subcommand (see the registry entry).
  getNkitInfo(path) {
    const params = new URLSearchParams({ path });
    return fetchJson(buildApiUrl('/nkit-info', params), undefined, 'Failed to get NKit info');
  },

  getJwudInfo(path) {
    const params = new URLSearchParams({ path });
    return fetchJson(buildApiUrl('/jwud-info', params), undefined, 'Failed to get Wii U image info');
  },

  getXisoInfo(path) {
    const params = new URLSearchParams({ path });
    return fetchJson(buildApiUrl('/xiso-info', params), undefined, 'Failed to get Xbox image info');
  },

  // Which tools the UI should show. Switch is reported unavailable when no
  // prod.keys are configured, so the sidebar can hide it entirely.
  getTools() {
    return fetchJson(`${API_BASE}/tools`, undefined, 'Failed to load tool availability');
  },

  // ─── Single-file verify (SSE + sync fallback) ─────────────────────────
  verifyCHD(path, { onProgress } = {}) {
    if (onProgress) {
      const params = new URLSearchParams({ path });
      return verifyEventSource(buildApiUrl('/verify/events', params), onProgress, {
        failureFallback: 'CHD verification failed',
      });
    }
    const params = new URLSearchParams({ path });
    return fetchJson(buildApiUrl('/verify', params), undefined, 'Failed to verify CHD');
  },

  verifyDolphin(path, { onProgress } = {}) {
    if (onProgress) {
      const params = new URLSearchParams({ path });
      return verifyEventSource(buildApiUrl('/dolphin-verify/events', params), onProgress, {
        failureFallback: 'Disc verification failed',
      });
    }
    const params = new URLSearchParams({ path });
    return fetchJson(buildApiUrl('/dolphin-verify', params), undefined, 'Failed to verify disc');
  },

  verify3DS(path, { onProgress } = {}) {
    if (onProgress) {
      const params = new URLSearchParams({ path });
      return verifyEventSource(buildApiUrl('/z3ds-verify/events', params), onProgress, {
        failureFallback: '3DS verification failed',
      });
    }
    const params = new URLSearchParams({ path });
    return fetchJson(buildApiUrl('/z3ds-verify', params), undefined, 'Failed to verify 3DS ROM');
  },

  verifyNsz(path, { onProgress } = {}) {
    if (onProgress) {
      const params = new URLSearchParams({ path });
      return verifyEventSource(buildApiUrl('/nsz-verify/events', params), onProgress, {
        failureFallback: 'Switch verification failed',
      });
    }
    const params = new URLSearchParams({ path });
    return fetchJson(buildApiUrl('/nsz-verify', params), undefined, 'Failed to verify Switch file');
  },

  verifyCso(path, { onProgress } = {}) {
    if (onProgress) {
      const params = new URLSearchParams({ path });
      return verifyEventSource(buildApiUrl('/cso-verify/events', params), onProgress, {
        failureFallback: 'CSO verification failed',
      });
    }
    const params = new URLSearchParams({ path });
    return fetchJson(buildApiUrl('/cso-verify', params), undefined, 'Failed to verify CSO file');
  },

  verifyRomz(path, { onProgress } = {}) {
    if (onProgress) {
      const params = new URLSearchParams({ path });
      return verifyEventSource(buildApiUrl('/romz-verify/events', params), onProgress, {
        failureFallback: 'ROM archive verification failed',
      });
    }
    const params = new URLSearchParams({ path });
    return fetchJson(buildApiUrl('/romz-verify', params), undefined, 'Failed to verify ROM archive');
  },

  verifyJwud(path, { onProgress } = {}) {
    if (onProgress) {
      const params = new URLSearchParams({ path });
      return verifyEventSource(buildApiUrl('/jwud-verify/events', params), onProgress, {
        failureFallback: 'Wii U verification failed',
      });
    }
    const params = new URLSearchParams({ path });
    return fetchJson(buildApiUrl('/jwud-verify', params), undefined, 'Failed to verify Wii U image');
  },

  verifyXiso(path, { onProgress } = {}) {
    if (onProgress) {
      const params = new URLSearchParams({ path });
      return verifyEventSource(buildApiUrl('/xiso-verify/events', params), onProgress, {
        failureFallback: 'XISO verification failed',
      });
    }
    const params = new URLSearchParams({ path });
    return fetchJson(buildApiUrl('/xiso-verify', params), undefined, 'Failed to verify XISO');
  },

  verifyExodos(path, { onProgress } = {}) {
    const params = new URLSearchParams({ path });
    if (onProgress) {
      return verifyEventSource(buildApiUrl('/exodos-verify/events', params), onProgress, {
        failureFallback: 'eXoDOS repack verification failed',
      });
    }
    return fetchJson(buildApiUrl('/exodos-verify', params), undefined, 'Failed to verify eXoDOS repack');
  },

  getVerifiedCHDs: () =>
    fetchJson(`${API_BASE}/verified`, undefined, 'Failed to fetch check results'),

  /** The same answer as getVerifiedCHDs, for just `paths` (at most 2000). */
  getVerifiedFor: (paths) =>
    fetchJson(
      `${API_BASE}/verified/lookup`,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ paths }),
      },
      'Failed to fetch check results',
    ),

  // ─── Batch verify (POST + ReadableStream SSE) ─────────────────────────
  verifyBatchCHDs(paths, opts) {
    return runBatchVerify(`${API_BASE}/verify-batch/events`, paths, opts);
  },

  verifyBatchDolphin(paths, opts) {
    return runBatchVerify(`${API_BASE}/dolphin-verify-batch/events`, paths, opts);
  },

  verifyBatchZ3DS(paths, opts) {
    return runBatchVerify(`${API_BASE}/z3ds-verify-batch/events`, paths, opts);
  },

  verifyBatchNsz(paths, opts) {
    return runBatchVerify(`${API_BASE}/nsz-verify-batch/events`, paths, opts);
  },

  verifyBatchCso(paths, opts) {
    return runBatchVerify(`${API_BASE}/cso-verify-batch/events`, paths, opts);
  },

  verifyBatchRomz(paths, opts) {
    return runBatchVerify(`${API_BASE}/romz-verify-batch/events`, paths, opts);
  },

  verifyBatchJwud(paths, opts) {
    return runBatchVerify(`${API_BASE}/jwud-verify-batch/events`, paths, opts);
  },

  verifyBatchXiso(paths, opts) {
    return runBatchVerify(`${API_BASE}/xiso-verify-batch/events`, paths, opts);
  },

  verifyBatchExodos(paths, opts) {
    return runBatchVerify(`${API_BASE}/exodos-verify-batch/events`, paths, opts);
  },

  // ─── CHD metadata cache ───────────────────────────────────────────────
  getCHDMetadataBatch(paths) {
    if (!paths || paths.length === 0) return Promise.resolve({});
    return jsonPost(`${API_BASE}/chd-metadata`, { paths }, {}, 'Failed to fetch CHD metadata');
  },

  scanMetadata(force = false) {
    const suffix = force ? '?force=true' : '';
    return fetchJson(
      `${API_BASE}/chd-metadata/scan${suffix}`,
      { method: 'POST' },
      'Failed to start metadata scan',
    );
  },

  getScanStatus: () =>
    fetchJson(`${API_BASE}/chd-metadata/scan/status`, undefined, 'Failed to get scan status'),

  // ─── DAT library ──────────────────────────────────────────────────────
  importDAT(file) {
    const formData = new FormData();
    formData.append('file', file);
    return fetchJson(
      `${API_BASE}/dat/import`,
      { method: 'POST', body: formData },
      'Failed to import DAT',
    );
  },

  // ─── RomM catalog overlay ─────────────────────────────────────────────
  // The ROM listing comes back as a DirectoryListing of FileEntry, the same
  // shape /files returns, so FileList/FileRow/ConvertPanel render it unchanged
  // and conversions submit through the ordinary createBatchJobs.

  getRommStatus: () =>
    fetchJson(`${API_BASE}/romm/status`, undefined, 'Failed to get RomM status'),

  getRommPlatforms: () =>
    fetchJson(`${API_BASE}/romm/platforms`, undefined, 'Failed to list RomM platforms'),

  getRommRoms: (platformId) =>
    fetchJson(
      buildApiUrl('/romm/roms', { platform_id: platformId }),
      undefined,
      'Failed to list RomM ROMs',
    ),

  // ── RomM settings, rules, and unattended conversion ──────────────────
  getRommSettings: () =>
    fetchJson(`${API_BASE}/romm/settings`, undefined, 'Failed to load RomM settings'),

  saveRommSettings: (patch) =>
    fetchJson(
      `${API_BASE}/romm/settings`,
      {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(patch),
      },
      'Failed to save RomM settings',
    ),

  // Probes without saving, so the user can check a URL/token before committing.
  testRommConnection: (patch = {}) =>
    jsonPost(`${API_BASE}/romm/settings/test`, patch, {}, 'Connection test failed'),

  getRommRules: () =>
    fetchJson(`${API_BASE}/romm/rules`, undefined, 'Failed to load RomM rules'),

  // The Rules importing RomM's converter targets would create (#385). Read
  // only: the store saves them through saveRommRules like any edit.
  getRommConverterImportPlan: () =>
    fetchJson(
      `${API_BASE}/romm/converter/import-plan`,
      undefined,
      "Failed to read RomM's converter targets",
    ),

  // `checkRules` (#305) is sent when given; without it the server keeps them.
  saveRommRules: (rules, checkRules) =>
    fetchJson(
      `${API_BASE}/romm/rules`,
      {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(
          checkRules === undefined ? { rules } : { rules, check_rules: checkRules },
        ),
      },
      'Failed to save RomM rules',
    ),

  // One-time notices any rule editor shows (#302). Not RomM-specific.
  getRuleNotices: () =>
    fetchJson(`${API_BASE}/rules/notices`, undefined, 'Failed to load rule notices'),

  dismissCheckLevelNotice: () =>
    jsonPost(
      `${API_BASE}/rules/notices/check-level/dismiss`,
      {},
      {},
      'Failed to dismiss the notice',
    ),

  // Clears the "already converted by this rule" history that stops overwrite
  // and rename rules from reconverting the same sources every interval.
  forgetRommConverted: (platformIds = null) =>
    jsonPost(
      `${API_BASE}/romm/rules/forget-converted`,
      platformIds ? { platform_ids: platformIds } : {},
      {},
      'Failed to clear the conversion history',
    ),

  // Shows what a sweep would queue, without queueing it.
  previewRommAutoConvert: (body = {}) =>
    jsonPost(`${API_BASE}/romm/auto-convert/preview`, body, {}, 'Preview failed'),

  runRommAutoConvert: (body = {}) =>
    jsonPost(`${API_BASE}/romm/auto-convert/run`, body, {}, 'Auto-convert run failed'),

  // ── Libraries (#303): RomM plus every folder registered as one ──────────
  // A Library id goes in the path (`romm`, `folder:3`), so it is encoded.
  listLibraries: () =>
    fetchJson(`${API_BASE}/libraries`, undefined, 'Failed to list Libraries'),

  registerLibrary: (path, name = null) =>
    jsonPost(`${API_BASE}/libraries`, { path, name }, {}, 'Failed to register the Library'),

  updateLibrary: (id, patch) =>
    fetchJson(
      `${API_BASE}/libraries/${encodeURIComponent(id)}`,
      {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(patch),
      },
      'Failed to save the Library',
    ),

  removeLibrary: (id) =>
    fetchJson(
      `${API_BASE}/libraries/${encodeURIComponent(id)}`,
      { method: 'DELETE' },
      'Failed to remove the Library',
    ),

  getLibraryPlatforms: (id) =>
    fetchJson(
      `${API_BASE}/libraries/${encodeURIComponent(id)}/platforms`,
      undefined,
      'Failed to list the Library',
    ),

  getLibraryRules: (id) =>
    fetchJson(
      `${API_BASE}/libraries/${encodeURIComponent(id)}/rules`,
      undefined,
      'Failed to load the Rules',
    ),

  saveLibraryRules: (id, rules, checkRules) =>
    fetchJson(
      `${API_BASE}/libraries/${encodeURIComponent(id)}/rules`,
      {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(
          checkRules === undefined ? { rules } : { rules, check_rules: checkRules },
        ),
      },
      'Failed to save the Rules',
    ),

  forgetLibraryConverted: (id, platformIds = null) =>
    jsonPost(
      `${API_BASE}/libraries/${encodeURIComponent(id)}/rules/forget-converted`,
      platformIds ? { platform_ids: platformIds } : {},
      {},
      'Failed to clear the conversion history',
    ),

  previewLibrary: (id, body = {}) =>
    jsonPost(
      `${API_BASE}/libraries/${encodeURIComponent(id)}/preview`, body, {}, 'Preview failed',
    ),

  runLibrary: (id, body = {}) =>
    jsonPost(`${API_BASE}/libraries/${encodeURIComponent(id)}/run`, body, {}, 'Run failed'),

  // ── Findings and folder Platform mappings (#304) ───────────────────────
  listFindings: (includeDismissed = false) =>
    fetchJson(
      `${API_BASE}/findings${includeDismissed ? '?include_dismissed=true' : ''}`,
      undefined,
      'Failed to list the Findings',
    ),

  // `fingerprint` is the cause the user was shown; the backend answers 409
  // when the Finding changed since, so a stale row acts on nothing else.
  dismissFinding: (id, fingerprint) =>
    jsonPost(
      `${API_BASE}/findings/${encodeURIComponent(id)}/dismiss`, { fingerprint }, {},
      'Failed to dismiss the Finding',
    ),

  undismissFinding: (id, fingerprint) =>
    jsonPost(
      `${API_BASE}/findings/${encodeURIComponent(id)}/undismiss`, { fingerprint }, {},
      'Failed to show the Finding again',
    ),

  checkFindings: () =>
    jsonPost(`${API_BASE}/findings/check`, {}, {}, 'Failed to check the Libraries'),

  listPlatforms: () =>
    fetchJson(`${API_BASE}/platforms`, undefined, 'Failed to list the Platforms'),

  listPlatformMappings: () =>
    fetchJson(`${API_BASE}/platform-mappings`, undefined, 'Failed to list the mappings'),

  // `platform: null` clears the folder's mapping.
  setPlatformMapping: (path, platform) =>
    fetchJson(
      `${API_BASE}/platform-mappings`,
      {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path, platform }),
      },
      'Failed to set the Platform',
    ),

  // Records the metadata to carry across a conversion. MUST be called before
  // the batch is submitted: the provider ids are read from the RomM record for
  // the source file, which goes stale once that file is converted.
  planRommRepin: (
    paths, mode, outputDir = null, duplicateAction = 'skip', platformId = null,
    outputPaths = null, generation = null,
  ) =>
    jsonPost(
      `${API_BASE}/romm/repin/plan`,
      {
        paths,
        mode,
        output_dir: outputDir,
        duplicate_action: duplicateAction,
        platform_id: platformId,
        // {source: destination} when the queue has already decided. Planning
        // predicts the destination; the batch can land elsewhere.
        output_paths: outputPaths,
        // The generation the run's first request was given, so every request
        // of one run is as old as the run (the server orders snapshots by it).
        generation,
      },
      {},
      'Failed to record RomM metadata',
    ),

  // Retires rows recorded for a batch that was then rejected. Row ids, from
  // the plan response: a destination names whichever row holds it now, which
  // after another client's re-plan is that client's live row.
  cancelRommRepin: (ids) =>
    jsonPost(
      `${API_BASE}/romm/repin/cancel`,
      { ids },
      {},
      'Failed to discard the recorded RomM metadata',
    ),

  runRommRepin: () =>
    jsonPost(`${API_BASE}/romm/repin`, {}, {}, 'Failed to re-match RomM entries'),

  listDATs: () => fetchJson(`${API_BASE}/dat/list`, undefined, 'Failed to list DATs'),

  deleteDAT: (datId) =>
    fetchJson(`${API_BASE}/dat/${datId}`, { method: 'DELETE' }, 'Failed to delete DAT'),

  getDATStats: () => fetchJson(`${API_BASE}/dat/stats`, undefined, 'Failed to get DAT stats'),

  /** Turn the Hasheous fallback on/off. `null` hands control back to the env var. */
  setHasheousEnabled(enabled) {
    return fetchJson(
      `${API_BASE}/dat/hasheous`,
      {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ enabled }),
      },
      'Failed to update the Hasheous setting',
    );
  },

  /**
   * Probe the configured Hasheous server. Resolves `{ ok, latency_ms, error }`
   * when the backend answers — `ok: false` is a reachability answer, not a
   * thrown error. Rejects only if the request itself fails (non-2xx, network),
   * so callers still need a catch.
   */
  testHasheous() {
    return jsonPost(`${API_BASE}/dat/hasheous/test`, {}, {}, 'Failed to reach Hasheous');
  },

  matchBatch(paths) {
    return jsonPost(`${API_BASE}/dat/match-batch`, { paths }, {}, 'Failed to match files');
  },

  startMatchJob(paths) {
    return jsonPost(
      `${API_BASE}/dat/match-batch/job`,
      { paths },
      {},
      'Failed to start DAT match job',
    );
  },

  getMatchCache(paths) {
    if (!paths || paths.length === 0) return Promise.resolve({ results: {} });
    return jsonPost(
      `${API_BASE}/dat/matches/lookup`,
      { paths },
      {},
      'Failed to look up DAT match cache',
    );
  },

  syncMAMERedump(tag = null) {
    return jsonPost(
      `${API_BASE}/dat/sync`,
      tag ? { tag } : {},
      {},
      'Failed to start sync',
    );
  },

  getSyncStatus: () =>
    fetchJson(`${API_BASE}/dat/sync/status`, undefined, 'Failed to get sync status'),

  cancelSync: () =>
    fetchJson(`${API_BASE}/dat/sync/cancel`, { method: 'POST' }, 'Failed to cancel sync'),
};
