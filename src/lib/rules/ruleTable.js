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
  return next;
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
 * "Re-check games every night" over the consoles that have a conversion
 * Rule: true when every one has a check Rule on, false when none does,
 * 'mixed' otherwise (the box shows indeterminate).
 */
export function nightlyCheckState(platformIds, rules, checkRules) {
  const converting = platformIds.filter((id) => rules?.[String(id)]?.mode);
  const on = converting.filter((id) => checkRules?.[String(id)]?.enabled);
  if (on.length === 0) return false;
  return on.length === converting.length ? true : 'mixed';
}
