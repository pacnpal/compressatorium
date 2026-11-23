// How a skipped plan row reads: the line it joins in the Convert plan.
// Pure (no DOM, no Svelte, no registry), so it runs under Node in the tests.

// The server's words for a tool that can't run here (`not_ready_message`):
// "<display name> isn't ready: <reason>".
const NOT_READY = /^(.+?) isn't ready: ([\s\S]*)$/;

// A reason that means the program isn't there at all, rather than there but
// unable to run.
const NOT_FOUND = /not found|no such file|not installed/i;

/**
 * The line a skipped row joins: `{ key, kind, label }`, `kind` one of `done`,
 * `pick` (Pick console), `missing` (a converter that can't run here, with
 * `tool`, `installed` and the raw reason as `why`) or `skip`.
 */
export function skipLine(reason) {
  const text = String(reason ?? 'Skipped');
  if (text.startsWith('Already ')) {
    return { key: text, kind: 'done', label: `already ${text.slice('Already '.length)}` };
  }
  if (text.startsWith('Unknown console')) {
    return { key: 'unknown', kind: 'pick', label: 'skipped: unknown console' };
  }
  if (text.startsWith('Console unclear')) {
    return { key: 'unclear', kind: 'pick', label: 'skipped: console unclear' };
  }
  const notReady = NOT_READY.exec(text);
  if (notReady) {
    const [, tool, why] = notReady;
    return {
      key: `missing|${tool}`, kind: 'missing', tool, why: text,
      installed: !NOT_FOUND.test(why),
    };
  }
  return { key: text, kind: 'skip', label: `skipped: ${text}` };
}

/**
 * "3 games need CHDMAN to become CHD, which isn't installed": the games a
 * missing converter would convert, in one line. `format` is what it would
 * make of them, when known.
 */
export function missingLabel(count, tool, format, installed) {
  const who = count === 1 ? '1 game needs' : `${count} games need`;
  const what = format ? `${tool} to become ${format}` : tool;
  return `${who} ${what}, which ${installed ? "can't run here" : "isn't installed"}`;
}
