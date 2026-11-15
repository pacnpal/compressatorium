// Check Rules (#305): a Rule with no target format checks Images instead of
// converting them. Stored as a Rule whose `mode` is empty; "off" is no Rule at
// all. The editor's target select needs a value for "check" that no real mode
// can take, so it uses CHECK_RULE and maps it here.

export const CHECK_RULE = ':check';

/** Whether a Rule is a check Rule: stored (configured) with no target. */
export function isCheckRule(rule, configured) {
  return Boolean(configured) && !rule?.mode;
}

/** The target select's value: off (''), a check Rule, or the mode. */
export function selectValue(rule, configured) {
  if (isCheckRule(rule, configured)) return CHECK_RULE;
  return rule?.mode ?? '';
}

/**
 * *rule* turned into a check Rule. The schedule (window, weekdays, enabled)
 * carries over; what described a conversion does not, and a check never
 * deletes anything.
 */
export function checkRuleFrom(rule) {
  return {
    ...rule,
    mode: '',
    compression: null,
    compression_level: null,
    split: false,
    output_dir: null,
    verify_after: false,
    delete_on_verify: false,
  };
}

/** The row summary for a check Rule. */
export function checkSummary(rule) {
  return `${rule?.check_budget_gb ?? 50} GB a night`;
}
