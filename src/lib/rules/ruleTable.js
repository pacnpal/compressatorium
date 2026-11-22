// How one edit in a Library's console table (RuleTable / RuleAdvanced) moves a
// console's Rule. Pure, no imports: the Node tests load it as it ships.

/**
 * The Rule after *patch*.
 *
 * - A new Rule (*isNew*) takes the browser's *zone*: the backend default's
 *   "UTC" is truthy, so `current.timezone ||` alone kept UTC and a 22:00
 *   window written in Berlin ran at 22:00 UTC. A saved Rule keeps its zone.
 * - Picking a target is the ask to convert, so it turns the Rule on (#520).
 *   It also drops what described the old target: `compression: "max"` from a
 *   CSO Rule would fail every CHDMAN job it queued.
 * - Deleting the original always checks the new file first, so turning
 *   delete on turns the check on.
 */
export function ruleUpdate(current, patch, { isNew = false, zone = 'UTC' } = {}) {
  const retarget = patch.mode !== undefined && patch.mode !== current?.mode;
  const next = {
    ...current,
    timezone: isNew ? zone : (current?.timezone || zone),
    ...(retarget
      ? { compression: null, compression_level: null, split: false, enabled: true }
      : {}),
    ...patch,
  };
  if (next.delete_on_verify) next.verify_after = true;
  // A value the server refused and paused the console for, now changed: drop
  // its flag (the server keeps a sent one) and, when nothing else still holds
  // it, turn the console back on, as it was before the refusal.
  let fixed = false;
  for (const [flag, fields] of Object.entries(VALIDATED)) {
    if (next[flag] && fields.some((f) => f in patch)) {
      delete next[flag];
      fixed = true;
    }
  }
  if (fixed && !Object.keys(VALIDATED).some((flag) => next[flag])) next.enabled = true;
  return next;
}

// The server's refusal flags, each with the fields that fix it.
const VALIDATED = Object.freeze({
  invalid_pattern: ['include_pattern', 'exclude_pattern'],
  invalid_output_dir: ['output_dir'],
  invalid_window: ['window_start', 'window_end'],
});

/** A number field's value, or null when it's blank or not a number (nothing is saved). */
export function numberValue(raw) {
  if (raw === '' || raw == null) return null;
  const n = Number(raw);
  return Number.isFinite(n) ? n : null;
}

/**
 * The safety fields a console's new Rule starts with, from the saved
 * defaults: delete only where *spec* can delete its original safely.
 */
export function newRuleSafety(spec, defaults) {
  const canDelete = !!spec?.supportsDeleteOnVerify;
  const deleteOnVerify = canDelete && defaults?.delete_on_verify === true;
  return {
    verify_after: deleteOnVerify || defaults?.verify_after !== false,
    delete_on_verify: deleteOnVerify,
  };
}

/**
 * The consoles "Re-check games every night" covers: every one with games or
 * a conversion Rule, since games already compressed need no format to be
 * re-checked. With none, the re-checks left behind
 * ("Don't convert" keeps them), so they can be turned off (CodeRabbit
 * 4652729352).
 */
export function nightlyConsoles(platforms, rules, checkRules, imageCount) {
  const ids = platforms.map((p) => String(p.id));
  const covered = ids.filter((id, i) => rules?.[id]?.mode || imageCount(platforms[i]) > 0);
  return covered.length ? covered : ids.filter((id) => checkRules?.[id]);
}

/**
 * "Re-check games every night" over *covered* (`nightlyConsoles`): true when
 * every one has a check Rule on, false when none does, 'mixed' otherwise (the
 * box shows indeterminate).
 */
export function nightlyCheckState(covered, checkRules) {
  const on = covered.filter((id) => checkRules?.[id]?.enabled);
  if (on.length === 0) return false;
  return on.length === covered.length ? true : 'mixed';
}
