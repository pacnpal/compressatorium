// Every route the app has, and every old one it still answers. Pure, no
// imports: the ui store and the router read it, and tests evaluate it in Node.
//
//   #/files[/<abs path>]                     Files, optionally opened at a folder
//   #/libraries[/<id>[/<tab>]]               the Library list, or one Library
//   #/activity[/<tab>[/<library id>]]        Needs attention, Running, History
//   #/settings[/<section>]                   Settings, scrolled to a section
//   #/help
//
// Ids and paths are one URI-encoded segment each (`folder:3`, `/data/roms`).

export const VIEWS = Object.freeze(['files', 'libraries', 'activity', 'settings', 'help']);

// Each view's top-bar title. A Library page shows its own name instead.
export const VIEW_TITLES = Object.freeze({
  files: 'Files',
  libraries: 'Libraries',
  activity: 'Activity',
  settings: 'Settings',
  help: 'Help',
});

export const LIBRARY_TABS = Object.freeze(['browse', 'automation', 'connection']);
export const ACTIVITY_TABS = Object.freeze(['attention', 'running', 'history']);
export const SETTINGS_SECTIONS = Object.freeze([
  'connections', 'identification', 'notifications', 'defaults', 'converters',
]);

export const DEFAULT_HASH = '#/files';

// The old views with a fixed new home. `rules` keeps its Library id, so
// `legacyRedirect` handles it on its own.
const LEGACY = new Map([
  ['workspace', '#/files'],
  ['dashboard', '#/activity'],
  ['romm', '#/libraries/romm'],
  ['findings', '#/activity/attention'],
  ['ledger', '#/activity/history'],
  ['dat', '#/settings/identification'],
]);

function decode(segment) {
  try {
    return decodeURIComponent(segment);
  } catch {
    return segment;
  }
}

// The path segments of a hash, without the leading `#/` or a `?query`.
function segments(hash) {
  const bare = String(hash ?? '').replace(/^#/, '').split('?', 1)[0];
  return bare.split('/').filter(Boolean);
}

/**
 * The hash an old or unknown route moves to, or null when *hash* is already
 * one of the current routes.
 */
export function legacyRedirect(hash) {
  const [view, ...rest] = segments(hash);
  if (!view) return DEFAULT_HASH;
  if (VIEWS.includes(view)) return null;
  if (view === 'rules') {
    if (!rest[0]) return '#/libraries';
    return `#/libraries/${encodeURIComponent(decode(rest[0]))}/automation`;
  }
  return LEGACY.get(view) ?? DEFAULT_HASH;
}

/**
 * `{ view, id, tab, path }` for a current route. An unknown view reads as
 * Files; an unknown tab or section reads as none (the view's own default).
 */
export function parseHash(hash) {
  const [first, ...rest] = segments(hash);
  const view = VIEWS.includes(first) ? first : 'files';
  const out = { view, id: null, tab: null, path: null };
  if (view === 'files') {
    out.path = rest.length ? decode(rest.join('/')) : null;
  } else if (view === 'libraries') {
    out.id = rest[0] ? decode(rest[0]) : null;
    out.tab = out.id && LIBRARY_TABS.includes(rest[1]) ? rest[1] : null;
  } else if (view === 'activity') {
    out.tab = ACTIVITY_TABS.includes(rest[0]) ? rest[0] : null;
    out.id = out.tab === 'attention' && rest[1] ? decode(rest[1]) : null;
  } else if (view === 'settings') {
    out.tab = SETTINGS_SECTIONS.includes(rest[0]) ? rest[0] : null;
  }
  return out;
}

/** The hash for *view* and its options; the inverse of `parseHash`. */
export function hashFor(view, { id = null, tab = null, path = null } = {}) {
  const enc = encodeURIComponent;
  switch (view) {
    case 'files':
      return path ? `#/files/${enc(path)}` : '#/files';
    case 'libraries':
      if (!id) return '#/libraries';
      return `#/libraries/${enc(id)}${LIBRARY_TABS.includes(tab) ? `/${tab}` : ''}`;
    case 'activity':
      if (!ACTIVITY_TABS.includes(tab)) return '#/activity';
      return `#/activity/${tab}${tab === 'attention' && id ? `/${enc(id)}` : ''}`;
    case 'settings':
      return SETTINGS_SECTIONS.includes(tab) ? `#/settings/${tab}` : '#/settings';
    case 'help':
      return '#/help';
    default:
      return DEFAULT_HASH;
  }
}
