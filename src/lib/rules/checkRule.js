// Check Rules (#305): a Platform's second kind of Rule. It has no target
// format: once a day it checks the stalest Images until its budget is used.
// Stored apart from the conversion Rule (`checkRules` in the rules source), so
// a Platform can have one of each.

/**
 * A new check Rule from the backend's Rule defaults. The schedule defaults
 * carry over; what describes a conversion does not, and a check never deletes
 * anything. On, since the operator just asked for it.
 */
export function newCheckRule(defaults) {
  return {
    ...(defaults ?? {}),
    mode: '',
    enabled: true,
    compression: null,
    compression_level: null,
    split: false,
    output_dir: null,
    verify_after: false,
    delete_on_verify: false,
  };
}

