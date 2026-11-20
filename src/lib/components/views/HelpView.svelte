<script>
  // Short on purpose: how it works in three steps, then links into the README,
  // which holds the detail. Repo links come from package.json so they live in
  // one place (`repository.url` is the npm git form, normalized here).
  import { repository, bugs } from '../../../../package.json';

  const repoUrl = repository.url.replace(/^git\+/, '').replace(/\.git$/, '');
  const issuesUrl = bugs?.url ?? `${repoUrl}/issues`;
  const readme = `${repoUrl}/blob/latest/README.md`;

  const steps = [
    ['Add a Library', 'your games folder, or RomM.'],
    ["Set each console's format", 'or press Apply recommended.'],
    ['Check Activity', 'for anything that needs you.'],
  ];

  const links = [
    ['Supported formats', `${readme}#supported-conversions`],
    ['RomM', `${readme}#romm-integration`],
    ['Folder Libraries', `${readme}#folder-libraries`],
    ['Watch folders', `${readme}#watch-folders`],
    ['Checking and deleting safely', `${readme}#verifying-and-deleting-safely`],
    ['Game identification', `${readme}#dat-matching`],
    ['Troubleshooting', `${readme}#troubleshooting`],
    ['Setting up the container', `${repoUrl}/blob/latest/docs/DOCKER-COMPOSE.md`],
  ];
</script>

<section class="view" aria-labelledby="help-title">
  <header class="header">
    <h1 id="help-title">Help</h1>
    <p class="hint">How Compressatorium works, and where to read more.</p>
  </header>

  <article class="panel">
    <h2 class="panel-title">How it works</h2>
    <ol class="steps">
      {#each steps as [lead, rest], i (lead)}
        <li class="step">
          <span class="num" aria-hidden="true">{i + 1}</span>
          <span><strong>{lead}</strong>: {rest}</span>
        </li>
      {/each}
    </ol>
  </article>

  <article class="panel">
    <h2 class="panel-title">Converting by hand</h2>
    <p>In Files, pick games and press Convert. The plan shows each game's new format before anything runs.</p>
  </article>

  <article class="panel">
    <h2 class="panel-title">Safe by default</h2>
    <p>Originals are only deleted when you ask, and only after the new file reads back as an exact copy.</p>
  </article>

  <article class="panel">
    <h2 class="panel-title">More help</h2>
    <ul class="links">
      {#each links as [label, href] (label)}
        <li><a {href} target="_blank" rel="noopener noreferrer">{label}</a></li>
      {/each}
    </ul>
    <p class="foot">
      Something broken, or missing? <a href={issuesUrl} target="_blank" rel="noopener noreferrer">Open an issue on GitHub</a>
      with what you did, what you expected, and the app version.
    </p>
  </article>
</section>

<style>
  .view { display: flex; flex-direction: column; gap: var(--space-4); padding: var(--space-5); max-width: var(--container-max); margin: 0 auto; width: 100%; min-width: 0; }
  .header h1 { margin: 0; font-size: var(--text-2xl); font-weight: var(--weight-semibold); color: var(--text-1); }
  .hint { color: var(--text-2); margin-top: var(--space-1); }

  .panel { background: var(--surface-1); border: 1px solid var(--border-subtle); border-radius: var(--radius-lg); padding: var(--space-4); box-shadow: var(--elev-1); }
  .panel-title { margin: 0 0 var(--space-3); font-size: var(--text-base); font-weight: var(--weight-semibold); color: var(--text-1); }
  .panel p { color: var(--text-2); margin: 0; line-height: 1.6; max-width: 72ch; }
  .panel strong { color: var(--text-1); font-weight: var(--weight-semibold); }

  .steps { list-style: none; margin: 0; padding: 0; display: grid; gap: var(--space-3); grid-template-columns: repeat(auto-fit, minmax(min(100%, 220px), 1fr)); }
  .step { display: flex; gap: var(--space-3); align-items: flex-start; padding: var(--space-3); border: 1px solid var(--border-subtle); border-radius: var(--radius-md); background: var(--surface-2); color: var(--text-2); }
  .num { flex-shrink: 0; display: inline-flex; align-items: center; justify-content: center; width: 24px; height: 24px; border-radius: var(--radius-full); background: var(--accent-muted); color: var(--accent); font-weight: var(--weight-semibold); font-size: var(--text-sm); }

  .links { margin: 0 0 var(--space-3); padding-left: var(--space-5); display: grid; gap: var(--space-1); }
  .links a, .foot a { color: var(--accent); }
  .foot { font-size: var(--text-sm); }
</style>
