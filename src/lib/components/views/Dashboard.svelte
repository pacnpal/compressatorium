<script>
  import { ui } from '$lib/stores/ui.svelte.js';
  import { registry } from '$lib/tools/registry.js';
  import Button from '$lib/components/ui/Button.svelte';

  const tools = registry.all();
</script>

<section class="view">
  <header class="hero">
    <h1>Welcome back</h1>
    <p class="subtitle">Pick a tool to start, or browse the workspace.</p>
  </header>

  <div class="cards">
    {#each tools as t (t.id)}
      <article class="card">
        <div class="card-head">
          <span class="card-glyph" aria-hidden="true">{t.label[0]}</span>
          <div>
            <h2 class="card-title">{t.label}</h2>
            <p class="card-hint">{t.hint}</p>
          </div>
        </div>
        <Button
          variant="secondary"
          fullWidth
          onclick={() => ui.navigate('workspace', t.id)}
        >
          Open workspace
        </Button>
      </article>
    {/each}
  </div>

  <p class="placeholder">Dashboard cards (queue summary, verification, recent conversions, DAT matches) land in P8.</p>
</section>

<style>
  .view {
    display: flex;
    flex-direction: column;
    gap: var(--space-5);
    padding: var(--space-5) var(--space-5) var(--space-7);
    max-width: var(--container-max);
    margin: 0 auto;
    width: 100%;
  }
  .hero h1 {
    font-size: var(--text-2xl);
    font-weight: var(--weight-semibold);
    color: var(--text-1);
    margin: 0;
  }
  .subtitle {
    color: var(--text-2);
    margin-top: var(--space-1);
  }
  .cards {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
    gap: var(--space-4);
  }
  .card {
    background: var(--surface-1);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-lg);
    padding: var(--space-4);
    display: flex;
    flex-direction: column;
    gap: var(--space-4);
    box-shadow: var(--elev-1);
  }
  .card-head {
    display: flex;
    align-items: center;
    gap: var(--space-3);
  }
  .card-glyph {
    width: 40px;
    height: 40px;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    border-radius: var(--radius-md);
    background: var(--accent-muted);
    color: var(--accent);
    font-weight: var(--weight-semibold);
    font-size: var(--text-lg);
    flex-shrink: 0;
  }
  .card-title {
    margin: 0;
    font-size: var(--text-lg);
    font-weight: var(--weight-semibold);
    color: var(--text-1);
  }
  .card-hint {
    margin: 0;
    font-size: var(--text-sm);
    color: var(--text-2);
  }
  .placeholder {
    color: var(--text-3);
    font-style: italic;
    font-size: var(--text-sm);
  }
</style>
