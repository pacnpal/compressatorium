// Conversion config store — selected tool, mode, compression, output dir,
// delete-on-verify, duplicate-check / delete-plan results. Drives the
// ConversionConfig panel and the submission path into JobsStore.

import { api } from '$lib/api/endpoints.js';
import { registry } from '$lib/tools/registry.js';
import { STORAGE_KEYS, readString, writeString } from '$lib/util/localStorage.js';
import { jobs } from './jobs.svelte.js';
import { ui } from './ui.svelte.js';

const DEFAULT_MODE = 'createcd';

function loadPrimaryTool() {
  return readString(STORAGE_KEYS.PRIMARY_TOOL, 'chdman') ?? 'chdman';
}

class ConversionStore {
  primaryTool = $state(loadPrimaryTool());
  mode = $state(DEFAULT_MODE);
  compressionSelection = $state(['zlib']);
  dolphinCompressionLevel = $state('19');
  outputDir = $state('');
  deleteOnVerify = $state(false);
  customFilterMode = $state(false);

  duplicateCheck = $state(null);
  deletePlan = $state(null);
  converting = $state(false);

  // ─── Derived ──────────────────────────────────────────────────────────
  get currentTool() {
    return registry.forTool(this.primaryTool);
  }

  get currentSpec() {
    return registry.specFor(this.mode);
  }

  get supportsCompression() {
    return !!this.currentSpec?.supportsCompression;
  }

  get supportsCompressionLevel() {
    return !!this.currentSpec?.supportsCompressionLevel;
  }

  get supportsDeleteOnVerify() {
    return !!this.currentSpec?.supportsDeleteOnVerify;
  }

  get allowsArchiveInput() {
    return !!this.currentSpec?.allowsArchiveInput;
  }

  /**
   * The wire-format compression value sent to the backend. Backend expects:
   * - comma-separated chdman codec list, or
   * - dolphin compression level string (rvz/wia only), or
   * - null if compression is unsupported by the current mode.
   */
  get compressionValue() {
    if (!this.supportsCompression && !this.supportsCompressionLevel) return null;
    if (this.supportsCompressionLevel) return this.dolphinCompressionLevel;
    if (this.compressionSelection.length === 0) return null;
    return this.compressionSelection.join(',');
  }

  // ─── Setters ──────────────────────────────────────────────────────────
  setPrimaryTool(toolId) {
    const tool = registry.forTool(toolId);
    if (!tool) return;
    this.primaryTool = toolId;
    writeString(STORAGE_KEYS.PRIMARY_TOOL, toolId);
    // Reset mode to the first declared mode of the new tool.
    const firstMode = tool.modes[0];
    if (firstMode) this.mode = firstMode.mode;
    this.compressionSelection = ['zlib'];
  }

  setMode(mode) {
    if (!registry.specFor(mode)) return;
    this.mode = mode;
  }

  setCompression(list) {
    this.compressionSelection = Array.isArray(list) ? list.slice() : [];
  }

  setDolphinLevel(level) {
    this.dolphinCompressionLevel = String(level);
  }

  // ─── Preflight ────────────────────────────────────────────────────────
  async checkDuplicates(filePaths) {
    if (!filePaths?.length) return null;
    try {
      this.duplicateCheck = await api.checkDuplicates(
        filePaths,
        this.outputDir || null,
        this.mode,
      );
      return this.duplicateCheck;
    } catch (e) {
      ui.notify(e?.message ?? 'Failed to check duplicates', 'error');
      this.duplicateCheck = null;
      throw e;
    }
  }

  clearDuplicateCheck() {
    this.duplicateCheck = null;
  }

  async fetchDeletePlan(filePaths) {
    if (!filePaths?.length) return null;
    try {
      this.deletePlan = await api.getDeletePlan(filePaths, this.mode);
      return this.deletePlan;
    } catch (e) {
      ui.notify(e?.message ?? 'Failed to build delete plan', 'error');
      this.deletePlan = null;
      throw e;
    }
  }

  clearDeletePlan() {
    this.deletePlan = null;
  }

  // ─── Submission ───────────────────────────────────────────────────────
  async submit(filePaths, { duplicateAction = 'skip' } = {}) {
    if (!filePaths?.length) return null;
    this.converting = true;
    try {
      const result = await jobs.createBatch(filePaths, this.mode, {
        outputDir: this.outputDir || null,
        duplicateAction,
        compression: this.compressionValue,
        deleteOnVerify: this.deleteOnVerify,
      });
      ui.notify(`Queued ${filePaths.length} job(s)`, 'success');
      return result;
    } catch (e) {
      ui.notify(e?.message ?? 'Failed to create jobs', 'error');
      throw e;
    } finally {
      this.converting = false;
    }
  }
}

export const conversion = new ConversionStore();
