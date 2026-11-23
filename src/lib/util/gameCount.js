// How many games a set of files is (#597): the discs of one set ("Game (Disc
// 1).cue", "Game (Disc 2).cue") are one game, and so are a selected playlist's
// discs, whatever they are named. Pure, so the Node tests load it as it ships.

// "(Disc 1)", "(CD2)", "[Disk 3 of 3]": the part of a name that numbers a disc.
const DISC_TAG = /\s*[([]\s*(?:disc|disk|cd)\s*\d+(?:\s*of\s*\d+)?\s*[)\]]/gi;

function gameKey(path) {
  const text = String(path ?? '').split('::').join('/');
  const cut = Math.max(text.lastIndexOf('/'), text.lastIndexOf('\\'));
  const folder = cut < 0 ? '' : text.slice(0, cut);
  const name = text.slice(cut + 1).replace(/\.[^.]+$/, '').replace(DISC_TAG, '').trim();
  return `${folder}/${name.toLowerCase()}`;
}

/**
 * The games among *paths*. `playlists` are the selected playlist rows
 * (`{ path, parts: [{ path }] }`): their discs count as that one game.
 */
export function countGames(paths, playlists = []) {
  const inPlaylist = {};
  for (const playlist of playlists ?? []) {
    for (const part of playlist?.parts ?? []) {
      if (part?.path) inPlaylist[part.path] = `playlist:${playlist.path}`;
    }
  }
  const games = new Set();
  for (const path of paths ?? []) {
    if (path) games.add(inPlaylist[path] ?? gameKey(path));
  }
  return games.size;
}
