# RomM integration roadmap

Where the RomM integration stands, what RomM itself now offers, and what to build
next, ordered by how much a user would feel it. Researched against RomM `master`
at `273b85c` (2026-10-25), latest release **5.4.1** (2026-10-22). Version tags
below say which RomM release introduced an API; anything marked **verify** needs
a spike against a live instance before it is relied on.

## Where we are today

Compressatorium talks to six RomM endpoints (`services/romm/client.py`):

| Endpoint | Used for |
|---|---|
| `GET /api/heartbeat` | Test connection / status |
| `GET /api/platforms` | Platform picker, per-platform tool narrowing |
| `GET /api/roms?platform_ids=…&with_files=true` | Library listing, automation sweeps |
| `GET /api/roms/{id}` | Single record |
| `GET /api/roms/by-hash?sha1_hash=…` | Re-pin: find the record RomM created for the output |
| `PUT /api/roms/{id}` (multipart) | Re-pin: write provider ids back |

The flow for a format RomM cannot hash-match (RVZ/CSO/NSZ/WUX/Z3DS) is:
snapshot provider ids → convert → *wait for RomM's own scan* → user presses
**Re-match in RomM** (or it runs on page load) → SHA-1 lookup → write ids. RomM
creates a **new** ROM record for the output; the old one is left behind.

## What changed in RomM since this integration was designed

Taken from the release notes and code of each tag.

- **4.8** — client API tokens with **8-digit pairing codes**
  (`POST /api/client-tokens/exchange`, unauthenticated, single use, 5 min).
  Chunked uploads.
- **4.9** — per-file `chd_sha1_hash` (the disc digest DATs publish), composite
  hashes for archives, `GET /api/roms` can include siblings.
- **5.0** — granular permission groups; native RetroAchievements hashing of
  RVZ/WIA and CSO/ZSO; scan socket events now need `tasks.run`.
- **5.1** — a renamed/moved file is **reassociated with its `missing` record**
  when crc+md5+sha1 all match (keeps collections, notes, saves).
- **5.2** — `with_total=false`, `GET /roms/random`, hash search in the gallery.
- **5.3** — `filesystem.structure` templates are mandatory (no auto-detect).
  Title ids are read from binaries (`sigil`), and **a matching title id is now a
  fallback for missing-record reassociation**. Platforms: PSP, PSX, PS2, Vita,
  Switch, 3DS, Wii, Wii U, GameCube, Dreamcast, PS3, Xbox, Xbox 360
  (`adapters/services/sigil.py`).
- **5.4** — **`POST /api/tasks/scan`** (REST scan for token clients, scoped by
  `platforms` / `roms_ids` / `type`), **notifications** (`POST /api/notifications`)
  with **webhook / Apprise / email channels** (HMAC-signed `X-RomM-Signature`),
  audit log, and **built-in conversion via rom-converto** (`convert_library`
  task + convert-on-download `?format=`), off by default.

RomM still has no plugin system. The notification webhook is the only event
feed, and RomM reports no compression or space-savings stats anywhere.

---

## Part 1 — New integrations, most user-visible first

### 1. "It's still the same game in RomM" — convert in place, keep the ROM id

**The biggest gap today.** Re-pin restores the *provider ids*, but RomM files
everything a user cares about under the ROM **id**. That covers saves, save
states, screenshots, notes, collections, favourites, play status and play
sessions. A conversion produces a new id, so all of that stays on the old record,
which RomM now shows as *missing*. Users get a duplicate entry, and their saves
appear to vanish.

RomM's own `convert_library` (5.4) avoids this by rewriting the record in place.
We can do the same from outside, through the public API, when the rule replaces
the source (delete-after-verify):

1. Convert to a staging name (the output is verified as today).
2. `PUT /api/roms/{id}` with `fs_name=<new name>`. RomM renames the file on disk
   (`rename_fs_rom`) and rewrites the `rom_files` rows, all under the **same id**.
   It refuses with 409 if the target exists, which is why the output is staged.
3. `os.replace(staged, renamed)`: the verified output takes the record's file.
   The source is gone, and that removal *is* the delete-after-verify.
4. `POST /api/tasks/scan {type: "hashes", roms_ids: [id]}` so RomM re-hashes the
   new bytes. Scans of named `roms_ids` jump ahead of queued library scans.

On failure, step 2 is undone with a second rename. Needs `roms.write` +
`tasks.run`. **Verify:** that a `hashes` scan by `roms_ids` refreshes
`fs_size_bytes` and the per-file hashes, and how folder (multi-file) ROMs behave,
where `fs_name` is the folder. Ship single-file platforms first.

Where RomM 5.3+ title-id reassociation already covers a platform (sigil list
above, with title-id extraction on), RomM keeps the id by itself once the source
is deleted. Detect that (heartbeat `TITLE_ID_EXTRACTION_ENABLED`) and skip both
the swap and the re-pin. **Verify** per format that sigil reads the same title
id out of the compressed container (RVZ, CSO, NSZ, Z3DS).

### 2. No more "let RomM rescan, then press Re-match"

Today step 2 of the README's re-pin recipe is "let RomM rescan (its watcher, a
scheduled scan, or a manual one)", so metadata reappears whenever RomM gets
round to it.

- After a batch or sweep finishes, call `POST /api/tasks/scan` with
  `{type: "quick", platforms: [ids touched]}`. Debounce per platform.
- `409` means a scan is already running: retry once it ends. `503` means RomM has
  no scan worker: fall back to today's behaviour. On RomM < 5.4, or a token
  without `tasks.run`, also fall back silently.
- Follow `GET /api/tasks/{id}` and settle the re-pin queue the moment the scan
  reports done. Show "RomM is scanning… N/M" in the RomM view header.

The user just converts, and the game shows up in RomM a minute later with its
artwork.

### 3. Pair with a code instead of copying a token

RomM 4.8+ can issue an 8-digit pairing code for a client token. Setup becomes:
in RomM, create the token and press **Pair**; in Compressatorium, type the code.
The backend calls `POST /api/client-tokens/exchange {code}` and stores the
token. That is the same flow Argosy and the Playnite plugin use, with no
64-character secret pasted through the clipboard. The paste-a-token field stays
for older RomM.

Two more setup wins, each one request:

- **Scope report.** Test connection already separates *reachable / token
  accepted / mounted*. Add a row per feature showing which scopes the token
  enables: browse (`roms.read`, `platforms.read`), re-match (`roms.write`),
  instant rescans (`tasks.run`), RomM notifications (`me.write`). Probe each
  with a harmless call and report "feature X needs scope Y" instead of failing
  later.
- **Library path suggestion.** Heartbeat `FILESYSTEM.FS_PLATFORMS` lists RomM's
  platform folder names. Search the configured volumes for a directory that
  contains them (bounded, like every other path probe) and offer it as the
  library path.

### 4. Cover art and "open in RomM" in the library

`GET /api/roms` already returns `path_cover_small` / `url_cover`. Show covers in
the RomM file list, or as an optional grid, and give each row an **Open in
RomM** link (`{url}/rom/{id}`). Proxy covers through the backend
(`/api/romm/cover/{rom_id}`) so the token never reaches the browser. The view
stops looking like a file manager with nicer names and starts looking like the
user's library.

### 5. Tell RomM what happened

- **Notifications (5.4).** After a sweep, `POST /api/notifications` with a
  custom kind such as `compressatorium.sweep_done`: "Converted 12 PS2 games to
  CHD, saved 38.4 GB, 0 failures". It lands in the RomM inbox and goes out
  through whatever webhook / Apprise / email channels the user configured in
  RomM, so Compressatorium gets phone push notifications without building any.
  Needs `me.write`. Opt-in.
- **Provenance note (opt-in).** `POST /api/roms/{id}/notes`: "Converted ISO →
  RVZ by Compressatorium on 2026-10-25, verified, 4.3 GB → 1.2 GB." The answer
  to "why is this an RVZ now?" then sits next to the game.

### 6. React to RomM instead of polling it

RomM 5.4 notification channels can POST to a webhook on `scans` / `tasks`
events. Add an inbound endpoint (HMAC-verified against a shared secret the user
pastes into both apps) so that **when a RomM scan completes**:

- the re-pin queue settles immediately;
- an automation rule set to **Convert new arrivals** sweeps just that platform,
  so a ROM dropped into the library is compressed minutes later instead of at
  the next interval. The sweep logic already exists; this only adds a trigger.
  Use `updated_after` to look only at what changed.

### 7. Select by what RomM knows: collections, favourites, regions

`GET /api/roms` filters server-side on `collection_id`, `smart_collection_id`,
`virtual_collection_id`, `favorite`, `matched`, `verified`, `missing`,
`regions`, `languages`, `has_saves` and more. Expose them:

- **Library view:** a filter bar (collection / favourites / region / matched).
  "Convert my *PS2 Favourites* collection to CHD" becomes one selection.
- **Automation rules:** an optional scope ("only collection X", "skip
  favourites", "only verified dumps"). It replaces some regex filtering with
  RomM's curated data and costs no extra stat calls.

### 8. Work with RomM's built-in conversion, don't race it

RomM 5.4 can convert too (rom-converto, lossless targets per platform, identified
ROMs only). When `CONVERTO.ENABLED` is set in the heartbeat:

- Read `CONVERTO_LIBRARY_TARGETS` from `GET /api/config` and flag platforms where
  a Compressatorium rule and RomM disagree (e.g. RomM `ps2: chd` vs a CSO rule).
  Offer **Import RomM's targets as rules**.
- Never pick up RomM's staging files (`.romm_tmp_*`) in listings or sweeps.
- In the rule editor, explain what Compressatorium adds on top: PS3 ISO, Wii U
  WUD/WUX, 7z, NKit, unidentified ROMs, verification, preview/dry-run,
  schedules, size and name filters, and savings reporting.
- If RomM's convert-on-download is enabled, say so next to aggressive targets.
  A user can compress to RVZ and still have RomM hand a device an ISO.

### 9. A library-wide savings view

RomM's `GET /api/stats` has `TOTAL_FILESIZE_BYTES` and per-platform counts but
nothing about compression. The RomM view already computes per-platform
"converted / left / reclaimable". Add one summary card for all platforms (before
→ after, by platform, by format). Optionally push it to RomM as a notification
(item 5).

---

## Part 2 — Improving what exists

### Bugs found while researching

1. **Re-match can never succeed on Switch, Switch 2, Wii U and PS3.**
   - What happens: RomM puts these in `NON_HASHABLE_PLATFORMS`
     (`handler/filesystem/roms_handler.py`) and never hashes their files. So
     `GET /api/roms/by-hash` returns 404 forever for exactly the outputs that
     need re-pinning on those systems: NSZ, WUX and PS3 ISO. Each row is hashed
     locally (minutes for a multi-GB image), waits `repin_abandon_days`, then is
     retired as "RomM never matched this output".
   - Fix: on those platforms, or whenever by-hash misses, match by **path**.
     Compressatorium knows exactly where it wrote the output. List the platform
     (`search_term=<file name>`) and compare each candidate's `full_path`,
     mapped through `local_path()`, to the recorded output. Skip the local SHA-1
     entirely on non-hashed platforms, and keep it as the tie-breaker elsewhere.
2. **The RomM version is never shown.**
   - What happens: `romm_status` and the connection test read
     `heartbeat["VERSION"]`, but RomM has nested it under `SYSTEM.VERSION` since
     4.0.0. The "Connected to RomM v…" text is always blank, and the test
     fixture uses the old flat shape.
   - Fix: read `SYSTEM.VERSION` first. This also unlocks capability gating
     (next section).

### Hardening and polish

3. **Capability detection.** Parse the version and heartbeat flags once and
   gate features on them (scan endpoint ≥ 5.4, pairing ≥ 4.8, title-id fallback
   ≥ 5.3, `with_total` ≥ 5.2). Show "RomM 5.4 unlocks: instant rescans,
   notifications" when the instance is older.
4. **Clean up the ghost record.** Until item 1 lands, delete-after-verify leaves
   the old record in RomM as `missing`, so users see a duplicate. After a
   successful re-pin, offer (opt-in) to:
   - move collection memberships and notes across;
   - delete the old record with `POST /api/roms/delete {roms:[old],
     delete_from_fs:[]}`. That is never a filesystem delete, and only for a
     record RomM marks `missing_from_fs`.
5. **Carry all provider ids.** `METADATA_ID_FIELDS` has 9 ids. RomM's
   `PUT /api/roms/{id}` now also accepts `sgdb_id`, `libretro_id`, `steam_id`,
   `csdb_id`, `demozoo_id` and `pouet_id`. Add them, plus user-curated
   `name` / `url_cover` overrides when the user changed them in RomM.
6. **Make "DAT-safe" data-driven.** `DAT_SAFE_OUTPUT_EXTS` is a hard-coded
   `{.chd, .zip, .7z}` based on pre-4.9 matching. RomM now does composite
   archive hashes, per-file `chd_sha1_hash`, native RA hashes for RVZ/CSO, and
   title-id reassociation. Decide per (platform, format, RomM version) whether a
   re-pin is needed, and serve that to the UI, which already reads it from
   `/romm/status`. **Verify** that zip/7z still keep the DAT match under
   composite hashing.
7. **Respect RomM's exclusions.** `GET /api/config` returns
   `EXCLUDED_SINGLE_EXT` / `EXCLUDED_MULTI_*`. If a rule targets an extension
   RomM excludes, the converted game would disappear from RomM. Warn in the
   rule editor and refuse the delete-source option for it.
8. **Show RomM's "missing" state.** Pass `missing=false` (or read
   `missing_from_fs`) and report "RomM marks N ROMs missing" separately in the
   diagnostics, instead of counting them as `not_found` path failures.
9. **Cheaper catalog reads.** Add `with_total=false` (5.2), and cache per
   platform with `updated_after` so a reload fetches only what changed. That
   matters on 5,000-ROM platforms over a remote link.
10. **Folder ROMs and .m3u.** RomM groups multi-disc games as folders with an
    `.m3u`. Converting the cue/bin discs inside to CHD leaves the `.m3u`
    pointing at `.cue` files that no longer exist. Rewrite (or generate) the
    `.m3u` as part of the conversion; RomM's own converter does. This belongs in
    shared infrastructure, not in the RomM package, since it affects plain
    volumes too.
11. **Structure-aware path mapping.** RomM 5.3 made `filesystem.structure`
    mandatory, and `{platform}/roms` (structure B) layouts are declared rather
    than guessed. `local_path()` still strips a `roms/` prefix by heuristic.
    Prefer what RomM reports (`GET /api/platforms/filesystem`, heartbeat
    `FILESYSTEM`) and keep the heuristic as the fallback.

---

## Suggested order

| # | Item | User impact | Effort | Needs |
|---|---|---|---|---|
| 1 | Bug: path-based re-match on Switch/Wii U/PS3 | High (silent data loss today) | S | — |
| 2 | Bug: read `SYSTEM.VERSION` + capability detection | Low alone, unlocks the rest | XS | — |
| 3 | Instant rescans via `POST /api/tasks/scan` | High | S–M | RomM 5.4, `tasks.run` |
| 4 | Pairing code + scope report + path suggestion | High (first-run) | S | RomM 4.8 |
| 5 | Covers + Open in RomM | High (look and feel) | S | — |
| 6 | In-place conversion keeping the ROM id | Very high (saves, collections) | M–L | spike first |
| 7 | Collection / favourite / region filters | Medium–high | M | — |
| 8 | Notifications + provenance notes | Medium | S | RomM 5.4, `me.write` |
| 9 | Inbound webhook → convert new arrivals | Medium | M | RomM 5.4 channel |
| 10 | Coexist with RomM's built-in converter | Medium (avoids conflicts) | S–M | RomM 5.4 |
| 11 | Hardening items 4–11 | Medium | S each | — |

## Sources

- RomM releases: <https://github.com/rommapp/romm/releases> (4.8.0, 4.9.0, 5.0.0,
  5.1.0, 5.3.0, 5.4.0)
- Client API tokens / pairing:
  <https://docs.romm.app/latest/developers/client-api-tokens/>
- WebSockets (scan events): <https://docs.romm.app/latest/developers/websockets/>
- Folder structure templates:
  <https://docs.romm.app/latest/getting-started/folder-structure/>
- Code at `273b85c`: `backend/endpoints/tasks.py` (`POST /scan`),
  `backend/endpoints/sockets/scan.py` (`ScanPayload`),
  `backend/handler/scan_handler.py` (`ScanType`),
  `backend/endpoints/roms/__init__.py` (PUT rename path, `/delete`),
  `backend/handler/database/roms_handler.py` (`get_matching_missing_rom`),
  `backend/handler/filesystem/roms_handler.py` (`NON_HASHABLE_PLATFORMS`),
  `backend/adapters/services/sigil.py`, `backend/endpoints/heartbeat.py`,
  `backend/endpoints/client_tokens.py`, `backend/models/notification.py`,
  `backend/adapters/services/rom_converto.py`.
