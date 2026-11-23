# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

Primary: a self-hosted game collector. They run Unraid or Docker at home and keep a large retro game library alongside RomM and the *arr apps (ROMarr, Sonarr, Radarr). They want the library smaller and in order with as little hands-on work as possible.

Second: the curator or archivist. They care about verified, bit-exact dumps and DAT matching. Their standard of correctness is how the product earns the collector's trust: collectors come first, and archivist-grade safety is the floor under every feature.

## Product Purpose

Compressatorium converts and checks game images. It runs thirteen conversion tools behind one interface: CHDMAN, dolphin-tool, z3ds, nsz, maxcso, 7z, makeps3iso, nkit2iso, JWUDTool, extract-xiso, rom-converto, unar, and a built-in eXoDOS repack. It also keeps the tools that point at those images working afterwards, for example by putting files where RomM expects them.

Success means the collector's library ends up in the best format for each console without the collector ever losing a game. Once Libraries, Rules and watch folders are set up, that happens without the collector touching it.

## Positioning

Safe, automatic and all-in-one:

- **One Auto button.** Each game goes to its best format across all thirteen tools.
- **Nothing is deleted on trust.** An original is removed only after its output is checked. By default the output must round-trip to the same content.
- **Hands-off once set up.** Libraries, per-console Rules, watch folders and *arr webhooks keep new files converted and placed without anyone stepping in.

Running the tools by hand gives none of this, and RomM's own converter covers only part of it.

## Operating Context

- Runs as a Docker container on a home server, usually Unraid. It is reached through a browser on the LAN; a headless CLI is also available.
- It works on folders the user mounts in, called Volumes in the code. The user groups those folders into Libraries, and RomM can act as a Library too.
- Other tools around it:
  - **RomM:** catalogue, placement and scans.
  - **ROMarr, Sonarr, Radarr:** hand new downloads over through a webhook.
  - **DATs:** No-Intro packs and Redump game lists, used to identify games.
  - **Hasheous:** optional online identification.
  - **ES-DE gamelists.**
  - **Notification webhooks.**
- The app has four places plus Help:
  - **Files:** browse, Auto convert, Info, rename, delete.
  - **Libraries:** per-console Rules, the watch folder, RomM placement.
  - **Activity:** Needs attention, Running, History.
  - **Settings:** Connections, Game identification, Defaults, Converters.

## Capabilities and Constraints

- **Safety is never traded.**
  - Volume containment, path validation, verify-before-delete, never-overwrite and archive size caps are fixed.
  - Delete, overwrite and move flows always show what will happen, and refuse when they cannot be sure.
  - A file another cue sheet, playlist or multi-part set still uses is never deleted alone.
  - No design or product change may weaken any of this.
- **Works with zero configuration.** Every screen works out of the box, and anything that can be detected or defaulted is. Settings are optional and never a prerequisite for getting started. Avoid "can't tell" states: make a best guess and offer a one-click choice.
- **Plain words, no jargon.**
  - Labels, messages and errors say what happens in everyday language.
  - The internal model terms in `CONTEXT.md` (Image, Platform, Intact, Finding, Ledger, Volume, settle) are for developers only. They do not belong in the interface unless a user would already use that word.
- **Never ships or fetches protected material.** The app never downloads or bundles commercial game images, console keys, BIOS files or seeddb. Switch conversion needs the user's own `prod.keys`.
- **Formats come from the tool registry.** The backend's `/api/tools` is the one source of truth for which tools, modes and extensions exist. The interface reads it and never keeps a hand-copied list.

## Brand Commitments

- **Name:** Compressatorium.
- **Origin:** a fork of MarcTV's Docker CHD Converter. That credit stays in the README.
- **Voice:** plain, direct and specific. It says what happened and what to do next, and it never uses internal terms.

## Evidence on Hand

- **Screenshots:** generated from `shots.yml` into `docs/screenshots/` (see `docs/SCREENSHOTS.md`) and embedded in the README.
- **Documentation:** the README and the design doc at `docs/DESIGN_tool_plugin_architecture.md`.
- **Absent:** no user testimonials, usage numbers, benchmarks or press. Future work must not invent any.

## Product Principles

1. **Never lose a game.** Every destructive step is checked first and refuses when unsure.
2. **It just works.** Detect and default before asking, and never require setup to get value.
3. **One way to do the common thing.** Auto is the way to convert; options are there but out of the way.
4. **Say it plainly.** If a person wouldn't say the word, the interface doesn't either.
5. **The best integration only.** Connect deeply to the tools collectors already use (RomM, the *arr apps, DATs) rather than adding shallow ones.

## Accessibility & Inclusion

Every surface meets WCAG 2.2 AA in both light and dark themes, works fully by keyboard with visible focus, and works on a phone-width screen as well as a desktop.
