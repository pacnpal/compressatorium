<script>
  // Compression configuration. Three "styles" supported, picked off the
  // tool descriptor:
  //   - 'multi'             chdman: comma-separated codec list (chips).
  //   - 'single-with-level' Dolphin RVZ/WIA / CSO: one codec, plus a numeric
  //                         level only when the mode sets supportsCompressionLevel
  //                         (CSO's effort presets show the dropdown alone).
  //   - 'none'              z3ds: fixed compression; renders nothing.
  // The CompressionPicker has zero tool-specific branches, adding a new
  // tool means declaring its codec list + style in registry.js and
  // (optionally) a level range; this file does not need editing.

  import { conversion } from '$lib/stores/conversion.svelte.js';
  import { registry } from '$lib/tools/registry.js';
  import { compat } from '$lib/stores/compat.svelte.js';
  import { compatEntry, compatWarning } from '$lib/tools/compat.js';
  import { onMount } from 'svelte';
  import { toast } from 'svelte-sonner';
  import Check from '@lucide/svelte/icons/check';
  import RotateCcw from '@lucide/svelte/icons/rotate-ccw';

  const tool = $derived(conversion.currentTool);
  const spec = $derived(conversion.currentSpec);
  // The mode's own codecs where it names them (WIA refuses zstd), else the tool's.
  const codecs = $derived(registry.codecsForMode(spec?.mode));
  const style = $derived(tool?.compressionStyle ?? 'none');
  const selection = $derived(conversion.compressionSelection);
  // The slider and the number box read this one value and range; the store
  // reconciles it on every mode or codec change, so they never disagree.
  const level = $derived(conversion.compressionLevel);
  // The selected codec's own range (Dolphin's lzma2 takes 1-9, zstd 1-22);
  // null for a codec that takes no level (none), which hides the control.
  const levelRange = $derived(conversion.levelRange);
  const supports = $derived(
    !!(spec?.supportsCompression || spec?.supportsCompressionLevel),
  );
  const singleCodec = $derived(conversion.singleCodec);
  // True when the current tool's settings already match its defaults; used to
  // disable the Reset button so it's not a silent no-op.
  const atDefault = $derived(conversion.isCompressionDefault);

  // "Compatible with...": the targets that have an entry for this mode, from
  // the table the server serves. Picking one selects its preferred codecs;
  // the warning only advises, it never disables a control.
  let compatId = $state('');
  const compatTargets = $derived(compat.targetsForMode(spec?.mode));
  const compatTarget = $derived(compatTargets.find((t) => t.id === compatId) ?? null);
  const compatNote = $derived(
    compatWarning(compatTarget, spec?.mode, style === 'multi' ? selection : [singleCodec]),
  );

  onMount(() => {
    compat.load();
  });

  function chooseCompat(id) {
    compatId = id;
    const entry = compatEntry(compat.target(id), spec?.mode);
    if (!entry) return;
    if (style === 'multi') conversion.setCompression(entry.preferred);
    else conversion.setSingleCodec(entry.preferred[0]);
  }

  function isSelected(value) {
    return selection.includes(value);
  }

  function resetToDefault() {
    conversion.resetCompression();
    toast.success(`${tool?.label ?? 'Compression'} reset to default`, { duration: 2500 });
  }
</script>

{#if supports && codecs.length > 0}
  <fieldset class="picker">
    <legend class="legend">Compression</legend>

    {#if compatTargets.length > 0}
      <label class="single-codec">
        <span class="sublabel">Compatible with…</span>
        <select
          value={compatId}
          onchange={(e) => chooseCompat(e.currentTarget.value)}
          aria-label="Compatible with"
        >
          <option value="">Any emulator</option>
          {#each compatTargets as target (target.id)}
            <option value={target.id}>{target.label}</option>
          {/each}
        </select>
      </label>
      {#if compatNote}
        <p class="compat-warning" role="status">{compatNote} You can still convert.</p>
      {/if}
    {/if}

    {#if style === 'multi'}
      {@const max = conversion.CHDMAN_MAX_CODECS ?? 4}
      {@const activeCount = selection.filter((c) => c && c !== 'none').length}
      {@const atCap = activeCount >= max}
      <div class="chips" role="group" aria-label="Codec selection">
        <button
          type="button"
          class="chip"
          class:active={isSelected('none')}
          aria-pressed={isSelected('none')}
          onclick={() => conversion.toggleCodec('none')}
        >
          {#if isSelected('none')}<Check size={12} aria-hidden="true" />{/if}
          None
        </button>
        {#each codecs as codec (codec.value)}
          {#if codec.value !== 'none'}
            {@const selected = isSelected(codec.value)}
            {@const capped = atCap && !selected}
            <button
              type="button"
              class="chip"
              class:active={selected}
              aria-pressed={selected}
              title={capped ? `chdman accepts at most ${max} codecs` : (codec.hint ?? '')}
              disabled={capped}
              onclick={() => conversion.toggleCodec(codec.value)}
            >
              {#if selected}<Check size={12} aria-hidden="true" />{/if}
              {codec.label}
            </button>
          {/if}
        {/each}
      </div>
      <p class="hint">
        Select up to {max} codecs. chdman tries them in order.
        {#if atCap}<span class="cap-note">Limit reached, deselect one to add another.</span>{/if}
      </p>

    {:else if style === 'single-with-level'}
      <label class="single-codec">
        <span class="sublabel">Codec</span>
        <select
          value={singleCodec}
          onchange={(e) => conversion.setSingleCodec(e.currentTarget.value)}
          aria-label="Codec"
        >
          {#each codecs as codec (codec.value)}
            <option value={codec.value} title={codec.hint ?? ''}>{codec.label}</option>
          {/each}
        </select>
      </label>

      {#if levelRange}
        <label class="level">
          <span class="sublabel">Level (compression effort)</span>
          <div class="level-row">
            <input
              type="range"
              min={levelRange.min}
              max={levelRange.max}
              step="1"
              value={level}
              oninput={(e) => conversion.setCompressionLevel(e.currentTarget.value)}
              aria-label="Compression level"
            />
            <input
              class="level-input"
              type="number"
              min={levelRange.min}
              max={levelRange.max}
              step="1"
              value={level}
              oninput={(e) => conversion.setCompressionLevel(e.currentTarget.value)}
              aria-label="Compression level value"
            />
          </div>
          <p class="hint">
            {levelRange.min}–{levelRange.max}; default {levelRange.default}{#if singleCodec === 'zstd' && levelRange.default === 19}, which matches MAME Redump{/if}.
          </p>
        </label>
      {/if}
    {/if}

    <div class="picker-footer">
      <button
        type="button"
        class="reset-btn"
        onclick={resetToDefault}
        disabled={atDefault}
        title={atDefault
          ? 'Compression is already at this tool’s default'
          : 'Reset this tool’s compression settings to the default'}
      >
        <RotateCcw size={12} aria-hidden="true" /> Reset to default
      </button>
    </div>
  </fieldset>
{/if}

<style>
  .picker {
    display: flex;
    flex-direction: column;
    gap: var(--space-2);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-md);
    padding: var(--space-3);
    background: var(--surface-2);
    margin: 0;
    min-width: 0;
  }
  .legend {
    font-size: var(--text-xs);
    text-transform: uppercase;
    letter-spacing: 0.05em;
    color: var(--text-2);
    font-weight: var(--weight-semibold);
    padding: 0 var(--space-1);
  }
  .chips {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-1);
  }
  .chip {
    display: inline-flex;
    align-items: center;
    gap: var(--space-1);
    border: 1px solid var(--border-subtle);
    background: var(--surface-1);
    color: var(--text-2);
    border-radius: var(--radius-full);
    padding: 4px 10px;
    font-size: var(--text-xs);
    cursor: pointer;
    transition: background var(--dur-fast) var(--ease-out),
      color var(--dur-fast) var(--ease-out),
      border-color var(--dur-fast) var(--ease-out);
  }
  .chip:hover {
    background: var(--surface-3);
    color: var(--text-1);
  }
  .chip.active {
    background: var(--accent-muted);
    color: var(--accent);
    border-color: var(--accent);
  }
  .chip:focus-visible {
    outline: none;
    box-shadow: var(--focus-ring);
  }

  .single-codec,
  .level {
    display: flex;
    flex-direction: column;
    gap: var(--space-1);
  }
  .sublabel {
    font-size: var(--text-xs);
    color: var(--text-2);
  }
  select,
  input[type='number'] {
    background: var(--surface-1);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-sm);
    color: var(--text-1);
    font-size: var(--text-sm);
    padding: 6px 8px;
  }
  select:focus-visible,
  input:focus-visible {
    outline: none;
    border-color: var(--accent);
    box-shadow: var(--focus-ring);
  }
  .level-row {
    display: flex;
    align-items: center;
    gap: var(--space-2);
  }
  .level-row input[type='range'] {
    flex: 1;
    accent-color: var(--accent);
  }
  .level-input {
    width: 60px;
    text-align: right;
    font-variant-numeric: tabular-nums;
  }
  .hint {
    margin: 0;
    color: var(--text-3);
    font-size: var(--text-xs);
  }
  .cap-note { color: var(--warning); margin-left: var(--space-1); }
  .compat-warning {
    margin: 0;
    padding: var(--space-2);
    border-radius: var(--radius-sm);
    border: 1px solid var(--warning);
    background: var(--warning-muted);
    color: var(--text-1);
    font-size: var(--text-xs);
  }
  .chip:disabled {
    opacity: 0.45;
    cursor: not-allowed;
  }

  .picker-footer {
    display: flex;
    justify-content: flex-end;
    margin-top: var(--space-1);
  }
  .reset-btn {
    display: inline-flex;
    align-items: center;
    gap: var(--space-1);
    border: 1px solid var(--border-subtle);
    background: var(--surface-1);
    color: var(--text-2);
    border-radius: var(--radius-sm);
    padding: 4px 10px;
    font-size: var(--text-xs);
    cursor: pointer;
    transition: background var(--dur-fast) var(--ease-out),
      color var(--dur-fast) var(--ease-out),
      border-color var(--dur-fast) var(--ease-out);
  }
  .reset-btn:hover:not(:disabled) {
    background: var(--surface-3);
    color: var(--text-1);
  }
  .reset-btn:focus-visible {
    outline: none;
    box-shadow: var(--focus-ring);
  }
  .reset-btn:disabled {
    opacity: 0.45;
    cursor: not-allowed;
  }
</style>
