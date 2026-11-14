# Value-add roadmap

What Compressatorium can offer that RomM doesn't, and what to build for people
who don't run RomM at all.

Researched against RomM `master` at `eb10d95` (2026-11-13), which is
**5.5.0-alpha.1** plus eight commits. `docs/ROMM_INTEGRATION_ROADMAP.md` covers
how the RomM integration itself should evolve. This doc is the product view:
where RomM stops, where we go further, and which features are stuck behind the
RomM integration today when they should work on a plain folder.

Anything marked **verify** needs a spike before it's relied on.

Words with a capital (Image, Library, Rule, Referrer, Round-tripped, and so on)
are defined in `CONTEXT.md`. The scope line is recorded in
`docs/adr/0001-scope-image-bytes-and-referrers.md`.

## Decisions this plan rests on

These were settled by going through the plan one question at a time.

1. **Standalone users come first.** People with a folder of Images and no RomM
   get the first two steps. Every item in those steps also helps RomM users.
2. **Scope.** The app changes or checks image bytes and keeps Referrers
   pointing at the result. Set completion, "best version" lists and serving
   files are out (ADR 0001).
3. **Use this instead of RomM's converter.** We say so plainly. For the formats
   only RomM has, we wrap the same rom-converto binary and put our checks
   around it.
4. **Round-trip is the default whenever a Source is deleted.** The weaker
   container-only check is a named opt-out. Existing rules switch to Round-trip
   on upgrade, with a one-time notice.
5. **One unattended delete, ever:** a Source replaced by its Round-tripped
   output, after every Referrer is updated. A Referrer that fails to update
   blocks the delete and the Source is kept.
6. **Three things run unattended:** converting, checking, and Inbox intake.
   Rename, duplicate cleanup, multi-disc tidy and patching are manual, each
   with a preview.
7. **Platform comes from the bytes first.** Header, then the user's explicit
   mapping, then the folder name. On a conflict the Image is skipped and gets
   a Finding.
8. **One scheduler per data directory.** The CLI's `run-rules` refuses to run
   beside a live server.
9. **The rule engine is extracted before folder Rules are built.** No second
   engine.
10. **Warn, never block,** when RomM's own converter is switched on.

## The model

```text
Volume
└── Library            a RomM library, or a folder the user registers
    │                  (an Inbox feeds exactly one Library)
    └── Platform       reported by RomM or worked out from the Image
        │              (no Platform: Unsorted, and no Rule touches it)
        └── Rule       convert, or check
            └── Image  one disc, cartridge dump or PS3 folder
                 │     one or more Images make a Title
                 └── Referrers: RomM's ROM, .m3u, gamelist.xml, .lpl
```

A check proves one of three things, and the bare word "verified" is not used:
**Intact** (the container reads back clean), **Round-tripped** (the output
decoded to the same content as its Source), **Recognized** (the hash is in a
DAT or Hasheous). The **Ledger** records every finished conversion. A
**Finding** is something a person has to decide on.

Browsing a Volume and converting by hand needs no Library. Libraries exist for
Rules, checks and reports.

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

### A1. Round-trip check, and RomM as a Referrer

**Why RomM doesn't cover it.** Its converter trusts an exit code. A truncated
CHD from a full disk or a flaky SMB mount would replace a good dump.

**What exists.** Verify for every tool, delete-on-verify, the re-pin queue with
persisted state.

**What's new, part one: the Round-trip check.** Each tool gets a
`content_digest(path)` on the plugin contract: a hash of the image content a
file decodes to. A conversion is Round-tripped when the output's digest equals
the Source's digest. It is a claim about content. Extracting a CD CHD gives one
`.bin` where the Source may have been six track files, and a CSO → CHD chain
never has the ISO on disk, so file equality can't be the test.

Each tool proves it the cheapest sound way. A starting point, each row to be
confirmed when it's built:

| Tool | How the output's content is obtained |
| --- | --- |
| chdman | Its verify recomputes the data SHA1 taken from the input at creation, so the output side is free. The extra cost is one independent read of the Source. **Verify** the CD frame layout so the Source side can be hashed without extracting; fall back to extract and compare. |
| dolphin-tool | `verify --algorithm sha1` rebuilds the disc SHA1, which is compared to the Source ISO's SHA1 |
| maxcso, z3ds, JWUDTool | decompress to a hashing stream, no temp file |
| nsz | its verify already checks NCA hashes against the originals. **Verify** that this is enough. |
| 7z / zip | member CRC from the archive against the CRC of the Source |
| chains | the final output's digest against the digest of the first step's decoded Source |
| PS3 folder → ISO, XISO trim | no round trip exists. Delete-source stays refused, as today. |

Round-trip is the default whenever the Source is deleted. Container-only is a
named opt-out ("faster, weaker"). When the Source is kept, the Intact check is
enough and nothing changes. On upgrade, existing rules with delete-source
switch to Round-trip and the rule editor says so once.

The same digest is the duplicate finder's key (N10), so it's built once.

**What's new, part two: RomM as a Referrer.** This is the in-place swap from
the integration roadmap (P1-1), run from the same hook as every other Referrer:

1. Convert to a staging name beside the Source.
2. Verify the staged output is Intact, then Round-trip it.
3. Update the file Referrers (`.m3u`, and any opted-in frontend files).
4. `PUT /api/roms/{id}` with the new `fs_name`, `os.replace` the staged file
   over it, then `POST /api/tasks/scan {type: "hashes", roms_ids: [id]}`.
5. Record every step so a crash resumes or surfaces the Image for manual
   recovery.

If RomM refuses the rename, the Source is kept, the output stays beside it and
the job ends as "converted, source kept: RomM refused the rename".

**MVP.** The Round-trip check ships first, by itself, for every Library. The
RomM swap follows for single-file Images behind an opt-in.

**Verify.** That a `hashes` scan by `roms_ids` refreshes size and hashes after
an out-of-band swap. The scan code was rewritten since the last check, and
quick scans now skip unchanged size+mtime, so use the `hashes` type and confirm
the mtime change is picked up.

### A2. Convert what RomM skips

**ROMs RomM hasn't identified.** RomM won't convert them. We can Recognize
them first, then convert.

1. Hash the Source (already done for DAT matching).
2. Look it up in local DATs, then Hasheous.
3. On a hit, show the DAT name next to the row and convert.
4. Afterwards, the RomM Referrer writes provider ids if Hasheous returned any.
   **Verify** which ids our Hasheous transport keeps; today it reads the
   signature, platform and publisher fields.

New UI: an "Unidentified in RomM" filter on the RomM view with a count of how
many we Recognize.

**Platforms with no RomM target.** Wii U, PS3, carts, GDI, original Xbox, the
long tail of CD systems. No new code. The rule editor shows, per Platform,
"RomM can't convert this, Compressatorium can" (see A7).

**Archives as input.** RomM's converter takes loose files only. A PS2 library
stored as `.7z` is invisible to it. We already convert from inside archives,
so say so in the RomM view.

### A3. Findings: health check and scheduled scrub

RomM has no check that a file still reads correctly. Its `verified` flag means
a hash matched once, which is our Recognized. A CHD with rotted hunks keeps its
header SHA1 and stays Recognized while no longer being Intact.

- **Check Rules.** A Rule with no target format: "check N GB per night,
  stalest first". The `verifications` table already stores results. Add the
  age of the last check and pick the stalest Images.
- **Findings view.** One list of everything that needs a decision: not Intact,
  Platform disagrees with its folder, not Recognized, duplicates. Each row
  opens the existing action (convert, Verify, delete plan).
- **Nothing acts on a Finding by itself.** A failed check sends a notification
  (N5) and, for RomM users, a RomM inbox message. It never moves or deletes
  the Image.
- **Uncompressed size.** RomM issue #4347 asks to show extracted size next to
  compressed size. We read the logical size out of every container already.
  Show it in the file inspector and the Findings view.

### A4. Rename to DAT name

RomM declined local DATs, and RomM issue #1865 (open, 11 upvotes) asks for a
bulk rename admin tool. We have `/files/rename` and the DAT's canonical name.

- **Whose name wins.** The user ranks their DATs once. The default is Redump
  for discs and No-Intro for cartridges. An Image Recognized by two DATs that
  the ranking doesn't order is not renamed and gets a Finding.
- **What gets renamed.** The whole Image: cue, track files and folder, with
  the cue rewritten to match.
- **Referrers.** With RomM connected the rename goes through
  `PUT /api/roms/{id}` with `fs_name`, so RomM keeps the ROM. File Referrers
  are updated from the shared hook (N8, N9). A Referrer that fails blocks the
  rename.
- **Manual only**, as a bulk action with a preview.

### A5. Savings in the RomM inbox

RomM reports `bytes_saved` once, in the result of one task run. See N6 for the
Ledger. For RomM users, post the sweep summary to the RomM inbox (integration
roadmap 5.1).

### A6. Patch bench for compressed Images

**Demand.** RomM #5572 and #2942: people store carts as zip/7z and RomM's
patcher rejects them.

**What exists.** Archive extraction, the 7z/zip packer, the job queue.

**What's new.** A "Patch" action: pick an Image (loose or inside an archive)
and a patch file. We unpack, apply, and repack as `Name (Patched).7z` beside
it. IPS and BPS are small enough to implement in Python. xdelta needs the
`xdelta3` binary. UPS and PPF can follow.

**What a patched Image is.** A new Image of the same Title. The original is
never changed. The patched Image is not Recognized, and Rules treat it like any
other Image that isn't. The Ledger records which Image and which patch file
produced it.

**MVP.** IPS and BPS on cartridge Images. Manual only.

**Guardrail.** No patch downloads, no patch database. The user supplies the
file.

### A7. Beside RomM's converter

The stance is "use this instead", and the app warns without blocking:

- A banner in RomM settings when the heartbeat reports `CONVERTO.ENABLED`:
  RomM's converter is on, and it deletes sources without checking the output.
- A conflict flag on each Rule whose Platform has an entry in RomM's
  `converto.platform_formats`, with the two targets side by side.
- Sweeps and listings ignore RomM's `.romm_tmp_*` staging files.
- The rule editor marks Platforms RomM can't convert at all.

This replaces item 4.3 of the integration roadmap. Its "import RomM's targets
as rules" idea stays as a button on the banner.

### A8. Later, and dropped

| Feature | Status |
| --- | --- |
| Multi-disc tidy (H5) | Kept, late. Groups loose discs into one Title folder with an `.m3u`. Manual, with a preview. Still open upstream as #5082. |
| Device packs (H4) | Kept, late. A batch copy that converts a chosen set onto a card. RomM pushes single games and converts on download; ours is the batch. |
| Library Wrapped (H6) | Kept, late. Reads the Ledger. No set-completion numbers. |
| Complete the set (H2), "best version" | **Dropped.** Outside ADR 0001. The DAT parser change to keep parent/clone data goes with it. |
| Make games better (H3, PCSX2 patches) | **Dropped.** It downloads third-party content and never touches image bytes. |

## Part N: for people who don't use RomM

Most of these are extractions. The logic exists under `services/romm` and has
to move onto shared infrastructure, which is what the modularity rule in
`CLAUDE.md` asks for anyway.

### N1. Rules for folder Libraries

**The problem.** `services/romm/auto.py` is 2,000 lines of scheduling,
filtering, preview and history that only runs against RomM. Someone with
`/data/roms` and no RomM gets a file browser and a convert button.

**Design.** Move the rule engine out first, with no behaviour change, then add
the folder Library as a second implementation of one interface.

```text
services/rules/
    rules.py      Rule schema, normalize, validation (moved from romm/auto.py)
    schedule.py   window, weekday mask, due check
    sweep.py      candidate inspection, queueing, converted-history
    library.py    Library protocol

Library
    id: str                        # "romm" | "folder:<path>"
    platforms() -> [Platform]
    images(platform) -> [Image]    # {key, path, name, size, added_at,
                                   #  recognized}
    referrers(image) -> [Referrer]
    capabilities                   # {recognized_filter, collections, ...}
```

- `RommLibrary` wraps today's client. An Image's `key` is the RomM ROM id.
- `FolderLibrary` is one root folder the user registers. Its Platforms come
  from N2, so `/data/roms/{ps2,psx,gc}` shows up as three Platforms without
  being told. An Image's `key` is its path relative to the root. Images with
  no Platform are Unsorted and no Rule touches them.
- A Rule belongs to a (Library, Platform) pair, the same shape RomM rules have
  now. The stored `romm.rules` and `romm.auto_state` preferences are migrated
  by prefixing their keys with the Library id.
- The converted-history memory moves with the Rule. A folder Rule's history is
  cleared when its Library root or target changes, the same way a RomM Rule's
  is cleared when the instance changes.

**UI.** `RommAutomation.svelte` becomes a generic Rules view with a Library
picker. The RomM view keeps its entry point. A Volume folder gets "Register as
a Library" in the browser.

**MVP.** One folder Library, skip policy only (the filesystem is the state, so
no history is needed), preview and run-now.

**Size.** L for the move, M for the folder Library. The move must leave every
existing RomM test passing untouched.

### N2. Work out the Platform without being told

**The problem.** A bare `.iso` could be PS2, PSP, GameCube, Wii or Xbox. With
RomM we know. Without RomM the user sets an "ISO handling" toggle and picks a
tool tab.

**Design.** One `detect_platform(image) -> (slug, how)` in shared
infrastructure. Precedence is fixed:

1. **Header.** A few hundred bytes:
   - GameCube magic `C2339F3D` at 0x1C, Wii magic `5D1C9EA3` at 0x18.
   - PS1/PS2 from `SYSTEM.CNF` (`BOOT` vs `BOOT2`), PSP from `PARAM.SFO`.
     `services/disc_id.py` already parses all three, and Dreamcast `IP.BIN`.
   - Xbox: the XISO probe that shipped with the Xbox tool.
   - Saturn `SEGA SEGASATURN`, Sega CD `SEGADISCSYSTEM` at sector 0.
2. **The user's mapping.** "This folder is PSP."
3. **Folder name.** Path components matched against the slugs RomM, ES-DE and
   Batocera use (`ps2`, `psx`, `gc`, `dreamcast`, …). Tools already declare
   `platform_slugs`.

A cached DAT match can fill in when all three are silent, since the DAT's name
names the system.

**Conflicts.** If the header says one Platform and the mapping or folder says
another, the Image is skipped by Rules and gets a Finding ("in the ps2 folder,
looks like GameCube"). It is never converted on a guess. If nothing answers,
the Image is Unsorted.

**What it unlocks.**

- An **Auto** entry in the tool picker: select mixed files, press convert,
  each goes to its Platform's default format.
- Folder Libraries (N1) that need no manual mapping.
- The Inbox (N4).
- The "ISO handling" toggle goes away.

**MVP.** The GameCube/Wii and PlayStation headers plus folder name. That
covers the ambiguous `.iso` cases.

**Size.** M.

### N3. A real headless CLI

**The problem.** The repo describes itself as "web UI + headless CLI". The CLI
is `CHD_MODE=cli`, runs chdman only, and reads only the top level of each
volume.

**Design.** A `compressatorium` entry point that drives the same registry and
job manager without starting uvicorn.

```bash
compressatorium convert /data/ps2 --to chd --delete-source
compressatorium convert /data --auto --dry-run --json
compressatorium verify /data/gamecube --since 30d
compressatorium run-rules               # one pass over the stored Rules, then exit
compressatorium report                  # savings and Findings as JSON
```

- `--to` takes an output format and resolves the mode through the registry, so
  it works for every tool, chains included.
- `--auto` uses N2.
- `--delete-source` implies the Round-trip check. `--container-only` is the
  opt-out.
- `convert` and `verify` run standalone and take a lock per output, so they
  are safe beside a running server.
- `run-rules` needs the data directory to itself. If a server holds it, the
  CLI prints why, exits with its own exit code and does nothing. One
  scheduler per data directory.
- Exit code 0 when everything converted or was already done, 1 on any failure,
  2 on bad arguments, 3 when `run-rules` found a live server. `--json` prints
  one line per Image.
- `CHD_MODE=cli` keeps working as an alias for
  `convert <each volume> --to chd`.

**MVP.** `convert` and `verify` with `--to`, `--dry-run`, `--json`.

**Size.** M. The job manager already runs without a browser attached.

### N4. Inbox

A watched folder that feeds exactly one Library. It is not a Library itself.

Per arriving Image, once it has stopped growing:

1. Unpack archives (`services/archive.py`; its size limits stay in force).
2. Work out the Platform (N2) and, where a DAT is loaded, Recognize it.
3. Convert *into* the Library under that Platform's Rule, named from the DAT
   when Recognized.
4. Move the original to `processed/` beside the Inbox.

Intake never deletes. An Unsorted Image stays where it is with a Finding that
says why. The Library's layout decides where output goes: RomM style
(`roms/{platform}/`), ES-DE/Batocera style (`{platform}/`), or flat. With a
RomM Library, trigger a RomM scan of the Platform afterwards.

Polling, since inotify is unreliable on NFS/SMB.

**Size.** M after N1 and N2.

### N5. Notifications

We have none. RomM has an inbox with webhook, Apprise and email channels.

**Design.** A small `services/notify.py` with one `send(event, title, body,
data)` and two transports: a plain JSON webhook with no extra dependency, and
[Apprise](https://github.com/caronc/apprise) URLs, shipped in the Docker image
(ntfy, Discord, Gotify, Pushover, email and most of the rest). Events:
`sweep_done`, `job_failed`, `check_failed`, `disk_low`. Batch per sweep so a
400-file run sends one message.

Everything is off until the user adds a destination. Settings live in the app
like the RomM settings do, with a Test button. Secrets never go back to the
browser.

For RomM users this is the fallback when the RomM token lacks `me.write`.

**Size.** S.

### N6. The Ledger

**The problem.** Job history is in memory, capped at 500, and gone on restart.
Jobs record `output_size` but not the input size. So there's no "you've saved
412 GB" anywhere, and no answer to "what happened to `Game.iso` in August".

**Design.** A `ledger` table, one row per finished conversion:

```text
id, finished_at, tool_id, mode, source_path, output_path,
input_bytes, output_bytes, duration_s, status,
check (none|intact|round_tripped), source_deleted (bool),
deleted_paths (json), referrers_updated (json),
library_id, image_key, platform_slug, content_digest,
patch_file (for a patched Image)
```

`job_manager` calls one `record(job)` hook at the terminal state. Rows are
kept forever by default, with a prune setting.

**Surfaces.**

- Dashboard card: total saved, by Platform, by format, last 30 days.
- A Ledger view with search and CSV export.
- `GET /api/stats/savings` and a Prometheus `/metrics` endpoint (jobs by
  status, bytes in and out, queue depth).
- The source for the Findings view's history and for Library Wrapped.

**MVP.** The table, the hook, the dashboard card.

**Size.** S–M. One migration.

### N7. Check Rules

The engine from A3 against folder Libraries. "Check 50 GB a night, stalest
first, tell me if anything is no longer Intact." It's a Rule with no target
format.

**Size.** S after N1.

### N8. Referrers, starting with `.m3u`

Converting `Disc 1.cue` to `Disc 1.chd` leaves any `.m3u` beside it pointing
at files that are gone. RomM's converter rewrites them. We don't.

The RomM swap, playlist rewriting and frontend files are one job, so they get
one contract, registry-driven like tools:

```text
Referrer
    detect(library, image) -> bool
    plan(renames: {old_path: new_path}) -> Change      # shown in the preview
    apply(change) -> Result                            # atomic, one .bak
```

One hook runs every Referrer for an Image when its Source is replaced or the
Image is renamed. They run **before** the Source is removed. If any fails, the
Source is kept, the output stays, and the job ends as "converted, source kept:
couldn't update X".

The `.m3u` Referrer is always on: it rewrites entries for names that were
actually replaced, and it's the piece multi-disc tidy needs. "Create .m3u" for
a selection of discs comes with it.

**Size.** M. Already specced as H-8.

### N9. Frontend Referrers

**The problem.** We tell RomM users their metadata survives conversion. Users
of ES-DE, Batocera and RetroArch have the same problem and get nothing.
`gamelist.xml` keys each game on `<path>./Game.iso</path>`. Convert to
`Game.chd` and the entry is orphaned: artwork, description, favourite flag and
play count all drop.

Three more Referrers on the N8 contract:

- **gamelist.xml** (EmulationStation, ES-DE, Batocera, Knulli, muOS variants):
  rewrite `<path>` for renamed files.
- **RetroArch `.lpl` playlists:** rewrite `path`, and blank the stale `crc32`.
- **LaunchBox platform XML:** `ApplicationPath`. **Verify** format stability
  before shipping.

These edit another app's files, so they are opt-in per Library ("this folder
is used by ES-DE"). Each one is listed in the delete confirmation ("will update
gamelist.xml, 3 entries") and takes a one-time backup. ES-DE rewrites its
gamelist when it exits, so the opt-in text says to close it first.

**MVP.** gamelist.xml only.

**Size.** M. This is the feature with the widest audience in this doc: every
handheld firmware uses a gamelist.

### N10. Duplicate finder

Find the same disc stored twice, including in two different containers. The
hashes we cache today don't line up across formats on their own, so this needs
one agreed content key per file before any query can group on it. That key is
the `content_digest` from A1.

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
- CD CHD against cue/bin can't be grouped by hash, and a shared DAT game name
  isn't enough either. The DAT parser gives every `<rom>`/`<disk>` child its
  parent's `game_name`, so one track file matches the same game as the whole
  disc does. Grouping on the name could offer a required track, or a different
  disc, as a duplicate. The rule is disc-level: the cue/bin side counts only
  when the cue's full track set matches every track entry of one DAT game
  (compare `rom_name` per track, none missing, none extra), and the CHD side
  matches that same disc's entry. **Verify** that MAME Redump's CHD entries
  and Redump's track entries can be tied to one disc reliably before building
  this. Until then CD CHD against cue/bin is reported as "possibly the same
  disc" with no delete action.
- Containers with no embedded image hash (CSO, NSZ, WUX, Z3DS) are left out at
  first. A Check Rule (N7) decompresses them anyway and can record a `raw:`
  key as a side effect later.

Never group on `chd_sha1`. `ChdmanTool.embedded_hashes` already returns the two
CHD hashes with separate labels, so the key builder reads the label rather than
guessing.

Groups show up as Findings with their sizes and a suggested keeper (Intact,
smallest, DAT-named). Cleanup is manual: the action is the existing delete
plan, and nothing is deleted without the confirmation list.

**MVP.** `raw:` keys only: ISO against Dolphin containers and DVD CHDs, plus
exact file duplicates. A delete is offered only for a hash-equal group. A
track file that belongs to a cue is never offered on its own.

**Size.** M. The content digest on the tool contract (shared with A1), then
the query and view.

### N11. Emulator compatibility presets

The README has a "Compression Compatibility Tips" section because NetherSX2
only reads zlib CHDs. That's a lookup table the app should apply.

A data file maps a target (PCSX2, NetherSX2, DuckStation, PPSSPP, Dolphin,
RetroArch cores, MiSTer) to the formats and codecs it reads. The compression
picker gains "Compatible with…" and warns when the chosen codec won't load.
Device packs read the same file.

**Size.** S for the picker, data kept in one YAML.

## Shared pieces

What has to exist once, so nothing above is built twice:

| Piece | Used by |
| --- | --- |
| `Library` protocol and the moved rule engine | N1, N3, N4, N7, existing RomM rules |
| `detect_platform` | N1, N2, N3, N4 |
| `content_digest` on the tool contract | Round-trip check (A1), duplicate finder (N10), Ledger |
| Ledger | N6, A5, Findings view, Library Wrapped |
| `notify` | N5, A3, N7 |
| `Referrer` contract and its one hook | A1 (RomM), N8, N9, A4 rename, multi-disc tidy |
| Findings store | A3, N2 conflicts, A4 rename conflicts, N10 |

All of these go in `docs/DESIGN_tool_plugin_architecture.md` as they land.

## Order of work

Ranked by how many users feel it against how much it costs. Each row is one
PR unless it says otherwise. The Round-trip check sits in step 1 because it's
the claim the whole pitch stands on, and folder Rules shouldn't ship a
delete-source option without it. That placement is my call; move it if you'd
rather have folder Rules sooner.

### Step 1: foundations both audiences need

| # | Item | Size |
| --- | --- | --- |
| 1.1 | Ledger and dashboard savings card (N6) | S–M |
| 1.2 | Notifications: webhook and Apprise (N5) | S |
| 1.3 | Platform detection: headers and folder name, conflicts as Findings (N2) | M |
| 1.4 | `content_digest` and the Round-trip check as the delete-source default, with the upgrade notice (A1 part one) | M |
| 1.5 | Referrer contract with the `.m3u` Referrer (N8) | M |

Done when: a restart keeps the Ledger, a finished batch can ping a phone, a
mixed selection of `.iso` files converts without the ISO toggle, and the README
can say "with the round-trip check on, a source is deleted only after the
output decodes back to the same disc image". That sentence is about image
content. A `cso_to_chd` job compares the decompressed image, and the `.cso`
container itself is not recoverable from the CHD. A rule set to the weaker
container-only check doesn't get the claim.

### Step 2: Rules for everyone

| # | Item | Size |
| --- | --- | --- |
| 2.1 | Move the rule engine out of `services/romm` behind `Library`, no behaviour change (N1) | L |
| 2.2 | `FolderLibrary` and the generic Rules view (N1) | M |
| 2.3 | CLI: `convert`, `verify`, `run-rules` (N3) | M |
| 2.4 | Check Rules and the Findings view (N7, A3) | S–M |

Done when: a user with no RomM sets "PS2 → CHD, nightly, delete source" on a
folder Library in the UI, or runs it from cron.

### Step 3: Referrers and RomM

| # | Item | Size |
| --- | --- | --- |
| 3.1 | Spike, then RomM as a Referrer: the in-place swap that keeps the ROM id (A1 part two, integration roadmap 3.0–3.1) | L |
| 3.2 | gamelist.xml Referrer, opt-in per Library (N9) | M |
| 3.3 | Duplicate finder, `raw:` keys (N10) | M |
| 3.4 | RomM converter banner, conflict flags, ignore `.romm_tmp_*` (A7) | S–M |

Done when: converting never orphans a game in RomM or ES-DE, and the README
says to turn RomM's converter off.

### Step 4: reach

| # | Item | Size |
| --- | --- | --- |
| 4.1 | rom-converto plugin for ZAR, PBP, CIA ↔ CCI (parity) | M |
| 4.2 | Rename to DAT name, with DAT ranking (A4) | S–M |
| 4.3 | Recognize, then convert, RomM's unidentified ROMs (A2) | M |
| 4.4 | Patch bench (A6) | M |
| 4.5 | Compatibility presets (N11) | S |
| 4.6 | Inbox (N4) | M |
| 4.7 | Multi-disc tidy, device packs, Library Wrapped (A8) | M each |

Steps 1 and 2 don't change how the RomM integration behaves, apart from the
Round-trip default in 1.4, so the remaining phases of
`ROMM_INTEGRATION_ROADMAP.md` (scan trigger, pairing, covers) can run alongside
them.

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
- H2 (complete the set) and H3 (make games better) are dropped. See ADR 0001
  and A8 above.
- Item 4.3 (coexist with RomM's converter) is replaced by A7: warn, never
  block.
- H-8 (`.m3u`) and the in-place swap (P1-1) are now two Referrers on one
  contract. See N8.

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
