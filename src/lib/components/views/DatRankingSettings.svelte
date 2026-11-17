<script>
  /**
   * The DAT ranking (#424): which imported DAT names an Image that more than
   * one DAT knows. Kept separately for discs and cartridges; until it is saved
   * the server orders by family (Redump first for discs, No-Intro first for
   * cartridges), and DATs sharing a rank are ones it doesn't order.
   */
  import { onMount } from 'svelte';
  import { toast } from 'svelte-sonner';
  import { api } from '$lib/api/endpoints.js';
  import Button from '$lib/components/ui/Button.svelte';
  import Badge from '$lib/components/ui/Badge.svelte';

  const KINDS = [
    { key: 'disc', title: 'Disc Images' },
    { key: 'cartridge', title: 'Cartridge Images' },
  ];
  const FAMILY_LABELS = {
    redump: 'Redump',
    'no-intro': 'No-Intro',
    tosec: 'TOSEC',
    mame: 'MAME',
    other: 'Other',
  };

  let ranking = $state({ disc: [], cartridge: [] });
  let loaded = $state(false);
  let dirty = $state(false);
  let busy = $state(false);

  async function load() {
    try {
      ranking = await api.getDATRanking();
      dirty = false;
    } catch (e) {
      toast.error(e?.message ?? 'Failed to load the DAT ranking');
    } finally {
      loaded = true;
    }
  }

  onMount(load);

  function move(kind, index, delta) {
    const list = ranking[kind];
    const [item] = list.splice(index, 1);
    list.splice(index + delta, 0, item);
    dirty = true;
  }

  // A DAT tied with the one above it: the ranking doesn't order the pair.
  const tiedWithAbove = (list, i) => !dirty && i > 0 && list[i].rank === list[i - 1].rank;

  async function save() {
    busy = true;
    try {
      ranking = await api.setDATRanking({
        disc: ranking.disc.map((d) => d.id),
        cartridge: ranking.cartridge.map((d) => d.id),
      });
      dirty = false;
      toast.success('DAT ranking saved');
    } catch (e) {
      toast.error(e?.message ?? 'Failed to save the DAT ranking');
    } finally {
      busy = false;
    }
  }
</script>

<section class="card" id="dat-ranking" aria-labelledby="dat-ranking-title">
  <header>
    <h2 id="dat-ranking-title">DAT ranking</h2>
    <p>
      When more than one imported DAT knows an Image, the highest one here names it.
      Two DATs at the same rank that name it differently are reported as a conflict.
    </p>
  </header>

  {#if !loaded}
    <p class="muted">Loading…</p>
  {:else if ranking.disc.length === 0}
    <p class="muted">No DATs imported yet. Import them on the DAT page.</p>
  {:else}
    <div class="lists">
      {#each KINDS as kind (kind.key)}
        {@const list = ranking[kind.key]}
        <div class="list">
          <h3>{kind.title}</h3>
          <ol>
            {#each list as d, i (d.id)}
              <li>
                <span class="pos">{i + 1}</span>
                <span class="name">{d.name}</span>
                <Badge tone="neutral" size="sm">{FAMILY_LABELS[d.family] ?? d.family}</Badge>
                {#if tiedWithAbove(list, i)}<span class="muted">same rank as above</span>{/if}
                <span class="moves">
                  <Button size="sm" variant="ghost" disabled={i === 0}
                    ariaLabel="Move {d.name} up" onclick={() => move(kind.key, i, -1)}>Up</Button>
                  <Button size="sm" variant="ghost" disabled={i === list.length - 1}
                    ariaLabel="Move {d.name} down" onclick={() => move(kind.key, i, 1)}>Down</Button>
                </span>
              </li>
            {/each}
          </ol>
        </div>
      {/each}
    </div>
    <div class="actions">
      <Button disabled={busy || !dirty} onclick={save}>Save ranking</Button>
      {#if dirty}<Button variant="ghost" disabled={busy} onclick={load}>Discard</Button>{/if}
    </div>
  {/if}
</section>

<style>
  .card {
    background: var(--surface-1);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-lg);
    padding: var(--space-4);
    display: grid;
    gap: var(--space-3);
    max-width: 720px;
  }
  header h2 { margin: 0; font-size: var(--text-lg); font-weight: var(--weight-semibold); }
  header p { margin: var(--space-1) 0 0; color: var(--text-2); font-size: var(--text-sm); }
  h3 { margin: 0 0 var(--space-2); font-size: var(--text-md, var(--text-sm)); font-weight: var(--weight-medium); }
  .lists { display: grid; gap: var(--space-4); }
  ol { list-style: none; margin: 0; padding: 0; display: grid; gap: var(--space-2); }
  li {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    flex-wrap: wrap;
    padding: var(--space-2) var(--space-3);
    background: var(--surface-2);
    border-radius: var(--radius-md);
  }
  .pos { color: var(--text-2); font-variant-numeric: tabular-nums; min-width: 1.5em; }
  .name { font-weight: var(--weight-medium); overflow-wrap: anywhere; }
  .moves { margin-left: auto; display: flex; gap: var(--space-1); }
  .muted { color: var(--text-2); font-size: var(--text-sm); }
  .actions { display: flex; gap: var(--space-2); flex-wrap: wrap; }
</style>
