#!/usr/bin/env bash
#
# Build a throwaway fixture volume tree for the screenshot workflow.
#
# The Web UI only has anything interesting to show when at least one volume is
# mounted with files a tool can convert. Rather than ship real (copyrighted)
# game images, this creates a handful of tiny placeholder files with the right
# extensions so the file browser, tool pickers and convert panels render with
# realistic content. The files are never actually converted — the screenshot
# run captures the UI, it does not start jobs.
#
# Usage:
#   scripts/make_screenshot_fixture.sh <target-dir>
#
# The directory named by <target-dir> is used as COMPRESSATORIUM_MOUNT_ROOT; each
# immediate subdirectory becomes a volume in the UI.

set -euo pipefail

root="${1:?usage: make_screenshot_fixture.sh <target-dir>}"

mkdir -p \
  "$root/Arcade CHD" \
  "$root/GameCube" \
  "$root/Nintendo 3DS" \
  "$root/PlayStation" \
  "$root/Wii U"

# Placeholder file sizes are in KiB. `dd` is used rather than `head -c` because
# the latter's byte count is a GNU extension the BSD `head` on macOS lacks.
placeholder() { dd if=/dev/zero of="$1" bs=1024 count="$2" 2>/dev/null; }

# CHDMAN: a .cue sheet referencing a .bin data track.
printf 'FILE "game.bin" BINARY\n  TRACK 01 MODE1/2352\n    INDEX 01 00:00:00\n' \
  > "$root/Arcade CHD/street-fighter.cue"
placeholder "$root/Arcade CHD/game.bin" 2304

# A two-disc game with its .m3u playlist, for the delete-on-verify
# confirmation, which lists the playlist edit made before a source is deleted.
for disc in 1 2; do
  name="Final Quest (Disc $disc)"
  printf 'FILE "%s.bin" BINARY\n  TRACK 01 MODE2/2352\n    INDEX 01 00:00:00\n' "$name" \
    > "$root/PlayStation/$name.cue"
  placeholder "$root/PlayStation/$name.bin" 2304
done
printf 'Final Quest (Disc 1).cue\nFinal Quest (Disc 2).cue\n' > "$root/PlayStation/Final Quest.m3u"

# A three-disc game with no playlist yet, for "Create .m3u from selected discs".
for disc in 3 1 2; do
  name="Star Saga (Disc $disc)"
  printf 'FILE "%s.bin" BINARY\n  TRACK 01 MODE2/2352\n    INDEX 01 00:00:00\n' "$name" \
    > "$root/PlayStation/$name.cue"
  placeholder "$root/PlayStation/$name.bin" 512
done

# Dolphin: a GameCube/Wii disc image plus an already-compressed RVZ.
placeholder "$root/GameCube/mario-kart.iso" 4096
placeholder "$root/GameCube/zelda-wind-waker.rvz" 1024

# 3DS: a couple of ROMs.
placeholder "$root/Nintendo 3DS/pokemon-x.3ds" 2048
placeholder "$root/Nintendo 3DS/mario-3d-land.cci" 2048

# Wii U: a raw disc dump plus an already-compressed .wux. These are placeholders
# for the file list only — a real .wud is a fixed 25 GB, which is neither
# shippable nor needed to render the row.
placeholder "$root/Wii U/splatoon.wud" 3072
placeholder "$root/Wii U/mario-maker.wux" 1024

# A folder Library (#303): Platform folders the way ES-DE and Batocera lay them
# out. The placeholders carry no header, so each Image's Platform comes from its
# folder name; the loose file at the top has none and shows as Unsorted.
mkdir -p "$root/ROM Library/roms/ps2" "$root/ROM Library/roms/gc"
placeholder "$root/ROM Library/roms/ps2/ico.iso" 2048
placeholder "$root/ROM Library/roms/ps2/okami.iso" 2048
placeholder "$root/ROM Library/roms/gc/metroid-prime.iso" 2048
placeholder "$root/ROM Library/roms/mystery-disc.iso" 1024

# A GameCube disc filed under ps2 (#304): its header (game ID, then the
# GameCube magic C2339F3D at 0x1C) disagrees with its folder, so the Findings
# view shows a Platform conflict. Octal escapes: portable across printf builds.
{
  printf 'GALE01'
  dd if=/dev/zero bs=1 count=22 2>/dev/null
  printf '\302\063\237\075'
  dd if=/dev/zero bs=1 count=1056 2>/dev/null
} > "$root/ROM Library/roms/ps2/melee.iso"

# A check Rule (#305) on GBA: one ROM archive that reads back cleanly and one
# whose archive is damaged, so a run checks both through romz's own Verify (pure
# Python, no tool to install) and the Findings view shows a Not Intact row.
mkdir -p "$root/ROM Library/roms/gba"
python3 - "$root/ROM Library/roms/gba/tiny-golf.zip" <<'PY'
import sys
import zipfile

with zipfile.ZipFile(sys.argv[1], "w", zipfile.ZIP_DEFLATED) as archive:
    archive.writestr("Tiny Golf.gba", bytes(range(256)) * 64)
PY
{ printf 'PK\003\004'; dd if=/dev/zero bs=1 count=60 2>/dev/null; } \
  > "$root/ROM Library/roms/gba/pocket-racer.zip"

echo "Fixture volume created under: $root"
