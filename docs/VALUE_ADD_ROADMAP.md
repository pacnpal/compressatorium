# Value-add roadmap

What Compressatorium can offer that RomM doesn't, and what to build for people
who don't run RomM at all.

Researched against RomM `master` at `eb10d95` (2026-11-13), which is
**5.5.0-alpha.1** plus eight commits. `docs/ROMM_INTEGRATION_ROADMAP.md` covers
how the RomM integration itself should evolve. This doc is the product view:
where RomM stops, where we go further, and which features are stuck behind the
RomM integration today when they should work on a plain folder.

Anything marked **verify** needs a spike before it's relied on.

## The short version

RomM converts ROMs now. It's off by default, it covers the popular disc
formats, and it keeps the ROM id in place, which we don't yet. So "RomM can't
convert" is no longer the pitch.

The pitch that still holds, from reading the code:

1. **RomM deletes the source without verifying the output.** It checks the
   converter's exit code and that exactly one file was written, then unlinks
   the originals. We read the whole output back before anything is deleted.
2. **RomM skips every ROM it hasn't identified**, and has no target at all for
   Wii U, PS3, cartridge systems, GDI dumps, original Xbox, or most CD consoles.
3. **RomM has no integrity checking, no local DATs, no duplicate detection by
   hash, and no savings reporting.** The maintainers closed the local DAT
   request saying they don't plan to handle DATs in the app.
4. **RomM's conversion is one manual button.** No schedule, no filters, no
   preview, no codec choice, one fixed 10 minute timeout per file.

For people without RomM the finding is less flattering to us. Our best
features only work when RomM is connected: automation rules, platform-aware
tool selection and the savings estimate all live under `services/romm`. The
headless CLI still wraps chdman alone and only reads the top level of a
volume. Fixing that is the biggest thing in this doc.

## What RomM does today

### The converter

RomM shells out to [rom-converto](https://github.com/DevYukine/rom-converto)
`v0.24.0`, a single CLI, from `backend/adapters/services/rom_converto.py`. It is
gated by `ROM_CONVERTO_ENABLED` (default `false`).

Library targets, from `OPERATIONS` in that file:

| Platform | Targets | Inputs it accepts |
| --- | --- | --- |
| 3DS | z3ds, cci, cia | .cia, .cci, .3ds, .cxi and the z-variants; also converts CIA ↔ CCI |
| PSP, PS2 | chd, cso, zso, iso | .iso, .cue, .cso, .zso, .dax, .chd; PSP also .pbp → iso |
| PSX, Saturn, Sega CD, Dreamcast | chd | .cue, .iso |
| GameCube | rvz, iso | .iso, .gcm, .gcz, .nkit.iso, .nkit.gcz |
| Wii | rvz, iso, wbfs | .iso, .wbfs, .wia, .gcz, .nkit.iso, .nkit.gcz |
| Switch, Switch 2 | nsz, xcz, nsp, xci | .nsp, .xci, .nsz, .xcz |
| Xbox 360 | zar | .iso |

Original Xbox XISO exists as an operation but is marked lossy, so it is never a
library target.

How `convert_library` (`backend/tasks/manual/convert_library.py`) works, step
by step:

1. Walk every ROM on platforms that have a `converto.platform_formats` entry.
2. Skip the ROM if RomM hasn't identified it (`stats.unmatched`). The comment
   says why: a converted file no longer hash-matches a DAT.
3. Convert into a `.romm_tmp_convert_*` stage dir beside the ROM.
4. Hard-link (or rename) the output to its final name, refusing to overwrite.
5. Update `fs_name` on the same ROM row, or rewrite the `.m3u` playlists in a
   folder ROM.
6. Unlink the sources, including a cue's track files.
7. Refresh the ROM's file rows.

Nothing between 3 and 6 reads the output back. The task is `manual_run=True`
with no cron string, `single_instance=True`, and runs one conversion after
another. Each operation gets `ROM_CONVERTO_TIMEOUT` (600 s by default).

There is also convert-on-download (`utils/conversion_cache.py`): a client asks
for `?format=zso,iso`, RomM converts into a cache (24 h TTL, 20 GB cap) and
serves that. So a library stored as RVZ can still hand a device an ISO.

### Everything else that matters here

- **Device install.** The web UI queues a ROM for a paired device, the device
  claims it and downloads it (`endpoints/device/install.py`).
- **Streaming.** Server-hosted play sessions (`handler/streaming/`). No overlap
  with us, listed so nobody plans around it.
- **Server-side patcher.** `POST /api/roms/{id}/patch` applies a patch to a ROM
  in the library. It doesn't handle compressed ROMs yet (open requests #5572
  and #2942).
- **Hashing.** Per-file CRC/MD5/SHA1, the CHD header SHA1, composite hashes for
  archives, native RetroAchievements hashes for RVZ/WIA and CSO. It still
  doesn't hash Switch, Wii U, PS3, PS4/5, Xbox 360 and newer, or PC.
- **Identification.** Hasheous and Playmatch, both network services. Local DAT
  support was requested in #4079 and closed with "We don't plan to include DAT
  handling within the app itself."
- **Scan internals moved.** `handler/scan_handler.py` was split into
  `handler/scan/{lookups,merge,sources}.py` since the last roadmap pass, and
  quick scans now skip files whose size and mtime are unchanged (#5516).

## Scorecard

| Capability | RomM 5.5-alpha | Compressatorium 4.5 |
| --- | --- | --- |
| Convert, popular disc formats | yes, opt-in | yes |
| Wii U (WUD ↔ WUX) | no | yes |
| PS3 folder → ISO | no | yes |
| Cartridge ROMs → 7z/zip | no | yes |
| Dreamcast `.gdi` → CHD | no (`.cue`/`.iso` only) | yes |
| Other CD systems → CHD (PC Engine CD, 3DO, Neo Geo CD, CD-i, …) | no | yes, chdman takes any cue/bin |
| Original Xbox → XISO | lossy, excluded from library | yes |
| Xbox 360 → ZAR | yes | **no** |
| PSP `.pbp` → ISO | yes | **no** |
| 3DS CIA ↔ CCI | yes | **no** |
| Wii → WBFS output | yes | **no** |
| Convert a ROM straight out of a zip/7z/rar | no | yes |
| Unidentified ROMs | skipped | converted |
| Verify output before deleting source | **no** | yes |
| Codec and level choice | no | yes |
| Schedule, time window, weekday mask | no | yes, RomM view only |
| Filters (size, name regex, identified) | no | yes, RomM view only |
| Preview / dry run | no | yes, RomM view only |
| Adaptive timeout for big files | fixed 600 s | yes |
| Keeps the RomM ROM id (saves, collections) | **yes** | no, re-pins provider ids onto a new record |
| Rewrites `.m3u` after converting discs | **yes** | no |
| Convert on download for a device | **yes** | no, we don't serve files |
| Integrity check of existing files | no | yes, per tool, manual |
| Local DAT matching (Redump, No-Intro, TOSEC) | no, declined | yes |
| Duplicate detection by content hash | no (only name "clones") | no report yet; cached hashes cover some formats (see N10) |
| Savings reporting | one number in a task result | estimate in RomM view only |
| Notifications | inbox, webhook, Apprise, email | **none** |
| Persistent conversion history | the last task result | in-memory, last 500 jobs |
| Works without RomM | n/a | yes, minus automation |
| Headless CLI | n/a | chdman only, top level of a volume |

## Where RomM is ahead, and what to do about it

These are worth closing because each one is a reason a RomM user would pick the
built-in converter.

| Gap | Plan | Size |
| --- | --- | --- |
| Keeps the ROM id in place | Phase 3 of the integration roadmap (in-place swap through `PUT /api/roms/{id}` plus a `hashes` scan). Move it up. See A1 below. | L |
| Rewrites `.m3u` | Shared playlist rewriter, integration roadmap H-8. Needed for plain volumes too. See N8. | M |
| Xbox 360 ZAR | New tool plugin. **Verify** which binary to wrap (Xenia's `zarchive` or rom-converto's `xenon compress`) and that Xenia builds read the output. | M |
| PSP PBP → ISO | New mode. **Verify** a standalone tool; wrapping rom-converto itself for the handful of modes we lack is the cheap path and keeps one plugin for PBP, CIA ↔ CCI and ZAR. | S–M |
| 3DS CIA ↔ CCI | Same plugin as above. | S |
| WBFS output | dolphin-tool can't write WBFS. Skip unless asked; RVZ → ISO covers USB loaders after a manual step. | skip |
| Convert on download | Out of scope. We don't serve files and shouldn't start. The device pack idea (integration roadmap H4) covers the same need as a batch copy. | skip |
| Notifications | N5 below. | S |

A single "rom-converto" tool plugin is the fastest way to reach format parity.
It's one Rust binary, RomM already pins a checksum for it, and the plugin
contract handles a tool that owns several unrelated modes.

## Part A: value adds for RomM users

Each item says why RomM doesn't cover it, what already exists here, what's new,
and the smallest version worth shipping.

### A1. Safe replace: convert, verify, then swap under the same ROM id

**Why RomM doesn't cover it.** Its converter trusts an exit code. A truncated
CHD from a full disk or a flaky SMB mount would replace a good dump.

**What exists.** Verify for every tool, delete-on-verify, the re-pin queue with
persisted state.

**What's new.** The in-place swap from the integration roadmap (P1-1), now
framed as the headline. Order of operations:

1. Convert to a staging name beside the source.
2. Verify the staged output with the tool's own check.
3. Where the format allows, decompress-compare: hash the decompressed stream
   and compare it to the source hash. chdman, dolphin-tool and maxcso can all
   stream back out. This is the strongest check we can offer and nobody else
   in this space does it.
4. `PUT /api/roms/{id}` with the new `fs_name`, `os.replace` the staged file
   over it, then `POST /api/tasks/scan {type: "hashes", roms_ids: [id]}`.
5. Record every step so a crash resumes or surfaces the ROM for manual
   recovery.

**MVP.** Single-file ROMs, behind an opt-in, with verify levels "container
check" and "round-trip compare" as a per-rule setting.

**Verify.** That a `hashes` scan by `roms_ids` refreshes size and hashes after
an out-of-band swap. The scan code was rewritten since the last check, and
quick scans now skip unchanged size+mtime, so use the `hashes` type and confirm
the mtime change is picked up.

### A2. Convert what RomM skips

**Unidentified ROMs.** RomM won't convert them. We can do better than just
converting them: identify first, then convert.

1. Hash the source (already done for DAT matching).
2. Look it up in local DATs, then Hasheous.
3. On a hit, show the DAT name next to the row and convert.
4. After conversion, the re-pin path writes provider ids if Hasheous returned
   any. **Verify** which ids our Hasheous transport keeps; today it reads the
   signature, platform and publisher fields.

New UI: an "Unidentified in RomM" filter on the RomM view with a count of how
many we can name.

**Platforms with no RomM target.** Wii U, PS3, carts, GDI, original Xbox, the
long tail of CD systems. No new code. This is a docs and UI job: when the
connected RomM has its converter on, the rule editor should show per platform
"RomM can't convert this, Compressatorium can". The detection is already
specced as item 4.3 in the integration roadmap. Update that item's operation
table from this doc.

**Archives as input.** RomM's converter takes loose files only. A PS2 library
stored as `.7z` is invisible to it. We already convert from inside archives,
so say so in the RomM view.

### A3. Library health report and scheduled scrub

RomM has no check that a file still reads correctly. Its `verified` flag means
a hash matched once. A CHD with rotted hunks keeps its header SHA1.

This is H0 in the integration roadmap. Two additions from this pass:

- **Scheduled scrub** as a rule type: "verify N GB per night, oldest check
  first". The `verifications` table already stores results. Add a
  `verified_at` age and a sweep that picks the stalest files. Report failures
  through notifications (N5) and, for RomM users, the RomM inbox.
- **Uncompressed size.** RomM issue #4347 asks to show extracted size next to
  compressed size. We read the logical size out of every container already.
  Show it in the file inspector and the health report.

### A4. Offline identification with local DATs

RomM declined this. We have it. What's missing is using it for more than a
badge:

- **Rename to the DAT name.** RomM issue #1865 (open, 11 upvotes) asks for a
  bulk rename admin tool. We have `/files/rename` and the DAT's canonical
  name. Add "Rename to DAT name" as a bulk action with a preview. With RomM
  connected, do the rename through `PUT /api/roms/{id}` with `fs_name` so RomM
  keeps the record. Without RomM, rename on disk and update any sidecar
  (N8, N9).
- **Bad dump and wrong-revision report.** Part of A3.
- **Parent/clone data.** The DAT parser drops `cloneof`/`romof` and region
  tags. Keep them. That unlocks set completion and "best version" (H2).

### A5. Savings ledger

RomM reports `bytes_saved` once, in the result of one task run. We only
estimate, and only in the RomM view. See N6 for the shared design. For RomM
users specifically: post the sweep summary to the RomM inbox (integration
roadmap 5.1).

### A6. Patch bench for compressed ROMs

**Demand.** RomM #5572 and #2942: people store carts as zip/7z and RomM's
patcher rejects them.

**What exists.** Archive extraction, the 7z/zip packer, the job queue.

**What's new.** A "Patch" action: pick a ROM (loose or inside an archive) and
a patch file, we unpack, apply, repack as `Name (Patched).7z` beside it. IPS
and BPS are small enough to implement in Python. xdelta needs the `xdelta3`
binary. UPS and PPF can follow.

**MVP.** IPS and BPS on cartridge ROMs, output never overwrites the source.

**Guardrail.** No patch downloads, no patch database. The user supplies the
file.

This one is useful with or without RomM, so it belongs to the shared tools.

### A7. The rest of the headline track

Still right, order adjusted for what RomM has shipped:

| Feature | Status after this pass |
| --- | --- |
| H1 Drop zone | Unchanged. RomM still needs to be told the platform on upload. See N4 for the no-RomM version. |
| H2 Complete the set | Unchanged. Depends on A4's parser change. |
| H3 Make games better (PCSX2 patches) | Unchanged. |
| H4 Device packs | Reposition. RomM now pushes single games to a device and converts on download. Our angle is the batch: "fit the most games on a 256 GB card", pre-converted, as one copy job. |
| H5 Multi-disc tidy | Still open upstream as #5082. RomM added "promote one file to a folder" (`handler/rom_conversion.py`) but not grouping a flat multi-disc set. Use `PUT` rename for the move where possible. |
| H6 Library Wrapped | Unchanged. Needs N6. |

## Part N: for people who don't use RomM

Most of these are extractions. The logic exists under `services/romm` and has
to move onto shared infrastructure, which is what the modularity rule in
`CLAUDE.md` asks for anyway.

### N1. Rules for plain folders

**The problem.** `services/romm/auto.py` is 2,000 lines of scheduling,
filtering, preview and history that only runs against a RomM catalog. Someone
with `/data/ps2` and no RomM gets a file browser and a convert button.

**Design.** Split the rule engine from where its ROM list comes from.

```text
services/automation/
    rules.py      rule schema, normalize, validation (moved from romm/auto.py)
    schedule.py   window, weekday mask, due check
    sweep.py      candidate inspection, queueing, converted-history
    sources.py    CatalogSource protocol

CatalogSource
    id: str                      # "romm" | "volume:<name>"
    scopes() -> [Scope]          # RomM platforms, or folders the user mapped
    items(scope) -> [Item]       # {key, path, name, size, added_at,
                                 #  platform_slug, identified}
    capabilities                 # {identified_filter, collections, ...}
```

- `RommSource` wraps today's client. `key` is the RomM ROM id.
- `FolderSource` walks a folder. `key` is the path relative to the volume.
  `platform_slug` comes from N2. `identified` means "has a DAT match".
- Rules are stored per `(source id, scope id)`. Migrate the existing
  `romm.rules` and `romm.auto_state` preferences by prefixing their keys with the source id.
- The converted-history memory moves with the rule. A folder rule is cleared
  when its folder or target changes, the same way a RomM rule is cleared when
  the instance changes.

**UI.** `RommAutomation.svelte` becomes a generic `Automation` view with a
source picker. The RomM view keeps its entry point. A volume gets "Add rule
for this folder" in the browser.

**MVP.** One folder, one rule, skip policy only (the filesystem is the state,
so no history table is needed), preview and run-now.

**Size.** L, mostly moving code and its tests. No behaviour change for RomM
users, which the existing test suite should prove.

### N2. Work out the platform without being told

**The problem.** A bare `.iso` could be PS2, PSP, GameCube, Wii or Xbox. With
RomM we know. Without RomM the user sets an "ISO handling" toggle and picks a
tool tab.

**Design.** One `detect_platform(path) -> (slug, confidence, how)` in shared
infrastructure, trying cheap things first:

1. **Folder name.** Match path components against the slugs RomM, ES-DE and
   Batocera use (`ps2`, `psx`, `gc`, `gamecube`, `dreamcast`, …). Tools already
   declare `platform_slugs`.
2. **Header sniff.** A few hundred bytes:
   - GameCube magic `C2339F3D` at 0x1C, Wii magic `5D1C9EA3` at 0x18.
   - PS1/PS2 from `SYSTEM.CNF` (`BOOT` vs `BOOT2`), PSP from `PARAM.SFO`.
     `services/disc_id.py` already parses all three, and Dreamcast `IP.BIN`.
   - Xbox: the XISO probe that shipped with the Xbox tool.
   - Saturn `SEGA SEGASATURN`, Sega CD `SEGADISCSYSTEM` at sector 0.
3. **DAT match.** A hash hit names the DAT, and the DAT name names the system.
   This is the slow path, so only use cached matches.

Cache the result per file next to the existing metadata scan.

**What it unlocks.**

- An **Auto** entry in the tool picker: select mixed files, press convert,
  each goes to its platform's default format.
- Folder rules (N1) that need no manual platform mapping.
- The drop zone (N4).
- The "ISO handling" toggle goes away.

**MVP.** Folder name plus the GameCube/Wii and PlayStation sniffs. That covers
the ambiguous `.iso` cases.

**Size.** M.

### N3. A real headless CLI

**The problem.** The repo describes itself as "web UI + headless CLI". The CLI
is `CHD_MODE=cli`, runs chdman only, and reads only the top level of each
volume.

**Design.** A `compressatorium` entry point that drives the same registry and
job manager without starting uvicorn.

```bash
compressatorium convert /data/ps2 --to chd --verify --delete-source
compressatorium convert /data --auto --dry-run --json
compressatorium verify /data/gamecube --since 30d
compressatorium run-rules               # one pass over the stored rules, then exit
compressatorium report                  # savings and health as JSON
```

- `--to` takes an output format and resolves the mode through the registry, so
  it works for every tool, chains included.
- `--auto` uses N2.
- `run-rules` is N1 as a one-shot, for cron and Unraid User Scripts.
- Exit code 0 when everything converted or was already done, 1 on any failure,
  2 on bad arguments. `--json` prints one line per file.
- Keep `CHD_MODE=cli` working as an alias for
  `convert <each volume> --to chd`.

**MVP.** `convert` and `verify` with `--to`, `--dry-run`, `--json`.

**Size.** M. The job manager already runs without a browser attached.

### N4. Drop zone without RomM

H1 from the integration roadmap, minus the RomM scan trigger. The output
layout is the open question when there's no RomM to ask. Offer three: RomM
style (`roms/{platform}/`), ES-DE/Batocera style (`{platform}/`), or flat.
Platform comes from N2. Names come from the DAT when there's a match.

**Size.** M after N2.

### N5. Notifications

We have none. RomM has an inbox with webhook, Apprise and email channels.

**Design.** A small `services/notify.py` with one `send(event, title, body,
data)` and two transports to start: a generic JSON webhook, and
[Apprise](https://github.com/caronc/apprise) URLs (which covers ntfy, Discord,
Gotify, Pushover, email and most of the rest). Events: `sweep_done`,
`job_failed`, `scrub_failed`, `disk_low`. Batch per sweep so a 400-file run
sends one message.

Settings live in the app like the RomM settings do, with a Test button. Secrets
never go back to the browser.

For RomM users this is the fallback when the RomM token lacks `me.write`.

**Size.** S.

### N6. Conversion history and savings ledger

**The problem.** Job history is in memory, capped at 500, and gone on restart.
Jobs record `output_size` but not the input size. So there's no "you've saved
412 GB" anywhere, and no audit trail of what was converted or deleted.

**Design.** A `conversions` table, written when a job reaches a terminal
state:

```text
id, finished_at, tool_id, mode, source_path, output_path,
input_bytes, output_bytes, duration_s, status,
verified (none|container|roundtrip), source_deleted (bool),
catalog_source, catalog_key, platform_slug
```

`job_manager` calls one `record_conversion(job)` hook. The RomM re-pin path
and N1's history can read from it later.

**Surfaces.**

- Dashboard card: total saved, by platform, by format, last 30 days.
- History view with search and CSV export.
- `GET /api/stats/savings` and a Prometheus `/metrics` endpoint (jobs by
  status, bytes in and out, queue depth). Home-lab users graph everything.
- The source for Library Wrapped (H6).

**MVP.** The table, the hook, the dashboard card.

**Size.** S–M. One migration.

### N7. Scheduled scrub

Same engine as A3, running against volumes. "Verify 50 GB a night, stalest
first, tell me if anything fails." It's a rule type in N1's engine with no
target format.

**Size.** S after N1.

### N8. Playlists and cue sheets that follow the conversion

Converting `Disc 1.cue` to `Disc 1.chd` leaves any `.m3u` beside it pointing
at files that are gone. RomM's converter rewrites them. We don't, and it bites
plain-volume users of RetroArch and ES-DE just as hard.

**Design.** A shared `rewrite_references(folder, renames)` called from the job
completion path when the source is deleted: rewrite `.m3u` entries, atomically,
only for names that were actually replaced. Add "Create .m3u" for a selection
of discs.

**Size.** M. Already specced as H-8.

### N9. Frontend metadata that survives conversion

**The problem.** We tell RomM users "your metadata survives conversion". Users
of ES-DE, Batocera and RetroArch have the same problem and get nothing.
`gamelist.xml` keys each game on `<path>./Game.iso</path>`. Convert to
`Game.chd` and the entry is orphaned: artwork, description, favourite flag and
play count all drop.

**Design.** A `FrontendAdapter` contract, registry-driven like tools:

```text
detect(folder) -> bool
rewrite(folder, renames: {old_name: new_name}) -> RewriteResult
```

- **gamelist.xml** (EmulationStation, ES-DE, Batocera, Knulli, muOS variants):
  rewrite `<path>` for renamed files. Write a `.bak` once per file.
- **RetroArch `.lpl` playlists:** rewrite `path`, and blank the stale `crc32`.
- **LaunchBox platform XML:** `ApplicationPath`. **Verify** format stability
  before shipping.

It runs from the same hook as N8, only when the source is removed, and shows
in the delete-plan confirmation: "will update gamelist.xml (3 entries)".

**MVP.** gamelist.xml only.

**Size.** M. This is the feature with the widest audience in this doc: every
handheld firmware uses a gamelist.

### N10. Duplicate finder

Find the same disc stored twice, including in two different containers. The
hashes we cache today don't line up across formats on their own, so this needs
one agreed content key per file before any query can group on it.

What each cached hash actually covers:

| File | Hash we have | Equals the raw image's SHA1? |
| --- | --- | --- |
| `.iso`, `.bin` | file SHA1 | yes, it is the raw image |
| Dolphin RVZ/WIA/GCZ | disc SHA1 rebuilt by `dolphin-tool verify` | yes |
| DVD or HD CHD (`createdvd`, `createhd`) | `chd_data_sha1` | yes, the logical data is the image. **Verify** on a real `createdvd` CHD. |
| CD CHD (`createcd`) | `chd_data_sha1` | no. The data is 2448-byte frames (sector plus subcode) with tracks padded to a 4-frame boundary. |
| any CHD | `chd_sha1` (overall) | no. It folds in the metadata hashes, so it never equals a raw file hash. |
| CSO/ZSO/DAX, NSZ, WUX, Z3DS | file SHA1 of the container | no |

So the content key is `(kind, sha1)`:

- `raw:<sha1>` for `.iso`/`.bin`, Dolphin containers and DVD/HD CHDs. These
  group across formats directly: `Game.iso`, `Game.rvz` and a `createdvd`
  `Game.chd` of one disc share a key.
- `chdcd:<chd_data_sha1>` for CD CHDs. Two CD CHDs of the same disc match each
  other whatever codec they use. They don't match a cue/bin by hash.
- CD CHD against cue/bin goes through the DAT instead: when both files have a
  `DATMatch` to the same game, they're the same disc. Without a DAT match on
  both sides we don't claim a duplicate.
- Containers with no embedded image hash (CSO, NSZ, WUX, Z3DS) are left out at
  first. The scrub (N7) already decompresses them to verify, and can record a
  `raw:` key as a side effect later.

Never group on `chd_sha1`. `ChdmanTool.embedded_hashes` already returns the two
CHD hashes with separate labels, so the key builder reads the label rather than
guessing.

Report groups with their sizes and a suggested keeper (verified, smallest,
DAT-named). The action is the existing delete plan. Nothing is deleted without
the confirmation list.

**MVP.** `raw:` keys only: ISO against Dolphin containers and DVD CHDs, plus
exact file duplicates.

**Size.** M. A content-key column on the cached hashes, the key builder on the
tool contract, then the query and view.

### N11. Emulator compatibility presets

The README has a "Compression Compatibility Tips" section because NetherSX2
only reads zlib CHDs. That's a lookup table the app should apply.

A data file maps a target (PCSX2, NetherSX2, DuckStation, PPSSPP, Dolphin,
RetroArch cores, MiSTer) to the formats and codecs it reads. The compression
picker gains "Compatible with…" and warns when the chosen codec won't load.
Device packs (H4) read the same file.

**Size.** S for the picker, data kept in one YAML.

## Shared pieces

What has to exist once, so nothing above is built twice:

| Piece | Used by |
| --- | --- |
| `CatalogSource` + moved rule engine | N1, N3, N7, existing RomM rules |
| `detect_platform` | N1, N2, N3, N4 |
| `conversions` ledger | N6, A5, H6, history view |
| `notify` | N5, A3, N7 |
| Post-conversion reference hook (`.m3u`, frontend adapters) | N8, N9, A4 rename |
| Verify levels (container, round-trip) | A1, N1 rules, CLI |
| DAT parser keeps clone and region data | A4, H2 |

All of these go in `docs/DESIGN_tool_plugin_architecture.md` as they land.

## Order of work

Ranked by how many users feel it against how much it costs. Each row is one
PR unless it says otherwise.

### Step 1: foundations both audiences need

| # | Item | Size |
| --- | --- | --- |
| 1.1 | Conversion ledger and dashboard savings card (N6) | S–M |
| 1.2 | Notifications: webhook and Apprise (N5) | S |
| 1.3 | Platform detection: folder name and header sniff (N2) | M |
| 1.4 | Shared `.m3u` rewriter (N8) | M |

Done when: a restart keeps the history, a finished batch can ping a phone, and
a mixed selection of `.iso` files converts without the ISO toggle.

### Step 2: automation for everyone

| # | Item | Size |
| --- | --- | --- |
| 2.1 | Move the rule engine out of `services/romm` behind `CatalogSource`, no behaviour change (N1) | L |
| 2.2 | `FolderSource` and the generic Automation view (N1) | M |
| 2.3 | CLI: `convert`, `verify`, `run-rules` (N3) | M |
| 2.4 | Scrub as a rule type (N7, A3) | S |

Done when: a user with no RomM sets "PS2 folder → CHD, nightly, verify, delete
source" in the UI or runs it from cron.

### Step 3: the safety story

| # | Item | Size |
| --- | --- | --- |
| 3.1 | Round-trip verify level (A1) | M |
| 3.2 | Spike, then in-place swap that keeps the RomM ROM id (A1, integration roadmap 3.0–3.1) | L |
| 3.3 | gamelist.xml adapter (N9) | M |
| 3.4 | Health report and duplicate finder (A3, N10) | M |

Done when: converting never orphans a game in RomM or ES-DE, and the README
can say "the source is deleted only after the output decompresses back to the
same bytes".

### Step 4: reach

| # | Item | Size |
| --- | --- | --- |
| 4.1 | rom-converto plugin for ZAR, PBP, CIA ↔ CCI (parity) | M |
| 4.2 | Rename to DAT name, through RomM when connected (A4) | S–M |
| 4.3 | Identify-then-convert for RomM's unidentified ROMs (A2) | M |
| 4.4 | Patch bench (A6) | M |
| 4.5 | Compatibility presets (N11) | S |
| 4.6 | Drop zone (N4, H1) | M |

Steps 1 and 2 don't touch the RomM integration's behaviour, so the remaining
phases of `ROMM_INTEGRATION_ROADMAP.md` (scan trigger, pairing, covers) can run
alongside them.

## Corrections to the integration roadmap from this pass

- RomM is at 5.5.0-alpha.1. rom-converto is pinned at v0.24.0. Its operation
  list now includes Xbox 360 ZAR, NKit migration to RVZ, Switch 2, and PSP PBP.
  Item 4.3 should read targets from this doc's table.
- RomM's stage dirs are `.romm_tmp_convert_*` (prefix `.romm_tmp_`). We don't
  filter them from listings or sweeps yet.
- RomM's converter rewrites `.m3u` files. H-8 said so; it's confirmed in
  `convert_library._rewrite_playlists`.
- The scan handler was split into `handler/scan/`. Every "verify" in Phase 3
  of that roadmap needs re-checking against the new code, the missing-record
  reassociation most of all.
- Quick scans skip files with unchanged size and mtime (#5516). After an
  out-of-band swap, ask for a `hashes` scan by `roms_ids`.
- H4 (device packs) overlaps with RomM's device install plus convert on
  download. Keep the batch planner, drop the idea of being the delivery path.
- H5's upstream issue (#5082) is still open. `promote_single_file_to_folder`
  exists for one file at a time.

## Sources

- RomM code at `eb10d95`: `backend/adapters/services/rom_converto.py`,
  `backend/tasks/manual/convert_library.py`, `backend/utils/conversion_cache.py`,
  `backend/handler/rom_conversion.py`, `backend/tasks/registry.py`,
  `backend/handler/filesystem/roms_handler.py` (`NON_HASHABLE_PLATFORMS`,
  `rom_file_unchanged`), `backend/utils/rvz_hasher.py`,
  `backend/endpoints/device/install.py`, `backend/endpoints/roms/patch.py`,
  `examples/config.example.yml` (`converto`), `env.template`, `Dockerfile`.
- RomM issues: #4079 (local DATs, declined), #1865 (bulk rename), #5572 and
  #2942 (patching compressed ROMs), #4347 (uncompressed size), #5082
  (multi-disc promote), #1022 (duplicate filter).
- This repo: `app/services/romm/auto.py`, `app/services/job_manager.py`,
  `app/services/db.py`, `app/services/disc_id.py`, `app/services/dat_parser.py`,
  `app/services/tools/spec.py`, README "CLI Mode".
