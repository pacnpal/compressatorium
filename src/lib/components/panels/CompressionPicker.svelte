<script>
  // Compression configuration, controlled: `value` is `{ codecs, level }` (see
  // $lib/tools/compression.js) and every change goes out through `onchange`.
  // It reads only the registry and the compat table, never a store, so the
  // Convert panel, a console's settings and Settings → Defaults share it.
  //
  // Three "styles", picked off the mode's tool:
  //   - 'multi'             chdman: a comma-separated codec list (chips).
  //   - 'single-with-level' Dolphin RVZ/WIA, nsz, CSO: one codec, plus a level
  //                         only when the codec takes one.
  //   - 'none'              fixed compression; renders nothing.
  // No tool-specific branches: a new tool declares its codecs and style in
  // registry.js and this file needs no edit.

  import { registry } from '$lib/tools/registry.js';
  import { compat } from '$lib/stores/compat.svelte.js';
  import { compatEntry, compatWarning } from '$lib/tools/compat.js';
  import {
    defaultCompression, effectiveCodec, isDefault, levelRange as rangeOf,
    toggleCodec, withCodec, withLevel,
  } from '$lib/tools/compression.js';
  import { onMount } from 'svelte';
  import Check from '@lucide/svelte/icons/check';
  import RotateCcw from '@lucide/svelte/icons/rotate-ccw';

  /**
   * @typedef {{ codecs: string[], level: number|null }} Compression
   * @typedef {Object} Props
   * @property {string} mode
   * @property {Compression} value
   * @property {(value: Compression) => void} onchange
   * @property {boolean} [compact] no frame, for a table row
   * @property {string} [label] the legend ("CHD compression")
   */

  /** @type {Props} */
  let { mode, value, onchange, compact = false, label = 'Compression' } = $props();

  const spec = $derived(registry.specFor(mode));
  const tool = $derived(registry.toolForMode(mode));
  // The mode's own codecs where it names them (WIA refuses zstd), else the tool's.
  const codecs = $derived(registry.codecsForMode(mode));
  const style = $derived(tool?.compressionStyle ?? 'none');
  const selection = $derived(value?.codecs ?? []);
  const level = $derived(value?.level ?? '');
  // The running codec's own range (Dolphin's lzma2 takes 1-9, zstd 1-22);
  // null for a codec that takes no level (none), which hides the control.
  const levelRange = $derived(rangeOf(mode, value));
  const supports = $derived(
    !!(spec?.supportsCompression || spec?.supportsCompressionLevel),
  );
  const singleCodec = $derived(effectiveCodec(mode, value));
  // Disables Reset when there is nothing to undo.
  const atDefault = $derived(isDefault(mode, value));
  const max = $derived(registry.maxCodecsFor(mode));

  // "Compatible with...": the targets that have an entry for this mode, from
  // the table the server serves. Picking one selects its preferred codecs;
  // the warning only advises, it never disables a control.
  let compatId = $state('');
  const compatTargets = $derived(compat.targetsForMode(mode));
  const compatTarget = $derived(compatTargets.find((t) => t.id === compatId) ?? null);
  const compatNote = $derived(
    compatWarning(
      compatTarget,
      mode,
      style === 'multi' ? selection : [singleCodec],
      compat.table,
    ),
  );

  onMount(() => {
    compat.load();
  });

  function chooseCompat(id) {
    compatId = id;
    const entry = compatEntry(compat.target(id), mode);
    if (!entry) return;
    if (style === 'multi') onchange({ ...value, codecs: [...entry.preferred] });
    else onchange(withCodec(mode, value, entry.preferred[0]));
  }

  function isSelected(codec) {
    return selection.includes(codec);
  }
</script>

{#if supports && codecs.length > 0}
  <fieldset class="picker" class:compact>
    <legend class="legend">{label}</legend>

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

    <details class="codecs">
      <summary>Choose codecs</summary>
      {#if style === 'multi'}
        {@const activeCount = selection.filter(
          (c) => c && c !== 'none' && codecs.some((k) => k.value === c),
        ).length}
        {@const atCap = activeCount >= max}
        <div class="chips" role="group" aria-label="Codec selection">
          <button
            type="button"
            class="chip"
            class:active={isSelected('none')}
            aria-pressed={isSelected('none')}
            onclick={() => onchange(toggleCodec(mode, value, 'none'))}
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
                title={capped ? `Up to ${max} codecs` : (codec.hint ?? '')}
                disabled={capped}
                onclick={() => onchange(toggleCodec(mode, value, codec.value))}
              >
                {#if selected}<Check size={12} aria-hidden="true" />{/if}
                {codec.label}
              </button>
            {/if}
          {/each}
        </div>
        <p class="hint">
          Up to {max}. The first one that fits each block is used.
          {#if atCap}<span class="cap-note">Limit reached, deselect one to add another.</span>{/if}
        </p>

      {:else if style === 'single-with-level'}
        <label class="single-codec">
          <span class="sublabel">Codec</span>
          <select
            value={singleCodec}
            onchange={(e) => onchange(withCodec(mode, value, e.currentTarget.value))}
            aria-label="Codec"
          >
            {#each codecs as codec (codec.value)}
              <option value={codec.value} title={codec.hint ?? ''}>{codec.label}</option>
            {/each}
          </select>
        </label>

        {#if levelRange}
          <label class="level">
            <span class="sublabel">Level</span>
            <div class="level-row">
              <input
                type="range"
                min={levelRange.min}
                max={levelRange.max}
                step="1"
                value={level === '' ? levelRange.default : level}
                oninput={(e) => onchange(withLevel(mode, value, e.currentTarget.value))}
                aria-label="Compression level"
              />
              <input
                class="level-input"
                type="number"
                min={levelRange.min}
                max={levelRange.max}
                step="1"
                value={level}
                oninput={(e) => onchange(withLevel(mode, value, e.currentTarget.value))}
                aria-label="Compression level value"
              />
            </div>
            <p class="hint">
              Higher is smaller and slower. {levelRange.min}–{levelRange.max}; default {levelRange.default}.
            </p>
          </label>
        {/if}
      {/if}
    </details>

    <div class="picker-footer">
      <button
        type="button"
        class="reset-btn"
        onclick={() => onchange(defaultCompression(mode))}
        disabled={atDefault}
        title={atDefault ? 'Already the default' : 'Go back to the default for this format'}
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
  .picker.compact {
    border: none;
    padding: 0;
    background: transparent;
  }
  .codecs summary {
    cursor: pointer;
    font-size: var(--text-sm);
    color: var(--text-2);
  }
  .codecs[open] summary { margin-bottom: var(--space-2); }
  .codecs > :not(summary) + :not(summary) { margin-top: var(--space-2); }
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
  .cap-note { color: var(--warning-text); margin-left: var(--space-1); }
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
