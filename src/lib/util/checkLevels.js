// A Rule's check level (#302): which check must pass before a delete-source
// job removes its Source. The values are the backend's `rules.CHECK_LEVELS`,
// held equal by tests/test_frontend_rule_check_level_302.py. They live here
// rather than in the rules API's `options`, because #296's characterization
// test pins that response shape.

export const CHECK_LEVEL = Object.freeze({
  roundTrip: 'round_trip',
  containerOnly: 'container_only',
});

// What Preview says about the check a delete-source batch will pass. Besides
// the two stored levels, Preview can show `recognized`: the effective level of
// a mode whose proof is Recognized (#423), whatever level the Rule stores.
export const CHECK_LEVEL_LABELS = Object.freeze({
  [CHECK_LEVEL.roundTrip]: 'Round-trip before deleting the source',
  [CHECK_LEVEL.containerOnly]: 'container check only before deleting the source',
  recognized: 'Recognized (in a DAT or Hasheous) before deleting the source',
});
