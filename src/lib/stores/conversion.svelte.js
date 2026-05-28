// Conversion config store — selected tool, mode, compression, output dir,
// delete-on-verify, duplicate-check / delete-plan results. Drives the
// ConversionConfig panel and the submission path into JobsStore.

import { api } from '$lib/api/endpoints.js';
import { registry } from '$lib/tools/registry.js';
import { STORAGE_KEYS, readString, writeString } from '$lib/util/localStorage.js';
import { jobs } from './jobs.svelte.js';
import { ui } from './ui.svelte.js';

function loadPrimaryTool() {
  const raw = readString(STORAGE_KEYS.PRIMARY_TOOL, 'chdman') ?? 'chdman';
  return registry.forTool(raw) ? raw : 'chdman';
}

// Each tool declares its own `defaultMode` in the registry descriptor.
// chdman picks `createcd` (legacy/backend default for cue/bin/ISO), dolphin
// picks rvz, z3ds picks compress. No tool-specific branching here.
function defaultModeFor(toolId) {
  return registry.defaultMode(toolId) ?? 'createcd';
}

// Per-tool default compression seed. chdman accepts a comma-separated codec
// list (zlib is the historical default); Dolphin RVZ/WIA codecs are a
// disjoint set (`dolphin-tool -c [none|zstd|bzip|lzma|lzma2]`), so a chdman
// codec like 'zlib' would be rejected by the dolphin binary if the user
// submitted a Dolphin job without first opening the codec picker. Seed with
// a tool-appropriate default so the first submission always works.
function defaultCompressionFor(toolId) {
  return toolId === 'dolphin' ? ['zstd'] : ['zlib'];
}

const INITIAL_TOOL = loadPrimaryTool();

class ConversionStore {
  primaryTool = $state(INITIAL_TOOL);
  // Mode must initialize from the persisted tool — defaulting to a chdman
  // mode (createcd) when the persisted tool is dolphin/z3ds would submit
  // wrong duplicate checks and compression flags before setPrimaryTool runs.
  mode = $state(defaultModeFor(INITIAL_TOOL));
  compressionSelection = $state(defaultCompressionFor(INITIAL_TOOL));
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
   * The wire-format compression value sent to the backend.
   *
   * - chdman create/copy modes: comma-separated codec list (e.g. "zlib,lzma").
   * - Dolphin RVZ/WIA: "<codec>:<level>" (e.g. "zstd:19"). The backend's
   *   dolphin_tool service splits on `:` and passes the codec to
   *   `dolphin-tool -c <codec> -l <level>`. Sending only the level (e.g.
   *   "19") would be interpreted as the codec name and fail.
   * - "none" selection: the literal string "none".
   * - null when compression is not configurable for the current mode.
   */
  get compressionValue() {
    if (!this.supportsCompression && !this.supportsCompressionLevel) return null;
    const selection = this.compressionSelection.filter((v) => v && v !== 'none');
    if (this.compressionSelection.includes('none') && selection.length === 0) {
      return 'none';
    }
    if (this.supportsCompressionLevel) {
      // Dolphin RVZ/WIA: pick the first non-'none' codec and pair with level.
      const codec = selection[0];
      if (!codec) return 'none';
      return `${codec}:${this.dolphinCompressionLevel}`;
    }
    if (selection.length === 0) return null;
    return selection.join(',');
  }

  // ─── Setters ──────────────────────────────────────────────────────────
  setPrimaryTool(toolId) {
    const tool = registry.forTool(toolId);
    if (!tool) return;
    // No-op guard so the App.svelte $effect that bridges
    // ui.workspaceTool → this store can fire whenever Svelte considers
    // the dependency dirty without resetting the user's customized
    // mode / compression selection on every reactive trigger.
    if (this.primaryTool === toolId) return;
    this.primaryTool = toolId;
    writeString(STORAGE_KEYS.PRIMARY_TOOL, toolId);
    // Same default-mode logic as the initial load — chdman keeps createcd
    // as its default, others use their first registered mode.
    this.mode = defaultModeFor(toolId);
    this.compressionSelection = defaultCompressionFor(toolId);
  }

  setMode(mode) {
    if (!registry.specFor(mode)) return;
    this.mode = mode;
    // If the new mode doesn't support delete-on-verify (e.g. switching
    // from a chdman create mode to an extract mode), clear the flag.
    // The backend rejects the combination outright, so a sticky flag
    // would fail the next submission instead of silently degrading.
    if (!this.supportsDeleteOnVerify) this.deleteOnVerify = false;
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
        // Mask the flag at submit time too — defense in depth against
        // any code path that might set deleteOnVerify true without
        // going through setMode (the backend rejects the combination
        // for extract/raw modes).
        deleteOnVerify: this.supportsDeleteOnVerify && this.deleteOnVerify,
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
