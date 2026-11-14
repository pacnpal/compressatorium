// One-time notices for the rule editor (#302). Shared by every Library's
// editor, so it lives outside the RomM store.
//
// `checkLevel`: the upgrade moved delete-source Rules to the Round-trip check.
// The server decides whether to show it (only when a Rule was switched) and
// stores the dismissal, so it stays dismissed across browsers and restarts.

import { api } from '$lib/api/endpoints.js';

class RuleNoticesStore {
  checkLevel = $state(null);

  async load() {
    try {
      const data = await api.getRuleNotices();
      this.checkLevel = data?.check_level ?? null;
    } catch {
      // A notice is not worth an error banner; the next open tries again.
    }
  }

  async dismissCheckLevel() {
    // Hidden at once; the server's answer replaces it.
    this.checkLevel = { ...(this.checkLevel ?? {}), show: false };
    const data = await api.dismissCheckLevelNotice();
    this.checkLevel = data?.check_level ?? this.checkLevel;
  }
}

export const ruleNotices = new RuleNoticesStore();
