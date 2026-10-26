# RomM integration roadmap

Where the RomM integration stands, what RomM itself now offers, and what to build
next, ordered by how much a user would feel it. Researched against RomM `master`
at `273b85c` (2026-10-25), latest release **5.4.1** (2026-10-22). Version tags
below say which RomM release introduced an API; anything marked **verify** needs
a spike against a live instance before it is relied on.

## Where we are today

Compressatorium talks to six RomM endpoints (`services/romm/client.py`):

| Endpoint | Used for |
| --- | --- |
| `GET /api/heartbeat` | Test connection / status |
| `GET /api/platforms` | Platform picker, per-platform tool narrowing |
| `GET /api/roms?platform_ids=…&with_files=true` | Library listing, automation sweeps |
| `GET /api/roms/{id}` | Single record |
| `GET /api/roms/by-hash?sha1_hash=…` | Re-pin: find the record RomM created for the output |
| `PUT /api/roms/{id}` (multipart) | Re-pin: write provider ids back |

The flow for a format RomM cannot hash-match (RVZ/CSO/NSZ/WUX/Z3DS) is:
snapshot provider ids → convert → *wait for RomM's own scan* → user presses
**Re-match in RomM** (or it runs on page load) → find the output's record →
write ids. Finding the record means a SHA-1 lookup on platforms RomM hashes,
and a path match on the ones it doesn't (Switch, Wii U, PS3, …; see bug 1).
RomM creates a **new** ROM record for the output; the old one is left behind.

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
  with **webhook / Apprise / email channels** (webhook deliveries carry an
  HMAC `X-RomM-Signature` when the channel has a secret), audit log, and **built-in conversion via rom-converto** (`convert_library`
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

Failure handling, by step:

- **Step 1 fails, or step 2 is cleanly refused (a 4xx):** the source and the
  RomM record are untouched. Remove the staged output.
- **Step 2's outcome is unknown** (a timeout or dropped connection, which RomM
  may have committed before the reply was lost): persist an *ambiguous* state
  and remove nothing. Re-read the record (`GET /api/roms/{id}`) and stat both
  paths, then continue from whichever state that shows: renamed or not.
- **Step 3 fails:** undo step 2 with a second rename, so the record names the
  original file again. Keep the staged output so a retry can run step 3 again.
- **Step 4 fails:** do not undo. The swap is complete; retry the scan later.
- **The undo rename itself fails, or the process dies between steps:** persist
  which step was reached before each one starts, like the re-pin queue does,
  so a restart either resumes or surfaces the ROM for manual recovery.

Needs `roms.write` + `tasks.run`. **Verify:** that a `hashes` scan by `roms_ids` refreshes
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
  instant rescans (`tasks.run`), RomM notifications (`me.write`). Report
  "feature X needs scope Y" instead of failing later. Keep the test free of side
  effects: a paired token's scopes come back in the exchange response
  (`ClientTokenCreateSchema.scopes`), so store them; for a pasted token, prove
  the read scopes with the reads the test already makes, and show write scopes
  as "unverified" until first use. Never probe a write scope by writing (a scan
  or an inbox notification).
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
   - **Fixed (Phase 0).** Each re-pin row now records its source platform
     (migration `0008`). On a platform in `UNHASHED_PLATFORM_SLUGS` (a mirror of
     RomM's list) the settle pass skips hashing and joins on the **path**: it
     lists the platform once per pass (`with_files=false`) and picks the record
     that `local_path()` would map to the output (`RommClient.rom_at_path`).
     RomM's `search_term` is a full-text name search, so it is not used for
     this. A ROM stored as a folder (a multi-file game) matches an output
     written inside that folder. Rows pending at upgrade carry no platform;
     they read it from the source ROM once and are rescued too. If RomM no
     longer has the source ROM (404), the platform is stored as unknown and the
     row keeps the SHA-1 join, which on an unhashed platform cannot match.
2. **The RomM version is never shown.**
   - What happens: `romm_status` and the connection test read
     `heartbeat["VERSION"]`, but RomM has nested it under `SYSTEM.VERSION` since
     4.0.0. The "Connected to RomM v…" text is always blank, and the test
     fixture uses the old flat shape.
   - Fix: read `SYSTEM.VERSION` first. This also unlocks capability gating
     (hardening item 1).

### Hardening and polish

1. **Capability detection.** Parse the version and heartbeat flags once and
   gate features on them (scan endpoint ≥ 5.4, pairing ≥ 4.8, title-id fallback
   ≥ 5.3, `with_total` ≥ 5.2). Show "RomM 5.4 unlocks: instant rescans,
   notifications" when the instance is older.
2. **Clean up the ghost record.** Until Part 1 item 1 lands, delete-after-verify leaves
   the old record in RomM as `missing`, so users see a duplicate. After a
   successful re-pin, offer (opt-in) to:
   - move collection memberships and notes across;
   - delete the old record with `POST /api/roms/delete {roms:[old],
     delete_from_fs:[]}`. That is never a filesystem delete, and only for a
     record RomM marks `missing_from_fs`.
3. **Carry all provider ids.** `METADATA_ID_FIELDS` has 9 ids. RomM's
   `PUT /api/roms/{id}` now also accepts `sgdb_id`, `libretro_id`, `steam_id`,
   `csdb_id`, `demozoo_id` and `pouet_id`. Add them, plus user-curated
   `name` / `url_cover` overrides when the user changed them in RomM.
4. **Make "DAT-safe" data-driven.** `DAT_SAFE_OUTPUT_EXTS` is a hard-coded
   `{.chd, .zip, .7z}` based on pre-4.9 matching. RomM now does composite
   archive hashes, per-file `chd_sha1_hash`, native RA hashes for RVZ/CSO, and
   title-id reassociation. Decide per (platform, format, RomM version) whether a
   re-pin is needed, and serve that to the UI, which already reads it from
   `/romm/status`. **Verify** that zip/7z still keep the DAT match under
   composite hashing.
5. **Respect RomM's exclusions.** `GET /api/config` returns
   `EXCLUDED_SINGLE_EXT` / `EXCLUDED_MULTI_*`. If a rule targets an extension
   RomM excludes, the converted game would disappear from RomM. Warn in the
   rule editor and refuse the delete-source option for it.
6. **Show RomM's "missing" state.** Pass `missing=false` (or read
   `missing_from_fs`) and report "RomM marks N ROMs missing" separately in the
   diagnostics, instead of counting them as `not_found` path failures.
7. **Cheaper catalog reads.** Add `with_total=false` (5.2), and cache per
   platform with `updated_after` so a reload fetches only what changed. That
   matters on 5,000-ROM platforms over a remote link.
8. **Folder ROMs and .m3u.** RomM groups multi-disc games as folders with an
   `.m3u`. Converting the cue/bin discs inside to CHD leaves the `.m3u`
   pointing at `.cue` files that no longer exist. Rewrite (or generate) the
   `.m3u` as part of the conversion; RomM's own converter does. This belongs in
   shared infrastructure, not in the RomM package, since it affects plain
   volumes too.
9. **Structure-aware path mapping.** RomM 5.3 made `filesystem.structure`
   mandatory, and `{platform}/roms` (structure B) layouts are declared rather
   than guessed. `local_path()` still strips a `roms/` prefix by heuristic.
   Prefer what RomM reports (`GET /api/platforms/filesystem`, heartbeat
   `FILESYSTEM`) and keep the heuristic as the fallback.

---

## Part 3 — Headline features: why a RomM user would want this

Parts 1 and 2 make the integration work well. On their own they don't answer
the RomM user who says "disk is cheap, and RomM converts now anyway". These
features do. In each one, compression happens as a side effect of something
the user actually wants. Each section has the demo moment, what already
exists, what is new, and the smallest version worth shipping (MVP).

### H0. Library health report — the no-risk first ten minutes

> Connect RomM with a **read-only** token and get a report on the whole
> library, before anything is converted: **2 discs are damaged, 6 dumps are
> bad, 14 duplicates, 380 GB reclaimable on PS2 alone, and 97 ROMs RomM
> couldn't identify that we can.**

It is the answer to "I don't want anything touching my library": it needs
only `roms.read` and changes nothing. It finds problems RomM can't see. RomM's
`verified` filter means "the hash matched a DAT". A CHD whose sectors have
rotted still matches, because its header hash doesn't change; only a
container check catches it.

| Report section | Built on |
| --- | --- |
| **Damaged files**: CHD/RVZ/archive internal checks fail | each tool's existing Verify, run in bulk |
| **Bad and unknown dumps**: no match in Redump/No-Intro/TOSEC | DAT matching plus Hasheous (`DATMatch`) |
| **Reclaimable space**: per platform, with the target format | the existing per-platform savings estimate (`SIZE_RATIOS`) |
| **Duplicates**: same dump, different names | file hashes already computed for DAT matching |
| **What RomM misses**: unidentified ROMs we can match; PS3, Wii U and NKit RomM's converter skips | Hasheous; per-platform tool matching |

**Exists:** all of the checks above, one file at a time.
**New:** a bulk scan with a time budget that runs as a job (it reads every
file, so it is heavy-I/O-lane work and resumable), a report page, and a
"Fix it" button per row that opens the existing convert or verify action.
Later: a scheduled re-check that posts "2 discs went bad this month" to the
RomM inbox (Phase 5.1).
**MVP:** reclaimable space, bad and unknown dumps, and duplicates from data
already in the database (fast). Container verification is a second pass the
user starts.
**Shared with H2:** both read `DATHash`/`DATMatch`, so they ship as one
"Library" dashboard: health on one tab, set completion on the other.

### H1. Drop zone — dump a messy pile, get a perfect library

> Drag a downloads folder full of `.zip`, `.7z` and loose bin/cue sets onto a
> watched folder. A minute later every game is in RomM: the right platform,
> its official Redump/No-Intro name, converted to the best format, verified,
> with its artwork. Anything that can't be identified waits in a tray with a
> best guess.

RomM's weakest step is getting files *in*. It has to be told the platform,
and it shows names exactly as they were on disk. Compressatorium can work out
both from the bytes.

**Pipeline**, per arriving file, once it has stopped growing:

1. Unpack archives (`services/archive.py`; its size limits stay in force).
2. Hash it, then look it up in the local DATs, falling back to Hasheous
   (`services/hasheous`). A hit gives the official game and ROM name, and the
   platform: from the DAT's name (`Sony - PlayStation`) mapped to a RomM
   slug, or straight from Hasheous's platform field.
3. Pick the target format from that platform's automation rule, so the drop
   zone obeys the rules the user already set up.
4. Convert into the RomM library: platform folder from
   `GET /api/platforms/filesystem`, filename from the DAT's `rom_name`.
   Verify, then trigger a scan of that platform (Phase 1.1).
5. Move the original to `processed/` (never deleted by default), or to
   `needs-attention/` with a reason: unknown hash, ambiguous platform, bad
   dump.

**Exists:** archive listing and extraction, DAT and Hasheous matching, every
converter, per-platform rules, verification, the job queue, path safety.
**New:** a watcher on one configured volume (polling, since inotify is
unreliable on NFS/SMB), a DAT-name → RomM-slug map, the needs-attention tray
UI, and a "stable size for N seconds" gate.
**MVP:** single files and zip/7z on disc platforms that have a DAT loaded.
No tray UI yet: unidentified files just stay put, with a reason in a sidecar
`.txt`.
**Verify:** that Hasheous's platform names map cleanly onto RomM slugs.

### H2. Complete the set — your library as a collection to finish

> **PS1 (USA): 812 / 1,034 — 78 %.** Tap it: 222 missing, 6 bad dumps,
> 14 duplicates, 31 files that are a different revision than the set
> expects. Turn on "best version of each game" and 14,000 files collapse into
> 3,200 entries in a RomM collection.

This is what collectors use clrmamepro and RomVault for: powerful, but old
desktop tools. Here it sits next to the library they browse, with their
artwork.

**Exists:** imported DATs keep every entry (`DATHash`: game name, ROM name,
size) and every local match (`DATMatch`), so completion is a grouped count
over tables that are already populated.
**New:** per-DAT completion queries, a platform view, and a RomM **collection**
holding the best version of each game (`collections.write`). It only lists
which ROMs are in it, and never deletes or hides anything. The "best version"
view needs the DAT parser to keep `cloneof`/`romof` and region tags, which it
currently drops.
**MVP:** completion % and the missing-games list per imported DAT, read-only.
**Guardrail:** the missing list shows names and hashes only. It links to
nothing and downloads nothing.

### H3. Make games better, not just smaller

> Convert your PS2 library, and every game that has a widescreen patch is
> widescreen now. GameCube titles come with their recommended Dolphin
> settings.

Compressatorium already reads the disc serial out of CHDs and ISOs
(`services/disc_id.py`, normalised the way PCSX2 does: `SLUS-20312`). That
serial is the key the community enhancement databases use.

**Exists:** serial extraction for PS1, PS2, PSP and Dreamcast; game IDs for
GameCube and Wii via dolphin-tool.
**New:** a source per emulator, starting with PCSX2's community patch
repository (widescreen, no-interlace, 60 fps); patches stored next to the game
so RomM files them as `PATCH` (RomM 5.x tags `patch/` subfolders); a per-game
"Enhancements" panel.
**MVP:** PCSX2 widescreen patches for PS2 only.
**Verify:** PCSX2 names patch files `SERIAL_CRC.pnach`, where CRC is the boot
ELF's CRC. So we must compute that CRC from the disc, which the existing
CHD/ISO sector reader should be able to do. Also check licensing and
attribution of each source before bundling or mirroring anything; prefer
fetching on demand from the upstream repo.

### H4. Device packs — fill my handheld with my favourites

> "Fit my favourites on a 256 GB card" → 412 games instead of 290, ranked by
> favourites, then recently played, then rating. In each device's preferred
> formats, laid out for muOS, Knulli or RetroDECK. Re-run it and it syncs the
> difference.

RomM knows what the user loves (`favorite`, `last_played`, `statuses` and
collection filters on `GET /api/roms`). Compressatorium knows how small each
game gets and in which format. The pack is a **copy**, so the master library
is never touched.

**Exists:** converters, size estimates (`SIZE_RATIOS`), the RomM catalog, the
job queue.
**New:** device profiles (folder layout plus per-emulator formats), a
"fit the most valuable games" planner, copy-and-convert jobs to a target
volume, and incremental re-sync.
**MVP:** one profile (RetroDECK on Steam Deck), favourites only, writing to a
mounted volume.
**Verify:** which compressed formats each firmware's cores accept (CSO and RVZ
support varies).

### H5. Multi-disc tidy — one game, one entry, disc swapping that works

> Before: 247 files, 98 RomM entries. After: 61 games, half the space, and
> RomM's player swaps discs.

A loose PS1, Saturn or Sega CD library becomes one folder per game: one CHD
per disc plus an `.m3u`. RomM shows a folder like that as a single game. This
is the open RomM request
[rommapp/romm#5082](https://github.com/rommapp/romm/issues/5082).

**Exists:** cue/bin → CHD; DAT names that carry `(Disc N)`.
**New:** grouping discs by DAT game name or filename, `.m3u` writing (shared
with hardening H-8), and the folder move. Under Phase 3 it would keep the ROM
id; without Phase 3 it re-pins.
**MVP:** PS1 only, triggered by hand from the RomM view, with a preview.

### H6. Library Wrapped — the shareable year in review

> "Your library in 2026: 1.2 TB saved, 3 sets completed, PS2 now 100 %
> widescreen, 41 hours in Final Fantasy IX." A good-looking card per user,
> made to be screenshotted and posted.

This turns the stats from the other features into something people want to
show off, which is how a self-hosted tool gets discovered. Play time comes
from RomM's play sessions (4.9+). Savings, completion and enhancements come
from H1–H3 and the existing savings estimate.

**MVP:** a single page from data that already exists (savings plus
completion), with play time once RomM's play-session API is confirmed.

---

## Phased delivery plan

Each phase ships on its own and leaves the integration better than it found
it. Inside a phase, every numbered item is one PR. Item references point at
the sections above: **P1-n** is Part 1 item *n*, **H-n** is Part 2 hardening
item *n*.

### Rules that apply to every phase

- **Gate on capability, never assume.** From Phase 0 on, every feature that
  needs a newer RomM or an extra scope checks the capability model first, and
  an older RomM gets today's behaviour, never an error. Each PR says which
  RomM version and scopes it needs.
- **Fall back silently, report loudly.** A missing scope or old RomM never
  breaks a conversion. It shows up once, in Settings, as "X needs Y".
- **Test against RomM's real shapes.** Fixtures copy response bodies from the
  RomM version they target (heartbeat, `/api/roms` items, task responses).
  The `SYSTEM.VERSION` bug came from a fixture in a shape RomM stopped sending
  at 4.0.
- **Keep docs and screenshots current** (CLAUDE.md): README for behaviour,
  `docs/RELEASE_NOTES.md` for every user-visible change, and `shots.yml`
  whenever a RomM view changes.
- **Never write by probing.** Connection tests and capability checks are
  read-only. Writes happen only as part of a feature the user turned on.

### Phase 0 — Correctness and a capability model

*Goal:* what exists today works on every platform and RomM version it claims,
and later phases have a capability model to build on. No new UI.

| # | Item | Needs | Size |
| --- | --- | --- | --- |
| 0.1 | ✅ Re-match on platforms RomM does not hash (bug 1) | — | S |
| 0.2 | Read `SYSTEM.VERSION`; add a capability model to `/romm/status` (bug 2, H-1) | — | S |
| 0.3 | Carry every provider id RomM accepts (H-3) | `roms.write` | XS |
| 0.4 | Report RomM's `missing` records separately in the diagnostics (H-6) | — | XS |

*Done when:* NSZ, WUX and single-file PS3 ISO outputs re-pin (a PS3 build
split into 4 GB parts still needs a manual re-match: there is no single file); Settings shows the RomM version;
`/romm/status` returns `{version, capabilities: {scan_task, pairing,
title_id_reassoc, notifications, …}}` for the UI and later phases to read.

### Phase 1 — Close the loop: no manual steps after converting

*Goal:* convert, and the game reappears in RomM with its metadata, with
nothing else to press.

| # | Item | Needs | Size |
| --- | --- | --- | --- |
| 1.1 | Trigger a targeted RomM scan after a batch or sweep, debounced per platform (P1-2) | RomM 5.4, `tasks.run` | M |
| 1.2 | Follow the scan task and settle re-pins when it finishes; "RomM is scanning…" in the header (P1-2) | 1.1 | S |
| 1.3 | Cheaper catalog reads: `with_total=false`, per-platform `updated_after` cache (H-7) | RomM 5.2 | S |

*Done when:* on RomM 5.4 with `tasks.run`, a converted RVZ/NSZ has its
metadata back within one scan, with no button pressed. On older RomM or
without the scope, behaviour is unchanged and Settings says what would turn
this on. *Risk:* `409` (scan already running) needs a retry-after-current-scan
path, not a dropped request.

### Phase 2 — First-run and look-and-feel

*Goal:* setup takes a minute, and the RomM view looks like the user's library.

| # | Item | Needs | Size |
| --- | --- | --- | --- |
| 2.1 | Pair with an 8-digit code; store the scopes from the exchange (P1-3) | RomM 4.8 | S |
| 2.2 | Per-feature scope report in Test connection, read-only (P1-3) | 0.2 | S |
| 2.3 | Suggest the library path from heartbeat `FILESYSTEM` (P1-3) | — | S |
| 2.4 | Cover art via a backend proxy, plus **Open in RomM** links (P1-4) | — | S–M |
| 2.5 | Structure-aware path mapping (H-9) | RomM 5.3 | S |

*Done when:* a new user pairs without copying a token; the library shows
covers; Settings lists exactly which features the token unlocks. Screenshots
are refreshed (`shots.yml` gains the paired-settings and cover views).

### Phase 3 — Keep the game: preserve the RomM record

*Goal:* converting never splits a game into a new entry and a "missing" ghost.
Saves, states, notes, collections and play history stay attached.

| # | Item | Needs | Size |
| --- | --- | --- | --- |
| 3.0 | **Spike** against a live RomM 5.4: does a `hashes` scan by `roms_ids` refresh size and hashes after an out-of-band swap? Which formats does sigil read the same title id from, before and after conversion? | test RomM | S |
| 3.1 | In-place swap for single-file ROMs under delete-after-verify, with the persisted per-step recovery (P1-1) | 3.0, 1.1, `roms.write`, `tasks.run` | L |
| 3.2 | Detect title-id reassociation and skip the swap and the re-pin where RomM already keeps the id (P1-1) | 3.0, 0.2 | S |
| 3.3 | Ghost-record cleanup (opt-in) for setups where 3.1 can't run (H-2) | `roms.write` | M |
| 3.4 | Make "DAT-safe" data-driven per platform, format and RomM version (H-4) | 3.0 | S |

*Done when:* a delete-after-verify conversion leaves the same ROM id in RomM
with its saves and collections, and a crash at any step resumes or surfaces
the ROM for manual recovery. *Risk:* this is the only phase that changes
RomM's own record. It ships behind an opt-in until it has run unattended for a
release, the same caution 4.5.0's beta applied to delete-after-verify.

### Phase 4 — Select by RomM's knowledge, and coexist with its converter

*Goal:* rules and selections speak in RomM's terms, and two converters never
fight over one library.

| # | Item | Needs | Size |
| --- | --- | --- | --- |
| 4.1 | Library filter bar: collection, smart collection, favourites, region, matched (P1-7) | — | M |
| 4.2 | Rule scopes: only collection X, skip favourites, only verified (P1-7) | 4.1 | M |
| 4.3 | Detect RomM's converter: flag conflicting targets, offer to import them, skip `.romm_tmp_*` (P1-8) | RomM 5.4, 0.2 | S–M |
| 4.4 | Warn when a target extension is excluded by RomM; refuse delete-source for it (H-5) | `GET /api/config` | S |
| 4.5 | Rewrite multi-disc `.m3u` playlists on conversion, in shared infrastructure (H-8) | — | M |

*Done when:* "convert my PS2 favourites to CHD" is one selection; a rule
that conflicts with RomM's converter says so before it runs.

### Phase 5 — Event-driven, and reporting back

*Goal:* Compressatorium reacts to RomM instead of polling it, and RomM users
see what it did without opening Compressatorium.

| # | Item | Needs | Size |
| --- | --- | --- | --- |
| 5.1 | Sweep summaries posted to the RomM inbox, reaching its webhook/Apprise/email channels (P1-5) | RomM 5.4, `me.write` | S |
| 5.2 | Per-ROM provenance notes, opt-in (P1-5) | `roms.user.write` (verify) | S |
| 5.3 | Inbound RomM webhook with HMAC check → settle re-pins and run a **Convert new arrivals** sweep (P1-6) | RomM 5.4 channel, 1.1 | M |
| 5.4 | Library-wide savings summary (P1-9) | — | S |

*Done when:* a ROM dropped into the library is converted minutes after
RomM's scan sees it, and the RomM inbox reports what was converted and saved.

### Headline track — runs alongside Phases 0–5

The Part 3 features are what make someone *want* the integration, so they are
not left until after the plumbing. Each one starts as soon as its
prerequisites are in:

| # | Feature | Can start | Needs from the plumbing | Size (MVP) |
| --- | --- | --- | --- | --- |
| H0 | Library health report (read-only) | **now** | nothing; scheduled re-checks use 5.1 | S–M |
| H2 | Complete the set (read-only completion) | **now**, with H0 | nothing | S |
| H1 | Drop zone | **now** | uses 1.1 (scan trigger) once it lands; falls back to RomM's own watcher | M |
| H5 | Multi-disc tidy | with 4.5 | `.m3u` writer (4.5); keeps the id once Phase 3 lands | M |
| H3 | Make games better (PCSX2 widescreen) | after a short spike | serial extraction (exists); ELF CRC (new) | M |
| H4 | Device packs | after 0.2 | capability model; RomM user-data filters | L |
| H6 | Library Wrapped | after H1/H2 | their stats; RomM play sessions | S |

**Suggested first sprint:** the Library dashboard (H0 health plus H2
completion: read-only, nothing blocks it, and immediately impressive on a real
library) alongside H1's MVP (the most compelling demo). Finish Phase 0.2 in the same sprint, since H4 and the
capability gating both need it.

### Dependencies at a glance

```text
Phase 0 ──► Phase 1 ──► Phase 3 (needs 1.1's scan trigger)
   │           └──────► Phase 5.3 (webhook reuses the scan/settle path)
   ├──────► Phase 2 (2.2 reads the capability model)
   └──────► Phase 4 (4.3 reads the capability model)
```

Phases 2 and 4 don't depend on each other or on Phase 1, so they can run in
parallel with it. Phase 3 is the largest and riskiest; its spike (3.0) can
start at any time, since its findings also decide 3.2 and 3.4.

## Sources

- RomM releases: <https://github.com/rommapp/romm/releases> (4.8.0, 4.9.0, 5.0.0,
  5.1.0, 5.2.0, 5.3.0, 5.4.0). 5.4.1 (2026-10-22) changed nothing
  under `backend/endpoints` or `backend/handler/auth` relative to 5.4.0
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
